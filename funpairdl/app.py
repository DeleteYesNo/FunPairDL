"""Application bootstrap: wires together Qt, asyncio, and FastAPI."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# Force UTF-8 on Windows (equivalent to python -X utf8)
os.environ.setdefault("PYTHONUTF8", "1")

# Chromium flags for QWebEngine — must be set BEFORE QWebEngineProfile is created
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", " ".join([
    "--enable-features=BackForwardCache",       # Instant back/forward navigation
    "--back-forward-cache-size=3",              # Cache up to 3 pages for back/forward
    "--disk-cache-size=268435456",              # 256 MB disk cache
    "--enable-quic",                            # QUIC protocol for faster HTTPS
    "--disable-renderer-backgrounding",         # Keep background tabs' renderers active
    "--disable-background-timer-throttling",    # Don't throttle JS timers in background tabs
    "--disable-backgrounding-occluded-windows", # Don't throttle occluded windows
    # GPU rendering acceleration
    "--ignore-gpu-blocklist",                   # Use GPU even if driver is blocklisted
    "--enable-gpu-rasterization",               # Rasterize page tiles on GPU
    "--enable-zero-copy",                       # Zero-copy texture uploads to GPU
    "--num-raster-threads=4",                   # Parallel raster threads
    "--enable-smooth-scrolling",                # Smooth scroll animations
]))

# Enable CDP (Chrome DevTools Protocol) for cookie extraction.
# PySide6's cookieAdded signal is broken — CDP is the reliable alternative.
#
# The port is PROBED, not hardcoded: Windows (Hyper-V/WinNAT) reserves a
# different block of "excluded" ports on every boot, and when 9223 lands in
# one, Chromium's devtools bind fails with WSAEACCES (0x271D) and the whole
# CDP cookie pipeline silently dies (exactly what happened 2026-08-06 —
# 9181-9280 got reserved). Bind-test candidates and use the first that works;
# an explicit QTWEBENGINE_REMOTE_DEBUGGING in the environment still wins.
def _pick_cdp_port() -> int:
    import socket
    for port in (9223, 9723, 9923, 10223, 10723, 11223):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
            finally:
                s.close()
            return port
        except OSError:
            continue
    return 0


if "QTWEBENGINE_REMOTE_DEBUGGING" not in os.environ:
    _cdp_port = _pick_cdp_port()
    if _cdp_port:
        os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = str(_cdp_port)

from PySide6.QtWidgets import QApplication

from funpairdl.api.server import start_api_server
from funpairdl.core.queue_manager import QueueManager
from funpairdl.gui.main_window import MainWindow
from funpairdl.persistence.queue_store import QueueStore
from funpairdl.persistence.settings import Settings
from funpairdl.utils.async_bridge import install_qasync_loop, start_worker_loop
from funpairdl.utils.logging_setup import setup_logging

logger = logging.getLogger("funpairdl.app")


def run():
    setup_logging()
    logger.info("Starting FunPairDL...")

    try:
        _run_app()
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        # Also write to a crash file for pythonw.exe where there's no console
        try:
            from funpairdl.constants import CONFIG_DIR
            crash_file = CONFIG_DIR / "crash.log"
            import traceback
            with open(crash_file, "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise


def _run_app():
    # Load settings
    settings = Settings.load()
    settings.save()  # Create config file if it doesn't exist

    # Create Qt application
    app = QApplication(sys.argv)
    app.setApplicationName("FunPairDL")
    app.setQuitOnLastWindowClosed(False)

    # Install qasync event loop (GUI thread). Heavy async work does NOT run
    # here: downloads live on the dl-thread, the API server and browser
    # bridge on the worker loop, queue saves on the store's writer thread.
    loop = install_qasync_loop(app)
    worker_loop = start_worker_loop()

    # Create queue manager
    from pathlib import Path
    qm = QueueManager(
        download_dir=Path(settings.download_dir),
        num_segments=settings.max_segments,
    )

    # Load saved queue. Saving is debounced onto the store's writer thread —
    # snapshot_dicts() takes the queue lock, so callers on any thread are safe.
    store = QueueStore()
    store.start_writer()
    qm.pairs = store.load()
    qm.on_save_needed = lambda: store.request_save(qm.snapshot_dicts)
    qm.archive_sink = store.append_archive
    # Move surplus completed pairs out of the live queue (window not created
    # yet — callbacks are still None, so this is just a cheap list split).
    archived = qm.archive_completed()
    if archived:
        logger.info("Archived %d completed pairs at startup", archived)

    # Create and show main window
    window = MainWindow(qm, settings)
    window.show()

    # Schedule async tasks
    async def _startup():
        await qm.start()
        logger.info("Queue manager started")

    async def _shutdown():
        # Save browser session + cookies
        try:
            if hasattr(window, "browser"):
                window.browser.save_session()
                # Give the async CDP cookie save a moment to complete
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Failed to save browser session on shutdown: %s", e)
        store.save_now(qm.snapshot_dicts)
        await qm.stop()
        store.stop_writer()
        logger.info("Queue manager stopped, queue saved")

    async def _run_auto_save():
        while True:
            await asyncio.sleep(30)
            # Each step gets its own try so one failure can never kill the
            # loop (the old single try/except silently ended persistence
            # for the rest of the run on first error).
            try:
                store.request_save(qm.snapshot_dicts)
            except Exception as e:
                logger.error("Auto-save request failed: %s", e, exc_info=True)
            # Also persist the live browser session so a restart restores
            # the latest tabs/scroll — not whatever was last saved at quit.
            # The shutdown coroutine is unreliable (the qasync loop stops
            # right after aboutToQuit), so we snapshot periodically instead.
            # save_session dirty-checks, so unchanged sessions cost nothing.
            try:
                if hasattr(window, "browser"):
                    window.browser.save_session()
            except Exception as e:
                logger.debug("Periodic browser session save failed: %s", e)

    loop.create_task(_startup())
    loop.create_task(_run_auto_save())

    # API server runs on the worker loop — never on the GUI thread.
    def _api_done(fut):
        try:
            fut.result()
        except Exception as e:
            logger.error("API server failed: %s", e, exc_info=True)

    api_future = asyncio.run_coroutine_threadsafe(
        start_api_server(qm, settings.api_host, settings.api_port), worker_loop
    )
    api_future.add_done_callback(_api_done)

    # Handle app quit
    app.aboutToQuit.connect(lambda: loop.create_task(_shutdown()))

    # Run event loop
    with loop:
        loop.run_forever()

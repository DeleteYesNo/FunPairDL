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
os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "9223")

from PySide6.QtWidgets import QApplication

from funpairdl.api.server import start_api_server
from funpairdl.core.queue_manager import QueueManager
from funpairdl.gui.main_window import MainWindow
from funpairdl.persistence.queue_store import QueueStore
from funpairdl.persistence.settings import Settings
from funpairdl.utils.async_bridge import install_qasync_loop
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

    # Install qasync event loop
    loop = install_qasync_loop(app)

    # Create queue manager
    from pathlib import Path
    qm = QueueManager(
        download_dir=Path(settings.download_dir),
        num_segments=settings.max_segments,
    )

    # Load saved queue
    store = QueueStore()
    qm.pairs = store.load()
    qm.on_save_needed = lambda: store.save(qm.pairs)

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
        store.save(qm.pairs)
        await qm.stop()
        logger.info("Queue manager stopped, queue saved")

    async def _run_api_server():
        try:
            await start_api_server(qm, settings.api_host, settings.api_port)
        except Exception as e:
            logger.error("API server failed: %s", e, exc_info=True)

    async def _run_auto_save():
        try:
            while True:
                await asyncio.sleep(30)
                store.save(qm.pairs)
        except Exception as e:
            logger.error("Auto-save failed: %s", e, exc_info=True)

    loop.create_task(_startup())
    loop.create_task(_run_api_server())
    loop.create_task(_run_auto_save())

    # Handle app quit
    app.aboutToQuit.connect(lambda: loop.create_task(_shutdown()))

    # Run event loop
    with loop:
        loop.run_forever()

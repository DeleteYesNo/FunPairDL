"""Bridge between PySide6 Qt event loop and asyncio."""

from __future__ import annotations

import asyncio
import threading


def install_qasync_loop(app):
    """Install qasync event loop that integrates Qt and asyncio."""
    import qasync
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    return loop


_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_lock = threading.Lock()


def start_worker_loop(name: str = "api-worker") -> asyncio.AbstractEventLoop:
    """Start (once) a daemon thread running its own asyncio loop.

    Hosts the FastAPI server, QWebChannel bridge handlers, and CDP cookie
    sync — network work that must never share the Qt main thread.
    """
    global _worker_loop
    with _worker_lock:
        if _worker_loop is not None and _worker_loop.is_running():
            return _worker_loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name=name, daemon=True)
        thread.start()
        _worker_loop = loop
        return loop


def get_worker_loop() -> asyncio.AbstractEventLoop | None:
    """The worker loop started by start_worker_loop(), or None."""
    return _worker_loop

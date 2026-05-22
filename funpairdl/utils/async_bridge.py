"""Bridge between PySide6 Qt event loop and asyncio."""

from __future__ import annotations

import asyncio
import sys


def install_qasync_loop(app):
    """Install qasync event loop that integrates Qt and asyncio."""
    import qasync
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    return loop

"""Lightweight server mode: API + download engine, no GUI.

Runs the FastAPI server and QueueManager using pure asyncio,
without any PySide6/Qt dependency. Designed for use with
the browser extension.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")

from funpairdl.api.server import start_api_server
from funpairdl.core.queue_manager import QueueManager
from funpairdl.persistence.queue_store import QueueStore
from funpairdl.persistence.settings import Settings
from funpairdl.utils.logging_setup import setup_logging

logger = logging.getLogger("funpairdl.server")


def run():
    setup_logging()
    logger.info("Starting FunPairDL Server (lightweight mode)...")

    settings = Settings.load()
    settings.save()

    qm = QueueManager(
        download_dir=Path(settings.download_dir),
        num_segments=settings.max_segments,
    )

    store = QueueStore()
    qm.pairs = store.load()
    qm.on_save_needed = lambda: store.save(qm.pairs)

    async def _main():
        await qm.start()
        logger.info("Queue manager started")
        logger.info(
            "API server on http://%s:%d  (Ctrl+C to stop)",
            settings.api_host,
            settings.api_port,
        )

        # Auto-save task
        async def _auto_save():
            while True:
                await asyncio.sleep(30)
                store.save(qm.pairs)

        save_task = asyncio.create_task(_auto_save())

        # Graceful shutdown
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGINT, _signal_handler)
            loop.add_signal_handler(signal.SIGTERM, _signal_handler)

        # Run API server in background
        server_task = asyncio.create_task(
            start_api_server(qm, settings.api_host, settings.api_port)
        )

        try:
            if sys.platform == "win32":
                # On Windows, asyncio signal handlers don't work well.
                # Poll for KeyboardInterrupt instead.
                while not stop_event.is_set():
                    await asyncio.sleep(1)
            else:
                await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            logger.info("Shutting down...")
            save_task.cancel()
            server_task.cancel()
            store.save(qm.pairs)
            await qm.stop()
            logger.info("Queue saved. Goodbye.")

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass

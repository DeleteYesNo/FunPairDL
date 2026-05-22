from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from funpairdl.api.routes import router, set_queue_manager
from funpairdl.core.queue_manager import QueueManager

logger = logging.getLogger("funpairdl.api.server")


def create_app(queue_manager: QueueManager) -> FastAPI:
    app = FastAPI(title="FunPairDL", docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    set_queue_manager(queue_manager)

    return app


async def start_api_server(
    queue_manager: QueueManager,
    host: str = "127.0.0.1",
    port: int = 9172,
) -> None:
    app = create_app(queue_manager)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,  # Disable uvicorn's own logging (crashes with pythonw.exe)
    )
    server = uvicorn.Server(config)
    logger.info("API server starting on %s:%d", host, port)
    await server.serve()

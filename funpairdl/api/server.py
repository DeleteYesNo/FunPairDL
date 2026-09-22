from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from funpairdl.api.routes import router, set_queue_manager
from funpairdl.core.queue_manager import QueueManager

logger = logging.getLogger("funpairdl.api.server")

# Who may call the API. Its real client is the embedded browser's bridge
# (BridgeCore, aiohttp in this process — no Origin header); the retired
# Chrome extension called it from its background/popup, which host
# permissions exempt from CORS. No web page ever needs it, yet /config hands
# out the gofile token and /resolve overwrites the forum cookies — so there
# are no CORS headers at all, and anything a web page could have sent is
# refused before a handler runs (a no-cors "simple" POST such as
# /pair/{id}/remove would still execute even though the page can't read the
# reply). Local processes aren't in scope: they can read config.json anyway.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://")


def _host_name(host_header: str) -> str:
    """Host header without the port ("[::1]:9172" → "::1")."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if ":" in h else h


def refusal_reason(headers: dict[str, str], allowed_hosts: frozenset[str]) -> str | None:
    """Why a request must be refused, or None to let it through.

    `headers` keys are lower-case. Pure — the middleware and tests share it.
    """
    # DNS rebinding: a page on attacker.example re-resolved to 127.0.0.1 is
    # same-origin with us from the browser's view, but its Host still says
    # attacker.example.
    if _host_name(headers.get("host", "")) not in allowed_hosts:
        return "host not allowed"
    origin = headers.get("origin")
    if origin is not None:
        # Browsers always stamp Origin on cross-origin fetches and on every
        # POST, and pages can't forge it. Only an extension origin is a
        # legitimate caller ("null" = sandboxed frame / file page).
        return None if origin.startswith(_EXTENSION_ORIGINS) else "cross-origin request"
    # No Origin: GETs from <img>/<script>/navigations. Their responses are
    # opaque, but refuse them anyway when the browser says a site sent them.
    if headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return "cross-site request"
    return None


class LocalClientGuard:
    """ASGI middleware applying refusal_reason to every HTTP request."""

    def __init__(self, app, allowed_hosts: frozenset[str]):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            reason = refusal_reason(headers, self.allowed_hosts)
            if reason:
                logger.warning("API: refused %s %s (%s; origin=%s host=%s)",
                               scope.get("method"), scope.get("path"), reason,
                               headers.get("origin", "-"), headers.get("host", "-"))
                await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(queue_manager: QueueManager, host: str = "127.0.0.1") -> FastAPI:
    app = FastAPI(title="FunPairDL", docs_url=None, redoc_url=None)

    # A non-loopback api_host set in config.json is reachable by its own name
    # too; a wildcard bind adds nothing (that would readmit DNS rebinding).
    allowed = set(_LOOPBACK_HOSTS)
    if host and host not in ("0.0.0.0", "::"):
        allowed.add(host.strip("[]").lower())
    app.add_middleware(LocalClientGuard, allowed_hosts=frozenset(allowed))

    app.include_router(router)
    set_queue_manager(queue_manager)

    return app


async def start_api_server(
    queue_manager: QueueManager,
    host: str = "127.0.0.1",
    port: int = 9172,
) -> None:
    app = create_app(queue_manager, host)
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

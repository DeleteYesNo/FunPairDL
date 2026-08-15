"""GoFile's X-Website-Token, computed by running GoFile's own generator.

GoFile used to accept a static website token published in its JS bundle and
passed as a `?wt=` query parameter. That token is now a decoy: the site sends
`X-Website-Token: generateWT(accountToken)` as a *header*, where generateWT
lives in an obfuscated bundle. Requests using the old contract come back as
HTTP 401 `error-notPremium` — a misleading error that has nothing to do with
account tier (a guest token and a paid token fail identically).

Rather than reverse-engineering the obfuscated function — whose salt GoFile
rotates, and which would silently break again on every rotation — we fetch
their current bundle and execute it. PySide6 ships QJSEngine, so this needs no
new dependency and no web view.

Verified against the real bundle (2026-07-22):

  * the result is stable within a 4-hour window aligned to the epoch, and
    changes across the boundary — so it is cached per window, never longer;
  * it mixes in navigator.userAgent and navigator.language, so the User-Agent
    used here MUST be the one actually sent on the request, or the server
    recomputes a different value;
  * it does NOT depend on the old static `appdata.wt`, which is why scraping
    that value is pointless.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import aiohttp

logger = logging.getLogger("funpairdl.providers.gofile_wt")

WT_JS_URL = "https://gofile.io/dist/js/wt.obf.js"

# The token rotates on absolute 4-hour boundaries.
WT_WINDOW_SECONDS = 4 * 60 * 60
# The bundle itself changes rarely; re-fetching it per call would be wasteful,
# but it must not be pinned for so long that a salt rotation goes unnoticed.
JS_CACHE_TTL_SECONDS = 60 * 60

_lock = threading.Lock()
_js_cache: tuple[str, float] | None = None        # (source, fetched_at)
_token_cache: dict[tuple[str, str, str, int], str] = {}
# QCoreApplication is not garbage-collected while referenced here; QJSEngine
# aborts the process outright if no application object exists.
_qt_app_keepalive: list = []


def current_window(now: float | None = None) -> int:
    """Index of the 4-hour window the token is valid for."""
    return int((time.time() if now is None else now) // WT_WINDOW_SECONDS)


def invalidate() -> None:
    """Drop cached tokens and the cached bundle.

    Called when GoFile rejects a request, so a rotated salt or a new bundle is
    picked up on the retry instead of being served from cache until the TTL.
    """
    global _js_cache
    with _lock:
        _js_cache = None
        _token_cache.clear()


def _ensure_qt_app():
    """QJSEngine requires a QCoreApplication to exist — without one the
    process dies silently rather than raising. The GUI always has one; the
    headless server and the tests may not, so create a bare one (its event
    loop is never started). Qt only allows that on the main thread.
    """
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is not None:
        return app
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "GoFile's website token needs a QCoreApplication, which can only "
            "be created on the main thread"
        )
    app = QCoreApplication([])
    _qt_app_keepalive.append(app)
    return app


def compute_token(js_source: str, account_token: str,
                  user_agent: str, language: str) -> str:
    """Run GoFile's generateWT for `account_token`. Pure apart from Qt setup.

    Constructing QJSEngine with no QCoreApplication aborts the process without
    raising, so the guard runs here too — callers that reach this directly
    (tests, any future caller) get an exception instead of a dead interpreter.
    On the worker-thread path website_token() has already created it, making
    this a no-op.
    """
    from PySide6.QtQml import QJSEngine

    _ensure_qt_app()
    engine = QJSEngine()
    # The bundle expects a browser environment. Only these fields are read;
    # anything else it touches must merely exist.
    boot = engine.evaluate(
        "var window = this;"
        "var document = {};"
        "var navigator = {"
        f"  userAgent: {_js_string(user_agent)},"
        f"  language: {_js_string(language)}"
        "};"
        "var appdata = { wt: '', apiServer: 'api' };"
    )
    if boot.isError():
        raise RuntimeError(f"GoFile wt bootstrap failed: {boot.toString()}")

    loaded = engine.evaluate(js_source)
    if loaded.isError():
        raise RuntimeError(f"GoFile wt bundle failed to evaluate: {loaded.toString()}")

    fn = engine.globalObject().property("generateWT")
    if not fn.isCallable():
        raise RuntimeError(
            "GoFile wt bundle no longer defines generateWT — the site's token "
            "scheme changed"
        )
    result = fn.call([engine.toScriptValue(account_token)])
    if result.isError():
        raise RuntimeError(f"generateWT raised: {result.toString()}")
    token = result.toString()
    if not token or token == "undefined":
        raise RuntimeError("generateWT returned nothing")
    return token


def _js_string(value: str) -> str:
    """Embed a Python string as a JS string literal."""
    import json
    return json.dumps(value)


async def _fetch_js(session: aiohttp.ClientSession, user_agent: str) -> str:
    global _js_cache
    with _lock:
        if _js_cache and (time.monotonic() - _js_cache[1]) < JS_CACHE_TTL_SECONDS:
            return _js_cache[0]
    async with session.get(
        WT_JS_URL, headers={"User-Agent": user_agent},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        source = await resp.text()
    if "generateWT" not in source:
        raise RuntimeError(f"{WT_JS_URL} no longer contains generateWT")
    with _lock:
        _js_cache = (source, time.monotonic())
    return source


async def website_token(
    session: aiohttp.ClientSession,
    account_token: str,
    user_agent: str,
    language: str = "en-US",
) -> str:
    """Return the X-Website-Token for `account_token`, cached per 4h window."""
    key = (account_token, user_agent, language, current_window())
    with _lock:
        cached = _token_cache.get(key)
    if cached:
        return cached

    source = await _fetch_js(session, user_agent)
    # QJSEngine must not be constructed before a QCoreApplication exists, and
    # the application object can only be created on the main thread — so do
    # that here, then evaluate off-loop (43 KB of obfuscated JS is not free).
    _ensure_qt_app()
    token = await asyncio.to_thread(
        compute_token, source, account_token, user_agent, language,
    )
    with _lock:
        _token_cache[key] = token
    logger.info("GoFile website token computed for window %d", key[3])
    return token

"""Embedded tabbed browser widget using QWebEngineView.

Provides an in-app browser for EroScripts with QWebChannel bridge,
replacing the need for a separate Chrome/Brave extension.
Supports multiple tabs for efficient browsing workflow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QShortcut, QKeySequence
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QLineEdit,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("funpairdl.gui.browser")

API_URL = "http://127.0.0.1:9172/api"

_TOPIC_ID_RE = re.compile(r"/t/[^/]+/(\d+)")


def _topic_id_from_url(url: str) -> str | None:
    """Discourse topic id from a URL (matches content.js _topicIdFromUrl)."""
    m = _TOPIC_ID_RE.search(url or "")
    return m.group(1) if m else None

# Serializes read-modify-write cycles on Settings within this module.
# Bridge handlers now run on the api-worker thread while save_session runs
# on the GUI thread — without this, concurrent Settings.load()+save() could
# clobber each other's fields.
_settings_lock = threading.Lock()

# ─── QWebChannel Bridge ───


class _BridgeDispatcher(QObject):
    """Long-lived GUI-thread relay for worker-loop bridge responses.

    Worker-loop handlers must NEVER emit on a per-tab BrowserBridge: the GUI
    thread can deleteLater that same QObject on tab close, and a cross-thread
    emit into a half-destroyed object is a use-after-free that try/except
    RuntimeError does not close (audit [6] / finding B). Instead the worker
    emits responseReady(bridge_key, cb_id, payload) on THIS object — created
    on the GUI thread and parented to BrowserWidget, so it outlives every tab.
    The queued slot then runs on the GUI thread and forwards to the still-
    registered per-tab bridge (looked up by key); if the tab was closed the
    key is already gone and the response is dropped.
    """

    responseReady = Signal(str, str, str)  # bridge_key, callback_id, payload_json
    # Batch overlay "register-autoclose": (topic_url, pair_ids_json). Emitted
    # from the worker loop, delivered queued on the GUI thread where
    # BrowserWidget tracks download completion and closes the topic's tabs.
    autocloseRequested = Signal(str, str)

    def __init__(self, bridges: dict, parent=None):
        super().__init__(parent)
        self._bridges = bridges
        # AutoConnection: emitted from the worker thread, delivered queued on
        # this object's (GUI) thread.
        self.responseReady.connect(self._deliver)

    @Slot(str, str, str)
    def _deliver(self, bridge_key: str, callback_id: str, payload: str):
        # GUI thread. _close_tab removes the key BEFORE deleteLater, and both
        # run on this thread, so a missing key means the tab is gone — drop.
        bridge = self._bridges.get(bridge_key)
        if bridge is None:
            return
        bridge.messageResponse.emit(callback_id, payload)


class BridgeCore:
    """Shared bridge backend: cookie store, CDP sync, API loopback calls.

    One instance per BrowserWidget; every tab's BrowserBridge delegates
    here. Handles the same message types as background.js, calling the
    local FastAPI backend. All async work runs on the api-worker loop
    (funpairdl.utils.async_bridge.get_worker_loop()), never on the Qt
    main thread's qasync loop.
    """

    # CDP port for cookie extraction (set via QTWEBENGINE_REMOTE_DEBUGGING)
    CDP_PORT = int(os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING", "9223"))

    def __init__(self, cookie_store=None, owner=None):
        self._cookie_store = cookie_store
        self._cookies: dict[str, str] = {}
        # -inf: the first freshness check must always count as stale, even
        # on a freshly booted machine where time.monotonic() itself is < 30.
        self._last_cookie_persist: float = float("-inf")
        # Gate for the 30s cookie-sync freshness check + inflight flag —
        # touched from the GUI thread (save_session) and the worker loop.
        self._gate_lock = threading.Lock()
        self._cdp_sync_inflight = False
        # Single shared aiohttp session for loopback API + CDP traffic.
        # Created lazily ON the worker loop (never per-message).
        self._session = None
        self._storage: dict = {}

        # Per-tab bridge registry + long-lived GUI-thread response relay.
        # Worker-loop handlers emit replies on the dispatcher (which outlives
        # every tab), never on a per-tab BrowserBridge — see _BridgeDispatcher
        # (audit [6] / finding B). Touched only on the GUI thread
        # (register/unregister in create_tab/_close_tab, lookup in _deliver),
        # so no lock is needed.
        self._bridges: dict[str, BrowserBridge] = {}
        self._bridge_seq = 0
        self._dispatcher = _BridgeDispatcher(self._bridges, parent=owner)

        # Try signal-based approach (broken in many PySide6 versions)
        if cookie_store:
            cookie_store.cookieAdded.connect(self._on_cookie_added)
            cookie_store.cookieRemoved.connect(self._on_cookie_removed)
            cookie_store.loadAllCookies()

        # Schedule CDP-based cookie extraction as reliable fallback
        QTimer.singleShot(3000, self._schedule_cdp_cookie_sync)
        # Restore cookies from backup 5s after init (CDP needs the browser loaded)
        QTimer.singleShot(5000, lambda: self.spawn(self.restore_cookies_if_needed()))

    def spawn(self, coro) -> None:
        """Run a coroutine on the api-worker loop (fire-and-forget).

        Falls back to the current thread's loop only if the worker loop
        does not exist (e.g. isolated tests).
        """
        from funpairdl.utils.async_bridge import get_worker_loop
        loop = get_worker_loop()
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        try:
            asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()

    # ─── Per-tab bridge registry (GUI thread only) ───

    def register_bridge(self, bridge) -> str:
        """Register a per-tab bridge and return its routing key (GUI thread)."""
        self._bridge_seq += 1
        key = str(self._bridge_seq)
        bridge._key = key
        self._bridges[key] = bridge
        return key

    def unregister_bridge(self, key: str) -> None:
        """Drop a per-tab bridge so late worker replies are discarded (GUI thread)."""
        self._bridges.pop(key, None)

    def dispatch_response(self, bridge_key: str, callback_id: str, data: dict) -> None:
        """Deliver a handler reply to the originating tab.

        Safe to call from the worker loop: emits on the long-lived GUI-thread
        dispatcher (auto-queued), which forwards to the still-registered
        per-tab bridge. Never touches a per-tab BrowserBridge off the GUI
        thread (finding B).
        """
        self._dispatcher.responseReady.emit(bridge_key, callback_id, json.dumps(data))

    def _get_session(self):
        """Shared aiohttp session, created lazily on the calling loop.

        Only ever called from coroutines already running on the worker
        loop, so the session is bound to that loop. No awaits between the
        check and the assignment — no duplicate-creation race.
        """
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    @staticmethod
    def _cookie_str(field) -> str:
        # Newer PySide6 returns str; older returns QByteArray. Handle both.
        if isinstance(field, str):
            return field
        try:
            return bytes(field).decode("utf-8", errors="ignore")
        except TypeError:
            return str(field)

    def _on_cookie_added(self, cookie):
        domain = self._cookie_str(cookie.domain())
        name = self._cookie_str(cookie.name())
        value = self._cookie_str(cookie.value())
        self._cookies[f"{domain}|{name}"] = f"{name}={value}"
        if "eroscripts" in domain:
            logger.info("Cookie captured (signal): %s (domain=%s)", name, domain)

    def _on_cookie_removed(self, cookie):
        domain = self._cookie_str(cookie.domain())
        name = self._cookie_str(cookie.name())
        self._cookies.pop(f"{domain}|{name}", None)

    def _get_eroscripts_cookies(self) -> str:
        parts = []
        # Copy: the GUI thread's cookieAdded/Removed signals mutate the dict
        # while worker-loop handlers iterate it.
        for key, val in list(self._cookies.items()):
            if "eroscripts.com" in key.split("|")[0]:
                parts.append(val)
        return "; ".join(parts)

    # ─── CDP-based cookie extraction ───

    async def _cdp_get_all_cookies(self) -> list[dict]:
        """Fetch raw cookie dicts from CDP (includes domain, httpOnly, etc.)."""
        import aiohttp

        try:
            session = self._get_session()
            async with session.get(
                f"http://127.0.0.1:{self.CDP_PORT}/json",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                targets = await resp.json()

            if not targets:
                return []

            ws_url = targets[0].get("webSocketDebuggerUrl")
            if not ws_url:
                return []

            async with session.ws_connect(ws_url) as ws:
                await ws.send_json({
                    "id": 1,
                    "method": "Network.getAllCookies",
                })
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                cookies = msg.get("result", {}).get("cookies", [])
                logger.debug("CDP: extracted %d cookies", len(cookies))
                return cookies
        except Exception as e:
            logger.debug("CDP cookie extraction failed: %s", e)
            return []

    async def _cdp_set_cookies(self, cookies: list[dict]) -> bool:
        """Restore cookies into the browser via CDP Network.setCookies."""
        import aiohttp

        if not cookies:
            return False
        try:
            session = self._get_session()
            async with session.get(
                f"http://127.0.0.1:{self.CDP_PORT}/json",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                targets = await resp.json()

            if not targets:
                return False
            ws_url = targets[0].get("webSocketDebuggerUrl")
            if not ws_url:
                return False

            async with session.ws_connect(ws_url) as ws:
                await ws.send_json({
                    "id": 1,
                    "method": "Network.setCookies",
                    "params": {"cookies": cookies},
                })
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                ok = "error" not in msg
                if ok:
                    logger.info("CDP: restored %d cookies", len(cookies))
                else:
                    logger.warning("CDP setCookies error: %s", msg.get("error"))
                return ok
        except Exception as e:
            logger.warning("CDP cookie restore failed: %s", e)
            return False

    def _schedule_cdp_cookie_sync(self):
        """Schedule async CDP cookie extraction (called from QTimer).

        Unconditional (no freshness gate) but still inflight-guarded so it
        can't overlap a background sync.
        """
        with self._gate_lock:
            if self._cdp_sync_inflight:
                return
            self._cdp_sync_inflight = True
        self.spawn(self._cdp_cookie_sync_guarded())

    def maybe_background_cookie_sync(self, max_age: float = 30.0) -> None:
        """Fire-and-forget CDP cookie sync if the last one is stale.

        Never awaited on a request path — check-status/send-pair/resolve-url
        must not pay CDP round trips before responding. The gate is locked:
        callers race between the GUI thread and the worker loop, and at most
        one sync runs at a time.
        """
        with self._gate_lock:
            if self._cdp_sync_inflight:
                return
            if time.monotonic() - self._last_cookie_persist <= max_age:
                return
            self._cdp_sync_inflight = True
        self.spawn(self._cdp_cookie_sync_guarded())

    async def _cdp_cookie_sync_guarded(self):
        try:
            await self._cdp_cookie_sync()
        except Exception as e:
            logger.debug("Background CDP cookie sync failed: %s", e)
        finally:
            with self._gate_lock:
                self._cdp_sync_inflight = False

    async def _sync_cookies_now(self, cap: float = 2.0) -> None:
        """Await one CDP cookie sync with a short cap (worker loop only).

        Called when the in-memory cookie store is EMPTY at send time so a
        send/resolve doesn't proceed on only the stale persisted cookies
        (audit [10] / finding C). Rides an already-inflight background sync
        instead of launching a duplicate; falls through (returns) on timeout
        or error so the caller still proceeds with whatever it has.
        """
        try:
            with self._gate_lock:
                start_new = not self._cdp_sync_inflight
                if start_new:
                    self._cdp_sync_inflight = True
            if start_new:
                try:
                    await asyncio.wait_for(self._cdp_cookie_sync(), cap)
                finally:
                    with self._gate_lock:
                        self._cdp_sync_inflight = False
            else:
                # A background sync is already running — wait for the gate to
                # clear, capped, then let the caller re-read cookies.
                deadline = time.monotonic() + cap
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                    with self._gate_lock:
                        if not self._cdp_sync_inflight:
                            break
        except Exception as e:
            logger.debug("On-demand cookie sync (cap=%.1fs) fell through: %s", cap, e)

    def flush_cookies_now(self, min_age: float = 0.0):
        """Force a CDP cookie sync, bypassing the 30s freshness gate.

        Only the inflight guard remains. Returns the concurrent.futures.Future
        (or None if a sync is already running / no worker loop). save_session
        calls this every 30s and at quit so cookies from the last <=30s reach
        the settings jar — the freshness gate would otherwise make the final
        save skip the sync and drop that window (audit [10] / finding D).
        Pass min_age>0 to skip when the last sync is more recent than that.
        """
        from funpairdl.utils.async_bridge import get_worker_loop
        loop = get_worker_loop()
        if loop is None or not loop.is_running():
            return None
        with self._gate_lock:
            if self._cdp_sync_inflight:
                return None
            if min_age > 0 and (time.monotonic() - self._last_cookie_persist) < min_age:
                return None
            self._cdp_sync_inflight = True
        return asyncio.run_coroutine_threadsafe(self._cdp_cookie_sync_guarded(), loop)

    async def _cdp_cookie_sync(self):
        """Extract cookies via CDP (ONE dump) and persist EroScripts cookies.

        The single getAllCookies result feeds both the in-memory cookie
        store and the raw jar backup — previously this dumped twice.
        """
        raw = await self._cdp_get_all_cookies()
        if raw:
            for cookie in raw:
                domain = cookie.get("domain", "")
                name = cookie.get("name", "")
                value = cookie.get("value", "")
                self._cookies[f"{domain}|{name}"] = f"{name}={value}"
            logger.info("CDP: updated cookie store with %d cookies", len(raw))
            # Also save full cookie details for EroScripts (for restore on startup)
            ero_cookies = [c for c in raw if "eroscripts" in c.get("domain", "")]
            if ero_cookies:
                from funpairdl.persistence.settings import Settings
                with _settings_lock:
                    settings = Settings.load()
                    if settings.eroscripts_cookie_jar != ero_cookies:
                        settings.eroscripts_cookie_jar = ero_cookies
                        settings.save()
        self._persist_eroscripts_cookies()

    async def restore_cookies_if_needed(self):
        """Restore EroScripts cookies from settings backup if missing in browser.

        Called once on startup after the browser profile is ready. Runs on
        the worker loop as a fire-and-forget task, so it must never leak an
        exception (nobody awaits its future).
        """
        try:
            await self._restore_cookies_if_needed()
        except Exception as e:
            logger.warning("Cookie restore failed: %s", e)

    async def _restore_cookies_if_needed(self):
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if not settings.eroscripts_cookie_jar:
            return

        # Check if browser already has EroScripts cookies
        raw = await self._cdp_get_all_cookies()
        has_ero = any("eroscripts" in c.get("domain", "") for c in raw)
        if has_ero:
            logger.debug("Browser already has EroScripts cookies, skip restore")
            return

        # Restore from backup
        logger.info(
            "No EroScripts cookies in browser — restoring %d from backup",
            len(settings.eroscripts_cookie_jar),
        )
        # CDP setCookies needs 'url' or 'domain' per cookie.
        # Ensure each cookie has the required fields.
        restore_cookies = []
        for c in settings.eroscripts_cookie_jar:
            entry = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".eroscripts.com"),
                "path": c.get("path", "/"),
            }
            if c.get("httpOnly"):
                entry["httpOnly"] = True
            if c.get("secure"):
                entry["secure"] = True
            if c.get("sameSite"):
                entry["sameSite"] = c["sameSite"]
            if c.get("expires", -1) > 0:
                entry["expires"] = c["expires"]
            restore_cookies.append(entry)

        await self._cdp_set_cookies(restore_cookies)

    def _persist_eroscripts_cookies(self):
        """Save EroScripts cookies to settings.

        ALWAYS latches _last_cookie_persist — even when nothing was found —
        so a broken/empty cookie store can't make every send re-pay the full
        CDP sync (the old never-latching gate bug, audit [10]).
        """
        try:
            cookie_str = self._get_eroscripts_cookies()
            if not cookie_str:
                logger.debug("No EroScripts cookies to persist (total cookies in store: %d)", len(self._cookies))
                return
            from funpairdl.persistence.settings import Settings
            with _settings_lock:
                settings = Settings.load()
                if settings.eroscripts_cookies != cookie_str:
                    settings.eroscripts_cookies = cookie_str
                    settings.save()
                    logger.info("Auto-saved EroScripts cookies (%d chars)", len(cookie_str))
        finally:
            with self._gate_lock:
                self._last_cookie_persist = time.monotonic()

    async def handle(self, msg_type: str, data: dict, callback_id: str, respond):
        """Handle one bridge message on the worker loop.

        `respond(callback_id, dict)` delivers the reply back to the
        ORIGINATING tab only (per-tab BrowserBridge signal emit).
        NOTE: never awaits a CDP cookie sync — stale cookies trigger a
        fire-and-forget background sync instead (audit [10]).
        """
        import aiohttp

        try:
            if msg_type == "check-status":
                # Periodically refresh cookies via CDP in the background
                self.maybe_background_cookie_sync()
                s = self._get_session()
                async with s.get(
                    f"{API_URL}/status", timeout=aiohttp.ClientTimeout(total=3)
                ) as r:
                    resp = await r.json()
                    respond(callback_id, {"online": True, **resp})
                return

            if msg_type == "send-pair":
                cookie_str = self._get_eroscripts_cookies()
                if not cookie_str:
                    # Empty in-memory store: try one capped CDP sync before
                    # falling back to the (possibly stale) persisted cookies
                    # instead of sending with none (finding C).
                    await self._sync_cookies_now(2.0)
                    cookie_str = self._get_eroscripts_cookies()
                logger.info("send-pair: EroScripts cookies = %d chars, total cookies = %d",
                            len(cookie_str), len(self._cookies))
                if cookie_str:
                    from funpairdl.persistence.settings import Settings
                    with _settings_lock:
                        settings = Settings.load()
                        if settings.eroscripts_cookies != cookie_str:
                            settings.eroscripts_cookies = cookie_str
                            settings.save()
                            logger.info("Saved EroScripts cookies (%d chars)", len(cookie_str))

                s = self._get_session()
                async with s.post(
                    f"{API_URL}/pair", json=data,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    resp = await r.json()
                    respond(callback_id, {"success": True, **resp})
                return

            if msg_type == "resolve-url":
                cookie_str = self._get_eroscripts_cookies()
                if not cookie_str:
                    # Same as send-pair: don't resolve on only stale cookies
                    # when the live store is empty — capped CDP sync first
                    # (finding C).
                    await self._sync_cookies_now(2.0)
                    cookie_str = self._get_eroscripts_cookies()
                s = self._get_session()
                async with s.post(
                    f"{API_URL}/resolve",
                    json={"url": data.get("url", ""), "cookies": cookie_str},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    resp = await r.json()
                    if resp.get("success"):
                        respond(callback_id, {"success": True, "finalUrl": resp["url"]})
                    else:
                        respond(callback_id, {"success": False, "error": resp.get("error", "Resolve failed")})
                return

            if msg_type == "probe-url":
                s = self._get_session()
                async with s.post(
                    f"{API_URL}/probe", json={"url": data.get("url", "")},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    resp = await r.json()
                    respond(callback_id, resp)
                return

            if msg_type == "get-config":
                s = self._get_session()
                async with s.get(
                    f"{API_URL}/config", timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    resp = await r.json()
                    respond(callback_id, resp)
                return

            if msg_type == "send-link":
                s = self._get_session()
                async with s.post(
                    f"{API_URL}/link", json=data,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    resp = await r.json()
                    respond(callback_id, {"success": True, **resp})
                return

            if msg_type == "get-ero-credentials":
                from funpairdl.persistence.settings import Settings
                settings = Settings.load()
                respond(callback_id, {
                    "username": settings.eroscripts_username,
                    "password": settings.eroscripts_password,
                })
                return

            if msg_type == "register-autoclose":
                # Batch overlay: close this topic's tab(s) once all the pairs
                # it created finish downloading. Routed to the GUI thread via
                # the long-lived dispatcher (never touch Qt widgets here).
                self._dispatcher.autocloseRequested.emit(
                    str(data.get("url", "")),
                    json.dumps(data.get("pair_ids", [])),
                )
                respond(callback_id, {"success": True})
                return

            if msg_type == "storage-get":
                respond(callback_id, dict(self._storage))
                return

            if msg_type == "storage-set":
                self._storage.update(data)
                respond(callback_id, {"success": True})
                return

            respond(callback_id, {"error": f"Unknown message type: {msg_type}"})

        except Exception as e:
            logger.error("Bridge error handling %s: %s", msg_type, e)
            respond(callback_id, {"success": False, "error": str(e)})


class BrowserBridge(QObject):
    """Per-tab Python <-> JS bridge exposed via QWebChannel.

    Thin QObject: receives sendMessage from THIS tab's JS, runs the shared
    BridgeCore handler on the api-worker loop, and emits messageResponse
    back to this tab only (cross-thread emit is auto-queued by Qt), so a
    response is no longer broadcast to and parsed by every open tab.
    The registered object name ("bridge") and the sendMessage /
    messageResponse contract are unchanged for content.js.
    """

    messageResponse = Signal(str, str)  # callbackId, responseJson

    def __init__(self, core: BridgeCore, parent=None):
        super().__init__(parent)
        self._core = core
        self._key = ""  # routing key, assigned by BridgeCore.register_bridge

    @Slot(str, str, str)
    def sendMessage(self, msg_type: str, data_json: str, callback_id: str):
        try:
            data = json.loads(data_json) if data_json else {}
        except json.JSONDecodeError:
            data = {}
        # respond() routes the reply back to THIS tab (by key) via the shared
        # GUI-thread dispatcher — the worker loop must never emit on this
        # per-tab bridge, which the GUI thread may be tearing down (finding B).
        key = self._key
        def respond(cb_id, payload, _k=key):
            self._core.dispatch_response(_k, cb_id, payload)
        # Handler runs on the api-worker loop — never the GUI/qasync loop.
        self._core.spawn(self._core.handle(msg_type, data, callback_id, respond))


# ─── Custom WebEnginePage (handles new tab requests) ───


class TabWebEnginePage(QWebEnginePage):
    """Custom page that opens link targets in new tabs."""

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self._create_tab_func: Callable[[bool], QWebEnginePage] | None = None

    def createWindow(self, window_type):
        """Called when JS does window.open() or user middle-clicks a link.

        Directly creates a new tab via callback and returns its page.
        Chromium will load the target URL into the returned page.
        Single page creation (fast) instead of temp-page URL capture (slow).

        Middle-click / Ctrl+click report WebBrowserBackgroundTab — open the
        tab WITHOUT switching to it (Chrome-like), so the user can queue up
        topics from a listing without losing their place. Explicit
        window.open()/target=_blank keep the switch-to behavior.
        """
        if self._create_tab_func:
            background = (
                window_type
                == QWebEnginePage.WebWindowType.WebBrowserBackgroundTab
            )
            return self._create_tab_func(background)
        return None


# ─── Tabbed Browser Widget ───


HOME_URL = "https://discuss.eroscripts.com/c/scripts/free-scripts/14"


def _is_topic_url(url: str) -> bool:
    """URL of an EroScripts topic page (matches content.js _currentTopicId)."""
    return "eroscripts.com" in url and "/t/" in url


class BrowserWidget(QWidget):
    """Embedded tabbed browser with navigation bar and QWebChannel bridge."""

    # Emitted from the worker loop when the saved MEGA sid turns out dead
    # and a hidden-page login is actually needed (queued to the GUI thread).
    sig_mega_login_needed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Lazily-restored tabs: view -> (url_str, scroll_y). The URL is only
        # loaded on first activation (audit [5]).
        self._pending_tab_loads: dict = {}
        # MEGA hidden-page state — may never be created at all now
        self._mega_page = None
        self._mega_timer = None
        self._mega_safety_timer = None
        self._mega_phase = "done"
        self.sig_mega_login_needed.connect(self._on_mega_login_needed)
        self._setup_profile()
        self._setup_ui()
        self._inject_scripts()
        # Restore previous session or open default tab
        self._restore_session()
        # MEGA session refresh: only when actually needed, and deferred
        # past the startup burst (audit [12]).
        self._schedule_mega_sid_refresh()

    def _setup_profile(self):
        """Create persistent profile and bridge (shared across all tabs)."""
        self._profile = QWebEngineProfile("funpairdl", self)

        # Let the named profile use its OWN default storage path (AppDataLocation).
        # Do NOT override with setPersistentStoragePath — that caused a mismatch
        # where Qt read cookies from one location (Roaming) but the override
        # wrote to another (Local), making cookies "disappear" on restart.
        # Just ensure the directory exists.
        storage = self._profile.persistentStoragePath()
        if storage:
            Path(storage).mkdir(parents=True, exist_ok=True)
            logger.info("Profile storage: %s", storage)

        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )

        # ─── Performance: disk cache + prefetch ───
        self._profile.setHttpCacheType(
            QWebEngineProfile.HttpCacheType.DiskHttpCache
        )
        self._profile.setHttpCacheMaximumSize(256 * 1024 * 1024)  # 256 MB
        self._profile.setSpellCheckEnabled(False)

        # Shared bridge backend (cookies, CDP, loopback API session).
        # Each tab gets its own thin BrowserBridge in create_tab(). owner=self
        # parents the long-lived response dispatcher to this widget (GUI thread).
        self._bridge_core = BridgeCore(
            cookie_store=self._profile.cookieStore(), owner=self
        )

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Navigation toolbar
        nav_bar = QToolBar()
        nav_bar.setMovable(False)

        self.btn_back = QPushButton("<")
        self.btn_back.setFixedWidth(30)
        self.btn_back.setToolTip("Back")
        self.btn_forward = QPushButton(">")
        self.btn_forward.setFixedWidth(30)
        self.btn_forward.setToolTip("Forward")
        self.btn_reload = QPushButton("R")
        self.btn_reload.setFixedWidth(30)
        self.btn_reload.setToolTip("Reload")
        self.btn_home = QPushButton("H")
        self.btn_home.setFixedWidth(30)
        self.btn_home.setToolTip("EroScripts Home")

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Enter URL...")

        self.btn_new_tab = QPushButton("+")
        self.btn_new_tab.setFixedWidth(30)
        self.btn_new_tab.setToolTip("New Tab (Ctrl+T)")

        nav_bar.addWidget(self.btn_back)
        nav_bar.addWidget(self.btn_forward)
        nav_bar.addWidget(self.btn_reload)
        nav_bar.addWidget(self.btn_home)
        nav_bar.addWidget(self.url_bar)
        nav_bar.addWidget(self.btn_new_tab)

        layout.addWidget(nav_bar)

        # Tab widget for browser tabs
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._tabs.setElideMode(Qt.TextElideMode.ElideRight)
        layout.addWidget(self._tabs)

        # Connect navigation
        self.btn_back.clicked.connect(lambda: self._current_view().back() if self._current_view() else None)
        self.btn_forward.clicked.connect(lambda: self._current_view().forward() if self._current_view() else None)
        self.btn_reload.clicked.connect(lambda: self._current_view().reload() if self._current_view() else None)
        self.btn_home.clicked.connect(self._go_home)
        self.btn_new_tab.clicked.connect(lambda: self.create_tab(QUrl(HOME_URL)))
        self.url_bar.returnPressed.connect(self._navigate)

        # Tab signals
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs.tabCloseRequested.connect(self._close_tab)

        # Keyboard shortcuts
        QShortcut(QKeySequence("Ctrl+T"), self, lambda: self.create_tab(QUrl(HOME_URL)))
        QShortcut(QKeySequence("Ctrl+W"), self, lambda: self._close_tab(self._tabs.currentIndex()))
        QShortcut(QKeySequence("F5"), self, lambda: self._current_view().reload() if self._current_view() else None)
        QShortcut(QKeySequence("Alt+Left"), self, lambda: self._current_view().back() if self._current_view() else None)

    # ─── Session save/restore ───

    def _restore_session(self):
        """Restore browser tabs from previous session, or open default tab.

        LAZY restore (audit [5]): only the active tab actually loads its
        URL. Every other tab gets an empty view + a pending URL that loads
        on first activation (_on_tab_changed → _load_pending), so startup
        no longer parses/executes 15-22 Discourse SPAs at once.

        Reentrancy note: create_tab() calls setCurrentIndex, which fires
        currentChanged → _on_tab_changed → _load_pending DURING this loop.
        That is safe because the pending entry is registered only AFTER
        create_tab returns, so the mid-restore signal is a no-op.
        """
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        tabs = settings.browser_tabs
        scroll_positions = settings.browser_scroll_positions
        if not tabs:
            self.create_tab(QUrl(HOME_URL))
            return

        for i, url_str in enumerate(tabs):
            try:
                self.create_tab()  # no URL — deferred until first activation
                view = self._tabs.currentWidget()  # create_tab selected it
                scroll_y = scroll_positions[i] if i < len(scroll_positions) else 0
                self._pending_tab_loads[view] = (url_str, float(scroll_y or 0))
                # Placeholder title from the saved URL until the real load
                idx = self._tabs.indexOf(view)
                if idx >= 0:
                    self._tabs.setTabText(idx, self._placeholder_title(url_str))
                    self._tabs.setTabToolTip(idx, url_str)
            except Exception:
                pass
        # Restore active tab index
        active = settings.browser_active_tab
        if 0 <= active < self._tabs.count():
            self._tabs.setCurrentIndex(active)
        # setCurrentIndex is a no-op when the target already IS current
        # (e.g. the last created tab) — kick the pending load explicitly.
        self._load_pending(self._current_view())
        logger.info(
            "Restored %d browser tabs from previous session (lazy: only the "
            "active tab loads now)", self._tabs.count()
        )

    @staticmethod
    def _placeholder_title(url_str: str) -> str:
        """Best-effort tab title from a saved URL (before the page loads)."""
        try:
            from urllib.parse import unquote, urlparse
            parsed = urlparse(url_str)
            segments = [s for s in parsed.path.split("/") if s]
            # Prefer the last non-numeric segment (Discourse: /t/<slug>/<id>)
            title = ""
            for seg in reversed(segments):
                if not seg.isdigit():
                    title = unquote(seg)
                    break
            title = title or parsed.netloc or url_str
        except Exception:
            title = url_str
        return (title[:30] + "...") if len(title) > 30 else (title or "Untitled")

    def _load_pending(self, view) -> None:
        """Start the deferred load for a lazily-restored tab (no-op otherwise).

        GUI thread only. Attaches the saved-scroll restore handler at real
        load time, so _scroll_progressive fires only when the tab actually
        loads — not during the startup restore burst.
        """
        if view is None:
            return
        pending = self._pending_tab_loads.pop(view, None)
        if not pending:
            return
        url_str, scroll_y = pending
        try:
            page = view.page()
            if scroll_y > 0 and page is not None:
                is_listing = "eroscripts.com" in url_str and "/t/" not in url_str
                scroll_fn = self._scroll_progressive if is_listing else self._scroll_simple

                def _make_scroll_handler(p, y, fn):
                    def _on_load(ok):
                        if ok:
                            QTimer.singleShot(2000, lambda: fn(p, y))
                        try:
                            p.loadFinished.disconnect(_on_load)
                        except RuntimeError:
                            pass
                    return _on_load

                page.loadFinished.connect(
                    _make_scroll_handler(page, scroll_y, scroll_fn)
                )
            view.setUrl(QUrl(url_str))
        except Exception as e:
            logger.warning("Deferred tab load failed for %s: %s", url_str, e)

    def save_session(self, final: bool = False):
        """Save current browser tab URLs, scroll positions, and cookies.

        final=True (shutdown) forces a cookie flush that bypasses the freshness
        gate entirely; the normal path bypasses only the 30s gate when the last
        sync is >5s old (audit [10] / finding D). Callers that can't pass final
        (app.py/_shutdown, main_window) still capture the last window because
        5s < the 30s auto-save cadence.
        """
        from funpairdl.persistence.settings import Settings
        urls = []
        scroll_positions = []
        for i in range(self._tabs.count()):
            view = self._tabs.widget(i)
            if not isinstance(view, QWebEngineView):
                continue
            pending = self._pending_tab_loads.get(view)
            if pending:
                # Lazily-restored tab that was never activated — carry its
                # saved URL/scroll forward instead of dropping it (its view
                # still reports about:blank).
                urls.append(pending[0])
                scroll_positions.append(pending[1])
                continue
            url = view.url().toString()
            if url and url not in ("about:blank", ""):
                urls.append(url)
                scroll_y = view.page().scrollPosition().y()
                scroll_positions.append(scroll_y)
        # Never wipe a good saved session with an empty snapshot. Two cases
        # produce an empty `urls`: (1) on shutdown the web views may already be
        # torn down and report about:blank, and (2) an early auto-save can fire
        # before restored tabs finish committing their URL. Overwriting in
        # either case loses the real tabs — which is exactly the "tabs not
        # restored on restart" bug. Only persist tab/scroll state when we
        # actually captured at least one loaded tab. (Closing every tab and
        # quitting just keeps the prior session, which restores harmlessly.)
        if urls:
            active = self._tabs.currentIndex()
            # Dirty-check against the FILE's actual state, not a module-local
            # memo (audit [10] / finding A): other components (tray toggles,
            # settings dialog, pixeldrain picker accept) can save STALE
            # long-lived Settings instances that revert browser_tabs — a memo
            # that says "unchanged" would then never rewrite the truth. Compare
            # to the freshly-loaded file, and write via Settings.update (locked
            # read-modify-write) so the tab fields can't clobber cookies written
            # concurrently by routes. _settings_lock also serializes us against
            # this module's own cookie writers.
            def _apply_tab_state(s):
                s.browser_tabs = urls
                s.browser_active_tab = active
                s.browser_scroll_positions = scroll_positions

            with _settings_lock:
                s = Settings.load()
                if (s.browser_tabs == urls
                        and s.browser_active_tab == active
                        and s.browser_scroll_positions == scroll_positions):
                    logger.debug("save_session: file already current — skipping write")
                else:
                    Settings.update(_apply_tab_state)
                    logger.debug("Saved %d browser tabs for session restore", len(urls))
        else:
            logger.debug(
                "save_session: captured no loaded tabs — keeping previously "
                "saved session instead of overwriting it"
            )

        # Cookie flush on the worker loop (never blocks this GUI-thread call).
        # Bypass the 30s freshness gate so the last <=30s of cookies actually
        # reach the settings jar: at quit (final) bypass entirely, otherwise
        # only when the last sync is >5s old (audit [10] / finding D).
        try:
            if final:
                self._bridge_core.flush_cookies_now()
            else:
                self._bridge_core.flush_cookies_now(min_age=5.0)
        except Exception:
            pass

    # ─── Scroll restoration ───

    @staticmethod
    def _scroll_simple(page, y):
        """Instant scroll for normal pages."""
        page.runJavaScript(f"window.scrollTo(0, {y});")

    @staticmethod
    def _scroll_progressive(page, target_y):
        """Progressive scroll for infinite-scroll pages (Discourse listings).

        Repeatedly scrolls to bottom to trigger lazy loading until the page
        is tall enough, then scrolls to the saved position.
        """
        js = """
(function() {
    var targetY = """ + str(int(target_y)) + """;
    var maxAttempts = 60;
    var attempt = 0;
    var lastHeight = 0;
    var staleCount = 0;

    function tryScroll() {
        attempt++;
        var h = document.body.scrollHeight;

        if (h >= targetY + 100) {
            window.scrollTo(0, targetY);
            return;
        }

        if (h === lastHeight) {
            staleCount++;
        } else {
            staleCount = 0;
        }

        if (attempt > maxAttempts || staleCount >= 4) {
            window.scrollTo(0, targetY);
            return;
        }

        lastHeight = h;
        window.scrollTo(0, h);
        setTimeout(tryScroll, 800);
    }

    tryScroll();
})();
"""
        page.runJavaScript(js)

    # ─── Tab management ───

    def create_tab(self, url: QUrl | None = None, select: bool = True) -> TabWebEnginePage:
        """Create a new browser tab and return its page.

        select=False adds the tab in the background (middle-click) without
        stealing focus from the current page.
        """
        view = QWebEngineView()
        page = TabWebEnginePage(self._profile, view)

        # Avoid the white "flashbang" before a page paints: QWebEngine defaults
        # the pre-paint background to white. Use the app's current window color
        # (follows the system light/dark palette) so a not-yet-loaded tab blends
        # in instead of flashing white. setBackgroundColor lives on the page;
        # also paint the view widget itself so its host surface isn't white
        # before the renderer attaches.
        from PySide6.QtGui import QPalette
        bg = self.palette().color(QPalette.ColorRole.Window)
        page.setBackgroundColor(bg)
        view_palette = view.palette()
        view_palette.setColor(QPalette.ColorRole.Base, bg)
        view_palette.setColor(QPalette.ColorRole.Window, bg)
        view.setPalette(view_palette)
        view.setAutoFillBackground(True)

        # Setup QWebChannel for this page — each tab gets its OWN bridge
        # object (audit [6]) so responses go only to the tab that asked,
        # instead of being serialized and IPC'd to all N tabs. The JS-side
        # object name ("bridge") is unchanged.
        channel = QWebChannel(page)
        bridge = BrowserBridge(self._bridge_core, parent=page)
        # Register under a routing key so worker-loop replies reach this tab
        # via the shared dispatcher; unregistered in _close_tab before the view
        # is destroyed (finding B). Key stored on the view for cleanup lookup.
        view._bridge_key = self._bridge_core.register_bridge(bridge)
        channel.registerObject("bridge", bridge)
        page.setWebChannel(channel)

        view.setPage(page)

        # Enable settings
        s = page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.DnsPrefetchEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)

        # Suppress intermediate repaints while adding tab
        self._tabs.setUpdatesEnabled(False)
        idx = self._tabs.addTab(view, "New Tab")
        if select:
            self._tabs.setCurrentIndex(idx)
        self._tabs.setUpdatesEnabled(True)

        # Connect signals
        view.titleChanged.connect(lambda title, v=view: self._on_title_changed(v, title))
        view.urlChanged.connect(lambda u, v=view: self._on_url_changed(v, u))
        # Load-visibility boost: background/hidden pages get their JS timers
        # throttled by Chromium (~1 wake/s), which slows a Discourse boot to a
        # crawl — see _boost_view.
        page.loadStarted.connect(lambda v=view: self._on_page_load_started(v))
        page.loadFinished.connect(lambda ok, v=view: self._on_page_load_finished(v, ok))
        page._create_tab_func = self._create_tab_for_window

        if url:
            view.setUrl(url)

        return page

    def _create_tab_for_window(self, background: bool = False) -> TabWebEnginePage:
        """Called from createWindow — create a new tab and return its page.

        Chromium will load the target URL into the returned page.
        background=True (middle-click) keeps the current tab focused.
        """
        return self.create_tab(select=not background)

    def _close_tab(self, index: int):
        """Close tab at index. Keep at least one tab open.

        Strategy: switch to the destination tab FIRST so the main page
        starts re-rendering, then remove and defer-destroy the old tab.
        This prevents the main page from being blocked by cleanup work.
        """
        if self._tabs.count() <= 1:
            return

        widget = self._tabs.widget(index)
        # Drop any never-activated deferred load for this tab
        self._pending_tab_loads.pop(widget, None)
        # Unregister this tab's bridge BEFORE the view is destroyed so a late
        # worker-loop reply is dropped instead of routed to a dying QObject
        # (finding B — the dispatcher looks it up on the GUI thread).
        key = getattr(widget, "_bridge_key", None)
        if key is not None:
            self._bridge_core.unregister_bridge(key)

        # Switch to the target tab before removing the old one,
        # giving the destination page a head start on rendering.
        self._tabs.setUpdatesEnabled(False)
        target = index - 1 if index == self._tabs.count() - 1 else index
        if target != index:
            self._tabs.setCurrentIndex(target if target < index else target)
        self._tabs.removeTab(index)
        self._tabs.setUpdatesEnabled(True)

        if widget:
            # Defer destruction — let the main page become interactive first
            from PySide6.QtCore import QTimer
            widget.setParent(None)  # detach from layout immediately
            QTimer.singleShot(500, widget.deleteLater)

    def close_all_tabs(self):
        """Close all tabs and destroy pages to flush cookies before shutdown."""
        self._pending_tab_loads.clear()
        for i in range(self._tabs.count() - 1, -1, -1):
            widget = self._tabs.widget(i)
            self._tabs.removeTab(i)
            if widget:
                key = getattr(widget, "_bridge_key", None)
                if key is not None:
                    self._bridge_core.unregister_bridge(key)
                widget.setParent(None)
                widget.deleteLater()

    def _current_view(self) -> QWebEngineView | None:
        w = self._tabs.currentWidget()
        return w if isinstance(w, QWebEngineView) else None

    # ─── Navigation ───

    def _go_home(self):
        view = self._current_view()
        if view:
            view.setUrl(QUrl(HOME_URL))

    def _navigate(self):
        url = self.url_bar.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        view = self._current_view()
        if view:
            view.setUrl(QUrl(url))

    # ─── Signal handlers ───

    def _on_tab_changed(self, index: int):
        """Update URL bar when switching tabs; kick deferred tab loads.

        Background tabs stay hidden so Chromium can throttle and idle them
        (audit [5] — the old force-show "keep alive" is deliberately gone;
        we accept a brief re-rasterize on switch instead of N tabs
        compositing at full speed forever).
        """
        view = self._current_view()
        if view:
            self.url_bar.setText(view.url().toString())
            # Lazily-restored tab activated for the first time → load now
            self._load_pending(view)
        # Switching away hides the old widget, and Qt then marks its page
        # invisible — which would kill an active load boost mid-load.
        # Re-assert the boost for any still-loading (or settling) tab.
        for i in range(self._tabs.count()):
            v = self._tabs.widget(i)
            if (isinstance(v, QWebEngineView) and v is not view
                    and self._view_boost_active(v)):
                self._boost_view(v)

    # --- Load-visibility boost -------------------------------------------
    #
    # Chromium throttles hidden pages' JS timers to ~1 wake/s. With the old
    # global anti-throttling flags removed (audit [5]), a page loading in a
    # background tab — or while the Downloads tab is focused, or while the
    # window is occluded — boots at a crawl (Discourse is timer-heavy). The
    # bounded fix: tell Chromium the page is visible ONLY while it loads
    # (plus a short settle window for post-onload SPA boot), then hand
    # visibility back to the widget state so idle tabs throttle as intended.

    _LOAD_BOOST_SETTLE_MS = 10_000

    def _on_page_load_started(self, view: QWebEngineView):
        view._load_started_at = time.monotonic()
        self._boost_view(view)

    def _on_page_load_finished(self, view: QWebEngineView, ok: bool):
        started = getattr(view, "_load_started_at", None)
        view._load_started_at = None
        # Never SHRINK an externally extended boost window (batch send-all
        # keeps background tabs unthrottled for its whole run).
        view._boost_until = max(
            getattr(view, "_boost_until", 0.0),
            time.monotonic() + self._LOAD_BOOST_SETTLE_MS / 1000,
        )
        # Generation counter: only the timer scheduled by the LATEST finish
        # may end the boost (a re-load within the settle window supersedes).
        gen = getattr(view, "_boost_gen", 0) + 1
        view._boost_gen = gen
        if started is not None:
            where = "foreground" if view is self._current_view() else "background"
            logger.info(
                "Page loaded in %.1fs (%s, ok=%s): %s",
                time.monotonic() - started, where, ok,
                view.url().toString()[:100],
            )
        QTimer.singleShot(
            self._LOAD_BOOST_SETTLE_MS, lambda v=view, g=gen: self._end_boost(v, g)
        )

    def _view_boost_active(self, view: QWebEngineView) -> bool:
        if getattr(view, "_load_started_at", None) is not None:
            return True
        return time.monotonic() < getattr(view, "_boost_until", 0.0)

    def _boost_view(self, view: QWebEngineView):
        try:
            page = view.page()
            if page is not None and not page.isVisible():
                page.setVisible(True)
        except RuntimeError:
            pass  # view/page mid-destruction

    def _end_boost(self, view: QWebEngineView, gen: int):
        """Settle timer fired — drop the Chromium-visible override unless the
        tab is current, still loading, or a newer load superseded this timer."""
        try:
            if self._tabs.indexOf(view) < 0:
                return  # tab closed
            if gen != getattr(view, "_boost_gen", 0):
                return  # superseded by a newer loadFinished
            if view is self._current_view():
                return
            if getattr(view, "_load_started_at", None) is not None:
                return  # a fresh load is in flight
            # An externally extended boost window (batch send-all) is still
            # active — check back when it lapses instead of dropping
            # visibility mid-run.
            remaining = getattr(view, "_boost_until", 0.0) - time.monotonic()
            if remaining > 0:
                QTimer.singleShot(
                    int(remaining * 1000) + 200,
                    lambda v=view, g=gen: self._end_boost(v, g),
                )
                return
            page = view.page()
            if page is not None and page.isVisible():
                page.setVisible(False)
        except RuntimeError:
            pass

    def _on_url_changed(self, view: QWebEngineView, url: QUrl):
        """Update URL bar if this is the active tab."""
        if view == self._current_view():
            self.url_bar.setText(url.toString())

    def _on_title_changed(self, view: QWebEngineView, title: str):
        """Update tab title."""
        idx = self._tabs.indexOf(view)
        if idx >= 0:
            # Truncate long titles
            display = title[:30] + "..." if len(title) > 30 else title
            self._tabs.setTabText(idx, display or "Untitled")
            self._tabs.setTabToolTip(idx, title)

    # ─── Script injection (profile-level, applies to all tabs) ───

    def _inject_scripts(self):
        """Inject qwebchannel.js, bridge, CSS, and content.js into the profile."""
        self._inject_qwebchannel_js()
        self._inject_bridge_script()
        self._inject_content_scripts()

    def _inject_qwebchannel_js(self):
        qwc_file = Path(__file__).resolve().parent / "qwebchannel.js"
        if not qwc_file.exists():
            logger.error("qwebchannel.js not found at %s", qwc_file)
            return
        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(qwc_file.read_text(encoding="utf-8"))
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self._profile.scripts().insert(script)

    def _inject_bridge_script(self):
        bridge_js = """
(function() {
    if (window._funpairdlBridgeReady) return;
    window._funpairdlBridgeReady = true;

    var _callbacks = {};
    var _cbId = 0;

    new QWebChannel(qt.webChannelTransport, function(channel) {
        var bridge = channel.objects.bridge;

        bridge.messageResponse.connect(function(callbackId, responseJson) {
            var cb = _callbacks[callbackId];
            if (cb) {
                delete _callbacks[callbackId];
                try { cb(JSON.parse(responseJson)); }
                catch(e) { cb({}); }
            }
        });

        window.funpairdlBridge = {
            sendMessage: function(type, data) {
                return new Promise(function(resolve) {
                    var id = "cb_" + (++_cbId);
                    _callbacks[id] = resolve;
                    bridge.sendMessage(type, JSON.stringify(data || {}), id);
                });
            },
            storage: {
                get: function(key) {
                    return window.funpairdlBridge.sendMessage("storage-get", {})
                        .then(function(data) { return data[key]; });
                },
                set: function(obj) {
                    return window.funpairdlBridge.sendMessage("storage-set", obj);
                }
            }
        };

        console.log("FunPairDL: QWebChannel bridge ready");
        window.dispatchEvent(new Event("funpairdl-bridge-ready"));
    });
})();
"""
        script = QWebEngineScript()
        script.setName("funpairdl-bridge")
        script.setSourceCode(bridge_js)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self._profile.scripts().insert(script)

    def _inject_content_scripts(self):
        base_dir = Path(__file__).resolve().parent.parent.parent / "extension"
        css_file = base_dir / "content.css"
        js_file = base_dir / "content.js"

        if css_file.exists():
            css_text = css_file.read_text(encoding="utf-8")
            css_escaped = (
                css_text.replace("\\", "\\\\")
                .replace("`", "\\`")
                .replace("${", "\\${")
            )
            css_inject_js = f"""
(function() {{
    if (document.getElementById("funpairdl-injected-css")) return;
    var style = document.createElement("style");
    style.id = "funpairdl-injected-css";
    style.textContent = `{css_escaped}`;
    document.head.appendChild(style);
}})();
"""
            css_script = QWebEngineScript()
            css_script.setName("funpairdl-css")
            css_script.setSourceCode(css_inject_js)
            css_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            css_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            css_script.setRunsOnSubFrames(False)
            self._profile.scripts().insert(css_script)

        if js_file.exists():
            content_script = QWebEngineScript()
            content_script.setName("funpairdl-content")
            content_script.setSourceCode(js_file.read_text(encoding="utf-8"))
            content_script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            content_script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            content_script.setRunsOnSubFrames(False)
            self._profile.scripts().insert(content_script)

    # ─── MEGA session auto-refresh ───

    def _schedule_mega_sid_refresh(self):
        """Decide whether the hidden MEGA login page is needed (audit [12]).

        Startup no longer unconditionally spawns the heavy hidden mega.nz
        renderer:
        - no credentials → never spawn it;
        - saved sid → validate it cheaply on the worker loop first; a live
          sid means no hidden page at all;
        - login actually needed → defer the hidden page by 60s so it stays
          clear of the session-restore startup burst.
        """
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if not settings.mega_email or not settings.mega_password:
            logger.info("MEGA session refresh: no credentials configured, skipping")
            return
        if settings.mega_sid:
            self._bridge_core.spawn(self._validate_mega_sid_bg(settings.mega_sid))
            return
        logger.info("MEGA session refresh: no saved sid — login page deferred 60s")
        QTimer.singleShot(60_000, self._refresh_mega_sid)

    async def _validate_mega_sid_bg(self, sid: str):
        """Worker loop: check the saved sid; request deferred login iff dead.

        No Qt widget/page access here — only a signal emit (auto-queued to
        the GUI thread).
        """
        try:
            from funpairdl.utils.mega_api import validate_mega_sid
            result = await validate_mega_sid(sid)
        except Exception as e:
            logger.debug("MEGA sid validation failed: %s", e)
            return
        if result.get("valid"):
            logger.info("MEGA session refresh: saved sid still valid — no hidden page needed")
            return
        if not result.get("auth_error"):
            # Transient server hiccup — keep the sid, don't spawn the page
            logger.info("MEGA session refresh: transient validation error, keeping sid")
            return
        logger.info("MEGA session refresh: saved sid is dead — scheduling login page")
        self.sig_mega_login_needed.emit()

    def _on_mega_login_needed(self):
        """GUI thread: defer the hidden MEGA login page past startup load."""
        QTimer.singleShot(60_000, self._refresh_mega_sid)

    def _stop_mega_timer(self):
        """Stop and discard the current MEGA poll timer, if any (GUI thread).

        Always called before assigning a fresh timer so reassignments can't
        orphan a still-running QTimer (audit [1i]).
        """
        timer = getattr(self, "_mega_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._mega_timer = None

    def _refresh_mega_sid(self):
        """Background-load mega.nz in a hidden page to extract u_sid session.

        Safety: auto-cleanup after 120 seconds regardless of outcome.

        Flow:
        1. Load mega.nz → check if already logged in (u_sid exists)
        2. If not logged in and credentials in settings → navigate to login page
        3. Auto-fill email/password and submit
        4. Extract u_sid after login → save to config
        """
        if getattr(self, "_mega_page", None):
            logger.debug("MEGA session refresh already in progress — skipping")
            return
        self._stop_mega_timer()
        self._mega_phase = "check"  # "check" → "login_page" → "login_submit" → "done"
        self._mega_poll_count = 0
        self._mega_page = QWebEnginePage(self._profile, self)
        # No view → Chromium treats the page as hidden and throttles its JS
        # timers, so the MEGA webclient would boot at a crawl. Mark it
        # visible for its bounded lifetime (cleaned up within 120s).
        self._mega_page.setVisible(True)
        self._mega_page.loadFinished.connect(self._on_mega_load_finished)
        logger.info("MEGA session refresh: loading mega.nz...")
        self._mega_page.setUrl(QUrl("https://mega.nz"))

        # Safety timeout: force cleanup after 120s no matter what
        self._mega_safety_timer = QTimer(self)
        self._mega_safety_timer.setSingleShot(True)
        self._mega_safety_timer.setInterval(120_000)
        self._mega_safety_timer.timeout.connect(self._mega_safety_cleanup)
        self._mega_safety_timer.start()

    def _on_mega_load_finished(self, ok: bool):
        """Handle page load for any phase of MEGA session refresh."""
        if not ok:
            logger.warning("MEGA session refresh: page load failed (phase=%s)", self._mega_phase)
            self._cleanup_mega_page()
            return
        if not getattr(self, "_mega_page", None):
            return  # already cleaned up (late loadFinished)

        # Stop any previous poll timer BEFORE assigning a fresh one — a
        # double loadFinished used to orphan the old repeating timer,
        # which then fired against a deleted page forever (audit [1i]).
        self._stop_mega_timer()

        if self._mega_phase == "check":
            # Phase 1: mega.nz loaded — poll for u_sid
            self._mega_poll_count = 0
            self._mega_timer = QTimer(self)
            self._mega_timer.setSingleShot(False)
            self._mega_timer.setInterval(2000)
            self._mega_timer.timeout.connect(self._poll_mega_sid)
            self._mega_timer.start()

        elif self._mega_phase == "login_page":
            # Phase 2: page (re)loaded during login phase — resume the JS
            # login poll. (The old code connected a nonexistent
            # _try_fill_login here — a latent AttributeError.)
            self._mega_poll_count = 0
            self._mega_timer = QTimer(self)
            self._mega_timer.setSingleShot(False)
            self._mega_timer.setInterval(1500)
            self._mega_timer.timeout.connect(self._try_js_login)
            self._mega_timer.start()

        elif self._mega_phase == "login_submit":
            # Phase 3: after login submit — poll for u_sid again
            self._mega_poll_count = 0
            self._mega_timer = QTimer(self)
            self._mega_timer.setSingleShot(False)
            self._mega_timer.setInterval(2000)
            self._mega_timer.timeout.connect(self._poll_mega_sid_after_login)
            self._mega_timer.start()

    def _poll_mega_sid(self):
        """Phase 1: Check if u_sid exists (already logged in from previous session)."""
        if not getattr(self, "_mega_page", None):
            self._stop_mega_timer()
            return
        self._mega_poll_count += 1
        if self._mega_poll_count > 8:  # ~16s
            logger.info("MEGA session refresh: not logged in, attempting auto-login...")
            self._stop_mega_timer()
            self._attempt_mega_login()
            return
        self._mega_page.runJavaScript(
            "typeof u_sid !== 'undefined' ? u_sid : ''",
            self._on_check_sid_result,
        )

    def _on_check_sid_result(self, result):
        """Handle u_sid check in phase 1."""
        sid = str(result).strip() if result else ""
        if sid:
            self._stop_mega_timer()
            self._save_mega_sid(sid)
            self._cleanup_mega_page()

    def _attempt_mega_login(self):
        """Try to auto-login using credentials from settings."""
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if not settings.mega_email or not settings.mega_password:
            logger.info("MEGA session refresh: no credentials configured, skipping login")
            self._cleanup_mega_page()
            return
        self._mega_email = settings.mega_email
        self._mega_password = settings.mega_password
        self._mega_phase = "login_page"
        # Stay on mega.nz (already loaded) — wait for MEGA JS to be ready, then login via JS API
        self._mega_poll_count = 0
        self._stop_mega_timer()
        self._mega_timer = QTimer(self)
        self._mega_timer.setSingleShot(False)
        self._mega_timer.setInterval(2000)
        self._mega_timer.timeout.connect(self._try_js_login)
        self._mega_timer.start()

    def _try_js_login(self):
        """Phase 2: Wait for MEGA JS to load, then call login via their internal API."""
        if not getattr(self, "_mega_page", None):
            self._stop_mega_timer()
            return
        self._mega_poll_count += 1
        if self._mega_poll_count > 15:  # ~30s
            logger.warning("MEGA session refresh: MEGA JS not ready, giving up")
            self._stop_mega_timer()
            self._cleanup_mega_page()
            return

        email = self._mega_email.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
        password = self._mega_password.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')

        # Use MEGA's internal JS API to login — much more reliable than form filling.
        # MEGA exposes security.login() or postLogin() after their JS loads.
        # Fallback chain: security.login → direct API call via their own u_login.
        login_js = f"""
(function() {{
    // Check if MEGA's JS framework is loaded
    if (typeof security === 'undefined' || typeof api_req === 'undefined') {{
        return 'not_ready';
    }}

    // Already logged in?
    if (typeof u_sid !== 'undefined' && u_sid) {{
        return 'already:' + u_sid;
    }}

    // Use MEGA's own login flow
    try {{
        security.login(null, null, new SecurityContext('{email}', '{password}'),
            function() {{
                // Login callback — u_sid should now be set
                console.log('MEGA login callback fired, u_sid:', typeof u_sid !== 'undefined' ? u_sid : 'none');
            }}
        );
        return 'login_called';
    }} catch(e1) {{
        // Fallback: try the older startLogin flow
        try {{
            if (typeof startLogin === 'function') {{
                startLogin('{email}', '{password}');
                return 'startLogin_called';
            }}
        }} catch(e2) {{}}

        // Fallback: try filling the form directly
        var inputs = document.querySelectorAll('input');
        var emailInput = null, passInput = null;
        for (var i = 0; i < inputs.length; i++) {{
            var t = inputs[i].type.toLowerCase();
            var id = (inputs[i].id || '').toLowerCase();
            var name = (inputs[i].name || '').toLowerCase();
            if (t === 'email' || id.includes('login-name') || name.includes('email') || id.includes('email')) {{
                emailInput = inputs[i];
            }} else if (t === 'password' || id.includes('password') || name.includes('password')) {{
                passInput = inputs[i];
            }}
        }}

        if (emailInput && passInput) {{
            var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(emailInput, '{email}');
            emailInput.dispatchEvent(new Event('input', {{bubbles: true}}));
            setter.call(passInput, '{password}');
            passInput.dispatchEvent(new Event('input', {{bubbles: true}}));

            // Find and click any login/submit button
            var btns = document.querySelectorAll('button, .login-button, .mega-button');
            for (var j = 0; j < btns.length; j++) {{
                var txt = (btns[j].textContent || '').toLowerCase();
                if (txt.includes('log in') || txt.includes('login') || txt.includes('sign in')) {{
                    btns[j].click();
                    return 'form_submitted';
                }}
            }}
            // Try Enter key
            passInput.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter',code:'Enter',keyCode:13,bubbles:true}}));
            return 'enter_sent';
        }}

        return 'no_method:' + e1.message;
    }}
}})();
"""
        self._mega_page.runJavaScript(login_js, self._on_login_result)

    def _on_login_result(self, result):
        """Handle JS login attempt result."""
        status = str(result).strip() if result else ""
        logger.info("MEGA session refresh: login attempt result: %s", status[:80])

        if status.startswith("already:"):
            sid = status[8:]
            self._stop_mega_timer()
            self._save_mega_sid(sid)
            self._cleanup_mega_page()
        elif status == "not_ready":
            pass  # Keep polling — MEGA JS not loaded yet
        elif status in ("login_called", "startLogin_called", "form_submitted", "enter_sent"):
            self._stop_mega_timer()
            self._mega_phase = "login_submit"
            # MEGA key derivation takes ~10-20s, wait longer before polling
            QTimer.singleShot(15000, self._start_post_login_poll)
        elif status.startswith("no_method:"):
            logger.warning("MEGA session refresh: no login method available: %s", status)

    def _start_post_login_poll(self):
        """Start polling for u_sid after login submit."""
        if not getattr(self, "_mega_page", None):
            return  # cleaned up while waiting for key derivation
        self._mega_poll_count = 0
        self._stop_mega_timer()  # never orphan a still-running poll timer
        self._mega_timer = QTimer(self)
        self._mega_timer.setSingleShot(False)
        self._mega_timer.setInterval(3000)  # 3s intervals — key derivation is slow
        self._mega_timer.timeout.connect(self._poll_mega_sid_after_login)
        self._mega_timer.start()

    def _poll_mega_sid_after_login(self):
        """Phase 3: Poll for u_sid after login attempt."""
        if not getattr(self, "_mega_page", None):
            self._stop_mega_timer()
            return
        self._mega_poll_count += 1
        if self._mega_poll_count > 20:  # ~60s total (15s wait + 20×3s)
            logger.warning("MEGA session refresh: login failed (no u_sid after 60s)")
            self._stop_mega_timer()
            self._cleanup_mega_page()
            return
        self._mega_page.runJavaScript(
            "typeof u_sid !== 'undefined' ? u_sid : ''",
            self._on_post_login_sid_result,
        )

    def _on_post_login_sid_result(self, result):
        """Handle u_sid check after login."""
        sid = str(result).strip() if result else ""
        if sid:
            self._stop_mega_timer()
            logger.info("MEGA session refresh: login successful!")
            self._save_mega_sid(sid)
            self._cleanup_mega_page()

    def _save_mega_sid(self, sid: str):
        """Save extracted MEGA session ID to settings."""
        from funpairdl.persistence.settings import Settings
        with _settings_lock:
            settings = Settings.load()
            if settings.mega_sid != sid:
                settings.mega_sid = sid
                settings.save()
                logger.info("MEGA session refresh: saved new sid (%d chars)", len(sid))
            else:
                logger.info("MEGA session refresh: sid unchanged")

    def _mega_safety_cleanup(self):
        """Force cleanup if MEGA session refresh is stuck."""
        if hasattr(self, "_mega_page") and self._mega_page:
            logger.warning("MEGA session refresh: safety timeout (120s), force cleanup")
            self._cleanup_mega_page()

    def _cleanup_mega_page(self):
        """Clean up the hidden MEGA page and ALL related timers."""
        self._stop_mega_timer()
        if getattr(self, "_mega_safety_timer", None):
            self._mega_safety_timer.stop()
            self._mega_safety_timer.deleteLater()
            self._mega_safety_timer = None
        if hasattr(self, "_mega_page") and self._mega_page:
            self._mega_page.deleteLater()
            self._mega_page = None
        self._mega_email = ""
        self._mega_password = ""

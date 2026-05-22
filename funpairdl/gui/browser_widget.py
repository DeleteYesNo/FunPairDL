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
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
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

# ─── QWebChannel Bridge ───


class BrowserBridge(QObject):
    """Python <-> JS bridge exposed via QWebChannel.

    Handles the same message types as background.js, calling
    the local FastAPI backend.
    """

    messageResponse = Signal(str, str)  # callbackId, responseJson

    # CDP port for cookie extraction (set via QTWEBENGINE_REMOTE_DEBUGGING)
    CDP_PORT = int(os.environ.get("QTWEBENGINE_REMOTE_DEBUGGING", "9223"))

    def __init__(self, cookie_store=None, parent=None):
        super().__init__(parent)
        self._cookie_store = cookie_store
        self._cookies: dict[str, str] = {}
        self._last_cookie_persist: float = 0

        # Try signal-based approach (broken in many PySide6 versions)
        if cookie_store:
            cookie_store.cookieAdded.connect(self._on_cookie_added)
            cookie_store.cookieRemoved.connect(self._on_cookie_removed)
            cookie_store.loadAllCookies()

        # Schedule CDP-based cookie extraction as reliable fallback
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, self._schedule_cdp_cookie_sync)
        # Restore cookies from backup 5s after init (CDP needs the browser loaded)
        QTimer.singleShot(5000, lambda: asyncio.ensure_future(self.restore_cookies_if_needed()))

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
        for key, val in self._cookies.items():
            if "eroscripts.com" in key.split("|")[0]:
                parts.append(val)
        return "; ".join(parts)

    # ─── CDP-based cookie extraction ───

    async def _extract_cookies_cdp(self) -> dict[str, str]:
        """Extract ALL cookies (including httpOnly) via Chrome DevTools Protocol.

        This bypasses the broken PySide6 cookieAdded signal by connecting
        to QWebEngine's built-in CDP endpoint.
        """
        raw_cookies = await self._cdp_get_all_cookies()
        cookies = {}
        for cookie in raw_cookies:
            domain = cookie.get("domain", "")
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            cookies[f"{domain}|{name}"] = f"{name}={value}"
        return cookies

    async def _cdp_get_all_cookies(self) -> list[dict]:
        """Fetch raw cookie dicts from CDP (includes domain, httpOnly, etc.)."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
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
            async with aiohttp.ClientSession() as session:
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
        """Schedule async CDP cookie extraction (called from QTimer)."""
        asyncio.ensure_future(self._cdp_cookie_sync())

    async def _cdp_cookie_sync(self):
        """Extract cookies via CDP and persist EroScripts cookies."""
        cdp_cookies = await self._extract_cookies_cdp()
        if cdp_cookies:
            self._cookies.update(cdp_cookies)
            logger.info("CDP: updated cookie store with %d cookies", len(cdp_cookies))
        # Also save full cookie details for EroScripts (for restore on startup)
        raw = await self._cdp_get_all_cookies()
        ero_cookies = [c for c in raw if "eroscripts" in c.get("domain", "")]
        if ero_cookies:
            from funpairdl.persistence.settings import Settings
            settings = Settings.load()
            settings.eroscripts_cookie_jar = ero_cookies
            settings.save()
        self._persist_eroscripts_cookies()

    async def restore_cookies_if_needed(self):
        """Restore EroScripts cookies from settings backup if missing in browser.

        Called once on startup after the browser profile is ready.
        """
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
        """Save EroScripts cookies to settings."""
        import time
        cookie_str = self._get_eroscripts_cookies()
        if not cookie_str:
            logger.debug("No EroScripts cookies to persist (total cookies in store: %d)", len(self._cookies))
            return
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if settings.eroscripts_cookies != cookie_str:
            settings.eroscripts_cookies = cookie_str
            settings.save()
            logger.info("Auto-saved EroScripts cookies (%d chars)", len(cookie_str))
        self._last_cookie_persist = time.monotonic()

    @Slot(str, str, str)
    def sendMessage(self, msg_type: str, data_json: str, callback_id: str):
        try:
            data = json.loads(data_json) if data_json else {}
        except json.JSONDecodeError:
            data = {}
        asyncio.ensure_future(self._handle(msg_type, data, callback_id))

    async def _handle(self, msg_type: str, data: dict, callback_id: str):
        import aiohttp

        try:
            if msg_type == "check-status":
                # Periodically sync cookies via CDP (every 60s)
                import time
                if time.monotonic() - self._last_cookie_persist > 30:
                    await self._cdp_cookie_sync()
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"{API_URL}/status", timeout=aiohttp.ClientTimeout(total=3)
                    ) as r:
                        resp = await r.json()
                        self._respond(callback_id, {"online": True, **resp})
                return

            if msg_type == "send-pair":
                # Ensure cookies are fresh via CDP before sending pair
                cookie_str = self._get_eroscripts_cookies()
                if not cookie_str:
                    await self._cdp_cookie_sync()
                    cookie_str = self._get_eroscripts_cookies()
                logger.info("send-pair: EroScripts cookies = %d chars, total cookies = %d",
                            len(cookie_str), len(self._cookies))
                if cookie_str:
                    from funpairdl.persistence.settings import Settings
                    settings = Settings.load()
                    if settings.eroscripts_cookies != cookie_str:
                        settings.eroscripts_cookies = cookie_str
                        settings.save()
                        logger.info("Saved EroScripts cookies (%d chars)", len(cookie_str))

                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{API_URL}/pair", json=data,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        resp = await r.json()
                        self._respond(callback_id, {"success": True, **resp})
                return

            if msg_type == "resolve-url":
                cookie_str = self._get_eroscripts_cookies()
                if not cookie_str:
                    await self._cdp_cookie_sync()
                    cookie_str = self._get_eroscripts_cookies()
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{API_URL}/resolve",
                        json={"url": data.get("url", ""), "cookies": cookie_str},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        resp = await r.json()
                        if resp.get("success"):
                            self._respond(callback_id, {"success": True, "finalUrl": resp["url"]})
                        else:
                            self._respond(callback_id, {"success": False, "error": resp.get("error", "Resolve failed")})
                return

            if msg_type == "probe-url":
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{API_URL}/probe", json={"url": data.get("url", "")},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        resp = await r.json()
                        self._respond(callback_id, resp)
                return

            if msg_type == "get-config":
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"{API_URL}/config", timeout=aiohttp.ClientTimeout(total=5)
                    ) as r:
                        resp = await r.json()
                        self._respond(callback_id, resp)
                return

            if msg_type == "send-link":
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{API_URL}/link", json=data,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        resp = await r.json()
                        self._respond(callback_id, {"success": True, **resp})
                return

            if msg_type == "get-ero-credentials":
                from funpairdl.persistence.settings import Settings
                settings = Settings.load()
                self._respond(callback_id, {
                    "username": settings.eroscripts_username,
                    "password": settings.eroscripts_password,
                })
                return

            if msg_type == "storage-get":
                self._respond(callback_id, getattr(self, "_storage", {}))
                return

            if msg_type == "storage-set":
                if not hasattr(self, "_storage"):
                    self._storage = {}
                self._storage.update(data)
                self._respond(callback_id, {"success": True})
                return

            self._respond(callback_id, {"error": f"Unknown message type: {msg_type}"})

        except Exception as e:
            logger.error("Bridge error handling %s: %s", msg_type, e)
            self._respond(callback_id, {"success": False, "error": str(e)})

    def _respond(self, callback_id: str, data: dict):
        self.messageResponse.emit(callback_id, json.dumps(data))


# ─── Custom WebEnginePage (handles new tab requests) ───


class TabWebEnginePage(QWebEnginePage):
    """Custom page that opens link targets in new tabs."""

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self._create_tab_func: Callable[[], QWebEnginePage] | None = None

    def createWindow(self, window_type):
        """Called when JS does window.open() or user middle-clicks a link.

        Directly creates a new tab via callback and returns its page.
        Chromium will load the target URL into the returned page.
        Single page creation (fast) instead of temp-page URL capture (slow).
        """
        if self._create_tab_func:
            return self._create_tab_func()
        return None


# ─── Tabbed Browser Widget ───


HOME_URL = "https://discuss.eroscripts.com/c/scripts/free-scripts/14"


class BrowserWidget(QWidget):
    """Embedded tabbed browser with navigation bar and QWebChannel bridge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_profile()
        self._setup_ui()
        self._inject_scripts()
        # Restore previous session or open default tab
        self._restore_session()
        # Auto-refresh MEGA session in background
        self._refresh_mega_sid()

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

        self._bridge = BrowserBridge(
            cookie_store=self._profile.cookieStore(), parent=self
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
        """Restore browser tabs from previous session, or open default tab."""
        from PySide6.QtCore import QTimer
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        tabs = settings.browser_tabs
        scroll_positions = settings.browser_scroll_positions
        if tabs:
            for i, url_str in enumerate(tabs):
                try:
                    page = self.create_tab(QUrl(url_str))
                    scroll_y = scroll_positions[i] if i < len(scroll_positions) else 0
                    if scroll_y > 0:
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
                except Exception:
                    pass
            # Restore active tab index
            active = settings.browser_active_tab
            if 0 <= active < self._tabs.count():
                self._tabs.setCurrentIndex(active)
            logger.info("Restored %d browser tabs from previous session", self._tabs.count())
        else:
            self.create_tab(QUrl(HOME_URL))

    def save_session(self):
        """Save current browser tab URLs, scroll positions, and cookies."""
        from funpairdl.persistence.settings import Settings
        urls = []
        scroll_positions = []
        for i in range(self._tabs.count()):
            view = self._tabs.widget(i)
            if isinstance(view, QWebEngineView):
                url = view.url().toString()
                if url and url not in ("about:blank", ""):
                    urls.append(url)
                    scroll_y = view.page().scrollPosition().y()
                    scroll_positions.append(scroll_y)
        settings = Settings.load()
        settings.browser_tabs = urls
        settings.browser_active_tab = self._tabs.currentIndex()
        settings.browser_scroll_positions = scroll_positions
        settings.save()
        logger.info("Saved %d browser tabs for session restore", len(urls))

        # Force-save cookies on shutdown (synchronous best-effort).
        # The async CDP path is preferred but may not complete before exit,
        # so we fire-and-forget and rely on the periodic sync having saved
        # a recent backup.
        try:
            asyncio.ensure_future(self._bridge._cdp_cookie_sync())
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

    def create_tab(self, url: QUrl | None = None) -> TabWebEnginePage:
        """Create a new browser tab and return its page."""
        view = QWebEngineView()
        page = TabWebEnginePage(self._profile, view)

        # Setup QWebChannel for this page
        channel = QWebChannel(page)
        channel.registerObject("bridge", self._bridge)
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
        self._tabs.setCurrentIndex(idx)
        self._tabs.setUpdatesEnabled(True)

        # Connect signals
        view.titleChanged.connect(lambda title, v=view: self._on_title_changed(v, title))
        view.urlChanged.connect(lambda u, v=view: self._on_url_changed(v, u))
        page._create_tab_func = self._create_tab_for_window

        if url:
            view.setUrl(url)

        return page

    def _create_tab_for_window(self) -> TabWebEnginePage:
        """Called from createWindow — create a new tab and return its page.

        Chromium will load the target URL into the returned page.
        """
        return self.create_tab()

    def _close_tab(self, index: int):
        """Close tab at index. Keep at least one tab open.

        Strategy: switch to the destination tab FIRST so the main page
        starts re-rendering, then remove and defer-destroy the old tab.
        This prevents the main page from being blocked by cleanup work.
        """
        if self._tabs.count() <= 1:
            return

        widget = self._tabs.widget(index)

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
        for i in range(self._tabs.count() - 1, -1, -1):
            widget = self._tabs.widget(i)
            self._tabs.removeTab(i)
            if widget:
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
        """Update URL bar when switching tabs.

        Also re-show background tab QWebEngineViews that QTabWidget hid.
        When a QWebEngineView is hidden, Qt tells Chromium to stop compositing.
        Chromium then evicts rendering resources (textures, compositor layers).
        On re-show, it must re-rasterize the entire page — very slow for
        long-scrolled Discourse pages.

        By keeping all views 'visible' (stacked behind the active one),
        Chromium keeps their renderer warm and switching back is instant.
        """
        view = self._current_view()
        if view:
            self.url_bar.setText(view.url().toString())

        # Re-show any hidden tab views so Chromium keeps rendering them.
        # They're behind the active tab (same position in stacked layout),
        # so they don't affect visuals or receive input.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._keep_background_tabs_alive)

    def _keep_background_tabs_alive(self):
        """Re-show background QWebEngineViews that QTabWidget hid."""
        current = self._tabs.currentIndex()
        for i in range(self._tabs.count()):
            w = self._tabs.widget(i)
            if w and isinstance(w, QWebEngineView) and not w.isVisible():
                w.show()
                w.lower()  # behind the active tab

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

    def _refresh_mega_sid(self):
        """Background-load mega.nz in a hidden page to extract u_sid session.

        Safety: auto-cleanup after 120 seconds regardless of outcome.

        Flow:
        1. Load mega.nz → check if already logged in (u_sid exists)
        2. If not logged in and credentials in settings → navigate to login page
        3. Auto-fill email/password and submit
        4. Extract u_sid after login → save to config
        """
        from PySide6.QtCore import QTimer
        self._mega_phase = "check"  # "check" → "login_page" → "login_submit" → "done"
        self._mega_poll_count = 0
        self._mega_timer = None
        self._mega_page = QWebEnginePage(self._profile, self)
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
        from PySide6.QtCore import QTimer
        if not ok:
            logger.warning("MEGA session refresh: page load failed (phase=%s)", self._mega_phase)
            self._cleanup_mega_page()
            return

        if self._mega_phase == "check":
            # Phase 1: mega.nz loaded — poll for u_sid
            self._mega_poll_count = 0
            self._mega_timer = QTimer(self)
            self._mega_timer.setSingleShot(False)
            self._mega_timer.setInterval(2000)
            self._mega_timer.timeout.connect(self._poll_mega_sid)
            self._mega_timer.start()

        elif self._mega_phase == "login_page":
            # Phase 2: login page loaded — wait for form to render, then fill
            self._mega_poll_count = 0
            self._mega_timer = QTimer(self)
            self._mega_timer.setSingleShot(False)
            self._mega_timer.setInterval(1500)
            self._mega_timer.timeout.connect(self._try_fill_login)
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
        self._mega_poll_count += 1
        if self._mega_poll_count > 8:  # ~16s
            logger.info("MEGA session refresh: not logged in, attempting auto-login...")
            self._mega_timer.stop()
            self._mega_timer.deleteLater()
            self._mega_timer = None
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
            self._mega_timer.stop()
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
        from PySide6.QtCore import QTimer
        self._mega_timer = QTimer(self)
        self._mega_timer.setSingleShot(False)
        self._mega_timer.setInterval(2000)
        self._mega_timer.timeout.connect(self._try_js_login)
        self._mega_timer.start()

    def _try_js_login(self):
        """Phase 2: Wait for MEGA JS to load, then call login via their internal API."""
        self._mega_poll_count += 1
        if self._mega_poll_count > 15:  # ~30s
            logger.warning("MEGA session refresh: MEGA JS not ready, giving up")
            self._mega_timer.stop()
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
            self._mega_timer.stop()
            self._save_mega_sid(sid)
            self._cleanup_mega_page()
        elif status == "not_ready":
            pass  # Keep polling — MEGA JS not loaded yet
        elif status in ("login_called", "startLogin_called", "form_submitted", "enter_sent"):
            self._mega_timer.stop()
            self._mega_timer.deleteLater()
            self._mega_timer = None
            self._mega_phase = "login_submit"
            # MEGA key derivation takes ~10-20s, wait longer before polling
            from PySide6.QtCore import QTimer
            QTimer.singleShot(15000, self._start_post_login_poll)
        elif status.startswith("no_method:"):
            logger.warning("MEGA session refresh: no login method available: %s", status)

    def _start_post_login_poll(self):
        """Start polling for u_sid after login submit."""
        from PySide6.QtCore import QTimer
        self._mega_poll_count = 0
        self._mega_timer = QTimer(self)
        self._mega_timer.setSingleShot(False)
        self._mega_timer.setInterval(3000)  # 3s intervals — key derivation is slow
        self._mega_timer.timeout.connect(self._poll_mega_sid_after_login)
        self._mega_timer.start()

    def _poll_mega_sid_after_login(self):
        """Phase 3: Poll for u_sid after login attempt."""
        self._mega_poll_count += 1
        if self._mega_poll_count > 20:  # ~60s total (15s wait + 20×3s)
            logger.warning("MEGA session refresh: login failed (no u_sid after 60s)")
            self._mega_timer.stop()
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
            self._mega_timer.stop()
            logger.info("MEGA session refresh: login successful!")
            self._save_mega_sid(sid)
            self._cleanup_mega_page()

    def _save_mega_sid(self, sid: str):
        """Save extracted MEGA session ID to settings."""
        from funpairdl.persistence.settings import Settings
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
        """Clean up the hidden MEGA page and timers."""
        if hasattr(self, "_mega_timer") and self._mega_timer:
            self._mega_timer.stop()
            self._mega_timer.deleteLater()
            self._mega_timer = None
        if hasattr(self, "_mega_safety_timer") and self._mega_safety_timer:
            self._mega_safety_timer.stop()
            self._mega_safety_timer.deleteLater()
            self._mega_safety_timer = None
        if hasattr(self, "_mega_page") and self._mega_page:
            self._mega_page.deleteLater()
            self._mega_page = None
        self._mega_email = ""
        self._mega_password = ""

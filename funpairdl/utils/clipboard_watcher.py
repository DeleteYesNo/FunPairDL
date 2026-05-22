from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import QApplication

from funpairdl.persistence.settings import Settings
from funpairdl.utils.url_parser import extract_pixeldrain_urls

logger = logging.getLogger("funpairdl.utils.clipboard_watcher")


class ClipboardWatcher(QObject):
    """Watches the system clipboard for download-worthy URLs.

    Emits `urls_detected` with a deduped, ordered list of URLs whenever the
    clipboard changes and the new content matches one of the configured
    domains. The watcher itself does NOT decide what to do with the URLs —
    callers (MainWindow) connect to the signal and route to the picker.
    """

    urls_detected = Signal(list)  # list[str]
    duplicate_in_queue = Signal(list)  # URLs already present in queue, for tray hint

    def __init__(self, settings: Settings, queue_lookup=None, parent=None):
        super().__init__(parent)
        self._settings = settings
        # Callable that returns set[str] of URLs already in the queue,
        # so we can skip them per `clipboard_skip_in_queue`.
        self._queue_lookup = queue_lookup or (lambda: set())
        self._recent: dict[str, float] = {}  # url -> first-seen monotonic time
        self._clipboard: QClipboard | None = None
        self._connected = False

    def start(self) -> None:
        if self._connected:
            return
        app = QApplication.instance()
        if app is None:
            logger.warning("No QApplication; ClipboardWatcher cannot start")
            return
        self._clipboard = app.clipboard()
        self._clipboard.dataChanged.connect(self._on_clipboard_changed)
        self._connected = True
        logger.info("ClipboardWatcher started")

    def stop(self) -> None:
        if self._clipboard and self._connected:
            try:
                self._clipboard.dataChanged.disconnect(self._on_clipboard_changed)
            except (RuntimeError, TypeError):
                pass
        self._connected = False
        logger.info("ClipboardWatcher stopped")

    def refresh_settings(self, settings: Settings) -> None:
        """Replace the cached settings reference; called by SettingsDialog
        save handler so toggles take effect immediately."""
        self._settings = settings

    def trigger_manual_check(self) -> None:
        """Re-scan current clipboard contents on demand (tray menu)."""
        if self._clipboard is None:
            app = QApplication.instance()
            if app is None:
                return
            self._clipboard = app.clipboard()
        self._on_clipboard_changed(force=True)

    def _on_clipboard_changed(self, force: bool = False) -> None:
        s = self._settings
        if not force and not s.clipboard_watch_enabled:
            return
        if not force and s.clipboard_dnd_enabled:
            return
        if self._clipboard is None:
            return

        text = self._clipboard.text()
        if not text:
            return

        # Pull every Pixeldrain URL out of arbitrary text. Future domains
        # would each contribute their own extractor and we'd merge results.
        urls = extract_pixeldrain_urls(text) if self._domain_enabled("pixeldrain.com") else []
        if not urls:
            return

        # Dedupe by recent-seen window
        now = time.monotonic()
        ttl = max(0, int(s.clipboard_dedupe_seconds))
        # Garbage-collect expired entries
        if ttl > 0:
            self._recent = {u: t for u, t in self._recent.items() if now - t < ttl}

        fresh: list[str] = []
        for u in urls:
            if ttl > 0 and u in self._recent:
                continue
            self._recent[u] = now
            fresh.append(u)

        if not fresh:
            return

        # Skip URLs already present in queue
        if s.clipboard_skip_in_queue:
            in_queue = self._queue_lookup() or set()
            already, new = [], []
            for u in fresh:
                (already if u in in_queue else new).append(u)
            if already:
                self.duplicate_in_queue.emit(already)
            fresh = new

        if fresh:
            logger.info("Clipboard detected %d Pixeldrain URL(s)", len(fresh))
            self.urls_detected.emit(fresh)

    def _domain_enabled(self, domain: str) -> bool:
        return domain in (self._settings.clipboard_watch_domains or [])


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""

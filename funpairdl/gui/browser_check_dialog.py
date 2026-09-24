"""The window a download opens when its host builds the link in the page
(core.browser_assist): the page loads in the embedded browser's profile
(same cookies as the tabs). Most of the time the page produces the link by
itself within seconds and the window never shows; if it doesn't, the
window comes to the front so the user can pass the page's check (e.g. a
"verify you are human" box), and the download continues on its own.
"""
from __future__ import annotations

import json
import logging
import time

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from funpairdl.core.browser_assist import get_browser_assist

logger = logging.getLogger("funpairdl.gui.browser_check")

# How long the page gets to produce the link before the window shows up.
_QUIET_MS = 12000


class BrowserCheckDialog(QDialog):
    # (title, body) — the window needs the user: say so in the tray too.
    sig_needs_user = Signal(str, str)
    # The request is answered (window closing).
    sig_done = Signal()

    def __init__(self, profile, request: dict, parent=None):
        super().__init__(parent)
        self._req = request
        self._label = request.get("label") or request.get("site") or "download host"
        self._done = False
        self._deadline = time.monotonic() + float(request.get("timeout") or 600)
        self.setWindowTitle(f"{self._label} — 取得下載連結")
        self.resize(1000, 720)

        info = QLabel(
            f"正在向 <b>{self._label}</b> 取得下載連結：<br>"
            "如果下方頁面要求驗證（例如「我是人類」的勾選框），請直接在頁面裡完成；"
            "連結一出現就會自動關閉這個視窗並繼續下載。")
        info.setWordWrap(True)
        self._status = QLabel(request.get("url", ""))
        self._status.setStyleSheet("color: gray;")
        cancel = QPushButton("取消這個下載")
        cancel.clicked.connect(self.reject)

        self.view = QWebEngineView(self)
        # A page of our own in the shared profile: the tabs' cookies (and a
        # passed check) carry over; window.open popups (ads) go nowhere.
        self.page = QWebEnginePage(profile, self.view)
        self.view.setPage(self.page)

        top = QHBoxLayout()
        top.addWidget(info, 1)
        top.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._status)
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)
        # A child timer, so it dies with the window (a finished request
        # deletes it before the quiet period is over).
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(_QUIET_MS)
        self._reveal_timer.timeout.connect(self._reveal)

    def start(self) -> None:
        # Not shown yet: tell Chromium the page is visible anyway, or a
        # hidden page's timers crawl (see BrowserWidget._boost_view).
        try:
            self.page.setVisible(True)
        except (AttributeError, RuntimeError):
            pass
        self.view.setUrl(QUrl(self._req["url"]))
        self._timer.start()
        self._reveal_timer.start()

    # ── the page's link ──
    def _poll(self) -> None:
        if self._done:
            return
        if time.monotonic() > self._deadline:
            self._finish(None, "Timed out waiting for the browser check (retry opens it again)")
            return
        try:
            self.page.runJavaScript(self._req.get("extract_js", ""), self._on_result)
        except RuntimeError:
            pass

    def _on_result(self, res) -> None:
        if self._done or not res:
            return
        try:
            data = json.loads(res)
        except (TypeError, ValueError):
            return
        if isinstance(data, dict) and data.get("url"):
            logger.info("Browser check #%s: link found (%s)", self._req.get("id"), data["url"][:80])
            self._finish(data, "")

    # ── the user ──
    def _reveal(self) -> None:
        if self._done:
            return
        self.show()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self)
        logger.info("Browser check #%s: waiting for the user (%s)", self._req.get("id"), self._req["url"][:80])
        self.sig_needs_user.emit(f"{self._label}：需要人工驗證",
                                 "請在跳出的視窗裡完成驗證，完成後會自動繼續下載。")

    def reject(self) -> None:
        self._finish(None, "Browser check cancelled by the user")

    def closeEvent(self, event) -> None:
        self._finish(None, "Browser check cancelled by the user")
        super().closeEvent(event)

    def _finish(self, result: dict | None, error: str) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self._reveal_timer.stop()
        get_browser_assist().complete(self._req["id"], result, error)
        try:
            self.view.setUrl(QUrl("about:blank"))
        except RuntimeError:
            pass
        self.hide()
        self.sig_done.emit()
        self.deleteLater()

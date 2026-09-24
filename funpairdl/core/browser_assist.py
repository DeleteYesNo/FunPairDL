"""Links only a real browser can open, with the user at hand.

Some hosts hand out their download link only to a page running in a
browser (the link is built by the page's script, behind a human check such
as Cloudflare Turnstile). A provider for such a host asks for help here:
the GUI opens the page in the embedded browser — usually the page passes
by itself and the dialog never has to be looked at; when it asks for a
human check, the dialog pops up and the user does it — and the link the
page produces is handed back to the download.

Nothing here solves or skips a check: the user's own browser session does.

The download loop awaits `open()`; the GUI registers a handler that is
called from that loop's thread (it must hand the request to its own
thread, e.g. with a queued Qt signal) and answers with `complete()`.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from typing import Callable

logger = logging.getLogger("funpairdl.browser_assist")

# Error text the panel reads (content.js _deadKind): not a dead link.
NO_BROWSER_ERROR = "Needs the embedded browser: this host builds its download link in the page"

# Per host: the script that reads the finished link from the page (JSON
# {"url", "name"} or "" while the page is still working), and what the
# dialog tells the user.
SITES: dict[str, dict] = {
    "vikingfile": {
        "label": "ViKiNG FiLE",
        "extract_js": r"""(() => {
  const a = document.getElementById("download-link");
  const href = a && a.href || "";
  if (!/^https?:\/\/[^/]*vik(?:ing|1ng)file\.[a-z]+\/d\//i.test(href)) return "";
  const n = document.getElementById("filename");
  return JSON.stringify({ url: href, name: (n && n.innerText || "").trim() });
})()""",
    },
}


class BrowserAssist:
    def __init__(self) -> None:
        self._handler: Callable[[dict], None] | None = None
        self._pending: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def set_handler(self, handler: Callable[[dict], None] | None) -> None:
        self._handler = handler

    @property
    def available(self) -> bool:
        return self._handler is not None

    async def open(self, url: str, site: str, title: str = "", timeout: float = 600.0) -> dict:
        """The link the page at `url` produces: {"url", "name"}. Raises
        ValueError when there is no GUI, the user cancels, or `timeout`
        passes."""
        handler = self._handler
        if handler is None or site not in SITES:
            raise ValueError(NO_BROWSER_ERROR)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            req_id = next(self._ids)
            self._pending[req_id] = (loop, fut)
        request = {"id": req_id, "url": url, "site": site, "title": title,
                   "timeout": timeout, **{k: v for k, v in SITES[site].items()}}
        logger.info("Browser assist #%d: %s (%s)", req_id, url[:80], site)
        try:
            handler(request)
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout + 30)
        except asyncio.TimeoutError:
            raise ValueError("Timed out waiting for the browser check (retry opens it again)")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def complete(self, req_id: int, result: dict | None, error: str = "") -> None:
        """Answer request `req_id` (any thread): the page's link, or an error."""
        with self._lock:
            entry = self._pending.get(req_id)
        if entry is None:
            return
        loop, fut = entry

        def _set() -> None:
            if fut.done():
                return
            if result and result.get("url"):
                fut.set_result(result)
            else:
                fut.set_exception(ValueError(error or "Browser check cancelled"))

        loop.call_soon_threadsafe(_set)


_assist = BrowserAssist()


def get_browser_assist() -> BrowserAssist:
    return _assist

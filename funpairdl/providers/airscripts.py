from __future__ import annotations

import html
import logging
import re
import ssl
from urllib.parse import urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.airscripts")

# AIRscripts (airscripts.fun) share pages: /s/<id> shows a "Download
# <name>.funscript" button pointing at /s/<id>/get/<n> — the free single
# script, no account needed. Posts link the share page, not the file.
_HOST = "airscripts.fun"
_SHARE_RE = re.compile(r"^/s/([A-Za-z0-9_-]+)/?$")
_BASE = "https://airscripts.fun"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_HEADERS = {"User-Agent": _USER_AGENT}


def ssl_context() -> ssl.SSLContext | bool:
    """certifi's roots only: the default context also loads the Windows
    store, whose expired legacy root makes OpenSSL 1.1.1 reject this site's
    Let's Encrypt chain ("certificate has expired")."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return True


def share_id(url: str) -> str:
    """"<id>" of an airscripts.fun/s/<id> share page, else ""."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if (parsed.hostname or "").lower().removeprefix("www.") != _HOST:
        return ""
    m = _SHARE_RE.match(parsed.path or "")
    return m.group(1) if m else ""


def parse_download(page: str, sid: str) -> tuple[str, str]:
    """(file URL, filename) from a share page; ("", "") when it has no free
    download (a members-only script)."""
    m = re.search(
        r'href="(/s/' + re.escape(sid) + r'/get/[^"]+)"[^>]*>\s*Download\s+([^<]+?)\s*</a>', page or "")
    if not m:
        return "", ""
    return _BASE + html.unescape(m.group(1)), sanitize_filename(html.unescape(m.group(2)).strip())


async def fetch_download(url: str, session: aiohttp.ClientSession) -> tuple[str, str]:
    sid = share_id(url)
    if not sid:
        raise ValueError(f"Not an AIRscripts share URL: {url}")
    async with session.get(f"{_BASE}/s/{sid}", headers=_HEADERS, ssl=ssl_context(),
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 404:
            raise ValueError("AIRscripts script not found (404)")
        resp.raise_for_status()
        page = await resp.text()
    file_url, filename = parse_download(page, sid)
    if not file_url:
        raise ValueError("Paid content: AIRscripts script has no free download")
    return file_url, filename


class AirscriptsProvider(BaseProvider):
    """Provider for AIRscripts share pages (a funscript, not a video)."""

    @staticmethod
    def can_handle(url: str) -> bool:
        return bool(share_id(url))

    @property
    def name(self) -> str:
        return "airscripts"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        async with aiohttp.ClientSession() as session:
            file_url, filename = await fetch_download(url, session)
        logger.info("AIRscripts resolved: %s -> %s (%s)", url[:60], file_url, filename)
        return ResolvedFile(
            direct_url=file_url,
            filename=filename or f"airscripts {share_id(url)}.funscript",
            total_size=0,
            supports_range=False,
            headers={**_HEADERS, "Referer": url},
        )

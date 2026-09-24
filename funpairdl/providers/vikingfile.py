"""ViKiNG FiLE (vik1ngfile.site / vikingfile.com, ``/f/<id>`` pages).

The file page builds its download link in the browser (obfuscated script,
with a Cloudflare Turnstile check when the site wants one). The link is
read from the page in the embedded browser (core.browser_assist); it points
at ``vikingfile.com/d/...``, which redirects to a signed, expiring URL that
downloads with ranged GETs — a retry simply opens the page again.

The probe needs no browser: the page's title is the file name and its
Usenet button carries the size ("[1.37 GB]").
"""
from __future__ import annotations

import logging
import re
import ssl
from contextlib import asynccontextmanager
from html import unescape
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.vikingfile")

_HOSTS = ("vik1ngfile.site", "vikingfile.com")
_PAGE_RE = re.compile(r"^/f/[A-Za-z0-9]+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SIZE_RE = re.compile(r"\[\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*\]", re.IGNORECASE)
_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}

GONE_ERROR = "File not found (404) on ViKiNG FiLE"


def is_page(url: str) -> bool:
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    return host in _HOSTS and bool(_PAGE_RE.match(p.path or ""))


def parse_page(html: str) -> dict:
    """{name, size} from the page without running its script (size is the
    Usenet button's rounded figure)."""
    m = _TITLE_RE.search(html or "")
    name = unescape(m.group(1)).strip() if m else ""
    sm = _SIZE_RE.search(unquote(html or ""))
    size = int(float(sm.group(1)) * _UNITS[sm.group(2).lower()]) if sm else 0
    return {"name": name, "size": size}


@asynccontextmanager
async def _get(session: aiohttp.ClientSession, url: str, **kw):
    """GET, reissued once without TLS verification if the certificate is
    rejected (the site's chain fails Python's bundle; browsers accept it) —
    the same per-request fallback the pixeldrain provider uses."""
    try:
        async with session.get(url, **kw) as r:
            yield r
            return
    except (aiohttp.ClientSSLError, ssl.SSLError) as e:
        logger.warning("ViKiNG FiLE TLS certificate rejected (%s) — retrying without verification", e)
    async with session.get(url, ssl=False, **kw) as r:
        yield r


async def fetch_page(url: str, session: aiohttp.ClientSession) -> dict:
    async with _get(session, url, headers={"User-Agent": BROWSER_USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=25)) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        info = parse_page(await resp.text(errors="ignore"))
    if not info["name"]:
        raise ValueError(GONE_ERROR)
    return info


class VikingFileProvider(BaseProvider):
    # The user may be asked to pass a check in the browser: wait for them.
    resolve_timeout = 660

    @staticmethod
    def can_handle(url: str) -> bool:
        return is_page(url)

    @property
    def name(self) -> str:
        return "vikingfile"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        from funpairdl.core.browser_assist import get_browser_assist

        got = await get_browser_assist().open(url, "vikingfile", title=kwargs.get("title", ""))
        link = got["url"]
        headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": url}
        size = 0
        final = link
        async with aiohttp.ClientSession() as session:
            async with _get(session, link, headers={**headers, "Range": "bytes=0-0"},
                            allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 404:
                    raise ValueError(GONE_ERROR)
                resp.raise_for_status()
                final = str(resp.url)
                cr = resp.headers.get("Content-Range", "")
                tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
                size = int(tail) if tail.isdigit() else int(resp.headers.get("Content-Length", 0) or 0)
        name = got.get("name") or unquote(urlparse(final).path.rsplit("/", 1)[-1])
        logger.info("ViKiNG FiLE resolved: %s -> %s (%d bytes)", url[:60], name, size)
        # The page's link, not the signed redirect target: it stays valid
        # for the resume, the redirect is followed on each request.
        return ResolvedFile(direct_url=link, filename=sanitize_filename(name or "vikingfile_download"),
                            total_size=size, supports_range=True, headers=headers)

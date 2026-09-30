"""filester (filester.gg / filester.me /d/<slug>), an anonymous file host.

The page asks the site's public API for a short-lived token
(``POST /v2/api/public/download {"file_slug"}`` → server, file, token,
name) and downloads ``<server>/v2/<file>?token=…&download=true`` — a
plain file that answers ranged GETs.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.filester")

_HOSTS = ("filester.gg", "filester.me")
_SLUG_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")
_FALLBACK_CDN = "https://cn1.filester.me"

GONE_ERROR = "File not found (404) on filester — deleted or expired"


def file_slug(url: str) -> str:
    m = _SLUG_RE.search(urlparse(url).path or "")
    return m.group(1) if m else ""


def media_url(token_data: dict) -> str:
    base = (token_data.get("server") or _FALLBACK_CDN).rstrip("/")
    return f"{base}/v2/{token_data['file']}?token={token_data['token']}&download=true"


async def fetch_token(url: str, session: aiohttp.ClientSession) -> dict:
    slug = file_slug(url)
    if not slug:
        raise ValueError(f"Not a filester file URL: {url}")
    origin = f"{urlparse(url).scheme or 'https'}://{urlparse(url).hostname}"
    async with session.post(f"{origin}/v2/api/public/download", json={"file_slug": slug},
                            headers={"User-Agent": BROWSER_USER_AGENT},
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status in (404, 410):
            raise ValueError(GONE_ERROR)
        text = await resp.text()
        if resp.status != 200:
            raise ValueError(f"filester API error {resp.status}: {text[:120]}")
    import json
    data = json.loads(text)
    if not data.get("success") or not data.get("file") or not data.get("token"):
        raise ValueError(GONE_ERROR if "not found" in text.lower() else f"filester: no download token ({text[:120]})")
    return data


async def ranged_size(url: str, session: aiohttp.ClientSession) -> int:
    async with session.get(url, headers={"User-Agent": BROWSER_USER_AGENT, "Range": "bytes=0-0"},
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        cr = resp.headers.get("Content-Range", "")
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return int(tail) if tail.isdigit() else int(resp.headers.get("Content-Length", 0) or 0)


class FilesterProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return any(host == h or host.endswith("." + h) for h in _HOSTS) and bool(file_slug(url))

    @property
    def name(self) -> str:
        return "filester"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        async with aiohttp.ClientSession() as session:
            data = await fetch_token(url, session)
            direct = media_url(data)
            size = await ranged_size(direct, session)
        filename = sanitize_filename(data.get("name") or f"filester_{file_slug(url)}")
        logger.info("filester resolved: %s -> %s (%d bytes)", url[:60], filename, size)
        return ResolvedFile(direct_url=direct, filename=filename, total_size=size,
                            supports_range=True, headers={"User-Agent": BROWSER_USER_AGENT})

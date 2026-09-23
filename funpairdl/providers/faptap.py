"""Faptap (faptap.net/v/<id>), a funscript player site.

The page is an SPA; its API gives the video (``/api/videos/<id>``: name,
length, where the video really lives) and the site's own proxied mp4
streams per quality (``/api/videos/<id>/sources`` → ``stream?s=…`` under
/api/), which answer ranged GETs without any header.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.faptap")

_HOST = "faptap.net"
_API = "https://faptap.net/api/"
_ID_RE = re.compile(r"/v/(\d+)")

GONE_ERROR = "Video not found (404) on Faptap"


def video_id(url: str) -> str:
    m = _ID_RE.search(urlparse(url).path or "")
    return m.group(1) if m else ""


def parse_sources(data: list) -> list[dict]:
    """[{url, height}] of the proxied mp4 streams, lowest first."""
    out = []
    for s in data or []:
        u = (s or {}).get("url") or ""
        if not u or str(s.get("format") or "mp4").lower() != "mp4":
            continue
        try:
            h = int(str(s.get("quality") or "0").rstrip("p"))
        except ValueError:
            h = 0
        out.append({"url": u if u.startswith("http") else _API + u.lstrip("/"), "height": h})
    out.sort(key=lambda x: x["height"])
    return out


def select_source(sources: list[dict], preferred_resolution: str) -> dict | None:
    if not sources:
        return None
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        target = 0
    exact = [s for s in sources if s["height"] == target]
    return exact[-1] if exact else sources[-1]


async def _get_json(session: aiohttp.ClientSession, path: str):
    async with session.get(_API + path, headers={"User-Agent": BROWSER_USER_AGENT},
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        return ((await resp.json(content_type=None)) or {}).get("data")


async def fetch_video(url: str, session: aiohttp.ClientSession) -> dict:
    vid = video_id(url)
    if not vid:
        raise ValueError(f"Not a Faptap video URL: {url}")
    meta = await _get_json(session, f"videos/{vid}") or {}
    if not meta or meta.get("is_softdeleted"):
        raise ValueError(GONE_ERROR)
    sources = parse_sources(await _get_json(session, f"videos/{vid}/sources") or [])
    if not sources:
        raise ValueError(f"No playable source on Faptap: {url}")
    return {"id": vid, "name": str(meta.get("name") or ""), "duration": float(meta.get("duration") or 0),
            "sources": sources}


async def ranged_size(url: str, session: aiohttp.ClientSession) -> int:
    async with session.get(url, headers={"User-Agent": BROWSER_USER_AGENT, "Range": "bytes=0-0"},
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        cr = resp.headers.get("Content-Range", "")
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return int(tail) if tail.isdigit() else int(resp.headers.get("Content-Length", 0) or 0)


class FaptapProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return (host == _HOST or host.endswith("." + _HOST)) and bool(video_id(url))

    @property
    def name(self) -> str:
        return "faptap"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        async with aiohttp.ClientSession() as session:
            v = await fetch_video(url, session)
            src = select_source(v["sources"], kwargs.get("preferred_resolution", "best"))
            size = await ranged_size(src["url"], session)
        filename = sanitize_filename((v["name"] or f"faptap_{v['id']}") + ".mp4")
        logger.info("Faptap resolved: %s -> %sp (%d bytes)", url[:60], src["height"], size)
        return ResolvedFile(direct_url=src["url"], filename=filename, total_size=size,
                            supports_range=True, headers={"User-Agent": BROWSER_USER_AGENT})

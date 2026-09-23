"""WatchHentai (watchhentai.net) episode pages.

An episode page (``/videos/<slug>/``) embeds a player page
(``data-primary-player-url``) whose JW Player sources are obfuscated: each
``file`` is base64url → XOR with ``(13 + i % 17)`` → reversed → base64 again,
decoding to a plain mp4 on the site's storage host. The mp4 answers ranged
GETs only with the site as Referer, so the segmented downloader takes it
directly with that header.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.watchhentai")

_HOST = "watchhentai.net"
_REFERER = "https://watchhentai.net/"
_PLAYER_RE = re.compile(r'data-primary-player-url\s*=\s*"([^"]+)"', re.IGNORECASE)
_SOURCES_RE = re.compile(r"whJwSources\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ITEMPROP_DUR_RE = re.compile(r'itemprop="duration"\s+content="([^"]+)"', re.IGNORECASE)
_HEIGHT_RE = re.compile(r"(\d{3,4})p", re.IGNORECASE)
_ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", re.IGNORECASE)

GONE_ERROR = "Video not found (404) on WatchHentai"


def decode_source(s: str) -> str:
    """The player's whDecodeMediaUrl, in Python."""
    t = (s or "").replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    x = base64.b64decode(t)
    r = "".join(chr(b ^ ((13 + i % 17) & 255)) for i, b in enumerate(x))[::-1]
    r += "=" * (-len(r) % 4)
    return base64.b64decode(r).decode("utf-8")


def parse_player_url(html: str, page_url: str) -> str:
    m = _PLAYER_RE.search(html or "")
    return urljoin(page_url, unescape(m.group(1))) if m else ""


def parse_sources(player_html: str) -> list[dict]:
    """[{url, height, label}] from the player page, lowest first."""
    m = _SOURCES_RE.search(player_html or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        return []
    out = []
    for src in raw if isinstance(raw, list) else []:
        f = (src or {}).get("file") or ""
        if not f:
            continue
        try:
            url = f if f.startswith("http") else decode_source(f)
        except (ValueError, UnicodeDecodeError):
            continue
        label = str(src.get("label") or "")
        h = _HEIGHT_RE.search(label) or _HEIGHT_RE.search(url)
        out.append({"url": url, "height": int(h.group(1)) if h else 0, "label": label})
    out.sort(key=lambda s: s["height"])
    return out


def parse_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    t = unescape(m.group(1)).strip()
    return re.sub(r"\s*[-–—|]\s*Watch\s*Hentai\b.*$", "", t, flags=re.IGNORECASE)


def parse_duration(html: str) -> float:
    """Seconds from the episode page's itemprop duration ("PT16M00S"), else
    0. (The player page's VideoObject says PT24M50S for every episode — a
    template value, not the video's.) Minute precision at best: the probe
    reads the real length from the mp4 itself."""
    m = _ITEMPROP_DUR_RE.search(html or "")
    d = _ISO_DUR_RE.fullmatch(m.group(1).strip()) if m else None
    if not d or not any(d.groups()):
        return 0.0
    h, mi, sec = (float(g or 0) for g in d.groups())
    return h * 3600 + mi * 60 + sec


def select_source(sources: list[dict], preferred_resolution: str) -> dict | None:
    if not sources:
        return None
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        target = 0
    exact = [s for s in sources if s["height"] == target]
    return exact[-1] if exact else sources[-1]


async def ranged_size(url: str, session: aiohttp.ClientSession) -> int:
    """Total bytes from a 1-byte ranged GET (the host answers HEAD with 404)."""
    headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": _REFERER, "Range": "bytes=0-0"}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        cr = resp.headers.get("Content-Range", "")
        if "/" in cr:
            tail = cr.rsplit("/", 1)[1]
            if tail.isdigit():
                return int(tail)
        return int(resp.headers.get("Content-Length", 0) or 0)


async def fetch_episode(url: str, session: aiohttp.ClientSession) -> dict:
    """Title, duration and mp4 sources of an episode page."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        html = await resp.text(errors="ignore")
        page_url = str(resp.url)
    player = parse_player_url(html, page_url)
    if not player:
        raise ValueError(f"No player on WatchHentai page: {url}")
    async with session.get(player, headers={**headers, "Referer": page_url}, timeout=timeout) as resp:
        resp.raise_for_status()
        player_html = await resp.text(errors="ignore")
    sources = parse_sources(player_html)
    if not sources:
        raise ValueError(f"No video source in WatchHentai player: {url}")
    return {"title": parse_title(html), "duration": parse_duration(html),
            "sources": sources, "page_url": page_url}


def build_filename(title: str, mp4_url: str) -> str:
    if title:
        return sanitize_filename(title + ".mp4")
    name = unquote((urlparse(mp4_url).path or "").rsplit("/", 1)[-1])
    return sanitize_filename(name or "watchhentai_video.mp4")


class WatchHentaiProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        p = urlparse(url)
        host = (p.hostname or "").lower().removeprefix("www.")
        return (host == _HOST or host.endswith("." + _HOST)) and (p.path or "").startswith("/videos/")

    @property
    def name(self) -> str:
        return "watchhentai"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred = kwargs.get("preferred_resolution", "best")
        async with aiohttp.ClientSession() as session:
            ep = await fetch_episode(url, session)
            src = select_source(ep["sources"], preferred)
            size = await ranged_size(src["url"], session)
        filename = build_filename(ep["title"], src["url"])
        logger.info("WatchHentai resolved: %s -> %s (%d bytes)", url[:60], src["url"][:80], size)
        return ResolvedFile(
            direct_url=src["url"],
            filename=filename,
            total_size=size,
            supports_range=True,
            headers={"User-Agent": BROWSER_USER_AGENT, "Referer": _REFERER},
        )

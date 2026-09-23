"""The JOI Database (the-joi-database.com) video pages.

A watch page (``/watch/<24-hex id>``) plays an HLS stream whose master
playlist is ``/api/stream/<id>`` — anonymous for free videos, 401 for a
subscriber-only one. yt-dlp has no extractor for the page but reads the
manifest fine, so resolve hands the downloader the manifest (the page title
names the file) and the probe reads the variants for the size preview.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.joidb")

_HOST = "the-joi-database.com"
_ORIGIN = "https://www.the-joi-database.com"
_ID_RE = re.compile(r"/watch/([0-9a-f]{16,40})", re.IGNORECASE)
_VIDEO_TITLE_RE = re.compile(r'data-video-title\s*=\s*"([^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)\n\s*([^\s#][^\n]*)", re.IGNORECASE)
_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)", re.IGNORECASE)

# Error texts the panel reads (content.js _deadKind): "Paid content" and
# "not found" say the video can't be had; anything else is a plain failure.
PAID_ERROR = "Paid content: this video needs a The JOI Database subscription"
GONE_ERROR = "Video not found (404) on The JOI Database"


def video_id(url: str) -> str:
    m = _ID_RE.search(urlparse(url).path or "")
    return m.group(1).lower() if m else ""


def manifest_url(vid: str) -> str:
    return f"{_ORIGIN}/api/stream/{vid}"


def parse_title(html: str) -> str:
    """The page's own name for the file (the download button's title, which
    carries ".mp4"), else <title> without the site suffix. No extension."""
    m = _VIDEO_TITLE_RE.search(html or "")
    if m:
        t = unescape(m.group(1)).strip()
        return re.sub(r"\.(mp4|mkv|webm|mov)$", "", t, flags=re.IGNORECASE)
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    t = unescape(m.group(1)).strip()
    return re.sub(r"\s*[-–—|]\s*The\s*joi\s*Database\s*$", "", t, flags=re.IGNORECASE)


def parse_variants(master: str, base_url: str) -> list[dict]:
    """[{height, bandwidth, url}] from an HLS master playlist, lowest first."""
    out = []
    for attrs, uri in _STREAM_INF_RE.findall(master or ""):
        bw = re.search(r"(?<![A-Z-])BANDWIDTH=(\d+)", attrs)
        res = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
        name = re.search(r'NAME="(\d{3,4})"', attrs)
        height = int(res.group(1)) if res else (int(name.group(1)) if name else 0)
        out.append({"height": height, "bandwidth": int(bw.group(1)) if bw else 0,
                    "url": urljoin(base_url, uri.strip())})
    out.sort(key=lambda v: v["height"])
    return out


def playlist_duration(playlist: str) -> float:
    return round(sum(float(x) for x in _EXTINF_RE.findall(playlist or "")), 3)


def select_variant(variants: list[dict], preferred_resolution: str) -> dict | None:
    """Exact height when offered, else the best (what yt-dlp is asked for)."""
    if not variants:
        return None
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        target = 0
    exact = [v for v in variants if v["height"] == target]
    return exact[-1] if exact else variants[-1]


def estimate_size(variant: dict, duration: float) -> int:
    return int(variant.get("bandwidth", 0) * duration / 8) if duration else 0


async def fetch_stream_info(url: str, session: aiohttp.ClientSession) -> dict:
    """Title, variants and duration of a watch page's stream.

    Raises ValueError with PAID_ERROR / GONE_ERROR when the video can't be
    had, so the panel can tell "gone" and "paid" from "unsupported".
    """
    vid = video_id(url)
    if not vid:
        raise ValueError(f"Not a The JOI Database watch URL: {url}")
    headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": f"{_ORIGIN}/watch/{vid}"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(f"{_ORIGIN}/watch/{vid}", headers=headers, timeout=timeout) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        html = await resp.text(errors="ignore")
    title = parse_title(html)
    master_url = manifest_url(vid)
    async with session.get(master_url, headers=headers, timeout=timeout) as resp:
        if resp.status in (401, 402, 403):
            raise ValueError(PAID_ERROR)
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        master = await resp.text(errors="ignore")
    variants = parse_variants(master, master_url)
    if not variants:
        raise ValueError(f"No stream variants on The JOI Database page: {url}")
    duration = 0.0
    try:
        async with session.get(variants[-1]["url"], headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                duration = playlist_duration(await resp.text(errors="ignore"))
    except aiohttp.ClientError:
        pass
    return {"id": vid, "title": title, "manifest": master_url,
            "variants": variants, "duration": duration}


class JoiDbProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return (host == _HOST or host.endswith("." + _HOST)) and bool(video_id(url))

    @property
    def name(self) -> str:
        return "joidb"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred = kwargs.get("preferred_resolution", "best")
        async with aiohttp.ClientSession() as session:
            info = await fetch_stream_info(url, session)
        variant = select_variant(info["variants"], preferred)
        filename = sanitize_filename((info["title"] or f"joidb_{info['id']}") + ".mp4")
        size = estimate_size(variant, info["duration"])
        logger.info("The JOI Database resolved: %s -> %sp (~%d bytes)",
                    url[:60], variant["height"], size)
        return ResolvedFile(
            direct_url=info["manifest"],
            filename=filename,
            total_size=size,
            supports_range=False,
            headers={"User-Agent": BROWSER_USER_AGENT, "Referer": url},
            is_hls=True,
            # yt-dlp can't open the watch page; it reads the manifest.
            manifest_url=info["manifest"],
        )

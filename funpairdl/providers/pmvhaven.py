"""PMVHaven (pmvhaven.com/video/<slug>_<24-hex id>).

The page is a Nuxt app; its payload names the video's HLS master on the
site's object storage (``.../videos/<file>.mp4/master.m3u8``, one per page).
The directory is named after the upload itself, and that original mp4 sits
at the same key without the suffix — a plain file with ranged GETs. The
resolution setting picks like the yt-dlp provider: the stream of exactly
that height when the original is bigger (a 4K upload can be 6 GB where its
1080p stream is 0.5 GB), else the original.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.pmvhaven")

_HOST = "pmvhaven.com"
_MASTER_RE = re.compile(r'https://[a-z0-9.-]+/videos/[^"\s<>]+?\.(?:mp4|mkv|webm|mov)/master\.m3u8', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

GONE_ERROR = "Video not found (404) on PMVHaven"


def unescape_payload(html: str) -> str:
    """The Nuxt payload writes "/" as the JS escape backslash-u002F."""
    return (html or "").replace(chr(92) + "u002F", "/")


def parse_master(html: str) -> str:
    m = _MASTER_RE.search(unescape_payload(html))
    return m.group(0) if m else ""


def original_url(master: str) -> str:
    return master[: -len("/master.m3u8")] if master.endswith("/master.m3u8") else ""


def parse_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    t = unescape(m.group(1)).strip()
    return re.sub(r"\s*[-–—|]\s*PMVHaven\s*$", "", t, flags=re.IGNORECASE)


def pick(variants: list[dict], preferred_resolution: str) -> dict | None:
    """The stream to fetch instead of the original: one of exactly the
    preferred height, below the top one; None = take the original."""
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        return None
    top = max((v["height"] for v in variants), default=0)
    exact = [v for v in variants if v["height"] == target]
    return exact[-1] if exact and target < top else None


def build_filename(title: str, mp4_url: str) -> str:
    if title:
        return sanitize_filename(title + ".mp4")
    return sanitize_filename(unquote(urlparse(mp4_url).path.rsplit("/", 1)[-1]) or "pmvhaven_video.mp4")


async def ranged_size(url: str, session: aiohttp.ClientSession, headers: dict | None = None) -> int:
    h = {"User-Agent": BROWSER_USER_AGENT, **(headers or {}), "Range": "bytes=0-0"}
    async with session.get(url, headers=h, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        cr = resp.headers.get("Content-Range", "")
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        return int(tail) if tail.isdigit() else int(resp.headers.get("Content-Length", 0) or 0)


async def fetch_video(url: str, session: aiohttp.ClientSession) -> dict:
    headers = {"User-Agent": BROWSER_USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        html = await resp.text(errors="ignore")
    master = parse_master(html)
    if not master:
        raise ValueError(f"No video on PMVHaven page (removed or private): {url}")
    variants: list[dict] = []
    try:
        async with session.get(master, headers=headers, timeout=timeout) as resp:
            if resp.status == 200:
                from funpairdl.providers.joidb import parse_variants
                variants = parse_variants(await resp.text(errors="ignore"), master)
    except aiohttp.ClientError:
        pass
    return {"title": parse_title(html), "master": master, "mp4": original_url(master),
            "height": max((v["height"] for v in variants), default=0), "variants": variants}


class PmvHavenProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        p = urlparse(url)
        host = (p.hostname or "").lower().removeprefix("www.")
        return (host == _HOST or host.endswith("." + _HOST)) and (p.path or "").startswith("/video/")

    @property
    def name(self) -> str:
        return "pmvhaven"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        async with aiohttp.ClientSession() as session:
            v = await fetch_video(url, session)
            size = await ranged_size(v["mp4"], session)
            stream = pick(v["variants"], kwargs.get("preferred_resolution", "best"))
            duration = 0.0
            if stream:
                from funpairdl.utils.media_duration import probe_media_duration
                duration = await probe_media_duration(
                    v["mp4"], session, headers={"User-Agent": BROWSER_USER_AGENT}) or 0.0
        filename = build_filename(v["title"], v["mp4"])
        if stream:
            est = int(stream["bandwidth"] * duration / 8) if duration else 0
            logger.info("PMVHaven resolved: %s -> %sp stream (~%d bytes)", url[:60], stream["height"], est)
            return ResolvedFile(direct_url=v["master"], filename=filename, total_size=est,
                                supports_range=False, headers={"User-Agent": BROWSER_USER_AGENT},
                                is_hls=True, manifest_url=v["master"])
        logger.info("PMVHaven resolved: %s -> %s (%d bytes)", url[:60], v["mp4"][:80], size)
        return ResolvedFile(
            direct_url=v["mp4"], filename=filename,
            total_size=size, supports_range=True,
            headers={"User-Agent": BROWSER_USER_AGENT},
        )

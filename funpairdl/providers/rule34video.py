from __future__ import annotations

import logging
import re
import ssl
from html import unescape
from urllib.parse import urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.rule34video")

# The page's KVS player config lists every rendition in its flashvars:
#   video_url: 'https://…/get_file/…_360.mp4/?v-acctoken=…',  video_url_text: '360p',
#   video_alt_url: '…_480p.mp4/…', video_alt_url_text: '480p', video_alt_url2: …
# yt-dlp's Rule34Video extractor stopped finding formats (2026-10: "No video
# formats found!" on every page) while these lines are still there.
_SOURCE_RE = re.compile(r"""\b(video_url|video_alt_url\d*)\s*:\s*'([^']+)'""")
_LABEL_RE = re.compile(r"""\b(video_url|video_alt_url\d*)_text\s*:\s*'([^']*)'""")
_HEIGHT_RE = re.compile(r"(\d{3,4})p?\b")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DURATION_RE = re.compile(r'"duration"\s*:\s*"P?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?"')


def parse_sources(html: str) -> list[dict]:
    """[{url, height}] from the player's flashvars, lowest first."""
    labels = {k: v for k, v in _LABEL_RE.findall(html)}
    out: list[dict] = []
    seen: set[str] = set()
    for key, url in _SOURCE_RE.findall(html):
        url = unescape(url)
        if not url.startswith("http") or url in seen:
            continue  # "function/0/…" is the obfuscated KVS form — not seen on this site
        seen.add(url)
        m = _HEIGHT_RE.search(labels.get(key, "")) or re.search(r"_(\d{3,4})p?\.mp4", url)
        out.append({"url": url, "height": int(m.group(1)) if m else 0})
    out.sort(key=lambda s: s["height"])
    return out


def parse_duration(html: str) -> float | None:
    """Seconds from the page's JSON-LD ("PT0H3M00S"), else None."""
    m = _DURATION_RE.search(html)
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def parse_title(html: str) -> str:
    m = _TITLE_RE.search(html)
    return " ".join(unescape(m.group(1)).split()) if m else ""


def select_source(sources: list[dict], preferred_resolution: str) -> dict | None:
    """The preferred height if offered, else the highest."""
    if not sources:
        return None
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        return sources[-1]
    return next((s for s in sources if s["height"] == target), sources[-1])


async def fetch_page(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """(html, final url). A missing video page raises 'not found (404)' so
    the panel files the post as gone, not as a download failure."""
    async with session.get(
        url, headers={"User-Agent": BROWSER_USER_AGENT}, allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status == 404:
            raise ValueError(f"Rule34Video video not found (404): {url}")
        resp.raise_for_status()
        return await resp.text(errors="ignore"), str(resp.url)


async def head_file(session: aiohttp.ClientSession, url: str, headers: dict) -> tuple[str, int]:
    """(final url, size) of a get_file link. The files come from a CDN whose
    certificate has lapsed before (boomio-cdn); like the segment downloader,
    retry that host unverified only when its certificate is rejected."""
    async def _go(verify: bool):
        async with session.get(
            url, headers={**headers, "Range": "bytes=0-0"}, allow_redirects=True, ssl=verify,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            r.raise_for_status()
            rng = r.headers.get("Content-Range", "")
            size = int(rng.rsplit("/", 1)[-1]) if "/" in rng else int(r.headers.get("Content-Length") or 0)
            return str(r.url), size
    try:
        return await _go(True)
    except (aiohttp.ClientSSLError, ssl.SSLError) as e:
        logger.warning("Rule34Video CDN certificate rejected (%s) — retrying without verification", e)
        return await _go(False)


class Rule34VideoProvider(BaseProvider):
    """rule34video.com video pages: the mp4 renditions straight from the
    page's player config, downloaded in ranged segments."""

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return host == "rule34video.com" and "/video/" in urlparse(url).path

    @property
    def name(self) -> str:
        return "rule34video"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred = kwargs.get("preferred_resolution", "best")
        async with aiohttp.ClientSession() as session:
            html, page_url = await fetch_page(session, url)
            sources = parse_sources(html)
            src = select_source(sources, preferred)
            if not src:
                raise ValueError(f"No video sources on Rule34Video page: {url}")
            headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": page_url}
            final_url, size = await head_file(session, src["url"], headers)
        title = parse_title(html) or urlparse(page_url).path.rstrip("/").split("/")[-1]
        logger.info("Rule34Video resolved: %s -> %dp, %d bytes", url[:70], src["height"], size)
        return ResolvedFile(
            direct_url=final_url,
            filename=sanitize_filename(title + ".mp4"),
            total_size=size,
            supports_range=True,
            headers=headers,
        )

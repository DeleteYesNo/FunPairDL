from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.socigames")

# The player config emitted by the site's WordPress video block. Its `src`
# is the canonical source, so it wins over scraping the markup.
_BLOCK_ATTRS_RE = re.compile(r"block-attributes='([^']*)'", re.IGNORECASE)
_VIDEO_RE = re.compile(r"<video([^>]*)>(.*?)</video>", re.IGNORECASE | re.DOTALL)
_SOURCE_SRC_RE = re.compile(r"""<source[^>]*?\ssrc=["']([^"']+)["']""", re.IGNORECASE)
# Bunny Stream player. The site lazy-loads it, so the URL sits in data-src
# rather than src — which is exactly why yt-dlp's generic extractor, which
# only looks at src, finds nothing on these pages.
_BUNNY_RE = re.compile(
    r"""["'\s](?:data-)?src=["'](https?://iframe\.mediadelivery\.net/embed/[^"']+)["']""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEIGHT_RE = re.compile(r"(?:^|[_\-/.])(\d{3,4})p(?:[_\-/.]|$)", re.IGNORECASE)

_HOST = "socigames.com"


class SociGamesProvider(BaseProvider):
    """Provider for socigames.com video pages.

    Two things make this site unreachable through the generic path:

    1. Cloudflare answers plain HTTP clients with a managed challenge (403),
       so the page has to be fetched with a browser-impersonating TLS stack.
    2. The page lazy-loads its player (``perfmatters-lazy``), keeping the real
       source in ``data-src``/a JSON block attribute. yt-dlp's generic
       extractor reads the title fine but finds zero formats.

    Past the page, two player layouts appear. Most posts embed a progressive
    mp4 on a partner CDN (fappingstream.com, fapnationsafe.link) which the
    segment downloader can pull directly — those hosts are not behind
    Cloudflare. The rest embed Bunny Stream, which yt-dlp already handles via
    its BunnyCdn extractor, so that variant is delegated rather than reimplemented.
    """

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return host == _HOST or host.endswith("." + _HOST)

    @property
    def name(self) -> str:
        return "socigames"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred_resolution = kwargs.get("preferred_resolution", "best")
        html, page_url = await fetch_page(url)
        title = parse_page_title(html)

        candidates = parse_video_sources(html, page_url)
        if candidates:
            mp4_url = select_by_resolution(candidates, preferred_resolution)
            headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": page_url}
            total_size, supports_range, final_url = await _head(mp4_url, headers)
            logger.info(
                "SociGames resolved (direct mp4): %s -> %s (%d bytes)",
                url[:60], final_url[:80], total_size,
            )
            return ResolvedFile(
                direct_url=final_url,
                filename=_build_filename(title, final_url),
                total_size=total_size,
                supports_range=supports_range,
                headers=headers,
            )

        embed_url = parse_bunny_embed(html)
        if embed_url:
            # BunnyCdn extraction needs neither impersonation nor a Referer
            # (verified against the site), so the stock yt-dlp provider — and
            # with it the shared format-selection and HLS routing — applies.
            from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider

            logger.info("SociGames resolved (Bunny embed): %s -> %s", url[:60], embed_url[:80])
            resolved = await YtdlpGenericProvider().resolve(
                embed_url, preferred_resolution=preferred_resolution,
            )
            # Bunny names the asset after the post slug; the page title is the
            # human-readable one, so prefer it while keeping yt-dlp's extension.
            if title:
                ext = (resolved.filename.rsplit(".", 1)[-1]
                       if "." in resolved.filename else "mp4")
                resolved.filename = sanitize_filename(f"{title}.{ext}")
            return resolved

        raise ValueError(f"No video source found on SociGames page: {url}")


async def fetch_page(url: str) -> tuple[str, str]:
    """Fetch a socigames page past Cloudflare. Returns (html, final_url).

    curl_cffi is what yt-dlp itself uses for impersonation; without it there
    is no way through the challenge, so this raises instead of silently
    falling back to aiohttp and reporting a confusing 403.
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as e:
        raise RuntimeError(
            "socigames.com is behind Cloudflare and needs browser impersonation. "
            "Install curl-cffi (pip install curl-cffi) to download from this site."
        ) from e

    def _get():
        return cffi_requests.get(url, impersonate="chrome", timeout=60,
                                 allow_redirects=True)

    resp = await asyncio.to_thread(_get)
    if resp.status_code != 200:
        raise RuntimeError(f"SociGames page returned HTTP {resp.status_code}: {url}")
    return resp.text, str(resp.url)


async def _head(mp4_url: str, headers: dict[str, str]) -> tuple[int, bool, str]:
    """HEAD the media URL for size + range support. Falls back to the
    unverified size rather than failing the resolve — the segment downloader
    copes with an unknown length, but not with a dead item."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                mp4_url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                r.raise_for_status()
                return (
                    int(r.headers.get("Content-Length", 0)),
                    r.headers.get("Accept-Ranges", "").lower() == "bytes",
                    str(r.url),
                )
    except Exception as e:
        logger.warning("SociGames HEAD failed for %s: %s", mp4_url[:80], e)
        return 0, True, mp4_url


def parse_video_sources(html: str, page_url: str) -> list[str]:
    """Collect progressive media URLs for the post's own player.

    Every page also carries autoplaying ad banners in <video> tags; those are
    self-hosted under socigames.com, while the real video always lives on a
    partner CDN. Off-site is therefore the discriminator, applied to all three
    lookups so a layout change in any one of them can't let a banner through.
    """
    page_host = (urlparse(page_url).hostname or "").lower().removeprefix("www.")
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        full = urljoin(page_url, unescape(raw).strip())
        host = (urlparse(full).hostname or "").lower().removeprefix("www.")
        if not host or host == page_host or host.endswith("." + page_host):
            return
        if full not in seen:
            seen.add(full)
            out.append(full)

    for raw in _BLOCK_ATTRS_RE.findall(html):
        try:
            cfg = json.loads(unescape(raw))
        except (ValueError, TypeError):
            continue
        src = cfg.get("src")
        if isinstance(src, str) and src:
            add(src)

    for attrs, inner in _VIDEO_RE.findall(html):
        # `controls` marks the post's player; ad banners are autoplay/muted.
        if "controls" not in attrs.lower():
            continue
        for src in _SOURCE_SRC_RE.findall(inner):
            add(src)

    for _attrs, inner in _VIDEO_RE.findall(html):
        for src in _SOURCE_SRC_RE.findall(inner):
            add(src)

    return out


def parse_bunny_embed(html: str) -> str:
    """Return the Bunny Stream embed URL for pages that use it, else ""."""
    m = _BUNNY_RE.search(html)
    return unescape(m.group(1)) if m else ""


def parse_page_title(html: str) -> str:
    """Parse <title> and strip the site suffix (e.g. 'Foo » SOCIGAMES')."""
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    title = unescape(m.group(1)).strip()
    return re.sub(r"\s*[»|\-–—]\s*SOCIGAMES\s*$", "", title, flags=re.IGNORECASE).strip()


def height_from_url(url: str) -> int:
    m = _HEIGHT_RE.search(url)
    return int(m.group(1)) if m else 0


def select_by_resolution(candidates: list[str], preferred_resolution: str) -> str:
    """Pick a media URL matching the requested height, mirroring
    hmvmania/_select_format: exact match wins, else the highest available."""
    if not candidates:
        return ""
    ranked = sorted(candidates, key=height_from_url)
    if not preferred_resolution or preferred_resolution == "best":
        return ranked[-1]
    try:
        target = int(preferred_resolution)
    except (ValueError, TypeError):
        return ranked[-1]
    exact = [c for c in candidates if height_from_url(c) == target]
    return exact[-1] if exact else ranked[-1]


def _build_filename(title: str, mp4_url: str) -> str:
    from urllib.parse import unquote

    path = urlparse(mp4_url).path
    url_name = unquote(path.split("/")[-1]) if path else ""
    ext = url_name.rsplit(".", 1)[-1].lower() if "." in url_name else "mp4"
    if ext not in ("mp4", "mkv", "webm", "mov", "m4v"):
        ext = "mp4"
    if title:
        return sanitize_filename(f"{title}.{ext}")
    if url_name:
        return sanitize_filename(url_name)
    return "socigames_video.mp4"

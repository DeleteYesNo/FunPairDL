from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.ytdlp_generic")

# Domains NOT to handle with yt-dlp (handled by specialized providers)
_SKIP_DOMAINS = [
    "pixeldrain.com",
    "mega.nz",
    "mega.co.nz",
    "gofile.io",
    "discuss.eroscripts.com",
    "eroscripts-discourse.eroscripts.com",
    "hmvmania.com",
]


class YtdlpGenericProvider(BaseProvider):
    """Generic provider using yt-dlp for video site extraction.

    Acts as a broad catch-all for any video hosting site that yt-dlp supports
    (PornHub, XVideos, xHamster, Rule34, Hanime1, etc.).
    Only URLs handled by specialized providers (Pixeldrain, MEGA, etc.) are skipped.
    """

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        # Skip domains handled by specialized providers
        if any(domain in host for domain in _SKIP_DOMAINS):
            return False
        # Skip direct file downloads (e.g. CDN URLs ending in .mp4/.funscript)
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in (".mp4", ".mkv", ".avi", ".funscript", ".zip", ".rar")):
            return False
        # Handle everything else — yt-dlp supports 1000+ sites
        return True

    @property
    def name(self) -> str:
        return "ytdlp"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        url = _normalize_url(url)
        preferred_resolution = kwargs.get("preferred_resolution", "best")

        def _extract():
            import yt_dlp
            base_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
            }

            # Strategy 1: impersonation (best for Cloudflare-protected sites)
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts = {**base_opts, "impersonate": ImpersonateTarget(client="chrome")}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except ImportError:
                pass  # curl_cffi not installed
            except Exception as e1:
                logger.debug("yt-dlp impersonation failed for %s: %s", url[:60], e1)

            # Strategy 2: clean extraction (no impersonation, no cookies)
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=120)
        except asyncio.TimeoutError:
            raise TimeoutError(f"yt-dlp extraction timed out after 120s for: {url[:80]}")

        if not info:
            raise ValueError(f"yt-dlp could not extract info from: {url}")

        # Get direct video URL
        video_url = info.get("url", "")
        selected_format = None
        if not video_url:
            formats = info.get("formats", [])
            if formats:
                selected_format = _select_format(formats, preferred_resolution)
                video_url = selected_format.get("url", "")

        if not video_url:
            raise ValueError(f"No video URL found for: {url}")

        title = info.get("title", "video")
        ext = (selected_format or info).get("ext", info.get("ext", "mp4"))
        height = _get_height(selected_format) if selected_format else 0
        if height:
            filename = sanitize_filename(f"{title} [{height}p].{ext}")
        else:
            filename = sanitize_filename(f"{title}.{ext}")

        filesize = 0
        if selected_format:
            filesize = selected_format.get("filesize") or selected_format.get("filesize_approx") or 0
        if not filesize:
            filesize = info.get("filesize") or info.get("filesize_approx") or 0

        is_hls = video_url.endswith(".m3u8") or ".m3u8?" in video_url

        # Sites that require yt-dlp for download (not just extraction):
        # Bilibili uses DASH with authenticated CDN URLs that reject direct HTTP.
        source_host = (urlparse(url).hostname or "").lower()
        YTDLP_DOWNLOAD_REQUIRED = ["bilibili.com", "b23.tv"]
        if any(d in source_host for d in YTDLP_DOWNLOAD_REQUIRED):
            is_hls = True  # Forces yt-dlp download path (handles DASH merge + auth)

        http_headers = info.get("http_headers", {})

        logger.info(
            "yt-dlp resolved: %s -> %sp (%d bytes, hls=%s)",
            url[:60], height or "best", filesize, is_hls,
        )

        return ResolvedFile(
            direct_url=video_url,
            filename=filename,
            total_size=filesize,
            supports_range=not is_hls,
            is_hls=is_hls,
            headers=http_headers,
        )


def _normalize_url(url: str) -> str:
    """Fix known URL patterns that yt-dlp doesn't support directly."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    # hanime1.me/download?v=ID → hanime1.me/watch?v=ID
    if "hanime1.me" in host and parsed.path.rstrip("/") == "/download":
        url = url.replace("/download?", "/watch?", 1)
        logger.info("Normalized hanime1 URL: download → watch")
    return url


def _get_height(fmt: dict) -> int:
    """Get height from format metadata or parse from URL."""
    import re
    h = fmt.get("height") or 0
    if h:
        return h
    url = fmt.get("url", "")
    m = re.search(r'[-_](\d{3,4})p?\.', url)
    if m:
        return int(m.group(1))
    return 0


def _select_format(formats: list[dict], preferred_resolution: str) -> dict:
    """Select format: exact match if available, otherwise best (highest) quality."""
    # Filter for formats with both video and audio
    candidates = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("acodec") != "none"
    ]
    if not candidates:
        candidates = formats

    if not preferred_resolution or preferred_resolution == "best":
        return candidates[-1]

    try:
        target = int(preferred_resolution)
    except (ValueError, TypeError):
        return candidates[-1]

    # Exact match → use it; otherwise → best quality
    exact = [f for f in candidates if _get_height(f) == target]
    if exact:
        return exact[-1]

    return candidates[-1]

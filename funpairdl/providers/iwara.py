from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.iwara")


class IwaraProvider(BaseProvider):
    """Provider for Iwara.tv. Uses yt-dlp for URL extraction.
    Iwara serves HLS streams - the user currently downloads with
    'Download HLS Streams' browser extension.
    We'll support both yt-dlp extraction and HLS download."""

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "iwara.tv" in host

    @property
    def name(self) -> str:
        return "iwara"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        """Try to resolve Iwara video URL using yt-dlp."""

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

            # Strategy 2: clean extraction
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=120)
        except Exception as e:
            logger.warning("yt-dlp failed for Iwara URL %s: %s", url, e)
            # Fallback: return URL as-is, user may need browser extension
            return ResolvedFile(
                direct_url=url,
                filename=sanitize_filename(url.split("/")[-1]) + ".mp4",
                total_size=0,
                supports_range=False,
                is_hls=True,
            )

        if not info:
            raise ValueError(f"Could not extract info from: {url}")

        # Get the best format URL
        video_url = info.get("url", "")
        if not video_url:
            formats = info.get("formats", [])
            if formats:
                # Pick best quality
                best = formats[-1]
                video_url = best.get("url", "")

        title = info.get("title", "iwara_video")
        ext = info.get("ext", "mp4")
        filename = sanitize_filename(f"{title}.{ext}")
        filesize = info.get("filesize") or info.get("filesize_approx") or 0

        # Check if it's HLS
        is_hls = False
        if video_url.endswith(".m3u8") or ".m3u8?" in video_url:
            is_hls = True

        http_headers = info.get("http_headers", {})

        return ResolvedFile(
            direct_url=video_url,
            filename=filename,
            total_size=filesize,
            supports_range=not is_hls,
            is_hls=is_hls,
            headers=http_headers,
        )

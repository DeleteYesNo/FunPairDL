from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.direct_http")


class DirectHTTPProvider(BaseProvider):
    """Fallback provider for direct HTTP/HTTPS downloads."""

    @staticmethod
    def can_handle(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https")

    @property
    def name(self) -> str:
        return "direct"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        headers = kwargs.get("headers", {})
        async with aiohttp.ClientSession() as session:
            async with session.head(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()

                final_url = str(resp.url)
                total_size = int(resp.headers.get("Content-Length", 0))
                accept_ranges = resp.headers.get("Accept-Ranges", "")
                supports_range = accept_ranges.lower() == "bytes"

                # Extract filename
                filename = ""
                cd = resp.headers.get("Content-Disposition", "")
                if cd:
                    from funpairdl.utils.filename import parse_content_disposition
                    filename = parse_content_disposition(cd)

                if not filename:
                    path = urlparse(final_url).path
                    filename = unquote(path.split("/")[-1]) if path else "download"

                filename = sanitize_filename(filename) if filename else "download"

                return ResolvedFile(
                    direct_url=final_url,
                    filename=filename,
                    total_size=total_size,
                    supports_range=supports_range,
                )

from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import parse_content_disposition, sanitize_filename

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
        # Always send a browser User-Agent. Some direct hosts (e.g. catbox.moe)
        # close the connection on UA-less requests, and the UA must travel with
        # the actual segment GETs too — so we return it in ResolvedFile.headers.
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("User-Agent", BROWSER_USER_AGENT)

        final_url = url
        total_size = 0
        supports_range = False
        filename = ""

        async with aiohttp.ClientSession() as session:
            # 1. Cheap HEAD first. Tolerate failure/blank metadata — catbox.moe
            #    answers HEAD with Content-Length: 0 and no Accept-Ranges, and
            #    some hosts reject HEAD outright. The ranged GET below recovers.
            try:
                async with session.head(
                    url, headers=headers, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        final_url = str(resp.url)
                        total_size = int(resp.headers.get("Content-Length", 0) or 0)
                        supports_range = (
                            resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                        )
                        cd = resp.headers.get("Content-Disposition", "")
                        if cd:
                            filename = parse_content_disposition(cd)
            except Exception as e:
                logger.debug("HEAD failed for %s (%s); trying ranged GET", url[:80], e)

            # 2. If HEAD gave no size or no range info, probe with a 1-byte
            #    ranged GET. A 206 + Content-Range reveals the true total size
            #    and proves range support even when Accept-Ranges is omitted.
            if total_size <= 0 or not supports_range:
                try:
                    probe_headers = {**headers, "Range": "bytes=0-0"}
                    async with session.get(
                        url, headers=probe_headers, allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        resp.raise_for_status()
                        final_url = str(resp.url)
                        content_range = resp.headers.get("Content-Range", "")
                        if resp.status == 206 and "/" in content_range:
                            supports_range = True
                            try:
                                total_size = int(content_range.rsplit("/", 1)[-1])
                            except ValueError:
                                pass
                        if total_size <= 0:
                            # 200 (no range support): Content-Length is the full size
                            total_size = int(resp.headers.get("Content-Length", 0) or 0)
                        if not filename:
                            cd = resp.headers.get("Content-Disposition", "")
                            if cd:
                                filename = parse_content_disposition(cd)
                except Exception as e:
                    # Re-raise so _resolve_item surfaces a real error instead of
                    # silently producing an unusable ResolvedFile.
                    logger.error("Ranged GET probe failed for %s: %s", url[:80], e)
                    raise

        if not filename:
            path = urlparse(final_url).path
            filename = unquote(path.split("/")[-1]) if path else "download"
        filename = sanitize_filename(filename) if filename else "download"

        return ResolvedFile(
            direct_url=final_url,
            filename=filename,
            total_size=total_size,
            supports_range=supports_range,
            headers=headers,
        )

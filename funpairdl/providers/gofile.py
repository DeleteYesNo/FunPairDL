from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.gofile")

GOFILE_API = "https://api.gofile.io"


class GoFileProvider(BaseProvider):
    """Provider for GoFile file hosting. Supports paid API token."""

    def __init__(self, token: str = ""):
        self.token = token

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return "gofile.io" in host

    @property
    def name(self) -> str:
        return "gofile"

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        """Return configured token, or create a guest account as fallback."""
        if self.token:
            return self.token
        async with session.post(
            f"{GOFILE_API}/accounts",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            token = data.get("data", {}).get("token", "")
            if not token:
                raise ValueError("Failed to create GoFile guest account")
            logger.info("Created GoFile guest account")
            return token

    async def _get_website_token(self, session: aiohttp.ClientSession) -> str:
        """Extract the website token (wt) from GoFile's JS."""
        import re

        js_urls = [
            "https://gofile.io/dist/js/alljs.js",
            "https://gofile.io/dist/js/global.js",
        ]

        for js_url in js_urls:
            try:
                async with session.get(
                    js_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    # Try multiple patterns GoFile has used
                    patterns = [
                        r'fetchData\s*\(\s*["\']wt["\']\s*,\s*["\']([^"\']+)["\']',
                        r'wt\s*:\s*["\']([a-zA-Z0-9]+)["\']',
                        r'websiteToken\s*=\s*["\']([a-zA-Z0-9]+)["\']',
                        r'["\']wt["\']\s*,\s*["\']([a-zA-Z0-9]+)["\']',
                    ]
                    for pat in patterns:
                        m = re.search(pat, text)
                        if m:
                            token = m.group(1)
                            logger.info("GoFile wt token extracted: %s from %s", token[:8], js_url)
                            return token
            except Exception as e:
                logger.debug("Failed to fetch %s: %s", js_url, e)

        logger.warning("Could not extract GoFile wt token, using fallback")
        return "4fd6sg89d7s6"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        content_id = self._extract_content_id(url)
        if not content_id:
            raise ValueError(f"Cannot extract GoFile content ID from: {url}")

        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            wt = await self._get_website_token(session)

            async with session.get(
                f"{GOFILE_API}/contents/{content_id}?wt={wt}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"GoFile API returned status {resp.status}")
                data = await resp.json()

            if data.get("status") != "ok":
                raise ValueError(f"GoFile API error: {data.get('status')}, data={data}")

            content_data = data.get("data", {})

            # GoFile may return a single file directly or a folder with children
            content_type = content_data.get("type", "")

            if content_type == "file" or "link" in content_data:
                # Single file response — use content_data directly as the target
                target = content_data
                logger.info("GoFile single file: %s (%d bytes)",
                            content_data.get("name", "?"), content_data.get("size", 0))
            else:
                # Folder response — look for children
                children = content_data.get("children", content_data.get("contents", {}))

                if isinstance(children, list):
                    files = [c for c in children if c.get("type") == "file"]
                elif isinstance(children, dict):
                    files = [c for c in children.values() if c.get("type") == "file"]
                else:
                    files = []

                if not files:
                    logger.error("GoFile returned no files. content keys: %s",
                                 list(content_data.keys()))
                    raise ValueError(f"No files found in GoFile content (wt={wt[:8]}...)")

                # Find the largest video file (most likely the main content)
                video_exts = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".wmv", ".m4v"}
                video_files = [
                    f for f in files
                    if any(f.get("name", "").lower().endswith(ext) for ext in video_exts)
                ]
                target = max(video_files or files, key=lambda f: f.get("size", 0))

            filename = sanitize_filename(target.get("name", f"{content_id}.mp4"))
            total_size = target.get("size", 0)
            direct_link = target.get("directLink") or target.get("link", "")

            if not direct_link:
                # Construct download URL from server info
                server = (target.get("serverSelected")
                          or target.get("serverChoosen")
                          or (target.get("servers", ["store1"])[0]
                              if isinstance(target.get("servers"), list) else "store1"))
                direct_link = f"https://{server}.gofile.io/download/web/{content_id}/{filename}"

            headers = {
                "Cookie": f"accountToken={token}",
                "Accept": "*/*",
            }

            logger.info("GoFile resolved: %s -> %s (%d bytes)", url[:60], filename, total_size)

            return ResolvedFile(
                direct_url=direct_link,
                filename=filename,
                total_size=total_size,
                supports_range=True,
                headers=headers,
            )

    @staticmethod
    def _extract_content_id(url: str) -> str | None:
        """Extract content ID from GoFile URL like https://gofile.io/d/{id}"""
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "d":
            return parts[1]
        if parts:
            return parts[-1]
        return None

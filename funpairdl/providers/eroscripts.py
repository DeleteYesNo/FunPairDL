from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.eroscripts")


class EroScriptsProvider(BaseProvider):
    """Provider for EroScripts Discourse attachments.

    Short-urls (/uploads/short-url/) require authentication cookies.
    Cookies are saved by the embedded browser bridge or can be extracted
    from the user's browser via cookies_from_browser setting.
    """

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "discuss.eroscripts.com" in host

    @property
    def name(self) -> str:
        return "eroscripts"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        import asyncio

        # Get EroScripts cookies from settings (saved by embedded browser bridge)
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        cookie_str = settings.eroscripts_cookies

        # If no saved cookies, try extracting from browser (in thread — browser_cookie3 is slow)
        if not cookie_str and settings.cookies_from_browser:
            cookie_str = await asyncio.to_thread(
                self._extract_browser_cookies, settings.cookies_from_browser
            )

        headers = {"User-Agent": BROWSER_USER_AGENT}
        if cookie_str:
            headers["Cookie"] = cookie_str

        async with aiohttp.ClientSession() as session:
            async with session.head(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 404 and "short-url" in url:
                    hint = " (no cookies)" if not cookie_str else ""
                    raise ValueError(
                        f"EroScripts short-URL returned 404{hint}. "
                        "Please log into EroScripts in the embedded browser first."
                    )
                resp.raise_for_status()

                # Capture Set-Cookie from response chain to prevent Discourse
                # auth-token rotation from invalidating the browser session.
                # When Discourse rotates _t, the new value is in Set-Cookie —
                # we must propagate it back to settings so the browser stays in sync.
                self._sync_rotated_cookies(resp, cookie_str, settings)

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

                # Ensure .funscript extension from original URL
                if not filename.endswith(".funscript"):
                    orig_path = urlparse(url).path
                    if ".funscript" in orig_path:
                        filename = unquote(orig_path.split("/")[-1])

                filename = sanitize_filename(filename) if filename else "download"

                logger.info(
                    "EroScripts resolved: %s -> %s (%d bytes)",
                    url[:60], final_url[:60], total_size,
                )

                return ResolvedFile(
                    direct_url=final_url,
                    filename=filename,
                    total_size=total_size,
                    supports_range=supports_range,
                    # Pass cookies for the download phase too
                    headers=headers if headers else None,
                )

    @staticmethod
    def _sync_rotated_cookies(resp, cookie_str: str, settings) -> None:
        """Propagate Set-Cookie from Discourse response back to settings.

        Discourse rotates the _t auth token periodically. If we don't capture
        the new value, the browser's old _t becomes invalid → user gets logged out.
        """
        if not cookie_str:
            return

        # Collect Set-Cookie from all responses in the redirect chain
        all_responses = list(resp.history) + [resp]
        updated = {}
        for r in all_responses:
            for sc in r.headers.getall("Set-Cookie", []):
                # Parse "name=value; path=/; ..." — only take the name=value part
                kv = sc.split(";", 1)[0].strip()
                if "=" not in kv:
                    continue
                name, _, value = kv.partition("=")
                name = name.strip()
                # Skip Set-Cookie attributes that look like cookie names
                if name.lower() in ("path", "domain", "expires", "max-age", "samesite"):
                    continue
                updated[name] = value

        if not updated:
            return

        # Merge with existing cookies
        existing = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                n, _, v = part.partition("=")
                existing[n.strip()] = v

        changed = False
        for name, value in updated.items():
            if existing.get(name) != value:
                existing[name] = value
                changed = True
                logger.info("Cookie rotated by server: %s", name)

        if changed:
            new_cookie_str = "; ".join(f"{n}={v}" for n, v in existing.items())
            settings.eroscripts_cookies = new_cookie_str
            settings.save()
            logger.info("Saved rotated cookies (%d chars)", len(new_cookie_str))

    @staticmethod
    def _extract_browser_cookies(browser: str) -> str:
        """Try to extract EroScripts cookies from the user's browser."""
        try:
            import browser_cookie3
            jar = getattr(browser_cookie3, browser, browser_cookie3.chrome)()
            parts = []
            for cookie in jar:
                if "eroscripts.com" in (cookie.domain or ""):
                    parts.append(f"{cookie.name}={cookie.value}")
            if parts:
                logger.info("Extracted %d EroScripts cookies from %s", len(parts), browser)
                return "; ".join(parts)
        except Exception as e:
            logger.debug("Could not extract browser cookies: %s", e)
        return ""

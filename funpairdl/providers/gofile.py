from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from urllib.parse import urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.providers import gofile_wt
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.gofile")

GOFILE_API = "https://api.gofile.io"

# Language advertised as X-BL. It feeds the website-token hash, so it has to
# match what gofile_wt was told.
GOFILE_LANGUAGE = "en-US"

# Query parameters the site sends on /contents. pageSize matters: without it
# large folders come back truncated.
CONTENTS_PARAMS = {
    "contentFilter": "",
    "page": "1",
    "pageSize": "1000",
    "sortField": "createTime",
    "sortDirection": "-1",
}


def api_headers(account_token: str, website_token: str) -> dict[str, str]:
    """Headers for a /contents request, mirroring GoFile's own front end.

    The User-Agent is not decoration: it is mixed into the website token, so
    the value sent here must be the one gofile_wt hashed.
    """
    return {
        "Authorization": f"Bearer {account_token}",
        "X-Website-Token": website_token,
        "X-BL": GOFILE_LANGUAGE,
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "*/*",
    }

# --- Module-level token TTL caches (audit [7]) ------------------------------
# The guest account token and the website token (wt) are stable for long
# stretches, but were re-fetched on EVERY probe and EVERY resolve (guest POST
# 10s + JS-bundle scrape up to 2x10s each time). Caching at module level means
# both the /probe path (which calls _get_token/_get_website_token directly)
# and resolve() share the same entries, across provider instances and event
# loops. Entries are dropped via invalidate_token_cache() on auth failures so
# a rotated/expired token self-heals on the next attempt.
TOKEN_CACHE_TTL_SECONDS = 30 * 60

_cache_lock = threading.Lock()
_guest_token_cache: tuple[str, float] | None = None  # (token, fetched_at)


def _cache_get(entry: tuple[str, float] | None) -> str | None:
    if entry and (time.monotonic() - entry[1]) < TOKEN_CACHE_TTL_SECONDS:
        return entry[0]
    return None


def invalidate_token_cache() -> None:
    """Drop the cached guest account token and the website-token cache."""
    global _guest_token_cache
    with _cache_lock:
        _guest_token_cache = None
    gofile_wt.invalidate()


def _describe_error(status_code: int, api_status: str | None) -> str:
    """Turn GoFile's terse status strings into something actionable.

    Two of them are actively misleading: `error-notPremium` is about the
    website token rather than the account tier, and `error-rateLimit` is the
    step before GoFile null-routes the IP for a while.
    """
    s = (api_status or "").lower()
    if "notpremium" in s:
        return (
            "GoFile rejected the request as error-notPremium. Despite the name "
            "this is the website-token check, not the account tier — GoFile has "
            "most likely rotated the salt in wt.obf.js. Retry; if it persists "
            "the token generator changed."
        )
    if "ratelimit" in s:
        return (
            "GoFile rate-limited this client (error-rateLimit). Wait before "
            "retrying — repeated failures get the IP temporarily blocked."
        )
    if "notfound" in s:
        return "GoFile content not found — the link has expired or was deleted."
    if api_status:
        return f"GoFile API error: {api_status} (HTTP {status_code})"
    return f"GoFile API returned HTTP {status_code}"


def _is_auth_error(status_code: int, api_status: str | None) -> bool:
    """Does this response mean 'retry with fresh tokens'?

    `error-notPremium` is included deliberately: despite its name it is what
    GoFile returns for a missing or stale X-Website-Token, and it appears for
    guest and paid accounts alike. Treating it as an auth error means a salt
    rotation self-heals on the retry instead of looking like a paywall.
    """
    if status_code in (401, 403):
        return True
    s = (api_status or "").lower()
    return "auth" in s or "token" in s or "notpremium" in s


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
        """Return configured token, or a (cached) guest account token."""
        if self.token:
            # Paid token: use as-is, never cached/invalidated here.
            return self.token
        global _guest_token_cache
        with _cache_lock:
            cached = _cache_get(_guest_token_cache)
        if cached:
            return cached
        async with session.post(
            f"{GOFILE_API}/accounts",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            token = data.get("data", {}).get("token", "")
            if not token:
                raise ValueError("Failed to create GoFile guest account")
            logger.info("Created GoFile guest account")
            with _cache_lock:
                _guest_token_cache = (token, time.monotonic())
            return token

    async def _get_website_token(self, session: aiohttp.ClientSession) -> str:
        """Website token for the current account, valid for this 4h window.

        Previously this scraped a static value out of alljs.js/global.js. Both
        of those 404 now, and the value they held is no longer what the API
        checks — see gofile_wt for what replaced it.
        """
        account_token = await self._get_token(session)
        return await gofile_wt.website_token(
            session, account_token, BROWSER_USER_AGENT, GOFILE_LANGUAGE,
        )

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        content_id = self._extract_content_id(url)
        if not content_id:
            raise ValueError(f"Cannot extract GoFile content ID from: {url}")

        async with aiohttp.ClientSession() as session:
            data: dict = {}
            for attempt in (0, 1):
                token = await self._get_token(session)
                wt = await self._get_website_token(session)

                async with session.get(
                    f"{GOFILE_API}/contents/{content_id}",
                    params=CONTENTS_PARAMS,
                    headers=api_headers(token, wt),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    status_code = resp.status
                    # Non-200 bodies still carry the real reason
                    # (error-notPremium / error-rateLimit); dropping them left
                    # users with a bare "status 401".
                    body = await resp.text()
                try:
                    data = json.loads(body) if body else {}
                except ValueError:
                    data = {}

                api_status = data.get("status")
                if status_code == 200 and api_status == "ok":
                    break
                if attempt == 0 and _is_auth_error(status_code, api_status):
                    # Cached token(s) likely expired/rotated — drop and retry
                    # once with freshly fetched ones.
                    logger.info(
                        "GoFile auth failure (HTTP %s, status=%s) — "
                        "invalidating cached tokens and retrying",
                        status_code, api_status,
                    )
                    invalidate_token_cache()
                    continue
                raise ValueError(_describe_error(status_code, api_status))

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

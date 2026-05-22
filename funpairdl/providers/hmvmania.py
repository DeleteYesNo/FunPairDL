from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.hmvmania")

# Direct mp4 hosted under WordPress uploads — captures the full URL.
_MP4_RE = re.compile(
    r"""https?://[^\s"'<>]*?hmvmania\.com/wp-content/uploads/[^\s"'<>]+?\.mp4""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Pull a height (e.g. 1080) out of filenames like "av1_1080p_*.mp4" or "*_720p.mp4".
_HEIGHT_RE = re.compile(r"(?:^|[_\-/.])(\d{3,4})p(?:[_\-/.]|$)", re.IGNORECASE)


class HmvManiaProvider(BaseProvider):
    """Provider for hmvmania.com video pages.

    The site embeds a WordPress wp-video shortcode that points at a direct
    .mp4 under /wp-content/uploads/. We scrape the page, pick the best mp4
    source, then resolve it via HEAD for size + range support.
    """

    @staticmethod
    def can_handle(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        return host == "hmvmania.com" or host.endswith(".hmvmania.com")

    @property
    def name(self) -> str:
        return "hmvmania"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred_resolution = kwargs.get("preferred_resolution", "best")
        headers = {"User-Agent": BROWSER_USER_AGENT}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                html = await resp.text(errors="ignore")
                page_url = str(resp.url)

            candidates = parse_mp4_candidates(html, page_url)
            page_title = parse_page_title(html)
            mp4_url = _select_by_resolution(candidates, preferred_resolution)
            if not mp4_url:
                raise ValueError(f"No mp4 source found on HMV Mania page: {url}")

            async with session.head(
                mp4_url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as head:
                head.raise_for_status()
                final_url = str(head.url)
                total_size = int(head.headers.get("Content-Length", 0))
                supports_range = head.headers.get("Accept-Ranges", "").lower() == "bytes"

        filename = self._build_filename(page_title, final_url)

        logger.info(
            "HMV Mania resolved: %s -> %s (%d bytes)",
            url[:60], final_url[:80], total_size,
        )

        return ResolvedFile(
            direct_url=final_url,
            filename=filename,
            total_size=total_size,
            supports_range=supports_range,
            headers={"User-Agent": BROWSER_USER_AGENT, "Referer": page_url},
        )

    @staticmethod
    def _build_filename(page_title: str, mp4_url: str) -> str:
        path = urlparse(mp4_url).path
        url_name = unquote(path.split("/")[-1]) if path else ""
        ext = ".mp4"
        if url_name.lower().endswith(".mp4"):
            ext = ".mp4"

        if page_title:
            return sanitize_filename(page_title + ext)
        if url_name:
            return sanitize_filename(url_name)
        return "hmvmania_video.mp4"


def parse_mp4_candidates(html: str, page_url: str) -> list[str]:
    """Extract all unique mp4 URLs from a hmvmania.com page.

    Preserves listing order so callers that don't care about resolution
    still get a stable pick.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _MP4_RE.findall(html):
        full = urljoin(page_url, unescape(raw))
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def parse_page_title(html: str) -> str:
    """Parse the <title> tag and strip the " - HMV Mania" site suffix."""
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    title = unescape(m.group(1)).strip()
    return re.sub(r"\s*[-–—|]\s*HMV\s*Mania\s*$", "", title, flags=re.IGNORECASE)


def height_from_url(url: str) -> int:
    m = _HEIGHT_RE.search(url)
    return int(m.group(1)) if m else 0


# Backwards-compat alias for callers still using the leading underscore.
_height_from_url = height_from_url


def _select_by_resolution(candidates: list[str], preferred_resolution: str) -> str:
    """Pick an mp4 to match the user's preferred resolution.

    Mirrors ytdlp_generic._select_format: exact match wins; otherwise fall
    back to the highest-resolution candidate (ties broken by listing order).
    """
    if not candidates:
        return ""

    ranked = sorted(candidates, key=height_from_url)  # ascending by height

    if not preferred_resolution or preferred_resolution == "best":
        return ranked[-1]

    try:
        target = int(preferred_resolution)
    except (ValueError, TypeError):
        return ranked[-1]

    exact = [c for c in candidates if height_from_url(c) == target]
    if exact:
        return exact[-1]
    return ranked[-1]

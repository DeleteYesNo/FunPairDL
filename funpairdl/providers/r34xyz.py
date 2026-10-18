from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.r34xyz")

# r-34.xyz is a Blazor app: the page carries no video URL (yt-dlp's generic
# extractor finds nothing, or a playlist of the sidebar's thumbnails). Its
# JSON API describes the post, and the CDN keeps the uploaded file under a
# path built from the id.
_HOSTS = ("r-34.xyz",)
_POST_ID_RE = re.compile(r"^/post/(\d+)(?:/|$)")
_CDN = "https://r34xyz.b-cdn.net/posts"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_VIDEO_TYPE = 1


def extract_post_id(url: str) -> int | None:
    """Post id from an r-34.xyz post URL ("/post/1234?&pr=5&d=…"), else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in _HOSTS:
        return None
    m = _POST_ID_RE.match(parsed.path or "")
    return int(m.group(1)) if m else None


def api_url(post_id: int) -> str:
    return f"https://r-34.xyz/api/v2/post/{post_id}"


def video_url(post_id: int) -> str:
    """The uploaded file: posts/<id // 1000>/<id>/<id>.mp4 (the ".480.mp4" /
    ".720.mp4" beside it are transcodes, not always present)."""
    return f"{_CDN}/{post_id // 1000}/{post_id}/{post_id}.mp4"


def thumbnail_url(post_id: int) -> str:
    return f"{_CDN}/{post_id // 1000}/{post_id}/{post_id}.thumbnail.jpg"


def build_filename(post_id: int) -> str:
    return sanitize_filename(f"r-34.xyz {post_id}.mp4")


def tag_names(post: dict) -> list[str]:
    return [t["value"] for t in (post.get("tags") or []) if isinstance(t, dict) and t.get("value")]


_HEADERS = {"User-Agent": _USER_AGENT, "Referer": "https://r-34.xyz/"}


async def fetch_post(post_id: int, session: aiohttp.ClientSession) -> dict:
    async with session.get(api_url(post_id), headers={**_HEADERS, "Accept": "application/json"},
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status in (204, 404):
            raise ValueError("r-34.xyz post not found (404)")
        resp.raise_for_status()
        post = await resp.json(content_type=None)
    if not isinstance(post, dict) or "id" not in post:
        raise ValueError("Unexpected r-34.xyz API response")
    if post.get("type") != _VIDEO_TYPE:
        raise ValueError("Not a video: r-34.xyz post is an image")
    return post


async def fetch_size(post_id: int, session: aiohttp.ClientSession) -> int:
    async with session.head(video_url(post_id), headers=_HEADERS, allow_redirects=True,
                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status == 404:
            raise ValueError("r-34.xyz video file not found (404)")
        resp.raise_for_status()
        return int(resp.headers.get("Content-Length") or 0)


class R34XyzProvider(BaseProvider):
    """Provider for r-34.xyz post pages: the API says it is a video, the
    CDN serves the uploaded mp4 with Range support."""

    @staticmethod
    def can_handle(url: str) -> bool:
        return extract_post_id(url) is not None

    @property
    def name(self) -> str:
        return "r34xyz"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        post_id = extract_post_id(url)
        if post_id is None:
            raise ValueError(f"Not an r-34.xyz post URL: {url}")
        async with aiohttp.ClientSession() as session:
            await fetch_post(post_id, session)
            size = await fetch_size(post_id, session)
        logger.info("r-34.xyz resolved: %s -> %s (%d bytes)", url[:60], video_url(post_id), size)
        return ResolvedFile(
            direct_url=video_url(post_id),
            filename=build_filename(post_id),
            total_size=size,
            supports_range=True,
            headers=dict(_HEADERS),
        )

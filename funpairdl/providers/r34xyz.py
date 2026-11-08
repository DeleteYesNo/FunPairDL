from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.r34xyz")

# r-34.xyz and rule34.xyz run one Blazor app (two sites, two numberings):
# the page carries no video URL (yt-dlp's generic extractor finds nothing,
# a playlist of the sidebar's thumbnails, or "Unsupported URL" — 21 of a
# post's 182 rule34.xyz links died that way). The JSON API describes the
# post, and each site's CDN keeps the uploaded file under a path built from
# the id. r-34.xyz names it "<id>.mp4"; rule34.xyz names it by what it kept
# of the upload — "<id>.mov.mp4", else only "<id>.mov720.mp4" or
# "<id>.mov480.mp4" — so the first of those the CDN has is the video.
_SITES = {
    "r-34.xyz": ("https://r34xyz.b-cdn.net/posts", (".mp4",)),
    "rule34.xyz": ("https://rule34xyz.b-cdn.net/posts", (".mov.mp4", ".mov720.mp4", ".mov480.mp4")),
}
_HOSTS = tuple(_SITES)
_POST_ID_RE = re.compile(r"^/post/(\d+)(?:/|$)")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_VIDEO_TYPE = 1


def extract_post(url: str) -> tuple[str, int] | None:
    """(site, post id) from a post URL ("/post/1234?&pr=5&d=…"), else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in _HOSTS:
        return None
    m = _POST_ID_RE.match(parsed.path or "")
    return (host, int(m.group(1))) if m else None


def extract_post_id(url: str) -> int | None:
    """Post id from an r-34.xyz / rule34.xyz post URL, else None."""
    post = extract_post(url)
    return post[1] if post else None


def api_url(post_id: int, site: str = "r-34.xyz") -> str:
    return f"https://{site}/api/v2/post/{post_id}"


def video_urls(post_id: int, site: str = "r-34.xyz") -> list[str]:
    """Where the CDN may keep the video, the uploaded file first:
    posts/<id // 1000>/<id>/<id><suffix>."""
    cdn, suffixes = _SITES[site]
    return [f"{cdn}/{post_id // 1000}/{post_id}/{post_id}{s}" for s in suffixes]


def video_url(post_id: int, site: str = "r-34.xyz") -> str:
    """The uploaded file (r-34.xyz: the ".480.mp4" / ".720.mp4" beside it
    are transcodes, not always present)."""
    return video_urls(post_id, site)[0]


def thumbnail_url(post_id: int, site: str = "r-34.xyz") -> str:
    if site != "r-34.xyz":
        return ""  # rule34.xyz keeps no ".thumbnail.jpg"
    cdn, _ = _SITES[site]
    return f"{cdn}/{post_id // 1000}/{post_id}/{post_id}.thumbnail.jpg"


_TRANSCODE_RE = re.compile(r"\.mov(\d{3,4})\.mp4$")


def file_frame(url: str, width: int, height: int) -> tuple[int, int]:
    """The frame of the file at `url`: the post's own, unless the CDN kept
    only a transcode ("….mov480.mp4" of a 1080p upload is 480 high)."""
    m = _TRANSCODE_RE.search(url)
    if not m or not height or int(m.group(1)) >= height:
        return width, height
    h = int(m.group(1))
    return round(width * h / height), h


def build_filename(post_id: int, site: str = "r-34.xyz") -> str:
    return sanitize_filename(f"{site} {post_id}.mp4")


def tag_names(post: dict) -> list[str]:
    return [t["value"] for t in (post.get("tags") or []) if isinstance(t, dict) and t.get("value")]


def _headers(site: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Referer": f"https://{site}/"}


_HEADERS = _headers("r-34.xyz")


async def fetch_post(post_id: int, session: aiohttp.ClientSession, site: str = "r-34.xyz") -> dict:
    async with session.get(api_url(post_id, site), headers={**_headers(site), "Accept": "application/json"},
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        if resp.status in (204, 404):
            raise ValueError(f"{site} post not found (404)")
        resp.raise_for_status()
        post = await resp.json(content_type=None)
    if not isinstance(post, dict) or "id" not in post:
        raise ValueError(f"Unexpected {site} API response")
    if post.get("type") != _VIDEO_TYPE:
        raise ValueError(f"Not a video: {site} post is an image")
    return post


async def fetch_file(post_id: int, session: aiohttp.ClientSession, site: str = "r-34.xyz") -> tuple[str, int]:
    """(url, size) of the first video file the CDN has."""
    for url in video_urls(post_id, site):
        async with session.head(url, headers=_headers(site), allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 404:
                continue
            resp.raise_for_status()
            return url, int(resp.headers.get("Content-Length") or 0)
    raise ValueError(f"{site} video file not found (404)")


async def fetch_size(post_id: int, session: aiohttp.ClientSession, site: str = "r-34.xyz") -> int:
    return (await fetch_file(post_id, session, site))[1]


class R34XyzProvider(BaseProvider):
    """Provider for r-34.xyz / rule34.xyz post pages: the API says it is a
    video, the CDN serves the mp4 with Range support."""

    @staticmethod
    def can_handle(url: str) -> bool:
        return extract_post(url) is not None

    @property
    def name(self) -> str:
        return "r34xyz"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        post = extract_post(url)
        if post is None:
            raise ValueError(f"Not an r-34.xyz / rule34.xyz post URL: {url}")
        site, post_id = post
        async with aiohttp.ClientSession() as session:
            await fetch_post(post_id, session, site)
            direct, size = await fetch_file(post_id, session, site)
        logger.info("%s resolved: %s -> %s (%d bytes)", site, url[:60], direct, size)
        return ResolvedFile(
            direct_url=direct,
            filename=build_filename(post_id, site),
            total_size=size,
            supports_range=True,
            headers=_headers(site),
        )

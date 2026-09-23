"""pixivFANBOX posts (``<creator>.fanbox.cc/posts/<id>``).

The public post API lists a post's attachments. A free post's files sit on
downloads.fanbox.cc and download anonymously (ranged GETs work); a
supporter-only post comes back restricted with no body — paid content, not
something this downloader can fetch. A creator's front page links no video.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.fanbox")

_POST_RE = re.compile(r"/posts/(\d+)")
_API = "https://api.fanbox.cc/post.info?postId={}"
_API_HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Origin": "https://www.fanbox.cc",
                "Referer": "https://www.fanbox.cc/", "Accept": "application/json, text/plain, */*"}
_VIDEO_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "avi", "wmv"}

# Error texts the panel reads (content.js _deadKind).
NOT_VIDEO_ERROR = "Not a video: pixivFANBOX creator page"
GONE_ERROR = "Post not found (404) on pixivFANBOX"


def is_fanbox(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "fanbox.cc" or host.endswith(".fanbox.cc")


def post_id(url: str) -> str:
    m = _POST_RE.search(urlparse(url).path or "")
    return m.group(1) if m else ""


def paid_error(fee: int) -> str:
    return (f"Paid content: pixivFANBOX supporter plan (¥{fee}) required" if fee
            else "Paid content: pixivFANBOX supporters only")


def post_files(post: dict) -> list[dict]:
    """A post's attachments as [{name, ext, size, url}] — a "file" post lists
    them in body.files, an article in body.fileMap."""
    body = post.get("body") or {}
    raw = list(body.get("files") or []) + list((body.get("fileMap") or {}).values())
    out = []
    for f in raw:
        url = (f or {}).get("url") or ""
        if not url:
            continue
        ext = str(f.get("extension") or url.rsplit(".", 1)[-1]).lower()
        out.append({"name": str(f.get("name") or ""), "ext": ext,
                    "size": int(f.get("size") or 0), "url": url})
    return out


def video_files(post: dict) -> list[dict]:
    return [f for f in post_files(post) if f["ext"] in _VIDEO_EXTS]


def file_name(post: dict, f: dict, several: bool) -> str:
    """"<post title>.mp4"; with several videos the file's own name tells
    them apart ("<title> - Censored.mp4")."""
    title = str(post.get("title") or "").strip()
    stem = title or f["name"] or "fanbox_video"
    if several and f["name"] and f["name"] != title:
        stem = f"{stem} - {f['name']}" if title else f["name"]
    return sanitize_filename(f"{stem}.{f['ext']}")


async def fetch_post(url: str) -> dict:
    """The post, or ValueError saying why it can't be downloaded.

    Its own cookieless session: the API sets a session cookie on the first
    answer and refuses (403) every later request that sends it back."""
    pid = post_id(url)
    if not pid:
        raise ValueError(NOT_VIDEO_ERROR)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        async with session.get(_API.format(pid), headers=_API_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 404:
                raise ValueError(GONE_ERROR)
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    body = (data or {}).get("body") or {}
    post = body.get("post", body) if isinstance(body, dict) else {}
    if not post:
        raise ValueError(GONE_ERROR)
    if post.get("isRestricted") or post.get("body") is None:
        raise ValueError(paid_error(int(post.get("feeRequired") or 0)))
    return post


class FanboxProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        return is_fanbox(url) and bool(post_id(url))

    @property
    def name(self) -> str:
        return "fanbox"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        post = await fetch_post(url)
        vids = video_files(post)
        if not vids:
            raise ValueError(f"No video attached to pixivFANBOX post: {url}")
        f = max(vids, key=lambda x: x["size"])
        logger.info("pixivFANBOX resolved: %s -> %s (%d bytes)", url[:60], f["url"][:80], f["size"])
        return ResolvedFile(
            direct_url=f["url"],
            filename=file_name(post, f, len(vids) > 1),
            total_size=f["size"],
            supports_range=True,
            headers={"User-Agent": BROWSER_USER_AGENT, "Referer": "https://www.fanbox.cc/"},
        )

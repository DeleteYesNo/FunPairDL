from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.e621")

# e621 asks API clients for a descriptive User-Agent; generic ones may be
# throttled. Used for the JSON call and the CDN download alike.
E621_USER_AGENT = "FunPairDL/1.0 (funscript pairing downloader)"

_HOSTS = ("e621.net", "e926.net")
# /posts/12345 (current) and /post/show/12345 (legacy) — trailing query
# strings such as "?q=artist" are common in forum links and ignored.
_POST_ID_RE = re.compile(r"^/(?:posts|post/show)/(\d+)(?:/|$)")


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_e621_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _HOSTS)


def extract_post_id(url: str) -> int | None:
    """Post id from an e621/e926 post page URL, or None for other URLs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not is_e621_host(host):
        return None
    m = _POST_ID_RE.match(parsed.path or "")
    return int(m.group(1)) if m else None


def api_url(url: str) -> str:
    """JSON endpoint for a post page (same host, so e926 stays e926)."""
    post_id = extract_post_id(url)
    if post_id is None:
        raise ValueError(f"Not an e621 post URL: {url}")
    return f"https://{_host_of(url)}/posts/{post_id}.json"


def build_formats(post: dict) -> list[dict]:
    """Pure: every downloadable rendition of a post, ascending by height.

    e621 keeps the uploaded file as ``file`` (usually a VP9 webm for
    animations) and, under ``sample.alternates``, transcoded h264 mp4s: a
    full-size ``variants.mp4`` plus downscaled ``samples`` (480p/720p...).
    The original is listed last so "best" picks it. Each entry carries
    ``url``/``ext``/``label`` for resolve plus the ``height``/``size``
    pair the /probe contract renders.
    """
    out: list[dict] = []
    file = post.get("file") or {}
    alternates = (post.get("sample") or {}).get("alternates") or {}

    def _add(label: str, node: dict | None, ext: str, is_original: bool) -> None:
        if not node or not node.get("url"):
            return
        out.append({
            "label": label,
            "url": node["url"],
            "ext": ext,
            "height": int(node.get("height") or 0),
            "width": int(node.get("width") or 0),
            "size": int(node.get("size") or 0),
            "codec": node.get("codec") or "",
            "original": is_original,
        })

    for label, node in sorted((alternates.get("samples") or {}).items()):
        _add(label, node, "mp4", False)
    for label, node in (alternates.get("variants") or {}).items():
        _add(label, node, label if label in ("mp4", "webm") else "mp4", False)
    _add("original", file, str(file.get("ext") or "").lower() or "bin", True)

    # Ascending height; the original outranks a same-height transcode.
    out.sort(key=lambda f: (f["height"], f["original"], f["size"]))
    return out


def select_format(formats: list[dict], preferred_resolution: str = "best") -> dict | None:
    """Pick a rendition: the original for "best", else the exact height
    match (an mp4 sample when several tie), else the original."""
    if not formats:
        return None
    original = next((f for f in formats if f["original"]), formats[-1])
    if not preferred_resolution or preferred_resolution == "best":
        return original
    try:
        target = int(preferred_resolution)
    except (TypeError, ValueError):
        return original
    exact = [f for f in formats if f["height"] == target]
    if not exact:
        return original
    exact.sort(key=lambda f: (f["ext"] != "mp4", -f["size"]))
    return exact[0]


_OG_TITLE_RE = re.compile(
    r"""<meta\s+(?:property|name)=["'](?:og|twitter):title["']\s+content=["']([^"']*)["']""",
    re.IGNORECASE,
)
_SITE_SUFFIX_RE = re.compile(r"\s*-\s*e(?:621|926)\s*$", re.IGNORECASE)
_ARTIST_PSEUDO = {"conditional_dnp", "unknown_artist", "avoid_posting"}


def title_from_html(html: str) -> str:
    """The post's human title from its page's og:title — the same
    "oc (somegame and etc) created by someartist" the forum's link
    card shows — minus the " - e621" site suffix. "" when absent."""
    m = _OG_TITLE_RE.search(html or "")
    if not m:
        return ""
    import html as _html
    title = _SITE_SUFFIX_RE.sub("", _html.unescape(m.group(1))).strip()
    # A bare "#1234567" is what e621 renders when it has no tags to name by.
    return "" if re.fullmatch(r"#?\d+", title) else title


def title_from_tags(post: dict) -> str:
    """Offline approximation of e621's humanized title, used when the page
    can't be read: characters (qualifiers dropped), first copyright, artists.
    e621 orders copyrights by popularity, which the JSON does not carry, so
    this can differ from the site's own wording."""
    tags = post.get("tags") or {}

    def _sentence(items: list[str]) -> str:
        if len(items) <= 1:
            return "".join(items)
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"

    chars = [re.sub(r"_\(.+\)$", "", c) for c in (tags.get("character") or [])]
    if len(chars) > 5:
        chars = chars[:5] + ["etc"]
    copyrights = list(tags.get("copyright") or [])
    if len(copyrights) > 1:
        copyrights = copyrights[:1] + ["etc"]
    artists = [
        a for a in (tags.get("artist") or [])
        if not a.endswith("_warning") and a not in _ARTIST_PSEUDO
    ]
    parts = []
    if chars:
        parts.append(_sentence(chars))
    if copyrights:
        parts.append(f"({_sentence(copyrights)})")
    if artists:
        parts.append(f"created by {_sentence(artists)}")
    return " ".join(parts).replace("_", " ").strip()


def build_filename(post: dict, fmt: dict, title: str = "") -> str:
    """``<human title>.<ext>`` — the page's og:title when the caller fetched
    it, else the tag-built approximation, else ``e621_<id>``."""
    post_id = post.get("id") or 0
    stem = (title or "").strip() or title_from_tags(post) or f"e621_{post_id}"
    return sanitize_filename(f"{stem}.{fmt['ext']}")


async def fetch_title(url: str, session: aiohttp.ClientSession) -> str:
    """og:title of the post page; "" on any failure (the name then falls
    back to the tag-built title, never blocking the download)."""
    post_id = extract_post_id(url)
    if post_id is None:
        return ""
    try:
        async with session.get(
            f"https://{_host_of(url)}/posts/{post_id}",
            headers={"User-Agent": E621_USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return ""
            # The metas sit in <head>; no need to read the whole page —
            # but wait for the bytes (StreamReader.read(n) returns whatever
            # happens to be buffered, which may stop before the metas).
            try:
                head = await resp.content.readexactly(64 * 1024)
            except asyncio.IncompleteReadError as e:
                head = e.partial
        return title_from_html(head.decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001 — cosmetic, never fatal
        logger.debug("e621 title fetch failed for %s: %s", url[:60], e)
        return ""


def describe_unavailable(post: dict) -> str:
    """Why a post has no file URL (login-only, global blacklist, deleted)."""
    flags = post.get("flags") or {}
    if flags.get("deleted"):
        return "e621 post was deleted"
    return "e621 hides this post's file (login or blacklist required)"


async def fetch_post(url: str, session: aiohttp.ClientSession) -> dict:
    async with session.get(
        api_url(url),
        headers={"User-Agent": E621_USER_AGENT, "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status == 404:
            raise ValueError("e621 post not found")
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    post = data.get("post") if isinstance(data, dict) else None
    if not isinstance(post, dict):
        raise ValueError("Unexpected e621 API response")
    return post


class E621Provider(BaseProvider):
    """Provider for e621.net / e926.net post pages.

    The post JSON lists the uploaded file and its transcodes with sizes, so
    no HTML scraping or HEAD is needed; the static CDN serves plain files
    with Range support.
    """

    @staticmethod
    def can_handle(url: str) -> bool:
        return extract_post_id(url) is not None

    @property
    def name(self) -> str:
        return "e621"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        preferred_resolution = kwargs.get("preferred_resolution", "best")
        async with aiohttp.ClientSession() as session:
            post, title = await asyncio.gather(
                fetch_post(url, session), fetch_title(url, session))

        formats = build_formats(post)
        fmt = select_format(formats, preferred_resolution)
        if fmt is None:
            raise ValueError(describe_unavailable(post))

        filename = build_filename(post, fmt, title)
        logger.info(
            "e621 resolved: %s -> %s (%s, %d bytes)",
            url[:60], fmt["url"][:80], fmt["label"], fmt["size"],
        )
        return ResolvedFile(
            direct_url=fmt["url"],
            filename=filename,
            total_size=fmt["size"],
            supports_range=True,
            headers={"User-Agent": E621_USER_AGENT, "Referer": url},
        )

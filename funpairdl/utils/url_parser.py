from urllib.parse import urlparse


def detect_provider(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "direct"

    host = host.lower().removeprefix("www.")

    # Specialized providers (checked first)
    provider_map = {
        "pixeldrain.com": "pixeldrain",
        "mega.nz": "mega",
        "mega.co.nz": "mega",
        "gofile.io": "gofile",
        "iwara.tv": "iwara",
        "discuss.eroscripts.com": "eroscripts",
        "hmvmania.com": "hmvmania",
        "socigames.com": "socigames",
        "e621.net": "e621",
        "e926.net": "e621",
    }

    for domain, provider in provider_map.items():
        if host == domain or host.endswith("." + domain):
            return provider

    # yt-dlp supported video sites (non-exhaustive, for display purposes)
    ytdlp_domains = [
        "rule34video.com", "rule34.xxx", "hanime1.me",
        "bilibili.com", "b23.tv",
        # VK serves video as HLS/DASH; yt-dlp resolves it to a manifest the
        # HLS downloader (ffmpeg) handles. Routing it to "direct" instead made
        # the segment downloader hit VK's okcdn CDN raw and get HTTP 400.
        "vk.com", "vkvideo.ru",
        "pornhub.com", "xvideos.com", "xnxx.com", "xhamster.com",
        "spankbang.com", "eporner.com", "redtube.com", "youporn.com",
        "tube8.com", "tnaflix.com",
        "youtube.com", "youtu.be", "dailymotion.com", "vimeo.com",
        "streamable.com", "twitter.com", "x.com",
    ]
    for domain in ytdlp_domains:
        if host == domain or host.endswith("." + domain):
            return "ytdlp"

    return "direct"


def extract_pixeldrain_id(url: str) -> str | None:
    parsed = urlparse(url)
    if "pixeldrain.com" not in (parsed.hostname or ""):
        return None
    parts = parsed.path.strip("/").split("/")
    # /u/{id}, /d/{id}, or /api/file/{id}
    if len(parts) >= 2 and parts[0] in ("u", "d"):
        return parts[1]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "file":
        return parts[2]
    return None


# Friendly names for the queue's "Source" column. Providers that cover one
# site get a brand name; the catch-alls (ytdlp/direct) fall back to the host.
_PROVIDER_LABELS = {
    "pixeldrain": "Pixeldrain",
    "mega": "MEGA",
    "gofile": "GoFile",
    "iwara": "Iwara",
    "eroscripts": "EroScripts",
    "hmvmania": "HMV Mania",
    "socigames": "SociGames",
    "e621": "e621",
}


def source_label(provider_name: str, url: str) -> str:
    """Short human-readable source for a queue item.

    ``provider_name`` is what the item was tagged with when queued; it is
    re-derived from the URL when it is a catch-all ("direct"/"ytdlp") so
    items queued before a site gained its own provider still get the brand
    name. Otherwise the bare host is shown (``rule34video.com``), with the
    forum's upload CDN folded into "EroScripts".
    """
    name = (provider_name or "").strip().lower()
    if name not in _PROVIDER_LABELS:
        name = detect_provider(url)
    label = _PROVIDER_LABELS.get(name)
    if label:
        return label
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        host = ""
    if host.endswith("eroscripts.com"):
        return "EroScripts"
    return host


# Match Pixeldrain file or list URLs in arbitrary text
import re

PIXELDRAIN_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?pixeldrain\.com/(?:u|d|l)/[A-Za-z0-9]+",
    re.IGNORECASE,
)


def extract_pixeldrain_urls(text: str) -> list[str]:
    """Extract all Pixeldrain URLs from arbitrary text, preserving order
    and removing duplicates."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in PIXELDRAIN_URL_PATTERN.findall(text):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def is_pixeldrain_list_url(url: str) -> bool:
    parsed = urlparse(url)
    if "pixeldrain.com" not in (parsed.hostname or ""):
        return False
    parts = parsed.path.strip("/").split("/")
    return len(parts) >= 2 and parts[0] == "l"


def extract_mega_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "mega.nz" in host or "mega.co.nz" in host:
        return url
    return None

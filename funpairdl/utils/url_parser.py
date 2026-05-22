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
    }

    for domain, provider in provider_map.items():
        if host == domain or host.endswith("." + domain):
            return provider

    # yt-dlp supported video sites (non-exhaustive, for display purposes)
    ytdlp_domains = [
        "rule34video.com", "rule34.xxx", "hanime1.me",
        "bilibili.com", "b23.tv",
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

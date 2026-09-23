"""MediaFire files and folders.

A file page (``/file/<quickkey>/<name>/file``, also ``/file_premium/``)
carries the real link on its download button (download*.mediafire.com,
ranged GETs work; the link changes per visit, so it is read at resolve
time). A folder (``/folder/<key>``) is a pack: the public folder API lists
its files, each kept as its file-page URL and resolved like a single file.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.mediafire")

_FILE_RE = re.compile(r"/(?:file|file_premium|view)/([a-z0-9]+)(?:/([^/?#]+))?", re.IGNORECASE)
_FOLDER_RE = re.compile(r"/folder/([a-z0-9]+)", re.IGNORECASE)
_BUTTON_RE = re.compile(r'href="(https?://download[^"]+)"[^>]*id="downloadButton"|'
                        r'id="downloadButton"[^>]*href="(https?://download[^"]+)"', re.IGNORECASE)
_ANY_DL_RE = re.compile(r'href="(https?://download\d*\.mediafire\.com/[^"]+)"', re.IGNORECASE)
_FOLDER_API = ("https://www.mediafire.com/api/1.5/folder/get_content.php"
               "?folder_key={}&content_type={}&response_format=json&chunk={}")

GONE_ERROR = "File not found (404) on MediaFire"


def _is_mediafire(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def file_key(url: str) -> str:
    m = _FILE_RE.search(urlparse(url).path or "")
    return m.group(1) if m else ""


def folder_key(url: str) -> str:
    m = _FOLDER_RE.search(urlparse(url).path or "")
    return m.group(1) if m else ""


def is_folder_url(url: str) -> bool:
    return _is_mediafire(url) and bool(folder_key(url))


def file_page(quickkey: str, name: str) -> str:
    return f"https://www.mediafire.com/file/{quickkey}/{name.replace(' ', '_')}/file"


def parse_download_link(html: str) -> str:
    m = _BUTTON_RE.search(html or "")
    if m:
        return unescape(m.group(1) or m.group(2))
    m = _ANY_DL_RE.search(html or "")
    return unescape(m.group(1)) if m else ""


def name_from_link(link: str) -> str:
    return unquote((urlparse(link).path or "").rsplit("/", 1)[-1]).replace("+", " ")


async def list_folder(url: str, session: aiohttp.ClientSession) -> list[dict]:
    """[{name, size, url}] of a folder's files, subfolders included (their
    files carry the subfolder in the name so a pack keeps its layout)."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    out: list[dict] = []

    async def _walk(key: str, prefix: str, depth: int) -> None:
        for kind in ("files", "folders"):
            chunk = 1
            while True:
                async with session.get(_FOLDER_API.format(key, kind, chunk), headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    resp.raise_for_status()
                    data = ((await resp.json(content_type=None)) or {}).get("response") or {}
                if data.get("result") != "Success":
                    raise ValueError(GONE_ERROR)
                fc = data.get("folder_content") or {}
                if kind == "files":
                    for f in fc.get("files") or []:
                        name = str(f.get("filename") or "")
                        out.append({"name": f"{prefix}{name}", "size": int(f.get("size") or 0),
                                    "url": file_page(str(f.get("quickkey") or ""), name)})
                elif depth < 3:
                    for sub in fc.get("folders") or []:
                        await _walk(str(sub.get("folderkey") or ""),
                                    f"{prefix}{sub.get('name') or ''}/", depth + 1)
                if fc.get("more_chunks") != "yes":
                    break
                chunk += 1

    key = folder_key(url)
    if not key:
        raise ValueError(f"Not a MediaFire folder URL: {url}")
    await _walk(key, "", 0)
    return out


async def resolve_file(url: str, session: aiohttp.ClientSession) -> tuple[str, str, int]:
    """(direct link, filename, size) of a file page."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 404:
            raise ValueError(GONE_ERROR)
        resp.raise_for_status()
        html = await resp.text(errors="ignore")
    link = parse_download_link(html)
    if not link:
        if "error.php" in html or "file-removed" in html or "has been removed" in html:
            raise ValueError(GONE_ERROR)
        raise ValueError(f"No download link on MediaFire page: {url}")
    size = 0
    async with session.get(link, headers={**headers, "Range": "bytes=0-0"},
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status in (200, 206):
            cr = resp.headers.get("Content-Range", "")
            tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
            size = int(tail) if tail.isdigit() else int(resp.headers.get("Content-Length", 0) or 0)
    return link, sanitize_filename(name_from_link(link) or "mediafire_file"), size


class MediafireProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        return _is_mediafire(url) and bool(file_key(url))

    @property
    def name(self) -> str:
        return "mediafire"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        async with aiohttp.ClientSession() as session:
            link, filename, size = await resolve_file(url, session)
        logger.info("MediaFire resolved: %s -> %s (%d bytes)", url[:60], filename, size)
        return ResolvedFile(direct_url=link, filename=filename, total_size=size,
                            supports_range=True, headers={"User-Agent": BROWSER_USER_AGENT})

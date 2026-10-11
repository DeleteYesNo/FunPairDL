"""Google Drive files and folders (public links, no account).

A file (``drive.google.com/file/d/<id>/view``, ``open?id=``, ``uc?id=``)
downloads from ``drive.usercontent.google.com/download?id=<id>&confirm=t``:
``confirm=t`` skips the "can't scan this file for viruses" page of large
files, and ranged GETs work. A folder (``/drive/folders/<id>``) is a pack:
its ``embeddedfolderview`` page lists the files (and subfolders), each kept
as its file URL and resolved like a single file.
"""
from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT
from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename

logger = logging.getLogger("funpairdl.providers.gdrive")

_FILE_PATH_RE = re.compile(r"/file/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})")
_FOLDER_PATH_RE = re.compile(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{10,})")
_ENTRY_RE = re.compile(r'class="flip-entry"[^>]*id="entry-([A-Za-z0-9_-]+)"(.*?)(?=class="flip-entry"|$)', re.DOTALL)
_TITLE_RE = re.compile(r'class="flip-entry-title">([^<]*)<')
_DISPOSITION_RE = re.compile(r"filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"", re.IGNORECASE)

GONE_ERROR = "File not found (404) on Google Drive"
HEADERS = {"User-Agent": BROWSER_USER_AGENT}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def file_id(url: str) -> str:
    p = urlparse(url)
    if not (_host(url) in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
        return ""
    m = _FILE_PATH_RE.search(p.path or "")
    if m:
        return m.group(1)
    if p.path.rstrip("/") in ("/open", "/uc", "/download"):
        return (parse_qs(p.query).get("id") or [""])[0]
    return ""


def folder_id(url: str) -> str:
    p = urlparse(url)
    if _host(url) != "drive.google.com":
        return ""
    m = _FOLDER_PATH_RE.search(p.path or "")
    if m:
        return m.group(1)
    if p.path.rstrip("/") in ("/folderview", "/embeddedfolderview"):
        return (parse_qs(p.query).get("id") or [""])[0]
    return ""


def is_folder_url(url: str) -> bool:
    return bool(folder_id(url))


def file_url(fid: str) -> str:
    return f"https://drive.google.com/file/d/{fid}/view"


def download_url(fid: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"


def parse_folder(html: str) -> list[dict]:
    """[{id, name, folder}] of an embeddedfolderview page."""
    out = []
    for fid, body in _ENTRY_RE.findall(html or ""):
        t = _TITLE_RE.search(body)
        out.append({"id": fid, "name": unescape(t.group(1)).strip() if t else fid,
                    "folder": "/drive/folders/" in body or "/folderview" in body})
    return out


def name_from_disposition(value: str) -> str:
    m = _DISPOSITION_RE.search(value or "")
    if not m:
        return ""
    return unquote(m.group(1)) if m.group(1) else m.group(2)


async def probe_file(fid: str, session: aiohttp.ClientSession) -> tuple[str, int]:
    """(filename, size) of a shared file from a 1-byte ranged GET. A file
    that is gone, private, or over its download quota answers with an HTML
    page instead of the file."""
    async with session.get(download_url(fid), headers={**HEADERS, "Range": "bytes=0-0"},
                           allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status == 404:
            raise ValueError(GONE_ERROR)
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            page = (await r.text(errors="ignore")).lower()
            if "quota" in page:
                raise ValueError("Google Drive download quota exceeded for this file; try again later")
            raise ValueError(GONE_ERROR)  # deleted or not shared publicly
        cr = r.headers.get("Content-Range", "")
        tail = cr.rsplit("/", 1)[-1] if "/" in cr else ""
        size = int(tail) if tail.isdigit() else int(r.headers.get("Content-Length") or 0)
        return name_from_disposition(r.headers.get("Content-Disposition", "")), size


async def list_folder(url: str, session: aiohttp.ClientSession) -> list[dict]:
    """[{name, size, url}] of a folder's files, subfolders included (their
    files carry the subfolder in the name so a pack keeps its layout)."""
    out: list[dict] = []

    async def _walk(fid: str, prefix: str, depth: int) -> None:
        async with session.get(f"https://drive.google.com/embeddedfolderview?id={fid}", headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 404:
                raise ValueError(GONE_ERROR)
            r.raise_for_status()
            entries = parse_folder(await r.text(errors="ignore"))
        files = [e for e in entries if not e["folder"]]
        sem = asyncio.Semaphore(4)

        async def _sized(e: dict) -> dict:
            async with sem:
                try:
                    _name, size = await probe_file(e["id"], session)
                except Exception:  # noqa: BLE001 — the listing still stands; resolve reports it
                    size = 0
            return {"name": f"{prefix}{e['name']}", "size": size, "url": file_url(e["id"])}

        out.extend(await asyncio.gather(*[_sized(e) for e in files]))
        if depth < 3:
            for e in entries:
                if e["folder"]:
                    await _walk(e["id"], f"{prefix}{e['name']}/", depth + 1)

    fid = folder_id(url)
    if not fid:
        raise ValueError(f"Not a Google Drive folder URL: {url}")
    await _walk(fid, "", 0)
    return out


class GoogleDriveProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        return bool(file_id(url))

    @property
    def name(self) -> str:
        return "gdrive"

    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        fid = file_id(url)
        async with aiohttp.ClientSession() as session:
            name, size = await probe_file(fid, session)
        filename = sanitize_filename(name or f"gdrive_{fid}")
        logger.info("Google Drive resolved: %s -> %s (%d bytes)", url[:60], filename, size)
        return ResolvedFile(direct_url=download_url(fid), filename=filename, total_size=size,
                            supports_range=True, headers=dict(HEADERS))

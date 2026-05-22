from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

import aiohttp

from funpairdl.providers.base import BaseProvider, ResolvedFile
from funpairdl.utils.filename import sanitize_filename
from funpairdl.utils.url_parser import (
    extract_pixeldrain_id,
    is_pixeldrain_list_url,
)

logger = logging.getLogger("funpairdl.providers.pixeldrain")

PIXELDRAIN_FILE_API = "https://pixeldrain.com/api/file"
PIXELDRAIN_FS_API = "https://pixeldrain.com/api/filesystem"
PIXELDRAIN_LIST_API = "https://pixeldrain.com/api/list"


@dataclass
class FsNode:
    """A node in the Pixeldrain filesystem tree.

    `path` is the full path used by the filesystem API
    (e.g. "/crWfNiT9/All Free Scripts (2025)/foo.mp4"). The first segment
    is the bucket id; subsequent segments are nested folders.
    """
    path: str
    name: str
    type: str  # "dir" | "file"
    size: int = 0
    mime_type: str = ""
    date_modified: str = ""
    sha256_sum: str = ""
    error: str = ""

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"

    @property
    def ext(self) -> str:
        if self.is_dir or "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()

    @property
    def bucket_id(self) -> str:
        parts = self.path.strip("/").split("/", 1)
        return parts[0] if parts else ""

    @property
    def download_url(self) -> str:
        """URL recognised by PixeldrainProvider.resolve() to fetch this file.

        We re-use the canonical filesystem API URL so the same string can be
        passed around as both a logical identifier and a download address."""
        # quote each segment but keep slashes
        encoded = "/".join(quote(p, safe="") for p in self.path.strip("/").split("/"))
        return f"{PIXELDRAIN_FS_API}/{encoded}?download"


def _node_from_dict(parent_path: str, d: dict) -> FsNode:
    name = d.get("name") or d.get("path", "").rsplit("/", 1)[-1] or ""
    full_path = d.get("path") or f"{parent_path.rstrip('/')}/{name}"
    return FsNode(
        path=full_path,
        name=name,
        type=d.get("type", "file"),
        size=d.get("file_size", 0),
        mime_type=d.get("file_type", ""),
        date_modified=d.get("modified", ""),
        sha256_sum=d.get("sha256_sum", ""),
    )


class PixeldrainProvider(BaseProvider):
    """Provider for Pixeldrain file hosting. Supports API key for premium.

    Backed by the modern `/api/filesystem/` endpoint which works for both
    /d/ (filesystem buckets) and /u/ (legacy single files via the bucket
    auto-created for each upload). Legacy /api/file/ and /api/list/ paths
    are still consulted as fallbacks.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    @staticmethod
    def can_handle(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return "pixeldrain.com" in host.lower()

    @property
    def name(self) -> str:
        return "pixeldrain"

    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            import base64
            token = base64.b64encode(f":{self.api_key}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        return {}

    # ── resolve() — used by the download pipeline ────────────────────
    async def resolve(self, url: str, **kwargs) -> ResolvedFile:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        if not path_parts:
            raise ValueError(f"Cannot parse Pixeldrain URL: {url}")

        # Filesystem download URL produced by the picker
        # (/api/filesystem/<path>?download). The path segments arrive
        # percent-encoded; decode them before handing off so
        # _resolve_filesystem's encoding pass doesn't double-encode.
        if path_parts[0] == "api" and len(path_parts) >= 2 and path_parts[1] == "filesystem":
            decoded = [unquote(p) for p in path_parts[2:]]
            fs_path = "/" + "/".join(decoded)
            return await self._resolve_filesystem(fs_path)

        # /l/{list_id} — legacy list, only first file
        if path_parts[0] == "l" and len(path_parts) >= 2:
            return await self._resolve_list(path_parts[1])

        # /d/{id} or /u/{id} — single id.
        #   /d/  = filesystem bucket (always works via /api/filesystem/)
        #   /u/  = legacy single-file id (works via /api/file/ and
        #          USUALLY but not always via /api/filesystem/). Try the
        #          modern endpoint first, fall back to legacy on 404.
        file_id = extract_pixeldrain_id(url)
        if not file_id:
            raise ValueError(f"Cannot extract Pixeldrain id from: {url}")
        is_legacy_u = path_parts[0] == "u"
        try:
            return await self._resolve_filesystem(f"/{file_id}")
        except aiohttp.ClientResponseError as e:
            if e.status == 404 and is_legacy_u:
                logger.info("Filesystem 404 for /u/%s; falling back to legacy /api/file/", file_id)
                return await self._resolve_legacy_file(file_id)
            raise

    async def _resolve_filesystem(self, fs_path: str) -> ResolvedFile:
        """Resolve a filesystem path to a downloadable file. If the path
        points to a directory, raise — bundle expansion is the caller's
        responsibility."""
        headers = self._auth_headers()
        encoded = "/".join(quote(p, safe="") for p in fs_path.strip("/").split("/"))
        # `?stat` returns metadata JSON; without it, /api/filesystem/<file>
        # returns the file's bytes directly.
        info_url = f"{PIXELDRAIN_FS_API}/{encoded}?stat"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                info_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        path_chain = data.get("path", [])
        if not path_chain:
            raise ValueError(f"Pixeldrain filesystem returned no path info for {fs_path}")
        node = path_chain[-1]
        if node.get("type") == "dir":
            raise ValueError(
                f"Pixeldrain path {fs_path} is a directory; expand it before downloading"
            )

        filename = sanitize_filename(node.get("name") or fs_path.rsplit("/", 1)[-1])
        size = node.get("file_size", 0)
        return ResolvedFile(
            direct_url=f"{PIXELDRAIN_FS_API}/{encoded}?download",
            filename=filename,
            total_size=size,
            supports_range=True,
            headers=headers,
        )

    async def _resolve_legacy_file(self, file_id: str) -> ResolvedFile:
        """Fallback for legacy /u/{id} files that aren't reachable via
        the filesystem endpoint. Uses /api/file/{id}/info for metadata
        and /api/file/{id} for download."""
        headers = self._auth_headers()
        info_url = f"{PIXELDRAIN_FILE_API}/{file_id}/info"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                info_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        filename = sanitize_filename(data.get("name", file_id))
        return ResolvedFile(
            direct_url=f"{PIXELDRAIN_FILE_API}/{file_id}",
            filename=filename,
            total_size=data.get("size", 0),
            supports_range=True,
            headers=headers,
        )

    async def _resolve_list(self, list_id: str) -> ResolvedFile:
        headers = self._auth_headers()
        list_url = f"{PIXELDRAIN_LIST_API}/{list_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                list_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        files = data.get("files", [])
        if not files:
            raise ValueError(f"Pixeldrain list {list_id} is empty")
        first = files[0]
        file_id = first["id"]
        filename = sanitize_filename(first.get("name", file_id))
        return ResolvedFile(
            direct_url=f"{PIXELDRAIN_FILE_API}/{file_id}",
            filename=filename,
            total_size=first.get("size", 0),
            supports_range=True,
            headers=headers,
        )

    # ── Filesystem tree API used by the picker ───────────────────────
    async def fetch_root_node(
        self, url: str, session: aiohttp.ClientSession | None = None,
    ) -> tuple[FsNode, list[FsNode], str]:
        """Resolve a user-supplied URL to its root FsNode plus first-level
        children. Returns (root_node, children, error). On error, root is
        a placeholder node and `error` is a human-readable message.

        Single-file URLs (`/u/{id}`, `/api/file/{id}`) are turned into a
        synthetic dir-with-one-child layout so the picker tree treats them
        uniformly with directories.
        """
        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=self._auth_headers(),
            )
        try:
            try:
                # Lists: synthesise a root using existing /api/list endpoint
                if is_pixeldrain_list_url(url):
                    list_id = urlparse(url).path.strip("/").split("/")[1]
                    return await self._fetch_list_as_tree(list_id, session, url)

                # /d/ and /u/ both go through filesystem API. /u/ may 404
                # because it isn't a bucket; fall back to legacy /api/file/.
                file_id = extract_pixeldrain_id(url)
                if not file_id:
                    return (FsNode(path="", name=url, type="dir",
                                   error="Cannot parse Pixeldrain id"),
                            [], "Cannot parse Pixeldrain id")

                fs_path = f"/{file_id}"
                try:
                    return await self.fetch_node_children(fs_path, session)
                except aiohttp.ClientResponseError as e:
                    if e.status == 404:
                        # Legacy single file fallback
                        return await self._fetch_legacy_file_as_tree(file_id, session, url)
                    raise
            except aiohttp.ClientResponseError as e:
                msg = f"HTTP {e.status}"
                if e.status == 403:
                    msg += " (hotlink limit — set API key in Settings)"
                return FsNode(path="", name=url, type="dir", error=msg), [], msg
            except asyncio.TimeoutError:
                return FsNode(path="", name=url, type="dir",
                              error="Request timed out"), [], "Request timed out"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                return FsNode(path="", name=url, type="dir", error=err), [], err
        finally:
            if owns_session:
                await session.close()

    async def fetch_node_children(
        self, fs_path: str, session: aiohttp.ClientSession,
    ) -> tuple[FsNode, list[FsNode], str]:
        """Fetch a directory's metadata + immediate children via /api/filesystem/."""
        encoded = "/".join(quote(p, safe="") for p in fs_path.strip("/").split("/"))
        # ?stat: return JSON metadata. Without it, this URL streams file bytes
        # for leaf paths, which crashes JSON decoding.
        api = f"{PIXELDRAIN_FS_API}/{encoded}?stat"
        async with session.get(api) as r:
            r.raise_for_status()
            data = await r.json()

        path_chain = data.get("path", [])
        base_index = data.get("base_index", 0)
        if not path_chain or base_index >= len(path_chain):
            return (FsNode(path=fs_path, name=fs_path.strip("/").split("/")[-1],
                           type="dir", error="Empty filesystem response"),
                    [], "Empty filesystem response")

        root_dict = path_chain[base_index]
        root = _node_from_dict("", root_dict)
        # Make sure root carries the user-facing path even when API omits it
        if not root.path:
            root.path = fs_path
        children_dicts = data.get("children", []) or []
        children = [_node_from_dict(root.path, c) for c in children_dicts]
        return root, children, ""

    async def _fetch_list_as_tree(
        self, list_id: str, session: aiohttp.ClientSession, source_url: str,
    ) -> tuple[FsNode, list[FsNode], str]:
        api = f"{PIXELDRAIN_LIST_API}/{list_id}"
        async with session.get(api) as r:
            r.raise_for_status()
            data = await r.json()
        files = data.get("files", []) or []
        # Use a synthetic path so children carry stable identifiers
        root = FsNode(path=f"list:{list_id}",
                      name=data.get("title") or f"list:{list_id}",
                      type="dir")
        children = []
        for f in files:
            fid = f["id"]
            children.append(FsNode(
                path=f"file:{fid}",  # marker → resolves via legacy /api/file/
                name=sanitize_filename(f.get("name", fid)),
                type="file",
                size=f.get("size", 0),
                mime_type=f.get("mime_type", ""),
                date_modified=f.get("date_upload", ""),
            ))
        return root, children, ""

    async def _fetch_legacy_file_as_tree(
        self, file_id: str, session: aiohttp.ClientSession, source_url: str,
    ) -> tuple[FsNode, list[FsNode], str]:
        api = f"{PIXELDRAIN_FILE_API}/{file_id}/info"
        async with session.get(api) as r:
            if r.status == 404:
                root = FsNode(path=f"file:{file_id}", name=file_id, type="file",
                              error="File not found (404)")
                return root, [], "File not found (404)"
            r.raise_for_status()
            data = await r.json()
        # A single file gets a synthetic dir wrapper so the tree UI works
        leaf = FsNode(
            path=f"file:{file_id}",
            name=sanitize_filename(data.get("name", file_id)),
            type="file",
            size=data.get("size", 0),
            mime_type=data.get("mime_type", ""),
            date_modified=data.get("date_upload", ""),
        )
        wrapper = FsNode(path=f"file-wrapper:{file_id}",
                         name=leaf.name, type="dir")
        return wrapper, [leaf], ""

    async def resolve_node_url(self, node: FsNode) -> str:
        """Translate an FsNode into the URL string that the queue manager
        passes to add_pair / resolve(). Files only — directories must be
        recursively expanded by the caller first."""
        if node.is_dir:
            raise ValueError("Cannot turn a directory into a download URL")
        if node.path.startswith("file:"):
            file_id = node.path.split(":", 1)[1]
            return f"https://pixeldrain.com/u/{file_id}"
        # Filesystem path → reuse download_url, which resolve() understands
        return node.download_url

    async def expand_directory_recursive(
        self,
        node: FsNode,
        session: aiohttp.ClientSession,
        progress_cb=None,
        cancel_event: asyncio.Event | None = None,
        max_concurrency: int = 4,
    ) -> list[FsNode]:
        """Walk a directory tree and return every file leaf beneath it.

        Skips legacy `list:`/`file:` markers — those can't be re-expanded
        because they live outside the filesystem API. Reports progress via
        `progress_cb(visited_dirs, found_files, current_path)`."""
        if not node.is_dir:
            return [node]
        if not node.path.startswith("/") and node.path.split(":", 1)[0] != "":
            # Synthetic wrapper — treat its (already known) children as the result
            return []

        sem = asyncio.Semaphore(max_concurrency)
        results: list[FsNode] = []
        visited = 0

        async def walk(path: str):
            nonlocal visited
            if cancel_event and cancel_event.is_set():
                return
            async with sem:
                if cancel_event and cancel_event.is_set():
                    return
                try:
                    _root, children, err = await self.fetch_node_children(path, session)
                    visited += 1
                    if progress_cb:
                        progress_cb(visited, len(results), path)
                    if err:
                        return
                except Exception as e:
                    logger.warning("Failed to expand %s: %s", path, e)
                    return
            tasks = []
            for c in children:
                if c.is_dir:
                    tasks.append(walk(c.path))
                else:
                    results.append(c)
                    if progress_cb:
                        progress_cb(visited, len(results), c.path)
            if tasks:
                await asyncio.gather(*tasks)

        await walk(node.path)
        return results

    # ── Legacy helpers retained for callers elsewhere in the codebase ──
    async def resolve_list_all(self, url: str) -> list[ResolvedFile]:
        """Resolve all files in a Pixeldrain list (used by the bundle path)."""
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        if not path_parts or path_parts[0] != "l":
            return [await self.resolve(url)]

        list_id = path_parts[1]
        headers = self._auth_headers()
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                f"{PIXELDRAIN_LIST_API}/{list_id}",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        results = []
        for f in data.get("files", []):
            file_id = f["id"]
            results.append(ResolvedFile(
                direct_url=f"{PIXELDRAIN_FILE_API}/{file_id}",
                filename=sanitize_filename(f.get("name", file_id)),
                total_size=f.get("size", 0),
                supports_range=True,
                headers=headers,
            ))
        return results

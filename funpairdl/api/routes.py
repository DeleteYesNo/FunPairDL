from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from funpairdl import __version__
from funpairdl.api.schemas import (
    AddLinkRequest,
    AddPairRequest,
    PairStatusResponse,
    ProbeRequest,
    QueueStatusResponse,
    ResolveRequest,
    StatusResponse,
)
from funpairdl.core.pair import PairState
from funpairdl.core.queue_manager import QueueManager
from funpairdl.utils.filename import guess_file_type

logger = logging.getLogger("funpairdl.api.routes")

router = APIRouter(prefix="/api")

# Will be set during server startup
_queue_manager: QueueManager | None = None


def set_queue_manager(qm: QueueManager) -> None:
    global _queue_manager
    _queue_manager = qm


def _get_qm() -> QueueManager:
    if _queue_manager is None:
        raise HTTPException(status_code=503, detail="Queue manager not ready")
    return _queue_manager


@router.get("/status")
async def get_status() -> StatusResponse:
    qm = _get_qm()
    return StatusResponse(
        version=__version__,
        queue_size=len(qm.pairs),
    )


@router.get("/config")
async def get_config() -> dict:
    """Return extension-relevant config values."""
    from funpairdl.persistence.settings import Settings
    settings = Settings.load()
    return {
        "gofile_token": settings.gofile_token,
        "default_resolution": settings.default_resolution,
    }


@router.post("/pair")
async def add_pair(req: AddPairRequest) -> dict:
    qm = _get_qm()

    # Validate: must have *something* to download, either in legacy flat
    # lists or in any group.
    has_legacy = bool(req.video_urls or req.script_urls)
    has_groups = bool(req.groups and any(
        g.video_urls or g.script_urls for g in req.groups
    ))
    if not has_legacy and not has_groups:
        raise HTTPException(status_code=400, detail="At least one URL required")

    # Save EroScripts cookies from extension for backend downloads
    if req.eroscripts_cookies:
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if settings.eroscripts_cookies != req.eroscripts_cookies:
            settings.eroscripts_cookies = req.eroscripts_cookies
            settings.save()
            logger.info("Saved EroScripts cookies from extension (%d chars)", len(req.eroscripts_cookies))

    groups_payload: list[dict] | None = None
    if req.groups:
        groups_payload = [g.model_dump() for g in req.groups]

    pair = qm.add_pair(
        name=req.name or "Untitled",
        video_urls=req.video_urls,
        script_urls=req.script_urls,
        preferred_resolution=req.preferred_resolution,
        script_authors=req.script_authors,
        auto_rename=req.auto_rename,
        groups=groups_payload,
    )

    logger.info("Pair added via API: %s (%d items)", pair.name, len(pair.items))

    return {
        "status": "ok",
        "pair_id": pair.id,
        "name": pair.name,
        "items_count": len(pair.items),
    }


@router.post("/resolve")
async def resolve_url(req: ResolveRequest) -> dict:
    """Resolve a short-url to a direct CDN URL using provided cookies.

    NOTE: This endpoint is now a fallback — the embedded browser resolves
    short-URLs in-browser via fetch() to avoid Discourse auth-token rotation.
    """
    import aiohttp
    from funpairdl.constants import BROWSER_USER_AGENT

    cookies: dict[str, str] = {}
    if req.cookies:
        for part in req.cookies.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k.strip()] = v.strip()

    # Also save cookies to settings if provided (for backend download use)
    if req.cookies:
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        if settings.eroscripts_cookies != req.cookies:
            settings.eroscripts_cookies = req.cookies
            settings.save()

    try:
        async with aiohttp.ClientSession(
            cookies=cookies,
            headers={"User-Agent": BROWSER_USER_AGENT},
        ) as session:
            async with session.head(
                req.url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                final_url = str(resp.url)
                if resp.status >= 400:
                    logger.warning("Resolve got %d for %s", resp.status, req.url[:80])
                    return {"success": False, "error": f"HTTP {resp.status}"}
                # If URL didn't change, the redirect didn't happen (likely missing auth)
                if final_url == req.url and "short-url" in req.url:
                    logger.warning("Resolve: no redirect for short-url %s (missing cookies?)", req.url[:80])
                    return {"success": False, "error": "No redirect (missing authentication?)"}

                # Propagate rotated cookies back to settings
                _sync_resolve_cookies(resp, req.cookies)

                logger.info("Resolved %s -> %s", req.url[:80], final_url[:80])
                return {"success": True, "url": final_url}
    except Exception as e:
        logger.error("Failed to resolve %s: %s", req.url[:80], e)
        return {"success": False, "error": str(e)}


def _sync_resolve_cookies(resp, original_cookie_str: str | None) -> None:
    """Capture rotated cookies from resolve response and update settings."""
    if not original_cookie_str:
        return

    all_responses = list(resp.history) + [resp]
    updated = {}
    for r in all_responses:
        for sc in r.headers.getall("Set-Cookie", []):
            kv = sc.split(";", 1)[0].strip()
            if "=" not in kv:
                continue
            name, _, value = kv.partition("=")
            name = name.strip()
            if name.lower() in ("path", "domain", "expires", "max-age", "samesite"):
                continue
            updated[name] = value

    if not updated:
        return

    existing = {}
    for part in original_cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            n, _, v = part.partition("=")
            existing[n.strip()] = v

    changed = False
    for name, value in updated.items():
        if existing.get(name) != value:
            existing[name] = value
            changed = True
            logger.info("Resolve: cookie rotated by server: %s", name)

    if changed:
        from funpairdl.persistence.settings import Settings
        settings = Settings.load()
        new_cookie_str = "; ".join(f"{n}={v}" for n, v in existing.items())
        settings.eroscripts_cookies = new_cookie_str
        settings.save()


@router.post("/probe")
async def probe_url(req: ProbeRequest) -> dict:
    """Probe a URL for metadata: file size and available formats."""
    import aiohttp
    from funpairdl.utils.url_parser import detect_provider, extract_pixeldrain_id

    provider = detect_provider(req.url)

    # For yt-dlp sites, extract format info
    if provider == "ytdlp" or provider in {"rule34video", "rule34", "hanime1", "iwara", "bilibili"}:
        try:
            import asyncio

            def _extract():
                import yt_dlp
                base_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": False,
                }

                # Strategy 1: impersonation (best for Cloudflare-protected sites)
                try:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    opts = {**base_opts, "impersonate": ImpersonateTarget(client="chrome")}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(req.url, download=False)
                except ImportError:
                    pass  # curl_cffi not installed
                except Exception as e1:
                    logger.debug("Probe impersonation failed for %s: %s", req.url[:60], e1)

                # Strategy 2: clean extraction (no impersonation, no cookies)
                with yt_dlp.YoutubeDL(base_opts) as ydl:
                    return ydl.extract_info(req.url, download=False)

            info = await asyncio.to_thread(_extract)
            formats = info.get("formats", [])

            def _extract_height(fmt):
                """Get height from format metadata or parse from URL."""
                import re
                h = fmt.get("height") or 0
                if h:
                    return h
                # Try to parse from URL (e.g. "3863141_720p.mp4" or "404797-1080p.mp4")
                url = fmt.get("url", "")
                m = re.search(r'[-_](\d{3,4})p?\.', url)
                if m:
                    return int(m.group(1))
                return 0

            # Build format list: prefer combined (video+audio), fallback to all
            candidates = [
                f for f in formats
                if f.get("vcodec") != "none" and f.get("acodec") != "none"
            ]
            if not candidates:
                candidates = formats

            available = []
            for fmt in candidates:
                h = _extract_height(fmt)
                size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                available.append({
                    "height": h, "size": size,
                    "format_id": fmt.get("format_id", ""),
                    "_url": fmt.get("url", ""),
                })

            # If sizes are missing, do concurrent HEAD requests to get Content-Length
            missing = [a for a in available if not a["size"] and a["_url"]]
            if missing:
                async def _head_size(entry):
                    try:
                        async with aiohttp.ClientSession() as s:
                            async with s.head(
                                entry["_url"], allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=8),
                            ) as r:
                                entry["size"] = int(r.headers.get("Content-Length", 0))
                    except Exception:
                        pass

                await asyncio.gather(*[_head_size(e) for e in missing])

            # Remove internal _url field
            for a in available:
                a.pop("_url", None)

            return {
                "success": True,
                "provider": "ytdlp",
                "title": info.get("title", ""),
                "filename": info.get("title", ""),
                "formats": available,
            }
        except Exception as e:
            logger.error("Probe failed for %s: %s", req.url[:80], e)
            return {"success": False, "error": str(e)}

    # GoFile: use API to get file info
    if provider == "gofile":
        try:
            from urllib.parse import urlparse
            from funpairdl.persistence.settings import Settings
            from funpairdl.providers.gofile import GoFileProvider

            settings = Settings.load()
            gf = GoFileProvider(token=settings.gofile_token)

            path_parts = urlparse(req.url).path.strip("/").split("/")
            content_id = path_parts[-1] if path_parts else None
            if not content_id:
                return {"success": False, "error": "Invalid GoFile URL"}

            async with aiohttp.ClientSession() as session:
                token = await gf._get_token(session)
                wt = await gf._get_website_token(session)

                async with session.get(
                    f"https://api.gofile.io/contents/{content_id}?wt={wt}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") != "ok":
                            return {"success": False, "error": data.get("status", "API error")}
                        children = data.get("data", {}).get("children", {})
                        total_size = 0
                        files = []
                        for child in children.values():
                            if child.get("type") == "file":
                                total_size += child.get("size", 0)
                                files.append({
                                    "name": child.get("name", ""),
                                    "size": child.get("size", 0),
                                    "url": f"https://gofile.io/d/{child.get('code') or child.get('id') or content_id}",
                                })
                        return {
                            "success": True,
                            "provider": "gofile",
                            "size": total_size,
                            "filename": files[0]["name"] if len(files) == 1 else f"{len(files)} files",
                            "files": files if files else None,
                        }
                    return {"success": False, "error": f"Status {resp.status}"}
        except Exception as e:
            logger.error("Probe failed for %s: %s", req.url[:80], e)
            return {"success": False, "error": str(e)}

    # Pixeldrain: use API to get file info
    if provider == "pixeldrain":
        try:
            from urllib.parse import urlparse
            path = urlparse(req.url).path.strip("/")

            async with aiohttp.ClientSession() as session:
                # List URL: /l/{listId}
                if path.startswith("l/"):
                    list_id = path.split("/")[1]
                    async with session.get(
                        f"https://pixeldrain.com/api/list/{list_id}",
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            files = data.get("files", [])
                            total_size = sum(f.get("size", 0) for f in files)
                            file_list = [
                                {
                                    "name": f.get("name", "?"),
                                    "size": f.get("size", 0),
                                    "url": f"https://pixeldrain.com/u/{f.get('id', '')}",
                                }
                                for f in files
                            ]
                            return {
                                "success": True,
                                "provider": "pixeldrain",
                                "size": total_size,
                                "filename": f"{len(files)} files",
                                "files": file_list,
                            }
                        return {"success": False, "error": f"Status {resp.status}"}

                # Folder URL: /d/{id} (filesystem bucket — may hold per-pack
                # subfolders). Walk it so the UI can list/expand the contents.
                if path.startswith("d/"):
                    from funpairdl.providers.pixeldrain import PixeldrainProvider
                    from funpairdl.persistence.settings import Settings
                    pd = PixeldrainProvider(api_key=Settings.load().pixeldrain_api_key)
                    rfs = await pd.resolve_folder_all(req.url)
                    if rfs:
                        file_list = [
                            {"name": rf.filename, "size": rf.total_size,
                             "url": rf.direct_url}
                            for rf in rfs
                        ]
                        return {
                            "success": True,
                            "provider": "pixeldrain",
                            "size": sum(rf.total_size for rf in rfs),
                            "filename": f"{len(file_list)} files",
                            "files": file_list,
                        }
                    return {"success": False, "error": "Empty Pixeldrain folder"}

                # Single file URL: /u/{fileId}
                file_id = extract_pixeldrain_id(req.url)
                if not file_id:
                    return {"success": False, "error": "Invalid Pixeldrain URL"}
                async with session.get(
                    f"https://pixeldrain.com/api/file/{file_id}/info",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {
                            "success": True,
                            "provider": "pixeldrain",
                            "size": data.get("size", 0),
                            "filename": data.get("name", ""),
                        }
                    return {"success": False, "error": f"Status {resp.status}"}
        except Exception as e:
            logger.error("Probe failed for %s: %s", req.url[:80], e)
            return {"success": False, "error": str(e)}

    # MEGA: use direct API to get file/folder info
    if provider == "mega":
        try:
            from funpairdl.utils.mega_api import parse_mega_url, probe_mega_file, probe_mega_folder

            info = parse_mega_url(req.url)
            if not info:
                return {"success": False, "error": "Invalid MEGA URL"}
            if info["type"] == "folder":
                return await probe_mega_folder(req.url)
            # "file" and "folder_file" both probe as single file
            return await probe_mega_file(req.url)
        except Exception as e:
            logger.error("MEGA probe failed for %s: %s", req.url[:80], e)
            return {"success": False, "error": str(e)}

    # HMV Mania: scrape page for mp4 candidates and HEAD each for size + height
    if provider == "hmvmania":
        try:
            import asyncio
            from funpairdl.constants import BROWSER_USER_AGENT
            from funpairdl.providers.hmvmania import (
                height_from_url, parse_mp4_candidates, parse_page_title,
            )

            headers = {"User-Agent": BROWSER_USER_AGENT}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    req.url, headers=headers, allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    html = await resp.text(errors="ignore")
                    page_url = str(resp.url)

                candidates = parse_mp4_candidates(html, page_url)
                title = parse_page_title(html)
                if not candidates:
                    return {"success": False, "error": "No mp4 source found on page"}

                async def _head_size(url: str) -> int:
                    try:
                        async with session.head(
                            url, headers=headers, allow_redirects=True,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as r:
                            return int(r.headers.get("Content-Length", 0))
                    except Exception:
                        return 0

                sizes = await asyncio.gather(*[_head_size(c) for c in candidates])

            formats = [
                {"height": height_from_url(c), "size": s, "format_id": ""}
                for c, s in zip(candidates, sizes)
            ]
            # UI expects ascending order (lo→hi) to render "lo p~hi p".
            formats.sort(key=lambda f: f["height"])

            filename = (title + ".mp4") if title else ""
            return {
                "success": True,
                "provider": "hmvmania",
                "title": title,
                "filename": filename,
                "formats": formats,
            }
        except Exception as e:
            logger.error("HMV Mania probe failed for %s: %s", req.url[:80], e)
            return {"success": False, "error": str(e)}

    # EroScripts short-urls: need cookies, skip probing
    if provider == "eroscripts":
        return {"success": True, "provider": "eroscripts", "size": 0}

    # For direct HTTP, do a HEAD request to get Content-Length
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                req.url, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                size = int(resp.headers.get("Content-Length", 0))
                return {
                    "success": True,
                    "provider": provider or "direct",
                    "size": size,
                }
    except Exception as e:
        logger.error("Probe failed for %s: %s", req.url[:80], e)
        return {"success": False, "error": str(e)}


@router.post("/link")
async def add_link(req: AddLinkRequest) -> dict:
    qm = _get_qm()

    file_type = req.file_type
    if file_type == "auto":
        file_type = guess_file_type(req.url)

    if file_type == "funscript":
        pair = qm.add_pair(
            name=req.name or "Script Download",
            video_urls=[],
            script_urls=[req.url],
        )
    else:
        pair = qm.add_pair(
            name=req.name or "Video Download",
            video_urls=[req.url],
            script_urls=[],
        )

    return {
        "status": "ok",
        "pair_id": pair.id,
        "name": pair.name,
    }


@router.get("/queue")
async def get_queue() -> QueueStatusResponse:
    qm = _get_qm()

    pairs = []
    active_pair = None

    for p in qm.pairs:
        pairs.append(PairStatusResponse(
            id=p.id,
            name=p.name,
            state=p.state.value,
            progress=p.progress,
            items=[i.to_dict() for i in p.items],
        ))
        if p.state == PairState.DOWNLOADING:
            active_pair = p.id

    return QueueStatusResponse(
        pairs=pairs,
        total_pairs=len(pairs),
        active_pair=active_pair,
    )


@router.post("/pair/{pair_id}/pause")
async def pause_pair(pair_id: str) -> dict:
    qm = _get_qm()
    qm.pause_pair(pair_id)
    return {"status": "ok"}


@router.post("/pair/{pair_id}/resume")
async def resume_pair(pair_id: str) -> dict:
    qm = _get_qm()
    qm.resume_pair(pair_id)
    return {"status": "ok"}


@router.delete("/pair/{pair_id}")
async def remove_pair(pair_id: str) -> dict:
    qm = _get_qm()
    qm.remove_pair(pair_id)
    return {"status": "ok"}


@router.post("/pair/{pair_id}/move")
async def move_pair(pair_id: str, direction: int = 0) -> dict:
    qm = _get_qm()
    qm.move_pair(pair_id, direction)
    return {"status": "ok"}


@router.post("/pair/{pair_id}/organize")
async def organize_pair(pair_id: str) -> dict:
    """Trigger file rename/organize for a completed pair."""
    qm = _get_qm()
    ok = qm.organize_pair(pair_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Pair not found, not completed, or already organized")
    return {"status": "ok"}


@router.post("/pair/{pair_id}/undo-organize")
async def undo_organize_pair(pair_id: str) -> dict:
    """Undo file rename/organize for a completed pair."""
    qm = _get_qm()
    ok = qm.undo_organize_pair(pair_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Pair not found, not completed, or not organized")
    return {"status": "ok"}


@router.post("/pump/restart")
async def restart_pump() -> dict:
    """Force-restart the download pump. Use when downloads appear stuck."""
    qm = _get_qm()
    await qm.force_restart_pump()
    return {"status": "ok", "message": "Pump restarted"}

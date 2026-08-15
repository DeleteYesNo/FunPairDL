from __future__ import annotations

import asyncio
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
from funpairdl.core.pair import ItemState, PairState
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
    # File I/O off the api-worker loop — handlers must not block it.
    settings = await asyncio.to_thread(Settings.load)
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

    # Save EroScripts cookies from extension for backend downloads.
    # Settings.update = locked read-modify-write: a plain load+save here
    # would race the browser's tab-state/cookie-jar saves on other threads
    # and revert their fields.
    if req.eroscripts_cookies:
        from funpairdl.persistence.settings import Settings

        def _apply(s, cookies=req.eroscripts_cookies):
            if s.eroscripts_cookies != cookies:
                s.eroscripts_cookies = cookies
                logger.info("Saved EroScripts cookies from extension (%d chars)", len(cookies))

        await asyncio.to_thread(Settings.update, _apply)

    groups_payload: list[dict] | None = None
    if req.groups:
        # model_dump includes each group's probed "sizes"/"filenames" maps.
        groups_payload = [g.model_dump() for g in req.groups]

    pair = qm.add_pair(
        name=req.name or "Untitled",
        video_urls=req.video_urls,
        script_urls=req.script_urls,
        preferred_resolution=req.preferred_resolution,
        script_authors=req.script_authors,
        auto_rename=req.auto_rename,
        groups=groups_payload,
        filenames=req.filenames,
        sizes=req.sizes,
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

    # Also save cookies to settings if provided (for backend download use).
    # Locked read-modify-write — see /pair's cookie save.
    if req.cookies:
        from funpairdl.persistence.settings import Settings

        def _apply(s, cookies=req.cookies):
            if s.eroscripts_cookies != cookies:
                s.eroscripts_cookies = cookies

        await asyncio.to_thread(Settings.update, _apply)

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
                await _sync_resolve_cookies(resp, req.cookies)

                logger.info("Resolved %s -> %s", req.url[:80], final_url[:80])
                return {"success": True, "url": final_url}
    except Exception as e:
        logger.error("Failed to resolve %s: %s", req.url[:80], e)
        return {"success": False, "error": str(e)}


async def _sync_resolve_cookies(resp, original_cookie_str: str | None) -> None:
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
        new_cookie_str = "; ".join(f"{n}={v}" for n, v in existing.items())

        def _apply(s, cookies=new_cookie_str):
            s.eroscripts_cookies = cookies

        # Locked read-modify-write — see /pair's cookie save.
        await asyncio.to_thread(Settings.update, _apply)


@router.post("/probe")
async def probe_url(req: ProbeRequest) -> dict:
    """Probe a URL for metadata: file size and available formats.

    The heavy lifting lives in funpairdl.providers.probe (TTL-cached,
    shared with the queue's off-slot metadata prober). Response shape is
    unchanged — content.js parses it.
    """
    from funpairdl.persistence.settings import Settings
    from funpairdl.providers.probe import probe_url_info

    settings = await asyncio.to_thread(Settings.load)
    return await probe_url_info(req.url, settings=settings)


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


def _pair_progress_from_dict(d: dict) -> float:
    """Replicate Pair.progress from a snapshot dict (see core/pair.py)."""
    items = d.get("items", [])
    if not items:
        return 0.0
    completed = sum(1 for i in items if i.get("state") == ItemState.COMPLETED.value)
    if completed == len(items):
        return 100.0
    total = sum(int(i.get("total_bytes") or 0) for i in items)
    if total <= 0:
        return completed / len(items) * 100
    downloaded = sum(int(i.get("downloaded_bytes") or 0) for i in items)
    pct = min(downloaded / total * 100, 100.0)
    # Same clamp as Pair.progress: byte-progress can hit 100 while an item
    # that failed before getting a size (total_bytes=0) is still incomplete.
    if pct >= 100.0 and completed < len(items):
        return completed / len(items) * 100
    return pct


@router.get("/queue")
async def get_queue() -> QueueStatusResponse:
    qm = _get_qm()

    # Lock-held snapshot with segments already stripped from item dicts —
    # the API response never needs them and they dominate payload size.
    pair_dicts = qm.get_queue_status()

    pairs = []
    active_pair = None

    for d in pair_dicts:
        pairs.append(PairStatusResponse(
            id=d.get("id", ""),
            name=d.get("name", ""),
            state=d.get("state", ""),
            progress=_pair_progress_from_dict(d),
            items=d.get("items", []),
        ))
        if d.get("state") == PairState.DOWNLOADING.value:
            active_pair = d.get("id")

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
    # Async variant runs the sha256/rename work via to_thread and returns
    # False when the pair is busy/not eligible.
    ok = await qm.organize_pair_async(pair_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Pair not found, not completed, or already organized")
    return {"status": "ok"}


@router.post("/pair/{pair_id}/undo-organize")
async def undo_organize_pair(pair_id: str) -> dict:
    """Undo file rename/organize for a completed pair."""
    qm = _get_qm()
    ok = await qm.undo_organize_pair_async(pair_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Pair not found, not completed, or not organized")
    return {"status": "ok"}


@router.post("/pump/restart")
async def restart_pump() -> dict:
    """Force-restart the download pump. Use when downloads appear stuck."""
    qm = _get_qm()
    await qm.force_restart_pump()
    return {"status": "ok", "message": "Pump restarted"}

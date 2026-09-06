from __future__ import annotations

"""Shared URL metadata probing.

This module owns the probe logic that used to live inline in the /probe API
handler. It is used from two places:

- ``probe_url_info()`` — the /probe endpoint (api-worker loop). Returns the
  full response dict (``success``/``provider``/``size``/``filename``/
  ``formats``/``files``/...) whose shape content.js depends on.
- ``probe_meta()`` — the off-slot metadata prober in QueueManager (dl loop).
  Returns a small ``ProbeMeta`` derived from the same result.

Results are cached with a TTL (PROBE_CACHE_TTL_SECONDS) so panel re-opens and
the off-slot prober don't re-pay network round trips. yt-dlp extraction runs
on a small dedicated executor so probe storms can never occupy the loop's
default executor (shared with disk flushes).
"""

import asyncio
import logging
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT, PROBE_CACHE_TTL_SECONDS
from funpairdl.utils.url_parser import detect_provider, extract_pixeldrain_id

logger = logging.getLogger("funpairdl.providers.probe")


@dataclass
class ProbeMeta:
    size: int = 0
    filename: str = ""
    source: str = ""  # "pixeldrain" | "gofile" | "mega" | "head" | "ytdlp" | ...


# ---------------------------------------------------------------------------
# yt-dlp executor: dedicated 2-worker pool, NOT the loop default executor.
# ---------------------------------------------------------------------------

_YTDLP_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdlp-probe")
_YTDLP_TIMEOUT = 60  # seconds — outer cap on one yt-dlp extraction

# wait_for can't kill a running extraction thread — dedup in-flight
# extractions per URL (re-probes await the same future instead of stacking
# more work on the 2-worker pool) and negative-cache timed-out URLs briefly
# so panel re-opens don't immediately poison the pool again.
_ytdlp_inflight: dict[str, "Future"] = {}
_ytdlp_negative: dict[str, float] = {}  # url -> monotonic ts of last timeout
_ytdlp_lock = threading.Lock()
_YTDLP_NEGATIVE_TTL = 120  # seconds

# ---------------------------------------------------------------------------
# TTL cache: url -> (monotonic timestamp, result dict). Only fruitful results
# (success + size>0 or non-empty filename) are cached. Thread-safe — the
# cache is shared by the api-worker loop and the dl loop.
# ---------------------------------------------------------------------------

_CACHE_MAX_ENTRIES = 500

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _is_fruitful(result: dict) -> bool:
    """Only results that actually carry metadata are worth caching."""
    if not result.get("success"):
        return False
    if int(result.get("size") or 0) > 0:
        return True
    return bool(result.get("filename"))


def _cache_get(url: str, now: float | None = None) -> dict | None:
    if now is None:
        now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        ts, result = entry
        if (now - ts) >= PROBE_CACHE_TTL_SECONDS:
            del _cache[url]
            return None
        return result


def _cache_put(url: str, result: dict, now: float | None = None) -> None:
    if not _is_fruitful(result):
        return
    if now is None:
        now = time.monotonic()
    with _cache_lock:
        # Drop expired entries first, then oldest-inserted until under cap.
        expired = [k for k, (ts, _) in _cache.items()
                   if (now - ts) >= PROBE_CACHE_TTL_SECONDS]
        for k in expired:
            del _cache[k]
        while len(_cache) >= _CACHE_MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[url] = (now, result)


def clear_probe_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Shared per-loop aiohttp session (used when the caller doesn't pass one).
# Keyed weakly by event loop: the api-worker loop and the dl loop each get
# their own long-lived pooled session instead of one throwaway per probe.
# ---------------------------------------------------------------------------

_loop_sessions: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aiohttp.ClientSession]" = (
    weakref.WeakKeyDictionary()
)
_sessions_lock = threading.Lock()


def _shared_session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    with _sessions_lock:
        sess = _loop_sessions.get(loop)
        if sess is None or sess.closed:
            sess = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=20),
            )
            _loop_sessions[loop] = sess
        return sess


async def close_probe_session() -> None:
    """Close the shared probe session of the *current* loop (shutdown aid)."""
    loop = asyncio.get_running_loop()
    with _sessions_lock:
        sess = _loop_sessions.pop(loop, None)
    if sess is not None and not sess.closed:
        await sess.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def probe_url_info(
    url: str,
    *,
    settings,
    session: aiohttp.ClientSession | None = None,
) -> dict:
    """Probe a URL for metadata; returns the /probe response dict.

    The response shape is the contract content.js parses — do not change it:
    ``{"success": bool, "provider": str, "size": int, "filename": str,
    "formats": [...], "files": [...], "title": str, "error": str}``
    (fields present per provider branch, exactly as the old inline handler).
    """
    cached = _cache_get(url)
    if cached is not None:
        return cached

    if session is None:
        session = _shared_session()

    result = await _probe_uncached(url, settings, session)
    _cache_put(url, result)
    return result


async def probe_meta(
    url: str,
    *,
    settings,
    session: aiohttp.ClientSession | None = None,
) -> ProbeMeta:
    """Small size/filename probe for the off-slot metadata prober (W2).

    Never raises for probe failures — returns an empty ProbeMeta instead.
    """
    try:
        info = await probe_url_info(url, settings=settings, session=session)
    except Exception as e:  # defensive: prober must never crash the dl loop
        logger.debug("probe_meta failed for %s: %s", url[:80], e)
        return ProbeMeta()
    return _meta_from_info(info)


def _meta_from_info(info: dict) -> ProbeMeta:
    """Pure: derive ProbeMeta from a /probe-style result dict."""
    if not info or not info.get("success"):
        return ProbeMeta()

    size = int(info.get("size") or 0)
    if size <= 0:
        # yt-dlp / hmvmania style results: pick the best (max height) format
        # that has a known size — matches the "best" default resolution. The
        # real download resolve overwrites this later, so approximate is fine.
        best: dict | None = None
        for fmt in info.get("formats") or []:
            if not fmt.get("size"):
                continue
            if best is None or (fmt.get("height") or 0) > (best.get("height") or 0):
                best = fmt
        if best is not None:
            size = int(best.get("size") or 0)

    filename = str(info.get("filename") or "")
    # Multi-file bundles report a placeholder like "3 files" — not a filename.
    files = info.get("files")
    if files and len(files) != 1:
        filename = ""
    # probe_mega_file keeps "MEGA file" as a UI placeholder when the name
    # can't be decrypted — never let it become an item's real filename.
    if filename == "MEGA file":
        filename = ""

    source = str(info.get("provider") or "")
    # yt-dlp results carry a bare title with no file extension — persisting
    # that as item.filename yields extensionless files if the pair fails
    # before resolve. Let resolve (which knows the chosen format) name it.
    if source in ("ytdlp", "rule34video", "rule34", "hanime1", "iwara", "bilibili"):
        filename = ""
    if source == "direct":
        source = "head"
    return ProbeMeta(size=size, filename=filename, source=source)


# ---------------------------------------------------------------------------
# Provider branches (moved from funpairdl/api/routes.py — behavior preserved,
# except: shared session reuse, yt-dlp on _YTDLP_POOL with a 60 s cap, the
# ranged-GET fallback tightened to 8 s, and unlisted video sites routed to
# the yt-dlp branch to mirror download routing (audit [8h]).
# ---------------------------------------------------------------------------


async def _probe_uncached(
    url: str,
    settings,
    session: aiohttp.ClientSession,
) -> dict:
    provider = detect_provider(url)

    # For yt-dlp sites, extract format info
    if provider == "ytdlp" or provider in {"rule34video", "rule34", "hanime1", "iwara", "bilibili"}:
        return await _probe_ytdlp(url, session)

    if provider == "gofile":
        return await _probe_gofile(url, settings, session)

    if provider == "pixeldrain":
        return await _probe_pixeldrain(url, settings, session)

    if provider == "mega":
        return await _probe_mega(url)

    if provider == "hmvmania":
        return await _probe_hmvmania(url, session)

    if provider == "socigames":
        return await _probe_socigames(url, session)

    if provider == "e621":
        return await _probe_e621(url, session)

    # EroScripts short-urls: need cookies, skip probing
    if provider == "eroscripts":
        return {"success": True, "provider": "eroscripts", "size": 0}

    # Direct file links on unlisted hosts (catbox .webm/.gif, .7z packs, …):
    # HEAD gives the right size in one RTT; sending them into yt-dlp costs a
    # multi-second generic extraction (or a permanent '?') for zero benefit —
    # the byte size is the same either way for a direct file.
    try:
        from urllib.parse import urlparse
        path = (urlparse(url).path or "").lower()
    except ValueError:
        return {"success": False, "error": "Malformed URL"}
    if any(path.endswith(ext) for ext in _DIRECT_FILE_EXTENSIONS):
        return await _probe_direct(url, provider, session)

    # Unlisted hosts: the download side routes anything YtdlpGenericProvider
    # accepts (non-direct-file URLs) through yt-dlp — probing them with a HEAD
    # reported the HTML page's Content-Length as the file size (audit [8h]).
    # Mirror the download routing so probe and resolve agree.
    try:
        from funpairdl.providers.ytdlp_generic import YtdlpGenericProvider
        route_ytdlp = YtdlpGenericProvider.can_handle(url)
    except ValueError:
        # urlparse can raise on mangled hrefs (unmatched brackets) — a bad
        # link in a post must return a probe failure, not an HTTP 500.
        return {"success": False, "error": "Malformed URL"}
    if route_ytdlp:
        return await _probe_ytdlp(url, session)

    return await _probe_direct(url, provider, session)


# File extensions that identify a URL as a direct file download — probed via
# HEAD regardless of the host. Superset of the exemptions in
# YtdlpGenericProvider.can_handle (those also affect download routing; this
# list only affects probing, where HEAD is always the right answer for a
# direct file).
_DIRECT_FILE_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".wmv", ".flv", ".m4v", ".ts",
    ".gif", ".funscript", ".zip", ".rar", ".7z",
)


async def _probe_ytdlp(url: str, session: aiohttp.ClientSession) -> dict:
    """yt-dlp format extraction on the dedicated probe executor (60 s cap)."""
    try:
        def _extract():
            import yt_dlp
            base_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": False,
                # Bound each network read — without this a stalled site can
                # occupy a pool worker long past our 60s outer cap.
                "socket_timeout": 30,
            }

            # Strategy 1: impersonation (best for Cloudflare-protected sites)
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts = {**base_opts, "impersonate": ImpersonateTarget(client="chrome")}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except ImportError:
                pass  # curl_cffi not installed
            except Exception as e1:
                logger.debug("Probe impersonation failed for %s: %s", url[:60], e1)

            # Strategy 2: clean extraction (no impersonation, no cookies)
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                return ydl.extract_info(url, download=False)

        now = time.monotonic()
        with _ytdlp_lock:
            neg = _ytdlp_negative.get(url)
            if neg is not None and (now - neg) < _YTDLP_NEGATIVE_TTL:
                return {"success": False,
                        "error": "yt-dlp extraction recently timed out (cached)"}
            fut = _ytdlp_inflight.get(url)
            if fut is None or fut.done():
                fut = _YTDLP_POOL.submit(_extract)
                _ytdlp_inflight[url] = fut

                def _pop(_f, u=url, mine=fut):
                    with _ytdlp_lock:
                        if _ytdlp_inflight.get(u) is mine:
                            del _ytdlp_inflight[u]
                fut.add_done_callback(_pop)

        try:
            info = await asyncio.wait_for(asyncio.wrap_future(fut), _YTDLP_TIMEOUT)
        except asyncio.TimeoutError:
            with _ytdlp_lock:
                _ytdlp_negative[url] = time.monotonic()
            raise
        formats = info.get("formats", [])

        def _extract_height(fmt):
            """Get height from format metadata or parse from URL."""
            import re
            h = fmt.get("height") or 0
            if h:
                return h
            # Try to parse from URL (e.g. "3863141_720p.mp4" or "404797-1080p.mp4")
            fmt_url = fmt.get("url", "")
            m = re.search(r'[-_](\d{3,4})p?\.', fmt_url)
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
                    async with session.head(
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
            "thumbnail": info.get("thumbnail") or "",
            "duration": info.get("duration") or None,
        }
    except asyncio.TimeoutError:
        logger.error("yt-dlp probe timed out after %ds for %s", _YTDLP_TIMEOUT, url[:80])
        return {"success": False, "error": f"yt-dlp probe timed out after {_YTDLP_TIMEOUT}s"}
    except Exception as e:
        logger.error("Probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_gofile(url: str, settings, session: aiohttp.ClientSession) -> dict:
    """GoFile: use API to get file info (tokens cached inside gofile.py)."""
    try:
        from urllib.parse import urlparse
        from funpairdl.providers.gofile import GoFileProvider

        gf = GoFileProvider(token=settings.gofile_token)

        path_parts = urlparse(url).path.strip("/").split("/")
        content_id = path_parts[-1] if path_parts else None
        if not content_id:
            return {"success": False, "error": "Invalid GoFile URL"}

        from funpairdl.providers.gofile import (
            CONTENTS_PARAMS, _describe_error, api_headers,
        )

        token = await gf._get_token(session)
        wt = await gf._get_website_token(session)

        async with session.get(
            f"https://api.gofile.io/contents/{content_id}",
            params=CONTENTS_PARAMS,
            headers=api_headers(token, wt),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("status") != "ok":
                    return {"success": False,
                            "error": _describe_error(resp.status, data.get("status"))}
                content = data.get("data", {}) or {}

                def _entry(node: dict, fallback_id: str) -> dict:
                    return {
                        "name": node.get("name", ""),
                        "size": node.get("size", 0),
                        "url": f"https://gofile.io/d/"
                               f"{node.get('code') or node.get('id') or fallback_id}",
                    }

                # A /d/ link can point straight at a file rather than a folder,
                # in which case there are no children and the metadata sits at
                # the top level. Reading only children reported "0 files" and a
                # zero size, so the UI showed no size for single-file links —
                # resolve() has always handled both shapes.
                if content.get("type") == "file" or "link" in content:
                    files = [_entry(content, content_id)]
                else:
                    children = content.get("children") or {}
                    values = (children.values() if isinstance(children, dict)
                              else children)
                    files = [_entry(c, content_id) for c in values
                             if c.get("type") == "file"]
                total_size = sum(f["size"] or 0 for f in files)
                return {
                    "success": True,
                    "provider": "gofile",
                    "size": total_size,
                    "filename": files[0]["name"] if len(files) == 1 else f"{len(files)} files",
                    "files": files if files else None,
                }
            # Non-200: the body names the real reason (error-notPremium etc.),
            # which is far more useful in the UI than a bare status code.
            try:
                api_status = (await resp.json()).get("status")
            except Exception:
                api_status = None
            return {"success": False,
                    "error": _describe_error(resp.status, api_status)}
    except Exception as e:
        logger.error("Probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_pixeldrain(url: str, settings, session: aiohttp.ClientSession) -> dict:
    """Pixeldrain: use API to get file info."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.strip("/")

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
            pd = PixeldrainProvider(api_key=settings.pixeldrain_api_key)
            rfs = await pd.resolve_folder_all(url)
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
        file_id = extract_pixeldrain_id(url)
        if not file_id:
            return {"success": False, "error": "Invalid Pixeldrain URL"}
        async with session.get(
            f"https://pixeldrain.com/api/file/{file_id}/info",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = {
                    "success": True,
                    "provider": "pixeldrain",
                    "size": data.get("size", 0),
                    "filename": data.get("name", ""),
                }
                # Duration from the container header — two small ranged
                # reads against the file endpoint, not a download.
                from funpairdl.utils.media_duration import (
                    looks_like_video, probe_media_duration,
                )
                name = data.get("name", "") or ""
                if looks_like_video(name):
                    duration = await probe_media_duration(
                        f"https://pixeldrain.com/api/file/{file_id}", session, {}, name)
                    if duration:
                        result["duration"] = duration
                return result
            return {"success": False, "error": f"Status {resp.status}"}
    except Exception as e:
        logger.error("Probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_mega(url: str) -> dict:
    """MEGA: use direct API to get file/folder info (own session inside)."""
    try:
        from funpairdl.utils.mega_api import parse_mega_url, probe_mega_file, probe_mega_folder

        info = parse_mega_url(url)
        if not info:
            return {"success": False, "error": "Invalid MEGA URL"}
        if info["type"] == "folder":
            return await probe_mega_folder(url)
        # "file" and "folder_file" both probe as single file
        return await probe_mega_file(url)
    except Exception as e:
        logger.error("MEGA probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_hmvmania(url: str, session: aiohttp.ClientSession) -> dict:
    """HMV Mania: scrape page for mp4 candidates and HEAD each for size + height."""
    try:
        from funpairdl.providers.hmvmania import (
            height_from_url, parse_mp4_candidates, parse_page_title,
        )

        headers = {"User-Agent": BROWSER_USER_AGENT}
        async with session.get(
            url, headers=headers, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            html = await resp.text(errors="ignore")
            page_url = str(resp.url)

        candidates = parse_mp4_candidates(html, page_url)
        title = parse_page_title(html)
        if not candidates:
            return {"success": False, "error": "No mp4 source found on page"}

        async def _head_size(u: str) -> int:
            try:
                async with session.head(
                    u, headers=headers, allow_redirects=True,
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
        logger.error("HMV Mania probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_e621(url: str, session: aiohttp.ClientSession) -> dict:
    """e621: one JSON call lists the original file and every transcode with
    exact sizes — no HEAD needed."""
    try:
        from funpairdl.providers.e621 import (
            build_filename, build_formats, describe_unavailable, fetch_post,
            fetch_title, select_format,
        )

        post, title = await asyncio.gather(
            fetch_post(url, session), fetch_title(url, session))
        renditions = build_formats(post)
        best = select_format(renditions, "best")
        if best is None:
            return {"success": False, "error": describe_unavailable(post)}

        # Ascending lo→hi like the other branches; format_id carries e621's
        # rendition label so the picker can tell "480p" from "original".
        formats = [
            {"height": f["height"], "size": f["size"], "format_id": f["label"]}
            for f in renditions
        ]
        filename = build_filename(post, best, title)
        # Posts in one topic often share a title ("alpha and beta
        # (…) created by X" ×2); the general tags name the scene (shower,
        # kneeling, reverse_cowgirl_position) and the thumbnail shows it —
        # what the panel needs to tell them apart and to pair scripts named
        # after scenes. Tags are alphabetical on e621; keep a generous slice.
        tags = post.get("tags") or {}
        general = [t for t in (tags.get("general") or []) if isinstance(t, str)][:120]
        thumbnail = ((post.get("preview") or {}).get("url")
                     or (post.get("sample") or {}).get("url") or "")
        return {
            "success": True,
            "provider": "e621",
            "title": filename.rsplit(".", 1)[0],
            "filename": filename,
            "size": best["size"],
            "formats": formats,
            "tags": general,
            "thumbnail": thumbnail,
            "duration": post.get("duration") or None,
        }
    except Exception as e:
        logger.error("e621 probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_socigames(url: str, session: aiohttp.ClientSession) -> dict:
    """SociGames: fetch the page past Cloudflare, then size whichever player
    layout it uses — a partner-CDN mp4 (HEAD it) or a Bunny Stream embed
    (hand the embed to the yt-dlp probe, which already reports formats)."""
    try:
        from funpairdl.providers.socigames import (
            fetch_page, height_from_url, parse_bunny_embed, parse_page_title,
            parse_video_sources,
        )

        html, page_url = await fetch_page(url)
        title = parse_page_title(html)
        candidates = parse_video_sources(html, page_url)

        if not candidates:
            embed = parse_bunny_embed(html)
            if not embed:
                return {"success": False, "error": "No video source found on page"}
            result = await _probe_ytdlp(embed, session)
            # Keep the yt-dlp formats but surface the page's own naming.
            if result.get("success") and title:
                result["provider"] = "socigames"
                result["title"] = title
                result["filename"] = f"{title}.mp4"
            return result

        headers = {"User-Agent": BROWSER_USER_AGENT, "Referer": page_url}

        async def _head_size(u: str) -> int:
            try:
                async with session.head(
                    u, headers=headers, allow_redirects=True,
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

        return {
            "success": True,
            "provider": "socigames",
            "title": title,
            "filename": (title + ".mp4") if title else "",
            "formats": formats,
        }
    except Exception as e:
        logger.error("SociGames probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}


async def _probe_direct(url: str, provider: str, session: aiohttp.ClientSession) -> dict:
    """Direct HTTP: HEAD for Content-Length. Send a browser User-Agent —
    some hosts (catbox.moe) close the connection on UA-less requests, and
    answer HEAD with Content-Length: 0, so fall back to a 1-byte ranged GET
    whose Content-Range reveals the true size."""
    headers = {"User-Agent": BROWSER_USER_AGENT}
    try:
        from urllib.parse import urlparse as _urlparse
        from funpairdl.utils.media_duration import (
            funscript_info, looks_like_video, probe_media_duration,
        )
        path = (_urlparse(url).path or "").lower()

        # A funscript is a few KB: read it whole. Its last action gives the
        # duration and its metadata may name the video outright — both feed
        # the pairing preview.
        if path.endswith(".funscript"):
            async with session.get(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                # resp.read(): the whole body. (StreamReader.read(n) returns
                # whatever happens to be buffered — a 3 KB slice of a 13 KB
                # script — which parses as nothing.)
                data = await resp.read()
            if len(data) > 8 * 1024 * 1024:
                data = b""
            info = funscript_info(data)
            return {
                "success": True,
                "provider": provider or "direct",
                "size": len(data),
                "duration": info["duration"],
                "script_title": info["title"],
                "video_url": info["video_url"],
            }

        size = 0
        try:
            async with session.head(
                url, headers=headers, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status < 400:
                    size = int(resp.headers.get("Content-Length", 0) or 0)
        except Exception as e:
            logger.debug("HEAD probe failed for %s (%s); trying ranged GET", url[:80], e)

        if size <= 0:
            async with session.get(
                url, headers={**headers, "Range": "bytes=0-0"},
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                resp.raise_for_status()
                content_range = resp.headers.get("Content-Range", "")
                if resp.status == 206 and "/" in content_range:
                    size = int(content_range.rsplit("/", 1)[-1])
                else:
                    size = int(resp.headers.get("Content-Length", 0) or 0)

        result = {
            "success": True,
            "provider": provider or "direct",
            "size": size,
        }
        if looks_like_video(path):
            duration = await probe_media_duration(url, session, headers, path)
            if duration:
                result["duration"] = duration
        return result
    except Exception as e:
        logger.error("Probe failed for %s: %s", url[:80], e)
        return {"success": False, "error": str(e)}

"""EroScripts (Discourse) topic metadata for the ``funlib.json`` sidecar:
tags, category, posted_at and the OP's username (``posted_by``).

Fetched with the embedded browser's saved cookies (the same session the
EroScripts provider downloads with); a rotated ``_t`` token in the reply is
written back through the provider's sync so the browser stays logged in.
Category names come from ``/site.json`` (cached per process).

Every request waits its turn behind one process-wide gate: at least
``MIN_INTERVAL_S`` after the previous one (``SEARCH_INTERVAL_S`` plus jitter
between searches, which Discourse never serves from cache), and a 429 waits
out the server's ``Retry-After`` and tries again instead of failing.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import re
import threading
import time
from pathlib import Path

import aiohttp

from funpairdl.constants import BROWSER_USER_AGENT

logger = logging.getLogger("funpairdl.discourse")

FORUM_BASE = "https://discuss.eroscripts.com"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

MIN_INTERVAL_S = 1.0
SEARCH_INTERVAL_S = 2.0
SEARCH_JITTER_S = 0.6
RETRY_429 = 3
RETRY_AFTER_DEFAULT_S = 30.0
RETRY_AFTER_MAX_S = 180.0

# Slot bookkeeping under a threading lock, sleeping outside it: callers run
# on different event loops (the queue's worker, tools' asyncio.run), so no
# asyncio primitive can be shared between them.
_gate_lock = threading.Lock()
_next_at = 0.0
_next_search_at = 0.0
_paused_until = 0.0


class RateLimited(RuntimeError):
    """The forum kept answering 429 after RETRY_429 waits."""


def retry_after_seconds(value, now: float | None = None) -> float:
    """Seconds a ``Retry-After`` header asks for (delta-seconds or an HTTP
    date), clamped to [1, RETRY_AFTER_MAX_S]; the default when absent."""
    raw = str(value or "").strip()
    secs = None
    if raw:
        try:
            secs = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                secs = when.timestamp() - (time.time() if now is None else now)
            except (TypeError, ValueError, IndexError, OverflowError):
                secs = None
    if secs is None:
        secs = RETRY_AFTER_DEFAULT_S if not raw else 1.0
    return max(1.0, min(float(secs), RETRY_AFTER_MAX_S))


def _reserve_slot(search: bool) -> float:
    """Seconds to wait before this request may go; books the next slot."""
    global _next_at, _next_search_at
    with _gate_lock:
        now = time.monotonic()
        start = max(now, _next_at, _paused_until)
        if search:
            start = max(start, _next_search_at)
            _next_search_at = start + SEARCH_INTERVAL_S + random.uniform(0, SEARCH_JITTER_S)
        _next_at = start + MIN_INTERVAL_S
        return start - now


def _pause(seconds: float) -> None:
    global _paused_until
    with _gate_lock:
        _paused_until = max(_paused_until, time.monotonic() + seconds)


_categories: dict[int, str] = {}
_categories_at = 0.0
_CATEGORIES_TTL = 6 * 3600


def normalize_tag(tag) -> str:
    """Tag as FunLib expects it: lowercase, words joined with '-'."""
    if isinstance(tag, dict):
        tag = tag.get("slug") or tag.get("name") or ""
    s = re.sub(r"[\s_]+", "-", str(tag or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def _headers() -> tuple[dict, str, object]:
    from funpairdl.persistence.settings import Settings
    settings = Settings.load()
    cookie = (settings.eroscripts_cookies or "").strip()
    h = {"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    return h, cookie, settings


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict | None:
    search = "/search.json" in url
    for attempt in range(RETRY_429 + 1):
        wait = _reserve_slot(search)
        if wait > 0:
            await asyncio.sleep(wait)
        headers, cookie, settings = _headers()
        async with session.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True) as resp:
            if cookie:
                try:
                    from funpairdl.providers.eroscripts import EroScriptsProvider
                    EroScriptsProvider._sync_rotated_cookies(resp, cookie, settings)
                except Exception as e:  # never let cookie bookkeeping fail a fetch
                    logger.debug("cookie sync skipped: %s", e)
            if resp.status == 429:
                secs = retry_after_seconds(resp.headers.get("Retry-After"))
                if attempt >= RETRY_429:
                    raise RateLimited(f"rate limited (429) after {RETRY_429} waits")
                logger.warning("Discourse rate limit (429) on %s — waiting %.0fs", url, secs)
                _pause(secs)
                continue
            if resp.status in (403, 404, 410):
                logger.info("Discourse %s -> %d", url, resp.status)
                return None
            resp.raise_for_status()
            return await resp.json(content_type=None)
    return None


async def categories(session: aiohttp.ClientSession, force: bool = False) -> dict[int, str]:
    global _categories, _categories_at
    if _categories and not force and time.monotonic() - _categories_at < _CATEGORIES_TTL:
        return _categories
    data = await _get_json(session, f"{FORUM_BASE}/site.json")
    cats: dict[int, str] = {}
    for c in (data or {}).get("categories") or []:
        try:
            cats[int(c["id"])] = str(c.get("name") or "")
        except (KeyError, TypeError, ValueError):
            continue
    if cats:
        _categories, _categories_at = cats, time.monotonic()
    return _categories


def topic_meta_from_json(topic: dict, cats: dict[int, str] | None = None) -> dict:
    """Sidecar fields from a ``/t/<id>.json`` document."""
    out: dict = {}
    tid = topic.get("id")
    if isinstance(tid, int):
        out["source"] = {"site": "eroscripts", "topic_id": tid,
                         "url": f"{FORUM_BASE}/t/{topic.get('slug') or 'topic'}/{tid}"}
    if topic.get("title"):
        out["title"] = str(topic["title"])
    tags = [normalize_tag(t) for t in (topic.get("tags") or [])]
    tags = [t for t in dict.fromkeys(tags) if t]
    if tags:
        out["tags"] = tags
    cid = topic.get("category_id")
    if isinstance(cid, int):
        cat = {"id": cid}
        name = (cats or {}).get(cid)
        if name:
            cat["name"] = name
        out["category"] = cat
    created = topic.get("created_at")
    if isinstance(created, str) and created:
        out["posted_at"] = created
    username = ""
    details = topic.get("details") or {}
    cb = details.get("created_by") or {}
    if isinstance(cb, dict) and cb.get("username"):
        username = str(cb["username"])
    if not username:
        posts = (topic.get("post_stream") or {}).get("posts") or []
        for p in posts:
            if isinstance(p, dict) and p.get("post_number") == 1 and p.get("username"):
                username = str(p["username"])
                break
    if username:
        # The OP (who posted / scripted). `author` stays the "(Author)" prefix.
        out["posted_by"] = username
        out["posted_by_url"] = f"{FORUM_BASE}/u/{username}"
    return out


async def fetch_topic_meta(topic_id: int, session: aiohttp.ClientSession | None = None) -> dict | None:
    """Sidecar fields for a topic, or None when it cannot be read."""
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        data = await _get_json(session, f"{FORUM_BASE}/t/{int(topic_id)}.json")
        if not data:
            return None
        cats = await categories(session)
        if isinstance(data.get("category_id"), int) and data["category_id"] not in cats:
            cats = await categories(session, force=True)
        return topic_meta_from_json(data, cats)
    finally:
        if own:
            await session.close()


async def search_topic(query: str, title_key, want_key: str,
                       session: aiohttp.ClientSession | None = None) -> int | None:
    """Topic id whose title has the same title key as ``want_key`` among the
    forum's search results for ``query``; None when nothing matches."""
    from urllib.parse import quote
    own = session is None
    session = session or aiohttp.ClientSession()
    try:
        data = await _get_json(session, f"{FORUM_BASE}/search.json?q={quote(query)}")
        for t in (data or {}).get("topics") or []:
            if not isinstance(t, dict):
                continue
            if title_key(str(t.get("title") or "")) == want_key:
                return int(t["id"])
        return None
    finally:
        if own:
            await session.close()


async def enrich_sidecar(work_dir: Path, topic_id: int,
                         session: aiohttp.ClientSession | None = None) -> bool:
    """Fill the forum fields (tags, category, posted_at, posted_by, title)
    of a work's sidecar from its topic. Fields already present are kept."""
    from funpairdl.core.library import update_sidecar
    meta = await fetch_topic_meta(topic_id, session)
    if not meta:
        return False
    # Read and write under the folder's lock: a reconcile writing between
    # a separate read and write here was overwritten with the older copy.
    update_sidecar(work_dir, meta, overwrite=False)
    logger.info("Sidecar enriched from topic %s: %s", topic_id, Path(work_dir).name)
    return True


def fetch_topic_meta_sync(topic_id: int) -> dict | None:
    return asyncio.run(fetch_topic_meta(topic_id))

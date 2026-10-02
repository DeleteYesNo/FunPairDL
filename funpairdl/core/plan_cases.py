"""Plan cases: the planners' real inputs, kept for regression replay.

Every post the panel plans (/video/plan, /bundle/plan) and every pair the
queue splits leaves its inputs here — the newest per post. Once a batch has
been checked, ``tools/replay_plans.py --accept`` stores what the current
code decides as each case's expected outcome; after a rule change,
``tools/replay_plans.py`` runs every case again and lists what moved, so a
fix for one post that breaks another shows before anything is sent.

The files hold real post titles and links: they stay on this machine
(``plan_cases/`` is gitignored). Recording is off until the app enables it,
so tests and tools never write here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("funpairdl.plan_cases")

KINDS = ("video", "bundle", "split")
MAX_CASES = 4000

_dir: Path | None = None
_lock = threading.Lock()
_writes = 0


def enable(path: Path) -> None:
    global _dir
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _dir = path


def disable() -> None:
    global _dir
    _dir = None


def _h(text: str, n: int) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def case_path(root: Path, kind: str, title: str, urls: list[str]) -> Path:
    return root / f"{kind}-{_h(title, 10)}-{_h(chr(10).join(sorted(set(urls))), 8)}.json"


def record(kind: str, title: str, urls: list[str], request: dict, source_url: str = "") -> None:
    """Keep `request` (the planner's input, JSON-able) as the newest case of
    this post. A case whose links are a subset of these is an earlier,
    thinner view of the same post (a pack not listed yet) and goes; a case
    with the very same input keeps its expected outcome."""
    root = _dir
    if root is None or kind not in KINDS:
        return
    try:
        _record(root, kind, title or "", list(urls), request, source_url)
    except Exception as e:  # never let bookkeeping break a plan
        logger.debug("plan case not kept (%s): %s", kind, e)


def _record(root: Path, kind: str, title: str, urls: list[str], request: dict, source_url: str) -> None:
    global _writes
    path = case_path(root, kind, title, urls)
    url_set = set(urls)
    with _lock:
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if old.get("request") == request:
                return
        for sib in root.glob(f"{kind}-{_h(title, 10)}-*.json"):
            if sib == path:
                continue
            try:
                other = json.loads(sib.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if other.get("title") == title and set(other.get("urls") or []) <= url_set:
                sib.unlink(missing_ok=True)
        case = {
            "kind": kind, "title": title, "source_url": source_url,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "urls": sorted(url_set), "request": request, "expected": None,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        _writes += 1
        if _writes % 50 == 0:
            _prune(root)


def _prune(root: Path) -> None:
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files[:max(0, len(files) - MAX_CASES)]:
        p.unlink(missing_ok=True)


def load_all(root: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(root.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError) as e:
            logger.warning("unreadable plan case %s: %s", p.name, e)
    return out


def save(path: Path, case: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ── Replay: the same planners on the kept inputs ──

def run_case(case: dict) -> object:
    """What the current code decides for a case, as plain comparable data."""
    kind, req = case["kind"], case["request"]
    if kind == "video":
        from funpairdl.core.video_plan import Prefs, VideoSpec, plan_videos
        videos = [VideoSpec(**v) for v in req["videos"]]
        res = plan_videos(videos, Prefs(**req["prefs"]), dict(req.get("decisions") or {}),
                          list(req.get("credits") or []), req.get("title") or "",
                          [dict(s) for s in (req.get("scripts") or [])])
        return {
            "roles": dict(sorted(res["roles"].items())),
            "groups": [{"kind": g.get("kind", ""), "tag": g.get("tag", ""), "chosen": g.get("chosen", ""),
                        "alternates": sorted(g.get("alternates") or [])} for g in res["groups"]],
            "ambiguous": sorted(a["url"] for a in res.get("ambiguous") or []),
        }
    from funpairdl.core.pair import FileType, PairItem
    from funpairdl.core.queue_manager import QueueManager
    qm = QueueManager.__new__(QueueManager)  # the planner only; no queue, no settings
    items = []
    for it in req["items"]:
        pi = PairItem(url=it["url"], filename=it["filename"], file_type=FileType(it["file_type"]),
                      group=it.get("group") or "Main")
        pi.duration = float(it.get("duration") or 0)
        pi.resolved_url = it.get("resolved_url") or ""
        items.append(pi)
    groups = qm.plan_bundle_split(
        items, req.get("plan"), req.get("name") or "", req.get("alt_group_config"),
        hints=req.get("hints"), durations=req.get("durations"), links=req.get("links"))
    if not groups:
        return None
    return [{"name": g["name"], "label": g.get("label", ""),
             "videos": [v.url for v in g["videos"]],
             "scripts": {s.url: g.get("script_basis", {}).get(s.url, "") for s in g["scripts"]},
             "others": [o.url for o in g.get("others") or []]} for g in groups]


def split_request(items, plan, name, alt_group_config, hints, durations, links) -> dict:
    """plan_bundle_split's inputs as JSON."""
    return {
        "name": name or "",
        "items": [{"url": i.url, "filename": i.filename, "file_type": i.file_type.value,
                   "group": i.group or "Main", "duration": float(i.duration or 0),
                   "resolved_url": i.resolved_url or ""} for i in items],
        "plan": dict(plan or {}) or None,
        "alt_group_config": {k: dict(v) for k, v in (alt_group_config or {}).items()} or None,
        "hints": dict(hints or {}) or None,
        "durations": {k: float(v) for k, v in (durations or {}).items() if v} or None,
        "links": dict(links or {}) or None,
    }

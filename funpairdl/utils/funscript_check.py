"""Is a downloaded ``.funscript`` a usable script?

Checked right after the download, before the pair is organized:

* fatal — the file is no script at all: empty, an HTML page (a login or
  error page saved under the script's name), not JSON, or no actions with
  a usable time and position. The item fails and is downloaded again.
* fixable — a script with some broken points: a time that is negative or
  past a day (one such point made FunLib show a negative length), a
  position outside 0–100, or actions out of order. The broken points are
  dropped, positions clamped and the actions sorted (``fixed``).
* notes — worth a log line only: the last action lands well past the end
  of the video the script came with.

Bekscript's Syncscript (binary, ``01 00 00 00 02 00 00 00``) is a script
too and passes untouched. Multi-axis "combined" files keep their actions
under ``axes[].actions``; every list is checked.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

SYNCSCRIPT_MAGIC = b"\x01\x00\x00\x00\x02\x00\x00\x00"
MAX_AT_MS = 24 * 3600 * 1000
# A script may run past its video by this much before it is worth a note.
LONGER_THAN_VIDEO_S = 60.0


@dataclass
class ScriptCheck:
    fatal: str | None = None
    fixed: bytes | None = None          # the repaired file, when anything changed
    notes: list[str] = field(default_factory=list)
    last_at_ms: int = 0


def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _repair_actions(actions: list) -> tuple[list, dict]:
    """(kept actions, counts of what changed)."""
    counts = {"dropped": 0, "clamped": 0, "sorted": 0}
    kept = []
    for a in actions:
        if not isinstance(a, dict):
            counts["dropped"] += 1
            continue
        at, pos = _number(a.get("at")), _number(a.get("pos"))
        if at is None or pos is None or at < 0 or at > MAX_AT_MS:
            counts["dropped"] += 1
            continue
        if pos < 0 or pos > 100:
            a = dict(a)
            a["pos"] = 0 if pos < 0 else 100
            counts["clamped"] += 1
        kept.append(a)
    ats = [_number(a.get("at")) for a in kept]
    if any(ats[i] > ats[i + 1] for i in range(len(ats) - 1)):
        kept.sort(key=lambda a: _number(a.get("at")))
        counts["sorted"] = 1
    return kept, counts


def check_funscript(data: bytes, video_seconds: float | None = None) -> ScriptCheck:
    if not data or not data.strip():
        return ScriptCheck(fatal="empty file")
    if data.startswith(SYNCSCRIPT_MAGIC):
        return ScriptCheck()
    head = data[:512].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml")):
        return ScriptCheck(fatal="an HTML page, not a script")
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return ScriptCheck(fatal="not JSON")
    if not isinstance(doc, dict):
        return ScriptCheck(fatal="not a funscript object")

    lists: list[tuple[dict, str]] = []          # (owner, key) of each action list
    if isinstance(doc.get("actions"), list):
        lists.append((doc, "actions"))
    for ax in doc.get("axes") or []:
        if isinstance(ax, dict) and isinstance(ax.get("actions"), list):
            lists.append((ax, "actions"))
    if not any(owner[key] for owner, key in lists):
        return ScriptCheck(fatal="no actions")

    total = {"dropped": 0, "clamped": 0, "sorted": 0}
    kept_any = 0
    last_at = 0.0
    for owner, key in lists:
        kept, counts = _repair_actions(owner[key])
        for k, v in counts.items():
            total[k] += v
        if counts["dropped"] or counts["clamped"] or counts["sorted"]:
            owner[key] = kept
        kept_any += len(kept)
        for a in kept:
            last_at = max(last_at, _number(a.get("at")) or 0.0)
    if kept_any == 0:
        return ScriptCheck(fatal="no usable actions")

    out = ScriptCheck(last_at_ms=int(last_at))
    if total["dropped"]:
        out.notes.append(f"{total['dropped']} broken point(s) dropped")
    if total["clamped"]:
        out.notes.append(f"{total['clamped']} position(s) clamped to 0-100")
    if total["sorted"]:
        out.notes.append("actions sorted by time")
    if out.notes:
        out.fixed = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if video_seconds and video_seconds > 0 and last_at / 1000.0 > video_seconds + LONGER_THAN_VIDEO_S:
        out.notes.append(f"runs to {last_at / 1000.0:.0f}s, the video is {video_seconds:.0f}s")
    return out

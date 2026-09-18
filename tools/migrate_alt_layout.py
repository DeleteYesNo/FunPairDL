"""Migrate legacy ``.alt`` / ``.altN`` variant subfolders to the flat
library layout (docs/library-layout.md).

For every work folder under the library roots:
  * ``<work>/<stem>.altN/`` whose video is a hardlink of the work's own
    video (or that has no video): the hardlinked video is deleted, every
    script that is a hardlink of a top-level script (inherited axis) is
    deleted, the remaining scripts move up as ``<work> (<Label>)[.axis]
    .funscript`` (Label = "Alt", "Alt 1", … or the folder's display stem),
    the empty subfolder is removed and ``funlib.json`` gets a fresh
    ``variants[]``.
  * a subfolder whose video is NOT a hardlink: byte-identical to a
    top-level video → a copy, deleted; different → the variant's own
    video, moved up as ``<work> (<Label>).<ext>`` (FunLib switches video
    and thumbnail with the variant).
  * anything unexpected (other file types, name clashes) skips that
    subfolder and is reported.

DRY-RUN by default: writes a Markdown report; ``--apply`` performs it.
Folders a queued/active pair is still writing are skipped. ``_trash/``
is never visited.

    python tools/migrate_alt_layout.py                       # roots from config
    python tools/migrate_alt_layout.py --roots F:\\Lib H:\\Lib --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.core import library as lib  # noqa: E402
from funpairdl.core.queue_manager import QueueManager  # noqa: E402

STAMP = time.strftime("%Y%m%d-%H%M%S")


def _samefile(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_content(a: Path, b: Path) -> bool:
    try:
        if _samefile(a, b):
            return True
        if a.stat().st_size != b.stat().st_size:
            return False
        return _sha256(a) == _sha256(b)
    except OSError:
        return False


def busy_folders() -> set[Path]:
    """Output folders of pairs the running app may still be writing."""
    from funpairdl.constants import QUEUE_FILE
    out: set[Path] = set()
    try:
        for d in json.loads(Path(QUEUE_FILE).read_text(encoding="utf-8")):
            if d.get("state") != "completed" and d.get("output_dir"):
                try:
                    out.add(Path(d["output_dir"]).resolve())
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    return out


def plan_work(work: Path) -> list[dict]:
    """One plan entry per ``.alt*`` subfolder of ``work``."""
    base = work.name
    try:
        entries = sorted(work.iterdir())
    except OSError as e:
        return [{"work": work, "sub": None, "status": "error", "why": str(e)}]
    top_videos = [f for f in entries if f.is_file() and f.suffix.lower() in lib.VIDEO_EXTS]
    top_scripts = {f.name.lower(): f for f in entries if f.is_file() and f.name.lower().endswith(".funscript")}
    used = lib.existing_labels(work, base, include_alt_dirs=False)   # flat labels only
    plans: list[dict] = []
    for sub in entries:
        if not sub.is_dir() or not lib._ALT_DIR_RE.match(sub.name):
            continue
        p: dict = {"work": work, "sub": sub, "status": "migrate", "why": "",
                   "delete": [], "moves": [], "label": ""}
        try:
            files = sorted(f for f in sub.iterdir())
        except OSError as e:
            p.update(status="skip", why=f"unreadable: {e}")
            plans.append(p)
            continue
        if any(f.is_dir() for f in files):
            p.update(status="skip", why="nested folder inside")
            plans.append(p)
            continue
        videos = [f for f in files if f.suffix.lower() in lib.VIDEO_EXTS]
        scripts = [f for f in files if f.name.lower().endswith(".funscript")]
        # a .linkinfo / funlib.json inside the subfolder is bookkeeping, not media
        junk = [f for f in files if f.name in (lib.SIDECAR_NAME, ".linkinfo")]
        others = [f for f in files if f not in videos and f not in scripts and f not in junk]
        if others:
            p.update(status="skip", why="other files: " + ", ".join(o.name for o in others[:3]))
            plans.append(p)
            continue
        label = lib.unique_label(lib.alt_dir_label(sub.name, base), used)
        p["label"] = label
        conflict = ""
        own_video: Path | None = None
        for v in videos:
            if any(_same_content(v, tv) for tv in top_videos):
                p["delete"].append(v)             # hardlink or byte-identical copy
            elif own_video is None:
                own_video = v
            else:
                conflict = f"two own videos: {own_video.name}, {v.name}"
        if own_video is not None and not conflict:
            target = work / f"{base} ({label}){own_video.suffix}"
            if target.exists():
                conflict = f"target exists: {target.name}"
            else:
                p["moves"].append((own_video, target))
                p["own_video"] = True
        for s in scripts:
            _canonical, suffix = QueueManager._parse_axis(s.name)
            main_name = lib.script_name(base, "", suffix).lower()
            main = top_scripts.get(main_name)
            if main is not None and _samefile(s, main):
                p["delete"].append(s)          # inherited axis (hardlink) — FunLib inherits at play time
                continue
            target = work / lib.script_name(base, label, suffix)
            if target.exists() or any(m[1] == target for m in p["moves"]):
                conflict = f"target exists: {target.name}"
                break
            p["moves"].append((s, target))
        if conflict:
            p.update(status="skip", why=conflict)
            plans.append(p)
            continue
        if not p["moves"] and not p["delete"]:
            p.update(status="skip", why="empty subfolder")
            plans.append(p)
            continue
        p["delete"].extend(junk)
        used.add(label)
        plans.append(p)
    return plans


def apply_plan(p: dict) -> str:
    sub: Path = p["sub"]
    for src, dst in p["moves"]:
        src.rename(dst)
    for f in p["delete"]:
        f.unlink(missing_ok=True)
    try:
        sub.rmdir()
    except OSError as e:
        return f"moved but folder not removed: {e}"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="*", help="library roots (default: download_dir + library_paths)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default=str(ROOT / f"_migrate_alt_{STAMP}.md"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    roots = [Path(r) for r in args.roots] if args.roots else lib.library_roots()
    busy = busy_folders()
    lines = [f"# .alt → flat layout migration {STAMP} ({'APPLY' if args.apply else 'DRY RUN'})",
             f"roots: {', '.join(str(r) for r in roots)}", ""]
    counts = {"migrate": 0, "keep": 0, "skip": 0, "busy": 0, "error": 0,
              "scripts_moved": 0, "files_deleted": 0, "works_touched": 0}
    for root in roots:
        for work in lib.iter_work_dirs(root):
            plans = plan_work(work)
            if not plans:
                continue
            try:
                if work.resolve() in busy:
                    counts["busy"] += 1
                    lines.append(f"- BUSY (pair still active): `{work.name}`")
                    continue
            except OSError:
                pass
            touched = False
            for p in plans:
                rel = f"{work.name}/{p['sub'].name}" if p.get("sub") else work.name
                if p["status"] == "migrate":
                    n_mv, n_del = len(p["moves"]), len(p["delete"])
                    detail = (f"({p['label']}) {n_mv} file(s) up, {n_del} hardlink/copy(s) removed"
                              + (" [own video]" if p.get("own_video") else ""))
                    err = apply_plan(p) if args.apply else ""
                    if err:
                        counts["error"] += 1
                        lines.append(f"- ERROR `{rel}`: {detail} — {err}")
                    else:
                        counts["migrate"] += 1
                        counts["scripts_moved"] += n_mv
                        counts["files_deleted"] += n_del
                        touched = True
                        lines.append(f"- MIGRATE `{rel}` → {detail}")
                else:
                    counts[p["status"] if p["status"] in counts else "skip"] += 1
                    lines.append(f"- {p['status'].upper()} `{rel}`: {p['why']}")
            if touched and args.apply:
                counts["works_touched"] += 1
                still_alt = any(d.is_dir() and lib._ALT_DIR_RE.match(d.name) for d in work.iterdir())
                if not still_alt:
                    (work / ".linkinfo").unlink(missing_ok=True)
                try:
                    lib.update_sidecar(work, {"variants": lib.scan_variants(work, work.name)})
                except OSError as e:
                    lines.append(f"- ERROR sidecar `{work.name}`: {e}")
    lines.append("")
    lines.append(f"**Summary**: {counts}")
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-3:]))
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()

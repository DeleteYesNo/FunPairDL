"""One-off repair for bundles that were split before the matching fix.

Older auto-split required a script's name to start with the video's name, so
scripts that carried a "(CHARACTER)" prefix or a different resolution tag than
their video were treated as unmatched, dumped onto the first split pair, and
promoted to `.alt/.alt1/...` subfolders (with the wrong video hard-linked in).
Their real videos ended up as script-less folders.

This script reads queue.json, finds those misplaced Alt-group funscripts,
re-matches each to its real video folder (same logic as the fixed code), and
moves the funscript into that folder renamed to the video's base name.

DEFAULT IS DRY-RUN — prints the plan and changes nothing. Pass --apply to act.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue.json"
AXES = {"twist", "surge", "sway", "roll", "pitch", "vibe", "vibration", "vib",
        "pump", "stroke", "suck", "valve", "lube",
        "l0", "l1", "l2", "l3", "r0", "r1", "r2", "v0", "v1", "v2", "a0", "a1", "a2"}


def key(name: str, strip_prefix: bool = False) -> str:
    s = name.lower()
    if strip_prefix:
        s = re.sub(r"^(\s*[\(\[（][^\)\]）]*[\)\]）]\s*)+", "", s)
    s = re.sub(
        r"(?<![a-z0-9])(?:\d{3,4}p|[248]k|\d{1,3}fps|no[-_ ]?wm|wm)(?![a-z0-9])",
        " ", s,
    )
    return re.sub(r"[^a-z0-9]+", "", s)


def strip_axis(name: str) -> str:
    base = name[:-len(".funscript")] if name.lower().endswith(".funscript") else name
    parts = base.rsplit(".", 1)
    if len(parts) == 2 and parts[1].lower() in AXES:
        base = parts[0]
    return base


def alt_slot(group: str) -> str | None:
    m = re.match(r"\s*Alt\s+(\d+)\s*$", group, re.IGNORECASE)
    if not m:
        return None
    idx = int(m.group(1)) - 1          # Alt 1 → slot 0 → ".alt"
    return "alt" if idx == 0 else f"alt{idx}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually move files")
    args = ap.parse_args()

    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    pairs = data if isinstance(data, list) else data.get("pairs", [])

    # Find "hub" pairs that absorbed FOREIGN scripts: an Alt-group funscript
    # whose real name doesn't match the pair's own video. Legitimate alts (same
    # work, different scripter) are left alone. Scope the repair to the hour
    # windows those hubs were created in, so we never match against unrelated
    # past downloads.
    hub_hours: set[str] = set()
    for p in pairs:
        base_k = key(Path(p.get("output_dir", "x")).name)
        orig = p.get("original_filenames") or {}
        for it in p.get("items", []):
            if it.get("file_type") != "funscript":
                continue
            if not (it.get("group", "Main") or "Main").lower().startswith("alt"):
                continue
            rk = key(strip_axis(orig.get(it.get("id", ""), it.get("filename", ""))))
            if rk and base_k and not (base_k in rk or rk in base_k):
                hub_hours.add(p.get("created_at", "")[:13])   # this alt is foreign
    if not hub_hours:
        print("No misplaced (foreign) Alt funscripts found — nothing to repair.")
        return
    batch = [p for p in pairs if p.get("created_at", "")[:13] in hub_hours]
    print(f"batch windows {sorted(hub_hours)}: {len(batch)} pairs (of {len(pairs)} total)\n")

    # Index every batch work's video folder by match key. Skip degenerate keys
    # (e.g. non-latin names that collapse to one digit) — they'd match anything.
    folder_exact: dict[str, Path] = {}
    folders: list[tuple[str, Path]] = []
    for p in batch:
        od = p.get("output_dir")
        if not od:
            continue
        d = Path(od)
        if not d.is_dir():
            continue
        if not any(it.get("file_type") == "video" for it in p.get("items", [])):
            continue
        k = key(d.name)
        if len(k) < 6:
            continue
        folder_exact.setdefault(k, d)
        folders.append((k, d))
    folders.sort(key=lambda x: len(x[0]), reverse=True)

    def match_folder(real_name: str) -> Path | None:
        sk = key(strip_axis(real_name))
        if len(sk) < 4:
            return None
        if sk in folder_exact:
            return folder_exact[sk]
        for fk, d in folders:                 # longest folder key first
            if fk and (fk in sk or sk in fk):
                return d
        # retry with the script's leading "(PREFIX)" removed
        sk2 = key(strip_axis(real_name), strip_prefix=True)
        if sk2 != sk and sk2 in folder_exact:
            return folder_exact[sk2]
        return None

    plan, skipped = [], []
    for p in batch:
        od = p.get("output_dir")
        if not od:
            continue
        base = Path(od).name
        orig = p.get("original_filenames") or {}
        own_key = key(base)
        for it in p.get("items", []):
            if it.get("file_type") != "funscript":
                continue
            grp = it.get("group", "Main") or "Main"
            slot = alt_slot(grp)
            if slot is None:
                continue                       # Main script — leave it
            real = orig.get(it.get("id", ""), it.get("filename", ""))
            # Only touch FOREIGN scripts (real name doesn't match this folder).
            rk = key(strip_axis(real))
            if rk and (own_key in rk or rk in own_key):
                continue                       # genuinely an alt of this work
            cur = Path(od) / f"{base}.{slot}" / f"{base}.{slot}.funscript"
            dest_dir = match_folder(real)
            row = (real, cur, dest_dir)
            if dest_dir is None or not cur.exists():
                skipped.append(row)
            else:
                plan.append(row)

    print(f"=== REPAIR PLAN ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"folders indexed: {len(folders)} | moves: {len(plan)} | skipped: {len(skipped)}\n")
    for real, cur, dest in plan:
        dest_name = f"{dest.name}.funscript"
        clash = (dest / dest_name).exists()
        print(f"  {real[:48]:50}")
        print(f"      from: {cur.parent.name}/{cur.name}")
        print(f"      ->    {dest.name}/{dest_name}{'   [DEST ALREADY HAS SCRIPT — will suffix]' if clash else ''}")
    if skipped:
        print("\n--- SKIPPED (no match or file missing) ---")
        for real, cur, dest in skipped:
            why = "no folder match" if dest is None else "file missing on disk"
            print(f"  {real[:55]:57} [{why}]")

    if not args.apply:
        print("\n(DRY-RUN — nothing changed. Re-run with --apply to perform the moves.)")
        return

    moved = 0
    for real, cur, dest in plan:
        dest_name = f"{dest.name}.funscript"
        target = dest / dest_name
        if target.exists():
            # keep the existing script, drop the foreign one beside it
            stem = key(strip_axis(real), strip_prefix=True)[:24] or "alt"
            target = dest / f"{dest.name}.{stem}.funscript"
        cur.rename(target)
        moved += 1
        # remove the now-empty .alt subfolder (and a hard-linked wrong video)
        sub = cur.parent
        for leftover in list(sub.glob("*")):
            if leftover.suffix.lower() != ".funscript":
                leftover.unlink()           # hard-linked wrong video copy
        if not any(sub.iterdir()):
            sub.rmdir()
    print(f"\nApplied: moved {moved} funscript(s) to their correct folders.")


if __name__ == "__main__":
    main()

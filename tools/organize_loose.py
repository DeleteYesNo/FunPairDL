"""Fold loose top-level files in a library into per-work folders.

Two safe operations (DRY-RUN by default; --apply to act):
  1. A loose video + its loose funscript(s) -> a new folder named after the work.
  2. A loose funscript whose work already has a folder -> move it into that folder.

Orphans (loose video with no script, loose script with no video anywhere) are
reported but never touched.
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

LIB = r"G:\Download\nakk7472"
VID = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".flv"}
AX = re.compile(r"\.(twist|surge|sway|roll|pitch|vibe|vibration|vib|pump|stroke|"
                r"suck|valve|lube|L0|L1|L2|L3|R0|R1|R2|V0|V1|V2|A0|A1|A2)$", re.I)


def stem_of(fn: str) -> str:
    base, ext = os.path.splitext(fn)
    if ext.lower() == ".funscript":
        base = AX.sub("", base)
    return base


def nkey(s: str) -> str:
    s = s.lower()
    s = re.sub(r"(?<![a-z0-9])(?:\d{3,4}p|[248]k|\d{1,3}fps|no[-_ ]?wm|wm)(?![a-z0-9])", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    files = [f for f in os.listdir(LIB) if os.path.isfile(os.path.join(LIB, f))]
    dirs = [d for d in os.listdir(LIB) if os.path.isdir(os.path.join(LIB, d))]

    from collections import defaultdict
    groups = defaultdict(lambda: {"v": [], "s": []})
    for f in files:
        e = os.path.splitext(f)[1].lower()
        if e in VID:
            groups[stem_of(f)]["v"].append(f)
        elif e == ".funscript":
            groups[stem_of(f)]["s"].append(f)

    folder_by_key = {}
    for d in dirs:
        folder_by_key.setdefault(nkey(d), d)

    fold_plan, move_plan = [], []
    for stem, g in groups.items():
        if g["v"] and g["s"]:
            fold_plan.append((stem, g["v"] + g["s"]))
        elif g["s"] and not g["v"]:
            k = nkey(stem)
            if k in folder_by_key:
                move_plan.append((folder_by_key[k], g["s"]))

    def move_into(dest_dir: str, fnames: list[str]) -> int:
        # Windows silently strips trailing spaces/dots from created dir names,
        # so strip them here too or the rename target won't match.
        dest_dir = dest_dir.rstrip(" .") or "_untitled"
        moved = 0
        os.makedirs(os.path.join(LIB, dest_dir), exist_ok=True)
        for fn in fnames:
            src = os.path.join(LIB, fn)
            dst = os.path.join(LIB, dest_dir, fn)
            if os.path.exists(dst):
                print(f"    SKIP (exists): {dest_dir}/{fn[:40]}")
                continue
            if args.apply:
                os.rename(src, dst)
            moved += 1
        return moved

    print(f"=== {'APPLY' if args.apply else 'DRY-RUN'} ===")
    print(f"1) new folders from video+script: {len(fold_plan)}")
    print(f"2) loose scripts into existing folders: {len(move_plan)}\n")

    total_moved = 0
    for stem, fnames in fold_plan:
        nv = sum(1 for f in fnames if os.path.splitext(f)[1].lower() in VID)
        ns = len(fnames) - nv
        print(f"  [+folder] {stem[:50]}  ({nv}V {ns}S)")
        try:
            total_moved += move_into(stem, fnames)
        except Exception as e:
            print(f"    ERROR: {e}")
    for dest, fnames in move_plan:
        print(f"  [->exist] {dest[:45]}  (+{len(fnames)} script)")
        try:
            total_moved += move_into(dest, fnames)
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n{'moved' if args.apply else 'would move'} {total_moved} files into folders.")
    if not args.apply:
        print("(DRY-RUN — nothing changed. Re-run with --apply.)")


if __name__ == "__main__":
    main()

"""(A) delete loose top-level videos that have no funscript, and
(B) move loose funscripts that have no video into  No Video/<work>/  subfolders.

DRY-RUN by default; --apply to act. The 'No Video' parent groups all the
video-less scripts, one dedicated subfolder per work.
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

LIB = r"G:\Download\nakk7472"
NOVIDEO = "No Video"
VID = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".flv"}
AX = re.compile(r"\.(twist|surge|sway|roll|pitch|vibe|vibration|vib|pump|stroke|"
                r"suck|valve|lube|L0|L1|L2|L3|R0|R1|R2|V0|V1|V2|A0|A1|A2)$", re.I)


def stem_of(fn: str) -> str:
    base, ext = os.path.splitext(fn)
    if ext.lower() == ".funscript":
        base = AX.sub("", base)
    return base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from collections import defaultdict
    g = defaultdict(lambda: {"v": [], "s": []})
    for f in os.listdir(LIB):
        if not os.path.isfile(os.path.join(LIB, f)):
            continue
        e = os.path.splitext(f)[1].lower()
        if e in VID:
            g[stem_of(f)]["v"].append(f)
        elif e == ".funscript":
            g[stem_of(f)]["s"].append(f)

    video_only = [(st, gg["v"]) for st, gg in g.items() if gg["v"] and not gg["s"]]
    script_only = [(st, gg["s"]) for st, gg in g.items() if gg["s"] and not gg["v"]]

    print(f"=== {'APPLY' if args.apply else 'DRY-RUN'} ===")
    print(f"(A) delete video-only files: {sum(len(v) for _, v in video_only)}")
    print(f"(B) script-only works -> 'No Video' subfolders: {len(script_only)} "
          f"({sum(len(s) for _, s in script_only)} files)\n")

    # (A) delete loose video-only files
    for st, vids in video_only:
        for v in vids:
            print(f"  [DELETE] {v[:55]}")
            if args.apply:
                try:
                    os.remove(os.path.join(LIB, v))
                except Exception as e:
                    print(f"    ERROR: {e}")

    # (B) No Video/<work>/
    moved = 0
    for st, scripts in script_only:
        sub = st.rstrip(" .") or "_untitled"
        dest = os.path.join(LIB, NOVIDEO, sub)
        if args.apply:
            os.makedirs(dest, exist_ok=True)
        for fn in scripts:
            target = os.path.join(dest, fn)
            if args.apply and os.path.exists(target):
                continue
            if args.apply:
                try:
                    os.rename(os.path.join(LIB, fn), target)
                    moved += 1
                except Exception as e:
                    print(f"    ERROR {fn[:40]}: {e}")
            else:
                moved += 1
    print(f"\n{'moved' if args.apply else 'would move'} {moved} scripts into 'No Video/'.")
    if not args.apply:
        print("(DRY-RUN — nothing changed.)")


if __name__ == "__main__":
    main()

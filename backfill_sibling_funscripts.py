"""One-shot maintenance tool: hardlink the primary funscript of every
already-completed Pair next to extra videos that lack a same-stem
funscript. Mirrors the new Phase 5 logic in _organize_output without
touching anything else.

Run:  python backfill_sibling_funscripts.py [--dry-run]

The tool reads the queue store directly so it works whether or not the
GUI is running. It is safe to re-run; it skips files that already exist.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 stdout so non-cp950 filenames (Japanese, etc.) don't crash print()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from funpairdl.core.pair import FileType, Pair, PairState
from funpairdl.persistence.queue_store import QueueStore


def _strip_funscript_axis(name: str) -> str:
    """Best-effort strip of '.funscript' and any axis suffix so we can
    detect 'is there ANY funscript that already pairs with this video's
    stem?'. Mirrors how players match script ↔ video by base name."""
    n = name
    if n.lower().endswith(".funscript"):
        n = n[: -len(".funscript")]
    # Drop one trailing axis if present (.roll / .pitch / etc.)
    parts = n.rsplit(".", 1)
    if len(parts) == 2 and parts[1].lower() in {
        "roll", "pitch", "yaw", "twist", "surge", "sway", "stroke",
    }:
        n = parts[0]
    return n


def find_primary_script(folder: Path) -> Path | None:
    """Return the .funscript whose stem matches the folder's base name
    (the one Phase 3 of organize would have produced). If we can't find
    that exact match, fall back to any top-level .funscript in the
    folder so the tool still helps slightly-non-standard layouts."""
    base = folder.name  # base_name was sanitize_filename(pair.name)
    candidate = folder / f"{base}.funscript"
    if candidate.exists():
        return candidate
    # Fallback: any single root-level funscript that isn't an axis-only file
    scripts = sorted(p for p in folder.glob("*.funscript") if p.is_file())
    if not scripts:
        return None
    # Prefer one without dotted axis suffix
    for s in scripts:
        stem = _strip_funscript_axis(s.name)
        if stem == base:
            return s
    return scripts[0]


def backfill_pair(pair: Pair, dry_run: bool) -> tuple[int, int]:
    """Returns (linked, skipped). Errors are printed but never raise."""
    if not pair.output_dir:
        return 0, 0
    folder = Path(pair.output_dir)
    if not folder.exists():
        return 0, 0
    primary = find_primary_script(folder)
    if primary is None:
        return 0, 0

    linked = 0
    skipped = 0
    for item in pair.items:
        if item.file_type != FileType.VIDEO:
            continue
        vid = folder / item.filename
        if not vid.exists():
            continue
        target = vid.with_suffix(".funscript")
        if target.exists():
            skipped += 1
            continue
        # Don't link to itself: if vid stem already equals primary stem,
        # we'd be writing target == primary (which already exists, so
        # the previous check catches it — but be defensive).
        if target.resolve() == primary.resolve():
            skipped += 1
            continue
        if dry_run:
            print(f"  [DRY] would hardlink: {primary.name} -> {target.name}")
            linked += 1
            continue
        try:
            os.link(str(primary), str(target))
            print(f"  + hardlinked: {target.name}")
            linked += 1
        except OSError as e:
            print(f"  ! failed: {target.name}: {e}")

    return linked, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen without creating links.")
    args = ap.parse_args()

    store = QueueStore()
    pairs = store.load()
    print(f"Loaded {len(pairs)} pair(s) from queue store.")

    completed = [p for p in pairs if p.state == PairState.COMPLETED]
    print(f"Scanning {len(completed)} completed pair(s)...")
    if args.dry_run:
        print("(dry-run mode — no files will be created)")

    total_linked = 0
    total_skipped = 0
    pairs_changed = 0
    for pair in completed:
        linked, skipped = backfill_pair(pair, args.dry_run)
        if linked:
            print(f"[{pair.name}]")
            pairs_changed += 1
        total_linked += linked
        total_skipped += skipped

    print()
    print(f"Done. {pairs_changed} pair(s) updated, "
          f"{total_linked} hardlink(s) created, "
          f"{total_skipped} already-correct video(s) skipped.")


if __name__ == "__main__":
    main()

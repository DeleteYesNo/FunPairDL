"""One-time queue.json migration for the 2026-07 performance fix.

1. Backs up queue.json to queue.json.bak-perf-migration-<ts>.
2. Strips dead `segments` from all COMPLETED items.
3. Moves COMPLETED pairs beyond the newest COMPLETED_KEEP_LIVE into
   queue_archive.jsonl (one pair dict per line, segments stripped).
4. Writes the pruned queue.json back (atomic).
5. Scans completed items for corruption signals (file exists but is
   shorter than total_bytes) and writes _queue_integrity_report.txt.
   Read-only scan — nothing on disk is touched.

Run with the app STOPPED (its auto-save would overwrite the result).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.constants import COMPLETED_KEEP_LIVE, QUEUE_ARCHIVE_FILE, QUEUE_FILE  # noqa: E402


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    raw = QUEUE_FILE.read_text(encoding="utf-8")
    data = json.loads(raw)
    orig_bytes = len(raw.encode("utf-8"))

    backup = QUEUE_FILE.with_name(f"queue.json.bak-perf-migration-{ts}")
    backup.write_text(raw, encoding="utf-8")
    print(f"backup: {backup.name} ({orig_bytes:,} bytes, {len(data)} pairs)")

    # --- strip segments from completed items ---
    stripped = 0
    for pair in data:
        for item in pair.get("items", []):
            if item.get("state") == "completed" and item.get("segments"):
                stripped += len(item["segments"])
                item["segments"] = []

    # --- split: archive completed pairs beyond the newest KEEP ---
    completed_idx = [i for i, p in enumerate(data) if p.get("state") == "completed"]
    keep_idx = set(completed_idx[-COMPLETED_KEEP_LIVE:])  # list is oldest-first
    to_archive = [p for i, p in enumerate(data) if p.get("state") == "completed" and i not in keep_idx]
    live = [p for i, p in enumerate(data) if p.get("state") != "completed" or i in keep_idx]

    with open(QUEUE_ARCHIVE_FILE, "a", encoding="utf-8") as f:
        for p in to_archive:
            for item in p.get("items", []):
                item["segments"] = []
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # --- atomic write of pruned live queue ---
    tmp = QUEUE_FILE.with_name(f"queue.json.tmp-{os.getpid()}")
    t0 = time.perf_counter()
    tmp.write_text(json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE_FILE)
    dt = time.perf_counter() - t0
    new_bytes = QUEUE_FILE.stat().st_size

    print(f"segments stripped: {stripped:,}")
    print(f"archived: {len(to_archive)} completed pairs -> {QUEUE_ARCHIVE_FILE.name}")
    print(f"live queue: {len(live)} pairs, {new_bytes:,} bytes "
          f"(was {orig_bytes:,}; {orig_bytes / max(new_bytes, 1):.1f}x smaller), write {dt*1000:.0f} ms")

    # --- integrity scan (read-only) ---
    short_files: list[str] = []
    missing = 0
    unverifiable = 0
    for pair in data:
        if pair.get("state") != "completed":
            continue
        out_dir = Path(pair.get("output_dir") or "")
        for item in pair.get("items", []):
            if item.get("state") != "completed":
                continue
            total = int(item.get("total_bytes") or 0)
            if total <= 0:
                unverifiable += 1  # no-range/unknown-size: cannot verify
                continue
            f = out_dir / (item.get("filename") or "")
            if not out_dir or not f.name:
                unverifiable += 1
                continue
            try:
                if not f.exists():
                    missing += 1  # likely moved/merged by library tools — not proof of corruption
                elif f.stat().st_size < total:
                    short_files.append(f"{f}  (on disk {f.stat().st_size:,} < expected {total:,})")
            except OSError:
                unverifiable += 1

    report = ROOT / "_queue_integrity_report.txt"
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"Queue integrity scan {ts}\n")
        f.write(f"SHORT (exists but smaller than expected — corruption signal): {len(short_files)}\n")
        for line in short_files:
            f.write(f"  {line}\n")
        f.write(f"missing from recorded path (moved/renamed by library tools, informational): {missing}\n")
        f.write(f"unverifiable (total_bytes unknown or no path): {unverifiable}\n")
    print(f"integrity report: {report.name} — SHORT={len(short_files)}, missing={missing}, unverifiable={unverifiable}")


if __name__ == "__main__":
    main()

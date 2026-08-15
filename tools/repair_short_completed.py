"""Re-queue COMPLETED items whose on-disk file is shorter than expected.

Companion to migrate_queue_prune.py's integrity scan: for every SHORT
finding, locate the owning pair (live queue.json or queue_archive.jsonl),
reset the damaged item to PENDING (fresh resolve + full re-download under
the fixed no-range semantics), re-queue the pair, and move archived pairs
back into the live queue. Backups are written before any rewrite.

Run with the app STOPPED.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.constants import QUEUE_ARCHIVE_FILE, QUEUE_FILE  # noqa: E402

REPORT = ROOT / "_queue_integrity_report.txt"


def parse_short_paths() -> list[Path]:
    paths = []
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(("G:", "H:")) and "(on disk" in line:
            paths.append(Path(line.split("  (on disk")[0]))
    return paths


def reset_item(item: dict) -> None:
    item["state"] = "pending"
    item["downloaded_bytes"] = 0
    item["error_message"] = ""
    item["resolved_url"] = ""   # stale CDN URLs must re-resolve
    item["segments"] = []


def try_repair(pair: dict, targets: dict[str, set[str]]) -> list[str]:
    """Reset matching damaged items in this pair; returns repaired filenames."""
    wanted = targets.get(pair.get("output_dir") or "")
    if not wanted:
        return []
    hit = []
    for item in pair.get("items", []):
        if item.get("state") == "completed" and item.get("filename") in wanted:
            reset_item(item)
            hit.append(item["filename"])
    if hit:
        pair["state"] = "queued"
        pair["error_message"] = ""
    return hit


def main() -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shorts = parse_short_paths()
    # {output_dir: {filename, ...}}
    targets: dict[str, set[str]] = {}
    for p in shorts:
        targets.setdefault(str(p.parent), set()).add(p.name)
    print(f"integrity report: {len(shorts)} damaged files in {len(targets)} folders")

    live = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    QUEUE_FILE.with_name(f"queue.json.bak-repair-{ts}").write_text(
        json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8")

    repaired: list[str] = []
    for pair in live:
        repaired += [f"[live] {pair['name']}: {f}" for f in try_repair(pair, targets)]

    # Archive pass: pull damaged pairs back into the live queue
    kept_lines: list[str] = []
    pulled = 0
    if QUEUE_ARCHIVE_FILE.exists():
        archive_backup = QUEUE_ARCHIVE_FILE.with_name(f"queue_archive.jsonl.bak-repair-{ts}")
        raw = QUEUE_ARCHIVE_FILE.read_text(encoding="utf-8")
        archive_backup.write_text(raw, encoding="utf-8")
        for line in raw.splitlines():
            if not line.strip():
                continue
            pair = json.loads(line)
            hit = try_repair(pair, targets)
            if hit:
                live.append(pair)
                pulled += 1
                repaired += [f"[archive->live] {pair['name']}: {f}" for f in hit]
            else:
                kept_lines.append(line)
        tmp = QUEUE_ARCHIVE_FILE.with_name(f"queue_archive.jsonl.tmp-{os.getpid()}")
        tmp.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
        os.replace(tmp, QUEUE_ARCHIVE_FILE)

    tmp = QUEUE_FILE.with_name(f"queue.json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(live, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, QUEUE_FILE)

    print(f"repaired items: {len(repaired)} (pairs pulled from archive: {pulled})")
    for r in repaired:
        print(" ", r)
    unmatched = len(shorts) - len(repaired)
    if unmatched:
        print(f"WARNING: {unmatched} damaged files had no matching queue item "
              f"(pair renamed/removed) — listed in the integrity report for manual review")


if __name__ == "__main__":
    main()

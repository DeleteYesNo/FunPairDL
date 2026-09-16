"""One-time backfill of topic_index.json from funpairdl.log.

Batch sends log "Added pair: <name> (n items)" followed within a couple of
seconds by "Auto-close: registered topic <id> with N pair(s)"; the pair id
comes from queue.json / queue_archive.jsonl by name (nearest created_at).
Topics sent from the sidebar panel never logged a topic id — those are
still recognised at lookup time by title. Safe to re-run (idempotent);
the app may be running (only topic_index.json is written).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.constants import LOG_FILE, QUEUE_ARCHIVE_FILE, QUEUE_FILE  # noqa: E402
from funpairdl.persistence.topic_index import TopicIndex  # noqa: E402

ADDED = re.compile(r"^(\S+ \S+) \[INFO\] funpairdl\.queue_manager: Added pair: (.*) \((\d+) items\)$")
REG = re.compile(r"^(\S+ \S+) \[INFO\] funpairdl\.gui\.browser: Auto-close: registered topic (\d+) with (\d+) pair\(s\)$")


def main() -> None:
    pairs_by_name: dict[str, list[tuple[datetime, str]]] = {}

    def _add(d):
        try:
            at = datetime.fromisoformat(d.get("created_at", ""))
        except ValueError:
            return
        pairs_by_name.setdefault(d.get("name", ""), []).append((at, d["id"]))

    for d in json.loads(QUEUE_FILE.read_text(encoding="utf-8")):
        _add(d)
    with open(QUEUE_ARCHIVE_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                _add(json.loads(line))
            except ValueError:
                pass

    idx = TopicIndex()
    pending: list[tuple[datetime, str]] = []   # (time, name) awaiting a topic registration
    n = 0
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = ADDED.match(line)
            if m:
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                pending = [(t, nm) for t, nm in pending if (at - t).total_seconds() <= 60]
                pending.append((at, m.group(2)))
                continue
            m = REG.match(line)
            if not m:
                continue
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            tid = m.group(2)
            fresh = [(t, nm) for t, nm in pending if (at - t).total_seconds() <= 60]
            if not fresh:
                continue
            # A batch card registers all N pairs it created at once — the
            # last N "Added pair" lines before it.
            count = max(1, int(m.group(3)))
            take, pending = fresh[-count:], fresh[:-count]
            for _t, name in take:
                cands = pairs_by_name.get(name) or []
                if not cands:
                    continue
                best = min(cands, key=lambda c: abs((c[0] - at).total_seconds()))
                if abs((best[0] - at).total_seconds()) > 120:
                    continue
                idx.record_pair(tid, "", name, best[1], name)
                n += 1
    print(f"recorded {n} topic→pair links into {idx.path.name}")


if __name__ == "__main__":
    main()

"""Topic index: which EroScripts topics were opened and which were sent to
the queue — so the forum's topic lists can show "downloaded" / "opened"
badges instead of relying on memory.

topic_index.json: {topic_id: {"url", "title", "visited_at",
                              "pairs": [{"id", "name", "at"}, ...]}}

Only ids and names live here; a pair's *state* is looked up live (queue,
else the archive, where everything is completed). Topics sent before this
index existed are still recognised by title: every pair name in the queue
and the archive is compared with the topic title (qualifier tags dropped).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from funpairdl.constants import CONFIG_DIR, QUEUE_ARCHIVE_FILE

logger = logging.getLogger("funpairdl.topic_index")

TOPIC_INDEX_FILE = CONFIG_DIR / "topic_index.json"

_lock = threading.RLock()
_ARCHIVE_TTL = 30.0  # seconds between re-reads of the archive when unchanged


class TopicIndex:
    def __init__(self, path: Path = TOPIC_INDEX_FILE, archive_path: Path = QUEUE_ARCHIVE_FILE):
        self.path = Path(path)
        self.archive_path = Path(archive_path)
        self._data: dict[str, dict] | None = None
        self._file_sig: tuple = ()
        self._archive: dict[str, str] = {}        # pair id -> name
        self._archive_titles: set[str] = set()    # title keys of archived pair names
        self._archive_sig: tuple = ()
        self._archive_checked = 0.0

    # ── storage ──
    def _load(self) -> dict[str, dict]:
        """In-memory copy, re-read when the file changed underneath us (the
        backfill tool writes it while the app runs)."""
        try:
            st = self.path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = ()
        if self._data is None or sig != self._file_sig:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            except Exception as e:
                logger.warning("topic index unreadable, starting empty: %s", e)
                self._data = {}
            self._file_sig = sig
        return self._data

    def _save(self) -> None:
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            st = self.path.stat()
            self._file_sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            self._file_sig = ()

    # ── writes ──
    def record_pair(self, topic_id: str, url: str, title: str, pair_id: str, pair_name: str) -> None:
        if not topic_id or not pair_id:
            return
        with _lock:
            data = self._load()
            e = data.setdefault(str(topic_id), {"url": "", "title": "", "visited_at": "", "pairs": []})
            if url:
                e["url"] = url
            if title and not e.get("title"):
                e["title"] = title
            if not any(p.get("id") == pair_id for p in e["pairs"]):
                e["pairs"].append({"id": pair_id, "name": pair_name, "at": datetime.now().isoformat(timespec="seconds")})
            self._save()

    def record_visit(self, topic_id: str, url: str, title: str) -> None:
        if not topic_id:
            return
        with _lock:
            data = self._load()
            e = data.setdefault(str(topic_id), {"url": "", "title": "", "visited_at": "", "pairs": []})
            e["visited_at"] = datetime.now().isoformat(timespec="seconds")
            if url:
                e["url"] = url
            if title:
                e["title"] = title
            self._save()

    # ── archive (pair id -> name; title keys) ──
    def _refresh_archive(self, title_key) -> None:
        now = time.monotonic()
        if now - self._archive_checked < _ARCHIVE_TTL:
            return
        self._archive_checked = now
        try:
            st = self.archive_path.stat()
            sig = (st.st_size, int(st.st_mtime))
        except OSError:
            sig = ()
        if sig == self._archive_sig:
            return
        pairs: dict[str, str] = {}
        titles: set[str] = set()
        try:
            with open(self.archive_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    pid, name = d.get("id"), d.get("name") or ""
                    if pid:
                        pairs[pid] = name
                    k = title_key(name)
                    if len(k) >= 4:
                        titles.add(k)
        except OSError:
            pass
        self._archive, self._archive_titles, self._archive_sig = pairs, titles, sig

    # ── reads ──
    _RANK = {"downloading": 4, "queued": 3, "paused": 3, "failed": 2, "completed": 1}

    def status(self, topics: list[dict], live_pairs: list, title_key) -> dict[str, dict]:
        """topics: [{"id": str, "title": str}] -> {id: {"state", "pairs", "names",
        "visited_at", "by_title"}}. `live_pairs` are the queue's Pair objects;
        `title_key` is QueueManager._title_key."""
        self._refresh_archive(title_key)
        live_by_id = {p.id: p for p in live_pairs}
        live_titles: dict[str, str] = {}
        for p in live_pairs:
            k = title_key(p.name)
            if len(k) >= 4:
                cur = live_titles.get(k)
                if cur is None or self._RANK.get(p.state.value, 0) > self._RANK.get(cur, 0):
                    live_titles[k] = p.state.value
        with _lock:
            data = self._load()
            out: dict[str, dict] = {}
            for t in topics:
                tid = str(t.get("id") or "")
                if not tid:
                    continue
                e = data.get(tid) or {}
                states: list[str] = []
                names: list[str] = []
                for p in e.get("pairs") or []:
                    pid = p.get("id")
                    lp = live_by_id.get(pid)
                    if lp is not None:
                        states.append(lp.state.value)
                        names.append(lp.name)
                    elif pid in self._archive:
                        states.append("completed")
                        names.append(self._archive[pid] or p.get("name", ""))
                by_title = False
                if not states:
                    k = title_key(t.get("title") or e.get("title") or "")
                    if len(k) >= 4:
                        if k in live_titles:
                            states.append(live_titles[k])
                            by_title = True
                        elif k in self._archive_titles:
                            states.append("completed")
                            by_title = True
                state = max(states, key=lambda s: self._RANK.get(s, 0)) if states else ""
                out[tid] = {
                    "state": state,
                    "pairs": len(states),
                    "names": names[:5],
                    "visited_at": e.get("visited_at", ""),
                    "by_title": by_title,
                }
            return out


_index: TopicIndex | None = None


def get_topic_index() -> TopicIndex:
    global _index
    if _index is None:
        _index = TopicIndex()
    return _index

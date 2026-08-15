from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from funpairdl.constants import (
    QUEUE_ARCHIVE_FILE,
    QUEUE_FILE,
    SAVE_DEBOUNCE_SECONDS,
    SAVE_MAX_LATENCY_SECONDS,
)
from funpairdl.core.pair import ItemState, Pair, PairState

logger = logging.getLogger("funpairdl.persistence.queue_store")


class QueueLoadError(RuntimeError):
    """The queue file exists but could not be READ (I/O failure, not
    corruption). Callers must not continue with an empty queue — a later
    save would overwrite the intact file on disk."""


def _replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.replace with retry — on Windows a concurrent reader of dst holds
    a share lock that makes ReplaceFile fail transiently."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


class QueueStore:
    """Persists download queue state to a JSON file.

    v2: queue saves go through a dedicated "queue-save" daemon thread with
    trailing-edge debouncing (capped at SAVE_MAX_LATENCY_SECONDS), so
    serializing the queue never blocks the Qt main thread or the download
    loop. Writes are atomic (temp file + os.replace).

    Load distinguishes corruption from I/O failure: a corrupt file is
    preserved as .corrupt-<ts> and an empty queue returned; an UNREADABLE
    file raises QueueLoadError so the caller aborts instead of running with
    an empty queue that a later save would persist (audit [4]).

    Archive appends (queue_archive.jsonl, one pair dict per line) are
    SYNCHRONOUS and raise on failure — queue_manager relies on that to keep
    pairs live when archiving fails, instead of dropping them from every
    store.
    """

    def __init__(
        self,
        path: Path = QUEUE_FILE,
        archive_path: Path = QUEUE_ARCHIVE_FILE,
    ):
        self.path = Path(path)
        self.archive_path = Path(archive_path)

        # Serializes ALL file access (queue writes from any thread + archive
        # appends). save_now and the writer thread both take this lock, so
        # two writers can never interleave on the same file.
        self._file_lock = threading.Lock()

        # Protects the pending-work state below; also the condition the
        # writer thread sleeps on.
        self._cond = threading.Condition()
        self._snapshot_fn: Callable[[], list[dict]] | None = None
        self._save_due_at: float = 0.0        # monotonic debounce deadline
        self._save_first_at: float = 0.0      # first uncoalesced request time
        self._stop = False
        self._flush_on_stop = True
        self._writer_thread: threading.Thread | None = None

        # Save generation: claimed under _cond when a snapshot is taken,
        # checked under _file_lock before os.replace — an older snapshot can
        # never overwrite a newer one (writer-vs-save_now shutdown race).
        self._generation = 0
        self._written_generation = 0

    # ------------------------------------------------------------------
    # Writer thread lifecycle
    # ------------------------------------------------------------------

    def start_writer(self) -> None:
        """Start the background writer thread (idempotent)."""
        with self._cond:
            if self._writer_thread is not None and self._writer_thread.is_alive():
                return
            self._stop = False
            self._flush_on_stop = True
            self._writer_thread = threading.Thread(
                target=self._writer_loop, name="queue-save", daemon=True,
            )
            self._writer_thread.start()

    def stop_writer(self, flush: bool = True, timeout: float = 10.0) -> None:
        """Stop the writer thread. With flush=True (default), any pending
        debounced save is written first — including one enqueued while the
        writer was doing its final I/O (drained here after the join)."""
        with self._cond:
            thread = self._writer_thread
            if thread is None:
                return
            self._stop = True
            self._flush_on_stop = flush
            if not flush:
                self._snapshot_fn = None
            self._cond.notify_all()
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("queue-save thread did not stop within %.1fs", timeout)
        leftover = None
        with self._cond:
            if self._writer_thread is thread:
                self._writer_thread = None
            # Work enqueued after the writer grabbed its final batch would
            # otherwise be silently dropped.
            if flush and self._snapshot_fn is not None:
                leftover = self._claim_snapshot_locked()
        if leftover is not None:
            self._save_from_fn(*leftover)

    def _claim_snapshot_locked(self) -> tuple[Callable[[], list[dict]], int] | None:
        """Take the pending snapshot_fn and assign it a generation.
        Caller must hold _cond."""
        if self._snapshot_fn is None:
            return None
        fn = self._snapshot_fn
        self._snapshot_fn = None
        self._generation += 1
        return fn, self._generation

    def _writer_loop(self) -> None:
        while True:
            claimed = None
            with self._cond:
                # Sleep until stopped or a pending save's debounce deadline
                # passes. request_save pushes the deadline forward on every
                # call (trailing edge, capped by SAVE_MAX_LATENCY_SECONDS
                # from the first request), so bursts coalesce into one write.
                while not self._stop:
                    if self._snapshot_fn is not None:
                        remaining = self._save_due_at - time.monotonic()
                        if remaining <= 0:
                            break
                        self._cond.wait(timeout=remaining)
                    else:
                        self._cond.wait()

                stop = self._stop
                if self._snapshot_fn is not None:
                    due = time.monotonic() >= self._save_due_at
                    if due or (stop and self._flush_on_stop):
                        claimed = self._claim_snapshot_locked()
                    elif stop:
                        # stopping without flush — drop the pending save
                        self._snapshot_fn = None

            # File I/O outside the condition lock so callers never block on it.
            if claimed is not None:
                self._save_from_fn(*claimed)
            if stop:
                return

    # ------------------------------------------------------------------
    # Save API
    # ------------------------------------------------------------------

    def request_save(self, snapshot_fn: Callable[[], list[dict]]) -> None:
        """Schedule a debounced save. snapshot_fn is called on the writer
        thread SAVE_DEBOUNCE_SECONDS after the last request (trailing edge);
        repeated requests within the window coalesce into one write, but the
        write happens no later than SAVE_MAX_LATENCY_SECONDS after the first
        pending request even under a sustained burst."""
        with self._cond:
            if self._writer_thread is not None and self._writer_thread.is_alive():
                now = time.monotonic()
                if self._snapshot_fn is None:
                    self._save_first_at = now
                self._snapshot_fn = snapshot_fn
                self._save_due_at = min(
                    self._save_first_at + SAVE_MAX_LATENCY_SECONDS,
                    now + SAVE_DEBOUNCE_SECONDS,
                )
                self._cond.notify_all()
                return
        # Writer not running (tools / tests / shutdown race): don't drop the
        # save — do it synchronously on the caller.
        logger.warning("queue-save thread not running — saving synchronously")
        with self._cond:
            self._generation += 1
            gen = self._generation
        self._save_from_fn(snapshot_fn, gen)

    def save_now(self, snapshot_fn: Callable[[], list[dict]]) -> None:
        """Synchronous save on the calling thread (shutdown path). Mutually
        exclusive with the writer thread via the shared file lock; claims a
        newer generation, so an in-flight older debounced write can never
        overwrite this snapshot afterwards."""
        with self._cond:
            # Supersede any pending debounced save — the writer would only
            # re-serialize the same (or older) state.
            self._snapshot_fn = None
            self._generation += 1
            gen = self._generation
        self._save_from_fn(snapshot_fn, gen)

    def save(self, pairs: list[Pair]) -> None:
        """Legacy synchronous API (tools/tests) — kept for compatibility."""
        self.save_now(lambda: [p.to_dict() for p in pairs])

    def _save_from_fn(self, snapshot_fn: Callable[[], list[dict]], generation: int) -> None:
        try:
            data = snapshot_fn()
        except Exception:
            # Never let a bad snapshot kill the writer thread.
            logger.error("Queue snapshot failed — skipping this save", exc_info=True)
            return
        self._write_queue(data, generation)

    def _write_queue(self, data: list[dict], generation: int) -> None:
        start = time.monotonic()
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        try:
            with self._file_lock:
                if generation < self._written_generation:
                    logger.debug(
                        "Skipping stale queue save (gen %d < %d)",
                        generation, self._written_generation,
                    )
                    return
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                # Atomic swap — a kill mid-write can never truncate queue.json.
                _replace_with_retry(tmp, self.path)
                self._written_generation = generation
        except Exception:
            logger.error("Failed to save queue", exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return

        duration = time.monotonic() - start
        try:
            size = self.path.stat().st_size
        except OSError:
            size = -1
        level = logger.info if duration > 0.2 else logger.debug
        level("Queue saved: %d pairs, %d bytes in %.3fs", len(data), size, duration)

    # ------------------------------------------------------------------
    # Archive (JSONL, append-only)
    # ------------------------------------------------------------------

    def append_archive(self, pair_dicts: list[dict]) -> None:
        """Append completed pair dicts to the JSONL archive, synchronously.

        RAISES on failure — queue_manager keeps the pairs in the live queue
        when this fails, so a full disk / locked archive file can never make
        completed pairs vanish from both stores. Serialization happens
        eagerly on the caller (which holds the pairs lock), so the written
        lines can't be affected by concurrent mutation either. Segments are
        stripped from every item before writing."""
        if not pair_dicts:
            return
        lines = []
        for pd in pair_dicts:
            pd = dict(pd)
            items = pd.get("items")
            if items:
                pd["items"] = [
                    {k: v for k, v in it.items() if k != "segments"}
                    for it in items
                ]
            lines.append(json.dumps(pd, ensure_ascii=False))
        with self._file_lock:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.archive_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
                f.flush()
                os.fsync(f.fileno())
        logger.debug("Archived %d pair(s) to %s", len(lines), self.archive_path.name)

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> list[Pair]:
        # Read with a short retry: transient Windows sharing violations
        # (AV scan / backup tool holding the file) are I/O failures, NOT
        # corruption — they must never sideline a good file or return an
        # empty queue that a later save would persist.
        raw: str | None = None
        io_err: OSError | None = None
        for attempt in range(5):
            try:
                raw = self.path.read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return []
            except OSError as e:
                io_err = e
                time.sleep(0.2 * (attempt + 1))
        if raw is None:
            raise QueueLoadError(
                f"queue file exists but could not be read after retries: {io_err}"
            )

        try:
            data = json.loads(raw)
            pairs = []
            for d in data:
                pair = Pair.from_dict(d)
                # Reset downloading pairs to queued on reload
                if pair.state == PairState.DOWNLOADING:
                    pair.state = PairState.QUEUED
                for item in pair.items:
                    if item.state == ItemState.COMPLETED:
                        # Force-sync bytes for completed items — queue may have
                        # been saved before bytes were synced (crash, old version).
                        if item.total_bytes > 0:
                            item.downloaded_bytes = item.total_bytes
                        # Segments are download-time bookkeeping; on completed
                        # items they are dead weight that bloats every save
                        # (audit [0]). Strip them here to repair old data.
                        if item.segments:
                            item.segments = []
                pairs.append(pair)

            logger.info("Loaded %d pairs from queue store", len(pairs))
            return pairs
        except Exception as e:
            # Parse/shape failure = real corruption. Preserve the broken file
            # for manual recovery — returning [] while leaving it in place
            # would let the next auto-save silently destroy it (audit [4]).
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, corrupt)
                logger.error(
                    "Corrupt queue file (%s) — preserved as %s, starting with "
                    "an empty queue", e, corrupt.name,
                )
            except OSError as mv_err:
                # Can't sideline it either — refuse to continue, otherwise a
                # later save overwrites a file we never managed to secure.
                raise QueueLoadError(
                    f"queue file is corrupt ({e}) and could not be preserved "
                    f"({mv_err})"
                ) from e
            return []

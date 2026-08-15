"""Tests for QueueStore v2: background writer, debounce, atomic writes,
corrupt-load preservation, segment stripping and the JSONL archive."""
import json
import time

import funpairdl.persistence.queue_store as qs_mod
from funpairdl.core.pair import (
    FileType, ItemState, Pair, PairItem, PairState, SegmentInfo,
)
from funpairdl.persistence.queue_store import QueueStore


def _item(state=ItemState.COMPLETED, with_segments=True):
    item = PairItem(
        url="https://host/a.mp4", filename="a.mp4", file_type=FileType.VIDEO,
    )
    item.state = state
    item.total_bytes = 100
    if with_segments:
        item.segments = [
            SegmentInfo(index=0, range_start=0, range_end=99,
                        downloaded=100, temp_file="a.mp4.part0"),
        ]
    return item


def _pair(name="P", item_state=ItemState.COMPLETED, pair_state=PairState.COMPLETED):
    return Pair(name=name, items=[_item(state=item_state)], state=pair_state)


def _store(tmp_path):
    return QueueStore(
        path=tmp_path / "queue.json",
        archive_path=tmp_path / "queue_archive.jsonl",
    )


def _wait_for(cond_fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond_fn():
            return True
        time.sleep(0.02)
    return cond_fn()


class TestSaveLoad:
    def test_legacy_save_roundtrip(self, tmp_path):
        # The legacy synchronous save(pairs) wrapper (tools/tests) must keep
        # working without start_writer.
        store = _store(tmp_path)
        store.save([_pair("Alpha"), _pair("Beta")])
        loaded = store.load()
        assert [p.name for p in loaded] == ["Alpha", "Beta"]

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        store = _store(tmp_path)
        store.save([_pair()])
        names = [p.name for p in tmp_path.iterdir()]
        assert "queue.json" in names
        assert not any(".tmp-" in n for n in names)
        # Format preserved: indent=2, ensure_ascii=False
        text = (tmp_path / "queue.json").read_text(encoding="utf-8")
        assert text.startswith("[\n  {")

    def test_save_now_runs_on_caller_without_writer(self, tmp_path):
        store = _store(tmp_path)
        store.save_now(lambda: [_pair("Now").to_dict()])
        assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))[0]["name"] == "Now"

    def test_load_strips_segments_from_completed_items_only(self, tmp_path):
        # Completed items get segments=[] on load (in-place repair of old
        # bloated queue files); non-completed items keep them for resume.
        store = _store(tmp_path)
        store.save([
            _pair("Done", item_state=ItemState.COMPLETED),
            _pair("Part", item_state=ItemState.FAILED, pair_state=PairState.FAILED),
        ])
        # The completed item's segments ARE in the file (written pre-repair)…
        raw = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
        assert raw[0]["items"][0]["segments"]
        # …but load strips them, while the failed item keeps its segments.
        done, part = store.load()
        assert done.items[0].segments == []
        assert len(part.items[0].segments) == 1

    def test_load_missing_file_returns_empty(self, tmp_path):
        assert _store(tmp_path).load() == []


class TestCorruptLoad:
    def test_corrupt_json_preserved_and_empty_returned(self, tmp_path):
        store = _store(tmp_path)
        corrupt_bytes = '[{"name": "truncated mid-wri'
        (tmp_path / "queue.json").write_text(corrupt_bytes, encoding="utf-8")

        assert store.load() == []

        # Original moved aside — NOT deleted, NOT left in place (where the
        # next auto-save would clobber it with an empty queue).
        assert not (tmp_path / "queue.json").exists()
        preserved = list(tmp_path.glob("queue.json.corrupt-*"))
        assert len(preserved) == 1
        assert preserved[0].read_text(encoding="utf-8") == corrupt_bytes

    def test_valid_json_wrong_shape_also_preserved(self, tmp_path):
        store = _store(tmp_path)
        (tmp_path / "queue.json").write_text('{"not": "a pair list"}', encoding="utf-8")
        assert store.load() == []
        assert list(tmp_path.glob("queue.json.corrupt-*"))


class TestArchive:
    def test_append_archive_jsonl_and_segment_strip(self, tmp_path):
        # Without a writer thread, appends happen synchronously. One JSON
        # line per pair; every item has its "segments" key stripped.
        store = _store(tmp_path)
        store.append_archive([_pair("A1").to_dict()])
        store.append_archive([_pair("A2").to_dict(), _pair("A3").to_dict()])

        lines = (tmp_path / "queue_archive.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        docs = [json.loads(line) for line in lines]
        assert [d["name"] for d in docs] == ["A1", "A2", "A3"]
        for d in docs:
            for it in d["items"]:
                assert "segments" not in it

    def test_append_archive_via_writer_thread(self, tmp_path):
        store = _store(tmp_path)
        store.start_writer()
        try:
            store.append_archive([_pair("Threaded").to_dict()])
            assert _wait_for(lambda: (tmp_path / "queue_archive.jsonl").exists())
        finally:
            store.stop_writer()
        lines = (tmp_path / "queue_archive.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["name"] == "Threaded"

    def test_append_archive_empty_is_noop(self, tmp_path):
        store = _store(tmp_path)
        store.append_archive([])
        assert not (tmp_path / "queue_archive.jsonl").exists()


class TestWriterDebounce:
    def test_burst_of_requests_coalesces_into_one_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qs_mod, "SAVE_DEBOUNCE_SECONDS", 0.3)
        store = _store(tmp_path)
        store.start_writer()
        calls = []

        def snapshot():
            calls.append(time.monotonic())
            return [_pair("Debounced").to_dict()]

        try:
            for _ in range(5):
                store.request_save(snapshot)
                time.sleep(0.01)
            # Trailing edge: nothing may have been written yet right after
            # the burst (debounce window still open).
            assert not (tmp_path / "queue.json").exists()
            assert _wait_for(lambda: (tmp_path / "queue.json").exists())
            # Give the writer a moment to (incorrectly) fire again.
            time.sleep(0.5)
        finally:
            store.stop_writer()
        assert len(calls) == 1  # 5 requests → exactly 1 snapshot+write

    def test_save_now_supersedes_pending_debounced_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qs_mod, "SAVE_DEBOUNCE_SECONDS", 0.3)
        store = _store(tmp_path)
        store.start_writer()
        calls = []

        def pending_snapshot():
            calls.append("pending")
            return []

        try:
            store.request_save(pending_snapshot)
            store.save_now(lambda: [_pair("Fresh").to_dict()])
            time.sleep(0.6)  # let the (cancelled) debounce window elapse
        finally:
            store.stop_writer()
        assert calls == []  # superseded snapshot never ran
        data = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
        assert data[0]["name"] == "Fresh"

    def test_stop_writer_flushes_pending_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qs_mod, "SAVE_DEBOUNCE_SECONDS", 30.0)
        store = _store(tmp_path)
        store.start_writer()
        store.request_save(lambda: [_pair("Flushed").to_dict()])
        store.stop_writer(flush=True)  # long debounce must not block shutdown
        data = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
        assert data[0]["name"] == "Flushed"

    def test_stop_writer_without_flush_drops_pending(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qs_mod, "SAVE_DEBOUNCE_SECONDS", 30.0)
        store = _store(tmp_path)
        store.start_writer()
        store.request_save(lambda: [_pair("Dropped").to_dict()])
        store.stop_writer(flush=False)
        assert not (tmp_path / "queue.json").exists()

    def test_snapshot_exception_keeps_writer_alive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qs_mod, "SAVE_DEBOUNCE_SECONDS", 0.05)
        store = _store(tmp_path)
        store.start_writer()

        def bad_snapshot():
            raise RuntimeError("boom during to_dict")

        try:
            store.request_save(bad_snapshot)
            time.sleep(0.3)
            assert store._writer_thread.is_alive()
            store.request_save(lambda: [_pair("Recovered").to_dict()])
            assert _wait_for(lambda: (tmp_path / "queue.json").exists())
        finally:
            store.stop_writer()
        data = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
        assert data[0]["name"] == "Recovered"

    def test_request_save_without_writer_falls_back_to_sync(self, tmp_path):
        # Tools/tests that never call start_writer must not lose saves.
        store = _store(tmp_path)
        store.request_save(lambda: [_pair("Sync").to_dict()])
        data = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
        assert data[0]["name"] == "Sync"

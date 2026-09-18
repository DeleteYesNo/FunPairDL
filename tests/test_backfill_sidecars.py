"""tools/backfill_funlib_sidecars.py: offline fields from the queue
archive, topic index and log; forum fields merged without overwriting."""
import importlib.util
import json
from pathlib import Path

from funpairdl.core import library as lib

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("backfill_funlib_sidecars",
                                               ROOT / "tools" / "backfill_funlib_sidecars.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _work(root: Path, name: str, files: dict[str, bytes]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        (d / fn).write_bytes(content)
    return d


def test_index_pairs_prefers_newest_completed():
    pairs = [
        {"id": "a", "name": "Work", "state": "completed", "output_dir": r"G:\old\Work", "created_at": "2026-01-01T00:00:00"},
        {"id": "b", "name": "Work", "state": "completed", "output_dir": r"H:\lib\Work", "created_at": "2026-05-01T00:00:00"},
        {"id": "c", "name": "Work", "state": "failed", "output_dir": r"H:\lib\Work", "created_at": "2026-06-01T00:00:00"},
    ]
    by_folder, by_title = bf.index_pairs(pairs)
    assert by_folder["work"]["id"] == "b"
    assert by_title[bf.QueueManager._title_key("Work")]["id"] == "b"


def test_load_log_topics_keeps_only_unambiguous(tmp_path):
    log = tmp_path / "funpairdl.log"
    log.write_text(
        "x Page loaded: https://discuss.eroscripts.com/t/casey-sample-demo-load/111\n"
        "x Page loaded: https://discuss.eroscripts.com/t/casey-sample-demo-load/111?u=x\n"
        "x Page loaded: https://discuss.eroscripts.com/t/two-titles-alike/222\n"
        "x Page loaded: https://discuss.eroscripts.com/t/two-titles-alike/333\n",
        encoding="utf-8")
    long_slug = "a-very-long-topic-slug-that-runs-right-up-to-the-log-limit-xyz"
    trunc_url = f"https://discuss.eroscripts.com/t/{long_slug}/12345"[:100]
    assert len(trunc_url) == 100
    with open(log, "a", encoding="utf-8") as f:
        f.write("x Page loaded: " + trunc_url + "\n")
    t = bf.load_log_topics(log)
    assert t[bf.QueueManager._match_key("casey sample demo load")] == "111"
    assert bf.QueueManager._match_key(long_slug.replace("-", " ")) not in t     # truncated → ignored
    assert bf.QueueManager._match_key("two titles alike") not in t


def test_offline_sidecar_from_pair_topic_index_and_log(tmp_path):
    root = tmp_path / "lib"
    w1 = _work(root, "(Casey Sample) Demo Load", {
        "(Casey Sample) Demo Load.mp4": b"V", "(Casey Sample) Demo Load.funscript": b"S",
        "(Casey Sample) Demo Load (Soft).funscript": b"S2"})
    w2 = _work(root, "Logged Only", {"Logged Only.mp4": b"V", "Logged Only.funscript": b"S"})
    w3 = _work(root, "Orphan", {"Orphan.mp4": b"V", "Orphan.funscript": b"S"})
    pairs = [
        {"id": "p1", "name": "(Casey Sample) Demo Load", "state": "completed", "created_at": "2026-03-09T03:42:55",
         "output_dir": r"G:\Download\old\(Casey Sample) Demo Load",
         "items": [{"file_type": "funscript", "group": "", "author": "casey"}]},
        {"id": "p2", "name": "Logged Only", "state": "completed", "created_at": "2026-04-01T00:00:00",
         "output_dir": str(w2), "items": []},
    ]
    by_folder, by_title = bf.index_pairs(pairs)
    topic_by_pair = {"p1": ("12345", "")}
    log_topics = {bf.QueueManager._match_key("logged only"): "777"}

    d1 = bf.offline_sidecar(w1, by_folder, by_title, topic_by_pair, log_topics)
    assert d1["title"] == "(Casey Sample) Demo Load" and d1["author"] == "Casey Sample"
    assert d1["pair_id"] == "p1" and d1["downloaded_at"].endswith("Z")
    assert d1["source"] == {"site": "eroscripts", "url": "https://discuss.eroscripts.com/t/12345", "topic_id": 12345}
    assert {v["label"] for v in d1["variants"]} == {"Main", "Soft"}

    d2 = bf.offline_sidecar(w2, by_folder, by_title, topic_by_pair, log_topics)
    assert d2["pair_id"] == "p2"
    assert d2["source"]["topic_id"] == 777 and "author" not in d2

    d3 = bf.offline_sidecar(w3, by_folder, by_title, topic_by_pair, log_topics)
    assert d3 == {"version": 2, "title": "Orphan",
                  "variants": [{"label": "Main", "primary": True, "video": "Orphan.mp4",
                                "files": {"L0": "Orphan.funscript"}}]}


def test_repair_op_author_moves_username_to_posted_by():
    fixed = bf.repair_op_author({"title": "(Anna) Work", "author": "bekscript",
                                 "author_url": "https://x/u/bekscript", "tags": ["a"]},
                                "(Anna) Work", "(Anna) Work")
    assert fixed == {"title": "(Anna) Work", "author": "Anna", "posted_by": "bekscript",
                     "posted_by_url": "https://x/u/bekscript", "tags": ["a"]}
    # nothing to repair / already repaired → untouched
    assert bf.repair_op_author({"author": "Anna"}, "(Anna) W", "(Anna) W") == {"author": "Anna"}
    done = {"author": "Anna", "author_url": "u", "posted_by": "op"}
    assert bf.repair_op_author(done, "(Anna) W", "(Anna) W") == done
    assert bf.repair_op_author(None, "t", "f") is None


def test_rerun_keeps_forum_fields_and_refreshes_variants(tmp_path):
    root = tmp_path / "lib"
    w = _work(root, "Work", {"Work.mp4": b"V", "Work.funscript": b"S"})
    lib.write_sidecar(w, {"title": "Work", "author": "opname", "author_url": "https://x/u/opname",
                          "tags": ["hmv"], "category": {"id": 14, "name": "Free Scripts"},
                          "posted_at": "2026-01-01T00:00:00Z", "pair_id": "old",
                          "variants": [{"label": "Main", "primary": True, "files": {"L0": "stale"}}]})
    (w / "Work (Hard).funscript").write_bytes(b"H")
    by_folder, by_title = bf.index_pairs([{"id": "new", "name": "(Someone) Work", "state": "completed",
                                           "created_at": "2026-05-01T00:00:00", "output_dir": str(w)}])
    data = bf.offline_sidecar(w, by_folder, by_title, {}, {})
    merged = lib.merge_sidecar(lib.read_sidecar(w), data)
    assert merged["author"] == "opname" and merged["tags"] == ["hmv"] and merged["pair_id"] == "old"
    assert merged["title"] == "Work"
    assert {v["label"]: v["files"] for v in merged["variants"]} == {
        "Main": {"L0": "Work.funscript"}, "Hard": {"L0": "Work (Hard).funscript"}}


def test_cli_apply_writes_a_repair_even_when_nothing_else_changed(tmp_path, monkeypatch):
    import sys
    root = tmp_path / "lib"
    w = _work(root, "(Anna) Work", {"(Anna) Work.mp4": b"V", "(Anna) Work.funscript": b"S"})
    lib.write_sidecar(w, {"title": "(Anna) Work", "author": "opname", "author_url": "https://x/u/opname",
                          "variants": [{"label": "Main", "primary": True, "video": "(Anna) Work.mp4",
                                        "files": {"L0": "(Anna) Work.funscript"}}]})
    monkeypatch.setattr(bf, "load_pairs", lambda: [])
    monkeypatch.setattr(bf, "load_topic_index", lambda: {})
    monkeypatch.setattr(bf, "load_log_topics", lambda: {})
    monkeypatch.setattr(sys, "argv", ["x", "--roots", str(root), "--report", str(tmp_path / "r.md"), "--apply"])
    bf.main()
    sc = lib.read_sidecar(w)
    assert sc["author"] == "Anna" and sc["posted_by"] == "opname" and "author_url" not in sc


def test_cli_dry_run_writes_nothing_and_apply_writes(tmp_path, monkeypatch):
    import sys
    root = tmp_path / "lib"
    w = _work(root, "Work", {"Work.mp4": b"V", "Work.funscript": b"S"})
    _work(root / "_trash" / "20260918-000000", "Binned", {"Binned.funscript": b"B"})
    monkeypatch.setattr(bf, "load_pairs", lambda: [])
    monkeypatch.setattr(bf, "load_topic_index", lambda: {})
    monkeypatch.setattr(bf, "load_log_topics", lambda: {})
    report = tmp_path / "r.md"
    monkeypatch.setattr(sys, "argv", ["x", "--roots", str(root), "--report", str(report)])
    bf.main()
    assert not (w / "funlib.json").exists()
    monkeypatch.setattr(sys, "argv", ["x", "--roots", str(root), "--report", str(report), "--apply"])
    bf.main()
    sc = json.loads((w / "funlib.json").read_text(encoding="utf-8"))
    assert sc["title"] == "Work" and sc["variants"][0]["files"] == {"L0": "Work.funscript"}
    assert not (root / "_trash" / "20260918-000000" / "Binned" / "funlib.json").exists()

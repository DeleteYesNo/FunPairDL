"""Tests for the topic index: what the forum's topic lists show as opened /
downloaded, by recorded pair ids and by title fallback."""
import json

from funpairdl.core.pair import Pair, PairState
from funpairdl.core.queue_manager import QueueManager
from funpairdl.persistence.topic_index import TopicIndex


def _pair(name, state):
    p = Pair(name=name)
    p.state = state
    return p


def _index(tmp_path, archive_lines=()):
    arch = tmp_path / "queue_archive.jsonl"
    arch.write_text("".join(json.dumps(d) + "\n" for d in archive_lines), encoding="utf-8")
    return TopicIndex(tmp_path / "topic_index.json", arch)


def test_recorded_pair_reports_live_state(tmp_path):
    idx = _index(tmp_path)
    live = _pair("Work A", PairState.DOWNLOADING)
    idx.record_pair("101", "https://x/t/work-a/101", "Work A", live.id, live.name)
    st = idx.status([{"id": "101", "title": "Work A"}], [live], QueueManager._title_key)
    assert st["101"]["state"] == "downloading"
    assert st["101"]["pairs"] == 1 and st["101"]["by_title"] is False


def test_archived_pair_counts_as_completed(tmp_path):
    idx = _index(tmp_path, [{"id": "abc123", "name": "Work B", "state": "completed"}])
    idx.record_pair("102", "", "Work B", "abc123", "Work B")
    st = idx.status([{"id": "102", "title": "Work B"}], [], QueueManager._title_key)
    assert st["102"]["state"] == "completed"
    assert st["102"]["names"] == ["Work B"]


def test_unknown_topic_matches_by_title_with_qualifiers_dropped(tmp_path):
    idx = _index(tmp_path, [{"id": "old1", "name": "Work C", "state": "completed"}])
    st = idx.status([{"id": "103", "title": "Work C (Requested, HQ Script)"}], [], QueueManager._title_key)
    assert st["103"]["state"] == "completed" and st["103"]["by_title"] is True
    # a live pair with the same title wins over the archive and reports its state
    st = idx.status([{"id": "103", "title": "Work C"}], [_pair("Work C", PairState.FAILED)], QueueManager._title_key)
    assert st["103"]["state"] == "failed" and st["103"]["by_title"] is True


def test_visit_only_and_nothing_known(tmp_path):
    idx = _index(tmp_path)
    idx.record_visit("104", "https://x/t/work-d/104", "Work D")
    st = idx.status([{"id": "104", "title": "Work D"}, {"id": "105", "title": "Work E"}], [], QueueManager._title_key)
    assert st["104"]["state"] == "" and st["104"]["visited_at"]
    assert st["105"]["state"] == "" and not st["105"]["visited_at"]


def test_index_survives_reload_and_dedupes_pairs(tmp_path):
    idx = _index(tmp_path)
    idx.record_pair("106", "u", "Work F", "p1", "Work F")
    idx.record_pair("106", "u", "Work F", "p1", "Work F")
    again = TopicIndex(idx.path, idx.archive_path)
    assert len(again._load()["106"]["pairs"]) == 1


def test_add_pair_records_topic(tmp_path, monkeypatch):
    import funpairdl.persistence.topic_index as ti
    idx = _index(tmp_path)
    monkeypatch.setattr(ti, "_index", idx)
    qm = QueueManager(download_dir=tmp_path)
    p = qm.add_pair(name="Work G", video_urls=["https://v/1"],
                    source_url="https://discuss.example/t/work-g/107")
    assert idx._load()["107"]["pairs"][0]["id"] == p.id
    assert p.to_dict()["source_url"].endswith("/107")

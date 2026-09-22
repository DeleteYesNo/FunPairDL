"""library_lookup: an existing work is found by a known video URL or by
title; scripts are judged identical / changed / new against the folder."""
import hashlib
import json
from pathlib import Path

from funpairdl.core import library_lookup as ll
from funpairdl.core.pair import FileType, ItemState, Pair, PairItem, PairState
from funpairdl.core.queue_manager import QueueManager


def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _work(root: Path, name: str, video: bytes | None = b"v" * 500, l0: bytes = b'{"actions":[{"at":120000,"pos":0}]}'):
    d = root / name
    d.mkdir(parents=True)
    if video is not None:
        (d / f"{name}.mp4").write_bytes(video)
    (d / f"{name}.funscript").write_bytes(l0)
    return d


def _lookup(title, videos, scripts, live, roots):
    return ll.lookup(title, videos, scripts, live, roots,
                     QueueManager._title_key, QueueManager._match_key, QueueManager._parse_axis)


class TestFindWork:
    def test_known_video_url_in_live_queue(self, tmp_path):
        d = _work(tmp_path, "Work Title")
        p = Pair(name="Work Title")
        p.state = PairState.COMPLETED
        p.output_dir = str(d)
        it = PairItem(url="https://pixeldrain.com/u/aaaa1111", filename="x.mp4", file_type=FileType.VIDEO)
        it.state = ItemState.COMPLETED
        p.items = [it]
        res = _lookup("Other Post Title", [{"url": "https://pixeldrain.com/u/aaaa1111"}], [], [p], [tmp_path])
        assert res["match"] == "url" and res["same_content"] is True
        assert res["work"]["dir"] == str(d) and res["work"]["video"] == "Work Title.mp4"

    def test_known_video_url_in_archive(self, tmp_path, monkeypatch):
        d = _work(tmp_path, "Work Title")
        arch = tmp_path / "archive.jsonl"
        arch.write_text(json.dumps({
            "id": "p1", "name": "Work Title", "state": "completed", "output_dir": str(d),
            "items": [{"url": "https://mega.nz/file/x#y", "resolved_url": "https://mega.nz/file/x#y"}],
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(ll, "QUEUE_ARCHIVE_FILE", arch)
        ll._archive_stamp = None
        res = _lookup("T", [{"url": "https://mega.nz/file/x#y"}], [], [], [tmp_path])
        assert res["match"] == "url" and res["work"]["dir"] == str(d)

    def test_title_match_needs_matching_length(self, tmp_path):
        _work(tmp_path, "Work Title")
        # Video on disk is not a real container -> duration falls back to the
        # L0 script (120 s).
        res = _lookup("Work Title (Requested, HQ Script)", [{"url": "https://h/v", "duration": 121.0}], [], [], [tmp_path])
        assert res["match"] == "title" and res["same_content"] is True
        res = _lookup("Work Title", [{"url": "https://h/v", "duration": 300.0}], [], [], [tmp_path])
        assert res["match"] == "title" and res["same_content"] is False
        res = _lookup("Work Title", [{"url": "https://h/v"}], [], [], [tmp_path])
        assert res["same_content"] is None

    def test_no_match(self, tmp_path):
        _work(tmp_path, "Work Title")
        res = _lookup("Another Thing", [{"url": "https://h/v"}], [], [], [tmp_path])
        assert res["work"] is None and res["match"] == ""

    def test_trash_and_meta_folders_are_ignored(self, tmp_path):
        _work(tmp_path / "_trash" / "20260101-000000", "Work Title")
        res = _lookup("Work Title", [], [], [], [tmp_path])
        assert res["work"] is None


class TestScripts:
    def test_sha1_identical_changed_new(self, tmp_path):
        l0 = b'{"actions":[{"at":120000,"pos":0}]}'
        d = _work(tmp_path, "Work Title", l0=l0)
        (d / "Work Title.pitch.funscript").write_bytes(b"pitch-old")
        cdn = "https://eroscripts-discourse.eroscripts.com/original/4X/a/b/c/"
        scripts = [
            {"url": "https://discuss.eroscripts.com/uploads/short-url/one.funscript",
             "resolved": cdn + _sha1(l0) + ".funscript", "name": "Work Title.funscript"},
            {"url": "https://discuss.eroscripts.com/uploads/short-url/two.funscript",
             "resolved": cdn + _sha1(b"pitch-new") + ".funscript", "name": "Work Title.pitch.funscript"},
            {"url": "https://discuss.eroscripts.com/uploads/short-url/three.funscript",
             "resolved": cdn + _sha1(b"surge") + ".funscript", "name": "Work Title.surge.funscript"},
        ]
        res = _lookup("Work Title", [], scripts, [], [tmp_path])
        assert res["same_content"] is True
        assert res["scripts"] == {scripts[0]["url"]: "identical",
                                  scripts[1]["url"]: "changed",
                                  scripts[2]["url"]: "new"}

    def test_size_identity_for_file_hosts(self, tmp_path):
        d = _work(tmp_path, "Work Title", l0=b"x" * 4321)
        res = _lookup("Work Title", [], [
            {"url": "https://pixeldrain.com/u/aaaa1111", "name": "Work Title.funscript", "size": 4321},
            {"url": "https://pixeldrain.com/u/bbbb2222", "name": "Work Title.funscript", "size": 999},
            {"url": "https://pixeldrain.com/u/cccc3333", "name": "Work Title.funscript"},
        ], [], [tmp_path])
        assert res["scripts"]["https://pixeldrain.com/u/aaaa1111"] == "identical"
        assert res["scripts"]["https://pixeldrain.com/u/bbbb2222"] == "changed"
        assert res["scripts"]["https://pixeldrain.com/u/cccc3333"] == "unknown"
        assert d.is_dir()


class TestDurationsAndMainVideo:
    def test_script_duration_stands_in_for_a_probe_without_one(self, tmp_path):
        _work(tmp_path, "Work Title")   # L0 says 120 s
        res = _lookup("Work Title", [{"url": "https://rule34video.com/video/1/work-title/"}],
                      [{"url": "https://h/s.funscript", "name": "Work Title.funscript", "duration": 119.0}],
                      [], [tmp_path])
        assert res["match"] == "title" and res["same_content"] is True

    def test_main_video_is_the_one_named_after_the_folder(self, tmp_path):
        d = _work(tmp_path, "Work Title")
        (d / "Work Title (stockings).mp4").write_bytes(b"v" * 500)
        res = _lookup("Work Title", [], [], [], [tmp_path])
        assert res["work"]["video"] == "Work Title.mp4" and res["work"]["base"] == "Work Title"

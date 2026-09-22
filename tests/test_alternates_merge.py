"""Fallback links on a video item, and sending a post INTO an existing
library work (merge_into)."""
from pathlib import Path

from funpairdl.core.pair import FileType, ItemState, PairItem
from funpairdl.core.queue_manager import QueueManager


def _qm(tmp_path):
    (tmp_path / "dl").mkdir()
    return QueueManager(download_dir=tmp_path / "dl")


class TestAlternates:
    def test_add_pair_records_alternates_on_the_chosen_video(self, tmp_path):
        qm = _qm(tmp_path)
        pair = qm.add_pair(name="Work", groups=[{
            "name": "Main",
            "video_urls": ["https://pixeldrain.com/u/aaaa1111"],
            "script_urls": ["https://h/s.funscript"],
            "filenames": {"https://pixeldrain.com/u/aaaa1111": "Work.mp4"},
            "alternates": {"https://pixeldrain.com/u/aaaa1111": [
                "https://mega.nz/file/x#y", "https://pixeldrain.com/u/aaaa1111"]},
        }])
        v = next(i for i in pair.items if i.file_type == FileType.VIDEO)
        assert v.alternates == ["https://mega.nz/file/x#y"]     # self dropped
        assert v.tried_urls == []
        d = v.to_dict()
        back = PairItem.from_dict(d)
        assert back.alternates == ["https://mega.nz/file/x#y"]

    def test_switch_to_alternate_after_plain_retries(self):
        it = PairItem(url="https://pixeldrain.com/u/aaaa1111", filename="Work.mp4", file_type=FileType.VIDEO)
        it.alternates = ["https://mega.nz/file/x#y", "https://h/c.mp4"]
        it.state = ItemState.FAILED
        it.error_message = "403"
        # Rounds 1 and 2 are the ordinary retries of the same url.
        assert QueueManager._switch_to_alternate(it, 1) is False
        assert QueueManager._switch_to_alternate(it, 2) is False
        assert it.url == "https://pixeldrain.com/u/aaaa1111"
        assert QueueManager._switch_to_alternate(it, 3) is True
        assert it.url == "https://mega.nz/file/x#y" and it.provider_name == "mega"
        assert it.tried_urls == ["https://pixeldrain.com/u/aaaa1111"]
        assert it.alternates == ["https://h/c.mp4"]
        assert QueueManager._switch_to_alternate(it, 4) is True
        assert it.url == "https://h/c.mp4"
        assert QueueManager._switch_to_alternate(it, 5) is False


class TestMergeInto:
    def test_merge_into_a_library_work_folder(self, tmp_path):
        qm = _qm(tmp_path)
        work = tmp_path / "dl" / "Work Title"
        work.mkdir()
        (work / "Work Title.mp4").write_bytes(b"v")
        pair = qm.add_pair(name="Work Title (Requested)", script_urls=["https://h/s.funscript"],
                           merge_into=str(work))
        assert Path(pair.output_dir) == work.resolve()

    def test_merge_into_no_video_work(self, tmp_path):
        qm = _qm(tmp_path)
        work = tmp_path / "dl" / "No Video" / "Work Title"
        work.mkdir(parents=True)
        pair = qm.add_pair(name="Work Title", script_urls=["https://h/s.funscript"],
                           merge_into=str(work))
        assert Path(pair.output_dir) == work.resolve()

    def test_merge_into_outside_the_library_is_ignored(self, tmp_path):
        qm = _qm(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        pair = qm.add_pair(name="Work Title", script_urls=["https://h/s.funscript"],
                           merge_into=str(elsewhere))
        assert Path(pair.output_dir) == tmp_path / "dl" / "Work Title"

    def test_merge_into_missing_folder_is_ignored(self, tmp_path):
        qm = _qm(tmp_path)
        pair = qm.add_pair(name="Work Title", script_urls=["https://h/s.funscript"],
                           merge_into=str(tmp_path / "dl" / "gone"))
        assert Path(pair.output_dir) == tmp_path / "dl" / "Work Title"

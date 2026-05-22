"""Tests for funpairdl.core.pair data models."""
from funpairdl.core.pair import (
    FileType,
    ItemState,
    Pair,
    PairItem,
    PairState,
)


class TestPairItem:
    def test_author_field_default(self):
        item = PairItem(url="http://x.com/f.funscript", filename="f.funscript", file_type=FileType.FUNSCRIPT)
        assert item.author == ""

    def test_author_field_set(self):
        item = PairItem(
            url="http://x.com/f.funscript", filename="f.funscript",
            file_type=FileType.FUNSCRIPT, author="TestAuthor",
        )
        assert item.author == "TestAuthor"

    def test_to_dict_includes_author(self):
        item = PairItem(
            url="http://x.com/f.funscript", filename="f.funscript",
            file_type=FileType.FUNSCRIPT, author="Alice",
        )
        d = item.to_dict()
        assert d["author"] == "Alice"

    def test_from_dict_with_author(self):
        d = {
            "url": "http://x.com/f.funscript",
            "filename": "f.funscript",
            "file_type": "funscript",
            "author": "Bob",
        }
        item = PairItem.from_dict(d)
        assert item.author == "Bob"

    def test_from_dict_without_author(self):
        """Backward compat: old data without author field."""
        d = {
            "url": "http://x.com/f.funscript",
            "filename": "f.funscript",
            "file_type": "funscript",
        }
        item = PairItem.from_dict(d)
        assert item.author == ""

    def test_progress_zero_total(self):
        item = PairItem(url="http://x.com/v.mp4", filename="v.mp4", file_type=FileType.VIDEO)
        assert item.progress == 0.0

    def test_progress_partial(self):
        item = PairItem(
            url="http://x.com/v.mp4", filename="v.mp4",
            file_type=FileType.VIDEO, total_bytes=1000, downloaded_bytes=500,
        )
        assert item.progress == 50.0

    def test_roundtrip_serialization(self):
        item = PairItem(
            url="http://x.com/f.funscript", filename="f.funscript",
            file_type=FileType.FUNSCRIPT, author="Charlie", is_bundle=True,
        )
        d = item.to_dict()
        restored = PairItem.from_dict(d)
        assert restored.author == "Charlie"
        assert restored.url == item.url
        assert restored.filename == item.filename


class TestPair:
    def test_script_items_filter(self):
        pair = Pair(name="Test")
        pair.items = [
            PairItem(url="http://x.com/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x.com/a.funscript", filename="a.funscript", file_type=FileType.FUNSCRIPT, author="A"),
            PairItem(url="http://x.com/b.funscript", filename="b.funscript", file_type=FileType.FUNSCRIPT, author="B"),
        ]
        assert len(pair.script_items) == 2
        assert len(pair.video_items) == 1

    def test_pair_roundtrip(self):
        pair = Pair(name="Test Pair")
        pair.items = [
            PairItem(url="http://x.com/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x.com/f.funscript", filename="f.funscript", file_type=FileType.FUNSCRIPT, author="X"),
        ]
        d = pair.to_dict()
        restored = Pair.from_dict(d)
        assert restored.name == "Test Pair"
        assert len(restored.items) == 2
        assert restored.items[1].author == "X"

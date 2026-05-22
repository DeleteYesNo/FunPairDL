"""Tests for QueueManager.add_pair with script_authors."""
from funpairdl.core.pair import FileType
from funpairdl.core.queue_manager import QueueManager


class TestAddPairAuthors:
    def test_add_pair_with_script_authors(self):
        qm = QueueManager()
        pair = qm.add_pair(
            name="Test",
            video_urls=["http://x.com/v.mp4"],
            script_urls=["http://x.com/a.funscript", "http://x.com/b.funscript"],
            script_authors={
                "http://x.com/a.funscript": "Alice",
                "http://x.com/b.funscript": "Bob",
            },
        )
        scripts = pair.script_items
        assert len(scripts) == 2
        assert scripts[0].author == "Alice"
        assert scripts[1].author == "Bob"

    def test_add_pair_without_script_authors(self):
        qm = QueueManager()
        pair = qm.add_pair(
            name="Test",
            video_urls=["http://x.com/v.mp4"],
            script_urls=["http://x.com/s.funscript"],
        )
        scripts = pair.script_items
        assert len(scripts) == 1
        assert scripts[0].author == ""

    def test_add_pair_partial_authors(self):
        """Some scripts have authors, some don't."""
        qm = QueueManager()
        pair = qm.add_pair(
            name="Test",
            video_urls=[],
            script_urls=["http://x.com/a.funscript", "http://x.com/b.funscript"],
            script_authors={"http://x.com/a.funscript": "OnlyA"},
        )
        scripts = pair.script_items
        assert scripts[0].author == "OnlyA"
        assert scripts[1].author == ""

    def test_author_ordering_preserved(self):
        """Authors should appear in the order they're added."""
        qm = QueueManager()
        pair = qm.add_pair(
            name="Order Test",
            video_urls=["http://x.com/v.mp4"],
            script_urls=[
                "http://x.com/first.funscript",
                "http://x.com/second.funscript",
                "http://x.com/third.funscript",
            ],
            script_authors={
                "http://x.com/first.funscript": "Alpha",
                "http://x.com/second.funscript": "Beta",
                "http://x.com/third.funscript": "Gamma",
            },
        )
        authors = [s.author for s in pair.script_items]
        assert authors == ["Alpha", "Beta", "Gamma"]


class TestCleanTitle:
    def test_bundle_url_detection(self):
        assert QueueManager._is_bundle_url("https://pixeldrain.com/l/abc123")
        # /d/ is a filesystem folder (may hold per-pack subfolders) — a bundle.
        assert QueueManager._is_bundle_url("https://pixeldrain.com/d/6tpQwDwA")
        assert QueueManager._is_bundle_url("https://mega.nz/folder/abc#key")
        assert not QueueManager._is_bundle_url("https://pixeldrain.com/u/abc123")
        assert not QueueManager._is_bundle_url("https://mega.nz/file/abc#key")
        # A single file *within* a folder is not a bundle — it must not be
        # re-expanded once resolved, or it would loop on the folder URL.
        assert not QueueManager._is_bundle_url(
            "https://mega.nz/folder/abc#key/file/FILEHANDLE"
        )

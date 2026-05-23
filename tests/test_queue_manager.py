"""Tests for QueueManager.add_pair with script_authors."""
from pathlib import Path

from funpairdl.core.pair import FileType, Pair, PairItem
from funpairdl.core.queue_manager import QueueManager


def _vi(name, ftype):
    return PairItem(url="u/" + name, filename=name, file_type=ftype)


class TestAutoSplitBundlePair:
    def test_splits_distinct_works(self):
        # A folder of distinct scenes (each video + its script) must split
        # into one pair per work — NOT collapse into one pair with the
        # extra scripts promoted to .alt variants.
        names = ["[Gweda] Shenhe", "[Teamboobs]Shenhe", "[simao] Shenhe"]
        items = []
        for n in names:
            items.append(_vi(n + ".mp4", FileType.VIDEO))
            items.append(_vi(n + ".funscript", FileType.FUNSCRIPT))
        result = QueueManager()._auto_split_bundle_pair(Pair(name="Pack", items=items))
        assert result is not None
        assert len(result) == 3
        for p in result:
            assert sum(1 for i in p.items if i.file_type == FileType.VIDEO) == 1
            assert sum(1 for i in p.items if i.file_type == FileType.FUNSCRIPT) == 1

    def test_mirror_videos_not_split(self):
        # Same work mirrored on two hosts (identical name) is one pair.
        items = [
            _vi("Shenhe.mp4", FileType.VIDEO),
            _vi("Shenhe.mp4", FileType.VIDEO),
            _vi("Shenhe.funscript", FileType.FUNSCRIPT),
        ]
        assert QueueManager()._auto_split_bundle_pair(Pair(name="Shenhe", items=items)) is None

    def test_single_video_not_split(self):
        items = [
            _vi("A.mp4", FileType.VIDEO),
            _vi("A.funscript", FileType.FUNSCRIPT),
            _vi("A.pitch.funscript", FileType.FUNSCRIPT),
        ]
        assert QueueManager()._auto_split_bundle_pair(Pair(name="A", items=items)) is None

    def test_real_names_pair_one_to_one(self):
        # Regression for the AishaBunny bundle bug: a list of N works, each a
        # video + its same-named script. With real filenames every script must
        # attach to ITS OWN video. The bug (items carried only URL-code names,
        # so no stem matched) dumped all scripts onto the first pair as
        # .alt/.alt1/... variants and named pairs after random file ids.
        works = [
            "Aisha Bunny - Wild asian babe lets me cum",
            "Aisha Bunny - 18 Years Old Sexy Fit Asian Model",
            "Aisha Bunny - Fit Japanese Hottie Cant Stop Riding",
        ]
        items = []
        for w in works:
            items.append(_vi(w + ".mp4", FileType.VIDEO))
            items.append(_vi(w + ".funscript", FileType.FUNSCRIPT))
        result = QueueManager()._auto_split_bundle_pair(Pair(name="Collection", items=items))
        assert result is not None
        assert len(result) == 3
        for p in result:
            vids = [i for i in p.items if i.file_type == FileType.VIDEO]
            scrs = [i for i in p.items if i.file_type == FileType.FUNSCRIPT]
            assert len(vids) == 1
            assert len(scrs) == 1                       # no alt pile-up
            # script paired with the matching work, pair named by the real stem
            assert Path(vids[0].filename).stem == Path(scrs[0].filename).stem
            assert p.name == Path(vids[0].filename).stem


class TestBundleFilenames:
    """A pre-expanded bundle sends file-locker URLs whose path is a random id;
    the extension supplies the real names via `filenames` so the backend names
    items correctly instead of guessing the id."""

    def test_grouped_uses_provided_filenames(self):
        qm = QueueManager()
        pair = qm.add_pair(
            name="Collection",
            groups=[{
                "name": "Main",
                "video_urls": ["https://pixeldrain.com/u/787M6f9b"],
                "script_urls": ["https://pixeldrain.com/u/fu4erZE8"],
                "filenames": {
                    "https://pixeldrain.com/u/787M6f9b": "Aisha Bunny - Wild.mp4",
                    "https://pixeldrain.com/u/fu4erZE8": "Aisha Bunny - Wild.funscript",
                },
            }],
        )
        vids = [i for i in pair.items if i.file_type == FileType.VIDEO]
        scrs = [i for i in pair.items if i.file_type == FileType.FUNSCRIPT]
        assert vids[0].filename == "Aisha Bunny - Wild.mp4"
        assert scrs[0].filename == "Aisha Bunny - Wild.funscript"

    def test_falls_back_to_url_guess_without_filenames(self):
        qm = QueueManager()
        pair = qm.add_pair(
            name="X",
            groups=[{"name": "Main", "video_urls": ["https://pixeldrain.com/u/787M6f9b"]}],
        )
        vids = [i for i in pair.items if i.file_type == FileType.VIDEO]
        assert vids[0].filename == "787M6f9b"  # _guess_filename → URL tail

    def test_legacy_flat_list_uses_filenames(self):
        qm = QueueManager()
        pair = qm.add_pair(
            name="X",
            video_urls=["https://pixeldrain.com/u/abc"],
            filenames={"https://pixeldrain.com/u/abc": "Real Name.mp4"},
        )
        vids = [i for i in pair.items if i.file_type == FileType.VIDEO]
        assert vids[0].filename == "Real Name.mp4"

    def test_provided_filename_cannot_escape_folder(self):
        # A web-supplied name is used directly as the on-disk path, so a
        # traversal attempt must be sanitized away (no separators / no `..`
        # that resolves outside the download dir).
        qm = QueueManager()
        evil = r"..\..\..\Users\Public\Startup\evil.lnk"
        pair = qm.add_pair(
            name="X",
            groups=[{
                "name": "Main",
                "video_urls": ["https://pixeldrain.com/u/abc"],
                "filenames": {"https://pixeldrain.com/u/abc": evil},
            }],
        )
        fn = pair.items[0].filename
        assert "/" not in fn and "\\" not in fn
        # the sanitized name must stay inside the output dir
        resolved = (Path(pair.output_dir) / fn).resolve()
        assert Path(pair.output_dir).resolve() in resolved.parents


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

"""Tests for QueueManager.add_pair with script_authors."""
import asyncio
import threading
from pathlib import Path

from funpairdl.core.pair import (
    FileType, ItemState, Pair, PairItem, PairState, SegmentInfo,
)
from funpairdl.core.queue_manager import QueueManager


def _vi(name, ftype):
    return PairItem(url="u/" + name, filename=name, file_type=ftype)


def _completed_pair(name):
    item = _vi(f"{name}.mp4", FileType.VIDEO)
    item.state = ItemState.COMPLETED
    item.segments = [SegmentInfo(index=0, range_start=0, range_end=9,
                                 downloaded=10, temp_file="x.part0")]
    pair = Pair(name=name, items=[item])
    pair.state = PairState.COMPLETED
    return pair


class TestPlanBundleSplit:
    """plan_bundle_split is the preview the panel shows AND the split the
    queue performs; a user plan (url → label) overrides the name heuristic."""

    def _items(self):
        items = []
        for n in ["Alpha", "Beta", "Gamma"]:
            items.append(_vi(n + ".mp4", FileType.VIDEO))
            items.append(_vi(n + ".funscript", FileType.FUNSCRIPT))
        return items

    def test_preview_without_plan_matches_heuristic_split(self):
        qm = QueueManager()
        groups = qm.plan_bundle_split(self._items(), None, "Pack")
        assert groups is not None and len(groups) == 3
        assert all(len(g["videos"]) == 1 and len(g["scripts"]) == 1 for g in groups)
        assert all(g["label"] == "" for g in groups)
        split = qm._auto_split_bundle_pair(Pair(name="Pack", items=self._items()))
        assert [p.name for p in split] == [g["name"] for g in groups]

    def test_plan_moves_files_into_labelled_groups(self):
        items = self._items()
        # The user says Gamma's script really belongs with Beta's video, and
        # names that group; Alpha is left to the heuristic.
        plan = {"u/Beta.mp4": "Beta Special", "u/Gamma.funscript": "Beta Special",
                "u/Gamma.mp4": "Gamma solo"}
        groups = QueueManager().plan_bundle_split(items, plan, "Pack")
        by_label = {g["label"]: g for g in groups}
        assert set(by_label) == {"", "Beta Special", "Gamma solo"}
        assert [i.filename for i in by_label["Beta Special"]["videos"]] == ["Beta.mp4"]
        assert [i.filename for i in by_label["Beta Special"]["scripts"]] == ["Gamma.funscript"]
        assert by_label["Beta Special"]["name"] == "Beta Special"
        assert [i.filename for i in by_label["Gamma solo"]["videos"]] == ["Gamma.mp4"]
        # Alpha: heuristic pair. Beta's own script lost its video to the plan
        # and, being unmatched, rides along with the first group.
        alpha = by_label[""]
        assert [i.filename for i in alpha["videos"]] == ["Alpha.mp4"]
        assert sorted(i.filename for i in alpha["scripts"]) == ["Alpha.funscript", "Beta.funscript"]

    def test_mirror_on_a_file_host_is_not_split_from_the_source_page(self):
        # The same work: the iwara page (descriptive slug) and a pixeldrain
        # re-upload whose filename carries a site prefix and bracket tags.
        iwara = PairItem(url="https://www.iwara.tv/video/abc123def/work-title-part-4",
                         filename="Work Title Part 4", file_type=FileType.VIDEO)
        pd = PairItem(url="https://pixeldrain.com/u/6qYDBQUm",
                      filename="Iwara - Work Title Part 4 [abc123def] [Source].mp4",
                      file_type=FileType.VIDEO)
        scripts = [_vi("Iwara - Work Title Part 4 abc123def Source.funscript", FileType.FUNSCRIPT),
                   _vi("Iwara - Work Title Part 4 abc123def Source.pitch.funscript", FileType.FUNSCRIPT)]
        assert QueueManager()._mirror_key(pd.filename) == QueueManager()._mirror_key("work-title-part-4")
        assert QueueManager().plan_bundle_split([iwara, pd, *scripts], None, "T") is None
        # A genuinely different part is still split.
        other = PairItem(url="https://www.iwara.tv/video/zzz/work-title-part-5",
                         filename="Work Title Part 5", file_type=FileType.VIDEO)
        assert QueueManager().plan_bundle_split([iwara, pd, other], None, "T") is not None

    def test_hints_pair_scene_named_scripts_with_tagged_posts(self):
        # Four booru posts whose titles are alike (two even identical), and
        # scripts named after the scene. Only the posts' tags can tell.
        def vid(n, url):
            return PairItem(url=url, filename=n, file_type=FileType.VIDEO)
        videos = [
            vid("alpha and beta (game) created by author", "https://e621.net/posts/1"),
            vid("alpha and beta (game) created by author", "https://e621.net/posts/2"),
            vid("gamma fox (game) created by author", "https://e621.net/posts/3"),
            vid("gamma fox (game) created by author", "https://e621.net/posts/4"),
        ]
        scripts = [
            _vi("author - fox dom blowjob.funscript", FileType.FUNSCRIPT),
            _vi("author - fox kneeling blowjob.funscript", FileType.FUNSCRIPT),
            _vi("author - fox reverse cowgirl.funscript", FileType.FUNSCRIPT),
            _vi("author - fox shower.funscript", FileType.FUNSCRIPT),
        ]
        hints = {
            "https://e621.net/posts/1": "fellatio dominant_female sofa_sex sex",
            "https://e621.net/posts/2": "fellatio kneeling_oral_position standing sex",
            "https://e621.net/posts/3": "reverse_cowgirl_position dominant_female sex",
            "https://e621.net/posts/4": "shower shower_sex from_behind_position sex",
        }
        groups = QueueManager().plan_bundle_split(videos + scripts, None, "T", hints=hints)
        assert groups is not None and len(groups) == 4
        # Same-titled posts on the same host are distinct works, and their
        # folders must not collide: the second of each pair carries its id.
        names = [g["name"] for g in groups]
        assert len(set(names)) == 4
        assert any(n.endswith("[2]") for n in names) and any(n.endswith("[4]") for n in names)
        by_video = {g["videos"][0].url: [s.filename for s in g["scripts"]] for g in groups}
        assert by_video["https://e621.net/posts/4"] == ["author - fox shower.funscript"]
        assert by_video["https://e621.net/posts/3"] == ["author - fox reverse cowgirl.funscript"]
        assert by_video["https://e621.net/posts/2"] == ["author - fox kneeling blowjob.funscript"]
        assert by_video["https://e621.net/posts/1"] == ["author - fox dom blowjob.funscript"]

    def test_duration_decides_when_names_and_tags_tie(self):
        # Two HMVs with tag-soup names that share every word; the scripts'
        # lengths pick them apart. A third video in range of nothing stays
        # unpaired → order rescue, flagged as a guess.
        v1 = PairItem(url="https://host.example/v/1", filename="hmv tags mix a.mp4", file_type=FileType.VIDEO)
        v2 = PairItem(url="https://host.example/v/2", filename="hmv tags mix b.mp4", file_type=FileType.VIDEO)
        v3 = PairItem(url="https://host.example/v/3", filename="hmv tags mix c.mp4", file_type=FileType.VIDEO)
        s1 = _vi("edit one.funscript", FileType.FUNSCRIPT)
        s2 = _vi("edit two.funscript", FileType.FUNSCRIPT)
        s3 = _vi("edit three.funscript", FileType.FUNSCRIPT)
        durations = {v1.url: 201.0, v2.url: 154.0, v3.url: 300.0,
                     s1.url: 199.5, s2.url: 153.2, s3.url: 240.0}
        groups = QueueManager().plan_bundle_split([v1, v2, v3, s1, s2, s3], None, "T", durations=durations)
        by_video = {g["videos"][0].url: g for g in groups}
        assert [s.filename for s in by_video[v1.url]["scripts"]] == ["edit one.funscript"]
        assert by_video[v1.url]["basis"] == "duration"
        assert [s.filename for s in by_video[v2.url]["scripts"]] == ["edit two.funscript"]
        assert [s.filename for s in by_video[v3.url]["scripts"]] == ["edit three.funscript"]
        assert by_video[v3.url]["basis"] == "order"
        assert by_video[v3.url]["script_basis"][s3.url] == "order"

    def test_duration_declines_when_two_videos_are_in_range(self):
        v1 = PairItem(url="https://host.example/v/1", filename="loop a.mp4", file_type=FileType.VIDEO)
        v2 = PairItem(url="https://host.example/v/2", filename="loop b.mp4", file_type=FileType.VIDEO)
        s = _vi("thing.funscript", FileType.FUNSCRIPT)
        durations = {v1.url: 30.0, v2.url: 31.0, s.url: 30.5}
        groups = QueueManager().plan_bundle_split([v1, v2, s], None, "T", durations=durations)
        # Undecidable by duration → placed by document order and marked so.
        g = next(g for g in groups if g["scripts"])
        assert g["basis"] == "order"

    def test_script_metadata_link_names_its_video(self):
        v1 = PairItem(url="https://host.example/v/1", filename="x.mp4", file_type=FileType.VIDEO)
        v2 = PairItem(url="https://host.example/v/2", filename="y.mp4", file_type=FileType.VIDEO)
        s = _vi("unrelated name.funscript", FileType.FUNSCRIPT)
        groups = QueueManager().plan_bundle_split(
            [v1, v2, s], None, "T", links={s.url: "https://host.example/v/2/"})
        g = next(g for g in groups if g["scripts"])
        assert g["videos"][0] is v2 and g["basis"] == "link"

    def test_name_match_reports_name_basis(self):
        groups = QueueManager().plan_bundle_split(self._items(), None, "Pack")
        assert all(g["basis"] == "name" for g in groups)

    def test_single_video_is_never_split_by_stale_labels(self):
        # The preview saw two encodes and labelled every row; the user then
        # unchecked one video. One video + its script must stay one work.
        v = PairItem(url="https://pixeldrain.com/u/abc", filename="Show-P4-RF35.mkv", file_type=FileType.VIDEO)
        s = _vi("Show.funscript", FileType.FUNSCRIPT)
        plan = {v.url: "Show-P4-RF35", s.url: "Show"}
        assert QueueManager().plan_bundle_split([v, s], plan, "Show") is None

    def test_two_encodes_of_one_work_are_mirrors(self):
        # Same title, one carrying encode tags, same length → one work, not two.
        a = PairItem(url="https://pixeldrain.com/u/aaa", filename="[Author] Show.mkv", file_type=FileType.VIDEO)
        b = PairItem(url="https://pixeldrain.com/u/bbb", filename="[Author] Show-P4-RF35.mkv", file_type=FileType.VIDEO)
        s = _vi("[Author] Show.funscript", FileType.FUNSCRIPT)
        qm = QueueManager()
        assert qm.plan_bundle_split([a, b, s], None, "T", durations={a.url: 183.7, b.url: 183.6}) is None
        # Different lengths → genuinely different videos → split.
        assert qm.plan_bundle_split([a, b, s], None, "T", durations={a.url: 183.7, b.url: 240.0}) is not None
        # Codec / rate-factor tokens alone never distinguish works.
        assert qm._match_key("Show x265 RF35 10bit") == qm._match_key("Show")

    def test_plan_with_two_labels_splits_even_a_mirror_set(self):
        # Same-named videos are normally kept as one pair (mirrors); an
        # explicit two-label plan says they are distinct works.
        items = [_vi("Clip.mp4", FileType.VIDEO), _vi("Clip.mp4", FileType.VIDEO)]
        items[1].url = "u/other/Clip.mp4"
        assert QueueManager().plan_bundle_split(items, None, "P") is None
        plan = {"u/Clip.mp4": "One", "u/other/Clip.mp4": "Two"}
        groups = QueueManager().plan_bundle_split(items, plan, "P")
        assert [g["label"] for g in groups] == ["One", "Two"]

    def test_pair_bundle_plan_round_trips_and_drives_the_split(self):
        pair = Pair(name="Pack", items=self._items())
        pair.bundle_plan = {"u/Alpha.mp4": "Custom", "u/Alpha.funscript": "Custom"}
        assert Pair.from_dict(pair.to_dict()).bundle_plan == pair.bundle_plan
        split = QueueManager()._auto_split_bundle_pair(pair)
        assert "Custom" in [p.name for p in split]
        custom = next(p for p in split if p.name == "Custom")
        assert sorted(i.filename for i in custom.items) == ["Alpha.funscript", "Alpha.mp4"]


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

    def test_split_names_use_display_name_and_op_title(self):
        # OP (Main) + two comment Alts, each a distinct work. The videos carry
        # iwara URL slugs as filenames (what _identity picks for matching), but
        # the split children must be NAMED from the post title (OP) and each
        # Alt's display_name — not from the slug.
        from funpairdl.utils.filename import sanitize_filename

        def norm(s):
            return sanitize_filename(QueueManager._clean_title(s))

        def gi(name, ftype, group):
            it = _vi(name, ftype)
            it.group = group
            return it

        items = [
            gi("demo-game-mock-battle-2.mp4", FileType.VIDEO, "Main"),
            gi("demo-game-mock-battle-2.funscript", FileType.FUNSCRIPT, "Main"),
            gi("sample-author-demo-knight-segs.mp4", FileType.VIDEO, "Alt 1"),
            gi("sample-author-demo-knight-segs.funscript", FileType.FUNSCRIPT, "Alt 1"),
            gi("x9zk.mp4", FileType.VIDEO, "Alt 3"),
            gi("x9zk.funscript", FileType.FUNSCRIPT, "Alt 3"),
        ]
        pair = Pair(name="[Author] Post Title 2", items=items)
        pair.alt_group_config = {
            "Alt 1": {"display_name": "Sample Author - Demo Knight Segs"},
            "Alt 3": {"display_name": "Distinct Alt Three"},
        }
        result = QueueManager()._auto_split_bundle_pair(pair)
        assert result is not None and len(result) == 3
        names = {p.name for p in result}
        assert norm("[Author] Post Title 2") in names        # OP -> post title
        assert norm("Sample Author - Demo Knight Segs") in names  # Alt 1 display
        assert norm("Distinct Alt Three") in names            # Alt 3 display
        # URL slugs must NOT have been used to name any work
        assert norm("demo-game-mock-battle-2") not in names
        assert norm("x9zk") not in names
        assert norm("sample-author-demo-knight-segs") not in names

    def test_plain_bundle_without_alts_still_uses_stems(self):
        # No alt_group_config and many Main videos: nothing better than the
        # per-video stem is available, so behavior is unchanged.
        names = ["[Gweda] Shenhe", "[Teamboobs]Shenhe", "[simao] Shenhe"]
        items = []
        for n in names:
            items.append(_vi(n + ".mp4", FileType.VIDEO))
            items.append(_vi(n + ".funscript", FileType.FUNSCRIPT))
        result = QueueManager()._auto_split_bundle_pair(Pair(name="Pack", items=items))
        assert result is not None and len(result) == 3
        # "Pack" (the bundle title) must NOT have leaked onto any child, since
        # there are multiple Main videos (no single OP).
        assert all(p.name != "Pack" for p in result)

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


    def test_matches_across_prefix_and_resolution(self):
        # Real bundles give scripts a "(CHARACTER)" prefix and/or a different
        # resolution tag than the video. Matching must still pair them with the
        # right work — the GreenTea bug orphaned the video and dumped the script
        # onto the first pair as a .alt because it required an exact prefix.
        items = [
            # Decoy with the LONGEST name — unmatched scripts get dumped on the
            # first (longest) pair, so if matching were broken the selune script
            # would land here instead of on its real video. It must not.
            _vi("a-very-long-unrelated-decoy-scene-title-here_2160p.mp4", FileType.VIDEO),
            _vi("a-very-long-unrelated-decoy-scene-title-here_2160p.funscript", FileType.FUNSCRIPT),
            _vi("blessings-of-selune-nyl2_2160p.mp4", FileType.VIDEO),
            _vi("DraeNelf_4Some.mp4", FileType.VIDEO),
            _vi("(SHADOWHEART)blessings-of-selune-nyl2_1080p.funscript", FileType.FUNSCRIPT),
            _vi("(Left)DraeNelf_4Some.funscript", FileType.FUNSCRIPT),
        ]
        result = QueueManager()._auto_split_bundle_pair(Pair(name="Vault", items=items))
        assert result is not None and len(result) == 3
        by = {p.name: p for p in result}
        # the selune script matched its video, not the longest decoy pair
        decoy = by["a-very-long-unrelated-decoy-scene-title-here_2160p"]
        assert all("selune" not in i.filename.lower() for i in decoy.items)
        bless = by["blessings-of-selune-nyl2_2160p"]
        drae = by["DraeNelf_4Some"]
        assert any(i.file_type == FileType.FUNSCRIPT and "selune" in i.filename.lower()
                   for i in bless.items)
        assert any(i.file_type == FileType.FUNSCRIPT and "draenelf" in i.filename.lower()
                   for i in drae.items)
        for p in result:  # exactly 1V + 1S each, no orphan, no pileup
            assert sum(1 for i in p.items if i.file_type == FileType.VIDEO) == 1
            assert sum(1 for i in p.items if i.file_type == FileType.FUNSCRIPT) == 1


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


class TestRequeueFailedPair:
    def test_requeue_failed_applies_new_resolution(self):
        # Re-adding a failed work must adopt the new submission's
        # preferred_resolution — e.g. switching to "best" after a bilibili
        # "format not available" failure — not silently keep the old value.
        qm = QueueManager()
        p1 = qm.add_pair(
            name="VKWork",
            video_urls=["https://m.vk.com/video-1_2"],
            preferred_resolution="1080",
        )
        p1.state = PairState.FAILED
        for i in p1.items:
            i.state = ItemState.FAILED
            i.error_message = "boom"

        p2 = qm.add_pair(
            name="VKWork",
            video_urls=["https://m.vk.com/video-1_2"],
            preferred_resolution="best",
        )
        assert p2 is p1                            # reused, not duplicated
        assert p1.preferred_resolution == "best"   # new pref applied
        assert p1.state == PairState.QUEUED
        assert all(i.state == ItemState.PENDING for i in p1.items)


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


class TestResumePair:
    """Resuming must re-queue the pair so the pump restarts it. Pausing
    cancels the live download coroutine, so resume can't rely on waking a
    paused coroutine — it has to reset items to PENDING + mark QUEUED."""

    def _qm_with_pair(self, pair_state, item_states):
        qm = QueueManager()
        items = [_vi(f"f{i}.mp4", FileType.VIDEO) for i in range(len(item_states))]
        for it, st in zip(items, item_states):
            it.state = st
        pair = Pair(name="P", items=items)
        pair.state = pair_state
        qm.pairs.append(pair)
        return qm, pair

    def test_paused_pair_requeues(self):
        qm, pair = self._qm_with_pair(
            PairState.PAUSED, [ItemState.PAUSED, ItemState.PAUSED]
        )
        qm.resume_pair(pair.id)
        assert pair.state == PairState.QUEUED
        assert all(i.state == ItemState.PENDING for i in pair.items)

    def test_stuck_downloading_with_paused_items_recovers(self):
        # An earlier buggy resume could leave the pair DOWNLOADING while its
        # items stayed PAUSED with no task running. Resume must rescue it.
        qm, pair = self._qm_with_pair(
            PairState.DOWNLOADING, [ItemState.PAUSED, ItemState.COMPLETED]
        )
        qm.resume_pair(pair.id)
        assert pair.state == PairState.QUEUED
        assert pair.items[0].state == ItemState.PENDING
        assert pair.items[1].state == ItemState.COMPLETED  # untouched

    def test_failed_pair_requeues(self):
        qm, pair = self._qm_with_pair(
            PairState.FAILED, [ItemState.FAILED, ItemState.COMPLETED]
        )
        qm.resume_pair(pair.id)
        assert pair.state == PairState.QUEUED
        assert pair.items[0].state == ItemState.PENDING
        assert pair.items[1].state == ItemState.COMPLETED

    def test_completed_pair_is_noop(self):
        qm, pair = self._qm_with_pair(
            PairState.COMPLETED, [ItemState.COMPLETED]
        )
        qm.resume_pair(pair.id)
        assert pair.state == PairState.COMPLETED


class TestRetention:
    """archive_completed / clear_completed — the retention policy that keeps
    the live queue (and every save/rebuild) small."""

    def _counted_qm(self):
        qm = QueueManager()
        calls = {"queue_changed": 0, "save": 0}
        qm.on_queue_changed = lambda: calls.__setitem__(
            "queue_changed", calls["queue_changed"] + 1)
        qm.on_save_needed = lambda: calls.__setitem__("save", calls["save"] + 1)
        return qm, calls

    def test_archive_completed_keeps_newest(self):
        qm, calls = self._counted_qm()
        completed = [_completed_pair(f"C{i}") for i in range(5)]
        queued = Pair(name="Q", items=[_vi("q.mp4", FileType.VIDEO)])
        qm.pairs = completed[:3] + [queued] + completed[3:]
        archived_batches = []
        qm.archive_sink = archived_batches.append

        n = qm.archive_completed(keep=2)

        assert n == 3
        # Newest completed (list tail) survive; queued pair untouched.
        assert qm.pairs == [queued, completed[3], completed[4]]
        # Archived dicts are the 3 OLDEST completed, segments stripped.
        assert len(archived_batches) == 1
        batch = archived_batches[0]
        assert [d["name"] for d in batch] == ["C0", "C1", "C2"]
        for d in batch:
            assert all(it["segments"] == [] for it in d["items"])
        # One queue-changed + one save for the whole batch.
        assert calls == {"queue_changed": 1, "save": 1}

    def test_archive_completed_noop_under_keep(self):
        qm, calls = self._counted_qm()
        qm.pairs = [_completed_pair("A"), _completed_pair("B")]
        qm.archive_sink = lambda batch: (_ for _ in ()).throw(
            AssertionError("sink must not be called"))
        assert qm.archive_completed(keep=2) == 0
        assert len(qm.pairs) == 2
        assert calls == {"queue_changed": 0, "save": 0}

    def test_archive_sink_failure_keeps_pairs_live(self):
        qm, calls = self._counted_qm()
        qm.pairs = [_completed_pair(f"C{i}") for i in range(3)]

        def _boom(batch):
            raise OSError("disk full")

        qm.archive_sink = _boom
        assert qm.archive_completed(keep=0) == 0
        assert len(qm.pairs) == 3          # nothing dropped on sink failure
        assert calls == {"queue_changed": 0, "save": 0}

    def test_clear_completed_removes_all_in_one_batch(self):
        qm, calls = self._counted_qm()
        queued = Pair(name="Q", items=[_vi("q.mp4", FileType.VIDEO)])
        qm.pairs = [_completed_pair("A"), queued, _completed_pair("B")]
        archived_batches = []
        qm.archive_sink = archived_batches.append

        assert qm.clear_completed() == 2
        assert qm.pairs == [queued]
        assert len(archived_batches) == 1
        assert [d["name"] for d in archived_batches[0]] == ["A", "B"]
        # ONE queue-changed + ONE save — not one per removed pair.
        assert calls == {"queue_changed": 1, "save": 1}

    def test_clear_completed_empty_is_silent(self):
        qm, calls = self._counted_qm()
        qm.pairs = [Pair(name="Q", items=[_vi("q.mp4", FileType.VIDEO)])]
        assert qm.clear_completed() == 0
        assert calls == {"queue_changed": 0, "save": 0}

    def test_clear_completed_proceeds_on_sink_failure(self):
        # User explicitly asked to clear — a broken archive must not block it.
        qm, _ = self._counted_qm()
        qm.pairs = [_completed_pair("A")]

        def _boom(batch):
            raise OSError("disk full")

        qm.archive_sink = _boom
        assert qm.clear_completed() == 1
        assert qm.pairs == []


class TestSnapshotDicts:
    def test_snapshot_returns_pair_dicts(self):
        qm = QueueManager()
        qm.pairs = [_completed_pair("A")]
        snap = qm.snapshot_dicts()
        assert len(snap) == 1
        assert snap[0]["name"] == "A"
        assert snap[0]["state"] == "completed"

    def test_snapshot_thread_safety_smoke(self):
        # A mutator thread churns the queue (append + clear_completed) while
        # this thread snapshots continuously. Every snapshot must be a fully
        # serialized list — no exception, no torn pair.
        qm = QueueManager()
        stop = threading.Event()
        errors = []

        def _mutate():
            i = 0
            try:
                while not stop.is_set():
                    with qm._pairs_lock:
                        qm.pairs.append(_completed_pair(f"C{i}"))
                    if i % 5 == 0:
                        qm.clear_completed()
                    i += 1
            except Exception as e:  # pragma: no cover
                errors.append(e)

        t = threading.Thread(target=_mutate, daemon=True)
        t.start()
        try:
            for _ in range(300):
                snap = qm.snapshot_dicts()
                for d in snap:
                    assert "items" in d and "state" in d
        finally:
            stop.set()
            t.join(timeout=5)
        assert not errors


class TestGetQueueStatus:
    def test_status_strips_segments(self):
        qm = QueueManager()
        qm.pairs = [_completed_pair("A")]
        assert qm.pairs[0].items[0].segments  # in-memory records intact
        status = qm.get_queue_status()
        assert status[0]["items"][0]["segments"] == []
        # stripping must not mutate the live objects
        assert qm.pairs[0].items[0].segments


class TestAddPairSizes:
    """Probed sizes from the extension flow into total_bytes at add time so
    Size/ETA are visible before the pair ever wins a download slot."""

    def test_grouped_sizes_applied(self):
        qm = QueueManager()
        v = "https://pixeldrain.com/u/787M6f9b"
        s = "https://pixeldrain.com/u/fu4erZE8"
        pair = qm.add_pair(
            name="Sized",
            groups=[{
                "name": "Main",
                "video_urls": [v],
                "script_urls": [s],
                "sizes": {v: 123456789, s: 4321},
            }],
        )
        vids = [i for i in pair.items if i.file_type == FileType.VIDEO]
        scrs = [i for i in pair.items if i.file_type == FileType.FUNSCRIPT]
        assert vids[0].total_bytes == 123456789
        assert scrs[0].total_bytes == 4321

    def test_flat_sizes_applied(self):
        qm = QueueManager()
        url = "https://pixeldrain.com/u/abc"
        pair = qm.add_pair(name="X", video_urls=[url], sizes={url: 777})
        assert pair.items[0].total_bytes == 777

    def test_missing_or_zero_sizes_default_to_zero(self):
        qm = QueueManager()
        url = "https://pixeldrain.com/u/abc"
        pair = qm.add_pair(name="X", video_urls=[url], sizes={url: None})
        assert pair.items[0].total_bytes == 0
        pair2 = qm.add_pair(name="Y", video_urls=["https://pixeldrain.com/u/z"])
        assert pair2.items[0].total_bytes == 0


class TestOrganizeAsync:
    def _organized_call_tracker(self, qm):
        calls = []
        qm._organize_output = lambda pair: calls.append(("organize", pair.id))
        qm._undo_organize = lambda pair: calls.append(("undo", pair.id))
        return calls

    def test_organize_pair_async_runs_and_flags(self):
        qm = QueueManager()
        pair = _completed_pair("A")
        qm.pairs = [pair]
        calls = self._organized_call_tracker(qm)
        assert asyncio.run(qm.organize_pair_async(pair.id)) is True
        assert calls == [("organize", pair.id)]

    def test_organize_rejects_wrong_state(self):
        qm = QueueManager()
        pair = Pair(name="Q", items=[_vi("q.mp4", FileType.VIDEO)])
        qm.pairs = [pair]  # still QUEUED
        assert asyncio.run(qm.organize_pair_async(pair.id)) is False
        pair2 = _completed_pair("B")
        pair2.organized = True
        qm.pairs = [pair2]
        assert asyncio.run(qm.organize_pair_async(pair2.id)) is False

    def test_undo_requires_organized(self):
        qm = QueueManager()
        pair = _completed_pair("A")
        qm.pairs = [pair]
        assert asyncio.run(qm.undo_organize_pair_async(pair.id)) is False
        pair.organized = True
        calls = self._organized_call_tracker(qm)
        assert asyncio.run(qm.undo_organize_pair_async(pair.id)) is True
        assert calls == [("undo", pair.id)]

    def test_reorganize_chains_undo_then_organize(self):
        qm = QueueManager()
        pair = _completed_pair("A")
        pair.organized = True
        qm.pairs = [pair]
        calls = self._organized_call_tracker(qm)
        assert asyncio.run(qm.reorganize_pair_async(pair.id)) is True
        assert calls == [("undo", pair.id), ("organize", pair.id)]

    def test_busy_guard_rejects_concurrent_organize(self):
        qm = QueueManager()
        pair = _completed_pair("A")
        qm.pairs = [pair]
        assert qm._begin_organize(pair.id)  # someone else is organizing
        try:
            assert asyncio.run(qm.organize_pair_async(pair.id)) is False
            assert asyncio.run(qm.reorganize_pair_async(pair.id)) is False
        finally:
            qm._end_organize(pair.id)
        # guard released → works again
        self._organized_call_tracker(qm)
        assert asyncio.run(qm.organize_pair_async(pair.id)) is True


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


class TestOrganizeDedupOnRedownload:
    """A video-only pair makes reconcile bail (no script), so it falls to
    _organize_output. When the organized target name already holds a
    pre-existing copy, the rename is skipped — historically that left the
    fresh download as a duplicate. It must now be dropped iff byte-identical."""

    def _video_pair(self, out_dir, name, download_name):
        item = PairItem(url="u/" + download_name, filename=download_name,
                        file_type=FileType.VIDEO)
        item.state = ItemState.COMPLETED
        return Pair(name=name, items=[item], output_dir=str(out_dir))

    def _base(self, name):
        from funpairdl.utils.filename import sanitize_filename
        return sanitize_filename(QueueManager._clean_title(name))

    def test_drops_byte_identical_redownload(self, tmp_path):
        name = "AkoTest"
        base = self._base(name)
        existing = tmp_path / f"{base}.mp4"
        existing.write_bytes(b"VIDEO-BYTES" * 1000)
        dl = tmp_path / "ako-bunny_1080p.mp4"
        dl.write_bytes(existing.read_bytes())  # identical re-download
        pair = self._video_pair(tmp_path, name, "ako-bunny_1080p.mp4")

        QueueManager()._organize_output(pair)

        assert existing.exists()
        assert not dl.exists()                      # duplicate removed
        assert pair.items[0].filename == f"{base}.mp4"   # manifest -> survivor
        mp4s = list(tmp_path.glob("*.mp4"))
        assert len(mp4s) == 1

    def test_keeps_both_when_content_differs(self, tmp_path):
        name = "AkoTest"
        base = self._base(name)
        existing = tmp_path / f"{base}.mp4"
        existing.write_bytes(b"ORIGINAL-RENDER" * 1000)
        dl = tmp_path / "ako-bunny_1080p.mp4"
        dl.write_bytes(b"DIFFERENT-CHARACTER-VARIANT" * 1000)  # different bytes
        pair = self._video_pair(tmp_path, name, "ako-bunny_1080p.mp4")

        QueueManager()._organize_output(pair)

        assert existing.exists()
        assert dl.exists()                          # variant kept, not deleted
        assert len(list(tmp_path.glob("*.mp4"))) == 2

"""plan_videos: one download per video, fallbacks for its mirrors and
re-encodes, variants on their own, ambiguities surfaced."""
from funpairdl.core.video_plan import Prefs, VideoSpec, plan_videos


def _v(url, name="", source="OP", size=0, height=0, duration=0.0, priority=5.0, failed=False):
    return VideoSpec(url=url, name=name, source=source, size=size, height=height,
                     duration=duration, priority=priority, failed=failed)


def _group_of(res, url):
    return next(g for g in res["groups"] if url in g["members"])


class TestMirrorsAndReencodes:
    def test_smallest_qualifying_wins_and_others_are_fallbacks(self):
        res = plan_videos([
            _v("https://rule34video.com/video/1/work-title/", "work-title", height=1080, size=300_000_000),
            _v("https://pixeldrain.com/u/aaaa1111", "Work Title [4K].mp4", size=900_000_000),
            _v("https://pixeldrain.com/u/bbbb2222", "Work Title [1080p] small.mp4", size=90_000_000, source="comment"),
        ], Prefs(pick_mode="smallest", min_resolution="1080"))
        assert len(res["groups"]) == 1
        g = res["groups"][0]
        assert g["kind"] == "primary"
        assert g["chosen"] == "https://pixeldrain.com/u/bbbb2222"
        assert set(g["alternates"]) == {"https://rule34video.com/video/1/work-title/",
                                        "https://pixeldrain.com/u/aaaa1111"}
        assert res["roles"]["https://pixeldrain.com/u/bbbb2222"] == "chosen"
        assert res["roles"]["https://pixeldrain.com/u/aaaa1111"] == "alternate"
        assert res["ambiguous"] == []

    def test_below_floor_loses_to_a_bigger_qualifying_file(self):
        res = plan_videos([
            _v("https://h/a", "Work 720p.mp4", size=50, height=720),
            _v("https://h/b", "Work 1080p.mp4", size=200, height=1080),
        ], Prefs(pick_mode="smallest", min_resolution="1080"))
        assert res["groups"][0]["chosen"] == "https://h/b"

    def test_best_quality_prefers_height_then_size(self):
        res = plan_videos([
            _v("https://h/a", "Work 1080p.mp4", size=200, height=1080),
            _v("https://h/b", "Work 4K.mp4", size=900, height=2160),
            _v("https://h/c", "Work 4K.mp4", size=950, height=2160, source="comment"),
        ], Prefs(pick_mode="best_quality", min_resolution="best"))
        g = res["groups"][0]
        assert g["chosen"] == "https://h/c"
        assert g["alternates"] == ["https://h/b", "https://h/a"]

    def test_identical_names_on_two_hosts_are_mirrors(self):
        res = plan_videos([
            _v("https://pixeldrain.com/u/aaaa1111", "Work.mp4", size=100),
            _v("https://mega.nz/file/x#y", "Work.mp4", size=100),
        ])
        g = res["groups"][0]
        assert g["members"] == {"https://pixeldrain.com/u/aaaa1111": "chosen",
                                "https://mega.nz/file/x#y": "mirror"}


class TestVariants:
    def test_outfit_tags_are_variants_each_downloaded(self):
        res = plan_videos([
            _v("https://pixeldrain.com/u/aaaa1111", "[Auth] Work (nude).mp4", size=100),
            _v("https://pixeldrain.com/u/bbbb2222", "[Auth] Work (stockings).mp4", size=100),
        ])
        kinds = sorted((g["kind"], g["tag"]) for g in res["groups"])
        assert kinds == [("primary", ""), ("variant", "stockings")]
        assert res["roles"]["https://pixeldrain.com/u/bbbb2222"] == "variant"

    def test_other_character_in_a_comment_is_a_variant(self):
        res = plan_videos([
            _v("https://h/op", "Work Title.mp4", size=100, duration=120.0),
            _v("https://h/c", "Work Title Mockgan.mp4", size=100, duration=120.0, source="comment"),
        ])
        g = _group_of(res, "https://h/c")
        assert g["kind"] == "variant" and g["tag"].lower() == "mockgan"

    def test_different_length_is_a_different_cut(self):
        res = plan_videos([
            _v("https://h/op", "Work.mp4", duration=120.0),
            _v("https://h/c", "Work.mp4", duration=200.0, source="comment"),
        ])
        assert _group_of(res, "https://h/c")["kind"] == "variant"

    def test_variant_with_its_own_mirror(self):
        res = plan_videos([
            _v("https://h/op", "Work.mp4", size=100),
            _v("https://h/v1", "Work (nude).mp4", size=300),
            _v("https://h/v2", "Work (nude) small.mp4", size=100, source="comment"),
        ])
        g = _group_of(res, "https://h/v1")
        assert g["kind"] == "variant" and g["chosen"] == "https://h/v2"
        assert g["alternates"] == ["https://h/v1"]


class TestAmbiguousAndUnrelated:
    def test_version_word_is_ambiguous_and_defaults_to_reencode(self):
        res = plan_videos([
            _v("https://h/op", "Work.mp4", size=300),
            _v("https://h/c", "Work v2.mp4", size=100, source="comment"),
        ], Prefs(encode_vs_variant="ask"))
        assert [a["url"] for a in res["ambiguous"]] == ["https://h/c"]
        assert res["roles"]["https://h/c"] == "ambiguous"
        # Until answered it rides as a fallback of the primary.
        assert res["groups"][0]["chosen"] == "https://h/op"

    def test_decision_variant_makes_its_own_group(self):
        vids = [_v("https://h/op", "Work.mp4", size=300),
                _v("https://h/c", "Work v2.mp4", size=100, source="comment")]
        res = plan_videos(vids, Prefs(encode_vs_variant="ask"), decisions={"https://h/c": "variant"})
        assert res["ambiguous"] == []
        assert _group_of(res, "https://h/c")["kind"] == "variant"

    def test_preference_reencode_answers_without_asking(self):
        vids = [_v("https://h/op", "Work.mp4", size=300),
                _v("https://h/c", "Work v2.mp4", size=100, source="comment")]
        res = plan_videos(vids, Prefs(pick_mode="smallest", encode_vs_variant="reencode"))
        assert res["ambiguous"] == []
        assert res["groups"][0]["chosen"] == "https://h/c"

    def test_comment_video_of_another_work_is_unrelated(self):
        res = plan_videos([
            _v("https://h/op", "Work Title.mp4"),
            _v("https://h/c", "Something Else Entirely.mp4", source="comment"),
        ])
        assert res["roles"]["https://h/c"] == "unrelated"
        assert _group_of(res, "https://h/c")["kind"] == "unrelated"

    def test_comment_video_is_the_work_when_op_has_none(self):
        res = plan_videos([_v("https://h/c", "Work.mp4", source="comment")])
        assert res["roles"]["https://h/c"] == "chosen"

    def test_two_op_works_stay_two_primaries(self):
        res = plan_videos([
            _v("https://h/a", "Alpha.mp4"),
            _v("https://h/b", "Beta.mp4"),
        ])
        assert sorted(g["kind"] for g in res["groups"]) == ["primary", "primary"]


class TestNumberedSeries:
    def test_numbered_parts_are_separate_works(self):
        res = plan_videos([
            _v("https://h/4", "[Auth] Diary 04.mp4"),
            _v("https://h/1", "[Auth] Diary 01.mp4"),
            _v("https://h/2", "[Auth] Diary 02.mp4"),
        ])
        assert res["ambiguous"] == []
        assert sorted(g["kind"] for g in res["groups"]) == ["primary"] * 3
        assert all(res["roles"][u] == "chosen" for u in ("https://h/1", "https://h/2", "https://h/4"))

    def test_a_comment_reencode_of_one_part_follows_its_number(self):
        res = plan_videos([
            _v("https://h/1", "Diary 01.mp4", size=300),
            _v("https://h/2", "Diary 02.mp4", size=300),
            _v("https://h/c", "Diary 02 1080p.mp4", size=90, source="comment"),
        ])
        g = _group_of(res, "https://h/c")
        assert g["kind"] == "primary" and g["chosen"] == "https://h/c"
        assert g["alternates"] == ["https://h/2"]

    def test_lone_number_is_still_a_question(self):
        res = plan_videos([
            _v("https://h/a", "Work.mp4", size=300),
            _v("https://h/b", "Work 2.mp4", size=100, source="comment"),
        ])
        assert [a["url"] for a in res["ambiguous"]] == ["https://h/b"]


class TestNamesBeatSlugs:
    def test_probed_titles_with_tags_make_variants_even_when_slugs_differ(self):
        res = plan_videos([
            _v("https://rule34video.com/video/1/4k-nude-work-auth/", "(4K/Nude) Work [Auth]"),
            _v("https://rule34video.com/video/2/4k-thighs-work-auth/", "(4K/Thighs) Work [Auth]"),
        ])
        kinds = sorted(g["kind"] for g in res["groups"])
        assert kinds == ["primary", "variant"]


class TestCoreAliasing:
    def test_slug_with_an_extra_tag_word_joins_the_titled_work(self):
        # The probed title drops "[HMV]"; the third link never probed and its
        # slug keeps the word. Still one video, three sources.
        res = plan_videos([
            _v("https://rule34video.com/video/1/hmv-channel-work-auth/", "[HMV] Channel Work - Auth", size=100),
            _v("https://www.iwara.tv/video/abc123def/hmv-channel-work-auth", "[HMV] Channel Work - Auth", size=120),
            _v("https://pmvhaven.com/video/hmv-channel-work-auth_0123456789abcdef01234567?from=", "", priority=9),
        ])
        assert len(res["groups"]) == 1
        g = res["groups"][0]
        assert g["chosen"] == "https://rule34video.com/video/1/hmv-channel-work-auth/"
        assert len(g["alternates"]) == 2


class TestDeadLinks:
    def test_live_mirror_wins_over_a_smaller_dead_one(self):
        res = plan_videos([
            _v("https://h/dead", "Work.mp4", size=10, failed=True),
            _v("https://h/live", "Work.mp4", size=500),
        ], Prefs(pick_mode="smallest"))
        g = res["groups"][0]
        assert g["chosen"] == "https://h/live"
        assert res["roles"]["https://h/dead"] == "alternate"

    def test_video_with_only_dead_links_is_dead(self):
        res = plan_videos([
            _v("https://www.the-joi-database.com/watch/aaaa1111bbbb2222", "", failed=True),
        ])
        assert res["roles"] == {"https://www.the-joi-database.com/watch/aaaa1111bbbb2222": "dead"}
        g = res["groups"][0]
        assert g["dead"] is True and g["chosen"] == ""

    def test_dead_link_is_never_an_open_question(self):
        res = plan_videos([
            _v("https://h/op", "Work.mp4", size=300),
            _v("https://h/c", "Work v2.mp4", size=100, source="comment", failed=True),
        ], Prefs(encode_vs_variant="ask"))
        assert res["ambiguous"] == []
        assert res["groups"][0]["chosen"] == "https://h/op"

    def test_dead_reference_does_not_block_a_live_link(self):
        res = plan_videos([
            _v("https://h/a", "Work.mp4", failed=True),
            _v("https://h/b", "Work 1080p.mp4", size=100),
        ])
        assert res["roles"]["https://h/b"] == "chosen"


class TestCredits:
    def test_creator_tag_on_a_host_title_is_the_same_video(self):
        vids = [
            _v("https://mega.nz/file/x#y", "Garden Party - Night Shift.mp4", size=22_000_000),
            _v("https://rule34video.com/video/1/garden-party-night-shift-creator/",
               "Garden Party Night Shift [Creator]", size=22_000_000, height=1080),
        ]
        res = plan_videos(vids, credits=["Creator"])
        assert len(res["groups"]) == 1
        assert sorted(res["roles"].values()) == ["alternate", "chosen"]

    def test_without_credits_the_tag_still_reads_as_a_variant(self):
        vids = [
            _v("https://h/a", "Work.mp4", size=100),
            _v("https://h/b", "Work [Creator]", size=100),
        ]
        res = plan_videos(vids)
        assert res["roles"]["https://h/b"] == "variant"


def _p(url, name, pack="https://pixeldrain.com/l/packid01", size=100, source="OP"):
    return VideoSpec(url=url, name=name, source=source, size=size, pack=pack)


class TestPacks:
    def test_each_streaming_link_joins_the_pack_file_it_names(self):
        # A scripter's collection: one character by several studios, each
        # animation in the pack and again on a streaming site.
        res = plan_videos([
            _p("https://pd/u/f1", "[StudioA] Heroine Nova.mp4", size=200),
            _p("https://pd/u/f2", "[FortyTwo3D] Heroine Nova.mp4", size=100),
            _p("https://pd/u/f3", "[StudioA] The Captain Heroine Nova.mp4", size=50),
            _v("https://tube/v/1", "[StudioA][4K] Heroine Nova Full Animation", size=900),
            _v("https://tube/v/2", "Heroine Nova [FortyTwo3D]", size=900),
            _v("https://tube/v/3", "THE CAPTAIN HEROINE NOVA [StudioA]", size=900),
        ], Prefs(pick_mode="smallest", min_resolution="1080"))
        for pack_file, link in [("https://pd/u/f1", "https://tube/v/1"),
                                ("https://pd/u/f2", "https://tube/v/2"),
                                ("https://pd/u/f3", "https://tube/v/3")]:
            g = _group_of(res, pack_file)
            assert link in g["members"], (pack_file, g)
            assert g["chosen"] == pack_file
            assert res["roles"][link] == "alternate"

    def test_links_of_other_lengths_are_no_copies_of_a_lengthless_pack_file(self):
        # Three scenes of a work linked by the OP (33 / 30 / 45 s) and a
        # reply's folder file "(Maker) Work.mp4" of unknown length: the
        # file anchors them by name, but three lengths are three videos.
        res = plan_videos([
            _v("https://tube/v/1", "Heroine Vale cowgirl", duration=33.387),
            _v("https://tube/v/2", "Heroine Vale Doggystyle [Maker]", duration=30.102),
            _v("https://tube/v/3", "Heroine Vale [Maker]", duration=45.099),
            _p("https://pd/f/1", "(Maker) Heroine Vale.mp4", source="comment"),
        ], Prefs(), scripts=[{"name": "(Maker) Heroine Vale Cowgirl.funscript", "duration": 33.408},
                             {"name": "(Maker) Heroine Vale Doggy.funscript", "duration": 30.05},
                             {"name": "(Maker) Heroine Vale Mating Press.funscript", "duration": 45.12}])
        groups = [_group_of(res, u) for u in ("https://tube/v/1", "https://tube/v/2", "https://tube/v/3")]
        assert len({id(g) for g in groups}) == 3          # three videos, none a copy of another
        assert all(g["chosen"] for g in groups)            # each one downloads
        assert res["roles"]["https://tube/v/1"] == "variant"
        assert res["roles"]["https://tube/v/2"] == "variant"
        # the link known to fit the third script goes before the folder
        # file of unknown length (it was the scenes back to back)
        assert res["roles"]["https://tube/v/3"] == "chosen"
        assert res["roles"]["https://pd/f/1"] == "alternate"

    def test_slug_named_pack_file_keeps_the_tag_words(self):
        res = plan_videos([
            _p("https://pd/u/s1", "studiox-pip-ember-alt-scene-2-no-wm-4k_2160p.mp4"),
            _p("https://pd/u/s2", "moss-ember-alt-scene-2-no-watermark-4k_2160p.mp4"),
            _v("https://tube/v/s1", "[StudioX] Pip (Ember alt) scene 2: NO WM - 4K", size=900),
            _v("https://tube/v/s2", "[StudioX] Moss (Ember alt) scene 2: NO WM 4K", size=900),
        ], Prefs())
        assert res["roles"]["https://tube/v/s1"] == "alternate"
        assert res["roles"]["https://tube/v/s2"] == "alternate"
        assert _group_of(res, "https://tube/v/s1")["chosen"] == "https://pd/u/s1"
        assert _group_of(res, "https://tube/v/s2")["chosen"] == "https://pd/u/s2"

    def test_two_encodes_in_a_pack_are_one_video(self):
        res = plan_videos([
            _p("https://pd/u/c1", "[Studio] Garden Party (1-3) uncompressed.mp4", size=1800, source="comment"),
            _p("https://pd/u/c2", "[Studio] Garden Party (1-3).mp4", size=900, source="comment"),
        ], Prefs(pick_mode="smallest"))
        g = _group_of(res, "https://pd/u/c1")
        assert g["chosen"] == "https://pd/u/c2"
        assert g["alternates"] == ["https://pd/u/c1"]


class TestNamesAndLengths:
    def test_a_host_title_that_is_only_its_id_falls_back_to_the_slug(self):
        res = plan_videos([
            _v("https://tube.example/videos/1/garden-party-2020-night-shift/", failed=True),
            _v("https://tube.example/74abc/video/garden+party+2020+night+shift60fps", "74abc",
               source="comment", height=720),
        ], Prefs())
        assert res["roles"]["https://tube.example/74abc/video/garden+party+2020+night+shift60fps"] == "chosen"

    def test_dead_op_page_is_a_fallback_of_the_live_comment_copy(self):
        res = plan_videos([
            _v("https://artist.example/films/stream-vid-garden-party", failed=True),
            _v("https://tube.example/video/9/garden-party-artist/", "Garden Party [Artist]",
               source="comment", height=2160),
        ], Prefs())
        assert res["roles"]["https://tube.example/video/9/garden-party-artist/"] == "chosen"
        assert res["roles"]["https://artist.example/films/stream-vid-garden-party"] == "alternate"

    def test_one_exact_length_under_unrelated_host_titles_is_one_video(self):
        res = plan_videos([
            _v("https://booru.example/view?id=1", "Booru - If it exists / tag one, tag two / 1 (1)", duration=727.6),
            _v("https://dev.example/r/2", "Clip (Sound) Booru Video #2 | Dev (1)", duration=727.62),
        ], Prefs())
        assert len([g for g in res["groups"] if g.get("chosen")]) == 1

    def test_ids_and_words_with_digits(self):
        from funpairdl.core.video_plan import _is_id
        assert _is_id("gu6l8lwb")
        assert _is_id("zfmgl9bq")
        assert _is_id("a1b2c3d4")
        assert not _is_id("fortytwo3d")
        assert not _is_id("studio2025")


class TestRenders:
    """One video offered as a 2D and a VR render (and passthrough)."""

    def _vids(self):
        return [
            _v("https://mega.nz/file/flat", "Garden Party.mp4", size=1_260, duration=160.0),
            _p("https://mega.nz/folder/x/file/vr", "Garden Party.mp4", size=1_480),
            _p("https://mega.nz/folder/x/file/pt", "Garden Party Passthrough.mp4", size=1_530),
        ]

    def _with_frames(self, vids):
        vids[0].width, vids[0].height = 3840, 2160
        for v in vids[1:]:
            v.width, v.height, v.duration = 7680, 3840, 160.0
        return vids

    def test_format_from_frame_or_name(self):
        from funpairdl.core.video_plan import video_format
        assert video_format(3840, 2160) == "flat"
        assert video_format(7680, 3840) == "vr"
        assert video_format(7680, 3840, "Work Passthrough.mp4") == "passthrough"
        assert video_format(2560, 1080) == "flat"          # cinema scope is no VR
        assert video_format(0, 0, "Work_LR_180.mp4") == "vr"
        assert video_format(0, 0, "Work.mp4") == ""

    def test_2d_only_by_default(self):
        res = plan_videos(self._with_frames(self._vids()), Prefs())
        assert res["roles"]["https://mega.nz/file/flat"] == "chosen"
        assert res["roles"]["https://mega.nz/folder/x/file/vr"] == "format"
        assert res["roles"]["https://mega.nz/folder/x/file/pt"] == "format"

    def test_vr_only(self):
        res = plan_videos(self._with_frames(self._vids()), Prefs(vr_versions="vr"))
        assert res["roles"]["https://mega.nz/folder/x/file/vr"] in ("chosen", "variant")
        assert res["roles"]["https://mega.nz/file/flat"] == "format"
        assert res["roles"]["https://mega.nz/folder/x/file/pt"] == "format"

    def test_all_renders_are_variants(self):
        res = plan_videos(self._with_frames(self._vids()), Prefs(vr_versions="all"))
        wanted = {u for u, r in res["roles"].items() if r in ("chosen", "variant")}
        assert wanted == {"https://mega.nz/file/flat", "https://mega.nz/folder/x/file/vr",
                          "https://mega.nz/folder/x/file/pt"}
        assert _group_of(res, "https://mega.nz/folder/x/file/vr")["tag"] == "VR"

    def test_same_name_renders_are_never_mirrors(self):
        # Without the frame sizes both "Garden Party.mp4" read as one video.
        res = plan_videos(self._with_frames(self._vids())[:2], Prefs(vr_versions="all"))
        assert res["roles"]["https://mega.nz/folder/x/file/vr"] != "alternate"


class TestPostEvidence:
    """The post's title, scripts and sequels as evidence."""

    def test_a_sequel_of_one_length_is_not_a_copy(self):
        res = plan_videos([
            _v("https://iw/v/1", "花園のリズム【春香、夏美】", duration=78.0),
            _v("https://iw/v/2", "花園のリズムpart2【秋子、冬子】", duration=78.0, source="comment"),
        ], Prefs(), scripts=[{"name": "花園のリズム【春香、夏美】.funscript", "duration": 76.7}])
        assert res["roles"]["https://iw/v/2"] != "alternate"

    def test_whole_second_host_and_slug_digits_are_one_video(self):
        res = plan_videos([
            _v("https://mega.nz/file/a", "brain-blob-hmv-studio3_1080p.mp4", duration=189.0, size=100),
            _v("https://tube/v/2", "BRAIN - Blob HMV | Studio", duration=189.963, size=900),
        ], Prefs())
        assert len([g for g in res["groups"] if g.get("chosen")]) == 1

    def test_a_title_word_one_host_adds_is_no_variant(self):
        res = plan_videos([
            _v("https://pd/u/a", "[Studio] Garden Party Progress - Night Shift.mp4", duration=197.248, size=100),
            _v("https://tube/v/b", "Night Shift - Garden Party (Sound)", duration=197.248, size=900),
        ], Prefs(), credits=["Studio"], title="[Studio] Garden Party Progress - Night Shift (Suggested)")
        assert len([g for g in res["groups"] if g.get("chosen")]) == 1

    def test_a_reply_rescues_a_dead_op_link(self):
        res = plan_videos([
            _v("https://ph/view?key=ph123", failed=True),
            _v("https://tube/v/9", "Garden Party Compilation With Sound - Tube Video", duration=1181.0, source="comment"),
        ], Prefs(), title="Garden Party Compilation",
            scripts=[{"name": "Garden Party Compilation With Sound.funscript", "duration": 1186.4}])
        assert res["roles"]["https://tube/v/9"] == "chosen"

    def test_a_reply_brings_the_video_of_an_unlinked_script(self):
        res = plan_videos([
            _v("https://up/a.mp4", "Night Shift Bunny.mp4", duration=272.0),
            _v("https://tube/v/7", "【Night Shift】ダブル動画", duration=85.7, source="comment"),
        ], Prefs(), scripts=[{"name": "Night Shift Bunny.funscript", "duration": 272},
                             {"name": "Night Shift Hands.funscript", "duration": 85}])
        assert res["roles"]["https://tube/v/7"] == "chosen"

    def test_a_reply_of_the_op_videos_length_stays_a_comment(self):
        # The OP's live video already covers the script: a reply's other
        # copy is no second work.
        res = plan_videos([
            _v("https://up/a.mp4", "Work.mp4", duration=272.0),
            _v("https://tube/v/7", "何か別の動画", duration=272.2, source="comment"),
        ], Prefs(), scripts=[{"name": "Work.funscript", "duration": 272}])
        assert res["roles"]["https://tube/v/7"] in ("unrelated", "alternate")

    def test_a_folder_file_and_a_host_copy_of_the_scripts_length_are_one_video(self):
        folder_file = _p("https://mega.nz/folder/x/file/v", "花園h.mp4", size=80)
        folder_file.duration = 116.0          # MEGA records whole seconds
        res = plan_videos([
            folder_file,
            _v("https://iw/v/1", "Garden 花園", duration=118.0),   # the host rounds up
        ], Prefs(), title="(multi-axis) Garden 花園",
            scripts=[{"name": "花園h.funscript", "duration": 116.6}])
        res_v = [g for g in res["groups"] if g.get("chosen")]
        assert len(res_v) == 1

    def test_two_copies_a_reply_brings_are_one_video(self):
        res = plan_videos([
            _v("https://ng/portal/view/1", failed=True),
            _v("https://pd/u/r", "Garden Night.mp4", duration=59.78, height=1920, source="comment"),
            _v("https://up/alternate/719_alternate_334.720p.mp4", duration=59.71, height=720, source="comment"),
        ], Prefs(), title="Garden Night - Artist",
            scripts=[{"name": "Garden night.funscript", "duration": 59.94}])
        assert [u for u, r in res["roles"].items() if r == "chosen"] == ["https://pd/u/r"]

    def test_a_reply_sequel_of_the_op_work_is_downloaded(self):
        res = plan_videos([
            _v("https://iw/v/1", "花園のリズム【春香】", duration=78.0),
            _v("https://iw/v/2", "花園のリズムpart2【秋子】", duration=78.0, source="comment"),
        ], Prefs(), scripts=[{"name": "花園のリズム【春香】.funscript", "duration": 76.7}])
        assert res["roles"]["https://iw/v/2"] in ("chosen", "variant")

    def test_a_folder_file_named_in_kana_takes_its_host_copy_and_not_the_sequels(self):
        res = plan_videos([
            _p("https://pd/u/p1", "花園のリズム【春香】.mp4", pack="https://pd/l/one"),
            _v("https://iw/v/1", "花園のリズム【春香】", duration=78.0),
            _p("https://pd/u/p2", "花園のリズムpart2【秋子】.mp4", pack="https://pd/l/two", source="comment"),
            _v("https://iw/v/2", "花園のリズムpart2【秋子】", duration=78.0, source="comment"),
        ], Prefs(), scripts=[{"name": "花園のリズム【春香】.funscript", "duration": 76.7}])
        assert _group_of(res, "https://iw/v/1") is _group_of(res, "https://pd/u/p1")
        assert _group_of(res, "https://iw/v/2") is _group_of(res, "https://pd/u/p2")
        assert _group_of(res, "https://pd/u/p1") is not _group_of(res, "https://pd/u/p2")

    def test_a_numbered_slug_copy_of_a_reply_is_a_mirror(self):
        res = plan_videos([
            _v("https://pd/u/r", "Garden Night.mp4", duration=59.78, source="comment"),
            _v("https://up/alternate/7194008_alternate_334950.720p.mp4", duration=59.71, source="comment"),
        ], Prefs(), title="Garden Night", scripts=[{"name": "Garden night.funscript", "duration": 59.9}])
        assert len([g for g in res["groups"] if g.get("chosen")]) == 1


class TestArchivesAndCharacters:
    def test_a_creator_archive_keeps_only_the_posts_work(self):
        pack = "https://mega.nz/folder/arc"
        names = ["OtherWorkCowgirl60FPS.mp4", "Lumi Taming 4K60FPS.mp4", "LumiDoggyVR8K60FPS.mp4",
                 "ThirdWorkVR8K.mp4", "LumiDriverAlt4K60FPS.mp4"]
        shapes = [(60, 3840, 2160, 900), (40, 3840, 2160, 250), (40, 8192, 4096, 320), (30, 8192, 4096, 260),
                  (40, 2160, 3840, 200)]
        vids = []
        for i, (n, (d, w, h, sz)) in enumerate(zip(names, shapes)):
            v = _p(f"https://mega.nz/folder/arc/file/{i}", n, pack=pack, size=sz)
            v.duration, v.width, v.height = d, w, h
            vids.append(v)
        link = _v("https://tube/v/1", "[Studio] Lumi Doggy POV Driver 1080p", duration=40.0, height=1080, size=40)
        link.width = 607                     # a portrait 1080p copy of the portrait render
        vids.append(link)
        res = plan_videos(vids, Prefs(), title="Studio - Lumi Doggy Flat and VR 8K",
                          scripts=[{"name": "LumiDoggy (Normal).funscript", "duration": 40.04}])
        r = res["roles"]
        assert r["https://mega.nz/folder/arc/file/0"] == "unrelated"     # another work of the archive
        assert r["https://mega.nz/folder/arc/file/3"] == "unrelated"
        assert r["https://mega.nz/folder/arc/file/2"] == "format"        # the VR render of the post's work
        assert r["https://mega.nz/folder/arc/file/1"] == "chosen"        # the landscape render
        assert r["https://tube/v/1"] == "chosen"                          # the portrait one, its small copy
        assert r["https://mega.nz/folder/arc/file/4"] == "alternate"

    def test_characters_of_one_length_keep_their_own_videos(self):
        vids = [_v(f"https://tube/v/{n}", n, duration=d) for n, d in [
            ("Hana Full", 63.211), ("Mira Full", 63.211), ("Hana Loop", 31.04), ("Mira Loop", 31.04),
            ("Guardsman (Series… Video #1 | Dev (1)", 63.211)]]
        res = plan_videos(vids, Prefs(), scripts=[
            {"name": "Studio Hana Ride.funscript", "duration": 63.211},
            {"name": "Studio Mira Ride.funscript", "duration": 63.211},
            {"name": "Studio Hana Ride Loop.funscript", "duration": 31.064},
            {"name": "Studio Mira Ride Loop.funscript", "duration": 31.064}])
        r = res["roles"]
        for n in ("Hana Full", "Mira Full", "Hana Loop", "Mira Loop"):
            assert r[f"https://tube/v/{n}"] == "chosen", n
        assert r["https://tube/v/Guardsman (Series… Video #1 | Dev (1)"] == "unrelated"

    def test_one_folders_4k_and_1080p_of_a_render_are_one_video(self):
        pack = "https://mega.nz/folder/hu"
        files = [("[Studio] Garden Dream (Full) - Nude.mp4", 651), ("[Studio] Garden Dream (Full) - Clothed.mp4", 652),
                 ("4k-full-nude-garden-dream-studio_1080p.mp4", 249), ("4k-full-clothed-garden-dream-studio_1080p.mp4", 250)]
        vids = []
        for i, (n, sz) in enumerate(files):
            v = _p(f"https://mega.nz/folder/hu/file/{i}", n, pack=pack, size=sz)
            v.duration, v.height = 432.0, 2160 if sz > 600 else 1080
            vids.append(v)
        res = plan_videos(vids, Prefs(), scripts=[{"name": "Studio Garden Dream (Full) - Nude.funscript", "duration": 432.7}])
        chosen = sorted(u for u, r in res["roles"].items() if r in ("chosen", "variant"))
        assert chosen == ["https://mega.nz/folder/hu/file/2", "https://mega.nz/folder/hu/file/3"]

    def test_the_copy_of_the_scripts_exact_length_is_the_pick(self):
        res = plan_videos([
            _v("https://pd/u/a", "Garden Night.mp4", duration=453.37, size=237),
            _v("https://ph/b", "Garden Night", duration=450.0, size=100),
        ], Prefs(), scripts=[{"name": "Garden Night.funscript", "duration": 453.37}])
        assert res["roles"]["https://pd/u/a"] == "chosen"

    def test_script_flavours_of_one_length_are_no_characters(self):
        res = plan_videos([
            _v("https://tube/v/1", "The Visit (Studio) 4K60FPS", duration=170.09),
            _v("https://disk/i/2", "The_Visit_(Studio)_4K60FPS.mp4", duration=170.09),
        ], Prefs(), scripts=[{"name": "SMOOTH The_Visit_(Studio)_4K60FPS.funscript", "duration": 170.095},
                             {"name": "The_Visit_(Studio)_4K60FPS_maxinterval.funscript", "duration": 170.095}])
        assert sorted(res["roles"].values()) == ["alternate", "chosen"]

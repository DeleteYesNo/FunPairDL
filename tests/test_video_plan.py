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

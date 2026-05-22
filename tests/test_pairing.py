"""Regression tests for funpairdl.core.pairing.

Anchored on two bugs seen in the Confirm-Pair-grouping preview:

  1. Punctuation variants split a video from its script. A source that
     rewrote "Jane Doe's" to "Jane Doe s" produced two orphans because
     normalize() left the apostrophe in the key.
  2. Files from a single Pixeldrain list all carried a unique synthetic
     parent_path, so a video and its funscripts never matched in-folder
     and were all flagged "exact name match across folders".

The picker derives parent_path; here we feed Candidates directly with the
parent_path the fixed picker now produces (the list root shared by all
siblings).
"""
from funpairdl.core.pairing import (
    Candidate,
    Confidence,
    FileKind,
    normalize,
    pair_files,
)


class TestNormalize:
    def test_apostrophe_variants_match(self):
        # Bug 1: "Doe's" vs "Doe s" must normalize to the same key.
        v = normalize("[Infected_Heart] MPOV Jane Doe's Valentine Confession.mp4")
        s = normalize("[Infected_Heart] MPOV Jane Doe s Valentine Confession.funscript")
        assert v == s

    def test_punctuation_stripped(self):
        # Commas, ampersands, brackets — all dropped for comparison.
        assert normalize("A, B & C.mp4") == normalize("A B C.funscript")

    def test_bracket_styles_unify(self):
        # [Author] and (Author) are the same author after the fix.
        assert normalize("[Author] Title.mp4") == normalize("(Author) Title.funscript")

    def test_different_authors_stay_distinct(self):
        # The whole point of preserving the author prefix: different
        # uploaders of a generic title must NOT collapse together.
        assert normalize("(Suppai) Compilation.mp4") != normalize("(Howlsfm) Compilation.mp4")

    def test_axis_suffix_stripped(self):
        assert normalize("Foo.pitch.funscript") == normalize("Foo.funscript")

    def test_resolution_token_stripped(self):
        assert normalize("Clip 1080p.mp4") == normalize("Clip.funscript")


def _vid(key, name, parent):
    return Candidate(key=key, name=name, kind=FileKind.VIDEO, parent_path=parent)


def _scr(key, name, parent):
    return Candidate(key=key, name=name, kind=FileKind.SCRIPT, parent_path=parent)


class TestPairFiles:
    def test_list_siblings_group_in_folder(self):
        # Bug 2: video + multiple scripts sharing one list parent_path
        # become a single HIGH-confidence same-folder group.
        LIST = "list:abc"
        cands = [
            _vid(1, "[LinuxDx] Mona Squatting.mp4", LIST),
            _scr(2, "[LinuxDx] Mona Squatting.funscript", LIST),
            _scr(3, "[LinuxDx] Mona Squatting.pitch.funscript", LIST),
            _scr(4, "[LinuxDx] Mona Squatting.roll.funscript", LIST),
        ]
        groups = pair_files(cands)
        assert len(groups) == 1
        g = groups[0]
        assert g.confidence == Confidence.HIGH
        assert "same folder" in g.note
        assert len(g.videos) == 1
        assert len(g.scripts) == 3
        assert not g.is_orphan

    def test_apostrophe_pair_not_orphaned(self):
        # Bug 1 end-to-end: the two halves land in one HIGH group, not two
        # orphans, once they share a folder scope.
        LIST = "list:abc"
        cands = [
            _vid(1, "[Infected_Heart] MPOV Jane Doe's Valentine Confession.mp4", LIST),
            _scr(2, "[Infected_Heart] MPOV Jane Doe s Valentine Confession.funscript", LIST),
        ]
        groups = pair_files(cands)
        assert len(groups) == 1
        g = groups[0]
        assert g.confidence == Confidence.HIGH
        assert len(g.videos) == 1 and len(g.scripts) == 1

    def test_cross_folder_match_is_medium(self):
        # Same name in genuinely different folders stays MEDIUM so the
        # preview still warns "verify before downloading".
        groups = pair_files([
            _vid(1, "Title.mp4", "/bucket/a"),
            _scr(2, "Title.funscript", "/bucket/b"),
        ])
        assert len(groups) == 1
        g = groups[0]
        assert g.confidence == Confidence.MEDIUM
        assert "across folders" in g.note

    def test_unmatched_files_become_orphans(self):
        groups = pair_files([
            _vid(1, "Alpha.mp4", "list:x"),
            _scr(2, "Beta.funscript", "list:x"),
        ])
        assert len(groups) == 2
        assert all(g.is_orphan and g.confidence == Confidence.LOW for g in groups)

    def test_distinct_authors_do_not_merge(self):
        # Two different uploaders' files in the same list must not be
        # forced into one Pair just because they share a folder scope.
        LIST = "list:x"
        groups = pair_files([
            _vid(1, "(Suppai) Compilation.mp4", LIST),
            _scr(2, "(Howlsfm) Compilation.funscript", LIST),
        ])
        # Different normalized keys → no pairing → two LOW orphans.
        assert len(groups) == 2
        assert all(g.is_orphan for g in groups)

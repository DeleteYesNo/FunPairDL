"""Tests for QueueManager._organize_output (flat library layout + funlib.json)."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from funpairdl.core.pair import FileType, Pair, PairItem
from funpairdl.core.queue_manager import QueueManager


def _make_pair(output_dir: str, name: str, items: list[PairItem]) -> Pair:
    pair = Pair(name=name)
    pair.output_dir = output_dir
    pair.items = items
    return pair


def _touch(path: Path, size: int = 100):
    """Create a file with dummy content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _organize(pair, variant_mode="flat"):
    qm = QueueManager()
    with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
        mock_load.return_value.script_variant_mode = variant_mode
        mock_load.return_value.reconcile_on_redownload = False
        qm._organize_output(pair)
    return qm


def _sidecar(folder: Path) -> dict:
    return json.loads((folder / "funlib.json").read_text(encoding="utf-8"))


def _variants(folder: Path) -> dict:
    return {v["label"]: v for v in _sidecar(folder)["variants"]}


class TestParseAxis:
    """Test _parse_axis static method."""

    def test_known_axis(self):
        assert QueueManager._parse_axis("video.pitch.funscript") == ("R2", "pitch")

    def test_known_axis_case_insensitive(self):
        assert QueueManager._parse_axis("video.Pitch.funscript") == ("R2", "Pitch")

    def test_main_axis_no_suffix(self):
        assert QueueManager._parse_axis("video.funscript") == ("L0", "")

    def test_unknown_suffix_maps_to_L0(self):
        assert QueueManager._parse_axis("video.max.funscript") == ("L0", "")

    def test_compound_suffix_L0_max(self):
        # .L0.max → known axis L0 found, "max" is variant qualifier
        assert QueueManager._parse_axis("video.L0.max.funscript") == ("L0", "L0")

    def test_compound_suffix_L0_plus(self):
        assert QueueManager._parse_axis("video.L0.plus.funscript") == ("L0", "L0")

    def test_compound_suffix_pitch_variant(self):
        assert QueueManager._parse_axis("video.pitch.hard.funscript") == ("R2", "pitch")

    def test_surge(self):
        assert QueueManager._parse_axis("video.surge.funscript") == ("L1", "surge")

    def test_suck(self):
        assert QueueManager._parse_axis("video.suck.funscript") == ("L3", "suck")

    def test_word_axis_with_glued_qualifier(self):
        # A scripter's ".suckManual" is the suction axis with a qualifier —
        # filing it as L0 renamed it over the stroke script's name.
        assert QueueManager._parse_axis("video.suckManual.funscript") == ("L3", "suckManual")
        assert QueueManager._parse_axis("video.twist_v2.funscript") == ("R0", "twist_v2")
        assert QueueManager._parse_axis("video.roll-soft.funscript") == ("R1", "roll-soft")
        assert QueueManager._parse_axis("video.strokeSoft.funscript") == ("L0", "strokeSoft")

    def test_plain_words_starting_with_an_axis_are_not_axes(self):
        assert QueueManager._parse_axis("video.rolling.funscript") == ("L0", "")
        assert QueueManager._parse_axis("video.manual.funscript") == ("L0", "")
        assert QueueManager._parse_axis("video.raw.funscript") == ("L0", "")

    def test_exact_axis_wins_over_prefixed_component(self):
        assert QueueManager._parse_axis("video.suckManual.pitch.funscript") == ("R2", "pitch")

    def test_vibe_aliases(self):
        assert QueueManager._parse_axis("video.vibe.funscript") == ("V0", "vibe")
        assert QueueManager._parse_axis("video.vibration.funscript") == ("V0", "vibration")
        assert QueueManager._parse_axis("video.vib.funscript") == ("V0", "vib")

    def test_all_erodeck_axes(self):
        expected = {
            "stroke": "L0", "l0": "L0", "surge": "L1", "l1": "L1",
            "sway": "L2", "l2": "L2", "suck": "L3", "l3": "L3",
            "twist": "R0", "r0": "R0", "roll": "R1", "r1": "R1",
            "pitch": "R2", "r2": "R2", "vibe": "V0", "vib": "V0",
            "vibration": "V0", "v0": "V0", "pump": "V1", "lube": "V1",
            "v1": "V1", "valve": "V2", "v2": "V2",
            "a0": "A0", "a1": "A1", "a2": "A2",
        }
        for suffix, canon in expected.items():
            result = QueueManager._parse_axis(f"x.{suffix}.funscript")
            assert result[0] == canon, f"{suffix} → expected {canon}, got {result[0]}"

    def test_suckManual_is_the_suction_axis(self):
        # Not in erodeck's exact list, but "suck" + a qualifier is still L3.
        # Filing it as L0 used to rename the suction script over the stroke
        # script's name and push the real stroke script into an .alt folder.
        assert QueueManager._parse_axis("video.suckManual.funscript") == ("L3", "suckManual")


class TestOrganizeOutputFlat:
    """Test flat mode (single author or no author info)."""

    def test_single_author_flat_rename(self, tmp_path):
        """Scripts from one author stay in root folder."""
        _touch(tmp_path / "original_video.mp4")
        _touch(tmp_path / "original_script.funscript")

        pair = _make_pair(str(tmp_path), "My Video Title", [
            PairItem(url="http://x/v.mp4", filename="original_video.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="original_script.funscript", file_type=FileType.FUNSCRIPT, author="Alice"),
        ])

        _organize(pair)

        assert (tmp_path / "My Video Title.mp4").exists()
        assert (tmp_path / "My Video Title.funscript").exists()
        assert not (tmp_path / "original_video.mp4").exists()

    def test_no_author_info_flat(self, tmp_path):
        """No author info → flat mode regardless of setting."""
        _touch(tmp_path / "video.mp4")
        _touch(tmp_path / "script.funscript")

        pair = _make_pair(str(tmp_path), "Test", [
            PairItem(url="http://x/v.mp4", filename="video.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="script.funscript", file_type=FileType.FUNSCRIPT),
        ])

        _organize(pair, "subfolder")

        assert (tmp_path / "Test.mp4").exists()
        assert (tmp_path / "Test.funscript").exists()

    def test_axis_suffix_preserved(self, tmp_path):
        """Multi-axis suffixes should be preserved in flat mode."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "something.pitch.funscript")
        _touch(tmp_path / "something.roll.funscript")

        pair = _make_pair(str(tmp_path), "Axis Test", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/p.funscript", filename="something.pitch.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/r.funscript", filename="something.roll.funscript", file_type=FileType.FUNSCRIPT),
        ])

        _organize(pair)

        assert (tmp_path / "Axis Test.mp4").exists()
        assert (tmp_path / "Axis Test.funscript").exists()
        assert (tmp_path / "Axis Test.pitch.funscript").exists()
        assert (tmp_path / "Axis Test.roll.funscript").exists()


class TestAxisCollision:
    """Axis collisions inside Main become flat "(Label)" variants."""

    def test_two_L0_variants_max_plus(self, tmp_path):
        """Two scripts both mapping to L0 (unknown suffixes) → second is a variant."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "Weekday.L0.max.funscript")
        _touch(tmp_path / "Weekday.L0.plus.funscript")

        pair = _make_pair(str(tmp_path), "Weekday", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/max.funscript", filename="Weekday.L0.max.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/plus.funscript", filename="Weekday.L0.plus.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        # First L0 script → primary (with L0 suffix since _parse_axis returns "L0")
        assert (tmp_path / "Weekday.L0.funscript").exists()
        # Second L0 script → flat variant, no subfolder, no linked video
        assert (tmp_path / "Weekday (Alt).L0.funscript").exists()
        assert not (tmp_path / "Weekday.alt").exists()
        assert not (tmp_path / ".linkinfo").exists()
        assert [f.name for f in tmp_path.iterdir() if f.suffix.lower() == ".mp4"] == ["Weekday.mp4"]

    def test_unknown_suffixes_collide_on_L0(self, tmp_path):
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "video.max.funscript")
        _touch(tmp_path / "video.plus.funscript")

        pair = _make_pair(str(tmp_path), "Video", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/max.funscript", filename="video.max.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/plus.funscript", filename="video.plus.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        assert (tmp_path / "Video.funscript").exists()
        assert (tmp_path / "Video (Alt).funscript").exists()

    def test_no_collision_different_axes(self, tmp_path):
        """Different known axes → no collision → all stay Main."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")
        _touch(tmp_path / "s.pitch.funscript")
        _touch(tmp_path / "s.surge.funscript")

        pair = _make_pair(str(tmp_path), "Multi", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/p.funscript", filename="s.pitch.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/su.funscript", filename="s.surge.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        assert (tmp_path / "Multi.funscript").exists()
        assert (tmp_path / "Multi.pitch.funscript").exists()
        assert (tmp_path / "Multi.surge.funscript").exists()
        v = _variants(tmp_path)
        assert list(v) == ["Main"]
        assert v["Main"]["files"] == {"L0": "Multi.funscript", "pitch": "Multi.pitch.funscript",
                                      "surge": "Multi.surge.funscript"}

    def test_axis_collision_alt_inherits_multiaxis_at_play_time(self, tmp_path):
        """An auto-promoted L0 extra is another stroke take on the same
        scene: nothing is copied — the sidecar leaves inherit_axes on so
        FunLib plays it with Main's pitch/roll."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")
        _touch(tmp_path / "s.pitch.funscript")
        _touch(tmp_path / "s.roll.funscript")
        _touch(tmp_path / "s.max.funscript")

        pair = _make_pair(str(tmp_path), "Mixed", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/p.funscript", filename="s.pitch.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/r.funscript", filename="s.roll.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/m.funscript", filename="s.max.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        assert pair.alt_group_config["Alt 1"]["inherit_multi_axis"] is True
        assert pair.alt_group_config["Alt 1"]["label"] == "Alt"
        assert (tmp_path / "Mixed (Alt).funscript").exists()
        assert not (tmp_path / "Mixed (Alt).pitch.funscript").exists()
        assert not (tmp_path / "Mixed.alt").exists()
        v = _variants(tmp_path)
        assert v["Alt"]["files"] == {"L0": "Mixed (Alt).funscript"}
        assert "inherit_axes" not in v["Alt"]          # default true

    def test_alt_whose_file_is_gone_makes_nothing(self, tmp_path):
        """A mirror bundle carried the same upload as the forum script: the
        second item was skipped as already on disk and shares the first's
        file. Once Main claims that file there is nothing left for the Alt."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "Work.funscript")

        pair = _make_pair(str(tmp_path), "Work", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/a.funscript", filename="Work.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://y/a.funscript", filename="Work.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        assert (tmp_path / "Work.funscript").exists()
        assert (tmp_path / "Work.mp4").exists()
        assert not (tmp_path / "Work (Alt).funscript").exists()
        assert list(_variants(tmp_path)) == ["Main"]

    def test_three_L0_variants_get_numbered_labels(self, tmp_path):
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s1.funscript")
        _touch(tmp_path / "s2.funscript")
        _touch(tmp_path / "s3.funscript")

        pair = _make_pair(str(tmp_path), "Three", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s1.funscript", filename="s1.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/s2.funscript", filename="s2.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/s3.funscript", filename="s3.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        assert (tmp_path / "Three.funscript").exists()
        assert (tmp_path / "Three (Alt).funscript").exists()
        assert (tmp_path / "Three (Alt 2).funscript").exists()
        assert set(_variants(tmp_path)) == {"Main", "Alt", "Alt 2"}

    def test_two_authors_become_author_labels(self, tmp_path):
        """A second scripter's take is labelled by their name."""
        _touch(tmp_path / "v.mp4", size=200)
        _touch(tmp_path / "a_script.funscript")
        _touch(tmp_path / "b_script.funscript")

        pair = _make_pair(str(tmp_path), "TwoAuth", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/a.funscript", filename="a_script.funscript", file_type=FileType.FUNSCRIPT, author="Alice"),
            PairItem(url="http://x/b.funscript", filename="b_script.funscript", file_type=FileType.FUNSCRIPT, author="Bob"),
        ])
        _organize(pair, "subfolder")

        assert (tmp_path / "TwoAuth.mp4").exists()
        assert (tmp_path / "TwoAuth.funscript").exists()
        assert (tmp_path / "TwoAuth (Bob).funscript").exists()
        assert not (tmp_path / "TwoAuth.alt").exists()
        assert not (tmp_path / ".linkinfo").exists()
        assert os.stat(tmp_path / "TwoAuth.mp4").st_nlink == 1

    def test_multiaxis_second_author_keeps_own_axes(self, tmp_path):
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "a_main.funscript")
        _touch(tmp_path / "a_main.pitch.funscript")
        _touch(tmp_path / "b_main.funscript")
        _touch(tmp_path / "b_main.pitch.funscript")

        pair = _make_pair(str(tmp_path), "MultiAx", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/a.funscript", filename="a_main.funscript", file_type=FileType.FUNSCRIPT, author="A"),
            PairItem(url="http://x/ap.funscript", filename="a_main.pitch.funscript", file_type=FileType.FUNSCRIPT, author="A"),
            PairItem(url="http://x/b.funscript", filename="b_main.funscript", file_type=FileType.FUNSCRIPT, author="B"),
            PairItem(url="http://x/bp.funscript", filename="b_main.pitch.funscript", file_type=FileType.FUNSCRIPT, author="B"),
        ])
        _organize(pair, "subfolder")

        assert (tmp_path / "MultiAx.funscript").exists()
        assert (tmp_path / "MultiAx.pitch.funscript").exists()
        assert (tmp_path / "MultiAx (B).funscript").exists()
        assert (tmp_path / "MultiAx (B).pitch.funscript").exists()
        v = _variants(tmp_path)
        assert v["B"]["files"] == {"L0": "MultiAx (B).funscript", "pitch": "MultiAx (B).pitch.funscript"}
        assert v["B"]["author"] == "B" and v["Main"]["author"] == "A"

    def test_subfolder_mode_but_single_author_stays_flat(self, tmp_path):
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")

        pair = _make_pair(str(tmp_path), "Single", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT, author="OnlyOne"),
        ])
        _organize(pair, "subfolder")

        assert (tmp_path / "Single.funscript").exists()
        assert list(_variants(tmp_path)) == ["Main"]


class TestCleanTitle:
    def test_removes_multi_axis_tag(self):
        assert QueueManager._clean_title("(multi-axis) Cool Video") == "Cool Video"

    def test_removes_free_tag(self):
        assert QueueManager._clean_title("My Script (Free)") == "My Script"

    def test_preserves_normal_title(self):
        assert QueueManager._clean_title("Just a Normal Title") == "Just a Normal Title"


class TestExplicitGroups:
    """Pairs with explicit `item.group` and Pair.alt_group_config — the
    layout produced by the EroScripts picker UI."""

    def test_alt_with_same_video_becomes_flat_variant(self, tmp_path):
        """Comment posted the same video again + its own script → the
        duplicate video is dropped, the script is a (Label) variant, Main's
        other axes are NOT copied (FunLib inherits them at play time)."""
        _touch(tmp_path / "main.mp4", size=300)
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "main.surge.funscript")
        _touch(tmp_path / "main.pitch.funscript")
        _touch(tmp_path / "alt.mp4", size=300)
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="main.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/p.funscript", filename="main.pitch.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.mp4", filename="alt.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": True, "display_name": "Remake"}}
        _organize(pair)

        assert (tmp_path / "Topic.mp4").exists()
        assert (tmp_path / "Topic.funscript").exists()
        assert (tmp_path / "Topic.surge.funscript").exists()
        assert (tmp_path / "Topic (Remake).funscript").exists()
        assert not (tmp_path / "alt.mp4").exists()
        assert not (tmp_path / "Topic (Remake).surge.funscript").exists()
        assert not (tmp_path / "Topic.alt").exists()
        assert not (tmp_path / ".linkinfo").exists()
        v = _variants(tmp_path)
        assert v["Main"]["primary"] is True
        assert v["Remake"]["files"] == {"L0": "Topic (Remake).funscript"}
        assert "inherit_axes" not in v["Remake"]

    def test_alt_inherit_disabled_is_recorded_in_sidecar(self, tmp_path):
        _touch(tmp_path / "main.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "main.surge.funscript")
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="main.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": False}}
        _organize(pair)

        assert (tmp_path / "Topic (Alt).funscript").exists()
        assert _variants(tmp_path)["Alt"]["inherit_axes"] is False

    def test_alt_with_different_video_is_a_variant_with_own_video(self, tmp_path):
        """A comment with its OWN (different) video stays one work: the video
        becomes `<work> (<Label>).mp4` and the sidecar points the variant at
        it, so FunLib swaps video + thumbnail when switching variants."""
        root = tmp_path / "lib"
        out = root / "Mockyu"
        _touch(out / "op.mp4", size=100)
        _touch(out / "op.funscript")
        _touch(out / "op.surge.funscript")
        _touch(out / "c1.mp4", size=150)
        _touch(out / "c1.funscript")
        _touch(out / "c1.pitch.funscript")

        pair = _make_pair(str(out), "Mockyu", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="op.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.mp4", filename="c1.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
            PairItem(url="http://x/c1p.funscript", filename="c1.pitch.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.source_url = "https://discuss.eroscripts.com/t/mockyu/4321"
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": True, "display_name": "示例风格"}}
        _organize(pair)

        assert (out / "Mockyu.mp4").exists() and (out / "Mockyu.funscript").exists()
        assert (out / "Mockyu (示例风格).mp4").exists()
        assert (out / "Mockyu (示例风格).funscript").exists()
        assert (out / "Mockyu (示例风格).pitch.funscript").exists()
        assert not (out / "Mockyu.alt").exists() and not (root / "Mockyu (示例风格)").exists()
        v = _variants(out)
        assert v["Main"]["video"] == "Mockyu.mp4"
        assert v["示例风格"] == {"label": "示例风格", "video": "Mockyu (示例风格).mp4",
                              "files": {"L0": "Mockyu (示例风格).funscript",
                                        "pitch": "Mockyu (示例风格).pitch.funscript"}}
        sc = _sidecar(out)
        assert sc["source"] == {"site": "eroscripts", "url": pair.source_url, "topic_id": 4321}

        # undo brings the original names back
        QueueManager()._undo_organize(pair)
        assert (out / "c1.mp4").exists() and (out / "c1.funscript").exists() and (out / "op.mp4").exists()
        assert not (out / "funlib.json").exists()

    def test_display_name_brackets_are_dropped_and_labels_unique(self, tmp_path):
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "c1.funscript")
        _touch(tmp_path / "c2.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
            PairItem(url="http://x/c2.funscript", filename="c2.funscript", file_type=FileType.FUNSCRIPT, group="Alt 2"),
        ])
        pair.alt_group_config = {
            "Alt 1": {"inherit_multi_axis": False, "display_name": "(Soft) [v2]"},
            "Alt 2": {"inherit_multi_axis": False, "display_name": "Soft v2"},
        }
        _organize(pair)

        assert (tmp_path / "Topic (Soft v2).funscript").exists()
        assert (tmp_path / "Topic (Soft v2 2).funscript").exists()
        assert set(_variants(tmp_path)) == {"Main", "Soft v2", "Soft v2 2"}

    def test_label_taken_on_disk_is_numbered(self, tmp_path):
        """A "(Soft)" from an earlier download is already in the folder."""
        _touch(tmp_path / "Topic (Soft).funscript")
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "c1.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"display_name": "Soft"}}
        _organize(pair)

        assert (tmp_path / "Topic (Soft).funscript").exists()
        assert (tmp_path / "Topic (Soft 2).funscript").exists()
        v = _variants(tmp_path)
        assert v["Soft"]["files"] == {"L0": "Topic (Soft).funscript"}
        assert v["Soft 2"]["files"] == {"L0": "Topic (Soft 2).funscript"}

    def test_undo_restores_flat_variants(self, tmp_path):
        _touch(tmp_path / "main.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": True}}
        _organize(pair)
        assert (tmp_path / "Topic (Alt).funscript").exists()
        assert (tmp_path / "funlib.json").exists()

        QueueManager()._undo_organize(pair)
        assert sorted(f.name for f in tmp_path.iterdir()) == ["alt.funscript", "main.funscript", "main.mp4"]
        assert pair.organized is False


class TestSidecar:
    def test_sidecar_fields_from_pair(self, tmp_path):
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")
        _touch(tmp_path / "s.roll.funscript")
        pair = _make_pair(str(tmp_path), "(Casey Sample) Demo Load", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/r.funscript", filename="s.roll.funscript", file_type=FileType.FUNSCRIPT),
        ])
        pair.source_url = "https://discuss.eroscripts.com/t/demo-load/12345"
        _organize(pair)

        sc = _sidecar(tmp_path)
        assert sc["version"] == 2
        assert sc["title"] == "(Casey Sample) Demo Load"
        assert sc["author"] == "Casey Sample"
        assert sc["source"] == {"site": "eroscripts", "url": pair.source_url, "topic_id": 12345}
        assert sc["pair_id"] == pair.id
        assert sc["downloaded_at"].endswith("Z")
        assert sc["variants"] == [{"label": "Main", "primary": True, "video": "(Casey Sample) Demo Load.mp4",
                                   "files": {"L0": "(Casey Sample) Demo Load.funscript",
                                             "roll": "(Casey Sample) Demo Load.roll.funscript"}}]

    def test_reorganize_keeps_existing_sidecar_values(self, tmp_path):
        """Forum fields filled earlier (tags, posted_at, a forum author) are
        not clobbered by a later organize; variants are refreshed."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")
        (tmp_path / "funlib.json").write_text(json.dumps({
            "version": 1, "title": "Old", "author": "opname", "author_url": "https://x/u/opname",
            "tags": ["hmv"], "posted_at": "2026-01-01T00:00:00Z", "downloaded_at": "2026-01-02T00:00:00Z",
            "pair_id": "oldpair", "variants": [{"label": "Main", "primary": True, "files": {"L0": "x"}}],
        }), encoding="utf-8")
        pair = _make_pair(str(tmp_path), "(Someone) Work", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT),
        ])
        _organize(pair)

        sc = _sidecar(tmp_path)
        assert sc["title"] == "Old" and sc["author"] == "opname"
        assert sc["tags"] == ["hmv"] and sc["posted_at"] == "2026-01-01T00:00:00Z"
        assert sc["downloaded_at"] == "2026-01-02T00:00:00Z" and sc["pair_id"] == "oldpair"
        assert sc["variants"][0]["files"] == {"L0": "(Someone) Work.funscript"}


class TestSecondMainVideoIsVariant:
    """A second, different video in Main is a variant of the work (a batch of
    two renders with one script set): it lands as `<work> (<tag>).mp4` with
    a copy of Main's L0 so FunLib lists it, the other axes inherited."""

    def _pair(self, out, names):
        items = [
            PairItem(url=f"https://pixeldrain.com/u/vid{i}", filename=n, file_type=FileType.VIDEO)
            for i, n in enumerate(names)
        ] + [
            PairItem(url="https://pixeldrain.com/u/s0", filename="Work Title.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="https://pixeldrain.com/u/s1", filename="Work Title.pitch.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="https://pixeldrain.com/u/s2", filename="Work Title.surge.funscript", file_type=FileType.FUNSCRIPT),
        ]
        return _make_pair(str(out), "Work Title", items)

    def test_different_second_video_becomes_variant_with_copied_L0(self, tmp_path):
        out = tmp_path / "Work Title"
        _touch(out / "Work Title (nude).mp4", size=100)
        (out / "Work Title (stockings).mp4").write_bytes(b"y" * 150)
        (out / "Work Title.funscript").write_bytes(b'{"actions":[]}')
        _touch(out / "Work Title.pitch.funscript")
        _touch(out / "Work Title.surge.funscript")
        pair = self._pair(out, ["Work Title (nude).mp4", "Work Title (stockings).mp4"])

        _organize(pair)

        assert (out / "Work Title.mp4").read_bytes() == b"x" * 100
        assert (out / "Work Title (stockings).mp4").read_bytes() == b"y" * 150
        assert not (out / "Work Title (nude).mp4").exists()
        assert (out / "Work Title (stockings).funscript").read_bytes() == b'{"actions":[]}'
        assert not (out / "Work Title (stockings).pitch.funscript").exists()
        v = _variants(out)
        assert v["Main"]["video"] == "Work Title.mp4"
        assert set(v["Main"]["files"]) == {"L0", "pitch", "surge"}
        assert v["stockings"] == {"label": "stockings", "video": "Work Title (stockings).mp4",
                                  "files": {"L0": "Work Title (stockings).funscript"}}
        assert pair.alt_group_config["Alt 1"]["label"] == "stockings"
        assert pair.items[1].group == "Alt 1"
        assert pair.items[1].filename == "Work Title (stockings).mp4"

    def test_identical_second_video_is_still_dropped(self, tmp_path):
        out = tmp_path / "Work Title"
        _touch(out / "Work Title (nude).mp4", size=100)
        _touch(out / "Work Title (mirror).mp4", size=100)
        _touch(out / "Work Title.funscript")
        _touch(out / "Work Title.pitch.funscript")
        _touch(out / "Work Title.surge.funscript")
        pair = self._pair(out, ["Work Title (nude).mp4", "Work Title (mirror).mp4"])

        _organize(pair)

        assert sorted(p.name for p in out.glob("*.mp4")) == ["Work Title.mp4"]
        assert not (out / "Work Title (mirror).funscript").exists()
        assert list(_variants(out)) == ["Main"]
        assert pair.alt_group_config == {}


class TestVariantTag:
    def test_tag_the_primary_lacks(self):
        assert QueueManager._variant_tag("Work (stockings).mp4", "Work (nude).mp4") == "stockings"
        assert QueueManager._variant_tag("[Auth] Work [4K].mp4", "[Auth] Work [1080p].mp4") == "4K"

    def test_suffix_after_common_prefix(self):
        assert QueueManager._variant_tag("Work 4K.mp4", "Work.mp4") == "4K"

    def test_nothing_distinguishing(self):
        assert QueueManager._variant_tag("Work.mp4", "Work (x).mp4") == ""

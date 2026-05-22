"""Tests for QueueManager._organize_output (erodeck variant naming)."""
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

    def test_suckManual_unknown(self):
        # suckManual is not in erodeck's known list → L0
        assert QueueManager._parse_axis("video.suckManual.funscript") == ("L0", "")


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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "subfolder"
            qm._organize_output(pair)

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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "Axis Test.mp4").exists()
        assert (tmp_path / "Axis Test.funscript").exists()
        assert (tmp_path / "Axis Test.pitch.funscript").exists()
        assert (tmp_path / "Axis Test.roll.funscript").exists()


class TestAxisCollision:
    """Test axis collision detection → alt variant structure."""

    def test_two_L0_variants_max_plus(self, tmp_path):
        """Two scripts both mapping to L0 (unknown suffixes) → alt structure."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "Wednesday.L0.max.funscript")
        _touch(tmp_path / "Wednesday.L0.plus.funscript")

        pair = _make_pair(str(tmp_path), "Wednesday", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/max.funscript", filename="Wednesday.L0.max.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/plus.funscript", filename="Wednesday.L0.plus.funscript", file_type=FileType.FUNSCRIPT),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # First L0 script → primary in root (with L0 suffix since _parse_axis returns "L0")
        assert (tmp_path / "Wednesday.L0.funscript").exists()
        # Second L0 script → alt subfolder
        alt_dir = tmp_path / "Wednesday.alt"
        assert alt_dir.is_dir()
        assert (alt_dir / "Wednesday.alt.L0.funscript").exists()
        # Hardlinked video
        assert (tmp_path / "Wednesday.alt" / "Wednesday.alt.mp4").exists()

    def test_unknown_suffixes_collide_on_L0(self, tmp_path):
        """Two unknown-suffix scripts → both map to L0 → collision → alt."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "video.max.funscript")
        _touch(tmp_path / "video.plus.funscript")

        pair = _make_pair(str(tmp_path), "Video", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/max.funscript", filename="video.max.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/plus.funscript", filename="video.plus.funscript", file_type=FileType.FUNSCRIPT),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # First → primary as main (no suffix since display_suffix is "")
        assert (tmp_path / "Video.funscript").exists()
        # Second → alt
        alt_dir = tmp_path / "Video.alt"
        assert alt_dir.is_dir()
        assert (alt_dir / "Video.alt.funscript").exists()

    def test_main_plus_unknown_collide(self, tmp_path):
        """Plain .funscript + .max.funscript → both L0 → collision."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "video.funscript")
        _touch(tmp_path / "video.max.funscript")

        pair = _make_pair(str(tmp_path), "Video", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s1.funscript", filename="video.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/s2.funscript", filename="video.max.funscript", file_type=FileType.FUNSCRIPT),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "Video.funscript").exists()
        alt_dir = tmp_path / "Video.alt"
        assert alt_dir.is_dir()
        assert (alt_dir / "Video.alt.funscript").exists()

    def test_no_collision_different_axes(self, tmp_path):
        """Different known axes → no collision → all stay in root."""
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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "Multi.funscript").exists()
        assert (tmp_path / "Multi.pitch.funscript").exists()
        assert (tmp_path / "Multi.surge.funscript").exists()
        assert not (tmp_path / "Multi.alt").exists()

    def test_axis_collision_with_multiaxis(self, tmp_path):
        """Multi-axis + two L0 variants: non-L0 axes stay flat, L0 extras → alt."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")
        _touch(tmp_path / "s.pitch.funscript")
        _touch(tmp_path / "s.max.funscript")  # unknown → L0 collision with main

        pair = _make_pair(str(tmp_path), "Mixed", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/p.funscript", filename="s.pitch.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/m.funscript", filename="s.max.funscript", file_type=FileType.FUNSCRIPT),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # Main L0 + pitch stay in root
        assert (tmp_path / "Mixed.funscript").exists()
        assert (tmp_path / "Mixed.pitch.funscript").exists()
        # "max" collides with main L0 → alt
        alt_dir = tmp_path / "Mixed.alt"
        assert alt_dir.is_dir()
        assert (alt_dir / "Mixed.alt.funscript").exists()

    def test_three_L0_variants(self, tmp_path):
        """Three L0 scripts → 1 primary + 2 alts (.alt, .alt1)."""
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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "Three.funscript").exists()
        assert (tmp_path / "Three.alt" / "Three.alt.funscript").exists()
        assert (tmp_path / "Three.alt1" / "Three.alt1.funscript").exists()

    def test_linkinfo_written_for_axis_collision(self, tmp_path):
        """Axis collision alt folders should generate .linkinfo."""
        _touch(tmp_path / "v.mp4", size=200)
        _touch(tmp_path / "s1.funscript")
        _touch(tmp_path / "s2.funscript")

        pair = _make_pair(str(tmp_path), "Link", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s1.funscript", filename="s1.funscript", file_type=FileType.FUNSCRIPT),
            PairItem(url="http://x/s2.funscript", filename="s2.funscript", file_type=FileType.FUNSCRIPT),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        linkinfo = tmp_path / ".linkinfo"
        assert linkinfo.exists()
        content = linkinfo.read_text(encoding="utf-8")
        assert "[hardlink]" in content
        assert "Link.alt" in content


class TestOrganizeOutputSubfolder:
    """Test subfolder mode (multi-author erodeck variant structure)."""

    def test_two_authors_alt_structure(self, tmp_path):
        """2nd author → .alt subfolder with hardlinked video."""
        _touch(tmp_path / "v.mp4", size=200)
        _touch(tmp_path / "a_script.funscript")
        _touch(tmp_path / "b_script.funscript")

        pair = _make_pair(str(tmp_path), "TwoAuth", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/a.funscript", filename="a_script.funscript", file_type=FileType.FUNSCRIPT, author="Alice"),
            PairItem(url="http://x/b.funscript", filename="b_script.funscript", file_type=FileType.FUNSCRIPT, author="Bob"),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "subfolder"
            qm._organize_output(pair)

        # Primary author (Alice) in root
        assert (tmp_path / "TwoAuth.mp4").exists()
        assert (tmp_path / "TwoAuth.funscript").exists()

        # Alt author (Bob) in .alt subfolder
        alt_dir = tmp_path / "TwoAuth.alt"
        assert alt_dir.is_dir()
        assert (alt_dir / "TwoAuth.alt.funscript").exists()

        # Hardlinked video
        alt_video = alt_dir / "TwoAuth.alt.mp4"
        assert alt_video.exists()
        # Verify it's a hardlink (same inode)
        assert os.path.samefile(str(tmp_path / "TwoAuth.mp4"), str(alt_video))

        # .linkinfo file
        linkinfo = tmp_path / ".linkinfo"
        assert linkinfo.exists()
        content = linkinfo.read_text(encoding="utf-8")
        assert "[hardlink]" in content
        assert "TwoAuth.alt" in content

    def test_three_authors_alt1(self, tmp_path):
        """3rd author → .alt1 subfolder."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s1.funscript")
        _touch(tmp_path / "s2.funscript")
        _touch(tmp_path / "s3.funscript")

        pair = _make_pair(str(tmp_path), "ThreeAuth", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s1.funscript", filename="s1.funscript", file_type=FileType.FUNSCRIPT, author="A"),
            PairItem(url="http://x/s2.funscript", filename="s2.funscript", file_type=FileType.FUNSCRIPT, author="B"),
            PairItem(url="http://x/s3.funscript", filename="s3.funscript", file_type=FileType.FUNSCRIPT, author="C"),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "subfolder"
            qm._organize_output(pair)

        # Primary (A) in root
        assert (tmp_path / "ThreeAuth.funscript").exists()
        # B → .alt
        assert (tmp_path / "ThreeAuth.alt" / "ThreeAuth.alt.funscript").exists()
        assert (tmp_path / "ThreeAuth.alt" / "ThreeAuth.alt.mp4").exists()
        # C → .alt1
        assert (tmp_path / "ThreeAuth.alt1" / "ThreeAuth.alt1.funscript").exists()
        assert (tmp_path / "ThreeAuth.alt1" / "ThreeAuth.alt1.mp4").exists()

        # .linkinfo has 2 entries
        content = (tmp_path / ".linkinfo").read_text(encoding="utf-8")
        assert content.count("[hardlink]") == 2

    def test_multiaxis_with_alt(self, tmp_path):
        """Axis suffixes in alt subfolders: .alt.pitch.funscript"""
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

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "subfolder"
            qm._organize_output(pair)

        # Primary (A) in root
        assert (tmp_path / "MultiAx.funscript").exists()
        assert (tmp_path / "MultiAx.pitch.funscript").exists()
        # Alt (B) in .alt with axis suffix
        alt_dir = tmp_path / "MultiAx.alt"
        assert (alt_dir / "MultiAx.alt.funscript").exists()
        assert (alt_dir / "MultiAx.alt.pitch.funscript").exists()

    def test_subfolder_mode_but_single_author_stays_flat(self, tmp_path):
        """Even with subfolder mode, single author + no collision → no alt folders."""
        _touch(tmp_path / "v.mp4")
        _touch(tmp_path / "s.funscript")

        pair = _make_pair(str(tmp_path), "Single", [
            PairItem(url="http://x/v.mp4", filename="v.mp4", file_type=FileType.VIDEO),
            PairItem(url="http://x/s.funscript", filename="s.funscript", file_type=FileType.FUNSCRIPT, author="OnlyOne"),
        ])

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "subfolder"
            qm._organize_output(pair)

        assert (tmp_path / "Single.funscript").exists()
        assert not (tmp_path / "Single.alt").exists()
        assert not (tmp_path / ".linkinfo").exists()


class TestCleanTitle:
    def test_removes_multi_axis_tag(self):
        assert QueueManager._clean_title("(multi-axis) Cool Video") == "Cool Video"

    def test_removes_free_tag(self):
        assert QueueManager._clean_title("My Script (Free)") == "My Script"

    def test_preserves_normal_title(self):
        assert QueueManager._clean_title("Just a Normal Title") == "Just a Normal Title"


class TestExplicitGroups:
    """Pairs with explicit `item.group` and Pair.alt_group_config — the
    layout produced by the new EroScripts picker UI."""

    def test_alt_with_own_video_and_inherit_axes(self, tmp_path):
        """Comment with its own video + Main multi-axis → Alt folder
        gets the comment's video + the comment's main script + Main's
        non-L0 axes hardlinked in."""
        _touch(tmp_path / "main.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "main.surge.funscript")
        _touch(tmp_path / "main.pitch.funscript")
        _touch(tmp_path / "alt.mp4")
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="main.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/p.funscript", filename="main.pitch.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.mp4", filename="alt.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": True}}

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # Main in root
        assert (tmp_path / "Topic.mp4").exists()
        assert (tmp_path / "Topic.funscript").exists()
        assert (tmp_path / "Topic.surge.funscript").exists()
        assert (tmp_path / "Topic.pitch.funscript").exists()

        # Alt in .alt/, with its OWN video (moved, not hardlinked from Main)
        alt_dir = tmp_path / "Topic.alt"
        assert (alt_dir / "Topic.alt.mp4").exists()
        # Alt's own video is NOT a hardlink to Main video (they had different content)
        assert not os.path.samefile(str(tmp_path / "Topic.mp4"), str(alt_dir / "Topic.alt.mp4"))

        # Alt's own main funscript
        assert (alt_dir / "Topic.alt.funscript").exists()
        # Inherited Main multi-axis (hardlinks)
        assert (alt_dir / "Topic.alt.surge.funscript").exists()
        assert (alt_dir / "Topic.alt.pitch.funscript").exists()
        assert os.path.samefile(
            str(tmp_path / "Topic.surge.funscript"),
            str(alt_dir / "Topic.alt.surge.funscript"),
        )

        # .linkinfo records the inherited hardlinks
        content = (tmp_path / ".linkinfo").read_text(encoding="utf-8")
        assert content.count("[hardlink]") == 2
        assert "Topic.alt.surge.funscript" in content
        assert "Topic.alt.pitch.funscript" in content

    def test_alt_inherit_disabled(self, tmp_path):
        """`inherit_multi_axis=False` → Alt gets its own video + main
        script, but Main's other axes do NOT propagate."""
        _touch(tmp_path / "main.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "main.surge.funscript")
        _touch(tmp_path / "alt.mp4")
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="main.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.mp4", filename="alt.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": False}}

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        alt_dir = tmp_path / "Topic.alt"
        assert (alt_dir / "Topic.alt.mp4").exists()
        assert (alt_dir / "Topic.alt.funscript").exists()
        # No inheritance
        assert not (alt_dir / "Topic.alt.surge.funscript").exists()

    def test_two_alts_each_with_own_video(self, tmp_path):
        """The EroScripts case: OP + 2 comments each with their own video.
        Both alts inherit Main's non-L0 axes."""
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "op.surge.funscript")
        _touch(tmp_path / "op.pitch.funscript")
        _touch(tmp_path / "c1.mp4")
        _touch(tmp_path / "c1.funscript")
        _touch(tmp_path / "c2.mp4")
        _touch(tmp_path / "c2.funscript")

        pair = _make_pair(str(tmp_path), "Lingyu", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="op.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/p.funscript", filename="op.pitch.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.mp4", filename="c1.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
            PairItem(url="http://x/c2.mp4", filename="c2.mp4", file_type=FileType.VIDEO, group="Alt 2"),
            PairItem(url="http://x/c2.funscript", filename="c2.funscript", file_type=FileType.FUNSCRIPT, group="Alt 2"),
        ])
        pair.alt_group_config = {
            "Alt 1": {"inherit_multi_axis": True},
            "Alt 2": {"inherit_multi_axis": True},
        }

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # Main in root
        assert (tmp_path / "Lingyu.mp4").exists()
        assert (tmp_path / "Lingyu.funscript").exists()
        assert (tmp_path / "Lingyu.surge.funscript").exists()
        assert (tmp_path / "Lingyu.pitch.funscript").exists()

        # Alt 1 → .alt/
        alt1 = tmp_path / "Lingyu.alt"
        assert (alt1 / "Lingyu.alt.mp4").exists()
        assert (alt1 / "Lingyu.alt.funscript").exists()
        assert (alt1 / "Lingyu.alt.surge.funscript").exists()
        assert (alt1 / "Lingyu.alt.pitch.funscript").exists()

        # Alt 2 → .alt1/
        alt2 = tmp_path / "Lingyu.alt1"
        assert (alt2 / "Lingyu.alt1.mp4").exists()
        assert (alt2 / "Lingyu.alt1.funscript").exists()
        assert (alt2 / "Lingyu.alt1.surge.funscript").exists()
        assert (alt2 / "Lingyu.alt1.pitch.funscript").exists()

        # Alt videos are NOT hardlinks to Main video
        assert not os.path.samefile(
            str(tmp_path / "Lingyu.mp4"), str(alt1 / "Lingyu.alt.mp4")
        )
        assert not os.path.samefile(
            str(tmp_path / "Lingyu.mp4"), str(alt2 / "Lingyu.alt1.mp4")
        )

        # Inherited multi-axis funscripts ARE hardlinks back to Main
        assert os.path.samefile(
            str(tmp_path / "Lingyu.surge.funscript"),
            str(alt1 / "Lingyu.alt.surge.funscript"),
        )
        assert os.path.samefile(
            str(tmp_path / "Lingyu.surge.funscript"),
            str(alt2 / "Lingyu.alt1.surge.funscript"),
        )

    def test_alt_display_name_drives_subfolder(self, tmp_path):
        """Alt with `display_name` gets its own meaningful subfolder
        name (e.g. `异域风情.alt/`) instead of the bland `Topic.alt/`."""
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "op.surge.funscript")
        _touch(tmp_path / "c1.mp4")
        _touch(tmp_path / "c1.funscript")

        pair = _make_pair(str(tmp_path), "Lingyu", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/s.funscript", filename="op.surge.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.mp4", filename="c1.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {
            "Alt 1": {"inherit_multi_axis": True, "display_name": "异域风情"},
        }

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        # Subfolder uses the display name (no .altN numbering)
        alt_dir = tmp_path / "异域风情.alt"
        assert alt_dir.is_dir()
        # And there's no fallback Lingyu.alt sitting around
        assert not (tmp_path / "Lingyu.alt").exists()

        # Files inside share the same stem
        assert (alt_dir / "异域风情.alt.mp4").exists()
        assert (alt_dir / "异域风情.alt.funscript").exists()
        assert (alt_dir / "异域风情.alt.surge.funscript").exists()
        # Inherited surge is a hardlink to Main
        assert os.path.samefile(
            str(tmp_path / "Lingyu.surge.funscript"),
            str(alt_dir / "异域风情.alt.surge.funscript"),
        )

    def test_alt_empty_display_name_falls_back(self, tmp_path):
        """`display_name` whitespace/empty → use Topic.altN fallback."""
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "c1.mp4")
        _touch(tmp_path / "c1.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.mp4", filename="c1.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {
            "Alt 1": {"inherit_multi_axis": False, "display_name": "   "},
        }

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "Topic.alt" / "Topic.alt.mp4").exists()
        assert (tmp_path / "Topic.alt" / "Topic.alt.funscript").exists()

    def test_two_alts_same_display_name_disambiguate(self, tmp_path):
        """Two Alts with the same display name → second gets `-2` suffix
        before `.alt`, keeping erodeck pattern intact."""
        _touch(tmp_path / "op.mp4")
        _touch(tmp_path / "op.funscript")
        _touch(tmp_path / "c1.mp4")
        _touch(tmp_path / "c1.funscript")
        _touch(tmp_path / "c2.mp4")
        _touch(tmp_path / "c2.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/op.mp4", filename="op.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/op.funscript", filename="op.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/c1.mp4", filename="c1.mp4", file_type=FileType.VIDEO, group="Alt 1"),
            PairItem(url="http://x/c1.funscript", filename="c1.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
            PairItem(url="http://x/c2.mp4", filename="c2.mp4", file_type=FileType.VIDEO, group="Alt 2"),
            PairItem(url="http://x/c2.funscript", filename="c2.funscript", file_type=FileType.FUNSCRIPT, group="Alt 2"),
        ])
        pair.alt_group_config = {
            "Alt 1": {"inherit_multi_axis": False, "display_name": "remake"},
            "Alt 2": {"inherit_multi_axis": False, "display_name": "remake"},
        }

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        assert (tmp_path / "remake.alt" / "remake.alt.mp4").exists()
        assert (tmp_path / "remake-2.alt" / "remake-2.alt.mp4").exists()

    def test_alt_without_video_still_gets_main_video(self, tmp_path):
        """Legacy case: comment scripter posted a script but no video.
        The Alt folder hardlinks the Main video (same as before)."""
        _touch(tmp_path / "main.mp4")
        _touch(tmp_path / "main.funscript")
        _touch(tmp_path / "alt.funscript")

        pair = _make_pair(str(tmp_path), "Topic", [
            PairItem(url="http://x/m.mp4", filename="main.mp4", file_type=FileType.VIDEO, group="Main"),
            PairItem(url="http://x/m.funscript", filename="main.funscript", file_type=FileType.FUNSCRIPT, group="Main"),
            PairItem(url="http://x/a.funscript", filename="alt.funscript", file_type=FileType.FUNSCRIPT, group="Alt 1"),
        ])
        pair.alt_group_config = {"Alt 1": {"inherit_multi_axis": True}}

        qm = QueueManager()
        with patch("funpairdl.persistence.settings.Settings.load") as mock_load:
            mock_load.return_value.script_variant_mode = "flat"
            qm._organize_output(pair)

        alt_dir = tmp_path / "Topic.alt"
        assert (alt_dir / "Topic.alt.mp4").exists()
        # Hardlinked from Main video (same content, just an alternate script)
        assert os.path.samefile(
            str(tmp_path / "Topic.mp4"), str(alt_dir / "Topic.alt.mp4")
        )
        assert (alt_dir / "Topic.alt.funscript").exists()

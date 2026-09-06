"""Tests for QueueManager._reconcile_with_library — merging a re-downloaded
work into its existing library folder (new axes in, changed scripts -> .alt,
identical dropped) and never touching a different work that shares a name."""
import funpairdl.persistence.settings as settings_mod
from funpairdl.core.pair import FileType, Pair, PairItem
from funpairdl.core.queue_manager import QueueManager


def _stub_settings(monkeypatch, **kw):
    s = settings_mod.Settings(**kw)
    monkeypatch.setattr(settings_mod.Settings, "load", lambda *a, **k: s)


def _existing_work(lib, base, video_bytes, scripts):
    """Create lib/<base>/ with a video + given {filename: content} scripts."""
    d = lib / base
    d.mkdir(parents=True)
    (d / f"{base}.mp4").write_bytes(video_bytes)
    for fn, content in scripts.items():
        (d / fn).write_text(content)
    return d


def _redownload_pair(tmp, base, video_bytes, scripts):
    """A freshly-downloaded pair sitting in its own temp folder."""
    temp = tmp / f"{base}__dl"
    temp.mkdir(parents=True)
    (temp / f"{base}.mp4").write_bytes(video_bytes)
    items = [PairItem(url="u/v", filename=f"{base}.mp4", file_type=FileType.VIDEO)]
    for fn, content in scripts.items():
        (temp / fn).write_text(content)
        items.append(PairItem(url="u/" + fn, filename=fn, file_type=FileType.FUNSCRIPT))
    pair = Pair(name=base, items=items)
    pair.output_dir = str(temp)
    return pair, temp


def test_new_axis_merges_into_existing_folder(tmp_path, monkeypatch):
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    work = _existing_work(tmp_path, "Work", b"VIDEO", {"Work.funscript": "MAIN"})
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO",
                                  {"Work.funscript": "MAIN",          # identical -> drop
                                   "Work.roll.funscript": "ROLL"})    # new axis -> merge

    assert qm._reconcile_with_library(pair) is True
    assert (work / "Work.roll.funscript").read_text() == "ROLL"      # new axis added
    assert (work / "Work.funscript").read_text() == "MAIN"           # untouched
    assert not (work / "Work.alt").exists()                          # nothing became a variant
    assert not temp.exists()                                         # temp folder cleaned up


def test_changed_script_becomes_alt_variant(tmp_path, monkeypatch):
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    work = _existing_work(tmp_path, "Work", b"VIDEO", {"Work.funscript": "OLD"})
    pair, _ = _redownload_pair(tmp_path, "Work", b"VIDEO", {"Work.funscript": "NEW"})

    assert qm._reconcile_with_library(pair) is True
    assert (work / "Work.funscript").read_text() == "OLD"            # original kept
    alt = work / "Work.alt"
    assert (alt / "Work.alt.funscript").read_text() == "NEW"         # changed -> variant
    assert (alt / "Work.alt.mp4").exists()                           # video brought in


def test_identical_redownload_is_noop(tmp_path, monkeypatch):
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    work = _existing_work(tmp_path, "Work", b"VIDEO",
                          {"Work.funscript": "A", "Work.pitch.funscript": "P"})
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO",
                                  {"Work.funscript": "A", "Work.pitch.funscript": "P"})

    assert qm._reconcile_with_library(pair) is True
    assert not (work / "Work.alt").exists()
    assert sorted(f.name for f in work.iterdir()) == [
        "Work.funscript", "Work.mp4", "Work.pitch.funscript"]
    assert not temp.exists()


def test_different_media_not_absorbed(tmp_path, monkeypatch):
    # Same name, DIFFERENT video bytes -> a different work; must not merge.
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    work = _existing_work(tmp_path, "Work", b"VIDEO-A", {"Work.funscript": "A"})
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO-B", {"Work.funscript": "B"})

    assert qm._reconcile_with_library(pair) is False
    assert not (work / "Work.alt").exists()
    assert temp.exists()                                             # left for normal organize


def test_extra_library_path_is_scanned(tmp_path, monkeypatch):
    # Existing copy lives in a separate library_paths folder, not download_dir.
    lib = tmp_path / "other_lib"
    lib.mkdir()
    _stub_settings(monkeypatch, reconcile_on_redownload=True, library_paths=[str(lib)])
    qm = QueueManager(download_dir=tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    work = _existing_work(lib, "Work", b"VIDEO", {"Work.funscript": "MAIN"})
    pair, _ = _redownload_pair(tmp_path / "dl", "Work", b"VIDEO",
                               {"Work.surge.funscript": "SURGE"})

    assert qm._reconcile_with_library(pair) is True
    assert (work / "Work.surge.funscript").read_text() == "SURGE"


def test_realistic_bracketed_name_multiaxis(tmp_path, monkeypatch):
    # Real-world shapes: bracketed/punctuated name + multi-axis suffixes.
    # Same post re-downloaded (same name): a new axis merges, a changed axis
    # becomes an .alt variant, identical axes are dropped.
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    base = "(ZZ-TEST-0001)(Casey Sample) Demo Load"
    work = _existing_work(
        tmp_path, base, b"V",
        {f"{base}.funscript": "L0",
         f"{base}.pitch.funscript": "PITCH",
         f"{base}.roll.funscript": "ROLL",
         f"{base}.surge.funscript": "SURGE"},
    )
    temp = tmp_path / "redl"
    temp.mkdir()
    (temp / f"{base}.mp4").write_bytes(b"V")
    items = [PairItem(url="u/v", filename=f"{base}.mp4", file_type=FileType.VIDEO)]
    redl = {
        f"{base}.funscript": "L0",            # identical -> drop
        f"{base}.pitch.funscript": "PITCH-2",  # changed -> .alt
        f"{base}.twist.funscript": "TWIST",    # new axis -> merge into folder
    }
    for fn, content in redl.items():
        (temp / fn).write_text(content)
        items.append(PairItem(url="u/" + fn, filename=fn, file_type=FileType.FUNSCRIPT))
    pair = Pair(name=base, items=items)
    pair.output_dir = str(temp)

    assert qm._reconcile_with_library(pair) is True
    assert (work / f"{base}.twist.funscript").read_text() == "TWIST"   # new axis merged
    assert (work / f"{base}.pitch.funscript").read_text() == "PITCH"   # original kept
    alt = work / f"{base}.alt"
    assert (alt / f"{base}.alt.pitch.funscript").read_text() == "PITCH-2"  # changed -> variant
    assert (alt / f"{base}.alt.mp4").exists()


def test_script_only_into_existing_folder(tmp_path, monkeypatch):
    # The exact failure case: re-download ONLY the scripts (no video) and they
    # land in the already-organized folder with their source names. Identical
    # axes must be dropped, a new axis merged — no duplicate files left.
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    base = "[Multi-axis][VAM]示例中文角色"
    work = tmp_path / base
    work.mkdir()
    (work / f"{base}.mp4").write_bytes(b"VID")
    (work / f"{base}.funscript").write_text("MAIN")
    (work / f"{base}.roll.funscript").write_text("ROLL")

    # script-only re-download dumped into the SAME folder with source names
    src = "VaM-示例中文角色-000101"
    (work / f"{src}.funscript").write_text("MAIN")        # identical -> drop
    (work / f"{src}.roll.funscript").write_text("ROLL")   # identical -> drop
    (work / f"{src}.twist.funscript").write_text("TWIST")  # new axis -> merge
    items = [
        PairItem(url="u/1", filename=f"{src}.funscript", file_type=FileType.FUNSCRIPT),
        PairItem(url="u/2", filename=f"{src}.roll.funscript", file_type=FileType.FUNSCRIPT),
        PairItem(url="u/3", filename=f"{src}.twist.funscript", file_type=FileType.FUNSCRIPT),
    ]
    pair = Pair(name=base, items=items)
    pair.output_dir = str(work)

    assert qm._reconcile_with_library(pair) is True
    names = sorted(f.name for f in work.iterdir() if f.is_file())
    assert names == [
        f"{base}.funscript", f"{base}.mp4",
        f"{base}.roll.funscript", f"{base}.twist.funscript",
    ]  # source-named dups gone, new twist axis merged under the work's base


def test_cjk_name_matches_separate_folder(tmp_path, monkeypatch):
    # CJK-named work in a separate library folder: _match_key must keep the
    # Chinese characters so the script-only re-download still finds it.
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    base = "示例中文角色"
    work = _existing_work(tmp_path / "dl", base, b"VID", {f"{base}.funscript": "MAIN"})
    temp = tmp_path / "dl" / "redl_temp"
    temp.mkdir()
    (temp / f"{base}.surge.funscript").write_text("SURGE")
    pair = Pair(name=base, items=[
        PairItem(url="u/s", filename=f"{base}.surge.funscript", file_type=FileType.FUNSCRIPT)])
    pair.output_dir = str(temp)

    assert qm._reconcile_with_library(pair) is True
    assert (work / f"{base}.surge.funscript").read_text() == "SURGE"


def test_video_download_never_merges_into_a_videoless_folder(tmp_path, monkeypatch):
    """With no video on the library side there is nothing to prove it is the
    same work — merging on the name alone once moved a sibling work's only
    script into a stranger's .alt folder."""
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    lib = tmp_path / "Work"
    lib.mkdir()
    (lib / "Work.funscript").write_text("OTHER")
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO", {"Work.funscript": "MINE"})

    assert qm._reconcile_with_library(pair) is False
    assert (lib / "Work.funscript").read_text() == "OTHER"
    assert not (lib / "Work.alt").exists()
    assert (temp / "Work.funscript").read_text() == "MINE"


def test_folder_of_an_active_sibling_pair_is_not_the_library_copy(tmp_path, monkeypatch):
    """Two auto-split siblings titled alike: one must not be absorbed into
    the other's folder while that one is still downloading."""
    from funpairdl.core.pair import PairState
    _stub_settings(monkeypatch, reconcile_on_redownload=True)
    qm = QueueManager(download_dir=tmp_path)
    work = _existing_work(tmp_path, "Work", b"VIDEO", {"Work.funscript": "SIBLING"})
    sibling = Pair(name="Work", items=[])
    sibling.output_dir = str(work)
    sibling.state = PairState.DOWNLOADING
    qm.pairs.append(sibling)
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO", {"Work.funscript": "MINE"})

    assert qm._reconcile_with_library(pair) is False
    assert (temp / "Work.funscript").read_text() == "MINE"
    # Once the sibling is done, an identical re-download reconciles as before.
    sibling.state = PairState.COMPLETED
    assert qm._reconcile_with_library(pair) is True


def test_toggle_off_skips_reconcile(tmp_path, monkeypatch):
    _stub_settings(monkeypatch, reconcile_on_redownload=False)
    qm = QueueManager(download_dir=tmp_path)
    _existing_work(tmp_path, "Work", b"VIDEO", {"Work.funscript": "A"})
    pair, temp = _redownload_pair(tmp_path, "Work", b"VIDEO", {"Work.funscript": "B"})

    assert qm._reconcile_with_library(pair) is False
    assert temp.exists()

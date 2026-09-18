"""tools/migrate_alt_layout.py: legacy .alt subfolders → flat variants."""
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("migrate_alt_layout", ROOT / "tools" / "migrate_alt_layout.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def _w(p: Path, content: bytes = b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def _run(work: Path, apply: bool):
    plans = mig.plan_work(work)
    if apply:
        for p in plans:
            if p["status"] == "migrate":
                assert mig.apply_plan(p) == ""
    return plans


def test_hardlinked_alt_is_flattened(tmp_path):
    w = tmp_path / "Work"
    _w(w / "Work.mp4", b"VIDEO")
    _w(w / "Work.funscript", b"MAIN")
    _w(w / "Work.pitch.funscript", b"PITCH")
    _w(w / "Work.roll.funscript", b"ROLL")
    alt = w / "Work.alt"
    alt.mkdir()
    os.link(w / "Work.mp4", alt / "Work.alt.mp4")
    _w(alt / "Work.alt.funscript", b"ALT-L0")
    os.link(w / "Work.pitch.funscript", alt / "Work.alt.pitch.funscript")   # inherited axis
    _w(alt / "Work.alt.roll.funscript", b"ALT-ROLL")                         # its own roll
    alt1 = w / "Work.alt1"
    alt1.mkdir()
    os.link(w / "Work.mp4", alt1 / "Work.alt1.mp4")
    _w(alt1 / "Work.alt1.funscript", b"ALT1-L0")
    _w(w / ".linkinfo", b"[hardlink]\n")

    plans = _run(w, apply=False)
    assert [p["status"] for p in plans] == ["migrate", "migrate"]
    assert plans[0]["label"] == "Alt" and plans[1]["label"] == "Alt 1"
    assert alt.exists()                                      # dry run touched nothing

    _run(w, apply=True)
    assert not alt.exists() and not alt1.exists()
    assert (w / "Work (Alt).funscript").read_bytes() == b"ALT-L0"
    assert (w / "Work (Alt).roll.funscript").read_bytes() == b"ALT-ROLL"
    assert not (w / "Work (Alt).pitch.funscript").exists()   # inherited link dropped, not copied
    assert (w / "Work (Alt 1).funscript").read_bytes() == b"ALT1-L0"
    assert os.stat(w / "Work.mp4").st_nlink == 1
    assert (w / "Work.pitch.funscript").read_bytes() == b"PITCH"


def test_own_encode_alt_becomes_variant_with_video_and_copy_is_deleted(tmp_path):
    w = tmp_path / "Work"
    _w(w / "Work.mp4", b"VIDEO")
    _w(w / "Work.funscript", b"MAIN")
    alt = w / "Work.alt"
    _w(alt / "Work.alt.mkv", b"ANOTHER ENCODE")          # different content → own video
    _w(alt / "Work.alt.funscript", b"ALT")
    alt1 = w / "Work.alt1"
    _w(alt1 / "Work.alt1.mp4", b"VIDEO")                 # same bytes, not a hardlink → copy
    _w(alt1 / "Work.alt1.funscript", b"ALT1")
    plans = _run(w, apply=True)
    assert [p["status"] for p in plans] == ["migrate", "migrate"]
    assert plans[0].get("own_video") is True and not plans[1].get("own_video")
    assert (w / "Work (Alt).mkv").read_bytes() == b"ANOTHER ENCODE"
    assert (w / "Work (Alt).funscript").read_bytes() == b"ALT"
    assert (w / "Work (Alt 1).funscript").read_bytes() == b"ALT1"
    assert not (w / "Work (Alt 1).mp4").exists()
    assert not alt.exists() and not alt1.exists()
    from funpairdl.core import library as lib
    by = {v["label"]: v for v in lib.scan_variants(w)}
    assert by["Alt"]["video"] == "Work (Alt).mkv" and "video" not in by["Alt 1"]


def test_display_name_alt_and_no_video_alt(tmp_path):
    w = tmp_path / "[Keke] Nicole x Alice (+Part2)"
    _w(w / f"{w.name}.mp4", b"VIDEO")
    _w(w / f"{w.name}.funscript", b"MAIN")
    alt = w / "Nicole x Alice.alt"
    alt.mkdir()
    os.link(w / f"{w.name}.mp4", alt / "Nicole x Alice.alt.mp4")
    _w(alt / "Nicole x Alice.alt.funscript", b"ALT")
    alt2 = w / f"{w.name}.alt1"
    _w(alt2 / f"{w.name}.alt1.funscript", b"SCRIPT ONLY")   # no video at all
    plans = _run(w, apply=True)
    assert [p["status"] for p in plans] == ["migrate", "migrate"]
    assert (w / f"{w.name} (Nicole x Alice).funscript").read_bytes() == b"ALT"
    assert (w / f"{w.name} (Alt 1).funscript").read_bytes() == b"SCRIPT ONLY"
    assert not alt.exists() and not alt2.exists()


def test_label_clash_and_other_files_skip(tmp_path):
    w = tmp_path / "Work"
    _w(w / "Work.mp4", b"VIDEO")
    _w(w / "Work.funscript", b"MAIN")
    _w(w / "Work (Alt).funscript", b"ALREADY")           # flat "(Alt)" exists → .alt gets "Alt 2"
    alt = w / "Work.alt"
    alt.mkdir()
    os.link(w / "Work.mp4", alt / "Work.alt.mp4")
    _w(alt / "Work.alt.funscript", b"ALT")
    alt1 = w / "Work.alt1"
    _w(alt1 / "Work.alt1.funscript", b"X")
    _w(alt1 / "thumb.jpg", b"J")                          # unexpected file → skip
    plans = _run(w, apply=True)
    assert plans[0]["status"] == "migrate" and plans[0]["label"] == "Alt 2"
    assert (w / "Work (Alt 2).funscript").read_bytes() == b"ALT"
    assert plans[1]["status"] == "skip" and "other files" in plans[1]["why"]
    assert (alt1 / "thumb.jpg").exists()


def test_cli_dry_run_then_apply_writes_sidecar_and_skips_trash(tmp_path, monkeypatch, capsys):
    root = tmp_path / "lib"
    w = root / "Work"
    _w(w / "Work.mp4", b"VIDEO")
    _w(w / "Work.funscript", b"MAIN")
    alt = w / "Work.alt"
    alt.mkdir()
    os.link(w / "Work.mp4", alt / "Work.alt.mp4")
    _w(alt / "Work.alt.funscript", b"ALT")
    _w(w / ".linkinfo", b"[hardlink]\n")
    t = root / "_trash" / "20260918-100000" / "Binned"
    _w(t / "Binned.alt" / "Binned.alt.funscript", b"B")
    _w(root / "No Video" / "Only" / "Only.funscript", b"O")
    _w(root / "No Video" / "Only" / "Only.alt" / "Only.alt.funscript", b"O2")
    monkeypatch.setattr(mig, "busy_folders", lambda: set())
    report = tmp_path / "r.md"

    monkeypatch.setattr(sys, "argv", ["x", "--roots", str(root), "--report", str(report)])
    mig.main()
    assert alt.exists() and not (w / "funlib.json").exists()
    assert "Binned" not in report.read_text(encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["x", "--roots", str(root), "--report", str(report), "--apply"])
    mig.main()
    assert not alt.exists() and not (w / ".linkinfo").exists()
    assert (w / "Work (Alt).funscript").exists()
    assert (t / "Binned.alt" / "Binned.alt.funscript").exists()          # bin untouched
    assert (root / "No Video" / "Only" / "Only (Alt).funscript").exists()
    sc = json.loads((w / "funlib.json").read_text(encoding="utf-8"))
    assert {v["label"]: v["files"] for v in sc["variants"]} == {
        "Main": {"L0": "Work.funscript"}, "Alt": {"L0": "Work (Alt).funscript"}}

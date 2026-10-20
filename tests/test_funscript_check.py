"""Downloaded scripts are checked before organize (utils/funscript_check and
QueueManager._check_scripts)."""
import asyncio
import json
from types import SimpleNamespace

from funpairdl.core.pair import FileType, ItemState, PairItem
from funpairdl.core.queue_manager import QueueManager
from funpairdl.utils.funscript_check import SYNCSCRIPT_MAGIC, check_funscript

INT64_MIN = -9223372036854775808


def _script(actions, **extra) -> bytes:
    return json.dumps({"version": "1.0", "actions": actions, **extra}).encode()


def test_a_good_script_passes_untouched():
    res = check_funscript(_script([{"at": 0, "pos": 10}, {"at": 500, "pos": 90}]))
    assert res.fatal is None and res.fixed is None and res.notes == []
    assert res.last_at_ms == 500


def test_files_that_are_no_script_are_fatal():
    assert check_funscript(b"").fatal == "empty file"
    assert check_funscript(b"  <!DOCTYPE html><html>login</html>").fatal.startswith("an HTML page")
    assert check_funscript(b"{not json").fatal == "not JSON"
    assert check_funscript(b"[1, 2]").fatal == "not a funscript object"
    assert check_funscript(_script([])).fatal == "no actions"
    assert check_funscript(_script([{"at": -5, "pos": 1}, {"at": "x", "pos": 2}])).fatal == "no usable actions"


def test_syncscript_binary_is_a_script():
    res = check_funscript(SYNCSCRIPT_MAGIC + b"\x00" * 64)
    assert res.fatal is None and res.fixed is None


def test_a_broken_last_point_is_dropped():
    # the shape of a real case: the last action's time was INT64_MIN
    data = _script([{"at": 0, "pos": 0}, {"at": 1000, "pos": 100}, {"at": INT64_MIN, "pos": 50}])
    res = check_funscript(data)
    assert res.fatal is None and res.fixed is not None
    doc = json.loads(res.fixed)
    assert [a["at"] for a in doc["actions"]] == [0, 1000]
    assert doc["version"] == "1.0"                # everything else kept
    assert "1 broken point(s) dropped" in res.notes


def test_positions_are_clamped_and_actions_sorted():
    res = check_funscript(_script([{"at": 500, "pos": 120}, {"at": 100, "pos": -3}, {"at": 300, "pos": 50}]))
    doc = json.loads(res.fixed)
    assert [(a["at"], a["pos"]) for a in doc["actions"]] == [(100, 0), (300, 50), (500, 100)]
    assert "2 position(s) clamped to 0-100" in res.notes and "actions sorted by time" in res.notes


def test_combined_multi_axis_lists_are_checked_too():
    data = json.dumps({"actions": [], "axes": [
        {"id": "R0", "actions": [{"at": 0, "pos": 50}, {"at": 10 ** 12, "pos": 50}]}]}).encode()
    res = check_funscript(data)
    assert res.fatal is None
    assert json.loads(res.fixed)["axes"][0]["actions"] == [{"at": 0, "pos": 50}]


def test_numeric_strings_are_accepted():
    res = check_funscript(_script([{"at": "0", "pos": "10"}, {"at": "200", "pos": "90"}]))
    assert res.fatal is None and res.fixed is None


def test_a_script_far_longer_than_its_video_is_noted():
    res = check_funscript(_script([{"at": 0, "pos": 0}, {"at": 400_000, "pos": 90}]), video_seconds=120)
    assert res.fixed is None
    assert any("the video is 120s" in n for n in res.notes)


def _run_check(tmp_path, monkeypatch, files: dict, repair=True, foreign=()):
    from funpairdl.persistence import settings as settings_mod
    monkeypatch.setattr(settings_mod.Settings, "load",
                        classmethod(lambda cls: SimpleNamespace(repair_funscripts=repair)))
    items = []
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        it = PairItem(url=f"u/{name}", filename=name, file_type=FileType.FUNSCRIPT)
        it.state = ItemState.COMPLETED
        items.append(it)
    pair = SimpleNamespace(items=items, foreign_files=list(foreign))
    fake = SimpleNamespace(on_item_updated=None)
    asyncio.run(QueueManager._check_scripts(fake, pair, tmp_path))
    return {it.filename: it for it in items}


def test_queue_fails_a_page_saved_as_script_and_repairs_a_broken_point(tmp_path, monkeypatch):
    good = _script([{"at": 0, "pos": 0}, {"at": 900, "pos": 80}])
    broken = _script([{"at": 0, "pos": 0}, {"at": 900, "pos": 80}, {"at": INT64_MIN, "pos": 1}])
    items = _run_check(tmp_path, monkeypatch, {
        "Work.funscript": good,
        "Work.surge.funscript": broken,
        "Other.funscript": b"<html>please log in</html>",
    })
    assert items["Work.funscript"].state == ItemState.COMPLETED
    assert (tmp_path / "Work.funscript").read_bytes() == good
    assert items["Work.surge.funscript"].state == ItemState.COMPLETED
    assert [a["at"] for a in json.loads((tmp_path / "Work.surge.funscript").read_bytes())["actions"]] == [0, 900]
    assert items["Other.funscript"].state == ItemState.FAILED
    assert "Broken script" in items["Other.funscript"].error_message
    assert not (tmp_path / "Other.funscript").exists()


def test_queue_leaves_broken_points_when_repair_is_off_and_never_touches_foreign_files(tmp_path, monkeypatch):
    broken = _script([{"at": 0, "pos": 0}, {"at": INT64_MIN, "pos": 1}])
    items = _run_check(tmp_path, monkeypatch, {"Work.funscript": broken, "Lib.funscript": b"<html></html>"},
                       repair=False, foreign=["Lib.funscript"])
    assert (tmp_path / "Work.funscript").read_bytes() == broken
    assert items["Lib.funscript"].state == ItemState.COMPLETED
    assert (tmp_path / "Lib.funscript").exists()

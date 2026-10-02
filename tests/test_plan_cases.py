"""plan_cases: the planners' inputs kept for regression replay."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from funpairdl.core import plan_cases
from funpairdl.core.pair import FileType, PairItem
from funpairdl.core.queue_manager import QueueManager

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cases(tmp_path):
    plan_cases.enable(tmp_path)
    yield tmp_path
    plan_cases.disable()


def _video_req(urls, durations):
    return {"videos": [{"url": u, "name": f"Sample Work {i}", "source": "OP", "size": 0, "height": 0,
                        "duration": d, "priority": 5.0, "failed": False, "pack": "", "width": 0}
                       for i, (u, d) in enumerate(zip(urls, durations))],
            "prefs": {"pick_mode": "smallest", "min_resolution": "1080", "encode_vs_variant": "ask",
                      "vr_versions": "flat"},
            "decisions": {}, "credits": [], "title": "Sample Work", "scripts": []}


def test_nothing_is_kept_until_enabled(tmp_path):
    plan_cases.disable()
    plan_cases.record("video", "Sample Work", ["https://h/a"], _video_req(["https://h/a"], [10]))
    assert not list(tmp_path.glob("*.json"))


def test_a_fuller_view_of_a_post_replaces_the_thinner_one(cases):
    plan_cases.record("video", "Sample Work", ["https://h/a"], _video_req(["https://h/a"], [10]))
    urls = ["https://h/a", "https://h/b"]
    plan_cases.record("video", "Sample Work", urls, _video_req(urls, [10, 20]))
    files = list(cases.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["urls"] == sorted(urls)


def test_an_unchanged_input_keeps_its_accepted_outcome(cases):
    req = _video_req(["https://h/a"], [10])
    plan_cases.record("video", "Sample Work", ["https://h/a"], req)
    (path, case), = plan_cases.load_all(cases)
    case["expected"], case["accepted"] = plan_cases.run_case(case), True
    plan_cases.save(path, case)
    plan_cases.record("video", "Sample Work", ["https://h/a"], req)
    assert plan_cases.load_all(cases)[0][1]["accepted"] is True


def test_split_cases_replay_the_bundle_planner(cases):
    items = []
    for n in ["Alpha", "Beta"]:
        items.append(PairItem(url=f"u/{n}.mp4", filename=f"{n}.mp4", file_type=FileType.VIDEO))
        items.append(PairItem(url=f"u/{n}.funscript", filename=f"{n}.funscript", file_type=FileType.FUNSCRIPT))
    plan_cases.record("split", "Pack", [i.url for i in items],
                      plan_cases.split_request(items, None, "Pack", None, None, None, None))
    (_, case), = plan_cases.load_all(cases)
    got = plan_cases.run_case(case)
    want = QueueManager().plan_bundle_split(items, None, "Pack")
    assert [g["name"] for g in got] == [g["name"] for g in want]
    assert got[0]["scripts"] == {s.url: s_b for s, s_b in
                                 ((s, want[0]["script_basis"][s.url]) for s in want[0]["scripts"])}


def test_the_tool_accepts_then_reports_what_moved(cases):
    urls = ["https://h/a", "https://h/b"]
    plan_cases.record("video", "Sample Work", urls, _video_req(urls, [10, 20]))
    tool = [sys.executable, str(ROOT / "tools" / "replay_plans.py"), "--dir", str(cases)]
    assert subprocess.run(tool + ["--accept"], capture_output=True).returncode == 0
    assert subprocess.run(tool, capture_output=True).returncode == 0
    (path, case), = plan_cases.load_all(cases)
    case["expected"]["roles"]["https://h/b"] = "alternate"   # as if a rule had changed
    plan_cases.save(path, case)
    run = subprocess.run(tool, capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 1
    assert "role alternate ->" in run.stdout

"""Replay the kept plan cases against the current code.

    python tools/replay_plans.py              # what changed since the cases were accepted
    python tools/replay_plans.py --accept     # store today's outcomes as expected
    python tools/replay_plans.py --only "<title words>" [--accept]

The app keeps every post's planner inputs in plan_cases/ (see
funpairdl/core/plan_cases.py). After a batch has been checked and sent,
--accept makes its outcomes the reference. After a rule change, a plain run
lists every case whose outcome moved: the post you meant to fix, and any
other post the change reached. Exit status 1 when something moved.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from funpairdl.constants import CONFIG_DIR  # noqa: E402
from funpairdl.core import plan_cases  # noqa: E402


def _names(case: dict) -> dict[str, str]:
    req = case["request"]
    out = {}
    for v in req.get("videos") or []:
        out[v["url"]] = v.get("name") or v["url"]
    for i in req.get("items") or []:
        out[i["url"]] = i.get("filename") or i["url"]
    return out


def _short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _diff_video(exp: dict, got: dict, names: dict[str, str]) -> list[str]:
    lines = []
    for u in sorted(set(exp["roles"]) | set(got["roles"])):
        a, b = exp["roles"].get(u), got["roles"].get(u)
        if a != b:
            lines.append(f"role {a} -> {b}: {_short(names.get(u, u))}")
    ea, ga = set(exp.get("ambiguous") or []), set(got.get("ambiguous") or [])
    for u in sorted(ga - ea):
        lines.append(f"now asks about: {_short(names.get(u, u))}")
    for u in sorted(ea - ga):
        lines.append(f"no longer asks about: {_short(names.get(u, u))}")
    et = sorted((g["chosen"], g["tag"]) for g in exp["groups"] if g["chosen"])
    gt = sorted((g["chosen"], g["tag"]) for g in got["groups"] if g["chosen"])
    if not lines and et != gt:
        lines.append(f"variant tags: {[t for _, t in et]} -> {[t for _, t in gt]}")
    return lines


def _work_of(groups: list[dict] | None) -> dict[str, str]:
    return {u: g["name"] for g in groups or [] for u in [*g["videos"], *g["scripts"]]}


def _diff_split(exp, got, names: dict[str, str]) -> list[str]:
    if (exp is None) != (got is None):
        return [f"split: {'no' if exp is None else len(exp)} -> {'no' if got is None else len(got)} works"]
    if exp is None:
        return []
    lines = []
    if len(exp) != len(got):
        lines.append(f"works: {len(exp)} -> {len(got)}")
    ew, gw = _work_of(exp), _work_of(got)
    for u in sorted(set(ew) | set(gw)):
        if ew.get(u) != gw.get(u):
            lines.append(f"{_short(names.get(u, u), 50)}: [{_short(ew.get(u) or '-', 40)}] -> "
                         f"[{_short(gw.get(u) or '-', 40)}]")
    if not lines and [g["name"] for g in exp] != [g["name"] for g in got]:
        lines.append(f"names: {[g['name'] for g in exp]} -> {[g['name'] for g in got]}")
    eb = {u: b for g in exp for u, b in g["scripts"].items()}
    gb = {u: b for g in got for u, b in g["scripts"].items()}
    for u in sorted(set(eb) & set(gb)):
        if eb[u] != gb[u] and ew.get(u) == gw.get(u):
            lines.append(f"basis {eb[u] or '-'} -> {gb[u] or '-'}: {_short(names.get(u, u), 60)}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(CONFIG_DIR / "plan_cases"))
    ap.add_argument("--accept", action="store_true", help="store the current outcomes as expected")
    ap.add_argument("--only", default="", help="cases whose title contains this text")
    ap.add_argument("--new", action="store_true", help="with --accept: only cases never accepted")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"no plan cases in {root}")
        return 0
    cases = [(p, c) for p, c in plan_cases.load_all(root)
             if args.only.lower() in (c.get("title") or "").lower()]
    moved = fresh = same = broken = 0
    for path, case in cases:
        try:
            got = plan_cases.run_case(case)
        except Exception as e:  # a case the planner now chokes on is a finding too
            broken += 1
            print(f"!! {case['kind']:6} {_short(case.get('title') or path.name)}: {type(e).__name__}: {e}")
            continue
        if args.accept:
            if args.new and case.get("accepted"):
                continue
            case["expected"] = got
            case["accepted"] = True
            plan_cases.save(path, case)
            fresh += 1
            continue
        if not case.get("accepted"):
            fresh += 1
            continue
        exp = case.get("expected")
        if exp == got:
            same += 1
            continue
        moved += 1
        names = _names(case)
        lines = (_diff_video(exp, got, names) if case["kind"] == "video"
                 else _diff_split(exp, got, names)) or ["(outcome changed)"]
        print(f"== {case['kind']:6} {_short(case.get('title') or '', 90)}")
        for line in lines[:20]:
            print(f"     {line}")
        if len(lines) > 20:
            print(f"     … {len(lines) - 20} more")
    if args.accept:
        print(f"accepted {fresh} case(s) of {len(cases)}")
        return 0
    print(f"{len(cases)} cases: {same} unchanged, {moved} changed, {fresh} not yet accepted"
          + (f", {broken} failed" if broken else ""))
    return 1 if moved or broken else 0


if __name__ == "__main__":
    sys.exit(main())

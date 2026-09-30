#!/usr/bin/env python3
"""
DAY7: read-only, independent "restored to stable" check, run by `make
day7-check` after EACH strategy experiment (and usable standalone with
the run's DAY7_RUN_ID).

Each experiment already verifies its own restoration in its `finally`;
this is a separate process that re-reads everything from scratch, so a
restoration that only looked right from inside the experiment (stale
in-process state, a later asynchronous change) is still caught before
the next experiment starts. It never mutates: it performs
`day7_strategy.verify_stable()` - `helm get values` equals
helm-values/day7/stable.yaml plus the verified build's image tags (the
run baseline's build must be the current build), every candidate-only
object explicitly absent, every stable container running that build
(day7_running_images), the HTTPRoute 100% stable and current for its generation, the
Gateway current, and external requests served by stable Pods with the
baseline's stable message - plus stable gateway 3/3, app 3/3, state 1/1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_strategy as d7
import kube


def check_workloads(rec: d7.Recorder) -> None:
    for kind, name, expected in (("deployment", kube.GATEWAY_DEPLOYMENT, 3), ("deployment", kube.APP_DEPLOYMENT, 3), ("statefulset", kube.STATE_STATEFULSET, 1)):
        state, obj = d7.get_json_or_none("-n", kube.NAMESPACE, "get", kind, name)
        ready = (obj or {}).get("status", {}).get("readyReplicas")
        rec.record(state == "found" and ready == expected, f"{name} ready {ready}/{expected}")


def main() -> int:
    rec = d7.Recorder()
    print(f"# Day 7 independent stable-state check (context {kube.CONTEXT}, release {kube.HELM_RELEASE_NAME})")
    try:
        d7.require_day7_profile()
        kube.verify_context()
        baseline = d7.load_strategy_baseline()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    d7.verify_stable(rec, baseline["stable_message"], "stable-check")
    check_workloads(rec)
    failures = rec.failures()
    print(f"\n{len(rec.results) - len(failures)}/{len(rec.results)} stable-state checks passed")
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed - the Day 7 release is NOT verified stable; do not start the next experiment", file=sys.stderr)
        return 1
    print("PASS: Day 7 release independently verified at the stable state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

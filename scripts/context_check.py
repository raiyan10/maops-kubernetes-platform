#!/usr/bin/env python3
"""
Fail-closed Day 3 context/topology/node-version verification.

DAY3-INT-M2: runs before any namespace apply, Secret bootstrap, image
load, or deploy step in `make day3-check` to prove, against the LIVE
cluster (never assumed from constants alone):

  - kubectl is actually talking to the isolated `kind-maops-k8s-day3`
    context, and every node name is an exact match for kind's own
    naming convention for this cluster (`kube.verify_context()` - this
    also rejects a prefix-collision cluster/node name such as
    "maops-k8s-day3-staging-control-plane", which a bare `startswith()`
    check would wrongly accept);
  - exactly 3 nodes, exactly 1 control-plane (identified dynamically via
    the `node-role.kubernetes.io/control-plane` label, never a
    hardcoded name), exactly 2 workers, and all 3 Ready
    (`scheduling_check.check_node_topology()`, reused directly rather
    than re-implemented);
  - the node version is exactly the pinned `v1.36.1`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import scheduling_check
from kube import get_json

EXPECTED_K8S_VERSION = "v1.36.1"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def main() -> int:
    print(f"# Day 3 context/topology/node-version verification (expected context: {kube.CONTEXT})")
    try:
        kube.verify_context()
        record(True, f"context {kube.CONTEXT!r} verified against live cluster node identity")
    except RuntimeError as exc:
        record(False, str(exc))
        print(f"FAIL: 1 context check(s) failed", file=sys.stderr)
        return 1

    scheduling_check.results = []
    scheduling_check.check_node_topology()

    version = get_json("version").get("serverVersion", {}).get("gitVersion")
    record(version == EXPECTED_K8S_VERSION, f"server version {version} (expected {EXPECTED_K8S_VERSION})")

    all_results = results + scheduling_check.results
    failures = [m for ok, m in all_results if not ok]
    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} context checks passed")
    if failures:
        print(f"FAIL: {len(failures)} context check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Day 3 context, topology, and node version verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

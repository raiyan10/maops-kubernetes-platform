#!/usr/bin/env python3
"""
DAY7: bounded wait for all three Day 7 kind nodes to become Ready,
run between `cni-install` and `cni-status` in `make day7-check`.

Why (run dd99769f587d4a869628d51b9725b458, 2026-09-28): `cni-install`
returns once the Cilium DaemonSet/operator rollouts succeed, but each
node's kubelet reports Ready a few seconds later, once it observes the
CNI configuration. `cni-status` - deliberately a truthful single-snapshot
read - then saw 1/3 nodes Ready at 09:33:21; both workers became Ready
at 09:33:25. This step closes that cold-start race WITHOUT weakening
`cni-status`: it waits (bounded) for the condition, and `cni-status`
still independently asserts 3/3 Ready afterwards.

Scope and failure rules:
  - Day 7 only. Refuses unless the Day 7 profile is selected AND the
    resolved context is exactly `kind-maops-k8s-day7` with the Day 7
    kubeconfig; then `kube.verify_context()` proves the live node names
    belong to maops-k8s-day7. Every call passes that explicit
    --kubeconfig/--context - the Day 6 cluster is never inspected.
  - Read-only: `kubectl get nodes -o json` only.
  - Success requires exactly 3 nodes, all named for maops-k8s-day7,
    exactly one control-plane, and every node's Ready condition True.
  - Any API error, kubectl timeout or unparseable response FAILS
    immediately (never retried into a pass); not reaching 3/3 Ready
    within the timeout FAILS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube

EXPECTED_CONTEXT = "kind-maops-k8s-day7"
EXPECTED_NODES = 3
TIMEOUT_SECONDS = float(os.environ.get("DAY7_NODES_READY_TIMEOUT_SECONDS", "180"))
POLL_SECONDS = 3.0
KUBECTL_TIMEOUT_SECONDS = 20.0


class NodeReadinessError(Exception):
    """API error / malformed response - fatal, never retried."""


def evaluate_nodes(items: list[dict]) -> tuple[bool, str]:
    """Pure. (all expected nodes Ready, detail)."""
    names = [n.get("metadata", {}).get("name", "") for n in items]
    foreign = [n for n in names if not kube._DAY_NODE_NAME_RE.match(n)]
    if foreign:
        raise NodeReadinessError(f"node(s) {foreign} do not belong to {kube.CLUSTER_NAME} - wrong cluster")
    control_planes = [n for n in items if kube.CONTROL_PLANE_LABEL in (n.get("metadata", {}).get("labels") or {})]
    ready = sorted(
        n.get("metadata", {}).get("name", "")
        for n in items
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", []) or [])
    )
    ok = len(items) == EXPECTED_NODES and len(control_planes) == 1 and len(ready) == EXPECTED_NODES
    return ok, f"{len(ready)}/{len(items)} nodes Ready {ready} (expected exactly {EXPECTED_NODES}, 1 control-plane; found {len(control_planes)})"


def read_nodes() -> list[dict]:
    require_day7_scope()  # guard at the call site, not only in main()
    try:
        result = kube.run("get", "nodes", "-o", "json", check=False, timeout=KUBECTL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise NodeReadinessError(f"kubectl get nodes timed out after {KUBECTL_TIMEOUT_SECONDS}s") from exc
    if result.returncode != 0:
        raise NodeReadinessError(f"kubectl get nodes failed: {(result.stderr or '').strip()[:300]}")
    try:
        items = json.loads(result.stdout).get("items")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise NodeReadinessError(f"unparseable kubectl get nodes response: {exc}") from exc
    if not isinstance(items, list):
        raise NodeReadinessError("kubectl get nodes response has no items list")
    return items


def require_day7_scope() -> None:
    if kube.PROFILE != "day7" or kube.CONTEXT != EXPECTED_CONTEXT:
        raise NodeReadinessError(
            f"refusing to run outside the Day 7 cluster (profile {kube.PROFILE!r}, context {kube.CONTEXT!r}; "
            f"expected profile 'day7', context {EXPECTED_CONTEXT!r})"
        )
    other_days = [f"maops-k8s-day{n}" for n in range(1, 7)]
    if any(name in kube.KUBECONFIG_PATH for name in other_days):
        raise NodeReadinessError(f"refusing kubeconfig {kube.KUBECONFIG_PATH!r}: it names another day's cluster")


def wait_for_nodes(timeout: float = TIMEOUT_SECONDS, poll: float = POLL_SECONDS, sleep=time.sleep, monotonic=time.monotonic) -> tuple[bool, str]:
    """Polls until all expected nodes are Ready. Returns (ok, detail);
    raises NodeReadinessError on any API error."""
    deadline = monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        ok, detail = evaluate_nodes(read_nodes())
        print(f"  attempt {attempts}: {detail}", flush=True)
        if ok:
            return True, f"{detail} after {attempts} observation(s)"
        if monotonic() >= deadline:
            return False, f"timed out after {timeout:.0f}s: {detail}"
        sleep(poll)


def main() -> int:
    print(f"# Day 7 node readiness wait (context {kube.CONTEXT}, bounded {TIMEOUT_SECONDS:.0f}s, read-only)")
    try:
        require_day7_scope()
        kube.verify_context()
        ok, detail = wait_for_nodes()
    except (NodeReadinessError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"FAIL: {detail}", file=sys.stderr)
        return 1
    print(f"PASS: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

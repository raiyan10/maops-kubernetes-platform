#!/usr/bin/env python3
"""
DAY6: read-only verification that Istio ambient (istiod, istio-cni,
ztunnel) is installed and healthy - the mesh analogue of
scripts/cni_check.py. Never mutates anything - installing the mesh
itself is `make mesh-install` (Helm, in the documented base -> istiod
-> cni -> ztunnel order), a separate, explicit, mutating step.

Runs only against a live cluster (never part of the cluster-free
`make ci-check`), immediately after `make mesh-install` in
`make day6-check`'s sequence; see docs/architecture.md's "DAY6: live
validation record" for the recorded Day 6 run.

Proves, against the live cluster:
  1. istiod Deployment (istio-system) has at least one available
     replica.
  2. The istio-cni-node DaemonSet (istio-system) has exactly as many
     Ready Pods as there are nodes - one CNI agent per node.
  3. The ztunnel DaemonSet (istio-system) has exactly as many Ready
     Pods as there are nodes - one per-node proxy, the ambient dataplane
     itself.

Deeper proof (strict mTLS, ambient enrollment with no sidecars,
identity-scoped AuthorizationPolicy allow/deny paths) is a separate
script, scripts/mesh_check.py - this one is a fast Ready/health gate
only, mirroring cni_check.py's own scope split from
networkpolicy_check.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import get_json

EXPECTED_NODE_COUNT = 3
ISTIOD_DEPLOYMENT = "istiod"
ISTIO_CNI_LABEL_SELECTOR = "k8s-app=istio-cni-node"
ZTUNNEL_LABEL_SELECTOR = "app=ztunnel"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def check_node_count() -> int:
    nodes = get_json("get", "nodes")["items"]
    record(len(nodes) == EXPECTED_NODE_COUNT, f"{len(nodes)} nodes found (expected {EXPECTED_NODE_COUNT})")
    return len(nodes)


def check_istiod() -> None:
    result = kube.run("-n", kube.ISTIO_NAMESPACE, "get", "deployment", ISTIOD_DEPLOYMENT, "-o", "json", check=False)
    if result.returncode != 0:
        record(False, f"istiod Deployment not found in {kube.ISTIO_NAMESPACE!r}")
        return
    import json

    deployment = json.loads(result.stdout)
    available = deployment.get("status", {}).get("availableReplicas", 0)
    record(available >= 1, f"istiod Deployment: availableReplicas={available} (expected >= 1)")


def check_daemonset(label_selector: str, description: str, expected_count: int) -> None:
    pods = get_json("-n", kube.ISTIO_NAMESPACE, "get", "pods", "-l", label_selector)["items"]
    ready_pods = [p for p in pods if _is_ready(p)]
    record(
        len(pods) == expected_count and len(ready_pods) == expected_count,
        f"{description}: {len(ready_pods)}/{len(pods)} Pods Ready (expected {expected_count}, one per node)",
    )


def main() -> int:
    print(f"# Day 6 mesh status check: istiod/istio-cni/ztunnel (context {kube.CONTEXT}) - READ ONLY, never mutates")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    node_count = check_node_count()
    check_istiod()
    check_daemonset(kube.ISTIO_CNI_LABEL_SELECTOR, "istio-cni-node DaemonSet", node_count)
    check_daemonset(kube.ZTUNNEL_LABEL_SELECTOR, "ztunnel DaemonSet", node_count)

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} mesh status checks passed")
    if failures:
        print(f"FAIL: {len(failures)} mesh status check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Istio ambient (istiod/istio-cni/ztunnel) is installed and healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

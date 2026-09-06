#!/usr/bin/env python3
"""
Real Day 3 scheduling/topology validation.

Proves, against the live 1-control-plane + 2-worker kind cluster:

  - the cluster has exactly the intended node topology (1 control-plane,
    2 workers), all Ready;
  - for BOTH maops-gateway and maops-app: 3 Ready Pods, zero of them on
    the control-plane node, both worker nodes host at least one Pod of
    that workload, and the per-workload worker replica-count skew is
    <= 1 (i.e. 2/1 or 1/2, never 3/0).

Node identity is discovered dynamically via the
`node-role.kubernetes.io/control-plane` label - no node name is ever
hardcoded, so this works regardless of what kind happens to name the
worker nodes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import (
    APP_LABEL_SELECTOR,
    CONTROL_PLANE_LABEL,
    GATEWAY_LABEL_SELECTOR,
    NAMESPACE,
    get_json,
)

EXPECTED_NODE_COUNT = 3
EXPECTED_WORKER_COUNT = 2
EXPECTED_REPLICAS = 3
MAX_WORKER_SKEW = 1

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _is_control_plane(node: dict) -> bool:
    return CONTROL_PLANE_LABEL in (node.get("metadata", {}).get("labels") or {})


def _is_ready(node_or_pod: dict) -> bool:
    conditions = node_or_pod.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def check_node_topology() -> tuple[list[str], list[str]]:
    """Returns (control_plane_names, worker_names). Both discovered
    dynamically from the live cluster, never hardcoded."""
    nodes = get_json("get", "nodes")["items"]
    record(
        len(nodes) == EXPECTED_NODE_COUNT,
        f"cluster has {len(nodes)} node(s) (expected {EXPECTED_NODE_COUNT})",
    )
    ready = [n for n in nodes if _is_ready(n)]
    record(len(ready) == len(nodes), f"{len(ready)}/{len(nodes)} nodes Ready")

    control_planes = [n["metadata"]["name"] for n in nodes if _is_control_plane(n)]
    workers = [n["metadata"]["name"] for n in nodes if not _is_control_plane(n)]
    record(len(control_planes) == 1, f"exactly one control-plane node found: {control_planes}")
    record(
        len(workers) == EXPECTED_WORKER_COUNT,
        f"exactly {EXPECTED_WORKER_COUNT} worker node(s) found: {workers}",
    )
    return control_planes, workers


def _pod_node_name(pod: dict) -> str | None:
    return pod.get("spec", {}).get("nodeName")


def check_workload_scheduling(component: str, label_selector: str, control_planes: list[str], workers: list[str]) -> None:
    pods = get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]
    ready_pods = [p for p in pods if _is_ready(p)]
    record(
        len(pods) == EXPECTED_REPLICAS and len(ready_pods) == EXPECTED_REPLICAS,
        f"{component}: {len(ready_pods)}/{len(pods)} Pods Ready (expected {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS})",
    )

    node_names = [_pod_node_name(p) for p in pods]
    on_control_plane = [n for n in node_names if n in control_planes]
    record(
        len(on_control_plane) == 0,
        f"{component}: zero Pods scheduled on the control-plane node (found on: {on_control_plane})",
    )

    per_worker_counts = {w: node_names.count(w) for w in workers}
    used_workers = [w for w, count in per_worker_counts.items() if count > 0]
    record(
        len(used_workers) == len(workers),
        f"{component}: both worker nodes host at least one Pod: {per_worker_counts}",
    )

    counts = list(per_worker_counts.values())
    skew = (max(counts) - min(counts)) if counts else 0
    record(
        skew <= MAX_WORKER_SKEW,
        f"{component}: worker replica-count skew == {skew} (expected <= {MAX_WORKER_SKEW}), distribution {per_worker_counts}",
    )


def main() -> int:
    print(f"# Real Day 3 scheduling/topology validation against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    control_planes, workers = check_node_topology()
    check_workload_scheduling("gateway", GATEWAY_LABEL_SELECTOR, control_planes, workers)
    check_workload_scheduling("app", APP_LABEL_SELECTOR, control_planes, workers)

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} scheduling checks passed")
    if failures:
        print(f"FAIL: {len(failures)} scheduling check(s) failed", file=sys.stderr)
        return 1
    print("PASS: worker-only scheduling and topology spread proven for both workloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

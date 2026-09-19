#!/usr/bin/env python3
"""
DAY5: read-only verification that Cilium is installed and healthy as
the Day 5 CNI dataplane, and that kube-proxy was left in place
(Day 5 does not adopt Cilium's kube-proxy-replacement mode). Never
mutates anything - this is a status check, not a bootstrap; installing
Cilium itself is `make cni-install` (Helm), a separate, explicit,
mutating step.

Proves, against the live cluster:
  1. Every node is Ready (kind's networking.disableDefaultCNI leaves
     every node NotReady - no pod network - until a CNI is actually
     installed, so this alone is a meaningful first signal).
  2. The Cilium agent DaemonSet (label k8s-app=cilium, kube-system) has
     exactly as many Ready Pods as there are nodes - one agent per
     node, the normal DaemonSet contract.
  3. The Cilium operator Deployment (kube-system) has at least one
     available replica.
  4. The kube-proxy DaemonSet (kube-system) still has Ready Pods -
     confirming it was never disabled, per this project's Day 5 scope
     (Cilium enforces NetworkPolicy; kube-proxy still does Service
     load-balancing).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import get_json

EXPECTED_NODE_COUNT = 3
CILIUM_AGENT_LABEL_SELECTOR = "k8s-app=cilium"
CILIUM_OPERATOR_DEPLOYMENT_CANDIDATES = ("cilium-operator",)
KUBE_PROXY_LABEL_SELECTOR = "k8s-app=kube-proxy"
KUBE_SYSTEM_NAMESPACE = "kube-system"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def check_nodes_ready() -> int:
    nodes = get_json("get", "nodes")["items"]
    ready = [
        n
        for n in nodes
        if any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in n.get("status", {}).get("conditions", [])
        )
    ]
    record(
        len(nodes) == EXPECTED_NODE_COUNT and len(ready) == len(nodes),
        f"{len(ready)}/{len(nodes)} nodes Ready (expected {EXPECTED_NODE_COUNT}/{EXPECTED_NODE_COUNT}) - "
        "a NotReady node here usually means no CNI is actually installed yet",
    )
    return len(nodes)


def check_cilium_agent(expected_count: int) -> None:
    pods = get_json("-n", KUBE_SYSTEM_NAMESPACE, "get", "pods", "-l", CILIUM_AGENT_LABEL_SELECTOR)["items"]
    ready_pods = [p for p in pods if _is_ready(p)]
    record(
        len(pods) == expected_count and len(ready_pods) == expected_count,
        f"Cilium agent DaemonSet: {len(ready_pods)}/{len(pods)} Pods Ready (expected {expected_count}, one per node)",
    )


def check_cilium_operator() -> None:
    for name in CILIUM_OPERATOR_DEPLOYMENT_CANDIDATES:
        result = kube.run("-n", KUBE_SYSTEM_NAMESPACE, "get", "deployment", name, "-o", "json", check=False)
        if result.returncode == 0:
            import json

            deployment = json.loads(result.stdout)
            available = deployment.get("status", {}).get("availableReplicas", 0)
            record(available >= 1, f"Cilium operator Deployment {name!r}: availableReplicas={available} (expected >= 1)")
            return
    record(False, f"Cilium operator Deployment not found (checked {CILIUM_OPERATOR_DEPLOYMENT_CANDIDATES})")


def check_kube_proxy_still_enabled() -> None:
    pods = get_json("-n", KUBE_SYSTEM_NAMESPACE, "get", "pods", "-l", KUBE_PROXY_LABEL_SELECTOR)["items"]
    ready_pods = [p for p in pods if _is_ready(p)]
    record(
        bool(pods) and len(ready_pods) == len(pods),
        f"kube-proxy DaemonSet: {len(ready_pods)}/{len(pods)} Pods Ready (Day 5 does not disable kube-proxy)",
    )


def main() -> int:
    print(f"# Day 5 CNI status check: Cilium + kube-proxy (context {kube.CONTEXT}) - READ ONLY, never mutates")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    node_count = check_nodes_ready()
    check_cilium_agent(node_count)
    check_cilium_operator()
    check_kube_proxy_still_enabled()

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} CNI status checks passed")
    if failures:
        print(f"FAIL: {len(failures)} CNI status check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Cilium is installed/healthy as the enforcing CNI, kube-proxy remains enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

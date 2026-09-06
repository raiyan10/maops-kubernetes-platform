#!/usr/bin/env python3
"""
Day 3 final restored-state validation.

Run after all mutating Day 3 experiments (scaling, rolling update +
rollback, PDB/Eviction) to independently prove the cluster is back in
its normal, fully-healthy Day 3 baseline state - not by re-reading the
manifest, but by querying the live cluster:

  - context is kind-maops-k8s-day3, 3 nodes Ready.
  - both workloads: desired 3, Ready 3, 3 Ready Pods, 3 ready
    EndpointSlice endpoints, worker-only scheduling, worker skew <= 1.
  - both PodDisruptionBudgets: minAvailable 2, and a normal healthy
    status (currentHealthy 3, desiredHealthy 2, disruptionsAllowed 1).
  - no temporary rollout-test annotation left on either Deployment's Pod
    template.
  - the runtime Secret still exists and is valid (never rotated,
    contents never inspected here beyond shape).
  - no leaked `kubectl port-forward` process from any of this project's
    Day 3 validation scripts (scoped to the Day 3 context/namespace -
    DAY3-INT-L2 - never flagging an unrelated operator's port-forward to
    a different cluster/project).
  - Day 1 (maops-k8s-day1) and Day 2 (maops-k8s-day2) kind clusters
    still EXIST (DAY3-INT-I2: existence only, not a byte-for-byte
    "unchanged" claim - the separate, stronger safety argument is that
    every Day 3 kubectl mutation is explicitly scoped to
    `kind-maops-k8s-day3` and gated by `context_check.py` before it ever
    runs).

Node-topology and per-workload scheduling proof reuses
scheduling_check's own functions directly (not a re-implementation) -
its module-level `results` list is reset and merged into this script's
own results so both scripts' pass/fail counts stay independently
meaningful.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import scheduling_check
from endpointslice import count_ready_endpoints
from kube import APP_LABEL_SELECTOR, APP_PDB, GATEWAY_LABEL_SELECTOR, GATEWAY_PDB, INTERNAL_SECRET, NAMESPACE, get_json, wait_until
from rollout_check import ANNOTATION_KEY
from secret_bootstrap import get_existing_secret, validate_secret_shape

EXPECTED_REPLICAS = 3
EXPECTED_MIN_AVAILABLE = 2
OTHER_DAY_CLUSTERS = ["maops-k8s-day1", "maops-k8s-day2"]

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _settled_snapshot(deployment: str, label_selector: str, service: str):
    """A prior mutating experiment's own restoration can report success
    (Deployment status readyReplicas at target) a moment before the
    matching Pod list and EndpointSlice fully settle to the same count -
    the same termination-race class fixed in rollout_check.py. Poll
    until Deployment desired/Ready, live Pod count/Ready, and
    EndpointSlice ready-count all simultaneously agree on
    EXPECTED_REPLICAS before trusting this as the final snapshot."""

    def predicate():
        dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
        if dep.get("spec", {}).get("replicas") != EXPECTED_REPLICAS or dep.get("status", {}).get("readyReplicas") != EXPECTED_REPLICAS:
            return None
        pods = get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]
        ready_pods = [
            p
            for p in pods
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
        ]
        if len(pods) != EXPECTED_REPLICAS or len(ready_pods) != EXPECTED_REPLICAS:
            return None
        slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}")["items"]
        if count_ready_endpoints(slices) != EXPECTED_REPLICAS:
            return None
        return dep, pods, ready_pods

    return wait_until(
        predicate,
        timeout=60,
        interval=2,
        description=f"{deployment} settled at {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} (Deployment, Pods, and EndpointSlice in agreement)",
    )


def check_workload_final_state(component: str, deployment: str, service: str, label_selector: str) -> None:
    try:
        dep, pods, ready_pods = _settled_snapshot(deployment, label_selector, service)
    except TimeoutError as exc:
        record(False, f"{component}: final state never settled to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS}: {exc}")
        dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
        pods = get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]
        ready_pods = []

    desired = dep.get("spec", {}).get("replicas")
    ready = dep.get("status", {}).get("readyReplicas")
    record(desired == EXPECTED_REPLICAS, f"{component}: desired replicas == {desired} (expected {EXPECTED_REPLICAS})")
    record(ready == EXPECTED_REPLICAS, f"{component}: ready replicas == {ready} (expected {EXPECTED_REPLICAS})")

    record(
        len(pods) == EXPECTED_REPLICAS and len(ready_pods) == EXPECTED_REPLICAS,
        f"{component}: {len(ready_pods)}/{len(pods)} Pods Ready (expected {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS})",
    )

    slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}")["items"]
    ready_endpoints = count_ready_endpoints(slices)
    record(
        ready_endpoints == EXPECTED_REPLICAS,
        f"{component}: EndpointSlice has {ready_endpoints} ready endpoint(s) (expected {EXPECTED_REPLICAS})",
    )

    annotations = dep.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}
    record(
        ANNOTATION_KEY not in annotations,
        f"{component}: temporary rollout-test annotation {ANNOTATION_KEY!r} absent from live Deployment template",
    )


def check_pdb_final_state(component: str, pdb_name: str) -> None:
    pdb = get_json("-n", NAMESPACE, "get", "poddisruptionbudget", pdb_name)
    spec = pdb.get("spec", {})
    status = pdb.get("status", {})
    record(
        spec.get("minAvailable") == EXPECTED_MIN_AVAILABLE,
        f"{component} {pdb_name}: spec.minAvailable == {spec.get('minAvailable')} (expected {EXPECTED_MIN_AVAILABLE})",
    )
    record(
        status.get("currentHealthy") == EXPECTED_REPLICAS
        and status.get("desiredHealthy") == EXPECTED_MIN_AVAILABLE
        and status.get("disruptionsAllowed") == EXPECTED_REPLICAS - EXPECTED_MIN_AVAILABLE,
        f"{component} {pdb_name}: normal healthy status - currentHealthy={status.get('currentHealthy')} "
        f"desiredHealthy={status.get('desiredHealthy')} disruptionsAllowed={status.get('disruptionsAllowed')} "
        f"(expected {EXPECTED_REPLICAS}/{EXPECTED_MIN_AVAILABLE}/{EXPECTED_REPLICAS - EXPECTED_MIN_AVAILABLE})",
    )


def check_secret_final_state() -> None:
    """DAY3-INT-L1: `get_existing_secret()` raises RuntimeError for a
    Secret/namespace API failure distinct from "does not exist" (e.g. the
    namespace itself is gone) - that must become a clean recorded False
    result here, never an uncaught traceback that replaces the rest of
    this script's final-state summary."""
    try:
        secret = get_existing_secret()
    except RuntimeError as exc:
        record(False, f"runtime Secret {INTERNAL_SECRET!r} final-state check failed: {exc}")
        return
    if secret is None:
        record(False, f"runtime Secret {INTERNAL_SECRET!r} does not exist")
        return
    ok, message = validate_secret_shape(secret)
    record(ok, f"runtime Secret final state: {message}")


def check_no_leaked_port_forwards() -> None:
    """DAY3-INT-L2: scoped to THIS project's Day 3 port-forwards only. A
    bare "kubectl" + "port-forward" substring match would also flag an
    unrelated operator's port-forward to a completely different
    cluster/project as a Day 3 leak. Every port-forward this project's
    own `scripts/portforward.py` starts always carries an explicit
    `--context kind-maops-k8s-day3 -n maops-platform` (see
    `port_forward()`), so requiring both substrings together is a
    reliable, minimal scope without needing to also enumerate the exact
    resource names it can create."""
    result = subprocess.run(["ps", "ax", "-o", "pid,args"], capture_output=True, text=True, check=True, timeout=10)
    leaked = [
        line
        for line in result.stdout.splitlines()
        if "kubectl" in line and "port-forward" in line and kube.CONTEXT in line and kube.NAMESPACE in line
    ]
    record(not leaked, f"no leaked Day 3 ({kube.CONTEXT}/{kube.NAMESPACE}) kubectl port-forward processes (found {len(leaked)}: {leaked})")


def check_other_day_clusters_still_exist() -> None:
    """DAY3-INT-I2: this proves only that the Day 1 and Day 2 kind
    clusters still EXIST as named clusters - not that every object inside
    them is byte-for-byte unchanged ("untouched" would overclaim that).
    The separate, stronger structural safety argument that nothing in
    them was ever mutated is: every Day 3 kubectl mutation in this
    project is explicitly scoped to `kind-maops-k8s-day3`
    (`kube.CONTEXT`, never the ambient current-context) and is preceded
    by the fail-closed identity/topology gate in `context_check.py` /
    `kube.verify_context()` - not a runtime snapshot comparison performed
    by this function."""
    result = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True, check=False, timeout=15)
    clusters = set(result.stdout.split())
    for name in OTHER_DAY_CLUSTERS:
        record(name in clusters, f"{name} kind cluster still exists (existence only - not a claim that its internal state is unchanged)")


def main() -> int:
    print(f"# Day 3 final restored-state validation against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    scheduling_check.results = []
    control_planes, workers = scheduling_check.check_node_topology()

    for component, deployment, service, label_selector in (
        ("gateway", "maops-gateway", "maops-gateway", GATEWAY_LABEL_SELECTOR),
        ("app", "maops-app", "maops-app", APP_LABEL_SELECTOR),
    ):
        check_workload_final_state(component, deployment, service, label_selector)
        scheduling_check.check_workload_scheduling(component, label_selector, control_planes, workers)

    check_pdb_final_state("gateway", GATEWAY_PDB)
    check_pdb_final_state("app", APP_PDB)

    check_secret_final_state()
    check_no_leaked_port_forwards()
    check_other_day_clusters_still_exist()

    all_results = results + scheduling_check.results
    failures = [m for ok, m in all_results if not ok]
    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} final-state checks passed")
    if failures:
        print(f"FAIL: {len(failures)} final-state check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Day 3 cluster fully restored to its normal healthy baseline state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

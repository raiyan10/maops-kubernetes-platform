#!/usr/bin/env python3
"""
Real-cluster check #14: controller reconciliation proof.

Records the two current maops-app pod UIDs, deletes exactly one pod
(never creates a replacement manually), waits for the Deployment
controller to reconcile back to 2 Ready replicas, proves a new pod UID
appeared, and re-runs the HTTP smoke path to prove the Service still
works afterward.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_checks import check_all_endpoints
from kube import CONTEXT, NAMESPACE, get_json, run, wait_until
from portforward import port_forward

LABEL_SELECTOR = "app.kubernetes.io/name=maops-kubernetes-platform,app.kubernetes.io/instance=maops-kubernetes-platform-day1"
SERVICE = "maops-app"
EXPECTED_REPLICAS = 2

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def get_pods() -> list[dict]:
    return get_json("-n", NAMESPACE, "get", "pods", "-l", LABEL_SELECTOR)["items"]


def pod_uids(pods: list[dict]) -> set[str]:
    return {p["metadata"]["uid"] for p in pods}


def main() -> int:
    print("# Controller reconciliation proof")

    before_pods = get_pods()
    record(len(before_pods) == EXPECTED_REPLICAS, f"observed {len(before_pods)} pods before deletion (expected {EXPECTED_REPLICAS})")
    if len(before_pods) != EXPECTED_REPLICAS:
        return 1

    before_uids = pod_uids(before_pods)
    # Deterministic selection (DAY1-INT-I1): sort by metadata.name rather
    # than relying on incidental Kubernetes API list ordering, so which
    # pod gets deleted is reproducible and reviewable across runs.
    victim = sorted(before_pods, key=lambda p: p["metadata"]["name"])[0]["metadata"]["name"]
    print(f"recorded pod UIDs before deletion: {before_uids}")
    print(f"deleting exactly one pod: {victim}")
    try:
        run("-n", NAMESPACE, "delete", "pod", victim, "--wait=false")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        record(False, f"failed to delete pod {victim}: {stderr if stderr else exc}")
        return _finish()

    def predicate():
        dep = get_json("-n", NAMESPACE, "get", "deployment", "maops-app")
        ready = dep.get("status", {}).get("readyReplicas")
        if ready != EXPECTED_REPLICAS:
            return None
        pods = get_pods()
        if len(pods) != EXPECTED_REPLICAS:
            return None
        all_ready = all(
            any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
            for p in pods
        )
        return pods if all_ready else None

    try:
        after_pods = wait_until(predicate, timeout=120, interval=3, description="Deployment reconciled back to 2 Ready replicas")
        record(True, "Deployment reconciled back to 2/2 Ready replicas after pod deletion")
    except TimeoutError as exc:
        record(False, str(exc))
        return _finish()

    after_uids = pod_uids(after_pods)
    print(f"observed pod UIDs after reconciliation: {after_uids}")
    new_uids = after_uids - before_uids
    survivors = after_uids & before_uids
    record(len(new_uids) >= 1, f"at least one new pod UID appeared after reconciliation: {new_uids}")
    record(len(survivors) == EXPECTED_REPLICAS - 1, f"the untouched pod survived unchanged: {survivors}")

    try:
        with port_forward(CONTEXT, NAMESPACE, SERVICE, 8080) as local_port:
            http_results = check_all_endpoints(local_port)
        for ok, msg in http_results:
            record(ok, f"post-reconciliation HTTP check: {msg}")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"post-reconciliation port-forward failed: {exc}")

    return _finish()


def _finish() -> int:
    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} reconciliation checks passed")
    if failures:
        print(f"FAIL: {len(failures)} reconciliation check(s) failed", file=sys.stderr)
        return 1
    print("PASS: controller reconciliation proven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

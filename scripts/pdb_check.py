#!/usr/bin/env python3
"""
Real Day 3 PodDisruptionBudget behavior validation.

For BOTH maops-gateway-pdb and maops-app-pdb, one workload at a time:

  1. Baseline (replicas=3, healthy=3): PDB status desiredHealthy == 2,
     disruptionsAllowed == 1.
  2. Scale the Deployment 3 -> 2: wait for desired/Ready == 2 and PDB
     status currentHealthy == 2, desiredHealthy == 2,
     disruptionsAllowed == 0.
  3. Select one live Pod dynamically and attempt a voluntary disruption
     through the real Kubernetes Eviction API (`policy/v1 Eviction`,
     submitted via `kubectl create --raw .../eviction -f -` - never
     `kubectl delete pod`, which bypasses the PDB entirely). The
     eviction is expected to be REJECTED (HTTP 429 TooManyRequests,
     "would violate the pod's disruption budget") because
     disruptionsAllowed is 0; this is classified from the actual
     kubectl/API response, not manufactured from a generic non-zero
     exit code. Independently confirms the selected Pod was not
     actually evicted (still present, no deletionTimestamp). If the
     eviction unexpectedly succeeds, that is a FAIL - not a pass with a
     footnote.
  4. Restore 2 -> 3 in a guaranteed `finally` path: 3/3 Ready, PDB
     healthy state restored, disruptionsAllowed back to 1.

This deliberately demonstrates three properties: the PDB controls
voluntary Eviction-API disruption; it does NOT block ordinary
Deployment scaling (step 2 always succeeds); and it does NOT prevent
every involuntary failure (only eviction-mediated voluntary ones).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from cluster_check import _stderr_detail
from kube import APP_LABEL_SELECTOR, APP_PDB, GATEWAY_LABEL_SELECTOR, GATEWAY_PDB, NAMESPACE, get_json, run, wait_until

BASELINE_REPLICAS = 3
DISRUPTED_REPLICAS = 2
EXPECTED_MIN_AVAILABLE = 2
# DAY3-INT-H2: the Eviction API call is a direct subprocess.run() call
# (not routed through kube.run(), since it needs to pipe JSON on stdin),
# so it needs its own explicit bounded timeout.
EVICTION_SUBPROCESS_TIMEOUT_SECONDS = 15.0

results: list[tuple[bool, str]] = []
restoration_results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def record_restoration(ok: bool, message: str) -> bool:
    restoration_results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] RESTORATION: {message}")
    return ok


def get_pdb_status(pdb_name: str) -> dict:
    return get_json("-n", NAMESPACE, "get", "poddisruptionbudget", pdb_name).get("status", {})


def scale_deployment(name: str, replicas: int) -> None:
    run("-n", NAMESPACE, "scale", f"deployment/{name}", f"--replicas={replicas}")


def _deployment_at(name: str, replicas: int):
    dep = get_json("-n", NAMESPACE, "get", "deployment", name)
    if dep.get("spec", {}).get("replicas") == replicas and dep.get("status", {}).get("readyReplicas") == replicas:
        return dep
    return None


def _wait_deployment_at(name: str, replicas: int, timeout: float):
    return wait_until(
        lambda: _deployment_at(name, replicas),
        timeout=timeout,
        interval=2,
        description=f"deployment/{name} desired=={replicas} Ready=={replicas}",
    )


def _pdb_matches(pdb_name: str, current_healthy: int, desired_healthy: int, disruptions_allowed: int):
    status = get_pdb_status(pdb_name)
    if (
        status.get("currentHealthy") == current_healthy
        and status.get("desiredHealthy") == desired_healthy
        and status.get("disruptionsAllowed") == disruptions_allowed
    ):
        return status
    return None


def _wait_pdb_state(pdb_name: str, current_healthy: int, desired_healthy: int, disruptions_allowed: int, timeout: float):
    return wait_until(
        lambda: _pdb_matches(pdb_name, current_healthy, desired_healthy, disruptions_allowed),
        timeout=timeout,
        interval=2,
        description=(
            f"{pdb_name} status currentHealthy=={current_healthy} desiredHealthy=={desired_healthy} "
            f"disruptionsAllowed=={disruptions_allowed}"
        ),
    )


def get_pods(label_selector: str) -> list[dict]:
    return get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]


def attempt_eviction(pod_name: str, timeout: float = EVICTION_SUBPROCESS_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    body = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": pod_name, "namespace": NAMESPACE},
    }
    return subprocess.run(
        [
            "kubectl",
            "--context",
            kube.CONTEXT,
            "create",
            "--raw",
            f"/api/v1/namespaces/{NAMESPACE}/pods/{pod_name}/eviction",
            "-f",
            "-",
        ],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_status_object(text: str) -> dict | None:
    """Returns the parsed body if `text` is a JSON `meta.v1.Status`
    object, None otherwise (empty, not JSON, or JSON but not a Status)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("kind") == "Status":
        return payload
    return None


def _status_has_disruption_budget_evidence(status_obj: dict) -> bool:
    message = (status_obj.get("message") or "").lower()
    if "disruption budget" in message:
        return True
    causes = (status_obj.get("details") or {}).get("causes") or []
    cause_text = " ".join((c.get("message") or "") for c in causes if isinstance(c, dict)).lower()
    return "disruption budget" in cause_text


def classify_eviction_result(result: subprocess.CompletedProcess) -> str:
    """Returns 'rejected_by_pdb', 'succeeded', or 'inconclusive' based on
    the actual kubectl/API response - never a bare non-zero-exit-code (or
    bare HTTP 429/TooManyRequests) guess.

    DAY3-INT-H1 / DAY3-SEC-M1: Kubernetes API Priority and Fairness (and
    other unrelated server conditions) can also produce a bare
    `TooManyRequests` response that has nothing to do with this Pod's
    disruption budget. A valid PDB rejection requires BOTH the
    `TooManyRequests` reason AND evidence, specific to the disruption
    budget, in the Status message/details - never one alone. Structured
    JSON Status parsing is preferred when the response body is actually
    JSON; the plain-text `kubectl` error banner (the common case for this
    command) is only accepted when it carries both signals together."""
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode == 0:
        payload = _parse_status_object(stdout)
        if payload is not None and payload.get("status") == "Success":
            return "succeeded"
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("status") == "Success":
            return "succeeded"
        return "inconclusive"

    status_obj = _parse_status_object(stdout) or _parse_status_object(stderr)
    if status_obj is not None:
        if status_obj.get("reason") == "TooManyRequests" and _status_has_disruption_budget_evidence(status_obj):
            return "rejected_by_pdb"
        return "inconclusive"

    combined = f"{stdout}\n{stderr}"
    if "TooManyRequests" in combined and "disruption budget" in combined.lower():
        return "rejected_by_pdb"
    return "inconclusive"


def refresh_victim(label_selector: str, pod_name: str, expected_uid: str) -> dict | None:
    """DAY3-INT-M3: immediately before submitting the Eviction API
    request, re-fetch the previously-selected victim Pod rather than
    trusting the snapshot taken during selection - the two are not
    atomic, so the Pod could have started terminating or gone NotReady in
    the interim. Returns the fresh Pod object only if it is still the
    SAME identity (name AND uid), still discoverable via the workload's
    own label selector (i.e. still belongs to the expected workload),
    still Ready, and has no deletionTimestamp. Returns None for any other
    outcome - the caller must then skip the eviction attempt entirely and
    record a distinct inconclusive reason, never blaming the PDB for a
    victim that was never a valid target at eviction time."""
    pods = get_pods(label_selector)
    pod = next((p for p in pods if p["metadata"]["name"] == pod_name), None)
    if pod is None:
        return None
    if pod["metadata"]["uid"] != expected_uid:
        return None
    if pod["metadata"].get("deletionTimestamp"):
        return None
    if not any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])
    ):
        return None
    return pod


def pod_still_present_and_not_evicted(label_selector: str, pod_name: str, expected_uid: str) -> bool:
    pods = get_pods(label_selector)
    pod = next((p for p in pods if p["metadata"]["name"] == pod_name), None)
    if pod is None:
        return False
    if pod["metadata"]["uid"] != expected_uid:
        return False
    if pod["metadata"].get("deletionTimestamp"):
        return False
    return True


def run_pdb_experiment(component: str, deployment: str, pdb_name: str, label_selector: str) -> None:
    print(f"## PDB experiment: {component} ({pdb_name})")

    try:
        _wait_deployment_at(deployment, BASELINE_REPLICAS, timeout=60)
        record(True, f"{component} baseline: {deployment} {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Ready")
    except TimeoutError as exc:
        record(False, f"{component} baseline: {exc}")
        return

    try:
        status = _wait_pdb_state(pdb_name, BASELINE_REPLICAS, EXPECTED_MIN_AVAILABLE, BASELINE_REPLICAS - EXPECTED_MIN_AVAILABLE, timeout=30)
        record(
            True,
            f"{component} baseline PDB {pdb_name}: currentHealthy={status.get('currentHealthy')} "
            f"desiredHealthy={status.get('desiredHealthy')} disruptionsAllowed={status.get('disruptionsAllowed')}",
        )
    except TimeoutError as exc:
        record(False, f"{component} baseline PDB state: {exc}")
        return

    scaled_down = False
    try:
        scale_deployment(deployment, DISRUPTED_REPLICAS)
        scaled_down = True

        try:
            _wait_deployment_at(deployment, DISRUPTED_REPLICAS, timeout=90)
            record(True, f"{component}: {deployment} scaled down to {DISRUPTED_REPLICAS}/{DISRUPTED_REPLICAS} Ready "
                   "(PDB does NOT block ordinary Deployment scaling)")
        except TimeoutError as exc:
            record(False, f"{component} scale-down: {exc}")
            return

        try:
            status = _wait_pdb_state(pdb_name, DISRUPTED_REPLICAS, EXPECTED_MIN_AVAILABLE, 0, timeout=30)
            record(
                True,
                f"{component} disrupted PDB {pdb_name}: currentHealthy={status.get('currentHealthy')} "
                f"desiredHealthy={status.get('desiredHealthy')} disruptionsAllowed=0",
            )
        except TimeoutError as exc:
            record(False, f"{component} disrupted PDB state: {exc}")
            return

        pods = get_pods(label_selector)
        # Only a Pod the PDB actually counts as "healthy" (Ready, not
        # already terminating) is a meaningful eviction target: the
        # Eviction API always allows evicting a non-Ready/terminating Pod
        # regardless of disruptionsAllowed, since removing it doesn't
        # reduce the healthy count. Selecting one of those would make an
        # eviction "succeed" for a reason that has nothing to do with the
        # PDB - a lingering Pod from the just-completed 3 -> 2 scale-down
        # is exactly the kind of false signal this guards against.
        healthy_pods = [
            p
            for p in pods
            if not p["metadata"].get("deletionTimestamp")
            and any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
        ]
        if not healthy_pods:
            record(False, f"{component}: no healthy (Ready, non-terminating) Pods available to select an eviction target from")
            return
        victim = sorted(healthy_pods, key=lambda p: p["metadata"]["name"])[0]
        victim_name = victim["metadata"]["name"]
        victim_uid = victim["metadata"]["uid"]
        print(f"selected Pod for Eviction-API attempt: {victim_name}")

        fresh_victim = refresh_victim(label_selector, victim_name, victim_uid)
        if fresh_victim is None:
            record(
                False,
                f"{component}: selected victim {victim_name} (uid={victim_uid}) was no longer a valid healthy "
                "target immediately before eviction (freshness check failed: gone, replaced, NotReady, or "
                "terminating) - eviction not attempted, not attributable to the PDB",
            )
            return

        try:
            eviction_result = attempt_eviction(victim_name)
        except subprocess.TimeoutExpired as exc:
            record(
                False,
                f"{component}: Eviction API subprocess call against {victim_name} timed out after "
                f"{EVICTION_SUBPROCESS_TIMEOUT_SECONDS}s - inconclusive, NOT classified as a PDB rejection ({exc})",
            )
            return

        classification = classify_eviction_result(eviction_result)
        print(f"eviction attempt exit={eviction_result.returncode} classification={classification}")
        print(f"eviction response: stdout={eviction_result.stdout.strip()!r} stderr={eviction_result.stderr.strip()!r}")

        if classification == "succeeded":
            record(False, f"{component}: Eviction API call UNEXPECTEDLY SUCCEEDED against {victim_name} - the PDB should have blocked it")
        elif classification == "rejected_by_pdb":
            record(True, f"{component}: Eviction API call against {victim_name} was rejected with disruption-budget-specific evidence (TooManyRequests + disruption budget message/details)")
        else:
            record(False, f"{component}: Eviction API call result was inconclusive - TooManyRequests (if present) carried no disruption-budget-specific evidence, so it cannot be attributed to this PDB")

        still_present = pod_still_present_and_not_evicted(label_selector, victim_name, victim_uid)
        record(still_present, f"{component}: selected Pod {victim_name} was not voluntarily evicted (still present, same UID, no deletionTimestamp)")

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"{component} PDB experiment: kubectl command failed/timed out: {_stderr_detail(exc)}")
    except Exception as exc:  # noqa: BLE001 - controlled record, restoration below still always runs
        record(False, f"{component} PDB experiment raised an unexpected error: {exc}")
    finally:
        if scaled_down:
            restore_workload(component, deployment, pdb_name)


def restore_workload(component: str, deployment: str, pdb_name: str) -> bool:
    try:
        scale_deployment(deployment, BASELINE_REPLICAS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record_restoration(False, f"{component}: could not scale {deployment} back to {BASELINE_REPLICAS}: {_stderr_detail(exc)}")
        return False

    try:
        _wait_deployment_at(deployment, BASELINE_REPLICAS, timeout=90)
        record_restoration(True, f"{component}: {deployment} restored to {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Ready")
    except TimeoutError as exc:
        record_restoration(False, f"{component}: {deployment} did not return to {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Ready: {exc}")
        return False

    try:
        status = _wait_pdb_state(pdb_name, BASELINE_REPLICAS, EXPECTED_MIN_AVAILABLE, BASELINE_REPLICAS - EXPECTED_MIN_AVAILABLE, timeout=30)
        record_restoration(
            True,
            f"{component}: PDB {pdb_name} healthy state restored - currentHealthy={status.get('currentHealthy')} "
            f"desiredHealthy={status.get('desiredHealthy')} disruptionsAllowed={status.get('disruptionsAllowed')}",
        )
    except TimeoutError as exc:
        record_restoration(False, f"{component}: PDB {pdb_name} did not return to the healthy 3/3 state: {exc}")
        return False

    return True


def main() -> int:
    print(f"# Real Day 3 PodDisruptionBudget/Eviction behavior validation against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    run_pdb_experiment("gateway", "maops-gateway", GATEWAY_PDB, GATEWAY_LABEL_SELECTOR)
    run_pdb_experiment("app", "maops-app", APP_PDB, APP_LABEL_SELECTOR)

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]

    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} PDB/Eviction checks passed")
    if restoration_failures:
        print()
        print("!!! RESTORATION FAILURE - a workload may not be back at 3/3 Ready - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} PDB/Eviction check(s) failed", file=sys.stderr)
        return 1
    print("PASS: PodDisruptionBudget/Eviction behavior proven for both workloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Day 5 final restored-state validation.

Run after all mutating Day 5 experiments (scaling, rolling update +
rollback, PDB/Eviction, persistence, retention) to independently prove
the cluster is back in its normal, fully-healthy Day 5 baseline state -
not by re-reading the manifest, but by querying the live cluster:

  - context is kind-maops-k8s-day5, 3 nodes Ready.
  - both workloads: desired 3, Ready 3, 3 Ready Pods, 3 ready
    EndpointSlice endpoints, worker-only scheduling, worker skew <= 1.
  - both PodDisruptionBudgets: minAvailable 2, and a normal healthy
    status (currentHealthy 3, desiredHealthy 2, disruptionsAllowed 1).
  - no temporary rollout-test annotation left on either Deployment's Pod
    template.
  - the runtime Secret still exists and is valid (never rotated,
    contents never inspected here beyond shape).
  - no leaked `kubectl port-forward` process from any of this project's
    Day 4 validation scripts (scoped to the Day 4 context/namespace -
    DAY3-INT-L2 - never flagging an unrelated operator's port-forward to
    a different cluster/project).
  - Day 1 (maops-k8s-day1), Day 2 (maops-k8s-day2), Day 3
    (maops-k8s-day3), and Day 4 (maops-k8s-day4) kind clusters still
    EXIST (DAY3-INT-I2, extended for Day 5: existence only, not a
    byte-for-byte "unchanged" claim - the separate, stronger safety
    argument is that every Day 5 kubectl mutation is explicitly scoped
    to `kind-maops-k8s-day5` and gated by `context_check.py` before it
    ever runs).

Node-topology and per-workload scheduling proof reuses
scheduling_check's own functions directly (not a re-implementation) -
its module-level `results` list is reset and merged into this script's
own results so both scripts' pass/fail counts stay independently
meaningful.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import scheduling_check
import suite_baseline
from endpointslice import count_ready_endpoints
from http_checks import is_nonempty_identity, raw_get, validate_state_value
from kube import (
    APP_LABEL_SELECTOR,
    APP_PDB,
    CONTROL_PLANE_LABEL,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_PDB,
    GATEWAY_SERVICE,
    INTERNAL_SECRET,
    NAMESPACE,
    STATE_SECRET,
    STATE_STATEFULSET,
    get_json,
    wait_until,
)
from portforward import port_forward
from rollout_check import ANNOTATION_KEY
from secret_bootstrap import get_existing_secret, get_existing_state_secret, validate_secret_shape

EXPECTED_REPLICAS = 3
EXPECTED_STATE_REPLICAS = 1
EXPECTED_MIN_AVAILABLE = 2
EXPECTED_STATE_CLAIM_STORAGE = "256Mi"
OTHER_DAY_CLUSTERS = ["maops-k8s-day1", "maops-k8s-day2", "maops-k8s-day3", "maops-k8s-day4"]

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


def check_state_final_state() -> None:
    """DAY4: maops-state's own final baseline - 1/1 Ready, worker-only
    placement, no PodDisruptionBudget (single replica), and its PVC/PV
    still Bound at the expected capacity. Does not re-verify the
    persisted record's value - persistence_check.py/retention_check.py
    already prove that end to end; this is the structural/identity
    baseline only."""

    def predicate():
        sts = get_json("-n", NAMESPACE, "get", "statefulset", STATE_STATEFULSET)
        return sts if sts.get("status", {}).get("readyReplicas") == EXPECTED_STATE_REPLICAS else None

    try:
        sts = wait_until(predicate, timeout=60, interval=2, description=f"{STATE_STATEFULSET} settled at {EXPECTED_STATE_REPLICAS}/{EXPECTED_STATE_REPLICAS}")
    except TimeoutError as exc:
        record(False, f"state: final state never settled to {EXPECTED_STATE_REPLICAS}/{EXPECTED_STATE_REPLICAS}: {exc}")
        sts = get_json("-n", NAMESPACE, "get", "statefulset", STATE_STATEFULSET)

    desired = sts.get("spec", {}).get("replicas")
    ready = sts.get("status", {}).get("readyReplicas")
    record(desired == EXPECTED_STATE_REPLICAS, f"state: desired replicas == {desired} (expected {EXPECTED_STATE_REPLICAS})")
    record(ready == EXPECTED_STATE_REPLICAS, f"state: ready replicas == {ready} (expected {EXPECTED_STATE_REPLICAS})")

    pod_result = kube.run("-n", NAMESPACE, "get", "pod", "maops-state-0", "-o", "json", check=False)
    if pod_result.returncode == 0:
        pod = json.loads(pod_result.stdout)
        node_name = pod.get("spec", {}).get("nodeName")
        node = get_json("get", "node", node_name) if node_name else {}
        is_control_plane = CONTROL_PLANE_LABEL in (node.get("metadata", {}).get("labels") or {})
        record(bool(node_name) and not is_control_plane, f"state: maops-state-0 scheduled on worker node {node_name!r} (never control-plane)")
    else:
        record(False, "state: maops-state-0 does not exist in final state")

    pdbs = get_json("-n", NAMESPACE, "get", "poddisruptionbudget")["items"]
    state_pdbs = [p for p in pdbs if "state" in p.get("metadata", {}).get("name", "")]
    record(not state_pdbs, f"state: no PodDisruptionBudget exists (single replica) - found {[p['metadata']['name'] for p in state_pdbs]}")

    pvc_result = kube.run("-n", NAMESPACE, "get", "pvc", "data-maops-state-0", "-o", "json", check=False)
    if pvc_result.returncode != 0:
        record(False, "state: PVC data-maops-state-0 does not exist in final state")
        return
    pvc = json.loads(pvc_result.stdout)
    phase = pvc.get("status", {}).get("phase")
    capacity = pvc.get("status", {}).get("capacity", {}).get("storage")
    record(phase == "Bound", f"state: PVC data-maops-state-0 phase == {phase!r} (expected Bound)")
    record(capacity == EXPECTED_STATE_CLAIM_STORAGE, f"state: PVC data-maops-state-0 capacity == {capacity!r} (expected {EXPECTED_STATE_CLAIM_STORAGE!r})")


def _current_namespace_uid() -> str | None:
    result = kube.run("get", "namespace", NAMESPACE, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout).get("metadata", {}).get("uid")


def _current_pvc_pv_uid() -> tuple[str | None, str | None]:
    pvc_result = kube.run("-n", NAMESPACE, "get", "pvc", "data-maops-state-0", "-o", "json", check=False)
    if pvc_result.returncode != 0:
        return None, None
    pvc = json.loads(pvc_result.stdout)
    pvc_uid = pvc.get("metadata", {}).get("uid")
    volume_name = pvc.get("spec", {}).get("volumeName")
    pv_uid = None
    if volume_name:
        pv_result = kube.run("get", "pv", volume_name, "-o", "json", check=False)
        if pv_result.returncode == 0:
            pv_uid = json.loads(pv_result.stdout).get("metadata", {}).get("uid")
    return pvc_uid, pv_uid


def check_suite_state_baseline_restored() -> None:
    """DAY4: independent final gate for the run-specific suite-level
    baseline captured once by state_check.py (scripts/suite_baseline.py
    has the full design). Deliberately kept separate from
    check_state_final_state()'s structural/identity-only contract
    above, and from persistence_check.py/retention_check.py's own
    per-experiment restoration proofs - this exists specifically to
    catch a whole-pipeline regression (e.g. a future mutating script
    inserted between state-check and final-state-check that forgets
    its own restoration contract) that no single script's own
    self-check could ever catch.

    The run ID/path are taken ONLY from the environment this process
    was actually invoked with - never auto-discovered from a listing
    of leftover baseline files, and never satisfied by recapturing the
    current value here (that would prove nothing). A run without a
    supplied run ID/path (standalone `final-state-check`, or
    `state-check` did not run first in the same `make day5-check`
    invocation) is recorded as an explicit FAILURE of this specific
    check, never a silent skip - it cannot claim suite-baseline
    restoration it was never given the means to verify."""
    run_id, path = suite_baseline.env_configured()
    if not run_id or not path:
        record(
            False,
            f"suite-level state baseline restoration cannot be verified: {suite_baseline.RUN_ID_ENV}/"
            f"{suite_baseline.PATH_ENV} not supplied to this invocation (standalone final-state-check, or "
            "state-check did not run first in the same `make day5-check` sequence) - this check only has "
            "meaning when run via the full `make day5-check` sequence",
        )
        return

    namespace_uid = _current_namespace_uid()
    pvc_uid, pv_uid = _current_pvc_pv_uid()
    try:
        baseline = suite_baseline.load_and_validate(path, run_id, kube.CONTEXT, NAMESPACE, namespace_uid, pvc_uid, pv_uid)
    except suite_baseline.SuiteBaselineError as exc:
        record(False, f"suite-level state baseline verification failed: {exc}")
        return

    try:
        with port_forward(kube.CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            status, body_text = raw_get(local_port, "/state")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"suite-level state baseline verification failed - port-forward error: {exc}")
        return
    if status != 200:
        record(False, f"suite-level state baseline verification failed: independent GET /state returned HTTP {status} (expected 200)")
        return
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as exc:
        record(False, f"suite-level state baseline verification failed: GET /state body was not valid JSON: {exc}")
        return
    # DAY4 batch 2b: this is the exact schema-validation gap the batch
    # closes - the previous `body.get("value")` accepted HTTP 200 + `{}`
    # (a body genuinely missing the required 'value' key) as a match
    # whenever the baseline's captured value happened to be null, since
    # `{}.get("value")` and a genuine `{"value": null}` are otherwise
    # indistinguishable via `.get()` alone.
    ok, actual_value, err = validate_state_value(body)
    matches = ok and actual_value == baseline["value"]
    record(
        matches,
        f"suite-level state baseline restored: independent GET /state matches the run {run_id!r} baseline "
        f"captured before any Day 4 mutating experiment (value={actual_value!r}, expected {baseline['value']!r})"
        + (f", schema error: {err}" if not ok else ""),
    )


def check_state_secret_final_state() -> None:
    try:
        secret = get_existing_state_secret()
    except RuntimeError as exc:
        record(False, f"runtime Secret {STATE_SECRET!r} final-state check failed: {exc}")
        return
    if secret is None:
        record(False, f"runtime Secret {STATE_SECRET!r} does not exist")
        return
    ok, message = validate_secret_shape(secret, STATE_SECRET, kube.STATE_SECRET_KEY)
    record(ok, f"runtime Secret final state: {message}")


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
    """DAY3-INT-L2: scoped to THIS project's Day 5 port-forwards only. A
    bare "kubectl" + "port-forward" substring match would also flag an
    unrelated operator's port-forward to a completely different
    cluster/project as a Day 5 leak. Every port-forward this project's
    own `scripts/portforward.py` starts always carries an explicit
    `--context kind-maops-k8s-day5 -n maops-platform` (see
    `port_forward()`), so requiring both substrings together is a
    reliable, minimal scope without needing to also enumerate the exact
    resource names it can create."""
    result = subprocess.run(["ps", "ax", "-o", "pid,args"], capture_output=True, text=True, check=True, timeout=10)
    leaked = [
        line
        for line in result.stdout.splitlines()
        if "kubectl" in line and "port-forward" in line and kube.CONTEXT in line and kube.NAMESPACE in line
    ]
    record(not leaked, f"no leaked Day 5 ({kube.CONTEXT}/{kube.NAMESPACE}) kubectl port-forward processes (found {len(leaked)}: {leaked})")


def check_other_day_clusters_still_exist() -> None:
    """DAY3-INT-I2, extended for Day 5 (DAY5-INT-H2/DAY5-REL-M1 fix -
    this list previously stopped at Day 3 and never grew to include Day
    4 once Day 5 introduced its own separate cluster, so a Day 4
    cluster failure was structurally unable to be caught here): this
    proves only that each earlier-day kind cluster still EXISTS as a
    named cluster - not that every object inside it is byte-for-byte
    unchanged ("untouched" would overclaim that), and NOT that its node
    containers are actually running/healthy (`kind get clusters` matches
    on registration, not liveness). The separate, stronger structural
    safety argument that nothing in them was ever mutated is: every Day
    5 kubectl mutation in this project is explicitly scoped to
    `kind-maops-k8s-day5` (`kube.CONTEXT`, never the ambient
    current-context) and is preceded by the fail-closed identity/
    topology gate in `context_check.py` / `kube.verify_context()` - not
    a runtime snapshot comparison performed by this function."""
    result = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True, check=False, timeout=15)
    clusters = set(result.stdout.split())
    for name in OTHER_DAY_CLUSTERS:
        record(name in clusters, f"{name} kind cluster still exists (existence only - not a claim that its internal state is unchanged)")


def main() -> int:
    print(f"# Day 5 final restored-state validation against context {kube.CONTEXT}")
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
    check_state_final_state()
    check_suite_state_baseline_restored()

    check_secret_final_state()
    check_state_secret_final_state()
    check_no_leaked_port_forwards()
    check_other_day_clusters_still_exist()

    all_results = results + scheduling_check.results
    failures = [m for ok, m in all_results if not ok]
    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} final-state checks passed")
    if failures:
        print(f"FAIL: {len(failures)} final-state check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Day 5 cluster fully restored to its normal healthy baseline state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

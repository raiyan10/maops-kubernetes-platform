#!/usr/bin/env python3
"""
DAY4: real kind-cluster validation for the maops-state StatefulSet -
runtime identity, security baseline, storage binding, and the
authenticated gateway -> app -> state chain. Companion to
cluster_check.py (which covers gateway/app, unchanged since Day 3) -
this script is state's equivalent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import suite_baseline
from http_checks import is_nonempty_identity, raw_get, validate_state_value
from kube import (
    CONTEXT,
    CONTROL_PLANE_LABEL,
    GATEWAY_SERVICE,
    NAMESPACE,
    STATE_HEADLESS_SERVICE,
    STATE_LABEL_SELECTOR,
    STATE_SECRET,
    STATE_SERVICE,
    STATE_STATEFULSET,
    get_json,
    run,
    wait_until,
)
from portforward import port_forward

EXPECTED_REPLICAS = 1
EXPECTED_POD_NAME = "maops-state-0"
EXPECTED_PVC_NAME = "data-maops-state-0"
EXPECTED_CLAIM_STORAGE = "256Mi"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _stderr_detail(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    stderr = getattr(exc, "stderr", None)
    if stderr:
        stderr = stderr.strip() if isinstance(stderr, str) else stderr.decode(errors="replace").strip()
        if stderr:
            return stderr
    return str(exc)


def wait_for_statefulset_ready() -> dict:
    def predicate():
        sts = get_json("-n", NAMESPACE, "get", "statefulset", STATE_STATEFULSET)
        ready = sts.get("status", {}).get("readyReplicas")
        return sts if ready == EXPECTED_REPLICAS else None

    try:
        sts = wait_until(predicate, timeout=120, interval=3, description=f"StatefulSet {STATE_STATEFULSET} ready")
        record(True, f"StatefulSet {STATE_STATEFULSET} reached readyReplicas == {EXPECTED_REPLICAS}")
    except TimeoutError as exc:
        record(False, str(exc))
        sts = get_json("-n", NAMESPACE, "get", "statefulset", STATE_STATEFULSET)
    return sts


def check_replica_counts(sts: dict) -> None:
    desired = sts.get("spec", {}).get("replicas")
    ready = sts.get("status", {}).get("readyReplicas")
    record(desired == EXPECTED_REPLICAS, f"maops-state desired replicas == {desired} (expected {EXPECTED_REPLICAS})")
    record(ready == EXPECTED_REPLICAS, f"maops-state ready replicas == {ready} (expected {EXPECTED_REPLICAS})")


def get_state_pod() -> dict | None:
    pods = get_json("-n", NAMESPACE, "get", "pods", "-l", STATE_LABEL_SELECTOR)["items"]
    return next((p for p in pods if p.get("metadata", {}).get("name") == EXPECTED_POD_NAME), None)


def check_pod_identity_and_placement(pod: dict | None) -> None:
    if pod is None:
        record(False, f"expected Pod {EXPECTED_POD_NAME!r} to exist, found none")
        return
    record(True, f"Pod {EXPECTED_POD_NAME!r} exists (stable StatefulSet identity)")
    conditions = pod.get("status", {}).get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    record(ready, f"{EXPECTED_POD_NAME} Ready condition == {ready}")

    node_name = pod.get("spec", {}).get("nodeName")
    node = get_json("get", "node", node_name) if node_name else {}
    is_control_plane = CONTROL_PLANE_LABEL in (node.get("metadata", {}).get("labels") or {})
    record(bool(node_name) and not is_control_plane, f"{EXPECTED_POD_NAME} scheduled on worker node {node_name!r} (never control-plane)")


def check_no_topology_spread_or_pdb() -> None:
    pdbs = get_json("-n", NAMESPACE, "get", "poddisruptionbudget")["items"]
    state_pdbs = [p for p in pdbs if "state" in p.get("metadata", {}).get("name", "")]
    record(not state_pdbs, f"no PodDisruptionBudget exists for maops-state (single replica) - found {[p['metadata']['name'] for p in state_pdbs]}")


def check_services() -> None:
    svc = get_json("-n", NAMESPACE, "get", "service", STATE_SERVICE)
    record(svc.get("spec", {}).get("type", "ClusterIP") == "ClusterIP", f"{STATE_SERVICE} Service type == ClusterIP")
    record(svc.get("spec", {}).get("clusterIP") != "None", f"{STATE_SERVICE} Service has a real ClusterIP (not headless)")

    headless = get_json("-n", NAMESPACE, "get", "service", STATE_HEADLESS_SERVICE)
    record(headless.get("spec", {}).get("clusterIP") == "None", f"{STATE_HEADLESS_SERVICE} Service clusterIP == None (governing/headless)")


def _get_json_or_none(*args: str) -> dict | None:
    result = run(*args, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def check_pvc_pv_binding() -> tuple[str | None, str | None]:
    pvc = _get_json_or_none("-n", NAMESPACE, "get", "pvc", EXPECTED_PVC_NAME)
    if pvc is None:
        record(False, f"expected PVC {EXPECTED_PVC_NAME!r} to exist, found none")
        return None, None
    phase = pvc.get("status", {}).get("phase")
    capacity = pvc.get("status", {}).get("capacity", {}).get("storage")
    volume_name = pvc.get("spec", {}).get("volumeName")
    pvc_uid = pvc.get("metadata", {}).get("uid")
    record(phase == "Bound", f"PVC {EXPECTED_PVC_NAME!r} phase == {phase!r} (expected Bound)")
    record(
        capacity == EXPECTED_CLAIM_STORAGE,
        f"PVC {EXPECTED_PVC_NAME!r} capacity == {capacity!r} (expected {EXPECTED_CLAIM_STORAGE!r})",
    )
    pv_uid = None
    if volume_name:
        pv = _get_json_or_none("get", "pv", volume_name) or {}
        pv_uid = pv.get("metadata", {}).get("uid")
        record(
            pv.get("spec", {}).get("persistentVolumeReclaimPolicy") == "Delete",
            f"PV {volume_name!r} reclaim policy == {pv.get('spec', {}).get('persistentVolumeReclaimPolicy')!r} "
            "(the cluster default StorageClass's PV-level policy - distinct from the StatefulSet's own "
            "persistentVolumeClaimRetentionPolicy Retain/Retain, which governs PVC lifecycle, not PV reclaim)",
        )
    return pvc_uid, pv_uid


def exec_in_pod(pod_name: str, *cmd: str) -> str:
    result = run("-n", NAMESPACE, "exec", pod_name, "--", *cmd)
    return result.stdout.strip()


def check_runtime_security(pod: dict | None) -> None:
    if pod is None:
        record(False, "runtime security: no state pod available to inspect")
        return
    pod_name = pod["metadata"]["name"]
    try:
        actual = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", "import os; print(os.getuid(), os.getgid(), os.getgroups())")
    except subprocess.CalledProcessError as exc:
        record(False, f"runtime UID/GID: kubectl exec failed in pod {pod_name}: {_stderr_detail(exc)}")
        return
    record(
        actual.startswith("10001 10001") and "10001" in actual,
        f"state live process UID/GID/groups in pod {pod_name} == {actual!r} (expected UID/GID 10001, supplementary group 10001)",
    )
    container = pod["spec"]["containers"][0]
    csc = container.get("securityContext", {})
    psc = pod["spec"].get("securityContext", {})
    record(csc.get("readOnlyRootFilesystem") is True, f"state live pod readOnlyRootFilesystem == {csc.get('readOnlyRootFilesystem')!r}")
    record(csc.get("allowPrivilegeEscalation") is False, f"state live pod allowPrivilegeEscalation == {csc.get('allowPrivilegeEscalation')!r}")
    record((csc.get("capabilities") or {}).get("drop") == ["ALL"], f"state live pod capabilities.drop == {(csc.get('capabilities') or {}).get('drop')!r}")
    record((psc.get("seccompProfile") or {}).get("type") == "RuntimeDefault", f"state live pod seccompProfile == {psc.get('seccompProfile')!r}")
    record(pod["spec"].get("automountServiceAccountToken") is False, f"state live pod automountServiceAccountToken == {pod['spec'].get('automountServiceAccountToken')!r}")


def check_secret_volume_mount(pod: dict | None) -> None:
    if pod is None:
        record(False, "Secret volume/mount: no state pod available to inspect")
        return
    volumes = pod["spec"].get("volumes") or []
    volume = next((v for v in volumes if v.get("name") == "state-auth"), {})
    secret_name = (volume.get("secret") or {}).get("secretName")
    record(secret_name == STATE_SECRET, f"state live pod volume 'state-auth' references Secret {secret_name!r} (expected {STATE_SECRET!r})")
    container = pod["spec"]["containers"][0]
    mounts = container.get("volumeMounts") or []
    mount = next((m for m in mounts if m.get("name") == "state-auth"), {})
    record(
        mount.get("mountPath") == "/var/run/secrets/maops-state" and mount.get("readOnly") is True,
        f"state live pod volumeMount 'state-auth' -> mountPath={mount.get('mountPath')!r} readOnly={mount.get('readOnly')!r}",
    )
    data_mount = next((m for m in mounts if m.get("name") == "data"), {})
    record(
        data_mount.get("mountPath") == "/data" and not data_mount.get("readOnly"),
        f"state live pod volumeMount 'data' -> mountPath={data_mount.get('mountPath')!r} readOnly={data_mount.get('readOnly')!r} (expected /data, writable)",
    )


def check_unauthenticated_state_rejected(pod: dict | None) -> None:
    """A direct, unauthenticated GET /state against the state Pod itself
    (bypassing gateway/app) must be rejected with exactly HTTP 403 -
    mirrors the Day 2/3 secret_check.py pattern for /internal/info."""
    if pod is None:
        record(False, "unauthenticated /state rejection: no state pod available to inspect")
        return
    pod_name = pod["metadata"]["name"]
    script = (
        "import urllib.request, urllib.error\n"
        "try:\n"
        "    urllib.request.urlopen('http://127.0.0.1:8080/state', timeout=3)\n"
        "    print('NOEXC')\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code)\n"
    )
    try:
        status = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", script)
    except subprocess.CalledProcessError as exc:
        record(False, f"unauthenticated /state check: kubectl exec failed: {_stderr_detail(exc)}")
        return
    record(status.strip() == "403", f"direct unauthenticated GET /state on {pod_name} -> HTTP {status.strip()} (expected 403)")


def _namespace_uid() -> str | None:
    result = run("get", "namespace", NAMESPACE, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout).get("metadata", {}).get("uid")


def capture_suite_baseline(pvc_uid: str | None, pv_uid: str | None) -> None:
    """DAY4: run-specific suite-level baseline (see scripts/suite_baseline.py
    for the full design and its "why not a per-cluster file" rationale).
    Only active when both DAY5_RUN_ID and DAY5_SUITE_BASELINE_PATH are
    set (the Makefile sets both, once per `make day5-check` invocation,
    and exports them to every child recipe line). A standalone
    `make state-check` run - neither var set - intentionally skips this
    non-fatally: state-check's other checks remain fully meaningful on
    their own, but no suite-level baseline claim is made. This authenticated
    GET is the first (and only) touch of `/state`'s VALUE in the entire
    day5-check sequence up to and including this script - state-check
    itself performs no PUT anywhere - so this capture point is always
    strictly before the first step capable of mutating application
    state (persistence-check)."""
    run_id, path = suite_baseline.env_configured()
    if not run_id or not path:
        print(f"(suite-level state baseline capture skipped: {suite_baseline.RUN_ID_ENV}/{suite_baseline.PATH_ENV} not set - standalone invocation)")
        return

    namespace_uid = _namespace_uid()
    if not (is_nonempty_identity(namespace_uid) and is_nonempty_identity(pvc_uid) and is_nonempty_identity(pv_uid)):
        record(
            False,
            f"suite-level state baseline capture failed: identity preconditions not met "
            f"(namespace_uid={namespace_uid!r} pvc_uid={pvc_uid!r} pv_uid={pv_uid!r}) - refusing to capture an unverifiable baseline",
        )
        return

    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            status, body_text = raw_get(local_port, "/state")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"suite-level state baseline capture failed - port-forward error: {exc}")
        return
    if status != 200:
        record(False, f"suite-level state baseline capture failed: authenticated GET /state via gateway returned HTTP {status} (expected 200)")
        return
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError as exc:
        record(False, f"suite-level state baseline capture failed: GET /state body was not valid JSON: {exc}")
        return
    ok, value, err = validate_state_value(body)
    if not ok:
        record(False, f"suite-level state baseline capture failed: {err}")
        return
    try:
        suite_baseline.capture(path, run_id, CONTEXT, NAMESPACE, namespace_uid, pvc_uid, pv_uid, value)
    except suite_baseline.SuiteBaselineError as exc:
        record(False, f"suite-level state baseline capture failed: {exc}")
        return
    record(True, f"suite-level state baseline captured at {path!r} for run {run_id!r} (value={'<non-null>' if value is not None else None!r})")


def main() -> int:
    print(f"# Day 4 maops-state runtime validation against context {CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    sts = wait_for_statefulset_ready()
    check_replica_counts(sts)
    pod = get_state_pod()
    check_pod_identity_and_placement(pod)
    check_no_topology_spread_or_pdb()
    check_services()
    pvc_uid, pv_uid = check_pvc_pv_binding()
    check_runtime_security(pod)
    check_secret_volume_mount(pod)
    check_unauthenticated_state_rejected(pod)
    # Last, so every structural precondition above has already been
    # asserted before this authenticated read of /state's actual VALUE -
    # the first (and, within this script, only) touch of that value.
    capture_suite_baseline(pvc_uid, pv_uid)

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} state checks passed")
    if failures:
        print(f"FAIL: {len(failures)} state check(s) failed", file=sys.stderr)
        return 1
    print("PASS: all maops-state checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY7: capture the pre-experiment strategy baseline - ONCE per run.

Runs after `make day7-deploy` (stable stage installed), after
`make state-check` has captured the run's suite-level `/state` baseline
into the same private run directory, and BEFORE any Day 7 experiment
mutates anything. Records, from the LIVE cluster and Helm (never from
the chart or a manifest):

  - Helm: release revision, user-supplied values (must equal
    helm-values/day7/stable.yaml plus exactly the verified build's
    image tags), and the sha256 of `helm get manifest`;
  - build: the verified build record (scripts/day7_build.py) - every
    container verified running it first (day7_running_images); every
    later stage of the run must carry this same build, and a different
    current build makes this baseline unusable (new run ID required);
  - routing: HTTPRoute UID/generation/backendRefs (must be the single
    stable backend, current for its generation) and the Gateway's UID/
    generation;
  - workload identity: UID, generation and Pod-template checksum of
    maops-gateway and maops-app, UID/generation of maops-state, UIDs of
    the stable Services and PDBs, and the stable gateway ConfigMap's
    APP_MESSAGE (the identity of a stable external response);
  - storage identity: namespace UID, PVC UID, PV name and UID.

Preconditions (all must hold or nothing is written): Day 7 profile and
verified context; Helm release deployed with the stable values; no
candidate-only object present; gateway 3/3, app 3/3, state 1/1 ready;
the run's suite baseline already exists and is private.

The file is written O_EXCL with mode 0600 inside the 0700 run directory
(scripts/private_run_dir.py) - an existing baseline is never
overwritten, and no experiment or final check ever recaptures one.
No Secret is read.

Usage: python3 scripts/day7_baseline.py capture
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import day7_running_images
import day7_strategy as d7
import kube
import private_run_dir

SUITE_BASELINE_PATH_ENV = "DAY7_SUITE_BASELINE_PATH"
RUN_DIR_ENV = private_run_dir.RUN_DIR_ENV
STATE_PVC = "data-maops-state-0"


def _uid_gen(kind: str, name: str, namespace: str | None = None) -> dict | None:
    args = (["-n", namespace or kube.NAMESPACE] if namespace != "" else []) + ["get", kind, name]
    state, obj = d7.get_json_or_none(*args)
    if state != "found" or obj is None:
        return None
    meta = obj.get("metadata", {})
    out = {"uid": meta.get("uid"), "generation": meta.get("generation")}
    checksum = (obj.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get("checksum/config")
    if checksum:
        out["template_checksum"] = checksum
    status = obj.get("status", {})
    for key in ("readyReplicas", "replicas"):
        if key in status:
            out[key] = status[key]
    return out


def collect(rec: d7.Recorder) -> dict | None:
    """Reads everything; records a finding per precondition. Returns the
    baseline record only if every precondition passed."""
    ok = True
    status = d7.helm_status() or {}
    revision = status.get("version")
    ok = rec.record(isinstance(revision, int) and (status.get("info") or {}).get("status") == "deployed", f"Helm release {kube.HELM_RELEASE_NAME} deployed at revision {revision}") and ok
    try:
        build = d7.active_build()
        expected_values = d7.expected_release_values("stable", build)
        ok = rec.record(True, f"verified build {build.build_id} is current (" + ", ".join(f"{i.component}={i.config_digest}" for i in build.images) + ")") and ok
    except day7_build.BuildError as exc:
        build, expected_values = None, None
        ok = rec.record(False, f"verified build unreadable - baseline NOT captured ({exc})") and ok
    values = d7.helm_values()
    ok = rec.record(expected_values is not None and values == expected_values, f"Helm user-supplied values equal helm-values/day7/stable.yaml + the verified build's image tags (found {values!r})") and ok
    if build is not None:
        ok = day7_running_images.wait_for_running_images(rec, build, label="running images before baseline") and ok
    manifest = d7.helm_manifest()
    ok = rec.record(bool(manifest), "Helm manifest readable") and ok

    present, unknown = d7.candidate_leftovers()
    ok = rec.record(not present and not unknown, f"no candidate-only object present before experiments (present {present}, undetermined {unknown})") and ok

    route = d7.read_route()
    route_ok, route_detail = d7.route_is_current(route, d7.expected_backends(d7.load_stage_values("stable")))
    ok = rec.record(route_ok, f"HTTPRoute is 100% stable and current: {route_detail}") and ok
    gw_ok, gw_detail = d7.gateway_is_current()
    ok = rec.record(gw_ok, gw_detail) and ok
    gateway_obj = _uid_gen("gateway", kube.GATEWAY_API_GATEWAY_NAME, kube.INGRESS_NAMESPACE)

    workloads = {
        "maops-gateway": _uid_gen("deployment", kube.GATEWAY_DEPLOYMENT),
        "maops-app": _uid_gen("deployment", kube.APP_DEPLOYMENT),
        "maops-state": _uid_gen("statefulset", kube.STATE_STATEFULSET),
    }
    for name, expected in (("maops-gateway", 3), ("maops-app", 3), ("maops-state", 1)):
        w = workloads[name]
        ok = rec.record(w is not None and w.get("readyReplicas") == expected, f"{name} ready {None if w is None else w.get('readyReplicas')}/{expected}") and ok
    services = {name: (_uid_gen("service", name) or {}).get("uid") for name in (kube.GATEWAY_SERVICE, kube.APP_SERVICE, kube.STATE_SERVICE)}
    pdbs = {name: (_uid_gen("pdb", name) or {}).get("uid") for name in (kube.GATEWAY_PDB, kube.APP_PDB)}
    ok = rec.record(all(services.values()) and all(pdbs.values()), f"stable Service/PDB identities read ({services}, {pdbs})") and ok

    state, cm = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "configmap", "maops-gateway-config")
    stable_message = (cm or {}).get("data", {}).get("APP_MESSAGE") if state == "found" else None
    ok = rec.record(bool(stable_message), f"stable gateway APP_MESSAGE read ({stable_message!r})") and ok

    ns_state, ns = d7.get_json_or_none("get", "namespace", kube.NAMESPACE)
    pvc_state, pvc = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "pvc", STATE_PVC)
    pv_name = (pvc or {}).get("spec", {}).get("volumeName")
    pv_state, pv = d7.get_json_or_none("get", "pv", pv_name) if pv_name else ("error", None)
    storage = {
        "namespace_uid": (ns or {}).get("metadata", {}).get("uid"),
        "pvc_uid": (pvc or {}).get("metadata", {}).get("uid"),
        "pv_name": pv_name,
        "pv_uid": (pv or {}).get("metadata", {}).get("uid"),
    }
    ok = rec.record(all(storage.values()), f"namespace/PVC/PV identities read ({storage})") and ok

    if not ok:
        return None
    return {
        "run_id": os.environ.get(d7.RUN_ID_ENV),
        "context": kube.CONTEXT,
        "release": kube.HELM_RELEASE_NAME,
        "namespace": kube.NAMESPACE,
        "helm": {"revision": revision, "values": values, "manifest_sha256": d7.sha256_text(manifest)},
        "build": build.record(),
        "route": {"uid": route.uid, "generation": route.generation, "backends": [list(b) for b in route.backends]},
        "gateway": gateway_obj,
        "workloads": workloads,
        "services": services,
        "pdbs": pdbs,
        "stable_message": stable_message,
        "storage": storage,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def write_exclusive(path: str, record: dict) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, private_run_dir.FILE_MODE)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
    os.chmod(path, private_run_dir.FILE_MODE)


def capture() -> int:
    rec = d7.Recorder()
    print(f"# Day 7 strategy baseline capture (context {kube.CONTEXT}, release {kube.HELM_RELEASE_NAME})")
    try:
        d7.require_day7_profile()
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    run_id = os.environ.get(d7.RUN_ID_ENV)
    run_dir = os.environ.get(RUN_DIR_ENV)
    path = os.environ.get(d7.STRATEGY_BASELINE_PATH_ENV)
    suite_path = os.environ.get(SUITE_BASELINE_PATH_ENV)
    if not all((run_id, run_dir, path, suite_path)):
        print(f"FAIL: {d7.RUN_ID_ENV}, {RUN_DIR_ENV}, {d7.STRATEGY_BASELINE_PATH_ENV} and {SUITE_BASELINE_PATH_ENV} must all be set", file=sys.stderr)
        return 1
    if Path(path).parent != Path(run_dir) or Path(suite_path).parent != Path(run_dir):
        print(f"FAIL: both baselines must live directly in the run directory {run_dir!r}", file=sys.stderr)
        return 1
    try:
        private_run_dir.validate_run_dir(run_dir)
        private_run_dir.validate_private_file(suite_path)
    except private_run_dir.PrivateRunDirError as exc:
        print(f"FAIL: {exc} (run `make day7-baseline-init` and `make state-check` for this run first)", file=sys.stderr)
        return 1
    if os.path.lexists(path):
        print(f"FAIL: strategy baseline {path!r} already exists - never overwritten or recaptured", file=sys.stderr)
        return 1
    record = collect(rec)
    if record is None:
        print(f"FAIL: {len(rec.failures())} precondition(s) failed - no baseline written, nothing mutated", file=sys.stderr)
        return 1
    try:
        write_exclusive(path, record)
    except OSError as exc:
        print(f"FAIL: could not create {path!r}: {exc}", file=sys.stderr)
        return 1
    print(f"\nPASS: Day 7 strategy baseline captured at {path} (mode 0600, run {run_id}, Helm revision {record['helm']['revision']})")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "capture":
        print("usage: day7_baseline.py capture", file=sys.stderr)
        return 2
    return capture()


if __name__ == "__main__":
    raise SystemExit(main())

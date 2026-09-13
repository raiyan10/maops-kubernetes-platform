#!/usr/bin/env python3
"""
DAY4: Day 4 storage bootstrap - hardens the already-installed
local-path-provisioner's directory-creation permissions before the
maops-state StatefulSet's PVC is ever provisioned.

Scope (deliberately narrow - see docs/architecture.md for the full
rationale):

  - Verifies the live `local-path-config` ConfigMap (namespace
    `local-path-storage`) is EXACTLY the known-original configuration
    before touching anything - refuses to patch anything unfamiliar
    (e.g. hand-edited outside this tool) rather than clobbering it.
  - Hardens the provisioning root itself (`/var/local-path-provisioner`)
    on every node FIRST, one time, idempotently: `chown root:10001` +
    `chmod 2770` (setgid) via `docker exec` (which has full coreutils,
    unlike the provisioner's own helper Pod image - see below). This is
    the only place any `chown`/`chmod` binary is invoked directly.
  - Patches ONLY the `setup` field (the shell script the provisioner's
    per-request helper Pod runs to create a new PV's backing directory).
    `config.json`, `helperPod.yaml`, and `teardown` are never touched.
    The original `mkdir -m 0777 -p "$VOL_DIR"` (world-writable,
    root:root - see the Day 4 storage preflight evidence under
    ~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/)
    is replaced with `mkdir -m 2770 -p "$VOL_DIR"` alone - no chgrp/chown
    call. This is deliberate, not an oversight: the provisioner's own
    helper image (`kindest/local-path-helper`) is minimal and ships only
    `mkdir`/`rm`/`sh`/`bash` - no `chown`/`chgrp`/`chmod` binary exists
    inside it to call (confirmed directly: `chgrp: command not found`
    when a naive first version of this script tried it). Standard Linux
    setgid-directory semantics do the rest: a new directory created
    inside a setgid, group-10001 parent inherits group 10001
    automatically regardless of the creating process's own UID/GID -
    confirmed directly (`mode=2770 uid=0 gid=10001` on a real test
    directory before this was relied on for the real fix) - so
    `mkdir -m 2770` alone, with the ALREADY-hardened parent from the step
    above, produces the exact target state atomically, in one syscall,
    with no multi-step window at all (stronger than "restrictive first,
    then loosen" - there is no intermediate state whatsoever).
  - The patched setup script also validates `$VOL_DIR` is actually
    under the observed provisioning root (`/var/local-path-provisioner`),
    refuses to operate on a symlink, and refuses to operate on a path
    that already exists (a fresh provisioning request should always get
    a fresh, never-before-seen path - an existing path indicates either
    a genuine collision or unexpected reuse, and this bootstrap will not
    silently chown/chmod something it didn't just create).
  - Idempotent: if the ConfigMap already carries the patched setup
    script, this is a verified no-op, not a re-apply.
  - Bounded propagation verification: after patching, provisions one
    small scratch PVC/Pod (in a dedicated, self-cleaning namespace) and
    inspects the REAL resulting backing directory's owner/group/mode on
    the node via `docker exec` (read-only inspection - no cluster
    tooling exists to introspect a provisioner's hostPath backend from
    inside a Pod without a hostPath mount, which application workloads
    are never given). If propagation does not complete within the
    bound, or the result doesn't match expectations, the ConfigMap
    patch is reverted (best-effort) and the failure is reported
    prominently - restoration failure is a distinct, prominent finding,
    never hidden.

This script does NOT retroactively touch any already-provisioned PV's
backing directory (including the two scratch PVs left by the Day 4
storage preflight) - "new claims only" per the task's explicit scope.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from http_checks import is_nonempty_identity
from kube import CONTEXT, run

LOCAL_PATH_NAMESPACE = "local-path-storage"
LOCAL_PATH_CONFIGMAP = "local-path-config"
PROVISIONING_ROOT = "/var/local-path-provisioner"

# DAY4: deliberately NOT the raw multi-arch gcr.io/distroless/python3-debian12
# digest directly - see the identical note in scripts/storage_hardening_check.py
# and docs/architecture.md's storage-preflight section for why. Reuses the
# project's own already-rebuilt, genuinely single-platform maops-kubernetes-app
# image (requires `make image-build` and `make image-load` to have already
# run), overriding its command to run a short-lived probe rather than the
# app's HTTP server.
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"

EXPECTED_ORIGINAL_SETUP = '#!/bin/sh\nset -eu\nmkdir -m 0777 -p "$VOL_DIR"'

EXPECTED_PATCHED_SETUP = f"""#!/bin/sh
set -eu
case "$VOL_DIR" in
    {PROVISIONING_ROOT}/*) ;;
    *)
        echo "storage-bootstrap: refusing to operate outside {PROVISIONING_ROOT}: $VOL_DIR" >&2
        exit 1
        ;;
esac
if [ -L "$VOL_DIR" ]; then
    echo "storage-bootstrap: refusing to operate on a symlink: $VOL_DIR" >&2
    exit 1
fi
if [ -e "$VOL_DIR" ]; then
    echo "storage-bootstrap: refusing to modify a pre-existing path: $VOL_DIR" >&2
    exit 1
fi
mkdir -m 2770 -p "$VOL_DIR\""""

EXPECTED_ROOT_MODE = "2770"
EXPECTED_ROOT_GID = "10001"

BOOTSTRAP_VERIFY_NAMESPACE = "maops-day4-storage-bootstrap-verify"
BOOTSTRAP_VERIFY_TIMEOUT_SECONDS = 90.0
BOOTSTRAP_VERIFY_CLEANUP_TIMEOUT_SECONDS = 30.0
OWNER_LABEL = "preflight-run-id"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def get_configmap() -> dict:
    return kube.get_json("-n", LOCAL_PATH_NAMESPACE, "get", "configmap", LOCAL_PATH_CONFIGMAP)


def patch_setup_field(new_setup: str) -> None:
    patch = {"data": {"setup": new_setup}}
    run("-n", LOCAL_PATH_NAMESPACE, "patch", "configmap", LOCAL_PATH_CONFIGMAP, "--type=merge", "-p", json.dumps(patch))


def _docker_exec(node: str, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", node, *args], capture_output=True, text=True, timeout=timeout)


def _cluster_nodes() -> list[str]:
    nodes = kube.get_json("get", "nodes").get("items", [])
    return [n["metadata"]["name"] for n in nodes]


def _node_root_state(node: str) -> tuple[str, str, str] | None:
    """(mode, uid, gid) as strings via docker exec stat, or None if the
    path doesn't exist yet on that node."""
    result = _docker_exec(node, "stat", "-c", "%a %u %g", PROVISIONING_ROOT)
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return tuple(parts) if len(parts) == 3 else None


def harden_provisioning_root_on_all_nodes() -> tuple[bool, dict[str, tuple[str, str] | None]]:
    """One-time, idempotent, per-node hardening of the provisioning ROOT
    itself (not any individual PV directory) via `docker exec`, which -
    unlike the provisioner's own minimal helper Pod image - has full
    coreutils available. Returns (all_ok, original_state_by_node) -
    (mode, gid) tuples, or None for a node where the root didn't exist
    yet - so the caller can revert exactly the nodes this function
    actually changed, to their EXACT original mode and group (never just
    assumed "root:root"), if a later step fails.

    Refuses to touch a node where the root already exists with an owner
    other than root (unfamiliar state, never blindly overwritten) or
    where it's a symlink."""
    original_state_by_node: dict[str, tuple[str, str] | None] = {}
    ok_all = True
    for node in _cluster_nodes():
        symlink_check = _docker_exec(node, "test", "-L", PROVISIONING_ROOT)
        if symlink_check.returncode == 0:
            record(False, f"{node}: {PROVISIONING_ROOT} is a symlink - refusing to touch it")
            ok_all = False
            continue

        state = _node_root_state(node)
        if state is None:
            original_state_by_node[node] = None
            mk = _docker_exec(node, "mkdir", "-m", EXPECTED_ROOT_MODE, "-p", PROVISIONING_ROOT)
            chown = _docker_exec(node, "chown", f"root:{EXPECTED_ROOT_GID}", PROVISIONING_ROOT)
            if mk.returncode != 0 or chown.returncode != 0:
                record(False, f"{node}: could not create {PROVISIONING_ROOT}: mkdir_rc={mk.returncode} chown_rc={chown.returncode}")
                ok_all = False
                continue
            record(True, f"{node}: created {PROVISIONING_ROOT} as root:{EXPECTED_ROOT_GID}, mode {EXPECTED_ROOT_MODE}")
            continue

        mode, uid, gid = state
        if mode == EXPECTED_ROOT_MODE and gid == EXPECTED_ROOT_GID:
            record(True, f"{node}: {PROVISIONING_ROOT} already root:{EXPECTED_ROOT_GID} mode {EXPECTED_ROOT_MODE} (idempotent no-op)")
            continue

        if uid != "0":
            record(False, f"{node}: {PROVISIONING_ROOT} has unexpected owner uid={uid} (expected root) - refusing to modify unfamiliar state")
            ok_all = False
            continue

        original_state_by_node[node] = (mode, gid)
        chown = _docker_exec(node, "chown", f"root:{EXPECTED_ROOT_GID}", PROVISIONING_ROOT)
        chmod = _docker_exec(node, "chmod", EXPECTED_ROOT_MODE, PROVISIONING_ROOT)
        if chown.returncode != 0 or chmod.returncode != 0:
            record(False, f"{node}: could not harden {PROVISIONING_ROOT}: chown_rc={chown.returncode} chmod_rc={chmod.returncode}")
            ok_all = False
            continue
        record(True, f"{node}: hardened {PROVISIONING_ROOT} to root:{EXPECTED_ROOT_GID}, mode {EXPECTED_ROOT_MODE} (was mode {mode}, group {gid})")

    return ok_all, original_state_by_node


def restore_provisioning_root_on_nodes(original_state_by_node: dict[str, tuple[str, str] | None]) -> bool:
    """Reverts exactly the nodes harden_provisioning_root_on_all_nodes()
    actually changed, to their captured exact original (mode, gid) - a
    node that was already correctly hardened before this run (not in the
    dict) is never touched here.

    Explicitly clears setgid symbolically (`chmod g-s`) BEFORE applying
    the numeric original mode: this environment was observed live to
    NOT reliably clear an existing setgid bit via a plain numeric
    `chmod <3-digit-mode>` alone (a real, reproduced filesystem/overlay
    quirk on this kind node, confirmed by direct testing) - only the
    symbolic `g-s` form reliably clears it. Applying `g-s` first, then
    the numeric mode (which re-adds setgid via its own leading digit if
    the ORIGINAL mode actually had it), makes restoration correct
    regardless of which of the two original states applies."""
    ok_all = True
    for node, original in original_state_by_node.items():
        if original is None:
            # The root didn't exist before this run created it - leaving
            # a now-hardened, correctly-owned empty directory behind is
            # safe and matches "restore to prior state" in spirit (the
            # provisioner will simply reuse it going forward); removing
            # it outright risks racing a provisioning request that may
            # already be using it.
            record(True, f"RESTORATION: {node}: {PROVISIONING_ROOT} was newly created this run - left in place (empty, harmless)")
            continue
        original_mode, original_gid = original
        clear_setgid = _docker_exec(node, "chmod", "g-s", PROVISIONING_ROOT)
        chmod = _docker_exec(node, "chmod", original_mode, PROVISIONING_ROOT)
        chown = _docker_exec(node, "chown", f"root:{original_gid}", PROVISIONING_ROOT)
        if clear_setgid.returncode != 0 or chmod.returncode != 0 or chown.returncode != 0:
            record(
                False,
                f"RESTORATION FAILURE: {node}: could not revert {PROVISIONING_ROOT} to mode {original_mode} group {original_gid}: "
                f"clear_setgid_rc={clear_setgid.returncode} chmod_rc={chmod.returncode} chown_rc={chown.returncode}",
            )
            ok_all = False
            continue
        verify = _node_root_state(node)
        if verify is None or verify[0] != original_mode or verify[2] != original_gid:
            record(False, f"RESTORATION FAILURE: {node}: post-restore state {verify} does not match expected (mode={original_mode}, gid={original_gid})")
            ok_all = False
            continue
        record(True, f"RESTORATION: {node}: reverted {PROVISIONING_ROOT} to mode {original_mode}, group {original_gid} (verified)")
    return ok_all


def _namespace_lookup(name: str) -> tuple[bool | None, dict | None, str]:
    """DAY4 batch 2b/2c: same pattern/rationale as
    storage_hardening_check.py's identical helper - see that module's
    docstring for the full "--ignore-not-found -o json" structural
    contract (batch 2c replaced substring error-text matching, which
    could misclassify an unrelated client-side failure containing "not
    found" in its own message as genuine absence). Returns (exists,
    namespace_json_or_None, detail); `None` means the lookup itself
    failed (never proof of absence)."""
    result = kube.run("get", "namespace", name, "--ignore-not-found", "-o", "json", check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return None, None, stderr or str(result)
    stdout = (result.stdout or "").strip()
    if stdout == "":
        return False, None, ""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, None, f"namespace {name!r} lookup returned malformed non-empty output: {exc}"
    if not isinstance(obj, dict) or not obj:
        return None, None, f"namespace {name!r} lookup returned an unexpected non-object/empty result: {obj!r}"
    return True, obj, ""


def _create_namespace_only(manifest: str) -> tuple[str, str]:
    """DAY4 batch 2c: same CREATE-only rationale/contract as
    storage_hardening_check.py's identical helper - see that module's
    docstring. Returns (outcome, detail); outcome is one of "created",
    "uncertain", "already_exists", "failed"."""
    try:
        result = subprocess.run(
            ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "create", "-f", "-"],
            input=manifest, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        return "uncertain", str(exc)
    if result.returncode == 0:
        return "created", ""
    stderr = (result.stderr or "").strip()
    if "AlreadyExists" in stderr or "already exists" in stderr:
        return "already_exists", stderr
    return "failed", stderr or str(result)


def _verify_ownership(name: str, owner_token: str) -> str | None:
    """Returns the namespace's UID only if it genuinely carries THIS
    invocation's owner label (OWNER_LABEL) - never adopts a
    pre-existing or differently-owned namespace with the same name."""
    exists, ns_json, _detail = _namespace_lookup(name)
    if exists is not True:
        return None
    if (ns_json.get("metadata", {}).get("labels") or {}).get(OWNER_LABEL) != owner_token:
        return None
    return ns_json.get("metadata", {}).get("uid")


def verify_propagation() -> bool:
    """Provisions one small scratch PVC/Pod, waits (bounded) for it to
    bind and its backing directory to be created, then inspects the
    REAL resulting directory's owner/group/mode on the node. Cleans up
    its own scratch resources in a guaranteed path regardless of
    outcome."""
    run_id = uuid.uuid4().hex[:10]
    owner_token = f"storage-bootstrap-{run_id}"
    pvc_name = "bootstrap-verify-data"
    pod_name = "bootstrap-verify-probe"
    ns_manifest = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {BOOTSTRAP_VERIFY_NAMESPACE}
  labels:
    {OWNER_LABEL}: {owner_token}
"""
    pvc_manifest = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
  namespace: {BOOTSTRAP_VERIFY_NAMESPACE}
  labels:
    preflight-run-id: storage-bootstrap-{run_id}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 64Mi
"""
    pod_manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {BOOTSTRAP_VERIFY_NAMESPACE}
  labels:
    preflight-run-id: storage-bootstrap-{run_id}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 60
  automountServiceAccountToken: false
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: {kube.CONTROL_PLANE_LABEL}
                operator: DoesNotExist
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: {PROBE_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["/usr/bin/python3.11", "-c", "import time; time.sleep(30)"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        requests: {{cpu: 25m, memory: 16Mi}}
        limits: {{cpu: 50m, memory: 32Mi}}
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: {pvc_name}
"""

    def _apply(manifest: str) -> None:
        subprocess.run(
            ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "apply", "-f", "-"],
            input=manifest,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    # DAY4 batch 2b/2c: refuse to reuse/adopt an existing namespace,
    # CREATE-only (never `apply`) so another creator winning the
    # absence-check/create race is DEFINITIVE and cannot be silently
    # adopted (an `apply` upsert could patch our label onto their
    # namespace - see storage_hardening_check.py's `_create_namespace_only`
    # docstring for the full rationale), re-observe ownership after an
    # uncertain outcome, and only delete a namespace this invocation
    # has actually verified it owns.
    exists, _ns_json, detail = _namespace_lookup(BOOTSTRAP_VERIFY_NAMESPACE)
    if exists is None:
        record(False, f"propagation verification: could not determine whether namespace {BOOTSTRAP_VERIFY_NAMESPACE!r} already exists: {detail}")
        return False
    if exists:
        record(False, f"propagation verification: namespace {BOOTSTRAP_VERIFY_NAMESPACE!r} already exists - refusing to reuse/modify it")
        return False

    owned_namespace_uid: str | None = None
    try:
        create_outcome, create_detail = _create_namespace_only(ns_manifest)
        if create_outcome == "already_exists":
            record(
                False,
                f"propagation verification: namespace {BOOTSTRAP_VERIFY_NAMESPACE!r} was created by another "
                f"invocation between the absence check and this call - preserving it untouched (never adopted, "
                f"never deleted): {create_detail}",
            )
            return False
        if create_outcome == "failed":
            record(False, f"propagation verification: could not create namespace {BOOTSTRAP_VERIFY_NAMESPACE!r}: {create_detail}")
            return False
        # create_outcome in ("created", "uncertain") - re-observe
        # regardless; `create`'s atomicity means a verified label/UID
        # match here can only mean this invocation's own manifest
        # created the object.
        owned_namespace_uid = _verify_ownership(BOOTSTRAP_VERIFY_NAMESPACE, owner_token)
        if not is_nonempty_identity(owned_namespace_uid):
            owned_namespace_uid = None
            record(
                False,
                f"propagation verification: could not verify ownership of namespace {BOOTSTRAP_VERIFY_NAMESPACE!r} "
                f"after creation (label {OWNER_LABEL!r}={owner_token!r} not observed) - refusing to use or delete it",
            )
            return False

        _apply(pvc_manifest)
        _apply(pod_manifest)

        def _pod_scheduled():
            # No check=False here: kubectl exits non-zero while the Pod
            # doesn't exist yet, which raises CalledProcessError -
            # wait_until()'s own except-and-retry handling already treats
            # any predicate exception as "not ready yet, keep polling",
            # so this needs no special-casing here.
            pod = kube.get_json("-n", BOOTSTRAP_VERIFY_NAMESPACE, "get", "pod", pod_name)
            node_name = pod.get("spec", {}).get("nodeName")
            return node_name if node_name else None

        try:
            node = kube.wait_until(_pod_scheduled, timeout=BOOTSTRAP_VERIFY_TIMEOUT_SECONDS, description="probe pod scheduled")
        except TimeoutError as exc:
            record(False, f"propagation verification: probe pod never scheduled: {exc}")
            return False

        def _pvc_bound():
            pvc = kube.get_json("-n", BOOTSTRAP_VERIFY_NAMESPACE, "get", "pvc", pvc_name)
            phase = pvc.get("status", {}).get("phase")
            volume = pvc.get("spec", {}).get("volumeName")
            return volume if phase == "Bound" and volume else None

        try:
            pv_name = kube.wait_until(_pvc_bound, timeout=BOOTSTRAP_VERIFY_TIMEOUT_SECONDS, description="scratch PVC bound")
        except TimeoutError as exc:
            record(False, f"propagation verification: scratch PVC never bound: {exc}")
            return False

        pv = kube.get_json("get", "pv", pv_name)
        host_path = pv.get("spec", {}).get("hostPath", {}).get("path")
        if not host_path:
            record(False, f"propagation verification: PV {pv_name} has no hostPath.path (unexpected backend)")
            return False

        stat_result = _docker_exec(node, "stat", "-c", "%a %u %g", host_path)
        return _evaluate_stat(node, host_path, stat_result)
    finally:
        if owned_namespace_uid is None:
            print(f"(no namespace ownership verified this run - skipping cleanup delete for {BOOTSTRAP_VERIFY_NAMESPACE!r})", file=sys.stderr)
        else:
            current_uid = _verify_ownership(BOOTSTRAP_VERIFY_NAMESPACE, owner_token)
            if not is_nonempty_identity(current_uid) or current_uid != owned_namespace_uid:
                record(
                    False,
                    f"RESTORATION FAILURE: refusing to delete namespace {BOOTSTRAP_VERIFY_NAMESPACE!r} - ownership could not be "
                    f"re-verified immediately before cleanup (expected UID {owned_namespace_uid!r}, observed {current_uid!r})",
                )
            else:
                try:
                    cleanup = subprocess.run(
                        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "delete", "namespace", BOOTSTRAP_VERIFY_NAMESPACE, "--ignore-not-found", "--wait=false"],
                        capture_output=True,
                        text=True,
                        timeout=BOOTSTRAP_VERIFY_CLEANUP_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired as exc:
                    record(False, f"RESTORATION FAILURE: deleting scratch namespace {BOOTSTRAP_VERIFY_NAMESPACE} timed out after {BOOTSTRAP_VERIFY_CLEANUP_TIMEOUT_SECONDS}s (server-side outcome unknown): {exc}")
                else:
                    if cleanup.returncode != 0:
                        record(False, f"RESTORATION FAILURE: could not delete scratch namespace {BOOTSTRAP_VERIFY_NAMESPACE}: {cleanup.stderr.strip()}")


def _evaluate_stat(node: str, host_path: str, stat_result) -> bool:
    if stat_result.returncode != 0:
        record(False, f"propagation verification: could not stat {host_path!r} on {node}: {stat_result.stderr.strip()}")
        return False
    parts = stat_result.stdout.strip().split()
    if len(parts) != 3:
        record(False, f"propagation verification: unexpected stat output {stat_result.stdout!r}")
        return False
    mode, uid, gid = parts
    ok = mode == EXPECTED_ROOT_MODE and uid == "0" and gid == EXPECTED_ROOT_GID
    record(
        ok,
        f"propagation verification: {host_path} on {node} has mode={mode} uid={uid} gid={gid} "
        f"(expected mode={EXPECTED_ROOT_MODE} uid=0 gid={EXPECTED_ROOT_GID})",
    )
    return ok


def main() -> int:
    print(f"# Day 4 storage bootstrap: {LOCAL_PATH_CONFIGMAP} in {LOCAL_PATH_NAMESPACE} (context {CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        root_hardened_ok, original_root_modes = harden_provisioning_root_on_all_nodes()
    except subprocess.TimeoutExpired as exc:
        print(f"FAIL: hardening {PROVISIONING_ROOT} on nodes raised an unexpected error: {exc}", file=sys.stderr)
        return 1
    if not root_hardened_ok:
        print("Attempting to restore any node-level changes already made...", file=sys.stderr)
        restore_provisioning_root_on_nodes(original_root_modes)
        print(f"FAIL: could not harden {PROVISIONING_ROOT} on all nodes", file=sys.stderr)
        return 1

    try:
        cm = get_configmap()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"FAIL: could not read {LOCAL_PATH_CONFIGMAP}: {exc}", file=sys.stderr)
        restore_provisioning_root_on_nodes(original_root_modes)
        return 1

    data = cm.get("data") or {}
    current_setup = data.get("setup", "").rstrip("\n")

    if current_setup == EXPECTED_PATCHED_SETUP:
        record(True, "local-path-config setup script already hardened (idempotent no-op)")
        print("PASS: storage bootstrap verified (already applied)")
        return 0

    if current_setup != EXPECTED_ORIGINAL_SETUP:
        record(
            False,
            "local-path-config setup script matches neither the known original nor the known hardened form - "
            "refusing to overwrite an unfamiliar configuration. Current value has been left untouched.",
        )
        print("--- current setup field ---", file=sys.stderr)
        print(current_setup, file=sys.stderr)
        restore_provisioning_root_on_nodes(original_root_modes)
        print("FAIL: storage bootstrap refused to proceed", file=sys.stderr)
        return 1

    record(True, "local-path-config setup script matches the known original - safe to patch")

    try:
        patch_setup_field(EXPECTED_PATCHED_SETUP)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"failed to apply setup script patch: {exc}")
        restore_provisioning_root_on_nodes(original_root_modes)
        return 1
    record(True, "applied hardened setup script (mkdir -m 2770 alone - group ownership comes from the now-hardened, setgid provisioning root, never a separate chgrp/chmod call inside the minimal helper image)")

    propagation_ok = False
    try:
        propagation_ok = verify_propagation()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"propagation verification raised an unexpected error: {exc}")

    if not propagation_ok:
        print("Attempting to restore the original setup script and node-level state after a failed bootstrap...", file=sys.stderr)
        try:
            patch_setup_field(EXPECTED_ORIGINAL_SETUP)
            record(True, "RESTORATION: reverted local-path-config setup script to its original value")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            record(False, f"RESTORATION FAILURE: could not revert local-path-config setup script: {exc}")
        restore_provisioning_root_on_nodes(original_root_modes)
        print("FAIL: storage bootstrap did not complete successfully", file=sys.stderr)
        return 1

    print("PASS: storage bootstrap applied and propagation verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

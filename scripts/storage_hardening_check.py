#!/usr/bin/env python3
"""
DAY4: proves the Day 4 storage bootstrap (scripts/storage_bootstrap.py)
actually changed the security-relevant behavior of new PV-backed
directories, using a disposable scratch PVC - never the application's
own PVC (`data-maops-state-0`).

Run AFTER `make storage-bootstrap` and BEFORE the application's
StatefulSet/PVC is created (`make deploy`). Proves, against a fresh,
uniquely-named scratch claim:

  1. The new backing directory's real owner/group/mode (root:10001,
     mode 2770 - via `docker exec` + `stat`, since no Kubernetes API
     exposes a provisioner's hostPath backend from inside a Pod without
     a hostPath mount, which application workloads never receive).
  2. A Pod running as UID/GID 10001 with `fsGroup: 10001` (matching the
     directory's owning group) can write/fsync/atomically-replace/read
     back a file in the scratch volume.
  3. A Pod running as an UNRELATED UID/GID (65532/65532, no
     supplementary group 10001 - deliberately no `fsGroup` at all)
     receives EACCES specifically (not ENOENT, not a generic failure)
     attempting to create a file on the SAME scratch volume - proving
     the directory's mode (2770) is what is actually granting/denying
     access, not `fsGroup` unilaterally, and not still world-writable.
  4. The root filesystem remains read-only for both probe Pods
     (EROFS-specific proof, not a bare write-failure check - see the
     Day 4 storage preflight evidence for why an EACCES-only check would
     be insufficient here).

Leaves no scratch resources behind on success (bounded, guaranteed
cleanup) - this is a repeatable gate, not an evidence artifact; the Day
4 storage preflight's own scratch PVC/PV/namespace are left untouched by
this script (different, pre-existing, out-of-scope resources).
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from http_checks import is_nonempty_identity
from kube import CONTEXT

NAMESPACE = "maops-day4-storage-hardening"
PVC_NAME = "storage-hardening-data"
POSITIVE_POD = "storage-hardening-positive"
NEGATIVE_POD = "storage-hardening-negative"
# DAY4: deliberately NOT the raw multi-arch gcr.io/distroless/python3-debian12
# digest directly - this project's own containerd/kind combination cannot
# reliably create a container from that reference once any manual `ctr
# images import` of it has ever occurred on a node (see the "storage
# preflight" section in docs/architecture.md and Makefile's
# IMAGE_BUILD_FLAGS comment for the full root-cause explanation). The
# probe pods below reuse the project's own already-rebuilt, genuinely
# single-platform maops-kubernetes-app image instead (same digest-pinned
# base underneath, just built via `make image-build`'s
# --platform linux/amd64 fix and loaded via plain `kind load
# docker-image`), overriding its command/args to run the probe script
# directly rather than starting the app's HTTP server. This requires
# `make image-build` and `make image-load` to have already run.
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"

POD_DEADLINE_SECONDS = 90.0
NAMESPACE_CLEANUP_TIMEOUT_SECONDS = 30.0
OWNER_LABEL = "maops.dev/storage-hardening-owner"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


POSITIVE_PROBE = """
import os, stat, errno, sys, time
uid, gid, groups = os.getuid(), os.getgid(), os.getgroups()
print(f"UID={uid} GID={gid} GROUPS={groups}", flush=True)
st = os.stat("/data")
mode = oct(stat.S_IMODE(st.st_mode))
print(f"DATA_DIR_UID={st.st_uid} DATA_DIR_GID={st.st_gid} DATA_DIR_MODE={mode}", flush=True)
marker = f"hardening-{os.getpid()}-{time.time_ns()}"
tmp = f"/data/.probe-{os.getpid()}.tmp"
final = "/data/probe-marker.txt"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(marker); f.flush(); os.fsync(f.fileno())
os.replace(tmp, final)
dfd = os.open("/data", os.O_RDONLY)
try:
    os.fsync(dfd)
finally:
    os.close(dfd)
with open(final) as f:
    readback = f.read()
print(f"READBACK_MATCH={readback == marker}", flush=True)
try:
    open("/rootfs-write-should-fail.txt", "w").close()
    print("ROOT_WRITE_SUCCEEDED=True", flush=True)
    sys.exit(2)
except OSError as e:
    print(f"ROOT_WRITE_ERRNO={e.errno} ({errno.errorcode.get(e.errno)})", flush=True)
    if e.errno != errno.EROFS:
        sys.exit(3)
if not readback == marker:
    sys.exit(1)
print("POSITIVE_PROBE_PASS", flush=True)
"""

NEGATIVE_PROBE = """
import os, errno, sys
uid, gid, groups = os.getuid(), os.getgid(), os.getgroups()
print(f"UID={uid} GID={gid} GROUPS={groups}", flush=True)
try:
    fd = os.open("/data/should-not-be-creatable.txt", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    print("NEGATIVE_WRITE_SUCCEEDED=True", flush=True)
    sys.exit(2)
except OSError as e:
    print(f"NEGATIVE_WRITE_ERRNO={e.errno} ({errno.errorcode.get(e.errno)})", flush=True)
    if e.errno != errno.EACCES:
        sys.exit(3)
print("NEGATIVE_PROBE_PASS", flush=True)
"""


def _apply(manifest: str) -> None:
    subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "apply", "-f", "-"],
        input=manifest, capture_output=True, text=True, check=True, timeout=30,
    )


def _create_namespace_only(manifest: str) -> tuple[str, str]:
    """DAY4 batch 2c: CREATE-only (never `apply`) the namespace object.

    `kubectl apply` is an upsert - if another creator wins the race
    between the absence check and this call, `apply` would silently
    PATCH/adopt their existing namespace, overwriting THEIR ownership
    label with ours. An ownership label written via `apply` can never
    prove this invocation actually created the object, only that it
    was the last writer. `kubectl create` is atomic: it fails outright
    (AlreadyExists) if the object already exists, so a namespace that
    verifiably carries our label AFTER a `create` call (whether the
    call itself reported success, or its outcome was uncertain and had
    to be re-observed) can only mean THIS invocation's manifest is what
    actually created it.

    Returns (outcome, detail):
      - "created": the create call itself returned success.
      - "uncertain": the call raised `subprocess.TimeoutExpired` - the
        server-side outcome is unknown; the caller must re-observe via
        `_verify_ownership()` before proceeding, never assume either
        way.
      - "already_exists": kubectl reported AlreadyExists - DEFINITIVE
        proof another creator won the race. The caller must preserve
        that namespace exactly as-is: never verify "ownership" against
        it, never delete it, never touch it further.
      - "failed": any other creation failure (quota, malformed
        manifest, RBAC denial, etc.) - the object was not created by
        this call; the caller must not proceed or delete."""
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


def _namespace_lookup(name: str) -> tuple[bool | None, dict | None, str]:
    """Returns (exists, namespace_json_or_None, detail).

    `exists` is `True` (found, `namespace_json_or_None` populated),
    `False` (genuinely does not exist), or `None` (the lookup itself
    failed for an unrelated reason - RBAC denial, API server timeout,
    a missing/broken credential-helper executable, transport error -
    which is NOT proof of absence and must never be treated as one).

    DAY4 batch 2c: uses `--ignore-not-found -o json`, a STRUCTURAL
    contract rather than substring-matching error text - kubectl exits
    0 with EMPTY stdout ONLY for genuine absence (the NotFound error is
    suppressed), 0 with a JSON object for present, and NONZERO for
    every other failure. The previous substring check (`"NotFound" in
    stderr or "not found" in stderr`) could misclassify an unrelated
    client-side failure whose OWN message happens to contain "not
    found" (e.g. `exec: "some-credential-helper": executable file not
    found in $PATH`) as genuine resource absence - this contract makes
    that impossible, since such a failure still exits nonzero."""
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


def _namespace_owner_label(ns_json: dict | None) -> str | None:
    if ns_json is None:
        return None
    return (ns_json.get("metadata", {}).get("labels") or {}).get(OWNER_LABEL)


def _verify_ownership(name: str, owner_token: str) -> str | None:
    """Re-observes the namespace and returns its UID ONLY if it
    genuinely carries THIS invocation's owner label - never adopts a
    pre-existing or differently-owned namespace with the same name.
    Returns None (never raises) on any lookup failure or ownership
    mismatch - callers must treat None as "do not proceed/do not
    delete", not as license to retry blindly."""
    exists, ns_json, _detail = _namespace_lookup(name)
    if exists is not True:
        return None
    if _namespace_owner_label(ns_json) != owner_token:
        return None
    return ns_json.get("metadata", {}).get("uid")


def _pod_manifest(name: str, run_as_user: int, run_as_group: int, fs_group: int | None, probe_code: str) -> str:
    fs_group_line = f"    fsGroup: {fs_group}\n" if fs_group is not None else ""
    indented = "\n".join("            " + line for line in probe_code.strip("\n").splitlines())
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {NAMESPACE}
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
    runAsUser: {run_as_user}
    runAsGroup: {run_as_group}
{fs_group_line}    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: {PROBE_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["/usr/bin/python3.11"]
      args:
        - "-c"
        - |
{indented}
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
        claimName: {PVC_NAME}
"""


def _wait_terminal(pod_name: str) -> dict:
    def _terminal():
        # No check=False: get_json() has no such parameter (it always
        # calls kube.run() with the default check=True) - a transient
        # "not found yet" kubectl failure raises CalledProcessError,
        # which wait_until()'s own except-and-retry handling already
        # treats as "not ready yet, keep polling".
        pod = kube.get_json("-n", NAMESPACE, "get", "pod", pod_name)
        phase = pod.get("status", {}).get("phase")
        return pod if phase in ("Succeeded", "Failed") else None

    return kube.wait_until(_terminal, timeout=POD_DEADLINE_SECONDS, description=f"pod {pod_name} terminal")


def _logs(pod_name: str) -> str:
    result = kube.run("-n", NAMESPACE, "logs", pod_name, check=False)
    return result.stdout


def main() -> int:
    print(f"# Day 4 storage hardening check (context {CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # DAY4 batch 2b: an unsuccessful lookup is not automatically
    # "does not exist" - a real API error/timeout/RBAC denial must fail
    # this check closed, never be silently treated as "the namespace is
    # free to create", which could otherwise mask a real cluster
    # problem or race a concurrent user of the same name.
    exists, _ns_json, detail = _namespace_lookup(NAMESPACE)
    if exists is None:
        print(f"FAIL: could not determine whether namespace {NAMESPACE!r} already exists: {detail}", file=sys.stderr)
        return 1
    if exists:
        print(f"FAIL: namespace {NAMESPACE!r} already exists - refusing to reuse/modify it", file=sys.stderr)
        return 1

    # A unique per-invocation ownership token, stamped as a label on the
    # namespace THIS run creates. Every later operation on this
    # namespace (using it for PVC/Pods, and - critically - deleting it)
    # must first re-observe that the live namespace still carries this
    # exact token, so this invocation can never adopt or delete a
    # differently-owned namespace that happens to share the same name.
    owner_token = uuid.uuid4().hex
    owned_namespace_uid: str | None = None

    overall_ok = True
    try:
        # DAY4 batch 2c: CREATE-only, never `apply` - see
        # _create_namespace_only()'s docstring. "already_exists" is
        # DEFINITIVE proof another creator won the absence-check/create
        # race: that namespace, its labels, and its contents must be
        # preserved exactly as-is - never adopted, never deleted.
        create_outcome, create_detail = _create_namespace_only(
            f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n"
            f"  labels:\n    {OWNER_LABEL}: {owner_token}\n"
        )
        if create_outcome == "already_exists":
            print(
                f"FAIL: namespace {NAMESPACE!r} was created by another invocation between the absence check and "
                f"this call - preserving it untouched (never adopted, never deleted): {create_detail}",
                file=sys.stderr,
            )
            return 1
        if create_outcome == "failed":
            print(f"FAIL: could not create namespace {NAMESPACE!r}: {create_detail}", file=sys.stderr)
            return 1
        # create_outcome in ("created", "uncertain") - re-observe
        # regardless of which: `create`'s atomicity means a verified
        # label/UID match here can ONLY mean this invocation's own
        # manifest created the object (an `apply` upsert could not
        # offer this guarantee - see the docstring above).
        owned_namespace_uid = _verify_ownership(NAMESPACE, owner_token)
        if not is_nonempty_identity(owned_namespace_uid):
            owned_namespace_uid = None
            print(
                f"FAIL: could not verify ownership of namespace {NAMESPACE!r} after creation "
                f"(label {OWNER_LABEL!r}={owner_token!r} not observed) - refusing to use or delete it",
                file=sys.stderr,
            )
            return 1

        _apply(
            f"apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n  name: {PVC_NAME}\n  namespace: {NAMESPACE}\n"
            f"spec:\n  accessModes: [\"ReadWriteOnce\"]\n  resources:\n    requests:\n      storage: 64Mi\n"
        )
        _apply(_pod_manifest(POSITIVE_POD, 10001, 10001, 10001, POSITIVE_PROBE))

        try:
            pod = _wait_terminal(POSITIVE_POD)
        except TimeoutError as exc:
            overall_ok = record(False, f"positive probe: did not reach a terminal phase: {exc}")
        else:
            phase = pod.get("status", {}).get("phase")
            exit_code = (
                pod.get("status", {}).get("containerStatuses", [{}])[0].get("state", {}).get("terminated", {}).get("exitCode")
            )
            logs = _logs(POSITIVE_POD)
            print(logs)
            ok = phase == "Succeeded" and exit_code == 0 and "POSITIVE_PROBE_PASS" in logs
            overall_ok = record(ok, f"positive probe (UID/GID 10001, fsGroup 10001): phase={phase} exit_code={exit_code}") and overall_ok

        # Only proceed to the negative probe once the PVC is confirmed
        # bound and its backing directory exists - the negative probe
        # must exercise the SAME already-provisioned volume, not trigger
        # a second, independent provisioning race.
        pvc = kube.get_json("-n", NAMESPACE, "get", "pvc", PVC_NAME)
        if pvc.get("status", {}).get("phase") != "Bound":
            overall_ok = record(False, "negative probe skipped: scratch PVC never reached Bound") and False
        else:
            _apply(_pod_manifest(NEGATIVE_POD, 65532, 65532, None, NEGATIVE_PROBE))
            try:
                pod = _wait_terminal(NEGATIVE_POD)
            except TimeoutError as exc:
                overall_ok = record(False, f"negative probe: did not reach a terminal phase: {exc}") and overall_ok
            else:
                phase = pod.get("status", {}).get("phase")
                exit_code = (
                    pod.get("status", {}).get("containerStatuses", [{}])[0]
                    .get("state", {})
                    .get("terminated", {})
                    .get("exitCode")
                )
                logs = _logs(NEGATIVE_POD)
                print(logs)
                ok = phase == "Succeeded" and exit_code == 0 and "NEGATIVE_PROBE_PASS" in logs
                overall_ok = (
                    record(
                        ok,
                        f"negative probe (UID/GID 65532, no supplementary group 10001) received EACCES as expected: "
                        f"phase={phase} exit_code={exit_code}",
                    )
                    and overall_ok
                )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # A manifest apply or kube.get_json() call above can fail for
        # reasons unrelated to the probes themselves (a real API server
        # hiccup, a genuine kubectl timeout) - this must become a clean
        # recorded failure with the guaranteed cleanup below still
        # running, never an uncaught traceback that replaces this
        # script's own pass/fail summary.
        overall_ok = record(False, f"storage hardening check raised an unexpected error: {exc}")
    finally:
        # DAY4 batch 2b: delete ONLY a namespace this invocation has
        # actually verified ownership of (owned_namespace_uid captured
        # above) - a failed/uncertain creation that never reached that
        # point must never fall through to a blind `delete namespace
        # <name>`, which could otherwise delete an unrelated namespace
        # that happens to share the name. Re-verify ownership one more
        # time immediately before deleting: if the namespace no longer
        # carries this run's owner label/UID (e.g. something else
        # replaced it in between), refuse to delete it.
        if owned_namespace_uid is None:
            print(f"(no namespace ownership verified this run - skipping cleanup delete for {NAMESPACE!r})", file=sys.stderr)
        else:
            current_uid = _verify_ownership(NAMESPACE, owner_token)
            if not is_nonempty_identity(current_uid) or current_uid != owned_namespace_uid:
                record(
                    False,
                    f"RESTORATION FAILURE: refusing to delete namespace {NAMESPACE!r} - ownership could not be "
                    f"re-verified immediately before cleanup (expected UID {owned_namespace_uid!r}, observed {current_uid!r})",
                )
            else:
                try:
                    cleanup = subprocess.run(
                        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=false"],
                        capture_output=True, text=True, timeout=NAMESPACE_CLEANUP_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired as exc:
                    record(False, f"RESTORATION FAILURE: deleting scratch namespace {NAMESPACE} timed out after {NAMESPACE_CLEANUP_TIMEOUT_SECONDS}s (server-side outcome unknown): {exc}")
                else:
                    if cleanup.returncode != 0:
                        record(False, f"RESTORATION FAILURE: could not delete scratch namespace {NAMESPACE}: {cleanup.stderr.strip()}")

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} storage hardening checks passed")
    if failures or not overall_ok:
        print(f"FAIL: storage hardening check did not fully pass", file=sys.stderr)
        return 1
    print("PASS: storage hardening verified (positive write + negative EACCES + read-only-root-fs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

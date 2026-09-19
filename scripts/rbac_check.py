#!/usr/bin/env python3
"""
DAY5: proves maops-diagnostics' RBAC scope against the LIVE cluster,
using a real mounted ServiceAccount token from inside an ephemeral
probe Pod - never `kubectl auth can-i --as=`, which only proves what
the INVOKING identity's own impersonation rights say, not what the
diagnostics token itself is actually authorized to do end to end
(token mount + API server authorization, together).

The probe Pod runs as ServiceAccount maops-diagnostics in
maops-day5-validation (the one identity in this project with
automountServiceAccountToken: true) and makes real HTTPS calls
directly to the in-cluster API server (https://kubernetes.default.svc)
using its own mounted token and CA bundle, asserting the EXACT HTTP
status code the API server returns for each case - never inferring
"denied" from a bare non-2xx response without confirming it is
specifically 403 Forbidden (a 404/400/etc. would mean something else
entirely and must not be misread as an authorization proof).

Proves, all from the SAME probe Pod/token in one run:
  - ALLOWED (200): GET pods, services, endpointslices in maops-platform.
  - DENIED (403): GET secrets in maops-platform.
  - DENIED (403): DELETE a Deployment in maops-platform (workload
    deletion).
  - DENIED (403): PATCH a Deployment's /scale subresource in
    maops-platform (workload scaling). Kubernetes performs RBAC
    authorization in the API server's authorization filter chain
    BEFORE the request body is decoded/validated, so an unauthorized
    PATCH is rejected 403 regardless of body content - this probe
    sends no body at all and still expects 403, never a body-shape
    error, which would indicate the request reached authorization
    successfully (a hole) rather than being rejected by it.
  - DENIED (403): GET pods in a different namespace (kube-system).
  - DENIED (403): GET a cluster-scoped resource (nodes).

Creates and deletes exactly one short-lived Pod, with guaranteed
cleanup in a `finally` block - never leaves a probe Pod behind, and
never mutates anything in maops-platform (every write-shaped case here
is expected to be REJECTED, and the script asserts the rejection
itself, not a mutation succeeding).
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import CONTEXT, DIAGNOSTICS_SERVICE_ACCOUNT, NAMESPACE, VALIDATION_NAMESPACE

POD_DEADLINE_SECONDS = 60.0
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
# Reuses the already-loaded maops-kubernetes-app image (command
# overridden to run the probe script, never the app's HTTP server) -
# same rationale as storage_hardening_check.py: avoids referencing the
# raw multi-arch Distroless base digest directly on this local
# kind/containerd combination. Requires `make image-load` to have
# already run.
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"

POD_NAME = f"rbac-check-{uuid.uuid4().hex[:10]}"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


# Plain (non-f) triple-quoted string: every brace below is literal
# Python source for the PROBE itself, not an outer f-string
# interpolation - avoids any brace-escaping confusion. Runs entirely
# inside the probe Pod under the maops-diagnostics token; never prints
# the token itself.
_PROBE = """
import http.client
import ssl

TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

with open(TOKEN_PATH) as f:
    token = f.read().strip()

ctx = ssl.create_default_context(cafile=CA_PATH)
headers = {"Authorization": "Bearer " + token}


def call(method, path):
    conn = http.client.HTTPSConnection("kubernetes.default.svc", 443, timeout=10, context=ctx)
    try:
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


CASES = [
    ("pods_read_allowed", "GET", "/api/v1/namespaces/maops-platform/pods"),
    ("services_read_allowed", "GET", "/api/v1/namespaces/maops-platform/services"),
    ("endpointslices_read_allowed", "GET", "/apis/discovery.k8s.io/v1/namespaces/maops-platform/endpointslices"),
    ("secrets_read_denied", "GET", "/api/v1/namespaces/maops-platform/secrets"),
    ("deployment_delete_denied", "DELETE", "/apis/apps/v1/namespaces/maops-platform/deployments/maops-gateway"),
    ("deployment_scale_denied", "PATCH", "/apis/apps/v1/namespaces/maops-platform/deployments/maops-app/scale"),
    ("cross_namespace_read_denied", "GET", "/api/v1/namespaces/kube-system/pods"),
    ("cluster_wide_read_denied", "GET", "/api/v1/nodes"),
]

for name, method, path in CASES:
    status = call(method, path)
    print(f"CASE={name} STATUS={status}", flush=True)
"""


def _pod_manifest() -> str:
    indented = "\n".join("            " + line for line in _PROBE.strip("\n").splitlines())
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {POD_NAME}
  namespace: {VALIDATION_NAMESPACE}
  labels:
    app.kubernetes.io/component: diagnostics
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 45
  serviceAccountName: {DIAGNOSTICS_SERVICE_ACCOUNT}
  automountServiceAccountToken: true
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
    seccompProfile:
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


def _wait_terminal() -> dict:
    def _terminal():
        pod = kube.get_json("-n", VALIDATION_NAMESPACE, "get", "pod", POD_NAME)
        phase = pod.get("status", {}).get("phase")
        return pod if phase in ("Succeeded", "Failed") else None

    return kube.wait_until(_terminal, timeout=POD_DEADLINE_SECONDS, description=f"pod {POD_NAME} terminal")


def _logs() -> str:
    result = kube.run("-n", VALIDATION_NAMESPACE, "logs", POD_NAME, check=False)
    return result.stdout


def _parse_cases(logs: str) -> dict[str, int]:
    observed: dict[str, int] = {}
    for line in logs.splitlines():
        line = line.strip()
        if not line.startswith("CASE="):
            continue
        try:
            case_part, status_part = line.split(" STATUS=", 1)
            observed[case_part[len("CASE=") :]] = int(status_part.strip())
        except (ValueError, IndexError):
            continue
    return observed


# (case name, expected HTTP status, human description)
_EXPECTED = [
    ("pods_read_allowed", 200, "diagnostics can list Pods in maops-platform"),
    ("services_read_allowed", 200, "diagnostics can list Services in maops-platform"),
    ("endpointslices_read_allowed", 200, "diagnostics can list EndpointSlices in maops-platform"),
    ("secrets_read_denied", 403, "diagnostics CANNOT read Secrets in maops-platform"),
    ("deployment_delete_denied", 403, "diagnostics CANNOT delete a Deployment in maops-platform"),
    ("deployment_scale_denied", 403, "diagnostics CANNOT scale a Deployment in maops-platform"),
    ("cross_namespace_read_denied", 403, "diagnostics CANNOT read Pods in kube-system (cross-namespace)"),
    ("cluster_wide_read_denied", 403, "diagnostics CANNOT list cluster-scoped Nodes"),
]


def main() -> int:
    print(f"# Day 5 RBAC check: maops-diagnostics scope (context {CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    overall_ok = True
    try:
        _apply(_pod_manifest())
        try:
            pod = _wait_terminal()
        except TimeoutError as exc:
            overall_ok = record(False, f"probe pod did not reach a terminal phase: {exc}")
        else:
            phase = pod.get("status", {}).get("phase")
            logs = _logs()
            print(logs)
            observed = _parse_cases(logs)
            for case_name, expected_status, description in _EXPECTED:
                status = observed.get(case_name)
                ok = status == expected_status
                overall_ok = (
                    record(ok, f"{description}: HTTP {status} (expected {expected_status})") and overall_ok
                )
            overall_ok = record(phase == "Succeeded", f"probe pod phase == Succeeded (found {phase!r})") and overall_ok
            overall_ok = (
                record(len(observed) == len(_EXPECTED), f"observed {len(observed)}/{len(_EXPECTED)} probe cases in pod logs")
                and overall_ok
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        overall_ok = record(False, f"RBAC check raised an unexpected error: {exc}")
    finally:
        cleanup = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                kube.KUBECONFIG_PATH,
                "--context",
                CONTEXT,
                "-n",
                VALIDATION_NAMESPACE,
                "delete",
                "pod",
                POD_NAME,
                "--ignore-not-found",
                "--wait=false",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if cleanup.returncode != 0:
            record(False, f"RESTORATION FAILURE: could not delete probe pod {POD_NAME}: {cleanup.stderr.strip()}")

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} RBAC checks passed")
    if failures or not overall_ok:
        print("FAIL: RBAC check did not fully pass", file=sys.stderr)
        return 1
    print("PASS: maops-diagnostics RBAC scope verified (allowed reads + denied writes/cross-namespace/cluster-wide)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

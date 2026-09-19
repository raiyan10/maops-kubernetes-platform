#!/usr/bin/env python3
"""
DAY5: proves the maops-platform NetworkPolicy default-deny + explicit-
allow behavior against the LIVE cluster, using real in-cluster TCP
connection attempts from actual Pods - never only a port-forward from
the host (a port-forward reaches a Service/Pod from OUTSIDE the
cluster network entirely and cannot observe pod-to-pod policy
enforcement at all) and never a static read of the NetworkPolicy
objects themselves (that's scripts/validate_manifests.py's job).

Two kinds of source Pod are used:
  - The REAL gateway/app Pods already running as part of the normal
    Deployment/StatefulSet - their labels are exactly what the
    NetworkPolicy selectors already key on, so no synthetic Pod is
    needed to test gateway -> app or app -> state.
  - ONE short-lived, uniquely-named probe Pod created in
    maops-day5-validation, labeled app.kubernetes.io/component:
    validation-client - the identity the gateway ingress-allow policy
    is scoped to. Exec'd into multiple times (gateway/app/state
    targets) rather than recreated per target, then deleted in a
    guaranteed `finally` block.

Every connection attempt is a real `http.client` request with a short,
explicit timeout (a policy-blocked connection is typically dropped
silently by the CNI dataplane, not actively refused, so the ONLY
reliable signal is a bounded client-side timeout/connection error -
never a bare "no response" without a timeout to bound how long the
check waits).

Proves:
  - validation-client -> gateway: ALLOWED (real HTTP 200 on /livez).
  - validation-client -> app: DENIED (connection blocked).
  - validation-client -> state: DENIED (connection blocked).
  - gateway -> app: ALLOWED (real HTTP 200 on /livez) - the existing
    Day 2-4 behavior must still work under NetworkPolicy enforcement.
  - gateway -> state: DENIED (connection blocked) - gateway must never
    reach state directly.
  - app -> state: ALLOWED (real HTTP 200 on /livez).
  - DNS resolution still works for gateway and app (the explicit DNS
    egress allow) - a real `socket.getaddrinfo()` call, same technique
    as scripts/discovery_check.py.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from cluster_check import get_pods
from kube import (
    APP_LABEL_SELECTOR,
    CONTEXT,
    GATEWAY_LABEL_SELECTOR,
    NAMESPACE,
    VALIDATION_NAMESPACE,
)

POD_DEADLINE_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 4
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"

VALIDATION_POD_NAME = f"netpol-check-{uuid.uuid4().hex[:10]}"

GATEWAY_FQDN = f"maops-gateway.{NAMESPACE}.svc.cluster.local"
APP_FQDN = f"maops-app.{NAMESPACE}.svc.cluster.local"
STATE_FQDN = f"maops-state.{NAMESPACE}.svc.cluster.local"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _connect_probe_snippet(host: str, port: int, path: str, timeout: int) -> str:
    # Plain string substitution (never .format()/f-string on this
    # template) so the generated probe's own Python syntax - including
    # any braces in library calls - never collides with template
    # placeholder syntax.
    template = (
        "import http.client\n"
        'conn = http.client.HTTPConnection("__HOST__", __PORT__, timeout=__TIMEOUT__)\n'
        "try:\n"
        '    conn.request("GET", "__PATH__")\n'
        "    resp = conn.getresponse()\n"
        "    resp.read()\n"
        '    print("RESULT=ok STATUS=" + str(resp.status))\n'
        "except Exception as e:\n"
        '    print("RESULT=blocked ERROR=" + type(e).__name__ + ":" + str(e))\n'
        "finally:\n"
        "    conn.close()\n"
    )
    return (
        template.replace("__HOST__", host)
        .replace("__PORT__", str(port))
        .replace("__PATH__", path)
        .replace("__TIMEOUT__", str(timeout))
    )


def _dns_probe_snippet(host: str) -> str:
    template = (
        "import socket\n"
        "try:\n"
        '    infos = socket.getaddrinfo("__HOST__", 8080)\n'
        "    addresses = sorted({info[4][0] for info in infos})\n"
        '    print("RESULT=ok COUNT=" + str(len(addresses)))\n'
        "except Exception as e:\n"
        '    print("RESULT=blocked ERROR=" + type(e).__name__ + ":" + str(e))\n'
    )
    return template.replace("__HOST__", host)


def _exec_in_pod(namespace: str, pod_name: str, *cmd: str) -> str:
    # Deliberately NOT cluster_check.exec_in_pod(), which hardcodes
    # `-n maops-platform` - the validation-client probe pod below lives
    # in maops-day5-validation, so every exec call in this script takes
    # its namespace explicitly rather than assuming the application
    # namespace.
    result = kube.run("-n", namespace, "exec", pod_name, "--", *cmd)
    return result.stdout.strip()


def _run_probe(namespace: str, pod_name: str, snippet: str) -> tuple[bool, str]:
    """Execs `snippet` in `pod_name` (in `namespace`) and parses its
    RESULT=ok/blocked line. Returns (connected, detail) - `connected` is
    True only for an explicit RESULT=ok with a 2xx-range STATUS; any
    exception, a non-2xx status, or a kubectl exec failure is treated as
    NOT connected (never silently assumed either way)."""
    try:
        output = _exec_in_pod(namespace, pod_name, "/usr/bin/python3.11", "-c", snippet)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return False, f"kubectl exec failed: {stderr if stderr else exc}"
    except subprocess.TimeoutExpired as exc:
        return False, f"kubectl exec timed out: {exc}"
    line = output.strip().splitlines()[-1] if output.strip() else ""
    if line.startswith("RESULT=ok"):
        # An HTTP connect probe's success line carries STATUS=<code> -
        # only a genuine 2xx counts as "connected", never any HTTP
        # response (a policy could theoretically let the TCP connection
        # through to a DIFFERENT, unintended backend that answers with
        # an error). A DNS probe's success line carries COUNT= instead
        # (no HTTP status exists for a raw resolution) and is accepted
        # on RESULT=ok alone.
        if "STATUS=" in line:
            status_token = line.split("STATUS=", 1)[-1].strip()
            try:
                status = int(status_token)
            except ValueError:
                return False, f"RESULT=ok but STATUS unparseable: {line!r}"
            return 200 <= status < 300, line
        return True, line
    if line.startswith("RESULT=blocked"):
        return False, line
    return False, f"unrecognized probe output: {output!r}"


def _validation_pod_manifest() -> str:
    # A long-lived (bounded by activeDeadlineSeconds), idle probe Pod -
    # exec'd into multiple times rather than recreated per target.
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {VALIDATION_POD_NAME}
  namespace: {VALIDATION_NAMESPACE}
  labels:
    app.kubernetes.io/component: validation-client
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 120
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
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: {PROBE_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["/usr/bin/python3.11"]
      args: ["-c", "import time; time.sleep(110)"]
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


def _wait_running(namespace: str, pod_name: str) -> dict:
    def _running():
        result = kube.run("-n", namespace, "get", "pod", pod_name, "-o", "json", check=False)
        if result.returncode != 0:
            return None
        import json as _json

        pod = _json.loads(result.stdout)
        phase = pod.get("status", {}).get("phase")
        if phase == "Running":
            return pod
        if phase in ("Failed", "Succeeded"):
            return pod  # terminal but not "Running" - let the caller decide
        return None

    return kube.wait_until(_running, timeout=POD_DEADLINE_SECONDS, description=f"pod {pod_name} Running")


def main() -> int:
    print(f"# Day 5 NetworkPolicy check: default-deny + explicit allows (context {CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    overall_ok = True

    # -- gateway -> app (allow), gateway -> state (deny), gateway DNS --
    gateway_pods = get_pods(GATEWAY_LABEL_SELECTOR)
    if not gateway_pods:
        overall_ok = record(False, "gateway -> app/state checks: no gateway pods available to exec into")
    else:
        gw_pod = gateway_pods[0]["metadata"]["name"]
        ok, detail = _run_probe(
            NAMESPACE, gw_pod, _connect_probe_snippet("maops-app", 8080, "/livez", CONNECT_TIMEOUT_SECONDS)
        )
        overall_ok = record(ok, f"gateway -> app ALLOWED: {detail}") and overall_ok
        ok, detail = _run_probe(
            NAMESPACE, gw_pod, _connect_probe_snippet("maops-state", 8080, "/livez", CONNECT_TIMEOUT_SECONDS)
        )
        overall_ok = record(not ok, f"gateway -> state DENIED: {detail}") and overall_ok
        ok, detail = _run_probe(NAMESPACE, gw_pod, _dns_probe_snippet("maops-app"))
        overall_ok = record(ok, f"gateway DNS resolution still works: {detail}") and overall_ok

    # -- app -> state (allow), app DNS --
    app_pods = get_pods(APP_LABEL_SELECTOR)
    if not app_pods:
        overall_ok = record(False, "app -> state check: no app pods available to exec into")
    else:
        app_pod = app_pods[0]["metadata"]["name"]
        ok, detail = _run_probe(
            NAMESPACE, app_pod, _connect_probe_snippet("maops-state", 8080, "/livez", CONNECT_TIMEOUT_SECONDS)
        )
        overall_ok = record(ok, f"app -> state ALLOWED: {detail}") and overall_ok
        ok, detail = _run_probe(NAMESPACE, app_pod, _dns_probe_snippet("maops-state"))
        overall_ok = record(ok, f"app DNS resolution still works: {detail}") and overall_ok

    # -- validation-client -> gateway (allow), -> app (deny), -> state (deny) --
    try:
        _apply(_validation_pod_manifest())
        pod = _wait_running(VALIDATION_NAMESPACE, VALIDATION_POD_NAME)
        phase = pod.get("status", {}).get("phase")
        if phase != "Running":
            overall_ok = record(False, f"validation-client probe pod never reached Running (phase={phase!r})") and overall_ok
        else:
            ok, detail = _run_probe(
                VALIDATION_NAMESPACE,
                VALIDATION_POD_NAME,
                _connect_probe_snippet(GATEWAY_FQDN, 8080, "/livez", CONNECT_TIMEOUT_SECONDS),
            )
            overall_ok = record(ok, f"validation-client -> gateway ALLOWED: {detail}") and overall_ok
            ok, detail = _run_probe(
                VALIDATION_NAMESPACE,
                VALIDATION_POD_NAME,
                _connect_probe_snippet(APP_FQDN, 8080, "/livez", CONNECT_TIMEOUT_SECONDS),
            )
            overall_ok = record(not ok, f"validation-client -> app DENIED: {detail}") and overall_ok
            ok, detail = _run_probe(
                VALIDATION_NAMESPACE,
                VALIDATION_POD_NAME,
                _connect_probe_snippet(STATE_FQDN, 8080, "/livez", CONNECT_TIMEOUT_SECONDS),
            )
            overall_ok = record(not ok, f"validation-client -> state DENIED: {detail}") and overall_ok
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError) as exc:
        overall_ok = record(False, f"validation-client probe raised an unexpected error: {exc}")
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
                VALIDATION_POD_NAME,
                "--ignore-not-found",
                "--wait=false",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if cleanup.returncode != 0:
            record(False, f"RESTORATION FAILURE: could not delete probe pod {VALIDATION_POD_NAME}: {cleanup.stderr.strip()}")

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} NetworkPolicy checks passed")
    if failures or not overall_ok:
        print("FAIL: NetworkPolicy check did not fully pass", file=sys.stderr)
        return 1
    print("PASS: default-deny + explicit-allow NetworkPolicy behavior verified end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY6 (post-restart remediation): read-only, per-Pod check that every
deployed application Pod in maops-platform carries the expected ambient
metadata AND has the expected ztunnel in-Pod listeners in its own
network namespace.

Why this exists (2026-09-25 incident, see docs/architecture.md): after a
WSL/Kind component restart, `mesh-status` (infrastructure only) passed,
yet `maops-state-0` was Kubernetes Ready with NO LISTEN sockets on
15001/15006/15008 in its network namespace, and one gateway Pod was
unready and likewise had none. Readiness and the
`ambient.istio.io/redirection` annotation did not reveal this; the
missing listeners did. This check looks for exactly that, per Pod,
before the lengthy rollout validation.

For each expected workload Pod - gateway (3), app (3), state (1) - it
verifies:
  1. Pod metadata/status: the Pod exists, is not terminating, is
     Running, and has a Pod IP;
  2. identity metadata: `spec.serviceAccountName` is the workload's own
     ServiceAccount;
  3. enrollment metadata: the namespace carries
     `istio.io/dataplane-mode=ambient`, the Pod does not opt out, the
     Pod carries `ambient.istio.io/redirection: enabled`, and it has no
     `istio-proxy` sidecar;
  4. listeners: TCP 15001, 15006, and 15008 are LISTEN sockets inside
     that Pod's own network namespace, read from `/proc/net/tcp` and
     `/proc/net/tcp6` via `kubectl exec` with the workload image's own
     interpreter - no extra image, tool, or dependency.

Scope - what this does NOT prove: that traffic is actually redirected
into those listeners (the iptables/nftables redirection rules), that
HBONE/mTLS connections succeed, or that AuthorizationPolicy allows or
denies the right identities. It checks sockets and metadata only. Those
traffic and policy behaviors are proven by the existing live traffic
tests - `make mesh-check` (mTLS, allowed identity paths, correlated
wrong-identity denial), `make networkpolicy-check`, `make
gateway-check`, and `make smoke` - which this check complements and
never replaces.

Fail-closed rules: a missing or extra Pod, a kubectl/API error, a
timeout, or malformed probe output is a FAILURE, never a skip. The Pod's
Ready condition is printed for context only and never satisfies any
check; neither does the redirection annotation on its own - check 4 is
always required. The in-Pod probe emits only port numbers and a
result marker; the findings also show Pod names, Pod IPs,
ServiceAccount names, Pod phase, enrollment label/annotation values,
and kubectl error text on failure. No environment variable, Secret
volume, or token content is ever read.

Read-only: `kubectl get` and a read-only `kubectl exec` only. Never
mutates, restarts, or recreates anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube

REQUIRED_LISTEN_PORTS = (15001, 15006, 15008)
REDIRECTION_ANNOTATION = "ambient.istio.io/redirection"
REDIRECTION_ENABLED = "enabled"
SIDECAR_CONTAINER_NAME = "istio-proxy"
PYTHON = "/usr/bin/python3.11"
KUBECTL_TIMEOUT_SECONDS = 20.0

# Runs INSIDE the workload container. /proc/net/tcp{,6} are per network
# namespace, so they list the Pod's own sockets, including ztunnel's
# in-Pod listeners. State 0A is TCP_LISTEN. Emits only port numbers and
# a result marker.
LISTEN_PROBE_SNIPPET = r"""
import sys
ports = set()
read_any = False
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(path) as f:
            lines = f.read().splitlines()[1:]
    except FileNotFoundError:
        continue
    except OSError as exc:
        print("MAOPS_RESULT=ERROR:" + type(exc).__name__)
        sys.exit(3)
    read_any = True
    for line in lines:
        fields = line.split()
        if len(fields) > 3 and fields[3] == "0A":
            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
if not read_any:
    print("MAOPS_RESULT=ERROR:no_proc_net_tcp")
    sys.exit(3)
print("MAOPS_LISTEN_PORTS=" + ",".join(str(p) for p in sorted(ports)))
print("MAOPS_RESULT=OK")
"""


@dataclass(frozen=True)
class Workload:
    component: str
    label_selector: str
    service_account: str
    container: str
    expected_pods: int


WORKLOADS = (
    Workload("gateway", kube.GATEWAY_LABEL_SELECTOR, kube.GATEWAY_SERVICE_ACCOUNT, "maops-gateway", 3),
    Workload("app", kube.APP_LABEL_SELECTOR, kube.APP_SERVICE_ACCOUNT, "maops-app", 3),
    Workload("state", kube.STATE_LABEL_SELECTOR, kube.STATE_SERVICE_ACCOUNT, "maops-state", 1),
)

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _detail(exc: BaseException) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return stderr.strip() if isinstance(stderr, str) and stderr.strip() else str(exc)


def _get_json(*args: str) -> dict:
    """kubectl get -o json; raises RuntimeError on any API/timeout/parse
    failure so callers record a single fail-closed finding."""
    try:
        result = kube.run(*args, "-o", "json", timeout=KUBECTL_TIMEOUT_SECONDS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {_detail(exc)}") from exc
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"kubectl {' '.join(args)} returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"kubectl {' '.join(args)} returned a non-object JSON document")
    return data


def parse_listen_output(output: str) -> set[int]:
    """Parses LISTEN_PROBE_SNIPPET output strictly. Raises ValueError
    unless the nonblank lines are exactly one `MAOPS_LISTEN_PORTS=` line
    and one `MAOPS_RESULT=OK` line - any other line, a duplicate marker,
    a malformed or out-of-range port, or a duplicate port is rejected.
    An empty port list is valid (the caller then reports every required
    port missing)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    port_lines = [line for line in lines if line.startswith("MAOPS_LISTEN_PORTS=")]
    result_lines = [line for line in lines if line.startswith("MAOPS_RESULT=")]
    unexpected = [line for line in lines if line not in port_lines and line not in result_lines]
    if unexpected:
        raise ValueError(f"unexpected probe output line(s): {unexpected}")
    if len(result_lines) != 1:
        raise ValueError(f"expected exactly one MAOPS_RESULT line, found {len(result_lines)}")
    if result_lines[0] != "MAOPS_RESULT=OK":
        raise ValueError(f"probe did not report success: {result_lines[0]}")
    if len(port_lines) != 1:
        raise ValueError(f"expected exactly one MAOPS_LISTEN_PORTS line, found {len(port_lines)}")
    raw = port_lines[0].split("=", 1)[1]
    if raw == "":
        return set()
    ports: set[int] = set()
    for token in raw.split(","):
        if not token.isdigit():
            raise ValueError(f"malformed port token {token!r}")
        port = int(token)
        if not 1 <= port <= 65535:
            raise ValueError(f"port out of range: {port}")
        if port in ports:
            raise ValueError(f"duplicate port {port}")
        ports.add(port)
    return ports


def probe_listen_ports(pod_name: str, container: str) -> set[int]:
    """Read-only exec into the Pod's own network namespace. Raises
    RuntimeError on exec/API failure, timeout, or malformed output."""
    try:
        result = kube.run(
            "-n", kube.NAMESPACE, "exec", pod_name, "-c", container, "--",
            PYTHON, "-c", LISTEN_PROBE_SNIPPET,
            timeout=KUBECTL_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"kubectl exec failed: {_detail(exc)}") from exc
    try:
        return parse_listen_output(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"malformed listener probe output: {exc}") from exc


def _is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def check_namespace_enrollment() -> bool:
    try:
        namespace = _get_json("get", "namespace", kube.NAMESPACE)
    except RuntimeError as exc:
        return record(False, f"namespace {kube.NAMESPACE!r}: {exc}")
    labels = namespace.get("metadata", {}).get("labels") or {}
    mode = labels.get(kube.AMBIENT_DATAPLANE_MODE_LABEL)
    return record(
        mode == kube.AMBIENT_DATAPLANE_MODE_VALUE,
        f"namespace {kube.NAMESPACE!r}: {kube.AMBIENT_DATAPLANE_MODE_LABEL}={mode!r} "
        f"(expected {kube.AMBIENT_DATAPLANE_MODE_VALUE!r})",
    )


def check_pod(workload: Workload, pod: dict) -> bool:
    """Every per-Pod condition is recorded; returns True only if all pass."""
    name = pod.get("metadata", {}).get("name", "<unnamed>")
    label = f"{workload.component} pod {name}"
    ok = True

    status = pod.get("status", {})
    spec = pod.get("spec", {})
    metadata = pod.get("metadata", {})
    print(f"  ({label}: Kubernetes Ready={_is_ready(pod)} - informational only, never sufficient)")

    ok = record(metadata.get("deletionTimestamp") is None, f"{label}: not terminating") and ok
    ok = record(status.get("phase") == "Running", f"{label}: phase={status.get('phase')!r} (expected 'Running')") and ok
    ok = record(bool(status.get("podIP")), f"{label}: has a Pod IP ({status.get('podIP')!r})") and ok

    sa = spec.get("serviceAccountName")
    ok = record(sa == workload.service_account, f"{label}: serviceAccountName={sa!r} (expected {workload.service_account!r})") and ok

    pod_mode = (metadata.get("labels") or {}).get(kube.AMBIENT_DATAPLANE_MODE_LABEL)
    ok = record(pod_mode in (None, kube.AMBIENT_DATAPLANE_MODE_VALUE), f"{label}: does not opt out of ambient (pod {kube.AMBIENT_DATAPLANE_MODE_LABEL}={pod_mode!r})") and ok

    redirection = (metadata.get("annotations") or {}).get(REDIRECTION_ANNOTATION)
    ok = record(redirection == REDIRECTION_ENABLED, f"{label}: {REDIRECTION_ANNOTATION}={redirection!r} (expected {REDIRECTION_ENABLED!r}; necessary, not sufficient)") and ok

    containers = {c.get("name") for c in spec.get("containers") or []}
    ok = record(SIDECAR_CONTAINER_NAME not in containers, f"{label}: no {SIDECAR_CONTAINER_NAME!r} sidecar container") and ok
    ok = record(workload.container in containers, f"{label}: has container {workload.container!r}") and ok

    required = ", ".join(str(p) for p in REQUIRED_LISTEN_PORTS)
    if workload.container not in containers:
        record(False, f"{label}: cannot probe listeners {required} - container {workload.container!r} missing")
        return False
    try:
        listening = probe_listen_ports(name, workload.container)
    except RuntimeError as exc:
        record(False, f"{label}: ambient listener probe for {required} failed closed: {exc}")
        return False
    missing = [p for p in REQUIRED_LISTEN_PORTS if p not in listening]
    ok = record(
        not missing,
        f"{label}: ztunnel in-Pod listeners {required} present"
        if not missing
        else f"{label}: MISSING ztunnel in-Pod LISTEN sockets on port(s) {', '.join(str(p) for p in missing)}",
    ) and ok
    return ok


def check_workload(workload: Workload) -> bool:
    try:
        pods = _get_json("-n", kube.NAMESPACE, "get", "pods", "-l", workload.label_selector).get("items")
    except RuntimeError as exc:
        return record(False, f"{workload.component}: could not list Pods: {exc}")
    if not isinstance(pods, list):
        return record(False, f"{workload.component}: Pod list response had no 'items' list")
    count_ok = record(
        len(pods) == workload.expected_pods,
        f"{workload.component}: {len(pods)} Pod(s) found (expected exactly {workload.expected_pods})",
    )
    pods_ok = True
    for pod in sorted(pods, key=lambda p: p.get("metadata", {}).get("name", "")):
        pods_ok = check_pod(workload, pod) and pods_ok
    return count_ok and pods_ok


def main() -> int:
    results.clear()
    print(f"# Day 6 ambient workload check: per-Pod ztunnel listeners (context {kube.CONTEXT}, namespace {kube.NAMESPACE})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    overall_ok = check_namespace_enrollment()
    for workload in WORKLOADS:
        overall_ok = check_workload(workload) and overall_ok

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} ambient workload checks passed")
    if failures or not overall_ok:
        print(f"FAIL: {len(failures)} ambient workload check(s) failed", file=sys.stderr)
        return 1
    print(
        "PASS: every gateway/app/state Pod has the expected ambient metadata and ztunnel in-Pod listeners "
        "on 15001/15006/15008 (traffic, mTLS, and AuthorizationPolicy behavior are proven by mesh-check, not here)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

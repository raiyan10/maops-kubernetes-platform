#!/usr/bin/env python3
"""
DAY5, adapted for DAY6 (three times remediated after independent
review/live findings): proves the maops-platform NetworkPolicy
default-deny + explicit-allow behavior against the LIVE cluster, using
real in-cluster TCP connection attempts from actual Pods - never only a
port-forward from the host (a port-forward reaches a Service/Pod from
OUTSIDE the cluster network entirely and cannot observe pod-to-pod
policy enforcement at all) and never a static read of the NetworkPolicy
objects themselves (that's scripts/validate_manifests.py's/
scripts/helm_check.py's job for the frozen k8s/base source and the Day
6 Helm chart respectively).

DAY6 topology change from Day 5: the Day 5 chart carried a
`validation-client -> gateway` ALLOW specifically so a validation probe
Pod could reach gateway directly, standing in for "the one path into
maops-platform from outside it" before Day 6 introduced a real
cluster-external path. Day 6 supersedes that path with the Istio
ingress Gateway (`maops-edge` in `maops-ingress`, proven live by
`scripts/gateway_check.py`) - and deliberately does NOT carry the old
validation-client shortcut forward into the Day 6 Helm chart's
NetworkPolicy templates (see charts/maops-kubernetes-platform and
docs/architecture.md). This script matches: `validation-client ->
gateway` is asserted DENIED, same as `-> app`/`-> state` always were -
there is no longer any NetworkPolicy-level path into maops-platform
from maops-day6-validation at all. `maops-day6-validation` is
deliberately NOT ambient-enrolled (see k8s/day6/validation-namespace.yaml),
so the validation-client probe Pod's raw TCP results genuinely isolate
Cilium's NetworkPolicy enforcement, unaffected by anything ambient - no
change needed there.

DAY6 SECOND REMEDIATION - phase-separated classification (replaces the
first remediation's coarse CONNECTED/BLOCKED/INCONCLUSIVE model, which
still wrapped TCP connect, HTTP request, response-header read, and body
read into ONE undifferentiated try/except - so an HTTP-layer read
timeout AFTER a successful TCP handshake could still be misread as a
"BLOCKED" (NetworkPolicy-denied) outcome, which it is not). Every probe
now resolves to exactly one of SEVEN distinct, phase-attributed states:

  - TCP_CONNECT_TIMEOUT - the initial `socket.connect()` call itself
    timed out within the bounded client-side timeout - the actual,
    specific signature of a CNI silently dropping the SYN, which is how
    Cilium's NetworkPolicy denial manifests on the wire. This is the
    ONLY state that may ever satisfy an expected DENIED assertion.
  - TCP_CONNECTED - the TCP handshake completed. Produced by the
    purpose-built TCP-connect-only probe used for NetworkPolicy
    reachability checks (see `_tcp_connect_probe_snippet` below) -
    proves connectivity without attempting any HTTP transaction at all.
  - HTTP_RESPONSE - a full HTTP response (status line + body) was
    received, AFTER TCP connect already succeeded. Produced by the
    separate HTTP health probe used for positive-path APPLICATION
    validation (`_http_probe_snippet`) - ANY status code counts,
    regardless of 2xx/4xx/5xx, since a completed response is proof of
    connectivity and application-level reachability, never a
    NetworkPolicy signal.
  - HTTP_READ_TIMEOUT - TCP connect succeeded, but reading the HTTP
    response (request send, header read, or body read) timed out. This
    is a DIFFERENT phase than TCP_CONNECT_TIMEOUT and must NEVER be
    treated as a NetworkPolicy block - the connection was already
    established, so whatever CNI-level enforcement exists already let
    the packet through. It is reported as its own distinct outcome
    (most plausibly an application-level condition - a slow/hung
    backend - never denial evidence) and satisfies neither
    `assert_denied()` nor `assert_connected()`.
  - CONNECTION_REFUSED_OR_RESET - the connection was actively refused
    or reset (`ConnectionRefusedError`/`ConnectionResetError`/other
    `OSError`), at either the connect or the HTTP phase. A DIFFERENT,
    non-NetworkPolicy-shaped symptom than a silent timeout - most
    likely nothing is listening, or an intermediate device actively
    rejected the connection, neither of which is how Cilium's
    NetworkPolicy denial actually behaves (a silent drop, not an active
    RST). Never treated as denial proof.
  - EXEC_INCONCLUSIVE - the `kubectl exec` invocation itself failed or
    timed out - the probe never even ran. Never treated as proof of
    anything about the network path.
  - OUTPUT_INCONCLUSIVE - `kubectl exec` completed, but the probe's own
    stdout was missing, unparseable, or didn't match any recognized
    `RESULT=` state (or an `HTTP_RESPONSE` line whose `STATUS=` token
    wasn't itself parseable). Also never treated as proof of anything.

`assert_denied()` accepts ONLY `TCP_CONNECT_TIMEOUT`. `assert_connected()`
accepts `TCP_CONNECTED`, `HTTP_RESPONSE`, or `DNS_RESOLVED` (the DNS
probe's own success state - a real `socket.getaddrinfo()` resolution,
not a TCP/HTTP concern, but conceptually "connectivity proven" for this
script's purposes). Every other state fails BOTH assertions - an
inconclusive or application-level-timeout probe can never be silently
read as proof of either ALLOWED or DENIED. THIS REMAINS UNCHANGED BY
THE THIRD REMEDIATION BELOW: for an isolated, non-ambient raw-TCP
NetworkPolicy test, only a genuine bounded TCP connect timeout may ever
satisfy a denied assertion.

DAY6 THIRD REMEDIATION (live-discovered) - ambient TCP-connect is not a
valid NetworkPolicy signal for a REAL ambient-enrolled Pod:

A live `make networkpolicy-check` run reported `gateway -> state
DENIED` as `TCP_CONNECTED` (a hard failure under `assert_denied()`) -
not because the NetworkPolicy stopped enforcing the denial, but because
the PREVIOUS revision of this script ran that assertion FROM THE REAL,
ambient-enrolled `maops-gateway` Pod. In ambient mode, a source Pod's
outbound `connect()` is transparently redirected to its own NODE-LOCAL
ztunnel (this is exactly the same fundamental distinction already
corrected in `scripts/mesh_check.py`'s fifth remediation) - so a
completed TCP handshake from a real ambient Pod proves only that the
LOCAL ztunnel accepted the client socket, never that the destination
was actually reached or that Cilium's NetworkPolicy allowed the raw TCP
path through in the way this script's classification model assumes.
Using ambient Pods for THIS script's raw-TCP NetworkPolicy assertions
was therefore never a valid test to begin with, for both the ALLOWED
and DENIED directions.

Fix: the `gateway -> app` (ALLOWED), `gateway -> state` (DENIED), and
`app -> state` (ALLOWED) application-port assertions now run from
FOUR dedicated, temporary, NON-ambient probe Pods created directly in
`maops-platform` (`check_application_port_networkpolicy_isolated()`),
each opted OUT of the namespace's `istio.io/dataplane-mode: ambient`
label via a POD-level `istio.io/dataplane-mode: none` override (Istio's
own documented per-Pod ambient opt-out mechanism) - so their raw
`connect()` calls are genuinely unredirected, isolating Cilium's
NetworkPolicy enforcement exactly the way `validation-client`'s
non-ambient probe already did. Each probe Pod carries ONLY the single
`app.kubernetes.io/component` label the applicable NetworkPolicy
selector actually keys on (never the complete live Deployment/
StatefulSet/Service label set, which would risk accidentally matching
unrelated selectors or Service endpoint selection) plus one unique,
run-scoped discovery/cleanup label (`NETPOL_PROBE_RUN_LABEL`). Source
Pods stay alive (idle sleep) for `kubectl exec`; target Pods run a
small Python TCP listener on port 8080. Assertions connect directly to
each target Pod's own `status.podIP` - never a Service - so the probe
can never alter or depend on live Service endpoint selection. Before
any assertion runs, every probe Pod is independently verified: Running,
carries `istio.io/dataplane-mode: none`, carries NO
`ambient.istio.io/redirection` annotation, has no `istio-proxy` sidecar
container, has no `ownerReferences` (never adopted by a controller),
and its Pod IP does not appear in any live application Service
EndpointSlice. Every probe Pod is deleted, and its absence verified
(never merely `--wait=false`), in a guaranteed `finally` - a cleanup
failure fails this check even if every connectivity assertion passed.

Responsibility boundary, restated: `networkpolicy_check.py` verifies
Cilium/Kubernetes NetworkPolicy using isolated, non-ambient plaintext
probes only; `scripts/mesh_check.py` verifies ambient mTLS and
identity-scoped Istio AuthorizationPolicy. Neither script claims to be
able to attribute a REAL ambient-Pod's combined-path result to the
other layer specifically - that combined-path ambiguity is exactly why
this script no longer uses real ambient Pods for its own raw-TCP
assertions at all.

DAY6 THIRD REMEDIATION also fixed a deadline-boundary race in Pod
cleanup verification: a live run reported a probe Pod's deletion as
"did not disappear within 30 seconds", while the very next read (for
failure diagnostics) already showed it gone - the Pod disappeared right
around the polling deadline. `delete_pod_and_verify_gone()`'s bounded
poll window is widened (30s -> 75s) and, if the normal polling loop
never observes an explicit `NOT_FOUND`, performs ONE final fresh
tri-state read after the deadline before deciding failure - passing
only on an explicit `NOT_FOUND` there too, still failing immediately on
`API_ERROR` at any point, and never using forced deletion to make a
check artificially pass.

Two purpose-built probe snippets, unchanged from the second remediation:
  - `_tcp_connect_probe_snippet()` - a raw `socket.connect()` attempt,
    nothing else. Used for EVERY NetworkPolicy reachability check in
    this script (both the ALLOWED assertions - the isolated gateway ->
    app, app -> state probes - and the DENIED assertions), since the
    only question those checks need answered is "did the CNI let this
    TCP handshake through", not "did the application actually respond
    correctly".
  - `_http_probe_snippet()` - a phase-separated HTTP GET, kept here as
    the shared, tested building block for callers that need real
    application-level health evidence (not used by this script's own
    NetworkPolicy reachability assertions directly).

Kinds of Pod used:
  - The REAL gateway/app Pods, used ONLY for their DNS checks now (a
    real `socket.getaddrinfo()` call - UDP name resolution, never
    redirected by ztunnel's TCP-focused interception, so this remains a
    semantically valid check against the real ambient Pods; unaffected
    by the third remediation above).
  - FOUR temporary, non-ambient, isolated probe Pods
    (`check_application_port_networkpolicy_isolated()`) - two sources,
    two targets - created directly in `maops-platform`, per the third
    remediation above.
  - ONE short-lived, uniquely-named validation-client probe Pod created
    in `maops-day6-validation` (never ambient-enrolled - see above),
    labeled `app.kubernetes.io/component: validation-client`. Exec'd
    into multiple times (gateway/app/state targets) rather than
    recreated per target, then deleted in a guaranteed, VERIFIED
    cleanup path.

Proves:
  - isolated (non-ambient) gateway-probe -> app-probe: TCP_CONNECTED -
    the application-port ALLOW rule genuinely lets this TCP handshake
    through.
  - isolated (non-ambient) gateway-probe -> state-probe:
    TCP_CONNECT_TIMEOUT - gateway must never reach state directly.
  - isolated (non-ambient) app-probe -> state-probe: TCP_CONNECTED.
  - validation-client -> gateway: TCP_CONNECT_TIMEOUT.
  - validation-client -> app: TCP_CONNECT_TIMEOUT.
  - validation-client -> state: TCP_CONNECT_TIMEOUT.
  - DNS resolution still works for the real gateway and app Pods (the
    explicit DNS egress allow) - a real `socket.getaddrinfo()` call,
    same technique as scripts/discovery_check.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from cluster_check import get_pods
from kube import (
    APP_LABEL_SELECTOR,
    APP_SERVICE,
    CONTEXT,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_SERVICE,
    NAMESPACE,
    STATE_SERVICE,
    VALIDATION_NAMESPACE,
)

POD_DEADLINE_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 4
READ_TIMEOUT_SECONDS = 4
# DAY6 third remediation (live-discovered boundary race): widened from
# 30s to comfortably clear the observed deadline race, with the final-
# boundary-read fix in delete_pod_and_verify_gone() as the real fix -
# see module docstring.
DELETE_VERIFY_TIMEOUT_SECONDS = 75.0
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"

VALIDATION_POD_NAME = f"netpol-check-{uuid.uuid4().hex[:10]}"

GATEWAY_FQDN = f"maops-gateway.{NAMESPACE}.svc.cluster.local"
APP_FQDN = f"maops-app.{NAMESPACE}.svc.cluster.local"
STATE_FQDN = f"maops-state.{NAMESPACE}.svc.cluster.local"

# DAY6 third remediation: the four isolated, non-ambient application-
# port probe Pods - see module docstring for the full design.
_NETPOL_PROBE_RUN_ID = uuid.uuid4().hex[:10]
# A single, unprefixed discovery/cleanup label key, present (with a
# per-run unique value) on every probe Pod this script creates directly
# in maops-platform - `final_state_check.py`'s
# `check_no_leaked_networkpolicy_probe_pods()` matches on this KEY
# (existence, any value) as its whole-suite-level leak backstop.
NETPOL_PROBE_RUN_LABEL = "netpol-probe-run"
GATEWAY_SOURCE_POD_NAME = f"netpol-src-gw-{_NETPOL_PROBE_RUN_ID}"
APP_SOURCE_POD_NAME = f"netpol-src-app-{_NETPOL_PROBE_RUN_ID}"
APP_TARGET_POD_NAME = f"netpol-tgt-app-{_NETPOL_PROBE_RUN_ID}"
STATE_TARGET_POD_NAME = f"netpol-tgt-state-{_NETPOL_PROBE_RUN_ID}"
TARGET_LISTEN_PORT = 8080

# Istio's documented per-Pod ambient opt-out: a Pod carrying this exact
# label/value inside an otherwise ambient-enrolled namespace (maops-
# platform carries `istio.io/dataplane-mode: ambient` at the namespace
# level) is never redirected into the mesh - see
# k8s/day6/platform-namespace.yaml.
NONE_DATAPLANE_MODE_VALUE = "none"
# Same literal constants scripts/mesh_check.py already uses for the
# same purpose - deliberately re-declared here rather than imported
# from mesh_check, keeping this script's own responsibility boundary
# (Cilium/NetworkPolicy only) independent of mesh_check's (ambient
# mTLS/AuthorizationPolicy) - see module docstring.
AMBIENT_REDIRECTION_ANNOTATION = "ambient.istio.io/redirection"
SIDECAR_CONTAINER_NAME = "istio-proxy"

# DAY6 second remediation: the seven phase-attributed probe outcome
# states - see module docstring for the full rationale behind each.
TCP_CONNECT_TIMEOUT = "TCP_CONNECT_TIMEOUT"
TCP_CONNECTED = "TCP_CONNECTED"
HTTP_RESPONSE = "HTTP_RESPONSE"
HTTP_READ_TIMEOUT = "HTTP_READ_TIMEOUT"
CONNECTION_REFUSED_OR_RESET = "CONNECTION_REFUSED_OR_RESET"
EXEC_INCONCLUSIVE = "EXEC_INCONCLUSIVE"
OUTPUT_INCONCLUSIVE = "OUTPUT_INCONCLUSIVE"
# The DNS probe's own success/failure states - not a TCP/HTTP concern,
# kept distinct from the six states above.
DNS_RESOLVED = "DNS_RESOLVED"
DNS_FAILED = "DNS_FAILED"

_KNOWN_STATES = {
    TCP_CONNECT_TIMEOUT,
    TCP_CONNECTED,
    HTTP_RESPONSE,
    HTTP_READ_TIMEOUT,
    CONNECTION_REFUSED_OR_RESET,
    DNS_RESOLVED,
    DNS_FAILED,
}
# States that satisfy assert_connected() - genuine proof of
# connectivity, at either the TCP or HTTP layer, or a real DNS
# resolution.
_CONNECTED_STATES = {TCP_CONNECTED, HTTP_RESPONSE, DNS_RESOLVED}

results: list[tuple[bool, str]] = []


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    detail: str


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def assert_connected(result: ProbeResult, label: str) -> bool:
    """Records PASS only for a state in `_CONNECTED_STATES`
    (TCP_CONNECTED/HTTP_RESPONSE/DNS_RESOLVED). Every other state -
    including HTTP_READ_TIMEOUT, which DID complete a TCP handshake -
    fails this assertion; a caller that specifically wants to accept
    "TCP connected even if the HTTP layer then timed out" should check
    `result.outcome` directly rather than relying on this helper."""
    return record(result.outcome in _CONNECTED_STATES, f"{label} [{result.outcome}]: {result.detail}")


def assert_denied(result: ProbeResult, label: str) -> bool:
    """UNCHANGED by the third remediation (deliberately - see module
    docstring): records PASS only for TCP_CONNECT_TIMEOUT - the one
    state this script accepts as proof of a NetworkPolicy denial. Every
    other state fails this assertion, INCLUDING TCP_CONNECTED,
    HTTP_READ_TIMEOUT, and CONNECTION_REFUSED_OR_RESET (none of these
    is the silent-drop signature a NetworkPolicy block actually
    produces) and both INCONCLUSIVE states (a `kubectl exec` failure/
    timeout or unparseable output is never treated as evidence of
    denial, only as a failure of the check itself). For an isolated,
    non-ambient raw-TCP NetworkPolicy test (the only kind this function
    is ever called for), a genuine bounded connect timeout is the only
    valid signature - this function does not, and must not, weaken that
    to accommodate any ambient-mode observation nuance; the fix for
    ambient's TCP-connect-acceptance behavior is to stop calling this
    function from a real ambient Pod at all (see
    `check_application_port_networkpolicy_isolated()`), never to loosen
    what it accepts."""
    return record(result.outcome == TCP_CONNECT_TIMEOUT, f"{label} [{result.outcome}]: {result.detail}")


def _tcp_connect_probe_snippet(host: str, port: int, timeout: int) -> str:
    """Purpose-built, TCP-connect-ONLY probe - no HTTP request is ever
    attempted. This is what every NetworkPolicy reachability assertion
    in this script (both ALLOWED and DENIED) actually needs: whether
    the CNI let the TCP handshake through, nothing about application-
    level behavior. `socket.timeout` is an alias of the builtin
    `TimeoutError` as of Python 3.10 and is itself a subclass of
    `OSError` - the timeout except clause MUST come first (as it does
    here) so a genuine connect timeout is never swallowed by the
    broader `OSError` catch-all below it."""
    template = (
        "import socket\n"
        'sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n'
        "sock.settimeout(__TIMEOUT__)\n"
        "try:\n"
        '    sock.connect(("__HOST__", __PORT__))\n'
        '    print("RESULT=TCP_CONNECTED")\n'
        "except socket.timeout:\n"
        '    print("RESULT=TCP_CONNECT_TIMEOUT")\n'
        "except (ConnectionRefusedError, ConnectionResetError, OSError) as e:\n"
        '    print("RESULT=CONNECTION_REFUSED_OR_RESET ERROR=" + type(e).__name__ + ":" + str(e))\n'
        "finally:\n"
        "    sock.close()\n"
    )
    return template.replace("__HOST__", host).replace("__PORT__", str(port)).replace("__TIMEOUT__", str(timeout))


def _http_probe_snippet(host: str, port: int, path: str, connect_timeout: int, read_timeout: int) -> str:
    """Purpose-built HTTP health probe with genuinely SEPARATE try/
    except blocks for the connect phase and the request/read phase -
    each with its own timeout - so a read-phase timeout can never be
    misattributed to the connect phase (the defect the first
    remediation left uncorrected: wrapping both phases in one
    undifferentiated try/except around the whole `http.client`
    transaction). `conn.connect()` is called explicitly, separately
    from `conn.request()`/`conn.getresponse()`, specifically to create
    this phase boundary; `conn.sock.settimeout()` then re-arms the
    timeout for the read phase independently of the connect-phase
    timeout. Reserved for callers that need real application-level
    health evidence (a completed HTTP response) - this script's own
    NetworkPolicy reachability assertions use the simpler
    `_tcp_connect_probe_snippet()` instead, per this remediation's
    explicit design split."""
    template = (
        "import http.client, socket\n"
        'conn = http.client.HTTPConnection("__HOST__", __PORT__, timeout=__CONNECT_TIMEOUT__)\n'
        "try:\n"
        "    try:\n"
        "        conn.connect()\n"
        "    except socket.timeout:\n"
        '        print("RESULT=TCP_CONNECT_TIMEOUT")\n'
        "    except (ConnectionRefusedError, ConnectionResetError, OSError) as e:\n"
        '        print("RESULT=CONNECTION_REFUSED_OR_RESET ERROR=" + type(e).__name__ + ":" + str(e))\n'
        "    else:\n"
        "        try:\n"
        "            conn.sock.settimeout(__READ_TIMEOUT__)\n"
        '            conn.request("GET", "__PATH__")\n'
        "            resp = conn.getresponse()\n"
        "            resp.read()\n"
        '            print("RESULT=HTTP_RESPONSE STATUS=" + str(resp.status))\n'
        "        except socket.timeout:\n"
        '            print("RESULT=HTTP_READ_TIMEOUT")\n'
        "        except (ConnectionRefusedError, ConnectionResetError, OSError) as e:\n"
        '            print("RESULT=CONNECTION_REFUSED_OR_RESET ERROR=" + type(e).__name__ + ":" + str(e))\n'
        "finally:\n"
        "    conn.close()\n"
    )
    return (
        template.replace("__HOST__", host)
        .replace("__PORT__", str(port))
        .replace("__PATH__", path)
        .replace("__CONNECT_TIMEOUT__", str(connect_timeout))
        .replace("__READ_TIMEOUT__", str(read_timeout))
    )


def _dns_probe_snippet(host: str) -> str:
    template = (
        "import socket\n"
        "try:\n"
        '    infos = socket.getaddrinfo("__HOST__", 8080)\n'
        "    addresses = sorted({info[4][0] for info in infos})\n"
        '    print("RESULT=DNS_RESOLVED COUNT=" + str(len(addresses)))\n'
        "except Exception as e:\n"
        '    print("RESULT=DNS_FAILED ERROR=" + type(e).__name__ + ":" + str(e))\n'
    )
    return template.replace("__HOST__", host)


def _exec_in_pod(namespace: str, pod_name: str, *cmd: str) -> str:
    # Deliberately NOT cluster_check.exec_in_pod(), which hardcodes
    # `-n maops-platform` - callers of this function pass their own
    # namespace explicitly rather than assuming the application
    # namespace (the validation-client probe pod lives in
    # maops-day6-validation; the isolated application-port probe Pods
    # live in maops-platform, but as standalone Pods this script itself
    # creates and tracks, not via that hardcoded helper).
    result = kube.run("-n", namespace, "exec", pod_name, "--", *cmd)
    return result.stdout.strip()


def _run_probe(namespace: str, pod_name: str, snippet: str) -> ProbeResult:
    """Execs `snippet` in `pod_name` (in `namespace`) and classifies its
    RESULT=<STATE> line into exactly one of the module's seven phase-
    attributed states (see module docstring). Never returns a bare bool
    - callers must go through `assert_connected()`/`assert_denied()`,
    which is what actually decides pass/fail for a given expectation."""
    try:
        output = _exec_in_pod(namespace, pod_name, "/usr/bin/python3.11", "-c", snippet)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return ProbeResult(EXEC_INCONCLUSIVE, f"kubectl exec failed: {stderr if stderr else exc}")
    except subprocess.TimeoutExpired as exc:
        return ProbeResult(EXEC_INCONCLUSIVE, f"kubectl exec timed out: {exc}")

    line = output.strip().splitlines()[-1] if output.strip() else ""
    if not line.startswith("RESULT="):
        return ProbeResult(OUTPUT_INCONCLUSIVE, f"missing/unrecognized probe output: {output!r}")

    state = line[len("RESULT=") :].split(" ", 1)[0].strip()
    if state not in _KNOWN_STATES:
        return ProbeResult(OUTPUT_INCONCLUSIVE, f"unrecognized RESULT state: {line!r}")

    if state == HTTP_RESPONSE:
        if "STATUS=" not in line:
            return ProbeResult(OUTPUT_INCONCLUSIVE, f"HTTP_RESPONSE missing STATUS=: {line!r}")
        status_token = line.split("STATUS=", 1)[-1].strip()
        try:
            int(status_token)
        except ValueError:
            return ProbeResult(OUTPUT_INCONCLUSIVE, f"HTTP_RESPONSE STATUS unparseable: {line!r}")

    return ProbeResult(state, line)


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


# DAY6 third remediation: the small in-container TCP listener the
# target probe Pods run - accepts and immediately closes connections in
# a bounded loop (never a single accept() - each target Pod may be
# connected to more than once across the isolated assertions below).
_TARGET_LISTENER_SNIPPET = (
    "import socket, time\n"
    "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    f"srv.bind(('0.0.0.0', {TARGET_LISTEN_PORT}))\n"
    "srv.listen(5)\n"
    "srv.settimeout(1)\n"
    "deadline = time.time() + 115\n"
    "while time.time() < deadline:\n"
    "    try:\n"
    "        conn, _addr = srv.accept()\n"
    "        conn.close()\n"
    "    except socket.timeout:\n"
    "        continue\n"
)


def _netpol_probe_source_pod_manifest(pod_name: str, component_label: str) -> str:
    """DAY6 third remediation: a temporary, non-ambient probe Pod used
    ONLY as a `kubectl exec` source. Carries ONLY the single
    `app.kubernetes.io/component` label the applicable NetworkPolicy
    selector keys on (never the complete live workload label set) plus
    the run-scoped discovery/cleanup label, and
    `istio.io/dataplane-mode: none` to opt out of maops-platform's
    namespace-level ambient enrollment - see module docstring."""
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {NAMESPACE}
  labels:
    app.kubernetes.io/component: {component_label}
    {NETPOL_PROBE_RUN_LABEL}: "{_NETPOL_PROBE_RUN_ID}"
    {kube.AMBIENT_DATAPLANE_MODE_LABEL}: {NONE_DATAPLANE_MODE_VALUE}
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


def _netpol_probe_target_pod_manifest(pod_name: str, component_label: str) -> str:
    """DAY6 third remediation: a temporary, non-ambient probe Pod
    running a small Python TCP listener on `TARGET_LISTEN_PORT` -
    connected to directly by its own `status.podIP`, never a Service.
    Same isolation labels as the source Pod manifest above."""
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {NAMESPACE}
  labels:
    app.kubernetes.io/component: {component_label}
    {NETPOL_PROBE_RUN_LABEL}: "{_NETPOL_PROBE_RUN_ID}"
    {kube.AMBIENT_DATAPLANE_MODE_LABEL}: {NONE_DATAPLANE_MODE_VALUE}
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
      args: ["-c", {json.dumps(_TARGET_LISTENER_SNIPPET)}]
      ports:
        - containerPort: {TARGET_LISTEN_PORT}
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
        pod = json.loads(result.stdout)
        phase = pod.get("status", {}).get("phase")
        if phase == "Running":
            return pod
        if phase in ("Failed", "Succeeded"):
            return pod  # terminal but not "Running" - let the caller decide
        return None

    return kube.wait_until(_running, timeout=POD_DEADLINE_SECONDS, description=f"pod {pod_name} Running")


def _verify_probe_pod_isolated(pod: dict, pod_name: str) -> bool:
    """DAY6 third remediation: before trusting ANY connectivity result
    from a temporary probe Pod, independently prove it is genuinely
    isolated from ambient - never assumed from how it was created.
    Returns True only if every check passes; each individual check is
    still separately recorded either way."""
    ok = True

    labels = pod.get("metadata", {}).get("labels", {}) or {}
    actual_mode = labels.get(kube.AMBIENT_DATAPLANE_MODE_LABEL)
    ok = record(
        actual_mode == NONE_DATAPLANE_MODE_VALUE,
        f"probe pod {pod_name!r}: {kube.AMBIENT_DATAPLANE_MODE_LABEL} == {actual_mode!r} "
        f"(expected {NONE_DATAPLANE_MODE_VALUE!r} - opted out of maops-platform's namespace-level ambient enrollment)",
    ) and ok

    annotations = pod.get("metadata", {}).get("annotations", {}) or {}
    has_redirection = annotations.get(AMBIENT_REDIRECTION_ANNOTATION) == "enabled"
    ok = record(
        not has_redirection,
        f"probe pod {pod_name!r}: no {AMBIENT_REDIRECTION_ANNOTATION!r} ambient enrollment annotation "
        "(never redirected into the mesh)",
    ) and ok

    containers = pod.get("spec", {}).get("containers", []) or []
    container_names = {c.get("name") for c in containers}
    ok = record(
        SIDECAR_CONTAINER_NAME not in container_names,
        f"probe pod {pod_name!r}: no {SIDECAR_CONTAINER_NAME!r} sidecar container",
    ) and ok

    owner_refs = pod.get("metadata", {}).get("ownerReferences") or []
    ok = record(
        not owner_refs,
        f"probe pod {pod_name!r}: no ownerReferences (never adopted by a Deployment/ReplicaSet/StatefulSet) - found {owner_refs!r}",
    ) and ok

    return ok


def _pod_ip_not_in_any_service_endpointslice(pod_ip: str, pod_name: str) -> bool:
    """DAY6 third remediation: confirms a probe Pod's IP never appears
    in ANY live application Service's EndpointSlice - the probe Pod is
    a standalone Pod no Service selects, so this should always hold;
    checked explicitly rather than assumed. Collects addresses across
    ALL endpoints (ready or not), not just ready ones, since even a
    not-yet-ready inclusion would mean the probe Pod was unexpectedly
    adopted into live Service routing."""
    all_addresses: set[str] = set()
    for service_name in (GATEWAY_SERVICE, APP_SERVICE, STATE_SERVICE):
        result = kube.run(
            "-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service_name}", "-o", "json", check=False
        )
        if result.returncode != 0:
            return record(False, f"probe pod {pod_name!r}: could not read EndpointSlices for Service {service_name!r} to verify isolation")
        try:
            slices = json.loads(result.stdout).get("items", [])
        except json.JSONDecodeError:
            return record(False, f"probe pod {pod_name!r}: unparseable EndpointSlice JSON for Service {service_name!r}")
        for s in slices:
            for ep in s.get("endpoints", []) or []:
                all_addresses.update(ep.get("addresses") or [])

    return record(
        pod_ip not in all_addresses,
        f"probe pod {pod_name!r} (IP {pod_ip}) does not appear in any live application Service EndpointSlice",
    )


# DAY6 fourth remediation (see scripts/mesh_check.py's module docstring
# item 2 for the full rationale): tri-state existence result - only an
# explicit NOT_FOUND proves deletion; any other kubectl failure
# (connection refusal, timeout, Forbidden, an auth failure, unparseable
# output) is API_ERROR and must fail cleanup, never be silently folded
# into "gone".
EXISTS = "EXISTS"
NOT_FOUND = "NOT_FOUND"
API_ERROR = "API_ERROR"


def _pod_state(namespace: str, pod_name: str) -> str:
    """`--ignore-not-found` makes kubectl exit 0 with EMPTY stdout when
    the Pod genuinely does not exist, and exit 0 with non-empty stdout
    when it does - a NONZERO exit therefore can only mean some other API
    error, classified API_ERROR here rather than treated as evidence of
    either state."""
    result = kube.run("-n", namespace, "get", "pod", pod_name, "--ignore-not-found", "-o", "name", check=False)
    if result.returncode != 0:
        return API_ERROR
    return EXISTS if result.stdout.strip() else NOT_FOUND


def delete_pod_and_verify_gone(
    namespace: str,
    pod_name: str,
    timeout: float = DELETE_VERIFY_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """DAY6 remediation item 3: submits Pod deletion, then POLLS until it
    actually disappears - never trusts `--wait=false` alone to mean the
    Pod is gone. DAY6 fourth remediation: that poll uses the tri-state
    `_pod_state()` rather than a boolean exists check - EXISTS keeps
    polling, NOT_FOUND is the only outcome that proves deletion, and any
    API_ERROR immediately fails cleanup (never retried-through as if it
    might still resolve to gone).

    DAY6 third remediation (live-discovered boundary race): a live run
    reported a Pod deletion as "did not disappear" purely because it
    vanished right around the polling deadline - the very next read (for
    failure diagnostics) already showed it gone. If the normal polling
    loop above never observes NOT_FOUND before `timeout` elapses, this
    performs exactly ONE final, fresh tri-state read before deciding
    failure: NOT_FOUND there still passes (labeled as a boundary-read
    pass, for transparency), EXISTS still fails, and API_ERROR still
    fails immediately - never forced deletion, never a silent pass.

    Returns (ok, detail). An API failure submitting the deletion, an
    API_ERROR while polling or on the final read, or a genuine failure
    to disappear even after the final boundary read (e.g. stuck
    Terminating), is a RESTORATION FAILURE - `ok=False` - never silently
    treated as success."""
    submit = subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", CONTEXT, "-n", namespace, "delete", "pod", pod_name, "--ignore-not-found", "--wait=false"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if submit.returncode != 0:
        return False, f"could not submit deletion for pod {pod_name!r}: {submit.stderr.strip()}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _pod_state(namespace, pod_name)
        if state == NOT_FOUND:
            return True, f"pod {pod_name!r} confirmed gone (explicit NOT_FOUND)"
        if state == API_ERROR:
            return False, (
                f"API_ERROR while polling for pod {pod_name!r} deletion - cannot confirm deletion "
                "(connection failure, timeout, or other API error, not an explicit NotFound response)"
            )
        time.sleep(1.0)

    final_state = _pod_state(namespace, pod_name)
    if final_state == NOT_FOUND:
        return True, f"pod {pod_name!r} confirmed gone on the final boundary read (explicit NOT_FOUND, observed just after the {timeout}s polling deadline)"
    if final_state == API_ERROR:
        return False, f"API_ERROR on the final boundary read for pod {pod_name!r} - cannot confirm deletion"
    return False, f"pod {pod_name!r} did not disappear within {timeout}s of deletion (final boundary read still returned EXISTS - possibly stuck Terminating)"


def check_application_port_networkpolicy_isolated() -> bool:
    """DAY6 third remediation - see module docstring for the full
    design. Creates four temporary, non-ambient probe Pods directly in
    maops-platform (two sources, two targets), verifies each is
    genuinely isolated before trusting it, runs the three application-
    port assertions directly against each target's own Pod IP, and
    ALWAYS deletes (and verifies gone) every Pod that was successfully
    created - a cleanup failure fails this check even if every
    connectivity assertion passed."""
    overall_ok = True
    created_pod_names: list[str] = []

    pods_to_create = (
        (GATEWAY_SOURCE_POD_NAME, _netpol_probe_source_pod_manifest(GATEWAY_SOURCE_POD_NAME, "gateway")),
        (APP_SOURCE_POD_NAME, _netpol_probe_source_pod_manifest(APP_SOURCE_POD_NAME, "app")),
        (APP_TARGET_POD_NAME, _netpol_probe_target_pod_manifest(APP_TARGET_POD_NAME, "app")),
        (STATE_TARGET_POD_NAME, _netpol_probe_target_pod_manifest(STATE_TARGET_POD_NAME, "state")),
    )

    try:
        for pod_name, manifest in pods_to_create:
            try:
                _apply(manifest)
                created_pod_names.append(pod_name)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                overall_ok = record(False, f"could not create probe pod {pod_name!r}: {exc}") and overall_ok

        live_pods: dict[str, dict] = {}
        for pod_name, _manifest in pods_to_create:
            if pod_name not in created_pod_names:
                continue
            try:
                pod = _wait_running(NAMESPACE, pod_name)
            except TimeoutError as exc:
                overall_ok = record(False, f"probe pod {pod_name!r} never reached a terminal wait state: {exc}") and overall_ok
                continue
            phase = pod.get("status", {}).get("phase")
            if phase != "Running":
                overall_ok = record(False, f"probe pod {pod_name!r} never reached Running (phase={phase!r})") and overall_ok
                continue
            record(True, f"probe pod {pod_name!r} is Running")

            if not _verify_probe_pod_isolated(pod, pod_name):
                overall_ok = False
                continue

            pod_ip = pod.get("status", {}).get("podIP")
            if not pod_ip:
                overall_ok = record(False, f"probe pod {pod_name!r} has no podIP assigned") and overall_ok
                continue

            if not _pod_ip_not_in_any_service_endpointslice(pod_ip, pod_name):
                overall_ok = False
                continue

            live_pods[pod_name] = pod

        # -- isolated gateway-probe -> app-probe: ALLOWED --
        if GATEWAY_SOURCE_POD_NAME in live_pods and APP_TARGET_POD_NAME in live_pods:
            target_ip = live_pods[APP_TARGET_POD_NAME]["status"]["podIP"]
            result = _run_probe(NAMESPACE, GATEWAY_SOURCE_POD_NAME, _tcp_connect_probe_snippet(target_ip, TARGET_LISTEN_PORT, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_connected(result, "isolated gateway-probe -> app-probe ALLOWED") and overall_ok
        else:
            overall_ok = record(False, "isolated gateway-probe -> app-probe check: source or target probe Pod unavailable") and overall_ok

        # -- isolated gateway-probe -> state-probe: DENIED --
        if GATEWAY_SOURCE_POD_NAME in live_pods and STATE_TARGET_POD_NAME in live_pods:
            target_ip = live_pods[STATE_TARGET_POD_NAME]["status"]["podIP"]
            result = _run_probe(NAMESPACE, GATEWAY_SOURCE_POD_NAME, _tcp_connect_probe_snippet(target_ip, TARGET_LISTEN_PORT, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_denied(result, "isolated gateway-probe -> state-probe DENIED") and overall_ok
        else:
            overall_ok = record(False, "isolated gateway-probe -> state-probe check: source or target probe Pod unavailable") and overall_ok

        # -- isolated app-probe -> state-probe: ALLOWED --
        if APP_SOURCE_POD_NAME in live_pods and STATE_TARGET_POD_NAME in live_pods:
            target_ip = live_pods[STATE_TARGET_POD_NAME]["status"]["podIP"]
            result = _run_probe(NAMESPACE, APP_SOURCE_POD_NAME, _tcp_connect_probe_snippet(target_ip, TARGET_LISTEN_PORT, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_connected(result, "isolated app-probe -> state-probe ALLOWED") and overall_ok
        else:
            overall_ok = record(False, "isolated app-probe -> state-probe check: source or target probe Pod unavailable") and overall_ok
    finally:
        for pod_name in created_pod_names:
            cleanup_ok, cleanup_detail = delete_pod_and_verify_gone(NAMESPACE, pod_name)
            if not cleanup_ok:
                record(False, f"RESTORATION FAILURE: {cleanup_detail}")
                overall_ok = False
            else:
                record(True, cleanup_detail)

    return overall_ok


def main() -> int:
    print(f"# Day 6 NetworkPolicy check: default-deny + explicit allows (context {CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    overall_ok = True

    # -- gateway DNS (kept against the REAL ambient gateway Pod - a UDP
    # socket.getaddrinfo() resolution, never redirected by ztunnel's
    # TCP-focused interception, so this remains semantically valid) --
    gateway_pods = get_pods(GATEWAY_LABEL_SELECTOR)
    if not gateway_pods:
        overall_ok = record(False, "gateway DNS check: no gateway pods available to exec into") and overall_ok
    else:
        gw_pod = gateway_pods[0]["metadata"]["name"]
        result = _run_probe(NAMESPACE, gw_pod, _dns_probe_snippet("maops-app"))
        overall_ok = assert_connected(result, "gateway DNS resolution still works") and overall_ok

    # -- app DNS (kept, same rationale) --
    app_pods = get_pods(APP_LABEL_SELECTOR)
    if not app_pods:
        overall_ok = record(False, "app DNS check: no app pods available to exec into") and overall_ok
    else:
        app_pod = app_pods[0]["metadata"]["name"]
        result = _run_probe(NAMESPACE, app_pod, _dns_probe_snippet("maops-state"))
        overall_ok = assert_connected(result, "app DNS resolution still works") and overall_ok

    # -- isolated, non-ambient application-port NetworkPolicy assertions
    # (gateway -> app ALLOWED, gateway -> state DENIED, app -> state
    # ALLOWED) - DAY6 third remediation, see module docstring --
    overall_ok = check_application_port_networkpolicy_isolated() and overall_ok

    # -- validation-client -> gateway (deny, DAY6 change from Day 5's allow), -> app (deny), -> state (deny) --
    # Unchanged: maops-day6-validation is never ambient-enrolled, so
    # this probe Pod's raw TCP results already genuinely isolate
    # Cilium's NetworkPolicy enforcement.
    try:
        _apply(_validation_pod_manifest())
        pod = _wait_running(VALIDATION_NAMESPACE, VALIDATION_POD_NAME)
        phase = pod.get("status", {}).get("phase")
        if phase != "Running":
            overall_ok = record(False, f"validation-client probe pod never reached Running (phase={phase!r})") and overall_ok
        else:
            result = _run_probe(VALIDATION_NAMESPACE, VALIDATION_POD_NAME, _tcp_connect_probe_snippet(GATEWAY_FQDN, 8080, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_denied(result, "validation-client -> gateway DENIED") and overall_ok
            result = _run_probe(VALIDATION_NAMESPACE, VALIDATION_POD_NAME, _tcp_connect_probe_snippet(APP_FQDN, 8080, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_denied(result, "validation-client -> app DENIED") and overall_ok
            result = _run_probe(VALIDATION_NAMESPACE, VALIDATION_POD_NAME, _tcp_connect_probe_snippet(STATE_FQDN, 8080, CONNECT_TIMEOUT_SECONDS))
            overall_ok = assert_denied(result, "validation-client -> state DENIED") and overall_ok
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError) as exc:
        overall_ok = record(False, f"validation-client probe raised an unexpected error: {exc}")
    finally:
        cleanup_ok, cleanup_detail = delete_pod_and_verify_gone(VALIDATION_NAMESPACE, VALIDATION_POD_NAME)
        if not cleanup_ok:
            record(False, f"RESTORATION FAILURE: {cleanup_detail}")
            overall_ok = False
        else:
            record(True, cleanup_detail)

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

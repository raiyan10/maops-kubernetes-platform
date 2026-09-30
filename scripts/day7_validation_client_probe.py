#!/usr/bin/env python3
"""
DAY7: bounded live proof that a validation-client cannot reach the
gateway CANDIDATE - closing the one path the Day 7 security review found
proven only statically (validate_helm_chart's NetworkPolicy path matrix).

Day 7-only, run separately from `make day7-check` (`make
day7-validation-client-probe`), against the run's own strategy baseline
(never recaptured). Every mutation is either a Helm stage (through
day7_strategy.apply_stage) or a short-lived probe Pod this script creates
and deletes itself.

Sequence:
  1. Entry gate: the release is at the stable stage (100% stable route,
     no candidate object, external stable response).
  2. Candidate-present stage: `green-prepared` (candidate enabled, route
     STILL 100% stable), and the candidate readiness gate must pass.
  3. Destination verified live: the candidate Service's ClusterIP and
     ready endpoints, and every candidate Pod answering its own /livez
     over loopback (the destination process is up and serving).
  4. Source: one probe Pod in the Day 7 validation namespace (never
     ambient-enrolled), labelled app.kubernetes.io/component=
     validation-client, non-root, no token, no Secret, bounded lifetime.
  5. Positive control from that SAME Pod: HTTP 200 through the ingress
     Gateway Service (`maops-edge-istio.maops-ingress`, Host maops.local)
     - proves the source's DNS and networking work.
  6. Correlated denial evidence: `cilium-dbg monitor --type drop` runs as
     an owned, bounded child on EVERY Cilium agent while the probe Pod
     requests the candidate Service. PASS requires ALL of: no HTTP
     response (the destination never answered); DNS resolved to the
     candidate's live ClusterIP; the positive control passed; and at
     least one Cilium drop event with reason "Policy denied" whose source
     is the probe Pod IP and whose destination is a candidate Pod IP on
     port 8080, with both Cilium identities resolved to labels showing the
     validation namespace (source) and component=gateway-candidate
     (destination). A timeout or failed connect WITHOUT such an event is
     INCONCLUSIVE and fails - never reported as a denial.
     What this proves: the Cilium (NetworkPolicy) layer dropped the
     connection before it reached the candidate. The Istio
     AuthorizationPolicy layer is not exercised by this path (the
     traffic never reaches ztunnel on the destination); its candidate
     coverage remains proven statically.
  7. finally: stop and reap the monitors, delete the probe Pod and
     confirm it is gone, then restore the stable stage and verify it
     (day7_strategy.restore_or_verify) - reported separately.

No Secret or token is read or printed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_blue_green
import day7_strategy as d7
import kube
import private_run_dir

TITLE = "Day 7 validation-client -> gateway-candidate denial probe"
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"
PROBE_RUN_LABEL = "maops.io/day7-vc-probe-run"
PROBE_POD_NAME = f"day7-vc-probe-{uuid.uuid4().hex[:10]}"
POD_READY_TIMEOUT_SECONDS = 90.0
POD_DELETE_TIMEOUT_SECONDS = 90.0
MONITOR_ATTACH_SECONDS = 5.0
MONITOR_TAIL_SECONDS = 4.0
NEGATIVE_ATTEMPTS = 2
INGRESS_GATEWAY_SERVICE = "maops-edge-istio"
CILIUM_AGENT_SELECTOR = "k8s-app=cilium"
CILIUM_CONTAINER = "cilium-agent"

# Tolerant parse of a `cilium-dbg monitor --type drop` line, e.g.
#   xx drop (Policy denied) flow 0x0 to endpoint 812, ifindex 9, file bpf_lxc.c:2110, ,
#   identity 51234->33901: 10.244.2.14:40312 -> 10.244.1.29:8080 tcp SYN
_DROP_RE = re.compile(
    r"drop \((?P<reason>[^)]*)\).*?identity (?P<src_id>\d+)->(?P<dst_id>\d+):\s*"
    r"(?P<src>\d+\.\d+\.\d+\.\d+):(?P<sport>\d+)\s*->\s*(?P<dst>\d+\.\d+\.\d+\.\d+):(?P<dport>\d+)"
)


# The shared DNS/connect/HTTP probe contract (day7_strategy.PROBE_SNIPPET)
# with ONE change: the Host header (and path) are parameters, so the
# positive control can address the ingress Gateway's `maops.local` route.
# Classification and output format are otherwise identical, so
# day7_strategy.parse_probe_output/path_verdict apply unchanged.
_SHARED_REQUEST_LINE = "c.request('GET', '/livez', headers={'Host': H})"
assert d7.PROBE_SNIPPET.count(_SHARED_REQUEST_LINE) == 1, "day7_strategy.PROBE_SNIPPET request line changed - update this probe"
_VC_PROBE_SNIPPET = d7.PROBE_SNIPPET.replace(_SHARED_REQUEST_LINE, "c.request('GET', '__PATH__', headers={'Host': '__HOSTHDR__'})")


def vc_probe_snippet(host: str, port: int, path: str = "/livez", host_header: str | None = None) -> str:
    for value in (host, path, host_header or ""):
        if "'" in value or "\\" in value or "\n" in value:
            raise ValueError(f"unsafe probe argument {value!r}")
    return (
        _VC_PROBE_SNIPPET.replace("__HOSTHDR__", host_header or host).replace("__PATH__", path)
        .replace("__HOST__", host).replace("__PORT__", str(port)).replace("__TIMEOUT__", str(d7.PROBE_HTTP_TIMEOUT_SECONDS))
    )


@dataclass(frozen=True)
class DropEvent:
    reason: str
    src_identity: str
    dst_identity: str
    src: str
    dst: str
    dport: int
    node: str = ""


def parse_drop_events(text: str, node: str = "") -> list[DropEvent]:
    """Pure. Every parseable drop line in a monitor capture."""
    events = []
    for line in (text or "").splitlines():
        m = _DROP_RE.search(line)
        if m:
            events.append(DropEvent(m["reason"].strip(), m["src_id"], m["dst_id"], m["src"], m["dst"], int(m["dport"]), node))
    return events


def correlated_denials(events: list[DropEvent], probe_ip: str, candidate_ips: set[str], port: int = d7.BACKEND_PORT) -> list[DropEvent]:
    """Pure. Policy-denied drops from THIS probe Pod to a candidate Pod."""
    return [e for e in events if e.reason.lower() == "policy denied" and e.src == probe_ip and e.dst in candidate_ips and e.dport == port]


def identity_labels_ok(src_labels: str, dst_labels: str, validation_namespace: str) -> tuple[bool, str]:
    """Pure. The Cilium identities of the drop must be the validation
    namespace's client (source) and the candidate (destination)."""
    src_ok = f"k8s:io.kubernetes.pod.namespace={validation_namespace}" in src_labels and "k8s:app.kubernetes.io/component=validation-client" in src_labels
    dst_ok = "k8s:app.kubernetes.io/component=gateway-candidate" in dst_labels and f"k8s:io.kubernetes.pod.namespace={kube.NAMESPACE}" in dst_labels
    return src_ok and dst_ok, f"source identity {'is' if src_ok else 'is NOT'} the {validation_namespace} validation-client; destination identity {'is' if dst_ok else 'is NOT'} the gateway-candidate"


@dataclass
class DenialEvidence:
    negative_results: list = field(default_factory=list)
    control: d7.ProbeResult | None = None
    control_dest: d7.Destination | None = None
    dest: d7.Destination | None = None
    probe_ip: str | None = None
    candidate_ips: set = field(default_factory=set)
    correlated: list = field(default_factory=list)
    identity_ok: bool = False
    identity_detail: str = ""
    monitors_ok: bool = False


def evaluate(ev: DenialEvidence) -> list[tuple[bool, str]]:
    """Pure fail-closed verdict. Every check must hold."""
    checks: list[tuple[bool, str]] = []
    checks.append((bool(ev.probe_ip), f"probe Pod has an IP ({ev.probe_ip!r})"))
    checks.append((ev.dest is not None and ev.dest.verified, f"destination {d7.CANDIDATE_SERVICE} verified live ({ev.dest})"))
    checks.append((bool(ev.candidate_ips), f"candidate Pod IPs known ({sorted(ev.candidate_ips)})"))
    if ev.control is None or ev.control_dest is None:
        checks.append((False, "positive control did not run"))
    else:
        ok, why = d7.path_verdict(ev.control, True, ev.control_dest)
        checks.append((ok, f"positive control: validation-client -> ingress Gateway (Host maops.local) - {why}"))
    checks.append((ev.monitors_ok, "Cilium drop monitors attached on every agent and were reaped"))
    if not ev.negative_results:
        checks.append((False, "negative probe did not run"))
    for i, result in enumerate(ev.negative_results, 1):
        no_response = result.kind in d7.NO_HTTP_RESPONSE_KINDS
        resolved = ev.dest is not None and result.addr == ev.dest.cluster_ip
        checks.append((resolved, f"negative attempt {i}: {d7.CANDIDATE_SERVICE} resolved to its live ClusterIP ({result.addr!r})"))
        checks.append((no_response, f"negative attempt {i}: no HTTP response from the candidate (observed {result.kind}:{result.detail}) - a status of any kind would mean it answered"))
    checks.append((bool(ev.correlated), f"authoritative: {len(ev.correlated)} Cilium 'Policy denied' drop(s) from the probe Pod {ev.probe_ip} to a candidate Pod on :{d7.BACKEND_PORT} ({[(e.node, e.src, e.dst) for e in ev.correlated][:3]}) - a timeout without such an event would be INCONCLUSIVE"))
    checks.append((ev.identity_ok, f"drop identities: {ev.identity_detail or 'not resolved'}"))
    return checks


def probe_pod_manifest(name: str = PROBE_POD_NAME, run_id: str = "") -> str:
    return f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {kube.VALIDATION_NAMESPACE}
  labels:
    app.kubernetes.io/component: validation-client
    {PROBE_RUN_LABEL}: "{run_id or name}"
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 240
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
      command:
        - /usr/bin/python3.11
      args:
        - "-c"
        - "import time; time.sleep(230)"
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop:
            - ALL
      resources:
        requests:
          cpu: 10m
          memory: 16Mi
        limits:
          cpu: 100m
          memory: 64Mi
"""


def _apply(manifest: str) -> None:
    d7.require_day7_profile()
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "apply", "-f", "-"],
        input=manifest, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl apply failed: {result.stderr.strip()[:300]}")


def wait_probe_pod_running(rec: d7.Recorder) -> dict | None:
    deadline = time.monotonic() + POD_READY_TIMEOUT_SECONDS
    pod = None
    while time.monotonic() < deadline:
        state, pod = d7.get_json_or_none("-n", kube.VALIDATION_NAMESPACE, "get", "pod", PROBE_POD_NAME)
        if state == "found" and pod.get("status", {}).get("phase") == "Running" and d7.pod_is_ready(pod) and pod.get("status", {}).get("podIP"):
            rec.record(True, f"probe Pod {kube.VALIDATION_NAMESPACE}/{PROBE_POD_NAME} Running on {pod['spec'].get('nodeName')} with IP {pod['status']['podIP']}")
            return pod
        time.sleep(2.0)
    rec.record(False, f"probe Pod {PROBE_POD_NAME} not Running/Ready within {POD_READY_TIMEOUT_SECONDS}s (phase {None if pod is None else pod.get('status', {}).get('phase')})")
    return None


def delete_probe_pod(rec: d7.Recorder) -> bool:
    """Delete (idempotent) and CONFIRM explicit NotFound, bounded."""
    try:
        d7._kubectl("-n", kube.VALIDATION_NAMESPACE, "delete", "pod", PROBE_POD_NAME, "--ignore-not-found", "--wait=false", check=False, timeout=30.0)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + POD_DELETE_TIMEOUT_SECONDS
    state = "unknown"
    while time.monotonic() < deadline:
        state, _ = d7.get_json_or_none("-n", kube.VALIDATION_NAMESPACE, "get", "pod", PROBE_POD_NAME)
        if state == "not_found":
            return rec.record(True, f"probe Pod {PROBE_POD_NAME} deleted and confirmed absent (explicit NotFound)")
        time.sleep(2.0)
    return rec.record(False, f"probe Pod {PROBE_POD_NAME} NOT confirmed absent within {POD_DELETE_TIMEOUT_SECONDS}s (last state {state}) - PROBE RESOURCE MAY BE LEFT BEHIND")


def candidate_serving(rec: d7.Recorder, candidate_pods: list[dict]) -> bool:
    """Each candidate Pod answers its own /livez over loopback - the
    destination process is up (in-Pod, not through any policy path)."""
    ok = bool(candidate_pods)
    for pod in candidate_pods:
        name = pod["metadata"]["name"]
        result = d7.exec_probe(name, d7.GATEWAY_CONTAINER, "127.0.0.1")
        passed = result.kind == "status" and result.detail == "200"
        ok = rec.record(passed, f"destination {name} serves /livez on loopback ({result.kind}:{result.detail})") and ok
    return ok


def exec_in_probe(host: str, port: int, path: str, host_header: str | None = None) -> d7.ProbeResult:
    try:
        result = d7._kubectl(
            "-n", kube.VALIDATION_NAMESPACE, "exec", PROBE_POD_NAME, "-c", "probe", "--",
            "/usr/bin/python3.11", "-c", vc_probe_snippet(host, port, path=path, host_header=host_header),
            check=False, timeout=kube.subprocess_timeout_for(d7.PROBE_HTTP_TIMEOUT_SECONDS),
        )
    except subprocess.TimeoutExpired:
        return d7.ProbeResult("exec_timeout", "kubectl exec timed out")
    if result.returncode != 0:
        return d7.ProbeResult("exec_failed", f"kubectl exec exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
    return d7.parse_probe_output(result.stdout)


@dataclass
class Monitor:
    pod: str
    node: str
    proc: object
    out: object


def start_monitors(agents: list[dict], popen=subprocess.Popen) -> list[Monitor]:
    """One owned `cilium-dbg monitor --type drop` child per Cilium agent,
    output to private temp files (never an undrained pipe)."""
    d7.require_day7_profile()
    monitors = []
    for agent in agents:
        sink = tempfile.TemporaryFile()
        cmd = ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "-n", "kube-system", "exec",
               agent["metadata"]["name"], "-c", CILIUM_CONTAINER, "--", "cilium-dbg", "monitor", "--type", "drop"]
        proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
        d7.OWNED_CHILDREN.append(proc)
        monitors.append(Monitor(agent["metadata"]["name"], agent.get("spec", {}).get("nodeName", ""), proc, sink))
    return monitors


def stop_monitors(monitors: list[Monitor]) -> tuple[dict[str, str], list[str]]:
    """Terminate -> kill -> reap every monitor (always), then read its
    capture. Returns ({node: text}, [child outcomes])."""
    captures, outcomes = {}, []
    for m in monitors:
        try:
            outcomes.append(f"{m.pod}: {d7.reap_child(m.proc)}")
        finally:
            try:
                m.out.seek(0)
                captures[m.node or m.pod] = m.out.read().decode("utf-8", errors="replace")
            finally:
                m.out.close()
    return captures, outcomes


def read_ingress_destination() -> d7.Destination:
    """The ingress Gateway Service (maops-ingress), verified exactly like
    day7_strategy.read_destination: live ClusterIP + ready endpoints."""
    name = f"{INGRESS_GATEWAY_SERVICE}.{kube.INGRESS_NAMESPACE}"
    state, svc = d7.get_json_or_none("-n", kube.INGRESS_NAMESPACE, "get", "service", INGRESS_GATEWAY_SERVICE)
    cluster_ip = (svc or {}).get("spec", {}).get("clusterIP") if state == "found" else None
    slices = d7.list_json("endpointslices", f"kubernetes.io/service-name={INGRESS_GATEWAY_SERVICE}", namespace=kube.INGRESS_NAMESPACE)
    ready = None if slices is None else len(d7.parse_endpoint_slices(slices).ready_addresses)
    return d7.Destination(name, cluster_ip, ready)


def save_captures(rec: d7.Recorder, captures: dict[str, str]) -> None:
    """Raw monitor captures -> the run's private directory (0600, O_EXCL,
    never overwritten). Drop lines carry only IPs/ports/identities."""
    run_dir = os.environ.get(private_run_dir.RUN_DIR_ENV)
    if not run_dir:
        rec.informational("no private run directory set - raw Cilium captures not saved")
        return
    try:
        private_run_dir.validate_run_dir(run_dir)
        path = os.path.join(run_dir, f"{PROBE_POD_NAME}-cilium-drop-monitor.txt")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, private_run_dir.FILE_MODE)
        with os.fdopen(fd, "w") as f:
            for node in sorted(captures):
                f.write(f"===== {node} =====\n{captures[node]}\n")
        rec.informational(f"raw Cilium drop captures saved to {path}")
    except (OSError, private_run_dir.PrivateRunDirError) as exc:
        rec.informational(f"raw Cilium captures NOT saved: {exc}")


def resolve_identity_labels(agent_pod: str, identity: str) -> str:
    try:
        result = d7._kubectl("-n", "kube-system", "exec", agent_pod, "-c", CILIUM_CONTAINER, "--", "cilium-dbg", "identity", "get", identity, "-o", "json", check=False, timeout=30.0)
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout if result.returncode == 0 else ""


def body(rec: d7.Recorder, baseline: dict) -> bool:
    if not day7_blue_green.entry_gate(rec, baseline):
        rec.record(False, "entry gate failed - no stage submitted")
        return False
    rec.section("candidate-present stage (route stays 100% stable)")
    if not d7.apply_stage(rec, "green-prepared"):
        return False
    stable_backends = d7.expected_backends(d7.load_stage_values("green-prepared"))
    if d7.wait_route(rec, stable_backends, "green-prepared") is None:
        return False
    gate_ok, checks = d7.run_gate(stable_backends, d7.load_stage_values("green-prepared")["candidate"]["replicas"])
    for passed, msg in checks:
        rec.record(passed, f"gate: {msg}")
    if not gate_ok:
        return rec.record(False, "candidate readiness gate failed - probe NOT run (a denial against an unready candidate would prove nothing)")

    ev = DenialEvidence()
    rec.section("destination")
    candidate_pods = [p for p in (d7.list_json("pods", d7.candidate_selector()) or []) if d7.pod_is_ready(p)]
    ev.candidate_ips = {p["status"]["podIP"] for p in candidate_pods if p.get("status", {}).get("podIP")}
    ev.dest = d7.read_destination(d7.CANDIDATE_SERVICE)
    rec.record(ev.dest.verified, f"destination Service {d7.CANDIDATE_SERVICE}: clusterIP {ev.dest.cluster_ip!r}, ready endpoints {ev.dest.ready_endpoints!r}, Pod IPs {sorted(ev.candidate_ips)}")
    if not candidate_serving(rec, candidate_pods):
        return rec.record(False, "destination not verified serving - probe NOT run")

    rec.section("source probe Pod + positive control + monitored negative probe")
    monitors: list[Monitor] = []
    pod_created = False
    try:
        _apply(probe_pod_manifest(run_id=os.environ.get(d7.RUN_ID_ENV, "")))
        pod_created = True
        pod = wait_probe_pod_running(rec)
        if pod is None:
            return False
        ev.probe_ip = pod["status"]["podIP"]
        state, ns = d7.get_json_or_none("get", "namespace", kube.VALIDATION_NAMESPACE)
        enrolled = ((ns or {}).get("metadata", {}).get("labels") or {}).get(kube.AMBIENT_DATAPLANE_MODE_LABEL)
        rec.record(state == "found" and enrolled is None, f"source namespace {kube.VALIDATION_NAMESPACE} is not mesh-enrolled ({kube.AMBIENT_DATAPLANE_MODE_LABEL}={enrolled!r})")

        ev.control_dest = read_ingress_destination()
        rec.record(ev.control_dest.verified, f"positive-control destination {ev.control_dest.service}: clusterIP {ev.control_dest.cluster_ip!r}, ready endpoints {ev.control_dest.ready_endpoints!r}")
        ev.control = exec_in_probe(f"{INGRESS_GATEWAY_SERVICE}.{kube.INGRESS_NAMESPACE}.svc.cluster.local", 80, "/", host_header=kube.ROUTING_HOSTNAME)
        rec.record(ev.control.kind == "status" and ev.control.detail == "200", f"positive control validation-client -> ingress Gateway: {ev.control.kind}:{ev.control.detail}")

        agents = d7.list_json("pods", CILIUM_AGENT_SELECTOR, namespace="kube-system") or []
        rec.record(len(agents) == 3, f"{len(agents)} Cilium agents to monitor (expected 3)")
        monitors = start_monitors(agents)
        time.sleep(MONITOR_ATTACH_SECONDS)
        attached = all(m.proc.poll() is None for m in monitors) and len(monitors) == 3
        for _ in range(NEGATIVE_ATTEMPTS):
            ev.negative_results.append(exec_in_probe(f"{d7.CANDIDATE_SERVICE}.{kube.NAMESPACE}.svc.cluster.local", d7.BACKEND_PORT, "/livez"))
        time.sleep(MONITOR_TAIL_SECONDS)
        still_running = all(m.proc.poll() is None for m in monitors)
        captures, outcomes = stop_monitors(monitors)
        monitors = []
        save_captures(rec, captures)
        ev.monitors_ok = attached and still_running
        rec.informational(f"monitor children: {outcomes}")
        events = [e for node, text in captures.items() for e in parse_drop_events(text, node)]
        rec.informational(f"{len(events)} drop event(s) captured across {len(captures)} agents")
        ev.correlated = correlated_denials(events, ev.probe_ip, ev.candidate_ips)
        if ev.correlated:
            first = ev.correlated[0]
            agent = next((a["metadata"]["name"] for a in agents if a.get("spec", {}).get("nodeName") == first.node), agents[0]["metadata"]["name"])
            src_labels = resolve_identity_labels(agent, first.src_identity)
            dst_labels = resolve_identity_labels(agent, first.dst_identity)
            ev.identity_ok, ev.identity_detail = identity_labels_ok(src_labels, dst_labels, kube.VALIDATION_NAMESPACE)
    finally:
        if monitors:
            stop_monitors(monitors)
        if pod_created:
            delete_probe_pod(rec)

    rec.section("verdict")
    ok = True
    for passed, msg in evaluate(ev):
        ok = rec.record(passed, msg) and ok
    rec.informational(
        "scope: this proves a Cilium NetworkPolicy drop of validation-client -> candidate traffic; the Istio "
        "AuthorizationPolicy layer is not reached on this path (its candidate coverage is proven statically)"
    )
    return ok and not rec.failures()


def main() -> int:
    rec = d7.Recorder()
    print(f"# {TITLE} (context {kube.CONTEXT}, probe Pod {kube.VALIDATION_NAMESPACE}/{PROBE_POD_NAME})")
    try:
        d7.require_day7_profile()
        kube.verify_context()
        baseline = d7.load_strategy_baseline()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"FAIL: {exc} - nothing was mutated", file=sys.stderr)
        return 1
    return d7.run_experiment(rec, TITLE, lambda: body(rec, baseline), lambda: d7.restore_or_verify(rec, baseline["stable_message"]))


if __name__ == "__main__":
    raise SystemExit(main())

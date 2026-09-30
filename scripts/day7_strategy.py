#!/usr/bin/env python3
"""
DAY7: shared library for the three bounded deployment-strategy
experiments (scripts/day7_blue_green.py, day7_canary.py,
day7_recreate.py) and the Day 7 baseline/final checks.

Nothing here runs on import. Every live function talks ONLY to the Day 7
profile's cluster (`require_day7_profile()` refuses to run under any
other profile) through scripts/kube.py's explicit --kubeconfig/--context
and Helm's explicit --kubeconfig/--kube-context.

Design rules this module encodes:

  - Every chart-owned change goes through `helm upgrade` with
    `--reset-values -f helm-values/day7/<stage>.yaml -f <verified build
    overlay>` - never `--reuse-values`, never `kubectl patch`. After
    every stage, `helm get values` must equal the stage file plus exactly
    the build's image tags (expected_release_values), so no candidate
    flag or route weight can silently carry into a later stage or into
    restoration, and no stage can run another build.
  - A route-changing stage (Blue/Green cutover, Canary, Recreate
    serving) can only be applied through `promote()`, which runs the
    candidate preflight gate first and refuses - without submitting
    anything - if any gate check fails.
  - A successful Helm command or rollout status is never proof on its
    own: routing is proven from the live HTTPRoute (backendRefs plus
    Accepted/ResolvedRefs conditions whose observedGeneration equals
    the route's current generation) AND real external requests through
    the Day 7 host port; cleanup is proven by explicit NotFound/empty
    lists for every candidate-only object.
  - `run_experiment()` always attempts restoration to the stable stage
    in a `finally`, reports PRIMARY and RESTORATION outcomes separately,
    and fails the run if either failed - a restoration failure is never
    hidden behind (or by) the experiment's own failure.
  - External traffic evidence is sampled, never assumed. A finite
    sample is reported as counts; it is never presented as proof of an
    exact percentage.

Mesh-path caveat: stable and candidate share the maops-gateway
ServiceAccount and therefore ONE Istio principal. The allowed/denied
path probes here prove end-to-end reachability (an HTTP status is
required for "allowed"; "denied" means no HTTP response at all - a
local TCP connect is never counted either way, because ambient
redirection completes the client's connect locally). They do not
isolate WHICH layer denied a path; scripts/mesh_check.py's correlated
ztunnel-log proof remains the AuthorizationPolicy-layer evidence for
the stable workloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ambient_workload_check
import day7_build
import k8s_yaml
import kube
import private_run_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
CHART_DIR = REPO_ROOT / "charts" / "maops-kubernetes-platform"
STAGE_DIR = REPO_ROOT / "helm-values" / "day7"

STAGES = (
    "stable",
    "green-prepared",
    "blue-green-cutover",
    "canary-90-10",
    "candidate-unready",
    "recreate-prepared",
    "recreate-serving",
    "recreate-changed",
)
# Stages that move external traffic onto the candidate: only ever
# applied through promote() (gate first). recreate-changed also routes
# to the candidate, but it is reached FROM recreate-serving (already
# gated) and changes only the candidate's ConfigMap.
PROMOTION_STAGES = frozenset({"blue-green-cutover", "canary-90-10", "recreate-serving"})
# The deliberately-unready candidate stage can never pass Helm's
# resource-readiness wait, so it is applied with --wait=hookOnly and its
# (non-)readiness is observed directly instead.
NO_RESOURCE_WAIT_STAGES = frozenset({"candidate-unready"})

HELM_TIMEOUT_SECONDS = 300
HELM_SUBPROCESS_TIMEOUT_SECONDS = HELM_TIMEOUT_SECONDS + 60
HELM_READ_TIMEOUT_SECONDS = 60

STABLE_DEPLOYMENT = "maops-gateway"
STABLE_SERVICE = "maops-gateway"
CANDIDATE_DEPLOYMENT = "maops-gateway-candidate"
CANDIDATE_SERVICE = "maops-gateway-candidate"
CANDIDATE_CONFIGMAP = "maops-gateway-candidate-config"
CANDIDATE_AUTHZ = "maops-gateway-candidate-authz"
CANDIDATE_NETWORK_POLICIES = (
    "maops-allow-gateway-candidate-ingress-from-istio-ingress-gateway",
    "maops-allow-gateway-candidate-egress-to-app",
    "maops-allow-app-ingress-from-gateway-candidate",
)
CANDIDATE_COMPONENT = "gateway-candidate"
CANDIDATE_COMPONENT_SELECTOR = f"app.kubernetes.io/component={CANDIDATE_COMPONENT}"
GATEWAY_CONTAINER = "maops-gateway"
BACKEND_PORT = 8080
STABLE_EXPECTED_REPLICAS = 3
APP_EXPECTED_SERVICE_NAME = "maops-kubernetes-app"

EXPECTED_POD_SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
}
EXPECTED_SECCOMP = "RuntimeDefault"
REQUIRED_LISTEN_PORTS = ambient_workload_check.REQUIRED_LISTEN_PORTS

EXTERNAL_TIMEOUT_SECONDS = 3.0
ROUTE_WAIT_SECONDS = 90.0
ROUTE_POLL_SECONDS = 2.0
EXTERNAL_CONVERGE_SECONDS = 90.0
CANDIDATE_ABSENT_WAIT_SECONDS = 150.0
PROBE_HTTP_TIMEOUT_SECONDS = 4
GATEWAY_API_DEFAULT_WEIGHT = 1

STRATEGY_BASELINE_PATH_ENV = "DAY7_STRATEGY_BASELINE_PATH"
RUN_ID_ENV = "DAY7_RUN_ID"


# --------------------------------------------------------------------------
# Result recording
# --------------------------------------------------------------------------


class Recorder:
    """Per-script PASS/FAIL ledger. `informational()` lines are printed
    but never counted as passes, so an observation (e.g. "no
    interruption was sampled") can never inflate a pass count."""

    def __init__(self) -> None:
        self.results: list[tuple[bool, str]] = []

    def record(self, ok: bool, message: str) -> bool:
        self.results.append((bool(ok), message))
        print(f"[{'PASS' if ok else 'FAIL'}] {message}", flush=True)
        return bool(ok)

    def informational(self, message: str) -> None:
        print(f"[INFO] {message}", flush=True)

    def section(self, title: str) -> None:
        print(f"\n## {title}", flush=True)

    def failures(self) -> list[str]:
        return [m for ok, m in self.results if not ok]


def require_day7_profile() -> None:
    """Called by every live entry point below (apply_stage, promote,
    observe_gate, check_candidate_mesh_paths) as well as by each script's
    main(): under any other profile these raise before any kubectl/helm
    call, so no code path here can reach the Day 6 cluster."""
    if kube.PROFILE != "day7":
        raise RuntimeError(
            f"Day 7 strategy tooling refuses to run under cluster profile {kube.PROFILE!r} "
            f"(context {kube.CONTEXT}); set {kube.PROFILE_ENV}=day7 - it never touches the Day 6 cluster"
        )


# --------------------------------------------------------------------------
# Helm stages
# --------------------------------------------------------------------------


def stage_path(stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unknown Day 7 stage {stage!r} (expected one of {STAGES})")
    return STAGE_DIR / f"{stage}.yaml"


def load_stage_values(stage: str) -> dict:
    docs = k8s_yaml.load_all(stage_path(stage).read_text())
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ValueError(f"stage file {stage_path(stage)} must contain exactly one mapping document")
    return docs[0]


def active_build() -> day7_build.Build:
    """The verified build every Day 7 stage of this invocation carries
    (scripts/day7_build.py `current.json`, fully re-validated on each
    call). Experiments additionally require it to equal the build their
    strategy baseline was captured under (load_strategy_baseline)."""
    return day7_build.load_current()


def expected_release_values(stage: str, build: day7_build.Build) -> dict:
    """What `helm get values` (user-supplied) must equal after
    `--reset-values -f <stage> -f <build overlay>`: the stage file with
    ONLY images.*.tag pinned by the build (the overlay may hold nothing
    else - day7_build.validate_overlay)."""
    overlay = build.overlay()
    day7_build.validate_overlay(overlay, build.version)
    return day7_build.deep_merge(load_stage_values(stage), overlay)


def helm_scope_args() -> list[str]:
    return ["--namespace", kube.NAMESPACE, "--kubeconfig", kube.KUBECONFIG_PATH, "--kube-context", kube.CONTEXT]


def helm_stage_command(stage: str, build: day7_build.Build) -> list[str]:
    """The ONE way a Day 7 stage is applied. Pure (unit-tested): always
    `--reset-values` + exactly two `-f`: the stage file, then the
    verified build overlay (image tags only - the chart refuses a Day 7
    stage without it), never `--reuse-values`/`--set`, never `--install`
    (the release must already exist - `make day7-deploy` installs it),
    always a bounded timeout."""
    wait = "hookOnly" if stage in NO_RESOURCE_WAIT_STAGES else "watcher"
    return [
        "helm", "upgrade", kube.HELM_RELEASE_NAME, str(CHART_DIR),
        "--reset-values",
        "-f", str(stage_path(stage)),
        "-f", str(day7_build.values_path(build)),
        f"--wait={wait}",
        "--timeout", f"{HELM_TIMEOUT_SECONDS}s",
        *helm_scope_args(),
    ]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Every Helm command of this module. Refuses (RuntimeError, before
    any process starts) unless the Day 7 profile is selected."""
    require_day7_profile()
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _kubectl(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Every kubectl call of this module goes through here: it refuses
    (RuntimeError, before any process starts) unless the Day 7 profile is
    selected, so no read in this module can ever reach the Day 6 cluster -
    not even from an insufficiently mocked unit test."""
    require_day7_profile()
    return kube.run(*args, **kwargs)


def helm_status() -> dict | None:
    try:
        result = _run(["helm", "status", kube.HELM_RELEASE_NAME, "-o", "json", *helm_scope_args()], HELM_READ_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def helm_revision() -> int | None:
    status = helm_status()
    version = (status or {}).get("version")
    return version if isinstance(version, int) else None


def helm_values(all_values: bool = False) -> dict | None:
    cmd = ["helm", "get", "values", kube.HELM_RELEASE_NAME, "-o", "json", *helm_scope_args()]
    if all_values:
        cmd.insert(4, "--all")
    try:
        result = _run(cmd, HELM_READ_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def helm_manifest() -> str | None:
    try:
        result = _run(["helm", "get", "manifest", kube.HELM_RELEASE_NAME, *helm_scope_args()], HELM_READ_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return result.stdout if result.returncode == 0 else None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Every stage whose `helm upgrade` was actually started in this process,
# in order - lets restore_or_verify() distinguish "nothing was ever
# submitted" (verify only, no mutation) from "something was" (restore).
SUBMITTED_STAGES: list[str] = []


def apply_stage(rec: Recorder, stage: str, label: str | None = None) -> bool:
    """Applies `stage` and proves it landed as exactly that stage:
    Helm exit 0, a NEW revision (previous + 1), and `helm get values`
    equal to the stage file plus the active build's image tags. Never
    proves routing or readiness - callers
    do that from live state."""
    require_day7_profile()
    label = label or f"stage {stage}"
    try:
        build = active_build()
        expected_values = expected_release_values(stage, build)
    except day7_build.BuildError as exc:
        return rec.record(False, f"{label}: no verified build to carry - NOT submitted ({exc})")
    revision_before = helm_revision()
    cmd = helm_stage_command(stage, build)
    print(f"  $ {' '.join(cmd)}", flush=True)
    SUBMITTED_STAGES.append(stage)
    try:
        result = _run(cmd, HELM_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        rec.record(False, f"{label}: helm upgrade did not return within {HELM_SUBPROCESS_TIMEOUT_SECONDS}s")
        return False
    ok = rec.record(result.returncode == 0, f"{label}: helm upgrade exit {result.returncode}" + ("" if result.returncode == 0 else f" ({result.stderr.strip()[:500]})"))
    revision_after = helm_revision()
    ok = rec.record(
        revision_before is not None and revision_after == revision_before + 1,
        f"{label}: Helm recorded a new revision ({revision_before} -> {revision_after})",
    ) and ok
    actual_values = helm_values()
    ok = rec.record(
        actual_values == expected_values,
        f"{label}: `helm get values` equals {stage_path(stage).relative_to(REPO_ROOT)} + build {build.build_id[:12]} image tags exactly (nothing carried from an earlier stage)"
        + ("" if actual_values == expected_values else f" - found {actual_values!r}"),
    ) and ok
    return ok


# --------------------------------------------------------------------------
# HTTPRoute / Gateway
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteSnapshot:
    uid: str | None
    generation: int | None
    backends: tuple[tuple[str, int | None, int], ...]
    parent_found: bool
    conditions: dict  # type -> (status, observedGeneration)


def _backend_key(ref: dict) -> tuple[str, int | None, int]:
    """Normalizes one backendRef. The Gateway API CRD defaults `weight`
    to 1, so an unweighted ref is compared as weight 1 on both sides. A
    ref to anything other than a same-namespace core Service is encoded
    so it can never equal an expected Service backend."""
    name = str(ref.get("name"))
    kind = ref.get("kind", "Service")
    group = ref.get("group", "")
    namespace = ref.get("namespace", kube.NAMESPACE)
    if kind != "Service" or group not in ("", None) or namespace != kube.NAMESPACE:
        name = f"{group}/{kind}/{namespace}/{name}"
    weight = ref.get("weight", GATEWAY_API_DEFAULT_WEIGHT)
    return name, ref.get("port"), weight


def parse_route(route: dict) -> RouteSnapshot:
    metadata = route.get("metadata", {})
    rules = route.get("spec", {}).get("rules", []) or []
    refs: list[dict] = []
    for rule in rules:
        refs.extend(rule.get("backendRefs", []) or [])
    parents = route.get("status", {}).get("parents", []) or []
    matching = [
        p for p in parents
        if p.get("parentRef", {}).get("name") == kube.GATEWAY_API_GATEWAY_NAME
        and p.get("parentRef", {}).get("namespace", kube.NAMESPACE) == kube.INGRESS_NAMESPACE
    ]
    conditions = {}
    if len(matching) == 1:
        for c in matching[0].get("conditions", []) or []:
            conditions[c.get("type")] = (c.get("status"), c.get("observedGeneration"))
    return RouteSnapshot(
        uid=metadata.get("uid"),
        generation=metadata.get("generation"),
        backends=tuple(sorted(_backend_key(r) for r in refs)),
        parent_found=len(matching) == 1,
        conditions=conditions,
    )


def expected_backends(stage_values: dict) -> tuple[tuple[str, int, int], ...]:
    """The backend set a stage's routing values must produce - derived
    from the stage file, independently of the chart template."""
    routing = stage_values.get("routing", {})
    mode = routing.get("mode", "stable")
    if mode == "stable":
        refs = [(STABLE_SERVICE, BACKEND_PORT, GATEWAY_API_DEFAULT_WEIGHT)]
    elif mode == "candidate":
        refs = [(CANDIDATE_SERVICE, BACKEND_PORT, GATEWAY_API_DEFAULT_WEIGHT)]
    elif mode == "weighted":
        refs = [(STABLE_SERVICE, BACKEND_PORT, routing["stableWeight"]), (CANDIDATE_SERVICE, BACKEND_PORT, routing["candidateWeight"])]
    else:
        raise ValueError(f"unknown routing.mode {mode!r}")
    return tuple(sorted(refs))


def route_is_current(snapshot: RouteSnapshot | None, expected: tuple) -> tuple[bool, str]:
    """Pure: the live route has exactly the expected backends AND a
    status entry for the maops-edge parent whose Accepted and
    ResolvedRefs conditions are True for the route's CURRENT
    generation (a stale True from an earlier generation never counts)."""
    if snapshot is None:
        return False, "HTTPRoute could not be read"
    if snapshot.backends != expected:
        return False, f"backendRefs {list(snapshot.backends)} != expected {list(expected)}"
    if not snapshot.parent_found:
        return False, f"no single status entry for parent {kube.INGRESS_NAMESPACE}/{kube.GATEWAY_API_GATEWAY_NAME}"
    for cond in ("Accepted", "ResolvedRefs"):
        status, observed = snapshot.conditions.get(cond, (None, None))
        if status != "True":
            return False, f"{cond}={status!r}"
        if observed != snapshot.generation:
            return False, f"{cond} observedGeneration {observed} != generation {snapshot.generation} (stale)"
    return True, f"generation {snapshot.generation}, backends {list(snapshot.backends)}, Accepted/ResolvedRefs True for the current generation"


def read_route() -> RouteSnapshot | None:
    try:
        result = _kubectl("-n", kube.NAMESPACE, "get", "httproute", kube.GATEWAY_HTTPROUTE_NAME, "-o", "json", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return parse_route(json.loads(result.stdout))
    except (json.JSONDecodeError, AttributeError):
        return None


def wait_route(rec: Recorder, expected: tuple, label: str, timeout: float = ROUTE_WAIT_SECONDS) -> RouteSnapshot | None:
    deadline = time.monotonic() + timeout
    snapshot, detail = None, "not read"
    while True:
        snapshot = read_route()
        ok, detail = route_is_current(snapshot, expected)
        if ok:
            rec.record(True, f"{label}: HTTPRoute {kube.GATEWAY_HTTPROUTE_NAME} is current - {detail}")
            return snapshot
        if time.monotonic() >= deadline:
            break
        time.sleep(ROUTE_POLL_SECONDS)
    rec.record(False, f"{label}: HTTPRoute {kube.GATEWAY_HTTPROUTE_NAME} not current within {timeout}s - {detail}")
    return None


def gateway_is_current() -> tuple[bool, str]:
    try:
        result = _kubectl("-n", kube.INGRESS_NAMESPACE, "get", "gateway", kube.GATEWAY_API_GATEWAY_NAME, "-o", "json", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        return False, "kubectl get gateway timed out"
    if result.returncode != 0:
        return False, f"kubectl get gateway failed: {result.stderr.strip()}"
    try:
        gw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"unparseable Gateway JSON: {exc}"
    generation = gw.get("metadata", {}).get("generation")
    conditions = {c.get("type"): c for c in gw.get("status", {}).get("conditions", []) or []}
    for cond in ("Accepted", "Programmed"):
        c = conditions.get(cond) or {}
        if c.get("status") != "True" or c.get("observedGeneration") != generation:
            return False, f"Gateway {cond}={c.get('status')!r} observedGeneration={c.get('observedGeneration')!r} generation={generation!r}"
    return True, f"Gateway {kube.GATEWAY_API_GATEWAY_NAME} Accepted/Programmed True for generation {generation}"


# --------------------------------------------------------------------------
# Pods / endpoints
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointView:
    ready_addresses: frozenset
    ready_target_pods: frozenset
    all_target_pods: frozenset


def parse_endpoint_slices(slices: list[dict]) -> EndpointView:
    ready_addresses: set[str] = set()
    ready_pods: set[str] = set()
    all_pods: set[str] = set()
    for s in slices or []:
        if not isinstance(s, dict):
            continue
        for ep in s.get("endpoints", []) or []:
            if not isinstance(ep, dict):
                continue
            target = (ep.get("targetRef") or {})
            pod_name = target.get("name") if target.get("kind") == "Pod" else None
            if pod_name:
                all_pods.add(pod_name)
            if (ep.get("conditions") or {}).get("ready") is True:
                ready_addresses.update(ep.get("addresses") or [])
                if pod_name:
                    ready_pods.add(pod_name)
    return EndpointView(frozenset(ready_addresses), frozenset(ready_pods), frozenset(all_pods))


def read_endpoints(service: str) -> EndpointView | None:
    try:
        result = _kubectl("-n", kube.NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}", "-o", "json", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return parse_endpoint_slices(json.loads(result.stdout).get("items", []))
    except json.JSONDecodeError:
        return None


def list_json(resource: str, selector: str | None = None, namespace: str | None = None) -> list[dict] | None:
    args = ["-n", namespace or kube.NAMESPACE, "get", resource]
    if selector:
        args += ["-l", selector]
    try:
        result = _kubectl(*args, "-o", "json", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        items = json.loads(result.stdout).get("items")
    except json.JSONDecodeError:
        return None
    return items if isinstance(items, list) else None


def get_json_or_none(*args: str) -> tuple[str, dict | None]:
    """Returns ("found", obj) / ("not_found", None) / ("error", None) -
    NotFound is only ever concluded from kubectl's explicit NotFound
    error, never from a timeout or a generic failure."""
    try:
        result = _kubectl(*args, "-o", "json", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        return "error", None
    if result.returncode == 0:
        try:
            return "found", json.loads(result.stdout)
        except json.JSONDecodeError:
            return "error", None
    if "NotFound" in (result.stderr or "") or "not found" in (result.stderr or ""):
        return "not_found", None
    return "error", None


def pod_is_ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", []) or [])


def candidate_selector() -> str:
    return (
        "app.kubernetes.io/name=maops-kubernetes-platform,"
        f"app.kubernetes.io/instance={kube.INSTANCE_LABEL},"
        f"app.kubernetes.io/component={CANDIDATE_COMPONENT}"
    )


def pod_names(pods: list[dict]) -> frozenset:
    return frozenset(p.get("metadata", {}).get("name") for p in pods if p.get("metadata", {}).get("name"))


# --------------------------------------------------------------------------
# Candidate preflight gate
# --------------------------------------------------------------------------


@dataclass
class GateObservation:
    expected_replicas: int
    deployment: dict | None
    pods: list[dict]
    stable_pods: list[dict]
    candidate_endpoints: EndpointView | None
    stable_endpoints: EndpointView | None
    listeners: dict  # pod name -> set[int] | error string
    route: RouteSnapshot | None
    expected_route: tuple
    gateway: tuple[bool, str]
    # DAY7 image contract: day7_running_images.evaluate_component() for
    # the candidate Pods against the verified build. None = not observed
    # -> the gate refuses (fail closed).
    image_checks: list | None = None


def _pod_security_findings(pod: dict) -> list[tuple[bool, str]]:
    name = pod.get("metadata", {}).get("name", "?")
    spec = pod.get("spec", {})
    sc = spec.get("securityContext", {}) or {}
    out = [
        (spec.get("serviceAccountName") == kube.GATEWAY_SERVICE_ACCOUNT, f"{name}: serviceAccountName={spec.get('serviceAccountName')!r} (shared stable identity {kube.GATEWAY_SERVICE_ACCOUNT!r})"),
        (spec.get("automountServiceAccountToken") is False, f"{name}: automountServiceAccountToken={spec.get('automountServiceAccountToken')!r}"),
        ((sc.get("seccompProfile") or {}).get("type") == EXPECTED_SECCOMP, f"{name}: seccompProfile={sc.get('seccompProfile')!r}"),
    ]
    for key, expected in EXPECTED_POD_SECURITY.items():
        out.append((sc.get(key) == expected, f"{name}: pod securityContext.{key}={sc.get(key)!r} (expected {expected!r})"))
    containers = spec.get("containers", []) or []
    names = [c.get("name") for c in containers]
    out.append(("istio-proxy" not in names, f"{name}: no istio-proxy sidecar (containers {names})"))
    container = next((c for c in containers if c.get("name") == GATEWAY_CONTAINER), None)
    if container is None:
        out.append((False, f"{name}: container {GATEWAY_CONTAINER!r} missing"))
    else:
        csc = container.get("securityContext", {}) or {}
        out.append((csc.get("allowPrivilegeEscalation") is False, f"{name}: allowPrivilegeEscalation={csc.get('allowPrivilegeEscalation')!r}"))
        out.append((csc.get("readOnlyRootFilesystem") is True, f"{name}: readOnlyRootFilesystem={csc.get('readOnlyRootFilesystem')!r}"))
        out.append(((csc.get("capabilities") or {}).get("drop") == ["ALL"], f"{name}: capabilities.drop={(csc.get('capabilities') or {}).get('drop')!r}"))
    secret_volumes = [v for v in spec.get("volumes", []) or [] if "secret" in v]
    secret_names = sorted(v["secret"].get("secretName") for v in secret_volumes)
    out.append((secret_names == [kube.INTERNAL_SECRET], f"{name}: mounts only Secret {kube.INTERNAL_SECRET!r} (found {secret_names})"))
    out.append((all(v["secret"].get("defaultMode") == 288 for v in secret_volumes), f"{name}: Secret volume mode 0440"))
    redirection = (pod.get("metadata", {}).get("annotations") or {}).get(ambient_workload_check.REDIRECTION_ANNOTATION)
    out.append((redirection == ambient_workload_check.REDIRECTION_ENABLED, f"{name}: {ambient_workload_check.REDIRECTION_ANNOTATION}={redirection!r}"))
    return out


def evaluate_gate(obs: GateObservation) -> list[tuple[bool, str]]:
    """Pure evaluation of every promotion precondition. The candidate is
    promotable only if EVERY returned check is True."""
    checks: list[tuple[bool, str]] = []
    dep = obs.deployment
    if dep is None:
        return [(False, f"candidate Deployment {CANDIDATE_DEPLOYMENT} does not exist")]
    spec, status, meta = dep.get("spec", {}), dep.get("status", {}), dep.get("metadata", {})
    n = obs.expected_replicas
    checks.append((status.get("observedGeneration") == meta.get("generation"), f"candidate Deployment observedGeneration {status.get('observedGeneration')} == generation {meta.get('generation')}"))
    checks.append((spec.get("replicas") == n, f"candidate Deployment spec.replicas={spec.get('replicas')} (expected {n})"))
    for key in ("readyReplicas", "updatedReplicas", "availableReplicas"):
        checks.append((status.get(key) == n, f"candidate Deployment status.{key}={status.get(key)} (expected {n})"))

    live = [p for p in obs.pods if not p.get("metadata", {}).get("deletionTimestamp")]
    checks.append((len(obs.pods) == n and len(live) == n, f"candidate Pods: {len(obs.pods)} present, {len(live)} not terminating (expected exactly {n})"))
    for pod in obs.pods:
        name = pod.get("metadata", {}).get("name", "?")
        checks.append((pod.get("status", {}).get("phase") == "Running", f"{name}: phase={pod.get('status', {}).get('phase')!r}"))
        checks.append((pod_is_ready(pod), f"{name}: Ready condition True"))
        checks.extend(_pod_security_findings(pod))
        listening = obs.listeners.get(name)
        if isinstance(listening, (set, frozenset)):
            missing = [p for p in REQUIRED_LISTEN_PORTS if p not in listening]
            checks.append((not missing, f"{name}: ztunnel in-Pod listeners {list(REQUIRED_LISTEN_PORTS)} present" + (f" (MISSING {missing})" if missing else "")))
        else:
            checks.append((False, f"{name}: ambient listener probe failed closed: {listening}"))

    ready_pod_ips = {p.get("status", {}).get("podIP") for p in obs.pods if pod_is_ready(p) and p.get("status", {}).get("podIP")}
    cand_pods = pod_names(obs.pods)
    stable_pods = pod_names(obs.stable_pods)
    ce, se = obs.candidate_endpoints, obs.stable_endpoints
    if ce is None or se is None:
        checks.append((False, "candidate and stable EndpointSlices could both be read"))
    else:
        checks.append((len(ce.ready_addresses) == n and set(ce.ready_addresses) == ready_pod_ips, f"candidate Service ready endpoints {sorted(ce.ready_addresses)} == ready candidate Pod IPs {sorted(ready_pod_ips)} ({n} expected)"))
        checks.append((ce.all_target_pods <= cand_pods, f"candidate EndpointSlices reference only candidate Pods ({sorted(ce.all_target_pods)})"))
        checks.append((len(se.ready_addresses) == STABLE_EXPECTED_REPLICAS, f"stable Service still has {STABLE_EXPECTED_REPLICAS} ready endpoints (found {len(se.ready_addresses)})"))
        checks.append((se.all_target_pods <= stable_pods, f"stable EndpointSlices reference only stable Pods ({sorted(se.all_target_pods)})"))
        overlap = set(ce.ready_addresses) & set(se.ready_addresses)
        pod_overlap = set(ce.all_target_pods) & set(se.all_target_pods)
        checks.append((not overlap and not pod_overlap, f"stable and candidate ready endpoints are disjoint (address overlap {sorted(overlap)}, Pod overlap {sorted(pod_overlap)})"))
    checks.append((not (cand_pods & stable_pods), "stable and candidate Pod sets are disjoint"))
    route_ok, route_detail = route_is_current(obs.route, obs.expected_route)
    checks.append((route_ok, f"pre-promotion HTTPRoute is current and unchanged: {route_detail}"))
    checks.append(obs.gateway)
    if obs.image_checks is None:
        checks.append((False, "candidate running images were not verified against the build"))
    else:
        checks.extend(obs.image_checks)
    return checks


def observe_gate(expected_route: tuple, expected_replicas: int) -> GateObservation:
    require_day7_profile()
    status, dep = get_json_or_none("-n", kube.NAMESPACE, "get", "deployment", CANDIDATE_DEPLOYMENT)
    pods = list_json("pods", candidate_selector()) or []
    stable_pods = list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or []
    listeners: dict = {}
    for pod in pods:
        name = pod.get("metadata", {}).get("name")
        try:
            listeners[name] = ambient_workload_check.probe_listen_ports(name, GATEWAY_CONTAINER)
        except RuntimeError as exc:
            listeners[name] = str(exc)
    return GateObservation(
        image_checks=observe_candidate_images(pods, expected_replicas),
        expected_replicas=expected_replicas,
        deployment=dep if status == "found" else None,
        pods=pods,
        stable_pods=stable_pods,
        candidate_endpoints=read_endpoints(CANDIDATE_SERVICE),
        stable_endpoints=read_endpoints(STABLE_SERVICE),
        listeners=listeners,
        route=read_route(),
        expected_route=expected_route,
        gateway=gateway_is_current(),
    )


def observe_candidate_images(pods: list[dict], expected_replicas: int) -> list[tuple[bool, str]]:
    """Every candidate container runs the verified build (read-only;
    unreadable build/node state -> a failing check, never an exception)."""
    import day7_running_images  # lazy: that module imports this one

    try:
        build = active_build()
        nodes = sorted({p.get("spec", {}).get("nodeName") for p in pods if p.get("spec", {}).get("nodeName")})
        node_images = {node: day7_running_images.read_node_images(node) for node in nodes}
        return day7_running_images.evaluate_component("candidate", pods, expected_replicas, build.image("candidate"), node_images)
    except (day7_build.BuildError, RuntimeError) as exc:
        return [(False, f"candidate running images could not be verified: {exc}")]


def run_gate(expected_route: tuple, expected_replicas: int) -> tuple[bool, list[tuple[bool, str]]]:
    """Observes live state once and evaluates the gate. Prints every
    check; records nothing (the caller decides whether a refusal is the
    expected outcome)."""
    checks = evaluate_gate(observe_gate(expected_route, expected_replicas))
    for ok, msg in checks:
        print(f"    gate [{'ok' if ok else 'BLOCK'}] {msg}", flush=True)
    return all(ok for ok, _ in checks), checks


@dataclass
class Promotion:
    gate_ok: bool
    gate_checks: list
    submitted: bool
    applied_ok: bool
    # None = path checks never ran (the readiness gate refused first);
    # True/False = their enforced result.
    paths_ok: bool | None = None
    refusal: str = ""


def promote(
    rec: Recorder,
    target_stage: str,
    current_stage: str,
    apply: Callable[[Recorder, str], bool] | None = None,
    path_check: Callable[[Recorder, str], bool] | None = None,
) -> Promotion:
    """The ONLY path to a route-changing stage. Two enforced
    pre-submission conditions, in order:

      1. the candidate readiness/preflight gate (run_gate). A refusal
         here returns immediately - path probes are never run against a
         candidate that is not Ready (Canary's deliberate
         unready-candidate scenario is refused for readiness alone);
      2. the candidate path checks (check_candidate_mesh_paths: allowed
         candidate -> app, denied candidate -> state and app ->
         candidate, plus controls). A failed, inconclusive, timed-out or
         unreadable observation - or any exception while observing -
         refuses the promotion.

    Only if both pass is the traffic-moving Helm stage submitted. Every
    refusal is recorded with its reason and submits nothing."""
    require_day7_profile()
    if target_stage not in PROMOTION_STAGES:
        raise ValueError(f"{target_stage!r} is not a promotion stage")
    current_values = load_stage_values(current_stage)
    if not current_values.get("candidate", {}).get("enabled"):
        raise ValueError(f"cannot promote from {current_stage!r}: it has no candidate")
    replicas = load_stage_values(target_stage)["candidate"]["replicas"]
    gate_ok, checks = run_gate(expected_backends(current_values), replicas)
    if not gate_ok:
        blocking = [msg for ok, msg in checks if not ok]
        reason = f"readiness/preflight gate refused ({len(blocking)} blocking check(s): {blocking[:3]})"
        rec.informational(f"promotion to {target_stage} REFUSED - {reason}; no Helm stage submitted, no path probe run")
        return Promotion(False, checks, False, False, None, reason)
    try:
        paths_ok = bool((path_check or check_candidate_mesh_paths)(rec, f"pre-promotion {target_stage}"))
        path_error = ""
    except Exception as exc:  # noqa: BLE001 - an unreadable observation is a refusal, never a pass
        paths_ok = False
        path_error = f" ({type(exc).__name__}: {exc})"
        rec.record(False, f"pre-promotion {target_stage}: candidate path observation failed{path_error}")
    if not paths_ok:
        reason = f"candidate path checks failed or were inconclusive{path_error}"
        rec.record(False, f"promotion to {target_stage} REFUSED - {reason}; no Helm stage submitted")
        return Promotion(True, checks, False, False, False, reason)
    applied = (apply or apply_stage)(rec, target_stage)
    return Promotion(True, checks, True, applied, True, "")


# --------------------------------------------------------------------------
# External traffic
# --------------------------------------------------------------------------


@dataclass
class Sample:
    t: float
    path: str
    outcome: str  # ok | http_error | inconclusive
    status: int | None
    payload: dict | None
    error: str = ""


def external_get(path: str, host: str | None = None, timeout: float = EXTERNAL_TIMEOUT_SECONDS, t0: float | None = None) -> Sample:
    start = time.monotonic()
    req = urllib.request.Request(f"{kube.GATEWAY_HOST_ADDRESS}{path}", headers={"Host": host or kube.ROUTING_HOSTNAME, "Connection": "close"})
    rel = start - (t0 if t0 is not None else start)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status, outcome = resp.status, "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status, outcome = exc.code, "http_error"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Sample(rel, path, "inconclusive", None, None, f"{type(exc).__name__}: {exc}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    return Sample(rel, path, outcome, status, payload if isinstance(payload, dict) else None, "" if payload is not None else body[:200])


@dataclass(frozen=True)
class Identity:
    stable_message: str
    candidate_message: str
    stable_pods: frozenset
    candidate_pods: frozenset


def classify(sample: Sample, ident: Identity) -> str:
    """Pure. Returns stable | candidate | error | unidentified. A
    response counts as a version only when BOTH its message (for "/")
    or its backend result (for "/backend") AND the serving Pod name
    match that version's live Pod set."""
    if sample.outcome != "ok" or sample.status != 200 or sample.payload is None:
        return "error"
    p = sample.payload
    if sample.path == "/":
        msg, host = p.get("message"), p.get("hostname")
        if msg == ident.stable_message and host in ident.stable_pods:
            return "stable"
        if msg == ident.candidate_message and host in ident.candidate_pods:
            return "candidate"
        return "unidentified"
    if sample.path == "/backend":
        if p.get("backend_service") != APP_EXPECTED_SERVICE_NAME:
            return "error"
        host = p.get("gateway_hostname")
        if host in ident.stable_pods:
            return "stable"
        if host in ident.candidate_pods:
            return "candidate"
        return "unidentified"
    return "unidentified"


def classify_by_message(sample: Sample, messages: dict[str, str]) -> str:
    """Pure (Recreate): label -> message; 'error' for any non-200/non-JSON."""
    if sample.outcome != "ok" or sample.status != 200 or sample.payload is None:
        return "error"
    for label, message in messages.items():
        if sample.payload.get("message") == message:
            return label
    return "unidentified"


def sample_series(n: int, path: str, interval: float, max_seconds: float) -> list[Sample]:
    """Up to `n` independent requests (a new connection each - the
    request sets `Connection: close`), bounded by `max_seconds`."""
    samples: list[Sample] = []
    t0 = time.monotonic()
    while len(samples) < n and time.monotonic() - t0 < max_seconds:
        samples.append(external_get(path, t0=t0))
        time.sleep(interval)
    return samples


def canary_verdict(classes: list[str], requested: int) -> list[tuple[bool, str]]:
    """Pure. Both versions must be observed; zero errors/unidentified;
    every requested sample must have been taken. Never an exact-ratio
    requirement."""
    counts = Counter(classes)
    total = len(classes)
    return [
        (total == requested, f"{total}/{requested} bounded external requests completed"),
        (counts["stable"] >= 1, f"stable responses observed: {counts['stable']}"),
        (counts["candidate"] >= 1, f"candidate responses observed: {counts['candidate']}"),
        (counts["error"] == 0, f"error responses: {counts['error']}"),
        (counts["unidentified"] == 0, f"unidentified responses: {counts['unidentified']}"),
    ]


def wait_external(rec: Recorder, ident: Identity, want: str, label: str, confirm: int = 10, path: str = "/", timeout: float = EXTERNAL_CONVERGE_SECONDS) -> bool:
    """Bounded poll until `want` is observed, then requires `confirm`
    consecutive further samples that are ALL `want`."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = classify(external_get(path), ident)
        if last == want:
            break
        time.sleep(1.0)
    else:
        return rec.record(False, f"{label}: external {path} never answered as {want} within {timeout}s (last: {last})")
    classes = [classify(s, ident) for s in sample_series(confirm, path, 0.2, 60.0)]
    counts = Counter(classes)
    return rec.record(
        counts[want] == confirm == len(classes),
        f"{label}: external {path} answered as {want} and then {counts[want]}/{confirm} consecutive samples were {want} ({dict(counts)})",
    )


# --------------------------------------------------------------------------
# Mesh / NetworkPolicy path probes (run inside existing workload Pods)
# --------------------------------------------------------------------------

# DAY7 probe contract (runs INSIDE an existing workload Pod, stdlib only).
# Each phase is reported separately so an unrelated observation failure
# can never stand in for a path result:
#   1. DNS:     resolve the Service name (IPv4). Failure -> DNS_FAIL.
#               On success one MAOPS_PROBE_ADDR=<ip> line is printed.
#   2. connect: TCP connect to that address. REFUSED / RESET /
#               CONNECT_TIMEOUT / NET_ERROR (any other OSError).
#   3. HTTP:    GET /livez with the Service name as Host. STATUS:<code>
#               for ANY HTTP response; RESET (closed without a response,
#               incl. RemoteDisconnected/BrokenPipe) / REFUSED /
#               READ_TIMEOUT / PROBE_ERROR (anything else).
# Exactly one MAOPS_PROBE=<CLASS>:<detail> line is printed. kubectl exec
# failures and timeouts are classified by the caller, never by the Pod.
PROBE_SNIPPET = (
    "import socket, sys, http.client\n"
    "H, P, T = '__HOST__', __PORT__, __TIMEOUT__\n"
    "def out(kind, detail=''):\n"
    "    print('MAOPS_PROBE=' + kind + ':' + detail)\n"
    "    sys.exit(0)\n"
    "try:\n"
    "    addr = socket.getaddrinfo(H, P, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]\n"
    "except Exception as e:\n"
    "    out('DNS_FAIL', type(e).__name__)\n"
    "print('MAOPS_PROBE_ADDR=' + addr)\n"
    "c = http.client.HTTPConnection(addr, P, timeout=T)\n"
    "try:\n"
    "    c.connect()\n"
    "except ConnectionRefusedError:\n"
    "    out('REFUSED', 'connect')\n"
    "except ConnectionResetError:\n"
    "    out('RESET', 'connect')\n"
    "except (socket.timeout, TimeoutError):\n"
    "    out('CONNECT_TIMEOUT', str(T))\n"
    "except OSError as e:\n"
    "    out('NET_ERROR', type(e).__name__ + ':' + str(e.errno))\n"
    "try:\n"
    "    c.request('GET', '/livez', headers={'Host': H})\n"
    "    r = c.getresponse()\n"
    "    out('STATUS', str(r.status))\n"
    "except (socket.timeout, TimeoutError):\n"
    "    out('READ_TIMEOUT', str(T))\n"
    "except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected):\n"
    "    out('RESET', 'request')\n"
    "except ConnectionRefusedError:\n"
    "    out('REFUSED', 'request')\n"
    "except Exception as e:\n"
    "    out('PROBE_ERROR', type(e).__name__)\n"
)

PROBE_KINDS = {"STATUS", "DNS_FAIL", "REFUSED", "RESET", "CONNECT_TIMEOUT", "READ_TIMEOUT", "NET_ERROR", "PROBE_ERROR"}
# Transport outcomes that mean "the destination produced NO HTTP
# response". They count as negative-reachability evidence ONLY together
# with a verified destination and a passing positive control from the
# same source Pod (see path_verdict). A timeout is included deliberately:
# a NetworkPolicy drop surfaces as a timeout, and treating every timeout
# as inconclusive would make that denial unobservable. None of these
# identify WHICH layer (NetworkPolicy, ztunnel/AuthorizationPolicy)
# denied the traffic.
NO_HTTP_RESPONSE_KINDS = {"reset", "refused", "connect_timeout", "read_timeout"}
# Observation failures: never evidence of anything; always refuse.
INCONCLUSIVE_KINDS = {"dns_fail", "net_error", "probe_error", "malformed", "exec_failed", "exec_timeout"}


@dataclass(frozen=True)
class ProbeResult:
    kind: str       # lower-case PROBE_KINDS member, or malformed / exec_failed / exec_timeout
    detail: str
    addr: str | None = None


def probe_snippet(host: str, port: int = BACKEND_PORT) -> str:
    return PROBE_SNIPPET.replace("__HOST__", host).replace("__PORT__", str(port)).replace("__TIMEOUT__", str(PROBE_HTTP_TIMEOUT_SECONDS))


def parse_probe_output(output: str) -> ProbeResult:
    """Pure. Requires exactly one MAOPS_PROBE line with a known class; a
    MAOPS_PROBE_ADDR line is required whenever DNS succeeded (every class
    except DNS_FAIL) and forbidden otherwise. Anything else is
    'malformed' (inconclusive)."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    results = [l for l in lines if l.startswith("MAOPS_PROBE=")]
    addrs = [l for l in lines if l.startswith("MAOPS_PROBE_ADDR=")]
    if len(results) != 1 or len(addrs) > 1:
        return ProbeResult("malformed", (output or "")[:200])
    kind, _, detail = results[0].split("=", 1)[1].partition(":")
    if kind not in PROBE_KINDS:
        return ProbeResult("malformed", results[0][:200])
    addr = addrs[0].split("=", 1)[1] if addrs else None
    if (kind == "DNS_FAIL") != (addr is None) or (addr is not None and not addr):
        return ProbeResult("malformed", f"{results[0]} with addr {addr!r}")
    if kind == "STATUS" and not detail.isdigit():
        return ProbeResult("malformed", results[0][:200])
    return ProbeResult(kind.lower(), detail, addr)


def exec_probe(pod: str, container: str, host: str) -> ProbeResult:
    try:
        result = _kubectl(
            "-n", kube.NAMESPACE, "exec", pod, "-c", container, "--", ambient_workload_check.PYTHON, "-c", probe_snippet(host),
            check=False, timeout=kube.subprocess_timeout_for(PROBE_HTTP_TIMEOUT_SECONDS),
        )
    except subprocess.TimeoutExpired:
        return ProbeResult("exec_timeout", "kubectl exec timed out")
    if result.returncode != 0:
        return ProbeResult("exec_failed", f"kubectl exec exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
    return parse_probe_output(result.stdout)


@dataclass(frozen=True)
class Destination:
    service: str
    cluster_ip: str | None
    ready_endpoints: int | None

    @property
    def verified(self) -> bool:
        return bool(self.cluster_ip) and self.cluster_ip != "None" and isinstance(self.ready_endpoints, int) and self.ready_endpoints >= 1


def read_destination(service: str) -> Destination:
    """Live Service ClusterIP + ready EndpointSlice count. Unreadable ->
    None fields -> not verified (refuses)."""
    state, svc = get_json_or_none("-n", kube.NAMESPACE, "get", "service", service)
    ep = read_endpoints(service)
    cluster_ip = (svc or {}).get("spec", {}).get("clusterIP") if state == "found" else None
    return Destination(service, cluster_ip, None if ep is None else len(ep.ready_addresses))


def path_verdict(result: ProbeResult, expect_allowed: bool, dest: Destination, control_ok: bool | None = None) -> tuple[bool, str]:
    """Pure. The fail-closed decision for one path observation.

    Always required: the destination Service verified live (ClusterIP
    readable, >=1 ready endpoint) and - once DNS resolved - that it
    resolved to exactly that ClusterIP. Any observation failure (DNS,
    malformed output, exec failure/timeout, other network/probe errors)
    is inconclusive and fails.
    Allowed path: an HTTP 200.
    Denied path: NO HTTP response at all (reset, refused, connect or read
    timeout) AND a passing positive control from the same source Pod in
    the same observation (so the source's own networking demonstrably
    worked). Any HTTP status from the destination fails: it answered."""
    if not dest.verified:
        return False, f"inconclusive: destination {dest.service} not verified live (clusterIP {dest.cluster_ip!r}, ready endpoints {dest.ready_endpoints!r})"
    if result.kind in INCONCLUSIVE_KINDS:
        return False, f"inconclusive: {result.kind}:{result.detail}"
    if result.addr != dest.cluster_ip:
        return False, f"inconclusive: {dest.service} resolved to {result.addr!r}, not its live ClusterIP {dest.cluster_ip!r}"
    if expect_allowed:
        ok = result.kind == "status" and result.detail == "200"
        return ok, f"{'HTTP 200' if ok else 'expected HTTP 200'} (observed {result.kind}:{result.detail})"
    if result.kind == "status":
        return False, f"destination ANSWERED with HTTP {result.detail} - not denied"
    if result.kind in NO_HTTP_RESPONSE_KINDS:
        if control_ok is not True:
            return False, f"inconclusive: no HTTP response ({result.kind}) but the same source's positive control did not pass"
        return True, f"no HTTP response ({result.kind}:{result.detail}) from a verified, ready destination while the same source's positive control passed - denied at some layer (not identified)"
    return False, f"inconclusive: unexpected probe class {result.kind}"


# (source role, destination Service, expect_allowed, description). Positive
# controls come first for each source; negatives are judged against them.
PATH_PLAN = (
    ("candidate", "maops-app", True, "candidate -> app (allowed: candidate egress NetworkPolicy + maops-app-authz admits the shared maops-gateway principal)"),
    ("app", "maops-state", True, "control: app -> state (allowed)"),
    ("candidate", "maops-state", False, "candidate -> state (denied: no egress allow; maops-state-authz admits only maops-app)"),
    ("app", CANDIDATE_SERVICE, False, "app -> candidate (denied: maops-gateway-candidate-authz admits only the ingress Gateway principal)"),
    ("app", STABLE_SERVICE, False, "parity control: app -> stable gateway (denied, same as the candidate)"),
)


def check_candidate_mesh_paths(rec: Recorder, label: str) -> bool:
    """Runs PATH_PLAN from one Ready candidate Pod and one Ready app Pod.
    Fails closed on unreadable Pods, any unverified destination, any
    inconclusive observation, a denied path that answers, or a denied
    path whose source's positive control did not pass."""
    require_day7_profile()
    cand_all, app_all = list_json("pods", candidate_selector()), list_json("pods", kube.APP_LABEL_SELECTOR)
    if cand_all is None or app_all is None:
        return rec.record(False, f"{label}: candidate/app Pods could not be listed - path observation unreadable")
    cand = [p for p in cand_all if pod_is_ready(p)]
    app = [p for p in app_all if pod_is_ready(p)]
    if not cand or not app:
        return rec.record(False, f"{label}: need a Ready candidate Pod and a Ready app Pod for path probes (found {len(cand)}/{len(app)})")
    sources = {"candidate": (cand[0]["metadata"]["name"], GATEWAY_CONTAINER), "app": (app[0]["metadata"]["name"], "maops-app")}
    destinations = {svc: read_destination(svc) for _, svc, _, _ in PATH_PLAN}
    ok = True
    for svc, dest in destinations.items():
        ok = rec.record(dest.verified, f"{label}: destination {svc} verified live (clusterIP {dest.cluster_ip!r}, ready endpoints {dest.ready_endpoints!r})") and ok
    control_ok: dict[str, bool] = {}
    # Positive controls are ALWAYS evaluated before any negative path of
    # the same source, regardless of how PATH_PLAN is ordered (stable sort:
    # allowed entries first), so a denied verdict can never be judged
    # against a control that has not run yet.
    for role, svc, allowed, desc in sorted(PATH_PLAN, key=lambda entry: not entry[2]):
        pod, container = sources[role]
        result = exec_probe(pod, container, svc)
        passed, why = path_verdict(result, allowed, destinations[svc], control_ok.get(role))
        if allowed:
            control_ok[role] = control_ok.get(role, True) and passed
        ok = rec.record(passed, f"{label}: {desc} - {why}") and ok
    return ok


# --------------------------------------------------------------------------
# Restoration and cleanup proof
# --------------------------------------------------------------------------


def candidate_leftovers() -> tuple[list[str], list[str]]:
    """(present, unknown). Every candidate-only object must be explicitly
    absent; an API error is 'unknown', never 'absent'."""
    present, unknown = [], []
    for resource in ("deployments", "replicasets", "pods", "services", "configmaps", "networkpolicies"):
        items = list_json(resource, CANDIDATE_COMPONENT_SELECTOR)
        if items is None:
            unknown.append(resource)
        else:
            present.extend(f"{resource}/{i['metadata']['name']}" for i in items)
    slices = list_json("endpointslices", f"kubernetes.io/service-name={CANDIDATE_SERVICE}")
    if slices is None:
        unknown.append("endpointslices")
    else:
        present.extend(f"endpointslices/{i['metadata']['name']}" for i in slices)
    for kind, name in (("authorizationpolicy.security.istio.io", CANDIDATE_AUTHZ), *(("networkpolicy", n) for n in CANDIDATE_NETWORK_POLICIES), ("service", CANDIDATE_SERVICE), ("configmap", CANDIDATE_CONFIGMAP), ("deployment", CANDIDATE_DEPLOYMENT)):
        state, _ = get_json_or_none("-n", kube.NAMESPACE, "get", kind, name)
        if state == "found":
            present.append(f"{kind}/{name}")
        elif state == "error":
            unknown.append(f"{kind}/{name}")
    return sorted(set(present)), sorted(set(unknown))


def wait_candidate_absent(rec: Recorder, label: str, timeout: float = CANDIDATE_ABSENT_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    present, unknown = [], []
    while time.monotonic() < deadline:
        present, unknown = candidate_leftovers()
        if not present and not unknown:
            return rec.record(True, f"{label}: no candidate-only object remains (explicitly absent: Deployment/ReplicaSets/Pods/Service/EndpointSlices/ConfigMap/NetworkPolicies/AuthorizationPolicy)")
        time.sleep(3.0)
    return rec.record(False, f"{label}: candidate leftovers after {timeout}s - present {present}, undetermined {unknown}")


def stable_identity(stable_message: str) -> Identity:
    return Identity(stable_message, "", pod_names(list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or []), frozenset())


def verify_stable(rec: Recorder, stable_message: str, label: str) -> bool:
    """Read-only proof of the stable state: Helm values are the stable
    stage plus the verified build's image tags, no candidate-only object
    exists, every stable container runs that build (and no candidate
    Pod exists), the route is 100% stable and current, the Gateway is
    current, and external requests are served by stable Pods with the
    stable message."""
    try:
        build = active_build()
        expected = expected_release_values("stable", build)
    except day7_build.BuildError as exc:
        build, expected = None, None
        rec.record(False, f"{label}: verified build unreadable ({exc})")
    values = helm_values()
    ok = rec.record(expected is not None and values == expected, f"{label}: `helm get values` equals helm-values/day7/stable.yaml + the verified build's image tags" + ("" if expected is not None and values == expected else f" - found {values!r}"))
    ok = wait_candidate_absent(rec, label) and ok
    if build is not None:
        import day7_running_images  # lazy: that module imports this one

        ok = day7_running_images.wait_for_running_images(rec, build, label=f"{label}: running images") and ok
    else:
        ok = False
    ok = (wait_route(rec, expected_backends(load_stage_values("stable")), label) is not None) and ok
    gw_ok, gw_detail = gateway_is_current()
    ok = rec.record(gw_ok, f"{label}: {gw_detail}") and ok
    ok = wait_external(rec, stable_identity(stable_message), "stable", label, confirm=10) and ok
    return ok


def restore_stable(rec: Recorder, stable_message: str, label: str = "RESTORATION") -> bool:
    """Applies the stable stage, then VERIFIES the restored state with
    verify_stable(); every step runs even if an earlier one failed, so
    the report shows the real end state."""
    rec.section(f"{label}: back to 100% stable, candidate disabled")
    applied = apply_stage(rec, "stable", label=label)
    return verify_stable(rec, stable_message, label) and applied


def restore_or_verify(rec: Recorder, stable_message: str) -> bool:
    """finally-hook for every experiment: FIRST reap any child process
    this experiment still owns (a late Helm upgrade must never be able to
    change the release after restoration), then restore if this process
    ever submitted a stage, otherwise only verify (no mutation at all)."""
    reaped_ok = reap_owned_children(rec)
    if SUBMITTED_STAGES:
        return restore_stable(rec, stable_message) and reaped_ok
    rec.section("RESTORATION: no stage was submitted - verifying the stable state read-only")
    return verify_stable(rec, stable_message, "RESTORATION (verify only)") and reaped_ok


# --------------------------------------------------------------------------
# Baseline access (never recaptured here)
# --------------------------------------------------------------------------


def load_strategy_baseline() -> dict:
    """Loads the Day 7 strategy baseline captured by `make day7-baseline`.
    Raises RuntimeError if it is missing/untrusted/for another run - it
    is NEVER recaptured by an experiment or the final check."""
    path = os.environ.get(STRATEGY_BASELINE_PATH_ENV)
    run_id = os.environ.get(RUN_ID_ENV)
    if not path or not run_id:
        raise RuntimeError(f"{STRATEGY_BASELINE_PATH_ENV}/{RUN_ID_ENV} not set - run Day 7 experiments through the Makefile after `make day7-baseline`")
    try:
        private_run_dir.validate_private_file(path)
    except private_run_dir.PrivateRunDirError as exc:
        raise RuntimeError(f"Day 7 strategy baseline is missing or untrusted (never recaptured): {exc}") from exc
    with open(path) as f:
        data = json.load(f)
    if data.get("run_id") != run_id:
        raise RuntimeError(f"strategy baseline run_id {data.get('run_id')!r} != this run {run_id!r}")
    if data.get("context") != kube.CONTEXT or data.get("release") != kube.HELM_RELEASE_NAME:
        raise RuntimeError(f"strategy baseline is for {data.get('context')}/{data.get('release')}, not {kube.CONTEXT}/{kube.HELM_RELEASE_NAME}")
    require_baseline_build(data)
    return data


def require_baseline_build(baseline: dict) -> day7_build.Build:
    """Every stage of a run carries the build its baseline was captured
    under. A baseline without a build record (captured before the image
    contract existed) or for a different build than the current one is
    refused: a new build needs a NEW run ID and new baselines."""
    recorded = baseline.get("build")
    if not isinstance(recorded, dict):
        raise RuntimeError("strategy baseline has no verified build record (captured before the Day 7 image contract) - capture a NEW run's baselines")
    try:
        build = active_build()
        baseline_build = day7_build.build_from_record(recorded)
    except day7_build.BuildError as exc:
        raise RuntimeError(f"verified build unreadable: {exc}") from exc
    if baseline_build != build:
        raise RuntimeError(f"strategy baseline was captured under build {baseline_build.build_id}, the current verified build is {build.build_id} - capture a NEW run's baselines")
    return build


# --------------------------------------------------------------------------
# Experiment runner
# --------------------------------------------------------------------------


def run_experiment(rec: Recorder, title: str, body: Callable[[], bool], restore: Callable[[], bool]) -> int:
    """Runs `body`, then ALWAYS `restore` (even after an exception or
    Ctrl-C in `body`). Reports the two outcomes separately; the exit
    status is 0 only if both succeeded and no finding failed."""
    primary_ok = False
    restored_ok = False
    interrupted = False
    try:
        primary_ok = bool(body())
    except KeyboardInterrupt:
        interrupted = True
        rec.record(False, f"{title}: PRIMARY sequence interrupted - restoration still runs")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        traceback.print_exc()
        rec.record(False, f"{title}: PRIMARY sequence aborted by {type(exc).__name__}: {exc}")
    finally:
        try:
            restored_ok = bool(restore())
        except BaseException as exc:  # noqa: BLE001 - restoration failure must be reported, not lost
            traceback.print_exc()
            rec.record(False, f"{title}: RESTORATION aborted by {type(exc).__name__}: {exc} - the cluster may NOT be in the stable state")
            restored_ok = False
    failures = rec.failures()
    print()
    print(f"{len(rec.results) - len(failures)}/{len(rec.results)} {title} checks passed")
    print(f"PRIMARY: {'PASS' if primary_ok and not interrupted else 'FAIL'}")
    print(f"RESTORATION: {'PASS - stable state verified' if restored_ok else 'FAIL - stable state NOT verified'}")
    if failures or not primary_ok or not restored_ok or interrupted:
        print(f"FAIL: {title} ({len(failures)} failed check(s))", file=sys.stderr)
        return 130 if interrupted else 1
    print(f"PASS: {title} completed and the stable state was restored and verified")
    return 0


# --------------------------------------------------------------------------
# Owned child processes (Recreate's concurrent Helm upgrade)
# --------------------------------------------------------------------------

# Every child process an experiment starts itself (not via _run) is
# registered here until it has been reaped, so restore_or_verify() can
# refuse to restore while one might still be changing the release.
OWNED_CHILDREN: list = []
CHILD_TERMINATE_GRACE_SECONDS = 15.0
CHILD_OUTPUT_TAIL_BYTES = 2000


def reap_child(proc, grace: float = CHILD_TERMINATE_GRACE_SECONDS) -> str:
    """Terminates (then kills) a still-running child and ALWAYS waits for
    it, so it is reaped and can no longer act. A Ctrl-C while waiting
    escalates to kill, reaps, and re-raises. Returns what happened."""
    if proc.poll() is not None:
        proc.wait()
        outcome = f"already exited ({proc.returncode})"
    else:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
                outcome = f"terminated ({proc.returncode})"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                outcome = f"killed after {grace:.0f}s ({proc.returncode})"
        except KeyboardInterrupt:
            proc.kill()
            proc.wait()
            if proc in OWNED_CHILDREN:
                OWNED_CHILDREN.remove(proc)
            raise
    if proc in OWNED_CHILDREN:
        OWNED_CHILDREN.remove(proc)
    return outcome


def reap_owned_children(rec: Recorder) -> bool:
    """Reaps every registered child. A child that was still running when
    restoration began is a failure of the experiment's own lifecycle
    (recorded), even though it is now stopped."""
    ok = True
    for proc in list(OWNED_CHILDREN):
        was_running = proc.poll() is None
        outcome = reap_child(proc)
        ok = rec.record(not was_running, f"owned child pid {getattr(proc, 'pid', '?')} reaped before restoration: {outcome}") and ok
    return ok


def read_tail(fileobj, limit: int = CHILD_OUTPUT_TAIL_BYTES) -> str:
    """Last `limit` bytes of a private, file-backed output sink (never a
    pipe - a pipe nobody drains can block the child)."""
    try:
        fileobj.flush()
        fileobj.seek(0, 2)
        size = fileobj.tell()
        fileobj.seek(max(0, size - limit))
        data = fileobj.read()
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)


# --------------------------------------------------------------------------
# Background external prober (Recreate)
# --------------------------------------------------------------------------


@dataclass
class Prober:
    """Samples `path` every `interval` seconds on a daemon thread until
    stop() - bounded by `max_seconds` regardless."""

    path: str = "/"
    interval: float = 0.25
    timeout: float = 2.0
    max_seconds: float = 420.0
    samples: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.t0 = time.monotonic()

    def _loop(self) -> None:
        while not self._stop.is_set() and time.monotonic() - self.t0 < self.max_seconds:
            self.samples.append(external_get(self.path, timeout=self.timeout, t0=self.t0))
            self._stop.wait(self.interval)

    def start(self) -> "Prober":
        self._thread.start()
        return self

    def stop(self) -> list:
        self._stop.set()
        self._thread.join(timeout=self.timeout + 5)
        return list(self.samples)

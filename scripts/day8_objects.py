#!/usr/bin/env python3
"""
DAY8: every Kubernetes object the Day 8 autoscaling demonstrations
create - generated here, in one place, as plain dicts - plus the
namespace resource budget derived from THE SAME numbers.

Pure and cluster-free: nothing in this module runs kubectl, Docker or
Helm, so `make day8-static-check` and the unit tests can prove the
design without a cluster:

  - ONE temporary namespace (`maops-day8-scaling`), Pod Security
    `restricted`, carrying a Day 8 identity (never day4/day6/day7).
  - Three targets, ONE scaling controller each:
        day8-hpa-target    <- HorizontalPodAutoscaler (CPU, 1..4)
        day8-vpa-target    <- VerticalPodAutoscaler (Off, then Initial)
        day8-queue-worker  <- KEDA ScaledObject (Redis list, 0..3)
    `scaler_conflicts()` refuses any target with more than one.
  - Support Pods: the bounded load Job, the disposable Redis queue, the
    producer Job, and the quota proof's in-budget probe Pod.
  - A LimitRange (per-container min/max/defaults) and a ResourceQuota
    whose hard limits are `budget()` - the worst case of every Pod at its
    maximum intended replica count, VPA targets at their VPA maximum
    (with limits scaled proportionally, as VPA does), all at once. The one
    deliberate allowance is the quota-proof probe Pod (50m/32Mi requests),
    which never runs concurrently with the demonstrations - it is counted
    so the quota proof itself is in budget. Otherwise nothing is padded:
    a quota-blocked expected scale-out is a design failure the live run
    reports, never something the quota is quietly widened for.

`python3 scripts/day8_objects.py render IMAGE` prints the objects as a
JSON List (what the live scripts apply); `budget` prints the budget.
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field

NAMESPACE = "maops-day8-scaling"
INSTANCE = "maops-kubernetes-platform-day8"
PART_OF = "maops-kubernetes-platform"
NAMESPACE_COMPONENT = "day8-scaling"
MANAGED_BY = "maops-day8"
CONTAINER = "scaling"
PORT = 8080

# The disposable queue. Pinned by digest (multi-arch index of the official
# image); runs as the image's own non-root redis user (uid 999, gid 1000).
REDIS_IMAGE = "redis:8.10.2-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0"
REDIS_UID = 999
REDIS_GID = 1000
QUEUE_LIST = "day8:jobs"
KEDA_NAMESPACE = "keda"
# Day 8 creates the `keda` namespace itself (never `helm --create-namespace`)
# with these identity labels, and marks its KEDA Helm release with
# KEDA_OWNER_LABEL; it never upgrades, uninstalls or deletes a release or
# namespace without them.
KEDA_NAMESPACE_COMPONENT = "day8-keda"
KEDA_OWNER_LABEL = ("maops-day8-owner", INSTANCE)
SCALING_SERVICE_ACCOUNT = "day8-scaling"

SCALING_IMAGE_REPOSITORY = "maops-kubernetes-scaling"
_SCALING_TAG_RE = re.compile(r"^maops-kubernetes-scaling:day8-cfg-[0-9a-f]{64}$")

HPA_TARGET = "day8-hpa-target"
HPA_LOAD = "day8-hpa-load"
VPA_TARGET = "day8-vpa-target"
QUEUE = "day8-queue"
WORKER = "day8-queue-worker"
PRODUCER = "day8-queue-producer"
QUOTA_PROBE = "day8-quota-in-budget"
LIMITRANGE = "day8-scaling-limits"
QUOTA = "day8-scaling-budget"
QUEUE_POLICY = "day8-queue-ingress"

# --- demonstration bounds (each one is asserted by the live scripts) -----
HPA_MIN, HPA_MAX, HPA_CPU_UTILIZATION = 1, 4, 50
HPA_SCALE_DOWN_WINDOW_SECONDS = 30
LOAD_SECONDS, LOAD_CONCURRENCY, LOAD_WORK_MS = 150, 4, 50
VPA_REPLICAS_AFTER_INITIAL = 2
KEDA_MIN, KEDA_MAX = 0, 3
KEDA_LIST_LENGTH_TARGET = 5
KEDA_POLLING_SECONDS, KEDA_COOLDOWN_SECONDS = 5, 30
PRODUCER_ITEMS, WORKER_PROCESS_MS = 60, 2000


@dataclass(frozen=True)
class Resources:
    cpu_request: str
    memory_request: str
    cpu_limit: str
    memory_limit: str

    def as_k8s(self) -> dict:
        return {
            "requests": {"cpu": self.cpu_request, "memory": self.memory_request},
            "limits": {"cpu": self.cpu_limit, "memory": self.memory_limit},
        }


# --- LimitRange (per container) -----------------------------------------
LIMIT_MIN = {"cpu": "10m", "memory": "16Mi"}
LIMIT_MAX = {"cpu": "250m", "memory": "256Mi"}
LIMIT_DEFAULT = {"cpu": "100m", "memory": "64Mi"}  # default limits
LIMIT_DEFAULT_REQUEST = {"cpu": "50m", "memory": "32Mi"}
DEFAULTED = Resources(LIMIT_DEFAULT_REQUEST["cpu"], LIMIT_DEFAULT_REQUEST["memory"], LIMIT_DEFAULT["cpu"], LIMIT_DEFAULT["memory"])

# --- VPA bounds -----------------------------------------------------------
# minAllowed memory sits ABOVE the target's declared 32Mi request, so every
# valid recommendation (VPA caps it into [minAllowed, maxAllowed] in both
# the recommender and the admission controller) differs from the template.
# A cold-start recommendation sits at the recommender floor
# (--pod-recommendation-min-memory-mb=32, i.e. the declared 32Mi); with
# minAllowed 32Mi it was admitted unchanged (run f837802b, VPA 16/17).
# `vpa_change_problems()` keeps this from regressing.
VPA_MIN_ALLOWED = {"cpu": "10m", "memory": "48Mi"}
VPA_MAX_ALLOWED = {"cpu": "40m", "memory": "96Mi"}


@dataclass(frozen=True)
class PodBudget:
    """One kind of Pod the namespace may hold: how many at most, at what
    size. `vpa` = its requests may be raised up to VPA_MAX_ALLOWED (limits
    proportionally)."""

    name: str
    max_pods: int
    resources: Resources
    scaler: str | None = None
    vpa: bool = False
    notes: str = ""


WORKLOADS: tuple[PodBudget, ...] = (
    PodBudget(HPA_TARGET, HPA_MAX, Resources("100m", "32Mi", "200m", "64Mi"), scaler="hpa", notes="HPA maxReplicas"),
    PodBudget(HPA_LOAD, 1, Resources("50m", "32Mi", "200m", "64Mi"), notes="bounded load Job, 1 Pod, backoffLimit 0"),
    PodBudget(VPA_TARGET, VPA_REPLICAS_AFTER_INITIAL, Resources("10m", "32Mi", "50m", "64Mi"), scaler="vpa", vpa=True, notes="1 Pod, then 1 more created under Initial"),
    PodBudget(QUEUE, 1, Resources("50m", "32Mi", "100m", "64Mi"), notes="disposable Redis, no persistence"),
    PodBudget(WORKER, KEDA_MAX, Resources("20m", "32Mi", "100m", "64Mi"), scaler="keda", notes="ScaledObject maxReplicaCount"),
    PodBudget(PRODUCER, 1, Resources("20m", "32Mi", "100m", "64Mi"), notes="producer Job, 1 Pod, backoffLimit 0"),
    PodBudget(QUOTA_PROBE, 1, DEFAULTED, notes="in-budget probe Pod, LimitRange defaults"),
)


# --------------------------------------------------------------------------
# Quantities
# --------------------------------------------------------------------------

_CPU_RE = re.compile(r"^(\d+)(m?)$")
_MEM_RE = re.compile(r"^(\d+)(Ki|Mi|Gi)?$")


def cpu_millis(quantity: str) -> int:
    m = _CPU_RE.match(str(quantity))
    if not m:
        raise ValueError(f"unsupported CPU quantity {quantity!r} (expected <n>m or <n>)")
    return int(m.group(1)) if m.group(2) else int(m.group(1)) * 1000


def memory_bytes(quantity: str) -> int:
    m = _MEM_RE.match(str(quantity))
    if not m:
        raise ValueError(f"unsupported memory quantity {quantity!r} (expected <n>, <n>Ki, <n>Mi or <n>Gi)")
    return int(m.group(1)) * {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[m.group(2)]


def fmt_cpu(millis: int) -> str:
    return f"{millis}m"


def fmt_memory(num_bytes: int) -> str:
    mib = num_bytes / 1024**2
    if mib != int(mib):
        raise ValueError(f"{num_bytes} bytes is not a whole number of MiB")
    return f"{int(mib)}Mi"


_QUANTITY = (
    ("cpu", "cpu_request", "cpu_limit", cpu_millis, fmt_cpu),
    ("memory", "memory_request", "memory_limit", memory_bytes, fmt_memory),
)


def _vpa_scaled(resources: Resources, bound: dict) -> Resources:
    """Pod resources when VPA (controlledValues RequestsAndLimits) sets each
    request to `bound` - limits scaled by the original limit/request ratio,
    VPA's proportional limit behaviour."""
    out = {}
    for res, req_key, lim_key, parse, fmt in _QUANTITY:
        request, limit = parse(getattr(resources, req_key)), parse(getattr(resources, lim_key))
        new_request = parse(bound[res])
        out[req_key] = fmt(new_request)
        out[lim_key] = fmt(math.ceil(limit * new_request / request))
    return Resources(**out)


def vpa_ceiling(resources: Resources) -> Resources:
    """The largest Pod resources VPA can produce: requests raised to
    VPA_MAX_ALLOWED (never below the declared request), limits scaled
    proportionally."""
    bound = {}
    for res, req_key, _, parse, _ in _QUANTITY:
        declared = getattr(resources, req_key)
        bound[res] = declared if parse(declared) > parse(VPA_MAX_ALLOWED[res]) else VPA_MAX_ALLOWED[res]
    return _vpa_scaled(resources, bound)


def vpa_floor(resources: Resources) -> Resources:
    """The smallest Pod resources VPA can admit: requests at VPA_MIN_ALLOWED,
    limits scaled proportionally."""
    return _vpa_scaled(resources, VPA_MIN_ALLOWED)


def vpa_change_problems(workload: PodBudget, min_allowed: dict | None = None, max_allowed: dict | None = None) -> list[str]:
    """A VPA target must be unable to receive a recommendation equal to BOTH
    declared requests - otherwise a valid recommendation (e.g. a cold start
    at minAllowed) is admitted with the template's own resources and the
    demonstration cannot show VPA changed anything. Sound when, for at least
    one resource, the declared request lies outside [minAllowed, maxAllowed]."""
    lo, hi = min_allowed or VPA_MIN_ALLOWED, max_allowed or VPA_MAX_ALLOWED
    reachable = []
    for res, req_key, _, parse, _ in _QUANTITY:
        declared = parse(getattr(workload.resources, req_key))
        if parse(lo[res]) > parse(hi[res]):
            return [f"{workload.name}: VPA minAllowed {res} {lo[res]} above maxAllowed {hi[res]}"]
        if parse(lo[res]) <= declared <= parse(hi[res]):
            reachable.append(f"{res} {getattr(workload.resources, req_key)}")
    if len(reachable) == len(_QUANTITY):
        return [
            f"{workload.name}: a valid VPA recommendation (minAllowed {lo}, maxAllowed {hi}) can equal both declared requests "
            f"({', '.join(reachable)}) - an admitted Pod would be indistinguishable from the template"
        ]
    return []


def worst_case(workload: PodBudget) -> Resources:
    return vpa_ceiling(workload.resources) if workload.vpa else workload.resources


@dataclass(frozen=True)
class Budget:
    requests_cpu_m: int
    requests_memory: int
    limits_cpu_m: int
    limits_memory: int
    pods: int
    rows: tuple = field(default=())

    def hard(self) -> dict:
        return {
            "requests.cpu": fmt_cpu(self.requests_cpu_m),
            "requests.memory": fmt_memory(self.requests_memory),
            "limits.cpu": fmt_cpu(self.limits_cpu_m),
            "limits.memory": fmt_memory(self.limits_memory),
            "pods": str(self.pods),
        }


def budget(workloads: tuple[PodBudget, ...] = WORKLOADS) -> Budget:
    """Worst case: every Pod kind at its maximum count and maximum size,
    simultaneously."""
    rq_cpu = rq_mem = lim_cpu = lim_mem = pods = 0
    rows = []
    for w in workloads:
        r = worst_case(w)
        rq_cpu += w.max_pods * cpu_millis(r.cpu_request)
        rq_mem += w.max_pods * memory_bytes(r.memory_request)
        lim_cpu += w.max_pods * cpu_millis(r.cpu_limit)
        lim_mem += w.max_pods * memory_bytes(r.memory_limit)
        pods += w.max_pods
        rows.append((w.name, w.max_pods, r.cpu_request, r.memory_request, r.cpu_limit, r.memory_limit, w.notes))
    return Budget(rq_cpu, rq_mem, lim_cpu, lim_mem, pods, tuple(rows))


# --------------------------------------------------------------------------
# Static design checks (also run live, before anything is applied)
# --------------------------------------------------------------------------


def within_limitrange(resources: Resources) -> list[str]:
    problems = []
    for res, req, lim, parse in (
        ("cpu", resources.cpu_request, resources.cpu_limit, cpu_millis),
        ("memory", resources.memory_request, resources.memory_limit, memory_bytes),
    ):
        if parse(req) < parse(LIMIT_MIN[res]):
            problems.append(f"{res} request {req} below LimitRange min {LIMIT_MIN[res]}")
        if parse(lim) > parse(LIMIT_MAX[res]):
            problems.append(f"{res} limit {lim} above LimitRange max {LIMIT_MAX[res]}")
        if parse(req) > parse(lim):
            problems.append(f"{res} request {req} above its limit {lim}")
    return problems


def design_problems(workloads: tuple[PodBudget, ...] = WORKLOADS) -> list[str]:
    """Every reason this design could not run as intended - empty when sound."""
    problems = []
    for w in workloads:
        sizes = [(w.resources, "declared"), (worst_case(w), "worst-case")]
        if w.vpa:
            sizes.append((vpa_floor(w.resources), "VPA minimum"))
            problems += vpa_change_problems(w)
        for size, label in sizes:
            problems += [f"{w.name} ({label}): {p}" for p in within_limitrange(size)]
        if w.max_pods < 1:
            problems.append(f"{w.name}: max_pods must be >= 1")
    problems += scaler_conflicts(scaling_objects(placeholder_image()))
    return problems


def scaler_conflicts(objects: list[dict]) -> list[str]:
    """One scaling controller per target: HPAs (including the one KEDA
    creates from a ScaledObject), VPAs and ScaledObjects may never share a
    target, and none may target anything outside the scaling namespace's
    own Deployments."""
    claims: dict[tuple[str, str], list[str]] = {}
    for obj in objects:
        kind = obj.get("kind")
        ref = None
        if kind == "HorizontalPodAutoscaler":
            ref = obj["spec"]["scaleTargetRef"]
        elif kind == "VerticalPodAutoscaler":
            ref = obj["spec"]["targetRef"]
        elif kind == "ScaledObject":
            ref = {"kind": obj["spec"]["scaleTargetRef"].get("kind", "Deployment"), "name": obj["spec"]["scaleTargetRef"]["name"]}
        if ref is None:
            continue
        namespace = obj.get("metadata", {}).get("namespace")
        if namespace != NAMESPACE:
            claims.setdefault(("outside", str(namespace)), []).append(f"{kind}/{obj['metadata']['name']}")
        claims.setdefault((ref.get("kind", ""), ref["name"]), []).append(f"{kind}/{obj['metadata']['name']}")
    problems = []
    for (kind, name), owners in sorted(claims.items()):
        if kind == "outside":
            problems.append(f"scaler(s) {owners} outside {NAMESPACE} (namespace {name!r})")
        elif kind != "Deployment":
            problems.append(f"{owners} target {kind}/{name} - only Deployments in {NAMESPACE} are scaled")
        elif len(owners) > 1:
            problems.append(f"Deployment/{name} has {len(owners)} scaling controllers {owners} - exactly one is allowed")
    return problems


# --------------------------------------------------------------------------
# Object builders
# --------------------------------------------------------------------------


def placeholder_image() -> str:
    """A syntactically valid pinned tag for static rendering only - it
    exists on no node."""
    return f"{SCALING_IMAGE_REPOSITORY}:day8-cfg-{'0' * 64}"


def check_image(image: str) -> str:
    if not _SCALING_TAG_RE.match(image or ""):
        raise ValueError(f"scaling image must be a pinned {SCALING_IMAGE_REPOSITORY}:day8-cfg-<64 hex> tag, got {image!r}")
    return image


def labels(component: str) -> dict:
    return {
        "app.kubernetes.io/name": component,
        "app.kubernetes.io/instance": INSTANCE,
        "app.kubernetes.io/component": component,
        "app.kubernetes.io/part-of": PART_OF,
        "app.kubernetes.io/managed-by": MANAGED_BY,
    }


def selector(component: str) -> dict:
    return {"app.kubernetes.io/instance": INSTANCE, "app.kubernetes.io/component": component}


def meta(name: str, component: str | None = None, namespace: str | None = NAMESPACE) -> dict:
    out = {"name": name, "labels": labels(component or name)}
    if namespace:
        out["namespace"] = namespace
    return out


WORKER_ONLY_AFFINITY = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}]}]
        }
    }
}


def pod_security(uid: int = 10001, gid: int = 10001, fs_group: int | None = None) -> dict:
    out = {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": gid, "seccompProfile": {"type": "RuntimeDefault"}}
    if fs_group is not None:
        out["fsGroup"] = fs_group
    return out


CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}


def http_probes() -> dict:
    return {
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": PORT}, "periodSeconds": 5, "timeoutSeconds": 2},
        "livenessProbe": {"httpGet": {"path": "/livez", "port": PORT}, "periodSeconds": 10, "timeoutSeconds": 2, "failureThreshold": 3},
    }


def scaling_container(image: str, role: str, resources: Resources | None, env: dict | None = None, probes: bool = True) -> dict:
    c = {
        "name": CONTAINER,
        "image": check_image(image),
        "imagePullPolicy": "Never",
        "args": [role],
        "securityContext": CONTAINER_SECURITY,
    }
    if resources is not None:
        c["resources"] = resources.as_k8s()
    if env:
        c["env"] = [{"name": k, "value": str(v)} for k, v in env.items()]
    if probes:
        c["ports"] = [{"name": "http", "containerPort": PORT}]
        c.update(http_probes())
    return c


def pod_spec(containers: list[dict], restart: str = "Always", security: dict | None = None, volumes: list | None = None) -> dict:
    spec = {
        "serviceAccountName": SCALING_SERVICE_ACCOUNT,
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": security or pod_security(),
        "affinity": WORKER_ONLY_AFFINITY,
        "terminationGracePeriodSeconds": 5,
        "restartPolicy": restart,
        "containers": containers,
    }
    if volumes:
        spec["volumes"] = volumes
    return spec


def deployment(name: str, replicas: int, template_spec: dict) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": meta(name),
        "spec": {
            "replicas": replicas,
            "revisionHistoryLimit": 2,
            "selector": {"matchLabels": selector(name)},
            "template": {"metadata": {"labels": labels(name)}, "spec": template_spec},
        },
    }


def service(name: str, port: int, target_port: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": meta(name),
        "spec": {"type": "ClusterIP", "selector": selector(name), "ports": [{"name": "tcp", "port": port, "targetPort": target_port}]},
    }


def job(name: str, container: dict, deadline_seconds: int) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": meta(name),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_seconds,
            "template": {"metadata": {"labels": labels(name)}, "spec": pod_spec([container], restart="Never")},
        },
    }


def _workload(name: str) -> PodBudget:
    return next(w for w in WORKLOADS if w.name == name)


# --- namespace, guard rails ----------------------------------------------


def namespace_object() -> dict:
    lab = labels(NAMESPACE_COMPONENT)
    lab.update(
        {
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "latest",
            "pod-security.kubernetes.io/warn": "restricted",
        }
    )
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": lab}}


def limitrange_object() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": meta(LIMITRANGE, NAMESPACE_COMPONENT),
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "min": dict(LIMIT_MIN),
                    "max": dict(LIMIT_MAX),
                    "default": dict(LIMIT_DEFAULT),
                    "defaultRequest": dict(LIMIT_DEFAULT_REQUEST),
                }
            ]
        },
    }


def quota_object(b: Budget | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": meta(QUOTA, NAMESPACE_COMPONENT),
        "spec": {"hard": (b or budget()).hard()},
    }


def service_account_object() -> dict:
    """The only identity Day 8 Pods run as: no token is mounted (object and
    Pod both say so), and nothing binds any role to it."""
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": meta(SCALING_SERVICE_ACCOUNT, NAMESPACE_COMPONENT),
        "automountServiceAccountToken": False,
    }


def keda_namespace_object() -> dict:
    """Day 8's own `keda` namespace (created before the KEDA install,
    deleted by cleanup)."""
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": KEDA_NAMESPACE, "labels": labels(KEDA_NAMESPACE_COMPONENT)}}


def is_day8_owned(object_labels: dict | None, component: str) -> bool:
    """Pure: both identity labels, exactly - the single ownership rule for
    every Day 8 namespace (scaling and keda)."""
    lab = object_labels or {}
    return lab.get("app.kubernetes.io/instance") == INSTANCE and lab.get("app.kubernetes.io/component") == component


def guard_objects() -> list[dict]:
    """Applied first and alone: nothing else may exist in the namespace
    before its LimitRange and ResourceQuota (and the token-less Pod
    identity) do."""
    return [namespace_object(), limitrange_object(), quota_object(), service_account_object()]


# --- HPA target -------------------------------------------------------------


def hpa_target_objects(image: str) -> list[dict]:
    w = _workload(HPA_TARGET)
    return [
        deployment(HPA_TARGET, HPA_MIN, pod_spec([scaling_container(image, "cpu-server", w.resources)])),
        service(HPA_TARGET, PORT, PORT),
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": meta(HPA_TARGET),
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": HPA_TARGET},
                "minReplicas": HPA_MIN,
                "maxReplicas": HPA_MAX,
                "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": HPA_CPU_UTILIZATION}}}],
                "behavior": {
                    "scaleUp": {"stabilizationWindowSeconds": 0, "selectPolicy": "Max", "policies": [{"type": "Pods", "value": 2, "periodSeconds": 15}]},
                    "scaleDown": {
                        "stabilizationWindowSeconds": HPA_SCALE_DOWN_WINDOW_SECONDS,
                        "selectPolicy": "Max",
                        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 15}],
                    },
                },
            },
        },
    ]


def hpa_load_job(image: str) -> dict:
    w = _workload(HPA_LOAD)
    env = {
        "TARGET_URL": f"http://{HPA_TARGET}.{NAMESPACE}.svc.cluster.local:{PORT}/work",
        "LOAD_SECONDS": LOAD_SECONDS,
        "LOAD_CONCURRENCY": LOAD_CONCURRENCY,
        "WORK_MS": LOAD_WORK_MS,
    }
    return job(HPA_LOAD, scaling_container(image, "load", w.resources, env, probes=False), LOAD_SECONDS + 90)


# --- VPA target -------------------------------------------------------------


def vpa_target_objects(image: str) -> list[dict]:
    w = _workload(VPA_TARGET)
    return [deployment(VPA_TARGET, 1, pod_spec([scaling_container(image, "vpa-idle", w.resources, {"HOLD_MIB": 24, "BURN_MS": 3, "PERIOD_MS": 100})]))]


def vpa_object(mode: str) -> dict:
    if mode not in ("Off", "Initial"):
        raise ValueError(f"Day 8 uses VPA updateMode Off or Initial only, never {mode!r} (no updater is installed)")
    return {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": meta(VPA_TARGET),
        "spec": {
            "targetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": VPA_TARGET},
            "updatePolicy": {"updateMode": mode},
            "resourcePolicy": {
                "containerPolicies": [
                    {
                        "containerName": CONTAINER,
                        "controlledResources": ["cpu", "memory"],
                        "controlledValues": "RequestsAndLimits",
                        "minAllowed": dict(VPA_MIN_ALLOWED),
                        "maxAllowed": dict(VPA_MAX_ALLOWED),
                    }
                ]
            },
        },
    }


# --- KEDA queue -------------------------------------------------------------


def queue_objects() -> list[dict]:
    w = _workload(QUEUE)
    redis = {
        "name": "redis",
        "image": REDIS_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        # The image's entrypoint is bypassed: redis-server directly, as the
        # image's own non-root user, no persistence of any kind - the queue
        # holds disposable test items only and never touches maops-state.
        "command": ["redis-server"],
        "args": ["--save", "", "--appendonly", "no", "--protected-mode", "no", "--maxmemory", "16mb", "--maxmemory-policy", "noeviction"],
        "ports": [{"name": "redis", "containerPort": 6379}],
        "resources": w.resources.as_k8s(),
        "securityContext": CONTAINER_SECURITY,
        "readinessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 5},
        "livenessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 10},
        "volumeMounts": [{"name": "scratch", "mountPath": "/data"}],
    }
    spec = pod_spec([redis], security=pod_security(REDIS_UID, REDIS_GID, REDIS_GID), volumes=[{"name": "scratch", "emptyDir": {"sizeLimit": "16Mi"}}])
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": meta(QUEUE_POLICY, QUEUE),
        "spec": {
            "podSelector": {"matchLabels": selector(QUEUE)},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"podSelector": {"matchLabels": selector(WORKER)}},
                        {"podSelector": {"matchLabels": selector(PRODUCER)}},
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": KEDA_NAMESPACE}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": 6379}],
                }
            ],
        },
    }
    return [deployment(QUEUE, 1, spec), service(QUEUE, 6379, 6379), policy]


def queue_env() -> dict:
    return {"QUEUE_HOST": f"{QUEUE}.{NAMESPACE}.svc.cluster.local", "QUEUE_PORT": 6379, "QUEUE_LIST": QUEUE_LIST}


def worker_objects(image: str) -> list[dict]:
    w = _workload(WORKER)
    env = {**queue_env(), "PROCESS_MS": WORKER_PROCESS_MS}
    # replicas 0: KEDA owns the count from the moment the ScaledObject exists;
    # this Deployment is applied once and never re-applied with a count.
    return [deployment(WORKER, 0, pod_spec([scaling_container(image, "worker", w.resources, env)]))]


def scaled_object() -> dict:
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": meta(WORKER),
        "spec": {
            "scaleTargetRef": {"name": WORKER},
            "pollingInterval": KEDA_POLLING_SECONDS,
            "cooldownPeriod": KEDA_COOLDOWN_SECONDS,
            "minReplicaCount": KEDA_MIN,
            "maxReplicaCount": KEDA_MAX,
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleDown": {"stabilizationWindowSeconds": 15, "policies": [{"type": "Pods", "value": 1, "periodSeconds": 10}]},
                    }
                }
            },
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": f"{QUEUE}.{NAMESPACE}.svc.cluster.local:6379",
                        "listName": QUEUE_LIST,
                        "listLength": str(KEDA_LIST_LENGTH_TARGET),
                        "activationListLength": "0",
                        "databaseIndex": "0",
                    },
                }
            ],
        },
    }


def producer_job(image: str) -> dict:
    w = _workload(PRODUCER)
    return job(PRODUCER, scaling_container(image, "producer", w.resources, {**queue_env(), "ITEMS": PRODUCER_ITEMS}, probes=False), 120)


# --- quota proof Pods ---------------------------------------------------------


def probe_pod(image: str, name: str, containers: list[dict]) -> dict:
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta(name, QUOTA_PROBE), "spec": pod_spec(containers, restart="Never")}


def in_budget_pod(image: str) -> dict:
    """No resources at all: the LimitRange must default them (DEFAULTED)."""
    return probe_pod(image, QUOTA_PROBE, [scaling_container(image, "cpu-server", None)])


def over_limitrange_pod(image: str) -> dict:
    """One container above the LimitRange max - must be rejected."""
    over = fmt_cpu(cpu_millis(LIMIT_MAX["cpu"]) + 50)
    return probe_pod(image, "day8-quota-over-limitrange", [scaling_container(image, "cpu-server", Resources(over, "32Mi", over, "64Mi"), probes=False)])


def over_quota_pod(image: str, b: Budget | None = None) -> dict:
    """Every container individually within the LimitRange, but together
    above the WHOLE quota's requests.cpu - rejected by ResourceQuota
    whatever else is running."""
    b = b or budget()
    per = cpu_millis(LIMIT_MAX["cpu"])
    count = b.requests_cpu_m // per + 1
    containers = []
    for i in range(count):
        c = scaling_container(image, "cpu-server", Resources(fmt_cpu(per), LIMIT_MIN["memory"], fmt_cpu(per), LIMIT_MIN["memory"]), probes=False)
        c["name"] = f"{CONTAINER}-{i}"
        containers.append(c)
    return probe_pod(image, "day8-quota-over-budget", containers)


def scaling_objects(image: str) -> list[dict]:
    """Every scaling controller object (for scaler_conflicts)."""
    return [o for o in hpa_target_objects(image) if o["kind"] == "HorizontalPodAutoscaler"] + [vpa_object("Off"), scaled_object()]


def all_objects(image: str) -> list[dict]:
    return [
        *guard_objects(),
        *hpa_target_objects(image),
        hpa_load_job(image),
        *vpa_target_objects(image),
        vpa_object("Off"),
        *queue_objects(),
        *worker_objects(image),
        scaled_object(),
        producer_job(image),
    ]


def as_list(objects: list[dict]) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2, sort_keys=True)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("render", "budget"):
        print("usage: day8_objects.py render [IMAGE] | budget", file=sys.stderr)
        return 2
    if sys.argv[1] == "budget":
        b = budget()
        print(f"# Day 8 scaling namespace budget ({NAMESPACE}) - worst case, all Pods at max count and size")
        print(f"{'pod kind':24} {'max':>3}  {'req cpu':>7} {'req mem':>7} {'lim cpu':>7} {'lim mem':>7}  notes")
        for row in b.rows:
            print(f"{row[0]:24} {row[1]:>3}  {row[2]:>7} {row[3]:>7} {row[4]:>7} {row[5]:>7}  {row[6]}")
        print("ResourceQuota hard:", json.dumps(b.hard(), sort_keys=True))
        return 0
    image = sys.argv[2] if len(sys.argv) > 2 else placeholder_image()
    print(as_list(all_objects(image)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

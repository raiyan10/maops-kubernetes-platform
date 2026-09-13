"""
Repository-owned static validation for the Day 4 Kubernetes manifests.

Operates on already-parsed Kubernetes objects (plain dict/list/scalar
Python structures - one entry per rendered document), not raw YAML
text, so the validation rules stay decoupled from parsing mechanics
and are directly unit-testable against constructed fixtures.

Day 4 keeps Day 3's two-Deployment architecture (gateway/app, scaling,
scheduling, rollout/rollback, PDBs) entirely unchanged and adds a third
workload, `maops-state` - a single-replica StatefulSet with a
PVC-backed `/data` volume (via `volumeClaimTemplates`, never a
standalone committed PersistentVolumeClaim), a governing headless
Service plus a normal ClusterIP Service, its own ConfigMap, and its own
runtime Secret (`maops-state-auth`, distinct from gateway/app's
`maops-internal-auth`). Neither Secret object is ever rendered by
k8s/base (see scope.no_forbidden_resources) - both are created
out-of-band by scripts/secret_bootstrap.py.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_VERSION = "0.4.0"
EXPECTED_INSTANCE = "maops-kubernetes-platform-day4"

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
GATEWAY_SERVICE = "maops-gateway"
APP_SERVICE = "maops-app"
GATEWAY_CONFIGMAP = "maops-gateway-config"
APP_CONFIGMAP = "maops-app-config"
GATEWAY_PDB = "maops-gateway-pdb"
APP_PDB = "maops-app-pdb"
GATEWAY_CONTAINER = "maops-gateway"
APP_CONTAINER = "maops-app"
GATEWAY_IMAGE = f"maops-kubernetes-gateway:{EXPECTED_VERSION}"
APP_IMAGE = f"maops-kubernetes-app:{EXPECTED_VERSION}"

# DAY4: maops-state StatefulSet - single replica, PVC-backed persistence.
STATE_STATEFULSET = "maops-state"
STATE_CONTAINER = "maops-state"
STATE_IMAGE = f"maops-kubernetes-state:{EXPECTED_VERSION}"
STATE_CONFIGMAP = "maops-state-config"
STATE_SERVICE = "maops-state"
STATE_HEADLESS_SERVICE = "maops-state-headless"
EXPECTED_STATE_REPLICAS = 1
STATE_VOLUME_CLAIM_TEMPLATE_NAME = "data"
EXPECTED_STATE_CLAIM_STORAGE = "256Mi"
STATE_MOUNT_PATH = "/data"

STATE_SECRET_NAME = "maops-state-auth"
STATE_SECRET_VOLUME_NAME = "state-auth"
STATE_SECRET_MOUNT_PATH = "/var/run/secrets/maops-state"
STATE_SECRET_KEY = "state-token"

# DAY4: total rendered portable application objects (excludes runtime
# Secrets and the generated PVC/PV, which are not part of k8s/base) -
# 1 Namespace + 3 ConfigMaps (gateway/app/state) + 2 Deployments +
# 1 StatefulSet + 4 Services (gateway/app/state/state-headless) +
# 2 PodDisruptionBudgets (gateway/app only - state carries no PDB).
EXPECTED_TOTAL_RENDERED_OBJECTS = 13

EXPECTED_REPLICAS = 3
EXPECTED_REQUESTS = {"cpu": "50m", "memory": "32Mi"}
EXPECTED_LIMITS = {"cpu": "250m", "memory": "128Mi"}

# DAY3: RollingUpdate/availability tuning - pinned explicitly rather than
# relying on Kubernetes' current defaults (see k8s/base/*-deployment.yaml
# for the full rationale).
EXPECTED_STRATEGY_TYPE = "RollingUpdate"
EXPECTED_MAX_UNAVAILABLE = 1
EXPECTED_MAX_SURGE = 1
EXPECTED_MIN_READY_SECONDS = 5
EXPECTED_PROGRESS_DEADLINE_SECONDS = 120
EXPECTED_REVISION_HISTORY_LIMIT = 5

# DAY3: scheduling.
CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"
EXPECTED_TOPOLOGY_KEY = "kubernetes.io/hostname"
EXPECTED_MAX_SKEW = 1
EXPECTED_WHEN_UNSATISFIABLE = "DoNotSchedule"
# DAY3-ARCH-L1: pinned explicitly rather than relying on the
# Kubernetes-version default for either policy.
EXPECTED_NODE_AFFINITY_POLICY = "Honor"
EXPECTED_NODE_TAINTS_POLICY = "Honor"

# DAY3: PodDisruptionBudget.
EXPECTED_PDB_MIN_AVAILABLE = 2

SECRET_NAME = "maops-internal-auth"
SECRET_VOLUME_NAME = "internal-auth"
SECRET_MOUNT_PATH = "/var/run/secrets/maops"
SECRET_KEY = "internal-token"

EXPECTED_BACKEND_HOST = "maops-app"
EXPECTED_BACKEND_PORT = "8080"

# DAY4 remediation batch 2 (DAY4-ARCH-M1): the real chain is three hops
# deep - gateway -> app -> state - not the two-hop chain this margin
# pattern was originally sized for. Each ConfigMap-provided client
# timeout is a per-call socket connect+read bound (see app/server.py's
# STATE_TIMEOUT_SECONDS / gateway/server.py's BACKEND_TIMEOUT_SECONDS),
# not a total wall-clock deadline over any retries a caller might make;
# each hop's own readinessProbe.timeoutSeconds is kept comfortably (2s)
# above the client timeout it bounds, mirroring the existing
# app/STATE_TIMEOUT_SECONDS relationship at the new outer layer.
EXPECTED_STATE_TIMEOUT_SECONDS = "3"
EXPECTED_APP_READINESS_TIMEOUT_SECONDS = 5
EXPECTED_BACKEND_TIMEOUT_SECONDS = "5"
EXPECTED_GATEWAY_READINESS_TIMEOUT_SECONDS = 7

FORBIDDEN_KINDS = {
    "Secret",
    "Ingress",
    # DAY4: PersistentVolumeClaim remains forbidden as a STANDALONE
    # committed object - the only sanctioned way to request storage is
    # StatefulSet.spec.volumeClaimTemplates (checked explicitly below),
    # which kubectl kustomize never renders as a separate top-level
    # PersistentVolumeClaim document.
    "PersistentVolumeClaim",
    "Role",
    "RoleBinding",
    "ClusterRole",
    "ClusterRoleBinding",
    "ServiceAccount",
    "NetworkPolicy",
    "HorizontalPodAutoscaler",
}

# Heuristic for a Deployment-managed pod's generated name, e.g.
# "maops-app-7d9f8c9c5-abcde" (Deployment name + ReplicaSet pod-template
# hash + random suffix). Used only to reject an obviously hardcoded,
# non-Service identity in BACKEND_HOST - the exact-match check below is
# the primary guard.
_POD_LIKE_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{6,10}-[a-z0-9]{5}$")


@dataclass
class Finding:
    ok: bool
    name: str
    detail: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.name}: {self.detail}"


class _Checker:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.findings: list[Finding] = []

    def check(self, ok: bool, name: str, detail: str) -> bool:
        self.findings.append(Finding(ok=bool(ok), name=name, detail=detail))
        return bool(ok)

    def by_kind(self, kind: str) -> list[dict]:
        return [d for d in self.docs if d.get("kind") == kind]


def _looks_secret(key: str, value) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("secret", "password", "token", "key", "credential"))


def _is_ip_literal(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _looks_pod_like(value) -> bool:
    return isinstance(value, str) and bool(_POD_LIKE_RE.match(value))


def _find_container(containers: list[dict], name: str) -> dict:
    return next((c for c in containers if c.get("name") == name), {})


def _find_volume(volumes: list[dict], name: str) -> dict:
    return next((v for v in (volumes or []) if isinstance(v, dict) and v.get("name") == name), {})


def _find_mount(mounts: list[dict], name: str) -> dict:
    return next((m for m in (mounts or []) if isinstance(m, dict) and m.get("name") == name), {})


def _node_affinity_terms(pod_spec: dict) -> list[dict]:
    return (
        (pod_spec.get("affinity") or {})
        .get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
        .get("nodeSelectorTerms")
        or []
    )


def _has_control_plane_exclusion(pod_spec: dict) -> bool:
    for term in _node_affinity_terms(pod_spec):
        for expr in term.get("matchExpressions") or []:
            if expr.get("key") == CONTROL_PLANE_LABEL and expr.get("operator") == "DoesNotExist":
                return True
    return False


def _check_workload_security_and_probes(
    c: _Checker,
    component: str,
    deployment: dict,
    expected_container: str,
    expected_image: str,
    expected_configmap: str,
) -> tuple[dict, dict, dict]:
    """Checks shared by both workloads: namespace, replicas, rollout
    strategy, scheduling, image, probes, resources, pod/container
    security baseline, ConfigMap wiring, Secret volume/mount wiring.
    Returns (pod_spec, pod_labels, container) for the caller's
    workload-specific checks (selectors, backend wiring, PDB)."""

    c.check(
        deployment.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        f"{component}.deployment.namespace_matches",
        f"expected Deployment metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{deployment.get('metadata', {}).get('namespace')!r}",
    )

    dep_labels = deployment.get("metadata", {}).get("labels", {}) or {}
    c.check(
        dep_labels.get("app.kubernetes.io/version") == EXPECTED_VERSION,
        f"{component}.deployment.version_label",
        f"expected Deployment app.kubernetes.io/version == {EXPECTED_VERSION!r}, found "
        f"{dep_labels.get('app.kubernetes.io/version')!r}",
    )

    dep_spec = deployment.get("spec", {})
    c.check(
        dep_spec.get("replicas") == EXPECTED_REPLICAS,
        f"{component}.deployment.replicas",
        f"expected replicas == {EXPECTED_REPLICAS}, found {dep_spec.get('replicas')!r}",
    )

    # DAY3: RollingUpdate strategy tuning.
    strategy = dep_spec.get("strategy") or {}
    c.check(
        strategy.get("type") == EXPECTED_STRATEGY_TYPE,
        f"{component}.deployment.strategy_type",
        f"expected strategy.type == {EXPECTED_STRATEGY_TYPE!r}, found {strategy.get('type')!r}",
    )
    rolling_update = strategy.get("rollingUpdate") or {}
    c.check(
        rolling_update.get("maxUnavailable") == EXPECTED_MAX_UNAVAILABLE,
        f"{component}.deployment.max_unavailable",
        f"expected strategy.rollingUpdate.maxUnavailable == {EXPECTED_MAX_UNAVAILABLE}, found "
        f"{rolling_update.get('maxUnavailable')!r}",
    )
    c.check(
        rolling_update.get("maxSurge") == EXPECTED_MAX_SURGE,
        f"{component}.deployment.max_surge",
        f"expected strategy.rollingUpdate.maxSurge == {EXPECTED_MAX_SURGE}, found {rolling_update.get('maxSurge')!r}",
    )
    c.check(
        dep_spec.get("minReadySeconds") == EXPECTED_MIN_READY_SECONDS,
        f"{component}.deployment.min_ready_seconds",
        f"expected minReadySeconds == {EXPECTED_MIN_READY_SECONDS}, found {dep_spec.get('minReadySeconds')!r}",
    )
    c.check(
        dep_spec.get("progressDeadlineSeconds") == EXPECTED_PROGRESS_DEADLINE_SECONDS,
        f"{component}.deployment.progress_deadline_seconds",
        f"expected progressDeadlineSeconds == {EXPECTED_PROGRESS_DEADLINE_SECONDS}, found "
        f"{dep_spec.get('progressDeadlineSeconds')!r}",
    )
    c.check(
        dep_spec.get("revisionHistoryLimit") == EXPECTED_REVISION_HISTORY_LIMIT,
        f"{component}.deployment.revision_history_limit",
        f"expected revisionHistoryLimit == {EXPECTED_REVISION_HISTORY_LIMIT}, found "
        f"{dep_spec.get('revisionHistoryLimit')!r}",
    )

    pod_spec = dep_spec.get("template", {}).get("spec", {})
    pod_labels = dep_spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
    c.check(
        pod_labels.get("app.kubernetes.io/version") == EXPECTED_VERSION,
        f"{component}.pod_template.version_label",
        f"expected pod template app.kubernetes.io/version == {EXPECTED_VERSION!r}, found "
        f"{pod_labels.get('app.kubernetes.io/version')!r}",
    )

    # DAY3: worker-only required node affinity - must not depend solely
    # on the default kind control-plane taint.
    c.check(
        _has_control_plane_exclusion(pod_spec),
        f"{component}.scheduling.control_plane_excluded",
        f"expected a required nodeAffinity term excluding {CONTROL_PLANE_LABEL!r} (operator DoesNotExist), "
        f"found nodeSelectorTerms={_node_affinity_terms(pod_spec)!r}",
    )

    # DAY3: topologySpreadConstraints - exactly one, scoped to this
    # workload's own component only (never counting the other workload's
    # Pods).
    spread_constraints = pod_spec.get("topologySpreadConstraints") or []
    c.check(
        len(spread_constraints) >= 1,
        f"{component}.scheduling.topology_spread_present",
        f"expected at least one topologySpreadConstraints entry, found {len(spread_constraints)}",
    )
    spread = spread_constraints[0] if spread_constraints else {}
    c.check(
        spread.get("maxSkew") == EXPECTED_MAX_SKEW,
        f"{component}.scheduling.max_skew",
        f"expected topologySpreadConstraints[0].maxSkew == {EXPECTED_MAX_SKEW}, found {spread.get('maxSkew')!r}",
    )
    c.check(
        spread.get("topologyKey") == EXPECTED_TOPOLOGY_KEY,
        f"{component}.scheduling.topology_key",
        f"expected topologySpreadConstraints[0].topologyKey == {EXPECTED_TOPOLOGY_KEY!r}, found "
        f"{spread.get('topologyKey')!r}",
    )
    c.check(
        spread.get("whenUnsatisfiable") == EXPECTED_WHEN_UNSATISFIABLE,
        f"{component}.scheduling.when_unsatisfiable",
        f"expected topologySpreadConstraints[0].whenUnsatisfiable == {EXPECTED_WHEN_UNSATISFIABLE!r}, found "
        f"{spread.get('whenUnsatisfiable')!r}",
    )
    c.check(
        spread.get("nodeAffinityPolicy") == EXPECTED_NODE_AFFINITY_POLICY,
        f"{component}.scheduling.node_affinity_policy",
        f"expected topologySpreadConstraints[0].nodeAffinityPolicy == {EXPECTED_NODE_AFFINITY_POLICY!r}, found "
        f"{spread.get('nodeAffinityPolicy')!r}",
    )
    c.check(
        spread.get("nodeTaintsPolicy") == EXPECTED_NODE_TAINTS_POLICY,
        f"{component}.scheduling.node_taints_policy",
        f"expected topologySpreadConstraints[0].nodeTaintsPolicy == {EXPECTED_NODE_TAINTS_POLICY!r}, found "
        f"{spread.get('nodeTaintsPolicy')!r}",
    )
    spread_selector = (spread.get("labelSelector") or {}).get("matchLabels") or {}
    c.check(
        bool(spread_selector) and spread_selector.get("app.kubernetes.io/component") == component,
        f"{component}.scheduling.topology_selector_scoped_to_own_component",
        f"expected topologySpreadConstraints[0].labelSelector.matchLabels.app.kubernetes.io/component == "
        f"{component!r}, found {spread_selector!r}",
    )

    containers = pod_spec.get("containers") or []
    c.check(
        len(containers) == 1,
        f"{component}.deployment.single_container",
        f"expected exactly one container, found {len(containers)}",
    )
    container = containers[0] if containers else {}

    c.check(
        container.get("name") == expected_container,
        f"{component}.deployment.container_name",
        f"expected container name {expected_container!r}, found {container.get('name')!r}",
    )
    c.check(
        container.get("image") == expected_image,
        f"{component}.deployment.image",
        f"expected image {expected_image!r}, found {container.get('image')!r}",
    )
    c.check(
        container.get("imagePullPolicy") == "IfNotPresent",
        f"{component}.deployment.image_pull_policy",
        f"expected imagePullPolicy IfNotPresent, found {container.get('imagePullPolicy')!r}",
    )

    # Probes
    startup = container.get("startupProbe") or {}
    liveness = container.get("livenessProbe") or {}
    readiness = container.get("readinessProbe") or {}
    c.check(
        bool(startup.get("httpGet")),
        f"{component}.probes.startup_present",
        f"expected an HTTP startupProbe, found {startup!r}",
    )
    c.check(
        startup.get("httpGet", {}).get("path") == "/livez",
        f"{component}.probes.startup_path",
        f"expected startupProbe httpGet path /livez, found {startup.get('httpGet', {}).get('path')!r}",
    )
    c.check(
        liveness.get("httpGet", {}).get("path") == "/livez",
        f"{component}.probes.liveness_path",
        f"expected livenessProbe httpGet path /livez, found {liveness.get('httpGet', {}).get('path')!r}",
    )
    c.check(
        readiness.get("httpGet", {}).get("path") == "/readyz",
        f"{component}.probes.readiness_path",
        f"expected readinessProbe httpGet path /readyz, found {readiness.get('httpGet', {}).get('path')!r}",
    )

    # Resources
    resources = container.get("resources") or {}
    c.check(
        resources.get("requests") == EXPECTED_REQUESTS,
        f"{component}.resources.requests",
        f"expected requests {EXPECTED_REQUESTS}, found {resources.get('requests')!r}",
    )
    c.check(
        resources.get("limits") == EXPECTED_LIMITS,
        f"{component}.resources.limits",
        f"expected limits {EXPECTED_LIMITS}, found {resources.get('limits')!r}",
    )

    # Security context - pod level
    pod_sc = pod_spec.get("securityContext") or {}
    c.check(
        pod_sc.get("runAsNonRoot") is True,
        f"{component}.security.pod_run_as_non_root",
        f"expected pod securityContext.runAsNonRoot == true, found {pod_sc.get('runAsNonRoot')!r}",
    )
    c.check(
        pod_sc.get("runAsUser") == 10001,
        f"{component}.security.pod_run_as_user",
        f"expected pod securityContext.runAsUser == 10001, found {pod_sc.get('runAsUser')!r}",
    )
    c.check(
        pod_sc.get("runAsGroup") == 10001,
        f"{component}.security.pod_run_as_group",
        f"expected pod securityContext.runAsGroup == 10001, found {pod_sc.get('runAsGroup')!r}",
    )
    c.check(
        (pod_sc.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
        f"{component}.security.seccomp_profile",
        f"expected seccompProfile.type == RuntimeDefault, found {pod_sc.get('seccompProfile')!r}",
    )

    # Security context - container level
    container_sc = container.get("securityContext") or {}
    c.check(
        container_sc.get("allowPrivilegeEscalation") is False,
        f"{component}.security.allow_privilege_escalation",
        f"expected allowPrivilegeEscalation == false, found {container_sc.get('allowPrivilegeEscalation')!r}",
    )
    c.check(
        container_sc.get("readOnlyRootFilesystem") is True,
        f"{component}.security.read_only_root_filesystem",
        f"expected readOnlyRootFilesystem == true, found {container_sc.get('readOnlyRootFilesystem')!r}",
    )
    c.check(
        (container_sc.get("capabilities") or {}).get("drop") == ["ALL"],
        f"{component}.security.capabilities_drop_all",
        f"expected capabilities.drop == ['ALL'], found {container_sc.get('capabilities')!r}",
    )

    c.check(
        pod_spec.get("automountServiceAccountToken") is False,
        f"{component}.security.automount_service_account_token",
        f"expected automountServiceAccountToken == false, found {pod_spec.get('automountServiceAccountToken')!r}",
    )

    c.check(
        not pod_spec.get("hostNetwork"),
        f"{component}.security.no_host_network",
        f"expected no hostNetwork, found {pod_spec.get('hostNetwork')!r}",
    )
    container_ports = container.get("ports") or []
    host_ports = [p for p in container_ports if p.get("hostPort")]
    c.check(
        not host_ports,
        f"{component}.security.no_host_port",
        f"expected no hostPort on any container port, found {host_ports}",
    )
    c.check(
        not pod_spec.get("hostPID"),
        f"{component}.security.no_host_pid",
        f"expected no hostPID, found {pod_spec.get('hostPID')!r}",
    )
    c.check(
        not pod_spec.get("hostIPC"),
        f"{component}.security.no_host_ipc",
        f"expected no hostIPC, found {pod_spec.get('hostIPC')!r}",
    )
    c.check(
        container_sc.get("privileged") is not True,
        f"{component}.security.not_privileged",
        f"expected container securityContext.privileged != true, found {container_sc.get('privileged')!r}",
    )

    volumes = pod_spec.get("volumes") or []
    hostpath_volumes = [v for v in volumes if isinstance(v, dict) and "hostPath" in v]
    c.check(
        not hostpath_volumes,
        f"{component}.security.no_host_path_volumes",
        f"expected no hostPath volumes, found {hostpath_volumes}",
    )

    # ConfigMap wiring
    env_from = container.get("envFrom") or []
    env = container.get("env") or []
    wired_via_env_from = any((ef.get("configMapRef") or {}).get("name") == expected_configmap for ef in env_from)
    wired_via_env = any(
        (e.get("valueFrom") or {}).get("configMapKeyRef", {}).get("name") == expected_configmap for e in env
    )
    c.check(
        wired_via_env_from or wired_via_env,
        f"{component}.configmap.wired_to_container",
        f"expected container to consume ConfigMap {expected_configmap!r} via envFrom or env, found "
        f"envFrom={env_from!r} env={env!r}",
    )

    # Secret volume/mount wiring
    secret_volume = _find_volume(volumes, SECRET_VOLUME_NAME)
    secret_spec = secret_volume.get("secret") or {}
    c.check(
        secret_spec.get("secretName") == SECRET_NAME,
        f"{component}.secret.volume_present",
        f"expected volume {SECRET_VOLUME_NAME!r} to reference Secret {SECRET_NAME!r}, found "
        f"{secret_spec.get('secretName')!r}",
    )
    items = secret_spec.get("items")
    if items:
        item_keys = [i.get("key") for i in items if isinstance(i, dict)]
        c.check(
            SECRET_KEY in item_keys,
            f"{component}.secret.key_present",
            f"expected Secret volume items to include key {SECRET_KEY!r}, found {item_keys}",
        )
    else:
        c.check(
            True,
            f"{component}.secret.key_present",
            f"no items restriction on volume {SECRET_VOLUME_NAME!r} - all Secret keys (including {SECRET_KEY!r}) mount",
        )

    mounts = container.get("volumeMounts") or []
    mount = _find_mount(mounts, SECRET_VOLUME_NAME)
    c.check(
        mount.get("mountPath") == SECRET_MOUNT_PATH,
        f"{component}.secret.mount_path",
        f"expected volumeMount {SECRET_VOLUME_NAME!r} mountPath == {SECRET_MOUNT_PATH!r}, found "
        f"{mount.get('mountPath')!r}",
    )
    c.check(
        mount.get("readOnly") is True,
        f"{component}.secret.mount_read_only",
        f"expected volumeMount {SECRET_VOLUME_NAME!r} readOnly == true, found {mount.get('readOnly')!r}",
    )

    return pod_spec, pod_labels, container


def _check_pdb(c: _Checker, component: str, pdb: dict | None, pod_labels: dict, other_pod_labels: dict) -> dict:
    c.check(
        pdb is not None,
        f"{component}.pdb.exists",
        f"expected a PodDisruptionBudget for {component!r} to exist, found none",
    )
    pdb = pdb or {}
    c.check(
        pdb.get("apiVersion") == "policy/v1",
        f"{component}.pdb.api_version",
        f"expected PodDisruptionBudget apiVersion == 'policy/v1', found {pdb.get('apiVersion')!r}",
    )
    c.check(
        pdb.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        f"{component}.pdb.namespace_matches",
        f"expected PodDisruptionBudget metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{pdb.get('metadata', {}).get('namespace')!r}",
    )
    pdb_spec = pdb.get("spec") or {}
    c.check(
        pdb_spec.get("minAvailable") == EXPECTED_PDB_MIN_AVAILABLE,
        f"{component}.pdb.min_available",
        f"expected PodDisruptionBudget spec.minAvailable == {EXPECTED_PDB_MIN_AVAILABLE}, found "
        f"{pdb_spec.get('minAvailable')!r}",
    )
    c.check(
        "maxUnavailable" not in pdb_spec,
        f"{component}.pdb.no_max_unavailable",
        f"expected PodDisruptionBudget to use minAvailable, not maxUnavailable, found spec={pdb_spec!r}",
    )
    selector = (pdb_spec.get("selector") or {}).get("matchLabels") or {}
    c.check(
        bool(selector) and all(pod_labels.get(k) == v for k, v in selector.items()),
        f"{component}.pdb.selector_matches_pod_labels",
        f"expected PodDisruptionBudget selector {selector} to be satisfied by pod labels {pod_labels}",
    )
    c.check(
        not (bool(selector) and all(other_pod_labels.get(k) == v for k, v in selector.items())),
        f"{component}.pdb.no_cross_workload_selector",
        f"PodDisruptionBudget selector {selector} for {component!r} must NOT be satisfied by the other "
        f"workload's pod labels {other_pod_labels}",
    )
    return pdb_spec


def _check_state_statefulset(c: _Checker, statefulset: dict | None) -> tuple[dict, dict]:
    """DAY4: maops-state StatefulSet - single replica, PVC-backed
    persistence via volumeClaimTemplates. Shares the same security
    baseline as the Deployment-based workloads, but deliberately has no
    RollingUpdate maxUnavailable/maxSurge tuning (meaningless at 1
    replica), no topologySpreadConstraints, and no PodDisruptionBudget
    (a single-replica workload cannot have a non-trivial disruption
    budget)."""
    c.check(
        statefulset is not None,
        "state.statefulset.exists",
        f"expected StatefulSet {STATE_STATEFULSET!r} to exist, found none",
    )
    statefulset = statefulset or {}

    c.check(
        statefulset.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "state.statefulset.namespace_matches",
        f"expected StatefulSet metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{statefulset.get('metadata', {}).get('namespace')!r}",
    )
    dep_labels = statefulset.get("metadata", {}).get("labels", {}) or {}
    c.check(
        dep_labels.get("app.kubernetes.io/version") == EXPECTED_VERSION,
        "state.statefulset.version_label",
        f"expected StatefulSet app.kubernetes.io/version == {EXPECTED_VERSION!r}, found "
        f"{dep_labels.get('app.kubernetes.io/version')!r}",
    )

    sts_spec = statefulset.get("spec", {})
    c.check(
        sts_spec.get("replicas") == EXPECTED_STATE_REPLICAS,
        "state.statefulset.replicas",
        f"expected replicas == {EXPECTED_STATE_REPLICAS}, found {sts_spec.get('replicas')!r}",
    )
    c.check(
        sts_spec.get("serviceName") == STATE_HEADLESS_SERVICE,
        "state.statefulset.service_name",
        f"expected spec.serviceName == {STATE_HEADLESS_SERVICE!r}, found {sts_spec.get('serviceName')!r}",
    )

    pod_spec = sts_spec.get("template", {}).get("spec", {})
    pod_labels = sts_spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
    c.check(
        pod_labels.get("app.kubernetes.io/version") == EXPECTED_VERSION,
        "state.pod_template.version_label",
        f"expected pod template app.kubernetes.io/version == {EXPECTED_VERSION!r}, found "
        f"{pod_labels.get('app.kubernetes.io/version')!r}",
    )
    c.check(
        pod_labels.get("app.kubernetes.io/component") == "state",
        "state.pod_template.component_label",
        f"expected pod template app.kubernetes.io/component == 'state', found "
        f"{pod_labels.get('app.kubernetes.io/component')!r}",
    )

    c.check(
        _has_control_plane_exclusion(pod_spec),
        "state.scheduling.control_plane_excluded",
        f"expected a required nodeAffinity term excluding {CONTROL_PLANE_LABEL!r} (operator DoesNotExist), "
        f"found nodeSelectorTerms={_node_affinity_terms(pod_spec)!r}",
    )
    c.check(
        not (pod_spec.get("topologySpreadConstraints") or []),
        "state.scheduling.no_topology_spread",
        "expected no topologySpreadConstraints on the single-replica state StatefulSet, found "
        f"{pod_spec.get('topologySpreadConstraints')!r}",
    )

    containers = pod_spec.get("containers") or []
    c.check(
        len(containers) == 1,
        "state.statefulset.single_container",
        f"expected exactly one container, found {len(containers)}",
    )
    container = containers[0] if containers else {}
    c.check(
        container.get("name") == STATE_CONTAINER,
        "state.statefulset.container_name",
        f"expected container name {STATE_CONTAINER!r}, found {container.get('name')!r}",
    )
    c.check(
        container.get("image") == STATE_IMAGE,
        "state.statefulset.image",
        f"expected image {STATE_IMAGE!r}, found {container.get('image')!r}",
    )
    c.check(
        container.get("imagePullPolicy") == "IfNotPresent",
        "state.statefulset.image_pull_policy",
        f"expected imagePullPolicy IfNotPresent, found {container.get('imagePullPolicy')!r}",
    )

    startup = container.get("startupProbe") or {}
    liveness = container.get("livenessProbe") or {}
    readiness = container.get("readinessProbe") or {}
    c.check(
        bool(startup.get("httpGet")),
        "state.probes.startup_present",
        f"expected an HTTP startupProbe, found {startup!r}",
    )
    c.check(
        liveness.get("httpGet", {}).get("path") == "/livez",
        "state.probes.liveness_path",
        f"expected livenessProbe httpGet path /livez, found {liveness.get('httpGet', {}).get('path')!r}",
    )
    c.check(
        readiness.get("httpGet", {}).get("path") == "/readyz",
        "state.probes.readiness_path",
        f"expected readinessProbe httpGet path /readyz, found {readiness.get('httpGet', {}).get('path')!r}",
    )

    resources = container.get("resources") or {}
    c.check(
        resources.get("requests") == EXPECTED_REQUESTS,
        "state.resources.requests",
        f"expected requests {EXPECTED_REQUESTS}, found {resources.get('requests')!r}",
    )
    c.check(
        resources.get("limits") == EXPECTED_LIMITS,
        "state.resources.limits",
        f"expected limits {EXPECTED_LIMITS}, found {resources.get('limits')!r}",
    )

    # Security context - identical baseline to gateway/app.
    pod_sc = pod_spec.get("securityContext") or {}
    c.check(pod_sc.get("runAsNonRoot") is True, "state.security.pod_run_as_non_root", f"found {pod_sc.get('runAsNonRoot')!r}")
    c.check(pod_sc.get("runAsUser") == 10001, "state.security.pod_run_as_user", f"found {pod_sc.get('runAsUser')!r}")
    c.check(pod_sc.get("runAsGroup") == 10001, "state.security.pod_run_as_group", f"found {pod_sc.get('runAsGroup')!r}")
    c.check(pod_sc.get("fsGroup") == 10001, "state.security.pod_fs_group", f"found {pod_sc.get('fsGroup')!r}")
    c.check(
        (pod_sc.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
        "state.security.seccomp_profile",
        f"found {pod_sc.get('seccompProfile')!r}",
    )
    container_sc = container.get("securityContext") or {}
    c.check(
        container_sc.get("allowPrivilegeEscalation") is False,
        "state.security.allow_privilege_escalation",
        f"found {container_sc.get('allowPrivilegeEscalation')!r}",
    )
    c.check(
        container_sc.get("readOnlyRootFilesystem") is True,
        "state.security.read_only_root_filesystem",
        f"found {container_sc.get('readOnlyRootFilesystem')!r}",
    )
    c.check(
        (container_sc.get("capabilities") or {}).get("drop") == ["ALL"],
        "state.security.capabilities_drop_all",
        f"found {container_sc.get('capabilities')!r}",
    )
    c.check(
        pod_spec.get("automountServiceAccountToken") is False,
        "state.security.automount_service_account_token",
        f"found {pod_spec.get('automountServiceAccountToken')!r}",
    )
    c.check(not pod_spec.get("hostNetwork"), "state.security.no_host_network", f"found {pod_spec.get('hostNetwork')!r}")
    c.check(not pod_spec.get("hostPID"), "state.security.no_host_pid", f"found {pod_spec.get('hostPID')!r}")
    c.check(not pod_spec.get("hostIPC"), "state.security.no_host_ipc", f"found {pod_spec.get('hostIPC')!r}")
    c.check(
        container_sc.get("privileged") is not True,
        "state.security.not_privileged",
        f"found {container_sc.get('privileged')!r}",
    )
    volumes = pod_spec.get("volumes") or []
    hostpath_volumes = [v for v in volumes if isinstance(v, dict) and "hostPath" in v]
    c.check(
        not hostpath_volumes,
        "state.security.no_host_path_volumes",
        f"expected no hostPath volumes (the PVC-backed volume is the sanctioned storage path), found {hostpath_volumes}",
    )

    # ConfigMap wiring
    env_from = container.get("envFrom") or []
    env = container.get("env") or []
    wired_via_env_from = any((ef.get("configMapRef") or {}).get("name") == STATE_CONFIGMAP for ef in env_from)
    wired_via_env = any(
        (e.get("valueFrom") or {}).get("configMapKeyRef", {}).get("name") == STATE_CONFIGMAP for e in env
    )
    c.check(
        wired_via_env_from or wired_via_env,
        "state.configmap.wired_to_container",
        f"expected container to consume ConfigMap {STATE_CONFIGMAP!r} via envFrom or env",
    )

    # Secret volume/mount wiring - maops-state-auth (distinct from the
    # internal-auth Secret gateway/app share).
    secret_volume = _find_volume(volumes, STATE_SECRET_VOLUME_NAME)
    secret_spec = secret_volume.get("secret") or {}
    c.check(
        secret_spec.get("secretName") == STATE_SECRET_NAME,
        "state.secret.volume_present",
        f"expected volume {STATE_SECRET_VOLUME_NAME!r} to reference Secret {STATE_SECRET_NAME!r}, found "
        f"{secret_spec.get('secretName')!r}",
    )
    mounts = container.get("volumeMounts") or []
    secret_mount = _find_mount(mounts, STATE_SECRET_VOLUME_NAME)
    c.check(
        secret_mount.get("mountPath") == STATE_SECRET_MOUNT_PATH,
        "state.secret.mount_path",
        f"expected volumeMount {STATE_SECRET_VOLUME_NAME!r} mountPath == {STATE_SECRET_MOUNT_PATH!r}, found "
        f"{secret_mount.get('mountPath')!r}",
    )
    c.check(
        secret_mount.get("readOnly") is True,
        "state.secret.mount_read_only",
        f"expected volumeMount {STATE_SECRET_VOLUME_NAME!r} readOnly == true, found {secret_mount.get('readOnly')!r}",
    )

    # PVC-backed data volume - via volumeClaimTemplates, never a direct
    # hostPath and never a standalone committed PersistentVolumeClaim.
    data_mount = _find_mount(mounts, STATE_VOLUME_CLAIM_TEMPLATE_NAME)
    c.check(
        data_mount.get("mountPath") == STATE_MOUNT_PATH,
        "state.storage.data_mount_path",
        f"expected volumeMount {STATE_VOLUME_CLAIM_TEMPLATE_NAME!r} mountPath == {STATE_MOUNT_PATH!r}, found "
        f"{data_mount.get('mountPath')!r}",
    )
    c.check(
        data_mount.get("readOnly") is not True,
        "state.storage.data_mount_writable",
        f"expected volumeMount {STATE_VOLUME_CLAIM_TEMPLATE_NAME!r} to be writable, found readOnly="
        f"{data_mount.get('readOnly')!r}",
    )

    claim_templates = sts_spec.get("volumeClaimTemplates") or []
    claim = next(
        (t for t in claim_templates if t.get("metadata", {}).get("name") == STATE_VOLUME_CLAIM_TEMPLATE_NAME),
        None,
    )
    c.check(
        claim is not None,
        "state.storage.volume_claim_template_exists",
        f"expected volumeClaimTemplates entry named {STATE_VOLUME_CLAIM_TEMPLATE_NAME!r}, found "
        f"{[t.get('metadata', {}).get('name') for t in claim_templates]}",
    )
    claim = claim or {}
    claim_spec = claim.get("spec") or {}
    c.check(
        claim_spec.get("accessModes") == ["ReadWriteOnce"],
        "state.storage.access_mode",
        f"expected volumeClaimTemplates accessModes == ['ReadWriteOnce'], found {claim_spec.get('accessModes')!r}",
    )
    c.check(
        claim_spec.get("volumeMode") in (None, "Filesystem"),
        "state.storage.volume_mode",
        f"expected volumeMode Filesystem (or omitted, which defaults to Filesystem), found "
        f"{claim_spec.get('volumeMode')!r}",
    )
    c.check(
        (claim_spec.get("resources") or {}).get("requests", {}).get("storage") == EXPECTED_STATE_CLAIM_STORAGE,
        "state.storage.requested_capacity",
        f"expected volumeClaimTemplates requests.storage == {EXPECTED_STATE_CLAIM_STORAGE!r}, found "
        f"{(claim_spec.get('resources') or {}).get('requests', {}).get('storage')!r}",
    )
    c.check(
        "storageClassName" not in claim_spec,
        "state.storage.default_storage_class",
        "expected storageClassName to be omitted so the cluster's default StorageClass is used, found "
        f"{claim_spec.get('storageClassName')!r}",
    )

    retention = sts_spec.get("persistentVolumeClaimRetentionPolicy") or {}
    c.check(
        retention.get("whenDeleted") == "Retain",
        "state.storage.retention_when_deleted",
        f"expected persistentVolumeClaimRetentionPolicy.whenDeleted == 'Retain', found {retention.get('whenDeleted')!r}",
    )
    c.check(
        retention.get("whenScaled") == "Retain",
        "state.storage.retention_when_scaled",
        f"expected persistentVolumeClaimRetentionPolicy.whenScaled == 'Retain', found {retention.get('whenScaled')!r}",
    )

    return pod_spec, pod_labels


def run_checks(docs: list[dict]) -> list[Finding]:
    c = _Checker(docs)

    namespaces = c.by_kind("Namespace")
    c.check(
        len(namespaces) == 1 and namespaces[0].get("metadata", {}).get("name") == EXPECTED_NAMESPACE,
        "namespace.exists",
        f"expected exactly one Namespace named {EXPECTED_NAMESPACE!r}, found "
        f"{[n.get('metadata', {}).get('name') for n in namespaces]}",
    )

    # ConfigMaps
    configmaps = c.by_kind("ConfigMap")
    cm_names = [cm.get("metadata", {}).get("name") for cm in configmaps]
    c.check(
        GATEWAY_CONFIGMAP in cm_names,
        "gateway.configmap.exists",
        f"expected ConfigMap {GATEWAY_CONFIGMAP!r} to exist, found {cm_names}",
    )
    c.check(
        APP_CONFIGMAP in cm_names,
        "app.configmap.exists",
        f"expected ConfigMap {APP_CONFIGMAP!r} to exist, found {cm_names}",
    )
    c.check(
        STATE_CONFIGMAP in cm_names,
        "state.configmap.exists",
        f"expected ConfigMap {STATE_CONFIGMAP!r} to exist, found {cm_names}",
    )
    gateway_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == GATEWAY_CONFIGMAP), None)
    app_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == APP_CONFIGMAP), None)
    state_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == STATE_CONFIGMAP), None)

    for label, cm in (("gateway", gateway_configmap), ("app", app_configmap), ("state", state_configmap)):
        cm_data = (cm or {}).get("data") or {}
        c.check(
            bool(cm_data) and all(not _looks_secret(k, v) for k, v in cm_data.items()),
            f"{label}.configmap.no_secret_like_values",
            f"ConfigMap data must be non-empty and non-secret-looking, found keys {list(cm_data.keys())}",
        )
        c.check(
            (cm or {}).get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
            f"{label}.configmap.namespace_matches",
            f"expected ConfigMap metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
            f"{(cm or {}).get('metadata', {}).get('namespace')!r}",
        )

    gw_cm_data = (gateway_configmap or {}).get("data") or {}
    c.check(
        gw_cm_data.get("BACKEND_HOST") == EXPECTED_BACKEND_HOST,
        "gateway.configmap.backend_host",
        f"expected gateway ConfigMap BACKEND_HOST == {EXPECTED_BACKEND_HOST!r}, found "
        f"{gw_cm_data.get('BACKEND_HOST')!r}",
    )
    c.check(
        not _is_ip_literal(gw_cm_data.get("BACKEND_HOST")),
        "gateway.configmap.backend_host_not_ip",
        f"expected BACKEND_HOST not to be a raw IP literal, found {gw_cm_data.get('BACKEND_HOST')!r}",
    )
    c.check(
        not _looks_pod_like(gw_cm_data.get("BACKEND_HOST")),
        "gateway.configmap.backend_host_not_pod_like",
        f"expected BACKEND_HOST not to be a hardcoded Pod/ReplicaSet-style identity, found "
        f"{gw_cm_data.get('BACKEND_HOST')!r}",
    )
    c.check(
        gw_cm_data.get("BACKEND_PORT") == EXPECTED_BACKEND_PORT,
        "gateway.configmap.backend_port",
        f"expected gateway ConfigMap BACKEND_PORT == {EXPECTED_BACKEND_PORT!r}, found "
        f"{gw_cm_data.get('BACKEND_PORT')!r}",
    )
    c.check(
        gw_cm_data.get("BACKEND_TIMEOUT_SECONDS") == EXPECTED_BACKEND_TIMEOUT_SECONDS,
        "gateway.configmap.backend_timeout_seconds",
        f"expected gateway ConfigMap BACKEND_TIMEOUT_SECONDS == {EXPECTED_BACKEND_TIMEOUT_SECONDS!r} "
        f"(DAY4-ARCH-M1: must comfortably exceed app's own STATE_TIMEOUT_SECONDS for the nested "
        f"gateway -> app -> state chain), found {gw_cm_data.get('BACKEND_TIMEOUT_SECONDS')!r}",
    )

    app_cm_data = (app_configmap or {}).get("data") or {}
    c.check(
        app_cm_data.get("STATE_TIMEOUT_SECONDS") == EXPECTED_STATE_TIMEOUT_SECONDS,
        "app.configmap.state_timeout_seconds",
        f"expected app ConfigMap STATE_TIMEOUT_SECONDS == {EXPECTED_STATE_TIMEOUT_SECONDS!r} "
        f"(the innermost budget the nested gateway -> app -> state timeout hierarchy is built from), "
        f"found {app_cm_data.get('STATE_TIMEOUT_SECONDS')!r}",
    )

    # Deployments
    deployments = c.by_kind("Deployment")
    c.check(
        len(deployments) == 2,
        "deployment.count",
        f"expected exactly two Deployments, found {len(deployments)}",
    )
    dep_names = [d.get("metadata", {}).get("name") for d in deployments]
    gateway_deployment = next((d for d in deployments if d.get("metadata", {}).get("name") == GATEWAY_DEPLOYMENT), None)
    app_deployment = next((d for d in deployments if d.get("metadata", {}).get("name") == APP_DEPLOYMENT), None)
    c.check(
        gateway_deployment is not None,
        "gateway.deployment.exists",
        f"expected Deployment {GATEWAY_DEPLOYMENT!r} to exist, found {dep_names}",
    )
    c.check(
        app_deployment is not None,
        "app.deployment.exists",
        f"expected Deployment {APP_DEPLOYMENT!r} to exist, found {dep_names}",
    )

    gw_pod_spec, gw_pod_labels, gw_container = _check_workload_security_and_probes(
        c, "gateway", gateway_deployment or {}, GATEWAY_CONTAINER, GATEWAY_IMAGE, GATEWAY_CONFIGMAP
    )
    app_pod_spec, app_pod_labels, app_container = _check_workload_security_and_probes(
        c, "app", app_deployment or {}, APP_CONTAINER, APP_IMAGE, APP_CONFIGMAP
    )

    # DAY4-ARCH-M1: readinessProbe.timeoutSeconds at each hop must stay
    # comfortably above the client-side call timeout it bounds - a
    # socket-level per-call timeout, not a total wall-clock deadline -
    # so a slow-but-healthy downstream never trips the probe before the
    # client call itself would have given up.
    gw_readiness = (gw_container or {}).get("readinessProbe") or {}
    c.check(
        gw_readiness.get("timeoutSeconds") == EXPECTED_GATEWAY_READINESS_TIMEOUT_SECONDS,
        "gateway.probes.readiness_timeout_seconds",
        f"expected gateway readinessProbe.timeoutSeconds == {EXPECTED_GATEWAY_READINESS_TIMEOUT_SECONDS!r} "
        f"(must exceed BACKEND_TIMEOUT_SECONDS={EXPECTED_BACKEND_TIMEOUT_SECONDS!r}), found "
        f"{gw_readiness.get('timeoutSeconds')!r}",
    )
    app_readiness = (app_container or {}).get("readinessProbe") or {}
    c.check(
        app_readiness.get("timeoutSeconds") == EXPECTED_APP_READINESS_TIMEOUT_SECONDS,
        "app.probes.readiness_timeout_seconds",
        f"expected app readinessProbe.timeoutSeconds == {EXPECTED_APP_READINESS_TIMEOUT_SECONDS!r} "
        f"(must exceed STATE_TIMEOUT_SECONDS={EXPECTED_STATE_TIMEOUT_SECONDS!r}), found "
        f"{app_readiness.get('timeoutSeconds')!r}",
    )

    # StatefulSet (DAY4)
    statefulsets = c.by_kind("StatefulSet")
    c.check(
        len(statefulsets) == 1,
        "statefulset.count",
        f"expected exactly one StatefulSet, found {len(statefulsets)}",
    )
    state_statefulset = next(
        (s for s in statefulsets if s.get("metadata", {}).get("name") == STATE_STATEFULSET), None
    )
    state_pod_spec, state_pod_labels = _check_state_statefulset(c, state_statefulset)

    # Services
    services = c.by_kind("Service")
    c.check(
        len(services) == 4,
        "service.count",
        f"expected exactly four Services (gateway/app/state/state-headless), found {len(services)}",
    )
    svc_names = [s.get("metadata", {}).get("name") for s in services]
    gateway_service = next((s for s in services if s.get("metadata", {}).get("name") == GATEWAY_SERVICE), None)
    app_service = next((s for s in services if s.get("metadata", {}).get("name") == APP_SERVICE), None)
    state_service = next(
        (
            s
            for s in services
            if s.get("metadata", {}).get("name") == STATE_SERVICE and (s.get("spec") or {}).get("clusterIP") != "None"
        ),
        None,
    )
    state_headless_service = next(
        (s for s in services if s.get("metadata", {}).get("name") == STATE_HEADLESS_SERVICE), None
    )
    c.check(
        gateway_service is not None,
        "gateway.service.exists",
        f"expected Service {GATEWAY_SERVICE!r} to exist, found {svc_names}",
    )
    c.check(
        app_service is not None,
        "app.service.exists",
        f"expected Service {APP_SERVICE!r} to exist, found {svc_names}",
    )
    c.check(
        state_service is not None,
        "state.service.exists",
        f"expected a normal ClusterIP Service {STATE_SERVICE!r} to exist, found {svc_names}",
    )
    c.check(
        state_headless_service is not None,
        "state.headless_service.exists",
        f"expected headless Service {STATE_HEADLESS_SERVICE!r} to exist, found {svc_names}",
    )
    state_headless_spec = (state_headless_service or {}).get("spec") or {}
    c.check(
        state_headless_spec.get("clusterIP") == "None",
        "state.headless_service.cluster_ip_none",
        f"expected governing Service {STATE_HEADLESS_SERVICE!r} clusterIP == 'None', found "
        f"{state_headless_spec.get('clusterIP')!r}",
    )
    state_headless_selector = state_headless_spec.get("selector") or {}
    c.check(
        bool(state_headless_selector) and all(state_pod_labels.get(k) == v for k, v in state_headless_selector.items()),
        "state.headless_service.selector_matches_pod_labels",
        f"expected headless Service selector {state_headless_selector} to be satisfied by state pod labels "
        f"{state_pod_labels}",
    )
    state_svc_spec = (state_service or {}).get("spec") or {}
    c.check(
        state_svc_spec.get("type", "ClusterIP") == "ClusterIP",
        "state.service.type_cluster_ip",
        f"expected Service type ClusterIP, found {state_svc_spec.get('type')!r}",
    )
    state_selector = state_svc_spec.get("selector") or {}
    c.check(
        bool(state_selector) and all(state_pod_labels.get(k) == v for k, v in state_selector.items()),
        "state.service.selector_matches_pod_labels",
        f"expected Service selector {state_selector} to be satisfied by state pod labels {state_pod_labels}",
    )

    gw_selector = None
    app_selector = None
    for label, svc, pod_labels in (
        ("gateway", gateway_service, gw_pod_labels),
        ("app", app_service, app_pod_labels),
    ):
        svc = svc or {}
        c.check(
            svc.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
            f"{label}.service.namespace_matches",
            f"expected Service metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
            f"{svc.get('metadata', {}).get('namespace')!r}",
        )
        svc_spec = svc.get("spec", {})
        c.check(
            svc_spec.get("type") == "ClusterIP",
            f"{label}.service.type_cluster_ip",
            f"expected Service type ClusterIP, found {svc_spec.get('type')!r}",
        )
        selector = svc_spec.get("selector") or {}
        c.check(
            bool(selector) and all(pod_labels.get(k) == v for k, v in selector.items()),
            f"{label}.service.selector_matches_pod_labels",
            f"expected Service selector {selector} to be satisfied by pod labels {pod_labels}",
        )
        svc_ports = svc_spec.get("ports") or []
        node_ports = [p for p in svc_ports if p.get("nodePort")]
        c.check(
            not node_ports,
            f"{label}.service.no_node_port",
            f"expected no nodePort set on any Service port, found {node_ports}",
        )
        if label == "gateway":
            gw_selector = selector
        else:
            app_selector = selector

    # Selector isolation: neither Service's selector may be satisfiable by
    # the other workload's pod labels (would silently load-balance traffic
    # across the wrong workload).
    gw_selector = gw_selector or {}
    app_selector = app_selector or {}
    c.check(
        not (bool(gw_selector) and all(app_pod_labels.get(k) == v for k, v in gw_selector.items())),
        "service.no_selector_collision_gateway_selects_app",
        f"gateway Service selector {gw_selector} must NOT be satisfied by app pod labels {app_pod_labels}",
    )
    c.check(
        not (bool(app_selector) and all(gw_pod_labels.get(k) == v for k, v in app_selector.items())),
        "service.no_selector_collision_app_selects_gateway",
        f"app Service selector {app_selector} must NOT be satisfied by gateway pod labels {gw_pod_labels}",
    )
    c.check(
        not (bool(state_selector) and all(app_pod_labels.get(k) == v for k, v in state_selector.items())),
        "service.no_selector_collision_state_selects_app",
        f"state Service selector {state_selector} must NOT be satisfied by app pod labels {app_pod_labels}",
    )
    c.check(
        not (bool(state_selector) and all(gw_pod_labels.get(k) == v for k, v in state_selector.items())),
        "service.no_selector_collision_state_selects_gateway",
        f"state Service selector {state_selector} must NOT be satisfied by gateway pod labels {gw_pod_labels}",
    )

    # PodDisruptionBudgets (DAY3) - gateway/app only. A single-replica
    # StatefulSet cannot carry a non-trivial disruption budget, so state
    # deliberately has none.
    pdbs = c.by_kind("PodDisruptionBudget")
    c.check(
        len(pdbs) == 2,
        "pdb.count",
        f"expected exactly two PodDisruptionBudgets (gateway/app only - state has none), found {len(pdbs)}",
    )
    pdb_names = [p.get("metadata", {}).get("name") for p in pdbs]
    gateway_pdb = next((p for p in pdbs if p.get("metadata", {}).get("name") == GATEWAY_PDB), None)
    app_pdb = next((p for p in pdbs if p.get("metadata", {}).get("name") == APP_PDB), None)
    c.check(
        gateway_pdb is not None,
        "gateway.pdb.named_correctly",
        f"expected PodDisruptionBudget {GATEWAY_PDB!r} to exist, found {pdb_names}",
    )
    c.check(
        app_pdb is not None,
        "app.pdb.named_correctly",
        f"expected PodDisruptionBudget {APP_PDB!r} to exist, found {pdb_names}",
    )
    _check_pdb(c, "gateway", gateway_pdb, gw_pod_labels, app_pod_labels)
    _check_pdb(c, "app", app_pdb, app_pod_labels, gw_pod_labels)

    # Forbidden Day 4 resources (Secret and a standalone PersistentVolumeClaim
    # must never be committed; HPA/RBAC/NetworkPolicy/Ingress remain
    # deferred to later days per docs/roadmap.md)
    present_kinds = {d.get("kind") for d in docs}
    forbidden_present = present_kinds & FORBIDDEN_KINDS
    c.check(
        not forbidden_present,
        "scope.no_forbidden_resources",
        f"expected none of {sorted(FORBIDDEN_KINDS)} present, found {sorted(forbidden_present)}",
    )

    # DAY4: exact total rendered object count - catches an accidentally
    # duplicated or missing object that individual per-kind counts above
    # wouldn't necessarily surface.
    c.check(
        len(docs) == EXPECTED_TOTAL_RENDERED_OBJECTS,
        "scope.total_rendered_object_count",
        f"expected exactly {EXPECTED_TOTAL_RENDERED_OBJECTS} rendered objects, found {len(docs)}",
    )

    return c.findings

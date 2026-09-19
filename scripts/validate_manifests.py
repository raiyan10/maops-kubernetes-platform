"""
Repository-owned static validation for the Day 5 Kubernetes manifests.

Operates on already-parsed Kubernetes objects (plain dict/list/scalar
Python structures - one entry per rendered document), not raw YAML
text, so the validation rules stay decoupled from parsing mechanics
and are directly unit-testable against constructed fixtures.

Day 5 keeps Day 4's three-workload architecture (gateway/app/state,
scaling, scheduling, rollout/rollback, PDBs, persistence) entirely
unchanged and adds security boundaries: a purpose-built ServiceAccount
per workload (automountServiceAccountToken: false, no RBAC binding), a
second namespace (`maops-day5-validation`) holding the one identity
that DOES receive an API token (`maops-diagnostics`) plus its
namespace-scoped Role/RoleBinding against `maops-platform`, and
standard `networking.k8s.io/v1` NetworkPolicy objects enforcing
default-deny ingress/egress with narrow, explicit allows (DNS,
validation-client -> gateway, gateway -> app, app -> state). Neither
Secret object (`maops-internal-auth`, `maops-state-auth`) is ever
rendered by k8s/base (see scope.no_forbidden_resources) - both are
created out-of-band by scripts/secret_bootstrap.py.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_VERSION = "0.5.0"
EXPECTED_INSTANCE = "maops-kubernetes-platform-day5"

# DAY5: validation namespace + identities.
VALIDATION_NAMESPACE = "maops-day5-validation"
GATEWAY_SERVICE_ACCOUNT = "maops-gateway"
APP_SERVICE_ACCOUNT = "maops-app"
STATE_SERVICE_ACCOUNT = "maops-state"
DIAGNOSTICS_SERVICE_ACCOUNT = "maops-diagnostics"
DIAGNOSTICS_ROLE = "maops-diagnostics-reader"
DIAGNOSTICS_ROLE_BINDING = "maops-diagnostics-reader-binding"
APPLICATION_SERVICE_ACCOUNTS = {GATEWAY_SERVICE_ACCOUNT, APP_SERVICE_ACCOUNT, STATE_SERVICE_ACCOUNT}

# DAY5: RBAC verbs/resources the diagnostics Role is allowed to grant,
# and a representative sample of what it must never grant - used both
# to assert the positive grant and to assert none of the negative
# surface leaked in via an overly broad rule (e.g. resources: ["*"] or
# verbs: ["*"] would technically "contain" get/list/watch too).
DIAGNOSTICS_ALLOWED_CORE_RESOURCES = {"pods", "services"}
DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES = {"endpointslices"}
DIAGNOSTICS_ALLOWED_VERBS = {"get", "list", "watch"}
DIAGNOSTICS_FORBIDDEN_RESOURCES = {
    "secrets",
    "deployments",
    "statefulsets",
    "replicasets",
    "pods/exec",
    "pods/eviction",
    "*",
}
DIAGNOSTICS_FORBIDDEN_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}

# DAY5: NetworkPolicy object names.
NETPOL_DEFAULT_DENY = "maops-default-deny-all"
NETPOL_ALLOW_DNS = "maops-allow-dns-egress"
NETPOL_ALLOW_GATEWAY_EGRESS_APP = "maops-allow-gateway-egress-to-app"
NETPOL_ALLOW_APP_INGRESS_GATEWAY = "maops-allow-app-ingress-from-gateway"
NETPOL_ALLOW_APP_EGRESS_STATE = "maops-allow-app-egress-to-state"
NETPOL_ALLOW_STATE_INGRESS_APP = "maops-allow-state-ingress-from-app"
NETPOL_ALLOW_GATEWAY_INGRESS_VALIDATION = "maops-allow-gateway-ingress-from-validation"
EXPECTED_NETWORK_POLICY_COUNT = 7
VALIDATION_CLIENT_COMPONENT = "validation-client"
DNS_PORT = 53
APP_PORT = 8080

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

# DAY5: total rendered portable application objects (excludes runtime
# Secrets and the generated PVC/PV, which are not part of k8s/base) -
# 2 Namespaces (maops-platform, maops-day5-validation) + 3 ConfigMaps
# (gateway/app/state) + 4 ServiceAccounts (gateway/app/state/
# diagnostics) + 1 Role + 1 RoleBinding + 2 Deployments + 1 StatefulSet
# + 4 Services (gateway/app/state/state-headless) + 2
# PodDisruptionBudgets (gateway/app only - state carries no PDB) + 7
# NetworkPolicies (default-deny + DNS + gateway<->app pair +
# app<->state pair + gateway<-validation).
EXPECTED_TOTAL_RENDERED_OBJECTS = 27

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
    # DAY5: Role/RoleBinding/ServiceAccount/NetworkPolicy are now
    # sanctioned (checked explicitly below) - but cluster-wide RBAC
    # remains forbidden at every day: Day 5's RBAC is deliberately
    # namespace-scoped only (see docs/roadmap.md).
    "ClusterRole",
    "ClusterRoleBinding",
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
    expected_service_account: str,
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

    # DAY5: purpose-built ServiceAccount, never the implicit `default`.
    c.check(
        pod_spec.get("serviceAccountName") == expected_service_account,
        f"{component}.security.service_account_name",
        f"expected serviceAccountName == {expected_service_account!r}, found {pod_spec.get('serviceAccountName')!r}",
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
    c.check(
        pod_spec.get("serviceAccountName") == STATE_SERVICE_ACCOUNT,
        "state.security.service_account_name",
        f"expected serviceAccountName == {STATE_SERVICE_ACCOUNT!r}, found {pod_spec.get('serviceAccountName')!r}",
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


def _check_service_accounts_and_rbac(c: _Checker) -> None:
    """DAY5: four ServiceAccounts (gateway/app/state - automount false,
    no RBAC binding - plus diagnostics, the one identity with automount
    true), one namespace-scoped Role, and one RoleBinding granting it to
    the diagnostics ServiceAccount only."""
    service_accounts = c.by_kind("ServiceAccount")
    c.check(
        len(service_accounts) == 4,
        "rbac.service_account_count",
        f"expected exactly 4 ServiceAccounts (gateway/app/state/diagnostics), found {len(service_accounts)}",
    )
    sa_by_name = {sa.get("metadata", {}).get("name"): sa for sa in service_accounts}

    for name in (GATEWAY_SERVICE_ACCOUNT, APP_SERVICE_ACCOUNT, STATE_SERVICE_ACCOUNT):
        sa = sa_by_name.get(name)
        c.check(
            sa is not None,
            f"rbac.service_account.{name}.exists",
            f"expected ServiceAccount {name!r} to exist, found {sorted(k for k in sa_by_name if k)}",
        )
        sa = sa or {}
        c.check(
            sa.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
            f"rbac.service_account.{name}.namespace",
            f"expected ServiceAccount {name!r} namespace == {EXPECTED_NAMESPACE!r}, found "
            f"{sa.get('metadata', {}).get('namespace')!r}",
        )
        c.check(
            sa.get("automountServiceAccountToken") is False,
            f"rbac.service_account.{name}.automount_false",
            f"expected ServiceAccount {name!r} automountServiceAccountToken == false, found "
            f"{sa.get('automountServiceAccountToken')!r}",
        )

    diagnostics_sa = sa_by_name.get(DIAGNOSTICS_SERVICE_ACCOUNT)
    c.check(
        diagnostics_sa is not None,
        "rbac.service_account.diagnostics.exists",
        f"expected ServiceAccount {DIAGNOSTICS_SERVICE_ACCOUNT!r} to exist, found {sorted(k for k in sa_by_name if k)}",
    )
    diagnostics_sa = diagnostics_sa or {}
    c.check(
        diagnostics_sa.get("metadata", {}).get("namespace") == VALIDATION_NAMESPACE,
        "rbac.service_account.diagnostics.namespace",
        f"expected ServiceAccount {DIAGNOSTICS_SERVICE_ACCOUNT!r} namespace == {VALIDATION_NAMESPACE!r}, found "
        f"{diagnostics_sa.get('metadata', {}).get('namespace')!r}",
    )
    c.check(
        diagnostics_sa.get("automountServiceAccountToken") is True,
        "rbac.service_account.diagnostics.automount_true",
        "expected ServiceAccount maops-diagnostics automountServiceAccountToken == true (it is the one identity "
        f"used to test API authorization), found {diagnostics_sa.get('automountServiceAccountToken')!r}",
    )

    roles = c.by_kind("Role")
    c.check(len(roles) == 1, "rbac.role_count", f"expected exactly one Role, found {len(roles)}")
    role = next((r for r in roles if r.get("metadata", {}).get("name") == DIAGNOSTICS_ROLE), None)
    c.check(
        role is not None,
        "rbac.role.exists",
        f"expected Role {DIAGNOSTICS_ROLE!r} to exist, found {[r.get('metadata', {}).get('name') for r in roles]}",
    )
    role = role or {}
    c.check(
        role.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "rbac.role.namespace_scoped",
        f"expected Role {DIAGNOSTICS_ROLE!r} namespace == {EXPECTED_NAMESPACE!r} (namespace-scoped, never "
        f"cluster-wide), found {role.get('metadata', {}).get('namespace')!r}",
    )

    rules = role.get("rules") or []
    granted_resources: set = set()
    granted_verbs: set = set()
    api_groups: set = set()
    for rule in rules:
        granted_resources.update(rule.get("resources") or [])
        granted_verbs.update(rule.get("verbs") or [])
        api_groups.update(rule.get("apiGroups") or [])

    c.check(
        DIAGNOSTICS_ALLOWED_CORE_RESOURCES <= granted_resources,
        "rbac.role.grants_pods_and_services",
        f"expected Role to grant access to {sorted(DIAGNOSTICS_ALLOWED_CORE_RESOURCES)}, found resources="
        f"{sorted(granted_resources)}",
    )
    c.check(
        DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES <= granted_resources,
        "rbac.role.grants_endpointslices",
        f"expected Role to grant access to {sorted(DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES)}, found resources="
        f"{sorted(granted_resources)}",
    )
    c.check(
        bool(granted_verbs) and granted_verbs <= DIAGNOSTICS_ALLOWED_VERBS,
        "rbac.role.verbs_read_only",
        f"expected every granted verb to be one of {sorted(DIAGNOSTICS_ALLOWED_VERBS)} (read-only, non-empty), "
        f"found {sorted(granted_verbs)}",
    )
    c.check(
        not (granted_resources & DIAGNOSTICS_FORBIDDEN_RESOURCES),
        "rbac.role.no_forbidden_resources",
        f"expected none of {sorted(DIAGNOSTICS_FORBIDDEN_RESOURCES)} granted, found overlap "
        f"{sorted(granted_resources & DIAGNOSTICS_FORBIDDEN_RESOURCES)}",
    )
    c.check(
        not (granted_verbs & DIAGNOSTICS_FORBIDDEN_VERBS),
        "rbac.role.no_forbidden_verbs",
        f"expected none of {sorted(DIAGNOSTICS_FORBIDDEN_VERBS)} granted, found overlap "
        f"{sorted(granted_verbs & DIAGNOSTICS_FORBIDDEN_VERBS)}",
    )
    c.check(
        api_groups <= {"", "discovery.k8s.io"},
        "rbac.role.api_groups_scoped",
        f"expected Role apiGroups to be a subset of {{'', 'discovery.k8s.io'}} (never a wildcard), found "
        f"{sorted(api_groups)}",
    )

    role_bindings = c.by_kind("RoleBinding")
    c.check(
        len(role_bindings) == 1,
        "rbac.role_binding_count",
        f"expected exactly one RoleBinding, found {len(role_bindings)}",
    )
    binding = next((b for b in role_bindings if b.get("metadata", {}).get("name") == DIAGNOSTICS_ROLE_BINDING), None)
    c.check(
        binding is not None,
        "rbac.role_binding.exists",
        f"expected RoleBinding {DIAGNOSTICS_ROLE_BINDING!r} to exist, found "
        f"{[b.get('metadata', {}).get('name') for b in role_bindings]}",
    )
    binding = binding or {}
    c.check(
        binding.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "rbac.role_binding.namespace_scoped",
        f"expected RoleBinding namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{binding.get('metadata', {}).get('namespace')!r}",
    )
    role_ref = binding.get("roleRef") or {}
    c.check(
        role_ref.get("kind") == "Role" and role_ref.get("name") == DIAGNOSTICS_ROLE,
        "rbac.role_binding.references_role_not_cluster_role",
        f"expected roleRef {{kind: Role, name: {DIAGNOSTICS_ROLE!r}}} (never ClusterRole), found {role_ref!r}",
    )
    subjects = binding.get("subjects") or []
    diagnostics_subject = next(
        (
            s
            for s in subjects
            if s.get("kind") == "ServiceAccount"
            and s.get("name") == DIAGNOSTICS_SERVICE_ACCOUNT
            and s.get("namespace") == VALIDATION_NAMESPACE
        ),
        None,
    )
    c.check(
        diagnostics_subject is not None,
        "rbac.role_binding.subject_is_diagnostics_sa",
        f"expected a subject {{kind: ServiceAccount, name: {DIAGNOSTICS_SERVICE_ACCOUNT!r}, "
        f"namespace: {VALIDATION_NAMESPACE!r}}}, found subjects={subjects!r}",
    )

    # Application ServiceAccounts must never be bound to any Role/ClusterRole.
    bound_subject_names = {
        s.get("name")
        for b in role_bindings
        for s in (b.get("subjects") or [])
        if s.get("kind") == "ServiceAccount"
    }
    c.check(
        not (bound_subject_names & APPLICATION_SERVICE_ACCOUNTS),
        "rbac.application_service_accounts_not_bound",
        f"expected none of {sorted(APPLICATION_SERVICE_ACCOUNTS)} to appear as a RoleBinding subject, found "
        f"overlap {sorted(bound_subject_names & APPLICATION_SERVICE_ACCOUNTS)}",
    )


def _netpol_egress(policy: dict) -> list[dict]:
    return (policy.get("spec") or {}).get("egress") or []


def _netpol_ingress(policy: dict) -> list[dict]:
    return (policy.get("spec") or {}).get("ingress") or []


def _peer_has_component(peer: dict, component: str) -> bool:
    return (peer.get("podSelector") or {}).get("matchLabels", {}).get("app.kubernetes.io/component") == component


def _peer_in_namespace(peer: dict, namespace: str) -> bool:
    return (peer.get("namespaceSelector") or {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == namespace


def _rule_allows_port(rule: dict, port: int, protocol: str) -> bool:
    ports = rule.get("ports") or []
    return any(p.get("port") == port and p.get("protocol") == protocol for p in ports)


def _check_network_policies(c: _Checker) -> None:
    """DAY5: default-deny ingress+egress for every Pod in maops-platform,
    plus exactly the narrow allows the architecture requires - checked
    both positively (the allow exists with the right selector/port) and
    negatively (the specific forbidden paths - validation-client -> app/
    state, gateway -> state - are never satisfiable by any rule)."""
    policies = c.by_kind("NetworkPolicy")
    c.check(
        len(policies) == EXPECTED_NETWORK_POLICY_COUNT,
        "networkpolicy.count",
        f"expected exactly {EXPECTED_NETWORK_POLICY_COUNT} NetworkPolicies, found {len(policies)}",
    )
    by_name = {p.get("metadata", {}).get("name"): p for p in policies}

    for policy in policies:
        name = policy.get("metadata", {}).get("name")
        c.check(
            policy.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
            f"networkpolicy.{name}.namespace_matches",
            f"expected NetworkPolicy metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
            f"{policy.get('metadata', {}).get('namespace')!r}",
        )

    # default-deny-all
    deny_all = by_name.get(NETPOL_DEFAULT_DENY)
    c.check(
        deny_all is not None,
        "networkpolicy.default_deny.exists",
        f"expected NetworkPolicy {NETPOL_DEFAULT_DENY!r} to exist, found {sorted(k for k in by_name if k)}",
    )
    deny_all = deny_all or {}
    deny_spec = deny_all.get("spec") or {}
    c.check(
        deny_spec.get("podSelector") == {},
        "networkpolicy.default_deny.applies_to_all_pods",
        f"expected default-deny podSelector == {{}} (every Pod in the namespace), found "
        f"{deny_spec.get('podSelector')!r}",
    )
    c.check(
        set(deny_spec.get("policyTypes") or []) == {"Ingress", "Egress"},
        "networkpolicy.default_deny.both_directions",
        f"expected policyTypes == ['Ingress', 'Egress'], found {deny_spec.get('policyTypes')!r}",
    )
    c.check(
        not deny_spec.get("ingress") and not deny_spec.get("egress"),
        "networkpolicy.default_deny.no_rules",
        "expected no ingress/egress rules on the default-deny policy (absence of rules is what makes it "
        f"deny-all), found ingress={deny_spec.get('ingress')!r} egress={deny_spec.get('egress')!r}",
    )

    # allow-dns-egress
    dns = by_name.get(NETPOL_ALLOW_DNS)
    c.check(
        dns is not None,
        "networkpolicy.dns.exists",
        f"expected NetworkPolicy {NETPOL_ALLOW_DNS!r} to exist, found {sorted(k for k in by_name if k)}",
    )
    dns = dns or {}
    c.check(
        (dns.get("spec") or {}).get("podSelector") == {},
        "networkpolicy.dns.applies_to_all_pods",
        f"expected DNS-allow podSelector == {{}} (every workload needs DNS), found "
        f"{(dns.get('spec') or {}).get('podSelector')!r}",
    )
    dns_ok = any(
        _peer_in_namespace(peer, "kube-system")
        and (peer.get("podSelector") or {}).get("matchLabels", {}).get("k8s-app") == "kube-dns"
        and _rule_allows_port(rule, DNS_PORT, "UDP")
        and _rule_allows_port(rule, DNS_PORT, "TCP")
        for rule in _netpol_egress(dns)
        for peer in rule.get("to") or []
    )
    c.check(
        dns_ok,
        "networkpolicy.dns.allows_udp_tcp_53_to_coredns",
        f"expected an egress rule to kube-system/k8s-app=kube-dns on UDP+TCP port {DNS_PORT}, found egress="
        f"{_netpol_egress(dns)!r}",
    )

    # gateway -> app pair
    gw_egress = by_name.get(NETPOL_ALLOW_GATEWAY_EGRESS_APP) or {}
    c.check(
        (gw_egress.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        == "gateway",
        "networkpolicy.gateway_egress_app.pod_selector_is_gateway",
        f"expected podSelector component == 'gateway', found {(gw_egress.get('spec') or {}).get('podSelector')!r}",
    )
    c.check(
        any(
            _peer_has_component(peer, "app") and _rule_allows_port(rule, APP_PORT, "TCP")
            for rule in _netpol_egress(gw_egress)
            for peer in rule.get("to") or []
        ),
        "networkpolicy.gateway_egress_app.allows_app_8080",
        f"expected an egress rule to component=app on TCP {APP_PORT}, found egress={_netpol_egress(gw_egress)!r}",
    )
    c.check(
        not any(
            _peer_has_component(peer, "state") for rule in _netpol_egress(gw_egress) for peer in rule.get("to") or []
        ),
        "networkpolicy.gateway_egress_app.never_targets_state",
        "expected gateway's egress allow to never target component=state (gateway -> state must stay denied)",
    )

    app_ingress_gw = by_name.get(NETPOL_ALLOW_APP_INGRESS_GATEWAY) or {}
    c.check(
        (app_ingress_gw.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        == "app",
        "networkpolicy.app_ingress_gateway.pod_selector_is_app",
        f"expected podSelector component == 'app', found {(app_ingress_gw.get('spec') or {}).get('podSelector')!r}",
    )
    c.check(
        any(
            _peer_has_component(peer, "gateway") and _rule_allows_port(rule, APP_PORT, "TCP")
            for rule in _netpol_ingress(app_ingress_gw)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.app_ingress_gateway.allows_gateway_8080",
        f"expected an ingress rule from component=gateway on TCP {APP_PORT}, found ingress="
        f"{_netpol_ingress(app_ingress_gw)!r}",
    )
    c.check(
        not any(
            _peer_in_namespace(peer, VALIDATION_NAMESPACE)
            for rule in _netpol_ingress(app_ingress_gw)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.app_ingress_gateway.never_allows_validation_namespace",
        "expected app's ingress allow to never reference the validation namespace (validation-client -> app must "
        "stay denied)",
    )

    # app -> state pair
    app_egress_state = by_name.get(NETPOL_ALLOW_APP_EGRESS_STATE) or {}
    c.check(
        (app_egress_state.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        == "app",
        "networkpolicy.app_egress_state.pod_selector_is_app",
        f"expected podSelector component == 'app', found {(app_egress_state.get('spec') or {}).get('podSelector')!r}",
    )
    c.check(
        any(
            _peer_has_component(peer, "state") and _rule_allows_port(rule, APP_PORT, "TCP")
            for rule in _netpol_egress(app_egress_state)
            for peer in rule.get("to") or []
        ),
        "networkpolicy.app_egress_state.allows_state_8080",
        f"expected an egress rule to component=state on TCP {APP_PORT}, found egress="
        f"{_netpol_egress(app_egress_state)!r}",
    )

    state_ingress_app = by_name.get(NETPOL_ALLOW_STATE_INGRESS_APP) or {}
    c.check(
        (state_ingress_app.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        == "state",
        "networkpolicy.state_ingress_app.pod_selector_is_state",
        f"expected podSelector component == 'state', found {(state_ingress_app.get('spec') or {}).get('podSelector')!r}",
    )
    c.check(
        any(
            _peer_has_component(peer, "app") and _rule_allows_port(rule, APP_PORT, "TCP")
            for rule in _netpol_ingress(state_ingress_app)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.state_ingress_app.allows_app_8080",
        f"expected an ingress rule from component=app on TCP {APP_PORT}, found ingress="
        f"{_netpol_ingress(state_ingress_app)!r}",
    )
    c.check(
        not any(
            _peer_has_component(peer, "gateway")
            for rule in _netpol_ingress(state_ingress_app)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.state_ingress_app.never_allows_gateway",
        "expected state's ingress allow to never reference component=gateway (gateway -> state must stay denied)",
    )
    c.check(
        not any(
            _peer_in_namespace(peer, VALIDATION_NAMESPACE)
            for rule in _netpol_ingress(state_ingress_app)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.state_ingress_app.never_allows_validation_namespace",
        "expected state's ingress allow to never reference the validation namespace (validation-client -> state "
        "must stay denied)",
    )

    # gateway <- validation-client
    gw_ingress_validation = by_name.get(NETPOL_ALLOW_GATEWAY_INGRESS_VALIDATION) or {}
    c.check(
        (gw_ingress_validation.get("spec") or {}).get("podSelector", {}).get("matchLabels", {}).get(
            "app.kubernetes.io/component"
        )
        == "gateway",
        "networkpolicy.gateway_ingress_validation.pod_selector_is_gateway",
        f"expected podSelector component == 'gateway', found "
        f"{(gw_ingress_validation.get('spec') or {}).get('podSelector')!r}",
    )
    c.check(
        any(
            _peer_in_namespace(peer, VALIDATION_NAMESPACE)
            and _peer_has_component(peer, VALIDATION_CLIENT_COMPONENT)
            and _rule_allows_port(rule, APP_PORT, "TCP")
            for rule in _netpol_ingress(gw_ingress_validation)
            for peer in rule.get("from") or []
        ),
        "networkpolicy.gateway_ingress_validation.allows_validation_client_8080",
        f"expected an ingress rule from namespace={VALIDATION_NAMESPACE!r}/component={VALIDATION_CLIENT_COMPONENT!r} "
        f"on TCP {APP_PORT}, found ingress={_netpol_ingress(gw_ingress_validation)!r}",
    )

    # Cross-cutting negative: no OTHER NetworkPolicy document introduces a
    # stray ingress rule referencing the validation namespace (a bypass
    # under a different object name would evade the per-policy checks
    # above, which only inspect the specific documents expected to carry
    # such a rule).
    already_checked = {
        NETPOL_ALLOW_APP_INGRESS_GATEWAY,
        NETPOL_ALLOW_STATE_INGRESS_APP,
        NETPOL_ALLOW_GATEWAY_INGRESS_VALIDATION,
    }
    for policy in policies:
        name = policy.get("metadata", {}).get("name")
        if name in already_checked:
            continue
        stray = any(
            _peer_in_namespace(peer, VALIDATION_NAMESPACE) for rule in _netpol_ingress(policy) for peer in rule.get("from") or []
        )
        c.check(
            not stray,
            f"networkpolicy.{name}.no_stray_validation_namespace_ingress",
            f"expected NetworkPolicy {name!r} to carry no ingress rule referencing the validation namespace",
        )


def run_checks(docs: list[dict]) -> list[Finding]:
    c = _Checker(docs)

    namespaces = c.by_kind("Namespace")
    ns_names = [n.get("metadata", {}).get("name") for n in namespaces]
    c.check(
        len(namespaces) == 2,
        "namespace.count",
        f"expected exactly two Namespaces (application + validation), found {len(namespaces)}: {ns_names}",
    )
    c.check(
        EXPECTED_NAMESPACE in ns_names,
        "namespace.exists",
        f"expected a Namespace named {EXPECTED_NAMESPACE!r}, found {ns_names}",
    )
    c.check(
        VALIDATION_NAMESPACE in ns_names,
        "namespace.validation_exists",
        f"expected a Namespace named {VALIDATION_NAMESPACE!r}, found {ns_names}",
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
        c, "gateway", gateway_deployment or {}, GATEWAY_CONTAINER, GATEWAY_IMAGE, GATEWAY_CONFIGMAP, GATEWAY_SERVICE_ACCOUNT
    )
    app_pod_spec, app_pod_labels, app_container = _check_workload_security_and_probes(
        c, "app", app_deployment or {}, APP_CONTAINER, APP_IMAGE, APP_CONFIGMAP, APP_SERVICE_ACCOUNT
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

    # ServiceAccounts, RBAC (DAY5), and NetworkPolicy (DAY5).
    _check_service_accounts_and_rbac(c)
    _check_network_policies(c)

    # Forbidden resources (Secret and a standalone PersistentVolumeClaim
    # must never be committed; HPA/ClusterRole/ClusterRoleBinding/Ingress
    # remain forbidden - Day 5's RBAC is namespace-scoped only, per
    # docs/roadmap.md)
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

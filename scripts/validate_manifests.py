"""
Repository-owned static validation for the Day 2 Kubernetes manifests.

Operates on already-parsed Kubernetes objects (plain dict/list/scalar
Python structures - one entry per rendered document), not raw YAML
text, so the validation rules stay decoupled from parsing mechanics
and are directly unit-testable against constructed fixtures.

Day 2 introduces a second workload (gateway) alongside the Day 1 app
workload, real service discovery (gateway -> app via the Kubernetes
Service DNS name), two workload-specific ConfigMaps, and a runtime
Secret consumed by both workloads as a read-only volume. The Secret
object itself is never rendered by k8s/base (see scope.no_forbidden_resources)
- it's created out-of-band by scripts/secret_bootstrap.py.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_VERSION = "0.2.0"

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
GATEWAY_SERVICE = "maops-gateway"
APP_SERVICE = "maops-app"
GATEWAY_CONFIGMAP = "maops-gateway-config"
APP_CONFIGMAP = "maops-app-config"
GATEWAY_CONTAINER = "maops-gateway"
APP_CONTAINER = "maops-app"
GATEWAY_IMAGE = f"maops-kubernetes-gateway:{EXPECTED_VERSION}"
APP_IMAGE = f"maops-kubernetes-app:{EXPECTED_VERSION}"

EXPECTED_REPLICAS = 2
EXPECTED_REQUESTS = {"cpu": "50m", "memory": "32Mi"}
EXPECTED_LIMITS = {"cpu": "250m", "memory": "128Mi"}

SECRET_NAME = "maops-internal-auth"
SECRET_VOLUME_NAME = "internal-auth"
SECRET_MOUNT_PATH = "/var/run/secrets/maops"
SECRET_KEY = "internal-token"

EXPECTED_BACKEND_HOST = "maops-app"
EXPECTED_BACKEND_PORT = "8080"

FORBIDDEN_KINDS = {
    "Secret",
    "Ingress",
    "PersistentVolumeClaim",
    "StatefulSet",
    "Role",
    "RoleBinding",
    "ClusterRole",
    "ClusterRoleBinding",
    "ServiceAccount",
    "NetworkPolicy",
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


def _check_workload_security_and_probes(
    c: _Checker,
    component: str,
    deployment: dict,
    expected_container: str,
    expected_image: str,
    expected_configmap: str,
) -> tuple[dict, dict, dict]:
    """Checks shared by both workloads: namespace, replicas, image,
    probes, resources, pod/container security baseline, ConfigMap wiring,
    Secret volume/mount wiring. Returns (pod_spec, pod_labels, container)
    for the caller's workload-specific checks (selectors, backend wiring)."""

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

    pod_spec = dep_spec.get("template", {}).get("spec", {})
    pod_labels = dep_spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
    c.check(
        pod_labels.get("app.kubernetes.io/version") == EXPECTED_VERSION,
        f"{component}.pod_template.version_label",
        f"expected pod template app.kubernetes.io/version == {EXPECTED_VERSION!r}, found "
        f"{pod_labels.get('app.kubernetes.io/version')!r}",
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

    # Secret volume/mount wiring (DAY2)
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
    gateway_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == GATEWAY_CONFIGMAP), None)
    app_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == APP_CONFIGMAP), None)

    for label, cm in (("gateway", gateway_configmap), ("app", app_configmap)):
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

    # Services
    services = c.by_kind("Service")
    c.check(
        len(services) == 2,
        "service.count",
        f"expected exactly two Services, found {len(services)}",
    )
    svc_names = [s.get("metadata", {}).get("name") for s in services]
    gateway_service = next((s for s in services if s.get("metadata", {}).get("name") == GATEWAY_SERVICE), None)
    app_service = next((s for s in services if s.get("metadata", {}).get("name") == APP_SERVICE), None)
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

    # Forbidden Day 2 resources (includes: Secret must never be committed)
    present_kinds = {d.get("kind") for d in docs}
    forbidden_present = present_kinds & FORBIDDEN_KINDS
    c.check(
        not forbidden_present,
        "scope.no_forbidden_resources",
        f"expected none of {sorted(FORBIDDEN_KINDS)} present, found {sorted(forbidden_present)}",
    )

    return c.findings

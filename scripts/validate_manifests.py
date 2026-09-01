"""
Repository-owned static validation for the Day 1 Kubernetes manifests.

Operates on already-parsed Kubernetes objects (plain dict/list/scalar
Python structures - one entry per rendered document), not raw YAML
text, so the validation rules stay decoupled from parsing mechanics
and are directly unit-testable against constructed fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_CONFIGMAP = "maops-app-config"
EXPECTED_DEPLOYMENT = "maops-app"
EXPECTED_SERVICE = "maops-app"
EXPECTED_CONTAINER = "maops-app"
EXPECTED_IMAGE = "maops-kubernetes-platform:0.1.0"
EXPECTED_REPLICAS = 2
EXPECTED_REQUESTS = {"cpu": "50m", "memory": "32Mi"}
EXPECTED_LIMITS = {"cpu": "250m", "memory": "128Mi"}

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


def run_checks(docs: list[dict]) -> list[Finding]:
    c = _Checker(docs)

    namespaces = c.by_kind("Namespace")
    c.check(
        len(namespaces) == 1 and namespaces[0].get("metadata", {}).get("name") == EXPECTED_NAMESPACE,
        "namespace.exists",
        f"expected exactly one Namespace named {EXPECTED_NAMESPACE!r}, found "
        f"{[n.get('metadata', {}).get('name') for n in namespaces]}",
    )

    configmaps = c.by_kind("ConfigMap")
    cm_names = [cm.get("metadata", {}).get("name") for cm in configmaps]
    c.check(
        EXPECTED_CONFIGMAP in cm_names,
        "configmap.exists",
        f"expected ConfigMap {EXPECTED_CONFIGMAP!r} to exist, found {cm_names}",
    )
    app_configmap = next((cm for cm in configmaps if cm.get("metadata", {}).get("name") == EXPECTED_CONFIGMAP), None)
    cm_data = (app_configmap or {}).get("data") or {}
    c.check(
        bool(cm_data) and all(not _looks_secret(k, v) for k, v in cm_data.items()),
        "configmap.no_secret_like_values",
        f"ConfigMap data must be non-empty and non-secret-looking, found keys {list(cm_data.keys())}",
    )
    c.check(
        (app_configmap or {}).get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "configmap.namespace_matches",
        f"expected ConfigMap {EXPECTED_CONFIGMAP!r} metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{(app_configmap or {}).get('metadata', {}).get('namespace')!r}",
    )

    deployments = c.by_kind("Deployment")
    c.check(
        len(deployments) == 1,
        "deployment.count",
        f"expected exactly one Deployment, found {len(deployments)}",
    )
    deployment = deployments[0] if len(deployments) == 1 else (deployments[0] if deployments else {})
    dep_spec = deployment.get("spec", {})

    c.check(
        deployment.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "deployment.namespace_matches",
        f"expected Deployment metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{deployment.get('metadata', {}).get('namespace')!r}",
    )

    c.check(
        dep_spec.get("replicas") == EXPECTED_REPLICAS,
        "deployment.replicas",
        f"expected replicas == {EXPECTED_REPLICAS}, found {dep_spec.get('replicas')!r}",
    )

    pod_spec = dep_spec.get("template", {}).get("spec", {})
    pod_labels = dep_spec.get("template", {}).get("metadata", {}).get("labels", {}) or {}
    containers = pod_spec.get("containers") or []
    c.check(
        len(containers) == 1,
        "deployment.single_container",
        f"expected exactly one container, found {len(containers)}",
    )
    container = containers[0] if containers else {}

    c.check(
        container.get("name") == EXPECTED_CONTAINER,
        "deployment.container_name",
        f"expected container name {EXPECTED_CONTAINER!r}, found {container.get('name')!r}",
    )
    c.check(
        container.get("image") == EXPECTED_IMAGE,
        "deployment.image",
        f"expected image {EXPECTED_IMAGE!r}, found {container.get('image')!r}",
    )
    c.check(
        container.get("imagePullPolicy") == "IfNotPresent",
        "deployment.image_pull_policy",
        f"expected imagePullPolicy IfNotPresent, found {container.get('imagePullPolicy')!r}",
    )

    # Probes
    startup = container.get("startupProbe") or {}
    liveness = container.get("livenessProbe") or {}
    readiness = container.get("readinessProbe") or {}
    c.check(
        bool(startup.get("httpGet")),
        "probes.startup_present",
        f"expected an HTTP startupProbe, found {startup!r}",
    )
    c.check(
        startup.get("httpGet", {}).get("path") == "/livez",
        "probes.startup_path",
        f"expected startupProbe httpGet path /livez, found {startup.get('httpGet', {}).get('path')!r}",
    )
    c.check(
        liveness.get("httpGet", {}).get("path") == "/livez",
        "probes.liveness_path",
        f"expected livenessProbe httpGet path /livez, found {liveness.get('httpGet', {}).get('path')!r}",
    )
    c.check(
        readiness.get("httpGet", {}).get("path") == "/readyz",
        "probes.readiness_path",
        f"expected readinessProbe httpGet path /readyz, found {readiness.get('httpGet', {}).get('path')!r}",
    )

    # Resources
    resources = container.get("resources") or {}
    c.check(
        resources.get("requests") == EXPECTED_REQUESTS,
        "resources.requests",
        f"expected requests {EXPECTED_REQUESTS}, found {resources.get('requests')!r}",
    )
    c.check(
        resources.get("limits") == EXPECTED_LIMITS,
        "resources.limits",
        f"expected limits {EXPECTED_LIMITS}, found {resources.get('limits')!r}",
    )

    # Security context - pod level
    pod_sc = pod_spec.get("securityContext") or {}
    c.check(
        pod_sc.get("runAsNonRoot") is True,
        "security.pod_run_as_non_root",
        f"expected pod securityContext.runAsNonRoot == true, found {pod_sc.get('runAsNonRoot')!r}",
    )
    c.check(
        pod_sc.get("runAsUser") == 10001,
        "security.pod_run_as_user",
        f"expected pod securityContext.runAsUser == 10001, found {pod_sc.get('runAsUser')!r}",
    )
    c.check(
        pod_sc.get("runAsGroup") == 10001,
        "security.pod_run_as_group",
        f"expected pod securityContext.runAsGroup == 10001, found {pod_sc.get('runAsGroup')!r}",
    )
    c.check(
        (pod_sc.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
        "security.seccomp_profile",
        f"expected seccompProfile.type == RuntimeDefault, found {pod_sc.get('seccompProfile')!r}",
    )

    # Security context - container level
    container_sc = container.get("securityContext") or {}
    c.check(
        container_sc.get("allowPrivilegeEscalation") is False,
        "security.allow_privilege_escalation",
        f"expected allowPrivilegeEscalation == false, found {container_sc.get('allowPrivilegeEscalation')!r}",
    )
    c.check(
        container_sc.get("readOnlyRootFilesystem") is True,
        "security.read_only_root_filesystem",
        f"expected readOnlyRootFilesystem == true, found {container_sc.get('readOnlyRootFilesystem')!r}",
    )
    c.check(
        (container_sc.get("capabilities") or {}).get("drop") == ["ALL"],
        "security.capabilities_drop_all",
        f"expected capabilities.drop == ['ALL'], found {container_sc.get('capabilities')!r}",
    )

    c.check(
        pod_spec.get("automountServiceAccountToken") is False,
        "security.automount_service_account_token",
        f"expected automountServiceAccountToken == false, found {pod_spec.get('automountServiceAccountToken')!r}",
    )

    c.check(
        not pod_spec.get("hostNetwork"),
        "security.no_host_network",
        f"expected no hostNetwork, found {pod_spec.get('hostNetwork')!r}",
    )
    container_ports = container.get("ports") or []
    host_ports = [p for p in container_ports if p.get("hostPort")]
    c.check(
        not host_ports,
        "security.no_host_port",
        f"expected no hostPort on any container port, found {host_ports}",
    )
    c.check(
        not pod_spec.get("hostPID"),
        "security.no_host_pid",
        f"expected no hostPID, found {pod_spec.get('hostPID')!r}",
    )
    c.check(
        not pod_spec.get("hostIPC"),
        "security.no_host_ipc",
        f"expected no hostIPC, found {pod_spec.get('hostIPC')!r}",
    )
    c.check(
        container_sc.get("privileged") is not True,
        "security.not_privileged",
        f"expected container securityContext.privileged != true, found {container_sc.get('privileged')!r}",
    )
    volumes = pod_spec.get("volumes") or []
    hostpath_volumes = [v for v in volumes if isinstance(v, dict) and "hostPath" in v]
    c.check(
        not hostpath_volumes,
        "security.no_host_path_volumes",
        f"expected no hostPath volumes, found {hostpath_volumes}",
    )

    # ConfigMap wiring
    env_from = container.get("envFrom") or []
    env = container.get("env") or []
    wired_via_env_from = any((ef.get("configMapRef") or {}).get("name") == EXPECTED_CONFIGMAP for ef in env_from)
    wired_via_env = any(
        (e.get("valueFrom") or {}).get("configMapKeyRef", {}).get("name") == EXPECTED_CONFIGMAP for e in env
    )
    c.check(
        wired_via_env_from or wired_via_env,
        "configmap.wired_to_container",
        f"expected container to consume ConfigMap {EXPECTED_CONFIGMAP!r} via envFrom or env, found "
        f"envFrom={env_from!r} env={env!r}",
    )

    # Service
    services = c.by_kind("Service")
    app_services = [s for s in services if s.get("metadata", {}).get("name") == EXPECTED_SERVICE]
    c.check(
        len(app_services) == 1,
        "service.exists",
        f"expected exactly one Service named {EXPECTED_SERVICE!r}, found {len(app_services)}",
    )
    service = app_services[0] if app_services else {}
    c.check(
        service.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE,
        "service.namespace_matches",
        f"expected Service metadata.namespace == {EXPECTED_NAMESPACE!r}, found "
        f"{service.get('metadata', {}).get('namespace')!r}",
    )
    svc_spec = service.get("spec", {})
    c.check(
        svc_spec.get("type") == "ClusterIP",
        "service.type_cluster_ip",
        f"expected Service type ClusterIP, found {svc_spec.get('type')!r}",
    )
    svc_selector = svc_spec.get("selector") or {}
    c.check(
        bool(svc_selector) and all(pod_labels.get(k) == v for k, v in svc_selector.items()),
        "service.selector_matches_pod_labels",
        f"expected Service selector {svc_selector} to be satisfied by pod labels {pod_labels}",
    )
    svc_ports = svc_spec.get("ports") or []
    node_ports = [p for p in svc_ports if p.get("nodePort")]
    c.check(
        not node_ports,
        "service.no_node_port",
        f"expected no nodePort set on any Service port, found {node_ports}",
    )

    # Forbidden Day 1 resources
    present_kinds = {d.get("kind") for d in docs}
    forbidden_present = present_kinds & FORBIDDEN_KINDS
    c.check(
        not forbidden_present,
        "scope.no_forbidden_resources",
        f"expected none of {sorted(FORBIDDEN_KINDS)} present, found {sorted(forbidden_present)}",
    )

    return c.findings


def _looks_secret(key: str, value) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("secret", "password", "token", "key", "credential"))

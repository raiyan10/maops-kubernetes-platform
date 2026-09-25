"""
Repository-owned static validation for the Day 6 Helm-rendered
application chart (charts/maops-kubernetes-platform).

Operates on already-parsed Kubernetes objects (plain dict/list/scalar
Python structures, one entry per rendered document - the same
`k8s_yaml.py`-parsed shape `scripts/validate_manifests.py` uses for
k8s/base), not raw YAML text, so the validation rules stay decoupled
from parsing mechanics and are directly unit-testable against
constructed fixtures.

This module is entirely independent of `scripts/validate_manifests.py`
(never imports it, never shares its constants) - k8s/base is a frozen,
separate Day 5 source that this file's checks never touch or assume
anything about, and `scripts/helm_check.py`'s own
`check_k8s_base_still_frozen()` proves that independently, by reading
k8s/base's OWN validator constants, not by comparing rendered objects.

Covers the Day 6 STATIC HELM VALIDATION requirements: exact rendered
inventory, chart/app/image versions, absence of Secret objects, intact
workload securityContext, least-privilege ServiceAccounts/RBAC, exact
NetworkPolicy topology, STRICT PeerAuthentication, exact
AuthorizationPolicy principals/targets, exact Gateway/HTTPRoute
references, and no Ingress/waypoint object anywhere in the chart's own
rendered output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_VERSION = "0.6.0"
EXPECTED_INSTANCE = "maops-kubernetes-platform-day6"

VALIDATION_NAMESPACE = "maops-day6-validation"
INGRESS_NAMESPACE = "maops-ingress"

GATEWAY_SERVICE_ACCOUNT = "maops-gateway"
APP_SERVICE_ACCOUNT = "maops-app"
STATE_SERVICE_ACCOUNT = "maops-state"
APPLICATION_SERVICE_ACCOUNTS = {GATEWAY_SERVICE_ACCOUNT, APP_SERVICE_ACCOUNT, STATE_SERVICE_ACCOUNT}

DIAGNOSTICS_ROLE = "maops-diagnostics-reader"
DIAGNOSTICS_ROLE_BINDING = "maops-diagnostics-reader-binding"
DIAGNOSTICS_SERVICE_ACCOUNT = "maops-diagnostics"
DIAGNOSTICS_ALLOWED_CORE_RESOURCES = {"pods", "services"}
DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES = {"endpointslices"}
DIAGNOSTICS_ALLOWED_VERBS = {"get", "list", "watch"}
DIAGNOSTICS_FORBIDDEN_RESOURCES = {"secrets", "deployments", "statefulsets", "replicasets", "pods/exec", "pods/eviction", "*"}
DIAGNOSTICS_FORBIDDEN_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
STATE_STATEFULSET = "maops-state"
GATEWAY_IMAGE_REPO = "maops-kubernetes-gateway"
APP_IMAGE_REPO = "maops-kubernetes-app"
STATE_IMAGE_REPO = "maops-kubernetes-state"

# DAY6 (live rollout remediation): every workload that mounts either
# runtime Secret projects it read-only at mode 0440 (decimal 288,
# unchanged by this remediation) - `runAsGroup` alone does not make
# those files group-readable by the container's process; `fsGroup` (with
# `fsGroupChangePolicy: OnRootMismatch`, to avoid an unconditional
# recursive chown/chmod on every Pod start once already correct) is what
# actually does. maops-gateway/maops-app/maops-state are exactly the set
# of workloads this chart renders that mount `internal-auth`/
# `state-auth` - see each template's own `volumes:`/`volumeMounts:`.
INTERNAL_AUTH_SECRET_NAME = "maops-internal-auth"
STATE_AUTH_SECRET_NAME = "maops-state-auth"
EXPECTED_SECRET_VOLUME_DEFAULT_MODE = 288  # 0440 octal
EXPECTED_FS_GROUP = 10001
EXPECTED_FS_GROUP_CHANGE_POLICY = "OnRootMismatch"

NETPOL_DEFAULT_DENY = "maops-default-deny-all"
NETPOL_ALLOW_DNS = "maops-allow-dns-egress"
NETPOL_ALLOW_GATEWAY_EGRESS_APP = "maops-allow-gateway-egress-to-app"
NETPOL_ALLOW_APP_INGRESS_GATEWAY = "maops-allow-app-ingress-from-gateway"
NETPOL_ALLOW_APP_EGRESS_STATE = "maops-allow-app-egress-to-state"
NETPOL_ALLOW_STATE_INGRESS_APP = "maops-allow-state-ingress-from-app"
NETPOL_ALLOW_GATEWAY_INGRESS_EXTERNAL = "maops-allow-gateway-ingress-from-istio-ingress-gateway"
NETPOL_ALLOW_HBONE = "maops-allow-hbone-ztunnel"
EXPECTED_NETWORK_POLICY_NAMES = {
    NETPOL_DEFAULT_DENY,
    NETPOL_ALLOW_DNS,
    NETPOL_ALLOW_GATEWAY_EGRESS_APP,
    NETPOL_ALLOW_APP_INGRESS_GATEWAY,
    NETPOL_ALLOW_APP_EGRESS_STATE,
    NETPOL_ALLOW_STATE_INGRESS_APP,
    NETPOL_ALLOW_GATEWAY_INGRESS_EXTERNAL,
    NETPOL_ALLOW_HBONE,
}
# DAY6: Day 5's validation-client -> gateway allow must NOT be carried
# forward - see docs/architecture.md.
FORBIDDEN_NETWORK_POLICY_NAMES = {"maops-allow-gateway-ingress-from-validation"}

PEER_AUTHENTICATION_NAME = "maops-platform-strict-mtls"
AUTHZ_GATEWAY = "maops-gateway-authz"
AUTHZ_APP = "maops-app-authz"
AUTHZ_STATE = "maops-state-authz"

GATEWAY_NAME = "maops-edge"
GATEWAY_ROUTE_NAME = "maops-gateway-route"
ROUTING_HOSTNAME = "maops.local"
# DAY6 remediation: Istio's Gateway API deployment controller names the
# generated ServiceAccount deterministically as "<gateway-name>-istio" -
# never a bare override of GATEWAY_NAME.
GATEWAY_PROXY_SERVICE_ACCOUNT = "maops-edge-istio"

FORBIDDEN_KINDS = {"Ingress", "ClusterRole", "ClusterRoleBinding", "Secret"}

# DAY6 (live-discovered Helm ConfigMap rollout remediation): a
# deterministic sha256 hex digest, as `sha256sum | quote` in the chart
# templates produces - a 64-character lowercase hex string wrapped in
# double quotes by Helm's `quote` function (the quotes are stripped by
# this project's YAML parser before this validator ever sees the
# value).
_SHA256_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")

# 2 Deployments + 1 StatefulSet + 3 ConfigMaps + 3 ServiceAccounts (app
# workloads only - diagnostics' own ServiceAccount lives in k8s/day6/,
# never rendered by this chart) + 1 Role + 1 RoleBinding + 4 Services +
# 2 PDBs + 8 NetworkPolicies + 1 PeerAuthentication +
# 3 AuthorizationPolicies + 1 HTTPRoute.
EXPECTED_TOTAL_OBJECT_COUNT = 30


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

    def by_kind_name(self, kind: str, name: str) -> dict | None:
        for d in self.by_kind(kind):
            if d.get("metadata", {}).get("name") == name:
                return d
        return None


def _container(doc: dict, name: str) -> dict | None:
    containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for c in containers:
        if c.get("name") == name:
            return c
    return None


def _check_inventory(c: _Checker) -> None:
    c.check(
        len(c.docs) == EXPECTED_TOTAL_OBJECT_COUNT,
        "inventory.total_object_count",
        f"expected exactly {EXPECTED_TOTAL_OBJECT_COUNT} rendered objects, found {len(c.docs)}",
    )
    for kind in FORBIDDEN_KINDS:
        found = c.by_kind(kind)
        c.check(not found, f"inventory.no_{kind.lower()}", f"expected zero {kind} objects, found {len(found)}")

    waypoint_like = [
        d
        for d in c.docs
        if d.get("kind") == "Gateway"
        or "istio.io/waypoint-for" in (d.get("metadata", {}).get("labels") or {})
    ]
    c.check(not waypoint_like, "inventory.no_waypoint", f"expected zero waypoint-shaped objects in the chart's own output, found {len(waypoint_like)}")


def _check_versions(c: _Checker) -> None:
    for kind, name, image_repo in (
        ("Deployment", GATEWAY_DEPLOYMENT, GATEWAY_IMAGE_REPO),
        ("Deployment", APP_DEPLOYMENT, APP_IMAGE_REPO),
        ("StatefulSet", STATE_STATEFULSET, STATE_IMAGE_REPO),
    ):
        doc = c.by_kind_name(kind, name)
        container = _container(doc, name) if doc else None
        image = container.get("image") if container else None
        expected = f"{image_repo}:{EXPECTED_VERSION}"
        c.check(image == expected, f"version.{name}.image_tag", f"expected {name} image == {expected!r}, found {image!r}")

    for doc in c.docs:
        labels = doc.get("metadata", {}).get("labels") or {}
        version = labels.get("app.kubernetes.io/version")
        if version is not None:
            name = f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"
            c.check(version == EXPECTED_VERSION, f"version.label_matches[{name}]", f"expected app.kubernetes.io/version == {EXPECTED_VERSION!r}, found {version!r}")
        instance = labels.get("app.kubernetes.io/instance")
        if instance is not None:
            name = f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"
            c.check(instance == EXPECTED_INSTANCE, f"version.instance_matches[{name}]", f"expected app.kubernetes.io/instance == {EXPECTED_INSTANCE!r}, found {instance!r}")


def _check_security_context(c: _Checker) -> None:
    for kind, name in (("Deployment", GATEWAY_DEPLOYMENT), ("Deployment", APP_DEPLOYMENT), ("StatefulSet", STATE_STATEFULSET)):
        doc = c.by_kind_name(kind, name)
        if doc is None:
            c.check(False, f"security.{name}.exists", f"expected {kind}/{name} to be rendered")
            continue
        pod_sc = doc.get("spec", {}).get("template", {}).get("spec", {}).get("securityContext", {})
        c.check(pod_sc.get("runAsNonRoot") is True, f"security.{name}.runAsNonRoot", f"expected true, found {pod_sc.get('runAsNonRoot')!r}")
        c.check(pod_sc.get("runAsUser") == 10001, f"security.{name}.runAsUser", f"expected 10001, found {pod_sc.get('runAsUser')!r}")
        c.check(pod_sc.get("runAsGroup") == 10001, f"security.{name}.runAsGroup", f"expected 10001, found {pod_sc.get('runAsGroup')!r}")
        c.check(
            (pod_sc.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
            f"security.{name}.seccompProfile",
            f"expected RuntimeDefault, found {pod_sc.get('seccompProfile')!r}",
        )
        # DAY6 (live rollout remediation): fsGroup is what actually makes
        # a projected 0440 Secret volume group-readable by this
        # non-root, non-matching-primary-group process - runAsGroup
        # alone does not. Checked for every workload that reaches this
        # loop, since gateway/app/state are exactly the chart's Secret-
        # mounting workloads (see INTERNAL_AUTH_SECRET_NAME/
        # STATE_AUTH_SECRET_NAME above).
        c.check(pod_sc.get("fsGroup") == EXPECTED_FS_GROUP, f"security.{name}.fsGroup", f"expected {EXPECTED_FS_GROUP}, found {pod_sc.get('fsGroup')!r}")
        c.check(
            pod_sc.get("fsGroupChangePolicy") == EXPECTED_FS_GROUP_CHANGE_POLICY,
            f"security.{name}.fsGroupChangePolicy",
            f"expected {EXPECTED_FS_GROUP_CHANGE_POLICY!r}, found {pod_sc.get('fsGroupChangePolicy')!r}",
        )

        pod_spec_for_volumes = doc.get("spec", {}).get("template", {}).get("spec", {})
        volumes = pod_spec_for_volumes.get("volumes", []) or []
        secret_volumes = [v for v in volumes if "secret" in v]
        relevant_secret_volumes = [
            v for v in secret_volumes if v["secret"].get("secretName") in (INTERNAL_AUTH_SECRET_NAME, STATE_AUTH_SECRET_NAME)
        ]
        c.check(
            bool(relevant_secret_volumes),
            f"security.{name}.mounts_a_runtime_secret",
            f"expected {name} to mount at least one of {{{INTERNAL_AUTH_SECRET_NAME!r}, {STATE_AUTH_SECRET_NAME!r}}} as a Secret volume, found {[v.get('name') for v in secret_volumes]!r}",
        )
        for v in relevant_secret_volumes:
            secret_name = v["secret"].get("secretName")
            mode = v["secret"].get("defaultMode")
            c.check(
                mode == EXPECTED_SECRET_VOLUME_DEFAULT_MODE,
                f"security.{name}.secret_volume[{secret_name}].defaultMode",
                f"expected {EXPECTED_SECRET_VOLUME_DEFAULT_MODE} (0440), found {mode!r} - this remediation must never change Secret file mode, only add fsGroup",
            )

        container = _container(doc, name)
        c.check(container is not None, f"security.{name}.container_present", f"expected a container named {name!r}")
        if container is None:
            continue
        container_sc = container.get("securityContext", {})
        c.check(container_sc.get("allowPrivilegeEscalation") is False, f"security.{name}.allowPrivilegeEscalation", f"expected false, found {container_sc.get('allowPrivilegeEscalation')!r}")
        c.check(container_sc.get("readOnlyRootFilesystem") is True, f"security.{name}.readOnlyRootFilesystem", f"expected true, found {container_sc.get('readOnlyRootFilesystem')!r}")
        drop = (container_sc.get("capabilities") or {}).get("drop") or []
        c.check(list(drop) == ["ALL"], f"security.{name}.capabilities_drop_all", f"expected ['ALL'], found {drop!r}")

        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        c.check(spec.get("automountServiceAccountToken") is False, f"security.{name}.automount_false", f"expected false, found {spec.get('automountServiceAccountToken')!r}")
        c.check(spec.get("serviceAccountName") == name, f"security.{name}.serviceAccountName", f"expected {name!r}, found {spec.get('serviceAccountName')!r}")


def _check_service_accounts_and_rbac(c: _Checker) -> None:
    for name in APPLICATION_SERVICE_ACCOUNTS:
        sa = c.by_kind_name("ServiceAccount", name)
        c.check(sa is not None, f"rbac.serviceaccount.{name}.exists", f"expected ServiceAccount/{name} to be rendered")
        if sa is not None:
            c.check(sa.get("automountServiceAccountToken") is False, f"rbac.serviceaccount.{name}.automount_false", f"expected false, found {sa.get('automountServiceAccountToken')!r}")

    diagnostics_sa = c.by_kind_name("ServiceAccount", DIAGNOSTICS_SERVICE_ACCOUNT)
    c.check(diagnostics_sa is None, "rbac.diagnostics_serviceaccount_not_in_chart", "expected maops-diagnostics ServiceAccount to live in k8s/day6/, never rendered by this chart")

    roles = c.by_kind("Role")
    c.check(len(roles) == 1, "rbac.exactly_one_role", f"expected exactly 1 Role, found {len(roles)}")
    role = c.by_kind_name("Role", DIAGNOSTICS_ROLE)
    c.check(role is not None, "rbac.role_name", f"expected Role/{DIAGNOSTICS_ROLE}")
    if role is not None:
        c.check(role.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE, "rbac.role_namespace", f"expected namespace {EXPECTED_NAMESPACE!r}")
        rules = role.get("rules", [])
        core_resources: set[str] = set()
        discovery_resources: set[str] = set()
        verbs: set[str] = set()
        for rule in rules:
            groups = set(rule.get("apiGroups", []))
            res = set(rule.get("resources", []))
            v = set(rule.get("verbs", []))
            verbs |= v
            if "" in groups:
                core_resources |= res
            if "discovery.k8s.io" in groups:
                discovery_resources |= res
        c.check(core_resources == DIAGNOSTICS_ALLOWED_CORE_RESOURCES, "rbac.role.core_resources_exact", f"expected {DIAGNOSTICS_ALLOWED_CORE_RESOURCES}, found {core_resources}")
        c.check(discovery_resources == DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES, "rbac.role.discovery_resources_exact", f"expected {DIAGNOSTICS_ALLOWED_DISCOVERY_RESOURCES}, found {discovery_resources}")
        c.check(verbs == DIAGNOSTICS_ALLOWED_VERBS, "rbac.role.verbs_exact", f"expected {DIAGNOSTICS_ALLOWED_VERBS}, found {verbs}")
        c.check(not (core_resources | discovery_resources) & DIAGNOSTICS_FORBIDDEN_RESOURCES, "rbac.role.no_forbidden_resources", "expected no forbidden resource in any rule")
        c.check(not verbs & DIAGNOSTICS_FORBIDDEN_VERBS, "rbac.role.no_forbidden_verbs", "expected no forbidden verb in any rule")

    bindings = c.by_kind("RoleBinding")
    c.check(len(bindings) == 1, "rbac.exactly_one_rolebinding", f"expected exactly 1 RoleBinding, found {len(bindings)}")
    binding = c.by_kind_name("RoleBinding", DIAGNOSTICS_ROLE_BINDING)
    c.check(binding is not None, "rbac.rolebinding_name", f"expected RoleBinding/{DIAGNOSTICS_ROLE_BINDING}")
    if binding is not None:
        subjects = binding.get("subjects", [])
        c.check(len(subjects) == 1, "rbac.rolebinding.single_subject", f"expected exactly 1 subject, found {len(subjects)}")
        subject_names = {s.get("name") for s in subjects}
        c.check(subject_names == {DIAGNOSTICS_SERVICE_ACCOUNT}, "rbac.rolebinding.subject_is_diagnostics_only", f"expected only {DIAGNOSTICS_SERVICE_ACCOUNT!r}, found {subject_names}")
        c.check(
            not (subject_names & APPLICATION_SERVICE_ACCOUNTS),
            "rbac.application_service_accounts_not_bound",
            "expected no application ServiceAccount (gateway/app/state) to ever be a RoleBinding subject",
        )
        subject_namespaces = {s.get("namespace") for s in subjects}
        c.check(subject_namespaces == {VALIDATION_NAMESPACE}, "rbac.rolebinding.subject_namespace", f"expected {VALIDATION_NAMESPACE!r}, found {subject_namespaces}")


def _network_policy(c: _Checker, name: str) -> dict | None:
    return c.by_kind_name("NetworkPolicy", name)


def _check_network_policy(c: _Checker) -> None:
    policies = c.by_kind("NetworkPolicy")
    names = {p.get("metadata", {}).get("name") for p in policies}
    c.check(names == EXPECTED_NETWORK_POLICY_NAMES, "networkpolicy.topology_exact", f"expected exactly {EXPECTED_NETWORK_POLICY_NAMES}, found {names}")
    c.check(not (names & FORBIDDEN_NETWORK_POLICY_NAMES), "networkpolicy.no_day5_validation_shortcut", f"expected the Day 5 validation-client -> gateway allow to be absent, found {names & FORBIDDEN_NETWORK_POLICY_NAMES}")

    default_deny = _network_policy(c, NETPOL_DEFAULT_DENY)
    if default_deny is not None:
        spec = default_deny.get("spec", {})
        c.check(spec.get("podSelector") == {}, "networkpolicy.default_deny.selects_all_pods", f"expected podSelector: {{}}, found {spec.get('podSelector')!r}")
        c.check(set(spec.get("policyTypes", [])) == {"Ingress", "Egress"}, "networkpolicy.default_deny.both_directions", f"found {spec.get('policyTypes')!r}")
        c.check(not spec.get("ingress") and not spec.get("egress"), "networkpolicy.default_deny.no_rules", "expected zero ingress/egress rules")

    hbone = _network_policy(c, NETPOL_ALLOW_HBONE)
    if hbone is not None:
        ingress_rules = hbone.get("spec", {}).get("ingress", [])
        egress_rules = hbone.get("spec", {}).get("egress", [])
        ingress_ports = {(r.get("protocol"), r.get("port")) for rule in ingress_rules for r in rule.get("ports", [])}
        egress_ports = {(r.get("protocol"), r.get("port")) for rule in egress_rules for r in rule.get("ports", [])}
        c.check(ingress_ports == {("TCP", 15008)}, "networkpolicy.hbone.ingress_port_exact", f"expected TCP/15008 only, found {ingress_ports}")
        c.check(egress_ports == {("TCP", 15008)}, "networkpolicy.hbone.egress_port_exact", f"expected TCP/15008 only, found {egress_ports}")
        # DAY6 remediation: this rule is deliberately peer-less (no
        # `from`/`to` selector) - ztunnel's hostNetwork DaemonSet
        # identity cannot be reliably matched by a namespaceSelector/
        # podSelector peer (see the template's own header comment).
        # Asserting NO peer key is present locks in that corrected
        # design and catches a regression back to the earlier,
        # incorrect istio-system-namespaceSelector assumption.
        ingress_has_peer = any("from" in rule for rule in ingress_rules)
        egress_has_peer = any("to" in rule for rule in egress_rules)
        c.check(not ingress_has_peer, "networkpolicy.hbone.ingress_is_peerless", "expected no 'from' peer restriction (ztunnel's real network identity cannot be matched by namespaceSelector/podSelector)")
        c.check(not egress_has_peer, "networkpolicy.hbone.egress_is_peerless", "expected no 'to' peer restriction (ztunnel's real network identity cannot be matched by namespaceSelector/podSelector)")

    gw_external = _network_policy(c, NETPOL_ALLOW_GATEWAY_INGRESS_EXTERNAL)
    if gw_external is not None:
        ingress = gw_external.get("spec", {}).get("ingress", [])
        peers = [p for rule in ingress for p in rule.get("from", [])]
        matches_expected = any(
            (p.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == INGRESS_NAMESPACE)
            and (p.get("podSelector", {}).get("matchLabels", {}).get("istio.io/gateway-name") == GATEWAY_NAME)
            for p in peers
        )
        c.check(matches_expected, "networkpolicy.gateway_ingress_external.scoped_to_istio_gateway", f"expected a from-peer scoped to namespace {INGRESS_NAMESPACE!r} + istio.io/gateway-name={GATEWAY_NAME!r}, found {peers}")

    gw_egress_app = _network_policy(c, NETPOL_ALLOW_GATEWAY_EGRESS_APP)
    state_ingress_app = _network_policy(c, NETPOL_ALLOW_STATE_INGRESS_APP)
    if gw_egress_app is not None:
        targets = [p.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") for rule in gw_egress_app.get("spec", {}).get("egress", []) for p in rule.get("to", [])]
        c.check("state" not in targets, "networkpolicy.gateway_egress_app.never_targets_state", f"expected gateway's egress allow to never target state, found targets {targets}")
    if state_ingress_app is not None:
        sources = [p.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component") for rule in state_ingress_app.get("spec", {}).get("ingress", []) for p in rule.get("from", [])]
        c.check("gateway" not in sources, "networkpolicy.state_ingress_app.never_allows_gateway", f"expected state's ingress allow to never accept gateway, found sources {sources}")


def _check_peer_authentication(c: _Checker) -> None:
    policies = c.by_kind("PeerAuthentication")
    c.check(len(policies) == 1, "mesh.exactly_one_peerauthentication", f"expected exactly 1 PeerAuthentication, found {len(policies)}")
    pa = c.by_kind_name("PeerAuthentication", PEER_AUTHENTICATION_NAME)
    c.check(pa is not None, "mesh.peerauthentication_name", f"expected PeerAuthentication/{PEER_AUTHENTICATION_NAME}")
    if pa is not None:
        c.check(pa.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE, "mesh.peerauthentication.namespace", f"expected {EXPECTED_NAMESPACE!r}")
        mode = pa.get("spec", {}).get("mtls", {}).get("mode")
        c.check(mode == "STRICT", "mesh.peerauthentication.strict", f"expected STRICT, found {mode!r}")
        c.check("selector" not in pa.get("spec", {}), "mesh.peerauthentication.namespace_wide", "expected no selector (namespace-wide), found one")


def _principal_of(policy: dict | None) -> set[str]:
    if policy is None:
        return set()
    principals: set[str] = set()
    for rule in policy.get("spec", {}).get("rules", []):
        for source in rule.get("from", []):
            principals |= set(source.get("source", {}).get("principals", []))
    return principals


def _check_authorization_policies(c: _Checker) -> None:
    policies = c.by_kind("AuthorizationPolicy")
    c.check(len(policies) == 3, "mesh.exactly_three_authorizationpolicies", f"expected exactly 3 AuthorizationPolicy objects, found {len(policies)}")

    gw_authz = c.by_kind_name("AuthorizationPolicy", AUTHZ_GATEWAY)
    app_authz = c.by_kind_name("AuthorizationPolicy", AUTHZ_APP)
    state_authz = c.by_kind_name("AuthorizationPolicy", AUTHZ_STATE)

    for name, policy, expected_component in (
        (AUTHZ_GATEWAY, gw_authz, "gateway"),
        (AUTHZ_APP, app_authz, "app"),
        (AUTHZ_STATE, state_authz, "state"),
    ):
        c.check(policy is not None, f"mesh.authz.{name}.exists", f"expected AuthorizationPolicy/{name}")
        if policy is None:
            continue
        c.check(policy.get("spec", {}).get("action") == "ALLOW", f"mesh.authz.{name}.action_allow", "expected action: ALLOW")
        selector = policy.get("spec", {}).get("selector", {}).get("matchLabels", {})
        c.check(
            selector.get("app.kubernetes.io/component") == expected_component,
            f"mesh.authz.{name}.selector_component",
            f"expected component {expected_component!r}, found {selector.get('app.kubernetes.io/component')!r}",
        )

    gw_expected = {f"cluster.local/ns/{INGRESS_NAMESPACE}/sa/{GATEWAY_PROXY_SERVICE_ACCOUNT}"}
    app_expected = {f"cluster.local/ns/{EXPECTED_NAMESPACE}/sa/{GATEWAY_SERVICE_ACCOUNT}"}
    state_expected = {f"cluster.local/ns/{EXPECTED_NAMESPACE}/sa/{APP_SERVICE_ACCOUNT}"}

    c.check(_principal_of(gw_authz) == gw_expected, "mesh.authz.gateway.principal_exact", f"expected {gw_expected}, found {_principal_of(gw_authz)}")
    c.check(_principal_of(app_authz) == app_expected, "mesh.authz.app.principal_exact", f"expected {app_expected}, found {_principal_of(app_authz)}")
    c.check(_principal_of(state_authz) == state_expected, "mesh.authz.state.principal_exact", f"expected {state_expected}, found {_principal_of(state_authz)}")

    # Required denied paths, proven negatively: neither the diagnostics/
    # validation identity nor a gateway -> state path is ever a listed
    # principal anywhere.
    all_principals = _principal_of(gw_authz) | _principal_of(app_authz) | _principal_of(state_authz)
    diagnostics_principal = f"cluster.local/ns/{VALIDATION_NAMESPACE}/sa/{DIAGNOSTICS_SERVICE_ACCOUNT}"
    c.check(diagnostics_principal not in all_principals, "mesh.authz.diagnostics_never_a_principal", f"expected {diagnostics_principal!r} to never appear as an allowed principal")
    gateway_principal = f"cluster.local/ns/{EXPECTED_NAMESPACE}/sa/{GATEWAY_SERVICE_ACCOUNT}"
    c.check(gateway_principal not in _principal_of(state_authz), "mesh.authz.gateway_never_allowed_to_state", f"expected {gateway_principal!r} to never be allowed to reach state")


def _check_gateway_api(c: _Checker) -> None:
    routes = c.by_kind("HTTPRoute")
    c.check(len(routes) == 1, "gateway_api.exactly_one_httproute", f"expected exactly 1 HTTPRoute, found {len(routes)}")
    route = c.by_kind_name("HTTPRoute", GATEWAY_ROUTE_NAME)
    c.check(route is not None, "gateway_api.httproute_name", f"expected HTTPRoute/{GATEWAY_ROUTE_NAME}")
    if route is None:
        return
    c.check(route.get("metadata", {}).get("namespace") == EXPECTED_NAMESPACE, "gateway_api.httproute.namespace", f"expected {EXPECTED_NAMESPACE!r}")
    parent_refs = route.get("spec", {}).get("parentRefs", [])
    c.check(len(parent_refs) == 1, "gateway_api.httproute.single_parent_ref", f"expected exactly 1 parentRef, found {len(parent_refs)}")
    if parent_refs:
        ref = parent_refs[0]
        c.check(ref.get("name") == GATEWAY_NAME, "gateway_api.httproute.parent_name", f"expected {GATEWAY_NAME!r}, found {ref.get('name')!r}")
        c.check(ref.get("namespace") == INGRESS_NAMESPACE, "gateway_api.httproute.parent_namespace", f"expected {INGRESS_NAMESPACE!r}, found {ref.get('namespace')!r}")
    hostnames = route.get("spec", {}).get("hostnames", [])
    c.check(hostnames == [ROUTING_HOSTNAME], "gateway_api.httproute.hostname_exact", f"expected [{ROUTING_HOSTNAME!r}], found {hostnames!r}")
    rules = route.get("spec", {}).get("rules", [])
    c.check(len(rules) == 1, "gateway_api.httproute.single_rule", f"expected exactly 1 rule, found {len(rules)}")
    if rules:
        rule = rules[0]
        matches = rule.get("matches", [])
        c.check(
            any(m.get("path", {}).get("type") == "PathPrefix" and m.get("path", {}).get("value") == "/" for m in matches),
            "gateway_api.httproute.pathprefix_root",
            f"expected a PathPrefix '/' match, found {matches!r}",
        )
        backend_refs = rule.get("backendRefs", [])
        c.check(
            any(b.get("name") == "maops-gateway" and b.get("port") == 8080 for b in backend_refs),
            "gateway_api.httproute.backend_ref_exact",
            f"expected backendRefs to include service/maops-gateway port 8080, found {backend_refs!r}",
        )


def _check_config_checksum(c: _Checker) -> None:
    """DAY6 (live-discovered Helm ConfigMap rollout remediation): a live
    `helm-lifecycle-check` run found that changing a ConfigMap's data
    (e.g. gateway.config.appMessage) does NOT by itself change the
    consuming Deployment/StatefulSet's Pod template - `envFrom`-mounted
    ConfigMap data is read only at container startup, and Kubernetes
    only rolls a new ReplicaSet/Pod generation when the Pod template
    ITSELF changes. Every workload that consumes a ConfigMap through
    environment variables must therefore carry a deterministic checksum
    of that ConfigMap's OWN rendered template under
    `spec.template.metadata.annotations['checksum/config']` - NEVER the
    workload's own top-level `metadata.annotations` (an annotation
    there does not touch the Pod template at all, so it would not
    actually fix the underlying defect; this check only ever looks in
    the Pod-template location, so a checksum placed on the wrong level
    is indistinguishable from a checksum that is simply missing - both
    are rejected).

    Checked for each of the three workloads: the annotation is present
    (as a string) on the Pod template specifically, non-empty, and
    shaped like a genuine sha256 hex digest. Then, pairwise: no two
    workloads may carry the SAME checksum value - since the three
    ConfigMaps' rendered content genuinely differs, an accidental
    reference to the wrong component's ConfigMap template (e.g.
    maops-app's Deployment hashing gateway-configmap.yaml) would
    produce an identical checksum to that OTHER workload, which this
    catches directly. True non-determinism (the same inputs producing a
    different checksum across renders) cannot be observed from a single
    parsed render - see the render-level tests in
    tests/test_validate_helm_chart.py, which render the chart more than
    once and assert reproducibility directly."""
    checksums: dict[str, str] = {}
    for kind, name in (("Deployment", GATEWAY_DEPLOYMENT), ("Deployment", APP_DEPLOYMENT), ("StatefulSet", STATE_STATEFULSET)):
        doc = c.by_kind_name(kind, name)
        if doc is None:
            c.check(False, f"checksum.{name}.exists", f"expected {kind}/{name} to be rendered")
            continue

        pod_template_annotations = doc.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}
        checksum = pod_template_annotations.get("checksum/config")
        present = isinstance(checksum, str)
        c.check(
            present,
            f"checksum.{name}.present_on_pod_template",
            f"expected a string spec.template.metadata.annotations['checksum/config'] on {kind}/{name}, found {checksum!r} "
            "(missing entirely, or placed on the workload's own top-level metadata instead of the Pod template, is reported here too)",
        )
        if not present:
            continue

        non_empty = bool(checksum.strip())
        c.check(non_empty, f"checksum.{name}.non_empty", f"expected a non-empty checksum on {kind}/{name}, found {checksum!r}")
        if not non_empty:
            continue

        well_formed = bool(_SHA256_CHECKSUM_RE.match(checksum))
        c.check(
            well_formed,
            f"checksum.{name}.well_formed_sha256",
            f"expected a 64-character lowercase hex sha256 digest on {kind}/{name}, found {checksum!r}",
        )
        if well_formed:
            checksums[name] = checksum

    names = list(checksums)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            c.check(
                checksums[a] != checksums[b],
                f"checksum.{a}_vs_{b}.distinct",
                f"expected {a} and {b} to carry DIFFERENT config checksums (each must hash only its OWN ConfigMap "
                "template) - found the same value for both, which would mean one workload is referencing the wrong "
                "component's ConfigMap",
            )


def run_checks(docs: list[dict]) -> list[Finding]:
    c = _Checker(docs)
    _check_inventory(c)
    _check_versions(c)
    _check_security_context(c)
    _check_service_accounts_and_rbac(c)
    _check_network_policy(c)
    _check_peer_authentication(c)
    _check_authorization_policies(c)
    _check_gateway_api(c)
    _check_config_checksum(c)
    return c.findings

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

DAY7: every check is now evaluated against an explicit
`RenderExpectation` - the render state a given values set must produce
(candidate on/off, route mode and weights, candidate strategy/replicas/
fault injection, release instance). The default expectation is the
stable-only render (candidate disabled, one unweighted stable backend,
the unchanged 30-object inventory). With the candidate enabled the
validator additionally proves: the candidate objects exist with the
stable gateway's security/identity/probe/Secret-mount parity and a
distinct ConfigMap message; stable and candidate Deployment/Service/PDB/
AuthorizationPolicy selectors are provably disjoint (a shared label KEY
with different values - no Pod can satisfy both); every NetworkPolicy
path the stable gateway has, the candidate has too - and no other
(no candidate -> state, no widening of app/state/unrelated paths); and
the single HTTPRoute's backendRefs are exactly the expected Services,
ports and weights.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXPECTED_NAMESPACE = "maops-platform"
EXPECTED_VERSION = "0.7.0"
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

# DAY7: the OPTIONAL gateway candidate. When enabled it adds exactly 7
# objects: its ConfigMap, Deployment and Service, three candidate-only
# NetworkPolicies, and its own AuthorizationPolicy (both policy sets are
# required - see the templates' header comments).
CANDIDATE_COMPONENT = "gateway-candidate"
CANDIDATE_DEPLOYMENT = "maops-gateway-candidate"
CANDIDATE_SERVICE = "maops-gateway-candidate"
CANDIDATE_CONFIGMAP = "maops-gateway-candidate-config"
STABLE_CONFIGMAP = "maops-gateway-config"
AUTHZ_CANDIDATE = "maops-gateway-candidate-authz"
GATEWAY_PDB = "maops-gateway-pdb"
NETPOL_ALLOW_CANDIDATE_INGRESS_EXTERNAL = "maops-allow-gateway-candidate-ingress-from-istio-ingress-gateway"
NETPOL_ALLOW_CANDIDATE_EGRESS_APP = "maops-allow-gateway-candidate-egress-to-app"
NETPOL_ALLOW_APP_INGRESS_CANDIDATE = "maops-allow-app-ingress-from-gateway-candidate"
CANDIDATE_NETWORK_POLICY_NAMES = {
    NETPOL_ALLOW_CANDIDATE_INGRESS_EXTERNAL,
    NETPOL_ALLOW_CANDIDATE_EGRESS_APP,
    NETPOL_ALLOW_APP_INGRESS_CANDIDATE,
}
CANDIDATE_OBJECT_COUNT = 7
FORCED_UNREADY_PATH = "/maops-day7-forced-unready"
BACKEND_PORT = 8080
ROUTE_MODES = ("stable", "candidate", "weighted")


@dataclass(frozen=True)
class RenderExpectation:
    """The render state a values set must produce. Defaults describe
    the chart's default values (stable-only, Day 6-shaped)."""

    instance: str = EXPECTED_INSTANCE
    # DAY7: the validation namespace the diagnostics RoleBinding subject
    # must name (Day 6 default; the Day 7 stages use maops-day7-validation).
    validation_namespace: str = VALIDATION_NAMESPACE
    candidate_enabled: bool = False
    route_mode: str = "stable"
    stable_weight: int | None = None
    candidate_weight: int | None = None
    candidate_strategy: str = "RollingUpdate"
    candidate_replicas: int = 2
    candidate_fail_readiness: bool = False
    # DAY7 image contract: image repository -> the pinned build tag the
    # render must carry (a Day 7 stage rendered with a build overlay).
    # Empty = the chart default <EXPECTED_VERSION> tag (Day 6 render).
    image_tags: tuple[tuple[str, str], ...] = ()

    def expected_object_count(self) -> int:
        return EXPECTED_TOTAL_OBJECT_COUNT + (CANDIDATE_OBJECT_COUNT if self.candidate_enabled else 0)

    def expected_backends(self) -> list[tuple[str, int, int | None]]:
        if self.route_mode == "stable":
            return [(GATEWAY_DEPLOYMENT, BACKEND_PORT, None)]
        if self.route_mode == "candidate":
            return [(CANDIDATE_SERVICE, BACKEND_PORT, None)]
        return sorted([(GATEWAY_DEPLOYMENT, BACKEND_PORT, self.stable_weight), (CANDIDATE_SERVICE, BACKEND_PORT, self.candidate_weight)])


DEFAULT_EXPECTATION = RenderExpectation()


@dataclass
class Finding:
    ok: bool
    name: str
    detail: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.name}: {self.detail}"


class _Checker:
    def __init__(self, docs: list[dict], expectation: RenderExpectation = DEFAULT_EXPECTATION):
        self.docs = docs
        self.exp = expectation
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
    expected_count = c.exp.expected_object_count()
    c.check(
        len(c.docs) == expected_count,
        "inventory.total_object_count",
        f"expected exactly {expected_count} rendered objects (candidate {'enabled' if c.exp.candidate_enabled else 'disabled'}), found {len(c.docs)}",
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
        expected = f"{image_repo}:{dict(c.exp.image_tags).get(image_repo, EXPECTED_VERSION)}"
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
            c.check(instance == c.exp.instance, f"version.instance_matches[{name}]", f"expected app.kubernetes.io/instance == {c.exp.instance!r}, found {instance!r}")


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
        c.check(subject_namespaces == {c.exp.validation_namespace}, "rbac.rolebinding.subject_namespace", f"expected {c.exp.validation_namespace!r}, found {subject_namespaces}")


def _network_policy(c: _Checker, name: str) -> dict | None:
    return c.by_kind_name("NetworkPolicy", name)


def _check_network_policy(c: _Checker) -> None:
    policies = c.by_kind("NetworkPolicy")
    names = {p.get("metadata", {}).get("name") for p in policies}
    expected_names = EXPECTED_NETWORK_POLICY_NAMES | (CANDIDATE_NETWORK_POLICY_NAMES if c.exp.candidate_enabled else set())
    c.check(names == expected_names, "networkpolicy.topology_exact", f"expected exactly {expected_names}, found {names}")
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
    expected_authz = 4 if c.exp.candidate_enabled else 3
    c.check(len(policies) == expected_authz, "mesh.exactly_three_authorizationpolicies" if not c.exp.candidate_enabled else "mesh.exactly_four_authorizationpolicies", f"expected exactly {expected_authz} AuthorizationPolicy objects, found {len(policies)}")

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
    diagnostics_principal = f"cluster.local/ns/{c.exp.validation_namespace}/sa/{DIAGNOSTICS_SERVICE_ACCOUNT}"
    c.check(diagnostics_principal not in all_principals, "mesh.authz.diagnostics_never_a_principal", f"expected {diagnostics_principal!r} to never appear as an allowed principal")
    gateway_principal = f"cluster.local/ns/{EXPECTED_NAMESPACE}/sa/{GATEWAY_SERVICE_ACCOUNT}"
    c.check(gateway_principal not in _principal_of(state_authz), "mesh.authz.gateway_never_allowed_to_state", f"expected {gateway_principal!r} to never be allowed to reach state")

    candidate_authz = c.by_kind_name("AuthorizationPolicy", AUTHZ_CANDIDATE)
    if not c.exp.candidate_enabled:
        c.check(candidate_authz is None, "mesh.authz.candidate_absent_when_disabled", f"expected no {AUTHZ_CANDIDATE} with the candidate disabled")
        return
    c.check(candidate_authz is not None, f"mesh.authz.{AUTHZ_CANDIDATE}.exists", f"expected AuthorizationPolicy/{AUTHZ_CANDIDATE}")
    if candidate_authz is None:
        return
    c.check(candidate_authz.get("spec", {}).get("action") == "ALLOW", f"mesh.authz.{AUTHZ_CANDIDATE}.action_allow", "expected action: ALLOW")
    c.check(_principal_of(candidate_authz) == gw_expected, "mesh.authz.candidate.principal_exact", f"expected only the ingress Gateway principal {gw_expected}, found {_principal_of(candidate_authz)}")


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
        _check_route_backends(c, backend_refs)


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
    workloads = [("Deployment", GATEWAY_DEPLOYMENT), ("Deployment", APP_DEPLOYMENT), ("StatefulSet", STATE_STATEFULSET)]
    if c.exp.candidate_enabled:
        workloads.append(("Deployment", CANDIDATE_DEPLOYMENT))
    for kind, name in workloads:
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


def run_checks(docs: list[dict], expectation: RenderExpectation | None = None) -> list[Finding]:
    c = _Checker(docs, expectation or DEFAULT_EXPECTATION)
    _check_inventory(c)
    _check_versions(c)
    _check_security_context(c)
    _check_service_accounts_and_rbac(c)
    _check_network_policy(c)
    _check_peer_authentication(c)
    _check_authorization_policies(c)
    _check_gateway_api(c)
    _check_config_checksum(c)
    _check_candidate(c)
    _check_selector_isolation(c)
    _check_network_policy_coverage(c)
    _check_authorization_coverage(c)
    return c.findings


# --------------------------------------------------------------------------
# DAY7: selector algebra (pure - also used directly by unit tests)
# --------------------------------------------------------------------------


def selector_matches(match_labels: dict, labels: dict) -> bool:
    """A matchLabels selector matches a label set iff every selector
    entry is present with the same value. An empty selector matches
    everything (Kubernetes semantics)."""
    return all(labels.get(k) == v for k, v in (match_labels or {}).items())


def selectors_provably_disjoint(a: dict, b: dict) -> bool:
    """True iff NO label set can satisfy both matchLabels selectors -
    i.e. they share at least one key with different values. Two
    selectors without such a conflict could both match one Pod, so they
    are NOT provably disjoint (even if today's Pods happen to differ)."""
    return any(k in (b or {}) and b[k] != v for k, v in (a or {}).items())


def _match_labels(obj: dict | None, *path: str) -> tuple[dict | None, bool]:
    """Returns (matchLabels, analyzable). A selector that uses
    matchExpressions is reported as not analyzable - this validator
    never guesses at set-based selectors."""
    node = obj or {}
    for key in path:
        node = (node or {}).get(key) or {}
    if "matchExpressions" in node:
        return None, False
    if "matchLabels" in node:
        return node.get("matchLabels") or {}, True
    return node, True  # Service spec.selector is a plain map


def _template_labels(doc: dict | None) -> dict:
    return ((doc or {}).get("spec", {}).get("template", {}).get("metadata", {}).get("labels")) or {}


# --------------------------------------------------------------------------
# DAY7: HTTPRoute backend set
# --------------------------------------------------------------------------


def _check_route_backends(c: _Checker, backend_refs: list) -> None:
    exp = c.exp
    normalized = []
    for b in backend_refs or []:
        foreign = b.get("kind", "Service") != "Service" or b.get("group", "") not in ("", None) or b.get("namespace") not in (None, EXPECTED_NAMESPACE)
        normalized.append((("FOREIGN:" if foreign else "") + str(b.get("name")), b.get("port"), b.get("weight")))
    expected = exp.expected_backends()
    c.check(
        sorted(normalized, key=str) == sorted(expected, key=str),
        "gateway_api.httproute.backend_ref_exact",
        f"expected route mode {exp.route_mode!r} backendRefs exactly {expected} (name, port, weight), found {normalized}",
    )
    if exp.route_mode == "weighted":
        weights = [w for _, _, w in normalized]
        c.check(
            all(isinstance(w, int) and not isinstance(w, bool) and 1 <= w <= 99 for w in weights) and sum(w for w in weights if isinstance(w, int)) == 100,
            "gateway_api.httproute.weights_valid",
            f"expected integer weights in 1..99 summing to 100, found {weights}",
        )
    else:
        c.check(all(w is None for _, _, w in normalized), "gateway_api.httproute.unweighted_single_backend", f"expected no weight field in mode {exp.route_mode!r}, found {normalized}")
    for name, port, _ in normalized:
        svc = c.by_kind_name("Service", name)
        c.check(svc is not None, f"gateway_api.httproute.backend[{name}].service_rendered", f"expected backend Service {name!r} to be rendered by this chart")
        if svc is None:
            continue
        ports = {p.get("port") for p in svc.get("spec", {}).get("ports", [])}
        c.check(port in ports, f"gateway_api.httproute.backend[{name}].port_matches_service", f"expected backend port {port} to be a port of Service/{name} ({sorted(ports)})")


# --------------------------------------------------------------------------
# DAY7: candidate presence, parity and config variant
# --------------------------------------------------------------------------

_PARITY_POD_FIELDS = ("serviceAccountName", "automountServiceAccountToken", "securityContext", "affinity", "volumes")
_PARITY_CONTAINER_FIELDS = ("name", "image", "imagePullPolicy", "ports", "securityContext", "resources", "volumeMounts", "startupProbe", "livenessProbe")


def _check_candidate(c: _Checker) -> None:
    candidate_docs = [
        d for d in c.docs
        if (d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/component") == CANDIDATE_COMPONENT
        or "candidate" in str(d.get("metadata", {}).get("name", ""))
    ]
    stable = c.by_kind_name("Deployment", GATEWAY_DEPLOYMENT)
    stable_strategy = (stable or {}).get("spec", {}).get("strategy", {})
    c.check(
        stable_strategy.get("type") == "RollingUpdate" and stable_strategy.get("rollingUpdate") == {"maxUnavailable": 1, "maxSurge": 1},
        "strategy.stable_gateway_stays_rollingupdate",
        f"expected maops-gateway to keep RollingUpdate (maxUnavailable 1, maxSurge 1) in every render - Recreate is candidate-only; found {stable_strategy}",
    )
    if not c.exp.candidate_enabled:
        c.check(not candidate_docs, "candidate.absent_when_disabled", f"expected no candidate object with candidate.enabled=false, found {[d.get('kind') + '/' + d.get('metadata', {}).get('name', '') for d in candidate_docs]}")
        return
    c.check(len(candidate_docs) == CANDIDATE_OBJECT_COUNT, "candidate.object_count", f"expected exactly {CANDIDATE_OBJECT_COUNT} candidate objects, found {len(candidate_docs)}")
    dep = c.by_kind_name("Deployment", CANDIDATE_DEPLOYMENT)
    svc = c.by_kind_name("Service", CANDIDATE_SERVICE)
    cm = c.by_kind_name("ConfigMap", CANDIDATE_CONFIGMAP)
    for kind, name, doc in (("Deployment", CANDIDATE_DEPLOYMENT, dep), ("Service", CANDIDATE_SERVICE, svc), ("ConfigMap", CANDIDATE_CONFIGMAP, cm)):
        c.check(doc is not None, f"candidate.{kind.lower()}.exists", f"expected {kind}/{name}")
    if dep is None or stable is None:
        return

    spec = dep.get("spec", {})
    c.check(spec.get("replicas") == c.exp.candidate_replicas, "candidate.replicas", f"expected {c.exp.candidate_replicas}, found {spec.get('replicas')!r}")
    strategy = spec.get("strategy", {})
    if c.exp.candidate_strategy == "Recreate":
        c.check(strategy == {"type": "Recreate"}, "candidate.strategy_recreate_without_rollingupdate", f"expected exactly {{'type': 'Recreate'}} (no rollingUpdate block), found {strategy}")
    else:
        c.check(strategy == stable_strategy, "candidate.strategy_matches_stable_rollingupdate", f"expected the stable gateway's RollingUpdate settings, found {strategy}")

    pod, stable_pod = spec.get("template", {}).get("spec", {}), stable.get("spec", {}).get("template", {}).get("spec", {})
    for fld in _PARITY_POD_FIELDS:
        c.check(pod.get(fld) == stable_pod.get(fld), f"candidate.parity.pod.{fld}", f"expected candidate {fld} identical to maops-gateway's ({stable_pod.get(fld)!r}), found {pod.get(fld)!r}")
    c.check(pod.get("serviceAccountName") == GATEWAY_SERVICE_ACCOUNT, "candidate.shares_gateway_serviceaccount", f"expected the shared {GATEWAY_SERVICE_ACCOUNT!r} ServiceAccount (one Istio principal for stable and candidate), found {pod.get('serviceAccountName')!r}")
    tsc, stable_tsc = pod.get("topologySpreadConstraints") or [], stable_pod.get("topologySpreadConstraints") or []
    strip = lambda items: [{k: v for k, v in i.items() if k != "labelSelector"} for i in items]  # noqa: E731
    c.check(strip(tsc) == strip(stable_tsc) and len(tsc) == 1, "candidate.parity.topology_spread", "expected the stable gateway's topology spread (same keys/skew), with its own labelSelector")
    if tsc:
        own = (tsc[0].get("labelSelector") or {}).get("matchLabels") or {}
        c.check(own == (spec.get("selector") or {}).get("matchLabels"), "candidate.topology_spread_selects_candidate_only", f"expected the candidate's spread labelSelector to equal its own selector, found {own}")

    cont = next((x for x in pod.get("containers", []) if x.get("name") == GATEWAY_DEPLOYMENT), None)
    stable_cont = next((x for x in stable_pod.get("containers", []) if x.get("name") == GATEWAY_DEPLOYMENT), None)
    c.check(cont is not None and len(pod.get("containers", [])) == 1, "candidate.single_gateway_container", "expected exactly one container named maops-gateway")
    if cont is not None and stable_cont is not None:
        for fld in _PARITY_CONTAINER_FIELDS:
            c.check(cont.get(fld) == stable_cont.get(fld), f"candidate.parity.container.{fld}", f"expected candidate container {fld} identical to maops-gateway's ({stable_cont.get(fld)!r}), found {cont.get(fld)!r}")
        env_from = [e.get("configMapRef", {}).get("name") for e in cont.get("envFrom", [])]
        c.check(env_from == [CANDIDATE_CONFIGMAP], "candidate.envfrom_own_configmap", f"expected envFrom only {CANDIDATE_CONFIGMAP!r}, found {env_from}")
        ready, stable_ready = dict(cont.get("readinessProbe") or {}), dict(stable_cont.get("readinessProbe") or {})
        path = (ready.get("httpGet") or {}).get("path")
        expected_path = FORCED_UNREADY_PATH if c.exp.candidate_fail_readiness else "/readyz"
        c.check(path == expected_path, "candidate.readiness_probe_path", f"expected readiness path {expected_path!r} (failReadiness={c.exp.candidate_fail_readiness}), found {path!r}")
        ready_no_path = {**ready, "httpGet": {k: v for k, v in (ready.get("httpGet") or {}).items() if k != "path"}}
        stable_no_path = {**stable_ready, "httpGet": {k: v for k, v in (stable_ready.get("httpGet") or {}).items() if k != "path"}}
        c.check(ready_no_path == stable_no_path, "candidate.parity.readiness_probe_timing", "expected the stable readiness probe's port/timing, only the path may differ")

    stable_cm = c.by_kind_name("ConfigMap", STABLE_CONFIGMAP)
    if cm is not None and stable_cm is not None:
        data, stable_data = cm.get("data", {}), stable_cm.get("data", {})
        c.check(set(data) == set(stable_data), "candidate.configmap.same_keys", f"expected the stable gateway ConfigMap's keys, found {sorted(data)}")
        others_same = all(data.get(k) == stable_data.get(k) for k in stable_data if k != "APP_MESSAGE")
        c.check(others_same, "candidate.configmap.only_message_differs", "expected every key except APP_MESSAGE to equal the stable gateway ConfigMap (configuration variant of one key)")
        c.check(bool(data.get("APP_MESSAGE")) and data.get("APP_MESSAGE") != stable_data.get("APP_MESSAGE"), "candidate.configmap.distinct_message", f"expected a non-empty APP_MESSAGE distinct from stable's ({stable_data.get('APP_MESSAGE')!r}), found {data.get('APP_MESSAGE')!r}")
        c.check(data.get("BACKEND_HOST") == "maops-app", "candidate.configmap.backend_is_app", f"expected BACKEND_HOST maops-app, found {data.get('BACKEND_HOST')!r}")


# --------------------------------------------------------------------------
# DAY7: selector isolation between stable and candidate
# --------------------------------------------------------------------------


def _check_selector_isolation(c: _Checker) -> None:
    stable_dep = c.by_kind_name("Deployment", GATEWAY_DEPLOYMENT)
    stable_sel, ok1 = _match_labels(stable_dep, "spec", "selector")
    stable_pods = _template_labels(stable_dep)
    stable_svc_sel, ok2 = _match_labels(c.by_kind_name("Service", GATEWAY_DEPLOYMENT), "spec", "selector")
    pdb_sel, ok3 = _match_labels(c.by_kind_name("PodDisruptionBudget", GATEWAY_PDB), "spec", "selector")
    c.check(ok1 and ok2 and ok3, "isolation.stable_selectors_analyzable", "expected stable gateway Deployment/Service/PDB selectors to use matchLabels only")
    c.check(selector_matches(stable_sel, stable_pods) and selector_matches(stable_svc_sel, stable_pods) and selector_matches(pdb_sel, stable_pods), "isolation.stable_selectors_select_stable_pods", "expected the stable Deployment, Service and PDB selectors to select the stable gateway Pod template")
    if not c.exp.candidate_enabled:
        return
    cand_dep = c.by_kind_name("Deployment", CANDIDATE_DEPLOYMENT)
    cand_sel, ok4 = _match_labels(cand_dep, "spec", "selector")
    cand_pods = _template_labels(cand_dep)
    cand_svc_sel, ok5 = _match_labels(c.by_kind_name("Service", CANDIDATE_SERVICE), "spec", "selector")
    c.check(ok4 and ok5, "isolation.candidate_selectors_analyzable", "expected candidate Deployment/Service selectors to use matchLabels only")
    if not (ok1 and ok2 and ok3 and ok4 and ok5) or cand_dep is None:
        return
    c.check(selector_matches(cand_sel, cand_pods) and selector_matches(cand_svc_sel, cand_pods), "isolation.candidate_selectors_select_candidate_pods", "expected the candidate Deployment and Service selectors to select the candidate Pod template")
    c.check(selectors_provably_disjoint(stable_sel, cand_sel), "isolation.deployment_selectors_disjoint", f"expected stable {stable_sel} and candidate {cand_sel} Deployment selectors to conflict on a shared key (no Pod can match both)")
    c.check(selectors_provably_disjoint(stable_svc_sel, cand_svc_sel), "isolation.service_selectors_disjoint", f"expected stable {stable_svc_sel} and candidate {cand_svc_sel} Service selectors to select disjoint Pods")
    c.check(selectors_provably_disjoint(pdb_sel, cand_sel) and not selector_matches(pdb_sel, cand_pods), "isolation.stable_pdb_cannot_select_candidate", f"expected the stable PDB selector {pdb_sel} to be unable to select candidate Pods {cand_pods}")
    c.check(not selector_matches(stable_sel, cand_pods) and not selector_matches(stable_svc_sel, cand_pods), "isolation.stable_selectors_reject_candidate_pods", "expected neither stable Deployment nor stable Service selector to match the candidate Pod template")
    c.check(not selector_matches(cand_sel, stable_pods) and not selector_matches(cand_svc_sel, stable_pods), "isolation.candidate_selectors_reject_stable_pods", "expected neither candidate selector to match the stable Pod template")
    others = [(k, n) for k, n in (("Deployment", APP_DEPLOYMENT), ("StatefulSet", STATE_STATEFULSET))]
    leaks = [n for k, n in others if selector_matches(cand_sel, _template_labels(c.by_kind_name(k, n))) or selector_matches(cand_svc_sel, _template_labels(c.by_kind_name(k, n)))]
    c.check(not leaks, "isolation.candidate_selectors_reject_other_workloads", f"expected candidate selectors to match no app/state Pod template, matched {leaks}")
    pdb_selecting = [p.get("metadata", {}).get("name") for p in c.by_kind("PodDisruptionBudget") if selector_matches(_match_labels(p, "spec", "selector")[0] or {"__unanalyzable__": "x"}, cand_pods)]
    c.check(not pdb_selecting, "isolation.no_pdb_selects_candidate", f"expected no PodDisruptionBudget to select candidate Pods (a PDB would not protect a Recreate rollout anyway), found {pdb_selecting}")


# --------------------------------------------------------------------------
# DAY7: NetworkPolicy coverage (pure evaluation of rendered policies)
# --------------------------------------------------------------------------

INGRESS_GATEWAY_POD = (INGRESS_NAMESPACE, {"istio.io/gateway-name": GATEWAY_NAME})
VALIDATION_CLIENT_LABELS = {"app.kubernetes.io/component": "validation-client"}
VALIDATION_CLIENT_POD = (VALIDATION_NAMESPACE, VALIDATION_CLIENT_LABELS)


def _peer_matches(peer: dict, peer_ns: str, peer_labels: dict, own_ns: str) -> bool:
    if "ipBlock" in peer:
        return False
    ns_sel = peer.get("namespaceSelector")
    pod_sel = peer.get("podSelector")
    if ns_sel is None:
        ns_ok = peer_ns == own_ns
    else:
        ns_ok = selector_matches(ns_sel.get("matchLabels") or {}, {"kubernetes.io/metadata.name": peer_ns}) and "matchExpressions" not in ns_sel
    pod_ok = True if pod_sel is None else (selector_matches(pod_sel.get("matchLabels") or {}, peer_labels) and "matchExpressions" not in pod_sel)
    return ns_ok and pod_ok


def network_path_allowed(policies: list[dict], direction: str, target_labels: dict, peer_ns: str, peer_labels: dict, port: int, own_ns: str = EXPECTED_NAMESPACE) -> bool:
    """Pure NetworkPolicy semantics for one side of a connection: is
    `direction` ("Ingress" to, or "Egress" from, a Pod with
    `target_labels` in `own_ns`) allowed for the given peer and TCP
    port? Isolated only if some policy of that type selects the Pod;
    then allowed iff some rule of a selecting policy admits the peer and
    port (standard additive semantics)."""
    rule_key, peer_key = ("ingress", "from") if direction == "Ingress" else ("egress", "to")
    selecting = [
        p for p in policies
        if direction in (p.get("spec", {}).get("policyTypes") or [])
        and "matchExpressions" not in (p.get("spec", {}).get("podSelector") or {})
        and selector_matches((p.get("spec", {}).get("podSelector") or {}).get("matchLabels") or {}, target_labels)
    ]
    if not selecting:
        return True
    for policy in selecting:
        for rule in policy.get("spec", {}).get(rule_key) or []:
            peers = rule.get(peer_key)
            ports = rule.get("ports")
            port_ok = not ports or any(p.get("protocol", "TCP") == "TCP" and p.get("port") == port for p in ports)
            peer_ok = not peers or any(_peer_matches(peer, peer_ns, peer_labels, own_ns) for peer in peers)
            if port_ok and peer_ok:
                return True
    return False


def connection_allowed(policies: list[dict], src: tuple[str, dict], dst: tuple[str, dict], port: int) -> bool:
    """Both ends must allow: source egress AND destination ingress. A
    source outside the release namespace is not governed by this
    chart's policies on its egress side."""
    src_ns, src_labels = src
    dst_ns, dst_labels = dst
    egress_ok = True if src_ns != EXPECTED_NAMESPACE else network_path_allowed(policies, "Egress", src_labels, dst_ns, dst_labels, port)
    ingress_ok = True if dst_ns != EXPECTED_NAMESPACE else network_path_allowed(policies, "Ingress", dst_labels, src_ns, src_labels, port)
    return egress_ok and ingress_ok


def _pod(doc: dict | None) -> tuple[str, dict]:
    return EXPECTED_NAMESPACE, _template_labels(doc)


def gateway_path_matrix(policies: list[dict], gateway: tuple[str, dict], app: tuple[str, dict], state: tuple[str, dict], validation_client: tuple[str, dict] = VALIDATION_CLIENT_POD) -> dict[str, bool]:
    """The application-port path matrix for one gateway-shaped workload."""
    VALIDATION_CLIENT_POD = validation_client  # noqa: N806 - shadows the module default for this render
    return {
        "istio-ingress -> gateway:8080": connection_allowed(policies, INGRESS_GATEWAY_POD, gateway, BACKEND_PORT),
        "validation-client -> gateway:8080": connection_allowed(policies, VALIDATION_CLIENT_POD, gateway, BACKEND_PORT),
        "app -> gateway:8080": connection_allowed(policies, app, gateway, BACKEND_PORT),
        "state -> gateway:8080": connection_allowed(policies, state, gateway, BACKEND_PORT),
        "gateway -> app:8080": connection_allowed(policies, gateway, app, BACKEND_PORT),
        "gateway -> state:8080": connection_allowed(policies, gateway, state, BACKEND_PORT),
        # One-sided entries: a widening on EITHER side is a defect even
        # while the other side still blocks the connection end to end.
        "gateway egress-side permits app:8080": network_path_allowed(policies, "Egress", gateway[1], app[0], app[1], BACKEND_PORT),
        "gateway egress-side permits state:8080": network_path_allowed(policies, "Egress", gateway[1], state[0], state[1], BACKEND_PORT),
        "state ingress-side permits gateway:8080": network_path_allowed(policies, "Ingress", state[1], gateway[0], gateway[1], BACKEND_PORT),
        "app ingress-side permits gateway:8080": network_path_allowed(policies, "Ingress", app[1], gateway[0], gateway[1], BACKEND_PORT),
        "gateway -> ztunnel HBONE:15008 (egress)": network_path_allowed(policies, "Egress", gateway[1], "istio-system", {}, 15008),
        "ztunnel HBONE:15008 -> gateway (ingress)": network_path_allowed(policies, "Ingress", gateway[1], "istio-system", {}, 15008),
    }


EXPECTED_GATEWAY_PATH_MATRIX = {
    "istio-ingress -> gateway:8080": True,
    "validation-client -> gateway:8080": False,
    "app -> gateway:8080": False,
    "state -> gateway:8080": False,
    "gateway -> app:8080": True,
    "gateway -> state:8080": False,
    "gateway egress-side permits app:8080": True,
    "gateway egress-side permits state:8080": False,
    "state ingress-side permits gateway:8080": False,
    "app ingress-side permits gateway:8080": True,
    "gateway -> ztunnel HBONE:15008 (egress)": True,
    "ztunnel HBONE:15008 -> gateway (ingress)": True,
}


def _check_network_policy_coverage(c: _Checker) -> None:
    policies = c.by_kind("NetworkPolicy")
    app = _pod(c.by_kind_name("Deployment", APP_DEPLOYMENT))
    state = _pod(c.by_kind_name("StatefulSet", STATE_STATEFULSET))
    stable = _pod(c.by_kind_name("Deployment", GATEWAY_DEPLOYMENT))
    client = (c.exp.validation_namespace, VALIDATION_CLIENT_LABELS)
    stable_matrix = gateway_path_matrix(policies, stable, app, state, client)
    c.check(stable_matrix == EXPECTED_GATEWAY_PATH_MATRIX, "networkpolicy.coverage.stable_gateway_paths", f"expected stable gateway paths {EXPECTED_GATEWAY_PATH_MATRIX}, found {stable_matrix}")
    c.check(connection_allowed(policies, app, state, BACKEND_PORT) and not connection_allowed(policies, state, app, BACKEND_PORT) and not connection_allowed(policies, client, app, BACKEND_PORT) and not connection_allowed(policies, client, state, BACKEND_PORT),
            "networkpolicy.coverage.app_state_paths_unchanged", "expected app -> state allowed and state -> app / validation-client -> app|state denied (no widening)")
    if not c.exp.candidate_enabled:
        return
    cand = _pod(c.by_kind_name("Deployment", CANDIDATE_DEPLOYMENT))
    cand_matrix = gateway_path_matrix(policies, cand, app, state, client)
    c.check(cand_matrix == EXPECTED_GATEWAY_PATH_MATRIX, "networkpolicy.coverage.candidate_paths_match_stable", f"expected the candidate to have exactly the stable gateway's paths {EXPECTED_GATEWAY_PATH_MATRIX}, found {cand_matrix}")
    c.check(not connection_allowed(policies, cand, stable, BACKEND_PORT) and not connection_allowed(policies, stable, cand, BACKEND_PORT), "networkpolicy.coverage.no_stable_candidate_lateral_path", "expected no application-port path between stable and candidate gateways")
    c.check(not connection_allowed(policies, app, cand, BACKEND_PORT) and not connection_allowed(policies, state, cand, BACKEND_PORT), "networkpolicy.coverage.candidate_not_reachable_from_app_or_state", "expected app/state -> candidate denied")
    candidate_policies = [p for p in policies if p.get("metadata", {}).get("name") in CANDIDATE_NETWORK_POLICY_NAMES]
    widened = [
        p.get("metadata", {}).get("name") for p in candidate_policies
        if selector_matches((p.get("spec", {}).get("podSelector") or {}).get("matchLabels") or {}, state[1])
        or selector_matches((p.get("spec", {}).get("podSelector") or {}).get("matchLabels") or {}, stable[1])
    ]
    c.check(not widened, "networkpolicy.coverage.candidate_policies_never_select_state_or_stable", f"expected candidate-only policies to select only candidate or app Pods, found {widened}")


# --------------------------------------------------------------------------
# DAY7: AuthorizationPolicy coverage
# --------------------------------------------------------------------------


def _check_authorization_coverage(c: _Checker) -> None:
    policies = c.by_kind("AuthorizationPolicy")

    def selecting(labels: dict) -> list[str]:
        return sorted(p.get("metadata", {}).get("name") for p in policies if selector_matches((p.get("spec", {}).get("selector") or {}).get("matchLabels") or {}, labels))

    stable = _template_labels(c.by_kind_name("Deployment", GATEWAY_DEPLOYMENT))
    c.check(selecting(stable) == [AUTHZ_GATEWAY], "mesh.authz.coverage.stable_gateway", f"expected only {AUTHZ_GATEWAY} to select stable gateway Pods, found {selecting(stable)}")
    if not c.exp.candidate_enabled:
        return
    cand = _template_labels(c.by_kind_name("Deployment", CANDIDATE_DEPLOYMENT))
    c.check(selecting(cand) == [AUTHZ_CANDIDATE], "mesh.authz.coverage.candidate", f"expected exactly {AUTHZ_CANDIDATE} to select candidate Pods (an unselected ambient workload would accept any mesh identity), found {selecting(cand)}")

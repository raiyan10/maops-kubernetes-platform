#!/usr/bin/env python3
"""
DAY8: the three pinned autoscaling add-ons - Metrics Server, VPA, KEDA.

  static  (cluster-free) helm-values/day8/*.yaml and the Makefile pins
          agree with day8_common.ADDONS, and every safety guard the
          design relies on is present in the values:
            - metrics-server: --kubelet-insecure-tls only (documented
              local-kind limitation), bounded resources;
            - VPA: updater DISABLED, --vpa-object-namespace on recommender
              and admission controller, webhook namespaceSelector matching
              ONLY the scaling namespace, failurePolicy Ignore;
            - KEDA: watchNamespace = the scaling namespace (its broad
              operator ClusterRole is then bound only by RoleBindings).
  check active
          (live, read-only, while the scaling namespace exists) Helm
          releases deployed at the pinned chart/app versions, every
          component Ready on its pinned image, resource metrics served for
          nodes AND the application Pods, external metrics APIService
          Available, no VPA updater, the VPA webhook scoped, KEDA's
          effective RBAC (every binding of a KEDA ServiceAccount, evaluated
          by keda_binding_problems()) and SubjectAccessReviews: no KEDA
          identity can read Secrets or scale/patch workloads or create HPAs
          in maops-platform, while the operator CAN in the scaling namespace.
  check after-cleanup
          the state cleanup leaves: Metrics Server and VPA still installed
          and serving, the scaling namespace gone, and KEDA UNINSTALLED -
          Helm release, every chart-owned object (CRDs, RBAC, webhook,
          APIService, Deployments, Services, ServiceAccounts, the scoped
          RoleBinding), the operator's runtime cert Secret and lease, every
          Pod in `keda`, and every binding naming a KEDA ServiceAccount all
          explicitly NotFound (an unreadable API is a failure).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day8_common as common
import day8_objects
import k8s_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
VALUES_DIR = REPO_ROOT / "helm-values" / "day8"
MAKEFILE = REPO_ROOT / "Makefile"
SCOPE_ARG = f"--vpa-object-namespace={day8_objects.NAMESPACE}"
MAKE_VARS = {
    "metrics-server": ("METRICS_SERVER_CHART_VERSION", "METRICS_SERVER_APP_VERSION"),
    "vertical-pod-autoscaler": ("VPA_CHART_VERSION", "VPA_APP_VERSION"),
    "keda": ("KEDA_CHART_VERSION", "KEDA_APP_VERSION"),
}
IMAGES = {
    "metrics-server": ("registry.k8s.io/metrics-server/metrics-server:v{v}",),
    "vertical-pod-autoscaler": ("registry.k8s.io/autoscaling/vpa-recommender:{v}", "registry.k8s.io/autoscaling/vpa-admission-controller:{v}"),
    "keda": ("ghcr.io/kedacore/keda:{v}", "ghcr.io/kedacore/keda-metrics-apiserver:{v}", "ghcr.io/kedacore/keda-admission-webhooks:{v}"),
}


def load_values(name: str, directory: Path = VALUES_DIR) -> dict:
    docs = k8s_yaml.load_all((directory / f"{name}.yaml").read_text())
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ValueError(f"{name}.yaml must hold exactly one mapping")
    return docs[0]


def makefile_vars(text: str) -> dict:
    return dict(re.findall(r"^([A-Z0-9_]+) := (\S+)\s*$", text, flags=re.M))


def static_problems(values: dict[str, dict], make_vars: dict) -> list[str]:
    """Pure. `values`: release -> parsed values file."""
    p = []
    for release, (chart_var, app_var) in MAKE_VARS.items():
        pin = common.ADDONS[release]
        if make_vars.get(chart_var) != pin["chart_version"]:
            p.append(f"Makefile {chart_var}={make_vars.get(chart_var)!r}, expected {pin['chart_version']!r}")
        if make_vars.get(app_var) != pin["app_version"]:
            p.append(f"Makefile {app_var}={make_vars.get(app_var)!r}, expected {pin['app_version']!r}")

    ms = values["metrics-server"]
    if ms.get("args") != ["--kubelet-insecure-tls"]:
        p.append(f"metrics-server args {ms.get('args')!r}: expected exactly ['--kubelet-insecure-tls']")
    if not (ms.get("resources") or {}).get("limits"):
        p.append("metrics-server has no resource limits")

    vpa = values["vertical-pod-autoscaler"]
    if (vpa.get("updater") or {}).get("enabled") is not False:
        p.append("VPA updater must be explicitly disabled (updater.enabled: false) - nothing may evict or resize a running Pod")
    for component in ("recommender", "admissionController"):
        section = vpa.get(component) or {}
        if SCOPE_ARG not in (section.get("extraArgs") or []):
            p.append(f"VPA {component} lacks {SCOPE_ARG}")
        if not (section.get("resources") or {}).get("limits"):
            p.append(f"VPA {component} has no resource limits")
    webhook = (vpa.get("admissionController") or {}).get("mutatingWebhookConfiguration") or {}
    if webhook.get("namespaceSelector") != {"matchLabels": {"kubernetes.io/metadata.name": day8_objects.NAMESPACE}}:
        p.append(f"VPA webhook namespaceSelector {webhook.get('namespaceSelector')!r} must match only {day8_objects.NAMESPACE}")
    if webhook.get("failurePolicy") != "Ignore":
        p.append("VPA webhook failurePolicy must be Ignore (an unavailable webhook must never block Pod creation)")

    keda = values["keda"]
    if (keda.get("webhooks") or {}).get("failurePolicy") != "Fail":
        p.append(f"KEDA webhooks.failurePolicy {(keda.get('webhooks') or {}).get('failurePolicy')!r}, expected 'Fail' (the webhooks intercept only KEDA's own kinds; fail closed rather than admit unvalidated ScaledObjects)")
    if common.ADDONS["keda"]["chart_version"] != KEDA_INVENTORY_CHART_VERSION:
        p.append(f"KEDA chart pin {common.ADDONS['keda']['chart_version']} != inventory version {KEDA_INVENTORY_CHART_VERSION} - regenerate the KEDA inventory from helm template")
    if keda.get("watchNamespace") != day8_objects.NAMESPACE:
        p.append(f"KEDA watchNamespace {keda.get('watchNamespace')!r}, expected {day8_objects.NAMESPACE!r} - an empty value makes KEDA cluster-wide (ClusterRoleBinding to its operator role)")
    for component in ("operator", "metricServer", "webhooks"):
        if not ((keda.get("resources") or {}).get(component) or {}).get("limits"):
            p.append(f"KEDA {component} has no resource limits")
    return p


def makefile_keda_problems(text: str) -> list[str]:
    """Pure over the Makefile text: the KEDA install is preceded by the
    ownership pre-check, carries Day 8's owner label, and never lets Helm
    create (adopt) the `keda` namespace."""
    m = re.search(r"^day8-addons-install:.*?\n((?:\t.*\n)+)", text, flags=re.M)
    if not m:
        return ["Makefile has no day8-addons-install recipe"]
    recipe = m.group(1)
    p = []
    keda_cmd = recipe[recipe.find("helm upgrade --install keda"):]
    key, value = day8_objects.KEDA_OWNER_LABEL
    if "keda-preinstall" not in recipe or recipe.find("keda-preinstall") > recipe.find("helm upgrade --install"):
        p.append("day8-addons-install must run `day8_addons.py keda-preinstall` before any helm install")
    if "--create-namespace" in keda_cmd.split("\n'")[0]:
        p.append("the KEDA helm install must not use --create-namespace (Day 8 creates its own labelled namespace)")
    if "--labels $(DAY8_KEDA_OWNER_LABEL)" not in keda_cmd or f"DAY8_KEDA_OWNER_LABEL := {key}={value}" not in text:
        p.append(f"the KEDA helm install must carry --labels {key}={value}")
    if "maops-kubernetes-platform-day8/day8-scaling" not in recipe:
        p.append("day8-addons-install must require BOTH Day 8 identity labels on maops-day8-scaling")
    return p


def static() -> int:
    checks = common.Checks("Day 8 static check (cluster-free)")
    values = {name: load_values(name) for name in common.ADDONS}
    makefile_text = MAKEFILE.read_text()
    problems = static_problems(values, makefile_vars(makefile_text)) + makefile_keda_problems(makefile_text)
    checks.record(not problems, "add-on values/pins: " + ("; ".join(problems) if problems else "pinned versions agree, every guard present"))
    design = day8_objects.design_problems()
    checks.record(not design, "scaling design: " + ("; ".join(design) if design else "within the LimitRange at declared and worst-case sizes, one scaler per target"))
    b = day8_objects.budget()
    checks.record(b.hard() == day8_objects.quota_object()["spec"]["hard"], f"ResourceQuota hard == computed worst-case budget {b.hard()}")
    over = day8_objects.over_quota_pod(day8_objects.placeholder_image(), b)
    over_cpu = sum(day8_objects.cpu_millis(c["resources"]["requests"]["cpu"]) for c in over["spec"]["containers"])
    checks.record(over_cpu > b.requests_cpu_m, f"over-budget proof Pod requests {over_cpu}m > quota requests.cpu {b.requests_cpu_m}m, each container within the LimitRange")
    try:
        day8_objects.all_objects(day8_objects.placeholder_image())
        checks.record(True, "every Day 8 object renders")
    except ValueError as exc:
        checks.record(False, f"render failed: {exc}")
    return checks.finish("Day 8 add-on values and scaling design are statically sound")


# --------------------------------------------------------------------------
# Live, read-only
# --------------------------------------------------------------------------


def _deployments(namespace: str) -> list[dict]:
    return common.kubectl_json("-n", namespace, "get", "deployments")["items"]


def _ready(dep: dict) -> bool:
    want = dep["spec"].get("replicas", 1)
    return want >= 1 and dep.get("status", {}).get("readyReplicas", 0) == want


def _images(dep: dict) -> list[str]:
    return [c["image"] for c in dep["spec"]["template"]["spec"]["containers"]]


def _apiservice_available(name: str) -> tuple[bool, str]:
    obj = common.kubectl_json_or_none("get", "apiservice", name)
    if obj is None:
        return False, "missing"
    cond = next((c for c in obj.get("status", {}).get("conditions", []) if c.get("type") == "Available"), {})
    return cond.get("status") == "True", f"Available={cond.get('status')} ({cond.get('reason')})"


def resource_metrics() -> tuple[int, int]:
    nodes = json.loads(common.kubectl("get", "--raw", "/apis/metrics.k8s.io/v1beta1/nodes").stdout).get("items", [])
    pods = json.loads(common.kubectl("get", "--raw", "/apis/metrics.k8s.io/v1beta1/namespaces/maops-platform/pods").stdout).get("items", [])
    with_usage = [n for n in nodes if (n.get("usage") or {}).get("cpu")]
    return len(with_usage), len([p for p in pods if p.get("containers")])


# --------------------------------------------------------------------------
# KEDA effective RBAC
# --------------------------------------------------------------------------

KEDA_NAMESPACE = day8_objects.KEDA_NAMESPACE
KEDA_SERVICE_ACCOUNTS = ("keda-operator", "keda-metrics-server", "keda-webhook")
KEDA_OPERATOR_ROLE = "keda-operator"
SCOPED_BINDING = "keda-operator"
# Cluster-wide bindings chart keda-2.21.0 creates in namespace-scoped mode
# (binding -> ClusterRole). None grants Secrets, */scale or HPA writes.
ALLOWED_KEDA_CLUSTER_BINDINGS = {
    "keda-operator-minimal": "keda-operator-minimal-cluster-role",
    "keda-operator-system-auth-delegator": "system:auth-delegator",
    "keda-operator-webhook": "keda-operator-webhook",
}
ALLOWED_KEDA_ROLE_BINDINGS = {
    (KEDA_NAMESPACE, "keda-operator"): ("ClusterRole", KEDA_OPERATOR_ROLE),
    (KEDA_NAMESPACE, "keda-operator-certs"): ("Role", "keda-operator-certs"),
    ("kube-system", "keda-operator-auth-reader"): ("Role", "extension-apiserver-authentication-reader"),
}
# Probe matrices (verb, resource, subresource). A namespace of None means a
# cluster-wide / all-namespaces SubjectAccessReview (`--all-namespaces`),
# which is also how cluster-scoped resources are probed.
APP_NAMESPACE = "maops-platform"
# What no Day 8 add-on identity may do in the application namespace.
FORBIDDEN_IN_APP = (
    ("get", "secrets", ""),
    ("list", "secrets", ""),
    ("watch", "secrets", ""),
    ("patch", "deployments.apps", "scale"),
    ("update", "deployments.apps", "scale"),
    ("patch", "statefulsets.apps", "scale"),
    ("update", "statefulsets.apps", "scale"),
    ("patch", "deployments.apps", ""),
    ("update", "deployments.apps", ""),
    ("delete", "deployments.apps", ""),
    ("patch", "statefulsets.apps", ""),
    ("update", "statefulsets.apps", ""),
    ("delete", "statefulsets.apps", ""),
    ("create", "pods", ""),
    ("patch", "pods", ""),
    ("delete", "pods", ""),
    ("create", "pods", "exec"),
    ("create", "pods", "eviction"),
    ("update", "configmaps", ""),
    ("delete", "configmaps", ""),
    ("create", "horizontalpodautoscalers.autoscaling", ""),
    ("create", "rolebindings.rbac.authorization.k8s.io", ""),
)
# Secrets elsewhere: all namespaces at once, and the sensitive ones by name.
FORBIDDEN_SECRETS_ELSEWHERE = (
    ("list", "secrets", "", None),
    ("get", "secrets", "", "kube-system"),
    ("get", "secrets", "", "istio-system"),
    ("get", "secrets", "", "maops-ingress"),
    ("get", "secrets", "", "maops-day7-validation"),
)
OPERATOR_IN_SCALING = (
    ("patch", "deployments.apps", "scale"),
    ("create", "horizontalpodautoscalers.autoscaling", ""),
    ("list", "scaledobjects.keda.sh", ""),
)
# The persistent add-ons' identities (namespace, ServiceAccount): probed in
# BOTH modes with the same FORBIDDEN matrices.
PERSISTENT_ADDON_IDENTITIES = (
    ("kube-system", "metrics-server"),
    ("vpa-system", "vertical-pod-autoscaler-recommender"),
    ("vpa-system", "vertical-pod-autoscaler-admission-controller"),
)
# Known grants that ARE expected (measured, must answer yes): the documented
# residual exposure, recorded as numbers rather than prose. A "no" here means
# the chart changed and the documentation no longer describes reality.
EXPECTED_GRANTS = (
    ("keda", "keda-operator", "patch", "apiservices.apiregistration.k8s.io", "", None),
    ("keda", "keda-operator", "patch", "validatingwebhookconfigurations.admissionregistration.k8s.io", "", None),
    ("keda", "keda-webhook", "list", "deployments.apps", "", None),
    ("kube-system", "metrics-server", "list", "pods", "", None),
    ("vpa-system", "vertical-pod-autoscaler-recommender", "list", "pods", "", None),
    ("vpa-system", "vertical-pod-autoscaler-admission-controller", "list", "pods", "", None),
)


def _keda_subjects(binding: dict) -> list[str]:
    return [
        s["name"]
        for s in binding.get("subjects") or []
        if s.get("kind") == "ServiceAccount" and s.get("namespace") == KEDA_NAMESPACE and s.get("name") in KEDA_SERVICE_ACCOUNTS
    ]


def keda_binding_problems(cluster_bindings: list[dict], role_bindings: list[dict], scaling_namespace_present: bool) -> list[str]:
    """Pure. Every (Cluster)RoleBinding whose subject is a KEDA ServiceAccount
    must be one the namespace-scoped chart creates; the operator ClusterRole
    may be bound only per namespace, in `keda` and - exactly while it exists -
    the scaling namespace."""
    problems = []
    for b in cluster_bindings:
        if not _keda_subjects(b):
            continue
        name, role = b["metadata"]["name"], b["roleRef"]["name"]
        if ALLOWED_KEDA_CLUSTER_BINDINGS.get(name) != role or b["roleRef"].get("kind") != "ClusterRole":
            problems.append(f"ClusterRoleBinding {name} -> {b['roleRef'].get('kind')}/{role} grants {_keda_subjects(b)} cluster-wide (not a namespace-scoped KEDA binding)")
    scoped_seen = False
    for b in role_bindings:
        if not _keda_subjects(b):
            continue
        ns, name = b["metadata"]["namespace"], b["metadata"]["name"]
        ref = (b["roleRef"].get("kind"), b["roleRef"]["name"])
        if ns == day8_objects.NAMESPACE and name == SCOPED_BINDING and ref == ("ClusterRole", KEDA_OPERATOR_ROLE):
            scoped_seen = True
        elif ALLOWED_KEDA_ROLE_BINDINGS.get((ns, name)) != ref:
            problems.append(f"RoleBinding {ns}/{name} -> {ref[0]}/{ref[1]} grants {_keda_subjects(b)} rights outside {KEDA_NAMESPACE} and {day8_objects.NAMESPACE}")
    if scaling_namespace_present and not scoped_seen:
        problems.append(f"scoped RoleBinding {day8_objects.NAMESPACE}/{SCOPED_BINDING} -> ClusterRole/{KEDA_OPERATOR_ROLE} is missing - KEDA cannot act in its own namespace")
    if not scaling_namespace_present and scoped_seen:
        problems.append(f"scoped RoleBinding {day8_objects.NAMESPACE}/{SCOPED_BINDING} still exists although its namespace is gone")
    return problems


def parse_can_i(returncode: int, stdout: str, stderr: str) -> bool:
    """Pure. `kubectl auth can-i` prints yes (exit 0) or no (exit 1).
    Anything else is an error - never read as 'no'. So is ANY kubectl
    warning: an unknown/misspelled resource ("the server doesn't have a
    resource type") or a mis-scoped probe ("is not namespace scoped") also
    print "no", which must not count as a proven denial."""
    if "Warning:" in (stderr or ""):
        raise common.Day8Error(f"kubectl auth can-i answered with a warning - probe is not meaningful: {stderr.strip()[:200]}")
    answer = (stdout.strip().splitlines() or [""])[0].strip()
    if returncode == 0 and answer == "yes":
        return True
    if returncode == 1 and answer == "no":
        return False
    raise common.Day8Error(f"kubectl auth can-i gave no yes/no answer (exit {returncode}): {stdout.strip()[:120]} {stderr.strip()[:200]}")


def can_i(service_account: str, verb: str, resource: str, namespace: str | None, subresource: str = "", sa_namespace: str = KEDA_NAMESPACE) -> bool:
    scope = ["--all-namespaces"] if namespace is None else ["-n", namespace]
    args = ["auth", "can-i", verb, resource, *scope, f"--as=system:serviceaccount:{sa_namespace}:{service_account}"]
    if subresource:
        args.append(f"--subresource={subresource}")
    r = common.kubectl(*args, check=False)
    return parse_can_i(r.returncode, r.stdout, r.stderr)


def _action(verb: str, resource: str, subresource: str, namespace: str | None = APP_NAMESPACE) -> str:
    where = "all namespaces" if namespace is None else namespace
    return f"{verb} {resource}{'/' + subresource if subresource else ''} @{where}"


def forbidden_access(sa_namespace: str, service_account: str) -> list[str]:
    """Every action from FORBIDDEN_IN_APP (in maops-platform) and
    FORBIDDEN_SECRETS_ELSEWHERE this identity CAN perform (should be [])."""
    allowed = [_action(v, r, s) for v, r, s in FORBIDDEN_IN_APP if can_i(service_account, v, r, APP_NAMESPACE, s, sa_namespace)]
    allowed += [_action(v, r, s, ns) for v, r, s, ns in FORBIDDEN_SECRETS_ELSEWHERE if can_i(service_account, v, r, ns, s, sa_namespace)]
    return allowed


def record_access_matrix(checks: common.Checks, identities, expected_grants) -> list[dict]:
    """Probes each (namespace, ServiceAccount) against the forbidden
    matrices and each expected grant; returns the evidence rows. Any
    forbidden 'yes' fails naming the identity and action; an expected grant
    that is no longer granted fails too (documentation drift); a probe
    error propagates (fail closed)."""
    rows = []
    for sa_ns, sa in identities:
        allowed = forbidden_access(sa_ns, sa)
        rows.append({"identity": f"{sa_ns}/{sa}", "probes": len(FORBIDDEN_IN_APP) + len(FORBIDDEN_SECRETS_ELSEWHERE), "allowed_forbidden_actions": allowed})
        checks.record(not allowed, f"{sa_ns}/{sa}: none of {len(FORBIDDEN_IN_APP)} forbidden actions in {APP_NAMESPACE} nor {len(FORBIDDEN_SECRETS_ELSEWHERE)} Secret probes elsewhere allowed ({allowed or 'all denied'})")
    for sa_ns, sa, v, r, s, ns in expected_grants:
        granted = can_i(sa, v, r, ns, s, sa_ns)
        rows.append({"identity": f"{sa_ns}/{sa}", "expected_grant": _action(v, r, s, ns), "granted": granted})
        checks.record(granted, f"expected (documented) grant {sa_ns}/{sa}: {_action(v, r, s, ns)} = {'yes' if granted else 'NO - chart/RBAC changed, documentation is stale'}")
    return rows


def _record_keda_rbac(checks: common.Checks, mode: str = "active") -> dict:
    """Active mode only: KEDA installed, the scaling namespace present."""
    scaling_present = True
    cluster_bindings = common.kubectl_json("get", "clusterrolebindings")["items"]
    role_bindings = common.kubectl_json("get", "rolebindings", "--all-namespaces")["items"]
    problems = keda_binding_problems(cluster_bindings, role_bindings, scaling_present)
    effective = {
        "cluster_role_bindings": sorted(f"{b['metadata']['name']} -> {b['roleRef']['name']} {_keda_subjects(b)}" for b in cluster_bindings if _keda_subjects(b)),
        "role_bindings": sorted(f"{b['metadata']['namespace']}/{b['metadata']['name']} -> {b['roleRef']['kind']}/{b['roleRef']['name']} {_keda_subjects(b)}" for b in role_bindings if _keda_subjects(b)),
        "access": [],
    }
    checks.record(not problems, "KEDA effective bindings are namespace-scoped: " + ("; ".join(problems) if problems else f"{effective['role_bindings']} + cluster-wide {effective['cluster_role_bindings']}"))
    keda_identities = [(KEDA_NAMESPACE, sa) for sa in KEDA_SERVICE_ACCOUNTS]
    keda_grants = [g for g in EXPECTED_GRANTS if g[0] == KEDA_NAMESPACE]
    effective["access"] = record_access_matrix(checks, keda_identities, keda_grants)
    granted = {f"{v} {r}{'/' + s if s else ''}": can_i("keda-operator", v, r, day8_objects.NAMESPACE, s) for v, r, s in OPERATOR_IN_SCALING}
    effective["access"].append({"identity": "keda/keda-operator", "namespace": day8_objects.NAMESPACE, "granted": granted})
    checks.record(all(granted.values()), f"keda/keda-operator CAN act in {day8_objects.NAMESPACE} (positive control): {granted}")
    return effective


def kube_namespace() -> str:
    import kube

    return kube.NAMESPACE


# --------------------------------------------------------------------------
# KEDA lifecycle: every chart-owned object (chart keda-2.21.0 rendered with
# helm-values/day8/keda.yaml) plus the two objects the operator creates at
# runtime. Day 8 installs KEDA per run and UNINSTALLS it in cleanup, so after
# cleanup every one of these must be explicitly NotFound.
# --------------------------------------------------------------------------

KEDA_RELEASE = "keda"
KEDA_CRDS = (
    "cloudeventsources.eventing.keda.sh",
    "clustercloudeventsources.eventing.keda.sh",
    "clustertriggerauthentications.keda.sh",
    "scaledjobs.keda.sh",
    "scaledobjects.keda.sh",
    "triggerauthentications.keda.sh",
)
KEDA_CLUSTER_OBJECTS = (
    *(("customresourcedefinition", crd) for crd in KEDA_CRDS),
    ("clusterrole", "keda-operator"),
    ("clusterrole", "keda-operator-external-metrics-reader"),
    ("clusterrole", "keda-operator-minimal-cluster-role"),
    ("clusterrole", "keda-operator-webhook"),
    ("clusterrolebinding", "keda-operator-hpa-controller-external-metrics"),
    ("clusterrolebinding", "keda-operator-minimal"),
    ("clusterrolebinding", "keda-operator-system-auth-delegator"),
    ("clusterrolebinding", "keda-operator-webhook"),
    ("validatingwebhookconfiguration", "keda-admission"),
    ("apiservice", "v1beta1.external.metrics.k8s.io"),
    # Not chart-owned: Day 8 creates and deletes the `keda` namespace itself.
    ("namespace", KEDA_NAMESPACE),
)
# The inventory above is chart keda-2.21.0's exact object set. It is only
# valid for that chart version: KEDA_INVENTORY_CHART_VERSION is asserted
# equal to the pinned add-on version (static check), and ownership checks
# refuse any release whose chart is not keda-<that version>. A chart bump
# must regenerate this list from `helm template`.
KEDA_INVENTORY_CHART_VERSION = "2.21.0"
KEDA_NAMESPACED_OBJECTS = (
    *(("deployment", KEDA_NAMESPACE, n) for n in ("keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks")),
    *(("service", KEDA_NAMESPACE, n) for n in ("keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks")),
    *(("serviceaccount", KEDA_NAMESPACE, n) for n in KEDA_SERVICE_ACCOUNTS),
    ("role", KEDA_NAMESPACE, "keda-operator-certs"),
    ("rolebinding", KEDA_NAMESPACE, "keda-operator"),
    ("rolebinding", KEDA_NAMESPACE, "keda-operator-certs"),
    ("rolebinding", "kube-system", "keda-operator-auth-reader"),
    ("rolebinding", day8_objects.NAMESPACE, SCOPED_BINDING),
)
# Created by the running operator, not by Helm - so `helm uninstall` leaves
# them behind: its self-signed webhook/gRPC certificate (a private key) and
# its leader-election lease. Cleanup deletes exactly these two, by name.
KEDA_RUNTIME_OBJECTS = (
    ("secret", KEDA_NAMESPACE, "kedaorg-certs"),
    ("lease.coordination.k8s.io", KEDA_NAMESPACE, "operator.keda.sh"),
)


def keda_inventory_states(state_of) -> dict[str, list[str]]:
    """Pure over `state_of(kind, name, namespace|None) -> "present"|"absent"`
    (which raises when existence cannot be determined): every KEDA object
    sorted into present / absent / unreadable."""
    out = {"present": [], "absent": [], "unreadable": []}
    items = [(k, None, n) for k, n in KEDA_CLUSTER_OBJECTS] + list(KEDA_NAMESPACED_OBJECTS) + list(KEDA_RUNTIME_OBJECTS)
    for kind, namespace, name in items:
        label = f"{kind}/{namespace + '/' if namespace else ''}{name}"
        try:
            out[state_of(kind, name, namespace)].append(label)
        except common.Day8Error as exc:
            out["unreadable"].append(f"{label} ({exc})")
    return out


def record_keda_absent(checks: common.Checks) -> dict:
    """Records that KEDA is fully gone - Helm release, every chart-owned and
    runtime object, every Pod in `keda`, and every binding naming a KEDA
    ServiceAccount. Unreadable answers are failures."""
    try:
        release = common.helm_release_state(KEDA_RELEASE, KEDA_NAMESPACE)
    except common.Day8Error as exc:
        release = f"unreadable ({exc})"
    checks.record(release == "absent", f"Helm release {KEDA_NAMESPACE}/{KEDA_RELEASE} uninstalled (state: {release})")
    states = keda_inventory_states(lambda kind, name, namespace: common.object_state(kind, name, namespace))
    checks.record(not states["present"] and not states["unreadable"], f"all {len(states['absent']) + len(states['present']) + len(states['unreadable'])} KEDA chart-owned and runtime objects explicitly NotFound (CRDs, RBAC, webhook, APIService, Deployments, Services, ServiceAccounts, scoped binding, cert Secret, lease): present {states['present'] or 'none'}, unreadable {states['unreadable'] or 'none'}")
    pods = common.kubectl("-n", KEDA_NAMESPACE, "get", "pods", "-o", "name", check=False)
    pod_names = pods.stdout.split() if pods.returncode == 0 else None
    checks.record(pod_names == [], f"no Pod left in namespace {KEDA_NAMESPACE} ({pod_names if pod_names is not None else 'UNREADABLE: ' + pods.stderr.strip()[:160]})")
    cluster_bindings = common.kubectl_json("get", "clusterrolebindings")["items"]
    role_bindings = common.kubectl_json("get", "rolebindings", "--all-namespaces")["items"]
    leftover = [f"ClusterRoleBinding/{b['metadata']['name']}" for b in cluster_bindings if _keda_subjects(b)] + [f"RoleBinding/{b['metadata']['namespace']}/{b['metadata']['name']}" for b in role_bindings if _keda_subjects(b)]
    checks.record(not leftover, f"no binding anywhere still names a KEDA ServiceAccount ({leftover or 'none'})")
    return {"release": release, "objects": states, "pods": pod_names, "bindings_naming_keda": leftover}


# --------------------------------------------------------------------------
# KEDA ownership: Day 8 never takes over, upgrades, uninstalls or deletes a
# KEDA release or `keda` namespace it did not create.
# --------------------------------------------------------------------------

KEDA_WEBHOOK_CONFIG = "keda-admission"
KEDA_WEBHOOK_GROUPS = {"keda.sh", "eventing.keda.sh"}


def keda_release_ownership_problems(metadata: dict | None) -> list[str]:
    """Pure over `helm get metadata -o json`: the release must be the pinned
    keda chart, in namespace `keda`, carrying Day 8's owner label."""
    if metadata is None:
        return ["no KEDA Helm release"]
    key, value = day8_objects.KEDA_OWNER_LABEL
    pin = common.ADDONS[KEDA_RELEASE]
    problems = []
    if metadata.get("chart") != "keda" or metadata.get("version") != pin["chart_version"]:
        problems.append(f"chart {metadata.get('chart')}-{metadata.get('version')} is not the pinned keda-{pin['chart_version']}")
    if metadata.get("namespace") != KEDA_NAMESPACE:
        problems.append(f"release namespace {metadata.get('namespace')!r} is not {KEDA_NAMESPACE!r}")
    if (metadata.get("labels") or {}).get(key) != value:
        problems.append(f"release lacks Day 8's owner label {key}={value} (labels: {sorted((metadata.get('labels') or {}).items())})")
    return problems


def keda_webhook_problems(config: dict | None) -> list[str]:
    """Pure over the live `keda-admission` ValidatingWebhookConfiguration:
    every webhook fails CLOSED (failurePolicy Fail) and only intercepts
    KEDA's own API groups - so it can never block a non-KEDA write."""
    if config is None:
        return ["ValidatingWebhookConfiguration keda-admission missing"]
    problems = []
    for hook in config.get("webhooks") or []:
        if hook.get("failurePolicy") != "Fail":
            problems.append(f"{hook.get('name')}: failurePolicy {hook.get('failurePolicy')!r}, expected 'Fail'")
        groups = {g for rule in hook.get("rules") or [] for g in rule.get("apiGroups") or []}
        if not groups or not groups <= KEDA_WEBHOOK_GROUPS:
            problems.append(f"{hook.get('name')}: intercepts API groups {sorted(groups)}, expected only {sorted(KEDA_WEBHOOK_GROUPS)}")
    if not config.get("webhooks"):
        problems.append("keda-admission has no webhooks")
    return problems


def crd_presence(crds=None) -> dict[str, str]:
    """"present"/"absent" per KEDA CRD (raises when unreadable)."""
    return {crd: common.object_state("customresourcedefinition", crd) for crd in (crds or KEDA_CRDS)}


def keda_preinstall() -> int:
    """Runs inside `day8-addons-install`, immediately before the KEDA Helm
    install, under the Day 7 lock. Refuses a KEDA release Day 8 does not
    own, KEDA CRDs without a Day 8 release (a foreign or orphaned KEDA), and
    a `keda` namespace without Day 8's labels; otherwise creates Day 8's own
    `keda` namespace if it is absent. The only mutation: that namespace,
    made with `kubectl create` (never `apply`), so a namespace that appears
    between the check and the create is refused (AlreadyExists), never
    adopted or relabelled. Any refusal stops BEFORE that mutation."""
    common.require_cluster_profile()
    checks = common.Checks("Day 8 KEDA pre-install ownership check")
    metadata = common.helm_release_metadata(KEDA_RELEASE, KEDA_NAMESPACE)
    if metadata is not None:
        problems = keda_release_ownership_problems(metadata)
        checks.record(not problems, f"existing KEDA release is Day 8's own (will be upgraded): {'; '.join(problems) or 'owner label, chart and namespace match'}")
    else:
        present = [crd for crd, state in crd_presence().items() if state == "present"]
        checks.record(not present, f"no KEDA CRD exists without a Day 8 KEDA release ({present or 'none'}) - otherwise a foreign or orphaned KEDA is installed and Day 8 refuses to take it over")
    done = f"KEDA may be installed: no foreign release, CRDs or namespace (namespace {KEDA_NAMESPACE} is Day 8's)"
    if checks.failed:
        checks.info(f"refused before any change - namespace {KEDA_NAMESPACE} not created, nothing adopted")
        return checks.finish(done)
    ns = common.kubectl_json_or_none("get", "namespace", KEDA_NAMESPACE)
    if ns is None:
        r = common.kubectl("create", "-f", "-", "-o", "json", stdin=json.dumps(day8_objects.keda_namespace_object()), check=False, timeout=60)
        if r.returncode != 0:
            why = "it appeared after the pre-install check - refusing to adopt it" if "AlreadyExists" in (r.stderr or "") else (r.stderr or "").strip()[:200]
            checks.record(False, f"could not create Day 8's namespace {KEDA_NAMESPACE}: {why}")
            return checks.finish(done)
        uid = json.loads(r.stdout)["metadata"]["uid"]
        checks.info(f"namespace/{KEDA_NAMESPACE} created by Day 8 (uid {uid})")
        ns = common.kubectl_json("get", "namespace", KEDA_NAMESPACE)
        checks.record(ns["metadata"].get("uid") == uid, f"namespace {KEDA_NAMESPACE} is the one Day 8 just created (uid {ns['metadata'].get('uid')}, expected {uid})")
    owned = day8_objects.is_day8_owned(ns["metadata"].get("labels"), day8_objects.KEDA_NAMESPACE_COMPONENT)
    checks.record(owned, f"namespace {KEDA_NAMESPACE} carries Day 8's identity labels (instance {day8_objects.INSTANCE}, component {day8_objects.KEDA_NAMESPACE_COMPONENT}) - {'owned' if owned else 'NOT Day 8 namespace, refusing to install into it'}")
    return checks.finish(done)


def check(mode: str = "active") -> int:
    common.require_cluster_profile()
    checks = common.Checks(f"Day 8 add-on check ({mode}, live, read-only)")
    releases = {r["name"]: r for r in json.loads(common.helm("list", "--all-namespaces", "-o", "json").stdout or "[]")}
    # After cleanup KEDA is UNINSTALLED (verified by record_keda_absent);
    # Metrics Server and VPA stay installed in both modes.
    expected = common.ADDONS if mode == "active" else {k: v for k, v in common.ADDONS.items() if k != KEDA_RELEASE}
    for name, pin in expected.items():
        r = releases.get(name)
        chart = f"{name}-{pin['chart_version']}"
        checks.record(
            bool(r) and r.get("namespace") == pin["namespace"] and r.get("status") == "deployed" and r.get("chart") == chart and r.get("app_version") == pin["app_version"],
            f"Helm release {name}: {'missing' if not r else (r.get('namespace'), r.get('status'), r.get('chart'), r.get('app_version'))} (expected {pin['namespace']}, deployed, {chart}, {pin['app_version']})",
        )
        deps = [d for d in _deployments(pin["namespace"]) if d["metadata"].get("labels", {}).get("app.kubernetes.io/instance") == name or name in d["metadata"]["name"]]
        expected_images = {i.format(v=pin["app_version"]) for i in IMAGES[name]}
        running = {i for d in deps for i in _images(d)}
        checks.record(expected_images <= running, f"{name}: pinned images {sorted(expected_images)} deployed (found {sorted(running)})")
        checks.record(bool(deps) and all(_ready(d) for d in deps), f"{name}: {len(deps)} Deployment(s) Ready {[(d['metadata']['name'], d.get('status', {}).get('readyReplicas', 0)) for d in deps]}")

    ok, detail = _apiservice_available("v1beta1.metrics.k8s.io")
    checks.record(ok, f"APIService v1beta1.metrics.k8s.io {detail}")
    (nodes, pods), satisfied = common.poll(resource_metrics, timeout=120, interval=10, until=lambda v: v[0] == 3 and v[1] >= 7)
    checks.record(satisfied, f"resource metrics served: {nodes}/3 nodes with CPU usage, {pods} maops-platform Pod(s) with container usage (expected >= 7)")
    if mode == "active":
        ok, detail = _apiservice_available("v1beta1.external.metrics.k8s.io")
        checks.record(ok, f"APIService v1beta1.external.metrics.k8s.io (KEDA) {detail}")

    all_images = [c["image"] for d in common.kubectl_json("get", "deployments", "--all-namespaces")["items"] for c in d["spec"]["template"]["spec"]["containers"]]
    updaters = [i for i in all_images if "vpa-updater" in i]
    checks.record(not updaters, f"no VPA updater deployed anywhere (found {updaters}) - nothing can evict or resize a running Pod")
    for dep in _deployments("vpa-system"):
        args = dep["spec"]["template"]["spec"]["containers"][0].get("args", [])
        checks.record(SCOPE_ARG in args, f"vpa-system/{dep['metadata']['name']}: {SCOPE_ARG} {'present' if SCOPE_ARG in args else 'MISSING'}")
    hooks = [
        (cfg["metadata"]["name"], w)
        for cfg in common.kubectl_json("get", "mutatingwebhookconfigurations")["items"]
        for w in cfg.get("webhooks", [])
        if ((w.get("clientConfig") or {}).get("service") or {}).get("namespace") == "vpa-system"
    ]
    scoped = bool(hooks) and all(
        (w.get("namespaceSelector") or {}).get("matchLabels") == {"kubernetes.io/metadata.name": day8_objects.NAMESPACE}
        and not (w.get("namespaceSelector") or {}).get("matchExpressions")
        and w.get("failurePolicy") == "Ignore"
        for _, w in hooks
    )
    checks.record(scoped, f"VPA mutating webhook(s) {[n for n, _ in hooks]} select only namespace {day8_objects.NAMESPACE}, failurePolicy Ignore")

    addon_rows = record_access_matrix(checks, PERSISTENT_ADDON_IDENTITIES, [g for g in EXPECTED_GRANTS if g[0] != KEDA_NAMESPACE])
    scaling_present = common.kubectl_json_or_none("get", "namespace", day8_objects.NAMESPACE) is not None
    checks.record(scaling_present == (mode == "active"), f"scaling namespace {day8_objects.NAMESPACE} {'present' if scaling_present else 'absent'} (expected {'present' if mode == 'active' else 'absent after cleanup'})")
    if mode == "active":
        operator = next((d for d in _deployments(KEDA_NAMESPACE) if d["metadata"]["name"] == "keda-operator"), None)
        env = {e["name"]: e.get("value") for e in (operator["spec"]["template"]["spec"]["containers"][0].get("env", []) if operator else [])}
        checks.record(env.get("WATCH_NAMESPACE") == day8_objects.NAMESPACE, f"keda-operator WATCH_NAMESPACE={env.get('WATCH_NAMESPACE')!r} (expected {day8_objects.NAMESPACE!r})")
        restarts = [
            (p["metadata"]["name"], sum(c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses", [])))
            for p in common.kubectl_json("-n", KEDA_NAMESPACE, "get", "pods", "-l", "app=keda-operator")["items"]
        ]
        checks.info(f"keda-operator Pod restarts: {restarts}")
        ownership = keda_release_ownership_problems(common.helm_release_metadata(KEDA_RELEASE, KEDA_NAMESPACE))
        checks.record(not ownership, f"KEDA Helm release is Day 8's own ({'; '.join(ownership) or 'owner label, chart and namespace match'})")
        webhook = keda_webhook_problems(common.kubectl_json_or_none("get", "validatingwebhookconfiguration", KEDA_WEBHOOK_CONFIG))
        checks.record(not webhook, f"KEDA admission webhooks fail closed and intercept only KEDA's own API groups ({'; '.join(webhook) or 'all 6 failurePolicy Fail, groups keda.sh/eventing.keda.sh'})")
        effective = _record_keda_rbac(checks, mode)
        effective["persistent_addons_access"] = addon_rows
        stray = [
            f"{x['metadata']['namespace']}/{kind}/{x['metadata']['name']}"
            for kind in ("scaledobjects.keda.sh", "scaledjobs.keda.sh")
            for x in common.kubectl_json("get", kind, "--all-namespaces")["items"]
            if x["metadata"]["namespace"] != day8_objects.NAMESPACE
        ]
        checks.record(not stray, f"no KEDA ScaledObject/ScaledJob outside {day8_objects.NAMESPACE} ({stray or 'none'})")
        try:
            path = common.write_evidence(f"keda-rbac-{mode}-{common.utc_now().replace(':', '')}.json", {"mode": mode, "watch_namespace": env.get("WATCH_NAMESPACE"), "operator_restarts": restarts, **effective})
            checks.info(f"evidence: {path}")
        except common.Day8Error as exc:
            checks.info(f"no run directory for evidence ({exc})")
    else:
        evidence = record_keda_absent(checks)
        evidence["persistent_addons_access"] = addon_rows
        try:
            path = common.write_evidence(f"keda-state-after-cleanup-{common.utc_now().replace(':', '')}.json", evidence)
            checks.info(f"evidence: {path}")
        except common.Day8Error as exc:
            checks.info(f"no run directory for evidence ({exc})")
    if mode == "active":
        return checks.finish("Metrics Server, VPA (no updater, scoped) and KEDA (scoped to the scaling namespace) are installed at their pinned versions and serving")
    return checks.finish("Metrics Server and VPA healthy after cleanup; KEDA fully uninstalled (release, objects, CRDs, Pods, bindings all gone)")


def main() -> int:
    usage = "usage: day8_addons.py static | keda-preinstall | check active|after-cleanup"
    if sys.argv[1:] == ["static"]:
        command = static
    elif sys.argv[1:] == ["keda-preinstall"]:
        command = keda_preinstall
    elif len(sys.argv) == 3 and sys.argv[1] == "check" and sys.argv[2] in ("active", "after-cleanup"):
        command = lambda: check(sys.argv[2])  # noqa: E731
    else:
        print(usage, file=sys.stderr)
        return 2
    try:
        return command()
    except (common.Day8Error, subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

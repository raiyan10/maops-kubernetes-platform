"""
Docker-free unit tests for scripts/validate_helm_chart.py - the Day 6
static validation logic for the Helm-rendered application chart
(charts/maops-kubernetes-platform).

Fixtures are constructed directly as Python dict/list structures (the
same pattern tests/test_validate_manifests.py uses for k8s/base),
built to match exactly what `helm template` renders for this chart's
30-object inventory. Each negative case mutates a single known-good
field of a deep copy of the baseline fixture and asserts that the
relevant check fails.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_helm_chart import run_checks

VERSION = "0.6.0"
INSTANCE = "maops-kubernetes-platform-day6"
NAMESPACE = "maops-platform"
VALIDATION_NAMESPACE = "maops-day6-validation"
INGRESS_NAMESPACE = "maops-ingress"
GATEWAY_NAME = "maops-edge"
# DAY6 remediation: the Gateway object's own name ("maops-edge") is
# distinct from Istio's deterministically-generated proxy ServiceAccount
# name ("<gateway-name>-istio") - never conflate the two.
GATEWAY_PROXY_SERVICE_ACCOUNT = "maops-edge-istio"


def _labels(component: str | None) -> dict:
    labels = {
        "app.kubernetes.io/name": "maops-kubernetes-platform",
        "app.kubernetes.io/instance": INSTANCE,
        "app.kubernetes.io/version": VERSION,
        "app.kubernetes.io/part-of": "maops-kubernetes-platform",
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": f"maops-kubernetes-platform-{VERSION}",
    }
    if component:
        labels["app.kubernetes.io/component"] = component
    return labels


def _selector(component: str) -> dict:
    return {
        "app.kubernetes.io/name": "maops-kubernetes-platform",
        "app.kubernetes.io/instance": INSTANCE,
        "app.kubernetes.io/component": component,
    }


def _security_context() -> dict:
    # DAY6 (live rollout remediation): fsGroup/fsGroupChangePolicy are
    # what actually make a projected 0440 Secret volume group-readable
    # by this non-root process - runAsGroup alone does not.
    return {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
        "fsGroup": 10001,
        "fsGroupChangePolicy": "OnRootMismatch",
    }


def _container_security_context() -> dict:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


_SECRET_VOLUME_NAMES = {"maops-internal-auth": "internal-auth", "maops-state-auth": "state-auth"}


def _secret_volume(secret_name: str) -> dict:
    return {"name": _SECRET_VOLUME_NAMES[secret_name], "secret": {"secretName": secret_name, "defaultMode": 288}}


def _config_checksum(name: str) -> str:
    """DAY6 (live-discovered Helm ConfigMap rollout remediation): a
    fixture-only stand-in for the real `sha256sum` Helm produces -
    distinct per workload NAME (never a shared constant), so the
    baseline fixture's three workloads never accidentally collide on
    the same checksum value the way a real "wrong ConfigMap reference"
    bug would."""
    import hashlib

    return hashlib.sha256(name.encode()).hexdigest()


def _workload(kind: str, name: str, component: str, image_repo: str, secret_names: tuple[str, ...] = ()) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(component)},
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"checksum/config": _config_checksum(name)},
                    "labels": _labels(component),
                },
                "spec": {
                    "serviceAccountName": name,
                    "automountServiceAccountToken": False,
                    "securityContext": _security_context(),
                    "containers": [
                        {
                            "name": name,
                            "image": f"{image_repo}:{VERSION}",
                            "securityContext": _container_security_context(),
                        }
                    ],
                    "volumes": [_secret_volume(secret_name) for secret_name in secret_names],
                },
            }
        },
    }


def _service_account(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(None)},
        "automountServiceAccountToken": False,
    }


def _configmap(name: str, component: str) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(component)}, "data": {}}


def _service(name: str, component: str) -> dict:
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(component)}, "spec": {}}


def _pdb(name: str, component: str) -> dict:
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(component)}, "spec": {"minAvailable": 2, "selector": {"matchLabels": _selector(component)}}}


def _role() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "maops-diagnostics-reader", "namespace": NAMESPACE, "labels": _labels("diagnostics")},
        "rules": [
            {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list", "watch"]},
        ],
    }


def _rolebinding() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "maops-diagnostics-reader-binding", "namespace": NAMESPACE, "labels": _labels("diagnostics")},
        "subjects": [{"kind": "ServiceAccount", "name": "maops-diagnostics", "namespace": VALIDATION_NAMESPACE}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "maops-diagnostics-reader"},
    }


def _netpol(name: str, component: str | None, spec: dict) -> dict:
    labels = _labels(component) if component else _labels(None)
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels}, "spec": spec}


def _network_policies() -> list[dict]:
    return [
        _netpol("maops-default-deny-all", None, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        _netpol("maops-allow-dns-egress", None, {"podSelector": {}, "policyTypes": ["Egress"], "egress": []}),
        _netpol(
            "maops-allow-gateway-egress-to-app",
            "gateway",
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}, "policyTypes": ["Egress"], "egress": [{"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]},
        ),
        _netpol(
            "maops-allow-app-ingress-from-gateway",
            "app",
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]},
        ),
        _netpol(
            "maops-allow-app-egress-to-state",
            "app",
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}, "policyTypes": ["Egress"], "egress": [{"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]},
        ),
        _netpol(
            "maops-allow-state-ingress-from-app",
            "state",
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}, "policyTypes": ["Ingress"], "ingress": [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}}], "ports": [{"protocol": "TCP", "port": 8080}]}]},
        ),
        _netpol(
            "maops-allow-gateway-ingress-from-istio-ingress-gateway",
            "gateway",
            {
                "podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": INGRESS_NAMESPACE}},
                                "podSelector": {"matchLabels": {"istio.io/gateway-name": GATEWAY_NAME}},
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8080}],
                    }
                ],
            },
        ),
        _netpol(
            "maops-allow-hbone-ztunnel",
            None,
            {
                # DAY6 remediation: deliberately peer-less (no
                # from/to selector) - see the real template's own
                # header comment for why a namespaceSelector-scoped
                # peer against ztunnel (a hostNetwork DaemonSet) is
                # not a reliable match.
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [{"ports": [{"protocol": "TCP", "port": 15008}]}],
                "egress": [{"ports": [{"protocol": "TCP", "port": 15008}]}],
            },
        ),
    ]


def _peer_authentication() -> dict:
    return {
        "apiVersion": "security.istio.io/v1",
        "kind": "PeerAuthentication",
        "metadata": {"name": "maops-platform-strict-mtls", "namespace": NAMESPACE, "labels": _labels(None)},
        "spec": {"mtls": {"mode": "STRICT"}},
    }


def _authz(name: str, component: str, principal: str) -> dict:
    return {
        "apiVersion": "security.istio.io/v1",
        "kind": "AuthorizationPolicy",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": _labels(component)},
        "spec": {
            "selector": {"matchLabels": _selector(component)},
            "action": "ALLOW",
            "rules": [{"from": [{"source": {"principals": [principal]}}]}],
        },
    }


def _httproute() -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "maops-gateway-route", "namespace": NAMESPACE, "labels": _labels("gateway")},
        "spec": {
            "parentRefs": [{"name": GATEWAY_NAME, "namespace": INGRESS_NAMESPACE}],
            "hostnames": ["maops.local"],
            "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}], "backendRefs": [{"name": "maops-gateway", "port": 8080}]}],
        },
    }


def _baseline_docs() -> list[dict]:
    docs = [
        _configmap("maops-gateway-config", "gateway"),
        _configmap("maops-app-config", "app"),
        _configmap("maops-state-config", "state"),
        _service_account("maops-gateway"),
        _service_account("maops-app"),
        _service_account("maops-state"),
        _role(),
        _rolebinding(),
        _workload("Deployment", "maops-gateway", "gateway", "maops-kubernetes-gateway", secret_names=("maops-internal-auth",)),
        _workload("Deployment", "maops-app", "app", "maops-kubernetes-app", secret_names=("maops-internal-auth", "maops-state-auth")),
        _workload("StatefulSet", "maops-state", "state", "maops-kubernetes-state", secret_names=("maops-state-auth",)),
        _service("maops-gateway", "gateway"),
        _service("maops-app", "app"),
        _service("maops-state", "state"),
        _service("maops-state-headless", "state"),
        _pdb("maops-gateway-pdb", "gateway"),
        _pdb("maops-app-pdb", "app"),
        *_network_policies(),
        _peer_authentication(),
        _authz("maops-gateway-authz", "gateway", f"cluster.local/ns/{INGRESS_NAMESPACE}/sa/{GATEWAY_PROXY_SERVICE_ACCOUNT}"),
        _authz("maops-app-authz", "app", f"cluster.local/ns/{NAMESPACE}/sa/maops-gateway"),
        _authz("maops-state-authz", "state", f"cluster.local/ns/{NAMESPACE}/sa/maops-app"),
        _httproute(),
    ]
    assert len(docs) == 30, len(docs)
    return docs


def _find(docs, kind, name):
    return next(d for d in docs if d.get("kind") == kind and d.get("metadata", {}).get("name") == name)


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


class BaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_checks(_baseline_docs())
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreater(len(findings), 30)


class InventoryTests(unittest.TestCase):
    def test_wrong_object_count_fails(self):
        docs = _baseline_docs()[:-1]
        failed = _failed_names(run_checks(docs))
        self.assertIn("inventory.total_object_count", failed)

    def test_secret_object_fails(self):
        docs = _baseline_docs()
        docs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "maops-internal-auth", "namespace": NAMESPACE}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("inventory.no_secret", failed)
        self.assertIn("inventory.total_object_count", failed)

    def test_ingress_object_fails(self):
        docs = _baseline_docs()
        docs.append({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "maops-ingress", "namespace": NAMESPACE}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("inventory.no_ingress", failed)

    def test_waypoint_gateway_object_fails(self):
        docs = _baseline_docs()
        docs.append({"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway", "metadata": {"name": "maops-waypoint", "namespace": NAMESPACE}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("inventory.no_waypoint", failed)


class VersionTests(unittest.TestCase):
    def test_wrong_image_tag_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-gateway:0.5.0"
        failed = _failed_names(run_checks(docs))
        self.assertIn("version.maops-gateway.image_tag", failed)

    def test_wrong_version_label_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "ServiceAccount", "maops-app")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.5.0"
        failed = _failed_names(run_checks(docs))
        self.assertTrue(any(n.startswith("version.label_matches[ServiceAccount/maops-app]") for n in failed))

    def test_wrong_instance_label_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "ServiceAccount", "maops-app")["metadata"]["labels"]["app.kubernetes.io/instance"] = "maops-kubernetes-platform-day5"
        failed = _failed_names(run_checks(docs))
        self.assertTrue(any(n.startswith("version.instance_matches[ServiceAccount/maops-app]") for n in failed))


class SecurityContextTests(unittest.TestCase):
    def test_root_user_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-gateway.runAsUser", failed)

    def test_writable_root_filesystem_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["template"]["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-state.readOnlyRootFilesystem", failed)

    def test_missing_capability_drop_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-app.capabilities_drop_all", failed)

    def test_automount_true_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-gateway.automount_false", failed)


class FsGroupSecretProjectionTests(unittest.TestCase):
    """DAY6 (live rollout remediation): a live rollout showed maops-app
    getting `PermissionError [Errno 13]` reading its projected
    `state-token` Secret file - `runAsGroup` alone does not make a
    projected Secret volume's files group-readable by the running
    process; `fsGroup` does. These tests prove the static checker
    actually catches a regression of any of: missing/wrong fsGroup,
    missing/wrong fsGroupChangePolicy, a Secret volume mode drifting
    away from 0440, or a runtime-Secret-mounting workload losing its
    Secret volume entirely - for every one of gateway/app/state."""

    def test_baseline_every_workload_has_fs_group_10001(self):
        """Positive control: state already carried this contract before
        this remediation and must be unaffected; gateway/app now carry
        the identical, equivalent contract."""
        docs = _baseline_docs()
        findings = run_checks(docs)
        for name in ("maops-gateway", "maops-app", "maops-state"):
            with self.subTest(name=name):
                fs_group_finding = next(f for f in findings if f.name == f"security.{name}.fsGroup")
                self.assertTrue(fs_group_finding.ok, fs_group_finding.detail)
                policy_finding = next(f for f in findings if f.name == f"security.{name}.fsGroupChangePolicy")
                self.assertTrue(policy_finding.ok, policy_finding.detail)

    def test_missing_fs_group_fails_for_app(self):
        docs = copy.deepcopy(_baseline_docs())
        del _find(docs, "Deployment", "maops-app")["spec"]["template"]["spec"]["securityContext"]["fsGroup"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-app.fsGroup", failed)

    def test_wrong_fs_group_fails_for_gateway(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["spec"]["securityContext"]["fsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-gateway.fsGroup", failed)

    def test_missing_fs_group_change_policy_fails_for_state(self):
        """Regression guard specifically for the workload that already
        had this contract pre-remediation - it must stay intact, not
        merely be assumed unaffected."""
        docs = copy.deepcopy(_baseline_docs())
        del _find(docs, "StatefulSet", "maops-state")["spec"]["template"]["spec"]["securityContext"]["fsGroupChangePolicy"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-state.fsGroupChangePolicy", failed)

    def test_wrong_fs_group_change_policy_fails_for_app(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["spec"]["securityContext"]["fsGroupChangePolicy"] = "Always"
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-app.fsGroupChangePolicy", failed)

    def test_secret_volume_mode_drifting_away_from_0440_fails(self):
        """This remediation must never change Secret file mode - only
        add fsGroup. A drift here would mean a future edit accidentally
        weakened (or broke) the Secret projection's own permissions."""
        docs = copy.deepcopy(_baseline_docs())
        app = _find(docs, "Deployment", "maops-app")
        for volume in app["spec"]["template"]["spec"]["volumes"]:
            if volume["secret"]["secretName"] == "maops-state-auth":
                volume["secret"]["defaultMode"] = 420  # 0644 - group+other readable, a real weakening
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-app.secret_volume[maops-state-auth].defaultMode", failed)

    def test_workload_missing_its_runtime_secret_volume_entirely_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["template"]["spec"]["volumes"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.maops-state.mounts_a_runtime_secret", failed)

    def test_app_secret_volumes_cover_both_internal_and_state_auth(self):
        """app is the one workload that mounts BOTH Secrets - both must
        independently be checked at mode 0440, not just whichever one a
        looser check happened to find first."""
        docs = _baseline_docs()
        findings = {f.name: f for f in run_checks(docs)}
        self.assertIn("security.maops-app.secret_volume[maops-internal-auth].defaultMode", findings)
        self.assertIn("security.maops-app.secret_volume[maops-state-auth].defaultMode", findings)
        self.assertTrue(findings["security.maops-app.secret_volume[maops-internal-auth].defaultMode"].ok)
        self.assertTrue(findings["security.maops-app.secret_volume[maops-state-auth].defaultMode"].ok)

    def test_container_uid_gid_remain_10001_unaffected_by_fs_group(self):
        """The fsGroup addition must never be mistaken for, or drift,
        the container's own runAsUser/runAsGroup - both remain checked
        and must both still be 10001."""
        docs = _baseline_docs()
        findings = {f.name: f for f in run_checks(docs)}
        for name in ("maops-gateway", "maops-app", "maops-state"):
            with self.subTest(name=name):
                self.assertTrue(findings[f"security.{name}.runAsUser"].ok)
                self.assertTrue(findings[f"security.{name}.runAsGroup"].ok)


class ConfigChecksumTests(unittest.TestCase):
    """DAY6 (live-discovered Helm ConfigMap rollout remediation): a live
    `helm-lifecycle-check` run found that a ConfigMap-only change never
    triggered a new rollout, because nothing in the Pod template itself
    changed. `_check_config_checksum()` must reject: a missing checksum
    annotation, an empty checksum, a malformed/non-sha256-shaped
    checksum, a workload whose checksum collides with another
    component's (the wrong-ConfigMap-reference signature), and a
    checksum placed on the workload's own top-level metadata instead of
    the Pod template."""

    def test_baseline_every_workload_has_a_present_well_formed_checksum(self):
        docs = _baseline_docs()
        findings = {f.name: f for f in run_checks(docs)}
        for name in ("maops-gateway", "maops-app", "maops-state"):
            with self.subTest(name=name):
                self.assertTrue(findings[f"checksum.{name}.present_on_pod_template"].ok)
                self.assertTrue(findings[f"checksum.{name}.non_empty"].ok)
                self.assertTrue(findings[f"checksum.{name}.well_formed_sha256"].ok)

    def test_missing_checksum_annotation_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        del _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-gateway.present_on_pod_template", failed)

    def test_missing_annotations_block_entirely_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        del _find(docs, "StatefulSet", "maops-state")["spec"]["template"]["metadata"]["annotations"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-state.present_on_pod_template", failed)

    def test_empty_checksum_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = ""
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-app.non_empty", failed)

    def test_whitespace_only_checksum_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = "   "
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-app.non_empty", failed)

    def test_malformed_checksum_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = "not-a-real-checksum"
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-gateway.well_formed_sha256", failed)

    def test_uppercase_hex_checksum_fails(self):
        """Helm's `sha256sum` always produces lowercase hex - an
        uppercase digest is not the genuine output shape and must be
        rejected, not silently case-normalized."""
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = _config_checksum("maops-state").upper()
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-state.well_formed_sha256", failed)

    def test_wrong_length_checksum_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = "abc123"
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-app.well_formed_sha256", failed)

    def test_workload_referencing_the_wrong_components_checksum_fails(self):
        """The exact wrong-ConfigMap-reference signature: two workloads
        carrying the SAME checksum value means one of them is hashing
        the wrong component's ConfigMap template."""
        docs = copy.deepcopy(_baseline_docs())
        shared = _config_checksum("maops-gateway")
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = shared
        _find(docs, "Deployment", "maops-app")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = shared
        failed = _failed_names(run_checks(docs))
        self.assertTrue(any(n.startswith("checksum.maops-gateway_vs_maops-app.distinct") for n in failed))

    def test_checksum_on_workload_top_level_metadata_instead_of_pod_template_fails(self):
        """A checksum annotation on the Deployment/StatefulSet's OWN
        metadata (rather than spec.template.metadata) never touches the
        Pod template, so it would not actually fix the underlying
        rollout defect - this must be rejected exactly like a missing
        checksum, since from the Pod-template's own perspective it IS
        missing."""
        docs = copy.deepcopy(_baseline_docs())
        gateway = _find(docs, "Deployment", "maops-gateway")
        checksum = gateway["spec"]["template"]["metadata"]["annotations"].pop("checksum/config")
        gateway["metadata"].setdefault("annotations", {})["checksum/config"] = checksum
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-gateway.present_on_pod_template", failed)

    def test_non_string_checksum_value_fails_without_raising(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = {"unexpected": "dict"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("checksum.maops-gateway.present_on_pod_template", failed)


class RenderedChartChecksumTests(unittest.TestCase):
    """DAY6 (live-discovered Helm ConfigMap rollout remediation):
    render-level tests against the REAL chart (via `helm template`,
    never a live cluster) proving the checksum annotations are
    genuinely reproducible and genuinely scoped per-workload - a single
    parsed-fixture check cannot observe cross-render reproducibility or
    a real accidental-cross-reference bug in the actual template files,
    only the constructed test fixtures above."""

    _CHART_DIR = Path(__file__).resolve().parent.parent / "charts" / "maops-kubernetes-platform"

    def _render(self, *extra_args: str) -> list[dict]:
        import subprocess

        # The project's own dependency-free YAML-subset loader - the same
        # one scripts/helm_check.py uses on `helm template` output - never
        # a third-party YAML package.
        import k8s_yaml

        cmd = [
            "helm", "template", "maops-kubernetes-platform-day6", str(self._CHART_DIR),
            "--namespace", NAMESPACE,
            *extra_args,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            self.skipTest(f"helm template failed (helm not available or chart error): {result.stderr}")
        return [d for d in k8s_yaml.load_all(result.stdout) if d]

    def _checksum(self, docs: list[dict], kind: str, name: str) -> str:
        for d in docs:
            if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
                return d["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        raise AssertionError(f"{kind}/{name} not found in rendered docs")

    def test_identical_values_produce_identical_checksums_across_renders(self):
        docs_a = self._render()
        docs_b = self._render()
        for kind, name in (("Deployment", "maops-gateway"), ("Deployment", "maops-app"), ("StatefulSet", "maops-state")):
            with self.subTest(name=name):
                self.assertEqual(self._checksum(docs_a, kind, name), self._checksum(docs_b, kind, name))

    def test_overriding_only_gateway_config_changes_only_the_gateway_checksum(self):
        baseline = self._render()
        changed = self._render("--set", "gateway.config.appMessage=overridden-for-test")
        self.assertNotEqual(
            self._checksum(baseline, "Deployment", "maops-gateway"),
            self._checksum(changed, "Deployment", "maops-gateway"),
        )
        self.assertEqual(self._checksum(baseline, "Deployment", "maops-app"), self._checksum(changed, "Deployment", "maops-app"))
        self.assertEqual(self._checksum(baseline, "StatefulSet", "maops-state"), self._checksum(changed, "StatefulSet", "maops-state"))

    def test_overriding_only_app_config_changes_only_the_app_checksum(self):
        baseline = self._render()
        changed = self._render("--set", "app.config.appMessage=overridden-for-test")
        self.assertNotEqual(
            self._checksum(baseline, "Deployment", "maops-app"),
            self._checksum(changed, "Deployment", "maops-app"),
        )
        self.assertEqual(self._checksum(baseline, "Deployment", "maops-gateway"), self._checksum(changed, "Deployment", "maops-gateway"))
        self.assertEqual(self._checksum(baseline, "StatefulSet", "maops-state"), self._checksum(changed, "StatefulSet", "maops-state"))

    def test_overriding_only_state_config_changes_only_the_state_checksum(self):
        baseline = self._render()
        changed = self._render("--set", "state.config.appMessage=overridden-for-test")
        self.assertNotEqual(
            self._checksum(baseline, "StatefulSet", "maops-state"),
            self._checksum(changed, "StatefulSet", "maops-state"),
        )
        self.assertEqual(self._checksum(baseline, "Deployment", "maops-gateway"), self._checksum(changed, "Deployment", "maops-gateway"))
        self.assertEqual(self._checksum(baseline, "Deployment", "maops-app"), self._checksum(changed, "Deployment", "maops-app"))

    def test_rendered_checksums_pass_the_static_validator(self):
        docs = self._render()
        findings = run_checks(docs)
        failed = [f for f in findings if not f.ok and f.name.startswith("checksum.")]
        self.assertEqual(failed, [])


class RbacTests(unittest.TestCase):
    def test_forbidden_resource_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Role", "maops-diagnostics-reader")["rules"][0]["resources"].append("secrets")
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.no_forbidden_resources", failed)

    def test_forbidden_verb_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "Role", "maops-diagnostics-reader")["rules"][0]["verbs"].append("delete")
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.no_forbidden_verbs", failed)

    def test_application_service_account_bound_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "RoleBinding", "maops-diagnostics-reader-binding")["subjects"].append(
            {"kind": "ServiceAccount", "name": "maops-gateway", "namespace": NAMESPACE}
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.application_service_accounts_not_bound", failed)
        self.assertIn("rbac.rolebinding.subject_is_diagnostics_only", failed)

    def test_extra_role_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        docs.append(_role())
        docs[-1]["metadata"]["name"] = "maops-second-role"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.exactly_one_role", failed)


class NetworkPolicyTests(unittest.TestCase):
    def test_day5_validation_shortcut_present_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        docs.append(
            _netpol(
                "maops-allow-gateway-ingress-from-validation",
                "gateway",
                {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}, "policyTypes": ["Ingress"], "ingress": []},
            )
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.no_day5_validation_shortcut", failed)
        self.assertIn("networkpolicy.topology_exact", failed)

    def test_gateway_egress_targets_state_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        gw_egress = _find(docs, "NetworkPolicy", "maops-allow-gateway-egress-to-app")
        gw_egress["spec"]["egress"][0]["to"].append({"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_egress_app.never_targets_state", failed)

    def test_state_ingress_allows_gateway_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        state_ingress = _find(docs, "NetworkPolicy", "maops-allow-state-ingress-from-app")
        state_ingress["spec"]["ingress"][0]["from"].append({"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.state_ingress_app.never_allows_gateway", failed)

    def test_hbone_wrong_port_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        hbone = _find(docs, "NetworkPolicy", "maops-allow-hbone-ztunnel")
        hbone["spec"]["ingress"][0]["ports"][0]["port"] = 8080
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.hbone.ingress_port_exact", failed)

    def test_hbone_reintroducing_namespace_peer_fails(self):
        """DAY6 remediation regression guard: a from/to peer must never
        reappear on the HBONE rule - ztunnel's hostNetwork identity
        cannot be reliably matched by namespaceSelector/podSelector, so
        a peer restriction here is a false precision claim, not a
        tightening."""
        docs = copy.deepcopy(_baseline_docs())
        hbone = _find(docs, "NetworkPolicy", "maops-allow-hbone-ztunnel")
        hbone["spec"]["ingress"][0]["from"] = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "istio-system"}}}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.hbone.ingress_is_peerless", failed)

    def test_hbone_egress_reintroducing_namespace_peer_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        hbone = _find(docs, "NetworkPolicy", "maops-allow-hbone-ztunnel")
        hbone["spec"]["egress"][0]["to"] = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "istio-system"}}}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.hbone.egress_is_peerless", failed)


class MeshPolicyTests(unittest.TestCase):
    def test_permissive_mtls_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "PeerAuthentication", "maops-platform-strict-mtls")["spec"]["mtls"]["mode"] = "PERMISSIVE"
        failed = _failed_names(run_checks(docs))
        self.assertIn("mesh.peerauthentication.strict", failed)

    def test_wrong_gateway_principal_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "AuthorizationPolicy", "maops-gateway-authz")["spec"]["rules"][0]["from"][0]["source"]["principals"] = [
            "cluster.local/ns/maops-day6-validation/sa/maops-diagnostics"
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("mesh.authz.gateway.principal_exact", failed)

    def test_gateway_allowed_to_state_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "AuthorizationPolicy", "maops-state-authz")["spec"]["rules"][0]["from"][0]["source"]["principals"].append(
            f"cluster.local/ns/{NAMESPACE}/sa/maops-gateway"
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("mesh.authz.state.principal_exact", failed)
        self.assertIn("mesh.authz.gateway_never_allowed_to_state", failed)

    def test_diagnostics_allowed_as_principal_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "AuthorizationPolicy", "maops-gateway-authz")["spec"]["rules"][0]["from"][0]["source"]["principals"].append(
            f"cluster.local/ns/{VALIDATION_NAMESPACE}/sa/maops-diagnostics"
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("mesh.authz.diagnostics_never_a_principal", failed)


class GatewayApiTests(unittest.TestCase):
    def test_wrong_hostname_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "HTTPRoute", "maops-gateway-route")["spec"]["hostnames"] = ["wrong.example"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway_api.httproute.hostname_exact", failed)

    def test_wrong_parent_namespace_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "HTTPRoute", "maops-gateway-route")["spec"]["parentRefs"][0]["namespace"] = "maops-platform"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway_api.httproute.parent_namespace", failed)

    def test_wrong_backend_port_fails(self):
        docs = copy.deepcopy(_baseline_docs())
        _find(docs, "HTTPRoute", "maops-gateway-route")["spec"]["rules"][0]["backendRefs"][0]["port"] = 9999
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway_api.httproute.backend_ref_exact", failed)


if __name__ == "__main__":
    unittest.main()

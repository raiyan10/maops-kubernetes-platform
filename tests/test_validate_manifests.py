"""
Docker-free unit tests for the repository-owned static validation logic
in scripts/validate_manifests.py (Day 5: adds ServiceAccounts, a
namespace-scoped Role/RoleBinding for the maops-diagnostics identity,
and standard networking.k8s.io/v1 NetworkPolicy default-deny +
narrow-allow objects, alongside the unchanged Day 4 gateway/app/state
architecture).

Fixtures are constructed directly as Python dict/list structures (not
parsed from YAML text) so these tests exercise validation rules only,
independent of the YAML-subset parser. Each negative case mutates a
single known-good field of a deep copy of the baseline fixture and
asserts that exactly the relevant check fails while the fixture is
otherwise valid.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_manifests import run_checks

VERSION = "0.5.0"
INSTANCE = "maops-kubernetes-platform-day5"
VALIDATION_NAMESPACE = "maops-day5-validation"


def _labels(component: str | None) -> dict:
    labels = {
        "app.kubernetes.io/name": "maops-kubernetes-platform",
        "app.kubernetes.io/instance": INSTANCE,
        "app.kubernetes.io/version": VERSION,
        "app.kubernetes.io/part-of": "maops-kubernetes-platform",
        "app.kubernetes.io/managed-by": "kustomize",
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
    return {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _node_affinity() -> dict:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}
                        ]
                    }
                ]
            }
        }
    }


def _topology_spread(component: str) -> list[dict]:
    return [
        {
            "maxSkew": 1,
            "topologyKey": "kubernetes.io/hostname",
            "whenUnsatisfiable": "DoNotSchedule",
            "nodeAffinityPolicy": "Honor",
            "nodeTaintsPolicy": "Honor",
            "labelSelector": {"matchLabels": _selector(component)},
        }
    ]


def _container(name: str, image: str, configmap: str, readiness_timeout_seconds: int = 5) -> dict:
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
        "envFrom": [{"configMapRef": {"name": configmap}}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "32Mi"},
            "limits": {"cpu": "250m", "memory": "128Mi"},
        },
        "volumeMounts": [{"name": "internal-auth", "mountPath": "/var/run/secrets/maops", "readOnly": True}],
        "startupProbe": {"httpGet": {"path": "/livez", "port": "http"}},
        "livenessProbe": {"httpGet": {"path": "/livez", "port": "http"}},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}, "timeoutSeconds": readiness_timeout_seconds},
    }


def _deployment(name: str, component: str, image: str, configmap: str) -> dict:
    pod_labels = _labels(component)
    # DAY4-ARCH-M1 (batch 2): gateway's readinessProbe stays 2s above its
    # own BACKEND_TIMEOUT_SECONDS (7 > 5); app's stays 2s above
    # STATE_TIMEOUT_SECONDS (5 > 3) - see docs/architecture.md's
    # "Timeout hierarchy" section.
    readiness_timeout_seconds = 7 if component == "gateway" else 5
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "maops-platform", "labels": _labels(component)},
        "spec": {
            "replicas": 3,
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 1}},
            "minReadySeconds": 5,
            "progressDeadlineSeconds": 120,
            "revisionHistoryLimit": 5,
            "selector": {"matchLabels": _selector(component)},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "serviceAccountName": name,
                    "automountServiceAccountToken": False,
                    "securityContext": _security_context(),
                    "affinity": _node_affinity(),
                    "topologySpreadConstraints": _topology_spread(component),
                    "containers": [_container(name, image, configmap, readiness_timeout_seconds)],
                    "volumes": [
                        {
                            "name": "internal-auth",
                            "secret": {"secretName": "maops-internal-auth", "defaultMode": 288},
                        }
                    ],
                },
            },
        },
    }


def _service(name: str, component: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "maops-platform", "labels": _labels(component)},
        "spec": {
            "type": "ClusterIP",
            "selector": _selector(component),
            "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
        },
    }


def _pdb(name: str, component: str) -> dict:
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": "maops-platform", "labels": _labels(component)},
        "spec": {"minAvailable": 2, "selector": {"matchLabels": _selector(component)}},
    }


def _state_container() -> dict:
    return {
        "name": "maops-state",
        "image": f"maops-kubernetes-state:{VERSION}",
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
        "envFrom": [{"configMapRef": {"name": "maops-state-config"}}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "32Mi"},
            "limits": {"cpu": "250m", "memory": "128Mi"},
        },
        "volumeMounts": [
            {"name": "state-auth", "mountPath": "/var/run/secrets/maops-state", "readOnly": True},
            {"name": "data", "mountPath": "/data"},
        ],
        "startupProbe": {"httpGet": {"path": "/livez", "port": "http"}},
        "livenessProbe": {"httpGet": {"path": "/livez", "port": "http"}},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
    }


def _state_statefulset() -> dict:
    pod_labels = _labels("state")
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "maops-state", "namespace": "maops-platform", "labels": _labels("state")},
        "spec": {
            "replicas": 1,
            "serviceName": "maops-state-headless",
            "selector": {"matchLabels": _selector("state")},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "serviceAccountName": "maops-state",
                    "automountServiceAccountToken": False,
                    "securityContext": _security_context(),
                    "affinity": _node_affinity(),
                    "containers": [_state_container()],
                    "volumes": [
                        {
                            "name": "state-auth",
                            "secret": {"secretName": "maops-state-auth", "defaultMode": 288},
                        }
                    ],
                },
            },
            "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Retain", "whenScaled": "Retain"},
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data", "labels": _labels("state")},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "volumeMode": "Filesystem",
                        "resources": {"requests": {"storage": "256Mi"}},
                    },
                }
            ],
        },
    }


def _service_account(name: str, namespace: str, component: str, automount: bool) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace, "labels": _labels(component)},
        "automountServiceAccountToken": automount,
    }


def _diagnostics_role() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "maops-diagnostics-reader", "namespace": "maops-platform", "labels": _labels("diagnostics")},
        "rules": [
            {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list", "watch"]},
        ],
    }


def _diagnostics_role_binding() -> dict:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "maops-diagnostics-reader-binding",
            "namespace": "maops-platform",
            "labels": _labels("diagnostics"),
        },
        "subjects": [{"kind": "ServiceAccount", "name": "maops-diagnostics", "namespace": VALIDATION_NAMESPACE}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "maops-diagnostics-reader"},
    }


def _netpol_default_deny() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "maops-default-deny-all", "namespace": "maops-platform", "labels": _labels(None)},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }


def _netpol_allow_dns() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "maops-allow-dns-egress", "namespace": "maops-platform", "labels": _labels(None)},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                }
            ],
        },
    }


def _netpol_gateway_egress_app() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "maops-allow-gateway-egress-to-app",
            "namespace": "maops-platform",
            "labels": _labels("gateway"),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def _netpol_app_ingress_gateway() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "maops-allow-app-ingress-from-gateway",
            "namespace": "maops-platform",
            "labels": _labels("app"),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def _netpol_app_egress_state() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "maops-allow-app-egress-to-state",
            "namespace": "maops-platform",
            "labels": _labels("app"),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def _netpol_state_ingress_app() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "maops-allow-state-ingress-from-app",
            "namespace": "maops-platform",
            "labels": _labels("state"),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def _netpol_gateway_ingress_validation() -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "maops-allow-gateway-ingress-from-validation",
            "namespace": "maops-platform",
            "labels": _labels("gateway"),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": VALIDATION_NAMESPACE}},
                            "podSelector": {"matchLabels": {"app.kubernetes.io/component": "validation-client"}},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def _base_docs() -> list[dict]:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "maops-platform", "labels": _labels(None)},
    }
    validation_namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": VALIDATION_NAMESPACE, "labels": _labels("validation")},
    }
    gateway_configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-gateway-config", "namespace": "maops-platform", "labels": _labels("gateway")},
        "data": {
            "BACKEND_HOST": "maops-app",
            "BACKEND_PORT": "8080",
            "BACKEND_TIMEOUT_SECONDS": "5",
            "APP_NAME": "maops-kubernetes-gateway",
            "APP_ENVIRONMENT": "day3-scaling-rollouts-availability",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Gateway (Day 3)",
            "APP_LOG_LEVEL": "info",
        },
    }
    app_configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-app-config", "namespace": "maops-platform", "labels": _labels("app")},
        "data": {
            "APP_NAME": "maops-kubernetes-app",
            "APP_ENVIRONMENT": "day3-scaling-rollouts-availability",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes App (Day 3)",
            "APP_LOG_LEVEL": "info",
            "STATE_HOST": "maops-state",
            "STATE_PORT": "8080",
            "STATE_TIMEOUT_SECONDS": "3",
        },
    }
    gateway_deployment = _deployment("maops-gateway", "gateway", f"maops-kubernetes-gateway:{VERSION}", "maops-gateway-config")
    app_deployment = _deployment("maops-app", "app", f"maops-kubernetes-app:{VERSION}", "maops-app-config")
    gateway_service = _service("maops-gateway", "gateway")
    app_service = _service("maops-app", "app")
    gateway_pdb = _pdb("maops-gateway-pdb", "gateway")
    app_pdb = _pdb("maops-app-pdb", "app")

    state_configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-state-config", "namespace": "maops-platform", "labels": _labels("state")},
        "data": {
            "APP_NAME": "maops-kubernetes-state",
            "APP_ENVIRONMENT": "day4-stateful-persistence",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes State service (Day 4)",
            "APP_LOG_LEVEL": "info",
            "STATE_FILE_PATH": "/data/state.json",
            "STATE_MAX_BODY_BYTES": "4096",
        },
    }
    state_statefulset = _state_statefulset()
    state_service = _service("maops-state", "state")
    state_headless_service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "maops-state-headless", "namespace": "maops-platform", "labels": _labels("state")},
        "spec": {
            "clusterIP": "None",
            "selector": _selector("state"),
            "ports": [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}],
        },
    }

    gateway_sa = _service_account("maops-gateway", "maops-platform", "gateway", False)
    app_sa = _service_account("maops-app", "maops-platform", "app", False)
    state_sa = _service_account("maops-state", "maops-platform", "state", False)
    diagnostics_sa = _service_account("maops-diagnostics", VALIDATION_NAMESPACE, "diagnostics", True)
    diagnostics_role = _diagnostics_role()
    diagnostics_role_binding = _diagnostics_role_binding()

    netpols = [
        _netpol_default_deny(),
        _netpol_allow_dns(),
        _netpol_gateway_egress_app(),
        _netpol_app_ingress_gateway(),
        _netpol_app_egress_state(),
        _netpol_state_ingress_app(),
        _netpol_gateway_ingress_validation(),
    ]

    return [
        namespace,
        validation_namespace,
        gateway_configmap,
        app_configmap,
        state_configmap,
        gateway_sa,
        app_sa,
        state_sa,
        diagnostics_sa,
        diagnostics_role,
        diagnostics_role_binding,
        gateway_deployment,
        app_deployment,
        state_statefulset,
        gateway_service,
        app_service,
        state_service,
        state_headless_service,
        gateway_pdb,
        app_pdb,
        *netpols,
    ]


def _find(docs, kind, name=None):
    if name is None:
        return next(d for d in docs if d.get("kind") == kind)
    return next(d for d in docs if d.get("kind") == kind and d.get("metadata", {}).get("name") == name)


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


def _container_of(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]


def _pod_spec_of(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]


class BaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_checks(_base_docs())
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 260)


class ObjectCountTests(unittest.TestCase):
    def test_only_one_deployment_fails_count(self):
        docs = copy.deepcopy(_base_docs())
        docs[:] = [d for d in docs if not (d.get("kind") == "Deployment" and d["metadata"]["name"] == "maops-gateway")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.count", failed)
        self.assertIn("gateway.deployment.exists", failed)

    def test_only_one_service_fails_count(self):
        docs = copy.deepcopy(_base_docs())
        docs[:] = [d for d in docs if not (d.get("kind") == "Service" and d["metadata"]["name"] == "maops-app")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.count", failed)
        self.assertIn("app.service.exists", failed)


class ReplicaTests(unittest.TestCase):
    def test_gateway_replicas_changed_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment", "maops-gateway")["spec"]["replicas"] = 1
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.deployment.replicas", failed)
        self.assertNotIn("app.deployment.replicas", failed)

    def test_app_replicas_changed_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment", "maops-app")["spec"]["replicas"] = 4
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.replicas", failed)
        self.assertNotIn("gateway.deployment.replicas", failed)


class RolloutStrategyTests(unittest.TestCase):
    """DAY3: explicit RollingUpdate tuning must be pinned, not left to
    Kubernetes defaults."""

    def test_strategy_type_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["strategy"]["type"] = "Recreate"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.strategy_type", failed)

    def test_max_unavailable_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] = 2
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.max_unavailable", failed)

    def test_max_surge_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["strategy"]["rollingUpdate"]["maxSurge"] = 0
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.max_surge", failed)

    def test_min_ready_seconds_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["minReadySeconds"] = 0
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.min_ready_seconds", failed)

    def test_progress_deadline_seconds_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["progressDeadlineSeconds"] = 600
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.progress_deadline_seconds", failed)

    def test_revision_history_limit_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["spec"]["revisionHistoryLimit"] = 10
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.revision_history_limit", failed)


class SchedulingTests(unittest.TestCase):
    """DAY3: worker-only required node affinity + per-workload
    topologySpreadConstraints."""

    def test_node_affinity_removed_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                del _pod_spec_of(_find(docs, "Deployment", dep_name))["affinity"]
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.control_plane_excluded", failed)

    def test_node_affinity_changed_to_allow_control_plane_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                expr = _pod_spec_of(_find(docs, "Deployment", dep_name))["affinity"]["nodeAffinity"][
                    "requiredDuringSchedulingIgnoredDuringExecution"
                ]["nodeSelectorTerms"][0]["matchExpressions"][0]
                expr["operator"] = "Exists"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.control_plane_excluded", failed)

    def test_topology_spread_removed_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"] = []
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.topology_spread_present", failed)

    def test_max_skew_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0]["maxSkew"] = 2
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.max_skew", failed)

    def test_topology_key_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0][
                    "topologyKey"
                ] = "topology.kubernetes.io/zone"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.topology_key", failed)

    def test_when_unsatisfiable_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0][
                    "whenUnsatisfiable"
                ] = "ScheduleAnyway"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.when_unsatisfiable", failed)

    def test_node_affinity_policy_removed_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                del _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0]["nodeAffinityPolicy"]
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.node_affinity_policy", failed)

    def test_node_affinity_policy_weakened_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0][
                    "nodeAffinityPolicy"
                ] = "Ignore"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.node_affinity_policy", failed)

    def test_node_taints_policy_removed_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                del _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0]["nodeTaintsPolicy"]
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.node_taints_policy", failed)

    def test_node_taints_policy_weakened_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0][
                    "nodeTaintsPolicy"
                ] = "Ignore"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.node_taints_policy", failed)

    def test_topology_selector_cross_wired_fails_both_workloads(self):
        other = {"gateway": "app", "app": "gateway"}
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _pod_spec_of(_find(docs, "Deployment", dep_name))["topologySpreadConstraints"][0]["labelSelector"][
                    "matchLabels"
                ] = _selector(other[component])
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.scheduling.topology_selector_scoped_to_own_component", failed)


class PdbTests(unittest.TestCase):
    """DAY3: PodDisruptionBudget presence/shape/selector isolation."""

    def test_gateway_pdb_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "PodDisruptionBudget" and d["metadata"]["name"] == "maops-gateway-pdb")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.pdb.named_correctly", failed)
        self.assertIn("gateway.pdb.exists", failed)
        self.assertIn("pdb.count", failed)

    def test_app_pdb_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "PodDisruptionBudget" and d["metadata"]["name"] == "maops-app-pdb")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.pdb.named_correctly", failed)
        self.assertIn("app.pdb.exists", failed)
        self.assertIn("pdb.count", failed)

    def test_pdb_wrong_api_version_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "PodDisruptionBudget", pdb_names[component])["apiVersion"] = "policy/v1beta1"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.api_version", failed)

    def test_pdb_wrong_min_available_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "PodDisruptionBudget", pdb_names[component])["spec"]["minAvailable"] = 1
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.min_available", failed)

    def test_pdb_wrong_namespace_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "PodDisruptionBudget", pdb_names[component])["metadata"]["namespace"] = "default"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.namespace_matches", failed)

    def test_pdb_wrong_selector_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "PodDisruptionBudget", pdb_names[component])["spec"]["selector"]["matchLabels"][
                    "app.kubernetes.io/instance"
                ] = "wrong-instance"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.selector_matches_pod_labels", failed)

    def test_pdb_cross_workload_selector_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        other = {"gateway": "app", "app": "gateway"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "PodDisruptionBudget", pdb_names[component])["spec"]["selector"]["matchLabels"] = _selector(
                    other[component]
                )
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.no_cross_workload_selector", failed)

    def test_pdb_uses_max_unavailable_instead_fails_both_workloads(self):
        pdb_names = {"gateway": "maops-gateway-pdb", "app": "maops-app-pdb"}
        for component, _dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                pdb = _find(docs, "PodDisruptionBudget", pdb_names[component])
                del pdb["spec"]["minAvailable"]
                pdb["spec"]["maxUnavailable"] = 1
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.pdb.min_available", failed)
                self.assertIn(f"{component}.pdb.no_max_unavailable", failed)


class StateStatefulSetTests(unittest.TestCase):
    """DAY4: maops-state StatefulSet - replicas, scheduling, and
    PVC-backed storage shape (volumeClaimTemplates, retention policy)."""

    def test_state_replicas_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["replicas"] = 2
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.replicas", failed)

    def test_state_topology_spread_wrongly_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "StatefulSet", "maops-state"))["topologySpreadConstraints"] = _topology_spread("state")
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.scheduling.no_topology_spread", failed)

    def test_state_volume_claim_template_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["volumeClaimTemplates"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.volume_claim_template_exists", failed)

    def test_state_volume_claim_template_wrong_name_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] = "storage"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.volume_claim_template_exists", failed)

    def test_state_storage_class_name_wrongly_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["volumeClaimTemplates"][0]["spec"][
            "storageClassName"
        ] = "standard"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.default_storage_class", failed)

    def test_state_retention_when_deleted_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["persistentVolumeClaimRetentionPolicy"][
            "whenDeleted"
        ] = "Delete"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.retention_when_deleted", failed)

    def test_state_retention_when_scaled_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["persistentVolumeClaimRetentionPolicy"][
            "whenScaled"
        ] = "Delete"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.retention_when_scaled", failed)

    def test_state_claim_storage_capacity_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"][
            "storage"
        ] = "512Mi"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.requested_capacity", failed)

    def test_state_claim_access_mode_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["volumeClaimTemplates"][0]["spec"]["accessModes"] = [
            "ReadWriteMany"
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.access_mode", failed)

    def test_state_data_mount_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _container_of(_find(docs, "StatefulSet", "maops-state"))
        container["volumeMounts"] = [m for m in container["volumeMounts"] if m["name"] != "data"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.data_mount_path", failed)

    def test_state_service_name_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "StatefulSet", "maops-state")["spec"]["serviceName"] = "maops-state"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.service_name", failed)

    def test_state_secret_volume_wrong_name_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "StatefulSet", "maops-state"))["volumes"][0]["secret"]["secretName"] = "wrong-secret"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.secret.volume_present", failed)


class StateServiceTests(unittest.TestCase):
    """DAY4: the two maops-state Services - a normal ClusterIP Service
    and the StatefulSet-governing headless Service."""

    def test_state_headless_cluster_ip_not_none_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state-headless")["spec"]["clusterIP"] = "10.96.10.10"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.headless_service.cluster_ip_none", failed)

    def test_state_service_selecting_app_labels_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state")["spec"]["selector"] = _selector("app")
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.no_selector_collision_state_selects_app", failed)

    def test_state_service_selecting_gateway_labels_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state")["spec"]["selector"] = _selector("gateway")
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.no_selector_collision_state_selects_gateway", failed)


class SelectorIsolationTests(unittest.TestCase):
    def test_gateway_service_selecting_app_labels_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-gateway")["spec"]["selector"] = _selector("app")
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.no_selector_collision_gateway_selects_app", failed)

    def test_app_service_selecting_gateway_labels_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-app")["spec"]["selector"] = _selector("gateway")
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.no_selector_collision_app_selects_gateway", failed)

    def test_service_selector_mismatch_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-gateway")["spec"]["selector"]["app.kubernetes.io/instance"] = "wrong-instance"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.service.selector_matches_pod_labels", failed)


class ServiceTypeTests(unittest.TestCase):
    def test_service_changed_to_nodeport_fails(self):
        docs = copy.deepcopy(_base_docs())
        svc = _find(docs, "Service", "maops-gateway")
        svc["spec"]["type"] = "NodePort"
        svc["spec"]["ports"][0]["nodePort"] = 30080
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.service.type_cluster_ip", failed)
        self.assertIn("gateway.service.no_node_port", failed)

    def test_service_changed_to_loadbalancer_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-app")["spec"]["type"] = "LoadBalancer"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.service.type_cluster_ip", failed)


class BackendHostWiringTests(unittest.TestCase):
    def test_backend_host_changed_to_ip_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap", "maops-gateway-config")["data"]["BACKEND_HOST"] = "10.96.5.23"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.backend_host", failed)
        self.assertIn("gateway.configmap.backend_host_not_ip", failed)

    def test_backend_host_changed_to_pod_like_identity_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap", "maops-gateway-config")["data"]["BACKEND_HOST"] = "maops-app-7d9f8c9c5-abcde"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.backend_host", failed)
        self.assertIn("gateway.configmap.backend_host_not_pod_like", failed)

    def test_backend_port_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap", "maops-gateway-config")["data"]["BACKEND_PORT"] = "9090"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.backend_port", failed)


class ForbiddenResourceTests(unittest.TestCase):
    def test_secret_object_committed_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "maops-internal-auth", "namespace": "maops-platform"},
                "data": {"internal-token": "c2hvdWxkLW5vdC1iZS1oZXJl"},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_forbidden_ingress_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": "maops-gateway", "namespace": "maops-platform"},
                "spec": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_forbidden_pvc_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "maops-app-data", "namespace": "maops-platform"},
                "spec": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_unexpected_extra_serviceaccount_fails_count(self):
        # DAY5: ServiceAccount is now a sanctioned kind (exactly 4 are
        # expected - gateway/app/state/diagnostics), so an extra one no
        # longer trips scope.no_forbidden_resources - it trips the exact
        # count check instead.
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "maops-unexpected", "namespace": "maops-platform"},
                "automountServiceAccountToken": False,
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account_count", failed)
        self.assertNotIn("scope.no_forbidden_resources", failed)

    def test_unexpected_extra_networkpolicy_fails_count(self):
        # DAY5: NetworkPolicy is now sanctioned (exactly 7 are expected),
        # so an extra one trips the exact count check, not
        # scope.no_forbidden_resources.
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "maops-unexpected", "namespace": "maops-platform"},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress"]},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.count", failed)
        self.assertNotIn("scope.no_forbidden_resources", failed)

    def test_forbidden_standalone_state_pvc_present_fails(self):
        # DAY4: StatefulSet itself is now sanctioned (it's how maops-state
        # gets its PVC-backed /data volume), but a *standalone* committed
        # PersistentVolumeClaim shadowing that same claim (e.g. someone
        # accidentally committing what volumeClaimTemplates should
        # generate at runtime instead) must still fail - only
        # StatefulSet.spec.volumeClaimTemplates is a sanctioned storage
        # path.
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "maops-state-data", "namespace": "maops-platform"},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "256Mi"}}},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_statefulset_alone_is_not_forbidden(self):
        # DAY5: the baseline fixture's StatefulSet, ServiceAccounts, Role,
        # RoleBinding, and NetworkPolicies must NOT trip
        # scope.no_forbidden_resources - only Secret/Ingress/PVC/
        # ClusterRole/ClusterRoleBinding/HPA remain forbidden as of Day 5.
        failed = _failed_names(run_checks(copy.deepcopy(_base_docs())))
        self.assertNotIn("scope.no_forbidden_resources", failed)


class ForbiddenRbacKindTests(unittest.TestCase):
    """DAY5: Role and RoleBinding are now sanctioned kinds (checked
    positively/structurally elsewhere), but ClusterRole/
    ClusterRoleBinding remain forbidden at every day - Day 5's RBAC is
    deliberately namespace-scoped only (see docs/roadmap.md)."""

    _CLUSTER_RBAC_FIXTURES = {
        "ClusterRole": {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "maops-cluster-role"},
            "rules": [],
        },
        "ClusterRoleBinding": {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "maops-cluster-role-binding"},
            "subjects": [],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "maops-cluster-role"},
        },
    }

    def test_forbidden_cluster_scoped_rbac_kinds_each_fail(self):
        for kind, fixture in self._CLUSTER_RBAC_FIXTURES.items():
            with self.subTest(kind=kind):
                docs = copy.deepcopy(_base_docs())
                docs.append(copy.deepcopy(fixture))
                failed = _failed_names(run_checks(docs))
                self.assertIn("scope.no_forbidden_resources", failed)

    def test_unexpected_extra_role_fails_count_not_forbidden(self):
        # DAY5: Role is sanctioned (exactly 1 is expected), so an extra
        # one trips the exact count check, not scope.no_forbidden_resources.
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "maops-unexpected-role", "namespace": "maops-platform"},
                "rules": [],
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role_count", failed)
        self.assertNotIn("scope.no_forbidden_resources", failed)

    def test_unexpected_extra_role_binding_fails_count_not_forbidden(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "maops-unexpected-binding", "namespace": "maops-platform"},
                "subjects": [],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": "maops-diagnostics-reader",
                },
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role_binding_count", failed)
        self.assertNotIn("scope.no_forbidden_resources", failed)


class SecretWiringTests(unittest.TestCase):
    def test_gateway_missing_secret_volume_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["volumes"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.secret.volume_present", failed)

    def test_app_missing_secret_volume_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-app"))["volumes"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.secret.volume_present", failed)

    def test_secret_mount_not_read_only_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["volumeMounts"][0]["readOnly"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.secret.mount_read_only", failed)

    def test_wrong_secret_name_fails(self):
        docs = copy.deepcopy(_base_docs())
        volume = _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["volumes"][0]
        volume["secret"]["secretName"] = "some-other-secret"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.secret.volume_present", failed)

    def test_wrong_mount_path_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["volumeMounts"][0]["mountPath"] = "/etc/maops-secret"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.secret.mount_path", failed)

    def test_wrong_key_via_items_restriction_fails(self):
        docs = copy.deepcopy(_base_docs())
        volume = _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["volumes"][0]
        volume["secret"]["items"] = [{"key": "wrong-key", "path": "wrong-key"}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.secret.key_present", failed)

    def test_token_like_key_added_to_configmap_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap", "maops-gateway-config")["data"]["INTERNAL_TOKEN"] = "should-not-be-here"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.no_secret_like_values", failed)


class ProbeTests(unittest.TestCase):
    def test_gateway_readiness_probe_removed_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _container_of(_find(docs, "Deployment", "maops-gateway"))["readinessProbe"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.probes.readiness_path", failed)

    def test_gateway_liveness_pointed_at_readyz_fails(self):
        # A circular-liveness misconfiguration: gateway liveness must
        # never depend on the backend-aware /readyz endpoint.
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["livenessProbe"]["httpGet"]["path"] = "/readyz"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.probes.liveness_path", failed)

    def test_app_readiness_probe_wrong_path_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["readinessProbe"]["httpGet"]["path"] = "/healthz"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.probes.readiness_path", failed)

    def test_startup_probe_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _container_of(_find(docs, "Deployment", "maops-gateway"))["startupProbe"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.probes.startup_present", failed)


class ResourceTests(unittest.TestCase):
    def test_gateway_resource_requests_relaxed_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["resources"]["requests"]["cpu"] = "100m"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.resources.requests", failed)

    def test_app_resource_limits_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _container_of(_find(docs, "Deployment", "maops-app"))["resources"]["limits"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.resources.limits", failed)


class SecurityContextTests(unittest.TestCase):
    def test_gateway_run_as_non_root_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["runAsNonRoot"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.pod_run_as_non_root", failed)

    def test_app_read_only_root_filesystem_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["securityContext"]["readOnlyRootFilesystem"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.read_only_root_filesystem", failed)

    def test_gateway_capabilities_not_drop_all_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["capabilities"] = {"drop": ["NET_RAW"]}
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.capabilities_drop_all", failed)

    def test_app_automount_service_account_token_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-app"))["automountServiceAccountToken"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.automount_service_account_token", failed)

    def test_gateway_host_network_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["hostNetwork"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.no_host_network", failed)

    def test_app_host_pid_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-app"))["hostPID"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.no_host_pid", failed)

    def test_gateway_host_ipc_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["hostIPC"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.no_host_ipc", failed)

    def test_app_host_port_set_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["ports"][0]["hostPort"] = 8080
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.no_host_port", failed)

    def test_gateway_privileged_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["privileged"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.not_privileged", failed)

    def test_app_hostpath_volume_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _pod_spec_of(_find(docs, "Deployment", "maops-app"))
        pod_spec["volumes"].append({"name": "host-tmp", "hostPath": {"path": "/tmp"}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.no_host_path_volumes", failed)

    def test_gateway_seccomp_profile_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["seccompProfile"] = {"type": "Unconfined"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.seccomp_profile", failed)

    # DAY2-TEST-M1: pod_run_as_user/pod_run_as_group had zero dedicated
    # negative-test coverage on either workload.
    def test_gateway_run_as_user_zero_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["runAsUser"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.pod_run_as_user", failed)

    def test_app_run_as_user_zero_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-app"))["securityContext"]["runAsUser"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.pod_run_as_user", failed)

    def test_gateway_run_as_group_zero_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["securityContext"]["runAsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.pod_run_as_group", failed)

    def test_app_run_as_group_zero_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-app"))["securityContext"]["runAsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.security.pod_run_as_group", failed)


class ConfigMapExistenceTests(unittest.TestCase):
    """DAY2-TEST-M1: gateway.configmap.exists / app.configmap.exists (the
    object-presence check, distinct from the namespace/no-secret-like-
    values checks on the same object) had no dedicated test either
    direction."""

    def test_gateway_configmap_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "maops-gateway-config")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.exists", failed)

    def test_app_configmap_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "maops-app-config")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.configmap.exists", failed)


_WORKLOADS = [("gateway", "maops-gateway"), ("app", "maops-app")]


class MirrorCoverageTests(unittest.TestCase):
    """DAY2-TEST-M2: closes one-sided mirror gaps identified in the
    independent test review. `_check_workload_security_and_probes()` is a
    single shared function invoked once per workload, so a workload-
    specific regression (e.g. a copy-paste bug that only breaks one
    side's wiring) would previously not be caught if the corresponding
    negative test only existed for the *other* workload. Table-driven
    over both workloads via subTest, rather than duplicating each test
    method, per the task's own "avoid vanity duplication" guidance."""

    def test_deployment_image_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["image"] = "wrong-image:9.9.9"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.image", failed)

    def test_image_pull_policy_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["imagePullPolicy"] = "Always"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.image_pull_policy", failed)

    def test_startup_probe_path_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["startupProbe"]["httpGet"]["path"] = "/wrong"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.probes.startup_path", failed)

    def test_liveness_probe_path_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["livenessProbe"]["httpGet"]["path"] = "/wrong"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.probes.liveness_path", failed)

    def test_resource_requests_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["resources"]["requests"]["memory"] = "999Mi"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.resources.requests", failed)

    def test_resource_limits_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["resources"]["limits"]["memory"] = "1Mi"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.resources.limits", failed)

    def test_deployment_namespace_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Deployment", dep_name)["metadata"]["namespace"] = "default"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.deployment.namespace_matches", failed)

    def test_service_selector_mismatch_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _find(docs, "Service", dep_name)["spec"]["selector"]["app.kubernetes.io/instance"] = "wrong-instance"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.service.selector_matches_pod_labels", failed)

    def test_service_type_and_nodeport_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                svc = _find(docs, "Service", dep_name)
                svc["spec"]["type"] = "NodePort"
                svc["spec"]["ports"][0]["nodePort"] = 30080
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.service.type_cluster_ip", failed)
                self.assertIn(f"{component}.service.no_node_port", failed)

    def test_secret_mount_path_wrong_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["volumeMounts"][0]["mountPath"] = "/wrong/path"
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.secret.mount_path", failed)

    def test_secret_mount_not_read_only_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["volumeMounts"][0]["readOnly"] = False
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.secret.mount_read_only", failed)

    def test_secret_key_wrong_via_items_restriction_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                volume = _pod_spec_of(_find(docs, "Deployment", dep_name))["volumes"][0]
                volume["secret"]["items"] = [{"key": "wrong-key", "path": "wrong-key"}]
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.secret.key_present", failed)

    def test_configmap_wiring_removed_fails_both_workloads(self):
        for component, dep_name in _WORKLOADS:
            with self.subTest(component=component):
                docs = copy.deepcopy(_base_docs())
                _container_of(_find(docs, "Deployment", dep_name))["envFrom"] = []
                failed = _failed_names(run_checks(docs))
                self.assertIn(f"{component}.configmap.wired_to_container", failed)


class VersionDriftTests(unittest.TestCase):
    def test_gateway_image_version_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["image"] = "maops-kubernetes-gateway:0.1.0"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.deployment.image", failed)

    def test_app_version_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment", "maops-app")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.version_label", failed)

    def test_gateway_pod_template_version_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        dep = _find(docs, "Deployment", "maops-gateway")
        dep["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "0.2.0"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.pod_template.version_label", failed)

    def test_image_pull_policy_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["imagePullPolicy"] = "Always"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.image_pull_policy", failed)


class NamespaceCrossCheckTests(unittest.TestCase):
    def test_gateway_configmap_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap", "maops-gateway-config")["metadata"]["namespace"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.namespace_matches", failed)

    def test_app_deployment_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment", "maops-app")["metadata"]["namespace"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.namespace_matches", failed)

    def test_gateway_service_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-gateway")["metadata"]["namespace"] = "other-namespace"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.service.namespace_matches", failed)

    def test_namespace_object_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("kind") != "Namespace"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("namespace.exists", failed)


class ContainerShapeTests(unittest.TestCase):
    def test_deployment_second_container_fails_single_container(self):
        docs = copy.deepcopy(_base_docs())
        containers = _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["containers"]
        containers.append(copy.deepcopy(containers[0]))
        containers[1]["name"] = "sidecar"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.deployment.single_container", failed)

    def test_container_name_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-app"))["name"] = "wrong-name"
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.container_name", failed)

    def test_configmap_wiring_removed_fails(self):
        docs = copy.deepcopy(_base_docs())
        _container_of(_find(docs, "Deployment", "maops-gateway"))["envFrom"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.configmap.wired_to_container", failed)


def _state_sts(docs):
    return _find(docs, "StatefulSet", "maops-state")


def _state_pod_spec(docs):
    return _state_sts(docs)["spec"]["template"]["spec"]


def _state_container_of(docs):
    return _state_pod_spec(docs)["containers"][0]


class StateSecurityContextMutationTests(unittest.TestCase):
    """DAY4-TEST-M4: every one of the 40 previously-untested `state.*`
    checks (52 total, only 12 previously named-asserted), security
    fields first. Each test asserts the SPECIFIC check name fails -
    not merely that some check fails - per the batch 2 briefing's
    "merely finding its name in a test file is not coverage proof"
    requirement."""

    def test_pod_run_as_non_root_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["securityContext"]["runAsNonRoot"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.pod_run_as_non_root", failed)

    def test_pod_run_as_user_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["securityContext"]["runAsUser"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.pod_run_as_user", failed)

    def test_pod_run_as_group_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["securityContext"]["runAsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.pod_run_as_group", failed)

    def test_pod_fs_group_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["securityContext"]["fsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.pod_fs_group", failed)

    def test_seccomp_profile_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["securityContext"]["seccompProfile"] = {"type": "Unconfined"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.seccomp_profile", failed)

    def test_allow_privilege_escalation_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["securityContext"]["allowPrivilegeEscalation"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.allow_privilege_escalation", failed)

    def test_read_only_root_filesystem_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["securityContext"]["readOnlyRootFilesystem"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.read_only_root_filesystem", failed)

    def test_capabilities_not_drop_all_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["securityContext"]["capabilities"] = {"drop": ["NET_RAW"]}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.capabilities_drop_all", failed)

    def test_automount_service_account_token_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["automountServiceAccountToken"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.automount_service_account_token", failed)

    def test_host_network_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["hostNetwork"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.no_host_network", failed)

    def test_host_pid_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["hostPID"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.no_host_pid", failed)

    def test_host_ipc_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["hostIPC"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.no_host_ipc", failed)

    def test_privileged_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["securityContext"]["privileged"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.not_privileged", failed)

    def test_host_path_volume_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_pod_spec(docs)["volumes"].append({"name": "evil", "hostPath": {"path": "/etc"}})
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.no_host_path_volumes", failed)


class StateStatefulSetShapeMutationTests(unittest.TestCase):
    def test_statefulset_missing_fails_exists(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "StatefulSet")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.exists", failed)

    def test_namespace_mismatch_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_sts(docs)["metadata"]["namespace"] = "wrong-namespace"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.namespace_matches", failed)

    def test_version_label_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_sts(docs)["metadata"]["labels"]["app.kubernetes.io/version"] = "9.9.9"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.version_label", failed)

    def test_pod_template_version_label_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_sts(docs)["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "9.9.9"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.pod_template.version_label", failed)

    def test_pod_template_component_label_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_sts(docs)["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] = "wrong"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.pod_template.component_label", failed)

    def test_second_container_fails_single_container(self):
        docs = copy.deepcopy(_base_docs())
        containers = _state_pod_spec(docs)["containers"]
        containers.append(copy.deepcopy(containers[0]))
        containers[1]["name"] = "sidecar"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.single_container", failed)

    def test_container_name_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["name"] = "wrong-name"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.container_name", failed)

    def test_image_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["image"] = "wrong-image:latest"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.image", failed)

    def test_image_pull_policy_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["imagePullPolicy"] = "Always"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.statefulset.image_pull_policy", failed)

    def test_node_affinity_removed_fails_control_plane_excluded(self):
        docs = copy.deepcopy(_base_docs())
        del _state_pod_spec(docs)["affinity"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.scheduling.control_plane_excluded", failed)

    def test_node_affinity_changed_to_allow_control_plane_fails(self):
        docs = copy.deepcopy(_base_docs())
        expr = _state_pod_spec(docs)["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]
        expr["operator"] = "Exists"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.scheduling.control_plane_excluded", failed)


class StateProbesResourcesConfigmapMutationTests(unittest.TestCase):
    def test_startup_probe_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _state_container_of(docs)["startupProbe"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.probes.startup_present", failed)

    def test_liveness_probe_path_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["livenessProbe"]["httpGet"]["path"] = "/wrong"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.probes.liveness_path", failed)

    def test_readiness_probe_path_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["readinessProbe"]["httpGet"]["path"] = "/wrong"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.probes.readiness_path", failed)

    def test_resource_requests_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["resources"]["requests"] = {"cpu": "1", "memory": "1Gi"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.resources.requests", failed)

    def test_resource_limits_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["resources"]["limits"] = {"cpu": "1", "memory": "1Gi"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.resources.limits", failed)

    def test_configmap_wiring_removed_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_container_of(docs)["envFrom"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.configmap.wired_to_container", failed)

    def test_configmap_object_missing_fails_exists(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "maops-state-config")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.configmap.exists", failed)


class StateSecretMutationTests(unittest.TestCase):
    def test_secret_mount_path_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        mounts = _state_container_of(docs)["volumeMounts"]
        next(m for m in mounts if m["name"] == "state-auth")["mountPath"] = "/wrong/path"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.secret.mount_path", failed)

    def test_secret_mount_not_read_only_fails(self):
        docs = copy.deepcopy(_base_docs())
        mounts = _state_container_of(docs)["volumeMounts"]
        next(m for m in mounts if m["name"] == "state-auth")["readOnly"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.secret.mount_read_only", failed)


class StateStorageMutationTests(unittest.TestCase):
    def test_data_mount_writable_violated_fails(self):
        docs = copy.deepcopy(_base_docs())
        mounts = _state_container_of(docs)["volumeMounts"]
        next(m for m in mounts if m["name"] == "data")["readOnly"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.data_mount_writable", failed)

    def test_volume_mode_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _state_sts(docs)["spec"]["volumeClaimTemplates"][0]["spec"]["volumeMode"] = "Block"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.storage.volume_mode", failed)


class StateServiceMutationTests(unittest.TestCase):
    def test_state_service_missing_fails_exists(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "Service" and d["metadata"]["name"] == "maops-state")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.service.exists", failed)

    def test_state_service_type_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state")["spec"]["type"] = "NodePort"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.service.type_cluster_ip", failed)

    def test_state_service_selector_mismatch_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state")["spec"]["selector"] = {"app.kubernetes.io/component": "wrong"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.service.selector_matches_pod_labels", failed)

    def test_state_headless_service_missing_fails_exists(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if not (d.get("kind") == "Service" and d["metadata"]["name"] == "maops-state-headless")]
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.headless_service.exists", failed)

    def test_state_headless_service_cluster_ip_not_none_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state-headless")["spec"]["clusterIP"] = "10.96.0.5"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.headless_service.cluster_ip_none", failed)

    def test_state_headless_service_selector_mismatch_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service", "maops-state-headless")["spec"]["selector"] = {"app.kubernetes.io/component": "wrong"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.headless_service.selector_matches_pod_labels", failed)


# ---------------------------------------------------------------------------
# DAY5: ServiceAccount / RBAC / NetworkPolicy
# ---------------------------------------------------------------------------


class ServiceAccountTests(unittest.TestCase):
    """ServiceAccount names and token automount behavior for all four
    Day 5 identities."""

    def test_gateway_service_account_missing_fails(self):
        docs = [
            d
            for d in copy.deepcopy(_base_docs())
            if not (d.get("kind") == "ServiceAccount" and d["metadata"]["name"] == "maops-gateway")
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account.maops-gateway.exists", failed)
        self.assertIn("rbac.service_account_count", failed)

    def test_app_service_account_automount_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ServiceAccount", "maops-app")["automountServiceAccountToken"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account.maops-app.automount_false", failed)
        self.assertNotIn("rbac.service_account.maops-gateway.automount_false", failed)

    def test_state_service_account_wrong_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ServiceAccount", "maops-state")["metadata"]["namespace"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account.maops-state.namespace", failed)

    def test_diagnostics_service_account_wrong_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ServiceAccount", "maops-diagnostics")["metadata"]["namespace"] = "maops-platform"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account.diagnostics.namespace", failed)

    def test_diagnostics_service_account_automount_false_fails(self):
        # DAY5: diagnostics is the ONE identity that must receive a
        # token, unlike the three application ServiceAccounts.
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ServiceAccount", "maops-diagnostics")["automountServiceAccountToken"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.service_account.diagnostics.automount_true", failed)

    def test_gateway_deployment_not_using_own_service_account_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "Deployment", "maops-gateway"))["serviceAccountName"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("gateway.security.service_account_name", failed)

    def test_state_statefulset_not_using_own_service_account_fails(self):
        docs = copy.deepcopy(_base_docs())
        _pod_spec_of(_find(docs, "StatefulSet", "maops-state"))["serviceAccountName"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("state.security.service_account_name", failed)


class RbacRoleAndBindingTests(unittest.TestCase):
    """Role/RoleBinding scope, and allowed/forbidden RBAC verbs and
    resources for the maops-diagnostics-reader Role."""

    def test_role_not_namespace_scoped_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Role", "maops-diagnostics-reader")["metadata"]["namespace"] = "kube-system"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.namespace_scoped", failed)

    def test_role_missing_pods_grant_fails(self):
        docs = copy.deepcopy(_base_docs())
        role = _find(docs, "Role", "maops-diagnostics-reader")
        role["rules"] = [r for r in role["rules"] if "pods" not in (r.get("resources") or [])]
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.grants_pods_and_services", failed)

    def test_role_missing_endpointslices_grant_fails(self):
        docs = copy.deepcopy(_base_docs())
        role = _find(docs, "Role", "maops-diagnostics-reader")
        role["rules"] = [r for r in role["rules"] if "discovery.k8s.io" not in (r.get("apiGroups") or [])]
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.grants_endpointslices", failed)

    def test_role_write_verb_added_fails_read_only_and_forbidden_verbs(self):
        docs = copy.deepcopy(_base_docs())
        role = _find(docs, "Role", "maops-diagnostics-reader")
        role["rules"][0]["verbs"].append("delete")
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.verbs_read_only", failed)
        self.assertIn("rbac.role.no_forbidden_verbs", failed)

    def test_role_secrets_resource_added_fails_forbidden_resources(self):
        docs = copy.deepcopy(_base_docs())
        role = _find(docs, "Role", "maops-diagnostics-reader")
        role["rules"][0]["resources"].append("secrets")
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.no_forbidden_resources", failed)

    def test_role_wildcard_api_group_fails(self):
        docs = copy.deepcopy(_base_docs())
        role = _find(docs, "Role", "maops-diagnostics-reader")
        role["rules"][0]["apiGroups"] = ["*"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role.api_groups_scoped", failed)

    def test_role_binding_references_cluster_role_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "RoleBinding", "maops-diagnostics-reader-binding")["roleRef"]["kind"] = "ClusterRole"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role_binding.references_role_not_cluster_role", failed)

    def test_role_binding_subject_wrong_service_account_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "RoleBinding", "maops-diagnostics-reader-binding")["subjects"][0]["name"] = "maops-gateway"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role_binding.subject_is_diagnostics_sa", failed)

    def test_role_binding_subject_wrong_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "RoleBinding", "maops-diagnostics-reader-binding")["subjects"][0]["namespace"] = "maops-platform"
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.role_binding.subject_is_diagnostics_sa", failed)

    def test_application_service_account_bound_to_role_fails(self):
        # DAY5 explicit requirement: application ServiceAccounts must
        # never be bound to any Role/ClusterRole.
        docs = copy.deepcopy(_base_docs())
        _find(docs, "RoleBinding", "maops-diagnostics-reader-binding")["subjects"].append(
            {"kind": "ServiceAccount", "name": "maops-gateway", "namespace": "maops-platform"}
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("rbac.application_service_accounts_not_bound", failed)


class NetworkPolicyDefaultDenyTests(unittest.TestCase):
    """Structure of the default-deny ingress+egress baseline policy."""

    def test_default_deny_missing_fails(self):
        docs = [
            d
            for d in copy.deepcopy(_base_docs())
            if not (d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "maops-default-deny-all")
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.default_deny.exists", failed)
        self.assertIn("networkpolicy.count", failed)

    def test_default_deny_pod_selector_not_empty_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "NetworkPolicy", "maops-default-deny-all")["spec"]["podSelector"] = {
            "matchLabels": {"app.kubernetes.io/component": "gateway"}
        }
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.default_deny.applies_to_all_pods", failed)

    def test_default_deny_missing_egress_policy_type_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "NetworkPolicy", "maops-default-deny-all")["spec"]["policyTypes"] = ["Ingress"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.default_deny.both_directions", failed)

    def test_default_deny_with_an_ingress_rule_fails_no_rules(self):
        # An accidental ingress rule on the deny-all policy would punch a
        # hole in the baseline for every Pod in the namespace at once.
        docs = copy.deepcopy(_base_docs())
        _find(docs, "NetworkPolicy", "maops-default-deny-all")["spec"]["ingress"] = [{"from": []}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.default_deny.no_rules", failed)


class NetworkPolicyAllowTests(unittest.TestCase):
    """DNS, gateway -> app, and app -> state allow rules."""

    def test_dns_policy_missing_kube_dns_selector_fails(self):
        docs = copy.deepcopy(_base_docs())
        peer = _find(docs, "NetworkPolicy", "maops-allow-dns-egress")["spec"]["egress"][0]["to"][0]
        peer["podSelector"]["matchLabels"]["k8s-app"] = "wrong"
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.dns.allows_udp_tcp_53_to_coredns", failed)

    def test_dns_policy_missing_tcp_port_fails(self):
        docs = copy.deepcopy(_base_docs())
        rule = _find(docs, "NetworkPolicy", "maops-allow-dns-egress")["spec"]["egress"][0]
        rule["ports"] = [{"protocol": "UDP", "port": 53}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.dns.allows_udp_tcp_53_to_coredns", failed)

    def test_gateway_egress_app_wrong_pod_selector_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-gateway-egress-to-app")
        netpol["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] = "app"
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_egress_app.pod_selector_is_gateway", failed)

    def test_gateway_egress_app_wrong_port_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-gateway-egress-to-app")
        netpol["spec"]["egress"][0]["ports"] = [{"protocol": "TCP", "port": 9999}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_egress_app.allows_app_8080", failed)

    def test_app_ingress_gateway_missing_fails(self):
        docs = [
            d
            for d in copy.deepcopy(_base_docs())
            if not (d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "maops-allow-app-ingress-from-gateway")
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.app_ingress_gateway.pod_selector_is_app", failed)
        self.assertIn("networkpolicy.app_ingress_gateway.allows_gateway_8080", failed)

    def test_app_egress_state_wrong_component_target_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-app-egress-to-state")
        netpol["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] = "gateway"
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.app_egress_state.allows_state_8080", failed)

    def test_state_ingress_app_wrong_port_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-state-ingress-from-app")
        netpol["spec"]["ingress"][0]["ports"] = [{"protocol": "TCP", "port": 80}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.state_ingress_app.allows_app_8080", failed)

    def test_gateway_ingress_validation_wrong_component_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-gateway-ingress-from-validation")
        peer = netpol["spec"]["ingress"][0]["from"][0]
        peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"] = "diagnostics"
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_ingress_validation.allows_validation_client_8080", failed)

    def test_gateway_ingress_validation_wrong_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-gateway-ingress-from-validation")
        peer = netpol["spec"]["ingress"][0]["from"][0]
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] = "maops-platform"
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_ingress_validation.allows_validation_client_8080", failed)


class NetworkPolicyNegativeTests(unittest.TestCase):
    """Absence of a gateway -> state allow, and absence of any
    validation-client bypass into app/state - the explicit denies this
    architecture requires."""

    def test_gateway_egress_allow_extended_to_state_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-gateway-egress-to-app")
        netpol["spec"]["egress"][0]["to"].append(
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}}
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.gateway_egress_app.never_targets_state", failed)

    def test_state_ingress_allow_extended_to_gateway_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-state-ingress-from-app")
        netpol["spec"]["ingress"][0]["from"].append(
            {"podSelector": {"matchLabels": {"app.kubernetes.io/component": "gateway"}}}
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.state_ingress_app.never_allows_gateway", failed)

    def test_app_ingress_allow_extended_to_validation_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-app-ingress-from-gateway")
        netpol["spec"]["ingress"][0]["from"].append(
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": VALIDATION_NAMESPACE}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/component": "validation-client"}},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.app_ingress_gateway.never_allows_validation_namespace", failed)

    def test_state_ingress_allow_extended_to_validation_namespace_fails(self):
        docs = copy.deepcopy(_base_docs())
        netpol = _find(docs, "NetworkPolicy", "maops-allow-state-ingress-from-app")
        netpol["spec"]["ingress"][0]["from"].append(
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": VALIDATION_NAMESPACE}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/component": "validation-client"}},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.state_ingress_app.never_allows_validation_namespace", failed)

    def test_stray_extra_policy_granting_validation_namespace_into_app_fails(self):
        # A bypass introduced under an unrelated NetworkPolicy name (not
        # one of the three already-scrutinized allow objects) must still
        # be caught by the cross-cutting stray-rule guard.
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "maops-sneaky-allow", "namespace": "maops-platform"},
                "spec": {
                    "podSelector": {"matchLabels": {"app.kubernetes.io/component": "app"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [
                                {
                                    "namespaceSelector": {
                                        "matchLabels": {"kubernetes.io/metadata.name": VALIDATION_NAMESPACE}
                                    }
                                }
                            ]
                        }
                    ],
                },
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("networkpolicy.maops-sneaky-allow.no_stray_validation_namespace_ingress", failed)
        # And it also trips the exact-count guard, independently.
        self.assertIn("networkpolicy.count", failed)


class NamespaceValidationTests(unittest.TestCase):
    """Namespace/pod selector correctness for the two-namespace Day 5
    topology."""

    def test_validation_namespace_missing_fails(self):
        docs = [
            d
            for d in copy.deepcopy(_base_docs())
            if not (d.get("kind") == "Namespace" and d["metadata"]["name"] == VALIDATION_NAMESPACE)
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("namespace.validation_exists", failed)
        self.assertIn("namespace.count", failed)
        self.assertNotIn("namespace.exists", failed)

    def test_application_namespace_missing_fails(self):
        docs = [
            d
            for d in copy.deepcopy(_base_docs())
            if not (d.get("kind") == "Namespace" and d["metadata"]["name"] == "maops-platform")
        ]
        failed = _failed_names(run_checks(docs))
        self.assertIn("namespace.exists", failed)
        self.assertNotIn("namespace.validation_exists", failed)


if __name__ == "__main__":
    unittest.main()

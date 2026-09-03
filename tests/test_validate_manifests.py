"""
Docker-free unit tests for the repository-owned static validation logic
in scripts/validate_manifests.py (Day 2: gateway + app workloads).

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

VERSION = "0.2.0"


def _labels(component: str | None) -> dict:
    labels = {
        "app.kubernetes.io/name": "maops-kubernetes-platform",
        "app.kubernetes.io/instance": "maops-kubernetes-platform-day2",
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
        "app.kubernetes.io/instance": "maops-kubernetes-platform-day2",
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


def _container(name: str, image: str, configmap: str) -> dict:
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
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
    }


def _deployment(name: str, component: str, image: str, configmap: str) -> dict:
    pod_labels = _labels(component)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "maops-platform", "labels": _labels(component)},
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": _selector(component)},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": _security_context(),
                    "containers": [_container(name, image, configmap)],
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


def _base_docs() -> list[dict]:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "maops-platform", "labels": _labels(None)},
    }
    gateway_configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-gateway-config", "namespace": "maops-platform", "labels": _labels("gateway")},
        "data": {
            "BACKEND_HOST": "maops-app",
            "BACKEND_PORT": "8080",
            "BACKEND_TIMEOUT_SECONDS": "3",
            "APP_NAME": "maops-kubernetes-gateway",
            "APP_ENVIRONMENT": "day2-service-discovery",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Gateway (Day 2)",
            "APP_LOG_LEVEL": "info",
        },
    }
    app_configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-app-config", "namespace": "maops-platform", "labels": _labels("app")},
        "data": {
            "APP_NAME": "maops-kubernetes-app",
            "APP_ENVIRONMENT": "day2-service-discovery",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes App (Day 2)",
            "APP_LOG_LEVEL": "info",
        },
    }
    gateway_deployment = _deployment("maops-gateway", "gateway", "maops-kubernetes-gateway:0.2.0", "maops-gateway-config")
    app_deployment = _deployment("maops-app", "app", "maops-kubernetes-app:0.2.0", "maops-app-config")
    gateway_service = _service("maops-gateway", "gateway")
    app_service = _service("maops-app", "app")
    return [
        namespace,
        gateway_configmap,
        app_configmap,
        gateway_deployment,
        app_deployment,
        gateway_service,
        app_service,
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
        self.assertGreaterEqual(len(findings), 90)


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
        _find(docs, "Deployment", "maops-app")["spec"]["replicas"] = 3
        failed = _failed_names(run_checks(docs))
        self.assertIn("app.deployment.replicas", failed)
        self.assertNotIn("gateway.deployment.replicas", failed)


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

    def test_forbidden_serviceaccount_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "maops-workload", "namespace": "maops-platform"},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_forbidden_networkpolicy_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "maops-deny-all", "namespace": "maops-platform"},
                "spec": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_forbidden_statefulset_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "maops-data", "namespace": "maops-platform"},
                "spec": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)


class ForbiddenRbacKindTests(unittest.TestCase):
    """DAY2-TEST-L3: Role/RoleBinding/ClusterRole/ClusterRoleBinding relied
    on the same already-proven scope.no_forbidden_resources set-
    intersection mechanism but had no dedicated mutation test of their
    own. RBAC itself is not implemented here - these tests only prove it
    remains forbidden during Day 2."""

    _RBAC_FIXTURES = {
        "Role": {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "maops-role", "namespace": "maops-platform"},
            "rules": [],
        },
        "RoleBinding": {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "maops-role-binding", "namespace": "maops-platform"},
            "subjects": [],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "maops-role"},
        },
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

    def test_forbidden_rbac_kinds_each_fail(self):
        for kind, fixture in self._RBAC_FIXTURES.items():
            with self.subTest(kind=kind):
                docs = copy.deepcopy(_base_docs())
                docs.append(copy.deepcopy(fixture))
                failed = _failed_names(run_checks(docs))
                self.assertIn("scope.no_forbidden_resources", failed)


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
        dep["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "0.3.0"
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


if __name__ == "__main__":
    unittest.main()

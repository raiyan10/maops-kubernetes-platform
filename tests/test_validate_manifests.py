"""
Docker-free unit tests for the repository-owned static validation logic
in scripts/validate_manifests.py.

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


def _base_docs() -> list[dict]:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "maops-platform"},
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "maops-app-config", "namespace": "maops-platform"},
        "data": {
            "APP_NAME": "maops-kubernetes-platform",
            "APP_ENVIRONMENT": "day1-kubernetes-foundation",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Platform (Day 1)",
            "APP_LOG_LEVEL": "info",
        },
    }
    pod_labels = {
        "app.kubernetes.io/name": "maops-kubernetes-platform",
        "app.kubernetes.io/instance": "maops-kubernetes-platform-day1",
        "app.kubernetes.io/component": "app",
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "maops-app", "namespace": "maops-platform"},
        "spec": {
            "replicas": 2,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "maops-kubernetes-platform",
                    "app.kubernetes.io/instance": "maops-kubernetes-platform-day1",
                }
            },
            "template": {
                "metadata": {"labels": dict(pod_labels)},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "maops-app",
                            "image": "maops-kubernetes-platform:0.1.0",
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                            "envFrom": [{"configMapRef": {"name": "maops-app-config"}}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                            "startupProbe": {"httpGet": {"path": "/livez", "port": "http"}},
                            "livenessProbe": {"httpGet": {"path": "/livez", "port": "http"}},
                            "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "maops-app", "namespace": "maops-platform"},
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": "maops-kubernetes-platform",
                "app.kubernetes.io/instance": "maops-kubernetes-platform-day1",
            },
            "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
        },
    }
    return [namespace, configmap, deployment, service]


def _find(docs, kind):
    return next(d for d in docs if d.get("kind") == kind)


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


class BaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_checks(_base_docs())
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 38)


class NegativeCaseTests(unittest.TestCase):
    def test_replicas_changed_to_one_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment")["spec"]["replicas"] = 1
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.replicas", failed)

    def test_service_changed_to_nodeport_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service")["spec"]["type"] = "NodePort"
        _find(docs, "Service")["spec"]["ports"][0]["nodePort"] = 30080
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.type_cluster_ip", failed)
        self.assertIn("service.no_node_port", failed)

    def test_service_selector_mismatch_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service")["spec"]["selector"]["app.kubernetes.io/instance"] = "wrong-instance"
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.selector_matches_pod_labels", failed)

    def test_readiness_probe_removed_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("probes.readiness_path", failed)

    def test_liveness_probe_wrong_path_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["livenessProbe"]["httpGet"]["path"] = "/healthz"
        failed = _failed_names(run_checks(docs))
        self.assertIn("probes.liveness_path", failed)

    def test_resource_request_changed_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["resources"]["requests"]["cpu"] = "100m"
        failed = _failed_names(run_checks(docs))
        self.assertIn("resources.requests", failed)

    def test_resource_limit_changed_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["resources"]["limits"]["memory"] = "256Mi"
        failed = _failed_names(run_checks(docs))
        self.assertIn("resources.limits", failed)

    def test_run_as_non_root_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["securityContext"]["runAsNonRoot"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.pod_run_as_non_root", failed)

    def test_read_only_root_filesystem_false_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["securityContext"]["readOnlyRootFilesystem"] = False
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.read_only_root_filesystem", failed)

    def test_capabilities_not_drop_all_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["securityContext"]["capabilities"] = {"drop": ["NET_RAW"]}
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.capabilities_drop_all", failed)

    def test_automount_service_account_token_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["automountServiceAccountToken"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.automount_service_account_token", failed)

    def test_host_network_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["hostNetwork"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.no_host_network", failed)

    def test_host_port_set_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["ports"][0]["hostPort"] = 8080
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.no_host_port", failed)

    def test_configmap_wiring_removed_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = []
        failed = _failed_names(run_checks(docs))
        self.assertIn("configmap.wired_to_container", failed)

    def test_image_pull_policy_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["imagePullPolicy"] = "Always"
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.image_pull_policy", failed)

    def test_wrong_image_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["image"] = "maops-kubernetes-platform:latest"
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.image", failed)

    def test_second_deployment_fails_count(self):
        docs = copy.deepcopy(_base_docs())
        extra = copy.deepcopy(_find(docs, "Deployment"))
        extra["metadata"]["name"] = "maops-app-extra"
        docs.append(extra)
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.count", failed)

    def test_forbidden_ingress_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": "maops-app", "namespace": "maops-platform"},
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

    def test_forbidden_secret_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "maops-app-secret", "namespace": "maops-platform"},
                "data": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)

    def test_secret_like_configmap_key_fails(self):
        docs = copy.deepcopy(_base_docs())
        cm = _find(docs, "ConfigMap")
        cm["data"]["APP_API_TOKEN"] = "should-not-be-here"
        failed = _failed_names(run_checks(docs))
        self.assertIn("configmap.no_secret_like_values", failed)

    # -- DAY1-TEST-H1: namespace cross-checks on every namespaced resource --

    def test_configmap_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "ConfigMap")["metadata"]["namespace"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("configmap.namespace_matches", failed)

    def test_configmap_namespace_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _find(docs, "ConfigMap")["metadata"]["namespace"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("configmap.namespace_matches", failed)

    def test_deployment_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Deployment")["metadata"]["namespace"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.namespace_matches", failed)

    def test_deployment_namespace_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _find(docs, "Deployment")["metadata"]["namespace"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.namespace_matches", failed)

    def test_service_namespace_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service")["metadata"]["namespace"] = "other-namespace"
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.namespace_matches", failed)

    def test_service_namespace_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _find(docs, "Service")["metadata"]["namespace"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.namespace_matches", failed)

    # -- DAY1-TEST-H2: negative tests for previously-untested existing checks --

    def test_allow_privilege_escalation_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["securityContext"]["allowPrivilegeEscalation"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.allow_privilege_escalation", failed)

    def test_pod_run_as_user_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["securityContext"]["runAsUser"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.pod_run_as_user", failed)

    def test_pod_run_as_group_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["securityContext"]["runAsGroup"] = 0
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.pod_run_as_group", failed)

    def test_seccomp_profile_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["securityContext"]["seccompProfile"] = {"type": "Unconfined"}
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.seccomp_profile", failed)

    def test_startup_probe_missing_fails(self):
        docs = copy.deepcopy(_base_docs())
        del _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("probes.startup_present", failed)

    def test_namespace_object_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("kind") != "Namespace"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("namespace.exists", failed)

    def test_namespace_object_wrong_name_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Namespace")["metadata"]["name"] = "default"
        failed = _failed_names(run_checks(docs))
        self.assertIn("namespace.exists", failed)

    def test_configmap_object_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("kind") != "ConfigMap"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("configmap.exists", failed)

    def test_deployment_second_container_fails_single_container(self):
        docs = copy.deepcopy(_base_docs())
        containers = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"]
        containers.append(copy.deepcopy(containers[0]))
        containers[1]["name"] = "sidecar"
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.single_container", failed)

    def test_deployment_container_name_wrong_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["name"] = "wrong-name"
        failed = _failed_names(run_checks(docs))
        self.assertIn("deployment.container_name", failed)

    def test_service_object_missing_fails(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("kind") != "Service"]
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.exists", failed)

    def test_service_object_wrong_name_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "Service")["metadata"]["name"] = "not-maops-app"
        failed = _failed_names(run_checks(docs))
        self.assertIn("service.exists", failed)

    # -- DAY1-SEC-M1: host-boundary security checks --

    def test_host_pid_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["hostPID"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.no_host_pid", failed)

    def test_host_ipc_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["hostIPC"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.no_host_ipc", failed)

    def test_privileged_true_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["securityContext"]["privileged"] = True
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.not_privileged", failed)

    def test_hostpath_volume_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["volumes"] = [{"name": "host-tmp", "hostPath": {"path": "/tmp"}}]
        failed = _failed_names(run_checks(docs))
        self.assertIn("security.no_host_path_volumes", failed)

    def test_harmless_emptydir_volume_does_not_fail_hostpath_check(self):
        docs = copy.deepcopy(_base_docs())
        pod_spec = _find(docs, "Deployment")["spec"]["template"]["spec"]
        pod_spec["volumes"] = [{"name": "scratch", "emptyDir": {}}]
        failed = _failed_names(run_checks(docs))
        self.assertNotIn("security.no_host_path_volumes", failed)

    # -- DAY1-TEST-L1: startup probe must target the exact expected path --

    def test_startup_probe_wrong_path_fails(self):
        docs = copy.deepcopy(_base_docs())
        container = _find(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
        container["startupProbe"]["httpGet"]["path"] = "/wrong-path"
        failed = _failed_names(run_checks(docs))
        self.assertIn("probes.startup_path", failed)

    # -- DAY1-TEST-I3: one representative forbidden-kind negative case beyond
    # Ingress/PVC/Secret, to document that later-day kinds are rejected too --

    def test_forbidden_networkpolicy_present_fails(self):
        docs = copy.deepcopy(_base_docs())
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "maops-app-deny-all", "namespace": "maops-platform"},
                "spec": {},
            }
        )
        failed = _failed_names(run_checks(docs))
        self.assertIn("scope.no_forbidden_resources", failed)


if __name__ == "__main__":
    unittest.main()

"""
Docker-free unit tests for scripts/validate_gateway_values_configmap.py -
the DAY6 remediation static check for
k8s/day6/gateway-values-configmap.yaml's Istio Gateway API
`infrastructure.parametersRef` schema.

Fixtures are constructed as raw YAML text strings (not dicts) since the
module under test is a line-scanner over the ConfigMap's literal block
scalars, not a structural parser - see its own module docstring for why.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_gateway_values_configmap import extract_data_blocks, run_checks

VALID_CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: maops-edge-gateway-values
  namespace: maops-ingress
data:
  deployment: |
    spec:
      replicas: 1
      template:
        spec:
          containers:
          - name: istio-proxy
            resources:
              requests:
                cpu: 50m
                memory: 64Mi
              limits:
                cpu: 200m
                memory: 128Mi
  service: |
    spec:
      type: NodePort
      ports:
      - name: http
        port: 80
        targetPort: 80
        nodePort: 30080
        protocol: TCP
  serviceAccount: |
    automountServiceAccountToken: false
"""

OLD_WRAPPER_CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: maops-edge-gateway-values
  namespace: maops-ingress
data:
  values.yaml: |
    replicaCount: 1
    service:
      type: NodePort
"""


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


class ExtractDataBlocksTests(unittest.TestCase):
    def test_extracts_all_three_blocks(self):
        blocks = extract_data_blocks(VALID_CONFIGMAP)
        self.assertEqual(set(blocks.keys()), {"deployment", "service", "serviceAccount"})

    def test_deployment_block_content(self):
        blocks = extract_data_blocks(VALID_CONFIGMAP)
        self.assertIn("replicas: 1", blocks["deployment"])
        self.assertIn("cpu: 50m", blocks["deployment"])

    def test_old_wrapper_extracted_under_its_own_key(self):
        blocks = extract_data_blocks(OLD_WRAPPER_CONFIGMAP)
        self.assertEqual(set(blocks.keys()), {"values.yaml"})


class BaselineTests(unittest.TestCase):
    def test_valid_configmap_passes_every_check(self):
        findings = run_checks(VALID_CONFIGMAP)
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 10)


class OldWrapperRejectionTests(unittest.TestCase):
    def test_values_yaml_wrapper_fails_key_exactness(self):
        findings = run_checks(OLD_WRAPPER_CONFIGMAP)
        failed = _failed_names(findings)
        self.assertIn("gateway_values.data_keys_exact", failed)

    def test_values_yaml_wrapper_fails_forbidden_key_check(self):
        findings = run_checks(OLD_WRAPPER_CONFIGMAP)
        failed = _failed_names(findings)
        self.assertIn("gateway_values.no_values_yaml_wrapper", failed)


class MissingKeyTests(unittest.TestCase):
    def test_missing_serviceaccount_key_fails(self):
        text = VALID_CONFIGMAP.replace(
            "  serviceAccount: |\n    automountServiceAccountToken: false\n", ""
        )
        findings = run_checks(text)
        failed = _failed_names(findings)
        self.assertIn("gateway_values.data_keys_exact", failed)


class DeploymentPatchTests(unittest.TestCase):
    def test_wrong_replica_count_fails(self):
        text = VALID_CONFIGMAP.replace("replicas: 1", "replicas: 3")
        findings = run_checks(text)
        self.assertIn("gateway_values.deployment.replicas_one", _failed_names(findings))

    def test_missing_resource_limit_fails(self):
        text = VALID_CONFIGMAP.replace("                memory: 64Mi\n", "")
        findings = run_checks(text)
        self.assertIn("gateway_values.deployment.resources[memory: 64Mi]", _failed_names(findings))

    def test_wrong_container_name_fails(self):
        text = VALID_CONFIGMAP.replace("name: istio-proxy", "name: sidecar")
        findings = run_checks(text)
        self.assertIn("gateway_values.deployment.targets_istio_proxy_container", _failed_names(findings))


class ServicePatchTests(unittest.TestCase):
    def test_wrong_service_type_fails(self):
        text = VALID_CONFIGMAP.replace("type: NodePort", "type: ClusterIP")
        findings = run_checks(text)
        self.assertIn("gateway_values.service.type_nodeport", _failed_names(findings))

    def test_wrong_nodeport_fails(self):
        text = VALID_CONFIGMAP.replace("nodePort: 30080", "nodePort: 31000")
        findings = run_checks(text)
        self.assertIn("gateway_values.service.nodeport_30080", _failed_names(findings))

    def test_wrong_target_port_fails(self):
        text = VALID_CONFIGMAP.replace("targetPort: 80", "targetPort: 8080")
        findings = run_checks(text)
        self.assertIn("gateway_values.service.targetport_80", _failed_names(findings))


class ServiceAccountPatchTests(unittest.TestCase):
    def test_missing_automount_false_fails(self):
        text = VALID_CONFIGMAP.replace(
            "  serviceAccount: |\n    automountServiceAccountToken: false\n",
            "  serviceAccount: |\n    labels:\n      foo: bar\n",
        )
        findings = run_checks(text)
        self.assertIn("gateway_values.serviceaccount.automount_false", _failed_names(findings))

    def test_name_override_attempt_fails(self):
        """DAY6 remediation: the serviceAccount patch must never try to
        rename the generated ServiceAccount - Istio's controller owns
        that name deterministically."""
        text = VALID_CONFIGMAP.replace(
            "  serviceAccount: |\n    automountServiceAccountToken: false\n",
            "  serviceAccount: |\n    name: maops-edge\n    automountServiceAccountToken: false\n",
        )
        findings = run_checks(text)
        self.assertIn("gateway_values.serviceaccount.no_name_override_attempted", _failed_names(findings))


if __name__ == "__main__":
    unittest.main()

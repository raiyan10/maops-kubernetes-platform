"""
Docker/Kubernetes-free unit tests for scripts/validate_cilium_probe_policy.py -
the DAY6 review SEC-3 static guard for
k8s/day6/cilium-ambient-probe-policy.yaml. Every negative fixture is a
single targeted mutation of the known-good policy text, and each test
asserts the SPECIFIC check that must catch it fails - never merely that
"something" failed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import validate_cilium_probe_policy
from validate_cilium_probe_policy import run_checks

REPO_POLICY_PATH = Path(__file__).resolve().parent.parent / "k8s" / "day6" / "cilium-ambient-probe-policy.yaml"

VALID_POLICY = """apiVersion: cilium.io/v2
kind: CiliumClusterwideNetworkPolicy
metadata:
  name: maops-allow-ambient-health-probes
  labels:
    app.kubernetes.io/part-of: maops-kubernetes-platform
spec:
  description: "Allow ztunnel SNAT'd (169.254.7.127, IPv4-only) kubelet probes to port 8080."
  endpointSelector:
    matchLabels:
      k8s:io.kubernetes.pod.namespace: maops-platform
      app.kubernetes.io/part-of: maops-kubernetes-platform
  ingress:
    - fromCIDR:
        - "169.254.7.127/32"
      toPorts:
        - ports:
            - port: "8080"
              protocol: TCP
"""


def _failed(text: str) -> set[str]:
    return {f.name for f in run_checks(text) if not f.ok}


class ValidPolicyTests(unittest.TestCase):
    def test_valid_fixture_passes_every_check(self):
        self.assertEqual(_failed(VALID_POLICY), set())

    def test_repository_policy_file_passes_every_check(self):
        """The real tracked manifest - the object `make mesh-install`
        applies - must itself pass."""
        findings = run_checks(REPO_POLICY_PATH.read_text())
        self.assertTrue(findings)
        self.assertEqual({f.name for f in findings if not f.ok}, set())

    def test_check_count_is_stable(self):
        self.assertEqual(len(run_checks(VALID_POLICY)), 13)


class IdentityMutationTests(unittest.TestCase):
    def test_wrong_api_version_fails(self):
        self.assertIn("cilium_probe_policy.api_version", _failed(VALID_POLICY.replace("cilium.io/v2", "cilium.io/v2alpha1")))

    def test_namespaced_kind_fails(self):
        self.assertIn("cilium_probe_policy.kind", _failed(VALID_POLICY.replace("kind: CiliumClusterwideNetworkPolicy", "kind: CiliumNetworkPolicy")))

    def test_wrong_name_fails(self):
        self.assertIn("cilium_probe_policy.name", _failed(VALID_POLICY.replace("name: maops-allow-ambient-health-probes", "name: allow-probes")))

    def test_second_document_fails(self):
        self.assertIn("cilium_probe_policy.single_document", _failed(VALID_POLICY + "---\n" + VALID_POLICY))

    def test_unparseable_input_fails_closed(self):
        self.assertIn("cilium_probe_policy.parse", _failed("spec:\n  ingress: [{fromCIDR: 0.0.0.0/0}]\n"))


class SelectorMutationTests(unittest.TestCase):
    def test_wildcard_endpoint_selector_fails(self):
        mutated = VALID_POLICY.replace(
            "  endpointSelector:\n    matchLabels:\n      k8s:io.kubernetes.pod.namespace: maops-platform\n      app.kubernetes.io/part-of: maops-kubernetes-platform\n",
            "  endpointSelector: {}\n",
        )
        self.assertIn("cilium_probe_policy.endpoint_selector_exact", _failed(mutated))

    def test_dropped_namespace_label_fails(self):
        mutated = VALID_POLICY.replace("      k8s:io.kubernetes.pod.namespace: maops-platform\n", "")
        self.assertIn("cilium_probe_policy.endpoint_selector_exact", _failed(mutated))

    def test_dropped_part_of_label_fails(self):
        mutated = VALID_POLICY.replace("      app.kubernetes.io/part-of: maops-kubernetes-platform\n  ingress:", "  ingress:")
        self.assertIn("cilium_probe_policy.endpoint_selector_exact", _failed(mutated))

    def test_other_namespace_fails(self):
        mutated = VALID_POLICY.replace("k8s:io.kubernetes.pod.namespace: maops-platform", "k8s:io.kubernetes.pod.namespace: kube-system")
        self.assertIn("cilium_probe_policy.endpoint_selector_exact", _failed(mutated))

    def test_extra_match_expressions_fails(self):
        mutated = VALID_POLICY.replace(
            "      app.kubernetes.io/part-of: maops-kubernetes-platform\n  ingress:",
            "      app.kubernetes.io/part-of: maops-kubernetes-platform\n    matchExpressions:\n      - key: tier\n        operator: Exists\n  ingress:",
        )
        self.assertIn("cilium_probe_policy.endpoint_selector_exact", _failed(mutated))


class RuleMutationTests(unittest.TestCase):
    def test_broader_cidr_fails(self):
        self.assertIn("cilium_probe_policy.source_cidr_exact", _failed(VALID_POLICY.replace("169.254.7.127/32", "169.254.0.0/16")))

    def test_any_source_cidr_fails(self):
        self.assertIn("cilium_probe_policy.source_cidr_exact", _failed(VALID_POLICY.replace("169.254.7.127/32", "0.0.0.0/0")))

    def test_additional_cidr_fails(self):
        mutated = VALID_POLICY.replace('        - "169.254.7.127/32"\n', '        - "169.254.7.127/32"\n        - "10.0.0.0/8"\n')
        self.assertIn("cilium_probe_policy.source_cidr_exact", _failed(mutated))

    def test_ipv6_cidr_fails_both_exact_and_family_checks(self):
        mutated = VALID_POLICY.replace('        - "169.254.7.127/32"\n', '        - "169.254.7.127/32"\n        - "fd00::1/128"\n')
        failed = _failed(mutated)
        self.assertIn("cilium_probe_policy.source_cidr_exact", failed)
        self.assertIn("cilium_probe_policy.ipv4_only_cidrs", failed)

    def test_udp_protocol_fails(self):
        self.assertIn("cilium_probe_policy.tcp_8080_only", _failed(VALID_POLICY.replace("protocol: TCP", "protocol: UDP")))

    def test_any_protocol_fails(self):
        self.assertIn("cilium_probe_policy.tcp_8080_only", _failed(VALID_POLICY.replace("protocol: TCP", "protocol: ANY")))

    def test_other_port_fails(self):
        self.assertIn("cilium_probe_policy.tcp_8080_only", _failed(VALID_POLICY.replace('port: "8080"', 'port: "9090"')))

    def test_extra_port_fails(self):
        mutated = VALID_POLICY.replace(
            '              protocol: TCP\n',
            '              protocol: TCP\n            - port: "15021"\n              protocol: TCP\n',
        )
        self.assertIn("cilium_probe_policy.tcp_8080_only", _failed(mutated))

    def test_l7_rules_fail(self):
        mutated = VALID_POLICY + "          rules:\n            http:\n              - method: GET\n"
        self.assertIn("cilium_probe_policy.to_ports_l4_only", _failed(mutated))

    def test_egress_rule_fails(self):
        mutated = VALID_POLICY + "  egress:\n    - toEntities:\n        - world\n"
        self.assertIn("cilium_probe_policy.spec_keys_exact", _failed(mutated))

    def test_ingress_deny_rule_fails(self):
        mutated = VALID_POLICY + "  ingressDeny:\n    - fromEntities:\n        - world\n"
        self.assertIn("cilium_probe_policy.spec_keys_exact", _failed(mutated))

    def test_extra_ingress_rule_fails(self):
        mutated = VALID_POLICY + "    - fromEntities:\n        - cluster\n"
        self.assertIn("cilium_probe_policy.single_ingress_rule", _failed(mutated))

    def test_extra_peer_type_in_the_single_rule_fails(self):
        mutated = VALID_POLICY.replace("    - fromCIDR:\n", "    - fromEntities:\n        - world\n      fromCIDR:\n")
        self.assertIn("cilium_probe_policy.ingress_rule_keys_exact", _failed(mutated))

    def test_missing_ipv4_only_description_fails(self):
        mutated = VALID_POLICY.replace(", IPv4-only", "")
        self.assertIn("cilium_probe_policy.ipv4_only_scope_documented", _failed(mutated))


class ClusterFreeTests(unittest.TestCase):
    def test_module_never_imports_subprocess_or_kube(self):
        """The validator must stay cluster-free (part of ci-check)."""
        source = Path(validate_cilium_probe_policy.__file__).read_text()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import kube\n", source)


if __name__ == "__main__":
    unittest.main()

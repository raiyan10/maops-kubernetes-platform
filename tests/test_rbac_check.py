"""
Docker/Kubernetes-free unit tests for scripts/rbac_check.py (DAY5-TEST-H1
remediation).

rbac_check.py is dominated by real live-cluster interaction by
necessity (it proves RBAC scope with a real mounted ServiceAccount
token making real API server calls), but `_parse_cases()` (the probe
Pod's log-line parser) and `_pod_manifest()` (the probe Pod's identity/
RBAC wiring) are pure, cleanly separable logic that had no fast test
coverage before this file - exactly the kind of regression main()'s own
`_EXPECTED` comparison table depends on being correct.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import rbac_check


class ParseCasesTests(unittest.TestCase):
    def test_parses_well_formed_lines(self):
        logs = "CASE=pods_read_allowed STATUS=200\nCASE=secrets_read_denied STATUS=403\n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {"pods_read_allowed": 200, "secrets_read_denied": 403})

    def test_ignores_non_case_lines(self):
        logs = "some banner text\nCASE=pods_read_allowed STATUS=200\nanother stray line\n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {"pods_read_allowed": 200})

    def test_ignores_malformed_case_line_missing_status(self):
        logs = "CASE=pods_read_allowed\nCASE=secrets_read_denied STATUS=403\n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {"secrets_read_denied": 403})

    def test_ignores_case_line_with_non_integer_status(self):
        logs = "CASE=pods_read_allowed STATUS=not-a-number\n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {})

    def test_empty_logs_returns_empty_dict(self):
        self.assertEqual(rbac_check._parse_cases(""), {})

    def test_strips_whitespace_around_lines(self):
        logs = "   CASE=pods_read_allowed STATUS=200   \n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {"pods_read_allowed": 200})

    def test_later_duplicate_case_name_overwrites_earlier(self):
        logs = "CASE=pods_read_allowed STATUS=500\nCASE=pods_read_allowed STATUS=200\n"
        observed = rbac_check._parse_cases(logs)
        self.assertEqual(observed, {"pods_read_allowed": 200})


class PodManifestTests(unittest.TestCase):
    """A copy-paste regression that dropped the diagnostics
    ServiceAccount wiring would silently make the probe run as
    `default` instead of `maops-diagnostics`, invalidating the entire
    RBAC proof without main() itself ever noticing - it would just
    observe unexpected 403s and report a (misleadingly generic)
    failure, not the actual root cause."""

    def test_carries_diagnostics_service_account(self):
        manifest = rbac_check._pod_manifest()
        self.assertIn(f"serviceAccountName: {rbac_check.DIAGNOSTICS_SERVICE_ACCOUNT}", manifest)

    def test_carries_automount_true(self):
        manifest = rbac_check._pod_manifest()
        self.assertIn("automountServiceAccountToken: true", manifest)

    def test_targets_validation_namespace(self):
        manifest = rbac_check._pod_manifest()
        self.assertIn(f"namespace: {rbac_check.VALIDATION_NAMESPACE}", manifest)

    def test_embeds_all_probe_cases(self):
        """Every case name main()'s _EXPECTED table checks for must
        actually be emitted by the embedded probe script - otherwise
        `_parse_cases()` would legitimately observe fewer cases than
        expected and main()'s own `len(observed) == len(_EXPECTED)`
        check would (correctly) fail, but only ever live."""
        manifest = rbac_check._pod_manifest()
        for case_name, _expected_status, _description in rbac_check._EXPECTED:
            self.assertIn(case_name, manifest)

    def test_generated_probe_source_is_syntactically_valid_python(self):
        compile(rbac_check._PROBE, "<probe>", "exec")


if __name__ == "__main__":
    unittest.main()

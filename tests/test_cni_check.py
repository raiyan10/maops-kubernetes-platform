"""
Docker/Kubernetes-free unit tests for scripts/cni_check.py (DAY5-TEST-H1
remediation).

`cni_check.py` is dominated by real live-cluster interaction by
necessity, but `_is_ready()` is pure, zero-cluster-argument logic that
every other check in the module depends on to decide Ready vs not
Ready - and was, until this file, completely untested. Directly tests
that function against constructed Pod dicts rather than mocking it
away.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cni_check


class IsReadyTests(unittest.TestCase):
    def test_ready_condition_true_is_ready(self):
        pod = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.assertTrue(cni_check._is_ready(pod))

    def test_ready_condition_false_is_not_ready(self):
        pod = {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}
        self.assertFalse(cni_check._is_ready(pod))

    def test_no_ready_condition_present_is_not_ready(self):
        pod = {"status": {"conditions": [{"type": "PodScheduled", "status": "True"}]}}
        self.assertFalse(cni_check._is_ready(pod))

    def test_missing_conditions_key_is_not_ready(self):
        pod = {"status": {}}
        self.assertFalse(cni_check._is_ready(pod))

    def test_missing_status_key_is_not_ready(self):
        pod = {}
        self.assertFalse(cni_check._is_ready(pod))

    def test_ready_true_among_other_conditions_is_ready(self):
        pod = {
            "status": {
                "conditions": [
                    {"type": "Initialized", "status": "True"},
                    {"type": "Ready", "status": "True"},
                    {"type": "ContainersReady", "status": "True"},
                ]
            }
        }
        self.assertTrue(cni_check._is_ready(pod))

    def test_ready_status_string_must_be_exactly_true(self):
        """A truthy-but-wrong value (e.g. a stray "true" lowercase, which
        the real Kubernetes API never sends but a malformed/mocked
        response could) must not be treated as Ready - the check is an
        exact string comparison, not a bool() coercion."""
        pod = {"status": {"conditions": [{"type": "Ready", "status": "true"}]}}
        self.assertFalse(cni_check._is_ready(pod))


if __name__ == "__main__":
    unittest.main()

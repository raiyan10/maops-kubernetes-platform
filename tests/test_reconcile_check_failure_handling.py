"""
Docker/Kubernetes-free unit tests for scripts/reconcile_check.py:

- DAY1-TEST-L3: a plausible kubectl delete failure must be caught and
  routed into the script's normal record(False, ...)/exit-code
  reporting, not left as an unhandled traceback.
- DAY1-INT-I1: victim-pod selection must be a deterministic function of
  metadata.name, not incidental API list ordering.

Monkeypatches get_pods/run so these tests never touch a real cluster.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reconcile_check


class DeletePodFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        reconcile_check.results = []

    def test_delete_failure_is_recorded_cleanly_not_raised(self):
        pods = [
            {"metadata": {"name": "maops-app-bbb", "uid": "uid-b"}},
            {"metadata": {"name": "maops-app-aaa", "uid": "uid-a"}},
        ]
        exc = subprocess.CalledProcessError(1, ["kubectl", "delete"], stderr='pods "maops-app-aaa" not found')
        with mock.patch.object(reconcile_check, "get_pods", return_value=pods):
            with mock.patch.object(reconcile_check, "run", side_effect=exc):
                exit_code = reconcile_check.main()

        self.assertEqual(exit_code, 1)
        ok_flags = [ok for ok, _ in reconcile_check.results]
        self.assertIn(False, ok_flags)
        messages = " ".join(msg for _, msg in reconcile_check.results)
        self.assertIn("failed to delete pod", messages)
        self.assertIn("not found", messages)


class DeterministicVictimSelectionTests(unittest.TestCase):
    def setUp(self):
        reconcile_check.results = []

    def test_victim_is_the_pod_that_sorts_first_by_name(self):
        # Pods are returned out of name order; the deleted pod must be
        # whichever sorts first by metadata.name, not whichever the
        # (mocked) API happened to list first - proves the selection no
        # longer depends on incidental list ordering.
        pods = [
            {"metadata": {"name": "maops-app-zzz", "uid": "uid-z"}},
            {"metadata": {"name": "maops-app-aaa", "uid": "uid-a"}},
        ]
        deleted = {}

        def fake_run(*args, **_kwargs):
            if "delete" in args:
                deleted["victim"] = args[args.index("pod") + 1]
            raise subprocess.CalledProcessError(1, args, stderr="stop here for this test")

        with mock.patch.object(reconcile_check, "get_pods", return_value=pods):
            with mock.patch.object(reconcile_check, "run", side_effect=fake_run):
                reconcile_check.main()

        self.assertEqual(deleted.get("victim"), "maops-app-aaa")

    def test_victim_selection_stable_regardless_of_input_order(self):
        pods_order_a = [
            {"metadata": {"name": "maops-app-2", "uid": "uid-2"}},
            {"metadata": {"name": "maops-app-1", "uid": "uid-1"}},
        ]
        pods_order_b = list(reversed(pods_order_a))

        def victim_for(pods):
            captured = {}

            def fake_run(*args, **_kwargs):
                if "delete" in args:
                    captured["victim"] = args[args.index("pod") + 1]
                raise subprocess.CalledProcessError(1, args, stderr="stop here for this test")

            with mock.patch.object(reconcile_check, "get_pods", return_value=pods):
                with mock.patch.object(reconcile_check, "run", side_effect=fake_run):
                    reconcile_check.main()
            return captured.get("victim")

        self.assertEqual(victim_for(pods_order_a), victim_for(pods_order_b))


if __name__ == "__main__":
    unittest.main()

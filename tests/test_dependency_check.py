"""
Docker/Kubernetes-free unit tests for scripts/dependency_check.py's
restoration guarantee: maops-app must always be scaled back to 2
replicas, even when the scale-to-zero experiment itself fails/raises,
and a restoration failure must be reported distinctly (never hidden
behind the original failure).

Monkeypatches every kube/port-forward call so these tests never touch a
real cluster.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dependency_check


class RestorationGuaranteeTests(unittest.TestCase):
    def setUp(self):
        dependency_check.results = []
        dependency_check.restoration_results = []

    def test_restoration_invoked_when_experiment_raises(self):
        with mock.patch.object(dependency_check, "check_starting_state", return_value=[{"metadata": {"name": "gw-1"}}]):
            with mock.patch.object(dependency_check, "scale_app") as mock_scale:
                with mock.patch.object(dependency_check, "run_experiment", side_effect=RuntimeError("boom")):
                    with mock.patch.object(dependency_check, "restore_app", return_value=True) as mock_restore:
                        exit_code = dependency_check.main()

        mock_scale.assert_any_call(0)
        mock_restore.assert_called_once()
        self.assertEqual(exit_code, 1, "an experiment failure must still fail the run even though restoration succeeded")

    def test_restoration_not_attempted_when_scale_down_itself_never_happened(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "scale"], stderr="deployment not found")
        with mock.patch.object(dependency_check, "check_starting_state", return_value=[{"metadata": {"name": "gw-1"}}]):
            with mock.patch.object(dependency_check, "scale_app", side_effect=exc):
                with mock.patch.object(dependency_check, "restore_app") as mock_restore:
                    exit_code = dependency_check.main()

        mock_restore.assert_not_called()
        self.assertEqual(exit_code, 1)

    def test_restoration_failure_is_reported_prominently_and_fails_the_run(self):
        def failing_restore():
            dependency_check.record_restoration(False, "could not scale maops-app back to 2 replicas: simulated failure")
            return False

        with mock.patch.object(dependency_check, "check_starting_state", return_value=[{"metadata": {"name": "gw-1"}}]):
            with mock.patch.object(dependency_check, "scale_app"):
                with mock.patch.object(dependency_check, "run_experiment"):
                    with mock.patch.object(dependency_check, "restore_app", side_effect=failing_restore):
                        exit_code = dependency_check.main()

        self.assertEqual(exit_code, 1)
        self.assertTrue(
            any(not ok for ok, _msg in dependency_check.restoration_results),
            "a restoration failure must be recorded in restoration_results, not merged silently into the original result",
        )

    def test_restoration_success_and_experiment_success_together_pass(self):
        with mock.patch.object(dependency_check, "check_starting_state", return_value=[{"metadata": {"name": "gw-1"}}]):
            with mock.patch.object(dependency_check, "scale_app"):
                with mock.patch.object(dependency_check, "run_experiment"):
                    with mock.patch.object(dependency_check, "restore_app", return_value=True):
                        exit_code = dependency_check.main()

        self.assertEqual(exit_code, 0)


class AppEndpointsDrainedTests(unittest.TestCase):
    """DAY2-TEST-H1: regression coverage for the real termination-race
    predicate, dependency_check._app_endpoints_drained(). It must require
    BOTH the EndpointSlice ready count to be 0 AND no matching app Pods to
    remain - EndpointSlice-empty alone is NOT sufficient. These tests call
    the actual production function (module-level, not a closure) with its
    two collaborators (kube.get_json, cluster_check.get_pods, both
    imported as dependency_check module attributes) mocked - no
    reimplementation of the predicate's logic."""

    @staticmethod
    def _endpointslices_json(ready_addresses: list[str]) -> dict:
        return {
            "items": [
                {
                    "endpoints": [
                        {"addresses": [addr], "conditions": {"ready": True}} for addr in ready_addresses
                    ]
                }
            ]
        }

    def test_case_a_endpointslice_empty_but_pods_still_exist_not_drained(self):
        # This is the exact regression the review flagged: reverting to
        # "EndpointSlice empty alone means drained" would make this case
        # return True instead of None.
        with mock.patch.object(dependency_check, "get_json", return_value=self._endpointslices_json([])):
            with mock.patch.object(
                dependency_check, "get_pods", return_value=[{"metadata": {"name": "maops-app-still-here"}}]
            ) as mock_get_pods:
                result = dependency_check._app_endpoints_drained()
        self.assertIsNone(result, "must not report drained while a matching app Pod object still exists")
        mock_get_pods.assert_called_once()

    def test_case_b_endpointslice_ready_and_pods_exist_not_drained(self):
        with mock.patch.object(dependency_check, "get_json", return_value=self._endpointslices_json(["10.244.0.5"])):
            with mock.patch.object(
                dependency_check, "get_pods", return_value=[{"metadata": {"name": "maops-app-still-here"}}]
            ):
                result = dependency_check._app_endpoints_drained()
        self.assertIsNone(result)

    def test_case_c_endpointslice_empty_and_no_pods_is_drained(self):
        with mock.patch.object(dependency_check, "get_json", return_value=self._endpointslices_json([])):
            with mock.patch.object(dependency_check, "get_pods", return_value=[]) as mock_get_pods:
                result = dependency_check._app_endpoints_drained()
        self.assertTrue(result, "must report drained once both signals genuinely agree")
        mock_get_pods.assert_called_once()

    def test_case_d_endpointslice_query_failure_propagates_not_false_pass(self):
        # A query failure must never resolve to a truthy "drained" result -
        # it must propagate so wait_until's retry/timeout handling (not
        # this predicate) decides the outcome.
        with mock.patch.object(dependency_check, "get_json", side_effect=RuntimeError("kubectl get endpointslices failed")):
            with mock.patch.object(dependency_check, "get_pods") as mock_get_pods:
                with self.assertRaises(RuntimeError):
                    dependency_check._app_endpoints_drained()
        mock_get_pods.assert_not_called()


class RestartCountTests(unittest.TestCase):
    def test_sums_restart_counts_across_containers(self):
        pods = [
            {
                "metadata": {"name": "gw-1"},
                "status": {"containerStatuses": [{"restartCount": 0}]},
            },
            {
                "metadata": {"name": "gw-2"},
                "status": {"containerStatuses": [{"restartCount": 2}]},
            },
        ]
        counts = dependency_check.get_restart_counts(pods)
        self.assertEqual(counts, {"gw-1": 0, "gw-2": 2})

    def test_missing_container_statuses_defaults_to_zero(self):
        pods = [{"metadata": {"name": "gw-1"}, "status": {}}]
        counts = dependency_check.get_restart_counts(pods)
        self.assertEqual(counts, {"gw-1": 0})


if __name__ == "__main__":
    unittest.main()

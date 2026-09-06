"""
Docker/Kubernetes-free unit tests for scripts/scaling_check.py's
restoration guarantee: a workload must always be scaled back to 3
replicas once it has been scaled to 4, even when the scale-up
experiment itself fails/raises, and a restoration failure must be
reported distinctly (never hidden behind the original failure).

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

import scaling_check


class RestorationGuaranteeTests(unittest.TestCase):
    def setUp(self):
        scaling_check.results = []
        scaling_check.restoration_results = []

    def test_restoration_invoked_after_successful_scale_up_even_if_functional_check_raises(self):
        with mock.patch.object(scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 4}, "status": {"readyReplicas": 4}}):
            with mock.patch.object(scaling_check, "_wait_pods_ready", return_value=4):
                with mock.patch.object(scaling_check, "_wait_endpointslice_count", return_value=4):
                    with mock.patch.object(scaling_check, "scale_deployment") as mock_scale:
                        with mock.patch.object(scaling_check, "restore_workload", return_value=True) as mock_restore:

                            def boom():
                                raise RuntimeError("functional check exploded")

                            scaling_check.run_scaling_experiment("app", "maops-app", "maops-app", "sel", boom)

        mock_scale.assert_any_call("maops-app", scaling_check.SCALE_TARGET_REPLICAS)
        mock_restore.assert_called_once()

    def test_restoration_not_attempted_when_baseline_itself_never_reached(self):
        with mock.patch.object(scaling_check, "_wait_deployment_at", side_effect=TimeoutError("never ready")):
            with mock.patch.object(scaling_check, "restore_workload") as mock_restore:
                scaling_check.run_scaling_experiment("app", "maops-app", "maops-app", "sel", lambda: True)
        mock_restore.assert_not_called()

    def test_restoration_not_attempted_when_scale_up_command_itself_fails(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "scale"], stderr="deployment not found")
        with mock.patch.object(scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 3}, "status": {"readyReplicas": 3}}):
            with mock.patch.object(scaling_check, "_wait_pods_ready", return_value=3):
                with mock.patch.object(scaling_check, "_wait_endpointslice_count", return_value=3):
                    with mock.patch.object(scaling_check, "scale_deployment", side_effect=exc):
                        with mock.patch.object(scaling_check, "restore_workload") as mock_restore:
                            scaling_check.run_scaling_experiment("app", "maops-app", "maops-app", "sel", lambda: True)
        mock_restore.assert_not_called()

    def test_restoration_failure_is_reported_prominently(self):
        def failing_restore(*_a, **_kw):
            scaling_check.record_restoration(False, "could not scale maops-app back to 3 replicas: simulated failure")
            return False

        with mock.patch.object(scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 4}, "status": {"readyReplicas": 4}}):
            with mock.patch.object(scaling_check, "_wait_pods_ready", return_value=4):
                with mock.patch.object(scaling_check, "_wait_endpointslice_count", return_value=4):
                    with mock.patch.object(scaling_check, "scale_deployment"):
                        with mock.patch.object(scaling_check, "restore_workload", side_effect=failing_restore):
                            scaling_check.run_scaling_experiment("app", "maops-app", "maops-app", "sel", lambda: True)

        self.assertTrue(
            any(not ok for ok, _msg in scaling_check.restoration_results),
            "a restoration failure must be recorded in restoration_results, not merged silently into the original result",
        )


class RestoreWorkloadDirectTests(unittest.TestCase):
    """DAY3-TEST-H1: directly unit-tests the REAL
    scaling_check.restore_workload function (never replaced by a mock) -
    only its lower-level collaborators are mocked."""

    def setUp(self):
        scaling_check.results = []
        scaling_check.restoration_results = []

    def test_scale_command_called_process_error_returns_false(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "scale"], stderr="deployment not found")
        with mock.patch.object(scaling_check, "scale_deployment", side_effect=exc):
            ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "could not scale" in msg for r_ok, msg in scaling_check.restoration_results))

    def test_scale_command_timeout_expired_returns_false(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl", "scale"], timeout=30)
        with mock.patch.object(scaling_check, "scale_deployment", side_effect=exc):
            ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "could not scale" in msg for r_ok, msg in scaling_check.restoration_results))

    def test_deployment_convergence_timeout_returns_false(self):
        with mock.patch.object(scaling_check, "scale_deployment"):
            with mock.patch.object(scaling_check, "_wait_deployment_at", side_effect=TimeoutError("never reached 3/3")):
                ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "did not return to" in msg for r_ok, msg in scaling_check.restoration_results))

    def test_pods_ready_timeout_returns_false(self):
        with mock.patch.object(scaling_check, "scale_deployment"):
            with mock.patch.object(
                scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 3}, "status": {"readyReplicas": 3}}
            ):
                with mock.patch.object(scaling_check, "_wait_pods_ready", side_effect=TimeoutError("pods never ready")):
                    ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "did not return to" in msg for r_ok, msg in scaling_check.restoration_results))

    def test_endpointslice_convergence_timeout_returns_false(self):
        with mock.patch.object(scaling_check, "scale_deployment"):
            with mock.patch.object(
                scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 3}, "status": {"readyReplicas": 3}}
            ):
                with mock.patch.object(scaling_check, "_wait_pods_ready", return_value=3):
                    with mock.patch.object(scaling_check, "_wait_endpointslice_count", side_effect=TimeoutError("never settled")):
                        ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "EndpointSlice did not return to" in msg for r_ok, msg in scaling_check.restoration_results))

    def test_full_success_path_returns_true(self):
        with mock.patch.object(scaling_check, "scale_deployment"):
            with mock.patch.object(
                scaling_check, "_wait_deployment_at", return_value={"spec": {"replicas": 3}, "status": {"readyReplicas": 3}}
            ):
                with mock.patch.object(scaling_check, "_wait_pods_ready", return_value=3):
                    with mock.patch.object(scaling_check, "_wait_endpointslice_count", return_value=3):
                        ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertTrue(ok)
        self.assertTrue(all(r_ok for r_ok, _msg in scaling_check.restoration_results))

    def test_failure_return_is_not_overwritten_by_a_later_success_record(self):
        with mock.patch.object(scaling_check, "scale_deployment"):
            with mock.patch.object(scaling_check, "_wait_deployment_at", side_effect=TimeoutError("never reached 3/3")):
                with mock.patch.object(scaling_check, "_wait_pods_ready") as mock_pods_ready:
                    ok = scaling_check.restore_workload("app", "maops-app", "maops-app", "sel")
        self.assertFalse(ok)
        mock_pods_ready.assert_not_called()
        self.assertFalse(any(r_ok for r_ok, _msg in scaling_check.restoration_results))


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        scaling_check.results = []
        scaling_check.restoration_results = []

    def test_verify_context_failure_short_circuits_before_any_scaling(self):
        with mock.patch.object(scaling_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(scaling_check, "run_scaling_experiment") as mock_run:
                exit_code = scaling_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

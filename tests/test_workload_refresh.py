"""
Docker/Kubernetes-free unit tests for scripts/workload_refresh.py (Day 4
batch 3 remediation): the explicit `kubectl rollout restart` step that
guarantees maops-state/maops-app/maops-gateway actually run whatever
image this run's `image-load` just placed on the nodes.

Covers only the meaningful ordering/failure behavior, not a broad new
framework:
- the strict state -> app -> gateway restart order;
- a failed restart or a failed convergence wait on one workload stops
  the sequence before any later workload is touched;
- kube.verify_context() failing short-circuits before any restart, same
  pattern as every other Day 3/4 mutating script;
- a genuinely stuck rollout (nonzero `kubectl rollout status` exit) and
  a hung subprocess (TimeoutExpired) are both ordinary recorded
  failures, never an uncaught exception.

Monkeypatches `run`/`kube.verify_context` so these tests never touch a
real cluster.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import workload_refresh


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


class RestartOrderTests(unittest.TestCase):
    def setUp(self):
        workload_refresh.results = []

    def test_restarts_state_then_app_then_gateway_in_order(self):
        calls: list[tuple] = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if "restart" in args:
                return _completed(0)
            if "status" in args:
                return _completed(0, stdout="rolled out\n")
            raise AssertionError(f"unexpected kubectl invocation: {args}")

        with mock.patch.object(workload_refresh, "kube") as mock_kube, mock.patch.object(
            workload_refresh, "run", side_effect=fake_run
        ):
            mock_kube.subprocess_timeout_for.side_effect = lambda t: t + 30
            mock_kube.CONTEXT = "kind-maops-k8s-day4"
            mock_kube.verify_context.return_value = None
            rc = workload_refresh.main()

        self.assertEqual(rc, 0)
        restart_targets = [c for c in calls if "restart" in c]
        # Exactly one restart call per workload, in the dependency order
        # state -> app -> gateway - not merely "all three eventually
        # called somewhere."
        self.assertIn("statefulset/maops-state", restart_targets[0])
        self.assertIn("deployment/maops-app", restart_targets[1])
        self.assertIn("deployment/maops-gateway", restart_targets[2])
        self.assertEqual(len(restart_targets), 3)


class DependencyChainFailureStopsLaterWorkloadsTests(unittest.TestCase):
    def setUp(self):
        workload_refresh.results = []

    def test_state_restart_failure_stops_before_touching_app_or_gateway(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "rollout", "restart"], stderr="statefulsets.apps \"maops-state\" not found")
        with mock.patch.object(workload_refresh.kube, "verify_context"):
            with mock.patch.object(workload_refresh, "run", side_effect=exc) as mock_run:
                rc = workload_refresh.main()
        self.assertEqual(rc, 1)
        mock_run.assert_called_once()
        self.assertIn("statefulset/maops-state", mock_run.call_args.args)

    def test_state_convergence_failure_stops_before_touching_app_or_gateway(self):
        def fake_run(*args, **kwargs):
            if "restart" in args:
                return _completed(0)
            # state's own rollout status: genuinely stuck.
            return _completed(1, stdout="Waiting for partitioned roll out to finish...\n", stderr="error: timed out waiting for the condition\n")

        with mock.patch.object(workload_refresh.kube, "verify_context"):
            with mock.patch.object(workload_refresh, "run", side_effect=fake_run) as mock_run:
                rc = workload_refresh.main()

        self.assertEqual(rc, 1)
        # Exactly restart(state) + status(state) - never reaches app/gateway.
        calls_with_targets = [c.args for c in mock_run.call_args_list]
        self.assertTrue(all("maops-state" in " ".join(c) for c in calls_with_targets))
        self.assertEqual(len(calls_with_targets), 2)

    def test_app_failure_stops_before_touching_gateway_but_state_already_succeeded(self):
        call_log: list[str] = []

        def fake_run(*args, **kwargs):
            verb = args[3]  # "restart" or "status"
            target = args[4]  # e.g. "statefulset/maops-state"
            call_log.append(f"{verb}:{target}")
            if "maops-state" in target:
                return _completed(0, stdout="statefulset rolling update complete\n")
            if "maops-app" in target and verb == "restart":
                raise subprocess.TimeoutExpired(cmd=["kubectl", "rollout", "restart"], timeout=30)
            raise AssertionError(f"gateway must never be touched: {args}")

        with mock.patch.object(workload_refresh.kube, "verify_context"):
            with mock.patch.object(workload_refresh, "run", side_effect=fake_run):
                rc = workload_refresh.main()

        self.assertEqual(rc, 1)
        self.assertTrue(any("maops-state" in c for c in call_log))
        self.assertFalse(any("maops-gateway" in c for c in call_log), "gateway must never be restarted once app's refresh failed")

    def test_verify_context_failure_short_circuits_before_any_restart(self):
        with mock.patch.object(workload_refresh.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(workload_refresh, "run") as mock_run:
                rc = workload_refresh.main()
        self.assertEqual(rc, 1)
        mock_run.assert_not_called()


class WaitRolloutFailureModesTests(unittest.TestCase):
    def setUp(self):
        workload_refresh.results = []

    def test_stuck_rollout_is_a_clean_failure(self):
        with mock.patch.object(
            workload_refresh,
            "run",
            return_value=_completed(1, stdout="Waiting...\n", stderr="error: timed out waiting for the condition\n"),
        ):
            ok = workload_refresh.wait_rollout("deployment", "maops-app", timeout_seconds=5)
        self.assertFalse(ok)
        ok_result, msg = workload_refresh.results[-1]
        self.assertFalse(ok_result)
        self.assertIn("timed out waiting for the condition", msg)

    def test_hung_subprocess_timeout_is_a_clean_failure_not_a_traceback(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl", "rollout", "status"], timeout=150)
        with mock.patch.object(workload_refresh, "run", side_effect=exc):
            ok = workload_refresh.wait_rollout("statefulset", "maops-state", timeout_seconds=120)
        self.assertFalse(ok)
        ok_result, msg = workload_refresh.results[-1]
        self.assertFalse(ok_result)
        self.assertIn("subprocess timed out", msg)

    def test_successful_rollout_uses_bounded_subprocess_timeout(self):
        import kube as kube_module

        with mock.patch.object(
            workload_refresh, "run", return_value=_completed(0, stdout="deployment successfully rolled out\n")
        ) as mock_run:
            ok = workload_refresh.wait_rollout("deployment", "maops-gateway", timeout_seconds=90)
        self.assertTrue(ok)
        called_timeout = mock_run.call_args.kwargs["timeout"]
        self.assertEqual(called_timeout, kube_module.subprocess_timeout_for(90))
        self.assertGreater(called_timeout, 90)


if __name__ == "__main__":
    unittest.main()

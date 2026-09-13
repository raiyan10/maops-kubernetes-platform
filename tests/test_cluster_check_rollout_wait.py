"""
Docker/Kubernetes-free unit tests for scripts/cluster_check.py's DAY4
rollout-completion wait (Day 4 batch 3 remediation).

Batch 3's live day4-check run showed `wait_for_deployment_available()`
returning a Deployment snapshot with `Available=True` while the
Deployment was still genuinely mid-rollout (readyReplicas=2, 3/5 Pods
live) - `Available` is a condition Kubernetes can set for a subset of
replicas, not a "this specific rollout has finished" signal. These
tests prove the fix:

- `wait_for_rollout_complete()` uses the generation-aware `kubectl
  rollout status`, bounded via `kube.subprocess_timeout_for()` (never a
  fixed sleep), and converts both a stuck/progress-deadline rollout AND
  a hung-subprocess timeout into an ordinary `(False, detail)` result
  rather than an uncaught exception or a silent pass.
- `wait_for_stable_pod_count()` settles out old terminating Pods before
  handing a Pod list to the strict count/readiness assertions, and on
  timeout returns the real (still-wrong) last-observed list rather than
  raising - so the caller's existing strict assertion still runs and
  correctly fails.

Monkeypatches `run`/`get_pods` so these tests never touch a real
cluster; the FakeClock (mirroring tests/test_kube.py's `_FakeClock`)
avoids any real wall-clock sleep in the pod-count-settle poll.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cluster_check
import kube


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _patched_clock(clock: _FakeClock):
    return mock.patch.multiple(kube.time, monotonic=clock.monotonic, sleep=clock.sleep)


class WaitForRolloutCompleteTests(unittest.TestCase):
    def test_successful_convergence_reports_ok(self):
        with mock.patch.object(
            cluster_check, "run", return_value=_completed(0, stdout='deployment "maops-gateway" successfully rolled out\n')
        ) as mock_run:
            ok, detail = cluster_check.wait_for_rollout_complete("maops-gateway", timeout_seconds=90)
        self.assertTrue(ok)
        self.assertIn("successfully rolled out", detail)
        # Bounded via the shared, already-tested kube helper - never a
        # bespoke or fixed subprocess timeout - and always strictly
        # greater than the Kubernetes-side --timeout it was given
        # (DAY3-INT-H2 pattern).
        called_timeout = mock_run.call_args.kwargs["timeout"]
        self.assertEqual(called_timeout, kube.subprocess_timeout_for(90))
        self.assertGreater(called_timeout, 90)
        self.assertIn("--timeout=90s", mock_run.call_args.args)
        self.assertIn("rollout", mock_run.call_args.args)
        self.assertIn("status", mock_run.call_args.args)

    def test_available_true_but_incomplete_rollout_is_reported_as_failure(self):
        """The exact batch-3 scenario: the Deployment's Available
        condition can already be True while `kubectl rollout status`
        still correctly reports the CURRENT rollout as unfinished (a
        stuck or merely-still-converging generation). This must surface
        as an ordinary FAIL from this function - the defect batch 3 found
        was precisely that nothing upstream of the strict assertions ever
        checked this and caught it before those assertions ran."""
        stuck_output = 'Waiting for deployment "maops-gateway" rollout to finish: 2 of 3 updated replicas are available...\n'
        with mock.patch.object(
            cluster_check,
            "run",
            return_value=_completed(1, stdout=stuck_output, stderr="error: timed out waiting for the condition\n"),
        ):
            ok, detail = cluster_check.wait_for_rollout_complete("maops-gateway", timeout_seconds=5)
        self.assertFalse(ok)
        self.assertIn("timed out waiting for the condition", detail)

    def test_hung_subprocess_timeout_is_a_clean_failure_not_a_traceback(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl", "rollout", "status"], timeout=150)
        with mock.patch.object(cluster_check, "run", side_effect=exc):
            ok, detail = cluster_check.wait_for_rollout_complete("maops-app", timeout_seconds=120)
        self.assertFalse(ok)
        self.assertIn("subprocess timed out", detail)


class WaitForStablePodCountTests(unittest.TestCase):
    def test_settles_immediately_when_already_exact(self):
        pods = [{"metadata": {"name": f"gw-{i}"}} for i in range(3)]
        with mock.patch.object(cluster_check, "get_pods", return_value=pods) as mock_get_pods:
            result = cluster_check.wait_for_stable_pod_count("sel", 3, timeout=10)
        self.assertEqual(result, pods)
        mock_get_pods.assert_called_once()

    def test_settles_after_old_terminating_pods_drain(self):
        stale_plus_new = [{"metadata": {"name": f"gw-old-{i}"}} for i in range(2)] + [
            {"metadata": {"name": f"gw-new-{i}"}} for i in range(3)
        ]
        settled = [{"metadata": {"name": f"gw-new-{i}"}} for i in range(3)]
        responses = iter([stale_plus_new, stale_plus_new, settled])
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(cluster_check, "get_pods", side_effect=lambda *_a, **_kw: next(responses)):
                result = cluster_check.wait_for_stable_pod_count("sel", 3, timeout=30)
        self.assertEqual(result, settled)

    def test_never_settling_times_out_and_returns_last_observed_pods_not_an_exception(self):
        stale_plus_new = [{"metadata": {"name": f"gw-old-{i}"}} for i in range(2)] + [
            {"metadata": {"name": f"gw-new-{i}"}} for i in range(3)
        ]
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(cluster_check, "get_pods", return_value=stale_plus_new):
                result = cluster_check.wait_for_stable_pod_count("sel", 3, timeout=10)
        # Never raises - hands back the real, current (still-wrong) Pod
        # list so the caller's strict len(pods)==count assertion still
        # runs and correctly FAILs against real data, rather than the
        # whole check crashing with an uncaught TimeoutError.
        self.assertEqual(result, stale_plus_new)
        self.assertEqual(len(result), 5)

    def test_get_pods_failing_on_every_poll_returns_empty_list_not_an_exception(self):
        """Regression for the reviewed must-fix: the original
        implementation's post-timeout fallback issued a brand-new,
        unguarded get_pods() call after wait_until() gave up - if THAT
        call also raised, it propagated straight out of
        wait_for_stable_pod_count() uncaught. The fix captures the last
        SUCCESSFUL observation from inside the predicate instead of ever
        calling get_pods() a second time outside wait_until()'s own
        exception handling. Here get_pods() never succeeds even once, so
        there is no observation to fall back to - the function must still
        return (an empty list), never raise."""
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(cluster_check, "get_pods", side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="etcdserver: request timed out")):
                result = cluster_check.wait_for_stable_pod_count("sel", 3, timeout=10)
        self.assertEqual(result, [])

    def test_get_pods_failing_only_on_the_final_poll_returns_last_good_observation(self):
        """Same must-fix, opposite edge: get_pods() succeeds (with the
        wrong count) on earlier polls, then raises on what would have
        been the call immediately following the deadline. The last good
        (still-wrong) observation must be returned, not a crash from a
        fresh unguarded call."""
        stale_plus_new = [{"metadata": {"name": f"gw-old-{i}"}} for i in range(2)] + [
            {"metadata": {"name": f"gw-new-{i}"}} for i in range(3)
        ]

        def flaky_get_pods(*_a, **_kw):
            # Every call from inside wait_until's polling loop raises
            # AFTER the one success below - wait_until swallows these and
            # retries until the deadline; wait_for_stable_pod_count must
            # not issue any further unguarded call once wait_until gives up.
            if not flaky_get_pods.called:
                flaky_get_pods.called = True
                return stale_plus_new
            raise subprocess.CalledProcessError(1, ["kubectl"], stderr="etcdserver: request timed out")

        flaky_get_pods.called = False
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(cluster_check, "get_pods", side_effect=flaky_get_pods):
                result = cluster_check.wait_for_stable_pod_count("sel", 3, timeout=10)
        self.assertEqual(result, stale_plus_new)


class MainLoopRereadsDeploymentAfterRolloutTests(unittest.TestCase):
    """Batch-3's actual regression: main() must judge the strict replica
    assertion against a Deployment snapshot taken AFTER rollout
    convergence, not the earlier wait_for_deployment_available() snapshot
    (which can be Available=True while genuinely mid-rollout). Exercises
    the real per-workload body inside main() by patching only the
    external collaborator boundaries, for exactly the gateway workload
    batch 3's real run actually failed on."""

    def setUp(self):
        cluster_check.results = []

    def test_replica_check_receives_fresh_post_rollout_snapshot(self):
        stale_available_dep = {
            "spec": {"replicas": 3},
            "status": {"readyReplicas": 2, "conditions": [{"type": "Available", "status": "True"}]},
        }
        fresh_converged_dep = {"spec": {"replicas": 3}, "status": {"readyReplicas": 3}}
        seen_deps: list[dict] = []

        def fake_check_replica_counts(deployment, dep):
            seen_deps.append(dep)

        with mock.patch.object(cluster_check, "check_cluster_ready"), mock.patch.object(
            cluster_check, "check_server_version"
        ), mock.patch.object(cluster_check, "check_namespace"), mock.patch.object(
            cluster_check.kube, "verify_context"
        ), mock.patch.object(
            cluster_check, "wait_for_deployment_available", return_value=stale_available_dep
        ), mock.patch.object(
            cluster_check, "wait_for_rollout_complete", return_value=(True, "successfully rolled out")
        ), mock.patch.object(
            cluster_check, "get_json", return_value=fresh_converged_dep
        ), mock.patch.object(
            cluster_check, "check_replica_counts", side_effect=fake_check_replica_counts
        ), mock.patch.object(
            cluster_check, "wait_for_stable_pod_count", return_value=[]
        ), mock.patch.object(
            cluster_check, "check_pods_ready"
        ), mock.patch.object(
            cluster_check, "check_service_type"
        ), mock.patch.object(
            cluster_check, "check_endpointslice"
        ), mock.patch.object(
            cluster_check, "check_configmap_consumption"
        ), mock.patch.object(
            cluster_check, "check_runtime_uid_gid"
        ), mock.patch.object(
            cluster_check, "check_runtime_security_context"
        ), mock.patch.object(
            cluster_check, "check_no_service_account_token_mount"
        ), mock.patch.object(
            cluster_check, "check_secret_volume_mount"
        ):
            cluster_check.main()

        # Two workloads (gateway, app) -> two check_replica_counts calls,
        # each against the freshly re-fetched, post-convergence snapshot -
        # never the earlier Available=True-but-mid-rollout one.
        self.assertEqual(len(seen_deps), 2)
        for dep in seen_deps:
            self.assertIs(dep, fresh_converged_dep)
            self.assertIsNot(dep, stale_available_dep)


if __name__ == "__main__":
    unittest.main()

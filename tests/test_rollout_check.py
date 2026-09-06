"""
Docker/Kubernetes-free unit tests for scripts/rollout_check.py:

- the temporary Pod-template annotation is only ever a harmless,
  uniquely-marked non-secret value under the maops.io/ namespace;
- rollback is always attempted once the annotation patch has actually
  been applied, even if the rollout-observation phase raises;
- a rollback/restoration failure is reported distinctly, never merged
  into the original experiment's result;
- the eviction-adjacent "Pods actually replaced" check is a real set
  comparison, not a length-only check.

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

import rollout_check


def _pod(name: str, uid: str) -> dict:
    return {"metadata": {"name": name, "uid": uid}}


class AnnotationMarkerTests(unittest.TestCase):
    def test_marker_is_not_secret_looking_and_namespaced(self):
        # Sanity check on the annotation key convention itself.
        self.assertTrue(rollout_check.ANNOTATION_KEY.startswith("maops.io/"))
        self.assertNotIn("token", rollout_check.ANNOTATION_KEY.lower())
        self.assertNotIn("secret", rollout_check.ANNOTATION_KEY.lower())


class RestorationGuaranteeTests(unittest.TestCase):
    def setUp(self):
        rollout_check.results = []
        rollout_check.restoration_results = []

    def test_rollback_invoked_when_rollout_observation_raises(self):
        with mock.patch.object(rollout_check, "_wait_ready", return_value={"status": {"conditions": []}}):
            with mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]):
                with mock.patch.object(rollout_check, "replicaset_uids", side_effect=[set(), {"rs-2"}]):
                    with mock.patch.object(rollout_check, "get_image", return_value="maops-kubernetes-gateway:0.3.0"):
                        with mock.patch.object(rollout_check, "patch_rollout_annotation"):
                            with mock.patch.object(rollout_check, "wait_rollout_status", side_effect=RuntimeError("boom")):
                                with mock.patch.object(rollout_check, "rollback_workload", return_value=True) as mock_rollback:
                                    rollout_check.run_rollout_experiment(
                                        "gateway", "maops-gateway", "maops-gateway", "sel", lambda: True
                                    )
        mock_rollback.assert_called_once()

    def test_rollback_not_attempted_when_baseline_never_reached(self):
        with mock.patch.object(rollout_check, "_wait_ready", side_effect=TimeoutError("never ready")):
            with mock.patch.object(rollout_check, "rollback_workload") as mock_rollback:
                rollout_check.run_rollout_experiment("gateway", "maops-gateway", "maops-gateway", "sel", lambda: True)
        mock_rollback.assert_not_called()

    def test_rollback_not_attempted_when_patch_itself_fails(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "patch"], stderr="deployment not found")
        with mock.patch.object(rollout_check, "_wait_ready", return_value={"status": {"conditions": []}}):
            with mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]):
                with mock.patch.object(rollout_check, "replicaset_uids", return_value=set()):
                    with mock.patch.object(rollout_check, "get_image", return_value="x"):
                        with mock.patch.object(rollout_check, "patch_rollout_annotation", side_effect=exc):
                            with mock.patch.object(rollout_check, "rollback_workload") as mock_rollback:
                                rollout_check.run_rollout_experiment(
                                    "gateway", "maops-gateway", "maops-gateway", "sel", lambda: True
                                )
        mock_rollback.assert_not_called()

    def test_rollback_failure_reported_prominently(self):
        def failing_rollback(*_a, **_kw):
            rollout_check.record_restoration(False, "kubectl rollout undo failed: simulated failure")
            return False

        with mock.patch.object(rollout_check, "_wait_ready", return_value={"status": {"conditions": []}}):
            with mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]):
                with mock.patch.object(rollout_check, "replicaset_uids", side_effect=[set(), {"rs-2"}]):
                    with mock.patch.object(rollout_check, "get_image", return_value="x"):
                        with mock.patch.object(rollout_check, "patch_rollout_annotation"):
                            with mock.patch.object(rollout_check, "wait_rollout_status", return_value=(True, "ok")):
                                with mock.patch.object(rollout_check, "_wait_exact_pod_count", return_value={"uid-2"}):
                                    with mock.patch.object(rollout_check, "_wait_endpointslice_count", return_value=3):
                                        with mock.patch.object(rollout_check, "rollback_workload", side_effect=failing_rollback):
                                            rollout_check.run_rollout_experiment(
                                                "gateway", "maops-gateway", "maops-gateway", "sel", lambda: True
                                            )

        self.assertTrue(
            any(not ok for ok, _msg in rollout_check.restoration_results),
            "a rollback failure must be recorded in restoration_results, not merged silently into the original result",
        )


class PodsWereReplacedTests(unittest.TestCase):
    """DAY3-TEST-M2: directly exercises the real production predicate
    `rollout_check._pods_were_replaced()` - the same function the
    production rollout assertion calls - rather than merely re-testing
    Python's built-in `set.isdisjoint`."""

    def test_exact_count_and_fully_disjoint_is_a_real_replacement(self):
        baseline = {"uid-1", "uid-2", "uid-3"}
        after = {"uid-4", "uid-5", "uid-6"}
        self.assertTrue(rollout_check._pods_were_replaced(after, baseline, 3))

    def test_identical_sets_are_not_a_replacement(self):
        baseline = {"uid-1", "uid-2", "uid-3"}
        after = {"uid-1", "uid-2", "uid-3"}
        self.assertFalse(rollout_check._pods_were_replaced(after, baseline, 3))

    def test_partial_overlap_is_not_a_replacement(self):
        baseline = {"uid-1", "uid-2", "uid-3"}
        after = {"uid-1", "uid-4", "uid-5"}
        self.assertFalse(rollout_check._pods_were_replaced(after, baseline, 3))

    def test_too_few_pods_is_not_a_replacement(self):
        baseline = {"uid-1", "uid-2", "uid-3"}
        after = {"uid-4", "uid-5"}
        self.assertFalse(rollout_check._pods_were_replaced(after, baseline, 3))

    def test_too_many_pods_is_not_a_replacement(self):
        baseline = {"uid-1", "uid-2", "uid-3"}
        after = {"uid-4", "uid-5", "uid-6", "uid-7"}
        self.assertFalse(rollout_check._pods_were_replaced(after, baseline, 3))


class _FakePortForward:
    def __init__(self, local_port: int = 8080):
        self._local_port = local_port

    def __call__(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self._local_port

    def __exit__(self, *_exc):
        return False


class RollbackWorkloadDirectTests(unittest.TestCase):
    """DAY3-TEST-H1: directly unit-tests the REAL
    rollout_check.rollback_workload function (never replaced by a mock) -
    only its lower-level collaborators are mocked."""

    def setUp(self):
        rollout_check.results = []
        rollout_check.restoration_results = []
        self.base_patches = [
            mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]),
            mock.patch.object(rollout_check, "rollout_undo", return_value=(True, "ok")),
            mock.patch.object(rollout_check, "wait_rollout_status", return_value=(True, "ok")),
            mock.patch.object(
                rollout_check,
                "_wait_ready",
                return_value={"status": {"conditions": [{"type": "Available", "status": "True"}]}},
            ),
            mock.patch.object(rollout_check, "get_annotation", return_value=None),
            mock.patch.object(rollout_check, "get_image", return_value="maops-kubernetes-gateway:0.3.0"),
            mock.patch.object(rollout_check, "_wait_exact_pod_count", return_value={"uid-2", "uid-3", "uid-4"}),
            mock.patch.object(rollout_check, "_wait_endpointslice_count", return_value=3),
            mock.patch.object(rollout_check, "port_forward", _FakePortForward()),
            mock.patch.object(rollout_check, "check_endpoint", return_value=(True, "200 OK")),
        ]
        for p in self.base_patches:
            p.start()
            self.addCleanup(p.stop)

    def test_rollout_undo_called_process_error_returns_false(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "rollout", "undo"], stderr="deployment not found")
        with mock.patch.object(rollout_check, "rollout_undo", side_effect=exc):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "rollback command failed/timed out" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_rollout_undo_timeout_expired_returns_false(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl", "rollout", "undo"], timeout=30)
        with mock.patch.object(rollout_check, "rollout_undo", side_effect=exc):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "rollback command failed/timed out" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_deployment_convergence_timeout_returns_false(self):
        with mock.patch.object(rollout_check, "_wait_ready", side_effect=TimeoutError("never reached 3/3")):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "did not return to" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_pod_count_convergence_timeout_returns_false(self):
        with mock.patch.object(rollout_check, "_wait_exact_pod_count", side_effect=TimeoutError("never settled")):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "Pod count did not settle" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_endpointslice_convergence_timeout_returns_false(self):
        with mock.patch.object(rollout_check, "_wait_endpointslice_count", side_effect=TimeoutError("never settled")):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok and "EndpointSlice did not return to" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_rollback_annotation_still_present_fails_overall_despite_later_success(self):
        # DAY3-TEST-H1: a leftover annotation is a real restoration defect
        # - it must fail the overall result even though every later step
        # (image match, pod replacement, EndpointSlice, functional check)
        # succeeds.
        with mock.patch.object(rollout_check, "get_annotation", return_value="day3-leftover-marker"):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok, "a later success must never overwrite an earlier recorded restoration failure")
        self.assertTrue(any(not r_ok and "annotation absent after rollback" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_rollback_image_mismatch_fails_overall_despite_later_success(self):
        with mock.patch.object(rollout_check, "get_image", return_value="maops-kubernetes-gateway:0.2.0"):
            ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok, "a later success must never overwrite an earlier recorded restoration failure")
        self.assertTrue(any(not r_ok and "live image after rollback" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_rollback_pod_replacement_proof_fails_despite_later_success(self):
        # post-rollback UIDs identical to pre-rollback UIDs - not a real
        # replacement, even though the count matches.
        with mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]):
            with mock.patch.object(rollout_check, "_wait_exact_pod_count", return_value={"uid-1"}):
                ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertFalse(ok, "a later success must never overwrite an earlier recorded restoration failure")
        self.assertTrue(any(not r_ok and "replacement set of Pods" in msg for r_ok, msg in rollout_check.restoration_results))

    def test_full_success_path_returns_true(self):
        ok = rollout_check.rollback_workload("gateway", "maops-gateway", "maops-gateway", "sel", "maops-kubernetes-gateway:0.3.0")
        self.assertTrue(ok)
        self.assertTrue(all(r_ok for r_ok, _msg in rollout_check.restoration_results))


class InFlightServiceSamplerTests(unittest.TestCase):
    """DAY3-ARCH-M1 / DAY3-INT-M1: the sampler must run concurrently with
    the rollout (start()/stop() controlled by the caller, not a fixed
    post-rollout window) and must correctly detect a sample taken while
    the live Pod set has diverged from the pre-rollout baseline."""

    def test_diverged_sample_is_confirmed_once_current_uids_differ_from_baseline(self):
        calls = {"n": 0}

        def current_uids():
            calls["n"] += 1
            # First call still baseline (no divergence yet), second call
            # onward reflects a rollout in progress.
            return {"uid-1", "uid-2", "uid-3"} if calls["n"] == 1 else {"uid-1", "uid-2", "uid-new"}

        sampler = rollout_check.InFlightServiceSampler(
            functional_check=lambda: True,
            get_current_uids=current_uids,
            baseline_uids={"uid-1", "uid-2", "uid-3"},
            interval=0.01,
        )
        sampler.start()
        import time as _time

        deadline = _time.monotonic() + 2.0
        while not sampler.diverged_sample_confirmed and _time.monotonic() < deadline:
            _time.sleep(0.01)
        successes, total = sampler.stop()
        self.assertTrue(sampler.diverged_sample_confirmed)
        self.assertGreaterEqual(successes, 1)
        self.assertGreaterEqual(total, successes)

    def test_no_divergence_observed_when_uids_never_change(self):
        sampler = rollout_check.InFlightServiceSampler(
            functional_check=lambda: True,
            get_current_uids=lambda: {"uid-1", "uid-2", "uid-3"},
            baseline_uids={"uid-1", "uid-2", "uid-3"},
            interval=0.01,
        )
        sampler.start()
        import time as _time

        _time.sleep(0.05)
        successes, total = sampler.stop()
        self.assertFalse(sampler.diverged_sample_confirmed)
        self.assertGreaterEqual(successes, 1)

    def test_failed_sample_never_counts_toward_divergence_confirmation(self):
        sampler = rollout_check.InFlightServiceSampler(
            functional_check=lambda: False,
            get_current_uids=lambda: {"uid-new"},
            baseline_uids={"uid-1"},
            interval=0.01,
        )
        sampler.start()
        import time as _time

        _time.sleep(0.05)
        successes, total = sampler.stop()
        self.assertEqual(successes, 0)
        self.assertFalse(sampler.diverged_sample_confirmed)

    def test_stop_always_joins_the_background_thread(self):
        sampler = rollout_check.InFlightServiceSampler(
            functional_check=lambda: True,
            get_current_uids=lambda: set(),
            baseline_uids=set(),
            interval=0.01,
        )
        sampler.start()
        sampler.stop()
        self.assertFalse(sampler._thread.is_alive(), "no background sampler thread may be left running (leak) after stop()")


class InFlightSamplingOrderTests(unittest.TestCase):
    """A regression test that fails if sampling is ever moved back to
    strictly after rollout completion: the sampler must be start()-ed
    before wait_rollout_status() is called, and stop()-ed before the
    post-rollout settling checks run."""

    def setUp(self):
        rollout_check.results = []
        rollout_check.restoration_results = []

    def test_sampler_starts_before_rollout_status_and_stops_before_post_rollout_checks(self):
        call_order: list[str] = []

        class RecordingSampler:
            def __init__(self, *_a, **_kw):
                pass

            def start(self):
                call_order.append("sampler.start")

            def stop(self, join_timeout=None):
                call_order.append("sampler.stop")
                return (1, 1)

            diverged_sample_confirmed = True

        def fake_wait_rollout_status(*_a, **_kw):
            call_order.append("wait_rollout_status")
            return True, "ok"

        def fake_wait_exact_pod_count(*_a, **_kw):
            call_order.append("_wait_exact_pod_count")
            return {"uid-2", "uid-3", "uid-4"}

        with mock.patch.object(rollout_check, "_wait_ready", return_value={"status": {"conditions": []}}):
            with mock.patch.object(rollout_check, "get_pods", return_value=[_pod("gw-1", "uid-1")]):
                with mock.patch.object(rollout_check, "replicaset_uids", side_effect=[set(), {"rs-2"}]):
                    with mock.patch.object(rollout_check, "get_image", return_value="x"):
                        with mock.patch.object(rollout_check, "patch_rollout_annotation"):
                            with mock.patch.object(rollout_check, "InFlightServiceSampler", RecordingSampler):
                                with mock.patch.object(rollout_check, "wait_rollout_status", side_effect=fake_wait_rollout_status):
                                    with mock.patch.object(rollout_check, "_wait_exact_pod_count", side_effect=fake_wait_exact_pod_count):
                                        with mock.patch.object(rollout_check, "_wait_endpointslice_count", return_value=3):
                                            with mock.patch.object(rollout_check, "rollback_workload", return_value=True):
                                                rollout_check.run_rollout_experiment(
                                                    "gateway", "maops-gateway", "maops-gateway", "sel", lambda: True
                                                )

        self.assertEqual(
            call_order,
            ["sampler.start", "wait_rollout_status", "sampler.stop", "_wait_exact_pod_count"],
            "sampling must start before rollout-status is awaited and stop before post-rollout settling checks - "
            "this fails if sampling is ever moved back to strictly after rollout completion",
        )


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        rollout_check.results = []
        rollout_check.restoration_results = []

    def test_verify_context_failure_short_circuits_before_any_patch(self):
        with mock.patch.object(rollout_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(rollout_check, "run_rollout_experiment") as mock_run:
                exit_code = rollout_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

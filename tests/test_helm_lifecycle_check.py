"""
Docker/Kubernetes-free unit tests for scripts/helm_lifecycle_check.py -
the DAY6 live-discovered Helm ConfigMap rollout remediation. A live run
found the script's ORIGINAL upgrade proof (`kubectl rollout status`
alone) insufficient: it trivially "passes" against an unchanged,
already-healthy Deployment, exactly what happened when a ConfigMap-only
change never touched the gateway Deployment's Pod template. These tests
exercise the hardened `_verify_rollout_diverged()` (the actual proof:
observedGeneration, checksum, ReplicaSet, Pod UIDs, Ready count - each
recorded as its own distinct finding) and the surrounding
upgrade/rollback orchestration, entirely mocked - never a live cluster.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import helm_lifecycle_check


def _state(
    message: str = "orig",
    replicas: int = 3,
    generation: int = 1,
    observed_generation: int = 1,
    checksum: str = "a" * 64,
    rs_uids: frozenset = frozenset({"rs-old"}),
    pod_uids: frozenset = frozenset({"p1", "p2", "p3"}),
    ready_names: tuple = ("p1", "p2", "p3"),
) -> helm_lifecycle_check.GatewayState:
    return helm_lifecycle_check.GatewayState(
        configmap_message=message,
        desired_replicas=replicas,
        deployment_generation=generation,
        observed_generation=observed_generation,
        pod_template_checksum=checksum,
        replicaset_uids=rs_uids,
        pod_uids=pod_uids,
        ready_pod_names=ready_names,
    )


def _failed_messages() -> list[str]:
    return [m for ok, m in helm_lifecycle_check.results if not ok]


class VerifyRolloutDivergedTests(unittest.TestCase):
    """`_verify_rollout_diverged()` is the hardened core proof - the
    exact live-discovered bug this remediation fixes: a Deployment can
    report a successful `kubectl rollout status` while nothing about
    its Pod template, ReplicaSet, or Pods actually changed."""

    def setUp(self):
        helm_lifecycle_check.results = []

    def _baseline(self):
        return _state(checksum="a" * 64, rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"))

    def test_configmap_changed_but_checksum_did_not_change_fails(self):
        """The exact live-discovered bug: everything else reports
        healthy, but the Pod-template checksum never moved."""
        reference = self._baseline()
        settled = _state(checksum="a" * 64, rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("checksum changed" in m for m in _failed_messages()))

    def test_rollout_status_success_but_pod_uids_remain_unchanged_fails(self):
        reference = self._baseline()
        settled = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("none of the prior gateway Pod UIDs remain" in m for m in _failed_messages()))

    def test_only_some_old_pod_uids_were_replaced_fails(self):
        reference = self._baseline()
        settled = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p1", "p4", "p5"}), ready_names=("p1", "p4", "p5"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("none of the prior gateway Pod UIDs remain" in m for m in _failed_messages()))

    def test_checksum_changed_and_all_pods_replaced_ready_passes(self):
        reference = self._baseline()
        settled = _state(
            checksum="b" * 64, generation=2, observed_generation=2,
            rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"),
        )
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNotNone(result)
        self.assertTrue(all(ok for ok, _m in helm_lifecycle_check.results))

    def test_observed_generation_not_caught_up_fails(self):
        reference = self._baseline()
        settled = _state(
            checksum="b" * 64, generation=2, observed_generation=1,
            rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"),
        )
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("observedGeneration" in m for m in _failed_messages()))

    def test_not_all_replicas_ready_fails(self):
        reference = self._baseline()
        settled = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5"}), ready_names=("p4", "p5"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("all 3 gateway Pods are Ready" in m for m in _failed_messages()))

    def test_same_replicaset_still_active_fails(self):
        """Even if UIDs somehow looked different (they should not for
        the same RS), the active ReplicaSet identity itself must have
        changed - a direct, independent signal of a real rollout."""
        reference = self._baseline()
        settled = _state(checksum="b" * 64, rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("new ReplicaSet was created/selected" in m for m in _failed_messages()))

    def test_api_error_or_timeout_never_becomes_success(self):
        """`_wait_for_gateway_rollout_settled()` returning None (every
        single read failed, even the best-effort final snapshot) must
        be an explicit INCONCLUSIVE failure, never treated as if the
        rollout had converged."""
        reference = self._baseline()
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=None):
            result = helm_lifecycle_check._verify_rollout_diverged(reference, 3, "post-upgrade")
        self.assertIsNone(result)
        self.assertTrue(any("INCONCLUSIVE" in m for m in _failed_messages()))

    # --- rollback-specific: exact_checksum required, not merely "changed" ---

    def test_rollback_restores_configmap_but_not_checksum_fails(self):
        upgrade_reference = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"))
        baseline_checksum = "a" * 64
        # A DIFFERENT checksum than both the upgrade's AND the original
        # baseline's - "changed away from the upgrade" alone is not
        # enough for a rollback; it must return to the EXACT baseline.
        settled = _state(checksum="c" * 64, rs_uids=frozenset({"rs-rollback"}), pod_uids=frozenset({"p7", "p8", "p9"}), ready_names=("p7", "p8", "p9"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(upgrade_reference, 3, "post-rollback", exact_checksum=baseline_checksum)
        self.assertIsNone(result)
        self.assertTrue(any("restored to the exact pre-upgrade baseline value" in m for m in _failed_messages()))

    def test_rollback_leaves_an_upgrade_pod_uid_fails(self):
        upgrade_reference = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"))
        baseline_checksum = "a" * 64
        settled = _state(checksum=baseline_checksum, rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p4", "p8", "p9"}), ready_names=("p4", "p8", "p9"))
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(upgrade_reference, 3, "post-rollback", exact_checksum=baseline_checksum)
        self.assertIsNone(result)
        self.assertTrue(any("none of the prior gateway Pod UIDs remain" in m for m in _failed_messages()))

    def test_rollback_fully_restores_config_checksum_pods_passes(self):
        upgrade_reference = _state(checksum="b" * 64, rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"))
        baseline_checksum = "a" * 64
        settled = _state(
            checksum=baseline_checksum, generation=3, observed_generation=3,
            rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"),
        )
        with mock.patch.object(helm_lifecycle_check, "_wait_for_gateway_rollout_settled", return_value=settled):
            result = helm_lifecycle_check._verify_rollout_diverged(upgrade_reference, 3, "post-rollback", exact_checksum=baseline_checksum)
        self.assertIsNotNone(result)
        self.assertTrue(all(ok for ok, _m in helm_lifecycle_check.results))


class VerifyAllPodsReportMessageTests(unittest.TestCase):
    """`_verify_all_pods_report_message()` - the direct, per-Pod /config
    proof, bypassing the Service entirely."""

    def setUp(self):
        helm_lifecycle_check.results = []

    def test_all_pods_report_expected_message_passes(self):
        with mock.patch.object(helm_lifecycle_check, "_pod_reports_message", return_value=(True, "reported APP_MESSAGE='new'")):
            ok = helm_lifecycle_check._verify_all_pods_report_message(("p1", "p2"), "new", "post-upgrade")
        self.assertTrue(ok)

    def test_one_pod_still_reports_the_old_app_message_fails(self):
        def side_effect(pod_name, _expected):
            if pod_name == "p2":
                return False, "reported APP_MESSAGE='old'"
            return True, "reported APP_MESSAGE='new'"

        with mock.patch.object(helm_lifecycle_check, "_pod_reports_message", side_effect=side_effect):
            ok = helm_lifecycle_check._verify_all_pods_report_message(("p1", "p2", "p3"), "new", "post-upgrade")
        self.assertFalse(ok)

    def test_no_ready_pods_available_fails(self):
        ok = helm_lifecycle_check._verify_all_pods_report_message((), "new", "post-upgrade")
        self.assertFalse(ok)

    def test_exec_error_for_one_pod_fails_without_raising(self):
        def side_effect(pod_name, _expected):
            if pod_name == "p1":
                return False, "kubectl exec failed: pod not found"
            return True, "reported APP_MESSAGE='new'"

        with mock.patch.object(helm_lifecycle_check, "_pod_reports_message", side_effect=side_effect):
            ok = helm_lifecycle_check._verify_all_pods_report_message(("p1", "p2"), "new", "post-upgrade")
        self.assertFalse(ok)


class WaitExternalMessageTests(unittest.TestCase):
    """`_wait_external_message()` - bounded polling against the
    externally-routed Gateway API path."""

    def setUp(self):
        helm_lifecycle_check.results = []

    def test_external_route_reports_expected_value_passes(self):
        with mock.patch.object(helm_lifecycle_check, "_external_app_message_quiet", return_value="new"):
            ok = helm_lifecycle_check._wait_external_message("new", "post-upgrade")
        self.assertTrue(ok)

    def test_external_route_still_reports_the_old_value_fails(self):
        with mock.patch.object(helm_lifecycle_check, "_external_app_message_quiet", return_value="old"), mock.patch.object(
            helm_lifecycle_check, "EXTERNAL_POLL_TIMEOUT_SECONDS", 0.05
        ), mock.patch.object(helm_lifecycle_check, "EXTERNAL_POLL_INTERVAL_SECONDS", 0.01):
            ok = helm_lifecycle_check._wait_external_message("new", "post-upgrade")
        self.assertFalse(ok)

    def test_api_error_during_polling_never_becomes_success(self):
        """A connection failure (`_external_app_message_quiet()`
        returning None throughout) must never be silently treated as
        the expected value having been reached."""
        with mock.patch.object(helm_lifecycle_check, "_external_app_message_quiet", return_value=None), mock.patch.object(
            helm_lifecycle_check, "EXTERNAL_POLL_TIMEOUT_SECONDS", 0.05
        ), mock.patch.object(helm_lifecycle_check, "EXTERNAL_POLL_INTERVAL_SECONDS", 0.01):
            ok = helm_lifecycle_check._wait_external_message("new", "post-upgrade")
        self.assertFalse(ok)

    def test_eventually_converges_within_bounded_polling_passes(self):
        calls = {"n": 0}

        def side_effect():
            calls["n"] += 1
            return "new" if calls["n"] >= 2 else "old"

        with mock.patch.object(helm_lifecycle_check, "_external_app_message_quiet", side_effect=side_effect), mock.patch.object(
            helm_lifecycle_check, "EXTERNAL_POLL_INTERVAL_SECONDS", 0.01
        ):
            ok = helm_lifecycle_check._wait_external_message("new", "post-upgrade")
        self.assertTrue(ok)


class MainOrchestrationTests(unittest.TestCase):
    """Integration-level coverage of `main()`'s upgrade/rollback
    orchestration - all kubectl/helm-touching calls mocked at the
    module-function boundary; never a live cluster."""

    def setUp(self):
        helm_lifecycle_check.results = []

    def _baseline_state(self):
        return _state(message="orig", checksum="a" * 64, rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"))

    def _history_result(self, revision: int = 2):
        return mock.Mock(returncode=0, stdout=f'[{{"revision": {revision}}}]', stderr="")

    def test_upgrade_failure_still_invokes_the_appropriate_restoration_path(self):
        """If `helm upgrade` itself never succeeds, the guaranteed
        `finally` block must still run cleanly, must NOT attempt a real
        `helm rollback` (nothing was ever changed to roll back), and the
        overall result must still be a FAILURE - never silently pass
        just because there was nothing to restore."""
        baseline = self._baseline_state()
        rollback_calls = []

        def helm_side_effect(*args, **_kwargs):
            if args[0] == "history":
                return self._history_result()
            if args[0] == "upgrade":
                return mock.Mock(returncode=1, stderr="upgrade failed: some reason")
            if args[0] == "rollback":
                rollback_calls.append(args)
                return mock.Mock(returncode=0, stderr="")
            raise AssertionError(f"unexpected helm call: {args}")

        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_pvc_identity", return_value=("pvc-1", "pv-1")), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ):
            exit_code = helm_lifecycle_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(rollback_calls, [])
        self.assertTrue(any("nothing was changed" in m for _ok, m in helm_lifecycle_check.results))

    def test_pvc_uid_change_across_upgrade_fails(self):
        baseline = self._baseline_state()
        upgraded_state = _state(
            message="upgraded", checksum="b" * 64, generation=2, observed_generation=2,
            rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"),
        )
        pvc_calls = {"n": 0}

        def pvc_side_effect():
            pvc_calls["n"] += 1
            # First call (baseline) returns the original UID; every
            # call after the upgrade returns a DIFFERENT UID - a real
            # (unexpected) PVC recreation.
            if pvc_calls["n"] == 1:
                return "pvc-1", "pv-1"
            return "pvc-DIFFERENT", "pv-1"

        def helm_side_effect(*args, **_kwargs):
            if args[0] == "history":
                return self._history_result()
            if args[0] == "upgrade":
                return mock.Mock(returncode=0, stderr="")
            if args[0] == "rollback":
                return mock.Mock(returncode=0, stderr="")
            raise AssertionError(f"unexpected helm call: {args}")

        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_configmap_message", return_value="upgraded"), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", side_effect=pvc_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_rollout_diverged", return_value=upgraded_state
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_all_pods_report_message", return_value=True
        ), mock.patch.object(
            helm_lifecycle_check, "_wait_external_message", return_value=True
        ):
            exit_code = helm_lifecycle_check.main()

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("state PVC/PV identity unchanged across upgrade" in m for _ok, m in helm_lifecycle_check.results if not _ok))

    def test_full_upgrade_and_rollback_success_passes(self):
        baseline = self._baseline_state()
        upgraded_state = _state(
            message="upgraded", checksum="b" * 64, generation=2, observed_generation=2,
            rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"),
        )
        rolled_back_state = _state(
            message="orig", checksum="a" * 64, generation=3, observed_generation=3,
            rs_uids=frozenset({"rs-old"}), pod_uids=frozenset({"p1", "p2", "p3"}), ready_names=("p1", "p2", "p3"),
        )
        configmap_calls = {"n": 0}

        def configmap_side_effect():
            configmap_calls["n"] += 1
            return helm_lifecycle_check.UPGRADE_MESSAGE if configmap_calls["n"] == 1 else "orig"

        def helm_side_effect(*args, **_kwargs):
            if args[0] == "history":
                return self._history_result()
            if args[0] in ("upgrade", "rollback"):
                return mock.Mock(returncode=0, stderr="")
            raise AssertionError(f"unexpected helm call: {args}")

        diverge_calls = {"n": 0}

        def diverge_side_effect(_reference, _expected_replicas, _label, exact_checksum=None):
            diverge_calls["n"] += 1
            return upgraded_state if diverge_calls["n"] == 1 else rolled_back_state

        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_configmap_message", side_effect=configmap_side_effect), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", return_value=("pvc-1", "pv-1")
        ), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_rollout_diverged", side_effect=diverge_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_all_pods_report_message", return_value=True
        ), mock.patch.object(
            helm_lifecycle_check, "_wait_external_message", return_value=True
        ):
            exit_code = helm_lifecycle_check.main()

        self.assertEqual(exit_code, 0)
        self.assertTrue(all(ok for ok, _m in helm_lifecycle_check.results))

    def test_baseline_capture_api_error_never_becomes_success(self):
        """A baseline that cannot even be established (API error/
        timeout) must refuse to mutate the release at all and must
        never report success."""
        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_current_revision", return_value=None
        ), mock.patch.object(helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=None), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", return_value=(None, None)
        ), mock.patch.object(helm_lifecycle_check, "_helm") as helm_mock:
            exit_code = helm_lifecycle_check.main()
        self.assertEqual(exit_code, 1)
        helm_mock.assert_not_called()

    def _upgraded_state(self):
        return _state(
            message="upgraded", checksum="b" * 64, generation=2, observed_generation=2,
            rs_uids=frozenset({"rs-new"}), pod_uids=frozenset({"p4", "p5", "p6"}), ready_names=("p4", "p5", "p6"),
        )

    def test_rollback_nonzero_after_submitted_upgrade_records_restoration_failure(self):
        """DAY6 review TEST-1: a successfully submitted (and otherwise
        fully converged) upgrade followed by a `helm rollback` that
        returns nonzero must record RESTORATION FAILURE, fail the
        command, and never claim the release was restored - no
        post-rollback ConfigMap/rollout/PVC restoration finding may be
        recorded as passing when the rollback itself never succeeded."""
        baseline = self._baseline_state()
        helm_calls = []

        def helm_side_effect(*args, **_kwargs):
            helm_calls.append(args[0])
            if args[0] == "history":
                return self._history_result()
            if args[0] == "upgrade":
                return mock.Mock(returncode=0, stderr="")
            if args[0] == "rollback":
                return mock.Mock(returncode=1, stderr="Error: rollback failed: simulated API error")
            raise AssertionError(f"unexpected helm call: {args}")

        stdout = io.StringIO()
        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_configmap_message", return_value=helm_lifecycle_check.UPGRADE_MESSAGE), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", return_value=("pvc-1", "pv-1")
        ), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_rollout_diverged", return_value=self._upgraded_state()
        ) as diverge_mock, mock.patch.object(
            helm_lifecycle_check, "_verify_all_pods_report_message", return_value=True
        ), mock.patch.object(
            helm_lifecycle_check, "_wait_external_message", return_value=True
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            exit_code = helm_lifecycle_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(helm_calls.count("rollback"), 1)
        failed = _failed_messages()
        self.assertTrue(any(m.startswith("RESTORATION FAILURE") for m in failed))
        self.assertTrue(any("helm rollback to revision 2" in m and "simulated API error" in m for m in failed))
        # Only the upgrade-side divergence proof ran; the post-rollback
        # proof must never run against a rollback that did not happen.
        self.assertEqual(diverge_mock.call_count, 1)
        passed = [m for ok, m in helm_lifecycle_check.results if ok]
        self.assertFalse(any("restored" in m or "end to end" in m for m in passed))
        self.assertNotIn("PASS:", stdout.getvalue())

    def test_exception_after_upgrade_submission_still_reaches_finally_rollback(self):
        """DAY6 review TEST-1: an unexpected exception raised by a
        post-upgrade step (here, the rollout-divergence proof) must not
        skip the guaranteed `finally` rollback. The rollback is
        attempted exactly once, and the exception still propagates -
        so the command exits nonzero instead of printing PASS."""
        baseline = self._baseline_state()
        helm_calls = []

        def helm_side_effect(*args, **_kwargs):
            helm_calls.append(args)
            if args[0] == "history":
                return self._history_result()
            if args[0] in ("upgrade", "rollback"):
                return mock.Mock(returncode=0, stderr="")
            raise AssertionError(f"unexpected helm call: {args}")

        diverge_calls = {"n": 0}

        def diverge_side_effect(_reference, _expected_replicas, label, exact_checksum=None):
            diverge_calls["n"] += 1
            if label == "post-upgrade":
                raise RuntimeError("simulated kubectl failure mid-verification")
            return _state(generation=3, observed_generation=3, rs_uids=frozenset({"rs-old-2"}), pod_uids=frozenset({"p7", "p8", "p9"}), ready_names=("p7", "p8", "p9"))

        stdout = io.StringIO()
        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_configmap_message", side_effect=[helm_lifecycle_check.UPGRADE_MESSAGE, "orig"]), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", return_value=("pvc-1", "pv-1")
        ), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_rollout_diverged", side_effect=diverge_side_effect
        ), mock.patch.object(
            helm_lifecycle_check, "_verify_all_pods_report_message", return_value=True
        ), mock.patch.object(
            helm_lifecycle_check, "_wait_external_message", return_value=True
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                helm_lifecycle_check.main()

        rollback_calls = [a for a in helm_calls if a[0] == "rollback"]
        self.assertEqual(len(rollback_calls), 1)
        self.assertEqual(rollback_calls[0][2], "2")
        self.assertNotIn("PASS:", stdout.getvalue())

    def test_exception_after_upgrade_with_failed_rollback_records_restoration_failure(self):
        """DAY6 review TEST-1: the combined worst case - a post-upgrade
        exception AND a failing rollback. The `finally` block must still
        record RESTORATION FAILURE (never a successful restoration)
        before the original exception propagates."""
        baseline = self._baseline_state()

        def helm_side_effect(*args, **_kwargs):
            if args[0] == "history":
                return self._history_result()
            if args[0] == "upgrade":
                return mock.Mock(returncode=0, stderr="")
            if args[0] == "rollback":
                return mock.Mock(returncode=1, stderr="Error: simulated rollback failure")
            raise AssertionError(f"unexpected helm call: {args}")

        stdout = io.StringIO()
        with mock.patch.object(helm_lifecycle_check.kube, "verify_context"), mock.patch.object(
            helm_lifecycle_check, "_capture_gateway_state_or_record", return_value=baseline
        ), mock.patch.object(helm_lifecycle_check, "_configmap_message", side_effect=RuntimeError("simulated API error reading ConfigMap")), mock.patch.object(
            helm_lifecycle_check, "_pvc_identity", return_value=("pvc-1", "pv-1")
        ), mock.patch.object(
            helm_lifecycle_check, "_helm", side_effect=helm_side_effect
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                helm_lifecycle_check.main()

        failed = _failed_messages()
        self.assertTrue(any(m.startswith("RESTORATION FAILURE") for m in failed))
        passed = [m for ok, m in helm_lifecycle_check.results if ok]
        self.assertFalse(any("rollback" in m for m in passed))
        self.assertNotIn("PASS:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

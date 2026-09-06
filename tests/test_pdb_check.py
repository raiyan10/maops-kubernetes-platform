"""
Docker/Kubernetes-free unit tests for scripts/pdb_check.py:

- Eviction API response classification (rejected-by-PDB vs. unexpectedly
  succeeded vs. inconclusive) is driven by the actual kubectl/API
  response content, never a bare non-zero-exit-code guess.
- The "Pod was not voluntarily evicted" check is a real identity check
  (name + UID + no deletionTimestamp), not just "a pod with this name
  still exists somewhere".
- Restoration (2 -> 3) is always attempted once the workload has been
  scaled down, even when the eviction experiment itself raises, and a
  restoration failure is reported distinctly.

Monkeypatches every kube/subprocess call so these tests never touch a
real cluster.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pdb_check


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


class EvictionClassificationTests(unittest.TestCase):
    """DAY3-INT-H1 / DAY3-SEC-M1: a bare HTTP 429 TooManyRequests (as
    Kubernetes API Priority and Fairness, or other unrelated server
    conditions, can also produce) must NEVER be classified as
    'rejected_by_pdb'. A valid PDB rejection requires BOTH the
    TooManyRequests reason AND disruption-budget-specific evidence in the
    Status message/details."""

    def test_rejected_with_too_many_requests_and_disruption_budget_message(self):
        result = _completed(
            1,
            stderr='Error from server (TooManyRequests): Cannot evict pod as it would violate the '
            "pod's disruption budget.",
        )
        self.assertEqual(pdb_check.classify_eviction_result(result), "rejected_by_pdb")

    def test_rejected_with_structured_json_status_and_disruption_budget_cause(self):
        # Prefer structured JSON Status parsing where the response body
        # actually is JSON.
        status = {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": "Cannot evict pod as it would violate the pod's disruption budget.",
            "reason": "TooManyRequests",
            "details": {"causes": [{"reason": "DisruptionBudget", "message": "The disruption budget maops-app-pdb needs 2 healthy pods and has 2 currently"}]},
            "code": 429,
        }
        result = _completed(1, stdout=json.dumps(status))
        self.assertEqual(pdb_check.classify_eviction_result(result), "rejected_by_pdb")

    def test_bare_too_many_requests_without_disruption_budget_evidence_is_inconclusive(self):
        # DAY3-INT-H1: this is the exact false-positive the finding
        # describes - e.g. API Priority and Fairness rejecting the request
        # for capacity reasons that have nothing to do with this PDB.
        result = _completed(1, stderr="Error from server (TooManyRequests): Too many requests, please try again later.")
        self.assertEqual(pdb_check.classify_eviction_result(result), "inconclusive")

    def test_bare_too_many_requests_structured_status_without_disruption_budget_evidence_is_inconclusive(self):
        status = {
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": "Too many requests, please try again later.",
            "reason": "TooManyRequests",
            "code": 429,
        }
        result = _completed(1, stdout=json.dumps(status))
        self.assertEqual(pdb_check.classify_eviction_result(result), "inconclusive")

    def test_unexpected_success_status_success(self):
        result = _completed(0, stdout='{"kind":"Status","apiVersion":"v1","metadata":{},"status":"Success"}')
        self.assertEqual(pdb_check.classify_eviction_result(result), "succeeded")

    def test_failure_for_unrelated_reason_is_inconclusive_not_pdb_rejection(self):
        result = _completed(1, stderr="Error from server (NotFound): pods \"ghost\" not found")
        self.assertEqual(pdb_check.classify_eviction_result(result), "inconclusive")

    def test_success_with_unparseable_body_is_inconclusive(self):
        result = _completed(0, stdout="not json")
        self.assertEqual(pdb_check.classify_eviction_result(result), "inconclusive")

    def test_malformed_non_json_failure_is_inconclusive(self):
        result = _completed(1, stdout="\x00not json at all", stderr="")
        self.assertEqual(pdb_check.classify_eviction_result(result), "inconclusive")


class PodNotEvictedTests(unittest.TestCase):
    def test_same_name_and_uid_no_deletion_timestamp_is_not_evicted(self):
        pods = [{"metadata": {"name": "maops-app-1", "uid": "uid-a"}}]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertTrue(pdb_check.pod_still_present_and_not_evicted("sel", "maops-app-1", "uid-a"))

    def test_missing_pod_is_evicted(self):
        with mock.patch.object(pdb_check, "get_pods", return_value=[]):
            self.assertFalse(pdb_check.pod_still_present_and_not_evicted("sel", "maops-app-1", "uid-a"))

    def test_deletion_timestamp_present_is_evicted(self):
        pods = [{"metadata": {"name": "maops-app-1", "uid": "uid-a", "deletionTimestamp": "2026-01-01T00:00:00Z"}}]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertFalse(pdb_check.pod_still_present_and_not_evicted("sel", "maops-app-1", "uid-a"))

    def test_replaced_pod_with_same_name_different_uid_is_evicted(self):
        # A same-named pod could in principle be a *replacement* the
        # Deployment controller created after the original was actually
        # evicted - matching by name alone would wrongly call this "not
        # evicted".
        pods = [{"metadata": {"name": "maops-app-1", "uid": "uid-different"}}]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertFalse(pdb_check.pod_still_present_and_not_evicted("sel", "maops-app-1", "uid-a"))


class VictimSelectionTests(unittest.TestCase):
    """DAY3 defect fix: a Pod the PDB doesn't count as healthy (still
    terminating, or not Ready - e.g. a straggler from the immediately
    preceding 3 -> 2 scale-down) must never be selected as the eviction
    target, since evicting it would "succeed" for a reason unrelated to
    the PDB and falsely look like the PDB failed to protect the
    workload."""

    def setUp(self):
        pdb_check.results = []
        pdb_check.restoration_results = []

    @staticmethod
    def _ready(name: str, uid: str) -> dict:
        return {"metadata": {"name": name, "uid": uid}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}

    @staticmethod
    def _terminating(name: str, uid: str) -> dict:
        return {
            "metadata": {"name": name, "uid": uid, "deletionTimestamp": "2026-01-01T00:00:00Z"},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        }

    def test_terminating_pod_is_never_selected_as_victim(self):
        pods = [VictimSelectionTests._terminating("aaa-terminating", "uid-term"), VictimSelectionTests._ready("bbb-healthy", "uid-healthy")]
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=pods):
                        with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                            mock_evict.return_value = _completed(1, stderr="Error from server (TooManyRequests): disruption budget")
                            with mock.patch.object(pdb_check, "pod_still_present_and_not_evicted", return_value=True):
                                with mock.patch.object(pdb_check, "restore_workload", return_value=True):
                                    pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        mock_evict.assert_called_once_with("bbb-healthy")

    def test_not_ready_pod_is_never_selected_as_victim(self):
        not_ready = {"metadata": {"name": "aaa-notready", "uid": "uid-nr"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}
        pods = [not_ready, VictimSelectionTests._ready("bbb-healthy", "uid-healthy")]
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=pods):
                        with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                            mock_evict.return_value = _completed(1, stderr="Error from server (TooManyRequests): disruption budget")
                            with mock.patch.object(pdb_check, "pod_still_present_and_not_evicted", return_value=True):
                                with mock.patch.object(pdb_check, "restore_workload", return_value=True):
                                    pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        mock_evict.assert_called_once_with("bbb-healthy")

    def test_no_healthy_pods_available_fails_without_attempting_eviction(self):
        pods = [VictimSelectionTests._terminating("aaa-terminating", "uid-term")]
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=pods):
                        with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                            with mock.patch.object(pdb_check, "restore_workload", return_value=True):
                                pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        mock_evict.assert_not_called()
        self.assertTrue(any(not ok and "no healthy" in msg for ok, msg in pdb_check.results))


class RestorationGuaranteeTests(unittest.TestCase):
    def setUp(self):
        pdb_check.results = []
        pdb_check.restoration_results = []

    def test_restoration_invoked_when_experiment_raises_after_scale_down(self):
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, RuntimeError("boom")]):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "restore_workload", return_value=True) as mock_restore:
                        pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")
        mock_restore.assert_called_once()

    def test_restoration_not_attempted_when_baseline_never_reached(self):
        with mock.patch.object(pdb_check, "_wait_deployment_at", side_effect=TimeoutError("never ready")):
            with mock.patch.object(pdb_check, "restore_workload") as mock_restore:
                pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")
        mock_restore.assert_not_called()

    def test_restoration_not_attempted_when_scale_down_command_fails(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "scale"], stderr="deployment not found")
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(pdb_check, "_wait_pdb_state", return_value={"currentHealthy": 3}):
                with mock.patch.object(pdb_check, "scale_deployment", side_effect=exc):
                    with mock.patch.object(pdb_check, "restore_workload") as mock_restore:
                        pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")
        mock_restore.assert_not_called()

    def test_restoration_failure_reported_prominently(self):
        def failing_restore(*_a, **_kw):
            pdb_check.record_restoration(False, "could not scale maops-app back to 3 replicas: simulated failure")
            return False

        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=[{"metadata": {"name": "p", "uid": "u"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]):
                        with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                            mock_evict.return_value = _completed(1, stderr="Error from server (TooManyRequests): disruption budget")
                            with mock.patch.object(pdb_check, "pod_still_present_and_not_evicted", return_value=True):
                                with mock.patch.object(pdb_check, "restore_workload", side_effect=failing_restore):
                                    pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        self.assertTrue(
            any(not ok for ok, _msg in pdb_check.restoration_results),
            "a restoration failure must be recorded in restoration_results, not merged silently into the original result",
        )

    def test_unexpected_eviction_success_is_a_failure(self):
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=[{"metadata": {"name": "p", "uid": "u"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]):
                        with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                            mock_evict.return_value = _completed(0, stdout='{"status":"Success"}')
                            with mock.patch.object(pdb_check, "pod_still_present_and_not_evicted", return_value=False):
                                with mock.patch.object(pdb_check, "restore_workload", return_value=True) as mock_restore:
                                    pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        self.assertTrue(
            any(not ok and "UNEXPECTEDLY SUCCEEDED" in msg for ok, msg in pdb_check.results),
            "an unexpectedly successful eviction must be recorded as a failure, not a pass",
        )
        # DAY3-TEST-L1: even an unexpectedly-successful eviction must still
        # trigger the guaranteed restoration path.
        mock_restore.assert_called_once()


class VictimFreshnessTests(unittest.TestCase):
    """DAY3-INT-M3: immediately before submitting the Eviction API
    request, the chosen victim must be re-fetched and re-verified - a
    stale snapshot from selection time is not good enough."""

    @staticmethod
    def _pod(name: str, uid: str, ready: bool = True, deleting: bool = False) -> dict:
        pod = {
            "metadata": {"name": name, "uid": uid},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }
        if deleting:
            pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        return pod

    def test_victim_remains_healthy_is_refreshed_successfully(self):
        pods = [self._pod("maops-app-1", "uid-a", ready=True)]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            fresh = pdb_check.refresh_victim("sel", "maops-app-1", "uid-a")
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["metadata"]["uid"], "uid-a")

    def test_victim_became_not_ready_fails_freshness(self):
        pods = [self._pod("maops-app-1", "uid-a", ready=False)]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertIsNone(pdb_check.refresh_victim("sel", "maops-app-1", "uid-a"))

    def test_victim_began_terminating_fails_freshness(self):
        pods = [self._pod("maops-app-1", "uid-a", ready=True, deleting=True)]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertIsNone(pdb_check.refresh_victim("sel", "maops-app-1", "uid-a"))

    def test_same_name_different_uid_fails_freshness(self):
        # The Deployment controller could have already replaced this Pod
        # with one of the same name (e.g. after a separate involuntary
        # failure) - matching by name alone would wrongly treat it as
        # fresh.
        pods = [self._pod("maops-app-1", "uid-different", ready=True)]
        with mock.patch.object(pdb_check, "get_pods", return_value=pods):
            self.assertIsNone(pdb_check.refresh_victim("sel", "maops-app-1", "uid-a"))

    def test_victim_gone_entirely_fails_freshness(self):
        with mock.patch.object(pdb_check, "get_pods", return_value=[]):
            self.assertIsNone(pdb_check.refresh_victim("sel", "maops-app-1", "uid-a"))

    def test_stale_victim_short_circuits_run_pdb_experiment_without_attempting_eviction(self):
        pdb_check.results = []
        pdb_check.restoration_results = []
        selected = self._pod("bbb-healthy", "uid-healthy", ready=True)
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=[selected]):
                        with mock.patch.object(pdb_check, "refresh_victim", return_value=None):
                            with mock.patch.object(pdb_check, "attempt_eviction") as mock_evict:
                                with mock.patch.object(pdb_check, "restore_workload", return_value=True) as mock_restore:
                                    pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        mock_evict.assert_not_called()
        mock_restore.assert_called_once()
        self.assertTrue(
            any(not ok and "freshness check failed" in msg for ok, msg in pdb_check.results),
            "a stale victim must record a distinct freshness-failure reason, never blame the PDB",
        )


class EvictionSubprocessTimeoutTests(unittest.TestCase):
    """DAY3-INT-H2: a bounded Eviction API subprocess timeout must be
    classified as inconclusive, never mistaken for a PDB rejection."""

    def setUp(self):
        pdb_check.results = []
        pdb_check.restoration_results = []

    def test_eviction_timeout_is_inconclusive_not_pdb_rejection(self):
        pod = {"metadata": {"name": "p", "uid": "u"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
            with mock.patch.object(
                pdb_check, "_wait_pdb_state", side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2, "disruptionsAllowed": 0}]
            ):
                with mock.patch.object(pdb_check, "scale_deployment"):
                    with mock.patch.object(pdb_check, "get_pods", return_value=[pod]):
                        with mock.patch.object(
                            pdb_check,
                            "attempt_eviction",
                            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=15),
                        ):
                            with mock.patch.object(pdb_check, "restore_workload", return_value=True) as mock_restore:
                                pdb_check.run_pdb_experiment("app", "maops-app", "maops-app-pdb", "sel")

        self.assertTrue(
            any(not ok and "timed out" in msg and "NOT classified as a PDB rejection" in msg for ok, msg in pdb_check.results)
        )
        self.assertFalse(any("rejected_by_pdb" in msg for ok, msg in pdb_check.results if ok))
        mock_restore.assert_called_once()


class PdbStatusParsingTests(unittest.TestCase):
    """DAY3-TEST-M1: get_pdb_status/_pdb_matches against realistic
    PodDisruptionBudget objects - never mocking _wait_pdb_state itself."""

    def test_get_pdb_status_returns_status_dict(self):
        pdb_obj = {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": {"name": "maops-app-pdb"},
            "spec": {"minAvailable": 2},
            "status": {"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 1},
        }
        with mock.patch.object(pdb_check, "get_json", return_value=pdb_obj):
            status = pdb_check.get_pdb_status("maops-app-pdb")
        self.assertEqual(status, {"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 1})

    def test_get_pdb_status_missing_status_key_returns_empty_dict(self):
        pdb_obj = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "maops-app-pdb"}, "spec": {}}
        with mock.patch.object(pdb_check, "get_json", return_value=pdb_obj):
            self.assertEqual(pdb_check.get_pdb_status("maops-app-pdb"), {})

    def test_pdb_matches_correct_status_returns_status(self):
        with mock.patch.object(
            pdb_check, "get_pdb_status", return_value={"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 1}
        ):
            result = pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1)
        self.assertEqual(result, {"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 1})

    def test_pdb_matches_empty_status_returns_none(self):
        with mock.patch.object(pdb_check, "get_pdb_status", return_value={}):
            self.assertIsNone(pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1))

    def test_pdb_matches_wrong_current_healthy_returns_none(self):
        with mock.patch.object(
            pdb_check, "get_pdb_status", return_value={"currentHealthy": 2, "desiredHealthy": 2, "disruptionsAllowed": 1}
        ):
            self.assertIsNone(pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1))

    def test_pdb_matches_wrong_desired_healthy_returns_none(self):
        with mock.patch.object(
            pdb_check, "get_pdb_status", return_value={"currentHealthy": 3, "desiredHealthy": 1, "disruptionsAllowed": 1}
        ):
            self.assertIsNone(pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1))

    def test_pdb_matches_wrong_disruptions_allowed_returns_none(self):
        with mock.patch.object(
            pdb_check, "get_pdb_status", return_value={"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 0}
        ):
            self.assertIsNone(pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1))

    def test_pdb_matches_malformed_string_values_returns_none(self):
        with mock.patch.object(
            pdb_check, "get_pdb_status", return_value={"currentHealthy": "3", "desiredHealthy": 2, "disruptionsAllowed": 1}
        ):
            self.assertIsNone(pdb_check._pdb_matches("maops-app-pdb", 3, 2, 1))


class RestoreWorkloadDirectTests(unittest.TestCase):
    """DAY3-TEST-H1: directly unit-tests the REAL pdb_check.restore_workload
    function (never replaced by a mock) - only its lower-level
    collaborators (scale_deployment, _wait_deployment_at, _wait_pdb_state)
    are mocked."""

    def setUp(self):
        pdb_check.results = []
        pdb_check.restoration_results = []

    def test_scale_command_called_process_error_returns_false_and_records_failure(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "scale"], stderr="deployment not found")
        with mock.patch.object(pdb_check, "scale_deployment", side_effect=exc):
            ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertFalse(ok)
        self.assertTrue(
            any(not r_ok and "could not scale" in msg for r_ok, msg in pdb_check.restoration_results)
        )

    def test_scale_command_timeout_expired_returns_false_and_records_failure(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl", "scale"], timeout=30)
        with mock.patch.object(pdb_check, "scale_deployment", side_effect=exc):
            ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertFalse(ok)
        self.assertTrue(
            any(not r_ok and "could not scale" in msg for r_ok, msg in pdb_check.restoration_results)
        )

    def test_deployment_convergence_timeout_returns_false(self):
        with mock.patch.object(pdb_check, "scale_deployment"):
            with mock.patch.object(pdb_check, "_wait_deployment_at", side_effect=TimeoutError("never reached 3/3")):
                ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertFalse(ok)
        self.assertTrue(
            any(not r_ok and "did not return to" in msg for r_ok, msg in pdb_check.restoration_results)
        )

    def test_pdb_state_convergence_timeout_returns_false(self):
        with mock.patch.object(pdb_check, "scale_deployment"):
            with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
                with mock.patch.object(pdb_check, "_wait_pdb_state", side_effect=TimeoutError("PDB never healthy again")):
                    ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertFalse(ok)
        self.assertTrue(
            any(not r_ok and "did not return to the healthy 3/3 state" in msg for r_ok, msg in pdb_check.restoration_results)
        )

    def test_full_success_path_returns_true_and_records_only_successes(self):
        with mock.patch.object(pdb_check, "scale_deployment"):
            with mock.patch.object(pdb_check, "_wait_deployment_at", return_value={}):
                with mock.patch.object(
                    pdb_check, "_wait_pdb_state", return_value={"currentHealthy": 3, "desiredHealthy": 2, "disruptionsAllowed": 1}
                ):
                    ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertTrue(ok)
        self.assertTrue(all(r_ok for r_ok, _msg in pdb_check.restoration_results))

    def test_failure_return_is_not_overwritten_by_a_later_success_record(self):
        # Once the Deployment convergence step fails and returns False, no
        # subsequent step (e.g. PDB-state) ever runs to record a True that
        # could be mistaken for overall success.
        with mock.patch.object(pdb_check, "scale_deployment"):
            with mock.patch.object(pdb_check, "_wait_deployment_at", side_effect=TimeoutError("never reached 3/3")):
                with mock.patch.object(pdb_check, "_wait_pdb_state") as mock_pdb_wait:
                    ok = pdb_check.restore_workload("app", "maops-app", "maops-app-pdb")
        self.assertFalse(ok)
        mock_pdb_wait.assert_not_called()
        self.assertFalse(any(r_ok for r_ok, _msg in pdb_check.restoration_results))


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        pdb_check.results = []
        pdb_check.restoration_results = []

    def test_verify_context_failure_short_circuits_before_any_scaling(self):
        with mock.patch.object(pdb_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(pdb_check, "run_pdb_experiment") as mock_run:
                exit_code = pdb_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""
Docker/Kubernetes-free unit tests for scripts/final_state_check.py
(DAY3-TEST-H2).

Directly tests the production logic - `_settled_snapshot`'s termination-
race guard, final PDB-state drift detection, the DAY3-INT-L1 Secret
RuntimeError fix, and the DAY3-INT-L2 Day-3-scoped leaked-port-forward
detection - rather than mocking the whole function under test away.

Monkeypatches every kube collaborator so these tests never touch a real
cluster. Timeout-path tests use a fake, manually-advanced clock (same
technique as tests/test_kube.py) so they are deterministic and do not
actually sleep.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import final_state_check
import kube


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _patched_clock(clock: _FakeClock):
    return mock.patch.multiple(kube.time, monotonic=clock.monotonic, sleep=clock.sleep)


def _ready_pod(name: str) -> dict:
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def _deployment(replicas: int, ready: int) -> dict:
    return {"spec": {"replicas": replicas}, "status": {"readyReplicas": ready}}


def _endpointslices(ready_count: int) -> dict:
    return {
        "items": [
            {
                "endpoints": [
                    {"addresses": [f"10.244.0.{i}"], "conditions": {"ready": True}} for i in range(ready_count)
                ]
            }
        ]
    }


class SettledSnapshotTests(unittest.TestCase):
    """DAY3-TEST-H2: `_settled_snapshot` must require the Deployment,
    live Pods, AND EndpointSlice to simultaneously agree on
    EXPECTED_REPLICAS before trusting the result - each of these must
    independently be able to block settlement."""

    def _fake_get_json(self, dep: dict, pods: list[dict], endpointslices: dict):
        def get_json(*args, **_kwargs):
            if "deployment" in args:
                return dep
            if "pods" in args:
                return {"items": pods}
            if "endpointslices" in args:
                return endpointslices
            raise AssertionError(f"unexpected get_json call: {args}")

        return get_json

    def test_deployment_ready_but_fewer_actual_pods_never_settles(self):
        # Deployment status claims 3/3 Ready but only 2 Pod objects
        # actually exist - the termination-race case this guard exists
        # for.
        fake = self._fake_get_json(_deployment(3, 3), [_ready_pod("a"), _ready_pod("b")], _endpointslices(3))
        clock = _FakeClock()
        with mock.patch.object(final_state_check, "get_json", side_effect=fake):
            with _patched_clock(clock):
                with self.assertRaises(TimeoutError):
                    final_state_check._settled_snapshot("maops-app", "sel", "maops-app")

    def test_deployment_and_pods_agree_but_endpointslice_lags_never_settles(self):
        pods = [_ready_pod("a"), _ready_pod("b"), _ready_pod("c")]
        fake = self._fake_get_json(_deployment(3, 3), pods, _endpointslices(2))
        clock = _FakeClock()
        with mock.patch.object(final_state_check, "get_json", side_effect=fake):
            with _patched_clock(clock):
                with self.assertRaises(TimeoutError):
                    final_state_check._settled_snapshot("maops-app", "sel", "maops-app")

    def test_lingering_extra_pod_never_settles(self):
        pods = [_ready_pod("a"), _ready_pod("b"), _ready_pod("c"), _ready_pod("d-lingering")]
        fake = self._fake_get_json(_deployment(3, 3), pods, _endpointslices(3))
        clock = _FakeClock()
        with mock.patch.object(final_state_check, "get_json", side_effect=fake):
            with _patched_clock(clock):
                with self.assertRaises(TimeoutError):
                    final_state_check._settled_snapshot("maops-app", "sel", "maops-app")

    def test_fully_agreeing_3_3_3_state_returns_a_real_snapshot(self):
        pods = [_ready_pod("a"), _ready_pod("b"), _ready_pod("c")]
        dep = _deployment(3, 3)
        fake = self._fake_get_json(dep, pods, _endpointslices(3))
        with mock.patch.object(final_state_check, "get_json", side_effect=fake):
            result_dep, result_pods, result_ready_pods = final_state_check._settled_snapshot("maops-app", "sel", "maops-app")
        self.assertEqual(result_dep, dep)
        self.assertEqual(result_pods, pods)
        self.assertEqual(len(result_ready_pods), 3)


class PdbFinalStateDriftTests(unittest.TestCase):
    """DAY3-TEST-H2: the final PDB-state check
    (currentHealthy=3/desiredHealthy=2/disruptionsAllowed=1) must reject
    drift of each individual field, not just the aggregate."""

    def setUp(self):
        final_state_check.results = []

    def _pdb(self, current_healthy=3, desired_healthy=2, disruptions_allowed=1) -> dict:
        return {
            "spec": {"minAvailable": 2},
            "status": {
                "currentHealthy": current_healthy,
                "desiredHealthy": desired_healthy,
                "disruptionsAllowed": disruptions_allowed,
            },
        }

    def test_healthy_final_state_passes(self):
        with mock.patch.object(final_state_check, "get_json", return_value=self._pdb()):
            final_state_check.check_pdb_final_state("app", "maops-app-pdb")
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))

    def test_current_healthy_drift_fails(self):
        with mock.patch.object(final_state_check, "get_json", return_value=self._pdb(current_healthy=2)):
            final_state_check.check_pdb_final_state("app", "maops-app-pdb")
        self.assertTrue(any(not ok for ok, _msg in final_state_check.results))

    def test_desired_healthy_drift_fails(self):
        with mock.patch.object(final_state_check, "get_json", return_value=self._pdb(desired_healthy=1)):
            final_state_check.check_pdb_final_state("app", "maops-app-pdb")
        self.assertTrue(any(not ok for ok, _msg in final_state_check.results))

    def test_disruptions_allowed_drift_fails(self):
        with mock.patch.object(final_state_check, "get_json", return_value=self._pdb(disruptions_allowed=0)):
            final_state_check.check_pdb_final_state("app", "maops-app-pdb")
        self.assertTrue(any(not ok for ok, _msg in final_state_check.results))


class SecretFinalStateErrorHandlingTests(unittest.TestCase):
    """DAY3-INT-L1: a Secret/API RuntimeError must become a clean recorded
    False result - no traceback may replace the final-state summary."""

    def setUp(self):
        final_state_check.results = []

    def test_runtime_error_is_recorded_not_raised(self):
        with mock.patch.object(final_state_check, "get_existing_secret", side_effect=RuntimeError("namespace gone")):
            final_state_check.check_secret_final_state()  # must not raise
        self.assertTrue(any(not ok and "namespace gone" in msg for ok, msg in final_state_check.results))

    def test_missing_secret_is_recorded_false(self):
        with mock.patch.object(final_state_check, "get_existing_secret", return_value=None):
            final_state_check.check_secret_final_state()
        self.assertTrue(any(not ok and "does not exist" in msg for ok, msg in final_state_check.results))

    def test_valid_secret_is_recorded_true(self):
        with mock.patch.object(final_state_check, "get_existing_secret", return_value={"data": {"internal-token": "x"}}):
            with mock.patch.object(final_state_check, "validate_secret_shape", return_value=(True, "ok")):
                final_state_check.check_secret_final_state()
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))


class LeakedPortForwardScopeTests(unittest.TestCase):
    """DAY3-INT-L2: leaked-port-forward detection must be scoped to THIS
    project's Day 3 context/namespace - an unrelated operator's
    port-forward to a different cluster/project must never be flagged as
    a Day 3 leak."""

    def setUp(self):
        final_state_check.results = []

    def _ps_output(self, lines: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout="\n".join(lines), stderr="")

    def test_day3_port_forward_is_detected(self):
        lines = [
            "  1 /sbin/init",
            f"1234 kubectl --context {kube.CONTEXT} -n {kube.NAMESPACE} port-forward service/maops-gateway 51234:8080",
        ]
        with mock.patch.object(final_state_check.subprocess, "run", return_value=self._ps_output(lines)):
            final_state_check.check_no_leaked_port_forwards()
        self.assertTrue(any(not ok for ok, _msg in final_state_check.results))

    def test_day1_port_forward_is_ignored(self):
        lines = [
            "  1 /sbin/init",
            "1234 kubectl --context kind-maops-k8s-day1 -n maops-platform port-forward service/maops-app 51234:8080",
        ]
        with mock.patch.object(final_state_check.subprocess, "run", return_value=self._ps_output(lines)):
            final_state_check.check_no_leaked_port_forwards()
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))

    def test_unrelated_project_port_forward_is_ignored(self):
        lines = [
            "1234 kubectl --context kind-some-other-project -n other-namespace port-forward service/thing 51234:8080",
        ]
        with mock.patch.object(final_state_check.subprocess, "run", return_value=self._ps_output(lines)):
            final_state_check.check_no_leaked_port_forwards()
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))

    def test_no_port_forward_at_all_passes(self):
        lines = ["  1 /sbin/init", "5678 some-other-process --flag"]
        with mock.patch.object(final_state_check.subprocess, "run", return_value=self._ps_output(lines)):
            final_state_check.check_no_leaked_port_forwards()
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))


class OtherDayClustersStillExistTests(unittest.TestCase):
    """DAY3-INT-I2: the function name/message now claim exactly what is
    proven - existence, not byte-for-byte "untouched"."""

    def setUp(self):
        final_state_check.results = []

    def test_both_clusters_present_passes(self):
        result = subprocess.CompletedProcess(args=["kind"], returncode=0, stdout="maops-k8s-day1\nmaops-k8s-day2\nmaops-k8s-day3\n", stderr="")
        with mock.patch.object(final_state_check.subprocess, "run", return_value=result):
            final_state_check.check_other_day_clusters_still_exist()
        self.assertTrue(all(ok for ok, _msg in final_state_check.results))
        self.assertTrue(all("still exists" in msg for _ok, msg in final_state_check.results))

    def test_missing_cluster_fails(self):
        result = subprocess.CompletedProcess(args=["kind"], returncode=0, stdout="maops-k8s-day3\n", stderr="")
        with mock.patch.object(final_state_check.subprocess, "run", return_value=result):
            final_state_check.check_other_day_clusters_still_exist()
        self.assertTrue(any(not ok for ok, _msg in final_state_check.results))


if __name__ == "__main__":
    unittest.main()

"""
DAY7: Docker/Kubernetes-free unit tests for the experiment-specific pure
logic: Recreate ordering and interruption analysis
(scripts/day7_recreate.py), the Canary unready-candidate observation
(scripts/day7_canary.py), the final check's manifest/process parsing
(scripts/day7_final_check.py), the host preflight's parsing
(scripts/day7_preflight.py), and the baseline capture's refusal paths
(scripts/day7_baseline.py).
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import day7_baseline
import day7_canary
import day7_final_check
import day7_preflight
import day7_recreate
import k8s_yaml
import kube

OLD = frozenset({"old-1", "old-2"})


def _p(uid, name=None, phase="Running", ready=True):
    return {"uid": uid, "name": name or uid, "phase": phase, "ready": ready}


def _snap(t, pods, ready=None):
    return {"t": t, "pods": pods, "ready_pod_names": ready}


class RecreateOrderingTests(unittest.TestCase):
    def test_clean_recreate_has_no_violations(self):
        snaps = [
            _snap(0.0, [_p("old-1"), _p("old-2")], {"old-1", "old-2"}),
            _snap(1.0, [_p("old-1", ready=False), _p("old-2", ready=False)], set()),
            _snap(2.0, [], set()),
            _snap(3.0, [_p("new-1", ready=False), _p("new-2", ready=False)], set()),
            _snap(9.0, [_p("new-1"), _p("new-2")], {"new-1", "new-2"}),
        ]
        ev = day7_recreate.analyze_recreate(snaps, OLD)
        self.assertEqual(ev["violations"], [])
        self.assertEqual((ev["last_old_seen"], ev["first_new_seen"], ev["first_new_ready"]), (1.0, 3.0, 9.0))
        self.assertEqual(ev["new_uids"], frozenset({"new-1", "new-2"}))

    def test_rolling_overlap_is_a_violation(self):
        snaps = [_snap(0.0, [_p("old-1"), _p("old-2")]), _snap(1.0, [_p("old-1"), _p("new-1", ready=False)])]
        ev = day7_recreate.analyze_recreate(snaps, OLD)
        self.assertTrue(any("coexisted" in v for v in ev["violations"]))

    def test_terminal_old_pod_alongside_new_is_not_a_coexistence_violation_but_ordering_still_checked(self):
        snaps = [_snap(0.0, [_p("old-1")]), _snap(1.0, [_p("old-1", phase="Succeeded", ready=False), _p("new-1", ready=False)])]
        ev = day7_recreate.analyze_recreate(snaps, OLD)
        self.assertFalse(any("coexisted" in v for v in ev["violations"]))
        self.assertTrue(any("last seen" in v for v in ev["violations"]))

    def test_simultaneous_ready_endpoints_is_a_violation(self):
        snaps = [_snap(0.0, [_p("old-1", phase="Failed"), _p("new-1")], {"old-1", "new-1"})]
        ev = day7_recreate.analyze_recreate(snaps, OLD)
        self.assertTrue(any("simultaneously" in v for v in ev["violations"]))


class InterruptionAnalysisTests(unittest.TestCase):
    def test_observed_interruption_and_recovery(self):
        labels = [(0.0, "old"), (0.5, "old"), (1.0, "error"), (1.5, "error"), (2.0, "new")] + [(2.5 + i, "new") for i in range(10)]
        s = day7_recreate.analyze_interruption(labels)
        self.assertEqual(s["errors"], 2)
        self.assertEqual(s["error_window"], (1.0, 1.5))
        self.assertTrue(s["recovered"])
        self.assertEqual(s["old_after_new"], 0)
        self.assertEqual([r[0] for r in s["runs"]], ["old", "error", "new"])

    def test_missed_interruption_is_reported_as_none_not_fabricated(self):
        labels = [(0.0, "old")] + [(1.0 + i, "new") for i in range(10)]
        s = day7_recreate.analyze_interruption(labels)
        self.assertEqual(s["errors"], 0)
        self.assertIsNone(s["error_window"])
        self.assertTrue(s["recovered"])

    def test_old_after_new_and_no_recovery_flagged(self):
        labels = [(0.0, "old"), (1.0, "new"), (2.0, "old"), (3.0, "error")]
        s = day7_recreate.analyze_interruption(labels)
        self.assertEqual(s["old_after_new"], 1)
        self.assertFalse(s["recovered"])

    def test_short_tail_is_not_recovery(self):
        self.assertFalse(day7_recreate.analyze_interruption([(0.0, "new")] * 3)["recovered"])


def _cand(name, ready=False, started=True, phase="Running"):
    return {
        "metadata": {"name": name},
        "status": {"phase": phase, "containerStatuses": [{"started": started}], "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


class UnreadyObservationTests(unittest.TestCase):
    def test_alive_but_unready(self):
        self.assertTrue(day7_canary.unready_observation_verdict([_cand("a"), _cand("b")], 0, 2)[0])

    def test_any_ready_pod_or_endpoint_fails(self):
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a", ready=True), _cand("b")], 0, 2)[0])
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a"), _cand("b")], 1, 2)[0])
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a"), _cand("b")], None, 2)[0])

    def test_not_started_or_missing_pods_are_not_yet_proof(self):
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a", started=False), _cand("b")], 0, 2)[0])
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a")], 0, 2)[0])
        self.assertFalse(day7_canary.unready_observation_verdict([_cand("a", phase="Pending"), _cand("b")], 0, 2)[0])


STABLE_MANIFEST = """---
apiVersion: v1
kind: Service
metadata:
  name: maops-gateway
  labels:
    app.kubernetes.io/component: gateway
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: maops-gateway-route
spec:
  rules:
    - backendRefs:
        - name: maops-gateway
          port: 8080
"""


class FinalCheckParsingTests(unittest.TestCase):
    def test_stable_manifest_has_no_candidate_objects(self):
        self.assertEqual(day7_final_check.manifest_candidate_objects(STABLE_MANIFEST), [])
        self.assertEqual(day7_final_check.manifest_route_backends(STABLE_MANIFEST), [{"name": "maops-gateway", "port": 8080}])

    def test_candidate_leftover_in_manifest_detected(self):
        text = STABLE_MANIFEST + "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  labels:\n    app.kubernetes.io/component: gateway-candidate\n"
        self.assertEqual(day7_final_check.manifest_candidate_objects(text), ["ConfigMap/x"])

    def test_leaked_processes_scoped_to_day7_context(self):
        ps = "\n".join([
            "  PID ARGS",
            f" 11 /usr/local/bin/kubectl --kubeconfig k --context {kube.CONTEXT} -n maops-platform port-forward svc/x 1:2",
            f" 12 helm upgrade {kube.HELM_RELEASE_NAME} chart --kube-context {kube.CONTEXT}",
            " 13 kubectl --context kind-some-other port-forward svc/x 1:2",
            f" 14 python3 scripts/day7_final_check.py --context {kube.CONTEXT}",
        ])
        leaked = day7_final_check.leaked_processes(ps)
        self.assertEqual(len(leaked), 2)
        self.assertTrue(leaked[0].startswith("11"))
        self.assertTrue(leaked[1].startswith("12"))


class PreflightParsingTests(unittest.TestCase):
    def _doc(self):
        return k8s_yaml.load_all((Path(__file__).resolve().parent.parent / "kind" / "cluster-day7.yaml").read_text())[0]

    def test_real_day7_kind_config_passes(self):
        self.assertTrue(all(ok for ok, _ in day7_preflight.kind_config_findings(self._doc())))

    def test_day6_host_port_or_name_rejected(self):
        doc = self._doc()
        doc["nodes"][0]["extraPortMappings"][0]["hostPort"] = 18080
        self.assertFalse(all(ok for ok, _ in day7_preflight.kind_config_findings(doc)))
        doc = self._doc()
        doc["name"] = "maops-k8s-day6"
        self.assertFalse(all(ok for ok, _ in day7_preflight.kind_config_findings(doc)))

    def test_meminfo_parsing(self):
        self.assertAlmostEqual(day7_preflight.meminfo_available_gib("MemTotal: 1 kB\nMemAvailable:    8388608 kB\n"), 8.0)
        self.assertIsNone(day7_preflight.meminfo_available_gib("MemTotal: 1 kB\n"))

    def test_port_bindable_detects_a_busy_port(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            self.assertFalse(day7_preflight.port_bindable(s.getsockname()[1])[0])
        finally:
            s.close()


class BaselineCaptureRefusalTests(unittest.TestCase):
    def _capture(self, env):
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(day7_baseline.d7, "require_day7_profile"), mock.patch.object(kube, "verify_context"), mock.patch.object(day7_baseline, "collect") as collect:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = day7_baseline.capture()
        return rc, err.getvalue(), collect

    def test_missing_environment_refused_before_any_read(self):
        rc, err, collect = self._capture({})
        self.assertEqual(rc, 1)
        collect.assert_not_called()

    def test_baselines_outside_the_run_dir_refused(self):
        rc, err, collect = self._capture({"DAY7_RUN_ID": "r1", "DAY7_BASELINE_DIR": "/home/x/runs/r1", "DAY7_STRATEGY_BASELINE_PATH": "/home/x/elsewhere/s.json", "DAY7_SUITE_BASELINE_PATH": "/home/x/runs/r1/suite.json"})
        self.assertEqual(rc, 1)
        self.assertIn("directly in the run directory", err)
        collect.assert_not_called()

    def test_existing_baseline_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "strategy-baseline.json"
            path.write_text("{}")
            env = {"DAY7_RUN_ID": "r1", "DAY7_BASELINE_DIR": d, "DAY7_STRATEGY_BASELINE_PATH": str(path), "DAY7_SUITE_BASELINE_PATH": str(Path(d) / "suite.json")}
            with mock.patch.object(day7_baseline.private_run_dir, "validate_run_dir"), mock.patch.object(day7_baseline.private_run_dir, "validate_private_file"):
                rc, err, collect = self._capture(env)
            self.assertEqual(rc, 1)
            self.assertIn("never overwritten", err)
            self.assertEqual(path.read_text(), "{}")
            collect.assert_not_called()

    def test_write_exclusive_creates_0600_and_refuses_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            day7_baseline.write_exclusive(path, {"a": 1})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                day7_baseline.write_exclusive(path, {"a": 2})


if __name__ == "__main__":
    unittest.main()

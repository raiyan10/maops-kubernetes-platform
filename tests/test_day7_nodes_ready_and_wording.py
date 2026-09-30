"""
DAY7 follow-up to live run dd99769f587d4a869628d51b9725b458 -
Docker/Kubernetes-free tests for:

  1. scripts/day7_nodes_ready.py, the bounded Day 7-only node readiness
     wait between cni-install and cni-status: delayed readiness, timeout,
     API failures (never retried into a pass), wrong profile / context /
     kubeconfig / cluster, and its Makefile position - while cni-status
     stays an unchanged single-snapshot check;
  2. profile-correct "Day N" wording in the reused live scripts: Day 7
     output says Day 7, Day 6 output is byte-for-byte what it was.

Profile-dependent output is produced in fresh subprocesses, so the
shared `kube` module other tests import is never re-profiled. kubectl is
never executed: kube.run is replaced everywhere.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import day7_nodes_ready as nr
import kube
import make_sequence

DAY7_RE = re.compile(r"^maops-k8s-day7-(control-plane|worker\d*)$")


def _node(name, ready=True, control_plane=False):
    labels = {kube.CONTROL_PLANE_LABEL: ""} if control_plane else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def _nodes(ready=(True, True, True), prefix="maops-k8s-day7"):
    return [_node(f"{prefix}-control-plane", ready[0], True), _node(f"{prefix}-worker", ready[1]), _node(f"{prefix}-worker2", ready[2])]


def _completed(items=None, rc=0, stderr="", stdout=None):
    out = stdout if stdout is not None else json.dumps({"items": items or []})
    return subprocess.CompletedProcess(["kubectl"], rc, out, stderr)


class _Day7(unittest.TestCase):
    def setUp(self):
        for p in (
            mock.patch.object(kube, "PROFILE", "day7"),
            mock.patch.object(kube, "CONTEXT", "kind-maops-k8s-day7"),
            mock.patch.object(kube, "CLUSTER_NAME", "maops-k8s-day7"),
            mock.patch.object(kube, "KUBECONFIG_PATH", str(Path.home() / ".kube" / "maops-k8s-day7.config")),
            mock.patch.object(kube, "_DAY_NODE_NAME_RE", DAY7_RE),
        ):
            p.start()
            self.addCleanup(p.stop)

    def clock(self):
        t = [0.0]
        return (lambda s: t.__setitem__(0, t[0] + s)), (lambda: t[0])


class EvaluateNodesTests(_Day7):
    def test_all_three_ready(self):
        ok, detail = nr.evaluate_nodes(_nodes())
        self.assertTrue(ok)
        self.assertIn("3/3 nodes Ready", detail)

    def test_not_ready_node_is_not_success(self):
        self.assertFalse(nr.evaluate_nodes(_nodes((True, False, True)))[0])

    def test_wrong_node_count_or_control_plane_count(self):
        self.assertFalse(nr.evaluate_nodes(_nodes()[:2])[0])
        extra_cp = _nodes()
        extra_cp[1]["metadata"]["labels"][kube.CONTROL_PLANE_LABEL] = ""
        self.assertFalse(nr.evaluate_nodes(extra_cp)[0])

    def test_foreign_cluster_nodes_are_a_hard_error(self):
        with self.assertRaises(nr.NodeReadinessError):
            nr.evaluate_nodes(_nodes(prefix="maops-k8s-day6"))


class WaitTests(_Day7):
    def test_delayed_readiness_succeeds_within_the_bound(self):
        sleep, mono = self.clock()
        responses = [_completed(_nodes((True, False, False))), _completed(_nodes((True, False, False))), _completed(_nodes())]
        with mock.patch.object(kube, "run", side_effect=responses) as run, redirect_stdout(io.StringIO()):
            ok, detail = nr.wait_for_nodes(timeout=60, poll=3, sleep=sleep, monotonic=mono)
        self.assertTrue(ok)
        self.assertIn("after 3 observation(s)", detail)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[:4], ("get", "nodes", "-o", "json"))

    def test_timeout_fails(self):
        sleep, mono = self.clock()
        with mock.patch.object(kube, "run", return_value=_completed(_nodes((True, True, False)))), redirect_stdout(io.StringIO()):
            ok, detail = nr.wait_for_nodes(timeout=10, poll=3, sleep=sleep, monotonic=mono)
        self.assertFalse(ok)
        self.assertIn("timed out after 10s", detail)
        self.assertIn("2/3 nodes Ready", detail)

    def test_api_failures_are_fatal_and_never_retried(self):
        for failure in (_completed(rc=1, stderr="connection refused"), _completed(stdout="not json"), _completed(stdout='{"kind": "List"}'), subprocess.TimeoutExpired("kubectl", 20)):
            with self.subTest(failure=failure):
                sleep, mono = self.clock()
                side = [_completed(_nodes((True, False, False))), failure]
                with mock.patch.object(kube, "run", side_effect=side) as run, redirect_stdout(io.StringIO()):
                    with self.assertRaises(nr.NodeReadinessError):
                        nr.wait_for_nodes(timeout=60, poll=3, sleep=sleep, monotonic=mono)
                self.assertEqual(run.call_count, 2)


class ScopeTests(_Day7):
    def _main(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = nr.main()
        return rc, err.getvalue()

    def test_wrong_profile_or_context_refuses_before_any_kubectl_call(self):
        for patch in (mock.patch.object(kube, "PROFILE", "day6"), mock.patch.object(kube, "CONTEXT", "kind-maops-k8s-day6")):
            with self.subTest(patch=patch.attribute), patch, mock.patch.object(kube, "run", side_effect=AssertionError("kubectl must not run")) as run, mock.patch.object(kube, "verify_context") as verify:
                rc, err = self._main()
            self.assertEqual(rc, 1)
            self.assertIn("refusing to run outside the Day 7 cluster", err)
            run.assert_not_called()
            verify.assert_not_called()

    def test_day6_kubeconfig_is_refused(self):
        with mock.patch.object(kube, "KUBECONFIG_PATH", str(Path.home() / ".kube" / "maops-k8s-day6.config")), mock.patch.object(kube, "run", side_effect=AssertionError("kubectl must not run")):
            rc, err = self._main()
        self.assertEqual(rc, 1)
        self.assertIn("names another day's cluster", err)

    def test_failed_context_verification_fails(self):
        with mock.patch.object(kube, "verify_context", side_effect=RuntimeError("node names do not all belong")), mock.patch.object(kube, "run", side_effect=AssertionError("no polling after a failed context check")):
            rc, err = self._main()
        self.assertEqual(rc, 1)
        self.assertIn("do not all belong", err)

    def test_success_path(self):
        with mock.patch.object(kube, "verify_context"), mock.patch.object(kube, "run", return_value=_completed(_nodes())):
            rc, _ = self._main()
        self.assertEqual(rc, 0)

    def test_real_day7_profile_resolves_to_the_day7_scope(self):
        env = {k: v for k, v in os.environ.items() if k not in ("KUBECONFIG_PATH",)}
        env["MAOPS_CLUSTER_PROFILE"] = "day7"
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import day7_nodes_ready as n; n.require_day7_scope(); print('ok')"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)
        env["MAOPS_CLUSTER_PROFILE"] = "day6"
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import day7_nodes_ready as n; n.require_day7_scope()"], capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(out.returncode, 0)


class MakefileOrderTests(unittest.TestCase):
    MAKEFILE = (REPO / "Makefile").read_text()

    def test_wait_sits_between_cni_install_and_cni_status(self):
        seq = [n for _, n in make_sequence.steps("day7-check", self.MAKEFILE)]
        i = seq.index("day7-nodes-ready")
        self.assertEqual(seq[i - 1], "cni-install")
        self.assertEqual(seq[i + 1], "cni-status")
        self.assertEqual(seq.count("day7-nodes-ready"), 1)

    def test_wait_runs_under_the_day7_env_only_and_not_in_day6(self):
        recipe = make_sequence.recipe("day7-nodes-ready", self.MAKEFILE)
        self.assertIn("env $(DAY7_ENV) python3 scripts/day7_nodes_ready.py", recipe)
        self.assertNotIn("day7-nodes-ready", [n for _, n in make_sequence.steps("day6-check", self.MAKEFILE)])

    def test_cni_status_stays_a_single_snapshot(self):
        self.assertEqual(make_sequence.recipe("cni-status", self.MAKEFILE).strip(), "python3 scripts/cni_check.py")
        text = (SCRIPTS / "cni_check.py").read_text()
        for waiting in ("wait_until", "time.sleep", "sleep("):
            self.assertNotIn(waiting, text)

    def test_cni_status_still_fails_on_the_observed_1_of_3_snapshot(self):
        """The exact 09:33:21 observation must still FAIL cni-status: the
        new wait adds time before the snapshot, it never relaxes it."""
        import cni_check
        snapshot = {"items": _nodes((True, False, False))}
        cni_check.results = []
        with mock.patch.object(cni_check, "get_json", return_value=snapshot) as get_json, redirect_stdout(io.StringIO()):
            cni_check.check_nodes_ready()
        get_json.assert_called_once()
        ok, message = cni_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("1/3 nodes Ready (expected 3/3)", message)


# ---------------------------------------------------------------------------
# Profile-correct wording
# ---------------------------------------------------------------------------

HEADERS = {
    "cni_check": "CNI status check: Cilium + kube-proxy",
    "context_check": "context/topology/node-version verification",
    "mesh_status": "mesh status check: istiod/istio-cni/ztunnel",
    "mesh_check": "mesh check: ambient enrollment",
    "ambient_workload_check": "ambient workload check: per-Pod ztunnel listeners",
    "networkpolicy_check": "NetworkPolicy check: default-deny + explicit allows",
    "gateway_check": "Gateway API check: GatewayClass/Gateway/HTTPRoute",
}

_HARNESS = r"""
import sys, io, json, contextlib
sys.path.insert(0, SCRIPTS)
from unittest import mock
import kube
out = {}
def stop(*a, **k):
    raise RuntimeError("stopped before any cluster contact")
with mock.patch.object(kube, "verify_context", side_effect=stop), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")):
    for name in NAMES:
        mod = __import__(name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                mod.main()
            except Exception:
                pass
        out[name] = buf.getvalue().splitlines()[0] if buf.getvalue() else ""
    import cni_check, final_state_check, context_check
    ready = {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
    with mock.patch.object(cni_check, "get_json", return_value=ready):
        cni_check.results = []
        cni_check.check_kube_proxy_still_enabled()
        out["kube_proxy"] = cni_check.results[-1][1]
    final_state_check.results = []
    ps = mock.Mock(stdout="  PID ARGS\n")
    with mock.patch.object(final_state_check.subprocess, "run", return_value=ps):
        final_state_check.check_no_leaked_port_forwards()
    out["port_forward"] = final_state_check.results[-1][1]
    buf = io.StringIO()
    with mock.patch.object(context_check.scheduling_check, "check_node_topology"), mock.patch.object(context_check, "get_json", return_value={"serverVersion": {"gitVersion": context_check.EXPECTED_K8S_VERSION}}), mock.patch.object(kube, "verify_context"), contextlib.redirect_stdout(buf):
        context_check.results = []
        context_check.scheduling_check.results = []
        context_check.main()
    out["context_pass"] = buf.getvalue().splitlines()[-1]
print(json.dumps(out))
"""


def _profile_output(profile: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("MAOPS_CLUSTER_PROFILE", "KUBECONFIG_PATH")}
    if profile:
        env["MAOPS_CLUSTER_PROFILE"] = profile
    code = f"SCRIPTS = {str(SCRIPTS)!r}\nNAMES = {list(HEADERS)!r}\n" + _HARNESS
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class WordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.day6 = _profile_output("")
        cls.day7 = _profile_output("day7")

    def test_headers_name_the_active_day(self):
        for name, fragment in HEADERS.items():
            with self.subTest(script=name):
                self.assertTrue(self.day6[name].startswith(f"# Day 6 {fragment}"), self.day6[name])
                self.assertTrue(self.day7[name].startswith(f"# Day 7 {fragment}"), self.day7[name])
                self.assertIn("kind-maops-k8s-day7", self.day7[name])
                self.assertNotIn("Day 6", self.day7[name])

    def test_day6_messages_are_unchanged(self):
        self.assertEqual(self.day6["kube_proxy"], "kube-proxy DaemonSet: 1/1 Pods Ready (Day 6 does not disable kube-proxy)")
        self.assertEqual(self.day6["port_forward"], "no leaked Day 6 (kind-maops-k8s-day6/maops-platform) kubectl port-forward processes (found 0: [])")
        self.assertEqual(self.day6["context_pass"], "PASS: Day 6 context, topology, and node version verified")

    def test_day7_messages_say_day7(self):
        self.assertEqual(self.day7["kube_proxy"], "kube-proxy DaemonSet: 1/1 Pods Ready (Day 7 does not disable kube-proxy)")
        self.assertEqual(self.day7["port_forward"], "no leaked Day 7 (kind-maops-k8s-day7/maops-platform) kubectl port-forward processes (found 0: [])")
        self.assertEqual(self.day7["context_pass"], "PASS: Day 7 context, topology, and node version verified")

    def test_suite_baseline_message_is_profile_worded(self):
        text = (SCRIPTS / "final_state_check.py").read_text()
        self.assertIn('f"captured before any Day {kube.PROFILE_DAY} mutating experiment', text)
        self.assertNotIn("captured before any Day 6 mutating experiment", text)


if __name__ == "__main__":
    unittest.main()

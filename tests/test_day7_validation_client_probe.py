"""
DAY7: Docker/Kubernetes-free tests for scripts/day7_validation_client_probe.py
(the bounded live validation-client -> gateway-candidate denial probe).

Pins the fail-closed contract: a denial is reported only with a verified
destination, a passing same-source positive control, no HTTP response,
a correlated Cilium "Policy denied" drop from THIS probe Pod to a
candidate Pod on :8080, and identities resolving to the validation client
and the candidate. A timeout alone is inconclusive. Also: profile guards
on every cluster-reaching leaf, and cleanup (monitors reaped, probe Pod
deleted) even when the probe body raises.

kubectl is blocked or faked everywhere; nothing contacts a cluster.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import day7_strategy as d7
import day7_validation_client_probe as vc
import k8s_yaml
import kube

PROBE_IP = "10.244.2.14"
CAND_IPS = {"10.244.1.29", "10.244.2.30"}
DENY_LINE = (
    "xx drop (Policy denied) flow 0x0 to endpoint 812, ifindex 9, file bpf_lxc.c:2110, , "
    f"identity 51234->33901: {PROBE_IP}:40312 -> 10.244.1.29:8080 tcp SYN"
)
SRC_LABELS = '[{"labels":["k8s:io.kubernetes.pod.namespace=maops-day7-validation","k8s:app.kubernetes.io/component=validation-client"]}]'
DST_LABELS = '[{"labels":["k8s:io.kubernetes.pod.namespace=maops-platform","k8s:app.kubernetes.io/component=gateway-candidate"]}]'


def _quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _good_evidence() -> vc.DenialEvidence:
    dest = d7.Destination(d7.CANDIDATE_SERVICE, "10.96.0.50", 2)
    return vc.DenialEvidence(
        negative_results=[d7.ProbeResult("connect_timeout", "4", "10.96.0.50")] * 2,
        control=d7.ProbeResult("status", "200", "10.96.0.80"),
        control_dest=d7.Destination("maops-edge-istio.maops-ingress", "10.96.0.80", 1),
        dest=dest,
        probe_ip=PROBE_IP,
        candidate_ips=set(CAND_IPS),
        correlated=vc.correlated_denials(vc.parse_drop_events(DENY_LINE, "w1"), PROBE_IP, CAND_IPS),
        identity_ok=True,
        identity_detail="ok",
        monitors_ok=True,
    )


def _failed(ev) -> list[str]:
    return [m for ok, m in vc.evaluate(ev) if not ok]


class DropParsingTests(unittest.TestCase):
    def test_parses_policy_denied_line(self):
        [e] = vc.parse_drop_events(DENY_LINE, "worker")
        self.assertEqual((e.reason, e.src_identity, e.dst_identity, e.src, e.dst, e.dport, e.node),
                         ("Policy denied", "51234", "33901", PROBE_IP, "10.244.1.29", 8080, "worker"))

    def test_ignores_non_drop_and_garbage(self):
        self.assertEqual(vc.parse_drop_events("Listening for events on 8 CPUs\n\ngarbage -> x\n"), [])

    def test_correlation_requires_reason_source_destination_and_port(self):
        base = DENY_LINE
        variants = {
            "other reason": base.replace("Policy denied", "Stale or unroutable IP"),
            "other source": base.replace(f"{PROBE_IP}:40312", "10.244.2.99:40312"),
            "non-candidate destination": base.replace("10.244.1.29:8080", "10.244.1.77:8080"),
            "other port": base.replace("10.244.1.29:8080", "10.244.1.29:15008"),
        }
        self.assertEqual(len(vc.correlated_denials(vc.parse_drop_events(base), PROBE_IP, CAND_IPS)), 1)
        for name, line in variants.items():
            with self.subTest(name):
                self.assertEqual(vc.correlated_denials(vc.parse_drop_events(line), PROBE_IP, CAND_IPS), [])


class IdentityTests(unittest.TestCase):
    def test_expected_identities(self):
        with mock.patch.object(kube, "NAMESPACE", "maops-platform"):
            ok, _ = vc.identity_labels_ok(SRC_LABELS, DST_LABELS, "maops-day7-validation")
        self.assertTrue(ok)

    def test_wrong_or_missing_identities_fail(self):
        with mock.patch.object(kube, "NAMESPACE", "maops-platform"):
            for src, dst in ((DST_LABELS, DST_LABELS), (SRC_LABELS, SRC_LABELS), ("", DST_LABELS), (SRC_LABELS, ""),
                             (SRC_LABELS.replace("day7", "day6"), DST_LABELS),
                             (SRC_LABELS, DST_LABELS.replace("gateway-candidate", "gateway"))):
                with self.subTest(src=src[:40], dst=dst[:40]):
                    self.assertFalse(vc.identity_labels_ok(src, dst, "maops-day7-validation")[0])


class EvaluateTests(unittest.TestCase):
    def test_complete_evidence_passes(self):
        self.assertEqual(_failed(_good_evidence()), [])

    def test_timeout_without_correlated_drop_is_inconclusive(self):
        ev = _good_evidence()
        ev.correlated = []
        ev.identity_ok = False
        failed = _failed(ev)
        self.assertTrue(any("authoritative" in m and "INCONCLUSIVE" in m for m in failed))

    def test_each_single_missing_element_fails(self):
        mutations = {
            "no probe ip": lambda e: setattr(e, "probe_ip", None),
            "destination unverified": lambda e: setattr(e, "dest", d7.Destination(d7.CANDIDATE_SERVICE, "10.96.0.50", 0)),
            "no candidate ips": lambda e: setattr(e, "candidate_ips", set()),
            "control not run": lambda e: setattr(e, "control", None),
            "control failed": lambda e: setattr(e, "control", d7.ProbeResult("connect_timeout", "4", "10.96.0.80")),
            "control resolved elsewhere": lambda e: setattr(e, "control", d7.ProbeResult("status", "200", "10.96.9.9")),
            "control destination unverified": lambda e: setattr(e, "control_dest", d7.Destination("x", None, None)),
            "monitors not ok": lambda e: setattr(e, "monitors_ok", False),
            "negative not run": lambda e: setattr(e, "negative_results", []),
            "candidate answered": lambda e: setattr(e, "negative_results", [d7.ProbeResult("status", "403", "10.96.0.50")]),
            "negative resolved elsewhere": lambda e: setattr(e, "negative_results", [d7.ProbeResult("connect_timeout", "4", "10.96.7.7")]),
            "negative exec failed": lambda e: setattr(e, "negative_results", [d7.ProbeResult("exec_failed", "x")]),
            "no correlated drop": lambda e: setattr(e, "correlated", []),
            "identities wrong": lambda e: setattr(e, "identity_ok", False),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                ev = _good_evidence()
                mutate(ev)
                self.assertTrue(_failed(ev), f"{name} must fail the verdict")


class SnippetAndManifestTests(unittest.TestCase):
    def test_snippet_uses_host_header_and_compiles(self):
        s = vc.vc_probe_snippet("maops-edge-istio.maops-ingress.svc.cluster.local", 80, "/livez", "maops.local")
        compile(s, "snippet", "exec")
        self.assertIn("headers={'Host': 'maops.local'}", s)
        for placeholder in ("__HOST__", "__PORT__", "__TIMEOUT__", "__PATH__", "__HOSTHDR__"):
            self.assertNotIn(placeholder, s)

    def test_default_host_header_is_the_target(self):
        s = vc.vc_probe_snippet("maops-gateway-candidate.maops-platform.svc.cluster.local", 8080)
        self.assertIn("'Host': 'maops-gateway-candidate.maops-platform.svc.cluster.local'", s)

    def test_output_contract_is_the_shared_one(self):
        self.assertEqual(vc.vc_probe_snippet("h", 1).replace("c.request('GET', '/livez', headers={'Host': 'h'})", ""),
                         d7.probe_snippet("h", 1).replace("c.request('GET', '/livez', headers={'Host': H})", ""))

    def test_rejects_injection(self):
        for bad in ("a'b", "a\nb", "a\\b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vc.vc_probe_snippet("h", 1, host_header=bad)

    def test_probe_pod_is_an_unprivileged_validation_client(self):
        with mock.patch.object(kube, "VALIDATION_NAMESPACE", "maops-day7-validation"):
            [pod] = k8s_yaml.load_all(vc.probe_pod_manifest("p1", "run1"))
        self.assertEqual(pod["metadata"]["namespace"], "maops-day7-validation")
        self.assertEqual(pod["metadata"]["labels"]["app.kubernetes.io/component"], "validation-client")
        spec = pod["spec"]
        self.assertIs(spec["automountServiceAccountToken"], False)
        self.assertLessEqual(spec["activeDeadlineSeconds"], 300)
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        c = spec["containers"][0]
        self.assertEqual(c["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertIs(c["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertNotIn("env", c)
        self.assertNotIn("volumes", spec)


class ProfileGuardTests(unittest.TestCase):
    """Every leaf that can reach a cluster refuses outside Day 7."""

    def test_leaves_refuse_under_day6(self):
        popen = mock.Mock()
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch("subprocess.run") as run:
            for call in (lambda: vc._apply("x"), lambda: vc.exec_in_probe("h", 1, "/"),
                         lambda: vc.start_monitors([{"metadata": {"name": "cilium-x"}}], popen=popen),
                         lambda: vc.resolve_identity_labels("cilium-x", "1"), vc.read_ingress_destination):
                with self.assertRaises(RuntimeError):
                    call()
        run.assert_not_called()
        popen.assert_not_called()

    def test_main_refuses_under_day6_without_mutation(self):
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            self.assertEqual(_quiet(vc.main), 1)
        run.assert_not_called()
        popen.assert_not_called()


class _Proc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(d7, "OWNED_CHILDREN", [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_stop_monitors_reaps_every_child_and_reads_capture(self):
        procs = [_Proc(), _Proc()]
        monitors = []
        for i, p in enumerate(procs):
            sink = io.BytesIO(f"{DENY_LINE}\n".encode())
            sink.seek(0, io.SEEK_END)
            monitors.append(vc.Monitor(f"cilium-{i}", f"node{i}", p, sink))
        with mock.patch.object(d7, "reap_child", side_effect=lambda p: (setattr(p, "terminated", True), "terminated")[1]) as reap:
            captures, outcomes = vc.stop_monitors(monitors)
        self.assertEqual(reap.call_count, 2)
        self.assertTrue(all(p.terminated for p in procs))
        self.assertEqual(sorted(captures), ["node0", "node1"])
        self.assertTrue(all(m.out.closed for m in monitors))
        self.assertEqual(len(vc.parse_drop_events(captures["node0"])), 1)

    def _run_body(self, exec_side_effect):
        """body() with every cluster call faked; returns (result, deleted, stopped)."""
        rec = d7.Recorder()
        candidate = {"metadata": {"name": "cand-1"}, "status": {"podIP": "10.244.1.29", "conditions": [{"type": "Ready", "status": "True"}]}}
        agents = [{"metadata": {"name": f"cilium-{i}"}, "spec": {"nodeName": f"n{i}"}} for i in range(3)]
        probe_pod = {"spec": {"nodeName": "n1"}, "status": {"phase": "Running", "podIP": PROBE_IP, "conditions": [{"type": "Ready", "status": "True"}]}}
        deleted, stopped = [], []
        patches = [
            mock.patch("day7_blue_green.entry_gate", return_value=True),
            mock.patch.object(d7, "apply_stage", return_value=True),
            mock.patch.object(d7, "load_stage_values", return_value={"candidate": {"replicas": 1}, "routing": {}}),
            mock.patch.object(d7, "expected_backends", return_value=()),
            mock.patch.object(d7, "wait_route", return_value=object()),
            mock.patch.object(d7, "run_gate", return_value=(True, [])),
            mock.patch.object(d7, "list_json", side_effect=lambda r, sel=None, namespace=None: agents if namespace == "kube-system" else [candidate]),
            mock.patch.object(d7, "read_destination", return_value=d7.Destination(d7.CANDIDATE_SERVICE, "10.96.0.50", 1)),
            mock.patch.object(d7, "exec_probe", return_value=d7.ProbeResult("status", "200", "127.0.0.1")),
            mock.patch.object(d7, "get_json_or_none", return_value=("found", {"metadata": {"labels": {}}})),
            mock.patch.object(vc, "_apply"),
            mock.patch.object(vc, "wait_probe_pod_running", return_value=probe_pod),
            mock.patch.object(vc, "read_ingress_destination", return_value=d7.Destination("gw", "10.96.0.80", 1)),
            mock.patch.object(vc, "exec_in_probe", side_effect=exec_side_effect),
            mock.patch.object(vc, "start_monitors", return_value=[vc.Monitor("cilium-0", "n0", _Proc(), io.BytesIO())]),
            mock.patch.object(vc, "stop_monitors", side_effect=lambda ms: (stopped.append(len(ms)), ({}, []))[1]),
            mock.patch.object(vc, "save_captures"),
            mock.patch.object(vc, "delete_probe_pod", side_effect=lambda r: deleted.append(True) or True),
            mock.patch.object(vc, "MONITOR_ATTACH_SECONDS", 0),
            mock.patch.object(vc, "MONITOR_TAIL_SECONDS", 0),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            try:
                result = _quiet(vc.body, rec, {})
            except RuntimeError:
                result = "raised"
        return result, deleted, stopped

    def test_probe_pod_deleted_and_monitors_stopped_when_negative_probe_raises(self):
        calls = iter([d7.ProbeResult("status", "200", "10.96.0.80")])

        def side_effect(host, *a, **k):
            if "ingress" in host:
                return next(calls)
            raise RuntimeError("boom")

        result, deleted, stopped = self._run_body(side_effect)
        self.assertEqual(result, "raised")
        self.assertEqual(deleted, [True])
        self.assertEqual(stopped, [1])

    def test_timeout_without_drop_evidence_fails(self):
        def side_effect(host, *a, **k):
            if "ingress" in host:
                return d7.ProbeResult("status", "200", "10.96.0.80")
            return d7.ProbeResult("connect_timeout", "4", "10.96.0.50")

        result, deleted, stopped = self._run_body(side_effect)
        self.assertIs(result, False)
        self.assertEqual(deleted, [True])


class MakefileTargetTests(unittest.TestCase):
    """The probe is an optional, locked, Day 7-profile target that no gate
    sequence runs implicitly."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_day7_makefile as tm
        self.tm = tm

    def test_recipe_is_locked_day7_and_runs_the_probe(self):
        recipe = self.tm._recipe("day7-validation-client-probe")
        self.assertIn("$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_validation_client_probe.py", recipe)
        self.assertIn("day7-validation-client-probe", self.tm.MAKEFILE.split("help:")[0], "declared .PHONY")

    def test_not_part_of_any_gate_sequence(self):
        for gate in ("day7-check", "day7-final-gate", "day7-resume-check"):
            with self.subTest(gate=gate):
                self.assertNotIn("day7-validation-client-probe", [name for _, name in self.tm._steps(gate)])


if __name__ == "__main__":
    unittest.main()

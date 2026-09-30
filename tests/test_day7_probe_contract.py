"""
DAY7 probe/verdict contract - Docker/Kubernetes-free tests.

The in-Pod probe snippet is EXECUTED here, in-process, against local
127.0.0.1 sockets (HTTP 200/503, refused, reset-without-response, silent
server -> read timeout); DNS failure, connect timeout and unreachable
host are injected by patching `socket`, so no external DNS query and no
cluster is ever involved. Then: output parsing, kubectl exec failure
classification, the fail-closed path_verdict() table, the full
check_candidate_mesh_paths() plan, and that an inconclusive observation
submits NO promotion stage - through promote() and through a real
strategy body.
"""

from __future__ import annotations

import http.server
import io
import socket
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_blue_green
import day7_recreate
import day7_strategy as d7
import kube
from test_day7_orchestration import BASELINE, FakeCluster, _Base

REAL_PATH_CHECK = d7.check_candidate_mesh_paths


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def run_snippet(host: str, port: int, timeout: float = 0.5) -> str:
    """Executes the real probe snippet in-process; returns its stdout."""
    code = d7.PROBE_SNIPPET.replace("__HOST__", host).replace("__PORT__", str(port)).replace("__TIMEOUT__", str(timeout))
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            exec(compile(code, "probe", "exec"), {"__name__": "probe"})
        except SystemExit:
            pass
    return out.getvalue()


class _Server:
    """A local listener with a chosen behavior: 'http200', 'http503',
    'close' (accept, then close without any response) or 'silent'."""

    def __init__(self, behavior: str):
        self.behavior = behavior
        if behavior.startswith("http"):
            code = int(behavior[4:])

            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

                def log_message(self, *a):
                    pass

            self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
            self.port = self.httpd.server_address[1]
            self.thread = threading.Thread(target=self.httpd.handle_request, daemon=True)
        else:
            self.sock = socket.socket()
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(1)
            self.port = self.sock.getsockname()[1]
            self.thread = threading.Thread(target=self._raw, daemon=True)
        self.thread.start()

    def _raw(self):
        conn, _ = self.sock.accept()
        if self.behavior == "close":
            conn.recv(1024)
            conn.close()
        else:
            threading.Event().wait(2)
            conn.close()

    def close(self):
        if hasattr(self, "httpd"):
            self.httpd.server_close()
        else:
            self.sock.close()


class SnippetBehaviorTests(unittest.TestCase):
    def _probe(self, behavior):
        server = _Server(behavior)
        try:
            return d7.parse_probe_output(run_snippet("127.0.0.1", server.port))
        finally:
            server.close()

    def test_http_response_is_status_with_resolved_address(self):
        self.assertEqual(self._probe("http200"), d7.ProbeResult("status", "200", "127.0.0.1"))
        self.assertEqual(self._probe("http503"), d7.ProbeResult("status", "503", "127.0.0.1"))

    def test_close_without_response_is_reset(self):
        result = self._probe("close")
        self.assertEqual((result.kind, result.detail, result.addr), ("reset", "request", "127.0.0.1"))

    def test_silent_destination_is_read_timeout(self):
        result = self._probe("silent")
        self.assertEqual((result.kind, result.addr), ("read_timeout", "127.0.0.1"))

    def test_refused_connect(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        result = d7.parse_probe_output(run_snippet("127.0.0.1", port))
        self.assertEqual((result.kind, result.detail), ("refused", "connect"))

    def test_dns_failure_is_its_own_class_without_an_address(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
            result = d7.parse_probe_output(run_snippet("maops-state", 8080))
        self.assertEqual((result.kind, result.detail, result.addr), ("dns_fail", "gaierror", None))

    def test_connect_timeout_and_unreachable_are_distinguished(self):
        with mock.patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            self.assertEqual(d7.parse_probe_output(run_snippet("127.0.0.1", 9)).kind, "connect_timeout")
        with mock.patch("socket.create_connection", side_effect=OSError(113, "No route to host")):
            result = d7.parse_probe_output(run_snippet("127.0.0.1", 9))
        self.assertEqual((result.kind, result.detail), ("net_error", "OSError:113"))


class ParseAndExecTests(unittest.TestCase):
    def test_malformed_outputs(self):
        for text in (
            "",
            "MAOPS_PROBE=STATUS:200\n",                                  # addr missing after successful DNS
            "MAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=DNS_FAIL:gaierror\n",  # addr with DNS failure
            "MAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=STATUS:abc\n",
            "MAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=ERROR:Exception\n",    # old/unknown class
            "MAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=RESET:x\nMAOPS_PROBE=RESET:y\n",
            "MAOPS_PROBE_ADDR=1\nMAOPS_PROBE_ADDR=2\nMAOPS_PROBE=RESET:x\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(d7.parse_probe_output(text).kind, "malformed")

    def test_kubectl_exec_timeout_and_failure(self):
        profile = mock.patch.object(kube, "PROFILE", "day7")
        profile.start()
        self.addCleanup(profile.stop)
        with mock.patch.object(kube, "run", side_effect=subprocess.TimeoutExpired("kubectl", 34)):
            self.assertEqual(d7.exec_probe("p", "c", "maops-state").kind, "exec_timeout")
        with mock.patch.object(kube, "run", return_value=subprocess.CompletedProcess(["kubectl"], 1, "", "container not found")):
            self.assertEqual(d7.exec_probe("p", "c", "maops-state").kind, "exec_failed")


GOOD = d7.Destination("maops-state", "10.96.0.20", 1)


def R(kind, detail="x", addr="10.96.0.20"):
    return d7.ProbeResult(kind, detail, addr)


class VerdictTests(unittest.TestCase):
    def test_every_observation_failure_is_inconclusive_for_both_directions(self):
        for result in (R("dns_fail", "gaierror", None), R("malformed"), R("exec_failed"), R("exec_timeout"), R("net_error"), R("probe_error")):
            for allowed in (True, False):
                with self.subTest(kind=result.kind, allowed=allowed):
                    ok, why = d7.path_verdict(result, allowed, GOOD, control_ok=True)
                    self.assertFalse(ok)
                    self.assertIn("inconclusive", why)

    def test_unverified_destination_fails_even_a_perfect_observation(self):
        for dest in (d7.Destination("maops-state", None, 1), d7.Destination("maops-state", "10.96.0.20", 0), d7.Destination("maops-state", "10.96.0.20", None), d7.Destination("maops-state", "None", 1)):
            with self.subTest(dest=dest):
                self.assertFalse(d7.path_verdict(R("reset"), False, dest, control_ok=True)[0])
                self.assertFalse(d7.path_verdict(R("status", "200"), True, dest)[0])

    def test_resolution_to_another_address_fails(self):
        self.assertFalse(d7.path_verdict(R("reset", addr="10.96.9.9"), False, GOOD, control_ok=True)[0])

    def test_allowed_requires_http_200(self):
        self.assertTrue(d7.path_verdict(R("status", "200"), True, GOOD)[0])
        for result in (R("status", "503"), R("reset"), R("read_timeout")):
            self.assertFalse(d7.path_verdict(result, True, GOOD)[0])

    def test_any_http_response_fails_a_denied_path(self):
        for code in ("200", "403", "503"):
            ok, why = d7.path_verdict(R("status", code), False, GOOD, control_ok=True)
            self.assertFalse(ok)
            self.assertIn("ANSWERED", why)

    def test_no_http_response_is_denial_evidence_only_with_a_passing_control(self):
        for kind in ("reset", "refused", "connect_timeout", "read_timeout"):
            with self.subTest(kind=kind):
                ok, why = d7.path_verdict(R(kind), False, GOOD, control_ok=True)
                self.assertTrue(ok)
                self.assertIn("not identified", why)
                for control in (None, False):
                    self.assertFalse(d7.path_verdict(R(kind), False, GOOD, control_ok=control)[0])


def _pods(prefix, comp):
    return [{"metadata": {"name": f"{prefix}-0", "labels": {"app.kubernetes.io/component": comp}}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}]


IPS = {"maops-app": "10.96.0.10", "maops-state": "10.96.0.20", d7.CANDIDATE_SERVICE: "10.96.0.30", d7.STABLE_SERVICE: "10.96.0.40"}


def _healthy_probe(pod, container, host):
    allowed = {("cand-0", "maops-app"), ("app-0", "maops-state")}
    return R("status", "200", IPS[host]) if (pod, host) in allowed else R("reset", "request", IPS[host])


class PathPlanTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")),
                  mock.patch.object(d7, "list_json", side_effect=lambda r, s=None, namespace=None: _pods("cand", "gateway-candidate") if s and "gateway-candidate" in s else _pods("app", "app"))):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, probe=_healthy_probe, destinations=None):
        dests = destinations or {svc: d7.Destination(svc, ip, 1) for svc, ip in IPS.items()}
        with mock.patch.object(d7, "exec_probe", side_effect=probe), mock.patch.object(d7, "read_destination", side_effect=lambda svc: dests[svc]):
            rec = d7.Recorder()
            return _quiet(d7.check_candidate_mesh_paths, rec, "t"), rec

    def test_healthy_plan_passes(self):
        ok, rec = self._run()
        self.assertTrue(ok, rec.failures())

    def test_dns_failure_on_a_denied_path_fails(self):
        def probe(pod, container, host):
            return R("dns_fail", "gaierror", None) if host == "maops-state" and pod == "cand-0" else _healthy_probe(pod, container, host)
        ok, rec = self._run(probe)
        self.assertFalse(ok)
        self.assertTrue(any("candidate -> state" in m and "dns_fail" in m for m in rec.failures()))

    def test_unready_destination_fails_the_plan(self):
        dests = {svc: d7.Destination(svc, ip, 1) for svc, ip in IPS.items()}
        dests["maops-state"] = d7.Destination("maops-state", IPS["maops-state"], 0)
        ok, rec = self._run(destinations=dests)
        self.assertFalse(ok)
        self.assertTrue(any("destination maops-state verified live" in m for m in rec.failures()))

    def test_failed_positive_control_invalidates_that_sources_negatives(self):
        def probe(pod, container, host):
            if (pod, host) == ("cand-0", "maops-app"):
                return R("read_timeout", "4", IPS[host])
            return _healthy_probe(pod, container, host)
        ok, rec = self._run(probe)
        self.assertFalse(ok)
        self.assertTrue(any("candidate -> state" in m and "positive control" in m for m in rec.failures()))

    def test_denied_destination_answering_fails(self):
        def probe(pod, container, host):
            return R("status", "503", IPS[host]) if host == d7.CANDIDATE_SERVICE else _healthy_probe(pod, container, host)
        ok, _ = self._run(probe)
        self.assertFalse(ok)


class InconclusiveBlocksPromotionTests(_Base):
    """An inconclusive path observation must submit no promotion stage."""

    def _promote_with(self, probe, dests=None):
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")), \
                mock.patch.object(d7, "run_gate", return_value=(True, [(True, "gate ok")])), \
                mock.patch.object(d7, "list_json", side_effect=lambda r, s=None, namespace=None: _pods("cand", "gateway-candidate") if s and "gateway-candidate" in s else _pods("app", "app")), \
                mock.patch.object(d7, "exec_probe", side_effect=probe), \
                mock.patch.object(d7, "read_destination", side_effect=lambda svc: (dests or {}).get(svc, d7.Destination(svc, IPS[svc], 1))):
            apply = mock.Mock(return_value=True)
            result = _quiet(d7.promote, d7.Recorder(), "canary-90-10", "green-prepared", apply=apply)
        return result, apply

    def test_dns_failure_submits_nothing(self):
        result, apply = self._promote_with(lambda p, c, h: R("dns_fail", "gaierror", None))
        apply.assert_not_called()
        self.assertFalse(result.submitted)
        self.assertIs(result.paths_ok, False)

    def test_kubectl_timeout_submits_nothing(self):
        result, apply = self._promote_with(lambda p, c, h: R("exec_timeout", "kubectl exec timed out", None))
        apply.assert_not_called()

    def test_malformed_output_submits_nothing(self):
        result, apply = self._promote_with(lambda p, c, h: R("malformed", "garbage", None))
        apply.assert_not_called()

    def test_destination_readiness_failure_submits_nothing(self):
        result, apply = self._promote_with(_healthy_probe, dests={"maops-app": d7.Destination("maops-app", IPS["maops-app"], 0)})
        apply.assert_not_called()

    def test_healthy_observation_submits_exactly_once(self):
        result, apply = self._promote_with(_healthy_probe)
        apply.assert_called_once()
        self.assertTrue(result.submitted)

    def test_blue_green_body_with_inconclusive_probe_never_cuts_over(self):
        cluster = FakeCluster(self)
        with mock.patch.object(d7, "check_candidate_mesh_paths", REAL_PATH_CHECK), \
                mock.patch.object(d7, "exec_probe", side_effect=lambda p, c, h: R("dns_fail", "gaierror", None)), \
                mock.patch.object(d7, "read_destination", side_effect=lambda svc: d7.Destination(svc, IPS[svc], 1)):
            rec = d7.Recorder()
            self.assertFalse(_quiet(day7_blue_green.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])
        self.assertTrue(any("REFUSED" in m for m in rec.failures()))


class RecreateSelectorValidationTests(unittest.TestCase):
    def test_unsupported_expression_fails_closed_even_when_match_labels_miss(self):
        selector = {"matchLabels": {"app.kubernetes.io/component": "something-else"}, "matchExpressions": [{"key": "k", "operator": "Gt", "values": ["1"]}]}
        with self.assertRaises(ValueError):
            day7_recreate.selector_selects(selector, {"app.kubernetes.io/component": "gateway-candidate"})
        selecting, errors = day7_recreate.pdbs_selecting([{"metadata": {"name": "odd"}, "spec": {"selector": selector}}], [{"metadata": {"labels": {"app.kubernetes.io/component": "gateway-candidate"}}}])
        self.assertEqual(selecting, [])
        self.assertTrue(errors)

    def test_non_list_expressions_fail_closed(self):
        with self.assertRaises(ValueError):
            day7_recreate.selector_selects({"matchExpressions": {"key": "k"}}, {})


class RestoreOrVerifyShapeTests(unittest.TestCase):
    def test_no_unreachable_return_remains(self):
        import inspect
        source = inspect.getsource(d7.restore_or_verify)
        self.assertEqual(source.count("return restore_stable("), 1)


if __name__ == "__main__":
    unittest.main()

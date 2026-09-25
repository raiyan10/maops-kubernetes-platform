"""
Docker/Kubernetes-free unit tests for scripts/ambient_workload_check.py -
the DAY6 post-restart remediation (2026-09-25). Every kubectl call is
mocked at the `kube.run` boundary with the real `kubectl ... -o json` /
`kubectl exec` result shapes; nothing here contacts a cluster.

The incident these tests encode: a Pod can be Kubernetes-Ready and carry
the ambient redirection annotation while its network namespace has NO
ztunnel listeners on 15001/15006/15008. Ready and the annotation must
never satisfy the check.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ambient_workload_check as awc
import kube

ALL_LISTENERS = "MAOPS_LISTEN_PORTS=8080,15001,15006,15008\nMAOPS_RESULT=OK\n"
NO_LISTENERS = "MAOPS_LISTEN_PORTS=8080\nMAOPS_RESULT=OK\n"

POD_NAMES = {
    "gateway": ["maops-gateway-aaa", "maops-gateway-bbb", "maops-gateway-ccc"],
    "app": ["maops-app-aaa", "maops-app-bbb", "maops-app-ccc"],
    "state": ["maops-state-0"],
}
SERVICE_ACCOUNTS = {"gateway": "maops-gateway", "app": "maops-app", "state": "maops-state"}
SELECTORS_BY_POD = {n: c for c, names in POD_NAMES.items() for n in names}
SELECTORS = {
    kube.GATEWAY_LABEL_SELECTOR: "gateway",
    kube.APP_LABEL_SELECTOR: "app",
    kube.STATE_LABEL_SELECTOR: "state",
}


def _pod(component: str, name: str, ready: bool = True, **overrides) -> dict:
    pod = {
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/component": component},
            "annotations": {awc.REDIRECTION_ANNOTATION: awc.REDIRECTION_ENABLED},
        },
        "spec": {"serviceAccountName": SERVICE_ACCOUNTS[component], "containers": [{"name": f"maops-{component}"}]},
        "status": {
            "phase": "Running",
            "podIP": "10.244.1.10",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }
    for key, value in overrides.items():
        section, field = key.split("__")
        pod[section][field] = value
    return pod


class FakeCluster:
    """Dispatches mocked `kube.run` calls to canned kubectl results."""

    def __init__(self, pods=None, exec_output=None, namespace_mode="ambient"):
        self.pods = pods if pods is not None else {c: [_pod(c, n) for n in names] for c, names in POD_NAMES.items()}
        self.exec_output = exec_output or {}  # pod name -> str | BaseException
        self.namespace_mode = namespace_mode
        self.exec_calls: list[tuple[str, ...]] = []
        self.list_errors: dict[str, BaseException] = {}

    def run(self, *args, check=True, timeout=None):
        self.assert_bounded(timeout)
        if "exec" in args:
            self.exec_calls.append(args)
            name = args[args.index("exec") + 1]
            out = self.exec_output.get(name, ALL_LISTENERS)
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        if args[:2] == ("get", "namespace"):
            labels = {} if self.namespace_mode is None else {kube.AMBIENT_DATAPLANE_MODE_LABEL: self.namespace_mode}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"metadata": {"labels": labels}}), stderr="")
        if "pods" in args:
            component = SELECTORS[args[args.index("-l") + 1]]
            if component in self.list_errors:
                raise self.list_errors[component]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"items": self.pods[component]}), stderr="")
        raise AssertionError(f"unexpected kubectl call: {args}")

    @staticmethod
    def assert_bounded(timeout):
        if timeout is None:
            raise AssertionError("every kubectl call must be bounded by a timeout")


def _run_main(cluster: FakeCluster) -> tuple[int, str]:
    awc.results = []
    out = io.StringIO()
    with mock.patch.object(awc.kube, "verify_context"), mock.patch.object(awc.kube, "run", side_effect=cluster.run), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = awc.main()
    return code, out.getvalue()


def _failed() -> list[str]:
    return [m for ok, m in awc.results if not ok]


class SuccessTests(unittest.TestCase):
    def test_all_seven_pods_with_listeners_pass(self):
        cluster = FakeCluster()
        code, out = _run_main(cluster)
        self.assertEqual(code, 0, _failed())
        self.assertEqual(_failed(), [])
        probed = sorted(call[call.index("exec") + 1] for call in cluster.exec_calls)
        self.assertEqual(probed, sorted(n for names in POD_NAMES.values() for n in names))
        self.assertEqual(len(probed), 7)
        self.assertIn("PASS:", out)

    def test_exec_targets_the_workload_container_read_only(self):
        cluster = FakeCluster()
        _run_main(cluster)
        for call in cluster.exec_calls:
            with self.subTest(call=call[:6]):
                self.assertIn("-c", call)
                self.assertEqual(call[call.index("-c") + 1], f"maops-{SELECTORS_BY_POD[call[call.index('exec') + 1]]}")
                snippet = call[-1]
                self.assertNotIn("environ", snippet)
                self.assertNotIn("secrets", snippet)
                self.assertNotIn("token", snippet)



class IncidentRegressionTests(unittest.TestCase):
    def test_ready_state_pod_without_listeners_fails_naming_pod_and_ports(self):
        """The 2026-09-25 maops-state-0 case: Kubernetes Ready, redirection
        annotation present, but no ztunnel listeners in its netns."""
        cluster = FakeCluster(exec_output={"maops-state-0": NO_LISTENERS})
        code, out = _run_main(cluster)
        self.assertEqual(code, 1)
        failed = _failed()
        self.assertEqual(len(failed), 1, failed)
        self.assertIn("state pod maops-state-0", failed[0])
        self.assertIn("15001, 15006, 15008", failed[0])
        self.assertNotIn("PASS:", out)

    def test_ready_condition_and_annotation_alone_never_pass(self):
        cluster = FakeCluster(exec_output={"maops-state-0": NO_LISTENERS})
        _run_main(cluster)
        passed = [m for ok, m in awc.results if ok and "maops-state-0" in m]
        self.assertTrue(any(awc.REDIRECTION_ANNOTATION in m for m in passed))
        self.assertFalse(any("listeners" in m for m in passed))

    def test_unready_gateway_missing_listeners_fails_naming_that_pod(self):
        """The maops-gateway-7d59b678df-f88mj case: one unready gateway Pod
        missing all three listeners while its siblings are healthy."""
        pods = {c: [_pod(c, n) for n in names] for c, names in POD_NAMES.items()}
        pods["gateway"][1] = _pod("gateway", "maops-gateway-bbb", ready=False)
        cluster = FakeCluster(pods=pods, exec_output={"maops-gateway-bbb": NO_LISTENERS})
        code, _ = _run_main(cluster)
        self.assertEqual(code, 1)
        failed = _failed()
        self.assertEqual(len(failed), 1, failed)
        self.assertIn("gateway pod maops-gateway-bbb", failed[0])
        self.assertIn("MISSING", failed[0])

    def test_partial_listener_set_reports_only_the_missing_port(self):
        cluster = FakeCluster(exec_output={"maops-app-aaa": "MAOPS_LISTEN_PORTS=15001,15006\nMAOPS_RESULT=OK\n"})
        code, _ = _run_main(cluster)
        self.assertEqual(code, 1)
        self.assertEqual(len(_failed()), 1)
        self.assertTrue(_failed()[0].endswith("port(s) 15008"))


class FailClosedTests(unittest.TestCase):
    def test_exec_nonzero_exit_fails_closed(self):
        err = subprocess.CalledProcessError(1, ["kubectl"], stderr="error: unable to upgrade connection: container not found")
        code, _ = _run_main(FakeCluster(exec_output={"maops-app-bbb": err}))
        self.assertEqual(code, 1)
        self.assertTrue(any("maops-app-bbb" in m and "failed closed" in m and "container not found" in m for m in _failed()))

    def test_exec_timeout_fails_closed(self):
        code, _ = _run_main(FakeCluster(exec_output={"maops-state-0": subprocess.TimeoutExpired(["kubectl"], 20)}))
        self.assertEqual(code, 1)
        self.assertTrue(any("maops-state-0" in m and "failed closed" in m for m in _failed()))

    def test_malformed_probe_outputs_fail_closed(self):
        for output in (
            "",
            "MAOPS_LISTEN_PORTS=15001,15006,15008\n",
            "MAOPS_RESULT=OK\n",
            "MAOPS_RESULT=ERROR:PermissionError\n",
            "MAOPS_LISTEN_PORTS=15001,abc\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001\nMAOPS_LISTEN_PORTS=15006\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=70000\nMAOPS_RESULT=OK\n",
        ):
            with self.subTest(output=output):
                code, _ = _run_main(FakeCluster(exec_output={"maops-gateway-ccc": output}))
                self.assertEqual(code, 1)
                self.assertTrue(any("maops-gateway-ccc" in m and "failed closed" in m for m in _failed()))

    def test_pod_list_api_error_fails_closed(self):
        cluster = FakeCluster()
        cluster.list_errors["app"] = subprocess.CalledProcessError(1, ["kubectl"], stderr="Unable to connect to the server")
        code, _ = _run_main(cluster)
        self.assertEqual(code, 1)
        self.assertTrue(any("app: could not list Pods" in m for m in _failed()))

    def test_pod_list_timeout_fails_closed(self):
        cluster = FakeCluster()
        cluster.list_errors["state"] = subprocess.TimeoutExpired(["kubectl"], 20)
        code, _ = _run_main(cluster)
        self.assertEqual(code, 1)
        self.assertTrue(any("state: could not list Pods" in m for m in _failed()))

    def test_missing_pod_fails(self):
        pods = {c: [_pod(c, n) for n in names] for c, names in POD_NAMES.items()}
        pods["gateway"] = pods["gateway"][:2]
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any("gateway: 2 Pod(s) found (expected exactly 3)" in m for m in _failed()))

    def test_missing_state_pod_fails(self):
        pods = {c: [_pod(c, n) for n in names] for c, names in POD_NAMES.items()}
        pods["state"] = []
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any("state: 0 Pod(s) found" in m for m in _failed()))

    def test_verify_context_failure_stops_before_any_kubectl_call(self):
        awc.results = []
        with mock.patch.object(awc.kube, "verify_context", side_effect=RuntimeError("wrong cluster")), \
                mock.patch.object(awc.kube, "run") as run_mock, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(awc.main(), 1)
        run_mock.assert_not_called()


class IdentityAndEnrollmentTests(unittest.TestCase):
    def _single_override(self, component: str, index: int, **overrides) -> dict:
        pods = {c: [_pod(c, n) for n in names] for c, names in POD_NAMES.items()}
        pods[component][index] = _pod(component, POD_NAMES[component][index], **overrides)
        return pods

    def test_wrong_service_account_fails(self):
        pods = self._single_override("app", 0, spec__serviceAccountName="default")
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any("serviceAccountName='default'" in m for m in _failed()))

    def test_missing_redirection_annotation_fails_even_with_listeners(self):
        pods = self._single_override("gateway", 0)
        pods["gateway"][0]["metadata"]["annotations"] = {}
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any(awc.REDIRECTION_ANNOTATION in m for m in _failed()))

    def test_pod_opting_out_of_ambient_fails(self):
        pods = self._single_override("state", 0)
        pods["state"][0]["metadata"]["labels"][kube.AMBIENT_DATAPLANE_MODE_LABEL] = "none"
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any("does not opt out" in m for m in _failed()))

    def test_sidecar_container_fails(self):
        pods = self._single_override("app", 2)
        pods["app"][2]["spec"]["containers"].append({"name": "istio-proxy"})
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        self.assertTrue(any("sidecar" in m for m in _failed()))

    def test_namespace_not_ambient_fails(self):
        code, _ = _run_main(FakeCluster(namespace_mode=None))
        self.assertEqual(code, 1)
        self.assertTrue(any("namespace 'maops-platform'" in m for m in _failed()))

    def test_terminating_or_not_running_pod_fails(self):
        pods = self._single_override("gateway", 2, status__phase="Pending")
        pods["gateway"][1]["metadata"]["deletionTimestamp"] = "2026-09-25T00:00:00Z"
        code, _ = _run_main(FakeCluster(pods=pods))
        self.assertEqual(code, 1)
        failed = _failed()
        self.assertTrue(any("maops-gateway-ccc: phase='Pending'" in m for m in failed))
        self.assertTrue(any("maops-gateway-bbb: not terminating" in m for m in failed))


class StrictParserTests(unittest.TestCase):
    """parse_listen_output() accepts only one ports line plus one OK
    marker; anything else is malformed and must fail closed."""

    def test_rejected_forms(self):
        for output in (
            "Traceback (most recent call last):\nMAOPS_LISTEN_PORTS=15001,15006,15008\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001,15006,15008\nsome stray diagnostic\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001,15006,15008\nMAOPS_RESULT=OK\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001,15006,15008\nMAOPS_RESULT=OK\nMAOPS_RESULT=ERROR:x\n",
            "MAOPS_LISTEN_PORTS=15001\nMAOPS_LISTEN_PORTS=15001\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001,15006,15006,15008\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS=15001,,15008\nMAOPS_RESULT=OK\n",
            "MAOPS_LISTEN_PORTS= 15001\nMAOPS_RESULT=OK\n",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    awc.parse_listen_output(output)

    def test_blank_lines_are_tolerated(self):
        self.assertEqual(
            awc.parse_listen_output("\nMAOPS_LISTEN_PORTS=15001,15006,15008\n\nMAOPS_RESULT=OK\n\n"),
            {15001, 15006, 15008},
        )

    def test_valid_empty_port_list_reports_all_three_ports_missing(self):
        cluster = FakeCluster(exec_output={"maops-state-0": "MAOPS_LISTEN_PORTS=\nMAOPS_RESULT=OK\n"})
        code, _ = _run_main(cluster)
        self.assertEqual(code, 1)
        failed = _failed()
        self.assertEqual(len(failed), 1, failed)
        self.assertIn("MISSING", failed[0])
        self.assertTrue(failed[0].endswith("port(s) 15001, 15006, 15008"))


class RepeatedMainTests(unittest.TestCase):
    def test_main_resets_results_between_calls_in_one_process(self):
        """First call fails, second succeeds - with NO clearing of
        awc.results by the test between the calls, main() itself must
        start each run from an empty result list."""
        failing = FakeCluster(exec_output={"maops-state-0": NO_LISTENERS})
        passing = FakeCluster()
        with mock.patch.object(awc.kube, "verify_context"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(awc.kube, "run", side_effect=failing.run):
                first = awc.main()
            first_count = len(awc.results)
            with mock.patch.object(awc.kube, "run", side_effect=passing.run):
                second = awc.main()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual([m for ok, m in awc.results if not ok], [])
        self.assertEqual(len(awc.results), first_count)


class ProbeSnippetTests(unittest.TestCase):
    def test_parse_accepts_empty_port_list(self):
        self.assertEqual(awc.parse_listen_output("MAOPS_LISTEN_PORTS=\nMAOPS_RESULT=OK\n"), set())

    def test_snippet_runs_and_parses_against_this_hosts_proc(self):
        """Executes the real in-Pod snippet against the local
        /proc/net/tcp{,6} - proves its output format round-trips through
        the parser (Linux-only, like the target Pods)."""
        if not Path("/proc/net/tcp").exists():
            self.skipTest("no /proc/net/tcp on this platform")
        result = subprocess.run([sys.executable, "-c", awc.LISTEN_PROBE_SNIPPET], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(awc.parse_listen_output(result.stdout), set)

    def test_snippet_detects_a_real_listen_socket(self):
        if not Path("/proc/net/tcp").exists():
            self.skipTest("no /proc/net/tcp on this platform")
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            result = subprocess.run([sys.executable, "-c", awc.LISTEN_PROBE_SNIPPET], capture_output=True, text=True, timeout=10)
        self.assertIn(port, awc.parse_listen_output(result.stdout))


if __name__ == "__main__":
    unittest.main()

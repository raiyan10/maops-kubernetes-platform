"""
Docker/Kubernetes-free unit tests for scripts/networkpolicy_check.py
(DAY5-TEST-H1 remediation).

networkpolicy_check.py is dominated by real live-cluster interaction by
necessity (it proves NetworkPolicy enforcement with actual in-cluster
TCP attempts), but it also contains several cleanly separable, pure
pieces of logic that decide how a probe's raw output gets classified as
ALLOWED/DENIED - exactly the kind of logic where a silent regression
(e.g. an accidental `ok`/`not ok` inversion, or a broken probe-snippet
template) would only ever surface on a live run, undetected by `make
test`. Directly tests those pieces here instead.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import networkpolicy_check


class ConnectProbeSnippetTests(unittest.TestCase):
    def test_substitutes_host_port_path_timeout(self):
        snippet = networkpolicy_check._connect_probe_snippet("maops-state", 8080, "/livez", 4)
        self.assertIn('HTTPConnection("maops-state", 8080, timeout=4)', snippet)
        self.assertIn('conn.request("GET", "/livez")', snippet)

    def test_output_contract_present_for_both_outcomes(self):
        snippet = networkpolicy_check._connect_probe_snippet("maops-app", 8080, "/livez", 4)
        self.assertIn('print("RESULT=ok STATUS=" + str(resp.status))', snippet)
        self.assertIn('print("RESULT=blocked ERROR=" + type(e).__name__ + ":" + str(e))', snippet)

    def test_no_stray_placeholder_tokens_left_unsubstituted(self):
        """A future edit that adds a new __TOKEN__ to the template but
        forgets to substitute it would silently ship broken Python into
        the probe Pod - only ever discovered on a live run. Guard it
        here instead."""
        snippet = networkpolicy_check._connect_probe_snippet("maops-app", 8080, "/livez", 4)
        self.assertNotIn("__HOST__", snippet)
        self.assertNotIn("__PORT__", snippet)
        self.assertNotIn("__PATH__", snippet)
        self.assertNotIn("__TIMEOUT__", snippet)

    def test_generated_snippet_is_syntactically_valid_python(self):
        snippet = networkpolicy_check._connect_probe_snippet("maops-state", 8080, "/livez", 4)
        compile(snippet, "<probe>", "exec")  # raises SyntaxError if broken


class DnsProbeSnippetTests(unittest.TestCase):
    def test_substitutes_host(self):
        snippet = networkpolicy_check._dns_probe_snippet("maops-app")
        self.assertIn('socket.getaddrinfo("maops-app", 8080)', snippet)

    def test_no_stray_placeholder_left(self):
        snippet = networkpolicy_check._dns_probe_snippet("maops-app")
        self.assertNotIn("__HOST__", snippet)

    def test_generated_snippet_is_syntactically_valid_python(self):
        snippet = networkpolicy_check._dns_probe_snippet("maops-app")
        compile(snippet, "<probe>", "exec")


class RunProbeTests(unittest.TestCase):
    """`_run_probe()` is the single point that decides ALLOWED vs DENIED
    from a probe Pod's raw exec output - main()'s DENIED assertions
    (`record(not ok, ...)`) depend entirely on this function's polarity
    being correct."""

    def setUp(self):
        networkpolicy_check.results = []

    def test_ok_with_2xx_status_is_connected(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=ok STATUS=200"):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertTrue(ok)
        self.assertEqual(detail, "RESULT=ok STATUS=200")

    def test_ok_with_non_2xx_status_is_not_connected(self):
        """A TCP connection that succeeds but reaches a backend
        answering with an error must NOT be reported as an allowed
        path - only a genuine 2xx counts."""
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=ok STATUS=503"):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)

    def test_ok_with_unparseable_status_is_not_connected(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=ok STATUS=not-a-number"):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)
        self.assertIn("unparseable", detail)

    def test_ok_without_status_token_is_connected(self):
        """A DNS probe's success line has no STATUS= token at all (only
        COUNT=) - RESULT=ok alone must be accepted for that case."""
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=ok COUNT=1"):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertTrue(ok)

    def test_blocked_is_not_connected(self):
        with mock.patch.object(
            networkpolicy_check, "_exec_in_pod", return_value="RESULT=blocked ERROR=TimeoutError:timed out"
        ):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)
        self.assertIn("blocked", detail)

    def test_unrecognized_output_is_not_connected_not_an_exception(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="garbage, no RESULT= line"):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)
        self.assertIn("unrecognized probe output", detail)

    def test_empty_output_is_not_connected_not_an_exception(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value=""):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)

    def test_kubectl_exec_called_process_error_is_not_connected(self):
        exc = subprocess.CalledProcessError(1, ["kubectl"], stderr="pod not found")
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", side_effect=exc):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)
        self.assertIn("kubectl exec failed", detail)

    def test_kubectl_exec_timeout_is_not_connected(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30)
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", side_effect=exc):
            ok, detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertFalse(ok)
        self.assertIn("kubectl exec timed out", detail)

    def test_uses_last_line_of_multiline_output(self):
        """kubectl exec output can carry warnings/banners before the
        actual probe's print() line - only the final line is the real
        result."""
        output = "Warning: some banner\nRESULT=ok STATUS=200"
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value=output):
            ok, _detail = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertTrue(ok)


class ValidationPodManifestTests(unittest.TestCase):
    def test_carries_no_service_account_token(self):
        """The validation-client probe pod must never hold an API
        token - it is network-privileged, not RBAC-privileged (DAY5-SEC
        design property). A copy-paste regression from rbac_check.py's
        manifest (which DOES set automountServiceAccountToken: true)
        would silently combine both privileges in one identity."""
        manifest = networkpolicy_check._validation_pod_manifest()
        self.assertIn("automountServiceAccountToken: false", manifest)

    def test_carries_validation_client_component_label(self):
        manifest = networkpolicy_check._validation_pod_manifest()
        self.assertIn("app.kubernetes.io/component: validation-client", manifest)

    def test_targets_validation_namespace(self):
        manifest = networkpolicy_check._validation_pod_manifest()
        self.assertIn(f"namespace: {networkpolicy_check.VALIDATION_NAMESPACE}", manifest)


if __name__ == "__main__":
    unittest.main()

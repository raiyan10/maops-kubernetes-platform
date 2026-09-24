"""
Docker/Kubernetes-free unit tests for scripts/networkpolicy_check.py
(DAY5-TEST-H1 remediation, then twice remediated for Day 6 after
independent review).

networkpolicy_check.py is dominated by real live-cluster interaction by
necessity (it proves NetworkPolicy enforcement with actual in-cluster
TCP attempts), but it also contains several cleanly separable, pure
pieces of logic that decide how a probe's raw output gets classified -
exactly the kind of logic where a silent regression (e.g. an accidental
`ok`/`not ok` inversion, or a broken probe-snippet template) would only
ever surface on a live run, undetected by `make test`. Directly tests
those pieces here instead, including real (never mocked) socket-level
behavior for the generated probe snippets themselves, so the phase-
separated classification is proven against actual TCP/HTTP semantics,
not just against hand-constructed RESULT= strings.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import networkpolicy_check


class TcpConnectProbeSnippetTests(unittest.TestCase):
    def test_substitutes_host_port_timeout(self):
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-state", 8080, 4)
        self.assertIn('sock.connect(("maops-state", 8080))', snippet)
        self.assertIn("sock.settimeout(4)", snippet)

    def test_output_contract_present_for_all_three_outcomes(self):
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-app", 8080, 4)
        self.assertIn('print("RESULT=TCP_CONNECTED")', snippet)
        self.assertIn('print("RESULT=TCP_CONNECT_TIMEOUT")', snippet)
        self.assertIn('print("RESULT=CONNECTION_REFUSED_OR_RESET ', snippet)

    def test_timeout_except_clause_precedes_oserror_catch_all(self):
        """`socket.timeout` (== `TimeoutError` as of Python 3.10) is
        itself a subclass of `OSError` - if the broad
        `except (..., OSError)` clause came first, it would silently
        swallow genuine connect timeouts too. Guards the ordering."""
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-app", 8080, 4)
        timeout_pos = snippet.index("except socket.timeout")
        oserror_pos = snippet.index("except (ConnectionRefusedError")
        self.assertLess(timeout_pos, oserror_pos)

    def test_no_stray_placeholder_tokens_left_unsubstituted(self):
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-app", 8080, 4)
        self.assertNotIn("__HOST__", snippet)
        self.assertNotIn("__PORT__", snippet)
        self.assertNotIn("__TIMEOUT__", snippet)

    def test_generated_snippet_is_syntactically_valid_python(self):
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-state", 8080, 4)
        compile(snippet, "<probe>", "exec")

    def test_never_attempts_an_http_request(self):
        """The whole point of this purpose-built probe: no HTTP
        transaction, ever - only a raw TCP connect."""
        snippet = networkpolicy_check._tcp_connect_probe_snippet("maops-app", 8080, 4)
        self.assertNotIn("http.client", snippet)
        self.assertNotIn(".request(", snippet)


class HttpProbeSnippetTests(unittest.TestCase):
    def test_substitutes_host_port_path_timeouts(self):
        snippet = networkpolicy_check._http_probe_snippet("maops-state", 8080, "/livez", 4, 3)
        self.assertIn('HTTPConnection("maops-state", 8080, timeout=4)', snippet)
        self.assertIn('conn.request("GET", "/livez")', snippet)
        self.assertIn("conn.sock.settimeout(3)", snippet)

    def test_connect_and_read_phases_have_separate_try_blocks(self):
        """The core structural fix this remediation makes: `conn.connect()`
        is called in its OWN try/except, structurally separate from the
        `conn.request()`/`conn.getresponse()`/`.read()` try/except - so a
        read-phase timeout can never be misattributed to a connect-phase
        timeout (both used to share a single try/except around the whole
        transaction)."""
        snippet = networkpolicy_check._http_probe_snippet("maops-app", 8080, "/livez", 4, 3)
        connect_call = snippet.index("conn.connect()")
        request_call = snippet.index("conn.request(")
        # conn.connect() must be followed by its OWN "except socket.timeout"
        # BEFORE the request() call even appears in the source.
        first_except_after_connect = snippet.index("except socket.timeout", connect_call)
        self.assertLess(first_except_after_connect, request_call)

    def test_output_contract_present_for_all_outcomes(self):
        snippet = networkpolicy_check._http_probe_snippet("maops-app", 8080, "/livez", 4, 3)
        self.assertIn('print("RESULT=TCP_CONNECT_TIMEOUT")', snippet)
        self.assertIn('print("RESULT=HTTP_RESPONSE STATUS="', snippet)
        self.assertIn('print("RESULT=HTTP_READ_TIMEOUT")', snippet)
        self.assertIn('print("RESULT=CONNECTION_REFUSED_OR_RESET ', snippet)

    def test_no_stray_placeholder_tokens_left_unsubstituted(self):
        snippet = networkpolicy_check._http_probe_snippet("maops-app", 8080, "/livez", 4, 3)
        for token in ("__HOST__", "__PORT__", "__PATH__", "__CONNECT_TIMEOUT__", "__READ_TIMEOUT__"):
            self.assertNotIn(token, snippet)

    def test_generated_snippet_is_syntactically_valid_python(self):
        snippet = networkpolicy_check._http_probe_snippet("maops-state", 8080, "/livez", 4, 3)
        compile(snippet, "<probe>", "exec")


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


class RealSocketClassificationTests(unittest.TestCase):
    """Runs the ACTUAL generated snippets as real local subprocesses
    against real sockets/servers - never mocked - proving the
    phase-separated classification against genuine TCP/HTTP behavior,
    not just hand-constructed RESULT= strings. This is what would have
    caught the first remediation's residual defect (connect vs. read
    timeout conflation) before a live run ever could."""

    def _run(self, snippet: str, timeout: float = 10.0) -> str:
        return subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=timeout).stdout.strip()

    def test_connection_refused_when_nothing_listens(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        # free_port is now closed again - nothing listens there.
        output = self._run(networkpolicy_check._tcp_connect_probe_snippet("127.0.0.1", free_port, 2))
        self.assertIn("RESULT=CONNECTION_REFUSED_OR_RESET", output)

    def test_connect_timeout_against_unroutable_address(self):
        # 192.0.2.0/24 is TEST-NET-1 (RFC 5737) - reserved for
        # documentation, never routable - a connect attempt to it
        # reliably times out rather than refusing.
        output = self._run(networkpolicy_check._tcp_connect_probe_snippet("192.0.2.1", 80, 2), timeout=6)
        self.assertIn("RESULT=TCP_CONNECT_TIMEOUT", output)

    def test_tcp_connected_against_real_listener(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            output = self._run(networkpolicy_check._tcp_connect_probe_snippet("127.0.0.1", port, 3))
            self.assertIn("RESULT=TCP_CONNECTED", output)
        finally:
            srv.close()

    def test_http_response_against_real_server(self):
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = self._run(networkpolicy_check._http_probe_snippet("127.0.0.1", port, "/", 3, 3))
            self.assertIn("RESULT=HTTP_RESPONSE STATUS=200", output)
        finally:
            server.shutdown()

    def test_http_read_timeout_when_connected_but_no_response_sent(self):
        """The scenario the first remediation could not distinguish
        from a connect timeout: a real TCP connection is accepted, but
        the server never sends an HTTP response - must classify as
        HTTP_READ_TIMEOUT, never TCP_CONNECT_TIMEOUT."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _accept_and_hang():
            conn, _addr = srv.accept()
            time.sleep(5)
            conn.close()

        thread = threading.Thread(target=_accept_and_hang, daemon=True)
        thread.start()
        try:
            output = self._run(networkpolicy_check._http_probe_snippet("127.0.0.1", port, "/", 3, 2), timeout=8)
            self.assertIn("RESULT=HTTP_READ_TIMEOUT", output)
        finally:
            srv.close()


class RunProbeTests(unittest.TestCase):
    """`_run_probe()` is the single point that classifies a probe Pod's
    raw exec output into exactly one of the seven phase-attributed
    states. main()'s ALLOWED/DENIED assertions (`assert_connected()`/
    `assert_denied()`) depend entirely on this classification being
    correct - a silent regression here could manufacture a false
    "DENIED" result from a probe that never actually ran."""

    def setUp(self):
        networkpolicy_check.results = []

    def test_tcp_connected(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=TCP_CONNECTED"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.TCP_CONNECTED)

    def test_tcp_connect_timeout(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=TCP_CONNECT_TIMEOUT"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.TCP_CONNECT_TIMEOUT)

    def test_http_response_with_2xx_status_is_http_response(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=HTTP_RESPONSE STATUS=200"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.HTTP_RESPONSE)

    def test_http_response_with_non_2xx_status_is_still_http_response(self):
        """Any completed HTTP response proves connectivity, regardless
        of status - a 403/404/500 must never be misread as a
        NetworkPolicy denial."""
        for status in (403, 404, 500):
            with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value=f"RESULT=HTTP_RESPONSE STATUS={status}"):
                result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
            self.assertEqual(result.outcome, networkpolicy_check.HTTP_RESPONSE, f"status {status}")

    def test_http_response_with_unparseable_status_is_output_inconclusive(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=HTTP_RESPONSE STATUS=not-a-number"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.OUTPUT_INCONCLUSIVE)
        self.assertIn("unparseable", result.detail)

    def test_http_response_missing_status_token_is_output_inconclusive(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=HTTP_RESPONSE"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.OUTPUT_INCONCLUSIVE)

    def test_http_read_timeout_is_its_own_state_never_tcp_connect_timeout(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=HTTP_READ_TIMEOUT"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.HTTP_READ_TIMEOUT)
        self.assertNotEqual(result.outcome, networkpolicy_check.TCP_CONNECT_TIMEOUT)

    def test_connection_refused_or_reset(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=CONNECTION_REFUSED_OR_RESET ERROR=ConnectionRefusedError:refused"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.CONNECTION_REFUSED_OR_RESET)

    def test_dns_resolved(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=DNS_RESOLVED COUNT=1"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.DNS_RESOLVED)

    def test_dns_failed(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=DNS_FAILED ERROR=gaierror:no such host"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.DNS_FAILED)

    def test_unrecognized_state_is_output_inconclusive(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="RESULT=SOMETHING_MADE_UP"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.OUTPUT_INCONCLUSIVE)

    def test_unrecognized_output_is_output_inconclusive_not_an_exception(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value="garbage, no RESULT= line"):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.OUTPUT_INCONCLUSIVE)
        self.assertIn("missing/unrecognized probe output", result.detail)

    def test_empty_output_is_output_inconclusive_not_an_exception(self):
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value=""):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.OUTPUT_INCONCLUSIVE)

    def test_kubectl_exec_called_process_error_is_exec_inconclusive(self):
        """DAY6 remediation: a kubectl exec failure must never be
        silently read as proof of denial - it means the check itself
        didn't run, not that the connection was blocked. Uses its OWN
        distinct EXEC_INCONCLUSIVE state, never conflated with
        OUTPUT_INCONCLUSIVE (a different failure mode: the exec
        succeeded but its output was unusable)."""
        exc = subprocess.CalledProcessError(1, ["kubectl"], stderr="pod not found")
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", side_effect=exc):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.EXEC_INCONCLUSIVE)
        self.assertIn("kubectl exec failed", result.detail)

    def test_kubectl_exec_timeout_is_exec_inconclusive(self):
        exc = subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30)
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", side_effect=exc):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.EXEC_INCONCLUSIVE)
        self.assertIn("kubectl exec timed out", result.detail)

    def test_uses_last_line_of_multiline_output(self):
        """kubectl exec output can carry warnings/banners before the
        actual probe's print() line - only the final line is the real
        result."""
        output = "Warning: some banner\nRESULT=TCP_CONNECTED"
        with mock.patch.object(networkpolicy_check, "_exec_in_pod", return_value=output):
            result = networkpolicy_check._run_probe("maops-platform", "pod", "snippet")
        self.assertEqual(result.outcome, networkpolicy_check.TCP_CONNECTED)


class AssertConnectedTests(unittest.TestCase):
    """`assert_connected()` covers every state's effect on the ALLOWED
    assertion."""

    def setUp(self):
        networkpolicy_check.results = []

    def test_tcp_connected_passes(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "connected")
        self.assertTrue(networkpolicy_check.assert_connected(result, "label"))

    def test_http_response_passes(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=200")
        self.assertTrue(networkpolicy_check.assert_connected(result, "label"))

    def test_dns_resolved_passes(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.DNS_RESOLVED, "resolved")
        self.assertTrue(networkpolicy_check.assert_connected(result, "label"))

    def test_tcp_connect_timeout_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "timed out")
        self.assertFalse(networkpolicy_check.assert_connected(result, "label"))

    def test_http_read_timeout_fails(self):
        """DAY6 remediation: HTTP_READ_TIMEOUT proves TCP connectivity
        was established, but does NOT prove the application responded -
        it must not satisfy assert_connected() either, to avoid
        overclaiming a working positive path."""
        result = networkpolicy_check.ProbeResult(networkpolicy_check.HTTP_READ_TIMEOUT, "read timed out")
        self.assertFalse(networkpolicy_check.assert_connected(result, "label"))

    def test_connection_refused_or_reset_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.CONNECTION_REFUSED_OR_RESET, "refused")
        self.assertFalse(networkpolicy_check.assert_connected(result, "label"))

    def test_exec_inconclusive_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.EXEC_INCONCLUSIVE, "exec failed")
        self.assertFalse(networkpolicy_check.assert_connected(result, "label"))

    def test_output_inconclusive_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.OUTPUT_INCONCLUSIVE, "unparseable")
        self.assertFalse(networkpolicy_check.assert_connected(result, "label"))


class AssertDeniedTests(unittest.TestCase):
    """`assert_denied()` covers every state's effect on the DENIED
    assertion - the core of this remediation. Only TCP_CONNECT_TIMEOUT
    may ever pass."""

    def setUp(self):
        networkpolicy_check.results = []

    def test_tcp_connect_timeout_passes(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "timed out")
        self.assertTrue(networkpolicy_check.assert_denied(result, "label"))

    def test_tcp_connected_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "connected")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))

    def test_http_response_fails(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=200")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))

    def test_http_read_timeout_never_satisfies_denied(self):
        """The exact scenario this second remediation fixes: a read
        timeout AFTER a successful TCP connect must never be
        interpreted as a NetworkPolicy block - the handshake already
        proved the CNI let the packet through."""
        result = networkpolicy_check.ProbeResult(networkpolicy_check.HTTP_READ_TIMEOUT, "read timed out")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))

    def test_connection_refused_or_reset_never_satisfies_denied(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.CONNECTION_REFUSED_OR_RESET, "refused")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))

    def test_exec_inconclusive_never_satisfies_denied(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.EXEC_INCONCLUSIVE, "kubectl exec failed")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))

    def test_output_inconclusive_never_satisfies_denied(self):
        result = networkpolicy_check.ProbeResult(networkpolicy_check.OUTPUT_INCONCLUSIVE, "malformed output")
        self.assertFalse(networkpolicy_check.assert_denied(result, "label"))


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


class IsolatedProbeManifestTests(unittest.TestCase):
    """DAY6 third remediation (live-discovered): the four temporary,
    non-ambient application-port probe Pods must use Istio's documented
    per-Pod `istio.io/dataplane-mode: none` opt-out label (never the
    automatically-generated `ambient.istio.io/redirection` annotation,
    which a Pod cannot set on itself - that's ztunnel's OWN evidence of
    enrollment, the opposite of what an opt-out needs), carry ONLY the
    single component label the applicable NetworkPolicy selector keys
    on, and never reproduce the complete live workload label set."""

    def test_source_manifest_uses_dataplane_mode_none_label(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway")
        self.assertIn(f"{networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL}: none", manifest)

    def test_target_manifest_uses_dataplane_mode_none_label(self):
        manifest = networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "state")
        self.assertIn(f"{networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL}: none", manifest)

    def test_manifests_never_set_the_ambient_redirection_annotation(self):
        """That annotation is ztunnel's OWN evidence a Pod was actually
        redirected - a Pod can never legitimately set it on itself, and
        this manifest must never even try."""
        for manifest in (
            networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway"),
            networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "app"),
        ):
            self.assertNotIn(networkpolicy_check.AMBIENT_REDIRECTION_ANNOTATION, manifest)

    def test_source_manifest_component_label_matches_gateway_selector(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway")
        self.assertIn("app.kubernetes.io/component: gateway", manifest)

    def test_source_manifest_component_label_matches_app_selector(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "app")
        self.assertIn("app.kubernetes.io/component: app", manifest)

    def test_target_manifest_component_label_matches_state_selector(self):
        manifest = networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "state")
        self.assertIn("app.kubernetes.io/component: state", manifest)

    def test_manifests_do_not_reproduce_the_complete_live_workload_label_set(self):
        """The real Deployment/StatefulSet/Service objects also carry
        app.kubernetes.io/name, /instance, /version, /part-of, /managed-by,
        and helm.sh/chart - a probe Pod copying all of these could
        accidentally match an unrelated selector or Service. Only the
        one component label plus this script's own discovery labels may
        appear."""
        for manifest in (
            networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway"),
            networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "app"),
        ):
            for forbidden_label in ("app.kubernetes.io/name:", "app.kubernetes.io/instance:", "app.kubernetes.io/part-of:", "app.kubernetes.io/managed-by:", "helm.sh/chart:"):
                self.assertNotIn(forbidden_label, manifest)

    def test_source_manifest_has_no_automount(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway")
        self.assertIn("automountServiceAccountToken: false", manifest)

    def test_source_manifest_security_context_non_root(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway")
        self.assertIn("runAsNonRoot: true", manifest)
        self.assertIn("runAsUser: 10001", manifest)

    def test_target_manifest_uses_locally_available_day6_image_never_a_new_build(self):
        manifest = networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "app")
        self.assertIn(f"image: {networkpolicy_check.PROBE_IMAGE}", manifest)
        self.assertIn("imagePullPolicy: IfNotPresent", manifest)

    def test_target_manifest_listens_on_target_port(self):
        manifest = networkpolicy_check._netpol_probe_target_pod_manifest("tgt", "state")
        self.assertIn(f"containerPort: {networkpolicy_check.TARGET_LISTEN_PORT}", manifest)
        self.assertIn(f"'0.0.0.0', {networkpolicy_check.TARGET_LISTEN_PORT}", networkpolicy_check._TARGET_LISTENER_SNIPPET)

    def test_target_listener_snippet_is_syntactically_valid_python(self):
        compile(networkpolicy_check._TARGET_LISTENER_SNIPPET, "<listener>", "exec")

    def test_manifests_carry_the_unique_run_discovery_label(self):
        manifest = networkpolicy_check._netpol_probe_source_pod_manifest("src", "gateway")
        self.assertIn(f"{networkpolicy_check.NETPOL_PROBE_RUN_LABEL}:", manifest)


class VerifyProbeIsolationTests(unittest.TestCase):
    """DAY6 third remediation: `_verify_probe_pod_isolated()` is the
    pre-assertion safety gate - every check must independently fail
    closed on the specific unsafe condition it targets."""

    def setUp(self):
        networkpolicy_check.results = []

    def _isolated_pod(self) -> dict:
        return {
            "metadata": {
                "labels": {networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL: "none"},
                "annotations": {},
                "ownerReferences": [],
            },
            "spec": {"containers": [{"name": "probe"}]},
        }

    def test_genuinely_isolated_pod_passes(self):
        self.assertTrue(networkpolicy_check._verify_probe_pod_isolated(self._isolated_pod(), "probe-1"))

    def test_rejects_ambient_redirection_annotation(self):
        pod = self._isolated_pod()
        pod["metadata"]["annotations"] = {networkpolicy_check.AMBIENT_REDIRECTION_ANNOTATION: "enabled"}
        self.assertFalse(networkpolicy_check._verify_probe_pod_isolated(pod, "probe-1"))

    def test_rejects_sidecar_container(self):
        pod = self._isolated_pod()
        pod["spec"]["containers"].append({"name": "istio-proxy"})
        self.assertFalse(networkpolicy_check._verify_probe_pod_isolated(pod, "probe-1"))

    def test_rejects_controller_adoption(self):
        pod = self._isolated_pod()
        pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "some-rs"}]
        self.assertFalse(networkpolicy_check._verify_probe_pod_isolated(pod, "probe-1"))

    def test_rejects_wrong_dataplane_mode_label(self):
        pod = self._isolated_pod()
        pod["metadata"]["labels"][networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL] = "ambient"
        self.assertFalse(networkpolicy_check._verify_probe_pod_isolated(pod, "probe-1"))

    def test_rejects_missing_dataplane_mode_label(self):
        pod = self._isolated_pod()
        del pod["metadata"]["labels"][networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL]
        self.assertFalse(networkpolicy_check._verify_probe_pod_isolated(pod, "probe-1"))


class PodIpNotInEndpointSliceTests(unittest.TestCase):
    """DAY6 third remediation: `_pod_ip_not_in_any_service_endpointslice()`
    is the direct-IP safety proof - a probe Pod must never actually be
    part of live Service routing."""

    def setUp(self):
        networkpolicy_check.results = []

    def _completed(self, returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr="")

    def test_passes_when_ip_absent_from_all_slices(self):
        empty = self._completed(0, stdout='{"items": []}')
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=empty):
            self.assertTrue(networkpolicy_check._pod_ip_not_in_any_service_endpointslice("10.244.1.5", "probe-1"))

    def test_fails_when_ip_present_in_a_slice(self):
        import json as _json

        slice_with_ip = self._completed(0, stdout=_json.dumps({"items": [{"endpoints": [{"addresses": ["10.244.1.5"]}]}]}))
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=slice_with_ip):
            self.assertFalse(networkpolicy_check._pod_ip_not_in_any_service_endpointslice("10.244.1.5", "probe-1"))

    def test_fails_on_api_error_reading_endpointslices(self):
        error = self._completed(1)
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=error):
            self.assertFalse(networkpolicy_check._pod_ip_not_in_any_service_endpointslice("10.244.1.5", "probe-1"))


class PodStateTriStateTests(unittest.TestCase):
    """DAY6 fourth remediation item 2: `_pod_state()` must distinguish
    EXISTS / NOT_FOUND / API_ERROR - only an explicit NOT_FOUND (exit 0,
    empty stdout under --ignore-not-found) proves absence; every other
    nonzero-exit case is API_ERROR, never silently folded into "gone"."""

    def _completed(self, returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_exists_when_exit_zero_and_stdout_nonempty(self):
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=self._completed(0, stdout="pod/foo\n")):
            self.assertEqual(networkpolicy_check._pod_state("ns", "foo"), networkpolicy_check.EXISTS)

    def test_not_found_when_exit_zero_and_stdout_empty(self):
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=self._completed(0, stdout="")):
            self.assertEqual(networkpolicy_check._pod_state("ns", "foo"), networkpolicy_check.NOT_FOUND)

    def test_api_error_on_connection_refused(self):
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=self._completed(1, stderr="connection refused")):
            self.assertEqual(networkpolicy_check._pod_state("ns", "foo"), networkpolicy_check.API_ERROR)

    def test_api_error_on_forbidden(self):
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=self._completed(1, stderr="Error from server (Forbidden)")):
            self.assertEqual(networkpolicy_check._pod_state("ns", "foo"), networkpolicy_check.API_ERROR)

    def test_never_confuses_api_error_with_not_found(self):
        with mock.patch.object(networkpolicy_check.kube, "run", return_value=self._completed(1, stderr="unauthorized")):
            state = networkpolicy_check._pod_state("ns", "foo")
        self.assertNotEqual(state, networkpolicy_check.NOT_FOUND)
        self.assertEqual(state, networkpolicy_check.API_ERROR)


class DeletePodAndVerifyGoneTests(unittest.TestCase):
    """DAY6 remediation item 3: `delete_pod_and_verify_gone()` must poll
    for actual absence, never trust `--wait=false` alone. DAY6 fourth
    remediation item 2: that poll now uses the tri-state `_pod_state()` -
    an API_ERROR must fail cleanup immediately, never be retried-through
    as if it might still resolve to gone."""

    def setUp(self):
        networkpolicy_check.results = []

    def _completed(self, returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout="", stderr=stderr)

    def test_successful_deletion(self):
        """Submit succeeds; the very next poll reports an explicit
        NOT_FOUND."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", return_value=networkpolicy_check.NOT_FOUND
        ):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertTrue(ok)
        self.assertIn("confirmed gone", detail)

    def test_submit_api_error_is_a_failure(self):
        """The deletion API call itself failing must be reported as a
        restoration failure - never silently ignored."""
        with mock.patch("subprocess.run", return_value=self._completed(1, "connection refused")):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("could not submit deletion", detail)

    def test_stuck_terminating_times_out_as_a_failure(self):
        """A Pod stuck Terminating (kubectl get pod keeps reporting
        EXISTS, never NOT_FOUND) must be reported as a bounded failure,
        never hang forever and never be silently treated as success."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", return_value=networkpolicy_check.EXISTS
        ), mock.patch("time.sleep"):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=0.01)
        self.assertFalse(ok)
        self.assertIn("did not disappear", detail)

    def test_api_error_while_polling_fails_immediately_never_retried_as_gone(self):
        """DAY6 fourth remediation item 2's core new behavior: an
        API_ERROR observed while polling must fail cleanup right away,
        not be silently treated as "keep polling, might still become
        NOT_FOUND"."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", return_value=networkpolicy_check.API_ERROR
        ), mock.patch("time.sleep"):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("API_ERROR", detail)

    def test_polls_until_gone_then_reports_success(self):
        """Exercises the real polling loop: exists, exists, then gone."""
        calls = {"n": 0}

        def _pod_state_side_effect(_namespace, _name):
            calls["n"] += 1
            return networkpolicy_check.EXISTS if calls["n"] < 3 else networkpolicy_check.NOT_FOUND

        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", side_effect=_pod_state_side_effect
        ), mock.patch("time.sleep"):
            ok, _detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)

    def test_cleanup_succeeds_when_not_found_observed_only_on_the_final_boundary_read(self):
        """DAY6 third remediation (live-discovered boundary race): the
        exact bug a live run hit - the normal polling loop never
        observes NOT_FOUND before its deadline, but the Pod actually
        disappears right around that boundary. The ONE final fresh read
        after the loop must still be allowed to pass the check.

        `time.monotonic` is mocked deterministically (real wall-clock
        timing is too fast/unreliable to force exactly one in-loop
        iteration before the deadline expires): call 1 computes the
        deadline, call 2 (the loop's first condition check) is still
        before it, call 3 (the second condition check, after one loop
        body ran and consumed the first `_pod_state` value) is at/after
        it - forcing exactly one in-loop EXISTS before falling through
        to the final boundary read, which returns NOT_FOUND."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", side_effect=[networkpolicy_check.EXISTS, networkpolicy_check.NOT_FOUND]
        ), mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertTrue(ok)
        self.assertIn("boundary read", detail)

    def test_cleanup_fails_if_the_final_boundary_read_still_returns_exists(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", return_value=networkpolicy_check.EXISTS
        ), mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("did not disappear", detail)
        self.assertIn("boundary read", detail)

    def test_cleanup_fails_immediately_on_final_boundary_read_api_error(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            networkpolicy_check, "_pod_state", side_effect=[networkpolicy_check.EXISTS, networkpolicy_check.API_ERROR]
        ), mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0.0, 0.0, 100.0]):
            ok, detail = networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("API_ERROR", detail)
        self.assertIn("final boundary read", detail)

    def test_never_uses_forced_deletion(self):
        """Forced deletion must never be used to make this check pass -
        the submitted kubectl delete call stays --wait=false with no
        --grace-period=0/--force flags added."""
        with mock.patch("subprocess.run", return_value=self._completed(0)) as run_mock, mock.patch.object(
            networkpolicy_check, "_pod_state", return_value=networkpolicy_check.NOT_FOUND
        ):
            networkpolicy_check.delete_pod_and_verify_gone("ns", "pod", timeout=5.0)
        submitted_cmd = run_mock.call_args_list[0].args[0]
        self.assertNotIn("--force", submitted_cmd)
        self.assertNotIn("--grace-period=0", submitted_cmd)


class CheckApplicationPortNetworkPolicyIsolatedTests(unittest.TestCase):
    """DAY6 third remediation: integration-level coverage of
    `check_application_port_networkpolicy_isolated()` - the four probe
    Pods, the three required assertions, and guaranteed cleanup even
    under partial setup failure. All kubectl-touching calls are mocked;
    this never contacts a live cluster."""

    def setUp(self):
        networkpolicy_check.results = []

    def _running_pod(self, ip: str) -> dict:
        return {
            "status": {"phase": "Running", "podIP": ip},
            "metadata": {"labels": {networkpolicy_check.kube.AMBIENT_DATAPLANE_MODE_LABEL: "none"}, "annotations": {}, "ownerReferences": []},
            "spec": {"containers": [{"name": "probe"}]},
        }

    def _patch_all_pods_healthy(self, ips: dict[str, str]):
        def _wait_running_side_effect(_namespace, pod_name):
            return self._running_pod(ips[pod_name])

        return (
            mock.patch.object(networkpolicy_check, "_apply"),
            mock.patch.object(networkpolicy_check, "_wait_running", side_effect=_wait_running_side_effect),
            mock.patch.object(networkpolicy_check, "_pod_ip_not_in_any_service_endpointslice", return_value=True),
            mock.patch.object(networkpolicy_check, "delete_pod_and_verify_gone", return_value=(True, "confirmed gone")),
        )

    def test_gateway_probe_to_app_probe_is_allowed(self):
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }

        def _run_probe_side_effect(_namespace, pod_name, snippet):
            if pod_name == networkpolicy_check.GATEWAY_SOURCE_POD_NAME and "10.0.0.3" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
            if pod_name == networkpolicy_check.GATEWAY_SOURCE_POD_NAME and "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")
            if pod_name == networkpolicy_check.APP_SOURCE_POD_NAME and "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
            raise AssertionError(f"unexpected probe call: {pod_name}, {snippet[:80]}")

        patches = self._patch_all_pods_healthy(ips)
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()
        self.assertTrue(overall_ok)
        self.assertTrue(any("gateway-probe -> app-probe ALLOWED" in msg and ok for ok, msg in networkpolicy_check.results))

    def test_gateway_probe_to_state_probe_is_denied_only_by_genuine_timeout(self):
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }

        def _run_probe_side_effect(_namespace, pod_name, snippet):
            if pod_name == networkpolicy_check.GATEWAY_SOURCE_POD_NAME and "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")
            return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")

        patches = self._patch_all_pods_healthy(ips)
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()
        self.assertTrue(overall_ok)
        self.assertTrue(any("gateway-probe -> state-probe DENIED" in msg and ok for ok, msg in networkpolicy_check.results))

    def test_tcp_connected_cannot_satisfy_the_denied_assertion(self):
        """The exact live-discovered bug this remediation fixes -
        proven at the integration level: if the isolated gateway->state
        probe ever reported TCP_CONNECTED (which it must not, once
        genuinely isolated from ambient), the check must fail, never
        silently pass."""
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }
        patches = self._patch_all_pods_healthy(ips)
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
            networkpolicy_check, "_run_probe", return_value=networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
        ):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()
        self.assertFalse(overall_ok)

    def test_app_probe_to_state_probe_is_allowed(self):
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }

        def _run_probe_side_effect(_namespace, pod_name, snippet):
            if pod_name == networkpolicy_check.APP_SOURCE_POD_NAME and "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
            if "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")
            return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")

        patches = self._patch_all_pods_healthy(ips)
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()
        self.assertTrue(overall_ok)
        self.assertTrue(any("app-probe -> state-probe ALLOWED" in msg and ok for ok, msg in networkpolicy_check.results))

    def test_direct_target_pod_ips_are_used_never_a_live_service(self):
        """Every snippet passed to _run_probe for a target connection
        must reference the target's own Pod IP - never the Service DNS
        names (maops-app/maops-state) this script's other checks use."""
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }
        captured_snippets = []

        def _run_probe_side_effect(_namespace, _pod_name, snippet):
            captured_snippets.append(snippet)
            return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")

        patches = self._patch_all_pods_healthy(ips)
        with patches[0], patches[1], patches[2], patches[3], mock.patch.object(networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect):
            networkpolicy_check.check_application_port_networkpolicy_isolated()

        self.assertEqual(len(captured_snippets), 3)
        for snippet in captured_snippets:
            self.assertTrue(any(ip in snippet for ip in ("10.0.0.3", "10.0.0.4")))
            self.assertNotIn("maops-app.maops-platform.svc.cluster.local", snippet)
            self.assertNotIn("maops-state.maops-platform.svc.cluster.local", snippet)
            self.assertNotIn('"maops-app"', snippet)
            self.assertNotIn('"maops-state"', snippet)

    def test_partial_setup_failure_still_cleans_every_pod_that_was_created(self):
        """One probe Pod fails to apply; the other three must still be
        created, tracked, and cleaned up - cleanup is never skipped for
        the Pods that DID succeed just because a sibling failed."""

        def _apply_side_effect(manifest: str):
            if networkpolicy_check.APP_TARGET_POD_NAME in manifest:
                raise subprocess.CalledProcessError(1, ["kubectl"], stderr="admission webhook denied")

        cleanup_calls = []

        def _cleanup_side_effect(_namespace, pod_name, timeout=None):
            cleanup_calls.append(pod_name)
            return True, f"pod {pod_name!r} confirmed gone"

        def _wait_running_side_effect(_namespace, pod_name):
            ip_map = {
                networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
                networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
                networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
            }
            return self._running_pod(ip_map[pod_name])

        with mock.patch.object(networkpolicy_check, "_apply", side_effect=_apply_side_effect), mock.patch.object(
            networkpolicy_check, "_wait_running", side_effect=_wait_running_side_effect
        ), mock.patch.object(networkpolicy_check, "_pod_ip_not_in_any_service_endpointslice", return_value=True), mock.patch.object(
            networkpolicy_check, "delete_pod_and_verify_gone", side_effect=_cleanup_side_effect
        ), mock.patch.object(
            networkpolicy_check, "_run_probe", return_value=networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")
        ):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()

        self.assertFalse(overall_ok)
        self.assertIn(networkpolicy_check.GATEWAY_SOURCE_POD_NAME, cleanup_calls)
        self.assertIn(networkpolicy_check.APP_SOURCE_POD_NAME, cleanup_calls)
        self.assertIn(networkpolicy_check.STATE_TARGET_POD_NAME, cleanup_calls)
        self.assertNotIn(networkpolicy_check.APP_TARGET_POD_NAME, cleanup_calls)

    def test_cleanup_failure_fails_the_check_even_when_all_assertions_passed(self):
        ips = {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }

        def _run_probe_side_effect(_namespace, pod_name, snippet):
            if "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECT_TIMEOUT, "RESULT=TCP_CONNECT_TIMEOUT")
            return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")

        with mock.patch.object(networkpolicy_check, "_apply"), mock.patch.object(
            networkpolicy_check, "_wait_running", side_effect=lambda _ns, name: self._running_pod(ips[name])
        ), mock.patch.object(networkpolicy_check, "_pod_ip_not_in_any_service_endpointslice", return_value=True), mock.patch.object(
            networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect
        ), mock.patch.object(
            networkpolicy_check, "delete_pod_and_verify_gone", return_value=(False, "pod did not disappear")
        ):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()

        self.assertFalse(overall_ok)

    def _ips(self) -> dict[str, str]:
        return {
            networkpolicy_check.GATEWAY_SOURCE_POD_NAME: "10.0.0.1",
            networkpolicy_check.APP_SOURCE_POD_NAME: "10.0.0.2",
            networkpolicy_check.APP_TARGET_POD_NAME: "10.0.0.3",
            networkpolicy_check.STATE_TARGET_POD_NAME: "10.0.0.4",
        }

    def test_ambient_redirected_source_pod_is_never_probed_and_fails_closed(self):
        """DAY6 review TEST-2: drives a real (unmocked)
        `_verify_probe_pod_isolated()` gate failure through the
        orchestration. The gateway-labelled source Pod comes back
        carrying the ambient redirection annotation - i.e. it is NOT
        isolated from ztunnel. No ALLOWED/DENIED assertion may then run
        from that Pod (its raw connect() would only prove node-local
        ztunnel acceptance), the check must fail closed, and cleanup
        must still be attempted and verified for all four Pods."""
        ips = self._ips()

        def _wait_running_side_effect(_namespace, pod_name):
            pod = self._running_pod(ips[pod_name])
            if pod_name == networkpolicy_check.GATEWAY_SOURCE_POD_NAME:
                pod["metadata"]["annotations"] = {networkpolicy_check.AMBIENT_REDIRECTION_ANNOTATION: "enabled"}
            return pod

        probe_sources = []

        def _run_probe_side_effect(_namespace, pod_name, snippet):
            probe_sources.append(pod_name)
            if pod_name == networkpolicy_check.APP_SOURCE_POD_NAME and "10.0.0.4" in snippet:
                return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
            raise AssertionError(f"probe must never run from a non-isolated Pod: {pod_name}, {snippet[:80]}")

        cleanup_calls = []

        def _cleanup_side_effect(_namespace, pod_name, timeout=None):
            cleanup_calls.append(pod_name)
            return True, f"pod {pod_name!r} confirmed gone"

        with mock.patch.object(networkpolicy_check, "_apply"), mock.patch.object(
            networkpolicy_check, "_wait_running", side_effect=_wait_running_side_effect
        ), mock.patch.object(networkpolicy_check, "_pod_ip_not_in_any_service_endpointslice", return_value=True), mock.patch.object(
            networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect
        ), mock.patch.object(
            networkpolicy_check, "delete_pod_and_verify_gone", side_effect=_cleanup_side_effect
        ):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()

        self.assertFalse(overall_ok)
        self.assertNotIn(networkpolicy_check.GATEWAY_SOURCE_POD_NAME, probe_sources)
        self.assertEqual(probe_sources, [networkpolicy_check.APP_SOURCE_POD_NAME])
        failed = [m for ok, m in networkpolicy_check.results if not ok]
        self.assertTrue(any(networkpolicy_check.AMBIENT_REDIRECTION_ANNOTATION in m for m in failed))
        self.assertTrue(any("gateway-probe -> app-probe check: source or target probe Pod unavailable" in m for m in failed))
        self.assertTrue(any("gateway-probe -> state-probe check: source or target probe Pod unavailable" in m for m in failed))
        # The gateway-probe DENIED assertion must never have been
        # recorded as passing from a non-isolated source.
        self.assertFalse(any("gateway-probe -> state-probe DENIED" in m for ok, m in networkpolicy_check.results if ok))
        self.assertCountEqual(cleanup_calls, list(ips.keys()))
        self.assertEqual(sum(1 for ok, m in networkpolicy_check.results if ok and "confirmed gone" in m), 4)

    def test_target_ip_present_in_service_endpointslice_is_never_probed(self):
        """DAY6 review TEST-2: the second isolation gate. A target probe
        Pod whose IP appears in a live application Service's
        EndpointSlice must be excluded before any assertion targets it;
        the check fails closed and every Pod is still cleaned up."""
        ips = self._ips()
        captured_snippets = []

        def _run_probe_side_effect(_namespace, _pod_name, snippet):
            captured_snippets.append(snippet)
            return networkpolicy_check.ProbeResult(networkpolicy_check.TCP_CONNECTED, "RESULT=TCP_CONNECTED")

        cleanup_calls = []

        def _cleanup_side_effect(_namespace, pod_name, timeout=None):
            cleanup_calls.append(pod_name)
            return True, f"pod {pod_name!r} confirmed gone"

        with mock.patch.object(networkpolicy_check, "_apply"), mock.patch.object(
            networkpolicy_check, "_wait_running", side_effect=lambda _ns, name: self._running_pod(ips[name])
        ), mock.patch.object(
            networkpolicy_check, "_pod_ip_not_in_any_service_endpointslice", side_effect=lambda ip, _name: ip != "10.0.0.4"
        ), mock.patch.object(
            networkpolicy_check, "_run_probe", side_effect=_run_probe_side_effect
        ), mock.patch.object(
            networkpolicy_check, "delete_pod_and_verify_gone", side_effect=_cleanup_side_effect
        ):
            overall_ok = networkpolicy_check.check_application_port_networkpolicy_isolated()

        self.assertFalse(overall_ok)
        self.assertFalse(any("10.0.0.4" in s for s in captured_snippets))
        self.assertEqual(len(captured_snippets), 1)
        self.assertFalse(any("state-probe" in m and ("DENIED" in m or "ALLOWED" in m) for ok, m in networkpolicy_check.results if ok))
        self.assertCountEqual(cleanup_calls, list(ips.keys()))


if __name__ == "__main__":
    unittest.main()

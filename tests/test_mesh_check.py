"""
Docker/Kubernetes-free unit tests for the pure, separable logic in
scripts/mesh_check.py - the manifest generation, classification
wrappers, tri-state existence checks, structured ztunnel access-log
parsing, and the transactional RUST_LOG mutation/restoration machinery
added by the DAY6 remediations. Live-cluster interaction (kubectl
exec/apply/delete) is exercised only by the live script itself, never by
these unit tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mesh_check
import networkpolicy_check as netpol


class MeshProbeNamespaceManifestTests(unittest.TestCase):
    def test_targets_expected_namespace_name(self):
        manifest = mesh_check._mesh_probe_namespace_manifest()
        self.assertIn(f"name: {mesh_check.MESH_PROBE_NAMESPACE}", manifest)

    def test_is_ambient_enrolled(self):
        manifest = mesh_check._mesh_probe_namespace_manifest()
        self.assertIn("istio.io/dataplane-mode: ambient", manifest)

    def test_never_reuses_the_normal_validation_namespace(self):
        """DAY6 remediation: this must be a SEPARATE namespace from
        maops-day6-validation, which stays deliberately non-ambient/
        untrusted for the RBAC/NetworkPolicy checks that already use
        it."""
        self.assertNotEqual(mesh_check.MESH_PROBE_NAMESPACE, netpol.VALIDATION_NAMESPACE)


class MeshProbeServiceAccountAndPodManifestTests(unittest.TestCase):
    def test_serviceaccount_has_no_automount(self):
        manifest = mesh_check._mesh_probe_serviceaccount_and_pod_manifest()
        self.assertIn("automountServiceAccountToken: false", manifest)

    def test_pod_runs_as_the_dedicated_serviceaccount(self):
        manifest = mesh_check._mesh_probe_serviceaccount_and_pod_manifest()
        self.assertIn(f"serviceAccountName: {mesh_check.MESH_PROBE_SERVICE_ACCOUNT}", manifest)

    def test_serviceaccount_name_never_collides_with_an_allowed_principal(self):
        """Foundational to this being a genuine wrong-identity test:
        the probe's own ServiceAccount must never accidentally equal
        one of the names EXPECTED_AUTHZ actually allows."""
        allowed_service_account_names = {
            expected["principal"].rsplit("/", 1)[-1] for expected in mesh_check.EXPECTED_AUTHZ.values()
        }
        self.assertNotIn(mesh_check.MESH_PROBE_SERVICE_ACCOUNT, allowed_service_account_names)

    def test_pod_carries_no_kubernetes_api_token(self):
        manifest = mesh_check._mesh_probe_serviceaccount_and_pod_manifest()
        pod_section = manifest.split("kind: Pod", 1)[1]
        self.assertIn("automountServiceAccountToken: false", pod_section)

    def test_pod_security_context_non_root(self):
        manifest = mesh_check._mesh_probe_serviceaccount_and_pod_manifest()
        self.assertIn("runAsNonRoot: true", manifest)
        self.assertIn("runAsUser: 10001", manifest)

    def test_pod_bounded_by_active_deadline(self):
        manifest = mesh_check._mesh_probe_serviceaccount_and_pod_manifest()
        self.assertIn("activeDeadlineSeconds: 120", manifest)


class IsAmbientEnrolledTests(unittest.TestCase):
    def test_enrolled_pod_no_sidecar_and_annotation_present(self):
        pod = {
            "spec": {"containers": [{"name": "probe"}]},
            "metadata": {"annotations": {"ambient.istio.io/redirection": "enabled"}},
        }
        no_sidecar, redirected = mesh_check._is_ambient_enrolled(pod)
        self.assertTrue(no_sidecar)
        self.assertTrue(redirected)

    def test_sidecar_present_fails_no_sidecar_check(self):
        pod = {
            "spec": {"containers": [{"name": "probe"}, {"name": "istio-proxy"}]},
            "metadata": {"annotations": {"ambient.istio.io/redirection": "enabled"}},
        }
        no_sidecar, redirected = mesh_check._is_ambient_enrolled(pod)
        self.assertFalse(no_sidecar)
        self.assertTrue(redirected)

    def test_missing_annotation_fails_redirection_check(self):
        pod = {"spec": {"containers": [{"name": "probe"}]}, "metadata": {"annotations": {}}}
        no_sidecar, redirected = mesh_check._is_ambient_enrolled(pod)
        self.assertTrue(no_sidecar)
        self.assertFalse(redirected)

    def test_missing_annotations_key_entirely_fails_redirection_check(self):
        pod = {"spec": {"containers": [{"name": "probe"}]}, "metadata": {}}
        no_sidecar, redirected = mesh_check._is_ambient_enrolled(pod)
        self.assertTrue(no_sidecar)
        self.assertFalse(redirected)

    def test_wrong_annotation_value_fails_redirection_check(self):
        pod = {
            "spec": {"containers": [{"name": "probe"}]},
            "metadata": {"annotations": {"ambient.istio.io/redirection": "disabled"}},
        }
        _no_sidecar, redirected = mesh_check._is_ambient_enrolled(pod)
        self.assertFalse(redirected)


class AssertConnectedWrapperTests(unittest.TestCase):
    """DAY6 remediation: mesh_check.py reuses networkpolicy_check.py's
    phase-attributed model via a local wrapper that records into
    mesh_check's OWN results list (never networkpolicy_check's)."""

    def setUp(self):
        mesh_check.results = []

    def test_assert_connected_passes_on_tcp_connected(self):
        result = netpol.ProbeResult(netpol.TCP_CONNECTED, "connected")
        self.assertTrue(mesh_check._assert_connected(result, "label"))

    def test_assert_connected_passes_on_http_response(self):
        result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=200")
        self.assertTrue(mesh_check._assert_connected(result, "label"))

    def test_assert_connected_fails_on_tcp_connect_timeout(self):
        result = netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "timed out")
        self.assertFalse(mesh_check._assert_connected(result, "label"))

    def test_assert_connected_fails_on_exec_inconclusive(self):
        result = netpol.ProbeResult(netpol.EXEC_INCONCLUSIVE, "kubectl exec failed")
        self.assertFalse(mesh_check._assert_connected(result, "label"))

    def test_assert_connected_fails_on_output_inconclusive(self):
        result = netpol.ProbeResult(netpol.OUTPUT_INCONCLUSIVE, "unparseable")
        self.assertFalse(mesh_check._assert_connected(result, "label"))

    def test_wrapper_records_into_mesh_check_results_not_netpol_results(self):
        netpol.results = []
        mesh_check.results = []
        mesh_check._assert_connected(netpol.ProbeResult(netpol.TCP_CONNECTED, "ok"), "label")
        self.assertEqual(len(mesh_check.results), 1)
        self.assertEqual(len(netpol.results), 0)


class ClientSideSupportingEvidenceTests(unittest.TestCase):
    """DAY6 fifth remediation (corrected after the first live run
    against maops-k8s-day6, which showed all three wrong-identity raw
    TCP connect() probes reporting TCP_CONNECTED - not because the
    unauthorized identity reached the application, but because in
    ambient mode a source Pod's connect() completes against its own
    NODE-LOCAL ztunnel before the destination-side HBONE/
    AuthorizationPolicy evaluation ever runs). `_record_client_side_supporting_evidence()`
    now NEVER hard-fails on any raw-TCP outcome - every one, including
    TCP_CONNECTED (relabeled LOCAL_ZTUNNEL_CONNECT_ACCEPTED), is
    non-gating supporting evidence only. The actual application-
    reachability leak check moved to the separate HTTP-level
    `_record_client_side_http_leak_check()` (see
    ClientSideHttpLeakCheckTests below)."""

    def setUp(self):
        mesh_check.results = []

    def test_tcp_connect_timeout_is_supporting_evidence_and_passes(self):
        result = netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "timed out")
        mesh_check._record_client_side_supporting_evidence(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)

    def test_connection_refused_or_reset_is_supporting_evidence_and_passes(self):
        """ztunnel actively resetting/closing a denied connection must
        NOT be treated as a failure or as insufficient - it is just as
        plausible a mesh-denial signature as a silent timeout."""
        result = netpol.ProbeResult(netpol.CONNECTION_REFUSED_OR_RESET, "connection reset by peer")
        mesh_check._record_client_side_supporting_evidence(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)

    def test_tcp_connected_is_now_non_gating_supporting_evidence_never_a_hard_failure(self):
        """The core fix this remediation makes: a live run proved
        TCP_CONNECTED is NOT authorization success in ambient mode - it
        is relabeled LOCAL_ZTUNNEL_CONNECT_ACCEPTED and must always pass
        as non-gating supporting evidence, never fail."""
        result = netpol.ProbeResult(netpol.TCP_CONNECTED, "connected")
        mesh_check._record_client_side_supporting_evidence(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("LOCAL_ZTUNNEL_CONNECT_ACCEPTED", message)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)
        self.assertNotIn("unexpectedly succeeded", message)

    def test_exec_inconclusive_is_non_gating_supporting_evidence(self):
        """Not a denial-shaped outcome, but also not a false client-side
        success - never gates, always recorded as (informational)
        supporting evidence."""
        result = netpol.ProbeResult(netpol.EXEC_INCONCLUSIVE, "kubectl exec failed")
        mesh_check._record_client_side_supporting_evidence(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("informational only", message)

    def test_never_satisfies_denial_by_itself_never_claims_authorizationpolicy(self):
        """No supporting state's message may claim the
        AuthorizationPolicy-specific denial assertion itself - that must
        always be attributed to the correlated log evidence."""
        for outcome in (netpol.TCP_CONNECT_TIMEOUT, netpol.CONNECTION_REFUSED_OR_RESET, netpol.TCP_CONNECTED):
            with self.subTest(outcome=outcome):
                mesh_check.results = []
                mesh_check._record_client_side_supporting_evidence(netpol.ProbeResult(outcome, "x"), "label")
                _ok, message = mesh_check.results[-1]
                self.assertIn("never by itself proves or disproves AuthorizationPolicy denial", message)


class ClientSideHttpLeakCheckTests(unittest.TestCase):
    """DAY6 fifth remediation item 4: `_record_client_side_http_leak_check()`
    is the actual application-reachability leak check, at the HTTP
    layer against /livez. HTTP 200 is the one hard, gating failure it
    reports - a ztunnel-generated 401 (or any other non-200 status), a
    reset/closed connection, a read timeout, or an INCONCLUSIVE probe
    are all non-gating supporting evidence only."""

    def setUp(self):
        mesh_check.results = []

    def test_http_200_is_a_hard_failure(self):
        result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=200")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("reached the application", message)

    def test_http_401_is_non_gating_supporting_evidence_only(self):
        """The exact live-observed shape: ztunnel can synthesize an
        HTTP 401 at the client layer for a denied identity - this must
        never be treated as a failure, and must never by itself satisfy
        denial either."""
        result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=401")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)
        self.assertIn("status=401", message)

    def test_other_non_200_status_is_supporting_evidence_only(self):
        result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=403")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, _message = mesh_check.results[-1]
        self.assertTrue(ok)

    def test_connection_refused_or_reset_is_supporting_evidence_only(self):
        result = netpol.ProbeResult(netpol.CONNECTION_REFUSED_OR_RESET, "connection reset by peer")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)

    def test_http_read_timeout_is_supporting_evidence_only(self):
        result = netpol.ProbeResult(netpol.HTTP_READ_TIMEOUT, "read timed out")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)

    def test_exec_inconclusive_is_supporting_evidence_only_never_gates(self):
        result = netpol.ProbeResult(netpol.EXEC_INCONCLUSIVE, "kubectl exec failed")
        mesh_check._record_client_side_http_leak_check(result, "label")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("SUPPORTING EVIDENCE ONLY", message)

    def test_no_supporting_outcome_claims_the_denial_assertion_itself(self):
        for outcome, detail in (
            (netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=401"),
            (netpol.CONNECTION_REFUSED_OR_RESET, "reset"),
            (netpol.HTTP_READ_TIMEOUT, "timeout"),
        ):
            with self.subTest(outcome=outcome):
                mesh_check.results = []
                mesh_check._record_client_side_http_leak_check(netpol.ProbeResult(outcome, detail), "label")
                _ok, message = mesh_check.results[-1]
                self.assertIn("never by itself proves AuthorizationPolicy denial", message)


class PodIfRunningOrTerminalTests(unittest.TestCase):
    def test_returns_none_on_kubectl_failure(self):
        """kubectl itself failing (e.g. Pod not found yet) must be
        treated as 'not ready yet, keep polling' - never as a terminal
        state - so kube.wait_until() keeps retrying instead of giving
        up early."""
        fake_result = mock.Mock(returncode=1)
        with mock.patch.object(mesh_check.kube, "run", return_value=fake_result):
            outcome = mesh_check._pod_if_running_or_terminal("ns", "pod")
        self.assertIsNone(outcome)


class ResourceStateTriStateTests(unittest.TestCase):
    """DAY6 fourth remediation item 2: `_resource_state()` must
    distinguish EXISTS / NOT_FOUND / API_ERROR - only an explicit
    NOT_FOUND (exit 0, empty stdout under --ignore-not-found) proves
    absence; every other nonzero-exit case is API_ERROR."""

    def _completed(self, returncode: int, stdout: str = "", stderr: str = ""):
        import subprocess as sp

        return sp.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_exists_when_exit_zero_and_stdout_nonempty(self):
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(0, stdout="namespace/foo\n")):
            self.assertEqual(mesh_check._resource_state("namespace", "foo"), mesh_check.EXISTS)

    def test_not_found_when_exit_zero_and_stdout_empty(self):
        """The documented --ignore-not-found contract: genuinely absent
        resources produce exit 0 with EMPTY stdout, never a nonzero
        exit."""
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(0, stdout="")):
            self.assertEqual(mesh_check._resource_state("namespace", "foo"), mesh_check.NOT_FOUND)

    def test_api_error_on_connection_refused(self):
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(1, stderr="connection refused")):
            self.assertEqual(mesh_check._resource_state("namespace", "foo"), mesh_check.API_ERROR)

    def test_api_error_on_forbidden(self):
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(1, stderr="Error from server (Forbidden)")):
            self.assertEqual(mesh_check._resource_state("namespace", "foo"), mesh_check.API_ERROR)

    def test_api_error_on_timeout_like_nonzero_exit(self):
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(1, stderr="context deadline exceeded")):
            self.assertEqual(mesh_check._resource_state("namespace", "foo"), mesh_check.API_ERROR)

    def test_never_confuses_api_error_with_not_found(self):
        """The exact regression this item fixes: a generic API failure
        must never be classified the same as a genuine NotFound."""
        with mock.patch.object(mesh_check.kube, "run", return_value=self._completed(1, stderr="unauthorized")):
            state = mesh_check._resource_state("pod", "foo", namespace="ns")
        self.assertNotEqual(state, mesh_check.NOT_FOUND)
        self.assertEqual(state, mesh_check.API_ERROR)


class DeleteNamespaceAndVerifyGoneTests(unittest.TestCase):
    """DAY6 remediation item 3 (polling for actual absence) + fourth
    remediation item 2 (tri-state EXISTS/NOT_FOUND/API_ERROR - an
    API_ERROR must fail cleanup immediately, never be retried-through as
    if it might resolve to gone)."""

    def setUp(self):
        mesh_check.results = []

    def _completed(self, returncode: int, stderr: str = ""):
        import subprocess as sp

        return sp.CompletedProcess(args=["kubectl"], returncode=returncode, stdout="", stderr=stderr)

    def test_successful_deletion(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            mesh_check, "_resource_state", return_value=mesh_check.NOT_FOUND
        ):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=5.0)
        self.assertTrue(ok)
        self.assertIn("confirmed gone", detail)

    def test_submit_api_error_is_a_failure(self):
        with mock.patch("subprocess.run", return_value=self._completed(1, "connection refused")):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("could not submit deletion", detail)

    def test_stuck_terminating_times_out_as_a_failure(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            mesh_check, "_resource_state", return_value=mesh_check.EXISTS
        ), mock.patch("time.sleep"):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=0.01)
        self.assertFalse(ok)
        self.assertIn("did not disappear", detail)

    def test_api_error_while_polling_fails_immediately_never_retried_as_gone(self):
        """DAY6 fourth remediation item 2's core new behavior: an
        API_ERROR observed while polling must fail cleanup right away,
        not be silently treated as "keep polling, might still become
        NOT_FOUND"."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            mesh_check, "_resource_state", return_value=mesh_check.API_ERROR
        ), mock.patch("time.sleep"):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("API_ERROR", detail)

    def test_leaked_resource_after_namespace_reported_gone_is_a_failure(self):
        """DAY6 remediation item 3's explicit ask: independently verify
        no probe Pod/ServiceAccount remains, even after the namespace
        itself reports gone."""
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            mesh_check, "_resource_state", side_effect=[mesh_check.NOT_FOUND, mesh_check.EXISTS, mesh_check.NOT_FOUND]
        ):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("still found", detail)

    def test_api_error_re_checking_leaked_resource_is_a_failure_not_a_silent_pass(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)), mock.patch.object(
            mesh_check, "_resource_state", side_effect=[mesh_check.NOT_FOUND, mesh_check.API_ERROR]
        ):
            ok, detail = mesh_check.delete_namespace_and_verify_gone("ns", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("API_ERROR", detail)


class ZtunnelLogEnvStateTests(unittest.TestCase):
    """DAY6 fourth remediation item 1: `_ztunnel_log_env_state()` must
    capture the COMPLETE original representation - absent, literal
    value, or valueFrom - not merely a `.value` string."""

    def test_absent_entirely(self):
        daemonset = {"spec": {"template": {"spec": {"containers": [{"env": []}]}}}}
        with mock.patch.object(mesh_check, "_get_json", return_value=daemonset):
            state = mesh_check._ztunnel_log_env_state()
        self.assertTrue(state.found_daemonset)
        self.assertFalse(state.present)
        self.assertIsNone(state.value)
        self.assertFalse(state.uses_value_from)

    def test_present_with_literal_value(self):
        daemonset = {"spec": {"template": {"spec": {"containers": [{"env": [{"name": "RUST_LOG", "value": "warn"}]}]}}}}
        with mock.patch.object(mesh_check, "_get_json", return_value=daemonset):
            state = mesh_check._ztunnel_log_env_state()
        self.assertTrue(state.present)
        self.assertEqual(state.value, "warn")
        self.assertFalse(state.uses_value_from)

    def test_present_with_value_from(self):
        """The case `kubectl set env` cannot restore exactly -
        `check_authorization_denial_isolated()` must refuse to mutate at
        all when this is detected."""
        env_entry = {"name": "RUST_LOG", "valueFrom": {"configMapKeyRef": {"name": "cm", "key": "level"}}}
        daemonset = {"spec": {"template": {"spec": {"containers": [{"env": [env_entry]}]}}}}
        with mock.patch.object(mesh_check, "_get_json", return_value=daemonset):
            state = mesh_check._ztunnel_log_env_state()
        self.assertTrue(state.present)
        self.assertTrue(state.uses_value_from)
        self.assertIsNone(state.value)
        self.assertEqual(state.raw_entry, env_entry)

    def test_daemonset_unreadable(self):
        with mock.patch.object(mesh_check, "_get_json", return_value=None):
            state = mesh_check._ztunnel_log_env_state()
        self.assertFalse(state.found_daemonset)


class ZtunnelEnvMutationStepTests(unittest.TestCase):
    """DAY6 fourth remediation item 1: submission and rollout-wait are
    now SEPARATE steps, each independently testable - the third
    remediation's combined function could not distinguish "submission
    itself failed" (safe to skip restoration) from "submission
    succeeded but rollout failed" (restoration now mandatory)."""

    def setUp(self):
        mesh_check.results = []

    def _completed(self, returncode: int, stderr: str = ""):
        import subprocess as sp

        return sp.CompletedProcess(args=["kubectl"], returncode=returncode, stdout="", stderr=stderr)

    def test_submit_literal_success(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)):
            ok, detail = mesh_check._submit_ztunnel_env_literal("info,access_log=info")
        self.assertTrue(ok)
        self.assertIn("submitted", detail)

    def test_submit_literal_failure(self):
        with mock.patch("subprocess.run", return_value=self._completed(1, "daemonset not found")):
            ok, detail = mesh_check._submit_ztunnel_env_literal("info")
        self.assertFalse(ok)
        self.assertIn("daemonset not found", detail)

    def test_submit_unset_uses_removal_syntax(self):
        captured = []

        def _capture(cmd, **_kwargs):
            captured.append(cmd)
            return self._completed(0)

        with mock.patch("subprocess.run", side_effect=_capture):
            mesh_check._submit_ztunnel_env_unset()
        self.assertIn(f"{mesh_check.ZTUNNEL_LOG_ENV_VAR}-", captured[0])

    def test_wait_rollout_success(self):
        with mock.patch("subprocess.run", return_value=self._completed(0)):
            ok, detail = mesh_check._wait_ztunnel_rollout()
        self.assertTrue(ok)
        self.assertIn("completed", detail)

    def test_wait_rollout_failure(self):
        with mock.patch("subprocess.run", return_value=self._completed(1, "timed out waiting for rollout")):
            ok, detail = mesh_check._wait_ztunnel_rollout()
        self.assertFalse(ok)
        self.assertIn("did not complete", detail)

    def test_all_pods_ready_true(self):
        pods = {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
        with mock.patch.object(mesh_check, "_get_json", return_value=pods):
            ok, detail = mesh_check._all_ztunnel_pods_ready()
        self.assertTrue(ok)
        self.assertIn("1/1", detail)

    def test_all_pods_ready_false_when_one_not_ready(self):
        pods = {
            "items": [
                {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                {"status": {"conditions": [{"type": "Ready", "status": "False"}]}},
            ]
        }
        with mock.patch.object(mesh_check, "_get_json", return_value=pods):
            ok, detail = mesh_check._all_ztunnel_pods_ready()
        self.assertFalse(ok)
        self.assertIn("1/2", detail)

    def test_all_pods_ready_false_when_read_fails(self):
        with mock.patch.object(mesh_check, "_get_json", return_value=None):
            ok, _detail = mesh_check._all_ztunnel_pods_ready()
        self.assertFalse(ok)

    def test_all_pods_ready_false_when_no_pods_at_all(self):
        """An empty ztunnel Pod list must never be treated as vacuously
        'all Ready' - that would mask a fully-down DaemonSet."""
        with mock.patch.object(mesh_check, "_get_json", return_value={"items": []}):
            ok, _detail = mesh_check._all_ztunnel_pods_ready()
        self.assertFalse(ok)


class RestoreZtunnelLogEnvTests(unittest.TestCase):
    """DAY6 fourth remediation item 1: `_restore_ztunnel_log_env()` is
    the transactional restoration sequence - submit -> wait for rollout
    -> reread -> verify exact match -> verify Pods Ready. Any
    uncertainty at any step must be its own explicit RESTORATION
    FAILURE, never silently treated as success."""

    def setUp(self):
        mesh_check.results = []

    def _pass_through(self, *, submit_ok=True, rollout_ok=True, reread_state=None, pods_ready=True):
        original = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        if reread_state is None:
            reread_state = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        patches = [
            mock.patch.object(mesh_check, "_submit_ztunnel_env_unset", return_value=(submit_ok, "submitted")),
            mock.patch.object(mesh_check, "_submit_ztunnel_env_literal", return_value=(submit_ok, "submitted")),
            mock.patch.object(mesh_check, "_wait_ztunnel_rollout", return_value=(rollout_ok, "rollout")),
            mock.patch.object(mesh_check, "_ztunnel_log_env_state", return_value=reread_state),
            mock.patch.object(mesh_check, "_all_ztunnel_pods_ready", return_value=(pods_ready, "3/3 ztunnel Pods Ready")),
        ]
        return original, patches

    def test_full_success_when_originally_absent(self):
        original, patches = self._pass_through()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertIn("restored and verified", message)

    def test_restores_via_unset_when_originally_absent(self):
        original = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        with mock.patch.object(mesh_check, "_submit_ztunnel_env_unset", return_value=(True, "unset")) as unset_mock, mock.patch.object(
            mesh_check, "_submit_ztunnel_env_literal"
        ) as literal_mock, mock.patch.object(mesh_check, "_wait_ztunnel_rollout", return_value=(True, "ok")), mock.patch.object(
            mesh_check, "_ztunnel_log_env_state", return_value=original
        ), mock.patch.object(
            mesh_check, "_all_ztunnel_pods_ready", return_value=(True, "3/3")
        ):
            mesh_check._restore_ztunnel_log_env(original)
        unset_mock.assert_called_once()
        literal_mock.assert_not_called()

    def test_restores_via_literal_when_originally_present(self):
        original = mesh_check.ZtunnelLogEnvState(True, True, "warn", False, {"name": "RUST_LOG", "value": "warn"})
        with mock.patch.object(mesh_check, "_submit_ztunnel_env_literal", return_value=(True, "set")) as literal_mock, mock.patch.object(
            mesh_check, "_submit_ztunnel_env_unset"
        ) as unset_mock, mock.patch.object(mesh_check, "_wait_ztunnel_rollout", return_value=(True, "ok")), mock.patch.object(
            mesh_check, "_ztunnel_log_env_state", return_value=original
        ), mock.patch.object(
            mesh_check, "_all_ztunnel_pods_ready", return_value=(True, "3/3")
        ):
            mesh_check._restore_ztunnel_log_env(original)
        literal_mock.assert_called_once_with("warn")
        unset_mock.assert_not_called()

    def test_submit_failure_is_restoration_failure(self):
        original, patches = self._pass_through(submit_ok=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("RESTORATION FAILURE", message)
        self.assertIn("could not submit", message)

    def test_rollout_failure_is_restoration_failure(self):
        original, patches = self._pass_through(rollout_ok=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("RESTORATION FAILURE", message)
        self.assertIn("rollout did not complete", message)

    def test_reread_unreadable_is_restoration_failure(self):
        unreadable = mesh_check.ZtunnelLogEnvState(False, False, None, False, None)
        original, patches = self._pass_through(reread_state=unreadable)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("RESTORATION FAILURE", message)
        self.assertIn("reread", message)

    def test_value_mismatch_after_restore_is_restoration_failure(self):
        """The exact-match verification this item requires: restoring
        to the WRONG value must be caught, not just 'restoration
        submitted successfully'."""
        original = mesh_check.ZtunnelLogEnvState(True, True, "warn", False, {"name": "RUST_LOG", "value": "warn"})
        mismatched = mesh_check.ZtunnelLogEnvState(True, True, "info", False, {"name": "RUST_LOG", "value": "info"})
        _o, patches = self._pass_through(reread_state=mismatched)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("RESTORATION FAILURE", message)
        self.assertIn("does not exactly match", message)

    def test_value_from_mismatch_after_restore_is_restoration_failure(self):
        """present/value matching alone is not enough - uses_value_from
        must match too, since a literal 'warn' is not the same
        representation as a valueFrom entry that happens to resolve to
        'warn'."""
        original = mesh_check.ZtunnelLogEnvState(True, True, None, True, {"name": "RUST_LOG", "valueFrom": {}})
        mismatched = mesh_check.ZtunnelLogEnvState(True, True, None, False, {"name": "RUST_LOG", "value": None})
        _o, patches = self._pass_through(reread_state=mismatched)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, _message = mesh_check.results[-1]
        self.assertFalse(ok)

    def test_pods_not_ready_after_restore_is_restoration_failure(self):
        original, patches = self._pass_through(pods_ready=False)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            mesh_check._restore_ztunnel_log_env(original)
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("RESTORATION FAILURE", message)
        self.assertIn("not all ztunnel Pods are Ready", message)


class TransactionalRestorationAlwaysRunsTests(unittest.TestCase):
    """DAY6 fourth remediation item 1's core regression test: once
    `_submit_ztunnel_env_literal()` for the DIAGNOSTIC mutation
    succeeds, `_restore_ztunnel_log_env()` must be invoked even if the
    diagnostic rollout wait fails, a probe raises, or log
    retrieval/parsing fails. This directly exercises the bug the third
    remediation had: a combined submit+rollout function meant a rollout
    failure returned early WITHOUT ever restoring."""

    def setUp(self):
        mesh_check.results = []

    def test_restoration_runs_even_when_default_log_probing_raises_then_diagnostic_rollout_times_out(self):
        original = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        with mock.patch.object(mesh_check, "_ztunnel_log_env_state", return_value=original), mock.patch.object(
            netpol, "_run_probe", side_effect=RuntimeError("unexpected probe crash")
        ), mock.patch.object(mesh_check, "_submit_ztunnel_env_literal", return_value=(True, "submitted")), mock.patch.object(
            mesh_check, "_wait_ztunnel_rollout", return_value=(False, "diagnostic rollout timed out")
        ), mock.patch.object(
            mesh_check, "_restore_ztunnel_log_env"
        ) as restore_mock:
            mesh_check.check_authorization_denial_isolated()
        restore_mock.assert_called_once_with(original)

    def test_restoration_runs_even_when_log_retrieval_is_empty(self):
        original = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        with mock.patch.object(mesh_check, "_ztunnel_log_env_state", return_value=original), mock.patch.object(
            netpol, "_run_probe", return_value=netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "x")
        ), mock.patch.object(mesh_check, "_submit_ztunnel_env_literal", return_value=(True, "submitted")), mock.patch.object(
            mesh_check, "_wait_ztunnel_rollout", return_value=(True, "ok")
        ), mock.patch.object(
            mesh_check, "_ztunnel_logs_since", return_value=""
        ), mock.patch.object(
            mesh_check, "_restore_ztunnel_log_env"
        ) as restore_mock:
            mesh_check.check_authorization_denial_isolated()
        restore_mock.assert_called_once_with(original)

    def test_restoration_never_attempted_when_submission_itself_fails(self):
        """The safe counterpart: if the mutation was never even accepted
        by the API, nothing changed, so restoration must NOT be
        invoked."""
        original = mesh_check.ZtunnelLogEnvState(True, False, None, False, None)
        with mock.patch.object(mesh_check, "_ztunnel_log_env_state", return_value=original), mock.patch.object(
            netpol, "_run_probe", return_value=netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "x")
        ), mock.patch.object(mesh_check, "_ztunnel_logs_since", return_value=""), mock.patch.object(
            mesh_check, "_submit_ztunnel_env_literal", return_value=(False, "kubectl set env failed")
        ), mock.patch.object(
            mesh_check, "_restore_ztunnel_log_env"
        ) as restore_mock:
            mesh_check.check_authorization_denial_isolated()
        restore_mock.assert_not_called()

    def test_refuses_to_mutate_when_original_uses_value_from(self):
        """Fail BEFORE mutation when the original entry cannot be
        restored exactly via kubectl set env - never mutate first and
        hope."""
        original = mesh_check.ZtunnelLogEnvState(True, True, None, True, {"name": "RUST_LOG", "valueFrom": {}})
        with mock.patch.object(mesh_check, "_ztunnel_log_env_state", return_value=original), mock.patch.object(
            netpol, "_run_probe", return_value=netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "x")
        ), mock.patch.object(mesh_check, "_ztunnel_logs_since", return_value=""), mock.patch.object(
            mesh_check, "_submit_ztunnel_env_literal"
        ) as submit_mock, mock.patch.object(
            mesh_check, "_restore_ztunnel_log_env"
        ) as restore_mock:
            mesh_check.check_authorization_denial_isolated()
        submit_mock.assert_not_called()
        restore_mock.assert_not_called()
        ok, message = mesh_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("valueFrom", message)

    def test_skips_mutation_entirely_when_default_logs_already_sufficient(self):
        """DAY6 fourth remediation item 3's stated preference: if
        default (unmutated) logs already carry transport evidence for
        every target, RUST_LOG must never be touched at all."""
        default_logs = "\n".join(
            f'access connection complete src.identity="{mesh_check.MESH_PROBE_SPIFFE_IDENTITY}" dst.service="{dest}.maops-platform.svc.cluster.local" dst.hbone_addr="10.0.0.1:15008" direction=INBOUND'
            for _fqdn, _name, dest in mesh_check._TARGETS
        )
        with mock.patch.object(
            netpol, "_run_probe", return_value=netpol.ProbeResult(netpol.TCP_CONNECT_TIMEOUT, "x")
        ), mock.patch.object(mesh_check, "_ztunnel_logs_since", return_value=default_logs), mock.patch.object(
            mesh_check, "_submit_ztunnel_env_literal"
        ) as submit_mock, mock.patch.object(
            mesh_check, "_restore_ztunnel_log_env"
        ) as restore_mock:
            mesh_check.check_authorization_denial_isolated()
        submit_mock.assert_not_called()
        restore_mock.assert_not_called()
        self.assertTrue(any("mutation was not needed" in message for _ok, message in mesh_check.results))


class ParseAccessLogFieldsTests(unittest.TestCase):
    """DAY6 fourth remediation item 3: `_parse_access_log_fields()`
    extracts structured key=value/key="value" pairs, the documented
    ztunnel access-log shape, rather than free-text marker matching."""

    def test_parses_quoted_and_unquoted_fields(self):
        line = 'access connection complete src.identity="spiffe://cluster.local/ns/ns/sa/sa" dst.service="maops-gateway.maops-platform.svc.cluster.local" direction=INBOUND bytes_sent=128'
        fields = mesh_check._parse_access_log_fields(line)
        self.assertEqual(fields["src.identity"], "spiffe://cluster.local/ns/ns/sa/sa")
        self.assertEqual(fields["dst.service"], "maops-gateway.maops-platform.svc.cluster.local")
        self.assertEqual(fields["direction"], "INBOUND")
        self.assertEqual(fields["bytes_sent"], "128")

    def test_parses_hbone_addr_field(self):
        line = 'access connection complete dst.hbone_addr="10.244.0.5:15008"'
        fields = mesh_check._parse_access_log_fields(line)
        self.assertEqual(fields["dst.hbone_addr"], "10.244.0.5:15008")

    def test_empty_line_yields_no_fields(self):
        self.assertEqual(mesh_check._parse_access_log_fields(""), {})

    def test_unstructured_line_yields_no_matching_fields(self):
        fields = mesh_check._parse_access_log_fields("this is not a structured log line at all")
        self.assertNotIn("src.identity", fields)


class FindTransportEvidenceTests(unittest.TestCase):
    """DAY6 fourth remediation item 3: transport proof requires the
    SPIFFE form of the identity, the documented `access`/`connection
    complete` markers, and a destination match via dst.service or
    dst.hbone_addr - never a free-text marker like 'mtls'/'established'."""

    def setUp(self):
        self.spiffe = mesh_check.MESH_PROBE_SPIFFE_IDENTITY

    def test_finds_matching_line(self):
        logs = (
            "some unrelated line\n"
            f'access connection complete src.identity="{self.spiffe}" dst.service="maops-gateway.maops-platform.svc.cluster.local" direction=INBOUND\n'
        )
        fields = mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway")
        self.assertIsNotNone(fields)
        self.assertEqual(fields["src.identity"], self.spiffe)

    def test_matches_via_hbone_addr_when_service_absent(self):
        logs = f'access connection complete src.identity="{self.spiffe}" dst.hbone_addr="maops-app.internal:15008"\n'
        fields = mesh_check._find_transport_evidence(logs, self.spiffe, "maops-app")
        self.assertIsNotNone(fields)

    def test_requires_access_marker(self):
        logs = f'connection complete src.identity="{self.spiffe}" dst.service="maops-gateway"\n'
        self.assertIsNone(mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway"))

    def test_requires_connection_complete_marker(self):
        logs = f'access src.identity="{self.spiffe}" dst.service="maops-gateway"\n'
        self.assertIsNone(mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway"))

    def test_requires_exact_spiffe_identity_match(self):
        logs = 'access connection complete src.identity="spiffe://cluster.local/ns/other/sa/other" dst.service="maops-gateway"\n'
        self.assertIsNone(mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway"))

    def test_requires_destination_match(self):
        logs = f'access connection complete src.identity="{self.spiffe}" dst.service="maops-unrelated"\n'
        self.assertIsNone(mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway"))

    def test_authorizationpolicy_form_never_matches_transport_evidence(self):
        """The transport check must key on the SPIFFE form specifically
        - the bare AuthorizationPolicy-principal form never appears in
        ztunnel's own logs and must not be accepted as a substitute."""
        principal_form = mesh_check.MESH_PROBE_PRINCIPAL
        logs = f'access connection complete src.identity="{principal_form}" dst.service="maops-gateway"\n'
        self.assertIsNone(mesh_check._find_transport_evidence(logs, self.spiffe, "maops-gateway"))


class FindDenialEvidenceTests(unittest.TestCase):
    """DAY6 fourth remediation item 3, extended by the fifth remediation
    item 5: `_find_denial_evidence()` looks for AUTHORITATIVE first (the
    two exact Istio 1.31 denial error strings this project has now
    observed live, correlated), then a structured CANDIDATE, then an
    explicitly-labeled BEST_EFFORT free-text fallback, and otherwise
    reports NONE (INCONCLUSIVE), never a confident 'not denied'."""

    def setUp(self):
        self.spiffe = mesh_check.MESH_PROBE_SPIFFE_IDENTITY
        self.wrong_spiffe = "spiffe://cluster.local/ns/other-namespace/sa/other-identity"

    def _line(self, *, src_identity=None, dst_service="maops-state.maops-platform.svc.cluster.local", error=None, bytes_sent=None, bytes_recv=None, connection_complete=False):
        src_identity = self.spiffe if src_identity is None else src_identity
        parts = ["access"]
        if connection_complete:
            parts.append("connection complete")
        parts.append(f'src.identity="{src_identity}"')
        parts.append(f'dst.service="{dst_service}"')
        parts.append('dst.hbone_addr="10.244.1.5:15008"')
        parts.append("direction=OUTBOUND")
        if bytes_sent is not None:
            parts.append(f"bytes_sent={bytes_sent}")
        if bytes_recv is not None:
            parts.append(f"bytes_recv={bytes_recv}")
        if error is not None:
            parts.append(f'error="{error}"')
        return " ".join(parts) + "\n"

    def test_candidate_when_identity_and_destination_correlated_with_an_unrecognized_error(self):
        """DAY6 fifth remediation: CANDIDATE is now keyed on the
        PRESENCE of a non-empty `error=` field that is not one of the
        two recognized exact strings - never on the absence of
        `connection complete` (a live run proved that marker appears on
        BOTH allowed and rejected connections)."""
        logs = f'access connection complete src.identity="{self.spiffe}" dst.service="maops-state.maops-platform.svc.cluster.local" direction=OUTBOUND error="some other unrecognized rejection reason"\n'
        kind, evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "CANDIDATE")
        self.assertIsInstance(evidence, dict)

    def test_connection_complete_line_with_no_error_field_is_not_denial_evidence(self):
        """DAY6 fifth remediation correction: `connection complete` with
        NO `error=` field is a normal/allowed record - ztunnel emits
        `connection complete` for rejected connections too (carrying an
        `error=` field), so its mere presence must never be read as
        proof of either outcome. Only the error field's presence/content
        decides denial evidence."""
        logs = f'access connection complete src.identity="{self.spiffe}" dst.service="maops-state"\n'
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "NONE")

    def test_best_effort_fallback_when_no_structured_candidate(self):
        logs = f"unstructured line mentioning {self.spiffe} and maops-state: RBAC DENIED\n"
        kind, evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "BEST_EFFORT")
        self.assertIn("DENIED", evidence)

    def test_none_when_nothing_correlates(self):
        logs = "totally unrelated log output\n"
        kind, evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "NONE")
        self.assertIsNone(evidence)

    def test_none_is_never_silently_upgraded_by_record_log_correlation(self):
        mesh_check.results = []
        logs = "totally unrelated log output\n"
        mesh_check._record_log_correlation(logs, "state", "maops-state")
        deny_findings = [message for _ok, message in mesh_check.results if "INCONCLUSIVE" in message]
        self.assertTrue(deny_findings)

    def test_explicit_policy_rejection_error_alone_is_authoritative(self):
        """DAY6 fifth remediation item 5: the exact live-observed
        structured policy-rejection error, correlated by identity and
        destination, is authoritative on its own - no bytes_sent/
        bytes_recv condition required for this form."""
        logs = self._line(error=mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        kind, evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "AUTHORITATIVE")
        self.assertEqual(evidence["error"], mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)

    def test_401_error_with_zero_bytes_is_authoritative(self):
        """DAY6 fifth remediation item 5: the exact live-observed HBONE
        401 error is authoritative ONLY when correlated AND
        bytes_sent=0/bytes_recv=0 - proving no application data was ever
        exchanged."""
        logs = self._line(error=mesh_check.AUTHORITATIVE_HBONE_401_ERROR, bytes_sent=0, bytes_recv=0)
        kind, evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "AUTHORITATIVE")
        self.assertEqual(evidence["error"], mesh_check.AUTHORITATIVE_HBONE_401_ERROR)

    def test_401_error_with_nonzero_application_bytes_does_not_qualify_as_authoritative(self):
        """Item 6's explicit regression case: nonzero application bytes
        must never qualify for the structured 401 authoritative form -
        this is a materially different (and unverified) situation."""
        logs = self._line(error=mesh_check.AUTHORITATIVE_HBONE_401_ERROR, bytes_sent=128, bytes_recv=0)
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertNotEqual(kind, "AUTHORITATIVE")
        self.assertEqual(kind, "CANDIDATE")

    def test_401_error_with_nonzero_bytes_recv_also_does_not_qualify(self):
        logs = self._line(error=mesh_check.AUTHORITATIVE_HBONE_401_ERROR, bytes_sent=0, bytes_recv=64)
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertNotEqual(kind, "AUTHORITATIVE")

    def test_401_error_without_bytes_fields_at_all_does_not_qualify(self):
        """Missing bytes_sent/bytes_recv fields entirely must not be
        treated as satisfying the zero-bytes requirement."""
        logs = self._line(error=mesh_check.AUTHORITATIVE_HBONE_401_ERROR)
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertNotEqual(kind, "AUTHORITATIVE")

    def test_wrong_source_identity_does_not_match(self):
        """A line carrying the AUTHORITATIVE error text but the WRONG
        src.identity must never be treated as evidence for OUR probe's
        identity."""
        logs = self._line(src_identity=self.wrong_spiffe, error=mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "NONE")

    def test_wrong_destination_does_not_match(self):
        """A line with the right identity and the AUTHORITATIVE error
        text but a destination that does not match our target must not
        be treated as evidence for that target."""
        logs = self._line(dst_service="maops-app.maops-platform.svc.cluster.local", error=mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertEqual(kind, "NONE")

    def test_generic_uncorrelated_401_string_is_never_authoritative(self):
        """An uncorrelated generic '401'/'denied' substring anywhere in
        the logs must never be promoted past BEST_EFFORT."""
        logs = f"some log line mentions 401 and denied near {self.spiffe} and maops-state but is not field-structured\n"
        kind, _evidence = mesh_check._find_denial_evidence(logs, self.spiffe, "maops-state")
        self.assertNotEqual(kind, "AUTHORITATIVE")

    def test_record_log_correlation_marks_authoritative_as_a_confirmed_pass(self):
        mesh_check.results = []
        logs = self._line(error=mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        mesh_check._record_log_correlation(logs, "state", "maops-state")
        authoritative_findings = [message for ok, message in mesh_check.results if ok and "AUTHORITATIVE" in message]
        self.assertTrue(authoritative_findings)


class ClientTcpPlusLogCorrelationEndToEndTests(unittest.TestCase):
    """DAY6 fifth remediation item 6: the exact combined scenarios a
    live run actually produced - TCP_CONNECTED on the client-side raw
    probe (LOCAL_ZTUNNEL_CONNECT_ACCEPTED) together with correlated
    ztunnel log evidence. Exercises `_record_client_side_supporting_evidence()`
    and `_record_log_correlation()` together, over the exact live Istio
    1.31 log shapes observed, to prove the overall combination behaves
    as designed: the client-side TCP result never gates by itself, and
    the identity-denial assertion is satisfied (or not) purely by the
    log correlation outcome."""

    def setUp(self):
        mesh_check.results = []
        self.spiffe = mesh_check.MESH_PROBE_SPIFFE_IDENTITY

    def _authoritative_line(self, error: str, **extra) -> str:
        """DAY6 fifth remediation live finding: ztunnel emits `access
        connection complete` for REJECTED connections too (carrying an
        `error=` field) - not just allowed ones. Including it here
        matches the real, combined live shape: the SAME log line
        satisfies both `_find_transport_evidence()` (proof of local
        ztunnel activity) and `_find_denial_evidence()` (proof of the
        rejection), which is exactly why the live run's default
        (unmutated) logs already contained sufficient evidence without
        ever needing the RUST_LOG escalation."""
        parts = [
            "access",
            "connection complete",
            f'src.identity="{self.spiffe}"',
            'dst.service="maops-state.maops-platform.svc.cluster.local"',
            'dst.hbone_addr="10.244.1.5:15008"',
            "direction=OUTBOUND",
        ]
        for key, value in extra.items():
            parts.append(f"{key}={value}")
        parts.append(f'error="{error}"')
        return " ".join(parts) + "\n"

    def test_tcp_connected_plus_correlated_401_denial_passes(self):
        tcp_result = netpol.ProbeResult(netpol.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
        mesh_check._record_client_side_supporting_evidence(tcp_result, "wrong-identity -> state client-side TCP outcome")
        logs = self._authoritative_line(mesh_check.AUTHORITATIVE_HBONE_401_ERROR, bytes_sent=0, bytes_recv=0)
        mesh_check._record_log_correlation(logs, "state", "maops-state")
        self.assertTrue(all(ok for ok, _msg in mesh_check.results))
        self.assertTrue(any("AUTHORITATIVE" in msg for ok, msg in mesh_check.results if ok))

    def test_tcp_connected_plus_correlated_explicit_policy_rejection_passes(self):
        tcp_result = netpol.ProbeResult(netpol.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
        mesh_check._record_client_side_supporting_evidence(tcp_result, "wrong-identity -> state client-side TCP outcome")
        logs = self._authoritative_line(mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        mesh_check._record_log_correlation(logs, "state", "maops-state")
        self.assertTrue(all(ok for ok, _msg in mesh_check.results))
        self.assertTrue(any("AUTHORITATIVE" in msg for ok, msg in mesh_check.results if ok))

    def test_tcp_connected_without_correlated_denial_fails_inconclusive(self):
        """The client-side TCP result alone (LOCAL_ZTUNNEL_CONNECT_ACCEPTED)
        passes as supporting evidence, but with NO correlated log
        evidence at all, the overall combination must still surface a
        failure - the identity-denial assertion is not satisfied."""
        tcp_result = netpol.ProbeResult(netpol.TCP_CONNECTED, "RESULT=TCP_CONNECTED")
        mesh_check._record_client_side_supporting_evidence(tcp_result, "wrong-identity -> state client-side TCP outcome")
        mesh_check._record_log_correlation("totally unrelated log output\n", "state", "maops-state")
        self.assertTrue(any(not ok for ok, _msg in mesh_check.results))
        self.assertTrue(any("INCONCLUSIVE" in msg for ok, msg in mesh_check.results if not ok))

    def test_http_livez_200_hard_fails_even_if_an_unrelated_denial_log_exists(self):
        """DAY6 fifth remediation item 6: an HTTP 200 leak on /livez
        must hard-fail regardless of whatever else the log correlation
        finds - even a genuinely correlated AUTHORITATIVE denial for
        this exact target must not paper over a leaked application
        response."""
        http_result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=200")
        mesh_check._record_client_side_http_leak_check(http_result, "wrong-identity -> state client-side HTTP /livez outcome")
        logs = self._authoritative_line(mesh_check.AUTHORITATIVE_POLICY_REJECTION_ERROR)
        mesh_check._record_log_correlation(logs, "state", "maops-state")
        self.assertTrue(any(not ok for ok, _msg in mesh_check.results))
        self.assertTrue(any("reached the application" in msg for ok, msg in mesh_check.results if not ok))

    def test_http_401_alone_does_not_satisfy_denial_without_correlated_ztunnel_fields(self):
        """The client-side HTTP 401 by itself (no log correlation at
        all) must never be sufficient to claim the identity-denial
        assertion passed - only the log-correlation findings may."""
        http_result = netpol.ProbeResult(netpol.HTTP_RESPONSE, "RESULT=HTTP_RESPONSE STATUS=401")
        mesh_check._record_client_side_http_leak_check(http_result, "wrong-identity -> state client-side HTTP /livez outcome")
        ok, message = mesh_check.results[-1]
        self.assertTrue(ok)
        self.assertNotIn("AUTHORITATIVE", message)
        self.assertIn("never by itself proves AuthorizationPolicy denial", message)


if __name__ == "__main__":
    unittest.main()

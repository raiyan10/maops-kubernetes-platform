"""
Docker/Kubernetes-free unit tests for scripts/state_check.py.

DAY4-TEST-H2 (state_check coverage) and the suite-level baseline
capture design (§C): proves `capture_suite_baseline()` - the writer
half of scripts/suite_baseline.py's contract - correctly skips
non-fatally when standalone (no DAY5_RUN_ID/DAY5_SUITE_BASELINE_PATH),
and correctly gates on valid identity preconditions AND a valid
authenticated GET before ever writing the baseline file.

DAY4 batch 2b: `capture_suite_baseline()` now also captures/verifies
the namespace UID (Part C) and refuses to capture at all if any of
namespace/PVC/PV UID is missing/empty (Part C's "matching missing UIDs
is not proof of identity preservation" - applied here as "a missing
UID must abort capture, not be silently written").
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import state_check
import suite_baseline


class _FakePortForward:
    def __call__(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return 54321

    def __exit__(self, *_exc):
        return False


def _fake_run_namespace_uid(namespace_uid="ns-uid-1"):
    """Returns a `mock.patch.object(state_check, "run", ...)` context
    manager whose fake `run()` answers ONLY the `get namespace ... -o
    json` call `_namespace_uid()` makes - matching this module's own
    real call shape."""

    def fake_run(*args, **_kwargs):
        if "namespace" in args and "get" in args:
            if namespace_uid is None:
                return subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr='Error from server (NotFound): namespaces "maops-platform" not found')
            return subprocess.CompletedProcess(
                args=["kubectl"], returncode=0, stdout=f'{{"metadata": {{"uid": "{namespace_uid}"}}}}', stderr=""
            )
        raise AssertionError(f"unexpected run() call: {args}")

    return mock.patch.object(state_check, "run", side_effect=fake_run)


class CaptureSuiteBaselineTests(unittest.TestCase):
    def setUp(self):
        state_check.results = []

    def test_standalone_invocation_skips_capture_non_fatally(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=(None, None)):
            with mock.patch.object(state_check, "port_forward") as mock_pf:
                state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_pf.assert_not_called()
        # A skip is not a recorded failure - state-check's other checks
        # remain fully meaningful standalone.
        self.assertEqual(state_check.results, [])

    def test_missing_namespace_uid_aborts_before_any_http_call(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid(namespace_uid=None):
                with mock.patch.object(state_check, "port_forward") as mock_pf:
                    state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_pf.assert_not_called()
        self.assertTrue(any(not ok and "identity preconditions" in msg for ok, msg in state_check.results))

    def test_missing_pvc_uid_aborts_before_any_http_call(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward") as mock_pf:
                    state_check.capture_suite_baseline(None, "pv-uid-1")
        mock_pf.assert_not_called()
        self.assertTrue(any(not ok and "identity preconditions" in msg for ok, msg in state_check.results))

    def test_empty_pv_uid_aborts_before_any_http_call(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward") as mock_pf:
                    state_check.capture_suite_baseline("pvc-uid-1", "")
        mock_pf.assert_not_called()
        self.assertTrue(any(not ok and "identity preconditions" in msg for ok, msg in state_check.results))

    def test_non_200_get_is_recorded_false_and_never_writes_baseline(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(503, "{}")):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_not_called()
        self.assertTrue(any(not ok for ok, _msg in state_check.results))

    def test_malformed_body_is_recorded_false_and_never_writes_baseline(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, "not json")):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_not_called()
        self.assertTrue(any(not ok for ok, _msg in state_check.results))

    def test_missing_value_key_is_recorded_false_and_never_writes_baseline(self):
        """DAY4 batch 2b (Part C): HTTP 200 + `{}` (missing 'value' key)
        must fail visibly - never silently accepted."""
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, '{"oops": true}')):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_not_called()
        self.assertTrue(any(not ok for ok, _msg in state_check.results))

    def test_invalid_value_type_is_recorded_false_and_never_writes_baseline(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/does-not-matter.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, '{"value": 12345}')):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_not_called()
        self.assertTrue(any(not ok for ok, _msg in state_check.results))

    def test_valid_null_value_is_captured(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/baseline.json")):
            with _fake_run_namespace_uid(namespace_uid="ns-uid-1"):
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, '{"value": null}')):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_called_once_with(
            "/tmp/baseline.json", "run-1", state_check.CONTEXT, state_check.NAMESPACE, "ns-uid-1", "pvc-uid-1", "pv-uid-1", None
        )
        self.assertTrue(all(ok for ok, _msg in state_check.results))

    def test_valid_non_null_value_is_captured(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/baseline.json")):
            with _fake_run_namespace_uid(namespace_uid="ns-uid-1"):
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, '{"value": "arbitrary-record"}')):
                        with mock.patch.object(suite_baseline, "capture") as mock_capture:
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        mock_capture.assert_called_once_with(
            "/tmp/baseline.json", "run-1", state_check.CONTEXT, state_check.NAMESPACE, "ns-uid-1", "pvc-uid-1", "pv-uid-1", "arbitrary-record"
        )
        self.assertTrue(all(ok for ok, _msg in state_check.results))

    def test_refuse_to_overwrite_existing_baseline_is_recorded_false(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/baseline.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(state_check, "raw_get", return_value=(200, '{"value": "x"}')):
                        with mock.patch.object(
                            suite_baseline, "capture",
                            side_effect=suite_baseline.SuiteBaselineError("suite baseline already exists"),
                        ):
                            state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        self.assertTrue(any(not ok and "already exists" in msg for ok, msg in state_check.results))

    def test_port_forward_failure_is_recorded_false(self):
        with mock.patch.object(suite_baseline, "env_configured", return_value=("run-1", "/tmp/baseline.json")):
            with _fake_run_namespace_uid():
                with mock.patch.object(state_check, "port_forward", side_effect=TimeoutError("no connection")):
                    state_check.capture_suite_baseline("pvc-uid-1", "pv-uid-1")
        self.assertTrue(any(not ok for ok, _msg in state_check.results))


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        state_check.results = []

    def test_verify_context_failure_short_circuits_before_any_checks(self):
        with mock.patch.object(state_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(state_check, "wait_for_statefulset_ready") as mock_wait:
                exit_code = state_check.main()
        self.assertEqual(exit_code, 1)
        mock_wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()

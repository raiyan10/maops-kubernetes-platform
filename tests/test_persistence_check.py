"""
Docker/Kubernetes-free unit tests for scripts/persistence_check.py.

DAY4-TEST-H2 / DAY4-REL-4: proves the actual production control flow in
main()/the finally-block restoration - not a re-implementation of it -
by mocking only the external collaborator boundaries this script
itself uses to reach the cluster/HTTP chain (`run`,
`portforward.port_forward`, and `_http`, the one HTTP primitive every
call in this file goes through). Every test below calls the real
`persistence_check.main()`.

A fake, manually-advanced clock (mirroring tests/test_kube.py's
`_FakeClock`/`_patched_clock`) replaces the stdlib `time` module's
`monotonic`/`sleep` for the whole call - both scripts/kube.py's
`wait_until()` and this script's own retry loops use plain
`import time`, so patching the shared `time` module's attributes
covers both without real wall-clock waits.

Covers, per the batch 2 briefing:
- Baseline-capture failure (non-200, malformed body, transport
  exception) aborts BEFORE any mutation - no marker PUT, no Pod
  delete - and returns nonzero.
- A genuine `{"value": null}` baseline is captured, not confused with
  a failed capture.
- A restoring PUT followed by an independent GET mismatch is a
  distinct, recorded RESTORATION FAILURE with nonzero exit - a
  successful PUT status alone is never treated as proof.
- The full happy path restores the true original value and exits 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import persistence_check


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakePortForward:
    def __call__(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return 54321

    def __exit__(self, *_exc):
        return False


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _patched_clock(clock: _FakeClock):
    return mock.patch.multiple("time", monotonic=clock.monotonic, sleep=clock.sleep)


def _pod_json(uid: str, ready: bool = True) -> str:
    return json.dumps(
        {
            "metadata": {"name": persistence_check.POD_NAME, "uid": uid},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }
    )


class _StateHarness:
    """Simulates the maops-state PVC/Pod identity and the gateway ->
    app -> state HTTP chain far enough to exercise persistence_check's
    real control flow end to end, without a live cluster."""

    def __init__(self, initial_value=None):
        self.value = initial_value
        self.pod_uid_before = "pod-uid-1"
        self.pod_uid_after = "pod-uid-2"
        self.deleted = False
        self.readyz_ok = True
        self.restore_put_status = 200
        self.restore_get_override = "__unset__"  # sentinel: use self.value if unset
        self.restore_put_attempted = False
        self._marker_put_done = False  # DAY4 batch 2b: dispatch PUT by call order, not by
        # `self.deleted` - a mutation attempt (e.g. interrupted by KeyboardInterrupt) may
        # commit server-side before the Pod is ever actually deleted.

    def run(self, *args, **_kwargs):
        if "pod" in args and "get" in args:
            uid = self.pod_uid_after if self.deleted else self.pod_uid_before
            return _completed(stdout=_pod_json(uid, ready=True))
        if "pvc" in args and "get" in args:
            return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "spec": {"volumeName": "pvc-vol-1"}}))
        if "pv" in args and "get" in args:
            return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}}))
        if "delete" in args:
            self.deleted = True
            return _completed(returncode=0)
        raise AssertionError(f"unexpected run() call: {args}")

    def http(self, _port, method, path, body=None):
        if path == "/readyz":
            return (200, {"status": "ready"}) if self.readyz_ok else (503, {"status": "not ready"})
        if path != "/state":
            raise AssertionError(f"unexpected path {path}")
        if method == "GET":
            if self.restore_put_attempted and self.restore_get_override != "__unset__":
                return 200, {"value": self.restore_get_override}
            return 200, {"value": self.value}
        if method == "PUT":
            if self._marker_put_done:
                # The SECOND PUT this harness ever sees - the restoring
                # PUT - regardless of whether the Pod was actually
                # deleted first (a mutation may be attempted and
                # committed even if a later step never ran).
                self.restore_put_attempted = True
                if self.restore_put_status == 200:
                    self.value = body["value"]
                return self.restore_put_status, ({"status": "ok"} if self.restore_put_status == 200 else {"error": "boom"})
            self._marker_put_done = True
            self.value = body["value"]
            return 200, {"status": "ok"}
        raise AssertionError(f"unexpected method {method}")


class _NoVerifyContextMixin:
    def setUp(self):
        super().setUp()
        persistence_check.results = []
        persistence_check.restoration_results = []
        patcher = mock.patch.object(persistence_check.kube, "verify_context")
        self.addCleanup(patcher.stop)
        patcher.start()


class BaselineCaptureAbortsBeforeMutationTests(_NoVerifyContextMixin, unittest.TestCase):
    def test_non_200_baseline_aborts_before_any_mutation(self):
        harness = _StateHarness(initial_value="real-value")
        http_calls = []

        def fake_http(*args, **kwargs):
            http_calls.append((args[1], args[2]))
            return 503, {"error": "state unavailable"}

        with mock.patch.object(persistence_check, "run", side_effect=harness.run):
            with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                with mock.patch.object(persistence_check, "_http", side_effect=fake_http):
                    exit_code = persistence_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(http_calls, [("GET", "/state")])
        messages = " ".join(msg for _, msg in persistence_check.results)
        self.assertIn("aborting before any mutation", messages)

    def test_malformed_baseline_body_aborts_before_any_mutation(self):
        http_calls = []

        def fake_http(*args, **kwargs):
            http_calls.append((args[1], args[2]))
            return 200, {"unexpected": "shape"}

        with mock.patch.object(persistence_check, "run", side_effect=_StateHarness().run):
            with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                with mock.patch.object(persistence_check, "_http", side_effect=fake_http):
                    exit_code = persistence_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(http_calls), 1)

    def test_transport_exception_during_baseline_aborts_before_any_mutation(self):
        http_calls = []

        def fake_http(*args, **kwargs):
            http_calls.append((args[1], args[2]))
            raise TimeoutError("connection timed out")

        with mock.patch.object(persistence_check, "run", side_effect=_StateHarness().run):
            with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                with mock.patch.object(persistence_check, "_http", side_effect=fake_http):
                    exit_code = persistence_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(http_calls), 1)

    def test_genuine_null_baseline_is_not_confused_with_capture_failure(self):
        harness = _StateHarness(initial_value=None)
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(persistence_check, "run", side_effect=harness.run):
                with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(persistence_check, "_http", side_effect=harness.http):
                        persistence_check.main()

        messages = " ".join(msg for _, msg in persistence_check.results)
        self.assertIn("baseline GET /state through gateway -> app -> state chain captured for restoration", messages)
        self.assertNotIn("capture failed", messages)


class RestorationIndependentVerificationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4-REL-4: a successful restoring PUT response alone must never
    be treated as proof of restoration - only an independent, matching
    GET does."""

    def _run_full_cycle(self, harness: _StateHarness) -> int:
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(persistence_check, "run", side_effect=harness.run):
                with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(persistence_check, "_http", side_effect=harness.http):
                        return persistence_check.main()

    def test_put_success_but_get_mismatch_is_a_restoration_failure(self):
        harness = _StateHarness(initial_value="the-real-value")
        harness.restore_put_status = 200
        harness.restore_get_override = "something-else-entirely"

        exit_code = self._run_full_cycle(harness)

        self.assertEqual(exit_code, 1)
        restoration_messages = " ".join(msg for _, msg in persistence_check.restoration_results)
        self.assertIn("independent GET /state after restoration", restoration_messages)
        self.assertTrue(
            any(not ok for ok, _ in persistence_check.restoration_results),
            f"expected a recorded restoration failure, got {persistence_check.restoration_results}",
        )

    def test_restoring_put_failure_is_a_distinct_restoration_failure(self):
        harness = _StateHarness(initial_value="the-real-value")
        harness.restore_put_status = 500

        exit_code = self._run_full_cycle(harness)

        self.assertEqual(exit_code, 1)
        restoration_messages = " ".join(msg for _, msg in persistence_check.restoration_results)
        self.assertIn("restoring PUT /state through service chain returned HTTP 500", restoration_messages)

    def test_full_happy_path_restores_and_verifies_independently(self):
        harness = _StateHarness(initial_value="the-real-value")

        exit_code = self._run_full_cycle(harness)

        self.assertEqual(exit_code, 0)
        self.assertFalse(
            any(not ok for ok, _ in persistence_check.restoration_results),
            f"expected no restoration failures, got {persistence_check.restoration_results}",
        )
        self.assertEqual(harness.value, "the-real-value")


class NeverRestoreWithoutBaselineTests(_NoVerifyContextMixin, unittest.TestCase):
    def test_no_put_is_ever_issued_when_baseline_capture_fails(self):
        puts = []

        def fake_http(*args, **kwargs):
            method, path = args[1], args[2]
            if method == "PUT":
                puts.append(args[3] if len(args) > 3 else kwargs.get("body"))
            return 500, {"error": "boom"}

        with mock.patch.object(persistence_check, "run", side_effect=_StateHarness().run):
            with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                with mock.patch.object(persistence_check, "_http", side_effect=fake_http):
                    persistence_check.main()

        self.assertEqual(puts, [])


class IdentityPreconditionAbortsBeforeMutationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b (Part C): a missing/empty pod/PVC/PV identity must
    abort BEFORE any mutation - previously only recorded, never
    gated."""

    def test_missing_pvc_uid_aborts_before_marker_write(self):
        def fake_run(*args, **_kwargs):
            if "pod" in args and "get" in args:
                return _completed(stdout=_pod_json("pod-uid-1"))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {}, "spec": {"volumeName": "pvc-vol-1"}}))  # no uid
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}}))
            raise AssertionError(f"unexpected run() call after identity abort: {args}")

        with mock.patch.object(persistence_check, "run", side_effect=fake_run):
            with mock.patch.object(persistence_check, "_put_state") as mock_put:
                exit_code = persistence_check.main()

        self.assertEqual(exit_code, 1)
        mock_put.assert_not_called()
        self.assertTrue(any(not ok and "identity captured" in msg for ok, msg in persistence_check.results))


class MalformedResponseAfterCommittedWriteTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b (Part B, mirrored here for persistence_check.py):
    `restore_needed` must be set BEFORE attempting the marker PUT, not
    only inside its except/else branches - a KeyboardInterrupt or any
    other unanticipated exception during the write must still leave
    `restore_needed` True so restoration runs."""

    def test_keyboard_interrupt_during_marker_put_still_restores(self):
        harness = _StateHarness(initial_value="pre-existing-value")
        call_count = {"n": 0}
        real_http = harness.http

        def interrupting_http(port, method, path, body=None):
            if method == "PUT" and path == "/state" and not harness._marker_put_done:
                call_count["n"] += 1
                harness._marker_put_done = True
                harness.value = body["value"]  # server-side effect committed
                raise KeyboardInterrupt()
            return real_http(port, method, path, body)

        clock = _FakeClock()
        with self.assertRaises(KeyboardInterrupt):
            with _patched_clock(clock):
                with mock.patch.object(persistence_check, "run", side_effect=harness.run):
                    with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                        with mock.patch.object(persistence_check, "_http", side_effect=interrupting_http):
                            persistence_check.main()

        self.assertTrue(harness.restore_put_attempted)
        self.assertEqual(harness.value, "pre-existing-value")


class SchemaValidatedReadbackTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b (Part C): HTTP 200 + `{}` (missing 'value' key)
    must never be silently accepted as a match against a null
    baseline/marker - applied at every readback comparison in this
    file, not only at initial capture."""

    def test_marker_readback_missing_value_key_is_recorded_false(self):
        harness = _StateHarness(initial_value="real-value")
        original_http = harness.http
        get_calls = {"n": 0}

        def fake_http(port, method, path, body=None):
            if method == "GET" and path == "/state":
                get_calls["n"] += 1
                if get_calls["n"] == 2:
                    # The SECOND GET /state call - the marker-readback
                    # check right after the marker PUT, not the initial
                    # baseline capture (the first GET).
                    return 200, {}
            return original_http(port, method, path, body)

        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(persistence_check, "run", side_effect=harness.run):
                with mock.patch.object(persistence_check.portforward, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(persistence_check, "_http", side_effect=fake_http):
                        persistence_check.main()

        self.assertTrue(any(not ok and "readback" in msg for ok, msg in persistence_check.results))


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        persistence_check.results = []
        persistence_check.restoration_results = []

    def test_verify_context_failure_short_circuits_before_any_run_call(self):
        with mock.patch.object(persistence_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(persistence_check, "run") as mock_run:
                exit_code = persistence_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

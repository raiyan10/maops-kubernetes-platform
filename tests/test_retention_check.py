"""
Docker/Kubernetes-free unit tests for scripts/retention_check.py.

DAY4-TEST-H3 / DAY4-REL-1: proves the actual production control flow in
main()/restore_state() - not a re-implementation of it - by mocking
only the external collaborator boundaries (`run`, `get_pods`,
`port_forward`, `check_endpoint`, `raw_get`, and `_http`, the one HTTP
primitive every /state call in this file goes through). Every test
below calls the real `retention_check.main()`.

A fake, manually-advanced clock (mirroring tests/test_kube.py's
`_FakeClock`/`_patched_clock`) replaces the stdlib `time` module's
`monotonic`/`sleep` for the whole call - both scripts/kube.py's
`wait_until()` and this script's own retry loops use plain
`import time`, so patching the shared `time` module's attributes
covers both without real wall-clock waits.

Covers:
- Baseline-capture failure aborts BEFORE any mutation - no marker PUT,
  no scale-to-0.
- An arbitrary non-null original value is genuinely restored and
  independently re-verified via GET - not just a leftover test marker
  surviving because nothing tried to overwrite it (the exact defect
  DAY4-TEST-H3 identified).
- Contract A (marker survival) is verified and recorded BEFORE
  Contract B (true-value restoration) overwrites it.
- A restoring PUT success with a GET mismatch is a distinct restoration
  failure with nonzero exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import retention_check


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


class _RetentionHarness:
    """Simulates maops-state's PVC/Pod identity, app's readyReplicas,
    and the gateway -> app -> state HTTP chain far enough to exercise
    retention_check's real control flow end to end."""

    def __init__(self, initial_value=None):
        self.value = initial_value
        self.pod_uid_before = "pod-uid-1"
        self.pod_uid_after = "pod-uid-2"
        self.scaled_to = 1
        self.pod_exists = True
        self.ever_scaled_down = False
        self.marker_written = False
        self.restore_put_status = 200
        self.restore_get_override = "__unset__"
        self.restore_put_attempted = False
        # DAY4 batch 2b: fault-injection hooks for the new failure-path
        # regression tests below - all default to "no fault", i.e. the
        # unmodified harness behavior above.
        self.marker_put_exception: Exception | None = None
        self.scale_down_exception: Exception | None = None
        self.scale_down_server_effect = True
        self.contract_a_get_exception: Exception | None = None
        self._contract_a_get_consumed = False

    def run(self, *args, **_kwargs):
        if "scale" in args:
            replicas_arg = next(a for a in args if a.startswith("--replicas="))
            self.scaled_to = int(replicas_arg.split("=")[1])
            if self.scaled_to == 0 and self.scale_down_exception is not None:
                # DAY4 batch 2b: simulate an exception from the
                # scale-to-0 call itself - `scale_down_server_effect`
                # controls whether the server-side state ALSO actually
                # changed despite the client-side exception (a timeout
                # after acceptance) or not (an outright rejection).
                if self.scale_down_server_effect:
                    self.pod_exists = False
                    self.ever_scaled_down = True
                raise self.scale_down_exception
            if self.scaled_to == 0:
                self.pod_exists = False
                self.ever_scaled_down = True
            else:
                self.pod_exists = True
            return _completed(returncode=0)
        if "pod" in args and "get" in args and "--ignore-not-found" in args:
            # _state_pod_gone()'s structural absence check (DAY4 batch
            # 2c): exit 0 + EMPTY stdout means genuine absence; exit 0
            # + a JSON object means still present. Distinguished from
            # _state_pod_ready()'s plain "-o json" (no --ignore-not-found)
            # call below by the presence of this flag, not by "-o"'s
            # absence (both calls use -o json now).
            if self.pod_exists:
                uid = self.pod_uid_after if self.ever_scaled_down else self.pod_uid_before
                return _completed(stdout=json.dumps({"metadata": {"uid": uid}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}))
            return _completed(returncode=0, stdout="")
        if "pod" in args and "get" in args:
            if not self.pod_exists:
                return _completed(returncode=1)
            uid = self.pod_uid_after if self.ever_scaled_down else self.pod_uid_before
            return _completed(
                stdout=json.dumps(
                    {
                        "metadata": {"uid": uid},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                )
            )
        if "pvc" in args and "get" in args:
            return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-vol-1"}}))
        if "pv" in args and "get" in args:
            return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
        if "deployment" in args and "get" in args:
            return _completed(stdout=json.dumps({"status": {"readyReplicas": 3}}))
        raise AssertionError(f"unexpected run() call: {args}")

    def http(self, _port, method, path, body=None):
        if path != "/state":
            raise AssertionError(f"unexpected path {path}")
        if method == "GET":
            if (
                self.contract_a_get_exception is not None
                and self.marker_written
                and self.ever_scaled_down
                and not self._contract_a_get_consumed
            ):
                self._contract_a_get_consumed = True
                raise self.contract_a_get_exception
            if self.restore_put_attempted and self.restore_get_override != "__unset__":
                return 200, {"value": self.restore_get_override}
            return 200, {"value": self.value}
        if method == "PUT":
            if self.marker_written:
                # This is the Contract-B restoring PUT - the SECOND PUT
                # call this harness ever sees, regardless of whether
                # the outage cycle actually completed (DAY4 batch 2b:
                # restoration must still be attempted even when
                # scale-down was rejected/timed out - dispatching on
                # call order, not on `ever_scaled_down`, mirrors that).
                self.restore_put_attempted = True
                if self.restore_put_status == 200:
                    self.value = body["value"]
                return self.restore_put_status, ({"status": "ok"} if self.restore_put_status == 200 else {"error": "boom"})
            # This is the pre-outage marker PUT.
            self.marker_written = True
            self.value = body["value"]
            if self.marker_put_exception is not None:
                # DAY4 batch 2b: the write already committed above
                # (self.value updated) BEFORE this raises - simulating a
                # response the client could not parse/receive despite
                # the server-side effect already happening.
                raise self.marker_put_exception
            return 200, {"status": "ok"}
        raise AssertionError(f"unexpected method {method}")


def _fake_get_pods(_selector):
    return [{"metadata": {"name": "fake-pod-0"}}]


def _fake_check_endpoint(_port, _path, role="gateway"):
    return True, f"{_path}: ok"


def _fake_raw_get(_port, path, headers=None):
    if path == "/readyz":
        return 503, "{}"
    raise AssertionError(f"unexpected raw_get path {path}")


class _NoVerifyContextMixin:
    def setUp(self):
        super().setUp()
        retention_check.results = []
        retention_check.restoration_results = []
        patcher = mock.patch.object(retention_check.kube, "verify_context")
        self.addCleanup(patcher.stop)
        patcher.start()


class IdentityPreconditionAbortsBeforeMutationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b (Part C, DAY4-TEST-M5): a missing/empty PVC/PV/Pod
    identity must abort BEFORE any mutation - previously only
    recorded, never gated. Mirrors persistence_check.py's identical
    test class."""

    def test_missing_pvc_uid_aborts_before_marker_write(self):
        http_calls = []

        def fake_run(*args, **_kwargs):
            if "pod" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pod-uid-1"}}))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {}, "spec": {"volumeName": "pvc-vol-1"}}))  # no uid
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call after identity abort: {args}")

        def fake_http(*args, **_kwargs):
            http_calls.append(args)
            return 200, {"value": "should-never-be-reached"}

        with mock.patch.object(retention_check, "run", side_effect=fake_run):
            with mock.patch.object(retention_check, "port_forward") as mock_pf:
                with mock.patch.object(retention_check, "_http", side_effect=fake_http):
                    exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        mock_pf.assert_not_called()
        self.assertEqual(http_calls, [])
        self.assertTrue(any(not ok and "identity captured" in msg for ok, msg in retention_check.results))

    def test_missing_pod_uid_aborts_before_marker_write(self):
        def fake_run(*args, **_kwargs):
            if "pod" in args and "get" in args:
                return _completed(returncode=1, stderr='Error from server (NotFound): pods "maops-state-0" not found')
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-vol-1"}}))
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call after identity abort: {args}")

        with mock.patch.object(retention_check, "run", side_effect=fake_run):
            with mock.patch.object(retention_check, "port_forward") as mock_pf:
                exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        mock_pf.assert_not_called()

    def test_pvc_not_bound_aborts_before_marker_write(self):
        def fake_run(*args, **_kwargs):
            if "pod" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pod-uid-1"}}))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Pending"}, "spec": {"volumeName": "pvc-vol-1"}}))
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call after identity abort: {args}")

        with mock.patch.object(retention_check, "run", side_effect=fake_run):
            with mock.patch.object(retention_check, "port_forward") as mock_pf:
                exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        mock_pf.assert_not_called()


class BaselineCaptureAbortsBeforeMutationTests(_NoVerifyContextMixin, unittest.TestCase):
    def test_non_200_baseline_aborts_before_scale_to_zero(self):
        scale_calls = []

        def fake_run(*args, **_kwargs):
            if "scale" in args:
                scale_calls.append(args)
                return _completed(returncode=0)
            if "pod" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pod-uid-1"}}))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-vol-1"}}))
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call before abort: {args}")

        def fake_http(*_args, **_kwargs):
            return 503, {"error": "state unavailable"}

        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(retention_check, "run", side_effect=fake_run):
                with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(retention_check, "_http", side_effect=fake_http):
                        exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(scale_calls, [])
        messages = " ".join(msg for _, msg in retention_check.results)
        self.assertIn("aborting before any mutation", messages)

    def test_malformed_baseline_aborts_before_scale_to_zero(self):
        scale_calls = []

        def fake_run(*args, **_kwargs):
            if "scale" in args:
                scale_calls.append(args)
                return _completed(returncode=0)
            if "pod" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pod-uid-1"}}))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-vol-1"}}))
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call before abort: {args}")

        def fake_http(*_args, **_kwargs):
            return 200, {"no_value_key": True}

        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(retention_check, "run", side_effect=fake_run):
                with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(retention_check, "_http", side_effect=fake_http):
                        exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(scale_calls, [])


class ArbitraryOriginalValueRestorationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4-TEST-H3: the true pre-existing record (not merely this
    script's own freshly-written marker) must be genuinely restored and
    independently re-verified."""

    def _run_full_cycle(self, harness: _RetentionHarness) -> int:
        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(retention_check, "run", side_effect=harness.run):
                with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(retention_check, "_http", side_effect=harness.http):
                        with mock.patch.object(retention_check, "get_pods", side_effect=_fake_get_pods):
                            with mock.patch.object(retention_check, "check_endpoint", side_effect=_fake_check_endpoint):
                                with mock.patch.object(retention_check, "raw_get", side_effect=_fake_raw_get):
                                    return retention_check.main()

    def test_arbitrary_non_null_original_value_is_restored_and_verified(self):
        harness = _RetentionHarness(initial_value="an-arbitrary-pre-existing-record")
        exit_code = self._run_full_cycle(harness)

        self.assertEqual(exit_code, 0)
        self.assertEqual(harness.value, "an-arbitrary-pre-existing-record")
        restoration_messages = " ".join(msg for _, msg in retention_check.restoration_results)
        self.assertIn("independent GET /state after restoration matches captured original value", restoration_messages)
        self.assertFalse(any(not ok for ok, _ in retention_check.restoration_results))

    def test_contract_a_marker_check_is_recorded_before_contract_b_overwrites_it(self):
        harness = _RetentionHarness(initial_value="the-true-original")
        self._run_full_cycle(harness)

        all_messages = [msg for _, msg in retention_check.results]
        marker_check_index = next(i for i, m in enumerate(all_messages) if "Contract A" in m)
        # Contract A's message must exist and must be recorded via
        # record() (module `results`), proving it ran as part of the
        # guaranteed restoration path, before the true value overwrote it.
        self.assertIsNotNone(marker_check_index)
        restoration_messages = [msg for _, msg in retention_check.restoration_results]
        self.assertTrue(any("restoring PUT /state (true original record)" in m for m in restoration_messages))

    def test_put_success_but_get_mismatch_is_a_restoration_failure(self):
        harness = _RetentionHarness(initial_value="the-true-original")
        harness.restore_get_override = "not-the-original-value"

        exit_code = self._run_full_cycle(harness)

        self.assertEqual(exit_code, 1)
        self.assertTrue(
            any(not ok for ok, _ in retention_check.restoration_results),
            f"expected a recorded restoration failure, got {retention_check.restoration_results}",
        )


class NeverRestoreWithoutBaselineTests(_NoVerifyContextMixin, unittest.TestCase):
    def test_no_scale_to_zero_when_baseline_capture_fails(self):
        scale_calls = []

        def fake_run(*args, **_kwargs):
            if "scale" in args:
                scale_calls.append(args)
                return _completed(returncode=0)
            if "pod" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pod-uid-1"}}))
            if "pvc" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pvc-uid-1"}, "status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-vol-1"}}))
            if "pv" in args and "get" in args:
                return _completed(stdout=json.dumps({"metadata": {"uid": "pv-uid-1"}, "status": {"phase": "Bound"}}))
            raise AssertionError(f"unexpected run() call: {args}")

        def fake_http(*_args, **_kwargs):
            raise TimeoutError("connection timed out")

        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(retention_check, "run", side_effect=fake_run):
                with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(retention_check, "_http", side_effect=fake_http):
                        exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(scale_calls, [])


def _run_full_cycle(harness: "_RetentionHarness"):
    clock = _FakeClock()
    with _patched_clock(clock):
        with mock.patch.object(retention_check, "run", side_effect=harness.run):
            with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                with mock.patch.object(retention_check, "_http", side_effect=harness.http):
                    with mock.patch.object(retention_check, "get_pods", side_effect=_fake_get_pods):
                        with mock.patch.object(retention_check, "check_endpoint", side_effect=_fake_check_endpoint):
                            with mock.patch.object(retention_check, "raw_get", side_effect=_fake_raw_get):
                                return retention_check.main()


class MalformedResponseAfterCommittedWriteTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B item 1: a JSONDecodeError/OSError from the
    marker PUT (server-side write already committed) must not escape
    main() before the try/finally guarding restoration is ever
    reached - restoration must still run."""

    def test_marker_put_malformed_response_still_restores_original_value(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")
        harness.marker_put_exception = ValueError("Expecting value: line 1 column 1 (char 0)")

        exit_code = _run_full_cycle(harness)

        self.assertEqual(exit_code, 1)  # the marker-write check itself is recorded as a failure
        self.assertEqual(harness.value, "pre-existing-value", "Contract B must still restore the true original value")
        self.assertTrue(harness.restore_put_attempted)
        self.assertFalse(any(not ok for ok, _ in retention_check.restoration_results))


class ScaleDownTimeoutStillTriggersRestorationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B item 2: `scaled_down` was previously set
    True only AFTER scale_state(0) returned - a TimeoutExpired skipped
    restoration even though the marker had already changed and the
    server may have accepted the scale-down."""

    def test_scale_down_timeout_after_server_accepted_still_restores(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")
        harness.scale_down_exception = subprocess.TimeoutExpired(cmd="kubectl", timeout=30)
        harness.scale_down_server_effect = True  # the scale DID take effect server-side

        exit_code = _run_full_cycle(harness)

        self.assertEqual(exit_code, 1)
        self.assertTrue(harness.ever_scaled_down)
        self.assertEqual(harness.value, "pre-existing-value")
        self.assertTrue(harness.restore_put_attempted, "restore_state() must have been called despite the scale-down timeout")


class ScaleDownRejectedOriginalPodHealthyTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B item 3: restore_state() previously required
    a NEW Pod UID before restoring Contract B's value at all. If
    scale-down is rejected outright and the ORIGINAL healthy Pod never
    leaves, restoration must still happen - failing to prove Pod
    replacement is its own, separate, recorded experiment failure, not
    a reason to skip cleanup."""

    def test_rejected_scale_down_still_restores_and_records_new_uid_failure(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")
        harness.scale_down_exception = subprocess.CalledProcessError(returncode=1, cmd="kubectl", stderr="admission denied")
        harness.scale_down_server_effect = False  # rejected outright - the original Pod never left

        exit_code = _run_full_cycle(harness)

        self.assertEqual(exit_code, 1)
        self.assertFalse(harness.ever_scaled_down)
        # Contract B restoration must still have been attempted and
        # succeeded even though the Pod was never actually replaced.
        self.assertTrue(harness.restore_put_attempted)
        self.assertEqual(harness.value, "pre-existing-value")
        self.assertFalse(any(not ok for ok, _ in retention_check.restoration_results))
        # The new-Pod-UID assertion is a SEPARATE, recorded experiment
        # failure (results, not restoration_results) - proving Pod
        # replacement never happened is not silently dropped.
        self.assertTrue(
            any(not ok and "identity actually replaced" in msg for ok, msg in retention_check.results),
            f"expected a recorded new-Pod-UID assertion failure, got {retention_check.results}",
        )


class ContractATransportFailureDoesNotBlockContractBTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B item 4: Contract A's verification previously
    caught only (TimeoutError, RuntimeError) - a JSONDecodeError/OSError
    from that GET escaped restore_state() entirely (it runs inside
    main()'s finally, with no enclosing handler for those types),
    which would abort Contract B's restoration too."""

    def test_contract_a_parsing_failure_does_not_prevent_contract_b_restoration(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")
        harness.contract_a_get_exception = ValueError("Expecting value: line 1 column 1 (char 0)")

        exit_code = _run_full_cycle(harness)

        self.assertEqual(exit_code, 1)  # Contract A's own check is recorded as a failure
        self.assertTrue(harness.restore_put_attempted, "Contract B restoration must still be attempted after a Contract A transport/parsing failure")
        self.assertEqual(harness.value, "pre-existing-value")
        self.assertFalse(any(not ok for ok, _ in retention_check.restoration_results))
        self.assertTrue(any(not ok and "Contract A" in msg for ok, msg in retention_check.results))


class FailedExperimentAndFailedRestorationTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B: the primary experiment failure and a
    genuine restoration failure must both be preserved and both
    contribute to a nonzero exit - neither hides the other."""

    def test_both_failures_are_recorded_distinctly(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")

        def fake_check_endpoint(_port, _path, role="gateway"):
            return False, f"{_path}: simulated failure"

        harness.restore_put_status = 503  # restoration PUT itself fails too

        clock = _FakeClock()
        with _patched_clock(clock):
            with mock.patch.object(retention_check, "run", side_effect=harness.run):
                with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                    with mock.patch.object(retention_check, "_http", side_effect=harness.http):
                        with mock.patch.object(retention_check, "get_pods", side_effect=_fake_get_pods):
                            with mock.patch.object(retention_check, "check_endpoint", side_effect=fake_check_endpoint):
                                with mock.patch.object(retention_check, "raw_get", side_effect=_fake_raw_get):
                                    exit_code = retention_check.main()

        self.assertEqual(exit_code, 1)
        self.assertTrue(any(not ok for ok, _ in retention_check.results), "primary experiment failure must be recorded")
        self.assertTrue(any(not ok for ok, _ in retention_check.restoration_results), "restoration failure must be recorded distinctly")


class SupportedInterruptionDuringAttemptedWriteTests(_NoVerifyContextMixin, unittest.TestCase):
    """DAY4 batch 2b, Part B: a KeyboardInterrupt during an attempted
    mutating write must still reach the `finally` that runs
    restore_state() (Python always runs `finally` regardless of
    exception type) before propagating - never silently converted into
    a clean/successful result, and never claimed as recoverable from
    something stronger (SIGKILL/power loss)."""

    def test_keyboard_interrupt_during_marker_write_still_restores(self):
        harness = _RetentionHarness(initial_value="pre-existing-value")
        harness.marker_put_exception = KeyboardInterrupt()

        clock = _FakeClock()
        with self.assertRaises(KeyboardInterrupt):
            with _patched_clock(clock):
                with mock.patch.object(retention_check, "run", side_effect=harness.run):
                    with mock.patch.object(retention_check, "port_forward", return_value=_FakePortForward()):
                        with mock.patch.object(retention_check, "_http", side_effect=harness.http):
                            with mock.patch.object(retention_check, "get_pods", side_effect=_fake_get_pods):
                                with mock.patch.object(retention_check, "check_endpoint", side_effect=_fake_check_endpoint):
                                    with mock.patch.object(retention_check, "raw_get", side_effect=_fake_raw_get):
                                        retention_check.main()

        # The KeyboardInterrupt propagated (asserted above) - but the
        # guaranteed finally must still have run restore_state() first,
        # genuinely restoring the true original value.
        self.assertTrue(harness.restore_put_attempted)
        self.assertEqual(harness.value, "pre-existing-value")


class StatePodGoneAbsenceClassificationTests(unittest.TestCase):
    """DAY4 batch 2c (DAY4-TEST-M6): direct unit coverage for
    `_state_pod_gone()`'s structural `--ignore-not-found -o json`
    absence contract - replaces substring error-text matching, which
    could misclassify an unrelated client-side failure (e.g. a
    missing/broken credential-helper executable) whose OWN message
    happens to contain "not found" as genuine Pod absence."""

    def test_genuine_absence_returns_true(self):
        with mock.patch.object(retention_check, "run", return_value=_completed(returncode=0, stdout="")):
            self.assertIs(retention_check._state_pod_gone(), True)

    def test_present_resource_returns_none(self):
        pod_json = json.dumps({"metadata": {"uid": "pod-uid-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}})
        with mock.patch.object(retention_check, "run", return_value=_completed(returncode=0, stdout=pod_json)):
            self.assertIsNone(retention_check._state_pod_gone())

    def test_missing_credential_helper_never_misread_as_absence(self):
        with mock.patch.object(
            retention_check, "run",
            return_value=_completed(returncode=1, stderr='exec: "some-credential-helper": executable file not found in $PATH'),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                retention_check._state_pod_gone()
        self.assertIn("not found in $PATH", str(ctx.exception))

    def test_forbidden_raises_not_absence(self):
        with mock.patch.object(
            retention_check, "run",
            return_value=_completed(returncode=1, stderr=f'Error from server (Forbidden): pods "{retention_check.POD_NAME}" is forbidden'),
        ):
            with self.assertRaises(RuntimeError):
                retention_check._state_pod_gone()

    def test_api_timeout_propagates_for_wait_until_to_treat_as_not_ready(self):
        with mock.patch.object(retention_check, "run", side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30)):
            with self.assertRaises(subprocess.TimeoutExpired):
                retention_check._state_pod_gone()

    def test_malformed_nonempty_output_returns_none_not_absent(self):
        with mock.patch.object(retention_check, "run", return_value=_completed(returncode=0, stdout="not json")):
            self.assertIsNone(retention_check._state_pod_gone())


class WrongContextFailsClosedTests(unittest.TestCase):
    def setUp(self):
        retention_check.results = []
        retention_check.restoration_results = []

    def test_verify_context_failure_short_circuits_before_any_run_call(self):
        with mock.patch.object(retention_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(retention_check, "run") as mock_run:
                exit_code = retention_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

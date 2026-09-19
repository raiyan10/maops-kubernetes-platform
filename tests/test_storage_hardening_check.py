"""
Docker/Kubernetes-free unit tests for scripts/storage_hardening_check.py.

DAY4-TEST-H1: the embedded NEGATIVE_PROBE/POSITIVE_PROBE Python source
(run live inside a scratch Pod against a real kind node in production)
is executed here directly, in-process, against a REAL temporary
directory - proving its own errno classification is genuine, not a
bare non-zero-exit check:

  - Negative probe: a real, induced EACCES (an owned-but-mode-555
    directory - denies write even to its own owner, no root required)
    must be classified as the expected/passing case; any OTHER errno
    (ENOENT, via a nonexistent parent directory) must be classified as
    an unrelated, distinct failure (sys.exit(3)), never silently
    treated the same as EACCES.
  - Positive probe: a real, induced EROFS on the root-write attempt
    (mocked at the `builtins.open` boundary - EROFS itself cannot be
    induced without a real read-only bind mount, which this
    unprivileged process cannot create) must be classified as the
    expected/passing case; any OTHER errno (EACCES) at that exact call
    must be classified as a distinct, unrelated failure.

Also covers scripts/storage_hardening_check.py's own orchestration:
positive/negative probe pass/fail classification from Pod
phase/exitCode/log content, and the guaranteed scratch-namespace
cleanup (finally block always runs).
"""

from __future__ import annotations

import builtins
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import storage_hardening_check


def _run_probe_source(source: str, data_dir: str) -> int:
    """Execs the ACTUAL embedded probe source (with its hardcoded /data
    references substituted for a real temp directory) in a fresh
    namespace, returning the SystemExit code (0 if the script ran to
    completion without calling sys.exit)."""
    patched_source = source.replace("/data", data_dir).replace("/rootfs-write-should-fail.txt", os.path.join(data_dir, "..", "rootfs-write-should-fail.txt"))
    namespace = {"__name__": "__probe__"}
    try:
        exec(compile(patched_source, "<probe>", "exec"), namespace)
    except SystemExit as exc:
        return exc.code or 0
    return 0


class NegativeProbeErrnoClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_real_eacces_is_classified_as_the_expected_pass_case(self):
        data_dir = os.path.join(self.tmpdir.name, "data")
        os.mkdir(data_dir)
        os.chmod(data_dir, 0o555)  # denies write even to the owning process, no root needed
        try:
            exit_code = _run_probe_source(storage_hardening_check.NEGATIVE_PROBE, data_dir)
        finally:
            os.chmod(data_dir, 0o755)
        self.assertEqual(exit_code, 0, "a genuine EACCES must be classified as the expected/passing case (exit 0)")

    def test_unrelated_errno_is_classified_as_a_distinct_failure_not_silently_accepted(self):
        # A nonexistent parent directory induces a real ENOENT, not
        # EACCES - this must NOT be silently accepted as "access denied
        # as expected".
        data_dir = os.path.join(self.tmpdir.name, "does-not-exist")
        exit_code = _run_probe_source(storage_hardening_check.NEGATIVE_PROBE, data_dir)
        self.assertEqual(exit_code, 3, "a non-EACCES errno must be classified as a distinct, unrelated failure")

    def test_write_unexpectedly_succeeding_is_classified_as_a_distinct_failure(self):
        data_dir = os.path.join(self.tmpdir.name, "writable")
        os.mkdir(data_dir)  # normal, writable - the write will succeed, which must itself be a failure
        exit_code = _run_probe_source(storage_hardening_check.NEGATIVE_PROBE, data_dir)
        self.assertEqual(exit_code, 2, "an unexpectedly-successful write must be classified as its own distinct failure code")


class PositiveProbeRootFsErrnoClassificationTests(unittest.TestCase):
    """The root-filesystem write attempt is mocked at the `open`
    builtin boundary (real EROFS requires a real read-only bind mount,
    unavailable to this unprivileged test process) - everything else in
    the probe (the /data read/write/fsync/readback sequence) runs for
    real against a temp directory."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = os.path.join(self.tmpdir.name, "data")
        os.mkdir(self.data_dir)

    def _run_with_open_override(self, root_write_effect) -> int:
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith("rootfs-write-should-fail.txt"):
                raise root_write_effect
            return real_open(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", side_effect=fake_open):
            return _run_probe_source(storage_hardening_check.POSITIVE_PROBE, self.data_dir)

    def test_real_erofs_on_root_write_is_classified_as_the_expected_pass_case(self):
        exit_code = self._run_with_open_override(OSError(errno.EROFS, "Read-only file system"))
        self.assertEqual(exit_code, 0, "a genuine EROFS on the root-write attempt must be the expected/passing case (exit 0)")

    def test_unrelated_errno_on_root_write_is_a_distinct_failure(self):
        exit_code = self._run_with_open_override(OSError(errno.EACCES, "Permission denied"))
        self.assertEqual(exit_code, 3, "EACCES on the root-write attempt is NOT the same as EROFS and must be classified distinctly")

    def test_root_write_unexpectedly_succeeding_is_a_distinct_failure(self):
        # No exception raised at all for the "should-fail" root write.
        exit_code = self._run_with_open_override_success()
        self.assertEqual(exit_code, 2)

    def _run_with_open_override_success(self) -> int:
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith("rootfs-write-should-fail.txt"):
                return real_open(os.devnull, "w")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(builtins, "open", side_effect=fake_open):
            return _run_probe_source(storage_hardening_check.POSITIVE_PROBE, self.data_dir)


class ProbeClassificationTests(unittest.TestCase):
    """DAY5 remediation: `_classify_probe()` turns captured diagnostics into
    a pass/fail verdict for both probes. These tests exercise it directly
    (no kube/subprocess mocking needed) against the exact scenarios reported
    from live runs - a positive probe reported as exit 137, a negative probe
    that received EACCES but had no recorded exit code, and the missing-
    termination-status/timeout cases neither probe's own errno logic can
    ever see."""

    @staticmethod
    def _diag(phase="Succeeded", exit_code=0, container_reason=None, pod_reason=None, logs=""):
        return {
            "phase": phase,
            "pod_reason": pod_reason,
            "exit_code": exit_code,
            "container_reason": container_reason,
            "container_message": None,
            "logs": logs,
        }

    def test_positive_probe_succeeds(self):
        diag = self._diag(logs="UID=10001 GID=10001\nPOSITIVE_PROBE_PASS\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "POSITIVE_PROBE_PASS", "positive probe"
        )
        self.assertTrue(ok, message)

    def test_positive_probe_killed_with_exit_137_is_rejected(self):
        diag = self._diag(phase="Failed", exit_code=137, container_reason="Error", logs="POSITIVE_PROBE_PASS\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "POSITIVE_PROBE_PASS", "positive probe"
        )
        self.assertFalse(ok)
        self.assertIn("killed", message)
        self.assertIn("137", message)

    def test_negative_probe_naturally_receives_eacces_and_is_accepted(self):
        diag = self._diag(logs="NEGATIVE_WRITE_ERRNO=13 (EACCES)\nNEGATIVE_PROBE_PASS\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "NEGATIVE_PROBE_PASS", "negative probe"
        )
        self.assertTrue(ok, message)

    def test_negative_probe_killed_with_exit_137_is_rejected_despite_eacces_in_logs(self):
        """The exact reported shape: EACCES was logged, but the container
        was killed (exit 137) rather than exiting on its own - this must
        never be accepted just because the expected marker string is
        present somewhere in the log."""
        diag = self._diag(
            phase="Failed", exit_code=137, container_reason="Error",
            logs="NEGATIVE_WRITE_ERRNO=13 (EACCES)\n",
        )
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "NEGATIVE_PROBE_PASS", "negative probe"
        )
        self.assertFalse(ok)
        self.assertIn("killed", message)

    def test_missing_termination_exit_code_is_rejected_not_treated_as_success(self):
        """The other exact reported shape: phase already Failed/Succeeded
        but no exit code was ever observed - must be its own explicit
        failure, never silently passed."""
        diag = self._diag(phase="Failed", exit_code=None, logs="NEGATIVE_WRITE_ERRNO=13 (EACCES)\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "NEGATIVE_PROBE_PASS", "negative probe"
        )
        self.assertFalse(ok)
        self.assertIn("no container termination exit code", message)

    def test_oom_killed_reason_is_rejected_even_with_a_non_137_exit_code(self):
        diag = self._diag(phase="Failed", exit_code=1, container_reason="OOMKilled")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "POSITIVE_PROBE_PASS", "positive probe"
        )
        self.assertFalse(ok)
        self.assertIn("killed", message)

    def test_timeout_while_waiting_is_rejected(self):
        ok, message = storage_hardening_check._classify_probe(
            None, "did not observe a container termination status within 90.0s: timed out",
            "POSITIVE_PROBE_PASS", "positive probe",
        )
        self.assertFalse(ok)
        self.assertIn("timed out", message)

    def test_exit_zero_without_expected_marker_is_rejected(self):
        diag = self._diag(logs="something else entirely\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "POSITIVE_PROBE_PASS", "positive probe"
        )
        self.assertFalse(ok)
        self.assertIn("marker", message)

    def test_accepted_when_container_terminated_but_pod_phase_has_not_caught_up_yet(self):
        """DAY5 remediation, second race - reproduced live: the container's
        own terminated state already shows a genuine clean exit
        (exit_code=0, container_reason='Completed', expected marker in
        logs) but Pod-level `phase` still reads 'Running' in that same
        snapshot (the symmetric case of the original phase-vs-terminated
        race). `phase` must never be a hard gate - the container's own
        terminated state is the ground truth."""
        diag = self._diag(phase="Running", exit_code=0, container_reason="Completed", logs="NEGATIVE_PROBE_PASS\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "NEGATIVE_PROBE_PASS", "negative probe"
        )
        self.assertTrue(ok, message)

    def test_nonzero_exit_code_is_rejected_regardless_of_phase(self):
        diag = self._diag(phase="Succeeded", exit_code=2, container_reason="Error", logs="NEGATIVE_WRITE_SUCCEEDED=True\n")
        ok, message = storage_hardening_check._classify_probe(
            diag, None, "NEGATIVE_PROBE_PASS", "negative probe"
        )
        self.assertFalse(ok)
        self.assertIn("non-zero", message)


class ObserveProbeRaceTests(unittest.TestCase):
    """DAY5 remediation: `_observe_probe()`/`_terminated_pod()` must wait for
    the container's own terminated state, not just Pod phase - reproducing
    the actual race that produced `phase=Failed, exit_code=None` live."""

    def test_observe_probe_waits_past_a_phase_only_transition_for_real_termination_status(self):
        calls = {"n": 0}

        def fake_get_json(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Exactly the reported race: phase already flipped, but the
                # container's own state hasn't been written yet.
                return {"status": {"phase": "Failed"}}
            return {
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [{"state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}],
                }
            }

        with mock.patch.object(storage_hardening_check.kube, "get_json", side_effect=fake_get_json):
            with mock.patch.object(storage_hardening_check, "_logs", return_value="POSITIVE_PROBE_PASS\n"):
                with mock.patch.object(storage_hardening_check, "_pod_events", return_value=""):
                    with mock.patch("time.sleep"):
                        diag, timeout_detail = storage_hardening_check._observe_probe("some-pod")

        self.assertIsNone(timeout_detail)
        self.assertEqual(diag["exit_code"], 0)
        self.assertGreaterEqual(calls["n"], 2, "must keep polling past the phase-only snapshot")

    def test_observe_probe_times_out_when_container_never_reaches_terminated_state(self):
        def fake_get_json(*_args, **_kwargs):
            return {"status": {"phase": "Running"}}

        with mock.patch.object(storage_hardening_check.kube, "get_json", side_effect=fake_get_json):
            with mock.patch.object(storage_hardening_check, "_pod_events", return_value=""):
                with mock.patch.object(storage_hardening_check, "POD_DEADLINE_SECONDS", 0.05):
                    with mock.patch("time.sleep"):
                        diag, timeout_detail = storage_hardening_check._observe_probe("some-pod")

        self.assertIsNone(diag)
        self.assertIn("did not observe", timeout_detail)

    def test_observe_probe_timeout_enriches_detail_with_last_known_state_and_events(self):
        """DAY5 remediation follow-up: a genuine timeout (container never
        reaches a terminated state at all - e.g. stuck ImagePullBackOff)
        must surface the last observed phase/container state and any Pod
        events, not just a bare 'timed out' message - this is the only
        record of what the Pod was doing, captured before cleanup deletes
        the namespace."""
        def fake_get_json(*_args, **_kwargs):
            return {
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
                }
            }

        with mock.patch.object(storage_hardening_check.kube, "get_json", side_effect=fake_get_json):
            with mock.patch.object(
                storage_hardening_check, "_pod_events",
                return_value="Failed: Failed to pull image \"maops-kubernetes-app:0.5.0\": not found (x3)",
            ):
                with mock.patch.object(storage_hardening_check, "POD_DEADLINE_SECONDS", 0.05):
                    with mock.patch("time.sleep"):
                        diag, timeout_detail = storage_hardening_check._observe_probe("some-pod")

        self.assertIsNone(diag)
        self.assertIn("ImagePullBackOff", timeout_detail)
        self.assertIn("Failed to pull image", timeout_detail)


class PodEventsAndLastKnownStateTests(unittest.TestCase):
    """DAY5 remediation follow-up: `_pod_events()`/`_last_known_pod_state()`
    are best-effort diagnostics only - a lookup failure must return an
    empty string, never raise and never itself fail the check."""

    def test_pod_events_parses_reason_and_message(self):
        payload = {
            "items": [
                {"reason": "Failed", "message": 'Failed to pull image "x": not found', "count": 3},
                {"reason": "BackOff", "message": "Back-off pulling image", "count": 5},
            ]
        }
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr=""),
        ):
            events = storage_hardening_check._pod_events("some-pod")
        self.assertIn("Failed to pull image", events)
        self.assertIn("Back-off pulling image", events)

    def test_pod_events_returns_empty_string_on_kubectl_failure(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Error from server: timeout"),
        ):
            events = storage_hardening_check._pod_events("some-pod")
        self.assertEqual(events, "")

    def test_pod_events_returns_empty_string_on_timeout(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
        ):
            events = storage_hardening_check._pod_events("some-pod")
        self.assertEqual(events, "")

    def test_last_known_pod_state_reports_phase_and_container_state(self):
        with mock.patch.object(
            storage_hardening_check.kube, "get_json",
            return_value={
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
                }
            },
        ):
            snapshot = storage_hardening_check._last_known_pod_state("some-pod")
        self.assertIn("Pending", snapshot)
        self.assertIn("ImagePullBackOff", snapshot)

    def test_last_known_pod_state_best_effort_returns_empty_on_error(self):
        with mock.patch.object(
            storage_hardening_check.kube, "get_json",
            side_effect=subprocess.CalledProcessError(1, ["kubectl"]),
        ):
            snapshot = storage_hardening_check._last_known_pod_state("some-pod")
        self.assertEqual(snapshot, "")


class ClassifyProbeEventsInMessageTests(unittest.TestCase):
    """`_classify_probe()` must surface captured events on rejection - this
    is what makes a DeadlineExceeded/ContainerStatusUnknown kill (the
    container never actually ran) distinguishable from one that ran and was
    then killed, without a manual repro."""

    def test_killed_probe_rejection_message_includes_events(self):
        diag = {
            "phase": "Failed",
            "pod_reason": "DeadlineExceeded",
            "exit_code": 137,
            "container_reason": "ContainerStatusUnknown",
            "container_message": None,
            "logs": "",
            "events": "Failed: Failed to pull image \"maops-kubernetes-app:0.5.0\": not found (x3)",
        }
        ok, message = storage_hardening_check._classify_probe(diag, None, "POSITIVE_PROBE_PASS", "positive probe")
        self.assertFalse(ok)
        self.assertIn("Failed to pull image", message)

    def test_missing_exit_code_events_absent_message_has_no_events_section(self):
        diag = {
            "phase": "Failed", "pod_reason": None, "exit_code": None,
            "container_reason": None, "container_message": None, "logs": "", "events": "",
        }
        ok, message = storage_hardening_check._classify_probe(diag, None, "POSITIVE_PROBE_PASS", "positive probe")
        self.assertFalse(ok)
        self.assertNotIn("events:", message)


def _fake_run_namespace_exists(exists: bool):
    def fake_run(*args, **_kwargs):
        if "namespace" in args and "get" in args:
            if exists:
                return subprocess.CompletedProcess(args=["kubectl"], returncode=0, stdout='{"metadata": {"uid": "existing-ns-uid"}}', stderr="")
            # DAY4 batch 2c: genuine absence is exit 0 + EMPTY stdout
            # under the `--ignore-not-found -o json` contract, not a
            # nonzero exit with NotFound-shaped stderr.
            return subprocess.CompletedProcess(args=["kubectl"], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected kube.run call: {args}")

    return fake_run


class _RaceSimulatingNamespaceAPI:
    """DAY4 batch 2c (DAY4-SEC-M2): a STATEFUL fake distinguishing
    CREATE (atomic - fails outright if the object already exists) from
    APPLY (upsert - would silently attach our label to someone else's
    pre-existing object) semantics, simulating another creator winning
    the race between our own absence check and our own create call.
    Exercises the ACTUAL command construction (the argv/manifest a
    regression to `apply` would still "pass" a naive
    returns-the-desired-label mock) - a mock that simply returned an
    ownership-label match would not catch a regression back to
    `apply`, since `apply` upserting a label onto someone else's
    object would ALSO make a bare label-match check "pass"."""

    def __init__(self):
        self.create_calls: list[str] = []
        self.apply_calls: list[str] = []

    def kube_run(self, *args, **_kwargs):
        if "namespace" in args and "get" in args:
            # The absence check always answers "not found" - the race
            # window is BETWEEN this check and our own create call.
            return subprocess.CompletedProcess(["kubectl"], 0, stdout="", stderr="")
        raise AssertionError(f"unexpected kube.run call: {args}")

    def subprocess_run(self, cmd, input=None, **_kwargs):
        if "create" in cmd and "-f" in cmd:
            self.create_calls.append(input)
            name = storage_hardening_check.NAMESPACE
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr=f'Error from server (AlreadyExists): namespaces "{name}" already exists',
            )
        if "apply" in cmd and "-f" in cmd:
            # A regression back to `apply` for the namespace step would
            # land here instead of "create" above, and would report
            # (false) success - proving this test would actually catch
            # that regression via create_calls/apply_calls below.
            self.apply_calls.append(input)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call (a delete must never be reached here): {cmd}")


class MainOrchestrationTests(unittest.TestCase):
    """DAY4 batch 2b (Part E): the namespace lifecycle now requires
    verified ownership (a per-invocation label + captured UID) before
    ANY use or deletion - an unsuccessful lookup is not automatically
    NotFound, a failed/uncertain creation must not trigger a blind
    delete-by-name, and cleanup is attempted ONLY once ownership was
    actually established."""

    def setUp(self):
        storage_hardening_check.results = []

    def test_existing_namespace_refuses_to_reuse_it(self):
        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(True)):
                with mock.patch.object(storage_hardening_check, "_apply") as mock_apply:
                    exit_code = storage_hardening_check.main()
        self.assertEqual(exit_code, 1)
        mock_apply.assert_not_called()

    def test_ambiguous_lookup_failure_fails_closed_without_creating_anything(self):
        """An unsuccessful lookup (RBAC denial, API hiccup - stderr
        contains neither NotFound nor "not found") must never be
        treated as proof the namespace is free to create."""
        def fake_run(*args, **_kwargs):
            if "namespace" in args and "get" in args:
                return subprocess.CompletedProcess(args=["kubectl"], returncode=1, stdout="", stderr="Error from server: etcdserver: request timed out")
            raise AssertionError(f"unexpected kube.run call: {args}")

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=fake_run):
                with mock.patch.object(storage_hardening_check, "_apply") as mock_apply:
                    exit_code = storage_hardening_check.main()
        self.assertEqual(exit_code, 1)
        mock_apply.assert_not_called()

    def test_another_creator_winning_the_race_is_preserved_never_adopted_never_deleted(self):
        """DAY4-SEC-M2: the actual race this batch fixes - our own
        absence check sees nothing, but another creator's namespace has
        already landed by the time our own create call reaches the
        API. Must fail closed, use CREATE (not apply) for the attempt,
        and never touch (adopt or delete) the resulting namespace."""
        api = _RaceSimulatingNamespaceAPI()

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=api.kube_run):
                with mock.patch.object(storage_hardening_check.subprocess, "run", side_effect=api.subprocess_run):
                    exit_code = storage_hardening_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(api.create_calls), 1, "the namespace step must issue exactly one CREATE attempt")
        self.assertEqual(api.apply_calls, [], "the namespace step must never use APPLY (upsert) - only CREATE (atomic)")
        self.assertIn(storage_hardening_check.NAMESPACE, api.create_calls[0])
        self.assertIn(storage_hardening_check.OWNER_LABEL, api.create_calls[0])

    def test_failed_namespace_creation_does_not_trigger_blind_cleanup_delete(self):
        """DAY4 batch 2b/2c: a failed namespace creation must never fall
        through to `kubectl delete namespace <name>` by name alone -
        ownership was never established, so cleanup must skip the
        delete entirely (not attempt and fail it - simply not touch a
        namespace this invocation never proved it owns)."""
        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("failed", "admission webhook denied")):
                    with mock.patch.object(storage_hardening_check.subprocess, "run") as mock_subprocess_run:
                        exit_code = storage_hardening_check.main()

        self.assertEqual(exit_code, 1)
        mock_subprocess_run.assert_not_called()  # no delete attempt of any kind

    def test_ownership_verified_then_later_apply_failure_still_cleans_up(self):
        """Once ownership IS verified (a successful CREATE), a later
        failure (e.g. the PVC manifest, still applied normally) must
        still result in the guaranteed cleanup delete actually running -
        the create-only fix is scoped to UNVERIFIED ownership, not to
        disabling cleanup altogether."""
        def fake_apply(manifest: str) -> None:
            raise subprocess.CalledProcessError(returncode=1, cmd=["kubectl", "apply"], stderr="pvc apply failed")

        cleanup_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            if "delete" in cmd:
                cleanup_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {cmd}")

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("created", "")):
                    with mock.patch.object(storage_hardening_check, "_apply", side_effect=fake_apply):
                        with mock.patch.object(storage_hardening_check, "_verify_ownership", return_value="owned-ns-uid"):
                            with mock.patch.object(storage_hardening_check.subprocess, "run", side_effect=fake_subprocess_run):
                                exit_code = storage_hardening_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(cleanup_calls), 1, "cleanup must still run once ownership was actually verified")

    def test_uncertain_creation_outcome_is_resolved_by_reobservation(self):
        """A `subprocess.TimeoutExpired` on the create call itself is an
        UNCERTAIN outcome - the caller must re-observe ownership rather
        than assume either way, and proceed (including cleanup) once
        verified. `_verify_ownership` is called both right after
        creation and again immediately before cleanup deletes."""
        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("uncertain", "client-side timeout")):
                    with mock.patch.object(storage_hardening_check, "_verify_ownership", return_value="owned-ns-uid") as mock_verify:
                        with mock.patch.object(storage_hardening_check, "_apply", side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="stop here")):
                            with mock.patch.object(storage_hardening_check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as mock_subprocess_run:
                                storage_hardening_check.main()

        self.assertGreaterEqual(mock_verify.call_count, 1, "an uncertain creation outcome must be resolved by re-observing ownership")
        mock_subprocess_run.assert_called_once()  # the cleanup delete, since ownership WAS verified

    def test_ownership_mismatch_at_cleanup_time_refuses_to_delete(self):
        """If the namespace no longer carries this run's owner UID by
        the time cleanup runs (e.g. something else replaced it), the
        delete must be refused, not attempted blindly by name. Fails
        fast via the PVC `_apply()` call rather than running the full
        probe-pod wait loop."""
        ownership_calls = {"count": 0}

        def fake_verify_ownership(_name, _token):
            ownership_calls["count"] += 1
            return "owned-ns-uid" if ownership_calls["count"] == 1 else "a-different-uid"

        def fake_apply(manifest: str) -> None:
            raise subprocess.CalledProcessError(returncode=1, cmd=["kubectl", "apply"], stderr="pvc apply failed")

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("created", "")):
                    with mock.patch.object(storage_hardening_check, "_apply", side_effect=fake_apply):
                        with mock.patch.object(storage_hardening_check, "_verify_ownership", side_effect=fake_verify_ownership):
                            with mock.patch.object(storage_hardening_check.subprocess, "run") as mock_subprocess_run:
                                storage_hardening_check.main()

        mock_subprocess_run.assert_not_called()
        self.assertTrue(any(not ok and "refusing to delete" in msg for ok, msg in storage_hardening_check.results))

    def test_cleanup_runs_after_a_fully_successful_run(self):
        """DAY5 remediation: end-to-end through the new observe/classify
        path - both probes complete naturally (phase=Succeeded, exit_code=0,
        marker present) and the guaranteed namespace cleanup still runs."""
        cleanup_calls = []

        def fake_subprocess_run(cmd, **_kwargs):
            if "delete" in cmd:
                cleanup_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {cmd}")

        def fake_get_json(*args, **_kwargs):
            if "pvc" in args:
                return {"status": {"phase": "Bound"}}
            if "pod" in args:
                return {
                    "status": {
                        "phase": "Succeeded",
                        "containerStatuses": [{"state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}],
                    }
                }
            raise AssertionError(f"unexpected kube.get_json call: {args}")

        def fake_logs(pod_name):
            marker = "POSITIVE_PROBE_PASS" if pod_name == storage_hardening_check.POSITIVE_POD else "NEGATIVE_PROBE_PASS"
            return marker + "\n"

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("created", "")):
                    with mock.patch.object(storage_hardening_check, "_verify_ownership", return_value="owned-ns-uid"):
                        with mock.patch.object(storage_hardening_check, "_apply"):
                            with mock.patch.object(storage_hardening_check.kube, "get_json", side_effect=fake_get_json):
                                with mock.patch.object(storage_hardening_check, "_logs", side_effect=fake_logs):
                                    with mock.patch.object(storage_hardening_check, "_pod_events", return_value=""):
                                        with mock.patch.object(storage_hardening_check.subprocess, "run", side_effect=fake_subprocess_run):
                                            with mock.patch("time.sleep"):
                                                exit_code = storage_hardening_check.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(cleanup_calls), 1, "cleanup must run after a fully successful probe run")

    def test_cleanup_runs_after_a_probe_is_killed(self):
        """DAY5 remediation: both probes come back exit_code=137 (killed) -
        the run must be reported as a failure, but the guaranteed namespace
        cleanup must still run exactly once."""
        cleanup_calls = []

        def fake_subprocess_run(cmd, **_kwargs):
            if "delete" in cmd:
                cleanup_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {cmd}")

        def fake_get_json(*args, **_kwargs):
            if "pvc" in args:
                return {"status": {"phase": "Bound"}}
            if "pod" in args:
                return {
                    "status": {
                        "phase": "Failed",
                        "containerStatuses": [{"state": {"terminated": {"exitCode": 137, "reason": "Error"}}}],
                    }
                }
            raise AssertionError(f"unexpected kube.get_json call: {args}")

        with mock.patch.object(storage_hardening_check.kube, "verify_context"):
            with mock.patch.object(storage_hardening_check.kube, "run", side_effect=_fake_run_namespace_exists(False)):
                with mock.patch.object(storage_hardening_check, "_create_namespace_only", return_value=("created", "")):
                    with mock.patch.object(storage_hardening_check, "_verify_ownership", return_value="owned-ns-uid"):
                        with mock.patch.object(storage_hardening_check, "_apply"):
                            with mock.patch.object(storage_hardening_check.kube, "get_json", side_effect=fake_get_json):
                                with mock.patch.object(storage_hardening_check, "_logs", return_value=""):
                                    with mock.patch.object(storage_hardening_check, "_pod_events", return_value=""):
                                        with mock.patch.object(storage_hardening_check.subprocess, "run", side_effect=fake_subprocess_run):
                                            with mock.patch("time.sleep"):
                                                exit_code = storage_hardening_check.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(cleanup_calls), 1, "cleanup must run even when a probe is rejected (killed/exit 137)")

    def test_verify_context_failure_short_circuits_before_any_namespace_check(self):
        with mock.patch.object(storage_hardening_check.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(storage_hardening_check.kube, "run") as mock_run:
                exit_code = storage_hardening_check.main()
        self.assertEqual(exit_code, 1)
        mock_run.assert_not_called()


class NamespaceLookupHelperTests(unittest.TestCase):
    """DAY4 batch 2b/2c: direct unit coverage for `_namespace_lookup()`
    - the tri-state exists/not-found/unknown classification every
    other orchestration test above depends on. Batch 2c replaced
    substring error-text matching with the structural
    `--ignore-not-found -o json` contract: exit 0 + empty stdout means
    genuine absence, exit 0 + a JSON object means present, and ANY
    nonzero exit means unverified - regardless of what the error text
    says."""

    def test_genuine_absence_returns_false(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ):
            exists, ns_json, _detail = storage_hardening_check._namespace_lookup("x")
        self.assertIs(exists, False)
        self.assertIsNone(ns_json)

    def test_unrelated_failure_returns_none_not_false(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Error from server: connection refused"),
        ):
            exists, ns_json, _detail = storage_hardening_check._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)

    def test_missing_credential_helper_never_misread_as_absence(self):
        """DAY4-TEST-M6: a client-side failure whose OWN message
        happens to contain "not found" (a missing/broken credential
        exec-plugin binary) must never be misclassified as genuine
        resource absence - it is a nonzero-exit failure, not a
        suppressed-NotFound success."""
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr='exec: "some-credential-helper": executable file not found in $PATH',
            ),
        ):
            exists, ns_json, detail = storage_hardening_check._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)
        self.assertIn("not found in $PATH", detail)

    def test_forbidden_returns_none_not_false(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr='Error from server (Forbidden): namespaces "x" is forbidden'),
        ):
            exists, ns_json, _detail = storage_hardening_check._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)

    def test_api_timeout_returns_none_not_false(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            side_effect=subprocess.TimeoutExpired(cmd="kubectl", timeout=30),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                storage_hardening_check._namespace_lookup("x")

    def test_found_returns_true_and_parsed_json(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout='{"metadata": {"uid": "abc"}}', stderr=""),
        ):
            exists, ns_json, _detail = storage_hardening_check._namespace_lookup("x")
        self.assertIs(exists, True)
        self.assertEqual(ns_json["metadata"]["uid"], "abc")

    def test_malformed_nonempty_output_on_success_returns_none(self):
        with mock.patch.object(
            storage_hardening_check.kube, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
        ):
            exists, ns_json, _detail = storage_hardening_check._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)


if __name__ == "__main__":
    unittest.main()

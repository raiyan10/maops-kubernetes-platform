"""
Docker/Kubernetes-free unit tests for scripts/kube.py's wait_until().

Uses a fake, manually-advanced clock (monkeypatching time.monotonic /
time.sleep) so these tests are deterministic and do not actually sleep.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import kube


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=returncode, stdout=stdout, stderr=stderr)


class VerifyContextTests(unittest.TestCase):
    """DAY3: verify_context() is the fail-closed guard every live script
    calls before touching the cluster - it must reject a missing context,
    an unreachable cluster, and node names that don't actually belong to
    the expected maops-k8s-day3 cluster, and pass only when both checks
    agree."""

    def test_context_missing_from_kubeconfig_raises(self):
        with mock.patch.object(
            kube, "run", return_value=_completed(stdout="kind-maops-k8s-day1\nkind-maops-k8s-day2\n")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                kube.verify_context()
        self.assertIn("not found in kubeconfig", str(ctx.exception))

    def test_get_contexts_failure_raises(self):
        with mock.patch.object(kube, "run", return_value=_completed(returncode=1, stderr="boom")):
            with self.assertRaises(RuntimeError) as ctx:
                kube.verify_context()
        self.assertIn("could not list kubeconfig contexts", str(ctx.exception))

    def test_cluster_unreachable_after_context_found_raises(self):
        def fake_run(*args, **_kwargs):
            if "get-contexts" in args:
                return _completed(stdout=f"{kube.CONTEXT}\n")
            return _completed(returncode=1, stderr="connection refused")

        with mock.patch.object(kube, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                kube.verify_context()
        self.assertIn("unreachable", str(ctx.exception))

    def test_node_names_not_matching_cluster_raises(self):
        import json

        def fake_run(*args, **_kwargs):
            if "get-contexts" in args:
                return _completed(stdout=f"{kube.CONTEXT}\n")
            nodes = {"items": [{"metadata": {"name": "maops-k8s-day2-control-plane"}}]}
            return _completed(stdout=json.dumps(nodes))

        with mock.patch.object(kube, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                kube.verify_context()
        self.assertIn("do not all belong to expected cluster", str(ctx.exception))

    def test_prefix_collision_node_name_raises(self):
        # DAY3-INT-M2: a node/cluster name that merely starts with the
        # expected cluster name prefix (e.g. a "staging" cluster whose name
        # happens to extend maops-k8s-day3-) must NOT be accepted just
        # because of a bare startswith() match.
        import json

        def fake_run(*args, **_kwargs):
            if "get-contexts" in args:
                return _completed(stdout=f"{kube.CONTEXT}\n")
            nodes = {
                "items": [
                    {"metadata": {"name": "maops-k8s-day3-staging-control-plane"}},
                    {"metadata": {"name": "maops-k8s-day3-staging-worker"}},
                ]
            }
            return _completed(stdout=json.dumps(nodes))

        with mock.patch.object(kube, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                kube.verify_context()
        self.assertIn("do not all belong to expected cluster", str(ctx.exception))

    def test_no_nodes_at_all_raises(self):
        import json

        def fake_run(*args, **_kwargs):
            if "get-contexts" in args:
                return _completed(stdout=f"{kube.CONTEXT}\n")
            return _completed(stdout=json.dumps({"items": []}))

        with mock.patch.object(kube, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                kube.verify_context()

    def test_matching_context_and_nodes_passes(self):
        import json

        def fake_run(*args, **_kwargs):
            if "get-contexts" in args:
                return _completed(stdout=f"kind-maops-k8s-day1\n{kube.CONTEXT}\n")
            nodes = {
                "items": [
                    {"metadata": {"name": f"{kube.CLUSTER_NAME}-control-plane"}},
                    {"metadata": {"name": f"{kube.CLUSTER_NAME}-worker"}},
                    {"metadata": {"name": f"{kube.CLUSTER_NAME}-worker2"}},
                ]
            }
            return _completed(stdout=json.dumps(nodes))

        with mock.patch.object(kube, "run", side_effect=fake_run):
            kube.verify_context()  # must not raise


class RunTimeoutTests(unittest.TestCase):
    """DAY3-INT-H2: every kubectl subprocess call is bounded. `run()` must
    forward a default timeout to subprocess.run() when the caller doesn't
    specify one, and forward an explicit longer timeout verbatim when the
    caller does (e.g. a real 180s rollout-status wait) - it must never
    silently clamp a legitimately long-running command down to the
    default."""

    def test_default_timeout_is_forwarded_to_subprocess_run(self):
        with mock.patch.object(kube.subprocess, "run", return_value=_completed()) as mock_run:
            kube.run("get", "nodes")
        self.assertEqual(mock_run.call_args.kwargs.get("timeout"), kube.DEFAULT_TIMEOUT_SECONDS)

    def test_explicit_long_timeout_is_respected_not_clamped(self):
        # A 180s Kubernetes-side rollout-status wait must reach
        # subprocess.run() with a subprocess timeout that actually exceeds
        # 180s, not the generic 30s default.
        long_timeout = kube.subprocess_timeout_for(180)
        self.assertGreater(long_timeout, 180)
        with mock.patch.object(kube.subprocess, "run", return_value=_completed()) as mock_run:
            kube.run("rollout", "status", "deployment/x", "--timeout=180s", timeout=long_timeout)
        self.assertEqual(mock_run.call_args.kwargs.get("timeout"), long_timeout)
        self.assertNotEqual(mock_run.call_args.kwargs.get("timeout"), kube.DEFAULT_TIMEOUT_SECONDS)

    def test_subprocess_timeout_expired_propagates_as_normal_exception(self):
        # A hung kubectl must never hang the calling script forever - once
        # bounded, a timeout is a normal, catchable Python exception, not a
        # silent hang.
        with mock.patch.object(
            kube.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30)
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                kube.run("get", "nodes")

    def test_get_json_forwards_timeout(self):
        with mock.patch.object(kube, "run", return_value=_completed(stdout="{}")) as mock_run:
            kube.get_json("get", "nodes", timeout=99)
        self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 99)


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _patched_clock(clock: _FakeClock):
    return mock.patch.multiple(kube.time, monotonic=clock.monotonic, sleep=clock.sleep)


class WaitUntilSentinelTests(unittest.TestCase):
    """DAY1-TEST-L2: None means retry, anything else (including falsy
    values) means success."""

    def test_truthy_value_is_returned(self):
        clock = _FakeClock()
        with _patched_clock(clock):
            value = kube.wait_until(lambda: {"ready": True}, timeout=10, interval=1)
        self.assertEqual(value, {"ready": True})

    def test_none_is_retried_until_timeout(self):
        clock = _FakeClock()
        with _patched_clock(clock):
            with self.assertRaises(TimeoutError):
                kube.wait_until(lambda: None, timeout=5, interval=1)

    def test_zero_is_a_successful_sentinel(self):
        clock = _FakeClock()
        calls = []

        def predicate():
            calls.append(1)
            return 0

        with _patched_clock(clock):
            value = kube.wait_until(predicate, timeout=10, interval=1)
        self.assertEqual(value, 0)
        self.assertEqual(len(calls), 1, "predicate should not be retried once it returns a non-None value")

    def test_empty_list_is_a_successful_sentinel(self):
        clock = _FakeClock()
        with _patched_clock(clock):
            value = kube.wait_until(lambda: [], timeout=10, interval=1)
        self.assertEqual(value, [])

    def test_empty_string_is_a_successful_sentinel(self):
        clock = _FakeClock()
        with _patched_clock(clock):
            value = kube.wait_until(lambda: "", timeout=10, interval=1)
        self.assertEqual(value, "")

    def test_predicate_eventually_returning_non_none_succeeds(self):
        clock = _FakeClock()
        results = iter([None, None, []])

        with _patched_clock(clock):
            value = kube.wait_until(lambda: next(results), timeout=10, interval=1)
        self.assertEqual(value, [])

    def test_exception_is_retried_and_surfaced_on_timeout(self):
        clock = _FakeClock()

        def predicate():
            raise RuntimeError("boom")

        with _patched_clock(clock):
            with self.assertRaises(TimeoutError) as ctx:
                kube.wait_until(predicate, timeout=3, interval=1, description="thing")
        self.assertIn("thing", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))


class WaitUntilMonotonicClockTests(unittest.TestCase):
    """DAY1-TEST-M4: deadline/elapsed math must use time.monotonic(), not
    time.time(), so an NTP/wall-clock jump cannot corrupt the bound."""

    def test_wall_clock_time_is_never_consulted(self):
        clock = _FakeClock()

        def _forbidden(*_a, **_kw):
            raise AssertionError("wait_until must not call time.time()")

        with mock.patch.object(kube.time, "time", side_effect=_forbidden):
            with _patched_clock(clock):
                kube.wait_until(lambda: True, timeout=5, interval=1)

    def test_backward_wall_clock_jump_does_not_affect_bounded_wait(self):
        # Simulate an NTP correction that moves time.time() far backward;
        # since wait_until no longer reads it at all, this must have zero
        # effect on the monotonic-clock-driven timeout.
        clock = _FakeClock()
        with mock.patch.object(kube.time, "time", return_value=0.0):
            with _patched_clock(clock):
                with self.assertRaises(TimeoutError):
                    kube.wait_until(lambda: None, timeout=3, interval=1)


if __name__ == "__main__":
    unittest.main()

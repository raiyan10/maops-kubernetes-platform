"""
Docker/Kubernetes-free unit tests for scripts/kube.py's wait_until().

Uses a fake, manually-advanced clock (monkeypatching time.monotonic /
time.sleep) so these tests are deterministic and do not actually sleep.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import kube


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

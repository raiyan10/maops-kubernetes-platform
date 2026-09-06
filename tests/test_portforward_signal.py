"""
Docker/Kubernetes-free unit tests for the SIGTERM-safe cleanup mechanism
in scripts/portforward.py (DAY1-INT-M1).

These tests exercise the signal-conversion primitive directly - no
kubectl, no cluster, no subprocess - by delivering a real SIGTERM to
this test process itself and asserting it becomes a catchable
exception that unwinds a try/finally, rather than killing the process.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import portforward


class SigtermConversionTests(unittest.TestCase):
    def test_sigterm_raises_signal_interrupt_within_scope(self):
        with self.assertRaises(portforward.PortForwardSignalInterrupt):
            with portforward._convert_sigterm_to_exception():
                os.kill(os.getpid(), signal.SIGTERM)
                self.fail("execution must not continue past a delivered SIGTERM")

    def test_cleanup_runs_via_try_finally_on_sigterm(self):
        cleanup_calls = []

        def do_work():
            with portforward._convert_sigterm_to_exception():
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                finally:
                    cleanup_calls.append("cleaned up")

        with self.assertRaises(portforward.PortForwardSignalInterrupt):
            do_work()
        self.assertEqual(cleanup_calls, ["cleaned up"])

    def test_normal_success_path_unaffected(self):
        with portforward._convert_sigterm_to_exception():
            value = 1 + 1
        self.assertEqual(value, 2)

    def test_exception_cleanup_still_works_without_any_signal(self):
        cleanup_calls = []

        def do_work():
            with portforward._convert_sigterm_to_exception():
                try:
                    raise RuntimeError("boom")
                finally:
                    cleanup_calls.append("cleaned up")

        with self.assertRaises(RuntimeError):
            do_work()
        self.assertEqual(cleanup_calls, ["cleaned up"])

    def test_sigterm_disposition_restored_after_scope_exits(self):
        original = signal.getsignal(signal.SIGTERM)
        with portforward._convert_sigterm_to_exception():
            during = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(during, original)
        restored = signal.getsignal(signal.SIGTERM)
        self.assertEqual(restored, original)

    def test_sigterm_disposition_restored_even_when_body_raises(self):
        original = signal.getsignal(signal.SIGTERM)
        try:
            with portforward._convert_sigterm_to_exception():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)

    def test_sigint_disposition_is_never_touched(self):
        before = signal.getsignal(signal.SIGINT)
        with portforward._convert_sigterm_to_exception():
            during = signal.getsignal(signal.SIGINT)
        after = signal.getsignal(signal.SIGINT)
        self.assertEqual(before, during)
        self.assertEqual(before, after)

    def test_does_not_leak_a_global_handler_outside_the_context(self):
        # Once the context has exited, SIGTERM must behave exactly as it
        # did before this module was ever used - no lingering handler.
        original = signal.getsignal(signal.SIGTERM)
        with portforward._convert_sigterm_to_exception():
            pass
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)
        # A second, independent use must behave identically (not affected
        # by any leftover state from the first).
        with self.assertRaises(portforward.PortForwardSignalInterrupt):
            with portforward._convert_sigterm_to_exception():
                os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original)


class BackgroundThreadSafetyTests(unittest.TestCase):
    """DAY3 regression (found live during the DAY3-ARCH-M1/DAY3-INT-M1
    in-flight rollout sampler remediation): `signal.signal()` raises
    ValueError when called from any thread other than the main thread of
    the main interpreter. Before this fix, using `port_forward()` (which
    wraps its body in `_convert_sigterm_to_exception()`) from a
    background thread raised that ValueError on every call, BEFORE the
    already-spawned kubectl child process could ever be terminated -
    leaking it every single time. Confirmed live: 61 leaked
    `kubectl port-forward` processes and 0/N successful samples across an
    actual rolling update before this fix; 0 leaked processes and normal
    success afterward."""

    def test_context_manager_does_not_raise_from_a_background_thread(self):
        outcome = {}

        def worker():
            try:
                with portforward._convert_sigterm_to_exception():
                    pass
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                outcome["ok"] = False
                outcome["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        self.assertTrue(outcome.get("ok"), f"must not raise off the main thread, got {outcome.get('exc')!r}")

    def test_main_thread_sigterm_handling_is_unaffected_by_the_thread_guard(self):
        # The background-thread no-op path must never accidentally become
        # the path taken on the main thread itself.
        with self.assertRaises(portforward.PortForwardSignalInterrupt):
            with portforward._convert_sigterm_to_exception():
                os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()

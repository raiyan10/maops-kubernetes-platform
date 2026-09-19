"""
Tests for scripts/day5_lock.py - the minimal process-held local mutual-
exclusion lock scoped to this project's single Day 5 target (DAY5
batch 2b/2c).

Uses REAL local subprocesses and harmless marker-file operations (never
a mock of `fcntl.flock` itself, and never a fixed `time.sleep` assumed
to guarantee lock acquisition - every test that needs to know a holder
has actually started waits for that holder's own readiness marker
file, polled with a bounded timeout) so contention is proven against
the actual OS primitive, not a stand-in for it. Every subprocess this
file starts is explicitly waited on or killed+reaped in a `finally` -
only test-owned processes are ever touched.

DAY5 batch 2c replaced the bare `DAY4_LOCK_HELD=1` boolean trust
mechanism (which an unrelated process could set to bypass acquisition
entirely, and which released the OS-level lock the instant the WRAPPER
process died even if its child kept running) with verifiable inherited
ownership via a shared, `pass_fds`-inherited file descriptor
(`DAY5_LOCK_FD`, checked via `/proc/self/fd/<n>` + a non-blocking
flock probe - see day5_lock.py's own module docstring for the full
design). This file's `test_recursive_ownership_...` test from batch 2b
(which treated an unrelated process merely SETTING the old boolean
flag as proof of successful recursive ownership) is replaced by
`test_forged_lock_fd_without_valid_ownership_does_not_bypass_active_holder`
below, which proves the OPPOSITE: a forged/stale claim of ownership
must never bypass a real, active holder.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "day5_lock.py")


def _wait_for_file(path: str, timeout: float = 5.0, interval: float = 0.02) -> None:
    """Readiness handshake: polls until `path` exists, never assuming a
    fixed sleep guarantees a holder has actually acquired the lock and
    started running its wrapped command."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for readiness marker {path!r}")


def _reap(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Cleans up only this test's own child process - waits, then
    force-kills and reaps if it hasn't exited on its own."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class RealLockContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.lock_path = os.path.join(self.tmpdir.name, "day4-mutation.lock")
        self.env = dict(os.environ)
        self.env["DAY5_LOCK_PATH"] = self.lock_path
        self.env.pop("DAY5_LOCK_FD", None)
        self.env.pop("DAY4_LOCK_HELD", None)  # the batch 2b mechanism this batch replaces

    def _run(self, *cmd: str, env: dict | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, SCRIPT, "run", "--", *cmd],
            capture_output=True, text=True, timeout=timeout, env=env if env is not None else self.env,
        )

    def _spawn_holder(self, ready_marker: str, sleep_seconds: float = 2.0) -> subprocess.Popen:
        """A real holder that touches `ready_marker` the instant its
        wrapped command actually starts running (i.e. after the lock
        was actually acquired), then sleeps."""
        return subprocess.Popen(
            [sys.executable, SCRIPT, "run", "--", "sh", "-c", f"touch {ready_marker}; sleep {sleep_seconds}"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_independent_contention_fails_before_running_its_command(self):
        ready = os.path.join(self.tmpdir.name, "holder-ready.marker")
        second_ran = os.path.join(self.tmpdir.name, "second-ran.marker")
        holder = self._spawn_holder(ready, sleep_seconds=2.0)
        try:
            _wait_for_file(ready)
            second = self._run("sh", "-c", f"touch {second_ran}")
        finally:
            _reap(holder)

        self.assertNotEqual(second.returncode, 0, "a second independent invocation must fail while the lock is held")
        self.assertFalse(os.path.exists(second_ran), "the second invocation's wrapped command must never have run at all")
        self.assertIn("could not acquire", second.stderr)

    def test_forged_lock_fd_without_valid_ownership_does_not_bypass_active_holder(self):
        """DAY5 batch 2c: replaces the batch-2b test that treated an
        unrelated process setting `DAY4_LOCK_HELD=1` as proof of
        successful recursive ownership. A forged `DAY5_LOCK_FD` (an
        arbitrary number, not an actually-inherited duplicate of the
        real holder's fd) must be REJECTED by verification and fall
        through to a real acquire attempt - which must then correctly
        fail while a genuine holder is active, exactly like any other
        independent invocation. This is the opposite property from
        what batch 2b's test proved."""
        ready = os.path.join(self.tmpdir.name, "holder-ready.marker")
        forged_ran = os.path.join(self.tmpdir.name, "forged-ran.marker")
        holder = self._spawn_holder(ready, sleep_seconds=2.0)
        try:
            _wait_for_file(ready)
            forged_env = dict(self.env)
            forged_env["DAY5_LOCK_FD"] = "99999"  # not a real, inherited fd in this process
            forged = self._run("sh", "-c", f"touch {forged_ran}", env=forged_env)
        finally:
            _reap(holder)

        self.assertNotEqual(forged.returncode, 0, "a forged DAY5_LOCK_FD must never bypass a real active holder")
        self.assertFalse(os.path.exists(forged_ran), "the forged invocation's wrapped command must never have run")

    def test_independently_opened_fd_on_same_path_genuinely_contends_and_fails(self):
        """DAY5 batch 2c (test-engineer review): `_verify_inherited_fd()`
        has two independent rejection gates - (a) the fd doesn't even
        resolve via `/proc/self/fd`, and (b) it resolves to the CORRECT
        lock path but is an INDEPENDENT `open()`, not an actually
        inherited duplicate of a real holder's open file description.
        The forged-fd tests above only exercise gate (a) (an fd number
        that isn't open at all). This exercises gate (b) directly: a
        genuinely open fd, on the exact right path, that must still be
        rejected because a non-blocking `flock()` on it truly contends
        with the active real holder and fails - proving path-matching
        alone is never sufficient, only a real shared lock is."""
        ready = os.path.join(self.tmpdir.name, "holder-ready.marker")
        contender_ran = os.path.join(self.tmpdir.name, "contender-ran.marker")
        holder = self._spawn_holder(ready, sleep_seconds=2.0)

        # A small helper process that independently os.open()s the
        # SAME lock file (never via pass_fds/fork inheritance from the
        # real holder), makes that fd inheritable across its own exec,
        # and claims it via DAY5_LOCK_FD before invoking day5_lock.py.
        helper_script = os.path.join(self.tmpdir.name, "open_independent_fd.py")
        with open(helper_script, "w") as f:
            f.write(
                "import os, sys\n"
                f"fd = os.open({self.lock_path!r}, os.O_RDWR)\n"
                "os.set_inheritable(fd, True)\n"
                "env = dict(os.environ)\n"
                "env['DAY5_LOCK_FD'] = str(fd)\n"
                f"os.execve(sys.executable, [sys.executable, {SCRIPT!r}, 'run', '--', 'sh', '-c', 'touch {contender_ran}'], env)\n"
            )

        try:
            _wait_for_file(ready)
            contender = subprocess.run(
                [sys.executable, helper_script], capture_output=True, text=True, timeout=10, env=self.env,
            )
        finally:
            _reap(holder)

        self.assertNotEqual(
            contender.returncode, 0,
            "an independently-opened fd on the same lock path must genuinely contend with, never bypass, an active real holder",
        )
        self.assertFalse(os.path.exists(contender_ran), "the contender's wrapped command must never have run")

    def test_forged_lock_fd_when_lock_is_actually_free_just_acquires_normally(self):
        """A forged/stale `DAY5_LOCK_FD` when NOTHING actually holds the
        lock is not a security issue (there is no active holder to
        bypass) - verification correctly fails, and the process simply
        falls through to a normal, real acquisition."""
        env = dict(self.env)
        env["DAY5_LOCK_FD"] = "99999"
        result = self._run("true", env=env)
        self.assertEqual(result.returncode, 0)

    def test_genuine_nested_wrapper_execution_runs_without_reacquiring(self):
        """Mirrors the actual Makefile nesting shape (`$(DAY5_LOCK) sh
        -c '... && $(MAKE) X && ...'`, where X's own recipe line is
        ANOTHER `$(DAY5_LOCK) ...` invocation): a real outer `day5_lock.py
        run -- sh -c '...'` whose shell body itself invokes
        `day5_lock.py run -- ...` again. The nested invocation must
        verify the inherited fd and run directly - proven here by a
        readiness marker the NESTED command itself touches, which could
        only exist if the nested invocation actually ran its wrapped
        command (rather than deadlocking or being refused)."""
        nested_ran = os.path.join(self.tmpdir.name, "nested-ran.marker")
        result = subprocess.run(
            [
                sys.executable, SCRIPT, "run", "--", "sh", "-c",
                f'"{sys.executable}" "{SCRIPT}" run -- sh -c "touch {nested_ran}"',
            ],
            capture_output=True, text=True, timeout=10, env=self.env,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")
        self.assertTrue(os.path.exists(nested_ran), "the nested invocation must have actually run its wrapped command")

    def test_wrapper_termination_while_child_still_active_keeps_exclusion(self):
        """DAY5 batch 2c: the core Bug-B fix. Killing ONLY the wrapper
        process (never its child) must NOT release exclusion while the
        child - which inherited its own reference to the same locked
        file descriptor - keeps running. A second, fully independent
        invocation attempted while that orphaned child is still alive
        must still fail; only once the child itself finishes does the
        lock become free again."""
        ready = os.path.join(self.tmpdir.name, "child-ready.marker")
        wrapper = subprocess.Popen(
            [sys.executable, SCRIPT, "run", "--", "sh", "-c", f"touch {ready}; sleep 3"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_file(ready)  # the child (sh -c ...) is now genuinely running
            os.kill(wrapper.pid, signal.SIGKILL)  # kill ONLY the day5_lock.py wrapper
            wrapper.wait(timeout=5)  # reap the wrapper itself (test-owned)

            # The orphaned "sh -c ... sleep 3" child should still be
            # alive and still holding the lock via its own inherited fd.
            during = self._run("true")
            self.assertNotEqual(during.returncode, 0, "exclusion must remain effective while the orphaned child is still running")

            # Wait out the orphaned child's sleep (it is not this test's
            # direct subprocess, so there is nothing to reap for it -
            # its own parent, PID 1 after reparenting, does that).
            time.sleep(3.2)
            after = self._run("true")
            self.assertEqual(after.returncode, 0, "the lock must be free again once the orphaned child itself has exited")
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)

    def test_lock_released_after_child_completes_normally(self):
        holder = self._run("true")
        self.assertEqual(holder.returncode, 0)

        later = self._run("true")
        self.assertEqual(later.returncode, 0, "the lock must be free again after the first holder's normal exit")

    def test_nonzero_command_exit_still_releases_lock(self):
        first = self._run("false")
        self.assertNotEqual(first.returncode, 0)

        second = self._run("true")
        self.assertEqual(second.returncode, 0, "a failing wrapped command must still release the lock on exit")

    def test_lock_is_scoped_to_the_lock_path_not_global(self):
        """Two DIFFERENT lock paths never contend with each other -
        proves the lock is scoped, not a single hardcoded global that
        would spuriously serialize unrelated tests/targets."""
        ready = os.path.join(self.tmpdir.name, "holder-ready.marker")
        other_env = dict(self.env)
        other_env["DAY5_LOCK_PATH"] = os.path.join(self.tmpdir.name, "different.lock")

        holder = self._spawn_holder(ready, sleep_seconds=1.5)
        try:
            _wait_for_file(ready)
            other = self._run("true", env=other_env)
        finally:
            _reap(holder)

        self.assertEqual(other.returncode, 0, "a different lock path must not contend with an unrelated holder")


if __name__ == "__main__":
    unittest.main()

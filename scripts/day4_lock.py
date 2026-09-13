#!/usr/bin/env python3
"""
DAY4 batch 2b/2c: minimal, dependency-free process-held local mutual
exclusion for this project's single Day 4 mutation target (one kind
cluster, one local operator). Not a general orchestration framework -
one `fcntl.flock()` on one fixed lock file, wrapping a caller-supplied
command.

Unique per-run artifacts (e.g. `scripts/suite_baseline.py`'s run-
specific baseline file) do NOT prevent two independent, concurrent
invocations from mutating the same live cluster at once - each would
happily generate its own distinct run ID/baseline path and neither
would ever notice the other. This module is the actual mutual-
exclusion primitive that closes that gap; it is deliberately
independent of the suite baseline (neither reads nor writes the
other's file, and the lock file path is fixed/shared across ALL runs -
one lock guards one target - while the baseline path is unique per
run).

DAY4 batch 2c: replaces batch 2b's bare `DAY4_LOCK_HELD=1` boolean
trust mechanism, which had two reproduced defects:

  1. An UNRELATED process that merely had `DAY4_LOCK_HELD=1` set in
     its own environment (for any reason) bypassed acquisition
     entirely, even while a real, unrelated holder was active - a
     flag has no way to prove it was actually inherited FROM that
     holder.
  2. Killing only the wrapper process (the one that called
     `os.open()`+`flock()`) released the OS-level lock immediately
     (its fd closes on process death), even while the CHILD it had
     spawned - the actual protected mutation - kept running. A second
     invocation could then acquire the "free" lock and race the still-
     running child.

Fix - verifiable inherited ownership via a shared, inherited file
descriptor, not a claimed boolean:

`flock()` locks are associated with the OPEN FILE DESCRIPTION the fd
refers to, never with a process or an fd number (see `flock(2)`: file
descriptors duplicated via `fork()`/`dup()` refer to the SAME lock and
may be used interchangeably to hold or release it; two INDEPENDENT
`open()` calls on the same path, even in the same process, are
different locks and genuinely contend). `subprocess.Popen`'s
`pass_fds` keeps a specific fd open and inheritable across the
fork+exec into a child; neither bash nor GNU Make close arbitrary
inherited fds > 2 by default, so a passed fd survives transparently
through every layer this project's Makefile nests through (a top-level
`sh -c '... && $(MAKE) X && ...'` body, `make`'s own recipe sub-shell
for `X`, and that recipe's own nested `day4_lock.py run` invocation).

So: the ACTUAL acquirer opens the lock file, `flock()`s it, and runs
its wrapped command with that exact fd passed through
(`pass_fds=(fd,)`) and its number exported as `DAY4_LOCK_FD`. A
process that sees `DAY4_LOCK_FD` set must never trust it at face
value - it VERIFIES it: (a) `/proc/self/fd/<n>` must resolve to this
module's own lock path (rules out an unrelated process that happens to
have some OTHER fd open under that same number, or that fabricated the
variable), and (b) a non-blocking `flock(LOCK_EX)` on that exact fd
must succeed (trivially true for a genuinely inherited duplicate of an
already-held lock; a coincidental INDEPENDENT open of the same path by
an unrelated process would genuinely contend and fail here while a
real holder is active, so it cannot be mistaken for inheritance).

Lifetime: only the process that performed the original `open()` +
`flock(LOCK_EX|LOCK_NB)` ever calls `flock(LOCK_UN)`/`os.close()` on
that fd, and only in ITS OWN `finally`, reached only after its
directly-wrapped child has already exited. A process running under
VERIFIED inherited ownership never unlocks or closes the shared fd
itself - it only passes it further down and lets normal process exit
reclaim its own reference. This is what keeps exclusion effective even
if an ancestor wrapper is killed outright: the descendant that
actually inherited the fd still holds a live reference to the SAME
open file description, so the advisory lock is not released until
EVERY process holding a reference to it - including that descendant -
has exited, never merely when one ancestor's process happens to die.

Recursive `make` ownership, concretely: `make day4-check`'s recipe is
`$(DAY4_LOCK) sh -c '$(MAKE) tool-check && ... && $(MAKE)
storage-bootstrap && ...'`. The `day4_lock.py run` invocation there is
the real acquirer; its direct child is the `sh -c` shell, which
inherits the fd and `DAY4_LOCK_FD`. That shell's own `make
storage-bootstrap` inherits both unchanged (make/bash do not strip
them), and `storage-bootstrap`'s own recipe line
(`$(DAY4_LOCK) python3 scripts/storage_bootstrap.py`) spawns yet
another `day4_lock.py run` - which sees `DAY4_LOCK_FD` already set,
verifies it against the real lock file, confirms the probe flock
succeeds, and runs `storage_bootstrap.py` directly under that SAME
inherited fd - no new acquire attempt, no contention, no deadlock.
Should the fd fail to verify for any reason (a broken assumption
somewhere in that chain), the process falls through to a REAL acquire
attempt instead of silently proceeding unprotected - if the actual
ancestor lock is genuinely still held, that attempt correctly fails
loudly (a visible nonzero exit for that recipe step) rather than
racing it silently.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys

FD_ENV = "DAY4_LOCK_FD"
LOCK_PATH_ENV = "DAY4_LOCK_PATH"
DEFAULT_LOCK_PATH = "/tmp/maops-day4-mutation.lock"


def _lock_path() -> str:
    return os.environ.get(LOCK_PATH_ENV) or DEFAULT_LOCK_PATH


def _verify_inherited_fd() -> int | None:
    """Returns a verified, live, inherited lock fd - or None if
    `DAY4_LOCK_FD` is unset, malformed, stale, or does not actually
    prove inherited ownership of THIS module's lock file. Never raises;
    never trusts the env var's mere presence."""
    raw = os.environ.get(FD_ENV)
    if not raw:
        return None
    try:
        fd = int(raw)
    except ValueError:
        return None
    try:
        actual_target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None  # fd not open in this process at all - not genuinely inherited
    try:
        matches = os.path.realpath(actual_target) == os.path.realpath(_lock_path())
    except OSError:
        return None
    if not matches:
        return None  # points somewhere else entirely - never our lock
    try:
        # A genuinely inherited duplicate of an already-held lock
        # succeeds here trivially (same open file description, not a
        # new contending one). An independent open of the same path by
        # an unrelated process would genuinely contend and fail while
        # a real holder is active, so this cannot be spoofed by
        # coincidence.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fd


def _run_child(argv: list[str], fd: int) -> int:
    env = dict(os.environ)
    env[FD_ENV] = str(fd)
    return subprocess.run(argv, env=env, pass_fds=(fd,)).returncode


def run(argv: list[str]) -> int:
    """Runs `argv` (a real subprocess argv list, never a shell string)
    under the Day 4 mutation lock.

    If `DAY4_LOCK_FD` verifiably refers to an already-held instance of
    this exact lock (see `_verify_inherited_fd()` - never a bare
    boolean check), the command runs directly under that SAME fd, with
    no new acquire attempt and no unlock/close of the shared
    descriptor - this is intentional, verified, transitive ownership
    within one invocation tree, never a bypass usable by an unrelated
    process (which can never produce a verifying fd for a lock it does
    not actually hold a reference to).

    Otherwise, attempts a non-blocking exclusive `flock()` on the fixed
    lock path. On failure (another holder is active), prints a clear
    message and returns nonzero WITHOUT running `argv` at all - the
    lock is acquired BEFORE mutation, and a second independent
    invocation fails before any mutation is attempted, not partway
    through one. On success, passes the lock's own fd through to the
    wrapped child (`pass_fds`) so exclusion remains effective even if
    THIS process is killed outright while the child continues (the
    child holds its own reference to the same open file description);
    only after the directly-wrapped child has exited does this
    process's own `finally` release the lock (`try/finally` cannot
    observe or protect against this process itself being SIGKILLed
    before reaching `finally` - the OS still reclaims the fd then, same
    as any other resource; the guarantee here is against KeyboardInterrupt,
    normal exceptions, and a nonzero/zero wrapped-command exit, not
    against SIGKILL of the holder itself).

    Documented boundary (DAY4 batch 2c specialist review): the
    directly-wrapped child holding its own reference to the fd is what
    keeps exclusion effective if the WRAPPER dies - this does NOT
    extend to a wrapped command that itself voluntarily backgrounds/
    detaches a grandchild process which inherits the fd and outlives
    its own direct parent. If the directly-wrapped command exits (even
    normally) while such a self-detached descendant is still running,
    this process's own `finally` still releases the lock once ITS
    `subprocess.run()` call returns, regardless of that descendant.
    Every mutating step in this project's Makefile is a synchronous
    foreground command (no wrapped step self-backgrounds work), so this
    does not arise in practice here - it is called out for anyone
    reusing this module elsewhere."""
    inherited_fd = _verify_inherited_fd()
    if inherited_fd is not None:
        return _run_child(argv, inherited_fd)

    path = _lock_path()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(
                f"FAIL: could not acquire the Day 4 mutation lock at {path!r} - "
                "another make/script invocation against this project's Day 4 target is already running "
                "(refusing to start before any mutation is attempted)",
                file=sys.stderr,
            )
            return 1
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass  # diagnostic only - never fatal to holding the lock
        return _run_child(argv, fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "run" or sys.argv[2] != "--":
        print("usage: day4_lock.py run -- <command> [args...]", file=sys.stderr)
        return 2
    return run(sys.argv[3:])


if __name__ == "__main__":
    raise SystemExit(main())

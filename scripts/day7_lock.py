#!/usr/bin/env python3
"""
DAY7: the Day 7 mutation lock - an independent lock for the isolated
`maops-k8s-day7` cluster, reusing Day 6's verified-inherited-fd
mechanism (scripts/day6_lock.py - see its module docstring for the full
design) without copying it.

The Day 7 lock differs from Day 6's only in its identifiers:
`DAY7_LOCK_FD`, `DAY7_LOCK_PATH`, and the lock file
/tmp/maops-day7-mutation.lock. A Day 6 run and a Day 7 run therefore
never contend with, or inherit, each other's lock: an inherited
`DAY6_LOCK_FD` is never consulted here, and a `DAY7_LOCK_FD` is
verified against the Day 7 lock path only (day6_lock's
`_verify_inherited_fd()` resolves `/proc/self/fd/<n>` and compares it
with the active lock path before trusting it).

The lock file itself is a zero-secret coordination file (it holds only
the holder's PID), so /tmp is acceptable for it; Day 7 EVIDENCE lives
in the private run directory instead (scripts/private_run_dir.py).

Known cosmetic limitation: day6_lock.py is deliberately left unchanged,
so its contention message still reads "could not acquire the Day 6
mutation lock at <path>" - the <path> it prints is the Day 7 lock file.

Usage: day7_lock.py run -- <command> [args...]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day6_lock

FD_ENV = "DAY7_LOCK_FD"
LOCK_PATH_ENV = "DAY7_LOCK_PATH"
DEFAULT_LOCK_PATH = "/tmp/maops-day7-mutation.lock"


def configure() -> None:
    """Points day6_lock's module-level identifiers at the Day 7 ones.
    day6_lock reads them at CALL time (never at import time), so this is
    sufficient; it is called before any lock operation in this process
    and only ever in a Day 7 lock process."""
    day6_lock.FD_ENV = FD_ENV
    day6_lock.LOCK_PATH_ENV = LOCK_PATH_ENV
    day6_lock.DEFAULT_LOCK_PATH = DEFAULT_LOCK_PATH


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "run" or sys.argv[2] != "--":
        print("usage: day7_lock.py run -- <command> [args...]", file=sys.stderr)
        return 2
    configure()
    return day6_lock.run(sys.argv[3:])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY7: private, per-run evidence directory for the Day 7 baselines.

Day 6's suite baseline defaulted to a /tmp path, which does not survive
a host reboot and is world-listable. Day 7 keeps every baseline for one
run in ONE directory:

    $HOME/.local/state/maops-kubernetes-platform/day7-runs/<DAY7_RUN_ID>/

and enforces, rather than assumes, that it is private:

  - the run directory is under the invoking user's home directory, and
    NOT under /tmp (or /var/tmp, /dev/shm) and NOT inside this
    repository (evidence never lands in a git working tree);
  - it is a real directory (not a symlink), owned by the current uid,
    with mode exactly 0700;
  - it is created exclusively (`os.mkdir` on a path that must not yet
    exist) - an existing run directory is never reused, so a repeated
    run ID can never overwrite or silently extend an earlier run's
    evidence;
  - every baseline file inside it is created O_EXCL with mode 0600 by
    its writer (scripts/suite_baseline.py, scripts/day7_baseline.py)
    and is re-checked (regular file, owner, mode exactly 0600) by
    `validate_private_file()` before it is ever trusted.

Never reads or prints Secret contents; this module only handles paths
and file metadata.

Usage (Makefile `day7-baseline-init`):
    python3 scripts/private_run_dir.py init
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ID_ENV = "DAY7_RUN_ID"
RUN_DIR_ENV = "DAY7_BASELINE_DIR"
DIR_MODE = 0o700
FILE_MODE = 0o600
FORBIDDEN_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")


class PrivateRunDirError(Exception):
    """Any violation of the private run-directory contract."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def check_location(path: str | os.PathLike, home: Path | None = None, repo_root: Path = REPO_ROOT, forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS) -> Path:
    """Pure location rules (no filesystem mode checks): absolute, under
    `home`, not under a temporary root, not inside the repository.
    Returns the resolved path."""
    raw = Path(path)
    if not raw.is_absolute():
        raise PrivateRunDirError(f"run directory {str(raw)!r} must be an absolute path")
    resolved = Path(os.path.realpath(raw))
    home_resolved = Path(os.path.realpath(home or Path.home()))
    for forbidden in forbidden_roots:
        if _is_within(resolved, Path(os.path.realpath(forbidden))):
            raise PrivateRunDirError(f"run directory {str(resolved)!r} is under {forbidden} - Day 7 evidence must live outside temporary storage")
    if _is_within(resolved, Path(os.path.realpath(repo_root))):
        raise PrivateRunDirError(f"run directory {str(resolved)!r} is inside the repository - evidence must never land in the working tree")
    if not _is_within(resolved, home_resolved) or resolved == home_resolved:
        raise PrivateRunDirError(f"run directory {str(resolved)!r} is not below the user's home directory {str(home_resolved)!r}")
    return resolved


def validate_run_dir(path: str | os.PathLike, home: Path | None = None, repo_root: Path = REPO_ROOT, uid: int | None = None, forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS) -> Path:
    """Location rules plus: exists, real directory (not a symlink),
    owned by `uid`, mode exactly 0700."""
    resolved = check_location(path, home=home, repo_root=repo_root, forbidden_roots=forbidden_roots)
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise PrivateRunDirError(f"run directory {str(path)!r} does not exist or is unreadable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PrivateRunDirError(f"run directory {str(path)!r} is not a real directory")
    expected_uid = os.getuid() if uid is None else uid
    if st.st_uid != expected_uid:
        raise PrivateRunDirError(f"run directory {str(path)!r} is owned by uid {st.st_uid}, expected {expected_uid}")
    mode = stat.S_IMODE(st.st_mode)
    if mode != DIR_MODE:
        raise PrivateRunDirError(f"run directory {str(path)!r} has mode {oct(mode)}, expected {oct(DIR_MODE)}")
    return resolved


def validate_private_file(path: str | os.PathLike, uid: int | None = None, home: Path | None = None, forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS) -> None:
    """A baseline file is trusted only if it is a regular file (not a
    symlink) owned by `uid` with mode exactly 0600, inside a directory
    that itself passes `validate_run_dir()`."""
    validate_run_dir(Path(path).parent, uid=uid, home=home, forbidden_roots=forbidden_roots)
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise PrivateRunDirError(f"baseline file {str(path)!r} not found or unreadable: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise PrivateRunDirError(f"baseline file {str(path)!r} is not a regular file")
    expected_uid = os.getuid() if uid is None else uid
    if st.st_uid != expected_uid:
        raise PrivateRunDirError(f"baseline file {str(path)!r} is owned by uid {st.st_uid}, expected {expected_uid}")
    mode = stat.S_IMODE(st.st_mode)
    if mode != FILE_MODE:
        raise PrivateRunDirError(f"baseline file {str(path)!r} has mode {oct(mode)}, expected {oct(FILE_MODE)}")


def create_run_dir(path: str | os.PathLike, home: Path | None = None, repo_root: Path = REPO_ROOT, forbidden_roots: tuple[str, ...] = FORBIDDEN_ROOTS) -> Path:
    """Creates the run directory EXCLUSIVELY (FileExistsError -> refusal:
    a run directory is never reused). Missing parents are created with
    mode 0700 too. The final directory's mode is set explicitly (umask
    cannot widen or narrow it) and then re-validated."""
    check_location(path, home=home, repo_root=repo_root, forbidden_roots=forbidden_roots)
    target = Path(path)
    for parent in reversed(target.parents):
        if not parent.exists():
            os.mkdir(parent, DIR_MODE)
            os.chmod(parent, DIR_MODE)
    try:
        os.mkdir(target, DIR_MODE)
    except FileExistsError as exc:
        raise PrivateRunDirError(
            f"run directory {str(target)!r} already exists - refusing to reuse it (a run ID is single-use; "
            "remove it manually only if you are certain it holds no evidence you need)"
        ) from exc
    os.chmod(target, DIR_MODE)
    return validate_run_dir(target, home=home, repo_root=repo_root, forbidden_roots=forbidden_roots)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "init":
        print("usage: private_run_dir.py init", file=sys.stderr)
        return 2
    run_id = os.environ.get(RUN_ID_ENV)
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_id or not run_dir:
        print(f"FAIL: {RUN_ID_ENV} and {RUN_DIR_ENV} must both be set (the Makefile's Day 7 targets export them)", file=sys.stderr)
        return 1
    if Path(run_dir).name != run_id:
        print(f"FAIL: {RUN_DIR_ENV}={run_dir!r} does not end in this run's ID {run_id!r}", file=sys.stderr)
        return 1
    try:
        resolved = create_run_dir(run_dir)
    except (PrivateRunDirError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: created private Day 7 run directory {resolved} (mode 0700, run ID {run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

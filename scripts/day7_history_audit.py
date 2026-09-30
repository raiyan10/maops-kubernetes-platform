#!/usr/bin/env python3
"""
DAY7: OPTIONAL history audit - never part of `day7-check`,
`day7-final-gate`, or any restoration verdict.

Day 7's operational gates must pass on a healthy Day 7 cluster whether
or not older kind clusters are running. Historical integrity is proven
instead from Git and static files; older-cluster existence is reported
here only, for whoever wants it.

Git/static (cluster-free):
  - `v0.6.0` still peels to the released commit 19d6b28b...;
  - the frozen Day 1-6 SOURCES are identical to `v0.6.0` (k8s/base,
    k8s/day6, kind/cluster{,-day5,-day6}.yaml, the Day 4/5/6 lock
    scripts);
  - the Day 1-6 historical RECORDS (docs/engineering-reviews,
    docs/images, docs/evidence) are identical to the post-release commit
    74832c41... (PR #9 added the Day 6 post-release record and its two
    screenshots after the tag - so the tag is the wrong reference for
    the records, and the post-release main commit is the right one);
  - the workload sources (app/, gateway/, state/ - server code and
    Dockerfiles) are identical to `v0.6.0`: the 0.7.0 images are
    REBUILDS of unchanged application code, and the gateway candidate
    is a configuration variant of that same image - evidence against
    ever describing 0.7.0 as a new application binary.
Older clusters (existence only): `kind get clusters` must list
maops-k8s-day1 ... maops-k8s-day6. No API server is contacted, and a
missing/stopped older cluster fails only this optional audit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
V060_TAG = "v0.6.0"
V060_COMMIT = "19d6b28b1282aedc8417a5a2ba10e74614afe244"
POST_RELEASE_COMMIT = "74832c41a35a04d905aaa203e22f7d06bb7eb4e9"
FROZEN_PATHS = (
    "k8s/base", "k8s/day6", "kind/cluster.yaml", "kind/cluster-day5.yaml", "kind/cluster-day6.yaml",
    "scripts/day4_lock.py", "scripts/day5_lock.py", "scripts/day6_lock.py",
)
HISTORICAL_RECORD_PATHS = ("docs/engineering-reviews", "docs/images", "docs/evidence")
WORKLOAD_PATHS = ("app", "gateway", "state")
OLDER_CLUSTERS = tuple(f"maops-k8s-day{n}" for n in range(1, 7))

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((bool(ok), message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return bool(ok)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_checks() -> None:
    peeled = _run(["git", "rev-parse", f"{V060_TAG}^{{commit}}"])
    record(peeled is not None and peeled.returncode == 0 and peeled.stdout.strip() == V060_COMMIT, f"{V060_TAG} peels to {V060_COMMIT} (found {None if peeled is None else peeled.stdout.strip() or peeled.stderr.strip()})")
    for label, ref, paths in (
        ("frozen Day 1-6 sources", V060_TAG, FROZEN_PATHS),
        ("Day 1-6 historical records", POST_RELEASE_COMMIT, HISTORICAL_RECORD_PATHS),
        ("workload sources (app/gateway/state)", V060_TAG, WORKLOAD_PATHS),
    ):
        diff = _run(["git", "diff", "--quiet", ref, "--", *paths])
        untracked = _run(["git", "status", "--porcelain", "--", *paths])
        clean = diff is not None and diff.returncode == 0 and untracked is not None and untracked.stdout.strip() == ""
        record(clean, f"{label} identical to {ref[:12]} in the working tree")


def older_clusters(listing: str) -> list[str]:
    """Pure: which older clusters are missing from a `kind get clusters` listing."""
    present = set(listing.split())
    return [c for c in OLDER_CLUSTERS if c not in present]


def main() -> int:
    print("# Day 7 OPTIONAL history audit (not part of any Day 7 gate)")
    git_checks()
    listing = _run(["kind", "get", "clusters"])
    if listing is None or listing.returncode != 0:
        record(False, "could not list kind clusters")
    else:
        missing = older_clusters(listing.stdout)
        record(not missing, f"older kind clusters listed (existence only, never contacted); missing: {missing}")
    failures = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} history audit checks passed")
    print("(optional audit - its result never affects Day 7 workload restoration)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

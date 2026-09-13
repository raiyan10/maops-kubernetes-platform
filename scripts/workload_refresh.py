#!/usr/bin/env python3
"""
Day 4 batch 3 remediation: explicit workload-refresh step.

Local kind image loading (`kind load docker-image ...`, under the
existing `<name>:0.4.0` mutable tag naming - no registry, no Helm, no
new image-management framework introduced here) makes a freshly built
image available in every node's containerd content store, but does NOT
by itself cause any ALREADY-RUNNING Pod to use it: Kubernetes only
(re)resolves an image reference when a Pod is actually (re)created.
Batch 3's live day4-check run proved this concretely: `deploy`'s
`kubectl apply -k` happened to change gateway's ConfigMap/Deployment
enough to trigger a real rolling update (so gateway picked up the
freshly loaded image), but `maops-app`'s Deployment and `maops-state`'s
StatefulSet pod-template hashes did not change, so their already-running
Pods kept running the previous build even though `kubectl apply`
reported those objects "configured" and the node's `:0.4.0` tag now
pointed at new content.

This script closes that gap explicitly and unconditionally: after
`deploy` has applied the current manifests, it forces exactly the three
Day 4 workloads to restart through their own controllers -
`kubectl rollout restart` (never a Pod delete, never a manual
scale-to-0/back-up, never touching any PVC/PV/Secret) - so every
workload is guaranteed to be running whatever image this run's
`image-load` step just placed on the nodes, regardless of whether the
manifest diff alone would have triggered it.

Order matters and is enforced strictly sequentially, never in parallel:
state first (the dependency both app and gateway build on), then app,
then gateway. Each restart is followed by a bounded wait for that exact
workload's rollout to fully converge (`kubectl rollout status`,
generation-aware, same bounded-subprocess-timeout pattern as
DAY3-INT-H2 / cluster_check.wait_for_rollout_complete) before the next
workload is touched at all. A restart or convergence failure on one
workload stops the sequence before any later workload is touched -
exactly like the Makefile's own `&&`-chained day4-check sequence, so a
partial/inconsistent refresh is never silently treated as done.

Intentional, documented trade-off (NOT a defect): `maops-state` is a
single-replica StatefulSet, so restarting it causes a brief, real
outage of `/state` (the old Pod terminates before the new one becomes
Ready). This is a deliberate, accepted local-image-refresh cost for
this Day 4 release, not a zero-downtime deployment claim. `maops-app`/
`maops-gateway` run 3 replicas each behind a PodDisruptionBudget
(minAvailable=2), so their own rolling restarts stay within that
existing budget.

PVC/PV and both Secrets are never touched here - `kubectl rollout
restart` only replaces Pods through the owning controller; it neither
deletes nor recreates any PersistentVolumeClaim, PersistentVolume, or
Secret, and never rotates a token.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import APP_DEPLOYMENT, GATEWAY_DEPLOYMENT, NAMESPACE, STATE_STATEFULSET, run

ROLLOUT_TIMEOUT_SECONDS = 120

# (component label, kubectl resource kind, resource name) - order is the
# actual dependency chain (state -> app -> gateway), and this list's
# order IS the enforced restart order below, not merely documentation.
WORKLOADS = [
    ("state", "statefulset", STATE_STATEFULSET),
    ("app", "deployment", APP_DEPLOYMENT),
    ("gateway", "deployment", GATEWAY_DEPLOYMENT),
]

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _stderr_detail(exc: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> str:
    stderr = getattr(exc, "stderr", None)
    if stderr:
        stderr = stderr.strip() if isinstance(stderr, str) else stderr.decode(errors="replace").strip()
        if stderr:
            return stderr
    return str(exc)


def restart(kind: str, name: str) -> bool:
    try:
        run("-n", NAMESPACE, "rollout", "restart", f"{kind}/{name}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return record(False, f"{kind}/{name}: kubectl rollout restart failed: {_stderr_detail(exc)}")
    return record(True, f"{kind}/{name}: rollout restart triggered")


def wait_rollout(kind: str, name: str, timeout_seconds: int = ROLLOUT_TIMEOUT_SECONDS) -> bool:
    """Bounded exactly like cluster_check.wait_for_rollout_complete
    (DAY3-INT-H2 pattern): the subprocess-level timeout always exceeds
    the Kubernetes-side `--timeout=<n>s`, so a hung kubectl and a
    genuinely stuck/progress-deadline-exceeded rollout both become an
    ordinary recorded failure here, never an uncaught exception and
    never a silent pass."""
    try:
        result = run(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"{kind}/{name}",
            f"--timeout={timeout_seconds}s",
            check=False,
            timeout=kube.subprocess_timeout_for(timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        return record(False, f"{kind}/{name}: kubectl rollout status subprocess timed out: {_stderr_detail(exc)}")
    output = (result.stdout or "").strip() + (("\n" + result.stderr.strip()) if result.stderr else "")
    return record(result.returncode == 0, f"{kind}/{name}: rollout status: {output.strip()!r}")


def refresh_workload(kind: str, name: str) -> bool:
    return restart(kind, name) and wait_rollout(kind, name)


def main() -> int:
    print(f"# Day 4 workload refresh (state -> app -> gateway) against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    print(
        "NOTE: this is an intentional local-image refresh (kubectl rollout restart "
        "through each workload's own controller), not a zero-downtime deployment - "
        "maops-state is single-replica and has a brief real outage while its one Pod "
        "is replaced. app/gateway stay within their existing PodDisruptionBudget."
    )

    for _component, kind, name in WORKLOADS:
        if not refresh_workload(kind, name):
            print(
                f"FAIL: {kind}/{name} refresh did not converge - stopping before touching any later workload "
                "in the state -> app -> gateway chain",
                file=sys.stderr,
            )
            break

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} workload refresh checks passed")
    if failures:
        print(f"FAIL: {len(failures)} workload refresh check(s) failed", file=sys.stderr)
        return 1
    print("PASS: state -> app -> gateway all refreshed onto this run's freshly loaded images and reconverged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY8: shared plumbing for the Day 8 live scripts.

Day 8 runs on the EXISTING `maops-k8s-day7` cluster (it is the platform
v1.0.0 is built on), so it uses the `day7` cluster profile, the Day 7
mutation lock and the Day 7 kubeconfig - but never a Day 7 baseline,
build record or Helm stage. The rules carried over from Day 7:

  - every function here that can reach a cluster refuses on its own
    outside the day7 profile (`require_cluster_profile()` runs before any
    kubectl/helm/docker-exec process is started), so not even an
    insufficiently mocked unit test can reach the Day 6 cluster;
  - evidence lives in ONE private run directory per run,
        $HOME/.local/state/maops-kubernetes-platform/day8-runs/<DAY8_RUN_ID>/
    (0700, created exclusively, never reused; each file O_EXCL 0600,
    never overwritten - scripts/private_run_dir.py enforces the rules).

Never reads or prints Secret contents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import private_run_dir

CLUSTER_PROFILE = "day7"
RUN_ID_ENV = "DAY8_RUN_ID"
RUN_DIR_ENV = "DAY8_RUN_DIR"
DEFAULT_RUNS_ROOT = Path.home() / ".local" / "state" / "maops-kubernetes-platform" / "day8-runs"
IMAGE_RECORD = "scaling-image.json"
STABLE_BASELINE = "stable-baseline.json"

# Add-on releases (pinned; the Makefile carries the same values and the
# static check cross-verifies them - see scripts/day8_addons.py).
ADDONS = {
    "metrics-server": {"namespace": "kube-system", "chart_version": "3.14.0", "app_version": "0.9.0"},
    "vertical-pod-autoscaler": {"namespace": "vpa-system", "chart_version": "0.13.0", "app_version": "1.8.0"},
    "keda": {"namespace": "keda", "chart_version": "2.21.0", "app_version": "2.21.0"},
}


class Day8Error(Exception):
    """A Day 8 contract violation (wrong profile, unsafe state, bad evidence)."""


def require_cluster_profile() -> None:
    if kube.PROFILE != CLUSTER_PROFILE:
        raise Day8Error(
            f"Day 8 tooling runs only on the maops-k8s-day7 cluster (profile {CLUSTER_PROFILE!r}); "
            f"refusing under profile {kube.PROFILE!r} (context {kube.CONTEXT}) - it never touches the Day 6 cluster"
        )


def kubectl(*args: str, check: bool = True, timeout: float = kube.DEFAULT_TIMEOUT_SECONDS, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Every Day 8 kubectl call goes through here (guard first)."""
    require_cluster_profile()
    if stdin is None:
        return kube.run(*args, check=check, timeout=timeout)
    cmd = ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=check, timeout=timeout)


def kubectl_json(*args: str, timeout: float = kube.DEFAULT_TIMEOUT_SECONDS):
    return json.loads(kubectl(*args, "-o", "json", timeout=timeout).stdout)


def kubectl_json_or_none(*args: str):
    """None only for an explicit `(NotFound)` answer (the same strict rule
    as get_state()); any other failure raises - never "absent" by accident.
    Never used for Secrets (see runtime_object_labels)."""
    result = kubectl(*args, "-o", "json", check=False)
    if result.returncode != 0:
        if "(NotFound)" in result.stderr:
            return None
        raise Day8Error(f"kubectl {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


def helm(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    require_cluster_profile()
    cmd = ["helm", *args, "--kubeconfig", kube.KUBECONFIG_PATH, "--kube-context", kube.CONTEXT]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def node_exec(node: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Read-only `docker exec` into one maops-k8s-day7 node container."""
    require_cluster_profile()
    if not kube._DAY_NODE_NAME_RE.match(node or ""):
        raise Day8Error(f"refusing to exec into {node!r}: not a {kube.CLUSTER_NAME} node")
    return subprocess.run(["docker", "exec", node, *args], capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------
# Explicit existence answers (never "unreadable means absent")
# --------------------------------------------------------------------------


def get_state(returncode: int, stderr: str) -> str:
    """Pure. A `kubectl get <kind> <name>` (or `helm status`) result ->
    "present" (exit 0) or "absent" (an explicit NotFound / release-not-found
    answer). Anything else - a timeout, an unserved API, a refused
    connection - raises: an unreadable API is never an empty answer."""
    if returncode == 0:
        return "present"
    if "(NotFound)" in stderr or "release: not found" in stderr:
        return "absent"
    raise Day8Error(f"existence could not be determined (exit {returncode}): {stderr.strip()[:240] or 'no error output'}")


def object_state(kind: str, name: str, namespace: str | None = None) -> str:
    args = (["-n", namespace] if namespace else []) + ["get", kind, name, "-o", "name"]
    try:
        r = kubectl(*args, check=False)
    except subprocess.SubprocessError as exc:
        raise Day8Error(f"kubectl get {kind} {name}: {type(exc).__name__}: {exc}") from exc
    return get_state(r.returncode, r.stderr)


def helm_release_state(name: str, namespace: str) -> str:
    r = helm("status", name, "--namespace", namespace)
    return get_state(r.returncode, r.stderr)


def helm_release_metadata(name: str, namespace: str) -> dict | None:
    """`helm get metadata -o json` (chart, version, labels, status - no
    manifest, values or Secret data). None = explicit `release: not found`."""
    r = helm("get", "metadata", name, "--namespace", namespace, "-o", "json")
    if get_state(r.returncode, r.stderr) == "absent":
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise Day8Error(f"helm get metadata {namespace}/{name}: not JSON ({exc})") from exc


# The only CRD-backed types Day 8 lists. Built-in types (HPA, LimitRange,
# ResourceQuota, ...) are never "absent": a failed list of one is a failure.
CRD_BACKED_TYPES = frozenset(
    {
        "scaledobjects.keda.sh",
        "scaledjobs.keda.sh",
        "triggerauthentications.keda.sh",
        "verticalpodautoscalers.autoscaling.k8s.io",
    }
)


def resource_type_absent(resource: str) -> bool:
    """True only for a known CRD-backed type whose CRD is explicitly
    NotFound - the one case in which "no objects of this type" is proven
    rather than assumed. Raises when the CRD's existence is unreadable."""
    if resource not in CRD_BACKED_TYPES:
        return False
    return object_state("customresourcedefinition", resource) == "absent"


# --------------------------------------------------------------------------
# Private evidence
# --------------------------------------------------------------------------


def run_dir() -> Path:
    run_id = os.environ.get(RUN_ID_ENV)
    directory = os.environ.get(RUN_DIR_ENV)
    if not run_id or not directory:
        raise Day8Error(f"{RUN_ID_ENV} and {RUN_DIR_ENV} must both be set (the Makefile's day8 targets export them)")
    if Path(directory).name != run_id:
        raise Day8Error(f"{RUN_DIR_ENV}={directory!r} does not end in this run's ID {run_id!r}")
    try:
        private_run_dir.validate_run_dir(directory)
    except private_run_dir.PrivateRunDirError as exc:
        raise Day8Error(f"{exc} - run `make day8-baseline-init` first") from exc
    return Path(directory)


def init_run_dir() -> Path:
    run_id = os.environ.get(RUN_ID_ENV)
    directory = os.environ.get(RUN_DIR_ENV)
    if not run_id or not directory or Path(directory).name != run_id:
        raise Day8Error(f"{RUN_ID_ENV}/{RUN_DIR_ENV} missing or inconsistent ({run_id!r}, {directory!r})")
    try:
        return private_run_dir.create_run_dir(directory)
    except private_run_dir.PrivateRunDirError as exc:
        raise Day8Error(str(exc)) from exc


def write_evidence(name: str, payload, directory: Path | None = None) -> Path:
    """Creates <run dir>/<name> once (O_EXCL, 0600) - evidence is never
    overwritten. JSON for dict/list payloads, text otherwise."""
    if "/" in name or name.startswith("."):
        raise Day8Error(f"invalid evidence file name {name!r}")
    directory = directory or run_dir()
    path = directory / name
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, private_run_dir.FILE_MODE)
    except FileExistsError as exc:
        raise Day8Error(f"evidence file {path} already exists - evidence is never overwritten") from exc
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, private_run_dir.FILE_MODE)
    return path


def read_evidence(name: str, directory: Path | None = None):
    directory = directory or run_dir()
    path = directory / name
    try:
        private_run_dir.validate_private_file(path)
    except private_run_dir.PrivateRunDirError as exc:
        raise Day8Error(str(exc)) from exc
    return json.loads(path.read_text())


def scaling_image(directory: Path | None = None) -> str:
    """The pinned scaling image this run recorded (scripts/day8_image.py)."""
    record = read_evidence(IMAGE_RECORD, directory)
    ref = record.get("ref")
    if not isinstance(ref, str):
        raise Day8Error(f"{IMAGE_RECORD} has no image ref")
    return ref


# --------------------------------------------------------------------------
# Check recording
# --------------------------------------------------------------------------


class Checks:
    """Ordered PASS/FAIL findings, printed as they happen."""

    def __init__(self, title: str):
        self.title = title
        self.results: list[tuple[bool, str]] = []
        print(f"# {title}", flush=True)

    def record(self, ok: bool, message: str) -> bool:
        self.results.append((bool(ok), message))
        print(f"[{'PASS' if ok else 'FAIL'}] {message}", flush=True)
        return bool(ok)

    def info(self, message: str) -> None:
        print(f"  {message}", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for ok, _ in self.results if not ok)

    def as_dict(self) -> dict:
        return {"title": self.title, "passed": len(self.results) - self.failed, "failed": self.failed, "results": [{"ok": ok, "message": m} for ok, m in self.results]}

    def finish(self, success: str) -> int:
        total = len(self.results)
        print(f"\n{total - self.failed}/{total} {self.title} checks passed")
        if self.failed or not total:
            print(f"FAIL: {self.failed} check(s) failed" if total else "FAIL: no checks ran", file=sys.stderr)
            return 1
        print(f"PASS: {success}")
        return 0


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def poll(fn, timeout: float, interval: float, until) -> tuple[object, bool]:
    """Calls fn() every `interval` seconds until `until(value)` is true or
    `timeout` elapses. Returns (last value, satisfied). Exceptions from fn
    are retried; the last one is re-raised if nothing ever succeeded."""
    deadline = time.monotonic() + timeout
    last, last_exc = None, None
    while True:
        try:
            last = fn()
            last_exc = None
            if until(last):
                return last, True
        except (subprocess.SubprocessError, Day8Error, json.JSONDecodeError, KeyError) as exc:
            last_exc = exc
        if time.monotonic() >= deadline:
            if last is None and last_exc is not None:
                raise Day8Error(f"never observed a value: {last_exc}") from last_exc
            return last, False
        time.sleep(interval)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "init":
        print("usage: day8_common.py init", file=sys.stderr)
        return 2
    try:
        directory = init_run_dir()
    except (Day8Error, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: created private Day 8 run directory {directory} (mode 0700, run ID {os.environ[RUN_ID_ENV]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

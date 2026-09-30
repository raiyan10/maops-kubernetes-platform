#!/usr/bin/env python3
"""
DAY7: read-only host preflight before the isolated Day 7 kind cluster is
created or used. Never creates, stops, deletes or modifies any cluster,
container, sysctl or file; it only reads.

Checks:
  - kind/cluster-day7.yaml is the Day 7 config: name maops-k8s-day7,
    host port 18081 -> NodePort 30080 on 127.0.0.1, and never Day 6's
    host port 18080.
  - Docker answers (`docker info`).
  - `kind get clusters`: reports every existing cluster. The released
    Day 6 cluster (maops-k8s-day6) is reported and left alone - this
    preflight never requires it to be stopped.
  - Host port 18081: if the Day 7 cluster does not exist yet, the port
    must be bindable on 127.0.0.1 right now (probe bind, immediately
    closed); if it does exist, the port must be published by its own
    control-plane container (`docker port`).
  - Resources for a SECOND concurrent 3-node cluster on this host/WSL VM:
    MemAvailable, CPU count, free disk under $HOME, and inotify limits
    (kind documents "too many open files" failures with low
    fs.inotify.max_user_instances/watches when several clusters run).
    Thresholds are conservative local heuristics, overridable via
    DAY7_MIN_MEM_AVAILABLE_GIB / DAY7_MIN_CPUS / DAY7_MIN_DISK_FREE_GIB.
  - WSL is detected and reported (informational).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KIND_CONFIG = REPO_ROOT / "kind" / "cluster-day7.yaml"
DAY7_CLUSTER = "maops-k8s-day7"
DAY6_CLUSTER = "maops-k8s-day6"
DAY7_HOST_PORT = 18081
DAY6_HOST_PORT = 18080
NODE_PORT = 30080
LISTEN_ADDRESS = "127.0.0.1"
MIN_INOTIFY_INSTANCES = 512
MIN_INOTIFY_WATCHES = 524288

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((bool(ok), message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return bool(ok)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def kind_config_findings(doc: dict) -> list[tuple[bool, str]]:
    """Pure checks of the parsed kind/cluster-day7.yaml."""
    out = [(doc.get("name") == DAY7_CLUSTER, f"kind config name is {doc.get('name')!r} (expected {DAY7_CLUSTER!r})")]
    nodes = doc.get("nodes") or []
    mappings = [m for n in nodes for m in (n.get("extraPortMappings") or [])]
    out.append((len(mappings) == 1, f"exactly one host port mapping ({len(mappings)})"))
    if mappings:
        m = mappings[0]
        out.append((m.get("hostPort") == DAY7_HOST_PORT, f"hostPort {m.get('hostPort')} (expected {DAY7_HOST_PORT}; never Day 6's {DAY6_HOST_PORT})"))
        out.append((m.get("containerPort") == NODE_PORT, f"containerPort {m.get('containerPort')} (expected NodePort {NODE_PORT})"))
        out.append((m.get("listenAddress") == LISTEN_ADDRESS, f"listenAddress {m.get('listenAddress')!r} (loopback only)"))
    return out


def meminfo_available_gib(text: str) -> float | None:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / (1024 * 1024)
    return None


def port_bindable(port: int, address: str = LISTEN_ADDRESS) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((address, port))
        return True, f"{address}:{port} is free (probe bind succeeded and was released)"
    except OSError as exc:
        return False, f"{address}:{port} is not bindable: {exc}"
    finally:
        sock.close()


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def main() -> int:
    print("# Day 7 host preflight (read-only - creates, stops and deletes nothing)")
    try:
        doc = k8s_yaml.load_all(KIND_CONFIG.read_text())[0]
    except (OSError, IndexError, ValueError) as exc:
        record(False, f"could not read {KIND_CONFIG}: {exc}")
        doc = {}
    for ok, msg in kind_config_findings(doc):
        record(ok, f"kind/cluster-day7.yaml: {msg}")

    info = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    record(info is not None and info.returncode == 0, f"Docker daemon reachable (server {'?' if info is None else info.stdout.strip() or info.stderr.strip()[:120]})")

    clusters_result = _run(["kind", "get", "clusters"])
    clusters = set((clusters_result.stdout if clusters_result and clusters_result.returncode == 0 else "").split())
    record(clusters_result is not None and clusters_result.returncode == 0, f"kind clusters listed: {sorted(clusters) or 'none'}")
    print(f"[INFO] released Day 6 cluster {DAY6_CLUSTER!r} {'exists and will be left untouched' if DAY6_CLUSTER in clusters else 'is not present'}")

    if DAY7_CLUSTER in clusters:
        print(f"[INFO] {DAY7_CLUSTER} already exists - cluster-create will reuse it, never recreate it")
        port = _run(["docker", "port", f"{DAY7_CLUSTER}-control-plane", f"{NODE_PORT}/tcp"])
        published = (port.stdout if port and port.returncode == 0 else "").strip()
        record(f"{LISTEN_ADDRESS}:{DAY7_HOST_PORT}" in published, f"{DAY7_CLUSTER} control-plane publishes {NODE_PORT}/tcp on {LISTEN_ADDRESS}:{DAY7_HOST_PORT} (found {published!r})")
    else:
        record(*port_bindable(DAY7_HOST_PORT))

    try:
        mem = meminfo_available_gib(Path("/proc/meminfo").read_text())
    except OSError:
        mem = None
    min_mem = _env_float("DAY7_MIN_MEM_AVAILABLE_GIB", 4.0)
    record(mem is not None and mem >= min_mem, f"MemAvailable {mem if mem is None else round(mem, 1)} GiB (need >= {min_mem} GiB for a second 3-node cluster)")
    cpus = os.cpu_count() or 0
    min_cpus = _env_float("DAY7_MIN_CPUS", 4)
    record(cpus >= min_cpus, f"{cpus} CPUs visible (need >= {min_cpus:.0f})")
    free = shutil.disk_usage(Path.home()).free / (1024 ** 3)
    min_disk = _env_float("DAY7_MIN_DISK_FREE_GIB", 15.0)
    record(free >= min_disk, f"{free:.1f} GiB free under $HOME (need >= {min_disk} GiB)")
    for name, minimum in (("max_user_instances", MIN_INOTIFY_INSTANCES), ("max_user_watches", MIN_INOTIFY_WATCHES)):
        try:
            value = int(Path(f"/proc/sys/fs/inotify/{name}").read_text().strip())
        except (OSError, ValueError):
            value = None
        record(value is not None and value >= minimum, f"fs.inotify.{name}={value} (need >= {minimum} for concurrent kind clusters; this preflight never changes it)")
    try:
        wsl = "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        wsl = False
    print(f"[INFO] WSL detected: {wsl} (resource figures above are the WSL VM's, not the Windows host's)")

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} Day 7 preflight checks passed")
    if failures:
        print(f"FAIL: {len(failures)} preflight check(s) failed - do not create or use the Day 7 cluster yet", file=sys.stderr)
        return 1
    print("PASS: host is ready for the isolated Day 7 cluster (Day 6 left untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

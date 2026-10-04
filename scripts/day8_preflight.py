#!/usr/bin/env python3
"""
DAY8: read-only capacity and compatibility preflight, run before any
Day 8 mutation of maops-k8s-day7.

  - context resolves to the Day 7 cluster (kube.verify_context), 3 nodes
    Ready;
  - the API server's version lies inside every pinned add-on's
    upstream-supported window (Metrics Server 0.9.x: 1.34+, VPA 1.8.x:
    1.36-1.38, KEDA 2.21: 1.34-1.36) - a different cluster version stops
    the run before anything is installed;
  - host headroom: MemAvailable and 1-minute load (all kind nodes share
    this one WSL host, whatever each node's "allocatable" says);
  - scheduler headroom: the workers' unrequested CPU/memory covers the
    add-ons' requests plus the scaling namespace's whole worst-case
    budget (scripts/day8_objects.py) - so an expected scale-out can never
    be Pending for lack of node capacity.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day8_common as common
import day8_objects as o
import kube

SUPPORTED_MINOR = {"metrics-server": (34, None), "vertical-pod-autoscaler": (36, 38), "keda": (34, 36)}
ADDON_REQUESTS_CPU_M = 50 + 25 + 25 + 50 + 50 + 25  # metrics-server, VPA x2, KEDA x3 (helm-values/day8)
ADDON_REQUESTS_MEMORY_MI = 64 + 64 + 96 + 96 + 64 + 48
MIN_MEM_AVAILABLE_MI = 2048
MAX_LOAD_PER_CPU = 3.0
# The 1-minute load right after `make test` (the sequence's own previous
# step) is a transient: wait - bounded - for it to settle under the SAME
# threshold, and fail if it never does.
LOAD_SETTLE_SECONDS = 180
LOAD_SETTLE_INTERVAL = 10


def version_problems(minor: int) -> list[str]:
    """Pure."""
    out = []
    for addon, (low, high) in SUPPORTED_MINOR.items():
        if minor < low or (high is not None and minor > high):
            out.append(f"Kubernetes 1.{minor} is outside {addon} {common.ADDONS[addon]['app_version']}'s supported window 1.{low}-{'' if high is None else f'1.{high}'}")
    return out


def host_headroom(meminfo: str, loadavg: str, cpus: int) -> list[str]:
    """Pure: /proc/meminfo and /proc/loadavg text."""
    out = []
    m = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, flags=re.M)
    available_mi = int(m.group(1)) // 1024 if m else 0
    if available_mi < MIN_MEM_AVAILABLE_MI:
        out.append(f"host MemAvailable {available_mi}Mi < {MIN_MEM_AVAILABLE_MI}Mi")
    load1 = float(loadavg.split()[0])
    if load1 > MAX_LOAD_PER_CPU * cpus:
        out.append(f"host 1-minute load {load1} > {MAX_LOAD_PER_CPU} x {cpus} CPUs")
    return out


def settle_host_headroom(read, cpus: int, timeout: float = LOAD_SETTLE_SECONDS, interval: float = LOAD_SETTLE_INTERVAL, sleep=time.sleep, clock=time.monotonic) -> tuple[list[str], list[str]]:
    """Re-reads (meminfo, loadavg) via `read()` until host_headroom() is
    clean or `timeout` elapses. Returns (final problems, every load sample)."""
    deadline = clock() + timeout
    samples = []
    while True:
        meminfo, loadavg = read()
        samples.append(loadavg.split()[0])
        problems = host_headroom(meminfo, loadavg, cpus)
        if not problems or clock() >= deadline:
            return problems, samples
        sleep(interval)


def keda_state_problems(release_metadata: dict | None, ownership_problems: list[str], crd_instances: dict, keda_namespace_labels: dict | None) -> list[str]:
    """Pure. A run starts only from a cluster with NO KEDA at all: no Helm
    release (an owned one means an interrupted run - clean it up first; a
    foreign one is never taken over), no KEDA CRD (and so no foreign KEDA
    object that a later uninstall could delete), no `keda` namespace.
    `crd_instances`: {crd: None (absent) | [instance, ...]}."""
    problems = []
    if release_metadata is not None:
        if ownership_problems:
            problems.append(f"a FOREIGN KEDA Helm release keda/keda exists ({'; '.join(ownership_problems)}) - Day 8 will not take it over or uninstall it")
        else:
            problems.append("a Day 8 KEDA release is still installed (interrupted run?) - run `PATH=/usr/bin:$PATH make day8-cleanup` first")
    for crd, instances in sorted(crd_instances.items()):
        if instances is None:
            continue
        problems.append(f"KEDA CRD {crd} exists with {len(instances)} instance(s){' ' + str(instances[:5]) if instances else ''} - KEDA is installed outside this run")
    if keda_namespace_labels is not None:
        owned = o.is_day8_owned(keda_namespace_labels, o.KEDA_NAMESPACE_COMPONENT)
        problems.append(f"namespace {o.KEDA_NAMESPACE} exists ({'Day 8-owned leftover - run make day8-cleanup' if owned else 'NOT Day 8-owned - Day 8 will not adopt it'})")
    return problems


def _quantity_m(q: str) -> int:
    q = str(q)
    return int(float(q[:-1])) if q.endswith("m") else int(float(q) * 1000)


def _quantity_mi(q: str) -> int:
    q = str(q)
    for suffix, factor in (("Ki", 1 / 1024), ("Mi", 1), ("Gi", 1024)):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * factor)
    return int(float(q) / 1024**2)


def worker_headroom(nodes: list[dict], pods: list[dict]) -> tuple[int, int]:
    """Pure: (free CPU millicores, free memory MiB) summed over schedulable
    worker nodes = allocatable minus the requests of non-terminal Pods."""
    workers = {n["metadata"]["name"]: n for n in nodes if kube.CONTROL_PLANE_LABEL not in (n["metadata"].get("labels") or {})}
    free_cpu = sum(_quantity_m(n["status"]["allocatable"]["cpu"]) for n in workers.values())
    free_mem = sum(_quantity_mi(n["status"]["allocatable"]["memory"]) for n in workers.values())
    for p in pods:
        if p["spec"].get("nodeName") not in workers or p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p["spec"].get("containers", []):
            req = (c.get("resources") or {}).get("requests") or {}
            free_cpu -= _quantity_m(req.get("cpu", "0"))
            free_mem -= _quantity_mi(req.get("memory", "0"))
    return free_cpu, free_mem


def main() -> int:
    try:
        common.require_cluster_profile()
        checks = common.Checks("Day 8 preflight (read-only)")
        kube.verify_context()
        checks.record(True, f"context {kube.CONTEXT} resolves to the {kube.CLUSTER_NAME} nodes")
        nodes = common.kubectl_json("get", "nodes")["items"]
        ready = [n["metadata"]["name"] for n in nodes if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])]
        checks.record(len(nodes) == 3 and len(ready) == 3, f"{len(ready)}/{len(nodes)} nodes Ready (expected 3/3)")
        version = json.loads(common.kubectl("version", "-o", "json").stdout)["serverVersion"]
        minor = int(re.match(r"\d+", version["minor"]).group(0))
        problems = version_problems(minor)
        checks.record(not problems, f"server {version['gitVersion']} inside every pinned add-on's supported window" if not problems else "; ".join(problems))
        cpus = os.cpu_count() or 1
        host, samples = settle_host_headroom(lambda: (Path("/proc/meminfo").read_text(), Path("/proc/loadavg").read_text()), cpus)
        checks.record(not host, f"host headroom ({cpus} CPUs, load samples {samples}, limit {MAX_LOAD_PER_CPU * cpus}): {'ok' if not host else '; '.join(host)}")
        pods = common.kubectl_json("get", "pods", "--all-namespaces")["items"]
        free_cpu, free_mem = worker_headroom(nodes, pods)
        b = o.budget()
        need_cpu = ADDON_REQUESTS_CPU_M + b.requests_cpu_m
        need_mem = ADDON_REQUESTS_MEMORY_MI + b.requests_memory // 1024**2
        checks.record(free_cpu >= need_cpu and free_mem >= need_mem, f"worker request headroom {free_cpu}m / {free_mem}Mi covers add-ons + scaling budget {need_cpu}m / {need_mem}Mi")
        existing = common.kubectl_json_or_none("get", "namespace", o.NAMESPACE)
        checks.record(existing is None, f"scaling namespace {o.NAMESPACE} does not exist yet ({'absent' if existing is None else 'PRESENT - a previous run was not cleaned up'})")
        import day8_addons
        import day8_scaling

        metadata = common.helm_release_metadata(day8_addons.KEDA_RELEASE, o.KEDA_NAMESPACE)
        ownership = day8_addons.keda_release_ownership_problems(metadata) if metadata is not None else []
        crd_instances = {}
        for crd in day8_addons.KEDA_CRDS:
            scope = day8_scaling._crd_scope(crd)
            crd_instances[crd] = None if scope is None else day8_scaling._crd_instances(crd, scope)
        keda_ns = common.kubectl_json_or_none("get", "namespace", o.KEDA_NAMESPACE)
        problems = keda_state_problems(metadata, ownership, crd_instances, None if keda_ns is None else (keda_ns["metadata"].get("labels") or {}))
        checks.record(not problems, "no KEDA present before the run (no keda release, none of the 6 KEDA CRDs, no keda namespace)" if not problems else "; ".join(problems))
        return checks.finish("capacity and compatibility allow the Day 8 run")
    except (common.Day8Error, RuntimeError, subprocess.SubprocessError, KeyError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Real service-discovery / DNS proof (Day 2).

Independently proves, from a live gateway Pod:

  1. `maops-app` resolves through Kubernetes DNS - via a real
     `socket.getaddrinfo()` call run inside the pod (the distroless image
     has no shell/dig/nslookup/getent, so the pinned interpreter path is
     used directly via `kubectl exec`, same pattern as cluster_check.py).
     No specific resolved IP is required or asserted - only that
     resolution succeeds.
  2. Real gateway -> maops-app HTTP succeeds through the Service, by
     port-forwarding service/maops-gateway and calling /backend (which
     itself only ever talks to BACKEND_HOST=maops-app, never a Pod IP).

Source/config inspection alone is never accepted as DNS proof here - both
of the above are live, real behavior.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from cluster_check import exec_in_pod, get_pods
from http_checks import check_endpoint
from kube import CONTEXT, GATEWAY_LABEL_SELECTOR, GATEWAY_SERVICE, NAMESPACE
from portforward import port_forward

_DNS_PROBE_SNIPPET = (
    "import socket, sys\n"
    "infos = socket.getaddrinfo('maops-app', 8080)\n"
    "addresses = sorted({info[4][0] for info in infos})\n"
    "sys.stdout.write(str(len(addresses)))\n"
)

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def check_dns_resolution() -> None:
    pods = get_pods(GATEWAY_LABEL_SELECTOR)
    if not pods:
        record(False, "DNS resolution: no gateway pods available to exec into")
        return
    pod_name = pods[0]["metadata"]["name"]
    try:
        output = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", _DNS_PROBE_SNIPPET)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        record(False, f"DNS resolution: kubectl exec failed in pod {pod_name}: {stderr if stderr else exc}")
        return
    try:
        resolved_count = int(output)
    except ValueError:
        record(False, f"DNS resolution: unexpected exec output {output!r}")
        return
    record(
        resolved_count >= 1,
        f"DNS resolution: 'maops-app' resolved to {resolved_count} address(es) via socket.getaddrinfo() "
        f"from live gateway pod {pod_name} (no specific IP required)",
    )


def check_real_service_http() -> None:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok, detail = check_endpoint(local_port, "/backend", role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"gateway -> maops-app real HTTP via Service: port-forward failed: {exc}")
        return
    record(ok, f"gateway -> maops-app real HTTP via Service: {detail}")


def main() -> int:
    print("# Real service discovery / DNS proof")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    check_dns_resolution()
    check_real_service_http()

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} discovery checks passed")
    if failures:
        print(f"FAIL: {len(failures)} discovery check(s) failed", file=sys.stderr)
        return 1
    print("PASS: real DNS resolution and Service HTTP both proven")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

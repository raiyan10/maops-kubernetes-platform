#!/usr/bin/env python3
"""
Normal external HTTP smoke test (Day 2): port-forward service/maops-gateway
(NOT maops-app - the gateway is the only externally-reached workload in
Day 2's architecture) and perform real HTTP checks against /, /livez,
/readyz, /config, /backend.

Uses a bounded, auto-cleaned-up port-forward (scripts/portforward.py) -
never leaves a background kubectl process running.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_checks import check_all_endpoints
from kube import CONTEXT, GATEWAY_SERVICE, NAMESPACE
from portforward import port_forward


def main() -> int:
    print(f"# HTTP smoke test via port-forward to service/{GATEWAY_SERVICE} in {NAMESPACE}")
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            print(f"port-forward established on 127.0.0.1:{local_port} -> service/{GATEWAY_SERVICE}:8080")
            results = check_all_endpoints(local_port, role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        print(f"FAIL: could not establish port-forward: {exc}", file=sys.stderr)
        return 1

    failures = [msg for ok, msg in results if not ok]
    for ok, msg in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {msg}")

    print()
    print(f"{len(results) - len(failures)}/{len(results)} smoke checks passed")
    if failures:
        print(f"FAIL: {len(failures)} smoke check(s) failed", file=sys.stderr)
        return 1
    print("PASS: all smoke checks passed (gateway /backend proved real maops-app data end-to-end)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

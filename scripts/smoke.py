#!/usr/bin/env python3
"""
Real-cluster check #11: port-forward the Service and perform real HTTP
checks against /, /livez, /readyz, /config.

Uses a bounded, auto-cleaned-up port-forward (scripts/portforward.py) -
never leaves a background kubectl process running.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_checks import check_all_endpoints
from kube import CONTEXT, NAMESPACE, SERVICE
from portforward import port_forward


def main() -> int:
    print(f"# HTTP smoke test via port-forward to service/{SERVICE} in {NAMESPACE}")
    try:
        with port_forward(CONTEXT, NAMESPACE, SERVICE, 8080) as local_port:
            print(f"port-forward established on 127.0.0.1:{local_port} -> service/{SERVICE}:8080")
            results = check_all_endpoints(local_port)
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
    print("PASS: all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

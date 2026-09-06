#!/usr/bin/env python3
"""
Render k8s/base with `kubectl kustomize` and run repository-owned static
validation against the result. Python standard library only.

Usage:
    python3 scripts/manifest_check.py [path/to/base]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_yaml
from validate_manifests import run_checks


def render(base_dir: str) -> str:
    result = subprocess.run(
        ["kubectl", "kustomize", base_dir],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def main() -> int:
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "k8s/base"

    try:
        rendered = render(base_dir)
    except subprocess.CalledProcessError as exc:
        print("FAIL: kubectl kustomize render failed", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("FAIL: kubectl not found on PATH", file=sys.stderr)
        return 1

    docs = k8s_yaml.load_all(rendered)
    if not docs:
        print("FAIL: no documents parsed from rendered manifests", file=sys.stderr)
        return 1

    findings = run_checks(docs)
    failures = [f for f in findings if not f.ok]

    for finding in findings:
        print(finding.render())

    print()
    print(f"{len(findings) - len(failures)}/{len(findings)} checks passed")

    if failures:
        print(f"FAIL: {len(failures)} static manifest check(s) failed", file=sys.stderr)
        return 1

    print("PASS: all static manifest checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

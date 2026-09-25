#!/usr/bin/env python3
"""
Render charts/maops-kubernetes-platform with `helm template` and run
repository-owned static validation against the result - the Day 6
analogue of `scripts/manifest_check.py` for k8s/base. Never contacts a
live cluster (`helm template` is a pure local render, exactly like
`kubectl kustomize`).

Also independently proves k8s/base remains the frozen Day 5 source and
is not part of the Day 6 Helm deploy: reads `scripts/validate_manifests.py`'s
own EXPECTED_VERSION/EXPECTED_INSTANCE constants (never re-rendering
k8s/base itself, and never importing anything from
`scripts/validate_helm_chart.py` into it or vice versa) and asserts
they are still pinned to Day 5's values, not silently advanced to Day 6.

DAY6 remediation: also runs
`scripts/validate_gateway_values_configmap.py` against
`k8s/day6/gateway-values-configmap.yaml` - a plain, hand-authored
kubectl-applied manifest, never Helm-templated, so it is validated by a
separate, purpose-built checker rather than folded into the
`helm template` render above. Likewise (DAY6 review SEC-3),
`scripts/validate_cilium_probe_policy.py` pins the exact shape of
`k8s/day6/cilium-ambient-probe-policy.yaml` - the one cluster-scoped
CiliumClusterwideNetworkPolicy this project applies.

Usage:
    python3 scripts/helm_check.py [path/to/chart]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_yaml
import validate_manifests
import validate_gateway_values_configmap
import validate_cilium_probe_policy
from validate_helm_chart import run_checks, Finding

DEFAULT_CHART_DIR = "charts/maops-kubernetes-platform"
RELEASE_NAME = "maops-kubernetes-platform-day6"
RELEASE_NAMESPACE = "maops-platform"
REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_VALUES_CONFIGMAP_PATH = REPO_ROOT / "k8s" / "day6" / "gateway-values-configmap.yaml"
CILIUM_PROBE_POLICY_PATH = REPO_ROOT / "k8s" / "day6" / "cilium-ambient-probe-policy.yaml"


def render(chart_dir: str) -> str:
    result = subprocess.run(
        ["helm", "template", RELEASE_NAME, chart_dir, "--namespace", RELEASE_NAMESPACE],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def check_k8s_base_still_frozen() -> Finding:
    """DAY6: k8s/base must never be advanced past its Day 5 pin - this
    is the one check in this file that looks outside the Helm chart, to
    close the specific risk the task's own self-review calls out:
    "accidental k8s/base changes." Reads validate_manifests.py's own
    constants rather than re-rendering k8s/base, since a stray edit to
    those constants (even without touching a single manifest file)
    would itself already be evidence the frozen-source boundary was
    violated."""
    ok = (
        validate_manifests.EXPECTED_VERSION == "0.5.0"
        and validate_manifests.EXPECTED_INSTANCE == "maops-kubernetes-platform-day5"
    )
    return Finding(
        ok=ok,
        name="scope.k8s_base_still_frozen_at_day5",
        detail=(
            f"expected validate_manifests.EXPECTED_VERSION == '0.5.0' and EXPECTED_INSTANCE == "
            f"'maops-kubernetes-platform-day5', found {validate_manifests.EXPECTED_VERSION!r} / "
            f"{validate_manifests.EXPECTED_INSTANCE!r} - k8s/base must never be advanced to Day 6"
        ),
    )


def main() -> int:
    chart_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHART_DIR

    try:
        rendered = render(chart_dir)
    except subprocess.CalledProcessError as exc:
        print("FAIL: helm template render failed", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("FAIL: helm not found on PATH", file=sys.stderr)
        return 1

    docs = k8s_yaml.load_all(rendered)
    if not docs:
        print("FAIL: no documents parsed from rendered chart output", file=sys.stderr)
        return 1

    findings = run_checks(docs)
    findings.append(check_k8s_base_still_frozen())

    try:
        gateway_values_text = GATEWAY_VALUES_CONFIGMAP_PATH.read_text()
    except OSError as exc:
        print(f"FAIL: could not read {GATEWAY_VALUES_CONFIGMAP_PATH}: {exc}", file=sys.stderr)
        return 1
    findings += validate_gateway_values_configmap.run_checks(gateway_values_text)

    try:
        cilium_probe_policy_text = CILIUM_PROBE_POLICY_PATH.read_text()
    except OSError as exc:
        print(f"FAIL: could not read {CILIUM_PROBE_POLICY_PATH}: {exc}", file=sys.stderr)
        return 1
    findings += validate_cilium_probe_policy.run_checks(cilium_probe_policy_text)

    failures = [f for f in findings if not f.ok]

    print(f"# Day 6 Helm chart static validation ({chart_dir}, release {RELEASE_NAME!r})")
    for finding in findings:
        print(finding.render())

    print()
    print(f"{len(findings) - len(failures)}/{len(findings)} checks passed")

    if failures:
        print(f"FAIL: {len(failures)} static Helm chart check(s) failed", file=sys.stderr)
        return 1

    print("PASS: all static Helm chart checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

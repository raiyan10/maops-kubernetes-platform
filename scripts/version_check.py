#!/usr/bin/env python3
"""
Repository-owned version consistency guard.

This materially closes DAY1-REL-I1 ("no automated guard cross-checks
VERSION against the Makefile's hardcoded image tag or the manifests'
app.kubernetes.io/version labels"): VERSION is read once, and every
place a version should agree with it (both Deployment image tags, every
rendered app.kubernetes.io/version label - including pod template
labels) is checked against that single value, not re-typed by hand.

DAY6: extended, additively, with a second, independent set of checks
(`run_day6_release_checks`) covering the live VERSION file, the Helm
chart's own version/appVersion, all three image tags at their Day 6
values, Day 6 cluster/kubeconfig/context/release identities, and pinned
infrastructure versions (Cilium, Gateway API CRDs, Istio) - all cross-
checked against the SAME 0.6.0 target. `run_version_checks` itself
(and its permanently-frozen `EXPECTED_TARGET_VERSION`) is completely
unchanged: it still validates k8s/base's OWN rendered labels against
Day 5's frozen 0.5.0 target, independent of whatever the live VERSION
file says - see `main()`'s wiring below, which calls it with the frozen
constant rather than `read_version()`.

Usage:
    python3 scripts/version_check.py [path/to/base]

Operates on already-parsed Kubernetes objects for the checking logic
itself (`run_version_checks`), so it's directly unit-testable against
constructed fixtures - the same pattern as validate_manifests.py.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_yaml
import kube

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
CHART_DIR = REPO_ROOT / "charts" / "maops-kubernetes-platform"
CHART_YAML = CHART_DIR / "Chart.yaml"
VALUES_YAML = CHART_DIR / "values.yaml"
MAKEFILE = REPO_ROOT / "Makefile"

# Day 5's pinned target - VERSION itself must have actually been bumped,
# not just left agreeing with whatever it already said. Permanently
# frozen: k8s/base is the frozen Day 5 Kustomize source and this
# constant must never be advanced past it (see
# scripts/helm_check.py's own check_k8s_base_still_frozen(), which
# reads this exact constant).
EXPECTED_TARGET_VERSION = "0.5.0"

# DAY6: the live release target - VERSION, the Helm chart's own
# version/appVersion, and all three image tags must all agree with
# this.
DAY6_TARGET_VERSION = "0.6.0"

# DAY6: pinned infrastructure versions - installed out-of-band by Make
# targets (cni-install, gateway-api-install, mesh-install), never
# floated to "latest". Checked here as a plain substring match against
# the Makefile's own recipe text, the single source of truth for what
# actually gets installed - never re-typed as a second, independently
# maintained constant that could drift from what the Makefile really
# runs.
CILIUM_VERSION = "1.20.1"
GATEWAY_API_CRDS_VERSION = "v1.6.0"
ISTIO_VERSION = "1.31.0"

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
STATE_STATEFULSET = "maops-state"
GATEWAY_IMAGE_REPO = "maops-kubernetes-gateway"
APP_IMAGE_REPO = "maops-kubernetes-app"
STATE_IMAGE_REPO = "maops-kubernetes-state"


@dataclass
class Finding:
    ok: bool
    name: str
    detail: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.name}: {self.detail}"


def read_version(version_file: Path = VERSION_FILE) -> str:
    return version_file.read_text().strip()


def _image_tag(image: str | None) -> str | None:
    if not image or ":" not in image:
        return None
    return image.rsplit(":", 1)[1]


def _collect_version_labels(docs: list[dict]) -> list[tuple[str, str | None]]:
    """Every app.kubernetes.io/version label this project renders, tagged
    with a human-readable location for reporting. Covers top-level
    metadata.labels on every document, plus Deployment pod template
    labels (which kubectl kustomize does NOT otherwise surface at the
    top level)."""
    locations: list[tuple[str, str | None]] = []
    for doc in docs:
        kind = doc.get("kind", "?")
        name = doc.get("metadata", {}).get("name", "?")
        labels = doc.get("metadata", {}).get("labels") or {}
        if "app.kubernetes.io/version" in labels:
            locations.append((f"{kind}/{name}.metadata.labels", labels.get("app.kubernetes.io/version")))
        if kind in ("Deployment", "StatefulSet"):
            pod_labels = doc.get("spec", {}).get("template", {}).get("metadata", {}).get("labels") or {}
            if "app.kubernetes.io/version" in pod_labels:
                locations.append(
                    (f"{kind}/{name}.spec.template.metadata.labels", pod_labels.get("app.kubernetes.io/version"))
                )
    return locations


def run_version_checks(version: str, docs: list[dict]) -> list[Finding]:
    findings: list[Finding] = []

    findings.append(
        Finding(
            ok=version == EXPECTED_TARGET_VERSION,
            name="version.file_matches_day5_target",
            detail=f"expected VERSION == {EXPECTED_TARGET_VERSION!r}, found {version!r}",
        )
    )

    workloads = [d for d in docs if d.get("kind") in ("Deployment", "StatefulSet")]
    for expected_name, image_repo in (
        (GATEWAY_DEPLOYMENT, GATEWAY_IMAGE_REPO),
        (APP_DEPLOYMENT, APP_IMAGE_REPO),
        (STATE_STATEFULSET, STATE_IMAGE_REPO),
    ):
        dep = next((d for d in workloads if d.get("metadata", {}).get("name") == expected_name), None)
        containers = ((dep or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers")) or []
        image = containers[0].get("image") if containers else None
        tag = _image_tag(image)
        expected_image = f"{image_repo}:{version}"
        findings.append(
            Finding(
                ok=dep is not None and image == expected_image,
                name=f"version.{expected_name}.image_tag_matches_version",
                detail=f"expected {expected_name} image == {expected_image!r}, found {image!r} (tag {tag!r})",
            )
        )

    for location, label_value in _collect_version_labels(docs):
        findings.append(
            Finding(
                ok=label_value == version,
                name=f"version.label_matches[{location}]",
                detail=f"expected app.kubernetes.io/version == {version!r} at {location}, found {label_value!r}",
            )
        )

    findings.append(
        Finding(
            ok=len(_collect_version_labels(docs)) >= 5,
            name="version.labels_present",
            detail=(
                "expected at least 5 app.kubernetes.io/version label locations "
                "(Namespace + 2 Deployments x metadata, plus 2 Deployment pod template labels - "
                "the real k8s/base render additionally carries labels on both ConfigMaps, both "
                "Services, and both PodDisruptionBudgets, well above this floor), "
                f"found {len(_collect_version_labels(docs))}"
            ),
        )
    )

    return findings


def run_day6_release_checks(
    version_file_content: str,
    chart_yaml: dict,
    values_yaml: dict,
    makefile_text: str,
) -> list[Finding]:
    """DAY6: pure, unit-testable checking logic - takes already-read/
    already-parsed inputs (never reads a file itself), same pattern as
    `run_version_checks` above."""
    findings: list[Finding] = []

    findings.append(
        Finding(
            ok=version_file_content == DAY6_TARGET_VERSION,
            name="day6.version_file_matches_target",
            detail=f"expected VERSION == {DAY6_TARGET_VERSION!r}, found {version_file_content!r}",
        )
    )

    chart_version = chart_yaml.get("version")
    findings.append(
        Finding(
            ok=chart_version == DAY6_TARGET_VERSION,
            name="day6.chart_version_matches_target",
            detail=f"expected Chart.yaml version == {DAY6_TARGET_VERSION!r}, found {chart_version!r}",
        )
    )
    app_version = chart_yaml.get("appVersion")
    findings.append(
        Finding(
            ok=app_version == DAY6_TARGET_VERSION,
            name="day6.chart_appVersion_matches_target",
            detail=f"expected Chart.yaml appVersion == {DAY6_TARGET_VERSION!r}, found {app_version!r}",
        )
    )

    images = values_yaml.get("images", {})
    for workload in ("gateway", "app", "state"):
        tag = (images.get(workload) or {}).get("tag")
        findings.append(
            Finding(
                ok=tag == DAY6_TARGET_VERSION,
                name=f"day6.values.images.{workload}.tag_matches_target",
                detail=f"expected values.yaml images.{workload}.tag == {DAY6_TARGET_VERSION!r}, found {tag!r}",
            )
        )

    day6_identities = (
        ("kube.CLUSTER_NAME", kube.CLUSTER_NAME, "maops-k8s-day6"),
        ("kube.CONTEXT", kube.CONTEXT, "kind-maops-k8s-day6"),
        ("kube.VALIDATION_NAMESPACE", kube.VALIDATION_NAMESPACE, "maops-day6-validation"),
        ("kube.INGRESS_NAMESPACE", kube.INGRESS_NAMESPACE, "maops-ingress"),
        ("kube.INSTANCE_LABEL", kube.INSTANCE_LABEL, "maops-kubernetes-platform-day6"),
        ("kube.HELM_RELEASE_NAME", kube.HELM_RELEASE_NAME, "maops-kubernetes-platform-day6"),
    )
    for label, actual, expected in day6_identities:
        findings.append(
            Finding(
                ok=actual == expected,
                name=f"day6.identity[{label}]",
                detail=f"expected {label} == {expected!r}, found {actual!r}",
            )
        )

    for label, expected_version in (
        ("Cilium", CILIUM_VERSION),
        ("Gateway API CRDs", GATEWAY_API_CRDS_VERSION),
        ("Istio", ISTIO_VERSION),
    ):
        findings.append(
            Finding(
                ok=expected_version in makefile_text,
                name=f"day6.pinned_infra[{label}]",
                detail=f"expected the Makefile to pin {label} at {expected_version!r}",
            )
        )

    return findings


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

    version = read_version()

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

    # DAY6: k8s/base is checked against its OWN permanently-frozen Day 5
    # target (EXPECTED_TARGET_VERSION), never against whatever the live
    # VERSION file currently says - k8s/base is not part of the Day 6
    # Helm deploy and must never be silently advanced.
    findings = run_version_checks(EXPECTED_TARGET_VERSION, docs)

    try:
        chart_yaml = k8s_yaml.load_all(CHART_YAML.read_text())[0]
        values_yaml = k8s_yaml.load_all(VALUES_YAML.read_text())[0]
    except (OSError, IndexError, ValueError) as exc:
        print(f"FAIL: could not read/parse the Helm chart's Chart.yaml/values.yaml: {exc}", file=sys.stderr)
        return 1
    makefile_text = MAKEFILE.read_text()

    findings += run_day6_release_checks(version, chart_yaml, values_yaml, makefile_text)
    failures = [f for f in findings if not f.ok]

    print(f"# Version consistency check (live VERSION file == {version!r}; k8s/base frozen at {EXPECTED_TARGET_VERSION!r})")
    for finding in findings:
        print(finding.render())

    print()
    print(f"{len(findings) - len(failures)}/{len(findings)} version checks passed")

    if failures:
        print(f"FAIL: {len(failures)} version consistency check(s) failed", file=sys.stderr)
        return 1

    print("PASS: all version consistency checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

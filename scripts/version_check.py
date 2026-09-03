#!/usr/bin/env python3
"""
Repository-owned version consistency guard.

This materially closes DAY1-REL-I1 ("no automated guard cross-checks
VERSION against the Makefile's hardcoded image tag or the manifests'
app.kubernetes.io/version labels"): VERSION is read once, and every
place a version should agree with it (both Deployment image tags, every
rendered app.kubernetes.io/version label - including pod template
labels) is checked against that single value, not re-typed by hand.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"

# Day 2's pinned target - VERSION itself must have actually been bumped,
# not just left agreeing with whatever it already said.
EXPECTED_TARGET_VERSION = "0.2.0"

GATEWAY_DEPLOYMENT = "maops-gateway"
APP_DEPLOYMENT = "maops-app"
GATEWAY_IMAGE_REPO = "maops-kubernetes-gateway"
APP_IMAGE_REPO = "maops-kubernetes-app"


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
        if kind == "Deployment":
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
            name="version.file_matches_day2_target",
            detail=f"expected VERSION == {EXPECTED_TARGET_VERSION!r}, found {version!r}",
        )
    )

    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    for expected_name, image_repo in (
        (GATEWAY_DEPLOYMENT, GATEWAY_IMAGE_REPO),
        (APP_DEPLOYMENT, APP_IMAGE_REPO),
    ):
        dep = next((d for d in deployments if d.get("metadata", {}).get("name") == expected_name), None)
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
            ok=len(_collect_version_labels(docs)) >= 4,
            name="version.labels_present",
            detail=(
                "expected at least 4 app.kubernetes.io/version label locations "
                f"(2 Deployments x metadata + pod template), found {len(_collect_version_labels(docs))}"
            ),
        )
    )

    return findings


def render(base_dir: str) -> str:
    result = subprocess.run(
        ["kubectl", "kustomize", base_dir],
        check=True,
        capture_output=True,
        text=True,
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

    findings = run_version_checks(version, docs)
    failures = [f for f in findings if not f.ok]

    print(f"# Version consistency check (VERSION file == {version!r})")
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

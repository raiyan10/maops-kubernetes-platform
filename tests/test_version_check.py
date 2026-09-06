"""
Docker-free unit tests for scripts/version_check.py - the guard that
materially closes DAY1-REL-I1 by cross-checking VERSION against rendered
image tags and app.kubernetes.io/version labels.

Fixtures are constructed directly as Python dict/list structures, same
pattern as test_validate_manifests.py.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from version_check import run_version_checks


def _deployment(name: str, image: str, version_label: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/version": version_label}},
        "spec": {
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/version": version_label}},
                "spec": {"containers": [{"name": name, "image": image}]},
            }
        },
    }


def _base_docs(version: str = "0.3.0") -> list[dict]:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "maops-platform", "labels": {"app.kubernetes.io/version": version}},
    }
    gateway = _deployment("maops-gateway", f"maops-kubernetes-gateway:{version}", version)
    app = _deployment("maops-app", f"maops-kubernetes-app:{version}", version)
    return [namespace, gateway, app]


def _find(docs, name):
    return next(d for d in docs if d.get("metadata", {}).get("name") == name)


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


class BaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_version_checks("0.3.0", _base_docs())
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 5)


class VersionFileDriftTests(unittest.TestCase):
    def test_version_file_not_bumped_fails(self):
        # VERSION file itself still says 0.2.0, but the manifests were
        # (hypothetically) already bumped - the target-version guard must
        # catch that VERSION itself wasn't actually bumped for Day 3.
        findings = run_version_checks("0.2.0", _base_docs(version="0.2.0"))
        failed = _failed_names(findings)
        self.assertIn("version.file_matches_day3_target", failed)


class ImageTagDriftTests(unittest.TestCase):
    def test_gateway_image_tag_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-gateway")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-gateway:0.1.0"
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertIn("version.maops-gateway.image_tag_matches_version", failed)

    def test_app_image_tag_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-app")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-app:latest"
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertIn("version.maops-app.image_tag_matches_version", failed)

    def test_missing_deployment_fails_image_check(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("metadata", {}).get("name") != "maops-app"]
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertIn("version.maops-app.image_tag_matches_version", failed)


class LabelDriftTests(unittest.TestCase):
    def test_deployment_metadata_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-gateway")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertTrue(any(name.startswith("version.label_matches[Deployment/maops-gateway.metadata") for name in failed))

    def test_pod_template_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-app")["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "0.2.0"
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertTrue(any("spec.template.metadata.labels" in name for name in failed))

    def test_namespace_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-platform")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_version_checks("0.3.0", docs))
        self.assertTrue(any("Namespace/maops-platform" in name for name in failed))


if __name__ == "__main__":
    unittest.main()

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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from version_check import run_version_checks, run_day6_release_checks


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


def _statefulset(name: str, image: str, version_label: str) -> dict:
    # DAY4: StatefulSet uses the identical spec.template.spec.containers[0].image
    # / spec.template.metadata.labels shape as Deployment for version-check
    # purposes (see version_check.py's `workloads` filter and
    # `_collect_version_labels`, both of which cover kind in
    # ("Deployment", "StatefulSet")).
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/version": version_label}},
        "spec": {
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/version": version_label}},
                "spec": {"containers": [{"name": name, "image": image}]},
            }
        },
    }


def _base_docs(version: str = "0.5.0") -> list[dict]:
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "maops-platform", "labels": {"app.kubernetes.io/version": version}},
    }
    gateway = _deployment("maops-gateway", f"maops-kubernetes-gateway:{version}", version)
    app = _deployment("maops-app", f"maops-kubernetes-app:{version}", version)
    state = _statefulset("maops-state", f"maops-kubernetes-state:{version}", version)
    return [namespace, gateway, app, state]


def _find(docs, name):
    return next(d for d in docs if d.get("metadata", {}).get("name") == name)


def _failed_names(findings):
    return {f.name for f in findings if not f.ok}


class BaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_version_checks("0.5.0", _base_docs())
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 5)


class VersionFileDriftTests(unittest.TestCase):
    def test_version_file_not_bumped_fails(self):
        # VERSION file itself still says 0.3.0, but the manifests were
        # (hypothetically) already bumped - the target-version guard must
        # catch that VERSION itself wasn't actually bumped for Day 5.
        findings = run_version_checks("0.3.0", _base_docs(version="0.3.0"))
        failed = _failed_names(findings)
        self.assertIn("version.file_matches_day5_target", failed)


class ImageTagDriftTests(unittest.TestCase):
    def test_gateway_image_tag_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-gateway")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-gateway:0.1.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertIn("version.maops-gateway.image_tag_matches_version", failed)

    def test_app_image_tag_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-app")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-app:latest"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertIn("version.maops-app.image_tag_matches_version", failed)

    def test_missing_deployment_fails_image_check(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("metadata", {}).get("name") != "maops-app"]
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertIn("version.maops-app.image_tag_matches_version", failed)

    def test_state_statefulset_image_tag_drift_fails(self):
        # DAY4: the StatefulSet branch of the image-tag-matches loop had
        # no dedicated negative-path coverage of its own - only the two
        # Deployment cases did.
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-state")["spec"]["template"]["spec"]["containers"][0]["image"] = "maops-kubernetes-state:0.1.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertIn("version.maops-state.image_tag_matches_version", failed)

    def test_missing_statefulset_fails_image_check(self):
        docs = [d for d in copy.deepcopy(_base_docs()) if d.get("metadata", {}).get("name") != "maops-state"]
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertIn("version.maops-state.image_tag_matches_version", failed)


class LabelDriftTests(unittest.TestCase):
    def test_deployment_metadata_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-gateway")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertTrue(any(name.startswith("version.label_matches[Deployment/maops-gateway.metadata") for name in failed))

    def test_pod_template_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-app")["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "0.2.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertTrue(any("spec.template.metadata.labels" in name for name in failed))

    def test_namespace_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-platform")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertTrue(any("Namespace/maops-platform" in name for name in failed))

    def test_state_statefulset_metadata_label_drift_fails(self):
        # DAY4: StatefulSet metadata.labels version drift had no dedicated
        # negative-path coverage of its own either.
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-state")["metadata"]["labels"]["app.kubernetes.io/version"] = "0.1.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertTrue(any(name.startswith("version.label_matches[StatefulSet/maops-state.metadata") for name in failed))

    def test_state_pod_template_label_drift_fails(self):
        docs = copy.deepcopy(_base_docs())
        _find(docs, "maops-state")["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"] = "0.2.0"
        failed = _failed_names(run_version_checks("0.5.0", docs))
        self.assertTrue(
            any(
                name.startswith("version.label_matches[StatefulSet/maops-state.spec.template.metadata.labels")
                for name in failed
            )
        )


def _day6_chart_yaml(version: str = "0.6.0", app_version: str = "0.6.0") -> dict:
    return {"apiVersion": "v2", "name": "maops-kubernetes-platform", "version": version, "appVersion": app_version}


def _day6_values_yaml(gateway_tag: str = "0.6.0", app_tag: str = "0.6.0", state_tag: str = "0.6.0") -> dict:
    return {
        "images": {
            "gateway": {"repository": "maops-kubernetes-gateway", "tag": gateway_tag, "pullPolicy": "IfNotPresent"},
            "app": {"repository": "maops-kubernetes-app", "tag": app_tag, "pullPolicy": "IfNotPresent"},
            "state": {"repository": "maops-kubernetes-state", "tag": state_tag, "pullPolicy": "IfNotPresent"},
        }
    }


_DAY6_MAKEFILE_TEXT = "helm upgrade --install cilium cilium/cilium --version 1.20.1\nkubectl apply -f https://...v1.6.0/standard-install.yaml\nistioVersion=1.31.0\n"


class Day6ReleaseChecksBaselineTests(unittest.TestCase):
    def test_baseline_passes_every_check(self):
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        failed = _failed_names(findings)
        self.assertEqual(failed, set(), f"unexpected failures: {failed}")
        self.assertGreaterEqual(len(findings), 15)


class Day6VersionFileDriftTests(unittest.TestCase):
    def test_stale_version_file_fails(self):
        findings = run_day6_release_checks("0.5.0", _day6_chart_yaml(), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.version_file_matches_target", _failed_names(findings))


class Day6ChartVersionDriftTests(unittest.TestCase):
    def test_stale_chart_version_fails(self):
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(version="0.5.0"), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.chart_version_matches_target", _failed_names(findings))

    def test_stale_app_version_fails(self):
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(app_version="0.5.0"), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.chart_appVersion_matches_target", _failed_names(findings))


class Day6ImageTagDriftTests(unittest.TestCase):
    def test_stale_gateway_tag_fails(self):
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(gateway_tag="0.5.0"), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.values.images.gateway.tag_matches_target", _failed_names(findings))

    def test_stale_state_tag_fails(self):
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(state_tag="latest"), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.values.images.state.tag_matches_target", _failed_names(findings))


class Day6PinnedInfraDriftTests(unittest.TestCase):
    def test_missing_cilium_pin_fails(self):
        makefile_text = _DAY6_MAKEFILE_TEXT.replace("1.20.1", "1.19.0")
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), makefile_text)
        self.assertIn("day6.pinned_infra[Cilium]", _failed_names(findings))

    def test_missing_istio_pin_fails(self):
        makefile_text = _DAY6_MAKEFILE_TEXT.replace("1.31.0", "1.30.0")
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), makefile_text)
        self.assertIn("day6.pinned_infra[Istio]", _failed_names(findings))

    def test_missing_gateway_api_crds_pin_fails(self):
        makefile_text = _DAY6_MAKEFILE_TEXT.replace("v1.6.0", "v1.5.0")
        findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), makefile_text)
        self.assertIn("day6.pinned_infra[Gateway API CRDs]", _failed_names(findings))


class Day6IdentityDriftTests(unittest.TestCase):
    def test_wrong_cluster_name_fails(self):
        import version_check

        with mock.patch.object(version_check.kube, "CLUSTER_NAME", "maops-k8s-day5"):
            findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.identity[kube.CLUSTER_NAME]", _failed_names(findings))

    def test_wrong_validation_namespace_fails(self):
        import version_check

        with mock.patch.object(version_check.kube, "VALIDATION_NAMESPACE", "maops-day5-validation"):
            findings = run_day6_release_checks("0.6.0", _day6_chart_yaml(), _day6_values_yaml(), _DAY6_MAKEFILE_TEXT)
        self.assertIn("day6.identity[kube.VALIDATION_NAMESPACE]", _failed_names(findings))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# DAY7: run_day7_release_checks (the live 0.7.0 contract). The Day 6
# function above stays as the historical v0.6.0 contract.
# --------------------------------------------------------------------------

import copy as _copy

from version_check import run_day7_release_checks

_DIGEST = "kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5"


def _kind(name: str, host_port: int, digest: str = _DIGEST) -> dict:
    return {
        "name": name,
        "nodes": [
            {"role": "control-plane", "image": digest, "extraPortMappings": [{"containerPort": 30080, "hostPort": host_port}]},
            {"role": "worker", "image": digest},
            {"role": "worker", "image": digest},
        ],
    }


_DAY7_VALUES = {
    "images": {w: {"tag": "0.7.0"} for w in ("gateway", "app", "state")},
    "candidate": {"enabled": False},
    "routing": {"mode": "stable"},
}
_DAY7_MAKEFILE = (
    _DAY6_MAKEFILE_TEXT
    + "DAY7_CLUSTER_NAME := maops-k8s-day7\nKIND_CONFIG := kind/cluster-$(CLUSTER_PROFILE).yaml\n"
    + "HELM_RELEASE := maops-kubernetes-platform-$(CLUSTER_PROFILE)\n"
)


def _day7(**overrides):
    args = dict(
        version_file_content="0.7.0",
        chart_yaml={"version": "0.7.0", "appVersion": "0.7.0"},
        values_yaml=_copy.deepcopy(_DAY7_VALUES),
        makefile_text=_DAY7_MAKEFILE,
        kind_day6=_kind("maops-k8s-day6", 18080),
        kind_day7=_kind("maops-k8s-day7", 18081),
    )
    args.update(overrides)
    return run_day7_release_checks(**args)


class Day7ReleaseChecksTests(unittest.TestCase):
    def test_all_pass(self):
        self.assertEqual(_failed_names(_day7()), set())

    def test_real_repository_passes(self):
        import version_check
        import k8s_yaml as _y
        findings = run_day7_release_checks(
            version_check.read_version(),
            _y.load_all(version_check.CHART_YAML.read_text())[0],
            _y.load_all(version_check.VALUES_YAML.read_text())[0],
            version_check.MAKEFILE.read_text(),
            _y.load_all(version_check.KIND_DAY6_CONFIG.read_text())[0],
            _y.load_all(version_check.KIND_DAY7_CONFIG.read_text())[0],
        )
        self.assertEqual(_failed_names(findings), set())

    def test_stale_versions_fail(self):
        self.assertIn("day7.version_file_matches_target", _failed_names(_day7(version_file_content="0.6.0")))
        self.assertIn("day7.chart_version_matches_target", _failed_names(_day7(chart_yaml={"version": "0.6.0", "appVersion": "0.7.0"})))
        values = _copy.deepcopy(_DAY7_VALUES)
        values["images"]["gateway"]["tag"] = "0.6.0"
        self.assertIn("day7.values.images.gateway.tag_matches_target", _failed_names(_day7(values_yaml=values)))

    def test_candidate_enabled_by_default_fails(self):
        values = _copy.deepcopy(_DAY7_VALUES)
        values["candidate"]["enabled"] = True
        self.assertIn("day7.values.default_is_stable_only", _failed_names(_day7(values_yaml=values)))
        values = _copy.deepcopy(_DAY7_VALUES)
        values["routing"]["stableWeight"] = 100
        self.assertIn("day7.values.default_is_stable_only", _failed_names(_day7(values_yaml=values)))

    def test_shared_host_port_fails(self):
        failed = _failed_names(_day7(kind_day7=_kind("maops-k8s-day7", 18080)))
        self.assertIn("day7.kind_config.host_port", failed)

    def test_profile_drift_fails(self):
        import kube
        profiles = _copy.deepcopy(kube.PROFILES)
        profiles["day7"]["host_port"] = 18080
        failed = _failed_names(_day7(profiles=profiles))
        self.assertIn("day7.identity[PROFILES['day7'].host_port]", failed)
        self.assertIn("day7.identity.host_ports_distinct", failed)
        profiles = _copy.deepcopy(kube.PROFILES)
        profiles["day6"]["cluster_name"] = "maops-k8s-day7"
        self.assertIn("day7.identity[PROFILES['day6'].cluster_name]", _failed_names(_day7(profiles=profiles)))

    def test_unpinned_or_different_kind_image_fails(self):
        self.assertIn("day7.kind_config.pinned_digest_matches_day6", _failed_names(_day7(kind_day7=_kind("maops-k8s-day7", 18081, "kindest/node:latest"))))
        other = _DIGEST.replace("3489c7", "000000")
        self.assertIn("day7.kind_config.pinned_digest_matches_day6", _failed_names(_day7(kind_day7=_kind("maops-k8s-day7", 18081, other))))

    def test_makefile_wiring_missing_fails(self):
        self.assertIn("day7.makefile[DAY7_CLUSTER_NAME := maops-k8s-day7]", _failed_names(_day7(makefile_text=_DAY6_MAKEFILE_TEXT)))

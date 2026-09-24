"""
Proves charts/maops-kubernetes-platform/values.schema.json rejects
controlled invalid fixtures, by shelling out to the real `helm
template` (helm is one of this project's expected local CLIs - see
.claude/CLAUDE.md's ground rules, the same category as `kubectl
kustomize` in tests exercising scripts/manifest_check.py). Never
contacts a live cluster - `helm template` is a pure local render.

Each test overrides exactly one value via `--set` and asserts helm
refuses to render (nonzero exit, schema-validation error text) - this
is what proves the schema is actually wired up and enforced, not just
present as an unused file.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

CHART_DIR = str(Path(__file__).resolve().parent.parent / "charts" / "maops-kubernetes-platform")


def _template(*set_args: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "schema-check", CHART_DIR]
    for arg in set_args:
        cmd += ["--set", arg]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class ValidBaselineTests(unittest.TestCase):
    def test_default_values_render_successfully(self):
        result = _template()
        self.assertEqual(result.returncode, 0, result.stderr)


class InvalidImageTagTests(unittest.TestCase):
    def test_empty_gateway_tag_rejected(self):
        result = _template("images.gateway.tag=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("images/gateway/tag", result.stderr)

    def test_tag_with_illegal_characters_rejected(self):
        result = _template("images.app.tag=not a valid tag!")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("images/app/tag", result.stderr)

    def test_invalid_pull_policy_rejected(self):
        result = _template("images.state.pullPolicy=Sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("images/state/pullPolicy", result.stderr)


class InvalidReplicaCountTests(unittest.TestCase):
    def test_zero_gateway_replicas_rejected(self):
        result = _template("gateway.replicas=0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gateway/replicas", result.stderr)

    def test_state_replicas_above_one_rejected(self):
        """DAY4/DAY5 invariant carried into the Day 6 schema: maops-state
        is single-writer only, never horizontally scaled - see
        docs/architecture.md. Schema enforces this statically, not just
        by convention."""
        result = _template("state.replicas=2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("state/replicas", result.stderr)

    def test_negative_app_replicas_rejected(self):
        result = _template("app.replicas=-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app/replicas", result.stderr)


class InvalidPortTests(unittest.TestCase):
    def test_port_above_65535_rejected(self):
        result = _template("app.containerPort=99999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app/containerPort", result.stderr)

    def test_zero_port_rejected(self):
        result = _template("gateway.containerPort=0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gateway/containerPort", result.stderr)


class EmptyRequiredValueTests(unittest.TestCase):
    def test_empty_routing_hostname_rejected(self):
        result = _template("routing.hostname=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routing/hostname", result.stderr)

    def test_empty_gateway_name_rejected(self):
        result = _template("routing.gateway.name=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routing/gateway/name", result.stderr)

    def test_empty_diagnostics_service_account_rejected(self):
        result = _template("validation.diagnostics.serviceAccountName=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("validation/diagnostics/serviceAccountName", result.stderr)

    def test_empty_release_namespace_rejected(self):
        result = _template("release.namespace=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release/namespace", result.stderr)


if __name__ == "__main__":
    unittest.main()

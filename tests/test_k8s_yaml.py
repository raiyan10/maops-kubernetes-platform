"""Unit tests for the dependency-free YAML-subset loader (scripts/k8s_yaml.py)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import k8s_yaml


class LoaderTests(unittest.TestCase):
    def test_simple_mapping(self):
        text = "a: 1\nb: two\nc: true\nd: null\n"
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"a": 1, "b": "two", "c": True, "d": None})

    def test_nested_mapping(self):
        text = "metadata:\n  name: foo\n  namespace: bar\n"
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"metadata": {"name": "foo", "namespace": "bar"}})

    def test_scalar_sequence(self):
        text = "drop:\n- ALL\n"
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"drop": ["ALL"]})

    def test_sequence_of_mappings_aligned_with_key(self):
        text = "containers:\n- name: app\n  image: example:1\n  ports:\n  - containerPort: 8080\n"
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(
            doc,
            {
                "containers": [
                    {"name": "app", "image": "example:1", "ports": [{"containerPort": 8080}]}
                ]
            },
        )

    def test_deeply_nested_sequence_item_with_map_value(self):
        text = (
            "containers:\n"
            "- envFrom:\n"
            "  - configMapRef:\n"
            "      name: app-config\n"
            "  image: example:1\n"
            "  imagePullPolicy: IfNotPresent\n"
        )
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(
            doc,
            {
                "containers": [
                    {
                        "envFrom": [{"configMapRef": {"name": "app-config"}}],
                        "image": "example:1",
                        "imagePullPolicy": "IfNotPresent",
                    }
                ]
            },
        )

    def test_multi_document_stream(self):
        text = "kind: A\n---\nkind: B\n"
        docs = k8s_yaml.load_all(text)
        self.assertEqual([d["kind"] for d in docs], ["A", "B"])

    def test_value_with_colon_is_not_split(self):
        text = "image: repo/name:1.2.3\n"
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"image": "repo/name:1.2.3"})

    def test_quoted_string_value(self):
        text = 'version: "0.1.0"\n'
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"version": "0.1.0"})

    # -- DAY1-TEST-M1: parser must fail closed on unconsumed/malformed input --

    def test_mis_indented_sibling_key_raises(self):
        # Reproduces the independent review's PoC: securityContext: is
        # indented one space short of its sibling keys under "- name: app",
        # so it falls outside the block the recursive descent parser
        # actually consumes.
        text = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "      - name: app\n"
            "        image: x\n"
            "       securityContext:\n"
            "          allowPrivilegeEscalation: false\n"
            "          readOnlyRootFilesystem: true\n"
        )
        with self.assertRaises(ValueError):
            k8s_yaml.load_all(text)

    def test_trailing_unparseable_content_raises(self):
        text = "a: 1\nb: 2\n   ~garbage~ line\n"
        with self.assertRaises(ValueError):
            k8s_yaml.load_all(text)

    # -- DAY1-TEST-M2: flow-style YAML must be explicitly rejected --

    def test_flow_style_mapping_value_raises(self):
        text = "capabilities: {drop: [ALL]}\n"
        with self.assertRaises(ValueError):
            k8s_yaml.load_all(text)

    def test_flow_style_sequence_value_raises(self):
        text = "list: [a, b, c]\n"
        with self.assertRaises(ValueError):
            k8s_yaml.load_all(text)

    def test_flow_style_bare_sequence_item_raises(self):
        text = "containers:\n- [a, b, c]\n"
        with self.assertRaises(ValueError):
            k8s_yaml.load_all(text)

    # -- DAY1-TEST-L4: trusted-input contract - quoted type-ambiguous
    # scalars stay strings, matching what kubectl kustomize always emits --

    def test_quoted_float_looking_value_stays_string(self):
        text = 'version: "1.0"\n'
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"version": "1.0"})

    def test_quoted_yes_stays_string(self):
        text = 'flag: "yes"\n'
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"flag": "yes"})

    def test_quoted_on_stays_string(self):
        text = 'flag: "on"\n'
        [doc] = k8s_yaml.load_all(text)
        self.assertEqual(doc, {"flag": "on"})

    def test_real_rendered_manifest_parses_into_nine_documents(self):
        # Day 3: Namespace, 2 ConfigMaps, 2 Deployments, 2 Services, 2
        # PodDisruptionBudgets (the runtime Secret is deliberately never
        # rendered by k8s/base).
        base = Path(__file__).resolve().parent.parent / "k8s" / "base"
        import subprocess

        result = subprocess.run(
            ["kubectl", "kustomize", str(base)], check=True, capture_output=True, text=True
        )
        docs = k8s_yaml.load_all(result.stdout)
        kinds = sorted(d.get("kind") for d in docs)
        self.assertEqual(
            kinds,
            [
                "ConfigMap",
                "ConfigMap",
                "Deployment",
                "Deployment",
                "Namespace",
                "PodDisruptionBudget",
                "PodDisruptionBudget",
                "Service",
                "Service",
            ],
        )


if __name__ == "__main__":
    unittest.main()

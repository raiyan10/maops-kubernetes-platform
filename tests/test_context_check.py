"""
Docker/Kubernetes-free unit tests for scripts/context_check.py.

DAY3-INT-M2: this is the fail-closed gate that must run before any
namespace apply, Secret bootstrap, image load, or deploy step. These
tests prove it actually verifies live topology (node count, control-
plane/worker split, Ready state, node version) rather than merely
trusting the constants, and that a prefix-collision cluster/node
identity is rejected rather than accepted by a broad prefix match.

Monkeypatches every kube/scheduling_check collaborator so these tests
never touch a real cluster.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import context_check
import scheduling_check


def _node(name: str, control_plane: bool, ready: bool = True) -> dict:
    labels = {"node-role.kubernetes.io/control-plane": ""} if control_plane else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _healthy_nodes() -> list[dict]:
    return [
        _node("maops-k8s-day3-control-plane", control_plane=True),
        _node("maops-k8s-day3-worker", control_plane=False),
        _node("maops-k8s-day3-worker2", control_plane=False),
    ]


class ContextCheckTopologyTests(unittest.TestCase):
    def setUp(self):
        context_check.results = []
        scheduling_check.results = []
        patcher = mock.patch.object(context_check.kube, "verify_context")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _run_with_nodes(self, nodes: list[dict], version: str = context_check.EXPECTED_K8S_VERSION) -> int:
        def fake_get_json(*args, **_kwargs):
            if "nodes" in args:
                return {"items": nodes}
            return {}

        with mock.patch.object(scheduling_check, "get_json", side_effect=fake_get_json):
            with mock.patch.object(context_check, "get_json", return_value={"serverVersion": {"gitVersion": version}}):
                return context_check.main()

    def test_healthy_topology_passes(self):
        exit_code = self._run_with_nodes(_healthy_nodes())
        self.assertEqual(exit_code, 0)

    def test_only_one_node_fails(self):
        exit_code = self._run_with_nodes([_node("maops-k8s-day3-control-plane", control_plane=True)])
        self.assertEqual(exit_code, 1)
        all_results = context_check.results + scheduling_check.results
        self.assertTrue(any(not ok and "node(s)" in msg for ok, msg in all_results))

    def test_wrong_worker_count_fails(self):
        nodes = [
            _node("maops-k8s-day3-control-plane", control_plane=True),
            _node("maops-k8s-day3-worker", control_plane=False),
        ]
        exit_code = self._run_with_nodes(nodes)
        self.assertEqual(exit_code, 1)

    def test_multiple_control_planes_fails(self):
        nodes = [
            _node("maops-k8s-day3-control-plane", control_plane=True),
            _node("maops-k8s-day3-control-plane2", control_plane=True),
            _node("maops-k8s-day3-worker", control_plane=False),
        ]
        exit_code = self._run_with_nodes(nodes)
        self.assertEqual(exit_code, 1)
        all_results = context_check.results + scheduling_check.results
        self.assertTrue(any(not ok and "control-plane" in msg for ok, msg in all_results))

    def test_non_ready_node_fails(self):
        nodes = [
            _node("maops-k8s-day3-control-plane", control_plane=True),
            _node("maops-k8s-day3-worker", control_plane=False, ready=False),
            _node("maops-k8s-day3-worker2", control_plane=False),
        ]
        exit_code = self._run_with_nodes(nodes)
        self.assertEqual(exit_code, 1)
        all_results = context_check.results + scheduling_check.results
        self.assertTrue(any(not ok and "Ready" in msg for ok, msg in all_results))

    def test_wrong_kubernetes_version_fails(self):
        exit_code = self._run_with_nodes(_healthy_nodes(), version="v1.30.0")
        self.assertEqual(exit_code, 1)
        self.assertTrue(any(not ok and "server version" in msg for ok, msg in context_check.results))


class ContextCheckPrefixCollisionTests(unittest.TestCase):
    """DAY3-INT-M2: a prefix-collision cluster/node identity (e.g. a
    "staging" cluster whose name extends maops-k8s-day3-) must be
    rejected by the fail-closed context gate before any topology check
    even runs - this is kube.verify_context()'s own responsibility,
    exercised here through context_check.main()."""

    def setUp(self):
        context_check.results = []
        scheduling_check.results = []

    def test_prefix_collision_short_circuits_before_topology_check(self):
        with mock.patch.object(
            context_check.kube,
            "verify_context",
            side_effect=RuntimeError(
                "node names ['maops-k8s-day3-staging-control-plane'] do not all belong to expected cluster "
                "'maops-k8s-day3' - refusing to proceed: this context may point at the wrong cluster"
            ),
        ):
            with mock.patch.object(scheduling_check, "check_node_topology") as mock_topology:
                exit_code = context_check.main()
        self.assertEqual(exit_code, 1)
        mock_topology.assert_not_called()


if __name__ == "__main__":
    unittest.main()

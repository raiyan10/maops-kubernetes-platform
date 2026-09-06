"""
Docker/Kubernetes-free unit tests for scripts/scheduling_check.py's pure
logic: control-plane exclusion, worker discovery, and worker skew
calculation. Monkeypatches kube.get_json so these tests never touch a
real cluster.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scheduling_check


def _node(name: str, control_plane: bool, ready: bool = True) -> dict:
    labels = {"node-role.kubernetes.io/control-plane": ""} if control_plane else {}
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    return {"metadata": {"name": name, "labels": labels}, "status": {"conditions": [condition]}}


def _pod(name: str, node_name: str, ready: bool = True) -> dict:
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    return {"metadata": {"name": name}, "spec": {"nodeName": node_name}, "status": {"conditions": [condition]}}


class NodeTopologyTests(unittest.TestCase):
    def setUp(self):
        scheduling_check.results = []

    def test_identifies_control_plane_and_workers_dynamically(self):
        nodes = {
            "items": [
                _node("maops-k8s-day3-control-plane", control_plane=True),
                _node("maops-k8s-day3-worker", control_plane=False),
                _node("maops-k8s-day3-worker2", control_plane=False),
            ]
        }
        with mock.patch.object(scheduling_check, "get_json", return_value=nodes):
            control_planes, workers = scheduling_check.check_node_topology()
        self.assertEqual(control_planes, ["maops-k8s-day3-control-plane"])
        self.assertEqual(set(workers), {"maops-k8s-day3-worker", "maops-k8s-day3-worker2"})

    def test_wrong_node_count_fails(self):
        nodes = {"items": [_node("only-node", control_plane=True)]}
        with mock.patch.object(scheduling_check, "get_json", return_value=nodes):
            scheduling_check.check_node_topology()
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("node(s)" in msg for msg in failed))

    def test_not_ready_node_fails(self):
        nodes = {
            "items": [
                _node("maops-k8s-day3-control-plane", control_plane=True),
                _node("maops-k8s-day3-worker", control_plane=False, ready=False),
                _node("maops-k8s-day3-worker2", control_plane=False),
            ]
        }
        with mock.patch.object(scheduling_check, "get_json", return_value=nodes):
            scheduling_check.check_node_topology()
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("Ready" in msg for msg in failed))


class WorkloadSchedulingTests(unittest.TestCase):
    def setUp(self):
        scheduling_check.results = []
        self.control_planes = ["cp"]
        self.workers = ["w1", "w2"]

    def _run(self, pods):
        with mock.patch.object(scheduling_check, "get_json", return_value={"items": pods}):
            scheduling_check.check_workload_scheduling("gateway", "irrelevant-selector", self.control_planes, self.workers)

    def test_even_2_1_split_passes(self):
        pods = [_pod("p1", "w1"), _pod("p2", "w1"), _pod("p3", "w2")]
        self._run(pods)
        self.assertTrue(all(ok for ok, _ in scheduling_check.results))

    def test_even_1_2_split_passes(self):
        pods = [_pod("p1", "w1"), _pod("p2", "w2"), _pod("p3", "w2")]
        self._run(pods)
        self.assertTrue(all(ok for ok, _ in scheduling_check.results))

    def test_pod_on_control_plane_fails(self):
        pods = [_pod("p1", "cp"), _pod("p2", "w1"), _pod("p3", "w2")]
        self._run(pods)
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("control-plane" in msg for msg in failed))

    def test_all_on_one_worker_fails_skew(self):
        pods = [_pod("p1", "w1"), _pod("p2", "w1"), _pod("p3", "w1")]
        self._run(pods)
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("skew" in msg for msg in failed))

    def test_unused_worker_fails(self):
        pods = [_pod("p1", "w1"), _pod("p2", "w1"), _pod("p3", "w1")]
        self._run(pods)
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("both worker nodes" in msg for msg in failed))

    def test_not_ready_pod_fails_ready_count(self):
        pods = [_pod("p1", "w1"), _pod("p2", "w1", ready=False), _pod("p3", "w2")]
        self._run(pods)
        failed = [msg for ok, msg in scheduling_check.results if not ok]
        self.assertTrue(any("Ready" in msg for msg in failed))


if __name__ == "__main__":
    unittest.main()

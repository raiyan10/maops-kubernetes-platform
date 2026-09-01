"""
Docker/Kubernetes-free unit tests for scripts/cluster_check.py's failure
handling around kubectl exec (DAY1-TEST-L3).

Monkeypatches exec_in_pod/get_json so these tests never touch a real
cluster; they assert that a subprocess.CalledProcessError from a
plausible kubectl exec failure is caught and routed into the script's
normal record(False, ...) reporting path instead of propagating as an
unhandled traceback.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cluster_check


class ExecFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        cluster_check.results = []

    def test_configmap_consumption_records_clean_failure_on_exec_error(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        exc = subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="pod not found")
        with mock.patch.object(cluster_check, "get_json", return_value={"data": {"APP_MESSAGE": "hi"}}):
            with mock.patch.object(cluster_check, "exec_in_pod", side_effect=exc):
                cluster_check.check_configmap_consumption(pods)

        self.assertEqual(len(cluster_check.results), 1)
        ok, msg = cluster_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("pod not found", msg)
        self.assertIn("maops-app-abc", msg)

    def test_runtime_uid_gid_records_clean_failure_on_exec_error(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        exc = subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="executable file not found in $PATH")
        with mock.patch.object(cluster_check, "exec_in_pod", side_effect=exc):
            cluster_check.check_runtime_uid_gid(pods)

        self.assertEqual(len(cluster_check.results), 1)
        ok, msg = cluster_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("executable file not found", msg)

    def test_configmap_consumption_no_traceback_and_still_passes_on_success(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        with mock.patch.object(cluster_check, "get_json", return_value={"data": {"APP_MESSAGE": "hi"}}):
            with mock.patch.object(cluster_check, "exec_in_pod", return_value="hi"):
                cluster_check.check_configmap_consumption(pods)

        ok, msg = cluster_check.results[-1]
        self.assertTrue(ok, msg)

    def test_runtime_uid_gid_still_passes_on_success(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        with mock.patch.object(cluster_check, "exec_in_pod", return_value="10001 10001"):
            cluster_check.check_runtime_uid_gid(pods)

        ok, msg = cluster_check.results[-1]
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()

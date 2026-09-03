"""
Docker/Kubernetes-free unit tests for scripts/cluster_check.py's failure
handling around kubectl exec (DAY1-TEST-L3, carried forward to Day 2's
two-workload version).

Monkeypatches exec_in_pod/get_json/run so these tests never touch a real
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
        pods = [{"metadata": {"name": "maops-gateway-abc"}}]
        exc = subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="pod not found")
        with mock.patch.object(cluster_check, "get_json", return_value={"data": {"APP_ENVIRONMENT": "day2-service-discovery"}}):
            with mock.patch.object(cluster_check, "exec_in_pod", side_effect=exc):
                cluster_check.check_configmap_consumption("gateway", "maops-gateway-config", pods)

        self.assertEqual(len(cluster_check.results), 1)
        ok, msg = cluster_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("pod not found", msg)
        self.assertIn("maops-gateway-abc", msg)

    def test_runtime_uid_gid_records_clean_failure_on_exec_error(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        exc = subprocess.CalledProcessError(1, ["kubectl", "exec"], stderr="executable file not found in $PATH")
        with mock.patch.object(cluster_check, "exec_in_pod", side_effect=exc):
            cluster_check.check_runtime_uid_gid("app", pods)

        self.assertEqual(len(cluster_check.results), 1)
        ok, msg = cluster_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("executable file not found", msg)

    def test_configmap_consumption_no_traceback_and_still_passes_on_success(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        with mock.patch.object(cluster_check, "get_json", return_value={"data": {"APP_ENVIRONMENT": "day2-service-discovery"}}):
            with mock.patch.object(cluster_check, "exec_in_pod", return_value="day2-service-discovery"):
                cluster_check.check_configmap_consumption("app", "maops-app-config", pods)

        ok, msg = cluster_check.results[-1]
        self.assertTrue(ok, msg)

    def test_runtime_uid_gid_still_passes_on_success(self):
        pods = [{"metadata": {"name": "maops-gateway-abc"}}]
        with mock.patch.object(cluster_check, "exec_in_pod", return_value="10001 10001"):
            cluster_check.check_runtime_uid_gid("gateway", pods)

        ok, msg = cluster_check.results[-1]
        self.assertTrue(ok, msg)

    def test_configmap_consumption_no_pods_records_clean_failure(self):
        cluster_check.check_configmap_consumption("gateway", "maops-gateway-config", [])
        ok, msg = cluster_check.results[-1]
        self.assertFalse(ok)
        self.assertIn("no pods available", msg)


class SecretVolumeMountFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        cluster_check.results = []

    def test_secret_volume_mismatch_records_failure(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        pod_json = {
            "spec": {
                "volumes": [{"name": "internal-auth", "secret": {"secretName": "wrong-secret"}}],
                "containers": [{"volumeMounts": [{"name": "internal-auth", "mountPath": "/var/run/secrets/maops", "readOnly": True}]}],
            }
        }
        with mock.patch.object(cluster_check, "get_json", return_value=pod_json):
            cluster_check.check_secret_volume_mount("app", pods)

        failures = [msg for ok, msg in cluster_check.results if not ok]
        self.assertTrue(any("wrong-secret" in msg for msg in failures))

    def test_secret_volume_correct_passes(self):
        pods = [{"metadata": {"name": "maops-app-abc"}}]
        pod_json = {
            "spec": {
                "volumes": [{"name": "internal-auth", "secret": {"secretName": "maops-internal-auth"}}],
                "containers": [{"volumeMounts": [{"name": "internal-auth", "mountPath": "/var/run/secrets/maops", "readOnly": True}]}],
            }
        }
        with mock.patch.object(cluster_check, "get_json", return_value=pod_json):
            cluster_check.check_secret_volume_mount("app", pods)

        failures = [msg for ok, msg in cluster_check.results if not ok]
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()

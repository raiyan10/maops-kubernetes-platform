"""
Docker/Kubernetes-free unit tests for scripts/portforward.py's kubectl
`--kubeconfig` argument (Day 4 batch 5 remediation).

Batch 4's live `make day4-check` run reached `discovery-check` for the
first time (batch 3 never got that far) and failed with
`error: context "kind-maops-k8s-day4" does not exist` -
`port_forward()`'s kubectl subprocess never passed `--kubeconfig` at
all, unlike every other kubectl call in this project (`kube.run()`), so
it silently fell back to the default `~/.kube/config`, which never
contains this project's isolated per-day context.

These tests prove the fix at the actual subprocess boundary - the
constructed kubectl argv `subprocess.Popen()` actually receives - never
the real socket/process lifecycle (tests/test_portforward_signal.py
already covers the SIGTERM-safety primitive separately, with real
signals; this file never sends one).

Monkeypatches `subprocess.Popen`, `_wait_connectable`, and `_terminate`
so these tests never spawn a real kubectl process or wait on a real
socket connection.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import kube
import portforward


class _FakeProc:
    """Minimal stand-in for subprocess.Popen's return value - just
    enough surface (`pid`, `poll()`, `returncode`) for port_forward()'s
    own code path once `_wait_connectable`/`_terminate` are mocked out."""

    def __init__(self):
        self.pid = 99999
        self.returncode = None
        self.stdout = None
        self.stderr = None

    def poll(self):
        return self.returncode


def _drive_port_forward(**kwargs):
    """Runs one full port_forward() context to completion, capturing the
    exact argv passed to subprocess.Popen(), without spawning a real
    process or touching a real socket. Returns (cmd, local_port,
    mock_terminate) so callers can assert on any of the three."""
    captured: dict = {}

    def fake_popen(cmd, **_kw):
        captured["cmd"] = cmd
        return _FakeProc()

    with mock.patch.object(portforward.subprocess, "Popen", side_effect=fake_popen):
        with mock.patch.object(portforward, "_wait_connectable"):
            with mock.patch.object(portforward, "_terminate") as mock_terminate:
                with portforward.port_forward(**kwargs) as local_port:
                    pass
    return captured["cmd"], local_port, mock_terminate


class KubeconfigArgumentTests(unittest.TestCase):
    def test_default_kubeconfig_comes_from_kube_module_configured_path(self):
        with mock.patch.object(portforward.kube, "KUBECONFIG_PATH", "/fake/day4.config"):
            cmd, _local_port, _terminate = _drive_port_forward(
                kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-gateway", remote_port=8080
            )
        self.assertIn("--kubeconfig", cmd)
        self.assertEqual(cmd[cmd.index("--kubeconfig") + 1], "/fake/day4.config")

    def test_kubeconfig_is_read_at_call_time_not_import_time(self):
        """A test/caller overriding kube.KUBECONFIG_PATH AFTER this
        module was imported must still be honored - the default must not
        be captured once at function-definition time."""
        with mock.patch.object(portforward.kube, "KUBECONFIG_PATH", "/fake/first.config"):
            cmd1, _lp1, _t1 = _drive_port_forward(
                kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-gateway", remote_port=8080
            )
        with mock.patch.object(portforward.kube, "KUBECONFIG_PATH", "/fake/second.config"):
            cmd2, _lp2, _t2 = _drive_port_forward(
                kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-gateway", remote_port=8080
            )
        self.assertEqual(cmd1[cmd1.index("--kubeconfig") + 1], "/fake/first.config")
        self.assertEqual(cmd2[cmd2.index("--kubeconfig") + 1], "/fake/second.config")

    def test_explicit_override_is_honored_over_the_default(self):
        with mock.patch.object(portforward.kube, "KUBECONFIG_PATH", "/fake/default.config"):
            cmd, _local_port, _terminate = _drive_port_forward(
                kube_context="kind-maops-k8s-day4",
                namespace="maops-platform",
                name="maops-gateway",
                remote_port=8080,
                kubeconfig_path="/explicit/override.config",
            )
        self.assertEqual(cmd[cmd.index("--kubeconfig") + 1], "/explicit/override.config")

    def test_context_namespace_and_service_resource_args_are_preserved(self):
        cmd, _local_port, _terminate = _drive_port_forward(
            kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-gateway", remote_port=8080
        )
        self.assertEqual(cmd[0], "kubectl")
        self.assertIn("--context", cmd)
        self.assertEqual(cmd[cmd.index("--context") + 1], "kind-maops-k8s-day4")
        self.assertIn("-n", cmd)
        self.assertEqual(cmd[cmd.index("-n") + 1], "maops-platform")
        self.assertIn("port-forward", cmd)
        self.assertIn("service/maops-gateway", cmd)

    def test_pod_resource_kind_is_preserved(self):
        cmd, _local_port, _terminate = _drive_port_forward(
            kube_context="kind-maops-k8s-day4",
            namespace="maops-platform",
            name="maops-gateway-abc123",
            remote_port=8080,
            resource_kind="pod",
        )
        self.assertIn("pod/maops-gateway-abc123", cmd)

    def test_local_port_to_remote_port_mapping_is_preserved(self):
        cmd, local_port, _terminate = _drive_port_forward(
            kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-state", remote_port=8080
        )
        self.assertIn(f"{local_port}:8080", cmd)


class ExistingCallerCompatibilityTests(unittest.TestCase):
    """Every one of this project's ~26 port_forward() call sites invokes
    it positionally as (context, namespace, name, port[, resource_kind=...])
    and never passes kubeconfig_path. The fix must work unchanged for
    exactly that calling convention - no caller needs to change."""

    def test_positional_call_without_a_kubeconfig_argument_still_works(self):
        cmd, _local_port, _terminate = _drive_port_forward_positional()
        self.assertIn("--kubeconfig", cmd)
        self.assertEqual(cmd[cmd.index("--kubeconfig") + 1], kube.KUBECONFIG_PATH)


def _drive_port_forward_positional():
    captured: dict = {}

    def fake_popen(cmd, **_kw):
        captured["cmd"] = cmd
        return _FakeProc()

    with mock.patch.object(portforward.subprocess, "Popen", side_effect=fake_popen):
        with mock.patch.object(portforward, "_wait_connectable"):
            with mock.patch.object(portforward, "_terminate") as mock_terminate:
                with portforward.port_forward(kube.CONTEXT, kube.NAMESPACE, "maops-gateway", 8080) as local_port:
                    pass
    return captured["cmd"], local_port, mock_terminate


class CleanupBehaviorPreservedTests(unittest.TestCase):
    """DAY1-INT-M1: cleanup must still run on every exit path - this
    fix touches only argv construction, never the cleanup mechanism, but
    proves that remains true rather than assuming it."""

    def test_terminate_is_called_on_normal_exit(self):
        _cmd, _local_port, mock_terminate = _drive_port_forward(
            kube_context="kind-maops-k8s-day4", namespace="maops-platform", name="maops-gateway", remote_port=8080
        )
        mock_terminate.assert_called_once()

    def test_terminate_is_called_when_the_body_raises(self):
        captured: dict = {}

        def fake_popen(cmd, **_kw):
            captured["cmd"] = cmd
            return _FakeProc()

        with mock.patch.object(portforward.subprocess, "Popen", side_effect=fake_popen):
            with mock.patch.object(portforward, "_wait_connectable"):
                with mock.patch.object(portforward, "_terminate") as mock_terminate:
                    with self.assertRaises(RuntimeError):
                        with portforward.port_forward(
                            kube_context="kind-maops-k8s-day4",
                            namespace="maops-platform",
                            name="maops-gateway",
                            remote_port=8080,
                        ):
                            raise RuntimeError("boom")
        mock_terminate.assert_called_once()
        self.assertIn("--kubeconfig", captured["cmd"])


if __name__ == "__main__":
    unittest.main()

"""
Docker/Kubernetes-free unit tests for scripts/secret_check.py.

DAY4-TEST-L1: this project-wide pre-existing gap (no dedicated test
file for secret_check.py at all) now also covers the new Day 4
functions - `check_gateway_never_gets_state_token`,
`check_state_secret_shape`, `get_state_token`, `check_state_pod_mounts` -
mocking `get_existing_secret`/`get_existing_state_secret`/`get_json` at
the same collaborator boundary the rest of the project's tests use.
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import secret_check


def _secret_json(key: str, value: str | None) -> dict:
    data = {}
    if value is not None:
        data[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return {"data": data}


class _ResultsMixin:
    def setUp(self):
        secret_check.results = []


class GetTokenTests(_ResultsMixin, unittest.TestCase):
    def test_missing_secret_returns_none(self):
        with mock.patch.object(secret_check, "get_existing_secret", return_value=None):
            self.assertIsNone(secret_check.get_token())

    def test_missing_key_returns_none(self):
        with mock.patch.object(secret_check, "get_existing_secret", return_value=_secret_json("internal-token", None)):
            self.assertIsNone(secret_check.get_token())

    def test_valid_secret_returns_decoded_bytes(self):
        with mock.patch.object(secret_check, "get_existing_secret", return_value=_secret_json("internal-token", "the-real-value")):
            self.assertEqual(secret_check.get_token(), b"the-real-value")


class GetStateTokenTests(_ResultsMixin, unittest.TestCase):
    def test_missing_secret_returns_none(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=None):
            self.assertIsNone(secret_check.get_state_token())

    def test_missing_key_returns_none(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=_secret_json("state-token", None)):
            self.assertIsNone(secret_check.get_state_token())

    def test_valid_secret_returns_decoded_bytes(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=_secret_json("state-token", "the-real-state-value")):
            self.assertEqual(secret_check.get_state_token(), b"the-real-state-value")


class CheckSecretShapeTests(_ResultsMixin, unittest.TestCase):
    def test_missing_secret_records_false(self):
        with mock.patch.object(secret_check, "get_existing_secret", return_value=None):
            ok = secret_check.check_secret_shape()
        self.assertFalse(ok)
        self.assertTrue(any(not r_ok for r_ok, _ in secret_check.results))

    def test_valid_secret_records_true(self):
        with mock.patch.object(secret_check, "get_existing_secret", return_value=_secret_json("internal-token", "x")):
            ok = secret_check.check_secret_shape()
        self.assertTrue(ok)


class CheckStateSecretShapeTests(_ResultsMixin, unittest.TestCase):
    def test_missing_state_secret_records_false(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=None):
            ok = secret_check.check_state_secret_shape()
        self.assertFalse(ok)
        messages = " ".join(m for _, m in secret_check.results)
        self.assertIn("maops-state-auth", messages)

    def test_valid_state_secret_records_true(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=_secret_json("state-token", "x")):
            ok = secret_check.check_state_secret_shape()
        self.assertTrue(ok)

    def test_malformed_state_secret_records_false(self):
        with mock.patch.object(secret_check, "get_existing_state_secret", return_value=_secret_json("state-token", "")):
            ok = secret_check.check_state_secret_shape()
        self.assertFalse(ok)


class GatewayNeverGetsStateTokenTests(_ResultsMixin, unittest.TestCase):
    """DAY4: gateway must never mount the state-auth Secret - only app
    and state ever hold that credential."""

    def _pod(self, volumes):
        return {"spec": {"volumes": volumes}}

    def test_no_gateway_pods_records_false(self):
        secret_check.check_gateway_never_gets_state_token([])
        self.assertTrue(any(not ok for ok, _ in secret_check.results))

    def test_gateway_pod_without_state_volume_passes(self):
        pods = [{"metadata": {"name": "gw-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod([{"name": "internal-auth"}])):
            secret_check.check_gateway_never_gets_state_token(pods)
        self.assertTrue(all(ok for ok, _ in secret_check.results))

    def test_gateway_pod_with_state_volume_fails(self):
        pods = [{"metadata": {"name": "gw-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod([{"name": "internal-auth"}, {"name": "state-auth"}])):
            secret_check.check_gateway_never_gets_state_token(pods)
        self.assertTrue(any(not ok for ok, _ in secret_check.results))


class StatePodMountsTests(_ResultsMixin, unittest.TestCase):
    def _pod(self, secret_name="maops-state-auth", mount_path="/var/run/secrets/maops-state", read_only=True):
        return {
            "spec": {
                "volumes": [{"name": "state-auth", "secret": {"secretName": secret_name}}],
                "containers": [
                    {
                        "volumeMounts": [{"name": "state-auth", "mountPath": mount_path, "readOnly": read_only}],
                    }
                ],
            }
        }

    def test_correct_mount_passes(self):
        pods = [{"metadata": {"name": "app-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod()):
            secret_check._state_mount_findings("app", pods)
        self.assertTrue(all(ok for ok, _ in secret_check.results))

    def test_wrong_secret_name_fails(self):
        pods = [{"metadata": {"name": "app-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod(secret_name="wrong-secret")):
            secret_check._state_mount_findings("app", pods)
        self.assertTrue(any(not ok for ok, _ in secret_check.results))

    def test_writable_mount_fails(self):
        pods = [{"metadata": {"name": "app-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod(read_only=False)):
            secret_check._state_mount_findings("app", pods)
        self.assertTrue(any(not ok for ok, _ in secret_check.results))

    def test_wrong_mount_path_fails(self):
        pods = [{"metadata": {"name": "app-0"}}]
        with mock.patch.object(secret_check, "get_json", return_value=self._pod(mount_path="/wrong/path")):
            secret_check._state_mount_findings("app", pods)
        self.assertTrue(any(not ok for ok, _ in secret_check.results))

    def test_no_pods_records_false(self):
        secret_check._state_mount_findings("app", [])
        self.assertTrue(any(not ok for ok, _ in secret_check.results))


if __name__ == "__main__":
    unittest.main()

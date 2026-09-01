"""
Docker/Kubernetes-free unit tests for scripts/http_checks.py (DAY1-TEST-M3).

Mocks urllib.request.urlopen so these tests exercise the semantic
assertions - not just "status 200 + valid JSON" - without any real
network call or live cluster.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import http_checks


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_urlopen(status: int, body: str):
    if status == 200:
        return mock.patch.object(
            http_checks.urllib.request, "urlopen", return_value=_FakeResponse(status, body)
        )

    def _raise(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            url="http://127.0.0.1/test", code=status, msg="error", hdrs=None, fp=io.BytesIO(body.encode("utf-8"))
        )

    return mock.patch.object(http_checks.urllib.request, "urlopen", side_effect=_raise)


class CheckEndpointSemanticsTests(unittest.TestCase):
    def test_livez_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez")
        self.assertTrue(ok, msg)

    def test_livez_wrong_but_valid_json_body_fails(self):
        # A 200 + parseable JSON body that is semantically wrong (e.g. a
        # routing bug serving /readyz's body on /livez) must not pass.
        with _mock_urlopen(200, json.dumps({"status": "ready"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez")
        self.assertFalse(ok)
        self.assertIn("alive", msg)

    def test_readyz_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps({"status": "ready"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz")
        self.assertTrue(ok, msg)

    def test_readyz_wrong_but_valid_json_body_fails(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz")
        self.assertFalse(ok)
        self.assertIn("ready", msg)

    def test_config_correct_body_passes(self):
        payload = {
            "APP_NAME": "maops-kubernetes-platform",
            "APP_ENVIRONMENT": "day1-kubernetes-foundation",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Platform (Day 1)",
            "APP_LOG_LEVEL": "info",
        }
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config")
        self.assertTrue(ok, msg)

    def test_config_missing_expected_key_fails(self):
        payload = {
            "APP_NAME": "maops-kubernetes-platform",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Platform (Day 1)",
        }
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config")
        self.assertFalse(ok)
        self.assertIn("missing", msg.lower())

    def test_config_wrong_app_name_fails(self):
        payload = {
            "APP_NAME": "some-other-app",
            "APP_ENVIRONMENT": "day1-kubernetes-foundation",
            "APP_MESSAGE": "Hello from the MAOps Kubernetes Platform (Day 1)",
            "APP_LOG_LEVEL": "info",
        }
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config")
        self.assertFalse(ok)
        self.assertIn("APP_NAME", msg)

    def test_config_wrong_app_message_fails(self):
        payload = {
            "APP_NAME": "maops-kubernetes-platform",
            "APP_ENVIRONMENT": "day1-kubernetes-foundation",
            "APP_MESSAGE": "a different message entirely",
            "APP_LOG_LEVEL": "info",
        }
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config")
        self.assertFalse(ok)
        self.assertIn("APP_MESSAGE", msg)

    def test_root_correct_body_passes(self):
        payload = {"service": "x", "message": "y", "hostname": "h", "uptime_seconds": 1.0}
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/")
        self.assertTrue(ok, msg)

    def test_root_missing_documented_field_fails(self):
        payload = {"service": "x", "message": "y"}
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/")
        self.assertFalse(ok)
        self.assertIn("missing", msg.lower())

    def test_wrong_route_shape_on_livez_fails(self):
        # Simulates a routing bug: /livez accidentally served /config's
        # handler body - a valid 200 JSON object, but the wrong shape.
        with _mock_urlopen(200, json.dumps({"APP_NAME": "x"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez")
        self.assertFalse(ok)

    def test_non_200_status_fails_before_semantics_are_checked(self):
        with _mock_urlopen(503, json.dumps({"status": "starting"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz")
        self.assertFalse(ok)
        self.assertIn("503", msg)

    def test_invalid_json_body_still_fails(self):
        with _mock_urlopen(200, "not json"):
            ok, msg = http_checks.check_endpoint(1234, "/livez")
        self.assertFalse(ok)
        self.assertIn("not valid JSON", msg)


if __name__ == "__main__":
    unittest.main()

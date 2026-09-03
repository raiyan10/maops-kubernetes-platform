"""
Docker/Kubernetes-free unit tests for scripts/http_checks.py (Day 2:
gateway + app roles, plus the /backend endpoint).

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


GATEWAY_CONFIG_PAYLOAD = {
    "BACKEND_HOST": "maops-app",
    "BACKEND_PORT": "8080",
    "BACKEND_TIMEOUT_SECONDS": "3",
    "APP_NAME": "maops-kubernetes-gateway",
    "APP_ENVIRONMENT": "day2-service-discovery",
    "APP_MESSAGE": "Hello from the MAOps Kubernetes Gateway (Day 2)",
    "APP_LOG_LEVEL": "info",
}

APP_CONFIG_PAYLOAD = {
    "APP_NAME": "maops-kubernetes-app",
    "APP_ENVIRONMENT": "day2-service-discovery",
    "APP_MESSAGE": "Hello from the MAOps Kubernetes App (Day 2)",
    "APP_LOG_LEVEL": "info",
}

BACKEND_PAYLOAD = {
    "gateway_hostname": "maops-gateway-abc",
    "backend_service": "maops-kubernetes-app",
    "backend_hostname": "maops-app-xyz",
    "backend_environment": "day2-service-discovery",
}


class CheckEndpointSemanticsTests(unittest.TestCase):
    def test_livez_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez", role="gateway")
        self.assertTrue(ok, msg)

    def test_livez_wrong_but_valid_json_body_fails(self):
        # A 200 + parseable JSON body that is semantically wrong (e.g. a
        # routing bug serving /readyz's body on /livez) must not pass.
        with _mock_urlopen(200, json.dumps({"status": "ready"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez", role="gateway")
        self.assertFalse(ok)
        self.assertIn("alive", msg)

    def test_readyz_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps({"status": "ready"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz", role="app")
        self.assertTrue(ok, msg)

    def test_readyz_wrong_but_valid_json_body_fails(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz", role="app")
        self.assertFalse(ok)
        self.assertIn("ready", msg)

    def test_gateway_config_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps(GATEWAY_CONFIG_PAYLOAD)):
            ok, msg = http_checks.check_endpoint(1234, "/config", role="gateway")
        self.assertTrue(ok, msg)

    def test_gateway_config_missing_expected_key_fails(self):
        payload = dict(GATEWAY_CONFIG_PAYLOAD)
        del payload["BACKEND_PORT"]
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config", role="gateway")
        self.assertFalse(ok)
        self.assertIn("missing", msg.lower())

    def test_gateway_config_wrong_backend_host_fails(self):
        payload = dict(GATEWAY_CONFIG_PAYLOAD)
        payload["BACKEND_HOST"] = "some-other-host"
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config", role="gateway")
        self.assertFalse(ok)
        self.assertIn("BACKEND_HOST", msg)

    def test_app_config_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps(APP_CONFIG_PAYLOAD)):
            ok, msg = http_checks.check_endpoint(1234, "/config", role="app")
        self.assertTrue(ok, msg)

    def test_app_config_wrong_app_name_fails(self):
        payload = dict(APP_CONFIG_PAYLOAD)
        payload["APP_NAME"] = "wrong-name"
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/config", role="app")
        self.assertFalse(ok)
        self.assertIn("APP_NAME", msg)

    def test_root_correct_body_passes(self):
        payload = {"service": "x", "message": "y", "hostname": "h", "uptime_seconds": 1.0}
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/", role="gateway")
        self.assertTrue(ok, msg)

    def test_root_missing_documented_field_fails(self):
        payload = {"service": "x", "message": "y"}
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/", role="app")
        self.assertFalse(ok)
        self.assertIn("missing", msg.lower())

    def test_backend_correct_body_passes(self):
        with _mock_urlopen(200, json.dumps(BACKEND_PAYLOAD)):
            ok, msg = http_checks.check_endpoint(1234, "/backend", role="gateway")
        self.assertTrue(ok, msg)

    def test_backend_missing_field_fails(self):
        payload = dict(BACKEND_PAYLOAD)
        del payload["backend_hostname"]
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/backend", role="gateway")
        self.assertFalse(ok)
        self.assertIn("missing", msg.lower())

    def test_backend_wrong_backend_service_fails(self):
        payload = dict(BACKEND_PAYLOAD)
        payload["backend_service"] = "not-the-app"
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/backend", role="gateway")
        self.assertFalse(ok)
        self.assertIn("backend_service", msg)

    def test_response_containing_token_field_fails_generically(self):
        # Defense in depth: any response that literally mentions "token"
        # anywhere in its JSON is treated as a disclosure regardless of
        # which endpoint served it.
        payload = {"status": "alive", "leaked_token": "should-never-appear"}
        with _mock_urlopen(200, json.dumps(payload)):
            ok, msg = http_checks.check_endpoint(1234, "/livez", role="gateway")
        self.assertFalse(ok)
        self.assertIn("token", msg.lower())

    def test_wrong_route_shape_on_livez_fails(self):
        # Simulates a routing bug: /livez accidentally served /config's
        # handler body - a valid 200 JSON object, but the wrong shape.
        with _mock_urlopen(200, json.dumps({"APP_NAME": "x"})):
            ok, msg = http_checks.check_endpoint(1234, "/livez", role="app")
        self.assertFalse(ok)

    def test_non_200_status_fails_before_semantics_are_checked(self):
        with _mock_urlopen(503, json.dumps({"status": "starting"})):
            ok, msg = http_checks.check_endpoint(1234, "/readyz", role="gateway")
        self.assertFalse(ok)
        self.assertIn("503", msg)

    def test_invalid_json_body_still_fails(self):
        with _mock_urlopen(200, "not json"):
            ok, msg = http_checks.check_endpoint(1234, "/livez", role="gateway")
        self.assertFalse(ok)
        self.assertIn("not valid JSON", msg)


class CheckSafeUnavailableBodyTests(unittest.TestCase):
    """DAY2-INT-L2: scripts/dependency_check.py's outage proof previously
    asserted only `status == 503`, despite its log message claiming "no
    traceback, no token" - these prove the actual body-semantic guard
    used to strengthen that check."""

    def test_correct_safe_body_passes(self):
        ok, _msg = http_checks.check_safe_unavailable_body(json.dumps({"error": "backend unavailable"}))
        self.assertTrue(ok)

    def test_arbitrary_wrong_json_fails(self):
        ok, msg = http_checks.check_safe_unavailable_body(json.dumps({"status": "starting"}))
        self.assertFalse(ok)
        self.assertIn("expected exactly", msg)

    def test_body_containing_token_fails(self):
        ok, msg = http_checks.check_safe_unavailable_body(
            json.dumps({"error": "backend unavailable", "debug_token": "should-never-appear"})
        )
        self.assertFalse(ok)

    def test_body_containing_traceback_like_content_fails(self):
        ok, msg = http_checks.check_safe_unavailable_body(
            json.dumps({"error": "backend unavailable", "detail": 'Traceback (most recent call last): File "x.py"'})
        )
        self.assertFalse(ok)

    def test_non_json_body_fails(self):
        ok, msg = http_checks.check_safe_unavailable_body("Internal Server Error\n  at line 42")
        self.assertFalse(ok)
        self.assertIn("valid JSON", msg)


class CheckAllEndpointsTests(unittest.TestCase):
    def test_gateway_role_checks_five_endpoints_including_backend(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            results = http_checks.check_all_endpoints(1234, role="gateway")
        checked_paths = [http_checks.GATEWAY_ENDPOINTS[i] for i in range(len(results))]
        self.assertIn("/backend", checked_paths)
        self.assertEqual(len(results), 5)

    def test_app_role_checks_four_endpoints_no_backend(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            results = http_checks.check_all_endpoints(1234, role="app")
        self.assertEqual(len(results), 4)
        self.assertNotIn("/backend", http_checks.APP_ENDPOINTS)


class RawGetTests(unittest.TestCase):
    def test_raw_get_returns_status_and_body_on_success(self):
        with _mock_urlopen(200, json.dumps({"status": "alive"})):
            status, body = http_checks.raw_get(1234, "/livez")
        self.assertEqual(status, 200)
        self.assertIn("alive", body)

    def test_raw_get_returns_status_and_body_on_http_error(self):
        with _mock_urlopen(403, json.dumps({"error": "forbidden"})):
            status, body = http_checks.raw_get(1234, "/internal/info")
        self.assertEqual(status, 403)
        self.assertIn("forbidden", body)


if __name__ == "__main__":
    unittest.main()

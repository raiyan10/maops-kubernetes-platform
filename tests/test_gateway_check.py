"""
Docker/Kubernetes-free unit tests for the pure, separable logic in
scripts/gateway_check.py - the DAY6 remediation of the wrong-Host
no-route check (requires a definite HTTP 404, never accepts an
arbitrary non-200 response as proof, and treats unreachability as
INCONCLUSIVE).
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gateway_check


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    exc = urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=None)
    exc.read = lambda: body
    return exc


class RequestClassificationTests(unittest.TestCase):
    def test_successful_response_is_ok(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"service": "gw"}'
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            outcome, status, body = gateway_check._request("maops.local")
        self.assertEqual(outcome, "ok")
        self.assertEqual(status, 200)
        self.assertIn("service", body)

    def test_http_error_is_http_error_outcome_with_status_code(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404, b"not found")):
            outcome, status, body = gateway_check._request("wrong.invalid.example")
        self.assertEqual(outcome, "http_error")
        self.assertEqual(status, 404)
        self.assertEqual(body, "not found")

    def test_connection_failure_is_inconclusive(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            outcome, status, _body = gateway_check._request("maops.local")
        self.assertEqual(outcome, "inconclusive")
        self.assertIsNone(status)

    def test_timeout_is_inconclusive(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            outcome, status, _body = gateway_check._request("maops.local")
        self.assertEqual(outcome, "inconclusive")
        self.assertIsNone(status)


class CheckWrongHostHasNoRouteTests(unittest.TestCase):
    def setUp(self):
        gateway_check.results = []

    def test_definite_404_passes(self):
        with mock.patch.object(gateway_check, "_request", return_value=("http_error", 404, "Not Found")):
            gateway_check.check_wrong_host_has_no_route()
        self.assertTrue(all(ok for ok, _m in gateway_check.results))

    def test_gateway_shaped_200_fails(self):
        """The exact bug this remediation fixes: a 200 response
        (whatever the body) must never be accepted as proof of no
        route."""
        with mock.patch.object(gateway_check, "_request", return_value=("ok", 200, '{"service": "maops-gateway"}')):
            gateway_check.check_wrong_host_has_no_route()
        self.assertFalse(all(ok for ok, _m in gateway_check.results))

    def test_empty_200_fails_too(self):
        """DAY6 remediation: an earlier revision of this check would
        have wrongly accepted this (a 200 with a body that doesn't
        look like the gateway's own shape) as proof of "no route" -
        now it correctly requires the definite 404 signal instead."""
        with mock.patch.object(gateway_check, "_request", return_value=("ok", 200, "")):
            gateway_check.check_wrong_host_has_no_route()
        self.assertFalse(all(ok for ok, _m in gateway_check.results))

    def test_unrelated_5xx_fails_not_accepted_as_no_route(self):
        with mock.patch.object(gateway_check, "_request", return_value=("http_error", 503, "backend unavailable")):
            gateway_check.check_wrong_host_has_no_route()
        self.assertFalse(all(ok for ok, _m in gateway_check.results))

    def test_unreachable_nodeport_is_inconclusive_and_fails(self):
        with mock.patch.object(gateway_check, "_request", return_value=("inconclusive", None, "connection refused")):
            gateway_check.check_wrong_host_has_no_route()
        self.assertFalse(all(ok for ok, _m in gateway_check.results))
        self.assertTrue(any("INCONCLUSIVE" in m for _ok, m in gateway_check.results))


if __name__ == "__main__":
    unittest.main()

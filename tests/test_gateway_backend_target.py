"""
Docker/Kubernetes-free unit tests for gateway/server.py's DAY2-SEC-L1
fail-closed backend-target allowlist.

BACKEND_HOST/BACKEND_PORT are sourced from a ConfigMap at runtime;
scripts/validate_manifests.py already enforces the expected pair at
build time, but that is a build-time guard only. These tests prove the
gateway process itself refuses to use any target other than the single
permitted internal target (maops-app:8080) - never merely trusting the
static manifest validator - and, critically, that the outbound backend
request function is never even invoked when the configured target is
invalid.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

# Loaded under an explicit, unique module name (mirroring
# tests/test_app_auth.py's pattern) so it can never collide with a
# same-named module import from app/server.py in the same test run -
# both define top-level `INTERNAL_TOKEN` and `Handler`.
_SPEC = importlib.util.spec_from_file_location(
    "maops_gateway_server", Path(__file__).resolve().parent.parent / "gateway" / "server.py"
)
gateway_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway_server)


class _FakeHandler:
    """Duck-typed stand-in for gateway_server.Handler. The two methods
    under test only ever call self._write_json(...), so a real
    BaseHTTPRequestHandler/socket is unnecessary - the real, unbound
    production methods are called against this object directly."""

    def __init__(self):
        self.responses: list[tuple[int, dict]] = []

    def _write_json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


class BackendTargetAllowlistTests(unittest.TestCase):
    def test_maops_app_8080_is_allowed(self):
        self.assertTrue(gateway_server._is_allowed_backend_target("maops-app", 8080))

    def test_wrong_host_is_blocked(self):
        self.assertFalse(gateway_server._is_allowed_backend_target("evil.example", 8080))

    def test_wrong_port_is_blocked(self):
        self.assertFalse(gateway_server._is_allowed_backend_target("maops-app", 9999))

    def test_raw_ip_is_blocked(self):
        self.assertFalse(gateway_server._is_allowed_backend_target("10.96.5.23", 8080))

    def test_pod_like_identity_is_blocked(self):
        self.assertFalse(gateway_server._is_allowed_backend_target("maops-app-7d9f8c9c5-abcde", 8080))


class BackendHandlerFailClosedTests(unittest.TestCase):
    def test_backend_request_not_invoked_when_target_invalid(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
            with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                gateway_server.Handler._handle_backend(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])

    def test_backend_503_does_not_echo_the_unsafe_target_when_invalid(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_HOST", "evil.example"):
            with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_backend(handler)
        mock_request.assert_not_called()
        status, payload = handler.responses[0]
        self.assertEqual(status, 503)
        self.assertNotIn("evil.example", str(payload))

    def test_readyz_becomes_503_when_target_invalid_but_process_is_ready(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "READY", True):
            with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_readyz(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"status": "backend target invalid"})])

    def test_internal_token_never_sent_when_target_invalid(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"real-token-value"):
            with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_backend(handler)
        # The token is only ever placed into the headers dict passed to
        # _backend_request() - if that function is never called, the
        # token was never sent anywhere.
        mock_request.assert_not_called()

    def test_backend_request_is_invoked_when_target_valid(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"real-token"):
                with mock.patch.object(
                    gateway_server,
                    "_backend_request",
                    return_value=(200, {"service": "maops-kubernetes-app", "hostname": "h", "environment": "e"}),
                ) as mock_request:
                    gateway_server.Handler._handle_backend(handler)
        mock_request.assert_called_once()
        status, _payload = handler.responses[0]
        self.assertEqual(status, 200)

    def test_livez_remains_200_regardless_of_backend_target_validity(self):
        # /livez is local-process liveness only and must stay 200 even
        # when the configured backend target is invalid - it must never
        # be coupled to BACKEND_TARGET_VALID the way /readyz and /backend
        # are.
        handler = _FakeHandler()
        handler.path = "/livez"
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
            gateway_server.Handler.do_GET(handler)
        self.assertEqual(handler.responses, [(200, {"status": "alive"})])


if __name__ == "__main__":
    unittest.main()

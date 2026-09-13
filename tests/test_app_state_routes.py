"""
Docker/Kubernetes-free unit tests for app/server.py's DAY4
`/internal/state` routes (`_handle_internal_state_get/put`) and the
app -> state client (`_state_request`).

DAY4-TEST-M1: mirrors tests/test_gateway_backend_target.py's
`_FakeHandler` technique.

DAY4-ARCH-M1 (Part E): proves STATE_TIMEOUT_SECONDS (unchanged, 3s -
the innermost hop of the corrected timeout hierarchy) is actually the
socket timeout passed to the state HTTPConnection.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "maops_app_server_state", Path(__file__).resolve().parent.parent / "app" / "server.py"
)
app_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(app_server)


class _FakeHandler:
    def __init__(self, headers=None, body: bytes = b""):
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[int, dict]] = []

    def _write_json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))

    def _gateway_authenticated(self) -> bool:
        return self.headers.get(app_server.INTERNAL_TOKEN_HEADER) == "valid"


class InternalStateGetFailClosedTests(unittest.TestCase):
    def test_state_request_not_invoked_when_not_authenticated(self):
        handler = _FakeHandler(headers={})
        with mock.patch.object(app_server, "_state_request") as mock_request:
            app_server.Handler._handle_internal_state_get(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(403, {"error": "forbidden"})])

    def test_state_request_not_invoked_when_target_invalid(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", False):
            with mock.patch.object(app_server, "_state_request") as mock_request:
                app_server.Handler._handle_internal_state_get(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])

    def test_state_request_not_invoked_when_state_token_missing(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", None):
                with mock.patch.object(app_server, "_state_request") as mock_request:
                    app_server.Handler._handle_internal_state_get(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])

    def test_valid_request_proxies_and_returns_body(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request", return_value=(200, {"value": "x"})) as mock_request:
                    app_server.Handler._handle_internal_state_get(handler)
        mock_request.assert_called_once()
        self.assertEqual(handler.responses, [(200, {"value": "x"})])

    def test_non_200_from_state_becomes_503_never_the_raw_status(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request", return_value=(500, {"error": "write failed"})):
                    app_server.Handler._handle_internal_state_get(handler)
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])

    def test_downstream_timeout_becomes_safe_503_not_propagated(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request", side_effect=TimeoutError("timed out")):
                    app_server.Handler._handle_internal_state_get(handler)  # must not raise
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])

    def test_http_exception_becomes_safe_503(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request", side_effect=http.client.HTTPException("boom")):
                    app_server.Handler._handle_internal_state_get(handler)
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])


class InternalStatePutFailClosedTests(unittest.TestCase):
    def test_state_request_not_invoked_when_not_authenticated(self):
        handler = _FakeHandler(headers={"Content-Length": "10"}, body=b'{"value":1}')
        with mock.patch.object(app_server, "_state_request") as mock_request:
            app_server.Handler._handle_internal_state_put(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(403, {"error": "forbidden"})])

    def test_missing_content_length_is_411_without_reading_body(self):
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid"})
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request") as mock_request:
                    app_server.Handler._handle_internal_state_put(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(411, {"error": "Content-Length required"})])

    def test_oversized_body_is_413_without_calling_state(self):
        oversized = app_server.STATE_MAX_BODY_BYTES + 1
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid", "Content-Length": str(oversized)}, body=b"x" * 10)
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request") as mock_request:
                    app_server.Handler._handle_internal_state_put(handler)
        mock_request.assert_not_called()
        status, payload = handler.responses[0]
        self.assertEqual(status, 413)
        self.assertEqual(payload["max_bytes"], app_server.STATE_MAX_BODY_BYTES)

    def test_downstream_timeout_becomes_safe_503(self):
        body = b'{"value": "x"}'
        handler = _FakeHandler(headers={app_server.INTERNAL_TOKEN_HEADER: "valid", "Content-Length": str(len(body))}, body=body)
        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server, "_state_request", side_effect=TimeoutError("timed out")):
                    app_server.Handler._handle_internal_state_put(handler)  # must not raise
        self.assertEqual(handler.responses, [(503, {"error": "state unavailable"})])

    def test_state_token_never_taken_from_client_headers(self):
        body = b'{"value": "x"}'
        handler = _FakeHandler(
            headers={
                app_server.INTERNAL_TOKEN_HEADER: "valid",
                app_server.STATE_TOKEN_HEADER: "client-supplied-fake-state-token",
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        captured = {}

        def fake_state_request(path, method="GET", body=None):
            captured["token"] = app_server.STATE_TOKEN
            return 200, {"status": "ok"}

        with mock.patch.object(app_server, "STATE_TARGET_VALID", True):
            with mock.patch.object(app_server, "STATE_TOKEN", b"the-real-state-token"):
                with mock.patch.object(app_server, "_state_request", side_effect=fake_state_request):
                    app_server.Handler._handle_internal_state_put(handler)
        self.assertEqual(captured["token"], b"the-real-state-token")


class StateTimeoutPropagationTests(unittest.TestCase):
    """DAY4-ARCH-M1: STATE_TIMEOUT_SECONDS (3s, unchanged - the
    innermost hop) must actually bound the app -> state HTTPConnection,
    not merely be documented."""

    def test_state_timeout_seconds_is_3_by_default(self):
        self.assertEqual(app_server.STATE_TIMEOUT_SECONDS, 3.0)

    def test_http_connection_receives_state_timeout_seconds(self):
        captured = {}

        class _FakeConn:
            def __init__(self, host, port, timeout=None):
                captured["timeout"] = timeout

            def request(self, *a, **kw):
                pass

            def getresponse(self):
                class _Resp:
                    status = 200

                    def read(self):
                        return b"{}"

                return _Resp()

            def close(self):
                pass

        with mock.patch.object(app_server, "STATE_TIMEOUT_SECONDS", 3.0):
            with mock.patch.object(app_server, "STATE_TOKEN", b"tok"):
                with mock.patch.object(app_server.http.client, "HTTPConnection", side_effect=_FakeConn):
                    app_server._state_request("/state", "GET")
        self.assertEqual(captured["timeout"], 3.0)


if __name__ == "__main__":
    unittest.main()

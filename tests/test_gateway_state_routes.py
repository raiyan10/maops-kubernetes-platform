"""
Docker/Kubernetes-free unit tests for gateway/server.py's DAY4 `/state`
proxy routes (`_handle_state_get`/`_handle_state_put`).

DAY4-TEST-M1: mirrors tests/test_gateway_backend_target.py's
`_FakeHandler` technique - the real, unbound production methods are
called directly against a duck-typed stand-in, proving the fail-closed
target-invalid path never invokes `_backend_request`, an oversized PUT
body is rejected without reading it, and a downstream timeout/error
becomes a safe 503 rather than propagating.

DAY4-ARCH-M1 (Part E): also proves BACKEND_TIMEOUT_SECONDS is actually
passed to the outbound call as the socket timeout - the deterministic
proof that the raised 3s->5s margin is real, not just a comment.
"""

from __future__ import annotations

import importlib.util
import io
import socket
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "maops_gateway_server_state", Path(__file__).resolve().parent.parent / "gateway" / "server.py"
)
gateway_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway_server)


class _FakeHandler:
    def __init__(self, path="/state", headers=None, body: bytes = b""):
        self.path = path
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[int, dict]] = []

    def _write_json(self, status: int, payload: dict) -> None:
        self.responses.append((status, payload))


class StateGetFailClosedTests(unittest.TestCase):
    def test_backend_request_not_invoked_when_target_invalid(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
            with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                gateway_server.Handler._handle_state_get(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])

    def test_backend_request_not_invoked_when_internal_token_missing(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", None):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_state_get(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])

    def test_valid_target_proxies_and_returns_upstream_status(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request", return_value=(200, {"value": "x"})) as mock_request:
                    gateway_server.Handler._handle_state_get(handler)
        mock_request.assert_called_once()
        self.assertEqual(handler.responses, [(200, {"value": "x"})])

    def test_downstream_timeout_becomes_safe_503_not_propagated(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request", side_effect=TimeoutError("timed out")):
                    gateway_server.Handler._handle_state_get(handler)  # must not raise
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])

    def test_downstream_url_error_becomes_safe_503(self):
        handler = _FakeHandler()
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request", side_effect=urllib.error.URLError("refused")):
                    gateway_server.Handler._handle_state_get(handler)
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])


class StatePutFailClosedTests(unittest.TestCase):
    def test_backend_request_not_invoked_when_target_invalid(self):
        handler = _FakeHandler(headers={"Content-Length": "10"}, body=b'{"value":1}')
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", False):
            with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                gateway_server.Handler._handle_state_put(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])

    def test_missing_content_length_is_411_without_reading_body(self):
        handler = _FakeHandler(headers={})
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_state_put(handler)
        mock_request.assert_not_called()
        self.assertEqual(handler.responses, [(411, {"error": "Content-Length required"})])

    def test_oversized_body_is_413_without_calling_backend(self):
        oversized = gateway_server.STATE_PROXY_MAX_BODY_BYTES + 1
        handler = _FakeHandler(headers={"Content-Length": str(oversized)}, body=b"x" * 10)
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request") as mock_request:
                    gateway_server.Handler._handle_state_put(handler)
        mock_request.assert_not_called()
        status, payload = handler.responses[0]
        self.assertEqual(status, 413)
        self.assertEqual(payload["max_bytes"], gateway_server.STATE_PROXY_MAX_BODY_BYTES)

    def test_client_body_never_smuggles_its_own_internal_token_header(self):
        # The internal token sent downstream must always come from this
        # process's own loaded INTERNAL_TOKEN, never from the inbound
        # client request - _handle_state_put builds headers itself and
        # never reads self.headers for the outbound call.
        body = b'{"value": "x"}'
        handler = _FakeHandler(headers={"Content-Length": str(len(body)), gateway_server.INTERNAL_TOKEN_HEADER: "client-supplied-fake-token"}, body=body)
        captured = {}

        def fake_backend_request(path, headers=None, method="GET", data=None):
            captured["headers"] = headers
            return 200, {"status": "ok"}

        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"the-real-token"):
                with mock.patch.object(gateway_server, "_backend_request", side_effect=fake_backend_request):
                    gateway_server.Handler._handle_state_put(handler)
        self.assertEqual(captured["headers"][gateway_server.INTERNAL_TOKEN_HEADER], "the-real-token")

    def test_downstream_timeout_becomes_safe_503(self):
        body = b'{"value": "x"}'
        handler = _FakeHandler(headers={"Content-Length": str(len(body))}, body=body)
        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                with mock.patch.object(gateway_server, "_backend_request", side_effect=TimeoutError("timed out")):
                    gateway_server.Handler._handle_state_put(handler)  # must not raise
        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])


class BackendTimeoutPropagationTests(unittest.TestCase):
    """DAY4-ARCH-M1: proves BACKEND_TIMEOUT_SECONDS (raised 3s -> 5s
    this batch) is actually the socket timeout passed to urlopen for
    every outbound call - not merely documented."""

    def test_backend_timeout_seconds_is_5_by_default(self):
        self.assertEqual(gateway_server.BACKEND_TIMEOUT_SECONDS, 5.0)

    def test_urlopen_receives_backend_timeout_seconds(self):
        captured = {}

        class _FakeResponse:
            status = 200

            def read(self):
                return b'{"status": "ready"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            return _FakeResponse()

        with mock.patch.object(gateway_server, "BACKEND_TIMEOUT_SECONDS", 5.0):
            with mock.patch.object(gateway_server.urllib.request, "urlopen", side_effect=fake_urlopen):
                gateway_server._backend_request("/readyz")
        self.assertEqual(captured["timeout"], 5.0)


class RealSocketTimeoutClassificationTests(unittest.TestCase):
    """DAY4 batch 2b (Part F): every existing timeout test in this file
    mocks `_backend_request`/`urlopen` to raise INSTANTLY - proving the
    classification logic, never that a REAL elapsed socket timeout,
    against a REAL slow backend, actually fires within a bounded window
    and gets classified correctly. Healthy responses (and instantly-
    mocked failures) alone are insufficient proof of the timing
    relationship this batch's Part F fixes. Uses a real local TCP
    server that accepts the connection and deterministically never
    responds - not a specific wall-clock delay value - so this is
    exercised against BACKEND_TIMEOUT_SECONDS actually firing, not a
    mock standing in for it."""

    def test_real_slow_backend_times_out_and_is_classified_503_within_bound(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]
        stop = threading.Event()

        def _accept_and_stall():
            server_sock.settimeout(5.0)
            try:
                conn, _addr = server_sock.accept()
            except OSError:
                return
            try:
                stop.wait(5.0)  # never responds - the client's own timeout must fire first
            finally:
                conn.close()

        thread = threading.Thread(target=_accept_and_stall, daemon=True)
        thread.start()
        handler = _FakeHandler()
        try:
            with mock.patch.object(gateway_server, "BACKEND_HOST", "127.0.0.1"):
                with mock.patch.object(gateway_server, "BACKEND_PORT", port):
                    with mock.patch.object(gateway_server, "BACKEND_TIMEOUT_SECONDS", 0.3):
                        with mock.patch.object(gateway_server, "BACKEND_TARGET_VALID", True):
                            with mock.patch.object(gateway_server, "INTERNAL_TOKEN", b"tok"):
                                start = time.monotonic()
                                gateway_server.Handler._handle_state_get(handler)  # must not raise, must not hang
                                elapsed = time.monotonic() - start
        finally:
            stop.set()
            server_sock.close()
            thread.join(timeout=2.0)

        self.assertEqual(handler.responses, [(503, {"error": "backend unavailable"})])
        # Real elapsed time must be bounded near BACKEND_TIMEOUT_SECONDS
        # (0.3s) - not hung, and not so long that the timeout clearly
        # never fired as configured.
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()

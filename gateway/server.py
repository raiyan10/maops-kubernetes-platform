#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - Day 2 "gateway" workload.

A deliberately tiny HTTP server, Python standard library only, that
proves real Kubernetes service discovery: it reaches the "app"
workload exclusively through the Kubernetes Service DNS name
(BACKEND_HOST=maops-app), never a Pod IP/name or hardcoded ClusterIP.

Liveness (/livez) is local-process-only. Readiness (/readyz) is
dependency-aware: it performs a bounded HTTP check against the app's
own /readyz through the Service. This intentional asymmetry (readiness
depends on the backend, liveness never does) is what Day 2's
dependency-failure validation proves live.
"""

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
CONFIG_PREFIX_APP = "APP_"
CONFIG_PREFIX_BACKEND = "BACKEND_"
STARTUP_DELAY_SECONDS = float(os.environ.get("STARTUP_DELAY_SECONDS", "3"))

BACKEND_HOST = os.environ.get("BACKEND_HOST", "maops-app")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8080"))
# Bounded, finite timeout for every gateway -> app HTTP call (no infinite
# waits). Sourced from ConfigMap-provided configuration, with a safe
# fallback if it were ever missing.
BACKEND_TIMEOUT_SECONDS = float(os.environ.get("BACKEND_TIMEOUT_SECONDS", "3"))

# DAY2-SEC-L1: BACKEND_HOST/BACKEND_PORT are ConfigMap-driven at runtime,
# and scripts/validate_manifests.py's static check is only a build-time
# guard - nothing at the Kubernetes layer (RBAC lands Day 5) stops the
# ConfigMap from being mutated live. This in-process allowlist is the
# gateway's own last line of defense: the only permitted Day 2 internal
# backend target is maops-app:8080, exactly. This does NOT replace Day
# 5's RBAC/NetworkPolicy work - it only prevents this process from
# sending the internal token to an unintended host if its own
# configuration were ever tampered with.
ALLOWED_BACKEND_HOST = "maops-app"
ALLOWED_BACKEND_PORT = 8080


def _is_allowed_backend_target(host: str, port: int) -> bool:
    return host == ALLOWED_BACKEND_HOST and port == ALLOWED_BACKEND_PORT


BACKEND_TARGET_VALID = _is_allowed_backend_target(BACKEND_HOST, BACKEND_PORT)

INTERNAL_TOKEN_PATH = "/var/run/secrets/maops/internal-token"
INTERNAL_TOKEN_HEADER = "X-MAOPS-Internal-Token"

START_TIME = time.time()
READY = False


def mark_ready_after_delay() -> None:
    time.sleep(STARTUP_DELAY_SECONDS)
    global READY
    READY = True


def load_internal_token() -> bytes | None:
    """Read the internal auth token from its mounted Secret file once at
    startup. Never logged, never echoed back."""
    try:
        with open(INTERNAL_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return None


INTERNAL_TOKEN = load_internal_token()


def visible_config() -> dict:
    """Only non-sensitive values sourced from the gateway ConfigMap - the
    internal auth token is never one of these environment variables."""
    return {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(CONFIG_PREFIX_APP) or key.startswith(CONFIG_PREFIX_BACKEND)
    }


def _backend_request(path: str, headers: dict | None = None):
    """A single bounded HTTP call to the app workload, reached only via
    the Kubernetes Service DNS name BACKEND_HOST. Returns (status, body_dict)
    on success. Raises on any network failure/timeout/non-JSON body - the
    caller decides how to translate that into a safe HTTP response."""
    url = f"http://{BACKEND_HOST}:{BACKEND_PORT}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=BACKEND_TIMEOUT_SECONDS) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return resp.status, body


class Handler(BaseHTTPRequestHandler):
    server_version = "maops-kubernetes-gateway/0.2.0"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        if self.path == "/":
            self._write_json(
                200,
                {
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-gateway"),
                    "message": os.environ.get("APP_MESSAGE", ""),
                    "hostname": socket.gethostname(),
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                },
            )
        elif self.path == "/livez":
            # Local-process liveness ONLY. Must never depend on the app
            # backend - that distinction is Day 2's readiness/liveness proof.
            self._write_json(200, {"status": "alive"})
        elif self.path == "/readyz":
            self._handle_readyz()
        elif self.path == "/config":
            self._write_json(200, visible_config())
        elif self.path == "/backend":
            self._handle_backend()
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def _handle_readyz(self) -> None:
        if not READY:
            self._write_json(503, {"status": "starting"})
            return
        if not BACKEND_TARGET_VALID:
            # Fail closed rather than trust a mutated/invalid configured
            # target - see DAY2-SEC-L1. _backend_request() is never
            # invoked in this branch.
            self._write_json(503, {"status": "backend target invalid"})
            return
        try:
            status, body = _backend_request("/readyz")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            self._write_json(503, {"status": "backend unavailable"})
            return
        if status == 200 and body.get("status") == "ready":
            self._write_json(200, {"status": "ready", "backend": "ready"})
        else:
            self._write_json(503, {"status": "backend not ready"})

    def _handle_backend(self) -> None:
        if not BACKEND_TARGET_VALID:
            # Fail closed - never send the internal token toward an
            # unvalidated target. _backend_request() is never invoked in
            # this branch, and the unsafe configured target is never
            # echoed back (DAY2-SEC-L1).
            self._write_json(503, {"error": "backend unavailable"})
            return
        if INTERNAL_TOKEN is None:
            self._write_json(503, {"error": "backend unavailable"})
            return
        headers = {INTERNAL_TOKEN_HEADER: INTERNAL_TOKEN.decode("utf-8")}
        try:
            status, body = _backend_request("/internal/info", headers=headers)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Network failure or timeout - controlled 503, no traceback,
            # no token in the response.
            self._write_json(503, {"error": "backend unavailable"})
            return
        if status != 200:
            self._write_json(503, {"error": "backend unavailable"})
            return
        self._write_json(
            200,
            {
                "gateway_hostname": socket.gethostname(),
                "backend_service": body.get("service"),
                "backend_hostname": body.get("hostname"),
                "backend_environment": body.get("environment"),
            },
        )

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Deliberately logs only the request line/status - never header
        # values, so the internal token is never printed.
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))


def main() -> None:
    threading.Thread(target=mark_ready_after_delay, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"maops-kubernetes-gateway listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

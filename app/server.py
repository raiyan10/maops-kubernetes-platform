#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - "app" workload (introduced Day 2). As of
Day 4, app is the middle hop in gateway -> app -> state: it terminates
the existing gateway -> app internal-auth token exactly as before, and
additionally proxies an authenticated GET/PUT /internal/state to the
new "state" StatefulSet workload using a second, separate credential
(maops-state-auth) that gateway never receives.

A deliberately tiny HTTP server, Python standard library only, used
solely to prove real Kubernetes behavior (Deployment, Service,
ConfigMap, Secret-backed internal auth, probes, security context, and
now StatefulSet/PVC-backed persistence reached through this hop). This
is not an application engineering exercise.
"""

import hmac
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
CONFIG_PREFIX = "APP_"
STARTUP_DELAY_SECONDS = float(os.environ.get("STARTUP_DELAY_SECONDS", "3"))

# Fixed mount path (DAY2 SECRET MOUNT) - not environment-driven, since the
# volume/mount wiring in k8s/base is what actually determines this path.
INTERNAL_TOKEN_PATH = "/var/run/secrets/maops/internal-token"
INTERNAL_TOKEN_HEADER = "X-MAOPS-Internal-Token"

# DAY4: state workload target. ConfigMap-driven at runtime, but - same
# rationale as gateway's DAY2-SEC-L1 allowlist - this in-process
# allowlist is app's own last line of defense: the only permitted Day 4
# internal state target is maops-state:8080, exactly. A mismatch fails
# closed (STATE_TARGET_VALID = False) rather than ever sending the state
# token toward an unintended/tampered host.
STATE_HOST = os.environ.get("STATE_HOST", "maops-state")
STATE_PORT = int(os.environ.get("STATE_PORT", "8080"))
STATE_TIMEOUT_SECONDS = float(os.environ.get("STATE_TIMEOUT_SECONDS", "3"))
ALLOWED_STATE_HOST = "maops-state"
ALLOWED_STATE_PORT = 8080


def _is_allowed_state_target(host: str, port: int) -> bool:
    return host == ALLOWED_STATE_HOST and port == ALLOWED_STATE_PORT


STATE_TARGET_VALID = _is_allowed_state_target(STATE_HOST, STATE_PORT)

STATE_TOKEN_PATH = "/var/run/secrets/maops-state/state-token"
STATE_TOKEN_HEADER = "X-MAOPS-State-Token"
STATE_MAX_BODY_BYTES = int(os.environ.get("STATE_MAX_BODY_BYTES", "4096"))

START_TIME = time.time()
READY = False


def mark_ready_after_delay() -> None:
    time.sleep(STARTUP_DELAY_SECONDS)
    global READY
    READY = True


def load_internal_token() -> bytes | None:
    """Read the internal auth token from its mounted Secret file once at
    startup. Never logged, never echoed back. Returns None (rather than
    raising) if the file is absent/unreadable, so /internal/info simply
    rejects every caller instead of crashing the process."""
    try:
        with open(INTERNAL_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return None


INTERNAL_TOKEN = load_internal_token()


def load_state_token() -> bytes | None:
    """Read the state auth token from its mounted Secret file once at
    startup. Never logged, never echoed back. Only app and state ever
    hold this credential - gateway does not."""
    try:
        with open(STATE_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return None


STATE_TOKEN = load_state_token()


def _state_request(path: str, method: str = "GET", body: bytes | None = None):
    """A single bounded HTTP call to the state workload, reached only
    via the allowlisted Kubernetes Service DNS name (STATE_HOST/PORT).
    Uses http.client directly rather than urllib.request: http.client
    never follows redirects and never consults proxy environment
    variables, so there is no redirect/proxy path that could ever divert
    this call - and therefore the state token - away from the
    allowlisted target. The X-MAOPS-State-Token header is always built
    from this process's own loaded STATE_TOKEN, never copied from an
    incoming request's headers. Returns (status, body_dict). Raises
    OSError/http.client.HTTPException/ValueError on any network/timeout/
    non-JSON failure - the caller decides how to translate that into a
    safe HTTP response."""
    conn = http.client.HTTPConnection(STATE_HOST, STATE_PORT, timeout=STATE_TIMEOUT_SECONDS)
    try:
        headers = {STATE_TOKEN_HEADER: STATE_TOKEN.decode("utf-8")}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        return resp.status, data
    finally:
        conn.close()


def visible_config() -> dict:
    """Only non-sensitive values sourced from the Kubernetes ConfigMap.
    The internal auth token is never an APP_-prefixed environment
    variable, so it can never leak through this endpoint."""
    return {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(CONFIG_PREFIX)
    }


def _token_is_valid(provided: str | None) -> bool:
    if not provided or INTERNAL_TOKEN is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), INTERNAL_TOKEN)


class Handler(BaseHTTPRequestHandler):
    server_version = "maops-kubernetes-app/0.4.0"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gateway_authenticated(self) -> bool:
        provided = self.headers.get(INTERNAL_TOKEN_HEADER)
        return _token_is_valid(provided)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        if self.path == "/":
            self._write_json(
                200,
                {
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-app"),
                    "message": os.environ.get("APP_MESSAGE", ""),
                    "hostname": socket.gethostname(),
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                },
            )
        elif self.path == "/livez":
            self._write_json(200, {"status": "alive"})
        elif self.path == "/readyz":
            self._handle_readyz()
        elif self.path == "/config":
            self._write_json(200, visible_config())
        elif self.path == "/internal/info":
            if not self._gateway_authenticated():
                self._write_json(403, {"error": "forbidden"})
                return
            self._write_json(
                200,
                {
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-app"),
                    "hostname": socket.gethostname(),
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                    "environment": os.environ.get("APP_ENVIRONMENT", ""),
                },
            )
        elif self.path == "/internal/state":
            self._handle_internal_state_get()
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def do_PUT(self) -> None:  # noqa: N802
        if self.path == "/internal/state":
            self._handle_internal_state_put()
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def _handle_readyz(self) -> None:
        if not READY:
            self._write_json(503, {"status": "starting"})
            return
        # DAY4: app readiness now depends on USABLE AUTHENTICATED state
        # access - not merely local process health. Calling the
        # authenticated GET /state (rather than an unauthenticated
        # /readyz) proves the state token/allowlist path itself works,
        # not just that the state process is reachable.
        if not STATE_TARGET_VALID or STATE_TOKEN is None:
            self._write_json(503, {"status": "state target invalid"})
            return
        try:
            status, _body = _state_request("/state", "GET")
        except (OSError, http.client.HTTPException, ValueError):
            self._write_json(503, {"status": "state unavailable"})
            return
        if status == 200:
            self._write_json(200, {"status": "ready", "state": "ready"})
        else:
            self._write_json(503, {"status": "state not ready"})

    def _handle_internal_state_get(self) -> None:
        if not self._gateway_authenticated():
            self._write_json(403, {"error": "forbidden"})
            return
        if not STATE_TARGET_VALID or STATE_TOKEN is None:
            # Fail closed rather than trust a mutated/invalid configured
            # target - mirrors gateway's DAY2-SEC-L1 pattern.
            # _state_request() is never invoked in this branch.
            self._write_json(503, {"error": "state unavailable"})
            return
        try:
            status, body = _state_request("/state", "GET")
        except (OSError, http.client.HTTPException, ValueError):
            self._write_json(503, {"error": "state unavailable"})
            return
        if status != 200:
            self._write_json(503, {"error": "state unavailable"})
            return
        self._write_json(200, body)

    def _handle_internal_state_put(self) -> None:
        if not self._gateway_authenticated():
            self._write_json(403, {"error": "forbidden"})
            return
        if not STATE_TARGET_VALID or STATE_TOKEN is None:
            self._write_json(503, {"error": "state unavailable"})
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._write_json(411, {"error": "Content-Length required"})
            return
        length = int(length_header)
        if length > STATE_MAX_BODY_BYTES:
            self._write_json(413, {"error": "payload too large", "max_bytes": STATE_MAX_BODY_BYTES})
            return
        raw = self.rfile.read(length)

        try:
            status, body = _state_request("/state", "PUT", body=raw)
        except (OSError, http.client.HTTPException, ValueError):
            self._write_json(503, {"error": "state unavailable"})
            return
        self._write_json(status, body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Deliberately logs only the request line/status (BaseHTTPRequestHandler's
        # default args), never header values - the internal token is never printed.
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))


def main() -> None:
    threading.Thread(target=mark_ready_after_delay, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"maops-kubernetes-app listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

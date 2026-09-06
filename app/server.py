#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - "app" workload (introduced Day 2, unchanged
application code as of Day 3 - Day 3's scaling/rollout/scheduling/PDB
work is entirely a Kubernetes-manifest and cluster-tooling concern).

A deliberately tiny HTTP server, Python standard library only, used
solely to prove real Kubernetes behavior (Deployment, Service,
ConfigMap, Secret-backed internal auth, probes, security context).
This is not an application engineering exercise.
"""

import hmac
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
    server_version = "maops-kubernetes-app/0.3.0"

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
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-app"),
                    "message": os.environ.get("APP_MESSAGE", ""),
                    "hostname": socket.gethostname(),
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                },
            )
        elif self.path == "/livez":
            self._write_json(200, {"status": "alive"})
        elif self.path == "/readyz":
            if READY:
                self._write_json(200, {"status": "ready"})
            else:
                self._write_json(503, {"status": "starting"})
        elif self.path == "/config":
            self._write_json(200, visible_config())
        elif self.path == "/internal/info":
            provided = self.headers.get(INTERNAL_TOKEN_HEADER)
            if not _token_is_valid(provided):
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
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

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

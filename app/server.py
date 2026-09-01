#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - Day 1 validation workload.

A deliberately tiny HTTP server, Python standard library only, used
solely to prove real Kubernetes behavior (Deployment, Service,
ConfigMap, probes, security context). This is not an application
engineering exercise.
"""

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

START_TIME = time.time()
READY = False


def mark_ready_after_delay() -> None:
    time.sleep(STARTUP_DELAY_SECONDS)
    global READY
    READY = True


def visible_config() -> dict:
    """Only non-sensitive values sourced from the Kubernetes ConfigMap."""
    return {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(CONFIG_PREFIX)
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "maops-k8s-day1/0.1.0"

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
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-platform"),
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
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))


def main() -> None:
    threading.Thread(target=mark_ready_after_delay, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"maops-kubernetes-platform listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - Day 8 disposable SCALING workload.

One Python-standard-library program, one image, five roles - each a
deliberately tiny stand-in that exists only to make a Kubernetes
autoscaler do something observable in the temporary scaling namespace
(`maops-day8-scaling`). It never talks to the platform application, its
Services, or maops-state, and holds no data worth keeping.

  cpu-server  HPA target. GET /work?ms=N busy-loops N ms (capped) so CPU
              usage follows offered load.
  load        Bounded closed-loop load generator for cpu-server: fixed
              concurrency, fixed duration (both capped), then exits.
  vpa-idle    VPA target. Holds a fixed amount of memory and burns a
              small, steady slice of CPU so the recommender has real
              usage to recommend from.
  worker      KEDA target. Pops items from a disposable Redis list
              (BLPOP), "processes" each for a fixed time, counts it.
  producer    Pushes a bounded number of items onto that list, exits.

The long-running roles serve GET /livez and /readyz on PORT (8080).
Redis is spoken with a minimal RESP client below - no third-party
packages, matching the rest of this repository.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
MAX_WORK_MS = 200
MAX_LOAD_SECONDS = 300
MAX_LOAD_CONCURRENCY = 8
MAX_PRODUCE_ITEMS = 500
MAX_HOLD_MIB = 64

READY = threading.Event()


def env_int(name: str, default: int, low: int, high: int) -> int:
    """Reads an integer setting and clamps it to [low, high] - every
    knob that shapes load is bounded here, not trusted from the manifest."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


def burn(ms: int) -> None:
    deadline = time.perf_counter() + ms / 1000.0
    x = 0
    while time.perf_counter() < deadline:
        x = (x * 31 + 7) % 1000003


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path == "/livez":
            self._send(200, {"status": "alive"})
        elif url.path == "/readyz":
            self._send(200 if READY.is_set() else 503, {"status": "ready" if READY.is_set() else "starting"})
        elif url.path == "/work" and ROLE == "cpu-server":
            try:
                ms = int(parse_qs(url.query).get("ms", ["20"])[0])
            except ValueError:
                ms = 20
            ms = max(1, min(MAX_WORK_MS, ms))
            burn(ms)
            self._send(200, {"worked_ms": ms, "hostname": socket.gethostname()})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # quiet: probes would flood the log
        pass


def serve_health() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# Minimal RESP (Redis protocol) client
# --------------------------------------------------------------------------


def encode_command(*parts: str) -> bytes:
    out = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        raw = str(part).encode()
        out.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    return b"".join(out)


class RespError(Exception):
    pass


def read_reply(stream):
    """Parses one RESP2 reply from a binary file-like object."""
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise RespError("connection closed mid-reply")
    kind, rest = line[:1], line[1:-2]
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise RespError(rest.decode())
    if kind == b":":
        return int(rest)
    if kind == b"$":
        size = int(rest)
        if size < 0:
            return None
        data = stream.read(size + 2)
        return data[:-2].decode()
    if kind == b"*":
        count = int(rest)
        if count < 0:
            return None
        return [read_reply(stream) for _ in range(count)]
    raise RespError(f"unknown reply type {kind!r}")


class Redis:
    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.stream = self.sock.makefile("rb")

    def call(self, *parts: str):
        self.sock.sendall(encode_command(*parts))
        return read_reply(self.stream)

    def close(self) -> None:
        self.stream.close()
        self.sock.close()


def redis_target() -> tuple[str, int, str]:
    return (
        os.environ.get("QUEUE_HOST", "day8-queue"),
        env_int("QUEUE_PORT", 6379, 1, 65535),
        os.environ.get("QUEUE_LIST", "day8:jobs"),
    )


PROCESSED_KEY_SUFFIX = ":processed"


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------


def run_cpu_server() -> int:
    server = serve_health()
    READY.set()
    try:
        threading.Event().wait()
    finally:
        server.shutdown()
    return 0


def run_load() -> int:
    target = os.environ.get("TARGET_URL", "http://day8-hpa-target:8080/work")
    seconds = env_int("LOAD_SECONDS", 120, 1, MAX_LOAD_SECONDS)
    concurrency = env_int("LOAD_CONCURRENCY", 4, 1, MAX_LOAD_CONCURRENCY)
    work_ms = env_int("WORK_MS", 50, 1, MAX_WORK_MS)
    url = urlparse(target)
    path = f"{url.path or '/work'}?ms={work_ms}"
    deadline = time.monotonic() + seconds
    counts = {"ok": 0, "error": 0}
    lock = threading.Lock()

    def loop():
        while time.monotonic() < deadline:
            # A new connection per request so kube-proxy spreads load over
            # every Ready endpoint, including newly scaled-out ones.
            conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=5)
            try:
                conn.request("GET", path)
                ok = conn.getresponse().status == 200
            except OSError:
                ok = False
            finally:
                conn.close()
            with lock:
                counts["ok" if ok else "error"] += 1
            if not ok:
                time.sleep(0.2)

    threads = [threading.Thread(target=loop) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps({"role": "load", "seconds": seconds, "concurrency": concurrency, "work_ms": work_ms, **counts}), flush=True)
    return 0 if counts["ok"] > 0 else 1


def run_vpa_idle() -> int:
    hold_mib = env_int("HOLD_MIB", 24, 0, MAX_HOLD_MIB)
    burn_ms = env_int("BURN_MS", 3, 0, 50)
    period_ms = env_int("PERIOD_MS", 100, 10, 1000)
    ballast = bytearray(hold_mib * 1024 * 1024)
    for i in range(0, len(ballast), 4096):  # touch every page so it is resident
        ballast[i] = 1
    server = serve_health()
    READY.set()
    try:
        while True:
            burn(burn_ms)
            time.sleep(max(0.0, (period_ms - burn_ms) / 1000.0))
    finally:
        server.shutdown()
        del ballast


# A scale-down terminates workers with SIGTERM. Without a handler the
# process dies at once - after BLPOP has removed an item but before it is
# counted, so the item is lost (run 7c99936d..., 59/60 processed). The
# worker therefore DRAINS: on SIGTERM it takes no new item, always finishes
# and counts the one in flight, then exits. BLPOP_TIMEOUT_SECONDS +
# PROCESS_MS stays below the Pod's terminationGracePeriodSeconds
# (asserted by tests/test_day8.py).
BLPOP_TIMEOUT_SECONDS = 1


class StopFlag:
    """Lock-free stop request: safe to set from a signal handler (a
    threading.Event would take an internal lock the interrupted main thread
    may already hold)."""

    def __init__(self):
        self.requested = False

    def set(self) -> None:
        self.requested = True

    def is_set(self) -> bool:
        return self.requested


STOP = StopFlag()


def work_loop(client, key: str, process_ms: int, stop, sleep=time.sleep) -> int:
    """Processes items until `stop` is set; never abandons a popped item."""
    done = 0
    while not stop.is_set():
        item = client.call("BLPOP", key, str(BLPOP_TIMEOUT_SECONDS))
        if item is None:
            continue
        sleep(process_ms / 1000.0)
        client.call("INCR", key + PROCESSED_KEY_SUFFIX)
        done += 1
    return done


def run_worker() -> int:
    host, port, key = redis_target()
    process_ms = env_int("PROCESS_MS", 2000, 0, 10000)
    signal.signal(signal.SIGTERM, lambda signum, frame: STOP.set())
    serve_health()
    done = 0
    while not STOP.is_set():
        try:
            client = Redis(host, port, timeout=15)
            client.call("PING")
        except (OSError, RespError) as exc:
            READY.clear()
            print(f"worker: queue unavailable ({exc}); retrying", flush=True)
            time.sleep(2)
            continue
        READY.set()
        try:
            done += work_loop(client, key, process_ms, STOP)
        except (OSError, RespError) as exc:
            READY.clear()
            print(f"worker: connection lost ({exc}); reconnecting", flush=True)
        finally:
            client.close()
    print(json.dumps({"role": "worker", "drained": True, "processed": done}), flush=True)
    return 0


def run_producer() -> int:
    host, port, key = redis_target()
    items = env_int("ITEMS", 60, 1, MAX_PRODUCE_ITEMS)
    client = Redis(host, port)
    try:
        for i in range(items):
            client.call("RPUSH", key, f"item-{i}")
        length = client.call("LLEN", key)
    finally:
        client.close()
    print(json.dumps({"role": "producer", "pushed": items, "list_length_after": length}), flush=True)
    return 0


ROLES = {
    "cpu-server": run_cpu_server,
    "load": run_load,
    "vpa-idle": run_vpa_idle,
    "worker": run_worker,
    "producer": run_producer,
}
ROLE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ROLE", "")


def main() -> int:
    if ROLE not in ROLES:
        print(f"usage: scaling.py {{{'|'.join(ROLES)}}}", file=sys.stderr)
        return 2
    return ROLES[ROLE]()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
MAOps Kubernetes Platform - "state" workload (introduced Day 4).

A deliberately tiny HTTP server, Python standard library only, that
proves real StatefulSet/PVC persistence behavior: a single writer
(exactly one replica) persists a small JSON record to a PVC-backed
volume at STATE_FILE_PATH (mounted at /data), authenticated by a
dedicated Secret (maops-state-auth) distinct from the gateway/app
internal-auth Secret - only this workload and "app" ever see the state
token; gateway does not.

Reached exclusively as: gateway /state -> app /internal/state ->
state /state. This process never talks to gateway directly.
"""

import hmac
import json
import os
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
CONFIG_PREFIX = "APP_"
STARTUP_DELAY_SECONDS = float(os.environ.get("STARTUP_DELAY_SECONDS", "3"))

# PVC-backed path (see k8s/base/state-statefulset.yaml's volumeClaimTemplates,
# mounted at /data) - never the read-only root filesystem.
STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "/data/state.json")
STATE_MAX_BODY_BYTES = int(os.environ.get("STATE_MAX_BODY_BYTES", "4096"))
_STATE_DIR = os.path.dirname(STATE_FILE_PATH) or "."

# Fixed mount path (DAY4 SECRET MOUNT) - matches the volume/mount wiring
# in k8s/base/state-statefulset.yaml, not environment-driven.
STATE_TOKEN_PATH = "/var/run/secrets/maops-state/state-token"
STATE_TOKEN_HEADER = "X-MAOPS-State-Token"

START_TIME = time.time()
READY = False

# Serializes every read-modify-write against STATE_FILE_PATH within this
# single process - PUT is idempotent per call, and concurrent PUTs are
# resolved as documented last-writer-wins, never an interleaved/torn
# write. This is a single-process, single-replica lock (maops-state
# never runs more than 1 replica); it does not solve multi-writer
# consensus, which is out of Day 4 scope.
_lock = threading.Lock()

# Set at startup if a PRE-EXISTING state file is unreadable or fails
# schema validation. Never auto-repaired - /readyz surfaces this and
# /state GET/PUT both fail closed while it holds, exactly as observed.
_init_error: str | None = None


def mark_ready_after_delay() -> None:
    time.sleep(STARTUP_DELAY_SECONDS)
    global READY
    READY = True


def load_state_token() -> bytes | None:
    """Read the state auth token from its mounted Secret file once at
    startup. Never logged, never echoed back."""
    try:
        with open(STATE_TOKEN_PATH, "rb") as f:
            return f.read().strip()
    except OSError:
        return None


STATE_TOKEN = load_state_token()


def _token_is_valid(provided: str | None) -> bool:
    if not provided or STATE_TOKEN is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), STATE_TOKEN)


def visible_config() -> dict:
    return {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(CONFIG_PREFIX)
    }


def _validate_record(obj) -> bool:
    """Exact schema: {"value": <string or null>} - nothing more, nothing
    less. A dict with extra keys, a non-dict top level, or a non-string/
    non-null value is rejected."""
    if not isinstance(obj, dict):
        return False
    if set(obj.keys()) != {"value"}:
        return False
    value = obj["value"]
    return value is None or isinstance(value, str)


class PersistOutcome:
    OK = "ok"
    # Nothing was persisted - the on-disk file (if any) is untouched.
    FAILED_CLEAN = "failed_clean"
    # os.replace() completed (the new content is very likely what a
    # subsequent read will see) but the parent-directory fsync that
    # should follow it failed - durability across a crash at exactly
    # this point cannot be guaranteed either way.
    FAILED_UNCERTAIN = "failed_uncertain"


def _persist_record(record: dict) -> tuple[str, str | None]:
    """Caller MUST hold _lock. Writes `record` to a restrictive (0600)
    temporary file in the same directory as STATE_FILE_PATH (so the
    following os.replace() is a same-filesystem atomic rename), fsyncs
    the temp file's contents, atomically replaces the target, then
    fsyncs the parent directory so the rename itself is durable. Success
    is acknowledged ONLY after all of these steps complete. Any owned
    temporary file is removed on failure."""
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".state-", dir=_STATE_DIR)
        os.chmod(tmp_path, 0o600)
        with os.fdopen(tmp_fd, "w") as f:
            tmp_fd = None  # ownership transferred to the file object
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return PersistOutcome.FAILED_CLEAN, f"write/flush/fsync failed: {exc}"

    try:
        os.replace(tmp_path, STATE_FILE_PATH)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return PersistOutcome.FAILED_CLEAN, f"atomic replace failed: {exc}"

    try:
        dir_fd = os.open(_STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        return PersistOutcome.FAILED_UNCERTAIN, f"replace succeeded but parent-directory fsync failed: {exc}"

    return PersistOutcome.OK, None


def _read_record() -> tuple[dict | None, str | None]:
    """Reads and validates the persisted record fresh from disk on every
    call - no in-memory cache is ever substituted for a real read, so a
    genuine storage failure can never be masked as a successful
    recovery. Never repairs a missing/corrupt file - only reports it."""
    try:
        with open(STATE_FILE_PATH, "r") as f:
            raw = f.read()
    except OSError as exc:
        return None, f"read failed: {exc}"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"stored state is not valid JSON: {exc}"
    if not _validate_record(obj):
        return None, "stored state does not match the expected {'value': <string|null>} schema"
    return obj, None


def _initialize_state_file() -> None:
    """Startup only. A missing file is initialized to {"value": null}
    via the exact same safe write path _persist_record() uses for every
    other write. An EXISTING file is only ever read, never overwritten -
    an unreadable or malformed pre-existing file is left exactly as-is
    and surfaced by /readyz (see _init_error), never silently replaced
    with a fresh default."""
    global _init_error
    if os.path.exists(STATE_FILE_PATH):
        _, err = _read_record()
        _init_error = err
        return
    with _lock:
        outcome, detail = _persist_record({"value": None})
    if outcome != PersistOutcome.OK:
        _init_error = f"failed to initialize state file: {detail}"


_initialize_state_file()


class Handler(BaseHTTPRequestHandler):
    server_version = "maops-kubernetes-state/0.4.0"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        provided = self.headers.get(STATE_TOKEN_HEADER)
        return _token_is_valid(provided)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler naming)
        if self.path == "/":
            self._write_json(
                200,
                {
                    "service": os.environ.get("APP_NAME", "maops-kubernetes-state"),
                    "message": os.environ.get("APP_MESSAGE", ""),
                    "hostname": socket.gethostname(),
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                },
            )
        elif self.path == "/livez":
            # Local-process liveness ONLY - never touches storage, so a
            # storage-layer failure never restarts an otherwise-healthy
            # process.
            self._write_json(200, {"status": "alive"})
        elif self.path == "/readyz":
            self._handle_readyz()
        elif self.path == "/config":
            self._write_json(200, visible_config())
        elif self.path == "/state":
            self._handle_get_state()
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def do_PUT(self) -> None:  # noqa: N802
        if self.path == "/state":
            self._handle_put_state()
        else:
            self._write_json(404, {"error": "not found", "path": self.path})

    def _method_not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, PUT")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _handle_readyz(self) -> None:
        if not READY:
            self._write_json(503, {"status": "starting"})
            return
        if _init_error is not None:
            # A pre-existing malformed/unreadable file was never
            # repaired - report it plainly rather than silently
            # replacing it with a fresh default.
            self._write_json(503, {"status": "invalid state", "detail": _init_error})
            return
        _, err = _read_record()
        if err is not None:
            self._write_json(503, {"status": "storage unusable", "detail": err})
            return
        self._write_json(200, {"status": "ready"})

    def _handle_get_state(self) -> None:
        if not self._authenticated():
            self._write_json(403, {"error": "forbidden"})
            return
        with _lock:
            record, err = _read_record()
        if err is not None:
            self._write_json(500, {"error": "state unavailable", "detail": err})
            return
        self._write_json(200, record)

    def _handle_put_state(self) -> None:
        if not self._authenticated():
            self._write_json(403, {"error": "forbidden"})
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._write_json(411, {"error": "Content-Length required"})
            return
        length = int(length_header)
        # Reject an oversized payload from the declared length alone -
        # never read an unbounded/oversized body into memory first.
        if length > STATE_MAX_BODY_BYTES:
            self._write_json(413, {"error": "payload too large", "max_bytes": STATE_MAX_BODY_BYTES})
            return

        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._write_json(400, {"error": "body must be UTF-8"})
            return
        try:
            obj = json.loads(text) if text else None
        except json.JSONDecodeError:
            self._write_json(400, {"error": "body must be valid JSON"})
            return
        if not _validate_record(obj):
            self._write_json(422, {"error": "body must match {'value': <string|null>} exactly"})
            return

        with _lock:
            outcome, detail = _persist_record(obj)

        if outcome == PersistOutcome.OK:
            self._write_json(200, {"status": "ok"})
        elif outcome == PersistOutcome.FAILED_UNCERTAIN:
            # Distinct from a clean failure: os.replace() itself
            # completed, so the write may or may not have durably
            # landed. Never reported as success.
            self._write_json(
                500,
                {
                    "error": "write outcome uncertain",
                    "detail": detail,
                },
            )
        else:
            self._write_json(500, {"error": "write failed", "detail": detail})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Deliberately logs only the request line/status - never header
        # values, so the state token is never printed.
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))


def main() -> None:
    threading.Thread(target=mark_ready_after_delay, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"maops-kubernetes-state listening on {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

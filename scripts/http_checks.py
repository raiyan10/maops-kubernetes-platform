"""Shared HTTP validation logic against the running Day 2 workloads via a
local port-forwarded port. Endpoint-specific semantic assertions
(inherited from DAY1-TEST-M3): HTTP 200 + valid JSON alone is not
sufficient - a routing bug that serves the wrong handler's body on the
right path/status would otherwise still PASS."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

GATEWAY_ENDPOINTS = ["/", "/livez", "/readyz", "/config", "/backend"]
APP_ENDPOINTS = ["/", "/livez", "/readyz", "/config"]

EXPECTED_LIVEZ_STATUS = "alive"
EXPECTED_READYZ_STATUS = "ready"
EXPECTED_ROOT_FIELDS = {"service", "message", "hostname", "uptime_seconds"}

GATEWAY_CONFIG_KEYS = {
    "BACKEND_HOST",
    "BACKEND_PORT",
    "BACKEND_TIMEOUT_SECONDS",
    "APP_NAME",
    "APP_ENVIRONMENT",
    "APP_MESSAGE",
    "APP_LOG_LEVEL",
}
GATEWAY_EXPECTED_BACKEND_HOST = "maops-app"
GATEWAY_EXPECTED_APP_NAME = "maops-kubernetes-gateway"

APP_CONFIG_KEYS = {"APP_NAME", "APP_ENVIRONMENT", "APP_MESSAGE", "APP_LOG_LEVEL"}
APP_EXPECTED_APP_NAME = "maops-kubernetes-app"

GATEWAY_BACKEND_FIELDS = {"gateway_hostname", "backend_service", "backend_hostname", "backend_environment"}

# Never assert against this literal - only used to sanity-check that a
# response/log line does not contain something header-shaped.
INTERNAL_TOKEN_HEADER = "X-MAOPS-Internal-Token"


def _check_semantics(role: str, path: str, payload) -> tuple[bool, str]:
    """Assert the documented stable response shape for a given
    role ('gateway' or 'app') + path, not just that *some* JSON came
    back. Returns (ok, detail)."""
    if not isinstance(payload, dict):
        return False, f"expected a JSON object, got {type(payload).__name__}"

    if "token" in json.dumps(payload).lower():
        return False, f"response unexpectedly appears to reference a token: keys={sorted(payload.keys())}"

    if path == "/livez":
        status = payload.get("status")
        if status != EXPECTED_LIVEZ_STATUS:
            return False, f"expected JSON status == {EXPECTED_LIVEZ_STATUS!r}, got {status!r}"
    elif path == "/readyz":
        status = payload.get("status")
        if status != EXPECTED_READYZ_STATUS:
            return False, f"expected JSON status == {EXPECTED_READYZ_STATUS!r}, got {status!r}"
    elif path == "/config":
        expected_keys = GATEWAY_CONFIG_KEYS if role == "gateway" else APP_CONFIG_KEYS
        expected_name = GATEWAY_EXPECTED_APP_NAME if role == "gateway" else APP_EXPECTED_APP_NAME
        missing = expected_keys - payload.keys()
        if missing:
            return False, f"missing expected ConfigMap-owned keys: {sorted(missing)}"
        if payload.get("APP_NAME") != expected_name:
            return False, f"expected APP_NAME == {expected_name!r}, got {payload.get('APP_NAME')!r}"
        if role == "gateway" and payload.get("BACKEND_HOST") != GATEWAY_EXPECTED_BACKEND_HOST:
            return False, f"expected BACKEND_HOST == {GATEWAY_EXPECTED_BACKEND_HOST!r}, got {payload.get('BACKEND_HOST')!r}"
    elif path == "/":
        missing = EXPECTED_ROOT_FIELDS - payload.keys()
        if missing:
            return False, f"missing expected stable fields: {sorted(missing)}"
    elif path == "/backend":
        missing = GATEWAY_BACKEND_FIELDS - payload.keys()
        if missing:
            return False, f"missing expected backend-proxy fields: {sorted(missing)}"
        if payload.get("backend_service") != APP_EXPECTED_APP_NAME:
            return False, (
                f"expected backend_service == {APP_EXPECTED_APP_NAME!r} (real data from maops-app via the "
                f"Service), got {payload.get('backend_service')!r}"
            )

    return True, ""


def check_endpoint(local_port: int, path: str, role: str = "gateway", timeout: float = 5.0) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{local_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return False, f"{path}: request failed: {exc}"

    if status != 200:
        return False, f"{path}: expected HTTP 200, got {status} (body={body!r})"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False, f"{path}: response was not valid JSON: {body!r}"

    semantics_ok, semantics_detail = _check_semantics(role, path, payload)
    if not semantics_ok:
        return False, f"{path}: {semantics_detail} (body={body!r})"
    return True, f"{path}: HTTP {status}, body={body}"


def check_all_endpoints(local_port: int, role: str = "gateway") -> list[tuple[bool, str]]:
    endpoints = GATEWAY_ENDPOINTS if role == "gateway" else APP_ENDPOINTS
    return [check_endpoint(local_port, path, role=role) for path in endpoints]


def raw_get(local_port: int, path: str, headers: dict | None = None, timeout: float = 5.0) -> tuple[int, str]:
    """Fetch `path` with optional headers, returning (status, body_text)
    without any semantic assertion - used for direct auth-negative checks
    (scripts/secret_check.py) where the caller decides what response is
    expected."""
    url = f"http://127.0.0.1:{local_port}{path}"
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


SAFE_UNAVAILABLE_BODY = {"error": "backend unavailable"}
_TRACEBACK_MARKERS = ("traceback", 'file "', "raise ", "exception:")


def check_safe_unavailable_body(body_text: str) -> tuple[bool, str]:
    """DAY2-INT-L2: validate a 503 "backend unavailable" response body is
    exactly the documented safe literal, not merely that the status code
    is 503. Used by scripts/dependency_check.py's outage proof, which
    previously only asserted `status == 503` despite its own log message
    claiming "no traceback, no token"."""
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return False, f"expected valid JSON, got {body_text!r}"
    if payload != SAFE_UNAVAILABLE_BODY:
        return False, f"expected exactly {SAFE_UNAVAILABLE_BODY}, got {payload!r}"
    lowered = body_text.lower()
    if "token" in lowered:
        return False, f"response unexpectedly references a token: {body_text!r}"
    if any(marker in lowered for marker in _TRACEBACK_MARKERS):
        return False, f"response looks like a traceback/diagnostic dump: {body_text!r}"
    return True, "body is the exact safe unavailable literal, no traceback, no token"


def fetch_config(local_port: int) -> dict:
    url = f"http://127.0.0.1:{local_port}/config"
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

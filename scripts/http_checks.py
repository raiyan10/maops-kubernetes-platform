"""Shared HTTP validation logic against the running workload via a local port."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

ENDPOINTS = ["/", "/livez", "/readyz", "/config"]

# Endpoint-specific semantic assertions (DAY1-TEST-M3): HTTP 200 + valid
# JSON alone is not sufficient - a routing bug that serves the wrong
# handler's body on the right path/status would otherwise still PASS.
EXPECTED_LIVEZ_STATUS = "alive"
EXPECTED_READYZ_STATUS = "ready"
EXPECTED_ROOT_FIELDS = {"service", "message", "hostname", "uptime_seconds"}
# The ConfigMap-owned keys app/server.py's /config exposes (envFrom from
# maops-app-config), and the two values that must match its current data.
EXPECTED_CONFIG_KEYS = {"APP_NAME", "APP_ENVIRONMENT", "APP_MESSAGE", "APP_LOG_LEVEL"}
EXPECTED_APP_NAME = "maops-kubernetes-platform"
EXPECTED_APP_MESSAGE = "Hello from the MAOps Kubernetes Platform (Day 1)"


def _check_semantics(path: str, payload) -> tuple[bool, str]:
    """Assert the documented stable response shape for a given path, not
    just that *some* JSON came back. Returns (ok, detail)."""
    if not isinstance(payload, dict):
        return False, f"expected a JSON object, got {type(payload).__name__}"

    if path == "/livez":
        status = payload.get("status")
        if status != EXPECTED_LIVEZ_STATUS:
            return False, f"expected JSON status == {EXPECTED_LIVEZ_STATUS!r}, got {status!r}"
    elif path == "/readyz":
        status = payload.get("status")
        if status != EXPECTED_READYZ_STATUS:
            return False, f"expected JSON status == {EXPECTED_READYZ_STATUS!r}, got {status!r}"
    elif path == "/config":
        missing = EXPECTED_CONFIG_KEYS - payload.keys()
        if missing:
            return False, f"missing expected ConfigMap-owned keys: {sorted(missing)}"
        if payload.get("APP_NAME") != EXPECTED_APP_NAME:
            return False, f"expected APP_NAME == {EXPECTED_APP_NAME!r}, got {payload.get('APP_NAME')!r}"
        if payload.get("APP_MESSAGE") != EXPECTED_APP_MESSAGE:
            return False, f"expected APP_MESSAGE == {EXPECTED_APP_MESSAGE!r}, got {payload.get('APP_MESSAGE')!r}"
    elif path == "/":
        missing = EXPECTED_ROOT_FIELDS - payload.keys()
        if missing:
            return False, f"missing expected stable fields: {sorted(missing)}"

    return True, ""


def check_endpoint(local_port: int, path: str, timeout: float = 5.0) -> tuple[bool, str]:
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

    semantics_ok, semantics_detail = _check_semantics(path, payload)
    if not semantics_ok:
        return False, f"{path}: {semantics_detail} (body={body!r})"
    return True, f"{path}: HTTP {status}, body={body}"


def check_all_endpoints(local_port: int) -> list[tuple[bool, str]]:
    return [check_endpoint(local_port, path) for path in ENDPOINTS]


def fetch_config(local_port: int) -> dict:
    url = f"http://127.0.0.1:{local_port}/config"
    with urllib.request.urlopen(url, timeout=5.0) as resp:
        return json.loads(resp.read().decode("utf-8"))

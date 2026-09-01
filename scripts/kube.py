"""Small kubectl subprocess/JSON helper shared by the real-cluster validation scripts."""

from __future__ import annotations

import json
import subprocess
import time

CONTEXT = "kind-maops-k8s-day1"
NAMESPACE = "maops-platform"
DEPLOYMENT = "maops-app"
SERVICE = "maops-app"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def get_json(*args: str):
    result = run(*args, "-o", "json")
    return json.loads(result.stdout)


def wait_until(predicate, timeout: float, interval: float = 2.0, description: str = "condition"):
    """Poll `predicate` until it signals success or `timeout` elapses.

    Sentinel contract: `predicate` returns `None` to mean "not ready yet,
    keep retrying" and anything else (including falsy values like `0`,
    `""`, or `[]`) to mean "success - return this value". This lets a
    predicate report a legitimately falsy successful result without it
    being mistaken for "not ready".
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value is not None:
                return value
        except Exception as exc:  # noqa: BLE001 - surfaced in the final timeout message
            last_error = exc
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for: {description} (last error: {last_error})")

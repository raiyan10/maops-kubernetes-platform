#!/usr/bin/env python3
"""
Runtime Secret bootstrap for the Day 3 internal auth token.

The committed Kustomize base deliberately contains NO Secret object -
`maops-internal-auth` is created out-of-band by this script, against
the explicit context/namespace below, and is never committed with a
usable token in it.

Behavior:
  1. Uses explicit context kind-maops-k8s-day3 and namespace maops-platform
     for every kubectl call.
  2. If the Secret does not exist: generates a cryptographically-strong
     random token (`secrets.token_urlsafe`), writes it to a private
     temporary file (0600, never on a command line), creates the Secret
     via `kubectl create secret generic --from-file`, then deletes the
     temp file in a guaranteed `finally` block.
  3. If the Secret already exists: it is preserved, never silently
     rotated - only validated.
  4. The token value is never printed, logged, or otherwise displayed by
     this script under any code path, success or failure.
  5. Fails non-zero if the Secret ends up missing the expected key or
     the key's value is empty.

Usage:
    python3 scripts/secret_bootstrap.py
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import CONTEXT, INTERNAL_SECRET, INTERNAL_SECRET_KEY, NAMESPACE, run


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def get_existing_secret() -> dict | None:
    """Returns the parsed Secret JSON if it exists, None if it does not.
    Raises RuntimeError for any other kubectl failure (e.g. namespace
    missing).

    DAY2-INT-L1: a missing namespace and a missing Secret both surface a
    kubectl stderr containing "NotFound" - treating that substring alone
    as "Secret does not exist" would misclassify a missing namespace as
    "generate a new token" territory. The namespace is checked explicitly
    first, so a missing namespace fails closed here with its own error,
    before any Secret-specific logic (or token generation) runs."""
    ns_result = run("get", "namespace", NAMESPACE, check=False)
    if ns_result.returncode != 0:
        ns_stderr = (ns_result.stderr or "").strip()
        raise RuntimeError(f"namespace {NAMESPACE!r} is not available: {ns_stderr}")

    result = run("-n", NAMESPACE, "get", "secret", INTERNAL_SECRET, "-o", "json", check=False)
    if result.returncode == 0:
        import json

        return json.loads(result.stdout)
    stderr = (result.stderr or "").strip()
    if "NotFound" in stderr or "not found" in stderr:
        return None
    raise RuntimeError(f"kubectl get secret {INTERNAL_SECRET} failed unexpectedly: {stderr}")


def validate_secret_shape(secret_json: dict) -> tuple[bool, str]:
    """Verifies the expected key exists and decodes to a non-empty value,
    WITHOUT ever returning or printing that value."""
    data = secret_json.get("data") or {}
    if INTERNAL_SECRET_KEY not in data:
        return False, f"Secret {INTERNAL_SECRET!r} is missing expected key {INTERNAL_SECRET_KEY!r}"
    try:
        decoded = base64.b64decode(data[INTERNAL_SECRET_KEY], validate=True)
    except (binascii.Error, ValueError):
        return False, f"Secret {INTERNAL_SECRET!r} key {INTERNAL_SECRET_KEY!r} is not valid base64"
    if len(decoded) == 0:
        return False, f"Secret {INTERNAL_SECRET!r} key {INTERNAL_SECRET_KEY!r} is empty"
    return True, f"Secret {INTERNAL_SECRET!r} key {INTERNAL_SECRET_KEY!r} present and non-empty ({len(decoded)} bytes)"


def create_secret(token: str) -> None:
    """Writes `token` to a private temp file (never a command-line
    argument) and creates the Secret from that file, guaranteeing the
    temp file is removed afterward regardless of outcome."""
    fd, tmp_path = tempfile.mkstemp(prefix="maops-internal-token-", text=False)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(token.encode("utf-8"))
        run(
            "-n",
            NAMESPACE,
            "create",
            "secret",
            "generic",
            INTERNAL_SECRET,
            f"--from-file={INTERNAL_SECRET_KEY}={tmp_path}",
        )
    finally:
        with _suppress_missing():
            os.remove(tmp_path)


class _suppress_missing:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        return exc_type is FileNotFoundError


def main() -> int:
    print(f"# Secret bootstrap: {INTERNAL_SECRET} in namespace {NAMESPACE} (context {CONTEXT})")

    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        existing = get_existing_secret()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if existing is not None:
        print(f"Secret {INTERNAL_SECRET!r} already exists - preserving it (no rotation).")
        ok, message = validate_secret_shape(existing)
        print(f"[{'PASS' if ok else 'FAIL'}] {message}")
        if not ok:
            print("FAIL: existing Secret is malformed", file=sys.stderr)
            return 1
        print("PASS: secret bootstrap verified existing Secret")
        return 0

    print(f"Secret {INTERNAL_SECRET!r} does not exist - generating a new token.")
    token = generate_token()
    try:
        create_secret(token)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", None) or ""
        stderr = stderr.strip() if isinstance(stderr, str) else str(stderr)
        print(f"FAIL: kubectl create secret failed/timed out: {stderr if stderr else exc}", file=sys.stderr)
        return 1
    print(f"Secret {INTERNAL_SECRET!r} created.")

    try:
        created = get_existing_secret()
    except RuntimeError as exc:
        print(f"FAIL: post-create verification failed: {exc}", file=sys.stderr)
        return 1
    if created is None:
        print("FAIL: Secret not found immediately after creation", file=sys.stderr)
        return 1
    ok, message = validate_secret_shape(created)
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    if not ok:
        print("FAIL: newly-created Secret is malformed", file=sys.stderr)
        return 1

    print("PASS: secret bootstrap created and verified a new Secret")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

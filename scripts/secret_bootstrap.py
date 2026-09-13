#!/usr/bin/env python3
"""
Runtime Secret bootstrap for the Day 4 internal auth tokens.

The committed Kustomize base deliberately contains NO Secret object -
`maops-internal-auth` (gateway -> app, unchanged since Day 2) and
`maops-state-auth` (app -> state, new in Day 4 - only app and state
ever hold this credential; gateway does not) are both created
out-of-band by this script, against the explicit context/namespace
below, and are never committed with a usable token in them.

Behavior, identical for both secrets:
  1. Uses explicit context kind-maops-k8s-day4 and namespace maops-platform
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
    python3 scripts/secret_bootstrap.py            # internal-auth only (default, unchanged since Day 2)
    python3 scripts/secret_bootstrap.py internal    # same as above, explicit
    python3 scripts/secret_bootstrap.py state       # maops-state-auth only
    python3 scripts/secret_bootstrap.py all         # both (what `make secret-bootstrap` runs as of Day 4)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from kube import CONTEXT, INTERNAL_SECRET, INTERNAL_SECRET_KEY, NAMESPACE, STATE_SECRET, STATE_SECRET_KEY, run


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _get_secret(secret_name: str) -> dict | None:
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

    result = run("-n", NAMESPACE, "get", "secret", secret_name, "-o", "json", check=False)
    if result.returncode == 0:
        return json.loads(result.stdout)
    stderr = (result.stderr or "").strip()
    if "NotFound" in stderr or "not found" in stderr:
        return None
    raise RuntimeError(f"kubectl get secret {secret_name} failed unexpectedly: {stderr}")


def get_existing_secret() -> dict | None:
    """maops-internal-auth (gateway -> app). Kept as its own zero-arg
    function - unchanged since Day 2 - so existing tests that mock this
    exact name/signature keep working unmodified."""
    return _get_secret(INTERNAL_SECRET)


def get_existing_state_secret() -> dict | None:
    """DAY4: maops-state-auth (app -> state)."""
    return _get_secret(STATE_SECRET)


def validate_secret_shape(
    secret_json: dict, secret_name: str = INTERNAL_SECRET, secret_key: str = INTERNAL_SECRET_KEY
) -> tuple[bool, str]:
    """Verifies the expected key exists and decodes to a non-empty value,
    WITHOUT ever returning or printing that value. Defaults to
    maops-internal-auth's name/key so existing single-argument call
    sites (and tests) are unaffected; DAY4's state-secret bootstrap
    passes STATE_SECRET/STATE_SECRET_KEY explicitly."""
    data = secret_json.get("data") or {}
    if secret_key not in data:
        return False, f"Secret {secret_name!r} is missing expected key {secret_key!r}"
    try:
        decoded = base64.b64decode(data[secret_key], validate=True)
    except (binascii.Error, ValueError):
        return False, f"Secret {secret_name!r} key {secret_key!r} is not valid base64"
    if len(decoded) == 0:
        return False, f"Secret {secret_name!r} key {secret_key!r} is empty"
    return True, f"Secret {secret_name!r} key {secret_key!r} present and non-empty ({len(decoded)} bytes)"


def _create_secret(secret_name: str, secret_key: str, token: str) -> None:
    """Writes `token` to a private temp file (never a command-line
    argument) and creates the Secret from that file, guaranteeing the
    temp file is removed afterward regardless of outcome."""
    fd, tmp_path = tempfile.mkstemp(prefix=f"maops-{secret_key}-", text=False)
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
            secret_name,
            f"--from-file={secret_key}={tmp_path}",
        )
    finally:
        with _suppress_missing():
            os.remove(tmp_path)


def create_secret(token: str) -> None:
    """maops-internal-auth. Kept as its own one-arg function - unchanged
    since Day 2 - so existing tests that mock this exact name/signature
    keep working unmodified."""
    _create_secret(INTERNAL_SECRET, INTERNAL_SECRET_KEY, token)


def create_state_secret(token: str) -> None:
    """DAY4: maops-state-auth."""
    _create_secret(STATE_SECRET, STATE_SECRET_KEY, token)


class _suppress_missing:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _tb):
        return exc_type is FileNotFoundError


def _bootstrap_internal() -> int:
    """Unchanged Day 2/3 body, byte-for-byte - kept as its own function
    (not merged into a generic loop) so every existing test that mocks
    `get_existing_secret`/`create_secret`/`validate_secret_shape` at
    module level, with exactly the call counts today's tests assert,
    keeps passing unmodified."""
    print(f"# Secret bootstrap: {INTERNAL_SECRET} in namespace {NAMESPACE} (context {CONTEXT})")

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


def _bootstrap_state() -> int:
    """DAY4: identical control flow to _bootstrap_internal(), against
    maops-state-auth via the dedicated get_existing_state_secret()/
    create_state_secret() functions."""
    print(f"# Secret bootstrap: {STATE_SECRET} in namespace {NAMESPACE} (context {CONTEXT})")

    try:
        existing = get_existing_state_secret()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if existing is not None:
        print(f"Secret {STATE_SECRET!r} already exists - preserving it (no rotation).")
        ok, message = validate_secret_shape(existing, STATE_SECRET, STATE_SECRET_KEY)
        print(f"[{'PASS' if ok else 'FAIL'}] {message}")
        if not ok:
            print("FAIL: existing Secret is malformed", file=sys.stderr)
            return 1
        print("PASS: secret bootstrap verified existing Secret")
        return 0

    print(f"Secret {STATE_SECRET!r} does not exist - generating a new token.")
    token = generate_token()
    try:
        create_state_secret(token)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", None) or ""
        stderr = stderr.strip() if isinstance(stderr, str) else str(stderr)
        print(f"FAIL: kubectl create secret failed/timed out: {stderr if stderr else exc}", file=sys.stderr)
        return 1
    print(f"Secret {STATE_SECRET!r} created.")

    try:
        created = get_existing_state_secret()
    except RuntimeError as exc:
        print(f"FAIL: post-create verification failed: {exc}", file=sys.stderr)
        return 1
    if created is None:
        print("FAIL: Secret not found immediately after creation", file=sys.stderr)
        return 1
    ok, message = validate_secret_shape(created, STATE_SECRET, STATE_SECRET_KEY)
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    if not ok:
        print("FAIL: newly-created Secret is malformed", file=sys.stderr)
        return 1

    print("PASS: secret bootstrap created and verified a new Secret")
    return 0


def main(target: str = "internal") -> int:
    """`target` defaults to "internal" so every existing zero-argument
    `secret_bootstrap.main()` call (all of today's tests, and the plain
    `python3 scripts/secret_bootstrap.py` invocation) behaves exactly as
    it did before Day 4. "state" bootstraps only maops-state-auth;
    "all" (what `make secret-bootstrap` runs as of Day 4) does both, in
    order, and fails if either does."""
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if target == "internal":
        return _bootstrap_internal()
    if target == "state":
        return _bootstrap_state()
    if target == "all":
        rc_internal = _bootstrap_internal()
        rc_state = _bootstrap_state()
        return 0 if (rc_internal == 0 and rc_state == 0) else 1

    print(f"FAIL: unknown secret bootstrap target {target!r} (expected internal/state/all)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    _target = sys.argv[1] if len(sys.argv) > 1 else "internal"
    raise SystemExit(main(_target))

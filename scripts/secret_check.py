#!/usr/bin/env python3
"""
Real Secret behavior / non-disclosure validation (Day 2).

Proves the runtime Secret (maops-internal-auth) is wired correctly and
never disclosed, end to end:

  - Secret exists, has the expected key, and the key is non-empty.
  - Both the gateway and app Pods mount it read-only at the expected
    path.
  - Both processes can actually use/read the mounted file.
  - A gateway-authenticated call to the app's protected endpoint
    succeeds.
  - A direct, unauthenticated call to the app's protected endpoint is
    rejected with exactly HTTP 403 (and so is a wrong-token call).
  - Neither workload's normal HTTP responses, /config, nor pod logs ever
    contain the token value.
  - No tracked repository file contains the live generated token.

SECRET NON-DISCLOSURE DISCIPLINE: this script holds the decoded token in
memory to build the correct Authorization header and to compare against
logs/repo content - it NEVER prints, logs, or otherwise displays it, and
never passes it as a subprocess command-line argument.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cluster_check import get_pods
from http_checks import check_all_endpoints, raw_get
from kube import (
    APP_LABEL_SELECTOR,
    APP_SERVICE,
    CONTEXT,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_SERVICE,
    INTERNAL_SECRET,
    INTERNAL_SECRET_KEY,
    NAMESPACE,
    get_json,
    run,
)
from portforward import port_forward
from secret_bootstrap import get_existing_secret, validate_secret_shape

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_HEADER = "X-MAOPS-Internal-Token"
WRONG_TOKEN = "definitely-not-the-real-internal-token"  # nosec: not a credential

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def get_token() -> bytes | None:
    secret = get_existing_secret()
    if secret is None:
        return None
    encoded = (secret.get("data") or {}).get(INTERNAL_SECRET_KEY)
    if not encoded:
        return None
    return base64.b64decode(encoded)


def check_secret_shape() -> bool:
    secret = get_existing_secret()
    if secret is None:
        record(False, f"Secret {INTERNAL_SECRET!r} does not exist")
        return False
    ok, message = validate_secret_shape(secret)
    record(ok, message)
    return ok


def _mount_findings(component: str, pods: list[dict]) -> None:
    if not pods:
        record(False, f"{component} Secret mount: no pods available to inspect")
        return
    pod = get_json("-n", NAMESPACE, "get", "pod", pods[0]["metadata"]["name"])
    volumes = pod["spec"].get("volumes") or []
    volume = next((v for v in volumes if v.get("name") == "internal-auth"), {})
    record(
        (volume.get("secret") or {}).get("secretName") == INTERNAL_SECRET,
        f"{component} pod mounts Secret {(volume.get('secret') or {}).get('secretName')!r} via volume "
        f"'internal-auth' (expected {INTERNAL_SECRET!r})",
    )
    container = pod["spec"]["containers"][0]
    mount = next((m for m in (container.get("volumeMounts") or []) if m.get("name") == "internal-auth"), {})
    record(
        mount.get("mountPath") == "/var/run/secrets/maops" and mount.get("readOnly") is True,
        f"{component} pod mount is read-only at {mount.get('mountPath')!r} (readOnly={mount.get('readOnly')!r})",
    )


def check_pod_mounts() -> tuple[list[dict], list[dict]]:
    gateway_pods = get_pods(GATEWAY_LABEL_SELECTOR)
    app_pods = get_pods(APP_LABEL_SELECTOR)
    _mount_findings("gateway", gateway_pods)
    _mount_findings("app", app_pods)
    return gateway_pods, app_pods


def check_process_can_read_mount(component: str, pods: list[dict], expected_length: int | None) -> None:
    if not pods:
        record(False, f"{component} secret readability: no pods available to exec into")
        return
    pod_name = pods[0]["metadata"]["name"]
    snippet = (
        "import sys\n"
        "with open('/var/run/secrets/maops/internal-token', 'rb') as f:\n"
        "    sys.stdout.write(str(len(f.read())))\n"
    )
    try:
        result = run("-n", NAMESPACE, "exec", pod_name, "--", "/usr/bin/python3.11", "-c", snippet)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        record(False, f"{component} secret readability: kubectl exec failed in pod {pod_name}: {stderr if stderr else exc}")
        return
    try:
        observed_length = int(result.stdout.strip())
    except ValueError:
        record(False, f"{component} secret readability: unexpected exec output length in pod {pod_name}")
        return
    ok = observed_length > 0 and (expected_length is None or observed_length == expected_length)
    record(
        ok,
        f"{component} pod {pod_name} can read the mounted Secret file (length {observed_length} bytes, "
        f"never displayed)",
    )


def check_gateway_authenticated_request() -> None:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            results_list = check_all_endpoints(local_port, role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"gateway HTTP checks: port-forward failed: {exc}")
        return
    for ok, msg in results_list:
        record(ok, f"gateway (via Service, non-disclosure scanned): {msg}")


def check_app_direct_auth(token: bytes | None) -> None:
    """DAY2 test-only exception: port-forwards service/maops-app directly
    (never the normal architecture) purely to exercise the app's own auth
    boundary and non-disclosure behavior."""
    try:
        with port_forward(CONTEXT, NAMESPACE, APP_SERVICE, 8080) as local_port:
            # Normal endpoints must not expose the token either.
            for ok, msg in check_all_endpoints(local_port, role="app"):
                record(ok, f"app (direct, non-disclosure scanned): {msg}")

            status, body = raw_get(local_port, "/internal/info")
            record(status == 403, f"app /internal/info with no token -> HTTP {status} (expected exactly 403)")
            record("token" not in body.lower(), "app 403 (no token) response body does not leak the token")

            status, body = raw_get(local_port, "/internal/info", headers={TOKEN_HEADER: WRONG_TOKEN})
            record(status == 403, f"app /internal/info with wrong token -> HTTP {status} (expected exactly 403)")
            record("token" not in body.lower(), "app 403 (wrong token) response body does not leak the token")

            if token is not None:
                status, body = raw_get(local_port, "/internal/info", headers={TOKEN_HEADER: token.decode("utf-8")})
                record(status == 200, f"app /internal/info with correct token -> HTTP {status} (expected 200)")
                record("token" not in body.lower(), "app 200 response body does not leak the token")
            else:
                record(False, "app correct-token check: no token available to test with")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"app direct auth checks: port-forward failed: {exc}")


def check_logs_do_not_expose_token(component: str, pods: list[dict], token: bytes | None) -> None:
    if token is None:
        record(False, f"{component} log non-disclosure: no token available to compare against")
        return
    if not pods:
        record(False, f"{component} log non-disclosure: no pods available to inspect")
        return
    token_str = token.decode("utf-8", errors="replace")
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        result = run("-n", NAMESPACE, "logs", pod_name, "--tail=2000", check=False)
        logs = result.stdout or ""
        record(token_str not in logs, f"{component} pod {pod_name} logs do not contain the secret value")


def check_repo_files_do_not_contain_token(token: bytes | None) -> None:
    if token is None:
        record(False, "repository non-disclosure: no token available to compare against")
        return
    token_str = token.decode("utf-8", errors="replace")
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True, text=True, check=True
    )
    tracked_files = [line for line in result.stdout.splitlines() if line]
    offending: list[str] = []
    for relative_path in tracked_files:
        path = REPO_ROOT / relative_path
        try:
            content = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        if token_str in content:
            offending.append(relative_path)
    record(
        not offending,
        f"no tracked repository file ({len(tracked_files)} checked) contains the live secret value"
        + (f" - offending: {offending}" if offending else ""),
    )


def main() -> int:
    print("# Real Secret behavior / non-disclosure validation")

    if not check_secret_shape():
        print()
        print(f"0/{len(results)} secret checks passed")
        print("FAIL: Secret is not in a valid state - aborting remaining checks", file=sys.stderr)
        return 1

    token = get_token()

    gateway_pods, app_pods = check_pod_mounts()
    check_process_can_read_mount("gateway", gateway_pods, len(token) if token else None)
    check_process_can_read_mount("app", app_pods, len(token) if token else None)

    check_gateway_authenticated_request()
    check_app_direct_auth(token)

    check_logs_do_not_expose_token("gateway", gateway_pods, token)
    check_logs_do_not_expose_token("app", app_pods, token)

    check_repo_files_do_not_contain_token(token)

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} secret checks passed")
    if failures:
        print(f"FAIL: {len(failures)} secret check(s) failed", file=sys.stderr)
        return 1
    print("PASS: all secret behavior/non-disclosure checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

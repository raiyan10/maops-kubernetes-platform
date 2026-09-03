#!/usr/bin/env python3
"""
Real dependency-failure behavior proof (Day 2).

Proves gateway liveness is genuinely independent of dependency-aware
readiness by scaling ONLY maops-app to 0 replicas (never touching
maops-gateway's replica count) and observing, from a live gateway Pod
(port-forwarded directly - bypassing the Service, which may stop
routing to a not-Ready gateway Pod):

  - gateway /livez  -> still HTTP 200 (local-process liveness only)
  - gateway /readyz -> HTTP 503 (dependency-aware readiness correctly
    reflects the unavailable backend)
  - gateway /backend -> controlled HTTP 503 (no traceback, no token)
  - gateway container restart counts do NOT increase merely because the
    app dependency became unavailable (only liveness failures restart a
    container; the readinessProbe failing does not)

maops-app is ALWAYS restored to exactly 2 replicas in a guaranteed
`finally` path, whether the experiment above succeeded or not. If
restoration itself fails, that is reported prominently and separately
from the original experiment's result - never silently swallowed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cluster_check import _stderr_detail, get_pods
from endpointslice import count_ready_endpoints
from http_checks import check_endpoint, check_safe_unavailable_body, raw_get
from kube import (
    APP_LABEL_SELECTOR,
    APP_SERVICE,
    CONTEXT,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_SERVICE,
    NAMESPACE,
    get_json,
    run,
    wait_until,
)
from portforward import port_forward

EXPECTED_REPLICAS = 2

results: list[tuple[bool, str]] = []
restoration_results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def record_restoration(ok: bool, message: str) -> bool:
    restoration_results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] RESTORATION: {message}")
    return ok


def _deployment_ready(name: str) -> dict | None:
    dep = get_json("-n", NAMESPACE, "get", "deployment", name)
    if dep.get("status", {}).get("readyReplicas") == EXPECTED_REPLICAS:
        return dep
    return None


def _wait_ready(name: str, timeout: float) -> None:
    wait_until(lambda: _deployment_ready(name), timeout=timeout, interval=3, description=f"deployment/{name} 2/2 Ready")


def get_restart_counts(pods: list[dict]) -> dict[str, int]:
    counts = {}
    for pod in pods:
        statuses = pod.get("status", {}).get("containerStatuses") or []
        counts[pod["metadata"]["name"]] = sum(s.get("restartCount", 0) for s in statuses)
    return counts


def check_starting_state() -> list[dict]:
    try:
        _wait_ready("maops-gateway", timeout=60)
        record(True, "starting state: gateway 2/2 Ready")
    except TimeoutError as exc:
        record(False, f"starting state: {exc}")
    try:
        _wait_ready("maops-app", timeout=60)
        record(True, "starting state: app 2/2 Ready")
    except TimeoutError as exc:
        record(False, f"starting state: {exc}")
    return get_pods(GATEWAY_LABEL_SELECTOR)


def scale_app(replicas: int) -> None:
    run("-n", NAMESPACE, "scale", "deployment/maops-app", f"--replicas={replicas}")


def restore_app() -> bool:
    """Guaranteed restoration path. Always attempted once the app has been
    scaled to 0, regardless of what happened during the experiment.
    Reports prominently and fails loudly if restoration itself fails -
    never hidden behind the original experiment's outcome."""
    try:
        scale_app(EXPECTED_REPLICAS)
    except subprocess.CalledProcessError as exc:
        record_restoration(False, f"could not scale maops-app back to {EXPECTED_REPLICAS} replicas: {_stderr_detail(exc)}")
        return False

    try:
        _wait_ready("maops-app", timeout=120)
    except TimeoutError as exc:
        record_restoration(False, f"maops-app did not recover to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready: {exc}")
        return False
    record_restoration(True, f"maops-app restored to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready")

    try:
        _wait_ready("maops-gateway", timeout=90)
    except TimeoutError as exc:
        record_restoration(False, f"maops-gateway did not recover to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready: {exc}")
        return False
    record_restoration(True, f"maops-gateway recovered to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready")

    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok_ready, detail_ready = check_endpoint(local_port, "/readyz", role="gateway")
            ok_backend, detail_backend = check_endpoint(local_port, "/backend", role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        record_restoration(False, f"post-recovery Service HTTP check failed: {exc}")
        return False

    record_restoration(ok_ready, f"post-recovery gateway /readyz: {detail_ready}")
    record_restoration(ok_backend, f"post-recovery gateway /backend: {detail_backend}")
    return ok_ready and ok_backend


def _app_endpoints_drained():
    """Termination-race predicate (DAY2-TEST-H1): EndpointSlice draining
    alone is not sufficient - a Pod can still be Terminating (grace
    period) and genuinely still answering HTTP for a short window after
    the API object is gone, and node-local kube-proxy iptables sync lags
    the API by a small margin too. Requiring the Pod objects themselves
    to be fully gone is the only signal that guarantees no process can
    still respond.

    Module-level (not a closure) and built only on the already-injectable
    `get_json`/`get_pods` module attributes specifically so it is directly
    unit-testable against the real function - see
    tests/test_dependency_check.py::AppEndpointsDrainedTests. Any
    exception raised while querying the cluster propagates to the caller
    (wait_until's retry/timeout handling) rather than resolving to a
    false "drained".
    """
    slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={APP_SERVICE}")[
        "items"
    ]
    if count_ready_endpoints(slices) != 0:
        return None
    return True if not get_pods(APP_LABEL_SELECTOR) else None


def run_experiment(gateway_pods: list[dict]) -> None:
    if not gateway_pods:
        record(False, "dependency-failure experiment: no gateway pods available")
        return

    before_restarts = get_restart_counts(gateway_pods)

    try:
        wait_until(
            _app_endpoints_drained,
            timeout=90,
            interval=1,
            description="maops-app EndpointSlice drained and all app Pods fully terminated",
        )
    except TimeoutError as exc:
        record(False, f"waiting for maops-app to scale to 0: {exc}")
        return
    record(True, "maops-app scaled to 0 replicas, EndpointSlice drained, and all app Pods fully terminated (maops-gateway replicas untouched)")

    gateway_pod_name = gateway_pods[0]["metadata"]["name"]
    try:
        with port_forward(CONTEXT, NAMESPACE, gateway_pod_name, 8080, resource_kind="pod") as local_port:
            ok_live, detail_live = check_endpoint(local_port, "/livez", role="gateway")
            record(ok_live, f"gateway /livez during app outage: {detail_live}")

            status, _body = raw_get(local_port, "/readyz")
            record(status == 503, f"gateway /readyz during app outage -> HTTP {status} (expected 503)")

            status, body = raw_get(local_port, "/backend")
            if status != 503:
                record(False, f"gateway /backend during app outage -> HTTP {status} (expected 503)")
            else:
                body_ok, body_detail = check_safe_unavailable_body(body)
                record(body_ok, f"gateway /backend during app outage -> HTTP {status}, {body_detail}")
    except (TimeoutError, RuntimeError) as exc:
        record(False, f"gateway direct pod port-forward failed during app outage: {exc}")
        return

    after_restarts = get_restart_counts(get_pods(GATEWAY_LABEL_SELECTOR))
    record(
        before_restarts == after_restarts,
        f"gateway container restart counts unchanged solely due to app outage: before={before_restarts} after={after_restarts}",
    )


def main() -> int:
    print("# Real dependency-failure behavior proof (gateway liveness vs. dependency-aware readiness)")

    gateway_pods = check_starting_state()

    scaled_down = False
    try:
        scale_app(0)
        scaled_down = True
        run_experiment(gateway_pods)
    except subprocess.CalledProcessError as exc:
        record(False, f"scaling maops-app to 0 failed: {_stderr_detail(exc)}")
    except Exception as exc:  # noqa: BLE001 - a controlled failure record, never a swallowed one; restoration below still always runs
        record(False, f"dependency-failure experiment raised an unexpected error: {exc}")
    finally:
        if scaled_down:
            restore_app()

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]

    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} dependency-failure checks passed")
    if restoration_failures:
        print()
        print("!!! RESTORATION FAILURE - maops-app may not be back at 2/2 Ready - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} dependency-failure check(s) failed", file=sys.stderr)
        return 1
    print("PASS: dependency-failure behavior proven and maops-app restored to 2/2 Ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

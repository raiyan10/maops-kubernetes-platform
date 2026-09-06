#!/usr/bin/env python3
"""
Real Day 3 scaling validation (manual `kubectl scale` - HorizontalPodAutoscaler
is out of scope for Day 3).

For BOTH maops-gateway and maops-app, one workload at a time:

  1. Baseline: 3/3 Ready, 3 ready EndpointSlice endpoints.
  2. Scale 3 -> 4: proves Deployment desired/Ready == 4, 4 Pods Ready,
     EndpointSlice shows 4 unique ready endpoints, and the Service
     remains functional (for app scaling: gateway -> app /backend stays
     functional; for gateway scaling: the gateway Service's own HTTP
     stays functional).
  3. Restore 4 -> 3 in a guaranteed `finally` path, independently
     re-verified: desired/Ready == 3, 3 Pods Ready, EndpointSlice back to
     3. A restoration failure is reported prominently and fails the run,
     even if the scale-up experiment itself passed.

A failed assertion during the scale-up phase never skips restoration -
the workload is never left at 4 replicas because this script's checks
failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
from cluster_check import _stderr_detail
from endpointslice import count_ready_endpoints
from http_checks import check_endpoint
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

BASELINE_REPLICAS = 3
SCALE_TARGET_REPLICAS = 4

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


def _deployment_at(name: str, replicas: int):
    dep = get_json("-n", NAMESPACE, "get", "deployment", name)
    if dep.get("spec", {}).get("replicas") == replicas and dep.get("status", {}).get("readyReplicas") == replicas:
        return dep
    return None


def _wait_deployment_at(name: str, replicas: int, timeout: float):
    return wait_until(
        lambda: _deployment_at(name, replicas),
        timeout=timeout,
        interval=2,
        description=f"deployment/{name} desired=={replicas} Ready=={replicas}",
    )


def _pods_ready_count(label_selector: str) -> int:
    pods = get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]
    ready = [
        p
        for p in pods
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
    ]
    return len(ready) if len(pods) == len(ready) else -1


def _wait_pods_ready(label_selector: str, count: int, timeout: float, description: str):
    def predicate():
        n = _pods_ready_count(label_selector)
        return n if n == count else None

    return wait_until(predicate, timeout=timeout, interval=2, description=description)


def _wait_endpointslice_count(service: str, count: int, timeout: float):
    def predicate():
        slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}")[
            "items"
        ]
        ready = count_ready_endpoints(slices)
        return ready if ready == count else None

    return wait_until(predicate, timeout=timeout, interval=2, description=f"{service} EndpointSlice ready endpoints == {count}")


def scale_deployment(name: str, replicas: int) -> None:
    run("-n", NAMESPACE, "scale", f"deployment/{name}", f"--replicas={replicas}")


def check_gateway_service_functional() -> bool:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok, detail = check_endpoint(local_port, "/livez", role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        return record(False, f"gateway Service HTTP functional check: port-forward failed: {exc}")
    return record(ok, f"gateway Service HTTP functional check: {detail}")


def check_app_reachable_via_gateway() -> bool:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok, detail = check_endpoint(local_port, "/backend", role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        return record(False, f"gateway -> app /backend functional check: port-forward failed: {exc}")
    return record(ok, f"gateway -> app /backend functional check: {detail}")


def run_scaling_experiment(component: str, deployment: str, service: str, label_selector: str, functional_check) -> None:
    print(f"## Scaling experiment: {component} ({deployment})")

    try:
        _wait_deployment_at(deployment, BASELINE_REPLICAS, timeout=60)
        record(True, f"{component} baseline: {deployment} desired/Ready == {BASELINE_REPLICAS}")
    except TimeoutError as exc:
        record(False, f"{component} baseline: {exc}")
        return
    try:
        _wait_pods_ready(label_selector, BASELINE_REPLICAS, timeout=30, description=f"{component} baseline Pods Ready")
        record(True, f"{component} baseline: {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Pods Ready")
    except TimeoutError as exc:
        record(False, f"{component} baseline Pods Ready: {exc}")
        return
    try:
        _wait_endpointslice_count(service, BASELINE_REPLICAS, timeout=30)
        record(True, f"{component} baseline: EndpointSlice has {BASELINE_REPLICAS} ready endpoint(s)")
    except TimeoutError as exc:
        record(False, f"{component} baseline EndpointSlice: {exc}")
        return

    scaled_up = False
    try:
        scale_deployment(deployment, SCALE_TARGET_REPLICAS)
        scaled_up = True

        try:
            dep = _wait_deployment_at(deployment, SCALE_TARGET_REPLICAS, timeout=90)
            record(
                dep.get("spec", {}).get("replicas") == SCALE_TARGET_REPLICAS
                and dep.get("status", {}).get("readyReplicas") == SCALE_TARGET_REPLICAS,
                f"{component} scale-up: {deployment} desired=={dep.get('spec', {}).get('replicas')} "
                f"Ready=={dep.get('status', {}).get('readyReplicas')} (expected {SCALE_TARGET_REPLICAS}/{SCALE_TARGET_REPLICAS})",
            )
        except TimeoutError as exc:
            record(False, f"{component} scale-up: {exc}")

        try:
            _wait_pods_ready(label_selector, SCALE_TARGET_REPLICAS, timeout=60, description=f"{component} scale-up Pods Ready")
            record(True, f"{component} scale-up: {SCALE_TARGET_REPLICAS}/{SCALE_TARGET_REPLICAS} Pods Ready")
        except TimeoutError as exc:
            record(False, f"{component} scale-up Pods Ready: {exc}")

        try:
            _wait_endpointslice_count(service, SCALE_TARGET_REPLICAS, timeout=60)
            record(True, f"{component} scale-up: EndpointSlice shows {SCALE_TARGET_REPLICAS} unique ready endpoints")
        except TimeoutError as exc:
            record(False, f"{component} scale-up EndpointSlice: {exc}")

        functional_check()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"{component} scale-up: kubectl scale failed/timed out: {_stderr_detail(exc)}")
    except Exception as exc:  # noqa: BLE001 - controlled record, restoration below still always runs
        record(False, f"{component} scaling experiment raised an unexpected error: {exc}")
    finally:
        if scaled_up:
            restore_workload(component, deployment, service, label_selector)


def restore_workload(component: str, deployment: str, service: str, label_selector: str) -> bool:
    try:
        scale_deployment(deployment, BASELINE_REPLICAS)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record_restoration(False, f"{component}: could not scale {deployment} back to {BASELINE_REPLICAS}: {_stderr_detail(exc)}")
        return False

    try:
        dep = _wait_deployment_at(deployment, BASELINE_REPLICAS, timeout=90)
        record_restoration(
            dep.get("spec", {}).get("replicas") == BASELINE_REPLICAS
            and dep.get("status", {}).get("readyReplicas") == BASELINE_REPLICAS,
            f"{component}: {deployment} restored to desired=={BASELINE_REPLICAS} Ready=={BASELINE_REPLICAS}",
        )
    except TimeoutError as exc:
        record_restoration(False, f"{component}: {deployment} did not return to {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Ready: {exc}")
        return False

    try:
        _wait_pods_ready(label_selector, BASELINE_REPLICAS, timeout=60, description=f"{component} restored Pods Ready")
        record_restoration(True, f"{component}: {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Pods Ready after restore")
    except TimeoutError as exc:
        record_restoration(False, f"{component}: Pods did not return to {BASELINE_REPLICAS}/{BASELINE_REPLICAS} Ready: {exc}")
        return False

    try:
        _wait_endpointslice_count(service, BASELINE_REPLICAS, timeout=60)
        record_restoration(True, f"{component}: EndpointSlice returned to {BASELINE_REPLICAS} ready endpoint(s)")
    except TimeoutError as exc:
        record_restoration(False, f"{component}: EndpointSlice did not return to {BASELINE_REPLICAS}: {exc}")
        return False

    return True


def main() -> int:
    print(f"# Real Day 3 scaling validation (3 -> 4 -> 3) against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    run_scaling_experiment("gateway", "maops-gateway", GATEWAY_SERVICE, GATEWAY_LABEL_SELECTOR, check_gateway_service_functional)
    run_scaling_experiment("app", "maops-app", APP_SERVICE, APP_LABEL_SELECTOR, check_app_reachable_via_gateway)

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]

    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} scaling checks passed")
    if restoration_failures:
        print()
        print("!!! RESTORATION FAILURE - a workload may not be back at 3/3 Ready - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} scaling check(s) failed", file=sys.stderr)
        return 1
    print("PASS: scaling behavior proven (3 -> 4 -> 3) for both workloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

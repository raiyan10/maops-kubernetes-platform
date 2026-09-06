#!/usr/bin/env python3
"""
Real Day 3 rolling-update + rollback validation.

For BOTH maops-gateway and maops-app, one workload at a time:

  1. Baseline: 3/3 Ready. Capture baseline Pod UIDs and the current
     ReplicaSet identities owned by the Deployment.
  2. Trigger a REAL new Deployment revision by patching a harmless,
     non-secret, uniquely-marked annotation under
     `spec.template.metadata.annotations` (key `maops.io/rollout-test`)
     - never a fake image tag. This changes the Pod template, so
     Kubernetes creates a new ReplicaSet and performs a genuine rolling
     update.
  3. Prove: a new ReplicaSet identity appears, Pods are actually
     replaced (new UIDs), the rollout reports successful completion
     (`kubectl rollout status`), and the final state is 3/3 Ready.
  4. While the rollout is in progress, sample the Service with bounded
     polling and record exactly what was observed (never claim
     continuous zero downtime without having actually sampled).
  5. Perform a REAL rollback via `kubectl rollout undo`, and prove: the
     command succeeds, the Deployment returns to Available and 3/3
     Ready, the temporary annotation is gone, the workload received a
     new replacement set of Pods, the Service remains functional, and
     the EndpointSlice is back to 3 ready endpoints.

Uses try/finally: if the rollout/rollback experiment raises or only
partially succeeds, rollback is still attempted, and a restoration
failure is reported prominently and separately from ordinary assertion
failures.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
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

EXPECTED_REPLICAS = 3
ANNOTATION_KEY = "maops.io/rollout-test"
ROLLOUT_STATUS_TIMEOUT_SECONDS = 180
# DAY3-INT-H2: the subprocess-level timeout for `kubectl rollout status
# --timeout=<n>s` must always exceed the Kubernetes-side timeout it was
# given, so a legitimately slow-but-successful 180s rollout is never
# killed early by a generic default.
ROLLOUT_STATUS_SUBPROCESS_TIMEOUT_SECONDS = kube.subprocess_timeout_for(ROLLOUT_STATUS_TIMEOUT_SECONDS)
SERVICE_SAMPLE_INTERVAL_SECONDS = 1.0
# Upper bound on how long the in-flight sampler thread is allowed to run
# even if something goes wrong joining it - always well above a real
# rollout's own bound so it never clips a legitimate in-progress rollout.
SERVICE_SAMPLER_JOIN_TIMEOUT_SECONDS = 10.0

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


def get_pods(label_selector: str) -> list[dict]:
    return get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]


def pod_uids(pods: list[dict]) -> set[str]:
    return {p["metadata"]["uid"] for p in pods}


def replicaset_uids(deployment: str) -> set[str]:
    rss = get_json("-n", NAMESPACE, "get", "replicasets", "-l", f"app.kubernetes.io/component={_component_of(deployment)}")[
        "items"
    ]
    owned = [
        rs
        for rs in rss
        if any(o.get("kind") == "Deployment" and o.get("name") == deployment for o in rs.get("metadata", {}).get("ownerReferences", []) or [])
    ]
    return {rs["metadata"]["uid"] for rs in owned}


def _component_of(deployment: str) -> str:
    return "gateway" if deployment == "maops-gateway" else "app"


def _deployment_ready(name: str) -> dict | None:
    dep = get_json("-n", NAMESPACE, "get", "deployment", name)
    if dep.get("status", {}).get("readyReplicas") == EXPECTED_REPLICAS and dep.get("spec", {}).get(
        "replicas"
    ) == EXPECTED_REPLICAS:
        return dep
    return None


def _wait_ready(name: str, timeout: float) -> dict:
    return wait_until(lambda: _deployment_ready(name), timeout=timeout, interval=2, description=f"deployment/{name} {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready")


def _wait_exact_pod_count(label_selector: str, count: int, timeout: float) -> set[str]:
    """Termination-race guard: `kubectl rollout status` reporting success
    and `status.readyReplicas` reaching the target count both race ahead
    of the OLD ReplicaSet's outgoing Pods actually being deleted - a
    naive one-shot `get_pods()` immediately afterward can still observe
    old-plus-new Pods together (more than `count`). Poll until the
    live Pod set settles to exactly `count` before trusting it as the
    post-rollout/post-rollback snapshot for UID-set comparison."""

    def predicate():
        pods = get_pods(label_selector)
        uids = pod_uids(pods)
        return uids if len(uids) == count else None

    return wait_until(predicate, timeout=timeout, interval=2, description=f"exactly {count} Pod(s) matching {label_selector!r}")


def _wait_endpointslice_count(service: str, count: int, timeout: float) -> int:
    def predicate():
        slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}")[
            "items"
        ]
        ready = count_ready_endpoints(slices)
        return ready if ready == count else None

    return wait_until(predicate, timeout=timeout, interval=2, description=f"{service} EndpointSlice ready endpoints == {count}")


def _pods_were_replaced(post_uids: set[str], baseline_uids: set[str], expected_count: int) -> bool:
    """DAY3-TEST-M2: the real "Pods were actually replaced" predicate,
    extracted so it can be exercised directly by unit tests (rather than
    tests merely re-testing Python's built-in `set.isdisjoint`) and reused
    verbatim by the production assertion below. A genuine replacement
    requires BOTH exactly the expected number of live Pods AND that none
    of them share an identity with the pre-rollout baseline set."""
    return len(post_uids) == expected_count and post_uids.isdisjoint(baseline_uids)


def wait_rollout_status(deployment: str, timeout_seconds: int) -> tuple[bool, str]:
    """DAY3-INT-H2: bounded at the subprocess level by a timeout that
    always exceeds `timeout_seconds` (the Kubernetes-side
    `--timeout=<n>s`), so kubectl's own timeout always has the chance to
    fire first. A subprocess timeout (a hung `kubectl` that never even
    reaches its own `--timeout`) is converted into an ordinary
    (False, detail) result here rather than propagating as an uncaught
    exception - callers already treat this function's return value as an
    ordinary pass/fail."""
    try:
        result = run(
            "-n",
            NAMESPACE,
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={timeout_seconds}s",
            check=False,
            timeout=kube.subprocess_timeout_for(timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"kubectl rollout status subprocess timed out: {_stderr_detail(exc)}"
    ok = result.returncode == 0
    output = (result.stdout or "").strip() + (("\n" + result.stderr.strip()) if result.stderr else "")
    return ok, output.strip()


def patch_rollout_annotation(deployment: str, marker: str) -> None:
    patch = {"spec": {"template": {"metadata": {"annotations": {ANNOTATION_KEY: marker}}}}}
    run("-n", NAMESPACE, "patch", "deployment", deployment, "--type=merge", "-p", json.dumps(patch))


def rollout_undo(deployment: str) -> tuple[bool, str]:
    result = run("-n", NAMESPACE, "rollout", "undo", f"deployment/{deployment}", check=False)
    ok = result.returncode == 0
    detail = (result.stdout or "").strip() or (result.stderr or "").strip()
    return ok, detail


def get_annotation(deployment: str) -> str | None:
    dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
    annotations = dep.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}
    return annotations.get(ANNOTATION_KEY)


def get_image(deployment: str) -> str | None:
    dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
    containers = dep.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
    return containers[0].get("image") if containers else None


class InFlightServiceSampler:
    """DAY3-ARCH-M1 / DAY3-INT-M1: samples the Service with bounded,
    single-worker (never more than one sample in flight at a time)
    polling in a background thread, starting at/just after the rollout
    mutation and running until `stop()` is called (by the caller, right
    after `kubectl rollout status` reports completion) - not a fixed
    window measured from some arbitrary later point. This is what makes
    the sampling genuinely concurrent with the real in-progress rollout,
    rather than merely post-rollout.

    Also tracks whether at least one successful sample happened while the
    live Pod set (by UID) had already diverged from the pre-rollout
    baseline - i.e. a sample proven to have landed during the actual
    transition window, not just sometime during the overall bounded
    duration. If the real rollout completes before any such divergent
    sample is observed, the caller must report that as inconclusive
    rather than manufacturing a PASS (a fast rollout is not a defect, but
    an unobserved claim is not proof either).

    The thread is always daemonic and `stop()` always joins it with a
    bound, so no background sampler is ever leaked past the end of the
    experiment even if something upstream raises."""

    def __init__(self, functional_check, get_current_uids, baseline_uids: set[str], interval: float):
        self._functional_check = functional_check
        self._get_current_uids = get_current_uids
        self._baseline_uids = baseline_uids
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.successes = 0
        self.total = 0
        self.diverged_sample_confirmed = False

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.total += 1
            try:
                ok = bool(self._functional_check())
            except Exception:  # noqa: BLE001 - a failed sample counts as a failed sample, not a crash
                ok = False
            if ok:
                self.successes += 1
                if not self.diverged_sample_confirmed:
                    try:
                        current_uids = self._get_current_uids()
                        if current_uids - self._baseline_uids:
                            self.diverged_sample_confirmed = True
                    except Exception:  # noqa: BLE001 - divergence bookkeeping must never crash the sampler
                        pass
            self._stop_event.wait(self._interval)

    def stop(self, join_timeout: float = SERVICE_SAMPLER_JOIN_TIMEOUT_SECONDS) -> tuple[int, int]:
        self._stop_event.set()
        self._thread.join(timeout=join_timeout)
        return self.successes, self.total


def check_gateway_functional_once() -> bool:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok, _detail = check_endpoint(local_port, "/livez", role="gateway")
            return ok
    except (TimeoutError, RuntimeError):
        return False


def check_app_via_gateway_functional_once() -> bool:
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok, _detail = check_endpoint(local_port, "/backend", role="gateway")
            return ok
    except (TimeoutError, RuntimeError):
        return False


def run_rollout_experiment(component: str, deployment: str, service: str, label_selector: str, functional_check) -> None:
    print(f"## Rolling-update experiment: {component} ({deployment})")

    try:
        _wait_ready(deployment, timeout=60)
        record(True, f"{component} baseline: {deployment} {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready")
    except TimeoutError as exc:
        record(False, f"{component} baseline: {exc}")
        return

    baseline_pods = get_pods(label_selector)
    baseline_uids = pod_uids(baseline_pods)
    baseline_rs_uids = replicaset_uids(deployment)
    baseline_image = get_image(deployment)
    record(
        len(baseline_pods) == EXPECTED_REPLICAS,
        f"{component} baseline: captured {len(baseline_pods)} Pod UID(s) and {len(baseline_rs_uids)} ReplicaSet identity(ies)",
    )

    marker = f"day3-{uuid.uuid4().hex[:12]}"
    patched = False
    sampler: InFlightServiceSampler | None = None
    try:
        patch_rollout_annotation(deployment, marker)
        patched = True

        # DAY3-ARCH-M1 / DAY3-INT-M1: start sampling the Service AT/JUST
        # AFTER the mutation that triggers the real rollout - not after
        # rollout status has already reported completion - so the
        # sampling window genuinely overlaps the in-progress replacement,
        # not merely the post-rollout state. Bounded to one worker thread
        # (single-flight polling), guaranteed stopped below regardless of
        # what happens in between.
        sampler = InFlightServiceSampler(
            functional_check,
            lambda: pod_uids(get_pods(label_selector)),
            baseline_uids,
            SERVICE_SAMPLE_INTERVAL_SECONDS,
        )
        sampler.start()

        try:
            new_rs_uids = wait_until(
                lambda: (replicaset_uids(deployment) - baseline_rs_uids) or None,
                timeout=60,
                interval=2,
                description=f"{component} new ReplicaSet created",
            )
            record(True, f"{component}: new ReplicaSet identity(ies) appeared: {new_rs_uids}")
        except TimeoutError as exc:
            record(False, f"{component} new ReplicaSet: {exc}")

        rollout_ok, rollout_output = wait_rollout_status(deployment, ROLLOUT_STATUS_TIMEOUT_SECONDS)
        record(rollout_ok, f"{component}: kubectl rollout status reported success: {rollout_output!r}")

        # Rollout status reporting completion is "rollout completion" -
        # the in-flight sampling window ends here, deterministically.
        successes, total = sampler.stop()
        diverged_confirmed = sampler.diverged_sample_confirmed
        sampler = None

        try:
            _wait_ready(deployment, timeout=60)
            record(True, f"{component} post-rollout: {deployment} {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready")
        except TimeoutError as exc:
            record(False, f"{component} post-rollout readiness: {exc}")

        try:
            post_rollout_uids = _wait_exact_pod_count(label_selector, EXPECTED_REPLICAS, timeout=60)
        except TimeoutError as exc:
            record(False, f"{component} post-rollout Pod count: {exc}")
            post_rollout_uids = pod_uids(get_pods(label_selector))
        record(
            _pods_were_replaced(post_rollout_uids, baseline_uids, EXPECTED_REPLICAS),
            f"{component}: Pods were actually replaced (baseline UIDs {baseline_uids} -> new UIDs {post_rollout_uids})",
        )

        try:
            _wait_endpointslice_count(service, EXPECTED_REPLICAS, timeout=60)
            record(True, f"{component} post-rollout: EndpointSlice has {EXPECTED_REPLICAS} ready endpoint(s)")
        except TimeoutError as exc:
            record(False, f"{component} post-rollout EndpointSlice: {exc}")

        # This records only the ACTUAL observed sample counts - never a
        # claim of continuous/mathematical zero downtime.
        record(
            total > 0 and successes >= 1,
            f"{component}: Service sampled {successes}/{total} times successfully in-flight "
            f"(from the rollout mutation to rollout-status completion, bounded polling at "
            f"{SERVICE_SAMPLE_INTERVAL_SECONDS}s intervals) - this records actual observed availability, "
            "not an assumption of zero downtime",
        )
        # The stronger claim - that a successful sample was taken while the
        # live Pod set had actually diverged from baseline - is reported
        # separately and is never silently promoted to a PASS: if the real
        # rollout completed too fast for any divergent sample to land, this
        # is inconclusive, not a manufactured green result.
        if diverged_confirmed:
            record(
                True,
                f"{component}: at least one successful Service sample was confirmed while the live Pod set had "
                "already diverged from the pre-rollout baseline (genuine in-flight proof, not merely post-rollout)",
            )
        else:
            print(
                f"[INCONCLUSIVE] {component}: rollout completed before any successful Service sample could be "
                f"confirmed against a diverged live Pod set ({successes}/{total} samples taken overall) - the "
                "stronger in-flight-divergence proof is inconclusive, not claimed as a PASS"
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"{component} rollout experiment: kubectl command failed/timed out: {_stderr_detail(exc)}")
    except Exception as exc:  # noqa: BLE001 - controlled record, rollback below still always attempted
        record(False, f"{component} rollout experiment raised an unexpected error: {exc}")
    finally:
        if sampler is not None:
            # Only reached if something above raised before the normal
            # sampler.stop() call - still guarantee the thread is joined
            # and never leaked.
            sampler.stop()
        if patched:
            rollback_workload(component, deployment, service, label_selector, baseline_image)


def rollback_workload(component: str, deployment: str, service: str, label_selector: str, expected_image: str | None) -> bool:
    """DAY3-INT-H2: this whole function is a restoration path, called from
    a `finally` block - a `subprocess.CalledProcessError` or
    `subprocess.TimeoutExpired` escaping ANY step here (rollout_undo,
    get_annotation, get_image, or any get_pods() call, all of which
    eventually call the bounded kube.run()) must never propagate out of a
    `finally` block uncaught (which would mask the real experiment result
    and crash the whole script) - it must become an ordinary, visible
    RESTORATION FAILURE record instead."""
    # Every step below folds into `restoration_ok` - a step recorded as a
    # failure part-way through must never be silently overwritten by a
    # later step's success (DAY3-TEST-H1). Only the two steps whose
    # failure means every following signal would be meaningless (undo
    # itself, and the Deployment/Pod-count convergence waits) short-
    # circuit with an early `return False`; every other step still runs
    # and still contributes to the final verdict.
    restoration_ok = True
    try:
        pre_rollback_uids = pod_uids(get_pods(label_selector))

        undo_ok, undo_detail = rollout_undo(deployment)
        record_restoration(undo_ok, f"{component}: kubectl rollout undo deployment/{deployment} -> {undo_detail!r}")
        if not undo_ok:
            return False

        rollout_ok, rollout_output = wait_rollout_status(deployment, ROLLOUT_STATUS_TIMEOUT_SECONDS)
        restoration_ok = record_restoration(rollout_ok, f"{component}: rollback rollout status reported success: {rollout_output!r}") and restoration_ok

        try:
            dep = _wait_ready(deployment, timeout=90)
            record_restoration(True, f"{component}: {deployment} back to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready after rollback")
        except TimeoutError as exc:
            record_restoration(False, f"{component}: {deployment} did not return to {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS} Ready: {exc}")
            return False

        available = any(
            c.get("type") == "Available" and c.get("status") == "True" for c in dep.get("status", {}).get("conditions", [])
        )
        restoration_ok = record_restoration(available, f"{component}: Deployment Available condition True after rollback") and restoration_ok

        annotation = get_annotation(deployment)
        restoration_ok = record_restoration(
            annotation is None, f"{component}: temporary rollout-test annotation absent after rollback (found {annotation!r})"
        ) and restoration_ok

        image = get_image(deployment)
        restoration_ok = record_restoration(
            expected_image is None or image == expected_image,
            f"{component}: live image after rollback == {image!r} (matches pre-experiment baseline {expected_image!r})",
        ) and restoration_ok

        try:
            post_rollback_uids = _wait_exact_pod_count(label_selector, EXPECTED_REPLICAS, timeout=60)
            record_restoration(True, f"{component}: exactly {EXPECTED_REPLICAS} Pods present after rollback")
        except TimeoutError as exc:
            record_restoration(False, f"{component}: Pod count did not settle to {EXPECTED_REPLICAS} after rollback: {exc}")
            return False
        restoration_ok = record_restoration(
            _pods_were_replaced(post_rollback_uids, pre_rollback_uids, EXPECTED_REPLICAS) or not pre_rollback_uids,
            f"{component}: rollback produced a replacement set of Pods (pre-rollback UIDs {pre_rollback_uids} -> "
            f"post-rollback UIDs {post_rollback_uids})",
        ) and restoration_ok

        try:
            _wait_endpointslice_count(service, EXPECTED_REPLICAS, timeout=60)
            record_restoration(True, f"{component}: EndpointSlice back to {EXPECTED_REPLICAS} ready endpoint(s) after rollback")
        except TimeoutError as exc:
            record_restoration(False, f"{component}: EndpointSlice did not return to {EXPECTED_REPLICAS} after rollback: {exc}")
            return False

        try:
            with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
                path = "/backend" if component == "app" else "/livez"
                ok, detail = check_endpoint(local_port, path, role="gateway")
        except (TimeoutError, RuntimeError) as exc:
            record_restoration(False, f"{component}: post-rollback Service functional check failed: {exc}")
            return False
        restoration_ok = record_restoration(ok, f"{component}: post-rollback Service functional check: {detail}") and restoration_ok

        return restoration_ok
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record_restoration(False, f"{component}: rollback command failed/timed out: {_stderr_detail(exc)}")
        return False


def main() -> int:
    print(f"# Real Day 3 rolling-update + rollback validation against context {kube.CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    run_rollout_experiment("gateway", "maops-gateway", GATEWAY_SERVICE, GATEWAY_LABEL_SELECTOR, check_gateway_functional_once)
    run_rollout_experiment("app", "maops-app", APP_SERVICE, APP_LABEL_SELECTOR, check_app_via_gateway_functional_once)

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]

    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} rollout/rollback checks passed")
    if restoration_failures:
        print()
        print("!!! ROLLBACK/RESTORATION FAILURE - a workload may not be back at 3/3 Ready with the annotation removed - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} rollout/rollback check(s) failed", file=sys.stderr)
        return 1
    print("PASS: rolling update and rollback behavior proven for both workloads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

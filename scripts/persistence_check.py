#!/usr/bin/env python3
"""
DAY4: proves data survives maops-state-0 Pod deletion/rescheduling.

1. Captures the original persisted record and the Pod/PVC/PV names and
   UIDs.
2. Writes then reads back a unique, non-secret marker through the real
   service chain: gateway /state -> app /internal/state -> state /state
   (via a bounded port-forward to service/maops-gateway - never a
   direct hostPath/PVC inspection, which would prove nothing about the
   actual application path).
3. Deletes ONLY the maops-state-0 Pod with a normal `kubectl delete pod`
   (never --force/--grace-period=0) and waits (bounded) for the
   StatefulSet controller to create its real replacement.
4. Proves: same Pod name, a genuinely DIFFERENT Pod UID, the SAME PVC
   UID and PV UID/binding (nothing was recreated), and the marker
   written in step 2 reads back unchanged through the same service
   chain - never by re-reading the marker from a variable already held
   in this script's memory (which would prove nothing about real
   persistence).

If the baseline (pre-experiment) record cannot be captured with
certainty before any mutation (marker write, Pod delete), the script
aborts immediately rather than risk restoring `null`/an invented value
over a real one.

Restores the pre-experiment record on every handled exit path
(guaranteed `finally`), then independently re-`GET`s and compares it -
a successful restoring `PUT` response alone is never treated as proof.
A restoration failure is a distinct, prominent finding, never hidden
behind the experiment's own pass/fail result. Bounded retries/timeouts
throughout; `try/finally` cannot recover from SIGKILL or host power
loss - this is a best-effort guarantee against normal exceptions and
`sys.exit`, not an unconditional promise.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import portforward
from http_checks import is_nonempty_identity, validate_state_value
from kube import CONTEXT, GATEWAY_SERVICE, NAMESPACE, get_json, run, wait_until

POD_NAME = "maops-state-0"
PVC_NAME = "data-maops-state-0"
POD_DELETE_TIMEOUT_SECONDS = 90.0
# DAY4-INT bug found live: maops-state's terminationGracePeriodSeconds
# is 30s, exactly equal to kube.DEFAULT_TIMEOUT_SECONDS - a normal
# `kubectl delete pod` (no --force) blocks client-side until the object
# is actually gone, which is itself gated by the grace period, so the
# default timeout has zero margin and raced/lost live (observed ~31s
# actual vs. a 30.0s subprocess timeout). This call needs its own
# explicit, comfortably-larger bound, and - per kube.run()'s own
# documented contract - must be caught, never left to propagate
# uncaught and abort the whole experiment before its finally-block
# restoration logic even runs correctly.
POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS = 90.0
RESTORE_READY_TIMEOUT_SECONDS = 90.0
RESTORE_PUT_RETRIES = 5
RESTORE_PUT_RETRY_INTERVAL_SECONDS = 3.0

# DAY4 batch 2b: socket connect+read timeout for ONE HTTP call this
# client makes to service/maops-gateway - not a total request deadline
# over any retries. Must exceed gateway/server.py's own
# BACKEND_TIMEOUT_SECONDS (5s) with real margin: gateway's own
# worst-case time to respond (including its own controlled 503 once
# ITS call to maops-app times out) is bounded by BACKEND_TIMEOUT_SECONDS,
# so a client timeout equal to (or smaller than) that value races the
# two timers and cannot reliably observe gateway's own classified
# response.
_GATEWAY_BACKEND_TIMEOUT_SECONDS = 5.0
CLIENT_HTTP_TIMEOUT_SECONDS = _GATEWAY_BACKEND_TIMEOUT_SECONDS + 5.0

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


class BaselineCaptureError(Exception):
    """Raised whenever the pre-experiment record could not be captured
    with certainty - a non-200 response, a malformed/missing 'value'
    key, an invalid value type, or any transport exception. The caller
    must treat this as fatal and abort BEFORE any mutation (marker
    write, Pod delete) is attempted - a failed capture must never be
    confused with a genuinely captured `{"value": null}` baseline."""


def _http(local_port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{local_port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=CLIENT_HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_state(local_port: int) -> tuple[int, dict]:
    return _http(local_port, "GET", "/state")


def _put_state(local_port: int, value: str | None) -> tuple[int, dict]:
    return _http(local_port, "PUT", "/state", {"value": value})


def _pod_identity() -> dict | None:
    result = run("-n", NAMESPACE, "get", "pod", POD_NAME, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _capture_baseline(local_port: int) -> str | None:
    """Returns the captured `value` (str or None, i.e. a genuine
    `{"value": null}` record) - or raises BaselineCaptureError on ANY
    condition that means the true pre-experiment record was not
    reliably captured: non-200 status, a body that isn't a dict, a
    missing 'value' key, or a value of the wrong type. json.JSONDecodeError
    (malformed JSON) is a ValueError subclass and propagates through
    unchanged; the caller catches it alongside this exception."""
    status, body = _get_state(local_port)
    if status != 200:
        raise BaselineCaptureError(f"baseline GET /state returned HTTP {status} (expected 200)")
    ok, value, err = validate_state_value(body)
    if not ok:
        raise BaselineCaptureError(f"baseline GET /state {err}")
    return value


def _pvc_pv_identity() -> tuple[str | None, str | None, str | None]:
    pvc_result = run("-n", NAMESPACE, "get", "pvc", PVC_NAME, "-o", "json", check=False)
    if pvc_result.returncode != 0:
        return None, None, None
    pvc = json.loads(pvc_result.stdout)
    pvc_uid = pvc.get("metadata", {}).get("uid")
    volume_name = pvc.get("spec", {}).get("volumeName")
    pv_uid = None
    if volume_name:
        pv_result = run("get", "pv", volume_name, "-o", "json", check=False)
        if pv_result.returncode == 0:
            pv_uid = json.loads(pv_result.stdout).get("metadata", {}).get("uid")
    return pvc_uid, volume_name, pv_uid


def main() -> int:
    print(f"# Day 4 persistence proof (Pod deletion/rescheduling) against context {CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    original_pod = _pod_identity()
    if original_pod is None:
        print(f"FAIL: {POD_NAME} does not exist - run `make deploy` first", file=sys.stderr)
        return 1
    original_pod_uid = original_pod.get("metadata", {}).get("uid")
    original_pvc_uid, volume_name, original_pv_uid = _pvc_pv_identity()
    identity_ok = is_nonempty_identity(original_pod_uid) and is_nonempty_identity(original_pvc_uid) and is_nonempty_identity(original_pv_uid)
    record(identity_ok, f"baseline identity captured: pod_uid={original_pod_uid} pvc_uid={original_pvc_uid} pv_uid={original_pv_uid}")
    if not identity_ok:
        # DAY4 batch 2b: a missing/empty identity means nothing
        # downstream (the "same PVC/PV, new Pod UID" proof) can be
        # trusted - abort before any mutation, exactly like a failed
        # baseline VALUE capture below.
        print("FAIL: baseline structural/identity preconditions not met - aborting before any mutation", file=sys.stderr)
        return 1

    original_value = None
    baseline_captured = False
    restore_needed = False
    try:
        # Baseline capture MUST succeed, with certainty, before any
        # mutation below (marker write, Pod delete) is ever attempted -
        # DAY4-REL-4 / the latent bug found reading this file during
        # remediation batch 1: the previous version proceeded to mutate
        # even when this GET failed, silently restoring `None` over
        # whatever real value existed. A transport exception (including
        # the port-forward itself failing to become connectable) is
        # exactly as fatal here as a non-200/malformed response.
        try:
            with portforward.port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as port:
                original_value = _capture_baseline(port)
            baseline_captured = True
            record(
                True,
                "baseline GET /state through gateway -> app -> state chain captured for restoration "
                f"(value={'<non-null>' if original_value is not None else None!r})",
            )
        except (BaselineCaptureError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
            record(False, f"baseline GET /state capture failed - aborting before any mutation: {exc}")
            return 1

        marker = f"day4-persistence-{uuid.uuid4().hex[:16]}"
        with portforward.port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as port:
            # DAY4 batch 2b: `restore_needed` is set to True IMMEDIATELY
            # BEFORE attempting the mutating PUT - not after it returns,
            # and not only from inside its except/else branches. The
            # previous placement meant any exception type NOT in the
            # caught tuple below (including KeyboardInterrupt, which is
            # never caught by any `except (...)` clause here) would
            # propagate with `restore_needed` still False, skipping
            # restoration even though the PUT may have already
            # committed server-side.
            restore_needed = True
            try:
                status, _ = _put_state(port, marker)
            except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
                # A PUT was attempted - it may have persisted on the
                # server despite this client-side exception (timeout
                # after the write already committed). Restoration must
                # still run; failure here must never be read as "nothing
                # changed".
                record(False, f"PUT /state marker through service chain raised {exc!r}")
            else:
                record(status == 200, f"PUT /state marker through service chain: HTTP {status}")
            status, body = _get_state(port)
            ok, value, err = validate_state_value(body)
            record(
                status == 200 and ok and value == marker,
                f"GET /state readback through service chain matches written marker: HTTP {status}, value={value!r} (expected {marker!r})"
                + (f", schema error: {err}" if not ok else ""),
            )

        try:
            # --wait=false (same pattern as scripts/reconcile_check.py's
            # own pod deletion): a normal, graceful deletion (still
            # respects terminationGracePeriodSeconds server-side, never
            # --force/--grace-period=0) whose CLIENT call returns as soon
            # as the deletion is accepted, rather than blocking until the
            # object is actually gone. This is what actually fixes the
            # bug found live: `kubectl delete pod` without --wait=false
            # blocks client-side for up to terminationGracePeriodSeconds
            # (30s on maops-state) - exactly equal to
            # kube.DEFAULT_TIMEOUT_SECONDS, with zero margin, and it
            # raced and lost once already (~31s actual vs. a 30.0s
            # subprocess timeout). The separate, already-bounded
            # wait_until() below still does the real polling for the
            # replacement Pod becoming Ready - --wait=false does not
            # weaken that proof, it only changes which call does the
            # waiting. The explicit longer subprocess timeout is kept as
            # defense in depth in case the delete acceptance itself is
            # slow, and the call is still caught, never left to
            # propagate uncaught (kube.run()'s own documented contract
            # for mutation callers).
            delete_result = run(
                "-n", NAMESPACE, "delete", "pod", POD_NAME, "--wait=false",
                check=False, timeout=POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS,
            )
            record(delete_result.returncode == 0, f"kubectl delete pod {POD_NAME} --wait=false (normal deletion, no --force) exit code {delete_result.returncode}")
        except subprocess.TimeoutExpired as exc:
            record(False, f"kubectl delete pod {POD_NAME} did not even confirm acceptance within {POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS}s: {exc}")

        def _replaced():
            pod = _pod_identity()
            if pod is None:
                return None
            new_uid = pod.get("metadata", {}).get("uid")
            conditions = pod.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if new_uid != original_pod_uid and ready:
                return pod
            return None

        try:
            new_pod = wait_until(_replaced, timeout=POD_DELETE_TIMEOUT_SECONDS, description=f"{POD_NAME} replaced and Ready")
            new_pod_uid = new_pod.get("metadata", {}).get("uid")
            record(True, f"{POD_NAME} replaced: new UID {new_pod_uid} (was {original_pod_uid}), Ready")
        except TimeoutError as exc:
            record(False, str(exc))
            new_pod_uid = None

        final_pvc_uid, final_volume_name, final_pv_uid = _pvc_pv_identity()
        record(is_nonempty_identity(final_pvc_uid) and final_pvc_uid == original_pvc_uid, f"PVC {PVC_NAME} UID unchanged: {final_pvc_uid} == {original_pvc_uid}")
        record(is_nonempty_identity(final_volume_name) and final_volume_name == volume_name, f"PVC {PVC_NAME} still bound to the same PV: {final_volume_name} == {volume_name}")
        record(is_nonempty_identity(final_pv_uid) and final_pv_uid == original_pv_uid, f"PV UID unchanged: {final_pv_uid} == {original_pv_uid}")

        # DAY4-INT bug found live: the state Pod's own Ready condition
        # (what _replaced() above waits for) is necessarily satisfied
        # BEFORE the three app replicas' own independent, periodically-
        # polled backend-health checks have re-converged against the
        # new state Pod's IP - a single immediate readback attempt here
        # observed a real, transient HTTP 503 live (all app/gateway
        # replicas briefly reported "state unavailable"/Unhealthy, then
        # settled within ~2 minutes with no data loss). Retry bounded,
        # same pattern as the finally-block restoration fix, rather than
        # asserting on a single immediate attempt.
        with portforward.port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as port:
            status, body, value_ok, value, err = None, {}, False, None, ""
            for attempt in range(RESTORE_PUT_RETRIES):
                status, body = _get_state(port)
                value_ok, value, err = validate_state_value(body)
                if status == 200 and value_ok and value == marker:
                    break
                time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
            record(
                status == 200 and value_ok and value == marker,
                f"post-recreation GET /state through service chain still returns the marker written before deletion: "
                f"HTTP {status}, value={value!r} (expected {marker!r})" + (f", schema error: {err}" if not value_ok else ""),
            )
    finally:
        # Never restore anything unless the original value was actually,
        # verifiably captured above - restoring `None`/an invented value
        # over a real one is exactly the defect this batch fixes.
        if restore_needed and baseline_captured:
            try:
                with portforward.port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as port:
                    # DAY4-INT bug found live: a Pod deletion/rescheduling
                    # experiment can genuinely still be mid-restart at
                    # this point (the replacement Pod may not be Ready
                    # yet) - a single immediate restore attempt observed
                    # a real HTTP 503 here once, live, leaving the actual
                    # persisted record stuck at the test marker instead
                    # of being silently retried. Wait (bounded) for the
                    # chain to report ready again, then retry the PUT
                    # itself a bounded number of times before giving up -
                    # both within the SAME port-forward, not a fresh one
                    # per attempt.
                    try:
                        wait_until(
                            lambda: True if _http(port, "GET", "/readyz")[0] == 200 else None,
                            timeout=RESTORE_READY_TIMEOUT_SECONDS,
                            interval=3,
                            description="service chain ready for restoration",
                        )
                    except TimeoutError as exc:
                        record_restoration(False, f"service chain never became ready within {RESTORE_READY_TIMEOUT_SECONDS}s before restore attempt: {exc}")

                    put_status = None
                    for _attempt in range(RESTORE_PUT_RETRIES):
                        try:
                            put_status, _ = _put_state(port, original_value)
                        except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
                            put_status = None
                            record_restoration(False, f"restoring PUT /state raised {exc!r} (attempt may still have persisted server-side)")
                        else:
                            if put_status == 200:
                                break
                        time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
                    record_restoration(put_status == 200, f"restoring PUT /state through service chain returned HTTP {put_status} after up to {RESTORE_PUT_RETRIES} attempt(s)")

                    # A successful PUT response alone cannot prove
                    # restoration (DAY4-REL-4) - independently GET and
                    # compare, with the same bounded retry pattern (the
                    # chain may still be briefly re-converging even after
                    # the PUT itself succeeded).
                    get_status, get_body, value_ok, value, err = None, {}, False, None, ""
                    for _attempt in range(RESTORE_PUT_RETRIES):
                        try:
                            get_status, get_body = _get_state(port)
                        except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
                            get_status, get_body, value_ok, value, err = None, {}, False, None, repr(exc)
                        else:
                            value_ok, value, err = validate_state_value(get_body)
                            if get_status == 200 and value_ok and value == original_value:
                                break
                        time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
                    record_restoration(
                        get_status == 200 and value_ok and value == original_value,
                        f"independent GET /state after restoration matches captured original value: HTTP {get_status}, "
                        f"value={value!r} (expected {original_value!r})" + (f", error: {err}" if not value_ok else ""),
                    )
            except Exception as exc:  # noqa: BLE001 - a restoration failure must be visible, never swallowed
                record_restoration(False, f"could not restore original record: {exc}")
        elif restore_needed and not baseline_captured:
            # Structurally unreachable (baseline-capture failure returns
            # before restore_needed can become True) - defensive
            # documentation of the invariant this branch protects, not
            # dead code covering a real path.
            record_restoration(False, "internal invariant violated: a mutation was attempted without a captured baseline - refusing to restore an unknown/invented value")

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]
    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} persistence checks passed")
    if restoration_failures:
        print()
        print("!!! RESTORATION FAILURE - the pre-experiment record may not be restored - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} persistence check(s) failed", file=sys.stderr)
        return 1
    print("PASS: persistence proof complete - data survived Pod deletion/rescheduling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

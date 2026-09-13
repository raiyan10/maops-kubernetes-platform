#!/usr/bin/env python3
"""
DAY4: proves PVC/PV retention and app/gateway degraded-but-live behavior
across a maops-state 1 -> 0 -> 1 cycle.

1. Scales maops-state to 0 and waits for maops-state-0 to be fully gone.
2. Proves the PVC (data-maops-state-0) and its bound PV remain Bound
   throughout - StatefulSet.spec.persistentVolumeClaimRetentionPolicy
   (whenScaled: Retain) means scaling to 0 must NEVER delete the claim.
3. Observes, via bounded DIRECT-POD port-forwards (never the Service,
   which stops routing to a not-Ready Pod - the same reason
   dependency_check.py forwards directly to a gateway Pod): app and
   gateway /livez stay HTTP 200 (local-process liveness never depends
   on state), and their /readyz becomes HTTP 503 (dependency-aware
   readiness correctly reflects the unavailable state backend).
4. Restores maops-state to 1 and waits for a genuinely NEW Pod identity
   to become Ready.
5. Proves full recovery in two independently-verified contracts, in
   this order:
   - Contract A: the marker this script itself wrote before the outage
     reads back unchanged through the real service chain.
   - Contract B: the TRUE pre-experiment record (captured via an
     authenticated baseline GET before any mutation, including before
     Contract A's own marker write) is restored via a PUT and then
     independently re-read to confirm - never trusted from the PUT's
     HTTP status alone.
   Contract A is always checked BEFORE Contract B's restoring PUT runs,
   since restoring first would make it impossible to ever observe
   whether the marker actually survived the cycle. app/gateway both
   return to normal 3/3 Ready Service-routed behavior.

If the baseline (pre-experiment) record cannot be captured with
certainty before any mutation, the script aborts immediately (no
marker write, no scale-to-0) rather than risk restoring `null`/an
invented value over a real one.

maops-state is ALWAYS restored to exactly 1 replica in a guaranteed
`finally` path. A restoration failure is reported prominently and
separately from the experiment's own result - never hidden. Bounded
retries/timeouts throughout; `try/finally` cannot recover from
SIGKILL or host power loss - this is a best-effort guarantee against
normal exceptions and `sys.exit`, not an unconditional promise.
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
from cluster_check import _stderr_detail, get_pods
from http_checks import check_endpoint, is_nonempty_identity, raw_get, validate_state_value
from kube import (
    APP_LABEL_SELECTOR,
    CONTEXT,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_SERVICE,
    NAMESPACE,
    STATE_STATEFULSET,
    run,
    wait_until,
)
from portforward import port_forward

POD_NAME = "maops-state-0"
PVC_NAME = "data-maops-state-0"
EXPECTED_APP_GATEWAY_REPLICAS = 3

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
    with certainty - see persistence_check.py's identical contract. The
    caller must abort BEFORE any mutation (marker write, scale to 0)."""


RESTORE_PUT_RETRIES = 5
RESTORE_PUT_RETRY_INTERVAL_SECONDS = 3.0

# DAY4 batch 2b: this is the socket connect+read timeout for ONE HTTP
# call this validation client makes to service/maops-gateway - not a
# total request deadline over any retries. It must exceed
# gateway/server.py's own BACKEND_TIMEOUT_SECONDS (5s) with real
# margin: gateway's own worst-case time to respond (including a
# controlled 503 "backend unavailable" once ITS call to maops-app times
# out) is bounded by BACKEND_TIMEOUT_SECONDS, so a client timeout equal
# to (or smaller than) that value races the two timers and cannot
# reliably observe gateway's own classified response - it may instead
# see its own socket timeout fire first. 5s + this margin comfortably
# separates the two.
_GATEWAY_BACKEND_TIMEOUT_SECONDS = 5.0
CLIENT_HTTP_TIMEOUT_SECONDS = _GATEWAY_BACKEND_TIMEOUT_SECONDS + 5.0


def _pvc_pv_snapshot() -> tuple[str | None, str | None, str | None, str | None]:
    pvc_result = run("-n", NAMESPACE, "get", "pvc", PVC_NAME, "-o", "json", check=False)
    if pvc_result.returncode != 0:
        return None, None, None, None
    pvc = json.loads(pvc_result.stdout)
    pvc_uid = pvc.get("metadata", {}).get("uid")
    phase = pvc.get("status", {}).get("phase")
    volume_name = pvc.get("spec", {}).get("volumeName")
    pv_phase = None
    if volume_name:
        pv_result = run("get", "pv", volume_name, "-o", "json", check=False)
        if pv_result.returncode == 0:
            pv_phase = json.loads(pv_result.stdout).get("status", {}).get("phase")
    return pvc_uid, phase, volume_name, pv_phase


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


def _capture_baseline(local_port: int) -> str | None:
    """Returns the captured `value` (str or None, i.e. a genuine
    `{"value": null}` record) - or raises BaselineCaptureError on any
    condition meaning the true pre-experiment record was not reliably
    captured. Mirrors persistence_check.py's identical contract - see
    that module for the full rationale."""
    status, body = _http(local_port, "GET", "/state")
    if status != 200:
        raise BaselineCaptureError(f"baseline GET /state returned HTTP {status} (expected 200)")
    ok, value, err = validate_state_value(body)
    if not ok:
        raise BaselineCaptureError(f"baseline GET /state {err}")
    return value


def scale_state(replicas: int) -> None:
    run("-n", NAMESPACE, "scale", f"statefulset/{STATE_STATEFULSET}", f"--replicas={replicas}")


def _state_pod_gone() -> bool | None:
    """Confirms GENUINE Pod absence only, via kubectl's own STRUCTURAL
    contract rather than substring-matching error text (DAY4 batch 2c):
    `--ignore-not-found -o json` exits 0 with EMPTY stdout ONLY for
    genuine absence (the NotFound error is suppressed); it exits
    NONZERO for every other failure - RBAC denial, an API server
    hiccup, a missing/broken credential-helper executable whose OWN
    error text might coincidentally contain "not found" (e.g. `exec:
    "some-credential-helper": executable file not found in $PATH`,
    which the previous substring check could misclassify as the Pod
    itself being gone). `wait_until()`'s own except-and-retry contract
    already treats any exception raised here (e.g.
    subprocess.TimeoutExpired from a hung kubectl call, or the
    RuntimeError below) as "not ready yet, keep polling" - this
    function itself is only responsible for distinguishing genuine
    absence from every other outcome, never for swallowing a hang."""
    result = run("-n", NAMESPACE, "get", "pod", POD_NAME, "--ignore-not-found", "-o", "json", check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # An unrelated failure - not proof of absence. Report distinctly
        # (visible in wait_until()'s timeout message's "last error" via
        # a raised exception, rather than being silently retried
        # indistinguishably from "still present") and let the bounded
        # poll continue.
        raise RuntimeError(f"could not confirm {POD_NAME} is gone - kubectl get pod failed: {stderr or result}")
    stdout = (result.stdout or "").strip()
    if stdout == "":
        return True
    return None  # present (or an unexpected non-empty shape) - not gone yet, keep polling


def _state_pod_ready() -> dict | None:
    """Returns the live Pod object once it reports Ready - regardless
    of whether its UID has changed. Recovery (being able to talk to
    SOME healthy Pod again) and the experiment's own claim that scale-
    to-0 genuinely replaced it are two separate, independently recorded
    assertions - see restore_state() below."""
    result = run("-n", NAMESPACE, "get", "pod", POD_NAME, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    pod = json.loads(result.stdout)
    conditions = pod.get("status", {}).get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    return pod if ready else None


def restore_state(original_pod_uid: str | None, marker: str, original_value: str | None, baseline_captured: bool) -> bool:
    try:
        scale_state(1)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record_restoration(False, f"could not scale {STATE_STATEFULSET} back to 1 replica: {_stderr_detail(exc)}")
        return False

    # Recovery gate: SOME healthy Ready Pod must exist again before
    # anything else below can be attempted - this is what genuinely
    # gates the rest of restoration. Whether that Pod's identity
    # actually changed is a SEPARATE assertion (below): if scale-down
    # was rejected or never actually replaced the original healthy Pod,
    # that is a real experiment failure (recorded, non-zero exit) - but
    # it must never prevent Contract A/B restoration, which is owed
    # regardless of whether the outage cycle "really" happened, since a
    # marker/mutation may already have been written against whatever
    # Pod is currently serving.
    try:
        pod = wait_until(_state_pod_ready, timeout=120, interval=3, description=f"{POD_NAME} Ready again after restore")
    except TimeoutError as exc:
        record_restoration(False, f"{POD_NAME} did not recover to Ready: {exc}")
        return False
    record_restoration(True, f"{STATE_STATEFULSET} restored to 1/1 Ready")

    new_pod_uid = pod.get("metadata", {}).get("uid")
    record(
        is_nonempty_identity(new_pod_uid) and new_pod_uid != original_pod_uid,
        f"{POD_NAME} identity actually replaced by the scale-to-0/1 cycle: new UID {new_pod_uid!r} != original UID {original_pod_uid!r}",
    )

    def _app_ready() -> bool | None:
        result = run("-n", NAMESPACE, "get", "deployment", "maops-app", "-o", "json", check=False)
        if result.returncode != 0:
            return None
        ready = json.loads(result.stdout).get("status", {}).get("readyReplicas")
        return True if ready == EXPECTED_APP_GATEWAY_REPLICAS else None

    try:
        wait_until(_app_ready, timeout=90, interval=3, description="maops-app back to 3/3 Ready")
        record_restoration(True, "maops-app back to 3/3 Ready")
    except TimeoutError as exc:
        record_restoration(False, f"maops-app did not recover to 3/3 Ready: {exc}")
        return False

    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            ok_ready, detail_ready = check_endpoint(local_port, "/readyz", role="gateway")
    except (TimeoutError, RuntimeError) as exc:
        record_restoration(False, f"post-recovery gateway Service /readyz check failed: {exc}")
        return False
    record_restoration(ok_ready, f"post-recovery gateway /readyz via Service: {detail_ready}")
    if not ok_ready:
        return False

    # Contract A: the marker written BEFORE the outage must still be
    # readable through the now-recovered chain. This must be verified
    # and recorded BEFORE Contract B (below) overwrites it with the true
    # original value - restoring first would make it impossible to ever
    # observe whether the marker actually survived the 1 -> 0 -> 1
    # cycle. Bounded retry: the chain may still be briefly
    # re-converging even after gateway's own /readyz above reported 200
    # once (same propagation-lag class documented in
    # persistence_check.py - a single successful /readyz call can be
    # load-balanced to only one of 3 replicas).
    # DAY4 batch 2b: this must catch the same transport/parsing failure
    # classes as the restoring PUT/GET below (ValueError covers
    # json.JSONDecodeError; OSError covers a dropped connection) - the
    # previous (TimeoutError, RuntimeError)-only clause let a malformed
    # response or a transport OSError escape restore_state() entirely
    # (it is not called from inside any try/except in main()'s
    # finally), which would abort Contract B's restoration too, not
    # just fail Contract A's own verification.
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            status, body, value_ok, value, err = None, {}, False, None, ""
            for _attempt in range(RESTORE_PUT_RETRIES):
                status, body = _http(local_port, "GET", "/state")
                value_ok, value, err = validate_state_value(body)
                if status == 200 and value_ok and value == marker:
                    break
                time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
            record(
                status == 200 and value_ok and value == marker,
                f"post-recovery GET /state via service chain matches pre-outage marker (Contract A, verified before Contract B restoration): "
                f"HTTP {status}, value={value!r} (expected {marker!r})" + (f", schema error: {err}" if not value_ok else ""),
            )
    except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
        record(False, f"post-recovery marker verification failed (Contract A) - proceeding to attempt Contract B restoration regardless: {exc}")

    # Never restore anything unless the true original value was
    # actually, verifiably captured before any mutation - restoring
    # None/an invented value over a real one is the exact defect this
    # batch fixes (DAY4-TEST-H3/DAY4-REL-1). This experiment's own
    # freshly-written marker surviving the 1 -> 0 -> 1 cycle (proven
    # separately, in main()) is Contract A; restoring the TRUE original
    # record here is Contract B - both are proven independently.
    if not baseline_captured:
        record_restoration(False, "internal invariant violated: cannot restore the true original record because baseline capture never succeeded earlier - refusing to write an unknown/invented value")
        return False

    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            put_status = None
            for _attempt in range(RESTORE_PUT_RETRIES):
                try:
                    put_status, _ = _http(local_port, "PUT", "/state", {"value": original_value})
                except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
                    put_status = None
                    record_restoration(False, f"restoring PUT /state raised {exc!r} (attempt may still have persisted server-side)")
                else:
                    if put_status == 200:
                        break
                time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
            record_restoration(put_status == 200, f"restoring PUT /state (true original record) through service chain returned HTTP {put_status} after up to {RESTORE_PUT_RETRIES} attempt(s)")

            # A successful PUT response alone cannot prove restoration -
            # independently GET and compare (bounded retry: the chain may
            # still be briefly re-converging even after the PUT itself
            # succeeded).
            get_status, get_body, value_ok, value, err = None, {}, False, None, ""
            for _attempt in range(RESTORE_PUT_RETRIES):
                try:
                    get_status, get_body = _http(local_port, "GET", "/state")
                except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
                    get_status, get_body, value_ok, value, err = None, {}, False, None, repr(exc)
                else:
                    value_ok, value, err = validate_state_value(get_body)
                    if get_status == 200 and value_ok and value == original_value:
                        break
                time.sleep(RESTORE_PUT_RETRY_INTERVAL_SECONDS)
            restored_ok = get_status == 200 and value_ok and value == original_value
            record_restoration(
                restored_ok,
                f"independent GET /state after restoration matches captured original value: HTTP {get_status}, "
                f"value={value!r} (expected {original_value!r})" + (f", error: {err}" if not value_ok else ""),
            )
    except (TimeoutError, RuntimeError) as exc:
        record_restoration(False, f"could not restore the true original record: {exc}")
        return False

    return restored_ok


def main() -> int:
    print(f"# Day 4 retention/outage proof (maops-state 1 -> 0 -> 1) against context {CONTEXT}")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    original_pvc_uid, original_phase, original_volume, original_pv_phase = _pvc_pv_snapshot()
    identity_ok = (
        is_nonempty_identity(original_pvc_uid)
        and original_phase == "Bound"
        and is_nonempty_identity(original_volume)
        and original_pv_phase == "Bound"
    )
    record(identity_ok, f"baseline identity captured: PVC {PVC_NAME} uid={original_pvc_uid} phase={original_phase}, volume={original_volume}, PV phase={original_pv_phase}")

    pod_result = run("-n", NAMESPACE, "get", "pod", POD_NAME, "-o", "json", check=False)
    original_pod_uid = json.loads(pod_result.stdout).get("metadata", {}).get("uid") if pod_result.returncode == 0 else None
    record(is_nonempty_identity(original_pod_uid), f"baseline identity captured: {POD_NAME} uid={original_pod_uid}")

    # DAY4 batch 2b: a missing/empty identity is not a lesser finding -
    # it means nothing downstream (retention proof, restoration
    # comparison) can be trusted, so this must abort before any
    # mutation, exactly like a failed baseline value capture below.
    if not (identity_ok and is_nonempty_identity(original_pod_uid)):
        record(False, "baseline structural/identity preconditions not met - aborting before any mutation")
        return 1

    # Baseline capture MUST succeed, with certainty, BEFORE any mutation
    # below - not just before scale_state(0), but before this script's
    # own marker PUT too (DAY4-TEST-H3/DAY4-REL-1: the previous version
    # had no `original_value` at all and silently overwrote the true
    # pre-existing record with its own fresh marker, permanently losing
    # it). A transport exception is exactly as fatal here as a non-200/
    # malformed response.
    original_value = None
    baseline_captured = False
    try:
        with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
            original_value = _capture_baseline(local_port)
        baseline_captured = True
        record(True, f"baseline GET /state captured for restoration (value={'<non-null>' if original_value is not None else None!r})")
    except (BaselineCaptureError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
        record(False, f"baseline GET /state capture failed - aborting before any mutation: {exc}")
        return 1

    marker = f"day4-retention-{uuid.uuid4().hex[:16]}"

    # DAY4 batch 2b: ONE outer boundary covers every mutating call in
    # this experiment, starting at the FIRST one (the marker PUT below)
    # - not just the scale-down. `mutation_attempted` is set
    # IMMEDIATELY BEFORE that first attempt, never after it returns or
    # only inside some of its exception handlers: a JSONDecodeError/
    # OSError from the marker PUT, or a subprocess.TimeoutExpired from
    # scale_state(0), can each occur AFTER the server-side effect has
    # already committed - the client not knowing that must never be
    # read as "nothing happened, no restoration owed". This also fixes
    # the previous structural bug where the marker PUT lived in its own
    # try/except entirely OUTSIDE the try/finally guarding restoration:
    # an exception type that except clause didn't catch (JSONDecodeError,
    # OSError) escaped main() before the finally below was ever reached.
    #
    # `scaled_down` remains a SEPARATE, narrower flag: it only gates
    # whether the outage-observation body below (which assumes the Pod
    # is actually gone) is worth attempting - a scale-down that was
    # outright rejected (CalledProcessError) or never returned
    # (TimeoutExpired) still requires restore_state() to run (via
    # mutation_attempted), but should not also waste a 90s wait_until
    # for a Pod that was never asked to leave with any confirmed effect.
    mutation_attempted = False
    scaled_down = False
    try:
        mutation_attempted = True
        try:
            with port_forward(CONTEXT, NAMESPACE, GATEWAY_SERVICE, 8080) as local_port:
                status, _ = _http(local_port, "PUT", "/state", {"value": marker})
                record(status == 200, f"marker write through service chain (Contract A: fresh-value survival): HTTP {status}")
        except (TimeoutError, RuntimeError, ValueError, OSError) as exc:
            record(False, f"marker write failed (server-side effect unknown - restoration still required): {exc}")

        try:
            scale_state(0)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            record(False, f"scale {STATE_STATEFULSET} to 0 failed or timed out (server-side effect unknown - may have been accepted): {_stderr_detail(exc)}")
        else:
            scaled_down = True

        if scaled_down:
            try:
                wait_until(_state_pod_gone, timeout=90, interval=2, description=f"{POD_NAME} fully terminated")
                record(True, f"{POD_NAME} fully terminated after scaling {STATE_STATEFULSET} to 0")
            except TimeoutError as exc:
                record(False, str(exc))

            pvc_uid, phase, volume, pv_phase = _pvc_pv_snapshot()
            record(is_nonempty_identity(pvc_uid) and pvc_uid == original_pvc_uid, f"PVC {PVC_NAME} UID unchanged during outage: {pvc_uid} == {original_pvc_uid}")
            record(phase == "Bound", f"PVC {PVC_NAME} remains Bound during outage (whenScaled: Retain): phase={phase}")
            record(is_nonempty_identity(volume) and volume == original_volume, f"PVC {PVC_NAME} still references the same PV during outage: {volume} == {original_volume}")
            record(pv_phase == "Bound", f"PV remains Bound during outage: phase={pv_phase}")

            app_pods = get_pods(APP_LABEL_SELECTOR)
            gateway_pods = get_pods(GATEWAY_LABEL_SELECTOR)
            for role, pods, label_selector in (("app", app_pods, APP_LABEL_SELECTOR), ("gateway", gateway_pods, GATEWAY_LABEL_SELECTOR)):
                if not pods:
                    record(False, f"{role} outage observation: no pods available")
                    continue
                pod_name = pods[0]["metadata"]["name"]
                try:
                    with port_forward(CONTEXT, NAMESPACE, pod_name, 8080, resource_kind="pod") as local_port:
                        ok_live, detail_live = check_endpoint(local_port, "/livez", role=role)
                        record(ok_live, f"{role} /livez during state outage (direct pod {pod_name}): {detail_live}")
                        status, _body = raw_get(local_port, "/readyz")
                        record(status == 503, f"{role} /readyz during state outage (direct pod {pod_name}) -> HTTP {status} (expected 503)")
                except (TimeoutError, RuntimeError) as exc:
                    record(False, f"{role} direct pod port-forward failed during state outage: {exc}")
    except Exception as exc:  # noqa: BLE001 - controlled record, restoration below always still runs. Never catches
        # KeyboardInterrupt (a BaseException, not an Exception) - a supported interruption during an
        # attempted write still reaches `finally` below (Python always runs `finally` regardless of
        # exception type) and then propagates, honestly, rather than being reported as a clean result.
        # `try/finally` cannot recover from SIGKILL/host power loss - this is a best-effort guarantee
        # against normal exceptions, sys.exit, and KeyboardInterrupt, never an unconditional promise.
        record(False, f"retention experiment raised an unexpected error: {exc}")
    finally:
        if mutation_attempted:
            restore_state(original_pod_uid, marker, original_value, baseline_captured)

    if scaled_down:
        final_pvc_uid, final_phase, final_volume, final_pv_phase = _pvc_pv_snapshot()
        record(is_nonempty_identity(final_pvc_uid) and final_pvc_uid == original_pvc_uid, f"PVC {PVC_NAME} UID unchanged after full cycle: {final_pvc_uid} == {original_pvc_uid}")
        record(is_nonempty_identity(final_volume) and final_volume == original_volume, f"PVC {PVC_NAME} bound to the same PV after full cycle: {final_volume} == {original_volume}")

    all_results = results + restoration_results
    failures = [m for ok, m in all_results if not ok]
    restoration_failures = [m for ok, m in restoration_results if not ok]

    print()
    print(f"{len(all_results) - len(failures)}/{len(all_results)} retention/outage checks passed")
    if restoration_failures:
        print()
        print(f"!!! RESTORATION FAILURE - {STATE_STATEFULSET} may not be back at 1/1 Ready - independent action required !!!", file=sys.stderr)
        for msg in restoration_failures:
            print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
    if failures:
        print(f"FAIL: {len(failures)} retention/outage check(s) failed", file=sys.stderr)
        return 1
    print("PASS: retention/outage behavior proven and maops-state restored to 1/1 Ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

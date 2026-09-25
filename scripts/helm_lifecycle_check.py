#!/usr/bin/env python3
"""
DAY6: bounded live proof of a real Helm release lifecycle - upgrade
then rollback - against the Day 6 release. This is Helm RELEASE
rollback (a `helm rollback` to a previous Helm revision of the SAME
chart/config), never the Day 7 Recreate/Blue-Green/Canary deployment-
STRATEGY demonstration (a Day 7 concern, built on top of what Day 6
introduces, not introduced here).

DAY6 (live-discovered Helm ConfigMap rollout remediation) - a live run
found the ORIGINAL revision of this script's "prove the upgrade
happened" step was insufficient: it accepted `kubectl rollout status`
returning success as proof of a NEW rollout, but that command ALSO
returns success (trivially, instantly) for an ALREADY-healthy,
UNCHANGED Deployment - which is exactly what happened. The probe
`helm upgrade` changed only `gateway.config.appMessage`, which the
chart previously rendered into `maops-gateway-config` alone, with
nothing in the gateway Deployment's own Pod template referencing that
ConfigMap's content. Since `envFrom`-mounted ConfigMap data is read
only once, at container startup, and Kubernetes only creates a new
ReplicaSet/rolls Pods when the Pod template ITSELF changes, the
existing, already-Ready gateway Pods were never replaced - their
processes kept running with the OLD `APP_MESSAGE` in memory, while
`kubectl rollout status` correctly (but insufficiently) reported the
Deployment as healthy.

Fix (chart-side, `charts/maops-kubernetes-platform/templates/*-{deployment,statefulset}.yaml`):
each workload's Pod template now carries a deterministic
`checksum/config` annotation of its OWN ConfigMap template's rendered
content - a ConfigMap-only value change is now a Pod-template change
too, and therefore a real rollout trigger. Fix (this script): the
upgrade proof no longer trusts `kubectl rollout status` alone. It
requires ALL of: the live ConfigMap actually contains the new message,
the Deployment's `observedGeneration` reached the new `generation`, the
Pod-template `checksum/config` actually changed, a DIFFERENT
ReplicaSet became the active one, every expected Pod is Ready, NONE of
the pre-upgrade Pod UIDs remain, EVERY ready Pod's own `/config`
endpoint (queried directly inside that specific Pod, never through the
Service) reports the new message, and the externally-routed Gateway
API path (bounded polling) agrees too. The guaranteed rollback path
applies the identical, symmetric proof in reverse - including that the
checksum returns to the EXACT pre-upgrade value, not merely "a
different" one.

Sequence (every step recorded; rollback is attempted in a guaranteed
`finally` once the upgrade was actually submitted, regardless of where
an earlier step failed - same pattern as
scripts/dependency_check.py/scaling_check.py's own restoration
guarantees):

  1. Record the current release revision (`helm history`) and a full
     healthy baseline gateway state (ConfigMap `APP_MESSAGE`, Deployment
     generation/observedGeneration, Pod-template checksum, the active
     ReplicaSet's UID, every gateway Pod's UID, and the state PVC/PV
     identity - never mutates `/state`'s VALUE itself).
  2. A controlled `helm upgrade` that changes ONLY
     `gateway.config.appMessage` (a harmless, purely cosmetic value -
     see docs/architecture.md; never a security-relevant or
     schema-shape change) via `--reuse-values --set`, so every other
     value stays exactly what the current release already has.
  3. Prove the upgrade actually rolled new Pods - not merely that Helm
     and `kubectl rollout status` reported success - per the full
     checklist above, then prove the new message is visible both
     directly (per-Pod) and through the externally-routed Gateway path.
  4. `helm rollback` back to the revision recorded in step 1.
  5. Prove the ORIGINAL message, checksum, ReplicaSet, and Pod set are
     ALL genuinely restored - the same full checklist, in reverse,
     against the exact pre-upgrade baseline.
  6. Prove the state PVC/PV identity is byte-for-byte the SAME
     before/after (nothing was recreated) - this script never issues a
     PUT /state at all, so the persisted VALUE is untouched throughout
     by construction, not merely by coincidence.
  7. Reject, as explicit failures or INCONCLUSIVE (never a silent
     pass): a missing/unusable revision to roll back to, an API error,
     malformed output, missing Pods, partial Pod replacement, a rollout
     that never reaches genuine convergence after either the upgrade or
     the rollback, or a post-rollback state that doesn't exactly match
     the pre-upgrade baseline (partial restoration).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube

HELM_RELEASE = kube.HELM_RELEASE_NAME
CHART_DIR = str(Path(__file__).resolve().parent.parent / "charts" / "maops-kubernetes-platform")
CONFIGMAP_NAME = "maops-gateway-config"
GATEWAY_DEPLOYMENT = "maops-gateway"
UPGRADE_MESSAGE = "Hello from the MAOps Kubernetes Gateway (Day 6 - helm-lifecycle-check probe)"
HTTP_TIMEOUT_SECONDS = 5.0
ROLLOUT_TIMEOUT_SECONDS = 180
EXTERNAL_POLL_TIMEOUT_SECONDS = 60.0
EXTERNAL_POLL_INTERVAL_SECONDS = 2.0
POD_CONFIG_CHECK_TIMEOUT_SECONDS = 10

results: list[tuple[bool, str]] = []


@dataclass(frozen=True)
class GatewayState:
    """A single, internally-consistent snapshot of everything this
    script needs to prove a genuine rollout happened (or didn't) -
    never partially populated with stale/mismatched reads from
    different points in time."""

    configmap_message: str
    desired_replicas: int
    deployment_generation: int
    observed_generation: int
    pod_template_checksum: str
    replicaset_uids: frozenset[str]
    pod_uids: frozenset[str]
    ready_pod_names: tuple[str, ...]


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _helm(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    cmd = ["helm", *args, "--namespace", kube.NAMESPACE, "--kubeconfig", kube.KUBECONFIG_PATH, "--kube-context", kube.CONTEXT]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _current_revision() -> int | None:
    result = _helm("history", HELM_RELEASE, "-o", "json")
    if result.returncode != 0:
        record(False, f"INCONCLUSIVE: helm history failed: {result.stderr.strip()}")
        return None
    try:
        history = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        record(False, f"INCONCLUSIVE: helm history returned unparseable JSON: {exc}")
        return None
    if not history:
        record(False, "no Helm release revisions found - refusing to proceed without a real revision to roll back to")
        return None
    return max(entry["revision"] for entry in history)


def _configmap_message() -> str | None:
    result = kube.run("-n", kube.NAMESPACE, "get", "configmap", CONFIGMAP_NAME, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("data", {}).get("APP_MESSAGE")
    except json.JSONDecodeError:
        return None


def _pvc_identity() -> tuple[str | None, str | None]:
    pvc_result = kube.run("-n", kube.NAMESPACE, "get", "pvc", "data-maops-state-0", "-o", "json", check=False)
    if pvc_result.returncode != 0:
        record(False, f"INCONCLUSIVE: could not read state PVC: {pvc_result.stderr.strip()}")
        return None, None
    pvc = json.loads(pvc_result.stdout)
    pvc_uid = pvc.get("metadata", {}).get("uid")
    pv_name = pvc.get("spec", {}).get("volumeName")
    pv_uid = None
    if pv_name:
        pv_result = kube.run("get", "pv", pv_name, "-o", "json", check=False)
        if pv_result.returncode == 0:
            pv_uid = json.loads(pv_result.stdout).get("metadata", {}).get("uid")
    return pvc_uid, pv_uid


def _is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _list_json(resource: str, label_selector: str) -> list[dict] | None:
    result = kube.run("-n", kube.NAMESPACE, "get", resource, "-l", label_selector, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("items", [])
    except json.JSONDecodeError:
        return None


def _capture_gateway_state_quiet() -> GatewayState | None:
    """Reads everything needed for a single, internally-consistent
    `GatewayState` snapshot. Returns None on ANY read/parse failure -
    NEVER fabricates a partial state, and never calls `record()` itself
    (intended for use inside bounded polling, where a single transient
    read failure mid-rollout is expected and must not spam `results`
    with noise; the caller records its own final, meaningful findings
    once polling settles or times out)."""
    result = kube.run("-n", kube.NAMESPACE, "get", "deployment", GATEWAY_DEPLOYMENT, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    try:
        deployment = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    message = _configmap_message()
    if message is None:
        return None

    desired_replicas = deployment.get("spec", {}).get("replicas")
    generation = deployment.get("metadata", {}).get("generation")
    observed_generation = deployment.get("status", {}).get("observedGeneration")
    if desired_replicas is None or generation is None or observed_generation is None:
        return None

    checksum = (deployment.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get("checksum/config")
    if not checksum:
        return None

    deployment_uid = deployment.get("metadata", {}).get("uid")
    replicasets = _list_json("replicasets", kube.GATEWAY_LABEL_SELECTOR)
    if replicasets is None:
        return None
    replicaset_uids = frozenset(
        rs["metadata"]["uid"]
        for rs in replicasets
        if any(o.get("uid") == deployment_uid for o in (rs.get("metadata", {}).get("ownerReferences") or []))
        and (rs.get("spec", {}).get("replicas") or 0) > 0
    )
    if not replicaset_uids:
        return None

    pods = _list_json("pods", kube.GATEWAY_LABEL_SELECTOR)
    if pods is None:
        return None
    pod_uids = frozenset(p["metadata"]["uid"] for p in pods if p.get("metadata", {}).get("uid"))
    ready_pod_names = tuple(p["metadata"]["name"] for p in pods if _is_ready(p))

    return GatewayState(
        configmap_message=message,
        desired_replicas=desired_replicas,
        deployment_generation=generation,
        observed_generation=observed_generation,
        pod_template_checksum=checksum,
        replicaset_uids=replicaset_uids,
        pod_uids=pod_uids,
        ready_pod_names=ready_pod_names,
    )


def _capture_gateway_state_or_record(label: str) -> GatewayState | None:
    state = _capture_gateway_state_quiet()
    if state is None:
        record(False, f"INCONCLUSIVE ({label}): could not fully capture live gateway Deployment/ConfigMap/ReplicaSet/Pod state")
    return state


def _wait_for_gateway_rollout_settled(expected_replicas: int, timeout: float = ROLLOUT_TIMEOUT_SECONDS) -> GatewayState | None:
    """Polls (quietly - no per-tick recording) until the gateway
    Deployment's observedGeneration matches its generation AND all
    expected replicas are Ready - i.e. the rollout, whatever it did or
    didn't change, has settled enough to meaningfully evaluate. Returns
    the settled state, or a best-effort LAST-OBSERVED snapshot on
    timeout (never fabricated - a genuine read, just one that didn't
    satisfy the settle predicate) so the caller can still report
    exactly which specific downstream condition was or was not
    satisfied. Returns None only if even that final read fails."""

    def predicate():
        state = _capture_gateway_state_quiet()
        if state is None:
            return None
        if state.observed_generation != state.deployment_generation:
            return None
        if len(state.ready_pod_names) != expected_replicas:
            return None
        return state

    try:
        return kube.wait_until(predicate, timeout=timeout, interval=2.0, description="gateway Deployment observedGeneration + all Pods Ready")
    except TimeoutError:
        return _capture_gateway_state_quiet()


def _pod_reports_message(pod_name: str, expected: str) -> tuple[bool, str]:
    """Execs directly INTO `pod_name` and queries its own `/config` over
    loopback (127.0.0.1) - never through the Service, so this is
    unambiguously THAT specific Pod's own process answering, not
    whichever Pod the Service happened to route to."""
    snippet = (
        "import urllib.request, json\n"
        'req = urllib.request.Request("http://127.0.0.1:__PORT__/config")\n'
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=__TIMEOUT__) as resp:\n"
        "        body = resp.read().decode('utf-8', errors='replace')\n"
        "    data = json.loads(body)\n"
        "    print('MESSAGE=' + str(data.get('APP_MESSAGE', '')))\n"
        "except Exception as e:\n"
        "    print('ERROR=' + type(e).__name__ + ':' + str(e))\n"
    ).replace("__PORT__", "8080").replace("__TIMEOUT__", str(POD_CONFIG_CHECK_TIMEOUT_SECONDS))
    result = kube.run(
        "-n", kube.NAMESPACE, "exec", pod_name, "--", "/usr/bin/python3.11", "-c", snippet,
        check=False, timeout=kube.subprocess_timeout_for(POD_CONFIG_CHECK_TIMEOUT_SECONDS),
    )
    if result.returncode != 0:
        return False, f"kubectl exec failed: {result.stderr.strip()}"
    output = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if output.startswith("MESSAGE="):
        actual = output[len("MESSAGE=") :]
        return actual == expected, f"reported APP_MESSAGE={actual!r}"
    return False, f"unrecognized/unparseable probe output: {output!r}"


def _verify_all_pods_report_message(pod_names: tuple[str, ...], expected: str, label: str) -> bool:
    if not pod_names:
        return record(False, f"{label}: no Ready gateway Pods available to verify /config against")
    overall = True
    for pod_name in pod_names:
        ok, detail = _pod_reports_message(pod_name, expected)
        overall = record(ok, f"{label}: gateway Pod {pod_name!r} directly reports /config APP_MESSAGE == {expected!r} ({detail})") and overall
    return overall


def _external_app_message_quiet() -> str | None:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{kube.GATEWAY_HOST_ADDRESS}/config", headers={"Host": kube.ROUTING_HOSTNAME})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        return json.loads(body).get("APP_MESSAGE")
    except json.JSONDecodeError:
        return None


def _wait_external_message(expected: str, label: str) -> bool:
    deadline = time.monotonic() + EXTERNAL_POLL_TIMEOUT_SECONDS
    last_seen = None
    while time.monotonic() < deadline:
        last_seen = _external_app_message_quiet()
        if last_seen == expected:
            return record(True, f"{label}: externally-routed /config reports APP_MESSAGE == {expected!r} (bounded polling)")
        time.sleep(EXTERNAL_POLL_INTERVAL_SECONDS)
    return record(
        False,
        f"{label}: externally-routed /config never reported APP_MESSAGE == {expected!r} within {EXTERNAL_POLL_TIMEOUT_SECONDS}s of bounded polling (last seen: {last_seen!r})",
    )


def _verify_rollout_diverged(reference: GatewayState, expected_replicas: int, label: str, exact_checksum: str | None = None) -> GatewayState | None:
    """The core hardened proof - see module docstring. Waits for the
    rollout to settle, then records FIVE individually distinct
    findings comparing the settled state against `reference`:
    observedGeneration caught up, the Pod-template checksum changed
    (or, for a rollback, returned to EXACTLY `exact_checksum`), a
    DIFFERENT ReplicaSet became active, no `reference` Pod UID remains,
    and all `expected_replicas` are Ready. Returns the settled state
    only if every one of the five passed - None otherwise (each
    individual failure is already recorded, so the caller never needs
    to re-explain why)."""
    settled = _wait_for_gateway_rollout_settled(expected_replicas)
    if settled is None:
        record(False, f"{label}: could not observe the gateway Deployment/Pods/ReplicaSets at all - INCONCLUSIVE")
        return None

    gen_ok = record(
        settled.observed_generation == settled.deployment_generation,
        f"{label}: gateway Deployment observedGeneration ({settled.observed_generation}) reached its generation ({settled.deployment_generation})",
    )
    if exact_checksum is not None:
        checksum_ok = record(
            settled.pod_template_checksum == exact_checksum,
            f"{label}: gateway Pod-template checksum restored to the exact pre-upgrade baseline value ({exact_checksum!r}), found {settled.pod_template_checksum!r}",
        )
    else:
        checksum_ok = record(
            settled.pod_template_checksum != reference.pod_template_checksum,
            f"{label}: gateway Pod-template checksum changed (was {reference.pod_template_checksum!r}, now {settled.pod_template_checksum!r})",
        )
    replicaset_ok = record(
        settled.replicaset_uids != reference.replicaset_uids,
        f"{label}: a new ReplicaSet was created/selected (was {sorted(reference.replicaset_uids)}, now {sorted(settled.replicaset_uids)})",
    )
    ready_ok = record(
        len(settled.ready_pod_names) == expected_replicas,
        f"{label}: all {expected_replicas} gateway Pods are Ready (found {len(settled.ready_pod_names)})",
    )
    remaining = settled.pod_uids & reference.pod_uids
    no_stale_pods_ok = record(
        not remaining,
        f"{label}: none of the prior gateway Pod UIDs remain (found overlap: {sorted(remaining) if remaining else 'none'})",
    )

    if gen_ok and checksum_ok and replicaset_ok and ready_ok and no_stale_pods_ok:
        return settled
    return None


def main() -> int:
    print(f"# Day 6 Helm lifecycle check: bounded upgrade + rollback proof (release {HELM_RELEASE!r}, context {kube.CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    baseline_revision = _current_revision()
    baseline_state = _capture_gateway_state_or_record("pre-upgrade baseline")
    baseline_pvc_uid, baseline_pv_uid = _pvc_identity()

    if baseline_revision is None or baseline_state is None or baseline_pvc_uid is None:
        print("FAIL: could not establish a healthy baseline - refusing to mutate the release", file=sys.stderr)
        return 1

    expected_replicas = baseline_state.desired_replicas
    upgrade_submitted = False
    upgrade_state: GatewayState | None = None
    overall_ok = True
    try:
        upgrade_result = _helm(
            "upgrade", HELM_RELEASE, CHART_DIR,
            "--reuse-values",
            "--set", f"gateway.config.appMessage={UPGRADE_MESSAGE}",
            "--wait",
        )
        upgrade_submitted = upgrade_result.returncode == 0
        overall_ok = record(upgrade_submitted, f"helm upgrade (visible config change only) submitted: {'succeeded' if upgrade_submitted else upgrade_result.stderr.strip()}") and overall_ok

        if upgrade_submitted:
            live_message = _configmap_message()
            overall_ok = record(live_message == UPGRADE_MESSAGE, f"live ConfigMap {CONFIGMAP_NAME} contains the upgraded APP_MESSAGE (found {live_message!r})") and overall_ok

            upgrade_state = _verify_rollout_diverged(baseline_state, expected_replicas, "post-upgrade")
            if upgrade_state is not None:
                overall_ok = _verify_all_pods_report_message(upgrade_state.ready_pod_names, UPGRADE_MESSAGE, "post-upgrade") and overall_ok
                overall_ok = _wait_external_message(UPGRADE_MESSAGE, "post-upgrade") and overall_ok
            else:
                overall_ok = False

            new_pvc_uid, new_pv_uid = _pvc_identity()
            overall_ok = record(
                new_pvc_uid == baseline_pvc_uid and new_pv_uid == baseline_pv_uid,
                f"state PVC/PV identity unchanged across upgrade (pvc={new_pvc_uid!r}, pv={new_pv_uid!r})",
            ) and overall_ok
    finally:
        if not upgrade_submitted:
            record(True, "helm upgrade was never submitted successfully - nothing was changed, so no rollback is needed and none was attempted")
        else:
            rollback_result = _helm("rollback", HELM_RELEASE, str(baseline_revision), "--wait")
            rollback_ok = rollback_result.returncode == 0
            overall_ok = record(rollback_ok, f"helm rollback to revision {baseline_revision}: {'succeeded' if rollback_ok else rollback_result.stderr.strip()}") and overall_ok

            if rollback_ok:
                restored_message = _configmap_message()
                overall_ok = record(
                    restored_message == baseline_state.configmap_message,
                    f"live ConfigMap restored to the pre-upgrade APP_MESSAGE (expected {baseline_state.configmap_message!r}, found {restored_message!r})",
                ) and overall_ok

                # Diverge AWAY from whatever the upgrade's state was (a
                # pre-rollback reference we may not have if the upgrade's
                # own convergence proof failed - fall back to the
                # original baseline as the reference in that case, which
                # still correctly requires the rollback's Pod UIDs to
                # differ from the ORIGINAL baseline's, a strictly
                # stronger requirement).
                rollback_reference = upgrade_state if upgrade_state is not None else baseline_state
                rollback_state = _verify_rollout_diverged(
                    rollback_reference, expected_replicas, "post-rollback", exact_checksum=baseline_state.pod_template_checksum
                )
                if rollback_state is not None:
                    overall_ok = _verify_all_pods_report_message(rollback_state.ready_pod_names, baseline_state.configmap_message, "post-rollback") and overall_ok
                    overall_ok = _wait_external_message(baseline_state.configmap_message, "post-rollback") and overall_ok
                else:
                    overall_ok = False

                final_pvc_uid, final_pv_uid = _pvc_identity()
                overall_ok = record(
                    final_pvc_uid == baseline_pvc_uid and final_pv_uid == baseline_pv_uid,
                    f"state PVC/PV identity unchanged end to end (pvc={final_pvc_uid!r}, pv={final_pv_uid!r})",
                ) and overall_ok
            else:
                record(False, "RESTORATION FAILURE: helm rollback did not succeed - the release may be left on the probe's temporary config")
                overall_ok = False

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} Helm lifecycle checks passed")
    if failures or not overall_ok:
        print(f"FAIL: {len(failures)} Helm lifecycle check(s) failed", file=sys.stderr)
        return 1
    print("PASS: Helm upgrade + rollback lifecycle verified end to end (genuine Pod replacement proven both ways), with state preserved throughout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

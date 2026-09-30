#!/usr/bin/env python3
"""
DAY7: bounded Canary experiment on the isolated Day 7 cluster - ONE
HTTPRoute with two weighted backendRefs (stable 90 / candidate 10), plus
a controlled candidate-unready negative scenario.

Entry gate: as Blue/Green - strategy baseline present (never
recaptured), release at the stable stage, route 100% stable and
current, no candidate-only object, external stable response.

Phase A - positive canary:
  1. green-prepared (candidate enabled, route unchanged) and the full
     candidate preflight gate.
  2. promote("canary-90-10") - the gate is re-run and then the candidate
     path checks are enforced immediately before the Helm upgrade; any
     failing, inconclusive or unreadable check prevents submission.
  3. Live route proof: backendRefs are exactly maops-gateway:8080 w=90
     and maops-gateway-candidate:8080 w=10, Accepted/ResolvedRefs True
     for the CURRENT generation; stable and candidate ready endpoints
     are 3 and 2 and disjoint. If the route identity or the endpoints
     fail, the experiment stops before sampling and restores at once.
  4. CANARY_SAMPLES independent external `/backend` requests (each on a
     new connection), bounded in time. Each response is classified by
     the serving Pod (gateway_hostname must be a live stable or
     candidate Pod name) and must be a normal app result. Required:
     every request completed, >=1 stable and >=1 candidate, zero
     errors, zero unidentified. The observed split is printed as
     counts only - a finite sample is never presented as proof of an
     exact 90/10 ratio.
  5. Verified return to stable before Phase B.

Phase B - candidate-unready negative scenario:
  1. candidate-unready stage (faultInjection.failReadiness; route stays
     stable; applied with --wait=hookOnly because the candidate is
     deliberately never Ready). Bounded wait until every candidate Pod
     is Running with its container started (startup probe passed, so
     it is alive) and then a hold period during which NO candidate Pod
     is Ready and the candidate Service has ZERO ready endpoints.
  2. promote("canary-90-10") must be REFUSED by the gate, with at least
     one failing readiness check; the Helm revision must not move, the
     route's UID/generation/backendRefs must be unchanged, and a bounded
     external sample must be 100% stable - no new traffic reached the
     unready candidate.

finally: restoration to stable, verified and reported separately.

Mesh caveat: an unready candidate is simply not an endpoint; this
scenario does not (and could not) prove mesh authorization - no local
TCP connect is treated as evidence of anything.
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_blue_green
import day7_strategy as d7
import kube

TITLE = "Day 7 Canary"
CANARY_SAMPLES = 200
CANARY_INTERVAL_SECONDS = 0.05
CANARY_MAX_SECONDS = 180.0
UNREADY_START_TIMEOUT_SECONDS = 150.0
UNREADY_HOLD_SECONDS = 30.0
NEGATIVE_EXTERNAL_SAMPLES = 30


def unready_observation_verdict(pods: list[dict], ready_endpoints: int | None, expected: int) -> tuple[bool, str]:
    """Pure: the candidate is alive-but-unready - exactly `expected`
    Pods, all Running, container started, none Ready, zero ready
    endpoints."""
    if len(pods) != expected:
        return False, f"{len(pods)} candidate Pods (expected {expected})"
    for pod in pods:
        name = pod.get("metadata", {}).get("name")
        if pod.get("status", {}).get("phase") != "Running":
            return False, f"{name} phase {pod.get('status', {}).get('phase')!r}"
        statuses = pod.get("status", {}).get("containerStatuses", []) or []
        if not statuses or not all(s.get("started") is True for s in statuses):
            return False, f"{name} container not started yet"
        if d7.pod_is_ready(pod):
            return False, f"{name} is Ready - the fault injection did not hold"
    if ready_endpoints != 0:
        return False, f"candidate Service ready endpoints = {ready_endpoints} (expected 0)"
    return True, f"{expected} candidate Pods alive (Running, started) and none Ready; 0 ready candidate endpoints"


def phase_a(rec: d7.Recorder, baseline: dict) -> bool:
    stable_msg = baseline["stable_message"]
    rec.section("Phase A step 1: prepare the candidate (route unchanged) and gate")
    if not d7.apply_stage(rec, "green-prepared"):
        return False
    prepared_backends = d7.expected_backends(d7.load_stage_values("green-prepared"))
    route_prepared = d7.wait_route(rec, prepared_backends, "green-prepared")
    if route_prepared is None:
        return False

    rec.section("Phase A step 2: promote to canary 90/10 (gate re-run before submission)")
    promotion = d7.promote(rec, "canary-90-10", "green-prepared")
    for passed, msg in promotion.gate_checks:
        rec.record(passed, f"gate: {msg}")
    if not promotion.submitted:
        rec.record(False, f"canary refused - {promotion.refusal}; no Helm upgrade submitted")
        return False
    if not promotion.applied_ok:
        rec.record(False, "canary 90/10 Helm upgrade failed after the gate and path checks passed - restoration follows")
        return False

    rec.section("Phase A step 3: live weighted route and disjoint ready endpoints")
    canary_values = d7.load_stage_values("canary-90-10")
    route = d7.wait_route(rec, d7.expected_backends(canary_values), "canary")
    if route is None:
        return False
    if not rec.record(route.uid == route_prepared.uid and route.generation > route_prepared.generation, f"same single HTTPRoute object, generation advanced {route_prepared.generation} -> {route.generation}"):
        rec.record(False, "canary route identity not verified - stopping before external sampling (restoration follows)")
        return False
    stable_ep, cand_ep = d7.read_endpoints(d7.STABLE_SERVICE), d7.read_endpoints(d7.CANDIDATE_SERVICE)
    replicas = canary_values["candidate"]["replicas"]
    endpoints_ok = (
        stable_ep is not None and cand_ep is not None
        and len(stable_ep.ready_addresses) == d7.STABLE_EXPECTED_REPLICAS
        and len(cand_ep.ready_addresses) == replicas
        and not (stable_ep.ready_addresses & cand_ep.ready_addresses)
    )
    if not rec.record(endpoints_ok, f"ready endpoints stable={None if stable_ep is None else sorted(stable_ep.ready_addresses)} candidate={None if cand_ep is None else sorted(cand_ep.ready_addresses)} (3 and {replicas}, disjoint)"):
        rec.record(False, "post-promotion endpoints not healthy/disjoint - stopping before the external sample; restoring promptly")
        return False

    rec.section(f"Phase A step 4: {CANARY_SAMPLES} bounded independent external requests")
    stable_pods = d7.pod_names(d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or [])
    cand_pods = d7.pod_names(d7.list_json("pods", d7.candidate_selector()) or [])
    ident = d7.Identity(stable_msg, canary_values["candidate"]["config"]["appMessage"], stable_pods, cand_pods)
    samples = d7.sample_series(CANARY_SAMPLES, "/backend", CANARY_INTERVAL_SECONDS, CANARY_MAX_SECONDS)
    classes = [d7.classify(s, ident) for s in samples]
    stable_after = d7.pod_names(d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or [])
    cand_after = d7.pod_names(d7.list_json("pods", d7.candidate_selector()) or [])
    rec.record(stable_after == stable_pods and cand_after == cand_pods, "no stable or candidate Pod churn during sampling (classification Pod sets stayed valid)")
    for passed, msg in d7.canary_verdict(classes, CANARY_SAMPLES):
        rec.record(passed, f"canary sample: {msg}")
    counts = Counter(classes)
    if samples:
        rec.informational(
            f"observed split over {len(samples)} requests in {samples[-1].t:.1f}s: stable {counts['stable']} "
            f"({100 * counts['stable'] / len(samples):.1f}%), candidate {counts['candidate']} ({100 * counts['candidate'] / len(samples):.1f}%) "
            "- a finite sample; configured weights are 90/10, this is NOT a claim of an exact ratio"
        )

    sample_ok = not rec.failures()

    rec.section("Phase A step 5: verified return to stable before the negative scenario")
    restored = d7.restore_stable(rec, stable_msg, label="Phase A restore")
    if not restored:
        rec.record(False, "Phase A restoration not verified - the negative scenario will NOT start")
    return sample_ok and restored


def phase_b(rec: d7.Recorder, baseline: dict) -> bool:
    stable_msg = baseline["stable_message"]
    rec.section("Phase B step 1: deliberately unready candidate (route stays 100% stable)")
    if not d7.apply_stage(rec, "candidate-unready"):
        return False
    unready_values = d7.load_stage_values("candidate-unready")
    replicas = unready_values["candidate"]["replicas"]
    stable_backends = d7.expected_backends(unready_values)
    route_before = d7.wait_route(rec, stable_backends, "candidate-unready")
    if route_before is None:
        return False

    deadline = time.monotonic() + UNREADY_START_TIMEOUT_SECONDS
    verdict = (False, "not observed")
    while time.monotonic() < deadline:
        pods = d7.list_json("pods", d7.candidate_selector()) or []
        ep = d7.read_endpoints(d7.CANDIDATE_SERVICE)
        verdict = unready_observation_verdict(pods, None if ep is None else len(ep.ready_addresses), replicas)
        if verdict[0]:
            break
        time.sleep(3.0)
    if not rec.record(verdict[0], f"candidate alive-but-unready within {UNREADY_START_TIMEOUT_SECONDS}s: {verdict[1]}"):
        return False
    hold_end = time.monotonic() + UNREADY_HOLD_SECONDS
    held = True
    while time.monotonic() < hold_end:
        pods = d7.list_json("pods", d7.candidate_selector()) or []
        ep = d7.read_endpoints(d7.CANDIDATE_SERVICE)
        v = unready_observation_verdict(pods, None if ep is None else len(ep.ready_addresses), replicas)
        if not v[0]:
            held = False
            verdict = v
            break
        time.sleep(3.0)
    if not rec.record(held, f"candidate stayed unready with 0 ready endpoints for the {UNREADY_HOLD_SECONDS:.0f}s hold ({verdict[1]})"):
        return False

    rec.section("Phase B step 2: promotion attempt must be refused by the gate")
    revision_before = d7.helm_revision()
    promotion = d7.promote(rec, "canary-90-10", "candidate-unready")
    failing = [msg for passed, msg in promotion.gate_checks if not passed]
    rec.record(not promotion.gate_ok and not promotion.submitted, f"promotion gate REFUSED the unready candidate and submitted nothing ({len(failing)} blocking check(s)) - reason: {promotion.refusal}")
    rec.record(any("Ready" in m or "readyReplicas" in m or "ready endpoints" in m for m in failing), f"refusal is attributable to readiness: {failing[:4]}")
    rec.record(promotion.paths_ok is None, "refused at the readiness gate, before any path probe was run against the unready candidate")
    revision_after = d7.helm_revision()
    rec.record(revision_before is not None and revision_after == revision_before, f"Helm revision unchanged by the refused promotion ({revision_before} -> {revision_after})")
    route_after = d7.read_route()
    ok, detail = d7.route_is_current(route_after, stable_backends)
    rec.record(
        ok and route_after.uid == route_before.uid and route_after.generation == route_before.generation,
        f"HTTPRoute unchanged (same UID, generation {route_before.generation} -> {None if route_after is None else route_after.generation}): {detail}",
    )
    ident = d7.Identity(stable_msg, unready_values["candidate"]["config"]["appMessage"], d7.pod_names(d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or []), d7.pod_names(d7.list_json("pods", d7.candidate_selector()) or []))
    classes = [d7.classify(s, ident) for s in d7.sample_series(NEGATIVE_EXTERNAL_SAMPLES, "/", 0.1, 60.0)]
    counts = Counter(classes)
    rec.record(counts["stable"] == NEGATIVE_EXTERNAL_SAMPLES == len(classes), f"external traffic stayed 100% stable while the candidate was unready ({dict(counts)} of {NEGATIVE_EXTERNAL_SAMPLES})")
    return not rec.failures()


def body(rec: d7.Recorder, baseline: dict) -> bool:
    if not day7_blue_green.entry_gate(rec, baseline):
        rec.record(False, "entry gate failed - no stage submitted by this experiment")
        return False
    a_ok = phase_a(rec, baseline)
    if not a_ok:
        rec.record(False, "Phase A failed - Phase B (negative scenario) not started")
        return False
    return phase_b(rec, baseline)


def main() -> int:
    rec = d7.Recorder()
    print(f"# {TITLE} (context {kube.CONTEXT}, release {kube.HELM_RELEASE_NAME}, external {kube.GATEWAY_HOST_ADDRESS})")
    try:
        d7.require_day7_profile()
        kube.verify_context()
        baseline = d7.load_strategy_baseline()
    except RuntimeError as exc:
        print(f"FAIL: {exc} - nothing was mutated", file=sys.stderr)
        return 1
    return d7.run_experiment(rec, TITLE, lambda: body(rec, baseline), lambda: d7.restore_or_verify(rec, baseline["stable_message"]))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY7: bounded Blue/Green experiment on the isolated Day 7 cluster.

Blue = the stable gateway (maops-gateway, 3 replicas); Green = the
OPTIONAL candidate (maops-gateway-candidate, a configuration variant of
the same image - see charts/maops-kubernetes-platform/values.yaml).
Traffic moves by changing the ONE Helm-owned HTTPRoute's backendRefs,
never by editing Pods or Services, and every change goes through Helm
with an explicit stage file.

Entry gate: the strategy baseline for this run exists (never
recaptured), the release is at the stable stage, the route is 100%
stable and current, no candidate-only object exists, and an external
request is answered by a stable Pod with the stable message.

Sequence:
  1. green-prepared: candidate enabled, route STILL 100% stable. Proof
     the route did not move: its backendRefs are unchanged and it is
     current; an external sample is 100% stable.
  2. Candidate preflight gate (day7_strategy.evaluate_gate): Deployment
     converged, every candidate Pod Ready, security context and Secret
     mount as intended, ambient-enrolled with ztunnel listeners, ready
     candidate endpoints == candidate Pod IPs, stable endpoints still
     3 and disjoint, route and Gateway current.
  3. Cutover through promote(), which enforces, immediately before
     submission: the gate again, then the mesh/NetworkPolicy path checks
     with the candidate present (candidate -> app allowed; candidate ->
     state denied; app -> candidate denied; controls app -> state
     allowed and app -> stable gateway denied). Any gate or path failure
     - including an unreadable or timed-out probe - submits nothing.
     Every earlier precondition (prepared route identity/generation,
     prepared-stage stable external traffic) also stops the experiment
     before cutover.
  4. After the cutover submission,
     the route generation must advance, backendRefs must be exactly the
     candidate Service, conditions current; external `/` must converge
     to the candidate message served by candidate Pods, and `/backend`
     must return a normal app result through a candidate Pod. Blue stays
     3/3 Ready (the instant-rollback property of Blue/Green).
  5. finally: restoration to stable (day7_strategy.restore_or_verify:
     a Helm upgrade to the stable stage if anything was submitted,
     otherwise read-only verification), proven by `helm get values`,
     candidate-object absence, route state, and external stable
     responses. Reported separately from the primary result.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_strategy as d7
import kube

TITLE = "Day 7 Blue/Green"


def entry_gate(rec: d7.Recorder, baseline: dict) -> bool:
    rec.section("entry gate: 100% stable, no candidate")
    try:
        build = d7.require_baseline_build(baseline)
        expected = d7.expected_release_values("stable", build)
    except RuntimeError as exc:
        return rec.record(False, f"entry: {exc}")
    ok = rec.record(d7.helm_values() == expected, f"release is at the stable stage (helm get values == stable.yaml + build {build.build_id[:12]} image tags)")
    present, unknown = d7.candidate_leftovers()
    ok = rec.record(not present and not unknown, f"no candidate-only object present (present {present}, undetermined {unknown})") and ok
    ok = (d7.wait_route(rec, d7.expected_backends(d7.load_stage_values("stable")), "entry", timeout=10) is not None) and ok
    ok = d7.wait_external(rec, d7.stable_identity(baseline["stable_message"]), "stable", "entry", confirm=5, timeout=20) and ok
    return ok


def body(rec: d7.Recorder, baseline: dict) -> bool:
    if not entry_gate(rec, baseline):
        rec.record(False, "entry gate failed - no stage submitted by this experiment")
        return False
    stable_msg = baseline["stable_message"]
    candidate_msg = d7.load_stage_values("green-prepared")["candidate"]["config"]["appMessage"]

    rec.section("step 1: prepare Green (candidate enabled, route unchanged)")
    route_before = d7.read_route()
    if not d7.apply_stage(rec, "green-prepared"):
        return False
    stable_backends = d7.expected_backends(d7.load_stage_values("green-prepared"))
    route_after = d7.wait_route(rec, stable_backends, "green-prepared")
    if route_after is None:
        return False
    if not rec.record(
        route_before is not None and route_after.uid == route_before.uid and route_after.generation == route_before.generation,
        f"route object and generation unchanged by preparing Green ({None if route_before is None else route_before.generation} -> {route_after.generation}) - traffic still 100% stable",
    ):
        rec.record(False, "prepared route identity/generation not verified - cutover NOT attempted")
        return False
    if not d7.wait_external(rec, d7.stable_identity(stable_msg), "stable", "green-prepared: external traffic", confirm=10, timeout=20):
        rec.record(False, "stable external traffic failed at the prepared stage - cutover NOT attempted")
        return False
    ok = True

    rec.section("step 2: candidate preflight gate")
    gate_ok, checks = d7.run_gate(stable_backends, d7.load_stage_values("green-prepared")["candidate"]["replicas"])
    for passed, msg in checks:
        rec.record(passed, f"gate: {msg}")
    if not gate_ok:
        rec.record(False, "candidate preflight gate FAILED - cutover not attempted")
        return False

    rec.section("step 3+4: promotion - gate re-run, candidate path checks (enforced), then cutover to 100% candidate")
    stable_pods = d7.pod_names(d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or [])
    promotion = d7.promote(rec, "blue-green-cutover", "green-prepared")
    if not promotion.submitted:
        rec.record(False, f"cutover refused - {promotion.refusal}; no Helm upgrade was submitted")
        return False
    if not promotion.applied_ok:
        rec.record(False, "cutover Helm upgrade failed after the gate and path checks passed - restoration follows")
        return False
    cutover_backends = d7.expected_backends(d7.load_stage_values("blue-green-cutover"))
    route_cut = d7.wait_route(rec, cutover_backends, "cutover")
    if route_cut is None:
        return False
    rec.record(route_cut.uid == route_after.uid and route_cut.generation > route_after.generation, f"same HTTPRoute object, generation advanced {route_after.generation} -> {route_cut.generation}")
    candidate_pods = d7.pod_names(d7.list_json("pods", d7.candidate_selector()) or [])
    ident = d7.Identity(stable_msg, candidate_msg, stable_pods, candidate_pods)
    ok = d7.wait_external(rec, ident, "candidate", "cutover: external /", confirm=20) and ok
    ok = d7.wait_external(rec, ident, "candidate", "cutover: external /backend (normal app result via a candidate Pod)", confirm=5, path="/backend") and ok
    blue = d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or []
    ok = rec.record(len([p for p in blue if d7.pod_is_ready(p)]) == d7.STABLE_EXPECTED_REPLICAS, f"Blue (stable) kept warm during cutover: {len([p for p in blue if d7.pod_is_ready(p)])}/{d7.STABLE_EXPECTED_REPLICAS} Ready") and ok
    return ok and not rec.failures()


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

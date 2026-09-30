#!/usr/bin/env python3
"""
DAY7: bounded Recreate experiment - on the isolated CANDIDATE Deployment
only. The stable gateway Deployment keeps RollingUpdate, and the state
StatefulSet is never touched; both are proven unchanged at the end
(generation and UID) by the final check.

Entry gate: as Blue/Green (baseline present, stable stage, 100% stable
route, no candidate objects, external stable response).

Sequence:
  1. recreate-prepared: candidate enabled with strategy.type=Recreate
     (live Deployment must show type Recreate and NO rollingUpdate
     block), route still 100% stable.
  2. promote("recreate-serving"): preflight gate, then the route moves
     to 100% candidate; external `/` must converge to candidate Pods.
  3. Capture the pre-change candidate identity: Deployment UID/
     generation/Pod-template checksum, active ReplicaSet UIDs, Pod UIDs,
     ready endpoints.
     Every item of steps 1-3 is an enforced precondition: a failed
     live-strategy check (candidate exactly Recreate, stable still
     RollingUpdate) stops before promotion; the promotion itself enforces
     the readiness gate AND the candidate path checks; a failed old-Pod /
     ready-endpoint identity, active-ReplicaSet, Helm-revision, or PDB
     coverage check stops before recreate-changed is submitted.
  4. run_observed_upgrade(): a background external prober (`/`, every
     PROBE_INTERVAL s, each request bounded) and a foreground observer
     (candidate Pods + EndpointSlices, every OBSERVE_INTERVAL s) run
     while recreate-changed (ONLY candidate.config.appMessage changes)
     runs as an OWNED `helm upgrade` child writing to private temporary
     files (never an undrained pipe). Its `finally` always stops/joins
     the prober and terminates -> kills -> reaps a still-running Helm
     child - on Popen failure, observer exceptions, deadline expiry or
     Ctrl-C (also during the wait) - so no Helm process can change the
     release once restoration begins; restore_or_verify() additionally
     reaps any registered child first.
  5. Evidence. WHERE THE ORDERING GUARANTEE COMES FROM: the native
     Deployment controller's `strategy.type: Recreate` - it scales the
     old ReplicaSet to zero and waits for the old Pods to terminate
     before the new ReplicaSet creates any Pod. This script does not
     (and cannot) prove that property by polling; it proves the
     preconditions that make the guarantee apply to THIS rollout, then
     reports what bounded observation actually saw:
       - required (revision_evidence, pure): the live Deployment's
         strategy is exactly Recreate (no rollingUpdate block) before
         AND after the rollout; same Deployment UID; generation advanced
         and observed; the Pod-template checksum changed; the
         `deployment.kubernetes.io/revision` annotation advanced; exactly
         one active ReplicaSet afterwards, new (UID not among the old
         ones), carrying the Deployment's new revision and a
         pod-template-hash different from the old ReplicaSet's; every
         old Pod carried the old hash, every remaining Pod carries the
         new hash, and no old Pod UID remains;
       - observed (analyze_recreate + ordering_finding, pure): candidate
         Pods/EndpointSlices sampled every ~OBSERVE_INTERVAL s. If any
         sample shows an old (non-terminal) Pod beside a new one, or old
         and new Pods ready at once, that CONTRADICTS the expected
         Recreate behavior and fails. If none does, the result is
         reported as "no overlap observed in N bounded samples" -
         consistent with the controller guarantee, never presented as
         proof that no overlap could have occurred between samples.
     External samples are summarized as old-message / new-message /
     error runs with timestamps. An interruption is reported if sampled;
     if none was sampled the report SAYS SO - it never fabricates an
     outage and never claims zero downtime from a finite sample. After
     the first new-message response, no old-message response may occur,
     and the final samples must all be the new message (recovery).
  6. No PodDisruptionBudget selects ANY live candidate Pod (full
     matchLabels + matchExpressions evaluation; an unreadable PDB list,
     no candidate Pods, or an unsupported selector fails closed), and
     the output states why that is not "protection" here: a PDB limits
     voluntary evictions via the Eviction API, while the Deployment
     controller's Recreate scale-down does not go through eviction.

finally: restoration to stable, verified and reported separately.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_blue_green
import day7_build
import day7_running_images
import day7_strategy as d7
import kube

TITLE = "Day 7 Recreate (candidate only)"
PROBE_INTERVAL_SECONDS = 0.25
PROBE_TIMEOUT_SECONDS = 2.0
OBSERVE_INTERVAL_SECONDS = 0.5
TRANSITION_DEADLINE_SECONDS = 300.0
POST_SETTLE_SAMPLING_SECONDS = 10.0
RECOVERY_TAIL_SAMPLES = 10


def pod_record(pod: dict, t: float) -> dict:
    meta, status = pod.get("metadata", {}), pod.get("status", {})
    return {
        "t": t,
        "uid": meta.get("uid"),
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": d7.pod_is_ready(pod),
        "terminating": bool(meta.get("deletionTimestamp")),
        "hash": (meta.get("labels") or {}).get("pod-template-hash"),
        "ip": status.get("podIP"),
    }


def analyze_recreate(snapshots: list[dict], old_uids: frozenset) -> dict:
    """Pure. `snapshots`: [{"t", "pods": [pod_record...], "ready_pod_names": set|None}].
    Returns evidence plus a list of violations. An empty list means only
    that no captured sample contradicted Recreate ordering - never that
    no overlap could have occurred between samples (see
    ordering_finding())."""
    violations: list[str] = []
    last_old_seen = first_new_seen = first_new_ready = None
    new_uids: set = set()
    for snap in snapshots:
        t = snap["t"]
        old_live = [p for p in snap["pods"] if p["uid"] in old_uids and p["phase"] not in ("Succeeded", "Failed")]
        new = [p for p in snap["pods"] if p["uid"] not in old_uids]
        new_uids.update(p["uid"] for p in new)
        if any(p["uid"] in old_uids for p in snap["pods"]):
            last_old_seen = t
        if new and first_new_seen is None:
            first_new_seen = t
        if any(p["ready"] for p in new) and first_new_ready is None:
            first_new_ready = t
        if old_live and new:
            violations.append(f"t={t:.2f}s: old Pod(s) {[p['name'] for p in old_live]} coexisted with new Pod(s) {[p['name'] for p in new]}")
        ready_names = snap.get("ready_pod_names")
        if ready_names is not None:
            old_names = {p["name"] for p in snap["pods"] if p["uid"] in old_uids}
            new_names = {p["name"] for p in new}
            if (ready_names & old_names) and (ready_names & new_names):
                violations.append(f"t={t:.2f}s: old and new Pods were ready endpoints simultaneously")
    if last_old_seen is not None and first_new_seen is not None and not last_old_seen < first_new_seen:
        violations.append(f"old Pods last seen at t={last_old_seen:.2f}s, not before the first new Pod at t={first_new_seen:.2f}s")
    return {
        "violations": violations,
        "last_old_seen": last_old_seen,
        "first_new_seen": first_new_seen,
        "first_new_ready": first_new_ready,
        "new_uids": frozenset(new_uids),
        "observations": len(snapshots),
    }


def analyze_interruption(labels: list[tuple[float, str]], tail: int = RECOVERY_TAIL_SAMPLES) -> dict:
    """Pure. `labels`: [(t, "old"|"new"|"error"|"unidentified"), ...] in
    time order. Summarizes runs; flags ordering problems."""
    runs: list[list] = []
    for t, label in labels:
        if runs and runs[-1][0] == label:
            runs[-1][2] = t
            runs[-1][3] += 1
        else:
            runs.append([label, t, t, 1])
    first_new = next((t for t, l in labels if l == "new"), None)
    old_after_new = [t for t, l in labels if l == "old" and first_new is not None and t > first_new]
    errors = [t for t, l in labels if l == "error"]
    tail_labels = [l for _, l in labels[-tail:]]
    return {
        "runs": [tuple(r) for r in runs],
        "errors": len(errors),
        "error_window": (errors[0], errors[-1]) if errors else None,
        "unidentified": sum(1 for _, l in labels if l == "unidentified"),
        "old_after_new": len(old_after_new),
        "first_new": first_new,
        "recovered": len(tail_labels) == tail and all(l == "new" for l in tail_labels),
        "samples": len(labels),
    }


def _candidate_deployment() -> dict | None:
    state, dep = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "deployment", d7.CANDIDATE_DEPLOYMENT)
    return dep if state == "found" else None


REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
HASH_LABEL = "pod-template-hash"


def _owned_replicasets(dep_uid: str) -> list[dict]:
    rss = d7.list_json("replicasets", d7.candidate_selector()) or []
    return [rs for rs in rss if any(o.get("uid") == dep_uid for o in rs.get("metadata", {}).get("ownerReferences", []) or [])]


def _active(rss: list[dict]) -> list[dict]:
    return [rs for rs in rss if (rs.get("spec", {}).get("replicas") or 0) > 0]


def _active_replicasets(dep_uid: str) -> frozenset:
    return frozenset(rs["metadata"]["uid"] for rs in _active(_owned_replicasets(dep_uid)))


def _revision(obj: dict) -> int | None:
    value = (obj.get("metadata", {}).get("annotations") or {}).get(REVISION_ANNOTATION)
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _hash(obj: dict) -> str | None:
    return (obj.get("metadata", {}).get("labels") or {}).get(HASH_LABEL)


def revision_evidence(dep_before: dict, dep_after: dict, rs_before: list[dict], rs_after: list[dict], old_pods: list[dict], pods_after: list[dict]) -> list[tuple[bool, str]]:
    """Pure. The facts that make the Deployment controller's Recreate
    ordering guarantee apply to THIS rollout - see the module docstring.
    Every returned finding must hold."""
    out: list[tuple[bool, str]] = []
    for label, dep in (("before", dep_before), ("after", dep_after)):
        strategy = dep.get("spec", {}).get("strategy", {})
        out.append((strategy == {"type": "Recreate"}, f"live strategy {label} the rollout is exactly Recreate with no rollingUpdate block ({strategy})"))
    out.append((dep_after.get("metadata", {}).get("uid") == dep_before.get("metadata", {}).get("uid"), "same Deployment object (UID unchanged) - rolled, not recreated"))
    gen_b, gen_a = dep_before.get("metadata", {}).get("generation"), dep_after.get("metadata", {}).get("generation")
    out.append((isinstance(gen_a, int) and isinstance(gen_b, int) and gen_a > gen_b and dep_after.get("status", {}).get("observedGeneration") == gen_a, f"generation {gen_b} -> {gen_a}, observedGeneration {dep_after.get('status', {}).get('observedGeneration')}"))
    ck = lambda d: (d.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get("checksum/config")  # noqa: E731
    out.append((bool(ck(dep_after)) and ck(dep_after) != ck(dep_before), f"Pod-template checksum changed ({str(ck(dep_before))[:12]} -> {str(ck(dep_after))[:12]})"))
    rev_b, rev_a = _revision(dep_before), _revision(dep_after)
    out.append((rev_b is not None and rev_a is not None and rev_a > rev_b, f"Deployment {REVISION_ANNOTATION} advanced ({rev_b} -> {rev_a})"))
    old_active = _active(rs_before)
    new_active = _active(rs_after)
    old_uids = {rs["metadata"]["uid"] for rs in old_active}
    old_hashes = {_hash(rs) for rs in old_active}
    out.append((len(old_active) == 1, f"exactly one active ReplicaSet before the rollout ({len(old_active)})"))
    out.append((len(new_active) == 1 and new_active[0]["metadata"]["uid"] not in old_uids, f"exactly one active ReplicaSet after, and it is new ({[rs['metadata']['uid'] for rs in new_active]})"))
    if len(new_active) == 1:
        new_rs = new_active[0]
        out.append((_revision(new_rs) == rev_a, f"new ReplicaSet carries the Deployment's new revision ({_revision(new_rs)} vs {rev_a})"))
        out.append((_hash(new_rs) is not None and _hash(new_rs) not in old_hashes, f"new pod-template-hash {_hash(new_rs)} differs from the old {sorted(h for h in old_hashes if h)}"))
        out.append((bool(old_pods) and all(_hash(p) in old_hashes for p in old_pods), "every pre-rollout Pod carried the old pod-template-hash"))
        out.append((bool(pods_after) and all(_hash(p) == _hash(new_rs) for p in pods_after), "every remaining Pod carries the new pod-template-hash"))
    old_pod_uids = {p["metadata"]["uid"] for p in old_pods}
    remaining = sorted(old_pod_uids & {p["metadata"]["uid"] for p in pods_after})
    out.append((not remaining, f"no pre-rollout Pod UID remains ({remaining})"))
    return out


def ordering_finding(evidence: dict, interval: float) -> tuple[bool, str]:
    """Pure. Truthful wording for what bounded observation shows."""
    n = evidence["observations"]
    if evidence["violations"]:
        return False, f"observation CONTRADICTS the expected Recreate ordering: {evidence['violations'][:3]}"
    if n == 0:
        return False, "no Pod/endpoint observation was captured during the rollout - nothing was observed"
    return True, (
        f"no old/new candidate Pod overlap and no simultaneous old/new ready endpoints observed in {n} bounded samples "
        f"(~{interval}s apart) - consistent with the Deployment controller's Recreate ordering (the guarantee comes "
        "from strategy.type=Recreate verified above; sampling cannot exclude a shorter overlap between samples)"
    )


PDB_NOT_PROTECTION = (
    "a PDB is NOT protection against this rollout: PDBs limit voluntary evictions made through the Eviction API; "
    "the Deployment controller's Recreate strategy scales the old ReplicaSet to zero directly, so even a PDB "
    "selecting these Pods would not keep one running"
)
_SUPPORTED_OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")


def selector_selects(selector, labels: dict) -> bool:
    """Pure Kubernetes LabelSelector evaluation (matchLabels AND every
    matchExpression). policy/v1 semantics: a missing/empty selector
    selects every Pod in the namespace. Raises ValueError for anything
    unreadable or unsupported - callers fail closed on it."""
    if selector is None:
        return True
    if not isinstance(selector, dict):
        raise ValueError(f"selector is not an object: {selector!r}")
    unknown = set(selector) - {"matchLabels", "matchExpressions"}
    if unknown:
        raise ValueError(f"unsupported selector fields {sorted(unknown)}")
    match_labels = selector.get("matchLabels") or {}
    if not isinstance(match_labels, dict):
        raise ValueError(f"matchLabels is not an object: {match_labels!r}")
    expressions = selector.get("matchExpressions") or []
    if not isinstance(expressions, list):
        raise ValueError(f"matchExpressions is not a list: {expressions!r}")
    # Validate EVERY expression before any early "does not match" return,
    # so an unsupported selector fails closed regardless of the Pod's
    # labels (never silently skipped because matchLabels already missed).
    for expr in expressions:
        if not isinstance(expr, dict) or expr.get("operator") not in _SUPPORTED_OPERATORS or not isinstance(expr.get("key"), str):
            raise ValueError(f"unsupported matchExpression {expr!r}")
        values = expr.get("values") or []
        if expr["operator"] in ("In", "NotIn") and (not isinstance(values, list) or not values):
            raise ValueError(f"{expr['operator']} requires a non-empty values list: {expr!r}")
    if any(labels.get(k) != v for k, v in match_labels.items()):
        return False
    for expr in expressions:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        present = key in labels
        if op == "In" and not (present and labels[key] in values):
            return False
        if op == "NotIn" and present and labels[key] in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
    return True


def pdbs_selecting(pdbs: list[dict], pods: list[dict]) -> tuple[list[str], list[str]]:
    """Pure. (names of PDBs selecting ANY of `pods`, evaluation errors)."""
    selecting, errors = [], []
    for pdb in pdbs:
        name = (pdb.get("metadata") or {}).get("name", "?")
        try:
            if any(selector_selects((pdb.get("spec") or {}).get("selector"), (pod.get("metadata") or {}).get("labels") or {}) for pod in pods):
                selecting.append(name)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
    return selecting, errors


def no_pdb_selects_candidate(rec: d7.Recorder) -> bool:
    """Fail closed: unreadable PDBs, no candidate Pods to evaluate, or an
    unsupported selector are failures - never 'no PDB selects them'.
    Every live candidate Pod is evaluated, not just the first."""
    pdbs = d7.list_json("pdb")
    cand_pods = d7.list_json("pods", d7.candidate_selector())
    if pdbs is None or cand_pods is None:
        return rec.record(False, "PDB coverage of candidate Pods could not be read - failing closed")
    if not cand_pods:
        return rec.record(False, "no candidate Pods to evaluate PDB coverage against - failing closed")
    selecting, errors = pdbs_selecting(pdbs, cand_pods)
    ok = rec.record(not selecting and not errors, f"no PodDisruptionBudget selects any of the {len(cand_pods)} candidate Pods (selecting: {selecting}; unevaluable: {errors})")
    rec.informational(PDB_NOT_PROTECTION)
    return ok


HELM_EXIT_GRACE_SECONDS = 60.0


@dataclass
class UpgradeObservation:
    returncode: int | None
    child_outcome: str
    stderr_tail: str
    samples: list
    snapshots: list
    settled_at: float | None


def run_observed_upgrade(
    cmd: list[str],
    old_uids: frozenset,
    replicas: int,
    popen=subprocess.Popen,
    deadline_seconds: float = TRANSITION_DEADLINE_SECONDS,
    helm_grace_seconds: float = HELM_EXIT_GRACE_SECONDS,
    observe_interval: float = OBSERVE_INTERVAL_SECONDS,
    post_settle_seconds: float = POST_SETTLE_SAMPLING_SECONDS,
) -> UpgradeObservation:
    """Runs the recreate-changed Helm upgrade as an OWNED child while the
    external prober and the Pod/endpoint observer run, under one
    lifecycle whose `finally` ALWAYS: stops and joins the prober, and
    terminates -> kills -> reaps a still-running Helm child (also on a
    Popen failure, an observation exception, deadline expiry, or Ctrl-C,
    including Ctrl-C while waiting for the child). Helm's output goes to
    private anonymous temporary files - never an undrained pipe that could
    block it - and only a bounded stderr tail is ever reported (Helm
    upgrade output carries no Secret values; none are read here). The
    child is reaped before this function returns, so it can never touch
    the release after restoration starts."""
    # Guard at the call site, not only in main(): nothing below (prober,
    # Helm child) may start outside the Day 7 profile.
    d7.require_day7_profile()
    prober = d7.Prober(path="/", interval=PROBE_INTERVAL_SECONDS, timeout=PROBE_TIMEOUT_SECONDS, max_seconds=deadline_seconds + helm_grace_seconds + 60)
    snapshots: list[dict] = []
    samples: list = []
    settled_at = None
    helm = None
    child_outcome = "not started"
    stderr_tail = ""
    out_sink = tempfile.TemporaryFile()
    err_sink = tempfile.TemporaryFile()
    try:
        prober.start()
        t0 = prober.t0
        d7.SUBMITTED_STAGES.append("recreate-changed")
        helm = popen(cmd, stdin=subprocess.DEVNULL, stdout=out_sink, stderr=err_sink)
        d7.OWNED_CHILDREN.append(helm)
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            now = time.monotonic() - t0
            pods = d7.list_json("pods", d7.candidate_selector())
            ep = d7.read_endpoints(d7.CANDIDATE_SERVICE)
            if pods is not None:
                snapshots.append({"t": now, "pods": [pod_record(p, now) for p in pods], "ready_pod_names": None if ep is None else set(ep.ready_target_pods)})
                new_ready = [p for p in pods if p["metadata"]["uid"] not in old_uids and d7.pod_is_ready(p)]
                if helm.poll() is not None and len(new_ready) == replicas and not any(p["metadata"]["uid"] in old_uids for p in pods):
                    settled_at = now
                    break
            time.sleep(observe_interval)
        if settled_at is not None:
            time.sleep(post_settle_seconds)
        if helm.poll() is None:
            try:
                helm.wait(timeout=helm_grace_seconds)
            except subprocess.TimeoutExpired:
                pass  # reaped (terminate -> kill) in finally
    finally:
        try:
            samples = prober.stop()
        finally:
            try:
                if helm is not None:
                    child_outcome = d7.reap_child(helm)
            finally:
                stderr_tail = d7.read_tail(err_sink)
                out_sink.close()
                err_sink.close()
    return UpgradeObservation(helm.returncode if helm is not None else None, child_outcome, stderr_tail, samples, snapshots, settled_at)


def body(rec: d7.Recorder, baseline: dict) -> bool:
    if not day7_blue_green.entry_gate(rec, baseline):
        rec.record(False, "entry gate failed - no stage submitted by this experiment")
        return False
    stable_msg = baseline["stable_message"]
    old_msg = d7.load_stage_values("recreate-serving")["candidate"]["config"]["appMessage"]
    new_msg = d7.load_stage_values("recreate-changed")["candidate"]["config"]["appMessage"]
    replicas = d7.load_stage_values("recreate-changed")["candidate"]["replicas"]

    rec.section("step 1: candidate with strategy Recreate (route unchanged)")
    if not d7.apply_stage(rec, "recreate-prepared"):
        return False
    if d7.wait_route(rec, d7.expected_backends(d7.load_stage_values("recreate-prepared")), "recreate-prepared") is None:
        return False
    dep = _candidate_deployment() or {}
    strategy = dep.get("spec", {}).get("strategy", {})
    if not rec.record(strategy == {"type": "Recreate"}, f"live candidate Deployment strategy is exactly Recreate with no rollingUpdate block ({strategy})"):
        return False
    state, stable_dep = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "deployment", d7.STABLE_DEPLOYMENT)
    stable_strategy = ((stable_dep or {}).get("spec", {}).get("strategy") or {}).get("type")
    if not rec.record(state == "found" and stable_strategy == "RollingUpdate", f"stable maops-gateway Deployment still uses RollingUpdate (found {stable_strategy!r}, read {state})"):
        rec.record(False, "stable strategy not verified - candidate promotion NOT attempted")
        return False

    rec.section("step 2: gate + enforced path checks, then candidate serves 100% of external traffic")
    promotion = d7.promote(rec, "recreate-serving", "recreate-prepared")
    for passed, msg in promotion.gate_checks:
        rec.record(passed, f"gate: {msg}")
    if not promotion.submitted:
        rec.record(False, f"Recreate serving stage refused - {promotion.refusal}; no Helm stage submitted")
        return False
    if not promotion.applied_ok:
        rec.record(False, "Recreate serving stage Helm upgrade failed")
        return False
    if d7.wait_route(rec, d7.expected_backends(d7.load_stage_values("recreate-serving")), "recreate-serving") is None:
        return False
    stable_pods = d7.pod_names(d7.list_json("pods", kube.GATEWAY_LABEL_SELECTOR) or [])
    before_pods = d7.list_json("pods", d7.candidate_selector())
    if before_pods is None:
        return rec.record(False, "candidate Pods unreadable after promotion - recreate-changed NOT submitted")
    if not d7.wait_external(rec, d7.Identity(stable_msg, old_msg, stable_pods, d7.pod_names(before_pods)), "candidate", "recreate-serving: external /", confirm=10):
        return False

    rec.section("step 3: pre-change candidate identity (every item must hold before the upgrade)")
    dep = _candidate_deployment()
    if dep is None:
        return rec.record(False, "candidate Deployment unreadable before the change - recreate-changed NOT submitted")
    dep_before = dep
    dep_uid = dep["metadata"]["uid"]
    gen_before = dep["metadata"]["generation"]
    rs_before_objs = _owned_replicasets(dep_uid)
    rs_before = frozenset(rs["metadata"]["uid"] for rs in _active(rs_before_objs))
    old_uids = frozenset(p["metadata"]["uid"] for p in before_pods)
    ready_before = d7.read_endpoints(d7.CANDIDATE_SERVICE)
    ready_pods = {p["metadata"]["name"] for p in before_pods if d7.pod_is_ready(p)}
    preconditions = [
        rec.record(
            len(old_uids) == replicas and len(ready_pods) == replicas and ready_before is not None
            and len(ready_before.ready_addresses) == replicas and set(ready_before.ready_target_pods) == ready_pods,
            f"pre-change: {len(old_uids)} candidate Pods ({len(ready_pods)} Ready), ready endpoints "
            f"{None if ready_before is None else sorted(ready_before.ready_target_pods)} (expected exactly these {replicas} Pods), generation {gen_before}",
        ),
        rec.record(len(rs_before) == 1, f"exactly one active candidate ReplicaSet before the change ({sorted(rs_before)})"),
    ]
    revision_before = d7.helm_revision()
    preconditions.append(rec.record(revision_before is not None, f"Helm revision readable before the change ({revision_before})"))
    preconditions.append(no_pdb_selects_candidate(rec))
    try:
        build = d7.active_build()
        expected_values = d7.expected_release_values("recreate-changed", build)
        preconditions.append(rec.record(True, f"verified build {build.build_id[:12]} carried into recreate-changed"))
    except day7_build.BuildError as exc:
        preconditions.append(rec.record(False, f"verified build unreadable ({exc})"))
    if not all(preconditions):
        rec.record(False, "pre-change precondition(s) failed - recreate-changed NOT submitted")
        return False

    rec.section("step 4: change ONLY the candidate message under Recreate, observing traffic and Pods")
    cmd = d7.helm_stage_command("recreate-changed", build)
    print(f"  $ {' '.join(cmd)}", flush=True)
    run = run_observed_upgrade(cmd, old_uids, replicas)
    ok = rec.record(run.child_outcome.startswith("already exited"), f"helm upgrade child exited on its own before being reaped ({run.child_outcome})")
    ok = rec.record(run.returncode == 0, f"helm upgrade recreate-changed exit {run.returncode}" + ("" if run.returncode == 0 else f" (stderr tail: {run.stderr_tail.strip()[-400:]})")) and ok
    samples, snapshots, settled_at = run.samples, run.snapshots, run.settled_at
    ok = rec.record(settled_at is not None, f"Recreate rollout settled within {TRANSITION_DEADLINE_SECONDS:.0f}s" + (f" (at t={settled_at:.1f}s)" if settled_at else "")) and ok
    revision_after = d7.helm_revision()
    ok = rec.record(revision_before is not None and revision_after == revision_before + 1, f"Helm revision {revision_before} -> {revision_after}") and ok
    ok = rec.record(d7.helm_values() == expected_values, "`helm get values` equals recreate-changed.yaml + the build's image tags exactly") and ok
    ok = day7_running_images.wait_for_running_images(rec, build, components=("candidate",), counts={"candidate": replicas}, label="replacement candidate images") and ok

    rec.section("step 5: controller and ordering evidence")
    dep_after = _candidate_deployment() or {}
    pods_after = d7.list_json("pods", d7.candidate_selector()) or []
    for passed, msg in revision_evidence(dep_before, dep_after, rs_before_objs, _owned_replicasets(dep_uid), before_pods, pods_after):
        ok = rec.record(passed, f"controller evidence: {msg}") and ok
    ok = rec.record(len([p for p in pods_after if d7.pod_is_ready(p)]) == replicas, f"{replicas} new candidate Pods Ready") and ok
    evidence = analyze_recreate(snapshots, old_uids)
    rec.informational(f"{evidence['observations']} Pod/endpoint observations; old last seen t={evidence['last_old_seen']}, first new seen t={evidence['first_new_seen']}, first new Ready t={evidence['first_new_ready']}")
    passed, msg = ordering_finding(evidence, OBSERVE_INTERVAL_SECONDS)
    ok = rec.record(passed, f"observed ordering: {msg}") and ok
    if evidence["last_old_seen"] is None or evidence["first_new_seen"] is None:
        rec.informational("the observer did not see both the old and the new Pod generation - no before/after timing is reported; the ordering claim rests on the controller evidence only")

    rec.section("step 6: external traffic during the transition")
    labels = [(s.t, d7.classify_by_message(s, {"old": old_msg, "new": new_msg})) for s in samples]
    summary = analyze_interruption(labels)
    rec.informational(f"{summary['samples']} external samples every ~{PROBE_INTERVAL_SECONDS}s (each bounded at {PROBE_TIMEOUT_SECONDS}s); runs: " + ", ".join(f"{l} x{n} [{a:.1f}s-{b:.1f}s]" for l, a, b, n in summary["runs"]))
    if summary["errors"]:
        a, b = summary["error_window"]
        rec.informational(f"OBSERVED interruption: {summary['errors']} failed external request(s) between t={a:.1f}s and t={b:.1f}s, then recovery to the new message - the planned Recreate outage")
    else:
        rec.informational("NO interruption was sampled. This does not show zero downtime - the outage window may have fallen between samples.")
    ok = rec.record(summary["unidentified"] == 0, f"every successful response was the old or the new candidate message (unidentified: {summary['unidentified']})") and ok
    ok = rec.record(summary["old_after_new"] == 0, f"no old-message response after the first new-message response ({summary['old_after_new']})") and ok
    ok = rec.record(summary["recovered"], f"recovered: the last {RECOVERY_TAIL_SAMPLES} samples were all the new message") and ok
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

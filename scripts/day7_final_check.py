#!/usr/bin/env python3
"""
DAY7: final restored-state gate for the Day 7 run - verifies, never
assumes, that every experiment left the isolated Day 7 cluster in the
stable state captured by `make day7-baseline` (loaded read-only; never
recaptured).

Checks (every one recorded; any failure fails the run):

  Helm
    - release deployed; `helm get values` equals helm-values/day7/
      stable.yaml plus exactly the run baseline's build image tags (the
      baseline's build must equal the current verified build, or the
      baseline is refused); `helm get values --all` has candidate.enabled=false,
      routing.mode=stable, and no route weights;
    - `helm get manifest` contains no candidate-only object, and its
      sha256 equals the baseline's (same values + same chart => the
      deployed manifest is byte-identical to the pre-experiment one).
  Live objects
    - no candidate-only object of any kind remains (explicit absence,
      API errors are failures, not absence);
    - maops-gateway 3/3, maops-app 3/3, maops-state 1/1 Ready, and the
      stable gateway/app Deployments and the state StatefulSet keep
      their baseline UIDs AND generations (no experiment ever changed
      their Pod templates or recreated them); stable Services/PDBs keep
      their baseline UIDs;
    - the HTTPRoute keeps its baseline UID, routes 100% to
      maops-gateway:8080, and is Accepted/ResolvedRefs for its current
      generation; the Gateway is Accepted/Programmed for its current
      generation;
    - storage: namespace, PVC and PV UIDs equal the baseline;
    - running images: every stable gateway/app/state container runs the
      run's verified build and no candidate Pod exists
      (day7_running_images - imageID mapped to the node's config digest).
  External
    - `/` answered only by stable Pods with the baseline stable message
      (bounded sample), `/backend` returns a normal app result, and a
      wrong Host gets HTTP 404.
  Leaks and identity (DAY7 remediation: checks what Day 7 actually
  creates)
    - no leftover NetworkPolicy probe Pods (maops-platform), no Pod at
      all left in the Day 7 validation namespace (maops-day7-validation
      holds only transient netpol/RBAC probe Pods), no Day 7 mesh-probe
      namespace, no Day 7 storage bootstrap/hardening scratch namespace;
    - no namespace named for an earlier day and no object anywhere
      labelled with an earlier release instance (e.g.
      maops-kubernetes-platform-day6) - the Day 7 cluster carries only
      Day 7 identities;
    - no kubectl port-forward or helm process for the Day 7
      context/release.

Older kind clusters are deliberately NOT checked here (never listed,
never contacted): this operational gate passes on a healthy Day 7
cluster whether they run, are stopped, or are absent. `make
day7-history-audit` is the separate, optional history audit.

Cilium/Istio health, per-Pod ambient listeners, NetworkPolicy and mesh
behavior, the `/state` VALUE and the Day 1-6 clusters' existence are
proven by the Makefile targets run immediately before this one in `make
day7-check` (cni-status, mesh-status, ambient-workload-check,
networkpolicy-check, mesh-check, final-state-check) against the same
run baseline; the run fails if any of them fails.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import day7_running_images
import day7_strategy as d7
import final_state_check
import k8s_yaml
import kube

EXTERNAL_SAMPLES = 20
EARLIER_INSTANCES = tuple(f"maops-kubernetes-platform-day{n}" for n in range(1, 7))
NAMESPACED_IDENTITY_KINDS = "deployments,statefulsets,replicasets,pods,services,configmaps,serviceaccounts,networkpolicies,roles,rolebindings,gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io"
CLUSTER_IDENTITY_KINDS = "namespaces,ciliumclusterwidenetworkpolicies.cilium.io"


def manifest_candidate_objects(manifest_text: str) -> list[str]:
    """Pure: names of any candidate-only object in a rendered manifest."""
    found = []
    for doc in k8s_yaml.load_all(manifest_text or ""):
        if not isinstance(doc, dict):
            continue
        meta = doc.get("metadata", {}) or {}
        labels = meta.get("labels") or {}
        name = meta.get("name", "")
        if labels.get("app.kubernetes.io/component") == d7.CANDIDATE_COMPONENT or "candidate" in name:
            found.append(f"{doc.get('kind')}/{name}")
    return found


def manifest_route_backends(manifest_text: str) -> list:
    for doc in k8s_yaml.load_all(manifest_text or ""):
        if isinstance(doc, dict) and doc.get("kind") == "HTTPRoute":
            return [r for rule in doc.get("spec", {}).get("rules", []) for r in rule.get("backendRefs", [])]
    return []


def leaked_processes(ps_output: str) -> list[str]:
    """Pure: kubectl port-forward or helm processes aimed at the Day 7
    context/release."""
    leaked = []
    for line in ps_output.splitlines():
        fields = line.split()
        if len(fields) < 2 or kube.CONTEXT not in line:
            continue
        program = Path(fields[1]).name
        if program == "kubectl" and "port-forward" in fields:
            leaked.append(line.strip())
        elif program == "helm" and kube.HELM_RELEASE_NAME in fields:
            leaked.append(line.strip())
    return leaked


def stale_namespaces(names: list[str]) -> list[str]:
    """Pure: namespaces whose NAME belongs to an earlier day."""
    return sorted(n for n in names if any(f"day{d}" in n for d in range(1, 7)))


def check_day7_identities(rec: d7.Recorder) -> None:
    """No earlier-day namespace name and no earlier-day instance label
    anywhere in the Day 7 cluster. An API error is a failure, never
    'nothing found'."""
    try:
        ns = d7._kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}", check=False, timeout=15.0)
    except subprocess.TimeoutExpired:
        ns = None
    if ns is None or ns.returncode != 0:
        rec.record(False, "could not list namespaces to check for earlier-day identities")
    else:
        stale = stale_namespaces(ns.stdout.split())
        rec.record(not stale, f"no namespace named for an earlier day (found {stale})")
    for instance in EARLIER_INSTANCES:
        found = []
        for args in (("get", CLUSTER_IDENTITY_KINDS), ("get", NAMESPACED_IDENTITY_KINDS, "-A")):
            try:
                result = d7._kubectl(*args, "-l", f"app.kubernetes.io/instance={instance}", "-o", "name", "--ignore-not-found", check=False, timeout=30.0)
            except subprocess.TimeoutExpired:
                result = None
            if result is None or result.returncode != 0:
                rec.record(False, f"could not query objects labelled instance={instance}")
                break
            found += [l for l in result.stdout.splitlines() if l.strip()]
        else:
            rec.record(not found, f"no object labelled app.kubernetes.io/instance={instance} ({found})")


def check_helm(rec: d7.Recorder, baseline: dict) -> None:
    status = d7.helm_status() or {}
    rec.record((status.get("info") or {}).get("status") == "deployed", f"Helm release {kube.HELM_RELEASE_NAME} deployed (revision {status.get('version')}, baseline revision {baseline['helm']['revision']})")
    build = day7_build.build_from_record(baseline["build"])
    values = d7.helm_values()
    rec.record(values == d7.expected_release_values("stable", build), f"`helm get values` equals helm-values/day7/stable.yaml + the run's build {build.build_id[:12]} image tags (found {values!r})")
    all_values = d7.helm_values(all_values=True) or {}
    routing = all_values.get("routing", {})
    rec.record(
        (all_values.get("candidate") or {}).get("enabled") is False and routing.get("mode") == "stable" and "stableWeight" not in routing and "candidateWeight" not in routing,
        f"effective values: candidate.enabled={(all_values.get('candidate') or {}).get('enabled')!r}, routing.mode={routing.get('mode')!r}, weights absent={('stableWeight' not in routing and 'candidateWeight' not in routing)}",
    )
    manifest = d7.helm_manifest()
    if manifest is None:
        rec.record(False, "Helm manifest unreadable")
        return
    leftovers = manifest_candidate_objects(manifest)
    rec.record(not leftovers, f"Helm's deployed manifest has no candidate-only object ({leftovers})")
    backends = manifest_route_backends(manifest)
    rec.record(backends == [{"name": d7.STABLE_SERVICE, "port": d7.BACKEND_PORT}], f"Helm's deployed HTTPRoute has one unweighted stable backend ({backends})")
    digest = d7.sha256_text(manifest)
    rec.record(digest == baseline["helm"]["manifest_sha256"], f"deployed manifest sha256 equals the pre-experiment baseline ({digest[:16]}... vs {baseline['helm']['manifest_sha256'][:16]}...)")


def check_live(rec: d7.Recorder, baseline: dict) -> None:
    present, unknown = d7.candidate_leftovers()
    rec.record(not present and not unknown, f"no candidate-only object remains in the cluster (present {present}, undetermined {unknown})")
    for kind, name, expected in (("deployment", kube.GATEWAY_DEPLOYMENT, 3), ("deployment", kube.APP_DEPLOYMENT, 3), ("statefulset", kube.STATE_STATEFULSET, 1)):
        state, obj = d7.get_json_or_none("-n", kube.NAMESPACE, "get", kind, name)
        meta, status = (obj or {}).get("metadata", {}), (obj or {}).get("status", {})
        base = baseline["workloads"][name]
        rec.record(state == "found" and status.get("readyReplicas") == expected and (obj or {}).get("spec", {}).get("replicas") == expected, f"{name} ready {status.get('readyReplicas')}/{expected}")
        rec.record(meta.get("uid") == base["uid"] and meta.get("generation") == base["generation"], f"{name} UID and generation unchanged since baseline (generation {base['generation']} -> {meta.get('generation')}) - never recreated or re-templated by an experiment")
    for name, uid in baseline["services"].items():
        state, obj = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "service", name)
        rec.record(state == "found" and (obj or {}).get("metadata", {}).get("uid") == uid, f"Service {name} UID unchanged")
    for name, uid in baseline["pdbs"].items():
        state, obj = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "pdb", name)
        rec.record(state == "found" and (obj or {}).get("metadata", {}).get("uid") == uid, f"PDB {name} UID unchanged")
    route = d7.read_route()
    ok, detail = d7.route_is_current(route, d7.expected_backends(d7.load_stage_values("stable")))
    rec.record(ok and route.uid == baseline["route"]["uid"], f"HTTPRoute (baseline UID kept) is 100% stable and current: {detail}")
    gw_ok, gw_detail = d7.gateway_is_current()
    rec.record(gw_ok, gw_detail)
    storage = baseline["storage"]
    ns_state, ns = d7.get_json_or_none("get", "namespace", kube.NAMESPACE)
    pvc_state, pvc = d7.get_json_or_none("-n", kube.NAMESPACE, "get", "pvc", "data-maops-state-0")
    pv_state, pv = d7.get_json_or_none("get", "pv", storage["pv_name"])
    rec.record((ns or {}).get("metadata", {}).get("uid") == storage["namespace_uid"], "namespace UID unchanged")
    rec.record((pvc or {}).get("metadata", {}).get("uid") == storage["pvc_uid"] and (pvc or {}).get("spec", {}).get("volumeName") == storage["pv_name"], "state PVC UID and bound PV unchanged")
    rec.record((pv or {}).get("metadata", {}).get("uid") == storage["pv_uid"], "state PV UID unchanged")


def check_external(rec: d7.Recorder, baseline: dict) -> None:
    ident = d7.stable_identity(baseline["stable_message"])
    classes = [d7.classify(s, ident) for s in d7.sample_series(EXTERNAL_SAMPLES, "/", 0.1, 60.0)]
    counts = Counter(classes)
    rec.record(counts["stable"] == EXTERNAL_SAMPLES == len(classes), f"external / answered only by stable Pods with the baseline stable message ({dict(counts)})")
    backend = [d7.classify(s, ident) for s in d7.sample_series(5, "/backend", 0.1, 30.0)]
    rec.record(backend == ["stable"] * 5, f"external /backend returns normal app results via stable Pods ({backend})")
    wrong = d7.external_get("/", host="wrong.invalid.example")
    rec.record(wrong.status == 404, f"wrong Host receives a definite no-route 404 (got {wrong.outcome}/{wrong.status})")


def check_leaks(rec: d7.Recorder) -> None:
    final_state_check.results = []
    # final_state_check's own record() prints each result; silence that so
    # each fact is printed exactly once, by rec.record() below (the run
    # dd99769f... log showed these two lines printed twice - the count of
    # distinct assertions was already correct, the output was not).
    with contextlib.redirect_stdout(io.StringIO()):
        final_state_check.check_no_leaked_networkpolicy_probe_pods()
        final_state_check.check_no_leaked_mesh_probe_namespace()
    for ok, msg in final_state_check.results:
        rec.record(ok, msg)
    pods = d7.list_json("pods", namespace=kube.VALIDATION_NAMESPACE)
    rec.record(pods == [], f"no Pod left in the Day 7 validation namespace {kube.VALIDATION_NAMESPACE!r} ({None if pods is None else [p['metadata']['name'] for p in pods]})")
    for namespace in (kube.STORAGE_BOOTSTRAP_VERIFY_NAMESPACE, kube.STORAGE_HARDENING_NAMESPACE):
        state, _ = d7.get_json_or_none("get", "namespace", namespace)
        rec.record(state == "not_found", f"storage scratch namespace {namespace!r} explicitly absent ({state})")
    try:
        ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        rec.record(False, f"could not list processes: {exc}")
        return
    leaked = leaked_processes(ps)
    rec.record(not leaked, f"no leaked kubectl port-forward/helm process for {kube.CONTEXT} ({leaked})")


def main() -> int:
    rec = d7.Recorder()
    print(f"# Day 7 final restored-state check (context {kube.CONTEXT}, release {kube.HELM_RELEASE_NAME})")
    try:
        d7.require_day7_profile()
        kube.verify_context()
        baseline = d7.load_strategy_baseline()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    rec.section("Helm release state")
    check_helm(rec, baseline)
    rec.section("live cluster state")
    check_live(rec, baseline)
    rec.section("running images (verified build)")
    day7_running_images.wait_for_running_images(rec, day7_build.build_from_record(baseline["build"]), label="running images")
    rec.section("external behavior")
    check_external(rec, baseline)
    rec.section("leaks and Day 7 identity")
    check_leaks(rec)
    check_day7_identities(rec)
    failures = rec.failures()
    print()
    print(f"{len(rec.results) - len(failures)}/{len(rec.results)} Day 7 final checks passed")
    if failures:
        print(f"FAIL: {len(failures)} final restoration assertion(s) failed - the Day 7 run is NOT restored", file=sys.stderr)
        return 1
    print("PASS: Day 7 stable state verified against the run baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

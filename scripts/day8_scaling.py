#!/usr/bin/env python3
"""
DAY8: the bounded live autoscaling demonstrations, in the temporary
`maops-day8-scaling` namespace of maops-k8s-day7 - one phase per
subcommand, each recording its observations (every sample, every
replica transition, every check) to a NEW timestamped file in the run's
private directory, so a failed attempt is kept, never overwritten.

  guards   Namespace (Pod Security restricted) + LimitRange + ResourceQuota
           (= the computed worst-case budget), applied before anything else.
  quota    in-budget Pod admitted with LimitRange defaults; a container
           above the LimitRange max rejected; a Pod above the whole quota
           rejected (each by its own admission message, nothing created).
  hpa      CPU HPA (1..4, 50%): metrics flowing first -> bounded load Job
           -> observed Ready scale-out -> load ends -> observed scale-in.
  vpa      VPA Off: a CPU/memory recommendation, running Pod untouched ->
           VPA Initial: ONE new Pod gets the bounded recommendation at
           admission, the existing Pod is still untouched.
  keda     Redis list + ScaledObject (0..3): KEDA's managed HPA, queue
           activity, activation 0 -> 1, scale-out, drain (every item
           processed), scale-down to 0.
  cleanup  deletes ONLY the labelled scaling namespace and proves it gone.

Every phase also fails if the namespace recorded a quota-blocked Pod
creation (an expected scale-out blocked by the budget is a failure),
and every scaling Pod must run the run's pinned scaling image, proven
through the node's containerd record.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_running_images
import day8_common as common
import day8_image
import day8_objects as o

NS = o.NAMESPACE
APPLY_TIMEOUT = 60.0
CLEANUP_RESOURCES = ("horizontalpodautoscalers", "verticalpodautoscalers.autoscaling.k8s.io", "scaledobjects.keda.sh", "resourcequotas", "limitranges")
KEDA_SCOPED_BINDING = "keda-operator"
KEDA_OBJECT_KINDS = ("scaledobjects.keda.sh", "scaledjobs.keda.sh", "triggerauthentications.keda.sh")
KEDA_RELEASE = "keda"
KEDA_NAMESPACE = o.KEDA_NAMESPACE
KEDA_UNINSTALL_TIMEOUT_SECONDS = 300
# The six CRDs chart keda-2.21.0 owns (no resource-policy keep): `helm
# uninstall` deletes them and with them every instance, Day 8's or not.
KEDA_CRDS = (
    "cloudeventsources.eventing.keda.sh",
    "clustercloudeventsources.eventing.keda.sh",
    "clustertriggerauthentications.keda.sh",
    "scaledjobs.keda.sh",
    "scaledobjects.keda.sh",
    "triggerauthentications.keda.sh",
)
# Objects the KEDA operator creates at runtime (not Helm-owned): its
# self-signed webhook/gRPC certificate and its leader-election lease. The
# Secret must carry the operator's label before deletion; the lease is
# deleted by its KEDA-specific name only (leases carry no owner label), in a
# namespace Day 8 itself created and owns - accepted.
KEDA_RUNTIME_LEFTOVERS = (
    ("secret", "kedaorg-certs", {"app": "keda-operator"}),
    ("lease.coordination.k8s.io", "operator.keda.sh", None),
)


# --------------------------------------------------------------------------
# Pure evaluation helpers (unit-tested)
# --------------------------------------------------------------------------


def transitions(samples: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Pure: the samples at which any of `keys` changed value (first sample
    always included) - the replica transition record."""
    out, last = [], None
    for s in samples:
        current = tuple(s.get(k) for k in keys)
        if current != last:
            out.append({"t": s.get("t"), **{k: s.get(k) for k in keys}})
            last = current
    return out


def quota_blocked(events: list[dict]) -> list[str]:
    """Pure: Pod creations the ResourceQuota refused in this namespace."""
    out = []
    for e in events:
        message = e.get("message") or ""
        if e.get("reason") == "FailedCreate" and "exceeded quota" in message:
            obj = e.get("involvedObject") or {}
            out.append(f"{obj.get('kind')}/{obj.get('name')}: {message[:200]}")
    return out


def evaluate_hpa(samples: list[dict], minimum: int = o.HPA_MIN, maximum: int = o.HPA_MAX) -> list[tuple[bool, str]]:
    """Pure: the HPA phase verdicts from its samples (each has phase
    'baseline'|'load'|'cooldown', spec, ready, desired)."""
    load = [s for s in samples if s["phase"] == "load"]
    cooldown = [s for s in samples if s["phase"] == "cooldown"]
    peak_ready = max((s["ready"] or 0 for s in load + cooldown), default=0)
    peak_desired = max((s["desired"] or 0 for s in samples), default=0)
    peak_spec = max((s["spec"] or 0 for s in samples), default=0)
    final = cooldown[-1] if cooldown else None
    return [
        (bool(samples) and samples[0]["spec"] == minimum and samples[0]["ready"] == minimum, f"started at minReplicas: spec/ready {samples[0]['spec'] if samples else None}/{samples[0]['ready'] if samples else None} (expected {minimum}/{minimum})"),
        (peak_ready >= minimum + 1, f"observed Ready scale-out under load: peak Ready replicas {peak_ready} (expected >= {minimum + 1})"),
        (peak_spec <= maximum and peak_desired <= maximum, f"bounded: peak desired {peak_desired}, peak spec {peak_spec} (maxReplicas {maximum})"),
        (final is not None and final["spec"] == minimum and final["ready"] == minimum and peak_ready > minimum, f"observed scale-in after load: final spec/ready {final['spec'] if final else None}/{final['ready'] if final else None} (expected {minimum}/{minimum})"),
    ]


def evaluate_keda(samples: list[dict], items: int = o.PRODUCER_ITEMS, maximum: int = o.KEDA_MAX) -> list[tuple[bool, str]]:
    """Pure: KEDA phase verdicts (samples: queue, processed, spec, ready,
    active, hpa_desired)."""
    peak_queue = max((s["queue"] or 0 for s in samples), default=0)
    peak_ready = max((s["ready"] or 0 for s in samples), default=0)
    peak_spec = max((s["spec"] or 0 for s in samples), default=0)
    first_active = next((i for i, s in enumerate(samples) if (s["spec"] or 0) >= 1), None)
    final = samples[-1] if samples else {}
    return [
        (bool(samples) and samples[0]["spec"] == 0, f"idle before work: worker spec {samples[0]['spec'] if samples else None} (expected 0)"),
        (peak_queue > 0, f"queue activity observed: peak list length {peak_queue}"),
        (any(s.get("active") for s in samples), "ScaledObject reported Active while items were queued"),
        (first_active is not None, f"activation 0 -> 1 observed at sample {first_active}"),
        (peak_ready >= 2, f"scale-out beyond activation: peak Ready workers {peak_ready} (expected >= 2)"),
        (peak_spec <= maximum, f"bounded: peak worker spec {peak_spec} (maxReplicaCount {maximum})"),
        (final.get("queue") == 0 and final.get("processed") == items, f"queue drained: final length {final.get('queue')}, processed {final.get('processed')}/{items}"),
        (final.get("spec") == 0 and final.get("pods") == 0, f"scaled down to zero: final spec {final.get('spec')}, worker Pods {final.get('pods')}"),
    ]


def quantity_equal(a: str | None, b: str | None, parse) -> bool:
    try:
        return a is not None and b is not None and parse(a) == parse(b)
    except ValueError:
        return False


def _cpu(q: str) -> float:
    """CPU quantity -> millicores (VPA may emit plain or milli units)."""
    q = str(q)
    return float(q[:-1]) if q.endswith("m") else float(q) * 1000


def _mem(q: str) -> float:
    q = str(q)
    for suffix, factor in (("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3), ("k", 1000), ("M", 1000**2), ("G", 1000**3)):
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * factor
    return float(q)


def same_quantities(a: dict | None, b: dict | None) -> bool:
    """Pure: two resource maps are equal by VALUE - the API server
    canonicalises quantities (e.g. 2000m is returned as 2)."""
    if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
        return False
    for key in a:
        if key.endswith("cpu"):
            same = _cpu(a[key]) == _cpu(b[key])
        elif key.endswith("memory"):
            same = _mem(a[key]) == _mem(b[key])
        else:
            same = str(a[key]) == str(b[key])
        if not same:
            return False
    return True


def within_bounds(target: dict) -> bool:
    return (
        _cpu(o.VPA_MIN_ALLOWED["cpu"]) <= _cpu(target["cpu"]) <= _cpu(o.VPA_MAX_ALLOWED["cpu"])
        and _mem(o.VPA_MIN_ALLOWED["memory"]) <= _mem(target["memory"]) <= _mem(o.VPA_MAX_ALLOWED["memory"])
    )


def evaluate_vpa(declared: dict, recommendation_off: dict | None, targets_at_admission: list[dict], old_before: dict, old_after: dict, new_pod: dict | None) -> list[tuple[bool, str]]:
    """Pure: VPA phase verdicts. `declared` is the Deployment's resources;
    pods are {uid, resources, restarts, annotations}."""
    v = []
    target = (recommendation_off or {}).get("target")
    v.append((bool(target) and "cpu" in target and "memory" in target, f"Off mode produced a CPU+memory recommendation: {recommendation_off}"))
    v.append((bool(target) and within_bounds(target), f"recommendation target {target} within minAllowed {o.VPA_MIN_ALLOWED} / maxAllowed {o.VPA_MAX_ALLOWED}"))
    unchanged = old_before["uid"] == old_after["uid"] and old_before["resources"] == old_after["resources"] == declared and old_after["restarts"] == old_before["restarts"]
    v.append((unchanged, f"existing Pod untouched through Off and Initial: uid {old_before['uid']} -> {old_after['uid']}, resources {old_after['resources']}, restarts {old_before['restarts']} -> {old_after['restarts']}"))
    v.append((not old_after["annotations"], f"existing Pod carries no VPA admission annotation ({old_after['annotations']})"))
    if new_pod is None:
        v.append((False, "no new Pod was created under Initial"))
        return v
    requests = (new_pod["resources"] or {}).get("requests", {})
    matched = next((t for t in targets_at_admission if quantity_equal(requests.get("cpu"), t.get("cpu"), _cpu) and quantity_equal(requests.get("memory"), t.get("memory"), _mem)), None)
    v.append((bool(new_pod["annotations"]), f"new Pod {new_pod['uid']} was mutated by the VPA admission controller (annotations {new_pod['annotations']})"))
    v.append((matched is not None, f"new Pod requests {requests} == the VPA target at admission ({targets_at_admission})"))
    changed = not (quantity_equal(requests.get("cpu"), declared["requests"]["cpu"], _cpu) and quantity_equal(requests.get("memory"), declared["requests"]["memory"], _mem))
    v.append((changed, f"the applied recommendation differs from the declared requests {declared['requests']} (so the change is VPA's, not the template's)"))
    v.append((bool(requests) and within_bounds(requests), f"applied requests {requests} within maxAllowed {o.VPA_MAX_ALLOWED}"))
    limits = (new_pod["resources"] or {}).get("limits", {})
    lim_ok = bool(limits) and _cpu(limits["cpu"]) <= _cpu(o.LIMIT_MAX["cpu"]) and _mem(limits["memory"]) <= _mem(o.LIMIT_MAX["memory"])
    v.append((lim_ok, f"applied limits {limits} within the namespace LimitRange max {o.LIMIT_MAX}"))
    return v


# --------------------------------------------------------------------------
# Live helpers
# --------------------------------------------------------------------------


def apply(objects: list[dict]) -> str:
    result = common.kubectl("apply", "-f", "-", check=False, timeout=APPLY_TIMEOUT, stdin=o.as_list(objects))
    if result.returncode != 0:
        raise common.Day8Error(f"kubectl apply failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def ns_get(*args: str):
    return common.kubectl_json_or_none("-n", NS, "get", *args)


def namespace_events() -> list[dict]:
    return common.kubectl_json("-n", NS, "get", "events")["items"]


def require_guards() -> None:
    """Every phase runs only inside the guarded namespace."""
    ns = common.kubectl_json_or_none("get", "namespace", NS)
    if ns is None:
        raise common.Day8Error(f"namespace {NS} does not exist - run `make day8-guards` first")
    if not o.is_day8_owned(ns["metadata"].get("labels"), o.NAMESPACE_COMPONENT):
        raise common.Day8Error(f"namespace {NS} does not carry the Day 8 identity labels - refusing to use it")
    quota = ns_get("resourcequota", o.QUOTA)
    if quota is None or not same_quantities(quota["spec"]["hard"], o.budget().hard()):
        raise common.Day8Error(f"ResourceQuota {o.QUOTA} missing or not the computed budget ({None if quota is None else quota['spec']['hard']})")
    if ns_get("limitrange", o.LIMITRANGE) is None:
        raise common.Day8Error(f"LimitRange {o.LIMITRANGE} missing")


def deployment_state(name: str) -> dict:
    d = ns_get("deployment", name)
    if d is None:
        return {"spec": None, "ready": None}
    return {"spec": d["spec"].get("replicas"), "ready": d.get("status", {}).get("readyReplicas", 0)}


def pods(component: str) -> list[dict]:
    return common.kubectl_json("-n", NS, "get", "pods", "-l", f"app.kubernetes.io/instance={o.INSTANCE},app.kubernetes.io/component={component}")["items"]


def pod_summary(p: dict) -> dict:
    c = p["spec"]["containers"][0]
    status = (p.get("status", {}).get("containerStatuses") or [{}])[0]
    return {
        "name": p["metadata"]["name"],
        "uid": p["metadata"]["uid"],
        "resources": c.get("resources", {}),
        "restarts": status.get("restartCount"),
        "ready": any(x.get("type") == "Ready" and x.get("status") == "True" for x in p.get("status", {}).get("conditions", [])),
        "annotations": {k: v for k, v in (p["metadata"].get("annotations") or {}).items() if k.startswith("vpa")},
        "created": p["metadata"].get("creationTimestamp"),
        "terminating": bool(p["metadata"].get("deletionTimestamp")),
    }


def wait_rollout(name: str, timeout: int = 180) -> None:
    result = common.kubectl("-n", NS, "rollout", "status", f"deployment/{name}", f"--timeout={timeout}s", check=False, timeout=timeout + 30)
    if result.returncode != 0:
        raise common.Day8Error(f"deployment/{name} not Ready within {timeout}s: {result.stderr.strip()[:300]}")


def image_problems(pod_list: list[dict]) -> list[str]:
    """Every scaling Pod runs the run's pinned image: spec reference AND the
    Kubernetes imageID resolved through the node's containerd record."""
    ref = common.scaling_image()
    digest = day8_image.digest_from_ref(ref)
    problems, cache = [], {}
    for p in pod_list:
        c = p["spec"]["containers"][0]
        if c.get("image") != ref:
            problems.append(f"{p['metadata']['name']}: image {c.get('image')} != {ref}")
            continue
        status = (p.get("status", {}).get("containerStatuses") or [{}])[0]
        node = p["spec"].get("nodeName")
        if node not in cache:
            cache[node] = day7_running_images.read_node_images(node)
        record, detail = day7_running_images.resolve_image_id(status.get("imageID", ""), cache[node] or [])
        if record is None or record.id != digest:
            problems.append(f"{p['metadata']['name']}: {detail} (expected config {digest})")
    return problems


def delete_if_present(kind: str, name: str) -> None:
    result = common.kubectl("-n", NS, "delete", kind, name, "--ignore-not-found", "--wait=true", "--timeout=90s", check=False, timeout=120)
    if result.returncode != 0:
        raise common.Day8Error(f"could not delete {kind}/{name}: {result.stderr.strip()[:300]}")


def finish(phase: str, checks: common.Checks, extra: dict, success: str) -> int:
    blocked = quota_blocked(namespace_events())
    checks.record(not blocked, f"no Pod creation blocked by the ResourceQuota ({blocked or 'none'})")
    quota = ns_get("resourcequota", o.QUOTA)
    extra["quota_status"] = (quota or {}).get("status")
    extra["result"] = checks.as_dict()
    path = common.write_evidence(f"{phase}-{common.utc_now().replace(':', '')}.json", extra)
    checks.info(f"evidence: {path}")
    return checks.finish(success)


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------


def phase_guards() -> int:
    checks = common.Checks("Day 8 scaling namespace guards (LimitRange + ResourceQuota)")
    existing = common.kubectl_json_or_none("get", "namespace", NS)
    if existing is not None:
        if not o.is_day8_owned(existing["metadata"].get("labels"), o.NAMESPACE_COMPONENT):
            checks.record(False, f"namespace {NS} exists without BOTH Day 8 identity labels - refusing to adopt it")
            return checks.finish("")
        leftovers = common.kubectl("-n", NS, "get", "deployments,jobs,pods", "-o", "name").stdout.split()
        checks.record(not leftovers, f"namespace {NS} already exists and is empty of workloads ({leftovers or 'none'})")
    out = apply(o.guard_objects())
    checks.info(out.replace("\n", "; "))
    ns = common.kubectl_json("get", "namespace", NS)
    lab = ns["metadata"].get("labels") or {}
    checks.record(lab.get("pod-security.kubernetes.io/enforce") == "restricted", f"namespace {NS} enforces Pod Security 'restricted'")
    quota = ns_get("resourcequota", o.QUOTA)
    checks.record(quota is not None and same_quantities(quota["spec"]["hard"], o.budget().hard()), f"ResourceQuota {o.QUOTA} hard == computed worst-case budget {o.budget().hard()}")
    lr = ns_get("limitrange", o.LIMITRANGE)
    checks.record(lr is not None and same_quantities(lr["spec"]["limits"][0]["max"], o.LIMIT_MAX) and same_quantities(lr["spec"]["limits"][0]["defaultRequest"], o.LIMIT_DEFAULT_REQUEST), f"LimitRange {o.LIMITRANGE} max {o.LIMIT_MAX}, defaults {o.LIMIT_DEFAULT_REQUEST}/{o.LIMIT_DEFAULT}")
    other = [n for n in common.kubectl("get", "resourcequota,limitrange", "--all-namespaces", "-o", "jsonpath={range .items[*]}{.metadata.namespace}/{.metadata.name}{\"\\n\"}{end}").stdout.split() if not n.startswith(NS + "/")]
    checks.record(not other, f"no ResourceQuota/LimitRange outside {NS} ({other or 'none'})")
    return finish("guards", checks, {"budget_rows": [list(r) for r in o.budget().rows], "hard": o.budget().hard()}, f"{NS} is guarded by its LimitRange and the computed ResourceQuota")


def _create_expect_rejection(pod: dict, needle: str) -> tuple[bool, str]:
    result = common.kubectl("create", "-f", "-", check=False, stdin=json.dumps(pod))
    exists = ns_get("pod", pod["metadata"]["name"]) is not None
    if exists:
        delete_if_present("pod", pod["metadata"]["name"])
    return result.returncode != 0 and needle in result.stderr and not exists, (result.stderr.strip() or result.stdout.strip())[:400]


def phase_quota() -> int:
    require_guards()
    image = common.scaling_image()
    checks = common.Checks("Day 8 LimitRange/ResourceQuota admission proof")
    evidence = {}
    delete_if_present("pod", o.QUOTA_PROBE)
    before = ns_get("resourcequota", o.QUOTA)["status"].get("used", {})
    created = common.kubectl("create", "-f", "-", check=False, stdin=json.dumps(o.in_budget_pod(image)))
    checks.record(created.returncode == 0, f"in-budget Pod {o.QUOTA_PROBE} (no resources declared) admitted: {(created.stdout or created.stderr).strip()[:200]}")
    if created.returncode == 0:
        wait = common.kubectl("-n", NS, "wait", f"pod/{o.QUOTA_PROBE}", "--for=condition=Ready", "--timeout=120s", check=False, timeout=150)
        pod = ns_get("pod", o.QUOTA_PROBE)
        res = pod["spec"]["containers"][0].get("resources", {})
        expected = o.DEFAULTED.as_k8s()
        checks.record(same_quantities(res.get("requests"), expected["requests"]) and same_quantities(res.get("limits"), expected["limits"]), f"LimitRange defaulted its resources to {res} (expected {o.DEFAULTED.as_k8s()})")
        checks.record(wait.returncode == 0, "in-budget Pod became Ready")
        checks.record(not image_problems([pod]), f"in-budget Pod runs the pinned scaling image ({image_problems([pod]) or 'verified'})")
        during = ns_get("resourcequota", o.QUOTA)["status"].get("used", {})
        checks.record(during.get("pods") != before.get("pods"), f"quota usage counted it: used {before} -> {during}")
        evidence["in_budget"] = {"resources": res, "quota_used_before": before, "quota_used_during": during}
        delete_if_present("pod", o.QUOTA_PROBE)
    ok, msg = _create_expect_rejection(o.over_limitrange_pod(image), "maximum cpu usage per Container")
    checks.record(ok, f"container above the LimitRange max rejected: {msg}")
    evidence["over_limitrange"] = msg
    ok, msg = _create_expect_rejection(o.over_quota_pod(image), "exceeded quota")
    checks.record(ok and "requests.cpu" in msg, f"Pod above the whole quota rejected by ResourceQuota: {msg}")
    evidence["over_quota"] = msg
    leftovers = [p["metadata"]["name"] for p in pods(o.QUOTA_PROBE)]
    checks.record(not leftovers, f"no quota proof Pod left behind ({leftovers or 'none'})")
    # The over-budget rejection is expected here; only unexpected blocks count.
    blocked = [b for b in quota_blocked(namespace_events()) if "day8-quota-" not in b]
    checks.record(not blocked, f"no workload creation blocked by the quota ({blocked or 'none'})")
    evidence["result"] = checks.as_dict()
    stamp = common.utc_now().replace(":", "")
    checks.info(f"evidence: {common.write_evidence(f'quota-{stamp}.json', evidence)}")
    return checks.finish("in-budget work is admitted with defaults; over-limit and over-budget requests are rejected")


def hpa_sample(phase: str) -> dict:
    dep = deployment_state(o.HPA_TARGET)
    hpa = ns_get("hpa", o.HPA_TARGET) or {}
    status = hpa.get("status", {})
    metric = next((m for m in status.get("currentMetrics") or [] if m.get("type") == "Resource"), {})
    return {
        "t": common.utc_now(),
        "phase": phase,
        "spec": dep["spec"],
        "ready": dep["ready"],
        "current": status.get("currentReplicas"),
        "desired": status.get("desiredReplicas"),
        "cpu_utilization": ((metric.get("resource") or {}).get("current") or {}).get("averageUtilization"),
    }


def phase_hpa() -> int:
    require_guards()
    image = common.scaling_image()
    checks = common.Checks("Day 8 HPA demonstration (CPU, bounded)")
    for kind, name in (("job", o.HPA_LOAD), ("hpa", o.HPA_TARGET)):
        delete_if_present(kind, name)
    checks.info(apply(o.hpa_target_objects(image)).replace("\n", "; "))
    wait_rollout(o.HPA_TARGET)
    target_pods = pods(o.HPA_TARGET)
    checks.record(not image_problems(target_pods), f"HPA target runs the pinned scaling image ({image_problems(target_pods) or 'verified'})")

    def metrics_ready(s):
        return s["cpu_utilization"] is not None
    first, ok = common.poll(lambda: hpa_sample("baseline"), timeout=180, interval=5, until=metrics_ready)
    checks.record(ok, f"HPA reads resource metrics before any load: {first}")
    samples = [first]
    if not ok:
        return finish("hpa", checks, {"samples": samples}, "")
    checks.info(apply([o.hpa_load_job(image)]))
    load_deadline = time.monotonic() + o.LOAD_SECONDS + 90
    while time.monotonic() < load_deadline:
        samples.append(hpa_sample("load"))
        job = ns_get("job", o.HPA_LOAD) or {}
        if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
            break
        time.sleep(5)
    job = ns_get("job", o.HPA_LOAD) or {}
    checks.record(bool(job.get("status", {}).get("succeeded")), f"bounded load Job finished: {job.get('status', {})}")
    log = common.kubectl("-n", NS, "logs", f"job/{o.HPA_LOAD}", check=False).stdout.strip()
    checks.info(f"load summary: {log[-300:]}")
    cooldown_deadline = time.monotonic() + 420
    while time.monotonic() < cooldown_deadline:
        s = hpa_sample("cooldown")
        samples.append(s)
        if s["spec"] == o.HPA_MIN and s["ready"] == o.HPA_MIN and s["desired"] == o.HPA_MIN:
            break
        time.sleep(5)
    for ok, message in evaluate_hpa(samples):
        checks.record(ok, message)
    checks.record(not image_problems(pods(o.HPA_TARGET)), "every remaining HPA target Pod runs the pinned scaling image")
    record = {"samples": samples, "transitions": transitions(samples, ("spec", "ready", "desired")), "load_log": log}
    for t in record["transitions"]:
        checks.info(f"transition {t}")
    return finish("hpa", checks, record, f"HPA scaled {o.HPA_TARGET} out under bounded load and back in to {o.HPA_MIN}")


def vpa_status() -> dict | None:
    vpa = ns_get("vpa", o.VPA_TARGET) or {}
    recs = ((vpa.get("status") or {}).get("recommendation") or {}).get("containerRecommendations") or []
    return next((r for r in recs if r.get("containerName") == o.CONTAINER), None)


def phase_vpa() -> int:
    require_guards()
    image = common.scaling_image()
    checks = common.Checks("Day 8 VPA demonstration (Off, then Initial on a new Pod only)")
    delete_if_present("vpa", o.VPA_TARGET)
    checks.info(apply([*o.vpa_target_objects(image), o.vpa_object("Off")]).replace("\n", "; "))
    wait_rollout(o.VPA_TARGET)
    declared = o.vpa_target_objects(image)[0]["spec"]["template"]["spec"]["containers"][0]["resources"]
    live = [pod_summary(p) for p in pods(o.VPA_TARGET) if not p["metadata"].get("deletionTimestamp")]
    checks.record(len(live) == 1, f"one VPA target Pod before any recommendation ({[p['name'] for p in live]})")
    old_before = live[0]
    checks.record(not image_problems(pods(o.VPA_TARGET)), "VPA target runs the pinned scaling image")
    rec, ok = common.poll(vpa_status, timeout=480, interval=15, until=lambda r: bool(r and (r.get("target") or {}).get("memory")))
    checks.info(f"Off-mode recommendation: {rec}")
    old_mid = pod_summary(next(p for p in pods(o.VPA_TARGET) if p["metadata"]["uid"] == old_before["uid"]))
    checks.record(ok and old_mid["resources"] == declared and old_mid["uid"] == old_before["uid"], "Off mode: recommendation exists and the running Pod still has its declared resources (recommendation only, nothing applied)")
    record = {"declared": declared, "recommendation_off": rec, "old_pod_before": old_before}
    if not ok:
        return finish("vpa", checks, record, "")

    checks.info(apply([o.vpa_object("Initial")]))
    mode = ((ns_get("vpa", o.VPA_TARGET) or {}).get("spec", {}).get("updatePolicy") or {}).get("updateMode")
    checks.record(mode == "Initial", f"VPA updateMode is now {mode!r}")
    at_admission = [(vpa_status() or {}).get("target") or {}]
    scaled = common.kubectl("-n", NS, "scale", f"deployment/{o.VPA_TARGET}", f"--replicas={o.VPA_REPLICAS_AFTER_INITIAL}", check=False)
    checks.record(scaled.returncode == 0, f"scaled {o.VPA_TARGET} 1 -> {o.VPA_REPLICAS_AFTER_INITIAL} to create exactly one NEW Pod")
    wait_rollout(o.VPA_TARGET)
    at_admission.append((vpa_status() or {}).get("target") or {})
    current = [pod_summary(p) for p in pods(o.VPA_TARGET) if not p["metadata"].get("deletionTimestamp")]
    new = [p for p in current if p["uid"] != old_before["uid"]]
    old_after = next((p for p in current if p["uid"] == old_before["uid"]), {"uid": None, "resources": None, "restarts": None, "annotations": None})
    checks.record(len(new) == 1, f"exactly one new Pod created under Initial ({[p['name'] for p in new]})")
    for ok_, message in evaluate_vpa(declared, rec, at_admission, old_before, old_after, new[0] if new else None):
        checks.record(ok_, message)
    checks.record(not image_problems(pods(o.VPA_TARGET)), "both VPA target Pods run the pinned scaling image")
    record.update({"targets_at_admission": at_admission, "old_pod_after": old_after, "new_pod": new[0] if new else None, "updater_installed": False})
    return finish("vpa", checks, record, "VPA recommended in Off mode and applied a bounded recommendation only to a newly created Pod")


def redis(*args: str) -> str:
    result = common.kubectl("-n", NS, "exec", f"deployment/{o.QUEUE}", "-c", "redis", "--", "redis-cli", *args, timeout=20)
    return result.stdout.strip()


def keda_sample() -> dict:
    dep = deployment_state(o.WORKER)
    so = ns_get("scaledobject", o.WORKER) or {}
    conditions = {c.get("type"): c.get("status") for c in (so.get("status") or {}).get("conditions") or []}
    hpa = ns_get("hpa", f"keda-hpa-{o.WORKER}") or {}
    processed = redis("GET", f"{o.QUEUE_LIST}:processed")
    return {
        "t": common.utc_now(),
        "queue": int(redis("LLEN", o.QUEUE_LIST) or 0),
        "processed": int(processed) if processed.isdigit() else 0,
        "spec": dep["spec"],
        "ready": dep["ready"],
        "pods": len([p for p in pods(o.WORKER) if not p["metadata"].get("deletionTimestamp")]),
        "active": conditions.get("Active") == "True",
        "so_ready": conditions.get("Ready") == "True",
        "hpa_desired": (hpa.get("status") or {}).get("desiredReplicas"),
    }


def phase_keda() -> int:
    require_guards()
    image = common.scaling_image()
    checks = common.Checks("Day 8 KEDA demonstration (Redis list, separate worker)")
    delete_if_present("job", o.PRODUCER)
    checks.info(apply(o.queue_objects()).replace("\n", "; "))
    wait_rollout(o.QUEUE)
    redis("DEL", o.QUEUE_LIST, f"{o.QUEUE_LIST}:processed")
    if ns_get("deployment", o.WORKER) is None:
        checks.info(apply(o.worker_objects(image)))
    checks.info(apply([o.scaled_object()]))
    first, ok = common.poll(keda_sample, timeout=120, interval=5, until=lambda s: s["so_ready"] and s["spec"] == 0)
    checks.record(ok, f"ScaledObject Ready and the worker idle at 0 before any work: {first}")
    hpa = ns_get("hpa", f"keda-hpa-{o.WORKER}")
    hpa_spec = (hpa or {}).get("spec", {})
    checks.record(
        hpa is not None and hpa_spec.get("maxReplicas") == o.KEDA_MAX and hpa_spec.get("scaleTargetRef", {}).get("name") == o.WORKER and any(m.get("type") == "External" for m in hpa_spec.get("metrics", [])),
        f"KEDA-managed HPA keda-hpa-{o.WORKER}: target {hpa_spec.get('scaleTargetRef')}, min {hpa_spec.get('minReplicas')}, max {hpa_spec.get('maxReplicas')}, external metric",
    )
    samples = [first]
    if not ok:
        return finish("keda", checks, {"samples": samples}, "")
    checks.info(apply([o.producer_job(image)]))
    deadline = time.monotonic() + 420
    worker_images = None
    while time.monotonic() < deadline:
        s = keda_sample()
        samples.append(s)
        if worker_images is None and (s["ready"] or 0) >= 1:
            running = [p for p in pods(o.WORKER) if not p["metadata"].get("deletionTimestamp")]
            worker_images = image_problems(running) if running else None
        if s["queue"] == 0 and s["processed"] >= o.PRODUCER_ITEMS and s["spec"] == 0 and s["pods"] == 0:
            break
        time.sleep(3)
    producer = ns_get("job", o.PRODUCER) or {}
    checks.record(bool(producer.get("status", {}).get("succeeded")), f"producer Job pushed its items: {common.kubectl('-n', NS, 'logs', f'job/{o.PRODUCER}', check=False).stdout.strip()[-200:]}")
    for ok_, message in evaluate_keda(samples):
        checks.record(ok_, message)
    checks.record(worker_images == [], f"KEDA-started workers ran the pinned scaling image ({worker_images if worker_images else 'verified' if worker_images == [] else 'never observed'})")
    worker_pods_seen = common.kubectl("-n", NS, "get", "replicasets", "-l", f"app.kubernetes.io/component={o.WORKER}", "-o", "name").stdout.split()
    checks.info(f"worker ReplicaSets: {worker_pods_seen}")
    record = {"samples": samples, "transitions": transitions(samples, ("spec", "ready", "active")), "keda_hpa": hpa_spec}
    for t in record["transitions"]:
        checks.info(f"transition {t}")
    return finish("keda", checks, record, f"KEDA activated, scaled out, drained the queue and scaled {o.WORKER} back to zero")


def _list_names(*args: str) -> tuple[list[str] | None, str]:
    """`kubectl get ... -o name` -> (names, error). None = unreadable."""
    try:
        r = common.kubectl(*args, "-o", "name", check=False)
    except subprocess.SubprocessError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return None, f"exit {r.returncode}: {r.stderr.strip()[:200] or 'no error output'}"
    return r.stdout.split(), ""


def release_keda_objects(checks: common.Checks) -> bool:
    """Step 1. Deletes this namespace's KEDA objects and waits until they are
    gone WHILE KEDA (and its namespace-scoped RoleBinding) still exist - KEDA
    must release `finalizer.keda.sh` and remove its managed HPA itself.
    Deleting the namespace or KEDA first strands the finalizer and the
    namespace hangs in Terminating (run c81534e5...). A KEDA kind whose CRD
    is explicitly NotFound (KEDA never installed, or already removed) can
    hold no objects; an unreadable answer is a failure. Returns False (and
    records why) when cleanup must stop and keep the namespace."""
    served, present = [], []
    for kind in KEDA_OBJECT_KINDS:
        try:
            if common.resource_type_absent(kind):
                continue
        except common.Day8Error as exc:
            checks.record(False, f"could not determine whether {kind} is served ({exc}) - keeping the namespace")
            return False
        names, err = _list_names("-n", NS, "get", kind)
        if names is None:
            checks.record(False, f"could not list {kind} in {NS} ({err}) - keeping the namespace")
            return False
        served.append(kind)
        present += names
    if not present:
        checks.record(True, f"no KEDA objects in {NS} to release (KEDA kinds served: {served or 'none - KEDA not installed'})")
        return True
    binding, err = _list_names("-n", NS, "get", "rolebinding", KEDA_SCOPED_BINDING)
    if not binding:
        checks.record(False, f"KEDA objects {present} exist but KEDA's scoped RoleBinding {NS}/{KEDA_SCOPED_BINDING} does not ({err or 'absent'}) - KEDA could not release its finalizers, so neither KEDA nor the namespace is removed (run make day8-addons-install, then retry)")
        return False
    r = common.kubectl("-n", NS, "delete", ",".join(served), "--all", "--wait=true", "--timeout=120s", check=False, timeout=150)
    remaining, err = _list_names("-n", NS, "get", ",".join(served))
    hpas, hpa_err = _list_names("-n", NS, "get", "horizontalpodautoscalers")
    keda_hpas = [h for h in (hpas or []) if h.split("/")[-1].startswith("keda-hpa-")]
    ok = r.returncode == 0 and remaining == [] and hpas is not None and not keda_hpas
    checks.record(ok, f"KEDA released {present} and removed its managed HPA before KEDA or the namespace was removed (delete exit {r.returncode}, remaining {remaining if remaining is not None else 'UNREADABLE: ' + err}, KEDA HPAs {keda_hpas if hpas is not None else 'UNREADABLE: ' + hpa_err})")
    return ok


def crd_instance_problems(scope_of, list_instances, crds=KEDA_CRDS) -> tuple[list[str], dict]:
    """Pure over two callables. For every KEDA CRD: `scope_of(crd)` returns
    "Namespaced", "Cluster" or None (CRD explicitly NotFound) and raises when
    unreadable; `list_instances(crd, scope)` returns the instance names
    across the CRD's FULL scope (all namespaces, or cluster-wide) and raises
    when unreadable. Returns (problems, per-CRD findings). Any instance -
    Day 8's own objects were already released - is unexpected and blocks the
    uninstall, which would delete it with its CRD."""
    problems, found = [], {}
    for crd in crds:
        try:
            scope = scope_of(crd)
        except common.Day8Error as exc:
            problems.append(f"CRD {crd}: state unreadable ({exc})")
            found[crd] = "unreadable"
            continue
        if scope is None:
            found[crd] = "CRD absent"
            continue
        if scope not in ("Namespaced", "Cluster"):
            problems.append(f"CRD {crd}: unexpected scope {scope!r}")
            found[crd] = f"scope {scope!r}"
            continue
        try:
            names = list_instances(crd, scope)
        except common.Day8Error as exc:
            problems.append(f"CRD {crd} ({scope}): instances unreadable ({exc})")
            found[crd] = "unreadable"
            continue
        found[crd] = {"scope": scope, "instances": names}
        if names:
            problems.append(f"CRD {crd} ({scope}) still has {len(names)} instance(s) {names[:10]} - not Day 8's (those were already released); uninstalling KEDA would delete them with the CRD")
    return problems, found


def _crd_scope(crd: str) -> str | None:
    """Only `.spec.scope` of the CRD reaches Python. None = explicitly NotFound."""
    r = common.kubectl("get", "customresourcedefinition", crd, "-o", "jsonpath={.spec.scope}", check=False)
    if common.get_state(r.returncode, r.stderr) == "absent":
        return None
    return r.stdout.strip()


def _crd_instances(crd: str, scope: str) -> list[str]:
    """Instance identities only (namespace/name via custom-columns for
    Namespaced types, `-o name` for Cluster types) across the CRD's full
    scope - never the objects' specs."""
    args = ("get", crd, "--all-namespaces", "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name", "--no-headers") if scope == "Namespaced" else ("get", crd, "-o", "name")
    names, err = _list_names_raw(*args)
    if names is None:
        raise common.Day8Error(err)
    rows = [line for line in names if line.strip()]
    if scope != "Namespaced":
        return rows
    out = []
    for line in rows:
        parts = line.split()
        # A Namespaced instance always has a namespace; anything else is an
        # answer this guard cannot interpret - fail closed as "unreadable".
        if len(parts) != 2 or parts[0] == "<none>":
            raise common.Day8Error(f"malformed instance row for {crd}: {line.strip()[:120]!r}")
        out.append(f"{parts[0]}/{parts[1]}")
    return out


def _list_names_raw(*args: str) -> tuple[list[str] | None, str]:
    try:
        r = common.kubectl(*args, check=False)
    except subprocess.SubprocessError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return None, f"exit {r.returncode}: {r.stderr.strip()[:200] or 'no error output'}"
    return r.stdout.splitlines(), ""


def runtime_object_labels(kind: str, name: str) -> dict | None:
    """Existence (`-o name`) and then ONLY `.metadata.labels` (jsonpath) of a
    KEDA runtime object. For the `kedaorg-certs` Secret this means Python
    receives the resource name and its labels - never `.data`, so no key
    material is printed, captured by Python or written to evidence. (The
    kubectl process itself still receives the object from the API server,
    as any `kubectl get` does.) None = explicitly NotFound."""
    if common.object_state(kind, name, KEDA_NAMESPACE) == "absent":
        return None
    r = common.kubectl("-n", KEDA_NAMESPACE, "get", kind, name, "-o", "jsonpath={.metadata.labels}", check=False)
    if r.returncode != 0:
        raise common.Day8Error(f"labels of {kind} {KEDA_NAMESPACE}/{name} unreadable (exit {r.returncode}): {r.stderr.strip()[:200]}")
    text = r.stdout.strip()
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise common.Day8Error(f"labels of {kind} {KEDA_NAMESPACE}/{name} not a JSON map: {exc}") from exc


def uninstall_keda(checks: common.Checks) -> bool:
    """Step 2. Uninstalls ONLY the Day 8 `keda` Helm release from namespace
    `keda` (bounded wait) - but first proves the six KEDA CRDs, which the
    uninstall deletes, hold no instance anywhere. Then deletes the two
    objects the operator created at runtime (Helm does not own them) and
    waits for every Pod in `keda` to be gone. Safe when KEDA was never
    installed or was already removed.

    The `keda` namespace's Day 8 identity labels are verified FIRST, before
    any uninstall or runtime-object delete: a namespace Day 8 did not create
    (a foreign or legacy KEDA) is refused and nothing in it is touched."""
    ns_state = _keda_namespace_state(checks)
    if ns_state is None:
        return False
    try:
        state = common.helm_release_state(KEDA_RELEASE, KEDA_NAMESPACE)
    except common.Day8Error as exc:
        checks.record(False, f"could not determine whether Helm release {KEDA_NAMESPACE}/{KEDA_RELEASE} exists ({exc}) - keeping the namespace")
        return False
    if state == "present":
        import day8_addons

        try:
            ownership = day8_addons.keda_release_ownership_problems(common.helm_release_metadata(KEDA_RELEASE, KEDA_NAMESPACE))
        except common.Day8Error as exc:
            ownership = [f"release metadata unreadable ({exc})"]
        if not checks.record(not ownership, f"KEDA release {KEDA_NAMESPACE}/{KEDA_RELEASE} is Day 8's own (owner label, pinned chart, namespace)" if not ownership else f"KEDA NOT uninstalled - the release is not Day 8's ({'; '.join(ownership)}); KEDA and the namespace kept, nothing taken over"):
            return False
        problems, found = crd_instance_problems(_crd_scope, _crd_instances)
        checks.info(f"KEDA CRD instances immediately before uninstall: {found}")
        if not checks.record(not problems, "no instance of any of the 6 KEDA CRDs exists in any namespace or at cluster scope - the uninstall deletes only empty CRDs" if not problems else "KEDA NOT uninstalled, KEDA and the namespace kept for investigation (nothing foreign deleted or adopted): " + "; ".join(problems)):
            return False
        r = common.helm("uninstall", KEDA_RELEASE, "--namespace", KEDA_NAMESPACE, "--cascade", "foreground", "--wait", "--timeout", f"{KEDA_UNINSTALL_TIMEOUT_SECONDS}s", timeout=KEDA_UNINSTALL_TIMEOUT_SECONDS + 60)
        if not checks.record(r.returncode == 0, f"helm uninstall {KEDA_RELEASE} -n {KEDA_NAMESPACE} --cascade foreground --wait --timeout {KEDA_UNINSTALL_TIMEOUT_SECONDS}s: exit {r.returncode} {(r.stdout or r.stderr).strip()[:200]}"):
            return False
    else:
        checks.record(True, f"Helm release {KEDA_NAMESPACE}/{KEDA_RELEASE} not installed - nothing to uninstall")
    if ns_state == "absent":
        checks.info(f"namespace {KEDA_NAMESPACE} absent - no KEDA runtime object or Pod can exist")
        return True
    for kind, name, label in KEDA_RUNTIME_LEFTOVERS:
        try:
            labels = runtime_object_labels(kind, name)
        except common.Day8Error as exc:
            checks.record(False, f"could not read {kind} {KEDA_NAMESPACE}/{name} ({exc})")
            return False
        if labels is None:
            continue
        if label and any(labels.get(k) != v for k, v in label.items()):
            checks.record(False, f"{kind} {KEDA_NAMESPACE}/{name} does not carry the KEDA operator labels {label} - not deleting it")
            return False
        r = common.kubectl("-n", KEDA_NAMESPACE, "delete", kind, name, "--wait=true", "--timeout=60s", check=False, timeout=90)
        if not checks.record(r.returncode == 0, f"deleted KEDA runtime {kind} {KEDA_NAMESPACE}/{name} (not Helm-owned): exit {r.returncode}"):
            return False
    pods, ok = common.poll(lambda: _list_names("-n", KEDA_NAMESPACE, "get", "pods"), timeout=120, interval=5, until=lambda v: v[0] == [])
    if not checks.record(ok, f"no Pod left in namespace {KEDA_NAMESPACE} ({pods[0] if pods[0] is not None else 'UNREADABLE: ' + pods[1]})"):
        return False
    return delete_keda_namespace(checks)


def _keda_namespace_state(checks: common.Checks):
    """'absent', 'owned', or None (refused: unreadable, or present without
    BOTH Day 8 identity labels). Nothing is changed here."""
    try:
        ns = common.kubectl_json_or_none("get", "namespace", KEDA_NAMESPACE)
    except common.Day8Error as exc:
        checks.record(False, f"could not read namespace {KEDA_NAMESPACE} ({exc}) - KEDA NOT uninstalled, nothing in it touched")
        return None
    if ns is None:
        return "absent"
    if not o.is_day8_owned(ns["metadata"].get("labels"), o.KEDA_NAMESPACE_COMPONENT):
        checks.record(False, f"namespace {KEDA_NAMESPACE} lacks Day 8's identity labels - KEDA NOT uninstalled; no runtime Secret, lease or namespace deleted (not Day 8's to touch)")
        return None
    checks.record(True, f"namespace {KEDA_NAMESPACE} carries Day 8's identity labels (verified before any KEDA change)")
    return "owned"


def delete_keda_namespace(checks: common.Checks) -> bool:
    """Deletes the `keda` namespace only if it carries BOTH Day 8 identity
    labels (Day 8 created it before installing KEDA); refuses otherwise."""
    ns = common.kubectl_json_or_none("get", "namespace", KEDA_NAMESPACE)
    if ns is None:
        checks.info(f"namespace {KEDA_NAMESPACE} already absent")
        return True
    if not o.is_day8_owned(ns["metadata"].get("labels"), o.KEDA_NAMESPACE_COMPONENT):
        checks.record(False, f"namespace {KEDA_NAMESPACE} lacks Day 8's identity labels - not deleting it")
        return False
    r = common.kubectl("delete", "namespace", KEDA_NAMESPACE, "--wait=true", "--timeout=120s", check=False, timeout=150)
    gone = common.kubectl_json_or_none("get", "namespace", KEDA_NAMESPACE) is None
    return checks.record(r.returncode == 0 and gone, f"deleted Day 8's own namespace {KEDA_NAMESPACE}: exit {r.returncode}, gone={gone}")


def _cleanup_finish(checks: common.Checks, success: str, keda_state=None) -> int:
    """Every cleanup exit - success or an early, fail-closed stop - leaves an
    evidence file; a failure to write it is itself a failed check."""
    try:
        common.write_evidence(f"cleanup-{common.utc_now().replace(':', '')}.json", {"result": checks.as_dict(), "keda": keda_state})
    except common.Day8Error as exc:
        checks.record(False, f"cleanup evidence could not be written: {exc}")
    return checks.finish(success)


def phase_cleanup() -> int:
    """Order: (1) release KEDA objects while KEDA can still remove its
    finalizers, (2) uninstall the KEDA release, (3) delete the namespace and
    prove KEDA and every Day 8 control are gone. Any failure in (1) or (2)
    stops cleanup and keeps the namespace for diagnosis."""
    import day8_addons

    common.require_cluster_profile()
    checks = common.Checks("Day 8 scaling namespace cleanup")
    ns = common.kubectl_json_or_none("get", "namespace", NS)
    if ns is not None:
        if not o.is_day8_owned(ns["metadata"].get("labels"), o.NAMESPACE_COMPONENT):
            checks.record(False, f"namespace {NS} lacks the Day 8 identity labels - refusing to delete it")
            return _cleanup_finish(checks, "")
        if not release_keda_objects(checks):
            return _cleanup_finish(checks, "")
    else:
        checks.info(f"namespace {NS} already absent")
    if not uninstall_keda(checks):
        checks.info(f"cleanup stopped; namespace {NS} {'kept for diagnosis' if ns is not None else 'was already absent'}")
        return _cleanup_finish(checks, "")
    if ns is not None:
        result = common.kubectl("delete", "namespace", NS, "--wait=true", "--timeout=240s", check=False, timeout=270)
        checks.record(result.returncode == 0, f"deleted namespace {NS}: {(result.stdout or result.stderr).strip()[:200]}")
    checks.record(common.kubectl_json_or_none("get", "namespace", NS) is None, f"namespace {NS} is gone")
    # Fail closed: a list that could not be read is a FAILED check, never
    # "nothing left". The one exception is PROVEN, not assumed: a KEDA type
    # that is no longer served because its CRD is explicitly NotFound.
    leftovers, unreadable = [], []
    for resource in CLEANUP_RESOURCES:
        names, err = _list_names("get", resource, "--all-namespaces")
        if names is None:
            try:
                gone = common.resource_type_absent(resource)
            except common.Day8Error as exc:
                gone, err = False, f"{err}; CRD state: {exc}"
            if gone:
                checks.info(f"{resource}: type not served - CRD explicitly NotFound (uninstalled with KEDA)")
                continue
            unreadable.append(resource)
            checks.record(False, f"could not list {resource} in all namespaces ({err})")
            continue
        leftovers += [f"{resource}: {line}" for line in names]
    checks.record(not leftovers and not unreadable, f"no HPA, VPA, ScaledObject, ResourceQuota or LimitRange remains in any namespace ({leftovers or ('none' if not unreadable else f'UNVERIFIED: {unreadable} could not be listed')})")
    keda_state = day8_addons.record_keda_absent(checks)
    return _cleanup_finish(checks, f"{NS} removed and KEDA uninstalled; no Day 8 scaling object or KEDA object remains", keda_state)


PHASES = {"guards": phase_guards, "quota": phase_quota, "hpa": phase_hpa, "vpa": phase_vpa, "keda": phase_keda, "cleanup": phase_cleanup}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in PHASES:
        print(f"usage: day8_scaling.py {'|'.join(PHASES)}", file=sys.stderr)
        return 2
    try:
        common.require_cluster_profile()
        return PHASES[sys.argv[1]]()
    except (common.Day8Error, subprocess.SubprocessError, KeyError, ValueError, OSError, StopIteration) as exc:
        print(f"FAIL: {sys.argv[1]}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

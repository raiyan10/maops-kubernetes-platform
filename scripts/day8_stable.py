#!/usr/bin/env python3
"""
DAY8: independent stable-state baseline for the Day 7 release that
Day 8 must leave untouched.

  capture  (read-only) snapshot -> <run dir>/stable-baseline.json (once)
  check    (read-only) re-observe and compare field by field

What is compared, and why it proves "not touched":

  - Helm: release revision/status, user-supplied values, manifest digest
    (no Helm stage, build or value changed);
  - gateway/app Deployments and the state StatefulSet: UID, generation,
    replicas, Pod-template digest (no rollout, no scale);
  - every application Pod: name, UID, node, image, imageID, restart
    count and container resources (no eviction, recreation or resize -
    the VPA guard), and no VPA admission annotation on any of them;
  - HTTPRoute/Gateway generation and spec digest (route unchanged);
  - PVC/PV UIDs and the state file's sha256/size, read on the node from
    the PV's host path (storage identity and contents unchanged);
  - external GET / and /state through 127.0.0.1:18081 (same service,
    message and state body);
  - no HPA, VPA, ScaledObject, LimitRange or ResourceQuota in
    maops-platform (Day 8 controls never reached the application).

The Pod-level comparison is deliberately strict: if any application Pod
is replaced for ANY reason during a run, `check` fails and the reason
must be investigated - never re-baselined to make it pass.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day8_common as common
import kube

NS = kube.NAMESPACE
VPA_ANNOTATIONS = ("vpaUpdates", "vpaObservedContainers")
STATE_FILE = "state.json"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _helm_json(*args: str):
    result = common.helm(*args, "--namespace", NS, "-o", "json")
    if result.returncode != 0:
        raise common.Day8Error(f"helm {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


def observe_helm() -> dict:
    status = _helm_json("status", kube.HELM_RELEASE_NAME)
    values = _helm_json("get", "values", kube.HELM_RELEASE_NAME)
    manifest = common.helm("get", "manifest", kube.HELM_RELEASE_NAME, "--namespace", NS)
    if manifest.returncode != 0:
        raise common.Day8Error(f"helm get manifest failed: {manifest.stderr.strip()[:300]}")
    return {
        "release": kube.HELM_RELEASE_NAME,
        "revision": status.get("version"),
        "status": (status.get("info") or {}).get("status"),
        "values": values,
        "manifest_sha256": hashlib.sha256(manifest.stdout.encode()).hexdigest(),
    }


def observe_workload(kind: str, name: str) -> dict:
    obj = common.kubectl_json("-n", NS, "get", kind, name)
    spec, status = obj["spec"], obj.get("status", {})
    return {
        "uid": obj["metadata"]["uid"],
        "generation": obj["metadata"].get("generation"),
        "replicas": spec.get("replicas"),
        "ready": status.get("readyReplicas", 0),
        "template_sha256": digest(spec["template"]),
    }


def observe_pods() -> dict:
    pods = common.kubectl_json("-n", NS, "get", "pods", "-l", f"app.kubernetes.io/instance={kube.INSTANCE_LABEL}")["items"]
    out = {}
    for p in pods:
        statuses = {c["name"]: c for c in p.get("status", {}).get("containerStatuses", [])}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
        out[p["metadata"]["name"]] = {
            "uid": p["metadata"]["uid"],
            "node": p["spec"].get("nodeName"),
            "ready": ready,
            "terminating": bool(p["metadata"].get("deletionTimestamp")),
            "vpa_annotations": sorted(a for a in (p["metadata"].get("annotations") or {}) if a in VPA_ANNOTATIONS),
            "containers": {
                c["name"]: {
                    "image": c.get("image"),
                    "image_id": statuses.get(c["name"], {}).get("imageID"),
                    "restarts": statuses.get(c["name"], {}).get("restartCount"),
                    "resources": c.get("resources", {}),
                }
                for c in p["spec"]["containers"]
            },
        }
    return out


def observe_route() -> dict:
    route = common.kubectl_json("-n", NS, "get", "httproute", kube.GATEWAY_HTTPROUTE_NAME)
    gateway = common.kubectl_json("-n", kube.INGRESS_NAMESPACE, "get", "gateway", kube.GATEWAY_API_GATEWAY_NAME)
    return {
        "httproute": {"uid": route["metadata"]["uid"], "generation": route["metadata"].get("generation"), "spec_sha256": digest(route["spec"])},
        "gateway": {"uid": gateway["metadata"]["uid"], "generation": gateway["metadata"].get("generation"), "spec_sha256": digest(gateway["spec"])},
    }


def observe_storage() -> dict:
    pvc = common.kubectl_json("-n", NS, "get", "pvc", f"data-{kube.STATE_STATEFULSET}-0")
    pv_name = pvc["spec"]["volumeName"]
    pv = common.kubectl_json("get", "pv", pv_name)
    host_path = (pv["spec"].get("hostPath") or pv["spec"].get("local") or {}).get("path")
    terms = (((pv["spec"].get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms") or [])
    node = next((v for t in terms for e in t.get("matchExpressions", []) for v in e.get("values", [])), None)
    if not host_path or not node:
        raise common.Day8Error(f"PV {pv_name} has no host path/node affinity to read the state file from")
    out = common.node_exec(node, "sh", "-c", f"stat -c %s {host_path}/{STATE_FILE} && sha256sum {host_path}/{STATE_FILE}")
    if out.returncode != 0:
        raise common.Day8Error(f"could not read {STATE_FILE} on {node}: {out.stderr.strip()[:200]}")
    size_line, sum_line = out.stdout.strip().splitlines()[:2]
    return {
        "pvc_uid": pvc["metadata"]["uid"],
        "pvc_phase": pvc.get("status", {}).get("phase"),
        "pv_name": pv_name,
        "pv_uid": pv["metadata"]["uid"],
        "pv_reclaim_policy": pv["spec"].get("persistentVolumeReclaimPolicy"),
        "pv_node": node,
        "state_file_size": int(size_line),
        "state_file_sha256": sum_line.split()[0],
    }


def http_get(path: str) -> tuple[int | None, str]:
    req = urllib.request.Request(f"{kube.GATEWAY_HOST_ADDRESS}{path}", headers={"Host": kube.ROUTING_HOSTNAME})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)


def observe_external() -> dict:
    out = {}
    code, body = http_get("/")
    try:
        parsed = json.loads(body)
        out["root"] = {"status": code, "service": parsed.get("service"), "message": parsed.get("message")}
    except json.JSONDecodeError:
        out["root"] = {"status": code, "unparsed": body[:200]}
    code, body = http_get("/state")
    out["state"] = {"status": code, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
    return out


def observe_controls() -> dict:
    """Autoscaling/budget objects in the application namespace. A CRD-backed
    type that is not served counts as "none" only when its CRD is explicitly
    NotFound (KEDA is uninstalled between Day 8 runs); any other failure
    raises - an unreadable API is never an empty list."""
    out = {}
    for resource in ("horizontalpodautoscalers.autoscaling", "verticalpodautoscalers.autoscaling.k8s.io", "scaledobjects.keda.sh", "limitranges", "resourcequotas"):
        result = common.kubectl("-n", NS, "get", resource, "-o", "name", check=False)
        if result.returncode == 0:
            out[resource] = sorted(line for line in result.stdout.splitlines() if line.strip())
        elif common.resource_type_absent(resource):
            out[resource] = []
        else:
            raise common.Day8Error(f"kubectl get {resource} failed although its CRD exists: {result.stderr.strip()[:200]}")
    return out


def observe() -> dict:
    common.require_cluster_profile()
    return {
        "context": kube.CONTEXT,
        "helm": observe_helm(),
        "workloads": {
            "deployment/maops-gateway": observe_workload("deployment", kube.GATEWAY_DEPLOYMENT),
            "deployment/maops-app": observe_workload("deployment", kube.APP_DEPLOYMENT),
            "statefulset/maops-state": observe_workload("statefulset", kube.STATE_STATEFULSET),
        },
        "pods": observe_pods(),
        "route": observe_route(),
        "storage": observe_storage(),
        "external": observe_external(),
        "controls": observe_controls(),
    }


EXPECTED_PODS = {"maops-gateway": 3, "maops-app": 3, "maops-state": 1}


def health_problems(snapshot: dict) -> list[str]:
    """Pure: what makes a snapshot unfit to be a baseline (or a pass)."""
    problems = []
    counts = {k: 0 for k in EXPECTED_PODS}
    for name, pod in snapshot["pods"].items():
        prefix = next((k for k in EXPECTED_PODS if name.startswith(k + "-")), None)
        if prefix is None:
            problems.append(f"unexpected application Pod {name}")
            continue
        counts[prefix] += 1
        if not pod["ready"] or pod["terminating"]:
            problems.append(f"Pod {name} is not Ready (ready={pod['ready']}, terminating={pod['terminating']})")
        if pod["vpa_annotations"]:
            problems.append(f"Pod {name} carries VPA admission annotations {pod['vpa_annotations']}")
    for prefix, expected in EXPECTED_PODS.items():
        if counts[prefix] != expected:
            problems.append(f"{counts[prefix]} {prefix} Pod(s), expected {expected}")
    for name, w in snapshot["workloads"].items():
        if w["ready"] != w["replicas"]:
            problems.append(f"{name}: {w['ready']}/{w['replicas']} ready")
    if snapshot["helm"]["status"] != "deployed":
        problems.append(f"Helm release status {snapshot['helm']['status']!r}, expected 'deployed'")
    if snapshot["storage"]["pvc_phase"] != "Bound":
        problems.append(f"PVC phase {snapshot['storage']['pvc_phase']!r}, expected Bound")
    root = snapshot["external"]["root"]
    if root.get("status") != 200 or "stable" not in str(root.get("message", "")):
        problems.append(f"external GET / -> {root} (expected 200 from the stable gateway)")
    if snapshot["external"]["state"]["status"] != 200:
        problems.append(f"external GET /state -> {snapshot['external']['state']['status']}, expected 200")
    for resource, names in snapshot["controls"].items():
        if names:
            problems.append(f"{resource} present in {NS}: {names} - Day 8 controls must never reach the application namespace")
    return problems


def diff(baseline, current, path: str = "") -> list[str]:
    """Pure: every leaf that differs, as 'path: baseline -> current'."""
    if isinstance(baseline, dict) and isinstance(current, dict):
        out = []
        for key in sorted(set(baseline) | set(current)):
            sub = f"{path}.{key}" if path else str(key)
            if key not in current:
                out.append(f"{sub}: present in baseline, missing now")
            elif key not in baseline:
                out.append(f"{sub}: absent from baseline, present now ({json.dumps(current[key])[:200]})")
            else:
                out += diff(baseline[key], current[key], sub)
        return out
    if baseline != current:
        return [f"{path}: {json.dumps(baseline)[:200]} -> {json.dumps(current)[:200]}"]
    return []


def capture() -> int:
    checks = common.Checks("Day 8 stable-state baseline capture (read-only)")
    snapshot = observe()
    problems = health_problems(snapshot)
    for p in problems:
        checks.record(False, p)
    if problems:
        return checks.finish("")
    snapshot["captured_at"] = common.utc_now()
    path = common.write_evidence(common.STABLE_BASELINE, snapshot)
    checks.record(True, f"stable state healthy: Helm revision {snapshot['helm']['revision']}, 3/3 gateway, 3/3 app, 1/1 state, "
                        f"PVC {snapshot['storage']['pvc_uid']}, state file sha256 {snapshot['storage']['state_file_sha256'][:16]}...")
    checks.record(True, f"baseline written once to {path} (0600)")
    return checks.finish("Day 8 stable baseline captured")


def check() -> int:
    checks = common.Checks("Day 8 independent stable-state check (read-only)")
    baseline = common.read_evidence(common.STABLE_BASELINE)
    current = observe()
    for p in health_problems(current):
        checks.record(False, p)
    for section in ("helm", "workloads", "pods", "route", "storage", "external", "controls"):
        differences = diff(baseline[section], current[section])
        checks.record(not differences, f"{section}: unchanged since baseline" if not differences else f"{section}: {len(differences)} difference(s): {'; '.join(differences[:6])}")
    result = checks.finish("the Day 7 release, its Pods, route, storage and state are exactly as baselined")
    try:
        common.write_evidence(f"stable-check-{common.utc_now().replace(':', '')}.json", {"result": checks.as_dict(), "observed": current})
    except common.Day8Error as exc:
        print(f"WARN: could not record stable-check evidence: {exc}", file=sys.stderr)
    return result


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("capture", "check"):
        print("usage: day8_stable.py capture|check", file=sys.stderr)
        return 2
    try:
        return capture() if sys.argv[1] == "capture" else check()
    except (common.Day8Error, subprocess.SubprocessError, KeyError, ValueError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

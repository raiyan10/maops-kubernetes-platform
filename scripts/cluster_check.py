#!/usr/bin/env python3
"""
Real kind-cluster validation for Day 1 (checks 1-10, 12-13 of the
required real-cluster proof list; #11 lives in smoke.py and #14 in
reconcile_check.py since they need their own bounded lifecycles).

Talks to the actual live cluster via `kubectl ... -o json` - this is
runtime evidence, not manifest re-reading.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kube import CONTEXT, DEPLOYMENT, NAMESPACE, SERVICE, get_json, run, wait_until

EXPECTED_K8S_VERSION = "v1.36.1"
EXPECTED_REPLICAS = 2
LABEL_SELECTOR = "app.kubernetes.io/name=maops-kubernetes-platform,app.kubernetes.io/instance=maops-kubernetes-platform-day1"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def check_cluster_ready():
    nodes = get_json("get", "nodes")["items"]
    ready_nodes = [
        n
        for n in nodes
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in n.get("status", {}).get("conditions", []))
    ]
    record(len(nodes) >= 1 and len(ready_nodes) == len(nodes), f"cluster has {len(nodes)} node(s), {len(ready_nodes)} Ready")


def check_server_version():
    version = get_json("version")
    git_version = version.get("serverVersion", {}).get("gitVersion")
    record(git_version == EXPECTED_K8S_VERSION, f"server version {git_version} (expected {EXPECTED_K8S_VERSION})")


def check_namespace():
    ns = get_json("get", "namespace", NAMESPACE)
    record(ns.get("metadata", {}).get("name") == NAMESPACE, f"namespace {NAMESPACE} exists")


def wait_for_deployment_available():
    def predicate():
        dep = get_json("-n", NAMESPACE, "get", "deployment", DEPLOYMENT)
        conditions = dep.get("status", {}).get("conditions", [])
        available = any(c.get("type") == "Available" and c.get("status") == "True" for c in conditions)
        return dep if available else None

    try:
        dep = wait_until(predicate, timeout=120, interval=3, description="Deployment Available")
        record(True, "Deployment maops-app reached Available=True")
    except TimeoutError as exc:
        record(False, str(exc))
        dep = get_json("-n", NAMESPACE, "get", "deployment", DEPLOYMENT)
    return dep


def check_replica_counts(dep: dict):
    desired = dep.get("spec", {}).get("replicas")
    ready = dep.get("status", {}).get("readyReplicas")
    record(desired == EXPECTED_REPLICAS, f"desired replicas == {desired} (expected {EXPECTED_REPLICAS})")
    record(ready == EXPECTED_REPLICAS, f"ready replicas == {ready} (expected {EXPECTED_REPLICAS})")


def get_pods() -> list[dict]:
    return get_json("-n", NAMESPACE, "get", "pods", "-l", LABEL_SELECTOR)["items"]


def check_pods_ready(pods: list[dict]):
    ready_pods = [
        p
        for p in pods
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
    ]
    record(
        len(pods) == EXPECTED_REPLICAS and len(ready_pods) == EXPECTED_REPLICAS,
        f"{len(ready_pods)}/{len(pods)} pods Ready (expected {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS})",
    )


def check_service():
    svc = get_json("-n", NAMESPACE, "get", "service", SERVICE)
    svc_type = svc.get("spec", {}).get("type")
    record(svc_type == "ClusterIP", f"Service type == {svc_type} (expected ClusterIP)")


def check_endpoints():
    def predicate():
        ep = get_json("-n", NAMESPACE, "get", "endpoints", SERVICE)
        subsets = ep.get("subsets") or []
        addresses = [a for s in subsets for a in s.get("addresses", [])]
        return len(addresses) if len(addresses) == EXPECTED_REPLICAS else None

    try:
        count = wait_until(predicate, timeout=60, interval=2, description="Service endpoints ready")
        record(True, f"Service has {count} ready endpoint(s) (expected {EXPECTED_REPLICAS})")
    except TimeoutError as exc:
        record(False, str(exc))


def exec_in_pod(pod_name: str, *cmd: str) -> str:
    result = run("-n", NAMESPACE, "exec", pod_name, "--", *cmd)
    return result.stdout.strip()


def _stderr_detail(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    return stderr if stderr else str(exc)


def check_configmap_consumption(pods: list[dict]):
    expected = get_json("-n", NAMESPACE, "get", "configmap", "maops-app-config").get("data", {}).get("APP_MESSAGE")
    if not pods:
        record(False, "configmap consumption: no pods available to exec into")
        return
    pod_name = pods[0]["metadata"]["name"]
    try:
        actual = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", "import os,sys; sys.stdout.write(os.environ.get('APP_MESSAGE',''))")
    except subprocess.CalledProcessError as exc:
        record(False, f"configmap consumption: kubectl exec failed in pod {pod_name}: {_stderr_detail(exc)}")
        return
    record(
        bool(expected) and actual == expected,
        f"ConfigMap APP_MESSAGE consumed by live pod {pod_name}: process env == ConfigMap data ({actual!r})",
    )


def check_runtime_uid_gid(pods: list[dict]):
    if not pods:
        record(False, "runtime UID/GID: no pods available to exec into")
        return
    pod_name = pods[0]["metadata"]["name"]
    try:
        actual = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", "import os; print(os.getuid(), os.getgid())")
    except subprocess.CalledProcessError as exc:
        record(False, f"runtime UID/GID: kubectl exec failed in pod {pod_name}: {_stderr_detail(exc)}")
        return
    record(actual == "10001 10001", f"live process UID/GID in pod {pod_name} == {actual!r} (expected '10001 10001')")


def check_runtime_security_context(pods: list[dict]):
    if not pods:
        record(False, "runtime securityContext: no pods available to inspect")
        return
    pod = get_json("-n", NAMESPACE, "get", "pod", pods[0]["metadata"]["name"])
    container = pod["spec"]["containers"][0]
    csc = container.get("securityContext", {})
    psc = pod["spec"].get("securityContext", {})
    record(csc.get("readOnlyRootFilesystem") is True, f"live pod readOnlyRootFilesystem == {csc.get('readOnlyRootFilesystem')!r}")
    record(csc.get("allowPrivilegeEscalation") is False, f"live pod allowPrivilegeEscalation == {csc.get('allowPrivilegeEscalation')!r}")
    record((csc.get("capabilities") or {}).get("drop") == ["ALL"], f"live pod capabilities.drop == {(csc.get('capabilities') or {}).get('drop')!r}")
    record((psc.get("seccompProfile") or {}).get("type") == "RuntimeDefault", f"live pod seccompProfile == {psc.get('seccompProfile')!r}")


def main() -> int:
    print(f"# Real cluster validation against context {CONTEXT}")
    check_cluster_ready()
    check_server_version()
    check_namespace()
    dep = wait_for_deployment_available()
    check_replica_counts(dep)
    pods = get_pods()
    check_pods_ready(pods)
    check_service()
    check_endpoints()
    check_configmap_consumption(pods)
    check_runtime_uid_gid(pods)
    check_runtime_security_context(pods)

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} real cluster checks passed")
    if failures:
        print(f"FAIL: {len(failures)} real cluster check(s) failed", file=sys.stderr)
        return 1
    print("PASS: all real cluster checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Real kind-cluster validation for Day 2 (checks 1-10, 12-20 of the
required real-cluster proof list; discovery/DNS lives in
discovery_check.py, Secret/auth behavior in secret_check.py, normal HTTP
smoke in smoke.py, and dependency-failure behavior in dependency_check.py
since each needs its own bounded lifecycle).

Talks to the actual live cluster via `kubectl ... -o json` - this is
runtime evidence, not manifest re-reading. Authoritative backend-
readiness evidence uses discovery.k8s.io/v1 EndpointSlice (not the
legacy v1 Endpoints API, which Kubernetes 1.36 deprecates).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from endpointslice import count_ready_endpoints
from kube import (
    APP_DEPLOYMENT,
    APP_LABEL_SELECTOR,
    APP_SERVICE,
    CONTEXT,
    GATEWAY_DEPLOYMENT,
    GATEWAY_LABEL_SELECTOR,
    GATEWAY_SERVICE,
    INTERNAL_SECRET,
    NAMESPACE,
    get_json,
    run,
    wait_until,
)

EXPECTED_K8S_VERSION = "v1.36.1"
EXPECTED_REPLICAS = 2

WORKLOADS = [
    ("gateway", GATEWAY_DEPLOYMENT, GATEWAY_SERVICE, GATEWAY_LABEL_SELECTOR, "maops-gateway-config"),
    ("app", APP_DEPLOYMENT, APP_SERVICE, APP_LABEL_SELECTOR, "maops-app-config"),
]

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


def wait_for_deployment_available(deployment: str):
    def predicate():
        dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
        conditions = dep.get("status", {}).get("conditions", [])
        available = any(c.get("type") == "Available" and c.get("status") == "True" for c in conditions)
        return dep if available else None

    try:
        dep = wait_until(predicate, timeout=120, interval=3, description=f"Deployment {deployment} Available")
        record(True, f"Deployment {deployment} reached Available=True")
    except TimeoutError as exc:
        record(False, str(exc))
        dep = get_json("-n", NAMESPACE, "get", "deployment", deployment)
    return dep


def check_replica_counts(deployment: str, dep: dict):
    desired = dep.get("spec", {}).get("replicas")
    ready = dep.get("status", {}).get("readyReplicas")
    record(desired == EXPECTED_REPLICAS, f"{deployment} desired replicas == {desired} (expected {EXPECTED_REPLICAS})")
    record(ready == EXPECTED_REPLICAS, f"{deployment} ready replicas == {ready} (expected {EXPECTED_REPLICAS})")


def get_pods(label_selector: str) -> list[dict]:
    return get_json("-n", NAMESPACE, "get", "pods", "-l", label_selector)["items"]


def check_pods_ready(component: str, pods: list[dict]):
    ready_pods = [
        p
        for p in pods
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in p.get("status", {}).get("conditions", []))
    ]
    record(
        len(pods) == EXPECTED_REPLICAS and len(ready_pods) == EXPECTED_REPLICAS,
        f"{component}: {len(ready_pods)}/{len(pods)} pods Ready (expected {EXPECTED_REPLICAS}/{EXPECTED_REPLICAS})",
    )


def check_service_type(component: str, service: str):
    svc = get_json("-n", NAMESPACE, "get", "service", service)
    svc_type = svc.get("spec", {}).get("type")
    record(svc_type == "ClusterIP", f"{component} Service type == {svc_type} (expected ClusterIP)")


def check_endpointslice(component: str, service: str):
    def predicate():
        slices = get_json("-n", NAMESPACE, "get", "endpointslices", "-l", f"kubernetes.io/service-name={service}")[
            "items"
        ]
        ready = count_ready_endpoints(slices)
        return ready if ready == EXPECTED_REPLICAS else None

    try:
        count = wait_until(predicate, timeout=60, interval=2, description=f"{service} EndpointSlice ready endpoints")
        record(True, f"{component} EndpointSlice has {count} ready endpoint(s) (expected {EXPECTED_REPLICAS})")
    except TimeoutError as exc:
        record(False, str(exc))


def exec_in_pod(pod_name: str, *cmd: str) -> str:
    result = run("-n", NAMESPACE, "exec", pod_name, "--", *cmd)
    return result.stdout.strip()


def _stderr_detail(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    return stderr if stderr else str(exc)


def check_configmap_consumption(component: str, configmap: str, pods: list[dict]):
    if not pods:
        record(False, f"{component} configmap consumption: no pods available to exec into")
        return
    expected = get_json("-n", NAMESPACE, "get", "configmap", configmap).get("data", {}).get("APP_ENVIRONMENT")
    pod_name = pods[0]["metadata"]["name"]
    try:
        actual = exec_in_pod(
            pod_name, "/usr/bin/python3.11", "-c", "import os,sys; sys.stdout.write(os.environ.get('APP_ENVIRONMENT',''))"
        )
    except subprocess.CalledProcessError as exc:
        record(False, f"{component} configmap consumption: kubectl exec failed in pod {pod_name}: {_stderr_detail(exc)}")
        return
    record(
        bool(expected) and actual == expected,
        f"{component} ConfigMap {configmap} APP_ENVIRONMENT consumed by live pod {pod_name}: "
        f"process env == ConfigMap data ({actual!r})",
    )


def check_runtime_uid_gid(component: str, pods: list[dict]):
    if not pods:
        record(False, f"{component} runtime UID/GID: no pods available to exec into")
        return
    pod_name = pods[0]["metadata"]["name"]
    try:
        actual = exec_in_pod(pod_name, "/usr/bin/python3.11", "-c", "import os; print(os.getuid(), os.getgid())")
    except subprocess.CalledProcessError as exc:
        record(False, f"{component} runtime UID/GID: kubectl exec failed in pod {pod_name}: {_stderr_detail(exc)}")
        return
    record(actual == "10001 10001", f"{component} live process UID/GID in pod {pod_name} == {actual!r} (expected '10001 10001')")


def check_runtime_security_context(component: str, pods: list[dict]):
    if not pods:
        record(False, f"{component} runtime securityContext: no pods available to inspect")
        return
    pod = get_json("-n", NAMESPACE, "get", "pod", pods[0]["metadata"]["name"])
    container = pod["spec"]["containers"][0]
    csc = container.get("securityContext", {})
    psc = pod["spec"].get("securityContext", {})
    record(csc.get("readOnlyRootFilesystem") is True, f"{component} live pod readOnlyRootFilesystem == {csc.get('readOnlyRootFilesystem')!r}")
    record(csc.get("allowPrivilegeEscalation") is False, f"{component} live pod allowPrivilegeEscalation == {csc.get('allowPrivilegeEscalation')!r}")
    record((csc.get("capabilities") or {}).get("drop") == ["ALL"], f"{component} live pod capabilities.drop == {(csc.get('capabilities') or {}).get('drop')!r}")
    record((psc.get("seccompProfile") or {}).get("type") == "RuntimeDefault", f"{component} live pod seccompProfile == {psc.get('seccompProfile')!r}")


def check_no_service_account_token_mount(component: str, pods: list[dict]):
    if not pods:
        record(False, f"{component} ServiceAccount token mount: no pods available to inspect")
        return
    pod = get_json("-n", NAMESPACE, "get", "pod", pods[0]["metadata"]["name"])
    automount = pod["spec"].get("automountServiceAccountToken")
    container = pod["spec"]["containers"][0]
    mounts = container.get("volumeMounts") or []
    sa_mounts = [m for m in mounts if "serviceaccount" in (m.get("mountPath") or "").lower()]
    record(
        automount is False and not sa_mounts,
        f"{component} live pod automountServiceAccountToken == {automount!r}, "
        f"no ServiceAccount token volumeMount present (found {sa_mounts})",
    )


def check_secret_volume_mount(component: str, pods: list[dict]):
    if not pods:
        record(False, f"{component} Secret volume/mount: no pods available to inspect")
        return
    pod = get_json("-n", NAMESPACE, "get", "pod", pods[0]["metadata"]["name"])
    volumes = pod["spec"].get("volumes") or []
    volume = next((v for v in volumes if v.get("name") == "internal-auth"), {})
    secret_name = (volume.get("secret") or {}).get("secretName")
    record(
        secret_name == INTERNAL_SECRET,
        f"{component} live pod volume 'internal-auth' references Secret {secret_name!r} (expected {INTERNAL_SECRET!r})",
    )
    container = pod["spec"]["containers"][0]
    mounts = container.get("volumeMounts") or []
    mount = next((m for m in mounts if m.get("name") == "internal-auth"), {})
    record(
        mount.get("mountPath") == "/var/run/secrets/maops" and mount.get("readOnly") is True,
        f"{component} live pod volumeMount 'internal-auth' -> mountPath={mount.get('mountPath')!r} "
        f"readOnly={mount.get('readOnly')!r} (expected /var/run/secrets/maops, readOnly=true)",
    )


def main() -> int:
    print(f"# Real Day 2 cluster validation against context {CONTEXT}")
    check_cluster_ready()
    check_server_version()
    check_namespace()

    pods_by_component: dict[str, list[dict]] = {}
    for component, deployment, service, label_selector, configmap in WORKLOADS:
        dep = wait_for_deployment_available(deployment)
        check_replica_counts(deployment, dep)
        pods = get_pods(label_selector)
        pods_by_component[component] = pods
        check_pods_ready(component, pods)
        check_service_type(component, service)
        check_endpointslice(component, service)
        check_configmap_consumption(component, configmap, pods)
        check_runtime_uid_gid(component, pods)
        check_runtime_security_context(component, pods)
        check_no_service_account_token_mount(component, pods)
        check_secret_volume_mount(component, pods)

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

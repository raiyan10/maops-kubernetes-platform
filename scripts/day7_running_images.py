#!/usr/bin/env python3
"""
DAY7: running-image gate - proves what every stable gateway/app/state
container (and every candidate container, when the candidate is enabled)
is ACTUALLY running, against the verified build (scripts/day7_build.py).

Why node image checks were not enough (run `ce55f5fb...`): every node
held the new build under the mutable `:<VERSION>` tag, yet the running
stable containers were the previous build - the Pods were never
replaced. Only a running container can prove what runs.

Digest mapping - compared like with like, never unlike digests:
  - Docker `.Id` on this host is the OCI manifest digest; the build
    record holds the CONFIG digest re-derived from `docker save`
    (day7_image_check / day7_build).
  - Kubernetes `containerStatuses[].imageID` is containerd's CRI
    imageRef, which for a `kind load`ed image is a repo digest such as
    `docker.io/library/import-2026-09-28@sha256:<manifest digest>` - a
    THIRD digest, neither of the above.
  - On the container's own node, `crictl images -o json` lists each
    image record with `id` (the CONFIG digest), `repoTags` and
    `repoDigests`. The gate finds the ONE record whose repoDigests
    contain the container's imageID (or whose id equals it, if a
    runtime reports the config digest directly) and requires that
    record's `id` == the build's config digest AND its repoTags to
    include the build's pinned tag. Each PASS line prints the chain
    imageID -> node record id -> build digest.

Fail-closed, per component (gateway, app, state, candidate):
  - exact Pod count from the release's own values (`helm get values
    --all`: gateway/app/state replicas; candidate.replicas when
    candidate.enabled, else ZERO candidate Pods allowed) - missing or
    extra (including terminating) Pods fail;
  - every Pod Running, Ready, not terminating; the expected container
    present, Ready, with a containerID and a well-formed imageID;
  - the Pod SPEC's container image names the build's pinned ref (a Pod
    still on the mutable `:<VERSION>` tag fails even if the bytes
    happen to match). NOT `containerStatuses[].image`: measured live
    (run c252aa3d..., 2026-09-29), containerd reports an image record's
    FIRST repoTag there - `...:0.7.0` for a container created from
    `...:0.7.0-cfg-<digest>` once both tags name the same record - so
    that field is informational only. The runtime identity is the
    imageID -> node record -> config digest chain below;
  - the node record resolved from the imageID exists exactly once, is
    readable, has the build's config digest and carries its pinned tag.
Unreadable API/node state fails immediately (never retried into a
pass). `wait_for_running_images` re-observes only while the rollout is
visibly converging (count/readiness/image not yet matching), bounded.

Day 7 only: refuses outside the Day 7 profile before any kubectl/docker
call; node names must be maops-k8s-day7 nodes. Read-only. Never reads a
Secret.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import day7_strategy as d7
import kube

STABLE_COMPONENTS = ("gateway", "app", "state")
ALL_COMPONENTS = (*STABLE_COMPONENTS, "candidate")
CONTAINER = {"gateway": "maops-gateway", "app": "maops-app", "state": "maops-state", "candidate": d7.GATEWAY_CONTAINER}
COMPONENT_LABEL = {"gateway": "gateway", "app": "app", "state": "state", "candidate": d7.CANDIDATE_COMPONENT}
_IMAGE_ID_RE = re.compile(r"(?:^|@)(sha256:[0-9a-f]{64})$")
_NODE_RE = re.compile(r"maops-k8s-day7-(control-plane|worker\d*)")  # always fullmatch()
CRICTL_TIMEOUT_SECONDS = 30.0
WAIT_TIMEOUT_SECONDS = 240.0
WAIT_POLL_SECONDS = 5.0


def selector(component: str) -> str:
    return (
        "app.kubernetes.io/name=maops-kubernetes-platform,"
        f"app.kubernetes.io/instance={kube.INSTANCE_LABEL},"
        f"app.kubernetes.io/component={COMPONENT_LABEL[component]}"
    )


def strip_default_registry(ref: str) -> str:
    return ref[len("docker.io/library/"):] if ref.startswith("docker.io/library/") else ref


@dataclass(frozen=True)
class NodeImage:
    id: str
    repo_tags: tuple[str, ...]
    repo_digests: tuple[str, ...]


def parse_node_images(stdout: str) -> list[NodeImage]:
    """Pure: `crictl images -o json`. Raises ValueError if unparseable."""
    data = json.loads(stdout)
    images = data.get("images")
    if not isinstance(images, list):
        raise ValueError("crictl images output has no images list")
    return [NodeImage(str(i.get("id") or ""), tuple(i.get("repoTags") or ()), tuple(i.get("repoDigests") or ())) for i in images]


def resolve_image_id(image_id: str, records: list[NodeImage]) -> tuple[NodeImage | None, str]:
    """Pure: the unique node record the Kubernetes imageID denotes."""
    if not image_id or not _IMAGE_ID_RE.search(image_id):
        return None, f"imageID {image_id!r} is missing or not a sha256 reference"
    wanted = strip_default_registry(image_id)
    matches = [r for r in records if r.id == image_id or any(strip_default_registry(d) == wanted for d in r.repo_digests)]
    if len(matches) != 1:
        return None, f"imageID {image_id} matches {len(matches)} image record(s) on the node (expected exactly 1)"
    return matches[0], f"imageID {image_id} -> node image record {matches[0].id}"


def expected_counts(values_all: dict) -> dict[str, int]:
    """Pure: from `helm get values --all`. Raises KeyError/TypeError if
    the release values are malformed (caller fails closed)."""
    candidate = values_all.get("candidate") or {}
    return {
        "gateway": int(values_all["gateway"]["replicas"]),
        "app": int(values_all["app"]["replicas"]),
        "state": int(values_all["state"]["replicas"]),
        "candidate": int(candidate["replicas"]) if candidate.get("enabled") is True else 0,
    }


def evaluate_component(
    component: str,
    pods: list[dict],
    expected_count: int,
    image: day7_build.BuildImage,
    node_images: dict[str, list[NodeImage] | None],
) -> list[tuple[bool, str]]:
    """Pure. Every check for one component; all must be True."""
    checks: list[tuple[bool, str]] = []
    names = sorted(p.get("metadata", {}).get("name", "?") for p in pods)
    checks.append((len(pods) == expected_count, f"{component}: {len(pods)} Pod(s) {names} (expected exactly {expected_count})"))
    container_name = CONTAINER[component]
    for pod in pods:
        meta, status, spec = pod.get("metadata", {}), pod.get("status", {}), pod.get("spec", {})
        name, node = meta.get("name", "?"), spec.get("nodeName")
        prefix = f"{component} {name} (uid {meta.get('uid')}, node {node})"
        checks.append((not meta.get("deletionTimestamp"), f"{prefix}: not terminating"))
        checks.append((status.get("phase") == "Running" and d7.pod_is_ready(pod), f"{prefix}: Running and Ready (phase {status.get('phase')!r}, Ready {d7.pod_is_ready(pod)})"))
        cs = next((c for c in status.get("containerStatuses") or [] if c.get("name") == container_name), None)
        if cs is None:
            checks.append((False, f"{prefix}: container {container_name!r} has no status"))
            continue
        checks.append((cs.get("ready") is True and bool(cs.get("containerID")), f"{prefix}: container {container_name} ready={cs.get('ready')} containerID={cs.get('containerID')} restarts={cs.get('restartCount')}"))
        spec_container = next((c for c in spec.get("containers") or [] if c.get("name") == container_name), {})
        spec_ref = strip_default_registry(spec_container.get("image") or "")
        checks.append((
            spec_ref == image.ref,
            f"{prefix}: Pod spec image reference {spec_container.get('image')!r} (expected the pinned build ref {image.ref!r};"
            f" runtime-reported tag {cs.get('image')!r} is informational - containerd reports the record's first tag)",
        ))
        image_id = cs.get("imageID") or ""
        records = node_images.get(node)
        if records is None:
            checks.append((False, f"{prefix}: node {node!r} image records unreadable - cannot map imageID {image_id!r}"))
            continue
        record, detail = resolve_image_id(image_id, records)
        if record is None:
            checks.append((False, f"{prefix}: {detail}"))
            continue
        checks.append((record.id == image.config_digest, f"{prefix}: {detail} == build config digest {image.config_digest}" if record.id == image.config_digest else f"{prefix}: {detail} is NOT the build config digest {image.config_digest}"))
        tagged = image.node_ref in record.repo_tags
        checks.append((tagged, f"{prefix}: node record carries the pinned tag {image.node_ref} (tags {list(record.repo_tags)})"))
    return checks


def read_node_images(node: str) -> list[NodeImage] | None:
    """`crictl images` inside one Day 7 kind node container (read-only).
    None = unreadable (fails closed)."""
    d7.require_day7_profile()
    if not _NODE_RE.fullmatch(node or ""):
        raise RuntimeError(f"refusing to inspect {node!r}: not a maops-k8s-day7 node")
    try:
        result = subprocess.run(["docker", "exec", node, "crictl", "images", "-o", "json"], capture_output=True, text=True, timeout=CRICTL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return parse_node_images(result.stdout)
    except (ValueError, json.JSONDecodeError, AttributeError):
        return None


@dataclass
class Observation:
    checks: list[tuple[bool, str]]
    fatal: str = ""  # unreadable state: never retried

    @property
    def ok(self) -> bool:
        return not self.fatal and all(ok for ok, _ in self.checks)


def observe(build: day7_build.Build, components: tuple[str, ...] = ALL_COMPONENTS, counts: dict[str, int] | None = None) -> Observation:
    d7.require_day7_profile()
    if counts is None:
        values_all = d7.helm_values(all_values=True)
        if values_all is None:
            return Observation([], "`helm get values --all` unreadable")
        try:
            counts = expected_counts(values_all)
        except (KeyError, TypeError, ValueError) as exc:
            return Observation([], f"release values lack replica counts: {exc}")
    pods_by_component: dict[str, list[dict]] = {}
    for component in components:
        pods = d7.list_json("pods", selector(component))
        if pods is None:
            return Observation([], f"{component} Pods unreadable")
        pods_by_component[component] = pods
    nodes = sorted({p.get("spec", {}).get("nodeName") for pods in pods_by_component.values() for p in pods if p.get("spec", {}).get("nodeName")})
    node_images = {node: read_node_images(node) for node in nodes}
    checks: list[tuple[bool, str]] = []
    for component in components:
        checks += evaluate_component(component, pods_by_component[component], counts[component], build.image(component), node_images)
    return Observation(checks)


def wait_for_running_images(
    rec: d7.Recorder,
    build: day7_build.Build,
    components: tuple[str, ...] = ALL_COMPONENTS,
    counts: dict[str, int] | None = None,
    timeout: float = WAIT_TIMEOUT_SECONDS,
    poll: float = WAIT_POLL_SECONDS,
    sleep=time.sleep,
    monotonic=time.monotonic,
    label: str = "running images",
) -> bool:
    """Bounded: re-observes while the rollout converges; records the
    FINAL observation's every check. Unreadable state fails at once."""
    deadline = monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        obs = observe(build, components, counts)
        if obs.fatal:
            return rec.record(False, f"{label}: {obs.fatal} - fails closed (not retried)")
        if obs.ok or monotonic() >= deadline:
            break
        sleep(poll)
    for ok, msg in obs.checks:
        rec.record(ok, f"{label}: {msg}")
    return rec.record(obs.ok, f"{label}: build {build.build_id} {'verified in every running container' if obs.ok else 'NOT verified'} after {attempts} observation(s)")


def baseline_exists() -> bool:
    """True once `day7-baseline` wrote this run's strategy baseline (the
    Makefile exports its path for every Day 7 run; before capture - e.g.
    right after `day7-deploy` - the file does not exist yet)."""
    path = os.environ.get(d7.STRATEGY_BASELINE_PATH_ENV)
    return bool(path) and os.path.lexists(path)


def main() -> int:
    rec = d7.Recorder()
    print(f"# Day 7 running-image gate (context {kube.CONTEXT}) - READ ONLY")
    try:
        d7.require_day7_profile()
        kube.verify_context()
        build = day7_build.load_current()
        # Once this run's strategy baseline exists, the gate judges the
        # baseline's build: an out-of-band `day7-build-record` that
        # re-pointed current.json is refused, never silently adopted.
        if baseline_exists():
            build = d7.require_baseline_build(d7.load_strategy_baseline())
            print(f"# this run's strategy baseline was captured under build {build.build_id}, which equals the current build (a mismatch would have been refused)")
    except (RuntimeError, OSError, ValueError, day7_build.BuildError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"# expected build {build.build_id}: " + ", ".join(f"{i.component}={i.config_digest}" for i in build.images))
    ok = wait_for_running_images(rec, build)
    failures = rec.failures()
    print(f"\n{len(rec.results) - len(failures)}/{len(rec.results)} running-image checks passed")
    if not ok or failures:
        print(f"FAIL: {len(failures)} running-image check(s) failed", file=sys.stderr)
        return 1
    print(f"PASS: every running gateway/app/state (and candidate) container runs verified build {build.build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

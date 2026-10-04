#!/usr/bin/env python3
"""
DAY8: the disposable scaling image (scaling/), pinned by content the
same way Day 7 pins the workload images - but recorded in the Day 8
run's private directory, never in the Day 7 build store, and never
touching the three application images.

  record        (cluster-free) config digest of the freshly built local
                `maops-kubernetes-scaling:day8` (from `docker save`, the
                digest kind a kind node's containerd reports; must be
                single-platform linux/amd64) -> local tag
                `maops-kubernetes-scaling:day8-cfg-<hex>`, digest
                re-derived FROM that tag -> <run dir>/scaling-image.json
  load-kind     re-derives the pinned tag's digest, refuses if it moved,
                then `kind load`s it into maops-k8s-day7 only
  verify-nodes  every maops-k8s-day7 node holds the pinned tag with the
                recorded config digest (crictl inspecti, read-only)

Day 8 Pods reference the pinned tag with imagePullPolicy Never, so a
Pod can only ever run these exact bytes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import day7_image_check
import day8_common as common
import day8_objects

SOURCE_TAG = f"{day8_objects.SCALING_IMAGE_REPOSITORY}:day8"


def pinned_ref(config_digest: str) -> str:
    """Pure. `sha256:<hex>` -> `maops-kubernetes-scaling:day8-cfg-<hex>`."""
    tag = day7_build.pinned_tag(config_digest, version="day8")
    return day8_objects.check_image(f"{day8_objects.SCALING_IMAGE_REPOSITORY}:{tag}")


def digest_from_ref(ref: str) -> str:
    day8_objects.check_image(ref)
    return day7_build.digest_from_tag(ref.split(":", 1)[1], version="day8")


def record(digest_of=None, run=None) -> dict:
    digest_of = digest_of or day7_build._saved_config_digest
    run = run or day7_build._run
    digest = digest_of(SOURCE_TAG)
    ref = pinned_ref(digest)
    tagged = run(["docker", "tag", SOURCE_TAG, ref], day7_build.DOCKER_TIMEOUT_SECONDS)
    if tagged.returncode != 0:
        raise common.Day8Error(f"docker tag {SOURCE_TAG} {ref} failed: {tagged.stderr.strip()[:300]}")
    again = digest_of(ref)
    if again != digest:
        raise common.Day8Error(f"{ref} resolves to config {again}, not {digest} - the local image changed while recording")
    entry = {"source": SOURCE_TAG, "ref": ref, "config_digest": digest, "node_ref": day7_build.node_ref(ref), "recorded_at": common.utc_now()}
    common.write_evidence(common.IMAGE_RECORD, entry)
    return entry


def load_kind(digest_of=None, run=None) -> str:
    common.require_cluster_profile()
    digest_of = digest_of or day7_build._saved_config_digest
    run = run or day7_build._run
    ref = common.scaling_image()
    expected = digest_from_ref(ref)
    actual = digest_of(ref)
    if actual != expected:
        raise common.Day8Error(f"{ref} now resolves to config {actual}, not {expected} - NOT loaded")
    result = run(["kind", "load", "docker-image", ref, "--name", day7_build.DAY7_CLUSTER], day7_build.KIND_LOAD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise common.Day8Error(f"kind load {ref} failed: {result.stderr.strip()[:300]}")
    return ref


def verify_nodes() -> int:
    common.require_cluster_profile()
    checks = common.Checks("Day 8 scaling image per-node check (maops-k8s-day7, read-only)")
    ref = common.scaling_image()
    expected = digest_from_ref(ref)
    listed = subprocess.run(["kind", "get", "nodes", "--name", day7_build.DAY7_CLUSTER], capture_output=True, text=True, timeout=30)
    nodes, rejected = day7_image_check.parse_kind_nodes(listed.stdout if listed.returncode == 0 else "")
    checks.record(listed.returncode == 0 and len(nodes) == day7_image_check.EXPECTED_NODE_COUNT and not rejected, f"kind lists {len(nodes)} maops-k8s-day7 node(s) {nodes} (expected {day7_image_check.EXPECTED_NODE_COUNT}, rejected {rejected})")
    for node in nodes:
        out = common.node_exec(node, "crictl", "inspecti", "-o", "json", day7_build.node_ref(ref))
        if out.returncode != 0:
            checks.record(False, f"{node}: {day7_build.node_ref(ref)} NOT present ({out.stderr.strip()[:200]})")
            continue
        ok, detail = day7_image_check.node_image_verdict(out.stdout, ref, expected)
        checks.record(ok, f"{node}: {detail}")
    return checks.finish(f"every maops-k8s-day7 node holds {ref}")


def main() -> int:
    commands = ("record", "load-kind", "verify-nodes")
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(f"usage: day8_image.py {'|'.join(commands)}", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "record":
            entry = record()
            print(f"PASS: {entry['source']} config {entry['config_digest']} pinned as {entry['ref']} (recorded in the run directory)")
            return 0
        if sys.argv[1] == "load-kind":
            print(f"PASS: loaded {load_kind()} into {day7_build.DAY7_CLUSTER}")
            return 0
        return verify_nodes()
    except (common.Day8Error, day7_build.BuildError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
DAY7: the image contract for the isolated Day 7 cluster - build, local
digest proof, load into maops-k8s-day7 ONLY, and per-node proof, all
BEFORE `make day7-deploy`.

The chart's images are `maops-kubernetes-{gateway,app,state}:<VERSION>`
with `imagePullPolicy: IfNotPresent` and no registry: a Pod can only
start if the kind node's containerd already holds that exact tag. A
missing tag would make kubelet try to pull from Docker Hub and fail
(ErrImagePull) mid-deploy; a stale tag (an older build under the same
name) would run the wrong content silently. This script turns both into
a hard failure before deploy.

Which digest is compared - measured, not assumed: on this host's Docker
engine (containerd image store) `docker inspect .Id` is the OCI
platform-manifest digest, while a kind node's `crictl inspecti` reports
the image CONFIG digest (docs/architecture.md, "DAY4: measured image
digest mapping"). So the host side is derived from `docker save`: its
`manifest.json` names the config blob, whose sha256 is re-computed from
the blob bytes here (never trusted from a filename alone). That config
digest is then compared with every Day 7 node's `crictl inspecti
.status.id` for the same tag.

  local  (after `image-build`, cluster-free): each of the three tags
         exists; `docker save` yields exactly one image whose RepoTags
         include the tag; the config blob hashes to its digest; the
         config says os=linux, architecture=amd64 (the single-platform
         build IMAGE_BUILD_FLAGS requires for kind loading).
  nodes  (after `image-load`, read-only): `kind get nodes --name
         maops-k8s-day7` returns exactly the 3 Day 7 node containers (a
         Day 6 node name is rejected); in EVERY node, `crictl inspecti`
         finds `docker.io/library/<tag>` with that tag in repoTags and
         `.status.id` equal to the local config digest; AND, for the
         current verified build (scripts/day7_build.py), every pinned
         `<repo>:<VERSION>-cfg-<digest>` tag is present on every node
         with `.status.id` equal to the digest its own name carries (the
         tags the Day 7 Pod templates actually reference). The mutable
         `<VERSION>` tags stay verified because the Day 7 helper Pods
         (storage/NetworkPolicy/validation-client probes) use them.

What a matching digest does NOT mean: 0.7.0 is a release VERSION tag.
The workload sources and Dockerfiles are unchanged since v0.6.0 (see
`make day7-history-audit`), so these are rebuilds of the same
application, not a new application binary. The gateway candidate uses
this same gateway image; it differs from stable only by configuration
(its ConfigMap's APP_MESSAGE).

Never loads, tags, pulls, pushes or deletes an image; never touches
maops-k8s-day6.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
REPOSITORIES = ("maops-kubernetes-gateway", "maops-kubernetes-app", "maops-kubernetes-state")
DAY7_CLUSTER = "maops-k8s-day7"
EXPECTED_NODE_COUNT = 3
_NODE_RE = re.compile(rf"^{re.escape(DAY7_CLUSTER)}-(control-plane|worker\d*)$")
EXPECTED_PLATFORM = ("linux", "amd64")
SAVE_TIMEOUT_SECONDS = 180
EXEC_TIMEOUT_SECONDS = 30

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((bool(ok), message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}", flush=True)
    return bool(ok)


def image_refs(version: str = VERSION) -> list[str]:
    return [f"{repo}:{version}" for repo in REPOSITORIES]


def node_ref(ref: str) -> str:
    """How containerd names a `kind load`ed local image."""
    return f"docker.io/library/{ref}"


class ImageContractError(Exception):
    pass


def config_from_saved_tar(tar_path: str, ref: str) -> tuple[str, dict]:
    """Pure (file in, facts out). Returns (config_digest, config_json)
    for `ref` from a `docker save` archive, verifying: exactly one image
    in manifest.json, its RepoTags include `ref`, and the config blob's
    real sha256 equals the digest its path claims."""
    try:
        with tarfile.open(tar_path) as tar:
            members = {m.name: m for m in tar.getmembers()}
            if "manifest.json" not in members:
                raise ImageContractError("docker save archive has no manifest.json")
            manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ImageContractError(f"expected exactly one image in manifest.json, found {len(manifest) if isinstance(manifest, list) else manifest!r}")
            entry = manifest[0]
            tags = entry.get("RepoTags") or []
            if ref not in tags and node_ref(ref) not in tags:
                raise ImageContractError(f"manifest.json RepoTags {tags} do not include {ref!r}")
            config_path = entry.get("Config") or ""
            if config_path not in members:
                raise ImageContractError(f"config blob {config_path!r} missing from the archive")
            blob = tar.extractfile(members[config_path]).read()
    except (tarfile.TarError, OSError, json.JSONDecodeError) as exc:
        raise ImageContractError(f"unreadable docker save archive: {exc}") from exc
    claimed = Path(config_path).name.removesuffix(".json")
    actual = hashlib.sha256(blob).hexdigest()
    if claimed != actual:
        raise ImageContractError(f"config blob {config_path!r} hashes to {actual}, not the digest its name claims")
    try:
        config = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ImageContractError(f"config blob is not JSON: {exc}") from exc
    return f"sha256:{actual}", config


def platform_ok(config: dict) -> tuple[bool, str]:
    platform = (config.get("os"), config.get("architecture"))
    return platform == EXPECTED_PLATFORM, f"{platform[0]}/{platform[1]}"


def parse_kind_nodes(output: str) -> tuple[list[str], list[str]]:
    """Pure: (day7_nodes, rejected_names)."""
    names = [n.strip() for n in output.splitlines() if n.strip()]
    return [n for n in names if _NODE_RE.match(n)], [n for n in names if not _NODE_RE.match(n)]


def node_image_verdict(inspect_stdout: str, ref: str, expected_digest: str) -> tuple[bool, str]:
    """Pure: evaluates `crictl inspecti -o json <ref>` output."""
    try:
        status = json.loads(inspect_stdout).get("status") or {}
    except (json.JSONDecodeError, AttributeError):
        return False, "unparseable crictl output"
    digest = status.get("id")
    tags = status.get("repoTags") or []
    if node_ref(ref) not in tags:
        return False, f"tag {node_ref(ref)} not among repoTags {tags}"
    if digest != expected_digest:
        return False, f"config digest {digest} != local build {expected_digest}"
    return True, f"{node_ref(ref)} present, config digest {digest}"


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def local_digests() -> dict[str, str]:
    """ref -> config digest for every local image that passes; records
    one finding per check."""
    digests: dict[str, str] = {}
    for ref in image_refs():
        inspect = _run(["docker", "image", "inspect", ref, "--format", "{{.Id}}"], EXEC_TIMEOUT_SECONDS)
        if not record(inspect is not None and inspect.returncode == 0, f"local image {ref} exists (docker image inspect)"):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = str(Path(tmp) / "image.tar")
            saved = _run(["docker", "save", "-o", tar_path, ref], SAVE_TIMEOUT_SECONDS)
            if not record(saved is not None and saved.returncode == 0, f"docker save {ref} succeeded"):
                continue
            try:
                digest, config = config_from_saved_tar(tar_path, ref)
            except ImageContractError as exc:
                record(False, f"{ref}: {exc}")
                continue
        ok, platform = platform_ok(config)
        if record(ok, f"{ref}: single image, config digest {digest}, platform {platform} (expected linux/amd64)"):
            digests[ref] = digest
    return digests


def verify_pinned_build(nodes: list[str], digests: dict[str, str]) -> None:
    """Every node holds the current build's pinned tags, each resolving
    to the config digest its name carries - which must also be the
    digest of today's local `<VERSION>` image (the build is current)."""
    import day7_build

    try:
        build = day7_build.load_current()
    except day7_build.BuildError as exc:
        record(False, f"no current verified build to check on the nodes ({exc}) - run `make day7-build-record`")
        return
    for image in build.images:
        local = digests.get(f"{image.repository}:{VERSION}")
        record(local == image.config_digest, f"current build {build.build_id[:12]}: {image.ref} == today's local {image.repository}:{VERSION} config ({local})")
        for node in nodes:
            out = _run(["docker", "exec", node, "crictl", "inspecti", "-o", "json", image.node_ref], EXEC_TIMEOUT_SECONDS)
            if out is None or out.returncode != 0:
                record(False, f"{node}: pinned {image.node_ref} NOT present (crictl inspecti failed: {'' if out is None else out.stderr.strip()[:200]})")
                continue
            ok, detail = node_image_verdict(out.stdout, image.ref, image.config_digest)
            record(ok, f"{node}: pinned {detail}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("local", "nodes"):
        print("usage: day7_image_check.py local|nodes", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    print(f"# Day 7 image contract ({mode}) - images {', '.join(image_refs())}")
    digests = local_digests()
    if mode == "nodes":
        listing = _run(["kind", "get", "nodes", "--name", DAY7_CLUSTER], EXEC_TIMEOUT_SECONDS)
        nodes, rejected = parse_kind_nodes(listing.stdout if listing and listing.returncode == 0 else "")
        record(len(nodes) == EXPECTED_NODE_COUNT and not rejected, f"{DAY7_CLUSTER} nodes: {nodes} (expected {EXPECTED_NODE_COUNT}; rejected {rejected})")
        for node in nodes:
            for ref in image_refs():
                if ref not in digests:
                    record(False, f"{node}: cannot verify {ref} - the local image failed its own check")
                    continue
                out = _run(["docker", "exec", node, "crictl", "inspecti", "-o", "json", node_ref(ref)], EXEC_TIMEOUT_SECONDS)
                if out is None or out.returncode != 0:
                    record(False, f"{node}: {node_ref(ref)} NOT present (crictl inspecti failed: {'' if out is None else out.stderr.strip()[:200]})")
                    continue
                ok, detail = node_image_verdict(out.stdout, ref, digests[ref])
                record(ok, f"{node}: {detail}")
        verify_pinned_build(nodes, digests)
    failures = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(failures)}/{len(results)} image contract checks passed")
    if failures:
        print(f"FAIL: {len(failures)} image check(s) failed - do NOT deploy", file=sys.stderr)
        return 1
    print("PASS: " + ("local images built and single-platform" if mode == "local" else f"every {DAY7_CLUSTER} node holds the exact locally built images"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

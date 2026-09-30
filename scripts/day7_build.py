#!/usr/bin/env python3
"""
DAY7: the verified BUILD a Day 7 release runs - one identity for the
three workload images, pinned into every Pod template by Helm.

The defect this closes (second live run, `ce55f5fb...`, 2026-09-28):
`image-build` rebuilt the images under the same mutable `<VERSION>` tags
and `kind load` put them on every node (verified per node), but the chart
still said `maops-kubernetes-*:<VERSION>`, so `day7-deploy` was a no-op
Helm upgrade - no Pod template changed, nothing rolled out, and the
stable Pods kept running the previous build while every gate passed.

The fix: a build is named by its content.

  - `record` (cluster-free, after `day7-image-verify-local`): computes
    each local image's CONFIG digest from `docker save` (the digest kind
    a kind node's containerd reports - see day7_image_check), creates a
    content-derived local tag `<repo>:<VERSION>-cfg-<64-hex config
    digest>` for each, re-derives the digest FROM THAT TAG (so the tag
    provably names its content), and writes one build record:

        $DAY7_BUILD_ROOT/<build id>/build.json   (0600, O_EXCL)
        $DAY7_BUILD_ROOT/<build id>/values.yaml  (0600, O_EXCL)
        $DAY7_BUILD_ROOT/current.json            (0600, atomic replace)

    DAY7_BUILD_ROOT defaults to $HOME/.local/state/maops-kubernetes-
    platform/day7-builds (0700; never /tmp, never inside the repo). The
    build id is the sha256 of the three component=digest lines, so the
    same content always gets the same id and a changed image always a
    new one. An existing build directory is reused only if its records
    are byte-identical to what this build would write.
  - `values.yaml` is the build OVERLAY - exactly
    images.{gateway,app,state}.tag, nothing else - passed as the SECOND
    `-f` after a stage file (`--reset-values -f <stage> -f <overlay>`).
    Every helm-values/day7 stage sets build.requirePinnedTags=true, so
    the chart REFUSES to render a Day 7 stage without it
    (maops.validatePinnedBuild). A new build therefore always changes the
    gateway, app and state Pod templates (and the candidate's, which uses
    the gateway image): the Deployments and the StatefulSet roll out
    through Helm; the StatefulSet's PVC is untouched (volumeClaimTemplates
    are immutable and not changed by an image).
  - `load-kind` loads the three pinned tags into maops-k8s-day7 only.
  - `values-path` prints the current overlay path (Makefile day7-deploy).

`current.json` names the build the NEXT deploy uses. Scripts that run
against an existing release never trust it alone: the strategy baseline
records the build it was captured under, and every experiment, the
stable check and the final gate refuse when the baseline's build, the
current build and the deployed Helm values disagree (a new build means a
new run ID and new baselines - an old baseline is never claimed to match
a new rollout).

Never pulls, pushes or deletes an image, never touches maops-k8s-day6,
never reads a Secret.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import private_run_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION = (REPO_ROOT / "VERSION").read_text().strip()
BUILD_ROOT_ENV = "DAY7_BUILD_ROOT"
DEFAULT_BUILD_ROOT = Path.home() / ".local" / "state" / "maops-kubernetes-platform" / "day7-builds"
CURRENT_FILE = "current.json"
BUILD_FILE = "build.json"
VALUES_FILE = "values.yaml"
DAY7_CLUSTER = "maops-k8s-day7"
COMPONENTS = ("gateway", "app", "state")
REPOSITORIES = {"gateway": "maops-kubernetes-gateway", "app": "maops-kubernetes-app", "state": "maops-kubernetes-state"}
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_BUILD_ID_RE = re.compile(r"^[0-9a-f]{64}$")
KIND_LOAD_TIMEOUT_SECONDS = 600
DOCKER_TIMEOUT_SECONDS = 60


class BuildError(Exception):
    """Any violation of the build-identity contract."""


def tag_pattern(version: str = VERSION) -> re.Pattern:
    return re.compile(rf"^{re.escape(version)}-cfg-([0-9a-f]{{64}})$")


def pinned_tag(config_digest: str, version: str = VERSION) -> str:
    """Pure. `sha256:<hex>` -> `<version>-cfg-<hex>`."""
    m = _DIGEST_RE.match(config_digest or "")
    if not m:
        raise BuildError(f"not a sha256 config digest: {config_digest!r}")
    return f"{version}-cfg-{m.group(1)}"


def digest_from_tag(tag: str, version: str = VERSION) -> str:
    m = tag_pattern(version).match(tag or "")
    if not m:
        raise BuildError(f"not a pinned build tag {version}-cfg-<64 hex>: {tag!r}")
    return f"sha256:{m.group(1)}"


def node_ref(ref: str) -> str:
    """How containerd names a `kind load`ed local image."""
    return f"docker.io/library/{ref}"


@dataclass(frozen=True)
class BuildImage:
    component: str
    repository: str
    config_digest: str
    tag: str

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}"

    @property
    def node_ref(self) -> str:
        return node_ref(self.ref)


@dataclass(frozen=True)
class Build:
    build_id: str
    version: str
    images: tuple[BuildImage, ...]

    def image(self, component: str) -> BuildImage:
        """`candidate` runs the gateway image."""
        key = "gateway" if component == "candidate" else component
        for image in self.images:
            if image.component == key:
                return image
        raise BuildError(f"build {self.build_id} has no {key!r} image")

    def overlay(self) -> dict:
        return {"images": {i.component: {"tag": i.tag} for i in self.images}}

    def record(self) -> dict:
        return {
            "schema": 1,
            "build_id": self.build_id,
            "version": self.version,
            "images": {i.component: {"repository": i.repository, "config_digest": i.config_digest, "tag": i.tag} for i in self.images},
        }


def build_id_for(digests: dict[str, str]) -> str:
    """Pure: sha256 over `component=digest` lines in fixed order."""
    lines = "".join(f"{c}={digests[c]}\n" for c in COMPONENTS)
    return hashlib.sha256(lines.encode()).hexdigest()


def build_from_digests(digests: dict[str, str], version: str = VERSION) -> Build:
    """Pure. `digests`: component -> `sha256:<hex>` config digest."""
    if set(digests) != set(COMPONENTS):
        raise BuildError(f"expected config digests for exactly {COMPONENTS}, got {sorted(digests)}")
    images = tuple(BuildImage(c, REPOSITORIES[c], digests[c], pinned_tag(digests[c], version)) for c in COMPONENTS)
    return Build(build_id_for(digests), version, images)


def build_from_record(record: dict, version: str = VERSION) -> Build:
    """Pure. Re-derives everything from the digests and refuses any
    record whose id, tags or repositories disagree with them."""
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise BuildError(f"unsupported build record: {record!r:.200}")
    if record.get("version") != version:
        raise BuildError(f"build record is for version {record.get('version')!r}, this checkout is {version!r}")
    images = record.get("images") or {}
    try:
        digests = {c: images[c]["config_digest"] for c in COMPONENTS}
    except (KeyError, TypeError) as exc:
        raise BuildError(f"build record lacks a config digest: {exc}") from exc
    build = build_from_digests(digests, version)
    if record != build.record():
        raise BuildError(f"build record {record.get('build_id')!r} is inconsistent with its own digests (expected {build.record()!r})")
    return build


def overlay_yaml(build: Build) -> str:
    """Pure, deterministic, block-style (k8s_yaml-loadable)."""
    lines = [
        f"# DAY7 verified build {build.build_id} (scripts/day7_build.py) - pass as the",
        "# SECOND -f after a helm-values/day7 stage file. Image tags only.",
        "images:",
    ]
    for image in build.images:
        lines += [f"  {image.component}:", f"    tag: \"{image.tag}\""]
    return "\n".join(lines) + "\n"


def validate_overlay(values: dict, version: str = VERSION) -> None:
    """An overlay may pin image tags and NOTHING else."""
    if set(values) != {"images"} or not isinstance(values["images"], dict) or set(values["images"]) != set(COMPONENTS):
        raise BuildError(f"a build overlay must contain exactly images.{{{','.join(COMPONENTS)}}}.tag, got {values!r:.300}")
    for component in COMPONENTS:
        entry = values["images"][component]
        if not isinstance(entry, dict) or set(entry) != {"tag"}:
            raise BuildError(f"overlay images.{component} must contain only 'tag', got {entry!r}")
        digest_from_tag(entry["tag"], version)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Pure. Helm's values merge for mappings: overlay wins, nested
    mappings merge, nothing is mutated."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# Private build store
# --------------------------------------------------------------------------


def build_root() -> Path:
    return Path(os.environ.get(BUILD_ROOT_ENV) or DEFAULT_BUILD_ROOT)


def _ensure_private_dir(path: Path, **loc) -> None:
    private_run_dir.check_location(path, **loc)
    for parent in reversed([path, *path.parents]):
        if not parent.exists():
            os.mkdir(parent, private_run_dir.DIR_MODE)
            os.chmod(parent, private_run_dir.DIR_MODE)
    private_run_dir.validate_run_dir(path, **loc)


def _write_exclusive(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, private_run_dir.FILE_MODE)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, private_run_dir.FILE_MODE)


def _read_private(path: Path, **loc) -> str:
    kwargs = {k: v for k, v in loc.items() if k in ("home", "forbidden_roots")}
    try:
        private_run_dir.validate_private_file(path, **kwargs)
    except private_run_dir.PrivateRunDirError as exc:
        raise BuildError(str(exc)) from exc
    return path.read_text()


def store_build(build: Build, root: Path | None = None, **loc) -> Path:
    """Writes the build's records once. Reuses an existing directory only
    if both files are byte-identical to what this build writes. A new
    directory is assembled under a temporary name and renamed into place,
    so an interrupted write never leaves a half-written build behind."""
    root = root or build_root()
    record_text = json.dumps(build.record(), indent=2, sort_keys=True) + "\n"
    values_text = overlay_yaml(build)
    try:
        _ensure_private_dir(root, **loc)
        directory = root / build.build_id
        if directory.exists():
            private_run_dir.validate_run_dir(directory, **loc)
            for name, text in ((BUILD_FILE, record_text), (VALUES_FILE, values_text)):
                if _read_private(directory / name, **loc) != text:
                    raise BuildError(f"existing build directory {directory} holds a different {name} - refusing to reuse it")
            return directory
        staging = Path(tempfile.mkdtemp(dir=root, prefix=f".{build.build_id[:12]}-"))
        try:
            os.chmod(staging, private_run_dir.DIR_MODE)
            _write_exclusive(staging / BUILD_FILE, record_text)
            _write_exclusive(staging / VALUES_FILE, values_text)
            # POSIX rename() would silently REPLACE an existing empty
            # directory, so re-check immediately before it (the root is a
            # private 0700 directory of this user; the result is re-validated).
            if directory.exists() or directory.is_symlink():
                raise BuildError(f"build directory {directory} appeared while recording - refusing to replace it")
            os.rename(staging, directory)
        except BaseException:
            for name in (BUILD_FILE, VALUES_FILE):
                if (staging / name).exists():
                    (staging / name).unlink()
            if staging.exists():
                staging.rmdir()
            raise
        private_run_dir.validate_run_dir(directory, **loc)
        return directory
    except private_run_dir.PrivateRunDirError as exc:
        raise BuildError(str(exc)) from exc


def set_current(build: Build, root: Path | None = None, **loc) -> None:
    """Atomically points current.json at an already-stored build."""
    root = root or build_root()
    load_build(build.build_id, root, **loc)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".current-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"build_id": build.build_id}, f)
        os.chmod(tmp, private_run_dir.FILE_MODE)
        os.replace(tmp, root / CURRENT_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_build(build_id: str, root: Path | None = None, **loc) -> Build:
    """Loads and fully re-validates one stored build (record consistent
    with its digests; overlay file byte-identical to the record's)."""
    root = root or build_root()
    if not _BUILD_ID_RE.match(build_id or ""):
        raise BuildError(f"not a build id: {build_id!r}")
    directory = root / build_id
    try:
        record = json.loads(_read_private(directory / BUILD_FILE, **loc))
    except json.JSONDecodeError as exc:
        raise BuildError(f"{directory / BUILD_FILE} is not JSON: {exc}") from exc
    build = build_from_record(record)
    if build.build_id != build_id:
        raise BuildError(f"{directory} holds build {build.build_id}, not {build_id}")
    if _read_private(directory / VALUES_FILE, **loc) != overlay_yaml(build):
        raise BuildError(f"{directory / VALUES_FILE} does not match build {build_id}")
    return build


def load_current(root: Path | None = None, **loc) -> Build:
    root = root or build_root()
    try:
        pointer = json.loads(_read_private(root / CURRENT_FILE, **loc))
    except json.JSONDecodeError as exc:
        raise BuildError(f"{root / CURRENT_FILE} is not JSON: {exc}") from exc
    if not isinstance(pointer, dict) or set(pointer) != {"build_id"}:
        raise BuildError(f"{root / CURRENT_FILE} must contain only a build_id, got {pointer!r}")
    return load_build(pointer["build_id"], root, **loc)


def values_path(build: Build, root: Path | None = None) -> Path:
    return (root or build_root()) / build.build_id / VALUES_FILE


def sample_build(version: str = VERSION) -> Build:
    """A SYNTHETIC build for static rendering only (helm-lint/helm-check):
    its tags exist on no node, so it can never run."""
    return build_from_digests({c: "sha256:" + hashlib.sha256(f"maops-static-sample-{c}".encode()).hexdigest() for c in COMPONENTS}, version)


# --------------------------------------------------------------------------
# Local Docker / kind (the only mutating commands: `docker tag`, `kind load`)
# --------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def record_local_build(digest_of=None, run=_run) -> Build:
    """Tags each verified local image with its content-derived tag and
    proves the tag names that content (digest re-derived from the new
    tag, via `docker save`)."""
    digest_of = digest_of or _saved_config_digest
    digests = {}
    for component in COMPONENTS:
        source = f"{REPOSITORIES[component]}:{VERSION}"
        digests[component] = digest_of(source)
    build = build_from_digests(digests)
    for image in build.images:
        source = f"{image.repository}:{VERSION}"
        tagged = run(["docker", "tag", source, image.ref], DOCKER_TIMEOUT_SECONDS)
        if tagged.returncode != 0:
            raise BuildError(f"docker tag {source} {image.ref} failed: {tagged.stderr.strip()[:300]}")
        again = digest_of(image.ref)
        if again != image.config_digest:
            raise BuildError(f"{image.ref} resolves to config {again}, not {image.config_digest} - the local image changed while recording; rebuild and retry")
    return build


def _saved_config_digest(ref: str) -> str:
    import day7_image_check

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = str(Path(tmp) / "image.tar")
        saved = _run(["docker", "save", "-o", tar_path, ref], day7_image_check.SAVE_TIMEOUT_SECONDS)
        if saved.returncode != 0:
            raise BuildError(f"docker save {ref} failed: {saved.stderr.strip()[:300]}")
        try:
            digest, config = day7_image_check.config_from_saved_tar(tar_path, ref)
        except day7_image_check.ImageContractError as exc:
            raise BuildError(f"{ref}: {exc}") from exc
    ok, platform = day7_image_check.platform_ok(config)
    if not ok:
        raise BuildError(f"{ref}: platform {platform}, expected linux/amd64")
    return digest


def load_into_kind(build: Build, run=_run, digest_of=None) -> None:
    """Re-derives each pinned tag's config digest immediately before
    loading it, so a tag moved after `record` is refused, not loaded.
    Refuses outside the Day 7 profile on its own (it reaches a cluster)."""
    import kube

    if kube.PROFILE != "day7":
        raise BuildError(f"refusing to load images outside the Day 7 profile (profile {kube.PROFILE!r})")
    digest_of = digest_of or _saved_config_digest
    for image in build.images:
        actual = digest_of(image.ref)
        if actual != image.config_digest:
            raise BuildError(f"{image.ref} now resolves to config {actual}, not {image.config_digest} - NOT loaded (re-run `make day7-build-record`)")
        result = run(["kind", "load", "docker-image", image.ref, "--name", DAY7_CLUSTER], KIND_LOAD_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise BuildError(f"kind load {image.ref} into {DAY7_CLUSTER} failed: {result.stderr.strip()[:300]}")
        print(f"PASS: loaded {image.ref} into {DAY7_CLUSTER}", flush=True)


def main() -> int:
    commands = ("record", "values-path", "show", "load-kind", "sample-overlay")
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        print(f"usage: day7_build.py {'|'.join(commands)}", file=sys.stderr)
        return 2
    command = sys.argv[1]
    try:
        if command == "sample-overlay":
            sys.stdout.write("# SYNTHETIC static-check overlay - these tags exist on no node and can never run\n" + overlay_yaml(sample_build()))
            return 0
        if command == "record":
            print(f"# Day 7 build record - pin {', '.join(f'{r}:{VERSION}' for r in REPOSITORIES.values())} to content-derived tags")
            build = record_local_build()
            directory = store_build(build)
            set_current(build)
            for image in build.images:
                print(f"PASS: {image.component}: config {image.config_digest} -> {image.ref}")
            print(f"PASS: build {build.build_id} recorded at {directory} and made current")
            return 0
        build = load_current()
        if command == "values-path":
            print(values_path(build))
        elif command == "show":
            print(json.dumps(build.record(), indent=2, sort_keys=True))
        elif command == "load-kind":
            load_into_kind(build)
            print(f"PASS: build {build.build_id} loaded into {DAY7_CLUSTER}")
        return 0
    except (BuildError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

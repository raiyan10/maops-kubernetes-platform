#!/usr/bin/env python3
"""
Render charts/maops-kubernetes-platform with `helm template` and run
repository-owned static validation against the result - the Day 6
analogue of `scripts/manifest_check.py` for k8s/base. Never contacts a
live cluster (`helm template` is a pure local render, exactly like
`kubectl kustomize`).

Also independently proves k8s/base remains the frozen Day 5 source and
is not part of the Day 6 Helm deploy: reads `scripts/validate_manifests.py`'s
own EXPECTED_VERSION/EXPECTED_INSTANCE constants (never re-rendering
k8s/base itself, and never importing anything from
`scripts/validate_helm_chart.py` into it or vice versa) and asserts
they are still pinned to Day 5's values, not silently advanced to Day 6.

DAY6 remediation: also runs
`scripts/validate_gateway_values_configmap.py` against
`k8s/day6/gateway-values-configmap.yaml` - a plain, hand-authored
kubectl-applied manifest, never Helm-templated, so it is validated by a
separate, purpose-built checker rather than folded into the
`helm template` render above. Likewise (DAY6 review SEC-3),
`scripts/validate_cilium_probe_policy.py` pins the exact shape of
`k8s/day6/cilium-ambient-probe-policy.yaml` - the one cluster-scoped
CiliumClusterwideNetworkPolicy this project applies.

DAY7: also renders the chart once per Day 7 stage values file
(helm-values/day7/*.yaml) with the Day 7 release name, and validates each
render against an expectation DECLARED HERE (DAY7_STAGE_EXPECTATIONS) -
never derived from the stage file itself, so a stage file that drifts
from its intended state (e.g. a weight edited to 80/20, or a candidate
flag flipped) fails this check. Every stage file on disk must have a
declared expectation and vice versa. DAY7 image contract: each stage is
rendered with a SYNTHETIC build overlay (scripts/day7_build.py
sample_build - tags that exist on no node) and must carry exactly its
pinned image refs; every stage rendered WITHOUT an overlay must be
refused by the chart; and two different builds must change every
workload Pod template's image and nothing else. The default render keeps its Day 6
contract (release maops-kubernetes-platform-day6, stable-only, 30
objects).

Usage:
    python3 scripts/helm_check.py [path/to/chart]
"""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import k8s_yaml
import validate_manifests
import validate_gateway_values_configmap
import validate_cilium_probe_policy
from validate_helm_chart import Finding, RenderExpectation, run_checks

DEFAULT_CHART_DIR = "charts/maops-kubernetes-platform"
RELEASE_NAME = "maops-kubernetes-platform-day6"
DAY7_RELEASE_NAME = "maops-kubernetes-platform-day7"
RELEASE_NAMESPACE = "maops-platform"
REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_VALUES_CONFIGMAP_PATH = REPO_ROOT / "k8s" / "day6" / "gateway-values-configmap.yaml"
CILIUM_PROBE_POLICY_PATH = REPO_ROOT / "k8s" / "day6" / "cilium-ambient-probe-policy.yaml"
DAY7_STAGE_DIR = REPO_ROOT / "helm-values" / "day7"
DAY6_PLATFORM_DIR = REPO_ROOT / "k8s" / "day6"
DAY7_PLATFORM_DIR = REPO_ROOT / "k8s" / "day7"
DAY7_INSTANCE = "maops-kubernetes-platform-day7"
DAY7_PLATFORM_EXPECTED = {
    ("Namespace", "maops-platform", None),
    ("Namespace", "maops-day7-validation", None),
    ("Namespace", "maops-ingress", None),
    ("ServiceAccount", "maops-diagnostics", "maops-day7-validation"),
    ("ConfigMap", "maops-edge-gateway-values", "maops-ingress"),
    ("Gateway", "maops-edge", "maops-ingress"),
    ("CiliumClusterwideNetworkPolicy", "maops-allow-ambient-health-probes", None),
}

DAY7_VALIDATION_NAMESPACE = "maops-day7-validation"
_CANDIDATE = dict(instance=DAY7_RELEASE_NAME, validation_namespace=DAY7_VALIDATION_NAMESPACE, candidate_enabled=True)
DAY7_STAGE_EXPECTATIONS: dict[str, RenderExpectation] = {
    "stable.yaml": RenderExpectation(instance=DAY7_RELEASE_NAME, validation_namespace=DAY7_VALIDATION_NAMESPACE),
    "green-prepared.yaml": RenderExpectation(**_CANDIDATE, route_mode="stable"),
    "blue-green-cutover.yaml": RenderExpectation(**_CANDIDATE, route_mode="candidate"),
    "canary-90-10.yaml": RenderExpectation(**_CANDIDATE, route_mode="weighted", stable_weight=90, candidate_weight=10),
    "candidate-unready.yaml": RenderExpectation(**_CANDIDATE, route_mode="stable", candidate_fail_readiness=True),
    "recreate-prepared.yaml": RenderExpectation(**_CANDIDATE, route_mode="stable", candidate_strategy="Recreate"),
    "recreate-serving.yaml": RenderExpectation(**_CANDIDATE, route_mode="candidate", candidate_strategy="Recreate"),
    "recreate-changed.yaml": RenderExpectation(**_CANDIDATE, route_mode="candidate", candidate_strategy="Recreate"),
}


def render(chart_dir: str, release: str = RELEASE_NAME, values_files: tuple[str, ...] = ()) -> str:
    extra: list[str] = []
    for values_file in values_files:
        extra += ["-f", values_file]
    result = subprocess.run(
        ["helm", "template", release, chart_dir, "--namespace", RELEASE_NAMESPACE, *extra],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def check_k8s_base_still_frozen() -> Finding:
    """DAY6: k8s/base must never be advanced past its Day 5 pin - this
    is the one check in this file that looks outside the Helm chart, to
    close the specific risk the task's own self-review calls out:
    "accidental k8s/base changes." Reads validate_manifests.py's own
    constants rather than re-rendering k8s/base, since a stray edit to
    those constants (even without touching a single manifest file)
    would itself already be evidence the frozen-source boundary was
    violated."""
    ok = (
        validate_manifests.EXPECTED_VERSION == "0.5.0"
        and validate_manifests.EXPECTED_INSTANCE == "maops-kubernetes-platform-day5"
    )
    return Finding(
        ok=ok,
        name="scope.k8s_base_still_frozen_at_day5",
        detail=(
            f"expected validate_manifests.EXPECTED_VERSION == '0.5.0' and EXPECTED_INSTANCE == "
            f"'maops-kubernetes-platform-day5', found {validate_manifests.EXPECTED_VERSION!r} / "
            f"{validate_manifests.EXPECTED_INSTANCE!r} - k8s/base must never be advanced to Day 6"
        ),
    )


def _prefixed(stage: str, findings: list[Finding]) -> list[Finding]:
    return [Finding(ok=f.ok, name=f"day7[{stage}].{f.name}", detail=f.detail) for f in findings]


def _strings(value) -> list[str]:
    """Every string key/value in a parsed document (comments are never
    part of a parsed document, so only real object content is checked)."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *_strings(v)]]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def _platform_docs(directory: Path) -> list[dict]:
    """Parses a k8s/dayN directory with the repository's YAML subset. The
    gateway-values ConfigMap's `data` holds literal block scalars the
    subset parser deliberately does not accept (see
    validate_gateway_values_configmap.py), so only its object header
    (everything before `data:`) is parsed here; its data is checked by
    that dedicated validator and a raw-text identity scan."""
    docs: list[dict] = []
    for path in sorted(directory.glob("*.yaml")):
        text = path.read_text()
        if path.name == "gateway-values-configmap.yaml":
            text = text.split("\ndata:", 1)[0] + "\n"
        docs += [d for d in k8s_yaml.load_all(text) if d]
    return docs


def check_day7_platform_manifests() -> list[Finding]:
    """DAY7: k8s/day7/ carries the Day 7 cluster's own identities, has the
    same object set as k8s/day6/, and passes the same shape validators;
    k8s/day6/ itself keeps its Day 6 identities."""
    findings: list[Finding] = []
    day6_names = sorted(p.name for p in DAY6_PLATFORM_DIR.glob("*.yaml"))
    day7_names = sorted(p.name for p in DAY7_PLATFORM_DIR.glob("*.yaml"))
    findings.append(Finding(day6_names == day7_names, "day7.platform.same_files_as_day6", f"expected k8s/day7 files {day6_names}, found {day7_names}"))
    docs = _platform_docs(DAY7_PLATFORM_DIR)
    found = {(d.get("kind"), d.get("metadata", {}).get("name"), d.get("metadata", {}).get("namespace")) for d in docs}
    findings.append(Finding(found == DAY7_PLATFORM_EXPECTED, "day7.platform.object_set", f"expected {sorted(map(str, DAY7_PLATFORM_EXPECTED))}, found {sorted(map(str, found))}"))
    for d in docs:
        label = f"{d.get('kind')}/{d.get('metadata', {}).get('name')}"
        instance = (d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/instance")
        findings.append(Finding(instance == DAY7_INSTANCE, f"day7.platform[{label}].instance", f"expected instance {DAY7_INSTANCE!r}, found {instance!r}"))
        stale = [s for s in _strings(d) if "day6" in s.lower() or "day4" in s.lower()]
        findings.append(Finding(not stale, f"day7.platform[{label}].no_earlier_day_identity", f"expected no day4/day6 identity in object content, found {stale}"))
    platform_ns = next((d for d in docs if d.get("kind") == "Namespace" and d["metadata"]["name"] == "maops-platform"), {})
    findings.append(Finding((platform_ns.get("metadata", {}).get("labels") or {}).get("istio.io/dataplane-mode") == "ambient", "day7.platform.maops-platform_ambient", "expected maops-platform to stay ambient-enrolled"))
    validation_ns = next((d for d in docs if d.get("kind") == "Namespace" and d["metadata"]["name"] == "maops-day7-validation"), {})
    findings.append(Finding("istio.io/dataplane-mode" not in (validation_ns.get("metadata", {}).get("labels") or {}), "day7.platform.validation_namespace_not_enrolled", "expected maops-day7-validation to stay outside the mesh"))
    gateway = next((d for d in docs if d.get("kind") == "Gateway"), {})
    allowed = gateway.get("spec", {}).get("listeners", [{}])[0].get("allowedRoutes", {}).get("namespaces", {}).get("selector", {}).get("matchLabels", {})
    findings.append(Finding(allowed == {"kubernetes.io/metadata.name": "maops-platform"}, "day7.platform.gateway_allowed_routes", f"expected allowedRoutes only from maops-platform, found {allowed}"))
    for name, validator in (("gateway-values-configmap.yaml", validate_gateway_values_configmap), ("cilium-ambient-probe-policy.yaml", validate_cilium_probe_policy)):
        findings += [Finding(f.ok, f"day7.platform[{name}].{f.name}", f.detail) for f in validator.run_checks((DAY7_PLATFORM_DIR / name).read_text())]
    raw_data = (DAY7_PLATFORM_DIR / "gateway-values-configmap.yaml").read_text().split("\ndata:", 1)[-1]
    findings.append(Finding("day6" not in raw_data.lower() and "day4" not in raw_data.lower(), "day7.platform[gateway-values-configmap.yaml].data_no_earlier_day_identity", "expected no day4/day6 identity in the ConfigMap's block-scalar data"))
    day6_docs = _platform_docs(DAY6_PLATFORM_DIR)
    day6_instances = {(d.get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/instance") for d in day6_docs}
    findings.append(Finding(day6_instances == {"maops-kubernetes-platform-day6"}, "day7.platform.day6_sources_keep_day6_identity", f"expected k8s/day6 to stay Day 6-labelled, found {day6_instances}"))
    return findings


WORKLOAD_COMPONENT = {"maops-gateway": "gateway", "maops-app": "app", "maops-state": "state", "maops-gateway-candidate": "candidate"}


def _write_overlay(directory: str, build: day7_build.Build) -> str:
    path = Path(directory) / f"build-{build.build_id[:12]}.yaml"
    path.write_text(day7_build.overlay_yaml(build))
    return str(path)


def _image_tags(build: day7_build.Build) -> tuple[tuple[str, str], ...]:
    return tuple((i.repository, i.tag) for i in build.images)


def _without_images(docs: list[dict]) -> list:
    """Docs with every container `image` field blanked (for comparing two
    builds' renders)."""
    def strip(node):
        if isinstance(node, dict):
            return {k: ("<image>" if k == "image" else strip(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return [strip(d) for d in docs]


def _pod_template_images(docs: list[dict]) -> dict[str, list[str]]:
    out = {}
    for d in docs:
        if d.get("kind") in ("Deployment", "StatefulSet"):
            containers = d.get("spec", {}).get("template", {}).get("spec", {}).get("containers") or []
            out[d["metadata"]["name"]] = [c.get("image") for c in containers]
    return out


def check_day7_build_pinning(chart_dir: str, overlay_dir: str) -> list[Finding]:
    """DAY7 image contract, static half: a Day 7 stage never renders
    without a verified build, and two different builds change every
    workload Pod template's image - and nothing else."""
    findings: list[Finding] = []
    for stage in sorted(DAY7_STAGE_EXPECTATIONS):
        try:
            render(chart_dir, DAY7_RELEASE_NAME, (str(DAY7_STAGE_DIR / stage),))
            findings.append(Finding(False, f"day7[{stage}].refuses_unpinned_build", "rendered WITHOUT a build overlay - the mutable tag would be deployable"))
        except subprocess.CalledProcessError as exc:
            findings.append(Finding("build.requirePinnedTags=true" in exc.stderr, f"day7[{stage}].refuses_unpinned_build", f"helm template without a build overlay failed with: {exc.stderr.strip()[:200]}"))
    a = day7_build.sample_build()
    b = day7_build.build_from_digests({c: "sha256:" + hashlib.sha256(f"maops-static-sample-b-{c}".encode()).hexdigest() for c in day7_build.COMPONENTS})
    for stage in ("stable.yaml", "green-prepared.yaml"):
        docs_a = k8s_yaml.load_all(render(chart_dir, DAY7_RELEASE_NAME, (str(DAY7_STAGE_DIR / stage), _write_overlay(overlay_dir, a))))
        docs_b = k8s_yaml.load_all(render(chart_dir, DAY7_RELEASE_NAME, (str(DAY7_STAGE_DIR / stage), _write_overlay(overlay_dir, b))))
        images_a, images_b = _pod_template_images(docs_a), _pod_template_images(docs_b)
        changed = sorted(n for n in images_a if images_a[n] != images_b.get(n))
        findings.append(Finding(changed == sorted(images_a), f"day7[{stage}].new_build_changes_every_pod_template", f"a different build changes the Pod template image of {changed} (workloads {sorted(images_a)})"))
        findings.append(Finding(_without_images(docs_a) == _without_images(docs_b), f"day7[{stage}].build_changes_only_images", "two builds' renders are identical apart from container image fields"))
        expected = {name: [a.image(WORKLOAD_COMPONENT[name]).ref] for name in images_a if name in WORKLOAD_COMPONENT}
        findings.append(Finding(images_a == expected, f"day7[{stage}].images_are_pinned_build_refs", f"Pod template images {images_a} (expected {expected})"))
    return findings


def check_day7_stages(chart_dir: str) -> list[Finding]:
    findings: list[Finding] = []
    on_disk = sorted(p.name for p in DAY7_STAGE_DIR.glob("*.yaml"))
    declared = sorted(DAY7_STAGE_EXPECTATIONS)
    findings.append(Finding(on_disk == declared, "day7.stage_files_match_declared_expectations", f"expected stage files {declared}, found {on_disk}"))
    messages: dict[str, str] = {}
    build = day7_build.sample_build()
    with tempfile.TemporaryDirectory() as overlay_dir:
        overlay = _write_overlay(overlay_dir, build)
        findings += check_day7_build_pinning(chart_dir, overlay_dir)
        for stage, expectation in DAY7_STAGE_EXPECTATIONS.items():
            path = DAY7_STAGE_DIR / stage
            try:
                text = render(chart_dir, DAY7_RELEASE_NAME, (str(path), overlay))
            except subprocess.CalledProcessError as exc:
                findings.append(Finding(False, f"day7[{stage}].render", f"helm template failed: {exc.stderr.strip()[:400]}"))
                continue
            docs = k8s_yaml.load_all(text)
            findings.append(Finding(bool(docs), f"day7[{stage}].render", f"rendered {len(docs)} objects (with synthetic build {build.build_id[:12]})"))
            findings += _prefixed(stage, run_checks(docs, dataclasses.replace(expectation, image_tags=_image_tags(build))))
            cm = next((d for d in docs if d.get("kind") == "ConfigMap" and d.get("metadata", {}).get("name") == "maops-gateway-candidate-config"), None)
            if cm is not None:
                messages[stage] = cm.get("data", {}).get("APP_MESSAGE")
    if "recreate-serving.yaml" in messages and "recreate-changed.yaml" in messages:
        findings.append(Finding(
            messages["recreate-serving.yaml"] != messages["recreate-changed.yaml"],
            "day7.recreate_changed_only_changes_the_candidate_message",
            f"expected recreate-changed to carry a different candidate message than recreate-serving ({messages['recreate-serving.yaml']!r} vs {messages['recreate-changed.yaml']!r})",
        ))
    return findings


def main() -> int:
    chart_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHART_DIR

    try:
        rendered = render(chart_dir)
    except subprocess.CalledProcessError as exc:
        print("FAIL: helm template render failed", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("FAIL: helm not found on PATH", file=sys.stderr)
        return 1

    docs = k8s_yaml.load_all(rendered)
    if not docs:
        print("FAIL: no documents parsed from rendered chart output", file=sys.stderr)
        return 1

    findings = run_checks(docs)
    findings.append(check_k8s_base_still_frozen())

    findings += check_day7_stages(chart_dir)
    findings += check_day7_platform_manifests()

    try:
        gateway_values_text = GATEWAY_VALUES_CONFIGMAP_PATH.read_text()
    except OSError as exc:
        print(f"FAIL: could not read {GATEWAY_VALUES_CONFIGMAP_PATH}: {exc}", file=sys.stderr)
        return 1
    findings += validate_gateway_values_configmap.run_checks(gateway_values_text)

    try:
        cilium_probe_policy_text = CILIUM_PROBE_POLICY_PATH.read_text()
    except OSError as exc:
        print(f"FAIL: could not read {CILIUM_PROBE_POLICY_PATH}: {exc}", file=sys.stderr)
        return 1
    findings += validate_cilium_probe_policy.run_checks(cilium_probe_policy_text)

    failures = [f for f in findings if not f.ok]

    print(f"# Helm chart static validation ({chart_dir}; default render as release {RELEASE_NAME!r}, Day 7 stages as {DAY7_RELEASE_NAME!r})")
    for finding in findings:
        print(finding.render())

    print()
    print(f"{len(findings) - len(failures)}/{len(findings)} checks passed")

    if failures:
        print(f"FAIL: {len(failures)} static Helm chart check(s) failed", file=sys.stderr)
        return 1

    print("PASS: all static Helm chart checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

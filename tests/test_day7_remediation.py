"""
DAY7 pre-live remediation - Docker/Kubernetes-free regression tests for:

  1. the image contract (build -> local digest -> load into Day 7 only ->
     per-node digest -> deploy), including host config-digest derivation
     from a `docker save` archive and fail-before-deploy behavior;
  2. an operational Day 7 final gate that never lists, contacts or
     requires older kind clusters (Day 6 profile behavior preserved);
  3. Day 7-only identities in everything the Day 7 sequence creates;
  4. truthful Recreate evidence (controller facts required, sampling
     reported as bounded observation);
  5. `make -n day7-check` taking no lock and writing nothing;
  6. the corrected live order (build/load before deploy, listeners
     before rollout, baselines before experiments, restoration checked
     after each experiment and by the final gate).

Every external command is mocked or is a pure local render/parse; the
only real subprocesses are `helm template`, `make -n`/echo-only `make`,
and Python itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from day7_build_fixture import SAMPLE_BUILD  # noqa: E402  (hermetic DAY7_BUILD_ROOT)
import day7_build
import day7_final_check
import day7_history_audit
import day7_image_check as img
import day7_recreate
import final_state_check
import helm_check
import k8s_yaml
import kube
import make_sequence
from validate_helm_chart import run_checks

MAKEFILE = (REPO / "Makefile").read_text()


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def _day7_seq() -> list[str]:
    return [name for _, name in make_sequence.steps("day7-check", MAKEFILE)]


def _py(code: str, **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k not in ("MAOPS_CLUSTER_PROFILE", "KUBECONFIG_PATH")}
    full.update(env)
    return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n{code}"], capture_output=True, text=True, env=full, timeout=60)


# --------------------------------------------------------------------------
# 1. Image contract
# --------------------------------------------------------------------------


def _saved_tar(path: str, ref: str, config: dict | None = None, tamper: bool = False, layout: str = "oci", images: int = 1) -> str:
    blob = json.dumps(config or {"os": "linux", "architecture": "amd64"}).encode()
    digest = hashlib.sha256(blob).hexdigest()
    config_name = f"blobs/sha256/{digest}" if layout == "oci" else f"{digest}.json"
    manifest = [{"Config": config_name, "RepoTags": [ref], "Layers": []}] * images
    with tarfile.open(path, "w") as tar:
        for name, data in (("manifest.json", json.dumps(manifest).encode()), (config_name, blob + (b" " if tamper else b""))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return f"sha256:{digest}"


class ImageDigestDerivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "image.tar")
        self.ref = "maops-kubernetes-gateway:0.7.0"

    def tearDown(self):
        self._tmp.cleanup()

    def test_config_digest_from_oci_and_legacy_layouts(self):
        for layout in ("oci", "legacy"):
            with self.subTest(layout=layout):
                expected = _saved_tar(self.path, self.ref, layout=layout)
                digest, config = img.config_from_saved_tar(self.path, self.ref)
                self.assertEqual(digest, expected)
                self.assertTrue(img.platform_ok(config)[0])

    def test_tampered_config_blob_rejected(self):
        _saved_tar(self.path, self.ref, tamper=True)
        with self.assertRaises(img.ImageContractError):
            img.config_from_saved_tar(self.path, self.ref)

    def test_wrong_tag_or_multiple_images_rejected(self):
        _saved_tar(self.path, "maops-kubernetes-gateway:0.6.0")
        with self.assertRaises(img.ImageContractError):
            img.config_from_saved_tar(self.path, self.ref)
        _saved_tar(self.path, self.ref, images=2)
        with self.assertRaises(img.ImageContractError):
            img.config_from_saved_tar(self.path, self.ref)

    def test_non_amd64_platform_rejected(self):
        _saved_tar(self.path, self.ref, config={"os": "linux", "architecture": "arm64"})
        _, config = img.config_from_saved_tar(self.path, self.ref)
        self.assertFalse(img.platform_ok(config)[0])


class NodeVerdictTests(unittest.TestCase):
    REF = "maops-kubernetes-app:0.7.0"

    def _out(self, digest="sha256:abc", tags=None):
        return json.dumps({"status": {"id": digest, "repoTags": tags if tags is not None else [img.node_ref(self.REF)]}})

    def test_match(self):
        self.assertTrue(img.node_image_verdict(self._out(), self.REF, "sha256:abc")[0])

    def test_stale_digest_missing_tag_or_garbage_rejected(self):
        self.assertFalse(img.node_image_verdict(self._out(digest="sha256:old"), self.REF, "sha256:abc")[0])
        self.assertFalse(img.node_image_verdict(self._out(tags=["docker.io/library/maops-kubernetes-app:0.6.0"]), self.REF, "sha256:abc")[0])
        self.assertFalse(img.node_image_verdict("not json", self.REF, "sha256:abc")[0])

    def test_only_day7_nodes_accepted(self):
        nodes, rejected = img.parse_kind_nodes("maops-k8s-day7-control-plane\nmaops-k8s-day7-worker\nmaops-k8s-day7-worker2\nmaops-k8s-day6-worker\n")
        self.assertEqual(len(nodes), 3)
        self.assertEqual(rejected, ["maops-k8s-day6-worker"])


def _sample_overlay(test: unittest.TestCase) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(day7_build.overlay_yaml(SAMPLE_BUILD))
    f.close()
    test.addCleanup(os.unlink, f.name)
    return f.name


class ImageCheckMainTests(unittest.TestCase):
    """nodes mode with every external command mocked."""

    NODES = ["maops-k8s-day7-control-plane", "maops-k8s-day7-worker", "maops-k8s-day7-worker2"]

    DIGESTS = {f"{i.repository}:{img.VERSION}": i.config_digest for i in SAMPLE_BUILD.images}

    def _all(self):
        """Every node holds the mutable tags AND the current build's
        pinned tags, all resolving to the build's config digests."""
        per_node = dict(self.DIGESTS)
        per_node.update({i.ref: i.config_digest for i in SAMPLE_BUILD.images})
        return {n: dict(per_node) for n in self.NODES}

    def _run(self, node_images: dict, kind_nodes=None, build=SAMPLE_BUILD):
        digests = dict(self.DIGESTS)

        def fake_run(cmd, timeout):
            if cmd[:2] == ["kind", "get"]:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(kind_nodes or self.NODES), "")
            if cmd[:2] == ["docker", "exec"]:
                node, ref = cmd[2], cmd[-1].removeprefix("docker.io/library/")
                if ref not in node_images.get(node, {}):
                    return subprocess.CompletedProcess(cmd, 1, "", "image not found")
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": {"id": node_images[node][ref], "repoTags": [cmd[-1]]}}), "")
            raise AssertionError(f"unexpected command {cmd}")

        img.results.clear()
        with mock.patch.object(img, "local_digests", return_value=digests), mock.patch.object(img, "_run", side_effect=fake_run), mock.patch.object(sys, "argv", ["x", "nodes"]), \
                mock.patch.object(day7_build, "load_current", return_value=build):
            return _quiet(img.main), digests

    def test_all_nodes_hold_exact_images(self):
        rc, _ = self._run(self._all())
        self.assertEqual(rc, 0)

    def test_missing_image_on_one_node_fails_before_deploy(self):
        partial = self._all()
        del partial["maops-k8s-day7-worker2"][f"maops-kubernetes-state:{img.VERSION}"]
        rc, _ = self._run(partial)
        self.assertEqual(rc, 1)

    def test_missing_pinned_build_tag_on_one_node_fails_before_deploy(self):
        partial = self._all()
        del partial["maops-k8s-day7-worker"][SAMPLE_BUILD.image("app").ref]
        rc, _ = self._run(partial)
        self.assertEqual(rc, 1)

    def test_pinned_tag_resolving_to_other_bytes_fails(self):
        nodes = self._all()
        nodes["maops-k8s-day7-control-plane"][SAMPLE_BUILD.image("gateway").ref] = "sha256:" + "0" * 64
        rc, _ = self._run(nodes)
        self.assertEqual(rc, 1)

    def test_current_build_not_todays_local_build_fails(self):
        stale = day7_build.build_from_digests({c: "sha256:" + "1" * 64 for c in day7_build.COMPONENTS})
        nodes = self._all()
        for n in self.NODES:
            nodes[n].update({i.ref: i.config_digest for i in stale.images})
        rc, _ = self._run(nodes, build=stale)
        self.assertEqual(rc, 1)

    def test_day6_node_listing_fails(self):
        rc, _ = self._run({}, kind_nodes=["maops-k8s-day6-control-plane"])
        self.assertEqual(rc, 1)


class ImageContractWiringTests(unittest.TestCase):
    def test_refs_match_version_and_chart(self):
        values = k8s_yaml.load_all((REPO / "charts/maops-kubernetes-platform/values.yaml").read_text())[0]["images"]
        chart_refs = sorted(f"{v['repository']}:{v['tag']}" for v in values.values())
        self.assertEqual(sorted(img.image_refs()), chart_refs)
        self.assertEqual(img.VERSION, (REPO / "VERSION").read_text().strip())
        self.assertTrue(all(v["pullPolicy"] == "IfNotPresent" for v in values.values()))

    def test_candidate_runs_the_same_gateway_image_as_stable(self):
        docs = {(d["kind"], d["metadata"]["name"]): d for d in k8s_yaml.load_all(subprocess.run(
            ["helm", "template", "r", str(REPO / "charts/maops-kubernetes-platform"), "-f", str(REPO / "helm-values/day7/green-prepared.yaml"), "-f", _sample_overlay(self)],
            capture_output=True, text=True, timeout=60, check=True).stdout) if d}
        image = lambda name: docs[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]["image"]  # noqa: E731
        self.assertEqual(image("maops-gateway-candidate"), image("maops-gateway"))
        self.assertEqual(image("maops-gateway"), SAMPLE_BUILD.image("gateway").ref)

    def test_build_then_verify_then_load_then_verify_then_deploy(self):
        seq = _day7_seq()
        order = ["image-build", "day7-image-verify-local", "day7-build-record", "cluster-create", "image-load", "day7-image-load", "day7-image-verify-nodes", "storage-bootstrap", "day7-deploy"]
        self.assertEqual([seq.index(s) for s in order], sorted(seq.index(s) for s in order))
        self.assertEqual(seq[seq.index("image-load") + 1], "day7-image-load")
        self.assertEqual(seq[seq.index("day7-image-load") + 1], "day7-image-verify-nodes")
        self.assertEqual(seq[seq.index("image-build") + 1], "day7-image-verify-local")

    def test_build_and_load_run_under_the_day7_profile(self):
        steps = dict((name, mk) for mk, name in make_sequence.steps("day7-check", MAKEFILE))
        self.assertTrue(steps["image-build"].startswith("day7 profile"))
        self.assertTrue(steps["image-load"].startswith("day7 profile"))

    def test_resolved_load_targets_only_the_day7_cluster(self):
        out = subprocess.run(["make", "-n", "CLUSTER_NAME=maops-k8s-day7", "KUBECONFIG_PATH=/nonexistent", "image-build", "image-load"], cwd=REPO, capture_output=True, text=True, timeout=60, check=True).stdout
        for repo in img.REPOSITORIES:
            self.assertIn(f"kind load docker-image {repo}:{img.VERSION} --name maops-k8s-day7", out)
            self.assertIn(f"-t {repo}:{img.VERSION}", out)
        self.assertIn("--platform linux/amd64 --provenance=false --sbom=false --load", out)
        self.assertNotIn("maops-k8s-day6", out)


# --------------------------------------------------------------------------
# 2. Independent Day 7 final gate
# --------------------------------------------------------------------------

_FINAL_STATE_CHECKS = (
    "check_workload_final_state", "check_pdb_final_state", "check_state_final_state", "check_suite_state_baseline_restored",
    "check_secret_final_state", "check_state_secret_final_state", "check_no_leaked_port_forwards",
    "check_no_leaked_mesh_probe_namespace", "check_no_leaked_networkpolicy_probe_pods",
)


class FinalGateIndependenceTests(unittest.TestCase):
    def _run_final_state_main(self, profile: str):
        patches = [mock.patch.object(final_state_check, name) for name in _FINAL_STATE_CHECKS]
        other = mock.MagicMock()

        def no_kind(cmd, *a, **k):
            raise AssertionError(f"older clusters must not be listed/contacted: {cmd}")

        with mock.patch.object(kube, "PROFILE", profile), mock.patch.object(kube, "verify_context"), \
                mock.patch.object(final_state_check.scheduling_check, "check_node_topology", return_value=([], [])), \
                mock.patch.object(final_state_check.scheduling_check, "check_workload_scheduling"), \
                mock.patch.object(final_state_check, "check_other_day_clusters_still_exist", other), \
                mock.patch.object(final_state_check.subprocess, "run", side_effect=no_kind):
            for p in patches:
                p.start()
            try:
                final_state_check.results = []
                final_state_check.scheduling_check.results = []
                rc = _quiet(final_state_check.main)
            finally:
                for p in patches:
                    p.stop()
        return rc, other

    def test_day7_final_state_check_passes_without_listing_older_clusters(self):
        rc, other = self._run_final_state_main("day7")
        self.assertEqual(rc, 0)
        other.assert_not_called()

    def test_day6_profile_still_requires_older_clusters(self):
        rc, other = self._run_final_state_main("day6")
        other.assert_called_once()

    def test_day6_missing_older_cluster_still_fails_under_day6(self):
        completed = subprocess.CompletedProcess(["kind"], 0, "maops-k8s-day1\n", "")
        final_state_check.results = []
        with mock.patch.object(final_state_check.subprocess, "run", return_value=completed):
            _quiet(final_state_check.check_other_day_clusters_still_exist)
        self.assertTrue(any(not ok for ok, _ in final_state_check.results))

    def test_day7_final_check_leak_checks_never_touch_older_clusters(self):
        final_state_check.results = []
        ps = subprocess.CompletedProcess(["ps"], 0, "  PID ARGS\n", "")

        def only_ps(cmd, *a, **k):
            if cmd[0] != "ps":
                raise AssertionError(f"unexpected command {cmd}")
            return ps

        with mock.patch.object(final_state_check, "check_no_leaked_networkpolicy_probe_pods"), \
                mock.patch.object(final_state_check, "check_no_leaked_mesh_probe_namespace"), \
                mock.patch.object(final_state_check, "check_other_day_clusters_still_exist", side_effect=AssertionError("must not run")), \
                mock.patch.object(day7_final_check.d7, "list_json", return_value=[]), \
                mock.patch.object(day7_final_check.d7, "get_json_or_none", return_value=("not_found", None)), \
                mock.patch.object(day7_final_check.subprocess, "run", side_effect=only_ps):
            rec = day7_final_check.d7.Recorder()
            _quiet(day7_final_check.check_leaks, rec)
        self.assertEqual(rec.failures(), [])

    def test_day7_scripts_do_not_reference_older_cluster_checks(self):
        for script in ("day7_final_check.py", "day7_stable_check.py", "day7_strategy.py"):
            text = (SCRIPTS / script).read_text()
            with self.subTest(script=script):
                self.assertNotIn("check_other_day_clusters_still_exist(", text)
                self.assertNotIn('"kind", "get", "clusters"', text)

    def test_history_audit_is_optional_and_outside_every_gate(self):
        for target in ("day7-check", "day7-final-gate", "day7-resume-check"):
            self.assertNotIn("day7-history-audit", [n for _, n in make_sequence.steps(target, MAKEFILE)])
        self.assertIn("day7-history-audit:", MAKEFILE)

    def test_history_audit_pure_listing(self):
        self.assertEqual(day7_history_audit.older_clusters("maops-k8s-day1 maops-k8s-day6"), ["maops-k8s-day2", "maops-k8s-day3", "maops-k8s-day4", "maops-k8s-day5"])
        self.assertEqual(day7_history_audit.older_clusters(" ".join(day7_history_audit.OLDER_CLUSTERS)), [])

    def test_history_audit_uses_post_release_commit_for_records(self):
        self.assertEqual(day7_history_audit.POST_RELEASE_COMMIT, "74832c41a35a04d905aaa203e22f7d06bb7eb4e9")
        self.assertIn("docs/engineering-reviews", day7_history_audit.HISTORICAL_RECORD_PATHS)
        self.assertNotIn("docs/engineering-reviews", day7_history_audit.FROZEN_PATHS)


# --------------------------------------------------------------------------
# 3. Day 7 resource identity
# --------------------------------------------------------------------------

EARLIER_DAY = re.compile(r"day[1-6](?![0-9])")
SHARED_NAMES = {"maops-platform", "maops-ingress", "maops-edge", "maops-edge-istio", "maops-diagnostics"}


def _strings(value):
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *_strings(v)]]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


class Day7IdentityTests(unittest.TestCase):
    def _stage_docs(self):
        docs = []
        for stage in sorted((REPO / "helm-values/day7").glob("*.yaml")):
            out = subprocess.run(["helm", "template", "maops-kubernetes-platform-day7", str(REPO / "charts/maops-kubernetes-platform"), "--namespace", "maops-platform", "-f", str(stage), "-f", _sample_overlay(self)], capture_output=True, text=True, timeout=60, check=True).stdout
            docs += [d for d in k8s_yaml.load_all(out) if d]
        return docs

    def test_no_earlier_day_identity_in_any_day7_render(self):
        stale = sorted({s for d in self._stage_docs() for s in _strings(d) if EARLIER_DAY.search(s)})
        self.assertEqual(stale, [])

    def test_no_earlier_day_identity_in_day7_platform_manifests(self):
        docs = helm_check._platform_docs(REPO / "k8s/day7")
        stale = sorted({s for d in docs for s in _strings(d) if EARLIER_DAY.search(s)})
        self.assertEqual(stale, [])
        raw = (REPO / "k8s/day7/gateway-values-configmap.yaml").read_text().split("\ndata:", 1)[1]
        self.assertIsNone(EARLIER_DAY.search(raw))
        names = {d["metadata"]["name"] for d in docs}
        self.assertTrue({"maops-platform", "maops-ingress", "maops-edge", "maops-diagnostics"} <= names, "genuinely shared names stay allowed")

    def test_no_earlier_day_identity_in_day7_probe_manifests(self):
        code = (
            "import json, mesh_check, networkpolicy_check as n, rbac_check, storage_bootstrap as b, storage_hardening_check as h\n"
            "texts = [mesh_check._mesh_probe_namespace_manifest(), mesh_check._mesh_probe_serviceaccount_and_pod_manifest(),\n"
            "         n._validation_pod_manifest(), rbac_check._pod_manifest(), h._pod_manifest('p', 10001, 10001, 10001, 'pass')]\n"
            "print(json.dumps({'texts': texts, 'names': [b.BOOTSTRAP_VERIFY_NAMESPACE, h.NAMESPACE, n.VALIDATION_NAMESPACE]}))\n"
        )
        out = _py(code, MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        for text in data["texts"]:
            content = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            with self.subTest(manifest=content[:60]):
                self.assertIsNone(EARLIER_DAY.search(content), content)
        self.assertFalse([n for n in data["names"] if EARLIER_DAY.search(n)])

    def test_rolebinding_subject_uses_the_day7_validation_namespace(self):
        docs = [d for d in self._stage_docs() if d.get("kind") == "RoleBinding"]
        self.assertTrue(docs)
        self.assertEqual({s["namespace"] for d in docs for s in d["subjects"]}, {"maops-day7-validation"})

    def test_validator_rejects_a_day6_rolebinding_subject_in_a_day7_render(self):
        out = subprocess.run(["helm", "template", "maops-kubernetes-platform-day7", str(REPO / "charts/maops-kubernetes-platform"), "--namespace", "maops-platform", "-f", str(REPO / "helm-values/day7/stable.yaml"), "-f", _sample_overlay(self), "--set", "validation.namespace=maops-day6-validation"], capture_output=True, text=True, timeout=60, check=True).stdout
        failed = {f.name for f in run_checks([d for d in k8s_yaml.load_all(out) if d], helm_check.DAY7_STAGE_EXPECTATIONS["stable.yaml"]) if not f.ok}
        self.assertIn("rbac.rolebinding.subject_namespace", failed)

    def test_platform_check_rejects_a_day6_label_in_k8s_day7(self):
        with tempfile.TemporaryDirectory() as d:
            for f in (REPO / "k8s/day7").glob("*.yaml"):
                text = f.read_text()
                if f.name == "gateway.yaml":
                    text = text.replace("maops-kubernetes-platform-day7", "maops-kubernetes-platform-day6")
                (Path(d) / f.name).write_text(text)
            with mock.patch.object(helm_check, "DAY7_PLATFORM_DIR", Path(d)):
                failed = {f.name for f in helm_check.check_day7_platform_manifests() if not f.ok}
        self.assertIn("day7.platform[Gateway/maops-edge].instance", failed)
        self.assertIn("day7.platform[Gateway/maops-edge].no_earlier_day_identity", failed)

    def test_real_day7_platform_manifests_pass(self):
        self.assertEqual([f.name for f in helm_check.check_day7_platform_manifests() if not f.ok], [])

    def test_makefile_applies_the_profiles_own_platform_manifests(self):
        day7 = subprocess.run(["make", "-n", "CLUSTER_NAME=maops-k8s-day7", "KUBECONFIG_PATH=/nonexistent", "namespace-apply", "gateway-apply"], cwd=REPO, capture_output=True, text=True, timeout=60, check=True).stdout
        day6 = subprocess.run(["make", "-n", "namespace-apply", "gateway-apply"], cwd=REPO, capture_output=True, text=True, timeout=60, check=True).stdout
        self.assertIn("-f k8s/day7/validation-namespace.yaml", day7)
        self.assertIn("-f k8s/day7/gateway.yaml", day7)
        self.assertNotIn("k8s/day6", day7)
        self.assertIn("-f k8s/day6/validation-namespace.yaml", day6)
        self.assertNotIn("k8s/day7", day6)
        self.assertIn("apply -f $(PLATFORM_K8S)/cilium-ambient-probe-policy.yaml", MAKEFILE)

    def test_final_check_flags_earlier_day_namespaces(self):
        self.assertEqual(day7_final_check.stale_namespaces(["maops-platform", "maops-day7-validation", "maops-day6-validation", "maops-day4-storage-hardening", "istio-system"]), ["maops-day4-storage-hardening", "maops-day6-validation"])


# --------------------------------------------------------------------------
# 4. Recreate evidence wording and required controller facts
# --------------------------------------------------------------------------


def _dep(generation, revision, checksum, strategy=None, uid="dep-1"):
    return {
        "metadata": {"uid": uid, "generation": generation, "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
        "spec": {"strategy": strategy or {"type": "Recreate"}, "template": {"metadata": {"annotations": {"checksum/config": checksum}}}},
        "status": {"observedGeneration": generation},
    }


def _rs(uid, revision, h, replicas):
    return {"metadata": {"uid": uid, "annotations": {"deployment.kubernetes.io/revision": str(revision)}, "labels": {"pod-template-hash": h}}, "spec": {"replicas": replicas}}


def _pod(uid, h):
    return {"metadata": {"uid": uid, "labels": {"pod-template-hash": h}}}


def _evidence(**over):
    args = dict(
        dep_before=_dep(3, 2, "aaa"),
        dep_after=_dep(4, 3, "bbb"),
        rs_before=[_rs("rs-old", 2, "h1", 2)],
        rs_after=[_rs("rs-old", 2, "h1", 0), _rs("rs-new", 3, "h2", 2)],
        old_pods=[_pod("p1", "h1"), _pod("p2", "h1")],
        pods_after=[_pod("p3", "h2"), _pod("p4", "h2")],
    )
    args.update(over)
    return [msg for ok, msg in day7_recreate.revision_evidence(**args) if not ok]


class RecreateEvidenceTests(unittest.TestCase):
    def test_genuine_recreate_revision_passes(self):
        self.assertEqual(_evidence(), [])

    def test_rollingupdate_block_or_strategy_change_fails(self):
        self.assertTrue(_evidence(dep_after=_dep(4, 3, "bbb", strategy={"type": "Recreate", "rollingUpdate": {"maxSurge": 1}})))
        self.assertTrue(_evidence(dep_before=_dep(3, 2, "aaa", strategy={"type": "RollingUpdate"})))

    def test_no_real_template_revision_fails(self):
        self.assertTrue(_evidence(dep_after=_dep(4, 2, "bbb")))
        self.assertTrue(_evidence(dep_after=_dep(4, 3, "aaa")))
        self.assertTrue(_evidence(rs_after=[_rs("rs-old", 2, "h1", 2)]))
        self.assertTrue(_evidence(rs_after=[_rs("rs-old", 2, "h1", 0), _rs("rs-new", 3, "h1", 2)]))

    def test_old_pod_left_or_mixed_hash_fails(self):
        self.assertTrue(_evidence(pods_after=[_pod("p1", "h1"), _pod("p3", "h2")]))
        self.assertTrue(_evidence(dep_after=_dep(4, 3, "bbb", uid="dep-2")))

    def test_ordering_wording_never_claims_proof_from_sampling(self):
        ok, msg = day7_recreate.ordering_finding({"violations": [], "observations": 40}, 0.5)
        self.assertTrue(ok)
        self.assertIn("observed in 40 bounded samples", msg)
        self.assertIn("consistent with", msg)
        self.assertIn("cannot exclude", msg)
        self.assertNotRegex(msg.lower(), r"\bprov(e|es|en)\b")

    def test_observed_overlap_or_no_observation_fails(self):
        self.assertFalse(day7_recreate.ordering_finding({"violations": ["t=1: overlap"], "observations": 3}, 0.5)[0])
        self.assertFalse(day7_recreate.ordering_finding({"violations": [], "observations": 0}, 0.5)[0])

    def test_missed_interruption_is_still_reported_honestly(self):
        summary = day7_recreate.analyze_interruption([(0.0, "old")] + [(1.0 + i, "new") for i in range(10)])
        self.assertEqual(summary["errors"], 0)
        self.assertIn("NO interruption was sampled", (SCRIPTS / "day7_recreate.py").read_text())


# --------------------------------------------------------------------------
# 5. make -n side effect
# --------------------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_make_n_day7_check_takes_no_lock_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            lock7, lock6 = os.path.join(d, "l7"), os.path.join(d, "l6")
            env = {**os.environ, "DAY7_LOCK_PATH": lock7, "DAY6_LOCK_PATH": lock6}
            result = subprocess.run(["make", "-n", "day7-check"], cwd=REPO, capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(os.path.exists(lock7))
            self.assertFalse(os.path.exists(lock6))
            self.assertEqual(os.listdir(d), [])
        self.assertNotIn("day7_lock.py", result.stdout)
        self.assertIn("kind create cluster --name maops-k8s-day7", result.stdout, "nested makes still print their commands")

    def test_real_invocations_keep_the_real_lock(self):
        def resolve(var, *assign):
            return subprocess.run(["make", "--no-print-directory", f"--eval=__r__: ; @echo '$({var})'", "__r__", *assign], cwd=REPO, capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        self.assertEqual(resolve("DAY7_LOCK"), "python3 scripts/day7_lock.py run --")
        self.assertEqual(resolve("DAY6_LOCK"), "python3 scripts/day6_lock.py run --")
        self.assertEqual(resolve("DAY7_LOCK", "-s"), "python3 scripts/day7_lock.py run --", "silent mode is not a dry run")

    def test_read_only_sequence_printer(self):
        out = subprocess.run([sys.executable, str(SCRIPTS / "make_sequence.py"), "day7-check"], cwd=REPO, capture_output=True, text=True, timeout=30, check=True).stdout
        self.assertIn("day7-image-verify-nodes", out)
        self.assertIn("day7-plan:", MAKEFILE)


# --------------------------------------------------------------------------
# 6. Live resource budget and gate order
# --------------------------------------------------------------------------


class GateOrderAndBudgetTests(unittest.TestCase):
    def test_restoration_checked_after_each_experiment_and_by_the_final_gate(self):
        seq = _day7_seq()
        for exp in ("day7-blue-green", "day7-canary", "day7-recreate"):
            self.assertEqual(seq[seq.index(exp) + 1], "day7-stable-check", exp)
        self.assertEqual(seq[-1], "day7-final-gate")
        self.assertEqual([n for _, n in make_sequence.steps("day7-final-gate", MAKEFILE)][-1], "day7-final-state-check")

    def test_listeners_before_rollout_and_fresh_baselines_before_experiments(self):
        seq = _day7_seq()
        self.assertLess(seq.index("ambient-workload-check"), seq.index("rollout-check"))
        self.assertLess(seq.index("day7-deploy"), seq.index("ambient-workload-check"))
        first_experiment = min(seq.index(e) for e in ("day7-blue-green", "day7-canary", "day7-recreate"))
        for step in ("day7-baseline-init", "state-check", "day7-baseline"):
            self.assertLess(seq.index(step), first_experiment)

    def test_preflight_precedes_the_second_cluster_and_is_read_only(self):
        seq = _day7_seq()
        self.assertLess(seq.index("day7-preflight"), seq.index("cluster-create"))
        text = (SCRIPTS / "day7_preflight.py").read_text()
        for forbidden in ('"delete"', '"stop"', "kind delete", "docker stop", "docker rm", "sysctl -w"):
            self.assertNotIn(forbidden, text)
        self.assertRegex(text, r"timeout: float = \d+")

    def test_candidate_replica_count_is_intentional_and_documented(self):
        values = k8s_yaml.load_all((REPO / "charts/maops-kubernetes-platform/values.yaml").read_text())[0]
        self.assertEqual(values["candidate"]["replicas"], 2)
        for stage in (REPO / "helm-values/day7").glob("*.yaml"):
            self.assertEqual(k8s_yaml.load_all(stage.read_text())[0]["candidate"]["replicas"], 2, stage.name)
        self.assertIn("Candidate replica count and resource budget", (REPO / "docs/architecture.md").read_text())

    def test_stable_check_script_is_read_only(self):
        text = (SCRIPTS / "day7_stable_check.py").read_text()
        self.assertIn("verify_stable(", text)
        for forbidden in ("apply_stage", "restore_stable", "promote(", "helm_stage_command"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()

"""
DAY7 image contract - Docker/Kubernetes-free tests for
scripts/day7_build.py, scripts/day7_running_images.py, the chart's
pinned-build guard and their integration into day7_strategy /
day7_baseline.

The defect under test (run ce55f5fb..., 2026-09-28): the images were
rebuilt under the SAME mutable 0.7.0 tags, every node was verified to
hold the new build, `day7-deploy` was a no-op Helm upgrade, and the
stable Pods kept running the previous build while every gate passed.

Required negatives, each proven here:
  - old running image under the current tag            -> OldImageUnderCurrentTagTests
  - missing image ID                                   -> RunningImageNegativeTests
  - wrong node image                                   -> RunningImageNegativeTests
  - an unready Pod                                     -> RunningImageNegativeTests
  - candidate mismatch                                 -> CandidateImageTests
  - a no-op Helm deployment that would otherwise pass  -> NoOpDeploymentTests

kubectl/docker are never run: every live read is faked, and the Day 7
profile guard is proven to refuse before any process starts.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from day7_build_fixture import SAMPLE_BUILD, pinned_build  # noqa: E402  (sets a hermetic DAY7_BUILD_ROOT)
import day7_baseline  # noqa: E402
import day7_build  # noqa: E402
import day7_running_images as ri  # noqa: E402
import day7_strategy as d7  # noqa: E402
import helm_check  # noqa: E402
import kube  # noqa: E402

CHART = str(REPO / "charts" / "maops-kubernetes-platform")
STAGES = REPO / "helm-values" / "day7"
OLD_BUILD = day7_build.build_from_digests({c: "sha256:" + format(i + 1, "x") * 64 for i, c in enumerate(day7_build.COMPONENTS)})
NEW_BUILD = SAMPLE_BUILD


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def _import_digest(build: day7_build.Build, component: str) -> str:
    """A kind-import repo digest - deliberately a THIRD digest, neither
    the config digest nor Docker's manifest id."""
    return "docker.io/library/import-2026-09-28@sha256:" + ("e" if build is NEW_BUILD else "d") + build.image(component).config_digest[8:71]


def _node_records(*builds: day7_build.Build, mutable_tag_on: day7_build.Build | None = None) -> list[ri.NodeImage]:
    """What `crictl images` reports on a node holding these builds. The
    mutable `:0.7.0` tag sits on `mutable_tag_on` (the newest load)."""
    records = []
    for build in builds:
        for image in build.images:
            tags = [image.node_ref] + ([f"docker.io/library/{image.repository}:{day7_build.VERSION}"] if build is mutable_tag_on else [])
            records.append(ri.NodeImage(image.config_digest, tuple(tags), (_import_digest(build, image.component),)))
    return records


def _pod(name, component="gateway", build=NEW_BUILD, node="maops-k8s-day7-worker", ready=True, image_ref=None, image_id=None, terminating=False, container_ready=True, container_id="containerd://abc", status_image=None):
    """`image_ref` is the Pod SPEC's container image (default: the build's
    pinned ref, as Helm renders it). `status_image` is what the runtime
    reports - by default the record's MUTABLE tag, exactly as measured
    live in run c252aa3d... (containerd reports the first repoTag)."""
    image = build.image(component)
    status = {
        "name": ri.CONTAINER[component],
        "ready": container_ready,
        "containerID": container_id,
        "restartCount": 0,
        "image": status_image if status_image is not None else f"docker.io/library/{image.repository}:{day7_build.VERSION}",
        "imageID": image_id if image_id is not None else _import_digest(build, component),
    }
    meta = {"name": name, "uid": f"uid-{name}"}
    if terminating:
        meta["deletionTimestamp"] = "2026-09-29T00:00:00Z"
    return {
        "metadata": meta,
        "spec": {"nodeName": node, "containers": [{"name": ri.CONTAINER[component], "image": image_ref if image_ref is not None else image.ref}]},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True" if ready else "False"}], "containerStatuses": [status]},
    }


def _failures(component, pods, count, build=NEW_BUILD, node_images=None):
    node_images = node_images if node_images is not None else {"maops-k8s-day7-worker": _node_records(OLD_BUILD, NEW_BUILD, mutable_tag_on=NEW_BUILD), "maops-k8s-day7-worker2": _node_records(OLD_BUILD, NEW_BUILD, mutable_tag_on=NEW_BUILD)}
    return [m for ok, m in ri.evaluate_component(component, pods, count, build.image(component), node_images) if not ok]


# --------------------------------------------------------------------------
# day7_build: content-derived identity
# --------------------------------------------------------------------------


class BuildIdentityTests(unittest.TestCase):
    def test_pinned_tag_names_the_config_digest(self):
        digest = "sha256:" + "ab" * 32
        tag = day7_build.pinned_tag(digest)
        self.assertEqual(tag, f"{day7_build.VERSION}-cfg-{'ab' * 32}")
        self.assertEqual(day7_build.digest_from_tag(tag), digest)

    def test_mutable_or_malformed_tags_are_not_builds(self):
        for tag in (day7_build.VERSION, f"{day7_build.VERSION}-cfg-{'ab' * 31}", f"{day7_build.VERSION}-cfg-{'AB' * 32}", f"0.6.0-cfg-{'ab' * 32}", "latest"):
            with self.subTest(tag=tag), self.assertRaises(day7_build.BuildError):
                day7_build.digest_from_tag(tag)

    def test_build_id_is_deterministic_and_changes_with_any_image(self):
        digests = {c: i.config_digest for c, i in zip(day7_build.COMPONENTS, SAMPLE_BUILD.images)}
        self.assertEqual(day7_build.build_from_digests(digests), SAMPLE_BUILD)
        for component in day7_build.COMPONENTS:
            changed = dict(digests, **{component: "sha256:" + "0" * 64})
            with self.subTest(component=component):
                self.assertNotEqual(day7_build.build_from_digests(changed).build_id, SAMPLE_BUILD.build_id)

    def test_candidate_runs_the_gateway_image(self):
        self.assertEqual(SAMPLE_BUILD.image("candidate"), SAMPLE_BUILD.image("gateway"))

    def test_tampered_records_are_refused(self):
        good = SAMPLE_BUILD.record()
        tampered = []
        r = json.loads(json.dumps(good)); r["images"]["app"]["tag"] = day7_build.VERSION; tampered.append(("mutable tag", r))
        r = json.loads(json.dumps(good)); r["build_id"] = "0" * 64; tampered.append(("wrong id", r))
        r = json.loads(json.dumps(good)); r["version"] = "0.6.0"; tampered.append(("other version", r))
        r = json.loads(json.dumps(good)); r["images"]["state"]["repository"] = "evil/state"; tampered.append(("other repository", r))
        r = json.loads(json.dumps(good)); r["extra"] = 1; tampered.append(("extra key", r))
        for name, record in tampered:
            with self.subTest(name), self.assertRaises(day7_build.BuildError):
                day7_build.build_from_record(record)

    def test_overlay_pins_image_tags_and_nothing_else(self):
        day7_build.validate_overlay(SAMPLE_BUILD.overlay())
        for bad in (
            {"images": {"gateway": {"tag": day7_build.VERSION}, "app": SAMPLE_BUILD.overlay()["images"]["app"], "state": SAMPLE_BUILD.overlay()["images"]["state"]}},
            {**SAMPLE_BUILD.overlay(), "candidate": {"enabled": True}},
            {"images": {k: v for k, v in SAMPLE_BUILD.overlay()["images"].items() if k != "state"}},
            {"images": {**SAMPLE_BUILD.overlay()["images"], "app": {"tag": SAMPLE_BUILD.image("app").tag, "repository": "evil/app"}}},
        ):
            with self.subTest(bad=str(bad)[:80]), self.assertRaises(day7_build.BuildError):
                day7_build.validate_overlay(bad)

    def test_overlay_yaml_round_trips_through_the_repository_loader(self):
        import k8s_yaml

        self.assertEqual(k8s_yaml.load_all(day7_build.overlay_yaml(SAMPLE_BUILD))[0], SAMPLE_BUILD.overlay())

    def test_expected_release_values_change_only_image_tags(self):
        values = d7.expected_release_values("canary-90-10", SAMPLE_BUILD)
        stage = d7.load_stage_values("canary-90-10")
        self.assertEqual({k: v for k, v in values.items() if k != "images"}, stage)
        self.assertEqual(values["images"], SAMPLE_BUILD.overlay()["images"])


class BuildStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "state" / "day7-builds"
        self.loc = {"home": self.home, "forbidden_roots": ()}

    def test_store_current_and_load_are_private_and_validated(self):
        directory = day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        self.assertEqual(day7_build.load_current(self.root, **self.loc), SAMPLE_BUILD)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        for name in ("build.json", "values.yaml"):
            self.assertEqual(stat.S_IMODE(os.stat(directory / name).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "current.json").st_mode), 0o600)

    def test_identical_rerecord_is_idempotent_but_different_content_is_refused(self):
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        (self.root / SAMPLE_BUILD.build_id / "values.yaml").write_text("images: {}\n")
        with self.assertRaises(day7_build.BuildError):
            day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)

    def test_tampered_overlay_or_pointer_fails_load(self):
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        values = self.root / SAMPLE_BUILD.build_id / "values.yaml"
        original = values.read_text()
        values.write_text(original.replace(SAMPLE_BUILD.image("app").tag, day7_build.VERSION))
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)
        values.write_text(original)
        (self.root / "current.json").write_text(json.dumps({"build_id": SAMPLE_BUILD.build_id, "note": "x"}))
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)

    def test_world_readable_record_is_refused(self):
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        os.chmod(self.root / SAMPLE_BUILD.build_id / "build.json", 0o644)
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)

    def test_symlinked_pointer_or_record_is_refused(self):
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        elsewhere = self.home / "elsewhere.json"
        elsewhere.write_text((self.root / "current.json").read_text())
        os.chmod(elsewhere, 0o600)
        (self.root / "current.json").unlink()
        os.symlink(elsewhere, self.root / "current.json")
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)
        (self.root / "current.json").unlink()
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        record = self.root / SAMPLE_BUILD.build_id / "build.json"
        copy = self.home / "build-copy.json"
        copy.write_text(record.read_text())
        os.chmod(copy, 0o600)
        record.unlink()
        os.symlink(copy, record)
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)

    def test_interrupted_store_leaves_no_build_directory(self):
        real = day7_build._write_exclusive
        calls = []

        def fail_second(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            real(path, text)

        with mock.patch.object(day7_build, "_write_exclusive", side_effect=fail_second), self.assertRaises(OSError):
            day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [], "no partial or staging directory may remain")
        day7_build.store_build(SAMPLE_BUILD, self.root, **self.loc)  # a retry is not wedged
        day7_build.set_current(SAMPLE_BUILD, self.root, **self.loc)
        self.assertEqual(day7_build.load_current(self.root, **self.loc), SAMPLE_BUILD)

    def test_missing_store_fails_closed(self):
        with self.assertRaises(day7_build.BuildError):
            day7_build.load_current(self.root, **self.loc)

    def test_store_inside_the_repository_or_tmp_is_refused(self):
        with self.assertRaises(day7_build.BuildError):
            day7_build.store_build(SAMPLE_BUILD, REPO / "day7-builds")


class RecordLocalBuildTests(unittest.TestCase):
    def _digests(self, changed_after_tag=None):
        base = {f"{i.repository}:{day7_build.VERSION}": i.config_digest for i in SAMPLE_BUILD.images}
        pinned = {i.ref: i.config_digest for i in SAMPLE_BUILD.images}
        if changed_after_tag:
            pinned[SAMPLE_BUILD.image(changed_after_tag).ref] = "sha256:" + "f" * 64
        return lambda ref: {**base, **pinned}[ref]

    def test_tags_each_image_with_its_digest_and_re_derives_it(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        build = day7_build.record_local_build(digest_of=self._digests(), run=run)
        self.assertEqual(build, SAMPLE_BUILD)
        tagged = [c.args[0] for c in run.call_args_list]
        self.assertEqual(tagged, [["docker", "tag", f"{i.repository}:{day7_build.VERSION}", i.ref] for i in SAMPLE_BUILD.images])

    def test_image_changed_while_recording_is_refused(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with self.assertRaises(day7_build.BuildError):
            day7_build.record_local_build(digest_of=self._digests(changed_after_tag="app"), run=run)

    def test_docker_tag_failure_is_refused(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", "no such image"))
        with self.assertRaises(day7_build.BuildError):
            day7_build.record_local_build(digest_of=self._digests(), run=run)

    def test_tag_moved_after_record_is_not_loaded(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch.object(kube, "PROFILE", "day7"), self.assertRaises(day7_build.BuildError):
            _quiet(day7_build.load_into_kind, SAMPLE_BUILD, run=run, digest_of=self._digests(changed_after_tag="gateway"))
        run.assert_not_called()

    def test_kind_load_refuses_outside_day7(self):
        run = mock.Mock()
        with mock.patch.object(kube, "PROFILE", "day6"), self.assertRaises(day7_build.BuildError):
            day7_build.load_into_kind(SAMPLE_BUILD, run=run, digest_of=self._digests())
        run.assert_not_called()

    def test_kind_load_targets_only_the_day7_cluster(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch.object(kube, "PROFILE", "day7"):
            _quiet(day7_build.load_into_kind, SAMPLE_BUILD, run=run, digest_of=self._digests())
        self.assertEqual([c.args[0] for c in run.call_args_list], [["kind", "load", "docker-image", i.ref, "--name", "maops-k8s-day7"] for i in SAMPLE_BUILD.images])


# --------------------------------------------------------------------------
# Digest mapping
# --------------------------------------------------------------------------


class DigestMappingTests(unittest.TestCase):
    def test_kubernetes_import_digest_maps_to_the_config_digest(self):
        records = _node_records(OLD_BUILD, NEW_BUILD, mutable_tag_on=NEW_BUILD)
        record, detail = ri.resolve_image_id(_import_digest(NEW_BUILD, "app"), records)
        self.assertEqual(record.id, NEW_BUILD.image("app").config_digest)
        self.assertIn("-> node image record", detail)

    def test_registry_prefix_normalized_and_config_id_accepted(self):
        records = _node_records(NEW_BUILD)
        short = _import_digest(NEW_BUILD, "state").replace("docker.io/library/", "")
        self.assertIsNotNone(ri.resolve_image_id(short, records)[0])
        self.assertIsNotNone(ri.resolve_image_id(NEW_BUILD.image("state").config_digest, records)[0])

    def test_unknown_ambiguous_or_malformed_ids_do_not_map(self):
        records = _node_records(NEW_BUILD)
        dup = records + [ri.NodeImage("sha256:" + "9" * 64, (), (_import_digest(NEW_BUILD, "app"),))]
        for image_id, recs in (("docker.io/library/import@sha256:" + "0" * 64, records), (_import_digest(NEW_BUILD, "app"), dup), ("", records), ("sha256:short", records)):
            with self.subTest(image_id=image_id):
                self.assertIsNone(ri.resolve_image_id(image_id, recs)[0])

    def test_parse_node_images(self):
        out = json.dumps({"images": [{"id": "sha256:" + "a" * 64, "repoTags": ["t"], "repoDigests": ["d"]}]})
        self.assertEqual(ri.parse_node_images(out), [ri.NodeImage("sha256:" + "a" * 64, ("t",), ("d",))])
        with self.assertRaises(ValueError):
            ri.parse_node_images(json.dumps({"nope": []}))


# --------------------------------------------------------------------------
# Running-image gate
# --------------------------------------------------------------------------


class RunningImagePositiveTests(unittest.TestCase):
    def test_three_gateway_pods_on_the_build_pass(self):
        pods = [_pod(f"gw-{i}", node=f"maops-k8s-day7-worker{'' if i % 2 else '2'}") for i in range(3)]
        self.assertEqual(_failures("gateway", pods, 3), [])

    def test_each_pass_line_shows_the_mapping_chain(self):
        checks = ri.evaluate_component("app", [_pod("app-0", "app")], 1, NEW_BUILD.image("app"), {"maops-k8s-day7-worker": _node_records(NEW_BUILD)})
        self.assertTrue(any("imageID docker.io/library/import-2026-09-28@sha256:" in m and "== build config digest" in m for ok, m in checks if ok))


class OldImageUnderCurrentTagTests(unittest.TestCase):
    """The observed defect: the pod runs the PREVIOUS build while the
    node's current tags point at the new one."""

    def test_pod_still_on_the_mutable_tag_running_the_previous_build(self):
        pod = _pod("gw-0", build=OLD_BUILD, image_ref=f"docker.io/library/maops-kubernetes-gateway:{day7_build.VERSION}")
        failures = _failures("gateway", [pod], 1)
        self.assertTrue(any("Pod spec image reference" in m for m in failures), failures)
        self.assertTrue(any("NOT the build config digest" in m for m in failures), failures)

    def test_pinned_reference_but_previous_bytes_is_caught_by_the_digest(self):
        pod = _pod("gw-0", build=OLD_BUILD, image_ref=NEW_BUILD.image("gateway").node_ref)
        failures = _failures("gateway", [pod], 1)
        self.assertFalse([m for m in failures if "Pod spec image reference" in m], "the spec reference is correct here - only the digest may fail")
        self.assertTrue(any("NOT the build config digest" in m for m in failures), failures)


class RuntimeReportedTagTests(unittest.TestCase):
    """Live finding (run c252aa3d..., 2026-09-29): after the pinned rollout
    every container's `containerStatuses[].image` read `...:0.7.0`
    because both tags name one containerd record. That field must never
    decide the verdict."""

    def test_pinned_spec_and_digest_pass_although_the_runtime_reports_the_mutable_tag(self):
        pods = [_pod(f"gw-{i}", status_image=f"docker.io/library/maops-kubernetes-gateway:{day7_build.VERSION}") for i in range(3)]
        self.assertEqual(_failures("gateway", pods, 3), [])

    def test_a_pinned_looking_runtime_tag_cannot_rescue_a_mutable_spec(self):
        pod = _pod("gw-0", image_ref=f"maops-kubernetes-gateway:{day7_build.VERSION}", status_image=NEW_BUILD.image("gateway").node_ref)
        self.assertTrue(any("Pod spec image reference" in m for m in _failures("gateway", [pod], 1)))

    def test_missing_spec_container_fails(self):
        pod = _pod("gw-0")
        pod["spec"]["containers"] = []
        self.assertTrue(any("Pod spec image reference None" in m for m in _failures("gateway", [pod], 1)))


class RunningImageNegativeTests(unittest.TestCase):
    def test_missing_image_id(self):
        for image_id in ("", "maops-kubernetes-gateway:0.7.0"):
            with self.subTest(image_id=image_id):
                failures = _failures("gateway", [_pod("gw-0", image_id=image_id)], 1)
                self.assertTrue(any("missing or not a sha256" in m for m in failures), failures)

    def test_wrong_node_image(self):
        other = ri.NodeImage("sha256:" + "7" * 64, (NEW_BUILD.image("gateway").node_ref,), (_import_digest(NEW_BUILD, "gateway"),))
        cases = {
            "record carries another config digest": {"maops-k8s-day7-worker": [other]},
            "node records unreadable": {"maops-k8s-day7-worker": None},
            "image only present on another node": {"maops-k8s-day7-worker": _node_records(OLD_BUILD), "maops-k8s-day7-worker2": _node_records(NEW_BUILD)},
        }
        for name, node_images in cases.items():
            with self.subTest(name):
                self.assertTrue(_failures("gateway", [_pod("gw-0")], 1, node_images=node_images))

    def test_record_without_the_pinned_tag(self):
        image = NEW_BUILD.image("gateway")
        untagged = ri.NodeImage(image.config_digest, (), (_import_digest(NEW_BUILD, "gateway"),))
        failures = _failures("gateway", [_pod("gw-0")], 1, node_images={"maops-k8s-day7-worker": [untagged]})
        self.assertTrue(any("pinned tag" in m for m in failures), failures)

    def test_unready_pod(self):
        for kwargs, needle in ((dict(ready=False), "Running and Ready"), (dict(container_ready=False), "ready=False"), (dict(container_id=""), "containerID="), (dict(terminating=True), "not terminating")):
            with self.subTest(needle=needle):
                failures = _failures("app", [_pod("app-0", "app", **kwargs)], 1)
                self.assertTrue(any(needle in m for m in failures), failures)

    def test_missing_or_extra_pods(self):
        self.assertTrue(any("expected exactly 3" in m for m in _failures("gateway", [_pod("gw-0"), _pod("gw-1")], 3)))
        self.assertTrue(any("expected exactly 1" in m for m in _failures("state", [_pod("s-0", "state"), _pod("s-1", "state")], 1)))

    def test_missing_container_status(self):
        pod = _pod("app-0", "app")
        pod["status"]["containerStatuses"] = []
        self.assertTrue(any("has no status" in m for m in _failures("app", [pod], 1)))


class CandidateImageTests(unittest.TestCase):
    def test_candidate_on_the_build_passes(self):
        self.assertEqual(_failures("candidate", [_pod("c-0", "candidate"), _pod("c-1", "candidate", node="maops-k8s-day7-worker2")], 2), [])

    def test_candidate_mismatch(self):
        cases = {
            "previous gateway build": _pod("c-0", "candidate", build=OLD_BUILD),
            "app image": _pod("c-0", "candidate", image_ref=NEW_BUILD.image("app").node_ref, image_id=_import_digest(NEW_BUILD, "app")),
        }
        for name, pod in cases.items():
            with self.subTest(name):
                self.assertTrue(_failures("candidate", [pod], 1))

    def test_candidate_present_when_disabled_or_missing_when_enabled(self):
        self.assertTrue(_failures("candidate", [_pod("c-0", "candidate")], 0))
        self.assertTrue(_failures("candidate", [], 2))

    def test_expected_counts_follow_the_release_values(self):
        base = {"gateway": {"replicas": 3}, "app": {"replicas": 3}, "state": {"replicas": 1}}
        self.assertEqual(ri.expected_counts({**base, "candidate": {"enabled": False, "replicas": 2}})["candidate"], 0)
        self.assertEqual(ri.expected_counts({**base, "candidate": {"enabled": True, "replicas": 2}})["candidate"], 2)
        with self.assertRaises(KeyError):
            ri.expected_counts({"gateway": {"replicas": 3}})

    def test_promotion_gate_blocks_a_candidate_on_another_build(self):
        with mock.patch.object(kube, "PROFILE", "day7"), pinned_build(), mock.patch.object(ri, "read_node_images", return_value=_node_records(OLD_BUILD, NEW_BUILD)):
            checks = d7.observe_candidate_images([_pod("c-0", "candidate", build=OLD_BUILD)], 1)
        self.assertTrue([m for ok, m in checks if not ok])

    def test_promotion_gate_fails_closed_without_a_build(self):
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(day7_build, "load_current", side_effect=day7_build.BuildError("no build")):
            checks = d7.observe_candidate_images([_pod("c-0", "candidate")], 1)
        self.assertEqual([ok for ok, _ in checks], [False])


class WaitAndGuardTests(unittest.TestCase):
    def test_converging_rollout_passes_once_settled(self):
        observations = iter([ri.Observation([(False, "extra terminating Pod")]), ri.Observation([(True, "all good")])])
        rec = d7.Recorder()
        with mock.patch.object(ri, "observe", side_effect=lambda *a, **k: next(observations)):
            ok = _quiet(ri.wait_for_running_images, rec, NEW_BUILD, sleep=lambda s: None, monotonic=lambda: 0.0)
        self.assertTrue(ok)
        self.assertEqual(rec.failures(), [])

    def test_unreadable_state_fails_immediately_without_retry(self):
        observe = mock.Mock(return_value=ri.Observation([], "gateway Pods unreadable"))
        rec = d7.Recorder()
        clock = iter(range(0, 10_000, 10))
        with mock.patch.object(ri, "observe", observe):
            self.assertFalse(_quiet(ri.wait_for_running_images, rec, NEW_BUILD, timeout=60, sleep=lambda s: None, monotonic=lambda: float(next(clock))))
        observe.assert_called_once()

    def test_timeout_records_the_final_failing_checks(self):
        clock = iter([0.0, 10.0, 999.0, 999.0])
        rec = d7.Recorder()
        with mock.patch.object(ri, "observe", return_value=ri.Observation([(False, "gw-0 runs another build")])):
            ok = _quiet(ri.wait_for_running_images, rec, NEW_BUILD, timeout=60, sleep=lambda s: None, monotonic=lambda: next(clock))
        self.assertFalse(ok)
        self.assertIn("running images: gw-0 runs another build", rec.failures())

    def test_node_reads_refuse_outside_day7(self):
        with mock.patch("subprocess.run") as run:
            with mock.patch.object(kube, "PROFILE", "day6"), self.assertRaises(RuntimeError):
                ri.read_node_images("maops-k8s-day7-worker")
            for bad in ("maops-k8s-day6-worker", "x maops-k8s-day7-worker", "maops-k8s-day7-worker;rm", "maops-k8s-day7-worker ", "maops-k8s-day7-worker\n", "maops-k8s-day7-workerx"):
                with self.subTest(node=bad), mock.patch.object(kube, "PROFILE", "day7"), self.assertRaises(RuntimeError):
                    ri.read_node_images(bad)
        run.assert_not_called()

    def test_observe_and_main_refuse_outside_day7(self):
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch("subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                ri.observe(NEW_BUILD)
            self.assertEqual(_quiet(ri.main), 1)
        run.assert_not_called()


class ObserveWiringTests(unittest.TestCase):
    """observe(): counts come from the release's own values and reach the
    evaluation; unreadable/malformed values are fatal."""

    VALUES = {"gateway": {"replicas": 1}, "app": {"replicas": 1}, "state": {"replicas": 1}, "candidate": {"enabled": False, "replicas": 2}}

    def _observe(self, values, pods_by_component, **kw):
        def list_json(resource, selector=None, namespace=None):
            for component, pods in pods_by_component.items():
                if selector == ri.selector(component):
                    return pods
            return []
        nodes = {n: _node_records(NEW_BUILD) for n in ("maops-k8s-day7-worker", "maops-k8s-day7-worker2")}
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(d7, "helm_values", return_value=values) as hv, \
                mock.patch.object(d7, "list_json", side_effect=list_json), mock.patch.object(ri, "read_node_images", side_effect=lambda n: nodes.get(n)):
            obs = ri.observe(NEW_BUILD, **kw)
        return obs, hv

    def _healthy(self):
        return {"gateway": [_pod("gw-0")], "app": [_pod("app-0", "app")], "state": [_pod("state-0", "state")], "candidate": []}

    def test_healthy_release_passes_and_reads_all_values(self):
        obs, hv = self._observe(self.VALUES, self._healthy())
        self.assertTrue(obs.ok, [m for ok, m in obs.checks if not ok])
        hv.assert_called_once_with(all_values=True)

    def test_unreadable_or_malformed_values_are_fatal(self):
        for values in (None, {"gateway": {"replicas": 1}}, {**self.VALUES, "app": {"replicas": "three"}}):
            with self.subTest(values=str(values)[:40]):
                obs, _ = self._observe(values, self._healthy())
                self.assertTrue(obs.fatal)
                self.assertFalse(obs.ok)

    def test_live_candidate_pod_while_disabled_fails(self):
        pods = self._healthy()
        pods["candidate"] = [_pod("c-0", "candidate")]
        obs, _ = self._observe(self.VALUES, pods)
        self.assertTrue(any("candidate" in m and "expected exactly 0" in m for ok, m in obs.checks if not ok))

    def test_replica_count_from_values_is_enforced(self):
        obs, _ = self._observe({**self.VALUES, "gateway": {"replicas": 3}}, self._healthy())
        self.assertTrue(any("gateway: 1 Pod" in m and "expected exactly 3" in m for ok, m in obs.checks if not ok))

    def test_component_and_count_overrides_skip_the_values_read(self):
        obs, hv = self._observe(None, {"candidate": [_pod("c-0", "candidate")]}, components=("candidate",), counts={"candidate": 1})
        self.assertTrue(obs.ok, obs.checks)
        hv.assert_not_called()

    def test_unreadable_pods_are_fatal(self):
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(d7, "helm_values", return_value=self.VALUES), mock.patch.object(d7, "list_json", return_value=None):
            obs = ri.observe(NEW_BUILD)
        self.assertTrue(obs.fatal)


class RunningImagesMainTests(unittest.TestCase):
    """Once the run's strategy baseline exists, the standalone gate judges
    the BASELINE's build and refuses a re-pointed current.json."""

    def _main(self, baseline_present, baseline_build):
        env = {d7.STRATEGY_BASELINE_PATH_ENV: "/nonexistent/strategy-baseline.json"}
        gate = mock.Mock(side_effect=lambda rec, build, **k: rec.record(True, f"images {build.build_id}"))
        with mock.patch.dict(os.environ, env), mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "verify_context"), pinned_build(NEW_BUILD), \
                mock.patch.object(ri.os.path, "lexists", return_value=baseline_present), \
                mock.patch.object(d7, "load_strategy_baseline", side_effect=lambda: d7.require_baseline_build({"build": baseline_build.record()}) and {"build": baseline_build.record()}), \
                mock.patch.object(ri, "wait_for_running_images", gate):
            rc = _quiet(ri.main)
        return rc, gate

    def test_before_the_baseline_the_current_build_is_judged(self):
        rc, gate = self._main(False, OLD_BUILD)
        self.assertEqual(rc, 0)
        self.assertEqual(gate.call_args.args[1], NEW_BUILD)

    def test_after_the_baseline_a_different_current_build_is_refused(self):
        rc, gate = self._main(True, OLD_BUILD)
        self.assertEqual(rc, 1)
        gate.assert_not_called()

    def test_after_the_baseline_the_matching_build_is_judged(self):
        rc, gate = self._main(True, NEW_BUILD)
        self.assertEqual(rc, 0)
        self.assertEqual(gate.call_args.args[1], NEW_BUILD)


class RecreateReplacementImageTests(unittest.TestCase):
    """The replacement candidate Pods created by recreate-changed must run
    the run's build - checked with the candidate's own count."""

    def _body(self, images_ok):
        import test_day7_orchestration as orch
        import day7_recreate

        saved = list(d7.SUBMITTED_STAGES)
        self.addCleanup(lambda: d7.SUBMITTED_STAGES.__setitem__(slice(None), saved))
        orch.FakeCluster(self)
        gate = mock.Mock(side_effect=lambda rec, build, **k: rec.record(images_ok, f"{k.get('label')}: fixture"))
        upgrade = day7_recreate.UpgradeObservation(0, "already exited (0)", "", [], [], 1.0)
        rec = d7.Recorder()
        with mock.patch.object(ri, "wait_for_running_images", gate), mock.patch.object(day7_recreate, "run_observed_upgrade", return_value=upgrade):
            _quiet(day7_recreate.body, rec, orch.BASELINE)
        return rec, gate

    def test_called_for_the_candidate_with_the_stage_replica_count_and_the_build(self):
        rec, gate = self._body(True)
        calls = [c for c in gate.call_args_list if c.kwargs.get("label") == "replacement candidate images"]
        self.assertEqual(len(calls), 1, gate.call_args_list)
        replicas = d7.load_stage_values("recreate-changed")["candidate"]["replicas"]
        self.assertEqual(calls[0].args[1], SAMPLE_BUILD)
        self.assertEqual(calls[0].kwargs["components"], ("candidate",))
        self.assertEqual(calls[0].kwargs["counts"], {"candidate": replicas})

    def test_replacement_on_another_build_fails_the_experiment(self):
        rec, _ = self._body(False)
        self.assertIn("replacement candidate images: fixture", rec.failures())


# --------------------------------------------------------------------------
# The no-op deployment that passed in run ce55f5fb...
# --------------------------------------------------------------------------


class NoOpDeploymentTests(unittest.TestCase):
    """Reconstructs the second run: new build on every node under the
    mutable tag, release values unchanged (no build overlay), every Pod
    Ready - the old gates passed; each new control must fail it."""

    def test_running_gate_fails_although_every_pod_is_ready_and_nodes_hold_the_new_build(self):
        nodes = {n: _node_records(OLD_BUILD, NEW_BUILD, mutable_tag_on=NEW_BUILD) for n in ("maops-k8s-day7-worker", "maops-k8s-day7-worker2")}
        for component, count in (("gateway", 3), ("app", 3), ("state", 1)):
            pods = [_pod(f"{component}-{i}", component, build=OLD_BUILD, image_ref=f"docker.io/library/{NEW_BUILD.image(component).repository}:{day7_build.VERSION}") for i in range(count)]
            with self.subTest(component=component):
                self.assertTrue(all(d7.pod_is_ready(p) for p in pods))
                self.assertTrue(_failures(component, pods, count, node_images=nodes))

    def test_release_values_without_the_build_are_not_the_stable_stage(self):
        self.assertNotEqual(d7.load_stage_values("stable"), d7.expected_release_values("stable", NEW_BUILD))
        self.assertNotEqual(d7.expected_release_values("stable", OLD_BUILD), d7.expected_release_values("stable", NEW_BUILD))

    def test_a_new_build_can_never_render_an_identical_pod_template(self):
        findings = helm_check.check_day7_build_pinning(CHART, tempfile.mkdtemp())
        self.assertEqual([f.name for f in findings if not f.ok], [])
        self.assertTrue(any(f.name.endswith("new_build_changes_every_pod_template") for f in findings))

    def test_old_baseline_is_refused_for_a_new_build(self):
        with pinned_build(NEW_BUILD):
            with self.assertRaises(RuntimeError):
                d7.require_baseline_build({"stable_message": "m"})
            with self.assertRaises(RuntimeError):
                d7.require_baseline_build({"build": OLD_BUILD.record()})
            self.assertEqual(d7.require_baseline_build({"build": NEW_BUILD.record()}), NEW_BUILD)


# --------------------------------------------------------------------------
# Chart guard (real `helm template`, no cluster)
# --------------------------------------------------------------------------


class ChartGuardTests(unittest.TestCase):
    def _render(self, *files):
        return subprocess.run(["helm", "template", "maops-kubernetes-platform-day7", CHART, "--namespace", "maops-platform", *sum((["-f", f] for f in files), [])], capture_output=True, text=True, timeout=60)

    def _overlay(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_stage_without_build_is_refused(self):
        result = self._render(str(STAGES / "stable.yaml"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build.requirePinnedTags=true", result.stderr)

    def test_mutable_short_uppercase_or_foreign_version_tags_are_refused(self):
        good = day7_build.overlay_yaml(SAMPLE_BUILD)
        tag = SAMPLE_BUILD.image("app").tag
        for bad in (day7_build.VERSION, tag[:-1], tag.upper().replace("-CFG-", "-cfg-"), tag.replace(day7_build.VERSION, "0.6.0", 1)):
            with self.subTest(tag=bad):
                result = self._render(str(STAGES / "stable.yaml"), self._overlay(good.replace(tag, bad)))
                self.assertNotEqual(result.returncode, 0, result.stdout[:200])
                self.assertIn("images.app.tag", result.stderr)

    def test_default_day6_render_keeps_the_version_tag(self):
        result = self._render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'image: "maops-kubernetes-gateway:{day7_build.VERSION}"', result.stdout)
        self.assertNotIn("-cfg-", result.stdout)


# --------------------------------------------------------------------------
# Baseline capture refuses an unverified build
# --------------------------------------------------------------------------


class BaselineBuildTests(unittest.TestCase):
    def _collect(self, values, images_ok=True, build_error=False):
        found = {"metadata": {"uid": "u", "generation": 1}, "status": {"readyReplicas": 3}, "spec": {}, "data": {"APP_MESSAGE": "stable"}}
        patches = [
            mock.patch.object(kube, "PROFILE", "day7"),
            mock.patch.object(d7, "helm_status", return_value={"version": 3, "info": {"status": "deployed"}}),
            mock.patch.object(d7, "helm_values", return_value=values),
            mock.patch.object(d7, "helm_manifest", return_value="manifest"),
            mock.patch.object(d7, "candidate_leftovers", return_value=([], [])),
            mock.patch.object(d7, "read_route", return_value=d7.RouteSnapshot("r", 1, ((d7.STABLE_SERVICE, 8080, 1),), True, {})),
            mock.patch.object(d7, "route_is_current", return_value=(True, "route ok")),
            mock.patch.object(d7, "gateway_is_current", return_value=(True, "gw ok")),
            mock.patch.object(d7, "get_json_or_none", return_value=("found", {**found, "spec": {"volumeName": "pv-1"}})),
            mock.patch.object(day7_baseline, "_uid_gen", side_effect=lambda kind, name, namespace=None: {"uid": f"{kind}-{name}", "generation": 1, "readyReplicas": 1 if kind == "statefulset" else 3}),
            mock.patch.object(ri, "wait_for_running_images", side_effect=lambda rec, build, **k: rec.record(images_ok, "running images")),
            mock.patch.object(day7_build, "load_current", side_effect=day7_build.BuildError("none")) if build_error else pinned_build(),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        rec = d7.Recorder()
        return _quiet(day7_baseline.collect, rec), rec

    def test_verified_build_is_recorded(self):
        record, rec = self._collect(d7.expected_release_values("stable", SAMPLE_BUILD))
        self.assertIsNotNone(record, rec.failures())
        self.assertEqual(record["build"], SAMPLE_BUILD.record())

    def test_mutable_tag_release_running_images_failure_or_no_build_refuse_capture(self):
        for kwargs in (dict(values=d7.load_stage_values("stable")), dict(values=d7.expected_release_values("stable", SAMPLE_BUILD), images_ok=False), dict(values=d7.expected_release_values("stable", SAMPLE_BUILD), build_error=True)):
            with self.subTest(kwargs=str(kwargs)[:60]):
                record, _ = self._collect(**kwargs)
                self.assertIsNone(record)


if __name__ == "__main__":
    unittest.main()

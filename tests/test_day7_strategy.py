"""
DAY7: Docker/Kubernetes-free unit tests for scripts/day7_strategy.py.

Every live call (helm, kubectl, HTTP) is replaced by fixtures or mocks;
these tests prove the decision logic: the exact Helm stage command, the
route/endpoint/gate evaluations, that a failed gate never reaches Helm,
response classification and the canary verdict (both versions required,
never an exact ratio), path-probe verdicts (a local connect is never
evidence), and that restoration always runs and is reported separately.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_build
import day7_strategy as d7
import kube
from day7_build_fixture import SAMPLE_BUILD, pinned_build

STAGES_DIR = Path(__file__).resolve().parent.parent / "helm-values" / "day7"


def _quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class StageFilesTests(unittest.TestCase):
    def test_every_stage_spells_out_every_day7_knob(self):
        for stage in d7.STAGES:
            with self.subTest(stage=stage):
                v = d7.load_stage_values(stage)
                self.assertEqual(set(v), {"candidate", "routing", "validation", "build"})
                self.assertEqual(v["build"], {"requirePinnedTags": True}, "every Day 7 stage must refuse the mutable image tag")
                self.assertEqual(v["validation"], {"namespace": "maops-day7-validation", "diagnostics": {"serviceAccountName": "maops-diagnostics"}})
                self.assertEqual(set(v["candidate"]), {"enabled", "replicas", "strategy", "config", "faultInjection"})
                self.assertIn("mode", v["routing"])

    def test_stable_stage_is_candidate_off_and_stable_only(self):
        v = d7.load_stage_values("stable")
        self.assertFalse(v["candidate"]["enabled"])
        self.assertEqual(v["routing"], {"mode": "stable"})

    def test_only_promotion_stages_move_traffic_off_stable_except_recreate_changed(self):
        moving = {s for s in d7.STAGES if d7.load_stage_values(s)["routing"]["mode"] != "stable"}
        self.assertEqual(moving, set(d7.PROMOTION_STAGES) | {"recreate-changed"})

    def test_recreate_changed_differs_from_serving_only_in_candidate_message(self):
        a, b = d7.load_stage_values("recreate-serving"), d7.load_stage_values("recreate-changed")
        self.assertNotEqual(a["candidate"]["config"]["appMessage"], b["candidate"]["config"]["appMessage"])
        b["candidate"]["config"]["appMessage"] = a["candidate"]["config"]["appMessage"]
        self.assertEqual(a, b)

    def test_unknown_stage_rejected(self):
        with self.assertRaises(ValueError):
            d7.stage_path("stable-ish")


class HelmStageCommandTests(unittest.TestCase):
    def test_command_resets_values_and_uses_the_stage_then_the_build_overlay(self):
        cmd = d7.helm_stage_command("canary-90-10", SAMPLE_BUILD)
        self.assertEqual(cmd[:4], ["helm", "upgrade", kube.HELM_RELEASE_NAME, str(d7.CHART_DIR)])
        self.assertIn("--reset-values", cmd)
        self.assertNotIn("--reuse-values", cmd)
        self.assertNotIn("--reset-then-reuse-values", cmd)
        self.assertNotIn("--set", cmd)
        self.assertNotIn("--install", cmd)
        files = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-f"]
        self.assertEqual(files, [str(STAGES_DIR / "canary-90-10.yaml"), str(day7_build.values_path(SAMPLE_BUILD))])
        self.assertEqual(cmd[cmd.index("--kube-context") + 1], kube.CONTEXT)
        self.assertEqual(cmd[cmd.index("--kubeconfig") + 1], kube.KUBECONFIG_PATH)
        self.assertEqual(cmd[cmd.index("--timeout") + 1], f"{d7.HELM_TIMEOUT_SECONDS}s")

    def test_wait_strategy(self):
        self.assertIn("--wait=watcher", d7.helm_stage_command("green-prepared", SAMPLE_BUILD))
        self.assertIn("--wait=hookOnly", d7.helm_stage_command("candidate-unready", SAMPLE_BUILD))


class _Day7Profile(unittest.TestCase):
    """Live entry points refuse non-day7 profiles; these tests mock every
    external call and select the day7 profile explicitly."""

    def setUp(self):
        patcher = mock.patch.object(kube, "PROFILE", "day7")
        patcher.start()
        self.addCleanup(patcher.stop)
        blocker = mock.patch.object(kube, "run", side_effect=AssertionError("unit tests must never run kubectl"))
        blocker.start()
        self.addCleanup(blocker.stop)


class ApplyStageTests(_Day7Profile):
    def _apply(self, revisions, values, rc=0):
        completed = mock.Mock(returncode=rc, stdout="", stderr="boom")
        rec = d7.Recorder()
        with pinned_build(), mock.patch.object(d7, "helm_revision", side_effect=revisions), mock.patch.object(d7, "helm_values", return_value=values), mock.patch.object(d7, "_run", return_value=completed) as run:
            ok = _quiet(d7.apply_stage, rec, "green-prepared")
        self.run_mock = run
        return ok, rec

    @staticmethod
    def _expected():
        return d7.expected_release_values("green-prepared", SAMPLE_BUILD)

    def test_success_requires_new_revision_and_exact_values(self):
        ok, rec = self._apply([4, 5], self._expected())
        self.assertTrue(ok)
        self.assertEqual(rec.failures(), [])
        cmd = self.run_mock.call_args[0][0]
        self.assertEqual(cmd[-len(d7.helm_scope_args()):], d7.helm_scope_args())
        self.assertIn(str(day7_build.values_path(SAMPLE_BUILD)), cmd)

    def test_values_carried_from_previous_stage_fail(self):
        carried = self._expected()
        carried["routing"]["stableWeight"] = 90
        ok, rec = self._apply([4, 5], carried)
        self.assertFalse(ok)

    def test_bare_stage_values_without_the_build_fail(self):
        """A release whose values lack the pinned tags is running the
        mutable tag - never accepted as the stage."""
        ok, rec = self._apply([4, 5], d7.load_stage_values("green-prepared"))
        self.assertFalse(ok)
        self.assertTrue(any("build" in m for m in rec.failures()))

    def test_other_build_values_fail(self):
        other = day7_build.build_from_digests({c: "sha256:" + "a" * 64 for c in day7_build.COMPONENTS})
        ok, _ = self._apply([4, 5], d7.expected_release_values("green-prepared", other))
        self.assertFalse(ok)

    def test_unreadable_build_submits_nothing(self):
        rec = d7.Recorder()
        before = list(d7.SUBMITTED_STAGES)
        with mock.patch.object(day7_build, "load_current", side_effect=day7_build.BuildError("no current.json")), mock.patch.object(d7, "_run") as run, mock.patch.object(d7, "helm_revision") as rev:
            ok = _quiet(d7.apply_stage, rec, "green-prepared")
        self.assertFalse(ok)
        run.assert_not_called()
        rev.assert_not_called()
        self.assertEqual(d7.SUBMITTED_STAGES, before)

    def test_revision_not_advanced_fails(self):
        ok, _ = self._apply([4, 4], self._expected())
        self.assertFalse(ok)

    def test_helm_failure_fails(self):
        ok, _ = self._apply([4, 5], self._expected(), rc=1)
        self.assertFalse(ok)


def _route(backends, generation=3, conditions=None, parent=True):
    status_parents = []
    if parent:
        status_parents = [{
            "parentRef": {"name": kube.GATEWAY_API_GATEWAY_NAME, "namespace": kube.INGRESS_NAMESPACE},
            "conditions": conditions if conditions is not None else [
                {"type": "Accepted", "status": "True", "observedGeneration": generation},
                {"type": "ResolvedRefs", "status": "True", "observedGeneration": generation},
            ],
        }]
    return {"metadata": {"uid": "r-1", "generation": generation}, "spec": {"rules": [{"backendRefs": backends}]}, "status": {"parents": status_parents}}


class RouteTests(unittest.TestCase):
    def test_expected_backends_by_mode(self):
        self.assertEqual(d7.expected_backends(d7.load_stage_values("stable")), (("maops-gateway", 8080, 1),))
        self.assertEqual(d7.expected_backends(d7.load_stage_values("blue-green-cutover")), (("maops-gateway-candidate", 8080, 1),))
        self.assertEqual(d7.expected_backends(d7.load_stage_values("canary-90-10")), (("maops-gateway", 8080, 90), ("maops-gateway-candidate", 8080, 10)))

    def test_unweighted_live_ref_matches_default_weight(self):
        snap = d7.parse_route(_route([{"name": "maops-gateway", "port": 8080}]))
        self.assertTrue(d7.route_is_current(snap, d7.expected_backends(d7.load_stage_values("stable")))[0])

    def test_crd_defaulted_fields_are_equivalent(self):
        snap = d7.parse_route(_route([{"group": "", "kind": "Service", "name": "maops-gateway", "port": 8080, "weight": 1}]))
        self.assertTrue(d7.route_is_current(snap, d7.expected_backends(d7.load_stage_values("stable")))[0])

    def test_weighted_route_exact(self):
        snap = d7.parse_route(_route([{"name": "maops-gateway", "port": 8080, "weight": 90}, {"name": "maops-gateway-candidate", "port": 8080, "weight": 10}]))
        self.assertTrue(d7.route_is_current(snap, d7.expected_backends(d7.load_stage_values("canary-90-10")))[0])

    def test_wrong_weight_port_or_foreign_backend_rejected(self):
        expected = d7.expected_backends(d7.load_stage_values("canary-90-10"))
        for refs in (
            [{"name": "maops-gateway", "port": 8080, "weight": 80}, {"name": "maops-gateway-candidate", "port": 8080, "weight": 20}],
            [{"name": "maops-gateway", "port": 8080, "weight": 90}, {"name": "maops-gateway-candidate", "port": 8081, "weight": 10}],
            [{"name": "maops-gateway", "port": 8080, "weight": 90}, {"name": "maops-gateway-candidate", "namespace": "other", "port": 8080, "weight": 10}],
            [{"name": "maops-gateway", "port": 8080, "weight": 90}],
        ):
            with self.subTest(refs=refs):
                self.assertFalse(d7.route_is_current(d7.parse_route(_route(refs)), expected)[0])

    def test_stale_conditions_rejected(self):
        stale = [
            {"type": "Accepted", "status": "True", "observedGeneration": 2},
            {"type": "ResolvedRefs", "status": "True", "observedGeneration": 3},
        ]
        snap = d7.parse_route(_route([{"name": "maops-gateway", "port": 8080}], generation=3, conditions=stale))
        ok, detail = d7.route_is_current(snap, d7.expected_backends(d7.load_stage_values("stable")))
        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_unresolved_refs_or_missing_parent_rejected(self):
        bad = [{"type": "Accepted", "status": "True", "observedGeneration": 3}, {"type": "ResolvedRefs", "status": "False", "observedGeneration": 3}]
        expected = d7.expected_backends(d7.load_stage_values("stable"))
        self.assertFalse(d7.route_is_current(d7.parse_route(_route([{"name": "maops-gateway", "port": 8080}], conditions=bad)), expected)[0])
        self.assertFalse(d7.route_is_current(d7.parse_route(_route([{"name": "maops-gateway", "port": 8080}], parent=False)), expected)[0])
        self.assertFalse(d7.route_is_current(None, expected)[0])


def _slice(eps):
    return {"endpoints": [{"addresses": [ip], "conditions": {"ready": ready}, "targetRef": {"kind": "Pod", "name": name}} for ip, name, ready in eps]}


def _pod(name, ip, ready=True, sa=None, run_as=10001, listeners=None, sidecar=False, annotation="enabled"):
    containers = [{
        "name": "maops-gateway",
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }]
    if sidecar:
        containers.append({"name": "istio-proxy"})
    return {
        "metadata": {"name": name, "uid": f"uid-{name}", "annotations": {"ambient.istio.io/redirection": annotation}},
        "spec": {
            "serviceAccountName": sa or kube.GATEWAY_SERVICE_ACCOUNT,
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": run_as, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": containers,
            "volumes": [{"name": "internal-auth", "secret": {"secretName": kube.INTERNAL_SECRET, "defaultMode": 288}}],
        },
        "status": {"phase": "Running", "podIP": ip, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _observation(**overrides):
    cand = [_pod("cand-a", "10.0.1.1"), _pod("cand-b", "10.0.2.1")]
    stable = [_pod("gw-a", "10.0.1.9"), _pod("gw-b", "10.0.2.9"), _pod("gw-c", "10.0.1.8")]
    obs = dict(
        expected_replicas=2,
        deployment={"metadata": {"generation": 2}, "spec": {"replicas": 2}, "status": {"observedGeneration": 2, "readyReplicas": 2, "updatedReplicas": 2, "availableReplicas": 2}},
        pods=cand,
        stable_pods=stable,
        candidate_endpoints=d7.parse_endpoint_slices([_slice([("10.0.1.1", "cand-a", True), ("10.0.2.1", "cand-b", True)])]),
        stable_endpoints=d7.parse_endpoint_slices([_slice([("10.0.1.9", "gw-a", True), ("10.0.2.9", "gw-b", True), ("10.0.1.8", "gw-c", True)])]),
        listeners={"cand-a": {15001, 15006, 15008}, "cand-b": {15001, 15006, 15008, 8080}},
        route=d7.parse_route(_route([{"name": "maops-gateway", "port": 8080}])),
        expected_route=d7.expected_backends(d7.load_stage_values("green-prepared")),
        gateway=(True, "gateway current"),
        image_checks=[(True, "candidate containers run the verified build (fixture)")],
    )
    obs.update(overrides)
    return d7.GateObservation(**obs)


def _gate_failures(obs):
    return [m for ok, m in d7.evaluate_gate(obs) if not ok]


class GateTests(unittest.TestCase):
    def test_healthy_candidate_passes_every_check(self):
        self.assertEqual(_gate_failures(_observation()), [])

    def test_unobserved_or_mismatched_candidate_images_block(self):
        self.assertTrue(any("not verified against the build" in m for m in _gate_failures(_observation(image_checks=None))))
        self.assertIn("cand-a runs another build", _gate_failures(_observation(image_checks=[(False, "cand-a runs another build")])))

    def test_missing_deployment_blocks(self):
        self.assertTrue(_gate_failures(_observation(deployment=None)))

    def test_unready_pod_blocks(self):
        obs = _observation(pods=[_pod("cand-a", "10.0.1.1", ready=False), _pod("cand-b", "10.0.2.1")])
        self.assertTrue(any("Ready" in m for m in _gate_failures(obs)))

    def test_unconverged_deployment_blocks(self):
        dep = {"metadata": {"generation": 3}, "spec": {"replicas": 2}, "status": {"observedGeneration": 2, "readyReplicas": 1, "updatedReplicas": 1, "availableReplicas": 1}}
        failures = _gate_failures(_observation(deployment=dep))
        self.assertTrue(any("observedGeneration" in m for m in failures))
        self.assertTrue(any("readyReplicas" in m for m in failures))

    def test_missing_listener_or_probe_error_blocks(self):
        self.assertTrue(any("MISSING" in m for m in _gate_failures(_observation(listeners={"cand-a": {15001, 15008}, "cand-b": {15001, 15006, 15008}}))))
        self.assertTrue(any("failed closed" in m for m in _gate_failures(_observation(listeners={"cand-a": "exec failed", "cand-b": {15001, 15006, 15008}}))))

    def test_security_drift_blocks(self):
        for pod, needle in (
            (_pod("cand-a", "10.0.1.1", run_as=0), "runAsUser"),
            (_pod("cand-a", "10.0.1.1", sa="default"), "serviceAccountName"),
            (_pod("cand-a", "10.0.1.1", sidecar=True), "sidecar"),
            (_pod("cand-a", "10.0.1.1", annotation=None), "redirection"),
        ):
            with self.subTest(needle=needle):
                failures = _gate_failures(_observation(pods=[pod, _pod("cand-b", "10.0.2.1")]))
                self.assertTrue(any(needle in m for m in failures), failures)

    def test_overlapping_endpoints_block(self):
        overlapping = d7.parse_endpoint_slices([_slice([("10.0.1.1", "cand-a", True), ("10.0.2.1", "cand-b", True), ("10.0.1.9", "gw-a", True)])])
        failures = _gate_failures(_observation(candidate_endpoints=overlapping))
        self.assertTrue(any("disjoint" in m or "only candidate Pods" in m for m in failures))

    def test_stable_endpoints_short_blocks(self):
        short = d7.parse_endpoint_slices([_slice([("10.0.1.9", "gw-a", True), ("10.0.2.9", "gw-b", False)])])
        self.assertTrue(any("stable Service" in m for m in _gate_failures(_observation(stable_endpoints=short))))

    def test_route_already_moved_blocks(self):
        moved = d7.parse_route(_route([{"name": "maops-gateway-candidate", "port": 8080}]))
        self.assertTrue(any("HTTPRoute" in m for m in _gate_failures(_observation(route=moved))))

    def test_gateway_not_current_blocks(self):
        self.assertIn("gateway stale", _gate_failures(_observation(gateway=(False, "gateway stale"))))


class PromotionTests(_Day7Profile):
    def test_failed_gate_never_submits_and_never_probes_paths(self):
        apply, paths = mock.Mock(), mock.Mock(return_value=True)
        with mock.patch.object(d7, "run_gate", return_value=(False, [(False, "blocked")])):
            result = _quiet(d7.promote, d7.Recorder(), "canary-90-10", "candidate-unready", apply=apply, path_check=paths)
        apply.assert_not_called()
        paths.assert_not_called()
        self.assertFalse(result.submitted)
        self.assertFalse(result.gate_ok)
        self.assertIsNone(result.paths_ok)
        self.assertIn("readiness/preflight gate refused", result.refusal)

    def test_failed_or_raising_path_check_never_submits(self):
        for paths in (mock.Mock(return_value=False), mock.Mock(side_effect=d7.subprocess.TimeoutExpired("kubectl", 5)), mock.Mock(side_effect=RuntimeError("unreadable"))):
            apply = mock.Mock()
            rec = d7.Recorder()
            with self.subTest(paths=paths), mock.patch.object(d7, "run_gate", return_value=(True, [(True, "ok")])):
                result = _quiet(d7.promote, rec, "blue-green-cutover", "green-prepared", apply=apply, path_check=paths)
            apply.assert_not_called()
            self.assertTrue(result.gate_ok)
            self.assertFalse(result.submitted)
            self.assertIs(result.paths_ok, False)
            self.assertIn("path checks", result.refusal)
            self.assertTrue(any("REFUSED" in m for m in rec.failures()))

    def test_live_entry_points_refuse_the_day6_profile(self):
        with mock.patch.object(kube, "PROFILE", "day6"):
            for call in (lambda: d7.promote(d7.Recorder(), "canary-90-10", "green-prepared"),
                         lambda: d7.check_candidate_mesh_paths(d7.Recorder(), "x"),
                         lambda: d7.apply_stage(d7.Recorder(), "stable"),
                         lambda: d7.observe_gate((), 2)):
                with self.assertRaises(RuntimeError):
                    call()

    def test_passing_gate_submits_exactly_the_target_stage(self):
        apply = mock.Mock(return_value=True)
        paths = mock.Mock(return_value=True)
        with mock.patch.object(d7, "run_gate", return_value=(True, [(True, "ok")])) as gate:
            result = _quiet(d7.promote, d7.Recorder(), "blue-green-cutover", "green-prepared", apply=apply, path_check=paths)
        paths.assert_called_once()
        apply.assert_called_once()
        self.assertEqual(apply.call_args[0][1], "blue-green-cutover")
        self.assertTrue(result.submitted and result.applied_ok)
        self.assertEqual(gate.call_args[0][0], d7.expected_backends(d7.load_stage_values("green-prepared")))

    def test_non_promotion_or_candidate_less_source_rejected(self):
        with self.assertRaises(ValueError):
            d7.promote(d7.Recorder(), "green-prepared", "stable")
        with self.assertRaises(ValueError):
            d7.promote(d7.Recorder(), "canary-90-10", "stable")


def _sample(path, payload, status=200, outcome="ok"):
    return d7.Sample(0.0, path, outcome, status, payload)


IDENT = d7.Identity("stable msg", "candidate msg", frozenset({"gw-a", "gw-b"}), frozenset({"cand-a"}))


class ClassificationTests(unittest.TestCase):
    def test_root_requires_message_and_pod_to_agree(self):
        self.assertEqual(d7.classify(_sample("/", {"message": "stable msg", "hostname": "gw-a"}), IDENT), "stable")
        self.assertEqual(d7.classify(_sample("/", {"message": "candidate msg", "hostname": "cand-a"}), IDENT), "candidate")
        self.assertEqual(d7.classify(_sample("/", {"message": "candidate msg", "hostname": "gw-a"}), IDENT), "unidentified")
        self.assertEqual(d7.classify(_sample("/", {"message": "stable msg", "hostname": "someone-else"}), IDENT), "unidentified")

    def test_backend_requires_a_normal_app_result(self):
        ok = {"gateway_hostname": "cand-a", "backend_service": "maops-kubernetes-app"}
        self.assertEqual(d7.classify(_sample("/backend", ok), IDENT), "candidate")
        self.assertEqual(d7.classify(_sample("/backend", {**ok, "backend_service": "other"}), IDENT), "error")

    def test_errors(self):
        self.assertEqual(d7.classify(_sample("/", None, status=None, outcome="inconclusive"), IDENT), "error")
        self.assertEqual(d7.classify(_sample("/", {"error": "x"}, status=503, outcome="http_error"), IDENT), "error")

    def test_classify_by_message(self):
        msgs = {"old": "a", "new": "b"}
        self.assertEqual(d7.classify_by_message(_sample("/", {"message": "b"}), msgs), "new")
        self.assertEqual(d7.classify_by_message(_sample("/", {"message": "zzz"}), msgs), "unidentified")
        self.assertEqual(d7.classify_by_message(_sample("/", None, status=503, outcome="http_error"), msgs), "error")


class CanaryVerdictTests(unittest.TestCase):
    def _ok(self, classes, n=None):
        return all(ok for ok, _ in d7.canary_verdict(classes, n if n is not None else len(classes)))

    def test_both_versions_observed_passes_without_any_ratio_requirement(self):
        self.assertTrue(self._ok(["stable"] * 199 + ["candidate"]))
        self.assertTrue(self._ok(["stable"] * 100 + ["candidate"] * 100))

    def test_single_version_fails(self):
        self.assertFalse(self._ok(["stable"] * 200))
        self.assertFalse(self._ok(["candidate"] * 200))

    def test_any_error_or_unidentified_fails(self):
        self.assertFalse(self._ok(["stable"] * 180 + ["candidate"] * 19 + ["error"]))
        self.assertFalse(self._ok(["stable"] * 180 + ["candidate"] * 19 + ["unidentified"]))

    def test_incomplete_series_fails(self):
        self.assertFalse(self._ok(["stable"] * 100 + ["candidate"] * 10, n=200))


class PathProbeTests(unittest.TestCase):
    """Shape of the probe; the full contract is in test_day7_probe_contract.py."""

    def test_parse(self):
        self.assertEqual(d7.parse_probe_output("MAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=STATUS:200\n"), d7.ProbeResult("status", "200", "10.0.0.1"))
        self.assertEqual(d7.parse_probe_output("noise\nMAOPS_PROBE_ADDR=10.0.0.1\nMAOPS_PROBE=RESET:request\n").kind, "reset")
        self.assertEqual(d7.parse_probe_output("").kind, "malformed")

    def test_probe_snippet_resolves_then_performs_an_http_request(self):
        snippet = d7.probe_snippet("maops-state")
        self.assertIn("socket.getaddrinfo(H, P", snippet)
        self.assertIn("c.request('GET', '/livez'", snippet)
        self.assertIn("H, P, T = 'maops-state', 8080,", snippet)
        self.assertNotIn("MAOPS_PROBE=ERROR", snippet)


class EndpointParseTests(unittest.TestCase):
    def test_only_ready_endpoints_count_and_targets_recorded(self):
        view = d7.parse_endpoint_slices([_slice([("10.0.0.1", "a", True), ("10.0.0.2", "b", False)]), "junk"])
        self.assertEqual(view.ready_addresses, frozenset({"10.0.0.1"}))
        self.assertEqual(view.ready_target_pods, frozenset({"a"}))
        self.assertEqual(view.all_target_pods, frozenset({"a", "b"}))


class RunExperimentTests(unittest.TestCase):
    def _run(self, body, restore):
        rec = d7.Recorder()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = d7.run_experiment(rec, "T", body, restore)
        return rc, out.getvalue(), rec

    def test_success(self):
        rc, out, _ = self._run(lambda: True, lambda: True)
        self.assertEqual(rc, 0)
        self.assertIn("RESTORATION: PASS", out)

    def test_primary_exception_still_restores(self):
        restore = mock.Mock(return_value=True)
        rc, out, rec = self._run(mock.Mock(side_effect=RuntimeError("boom")), restore)
        restore.assert_called_once()
        self.assertEqual(rc, 1)
        self.assertIn("PRIMARY: FAIL", out)
        self.assertIn("RESTORATION: PASS", out)

    def test_restoration_failure_never_hidden_by_primary_success(self):
        rc, out, _ = self._run(lambda: True, lambda: False)
        self.assertEqual(rc, 1)
        self.assertIn("PRIMARY: PASS", out)
        self.assertIn("RESTORATION: FAIL", out)

    def test_both_failures_reported(self):
        def restore():
            raise TimeoutError("helm hung")
        rc, out, rec = self._run(lambda: False, restore)
        self.assertEqual(rc, 1)
        self.assertIn("PRIMARY: FAIL", out)
        self.assertIn("RESTORATION: FAIL", out)
        self.assertTrue(any("RESTORATION aborted" in m for m in rec.failures()))

    def test_interrupt_still_restores(self):
        restore = mock.Mock(return_value=True)
        rc, _, _ = self._run(mock.Mock(side_effect=KeyboardInterrupt()), restore)
        restore.assert_called_once()
        self.assertEqual(rc, 130)

    def test_recorded_failure_fails_even_if_body_returns_true(self):
        rec = d7.Recorder()
        rec.record(False, "something failed")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(d7.run_experiment(rec, "T", lambda: True, lambda: True), 1)


class RestoreOrVerifyTests(_Day7Profile):
    def setUp(self):
        super().setUp()
        self._saved = list(d7.SUBMITTED_STAGES)
        d7.SUBMITTED_STAGES.clear()

    def tearDown(self):
        d7.SUBMITTED_STAGES[:] = self._saved

    def test_nothing_submitted_means_verify_only(self):
        with mock.patch.object(d7, "apply_stage") as apply, mock.patch.object(d7, "verify_stable", return_value=True) as verify:
            self.assertTrue(_quiet(d7.restore_or_verify, d7.Recorder(), "m"))
        apply.assert_not_called()
        verify.assert_called_once()

    def test_submitted_means_helm_restore_then_verify(self):
        d7.SUBMITTED_STAGES.append("green-prepared")
        with mock.patch.object(d7, "apply_stage", return_value=True) as apply, mock.patch.object(d7, "verify_stable", return_value=True):
            self.assertTrue(_quiet(d7.restore_or_verify, d7.Recorder(), "m"))
        self.assertEqual(apply.call_args[0][1], "stable")

    def test_failed_helm_restore_is_a_failure_even_if_state_looks_stable(self):
        d7.SUBMITTED_STAGES.append("green-prepared")
        with mock.patch.object(d7, "apply_stage", return_value=False), mock.patch.object(d7, "verify_stable", return_value=True):
            self.assertFalse(_quiet(d7.restore_or_verify, d7.Recorder(), "m"))

    def test_apply_stage_tracks_submission_before_running_helm(self):
        with pinned_build(), mock.patch.object(d7, "helm_revision", return_value=None), mock.patch.object(d7, "helm_values", return_value={}), mock.patch.object(d7, "_run", side_effect=d7.subprocess.TimeoutExpired("helm", 1)):
            _quiet(d7.apply_stage, d7.Recorder(), "green-prepared")
        self.assertEqual(d7.SUBMITTED_STAGES, ["green-prepared"])


class CandidateLeftoverTests(unittest.TestCase):
    def test_api_error_is_undetermined_not_absent(self):
        with mock.patch.object(d7, "list_json", return_value=None), mock.patch.object(d7, "get_json_or_none", return_value=("error", None)):
            present, unknown = d7.candidate_leftovers()
        self.assertEqual(present, [])
        self.assertTrue(unknown)

    def test_explicit_absence(self):
        with mock.patch.object(d7, "list_json", return_value=[]), mock.patch.object(d7, "get_json_or_none", return_value=("not_found", None)):
            self.assertEqual(d7.candidate_leftovers(), ([], []))

    def test_leftover_reported(self):
        def lj(resource, selector=None, namespace=None):
            return [{"metadata": {"name": "maops-gateway-candidate-abc"}}] if resource == "pods" else []
        with mock.patch.object(d7, "list_json", side_effect=lj), mock.patch.object(d7, "get_json_or_none", return_value=("not_found", None)):
            present, _ = d7.candidate_leftovers()
        self.assertEqual(present, ["pods/maops-gateway-candidate-abc"])


class ProfileAndBaselineGuardTests(unittest.TestCase):
    def test_day7_tooling_refuses_the_day6_profile(self):
        with mock.patch.object(kube, "PROFILE", "day6"):
            with self.assertRaises(RuntimeError):
                d7.require_day7_profile()

    def test_missing_baseline_env_is_never_recaptured(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                d7.load_strategy_baseline()

    def test_untrusted_baseline_file_rejected(self):
        with mock.patch.dict("os.environ", {d7.STRATEGY_BASELINE_PATH_ENV: "/tmp/does-not-matter.json", d7.RUN_ID_ENV: "abc"}):
            with self.assertRaises(RuntimeError) as ctx:
                d7.load_strategy_baseline()
        self.assertIn("never recaptured", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

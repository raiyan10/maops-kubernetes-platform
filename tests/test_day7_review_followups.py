"""
DAY7 independent-review follow-ups - Docker/Kubernetes-free tests closing
the coverage gaps the Day 7 test, integration and security reviews found:

  - day7_final_check's real comparison logic (check_helm / check_live /
    check_external / check_day7_identities / check_leaks / main) against
    a fake cluster: an all-pass baseline and single-field mutations, each
    of which must produce its specific failure;
  - each leak fact printed exactly once (the live run printed two twice);
  - gateway_is_current() itself (stale generation, False condition, API
    error, timeout, bad JSON);
  - verify_stable() composition: any single failing sub-check fails it;
  - a promotion whose Helm upgrade fails after a clean gate is recorded
    and returns False, for every strategy;
  - Canary Phase A -> Phase B happy path, Pod churn during sampling, and a
    failed Phase A restoration stopping Phase B;
  - Blue/Green "Blue kept warm" negative;
  - PATH_PLAN order independence;
  - version_check Day 7 negatives that had no dedicated test.

kube.run is blocked (or faked) everywhere; nothing contacts a cluster.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import day7_blue_green
import day7_canary
import day7_build
import day7_final_check as fc
import day7_running_images
import day7_recreate
import day7_strategy as d7
import final_state_check
import kube
from day7_build_fixture import SAMPLE_BUILD, pinned_build
from test_day7_orchestration import BASELINE as STRATEGY_BASELINE, FakeCluster, STABLE_MSG, _Base, _pod
from test_version_check import _day7, _DAY6_MAKEFILE_TEXT, _failed_names

STABLE_MANIFEST = """---
apiVersion: v1
kind: Service
metadata:
  name: maops-gateway
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: maops-gateway-route
spec:
  rules:
    - backendRefs:
        - name: maops-gateway
          port: 8080
"""


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def _baseline():
    return {
        "helm": {"revision": 1, "manifest_sha256": d7.sha256_text(STABLE_MANIFEST)},
        "route": {"uid": "route-1"},
        "workloads": {
            "maops-gateway": {"uid": "gw-uid", "generation": 1},
            "maops-app": {"uid": "app-uid", "generation": 1},
            "maops-state": {"uid": "state-uid", "generation": 1},
        },
        "services": {"maops-gateway": "svc-gw", "maops-app": "svc-app", "maops-state": "svc-state"},
        "pdbs": {"maops-gateway-pdb": "pdb-gw", "maops-app-pdb": "pdb-app"},
        "stable_message": STABLE_MSG,
        "storage": {"namespace_uid": "ns-uid", "pvc_uid": "pvc-uid", "pv_name": "pv-1", "pv_uid": "pv-uid"},
        "build": SAMPLE_BUILD.record(),
    }


class FinalCluster:
    """A healthy restored cluster matching _baseline(); `mutate` changes
    exactly one observed fact."""

    def __init__(self, test: unittest.TestCase, **mutate):
        self.m = mutate
        stable_values = d7.expected_release_values("stable", SAMPLE_BUILD)
        all_values = {"candidate": {"enabled": False}, "routing": {"mode": "stable"}}
        all_values.update(mutate.get("all_values", {}))
        objects = {
            ("deployment", "maops-gateway"): {"metadata": {"uid": "gw-uid", "generation": 1}, "spec": {"replicas": 3}, "status": {"readyReplicas": 3}},
            ("deployment", "maops-app"): {"metadata": {"uid": "app-uid", "generation": 1}, "spec": {"replicas": 3}, "status": {"readyReplicas": 3}},
            ("statefulset", "maops-state"): {"metadata": {"uid": "state-uid", "generation": 1}, "spec": {"replicas": 1}, "status": {"readyReplicas": 1}},
            ("service", "maops-gateway"): {"metadata": {"uid": "svc-gw"}},
            ("service", "maops-app"): {"metadata": {"uid": "svc-app"}},
            ("service", "maops-state"): {"metadata": {"uid": "svc-state"}},
            ("pdb", "maops-gateway-pdb"): {"metadata": {"uid": "pdb-gw"}},
            ("pdb", "maops-app-pdb"): {"metadata": {"uid": "pdb-app"}},
            ("namespace", kube.NAMESPACE): {"metadata": {"uid": "ns-uid"}},
            ("pvc", "data-maops-state-0"): {"metadata": {"uid": "pvc-uid"}, "spec": {"volumeName": "pv-1"}},
            ("pv", "pv-1"): {"metadata": {"uid": "pv-uid"}},
        }
        for key, patch in mutate.get("objects", {}).items():
            objects[key] = patch
        self.objects = objects
        stable_route = d7.RouteSnapshot(mutate.get("route_uid", "route-1"), 7, ((d7.STABLE_SERVICE, 8080, 1),), True, {"Accepted": ("True", 7), "ResolvedRefs": ("True", 7)})
        sample_msg = mutate.get("external_message", STABLE_MSG)
        patches = {
            "helm_status": lambda: {"version": 13, "info": {"status": "deployed"}},
            "helm_values": lambda all_values=False: all_values_ if all_values else mutate.get("values", stable_values),
            "helm_manifest": lambda: mutate.get("manifest", STABLE_MANIFEST),
            "candidate_leftovers": lambda: mutate.get("leftovers", ([], [])),
            "get_json_or_none": self.get_json_or_none,
            "read_route": lambda: stable_route,
            "gateway_is_current": lambda: mutate.get("gateway", (True, "gateway current")),
            "sample_series": lambda n, path, interval, max_s: [d7.Sample(0.0, path, "ok", 200, {"message": sample_msg, "hostname": "gw-0", "gateway_hostname": "gw-0", "backend_service": d7.APP_EXPECTED_SERVICE_NAME})] * n,
            "external_get": lambda path, host=None, **k: d7.Sample(0.0, path, "http_error", mutate.get("wrong_host_status", 404), None),
            "list_json": lambda r, s=None, namespace=None: [{"metadata": {"name": "gw-0"}}] if s == kube.GATEWAY_LABEL_SELECTOR else mutate.get("validation_pods", []),
        }
        all_values_ = all_values
        for name, value in patches.items():
            p = mock.patch.object(d7, name, value)
            p.start()
            test.addCleanup(p.stop)
        for p in (mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", side_effect=self.kube_run),
                  mock.patch.object(final_state_check, "check_no_leaked_networkpolicy_probe_pods", self.fake_netpol_leak),
                  mock.patch.object(final_state_check, "check_no_leaked_mesh_probe_namespace", self.fake_mesh_leak),
                  mock.patch.object(fc.subprocess, "run", return_value=subprocess.CompletedProcess(["ps"], 0, "  PID ARGS\n", ""))):
            p.start()
            test.addCleanup(p.stop)

    def get_json_or_none(self, *args):
        if args[-2:] and args[-1] in (kube.STORAGE_BOOTSTRAP_VERIFY_NAMESPACE, kube.STORAGE_HARDENING_NAMESPACE):
            return self.m.get("scratch_state", "not_found"), None
        kind, name = args[-2], args[-1]
        obj = self.objects.get((kind, name))
        return ("found", obj) if obj is not None else ("not_found", None)

    def kube_run(self, *args, **kwargs):
        if "namespaces" in args and "jsonpath={.items[*].metadata.name}" in args:
            names = "maops-platform maops-ingress maops-day7-validation istio-system " + " ".join(self.m.get("extra_namespaces", []))
            return subprocess.CompletedProcess(["kubectl"], 0, names, "")
        label = next((a for a in args if a.startswith("app.kubernetes.io/instance=")), "")
        found = self.m.get("foreign_objects", {}).get(label.split("=", 1)[-1], "")
        return subprocess.CompletedProcess(["kubectl"], 0, found, "")

    def fake_netpol_leak(self):
        final_state_check.record(True, "no leaked NetworkPolicy probe Pods in 'maops-platform' - confirmed absent")

    def fake_mesh_leak(self):
        final_state_check.record(True, "no leaked temporary mesh-probe namespace - confirmed absent")


def _run_all(test, **mutate):
    FinalCluster(test, **mutate)
    rec = d7.Recorder()
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        base = _baseline()
        fc.check_helm(rec, base)
        fc.check_live(rec, base)
        fc.check_external(rec, base)
        fc.check_leaks(rec)
        fc.check_day7_identities(rec)
    return rec, out.getvalue()


class FinalCheckLogicTests(unittest.TestCase):
    def test_restored_cluster_passes_every_check(self):
        rec, _ = _run_all(self)
        self.assertEqual(rec.failures(), [])
        self.assertGreaterEqual(len(rec.results), 35)

    def _fails(self, needle, **mutate):
        rec, _ = _run_all(self, **mutate)
        failures = rec.failures()
        self.assertTrue(any(needle in m for m in failures), f"expected a failure containing {needle!r}, got {failures}")

    def test_each_single_mutation_fails_its_specific_check(self):
        other_manifest = STABLE_MANIFEST.replace("port: 8080", "port: 8080\n          weight: 90")
        cases = [
            ("manifest sha256 equals the pre-experiment baseline", dict(manifest=other_manifest)),
            ("no candidate-only object", dict(manifest=STABLE_MANIFEST + "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: maops-gateway-candidate\n")),
            ("helm-values/day7/stable.yaml", dict(values={"candidate": {"enabled": True}})),
            # DAY7 image contract: a release still on the mutable tag (no
            # build overlay) is not the run's stable state.
            ("the run's build", dict(values=d7.load_stage_values("stable"))),
            ("effective values", dict(all_values={"routing": {"mode": "weighted", "stableWeight": 90}})),
            ("maops-gateway UID and generation unchanged", dict(objects={("deployment", "maops-gateway"): {"metadata": {"uid": "NEW", "generation": 1}, "spec": {"replicas": 3}, "status": {"readyReplicas": 3}}})),
            ("maops-app UID and generation unchanged", dict(objects={("deployment", "maops-app"): {"metadata": {"uid": "app-uid", "generation": 2}, "spec": {"replicas": 3}, "status": {"readyReplicas": 3}}})),
            ("maops-state ready", dict(objects={("statefulset", "maops-state"): {"metadata": {"uid": "state-uid", "generation": 1}, "spec": {"replicas": 1}, "status": {"readyReplicas": 0}}})),
            ("Service maops-app UID unchanged", dict(objects={("service", "maops-app"): {"metadata": {"uid": "recreated"}}})),
            ("PDB maops-gateway-pdb UID unchanged", dict(objects={("pdb", "maops-gateway-pdb"): None})),
            ("baseline UID kept", dict(route_uid="route-2")),
            ("Gateway Programmed", dict(gateway=(False, "Gateway Programmed='False'"))),
            ("namespace UID unchanged", dict(objects={("namespace", kube.NAMESPACE): {"metadata": {"uid": "other"}}})),
            ("state PVC UID and bound PV unchanged", dict(objects={("pvc", "data-maops-state-0"): {"metadata": {"uid": "pvc-uid"}, "spec": {"volumeName": "pv-2"}}})),
            ("state PV UID unchanged", dict(objects={("pv", "pv-1"): {"metadata": {"uid": "pv-new"}}})),
            ("no candidate-only object remains in the cluster", dict(leftovers=(["pods/maops-gateway-candidate-x"], []))),
            ("external / answered only by stable Pods", dict(external_message="Hello from the candidate")),
            ("wrong Host receives a definite no-route 404", dict(wrong_host_status=200)),
            ("no Pod left in the Day 7 validation namespace", dict(validation_pods=[{"metadata": {"name": "netpol-check-abc"}}])),
            ("storage scratch namespace", dict(scratch_state="found")),
            ("no namespace named for an earlier day", dict(extra_namespaces=["maops-day6-validation"])),
            ("no object labelled app.kubernetes.io/instance=maops-kubernetes-platform-day6", dict(foreign_objects={"maops-kubernetes-platform-day6": "namespace/maops-ingress\n"})),
        ]
        for needle, mutate in cases:
            with self.subTest(needle=needle):
                self._fails(needle, **mutate)

    def test_leak_facts_are_printed_exactly_once(self):
        _, output = _run_all(self)
        for line in ("no leaked NetworkPolicy probe Pods", "no leaked temporary mesh-probe namespace"):
            self.assertEqual(output.count(line), 1, output)

    def _main(self, **mutate):
        FinalCluster(self, **mutate)
        with mock.patch.object(d7, "require_day7_profile"), mock.patch.object(kube, "verify_context"), mock.patch.object(d7, "load_strategy_baseline", return_value=_baseline()), \
                mock.patch.object(day7_running_images, "wait_for_running_images", side_effect=lambda rec, build, **k: rec.record(mutate.get("images_ok", True), f"running images: build {build.build_id}")):
            return _quiet(fc.main)

    def test_main_exit_codes(self):
        self.assertEqual(self._main(), 0)
        self.assertEqual(self._main(route_uid="route-2"), 1)

    def test_main_fails_when_running_images_are_not_the_run_build(self):
        self.assertEqual(self._main(images_ok=False), 1)

    def test_main_refuses_without_a_trusted_baseline(self):
        FinalCluster(self)
        with mock.patch.object(d7, "require_day7_profile"), mock.patch.object(kube, "verify_context"), mock.patch.object(d7, "load_strategy_baseline", side_effect=RuntimeError("missing - never recaptured")):
            self.assertEqual(_quiet(fc.main), 1)


def _gw(generation=2, accepted=("True", 2), programmed=("True", 2)):
    conds = [{"type": "Accepted", "status": accepted[0], "observedGeneration": accepted[1]}, {"type": "Programmed", "status": programmed[0], "observedGeneration": programmed[1]}]
    return json.dumps({"metadata": {"generation": generation}, "status": {"conditions": conds}})


class GatewayIsCurrentTests(unittest.TestCase):
    def test_refuses_outside_the_day7_profile_before_any_kubectl_call(self):
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch.object(kube, "run") as run:
            with self.assertRaises(RuntimeError):
                d7.gateway_is_current()
        run.assert_not_called()

    def _run(self, **kw):
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", **kw) as run:
            result = d7.gateway_is_current()
        return result, run

    def test_current_gateway(self):
        (ok, detail), run = self._run(return_value=subprocess.CompletedProcess(["kubectl"], 0, _gw(), ""))
        self.assertTrue(ok, detail)
        self.assertEqual(run.call_args.args[:5], ("-n", kube.INGRESS_NAMESPACE, "get", "gateway", kube.GATEWAY_API_GATEWAY_NAME))

    def test_every_failure_mode(self):
        cases = [
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 0, _gw(accepted=("True", 1)), "")), "observedGeneration=1"),
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 0, _gw(programmed=("True", 1)), "")), "Programmed"),
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 0, _gw(accepted=("False", 2)), "")), "Accepted='False'"),
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 0, json.dumps({"metadata": {"generation": 2}}), "")), "Accepted=None"),
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 1, "", "gateways.gateway.networking.k8s.io not found")), "not found"),
            (dict(side_effect=subprocess.TimeoutExpired("kubectl", 15)), "timed out"),
            (dict(return_value=subprocess.CompletedProcess(["kubectl"], 0, "garbage", "")), "unparseable"),
        ]
        for kw, needle in cases:
            with self.subTest(needle=needle):
                (ok, detail), _ = self._run(**kw)
                self.assertFalse(ok)
                self.assertIn(needle, detail)


class VerifyStableCompositionTests(unittest.TestCase):
    SUBCHECKS = ("helm_values", "wait_candidate_absent", "wait_route", "gateway_is_current", "wait_external", "running_images", "active_build")

    def _verify(self, failing=None):
        good = {
            "helm_values": lambda all_values=False: d7.expected_release_values("stable", SAMPLE_BUILD),
            "wait_candidate_absent": lambda rec, label, timeout=None: rec.record(True, "absent"),
            "wait_route": lambda rec, expected, label, timeout=None: d7.RouteSnapshot("r", 1, expected, True, {}),
            "gateway_is_current": lambda: (True, "gateway ok"),
            "wait_external": lambda *a, **k: True,
        }
        bad = {
            "helm_values": lambda all_values=False: {"candidate": {"enabled": True}},
            "wait_candidate_absent": lambda rec, label, timeout=None: rec.record(False, "leftover"),
            "wait_route": lambda rec, expected, label, timeout=None: None,
            "gateway_is_current": lambda: (False, "gateway stale"),
            "wait_external": lambda *a, **k: False,
        }
        good["running_images"] = lambda rec, build, **k: rec.record(True, "images ok")
        bad["running_images"] = lambda rec, build, **k: rec.record(False, "stable Pod runs the previous build")
        good["active_build"] = lambda: SAMPLE_BUILD

        def _no_build():
            raise day7_build.BuildError("no current.json")
        bad["active_build"] = _no_build
        patches = {name: (bad[name] if name == failing else good[name]) for name in self.SUBCHECKS if name != "running_images"}
        images = bad["running_images"] if failing == "running_images" else good["running_images"]
        # stable_identity() is evaluated eagerly as wait_external's argument
        # and reads Pods; stub it too (and block kubectl outright) so this
        # unit test can never reach a cluster.
        patches["stable_identity"] = lambda msg: d7.Identity(msg, "", frozenset({"gw-0"}), frozenset())
        with mock.patch.multiple(d7, **patches), mock.patch.object(day7_running_images, "wait_for_running_images", side_effect=images), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")):
            return _quiet(d7.verify_stable, d7.Recorder(), STABLE_MSG, "t")

    def test_all_passing(self):
        self.assertTrue(self._verify())

    def test_any_single_failure_fails_the_whole_verification(self):
        for name in self.SUBCHECKS:
            with self.subTest(failing=name):
                self.assertFalse(self._verify(failing=name))


class PromotionHelmFailureTests(_Base):
    def test_every_strategy_records_and_stops_on_a_failed_promotion_upgrade(self):
        for module, stage in ((day7_blue_green, "blue-green-cutover"), (day7_canary, "canary-90-10"), (day7_recreate, "recreate-serving")):
            with self.subTest(module=module.__name__):
                cluster = FakeCluster(self)
                original = cluster.apply_stage

                def apply(rec, s, label=None, original=original, stage=stage):
                    ok = original(rec, s, label)
                    return False if s == stage else ok

                with mock.patch.object(d7, "apply_stage", apply), mock.patch.object(day7_recreate, "run_observed_upgrade", side_effect=AssertionError("must not run")):
                    rec = d7.Recorder()
                    self.assertFalse(_quiet(module.body, rec, STRATEGY_BASELINE))
                self.assertIn(stage, cluster.applied)
                self.assertTrue(any("failed" in m.lower() for m in rec.failures()), rec.failures())


class CanaryPhaseTests(_Base):
    def _cluster(self):
        cluster = FakeCluster(self)
        refusal = (False, [(False, "candidate Deployment status.readyReplicas=None (expected 2)"), (False, "cand-u0: Ready condition True")])
        gate = mock.patch.object(d7, "run_gate", lambda route, replicas: refusal if cluster.stage == "candidate-unready" else (True, [(True, "gate ok")]))
        gate.start()
        self.addCleanup(gate.stop)
        for p in (mock.patch.object(day7_canary, "UNREADY_HOLD_SECONDS", 0), mock.patch.object(day7_canary.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        return cluster

    def test_full_happy_path_phase_a_then_refused_phase_b(self):
        cluster = self._cluster()
        rec = d7.Recorder()
        self.assertTrue(_quiet(day7_canary.body, rec, STRATEGY_BASELINE), rec.failures())
        self.assertEqual(cluster.applied, ["green-prepared", "canary-90-10", "candidate-unready"])
        self.assertEqual(cluster.path_calls, 1, "path checks ran for the Phase A promotion only")
        self.assertTrue(any("REFUSED the unready candidate" in m for ok, m in rec.results if ok))

    def test_pod_churn_during_sampling_fails(self):
        cluster = self._cluster()
        original = cluster.list_json
        calls = {"n": 0}

        def churny(resource, selector=None, namespace=None):
            items = original(resource, selector, namespace)
            if resource == "pods" and selector == kube.GATEWAY_LABEL_SELECTOR and cluster.stage == "canary-90-10":
                calls["n"] += 1
                if calls["n"] > 1:
                    items = [_pod("gw-replaced", comp="gateway")] + items[1:]
            return items

        with mock.patch.object(d7, "list_json", churny):
            rec = d7.Recorder()
            _quiet(day7_canary.phase_a, rec, STRATEGY_BASELINE)
        self.assertTrue(any("Pod churn" in m for m in rec.failures()), rec.failures())

    def test_failed_phase_a_restoration_stops_phase_b(self):
        cluster = self._cluster()
        with mock.patch.object(d7, "restore_stable", lambda *a, **k: False):
            rec = d7.Recorder()
            self.assertFalse(_quiet(day7_canary.body, rec, STRATEGY_BASELINE))
        self.assertNotIn("candidate-unready", cluster.applied)
        self.assertTrue(any("negative scenario will NOT start" in m for m in rec.failures()))


class BlueKeptWarmTests(_Base):
    def test_degraded_blue_during_cutover_fails(self):
        cluster = FakeCluster(self)
        original = cluster.list_json

        def degraded(resource, selector=None, namespace=None):
            items = original(resource, selector, namespace)
            if resource == "pods" and selector == kube.GATEWAY_LABEL_SELECTOR and cluster.stage == "blue-green-cutover":
                items = items[:2] + [_pod("gw-2", ready=False, comp="gateway")]
            return items

        with mock.patch.object(d7, "list_json", degraded):
            rec = d7.Recorder()
            self.assertFalse(_quiet(day7_blue_green.body, rec, STRATEGY_BASELINE))
        self.assertTrue(any("kept warm" in m for m in rec.failures()), rec.failures())


class PathPlanOrderTests(unittest.TestCase):
    IPS = {"maops-app": "10.96.0.10", "maops-state": "10.96.0.20", d7.CANDIDATE_SERVICE: "10.96.0.30", d7.STABLE_SERVICE: "10.96.0.40"}

    def _run(self, plan, candidate_control_ok=True):
        def probe(pod, container, host):
            allowed = {("cand-0", "maops-app"), ("app-0", "maops-state")}
            if (pod, host) == ("cand-0", "maops-app") and not candidate_control_ok:
                return d7.ProbeResult("read_timeout", "4", self.IPS[host])
            kind = "status" if (pod, host) in allowed else "reset"
            return d7.ProbeResult(kind, "200" if kind == "status" else "request", self.IPS[host])

        pods = lambda r, s=None, namespace=None: [_pod("cand-0")] if s and "gateway-candidate" in s else [_pod("app-0", comp="app")]  # noqa: E731
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")), \
                mock.patch.object(d7, "PATH_PLAN", plan), mock.patch.object(d7, "list_json", pods), mock.patch.object(d7, "exec_probe", probe), \
                mock.patch.object(d7, "read_destination", lambda svc: d7.Destination(svc, self.IPS[svc], 1)):
            rec = d7.Recorder()
            ok = _quiet(d7.check_candidate_mesh_paths, rec, "t")
        return ok, rec

    def test_reversed_plan_gives_the_same_verdicts(self):
        for plan in (d7.PATH_PLAN, tuple(reversed(d7.PATH_PLAN))):
            with self.subTest(order="reversed" if plan is not d7.PATH_PLAN else "declared"):
                ok, rec = self._run(plan)
                self.assertTrue(ok, rec.failures())

    def test_failed_control_invalidates_negatives_whatever_the_order(self):
        for plan in (d7.PATH_PLAN, tuple(reversed(d7.PATH_PLAN))):
            ok, rec = self._run(plan, candidate_control_ok=False)
            self.assertFalse(ok)
            self.assertTrue(any("candidate -> state" in m and "positive control" in m for m in rec.failures()))


class LeafProfileGuardTests(unittest.TestCase):
    """Security re-review: every Day 7 leaf that can reach a cluster must
    refuse by itself outside the Day 7 profile - never rely only on
    main()'s guard."""

    def test_final_check_identity_scan_refuses_outside_day7(self):
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch.object(kube, "run") as run:
            with self.assertRaises(RuntimeError):
                _quiet(fc.check_day7_identities, d7.Recorder())
        run.assert_not_called()

    def test_nodes_ready_read_refuses_outside_day7(self):
        import day7_nodes_ready
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch.object(kube, "run") as run:
            with self.assertRaises(day7_nodes_ready.NodeReadinessError):
                day7_nodes_ready.read_nodes()
        run.assert_not_called()

    def test_observed_upgrade_refuses_before_starting_prober_or_helm(self):
        popen = mock.Mock()
        with mock.patch.object(kube, "PROFILE", "day6"), mock.patch.object(d7, "Prober") as prober:
            with self.assertRaises(RuntimeError):
                day7_recreate.run_observed_upgrade(["helm", "upgrade"], frozenset(), 2, popen=popen)
        popen.assert_not_called()
        prober.assert_not_called()
        self.assertNotIn("recreate-changed", d7.SUBMITTED_STAGES)


class VersionCheckDay7NegativeTests(unittest.TestCase):
    def test_app_version_mismatch(self):
        self.assertIn("day7.chart_appVersion_matches_target", _failed_names(_day7(chart_yaml={"version": "0.7.0", "appVersion": "0.6.0"})))

    def test_kind_name_and_day6_port(self):
        from test_version_check import _kind
        self.assertIn("day7.kind_config.name", _failed_names(_day7(kind_day7=_kind("maops-k8s-day6", 18081))))
        self.assertIn("day7.kind_config.day6_host_port_preserved", _failed_names(_day7(kind_day6=_kind("maops-k8s-day6", 18081))))

    def test_each_pinned_infra_version(self):
        from test_version_check import _DAY7_MAKEFILE
        for label, version in (("Cilium", "1.20.1"), ("Gateway API CRDs", "v1.6.0"), ("Istio", "1.31.0")):
            with self.subTest(label=label):
                self.assertIn(f"day7.pinned_infra[{label}]", _failed_names(_day7(makefile_text=_DAY7_MAKEFILE.replace(version, "0.0.0"))))


if __name__ == "__main__":
    unittest.main()

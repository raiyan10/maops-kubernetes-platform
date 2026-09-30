"""
DAY7 orchestration-level regression tests: each strategy's real body()
is driven end to end against a fake cluster (every day7_strategy live
call replaced, `kube.run` blocked), proving that

  - each strategy REFUSES its traffic-moving promotion when an otherwise
    healthy candidate fails (or cannot complete) the candidate path
    checks - no promotion stage is ever submitted;
  - every recorded precondition failure blocks the NEXT mutation
    (Blue/Green route identity / prepared-stage traffic; Canary
    post-promotion endpoints stop the sample; Recreate stable strategy,
    old-Pod/endpoint identity, ReplicaSet, Helm revision and PDB
    coverage stop recreate-changed);
  - Canary's deliberate unready-candidate scenario is refused for
    readiness, without any path probe;
  - the Recreate Helm child is owned: on Popen failure, observer
    exceptions, stalls/deadline expiry, a child ignoring SIGTERM, and
    Ctrl-C (including during the wait) it is terminated/killed and
    REAPED - and the prober stopped - before restoration begins, and its
    output never goes to an undrained pipe;
  - the PDB coverage check evaluates every candidate Pod with full
    selector semantics and fails closed.
"""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from day7_build_fixture import SAMPLE_BUILD, pinned_build, running_images_pass
import day7_blue_green
import day7_canary
import day7_recreate
import day7_strategy as d7
import kube

STABLE_MSG = "Hello from the MAOps Kubernetes Gateway (Day 7 stable)"
CAND_MSG = d7.load_stage_values("green-prepared")["candidate"]["config"]["appMessage"]
BASELINE = {"stable_message": STABLE_MSG, "build": SAMPLE_BUILD.record()}


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


_FAKE_IPS: dict[str, str] = {}


def _fake_ip(name: str) -> str:
    """Unique, deterministic fake Pod IP per name. (An earlier
    hash(name)-based IP collided for ~3% of PYTHONHASHSEED values, making
    'disjoint endpoints' checks fail at random.)"""
    return _FAKE_IPS.setdefault(name, f"10.0.{len(_FAKE_IPS) // 250}.{len(_FAKE_IPS) % 250 + 1}")


def _pod(name, uid=None, ready=True, started=True, h="h1", comp="gateway-candidate"):
    return {
        "metadata": {"name": name, "uid": uid or f"uid-{name}", "labels": {"app.kubernetes.io/component": comp, "pod-template-hash": h}},
        "status": {"phase": "Running", "podIP": _fake_ip(name), "containerStatuses": [{"started": started}],
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def _snap(backends, generation, uid="route-1"):
    return d7.RouteSnapshot(uid, generation, tuple(sorted(backends)), True, {"Accepted": ("True", generation), "ResolvedRefs": ("True", generation)})


class FakeCluster:
    """Minimal stand-in for the live cluster, driven by the stages that
    are actually applied. Everything the strategy bodies call through
    day7_strategy is patched onto this object."""

    def __init__(self, test: unittest.TestCase, **overrides):
        self.applied: list[str] = []
        self.stage = "stable"
        self.generation = 5
        self.path_calls = 0
        self.path_result = overrides.pop("path_result", True)
        self.gate_result = overrides.pop("gate_result", (True, [(True, "gate ok")]))
        self.overrides = overrides
        self.stable_pods = [_pod(f"gw-{i}", comp="gateway") for i in range(3)]
        self.cand_pods = [_pod(f"cand-{i}") for i in range(2)]
        self.sampled = 0
        patches = {
            "helm_values": lambda all_values=False: d7.expected_release_values(self.stage, SAMPLE_BUILD),
            "candidate_leftovers": lambda: ([], []),
            "apply_stage": self.apply_stage,
            "wait_route": self.wait_route,
            "read_route": self.read_route,
            "wait_external": overrides.get("wait_external", lambda *a, **k: True),
            "run_gate": lambda route, replicas: self.gate_result,
            "check_candidate_mesh_paths": self.paths,
            "list_json": self.list_json,
            "read_endpoints": self.read_endpoints,
            "helm_revision": overrides.get("helm_revision", lambda: 10 + len(self.applied)),
            "sample_series": self.sample_series,
            "restore_stable": lambda *a, **k: True,
            "gateway_is_current": lambda: (True, "gateway ok"),
            "get_json_or_none": self.get_json_or_none,
        }
        for name, value in patches.items():
            p = mock.patch.object(d7, name, value)
            p.start()
            test.addCleanup(p.stop)
        for p in (mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(kube, "run", side_effect=AssertionError("kubectl must never run in unit tests")), pinned_build(), running_images_pass()):
            p.start()
            test.addCleanup(p.stop)

    def apply_stage(self, rec, stage, label=None):
        self.applied.append(stage)
        d7.SUBMITTED_STAGES.append(stage)
        self.stage = stage
        if stage in ("blue-green-cutover", "canary-90-10", "recreate-serving"):
            self.generation += 1
        if stage == "candidate-unready":
            self.cand_pods = [_pod(f"cand-u{i}", ready=False) for i in range(2)]
        return True

    def paths(self, rec, label):
        self.path_calls += 1
        result = self.path_result
        if isinstance(result, BaseException):
            raise result
        return rec.record(result, f"{label}: fake path checks {'passed' if result else 'FAILED'}")

    def backends(self):
        return d7.expected_backends(d7.load_stage_values(self.stage))

    def wait_route(self, rec, expected, label, timeout=None):
        return _snap(expected, self.generation)

    def read_route(self):
        fn = self.overrides.get("read_route")
        return fn() if fn else _snap(self.backends(), self.generation)

    def list_json(self, resource, selector=None, namespace=None):
        if resource == "pods" and selector and "gateway-candidate" in selector:
            return list(self.cand_pods)
        if resource == "pods" and selector == kube.GATEWAY_LABEL_SELECTOR:
            return list(self.stable_pods)
        if resource == "pods":
            return [_pod("app-0", comp="app")]
        if resource == "pdb":
            return self.overrides.get("pdbs", [])
        if resource == "replicasets":
            return self.overrides.get("replicasets", [{"metadata": {"uid": "rs-1", "ownerReferences": [{"uid": "dep-1"}]}, "spec": {"replicas": 2}}])
        return []

    def read_endpoints(self, service):
        fn = self.overrides.get("read_endpoints")
        if fn:
            return fn(service)
        pods = self.stable_pods if service == d7.STABLE_SERVICE else self.cand_pods
        ready = [p for p in pods if d7.pod_is_ready(p)]
        return d7.EndpointView(frozenset(p["status"]["podIP"] for p in ready), frozenset(p["metadata"]["name"] for p in ready), frozenset(p["metadata"]["name"] for p in pods))

    def sample_series(self, n, path, interval, max_seconds):
        self.sampled += 1
        out = []
        for i in range(n):
            pod = self.cand_pods[0] if (i % 10 == 0 and self.stage == "canary-90-10") else self.stable_pods[i % 3]
            payload = {"gateway_hostname": pod["metadata"]["name"], "backend_service": d7.APP_EXPECTED_SERVICE_NAME, "message": STABLE_MSG, "hostname": pod["metadata"]["name"]}
            out.append(d7.Sample(float(i), path, "ok", 200, payload))
        return out

    def get_json_or_none(self, *args):
        if "deployment" in args and d7.STABLE_DEPLOYMENT in args:
            return "found", {"spec": {"strategy": {"type": self.overrides.get("stable_strategy", "RollingUpdate")}}}
        if "deployment" in args and d7.CANDIDATE_DEPLOYMENT in args:
            return "found", {"metadata": {"uid": "dep-1", "generation": 3, "annotations": {"deployment.kubernetes.io/revision": "1"}},
                             "spec": {"strategy": {"type": "Recreate"}, "template": {"metadata": {"annotations": {"checksum/config": "abc"}}}},
                             "status": {"observedGeneration": 3}}
        return "not_found", None


class _Base(unittest.TestCase):
    def setUp(self):
        saved_stages, saved_children = list(d7.SUBMITTED_STAGES), list(d7.OWNED_CHILDREN)
        d7.SUBMITTED_STAGES.clear()
        d7.OWNED_CHILDREN.clear()
        self.addCleanup(lambda: (d7.SUBMITTED_STAGES.__setitem__(slice(None), saved_stages), d7.OWNED_CHILDREN.__setitem__(slice(None), saved_children)))


# --------------------------------------------------------------------------
# Path checks are enforced for every promotion
# --------------------------------------------------------------------------


class PathCheckEnforcementTests(_Base):
    def _assert_refused(self, cluster, rec, promotion_stage):
        self.assertNotIn(promotion_stage, cluster.applied)
        self.assertEqual(cluster.path_calls, 1)
        self.assertTrue(any("REFUSED" in m and "path checks" in m for m in rec.failures()), rec.failures())

    def test_blue_green_refuses_cutover_when_paths_fail(self):
        cluster = FakeCluster(self, path_result=False)
        rec = d7.Recorder()
        self.assertFalse(_quiet(day7_blue_green.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])
        self._assert_refused(cluster, rec, "blue-green-cutover")

    def test_canary_refuses_weighted_route_when_paths_fail(self):
        cluster = FakeCluster(self, path_result=False)
        rec = d7.Recorder()
        self.assertFalse(_quiet(day7_canary.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])
        self._assert_refused(cluster, rec, "canary-90-10")
        self.assertEqual(cluster.sampled, 0)

    def test_recreate_refuses_serving_when_paths_fail(self):
        cluster = FakeCluster(self, path_result=False)
        rec = d7.Recorder()
        with mock.patch.object(day7_recreate, "run_observed_upgrade", side_effect=AssertionError("must not run")) as upgrade:
            self.assertFalse(_quiet(day7_recreate.body, rec, BASELINE))
        upgrade.assert_not_called()
        self.assertEqual(cluster.applied, ["recreate-prepared"])
        self._assert_refused(cluster, rec, "recreate-serving")

    def test_inconclusive_path_observation_refuses_every_strategy(self):
        for module, stage in ((day7_blue_green, "blue-green-cutover"), (day7_canary, "canary-90-10"), (day7_recreate, "recreate-serving")):
            with self.subTest(module=module.__name__):
                cluster = FakeCluster(self, path_result=subprocess.TimeoutExpired("kubectl exec", 34))
                rec = d7.Recorder()
                self.assertFalse(_quiet(module.body, rec, BASELINE))
                self.assertNotIn(stage, cluster.applied)

    def test_healthy_paths_allow_blue_green_cutover(self):
        cluster = FakeCluster(self)
        _quiet(day7_blue_green.body, d7.Recorder(), BASELINE)
        self.assertEqual(cluster.applied, ["green-prepared", "blue-green-cutover"])


# --------------------------------------------------------------------------
# Recorded precondition failures block the next mutation
# --------------------------------------------------------------------------


class BlueGreenPreconditionTests(_Base):
    def test_unverifiable_route_identity_stops_before_cutover(self):
        routes = iter([_snap(d7.expected_backends(d7.load_stage_values("stable")), 5, uid="route-OLD")])
        cluster = FakeCluster(self, read_route=lambda: next(routes))
        rec = d7.Recorder()
        self.assertFalse(_quiet(day7_blue_green.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])
        self.assertEqual(cluster.path_calls, 0)

    def test_missing_route_snapshot_stops_before_cutover(self):
        cluster = FakeCluster(self, read_route=lambda: None)
        self.assertFalse(_quiet(day7_blue_green.body, d7.Recorder(), BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])

    def test_prepared_stage_stable_traffic_failure_stops_before_cutover(self):
        results = iter([True, False])  # entry gate passes, prepared-stage check fails
        cluster = FakeCluster(self, wait_external=lambda *a, **k: next(results))
        rec = d7.Recorder()
        self.assertFalse(_quiet(day7_blue_green.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared"])
        self.assertEqual(cluster.path_calls, 0)
        self.assertTrue(any("cutover NOT attempted" in m for m in rec.failures()))


class CanaryPreconditionTests(_Base):
    def test_unhealthy_post_promotion_endpoints_stop_before_sampling(self):
        def endpoints(service):
            return d7.EndpointView(frozenset({"10.0.0.1"}), frozenset(), frozenset())
        cluster = FakeCluster(self, read_endpoints=endpoints)
        rec = d7.Recorder()
        self.assertFalse(_quiet(day7_canary.body, rec, BASELINE))
        self.assertEqual(cluster.applied, ["green-prepared", "canary-90-10"])
        self.assertEqual(cluster.sampled, 0, "the 200-request sample must not run on unhealthy endpoints")
        self.assertTrue(any("stopping before the external sample" in m for m in rec.failures()))

    def test_unready_candidate_is_refused_for_readiness_without_path_probes(self):
        cluster = FakeCluster(self)
        cluster.stage = "green-prepared"
        cluster.gate_result = (False, [(False, "cand-u0: Ready condition True"), (False, "candidate Deployment status.readyReplicas=0 (expected 2)")])
        d7.SUBMITTED_STAGES.append("candidate-unready")
        cluster.apply_stage(d7.Recorder(), "candidate-unready")
        cluster.applied.clear()
        rec = d7.Recorder()
        with mock.patch.object(day7_canary, "UNREADY_HOLD_SECONDS", 0), mock.patch.object(day7_canary.time, "sleep"):
            # Phase B from the point the unready stage is applied
            with mock.patch.object(d7, "apply_stage", side_effect=lambda r, s, label=None: s == "candidate-unready"):
                ok = _quiet(day7_canary.phase_b, rec, BASELINE)
        self.assertEqual(cluster.path_calls, 0, "no path probe may run against an unready candidate")
        self.assertTrue(any("REFUSED the unready candidate" in m for ok_, m in rec.results if ok_), rec.results)
        self.assertTrue(any("attributable to readiness" in m for ok_, m in rec.results if ok_))
        self.assertTrue(any("before any path probe" in m for ok_, m in rec.results if ok_))
        self.assertTrue(ok, rec.failures())


class RecreatePreconditionTests(_Base):
    def _run(self, **overrides):
        cluster = FakeCluster(self, **overrides)
        rec = d7.Recorder()
        with mock.patch.object(day7_recreate, "run_observed_upgrade", side_effect=AssertionError("recreate-changed must not be submitted")) as upgrade:
            result = _quiet(day7_recreate.body, rec, BASELINE)
        self.assertFalse(result)
        upgrade.assert_not_called()
        return cluster, rec

    def test_stable_strategy_failure_blocks_promotion(self):
        cluster, rec = self._run(stable_strategy="Recreate")
        self.assertEqual(cluster.applied, ["recreate-prepared"])
        self.assertEqual(cluster.path_calls, 0)

    def test_old_pod_endpoint_identity_failure_blocks_upgrade(self):
        def endpoints(service):
            if service == d7.CANDIDATE_SERVICE:
                return d7.EndpointView(frozenset({"10.9.9.9"}), frozenset({"cand-0"}), frozenset({"cand-0"}))
            return d7.EndpointView(frozenset({"a", "b", "c"}), frozenset({"gw-0", "gw-1", "gw-2"}), frozenset({"gw-0", "gw-1", "gw-2"}))
        cluster, rec = self._run(read_endpoints=endpoints)
        self.assertIn("recreate-serving", cluster.applied)
        self.assertTrue(any("recreate-changed NOT submitted" in m for m in rec.failures()))

    def test_unreadable_helm_revision_blocks_upgrade(self):
        cluster, rec = self._run(helm_revision=lambda: None)
        self.assertTrue(any("Helm revision readable" in m for m in rec.failures()))

    def test_pdb_selecting_a_candidate_pod_blocks_upgrade(self):
        pdb = {"metadata": {"name": "too-broad"}, "spec": {"selector": {"matchExpressions": [{"key": "app.kubernetes.io/component", "operator": "In", "values": ["gateway-candidate"]}]}}}
        cluster, rec = self._run(pdbs=[pdb])
        self.assertTrue(any("too-broad" in m for m in rec.failures()))

    def test_multiple_active_replicasets_block_upgrade(self):
        rss = [{"metadata": {"uid": f"rs-{i}", "ownerReferences": [{"uid": "dep-1"}]}, "spec": {"replicas": 1}} for i in range(2)]
        cluster, rec = self._run(replicasets=rss)
        self.assertTrue(any("exactly one active candidate ReplicaSet" in m for m in rec.failures()))


# --------------------------------------------------------------------------
# PDB selector evaluation
# --------------------------------------------------------------------------


class PdbSelectorTests(unittest.TestCase):
    PODS = [_pod("cand-0"), {"metadata": {"name": "cand-1", "labels": {"app.kubernetes.io/component": "gateway-candidate", "track": "b"}}}]

    def test_every_pod_is_evaluated_not_just_the_first(self):
        pdb = {"metadata": {"name": "track-b"}, "spec": {"selector": {"matchLabels": {"track": "b"}}}}
        self.assertEqual(day7_recreate.pdbs_selecting([pdb], self.PODS), (["track-b"], []))

    def test_match_expressions_are_evaluated(self):
        cases = [
            ({"matchExpressions": [{"key": "track", "operator": "Exists"}]}, True),
            ({"matchExpressions": [{"key": "app.kubernetes.io/component", "operator": "NotIn", "values": ["gateway-candidate"]}]}, False),
            ({"matchExpressions": [{"key": "missing", "operator": "DoesNotExist"}]}, True),
            ({"matchLabels": {"app.kubernetes.io/component": "gateway"}}, False),
        ]
        for selector, selects in cases:
            with self.subTest(selector=selector):
                pdb = {"metadata": {"name": "p"}, "spec": {"selector": selector}}
                self.assertEqual(bool(day7_recreate.pdbs_selecting([pdb], self.PODS)[0]), selects)

    def test_empty_or_missing_selector_selects_everything(self):
        for spec in ({"selector": {}}, {}):
            self.assertEqual(day7_recreate.pdbs_selecting([{"metadata": {"name": "all"}, "spec": spec}], self.PODS)[0], ["all"])

    def test_unsupported_or_malformed_selectors_fail_closed(self):
        for selector in ({"matchExpressions": [{"key": "k", "operator": "Gt", "values": ["1"]}]}, {"matchExpressions": [{"key": "k", "operator": "In"}]}, "junk", {"weird": {}}):
            with self.subTest(selector=selector):
                selecting, errors = day7_recreate.pdbs_selecting([{"metadata": {"name": "p"}, "spec": {"selector": selector}}], self.PODS)
                self.assertTrue(errors)

    def test_live_check_fails_closed_on_unreadable_input(self):
        for pdbs, pods in ((None, self.PODS), ([], None), ([], [])):
            with self.subTest(pdbs=pdbs, pods=pods):
                with mock.patch.object(d7, "list_json", side_effect=lambda r, s=None, namespace=None: pdbs if r == "pdb" else pods):
                    rec = d7.Recorder()
                    self.assertFalse(_quiet(day7_recreate.no_pdb_selects_candidate, rec))

    def test_pdb_is_never_called_protection(self):
        self.assertIn("NOT protection", day7_recreate.PDB_NOT_PROTECTION)


# --------------------------------------------------------------------------
# Owned Helm child / prober lifecycle
# --------------------------------------------------------------------------


class FakeChild:
    def __init__(self, exits_after_polls=None, ignores_terminate=False, interrupt_on_wait=False):
        self.pid = 4242
        self.returncode = None
        self.polls = 0
        self.exits_after_polls = exits_after_polls
        self.ignores_terminate = ignores_terminate
        self.interrupt_on_wait = interrupt_on_wait
        self.events: list[str] = []

    def poll(self):
        self.polls += 1
        if self.returncode is None and self.exits_after_polls is not None and self.polls >= self.exits_after_polls:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            self.events.append("reaped")
            return self.returncode
        if self.interrupt_on_wait and "terminate" in self.events and "kill" not in self.events:
            raise KeyboardInterrupt
        if timeout is not None:
            raise subprocess.TimeoutExpired("helm", timeout)
        raise AssertionError("unbounded wait on a live child")

    def terminate(self):
        self.events.append("terminate")
        if not self.ignores_terminate:
            self.returncode = -15

    def kill(self):
        self.events.append("kill")
        self.returncode = -9


class FakeProber:
    instances: list = []

    def __init__(self, **kwargs):
        self.started = self.stopped = False
        self.t0 = 0.0
        FakeProber.instances.append(self)

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True
        return []


class OwnedLifecycleTests(_Base):
    def setUp(self):
        super().setUp()
        FakeProber.instances.clear()
        for p in (mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(d7, "Prober", FakeProber), mock.patch.object(kube, "run", side_effect=AssertionError("no kubectl")),
                  mock.patch.object(d7, "read_endpoints", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.popen_kwargs = {}

    def _popen(self, child):
        def popen(cmd, **kwargs):
            self.popen_kwargs = kwargs
            return child
        return popen

    def _run(self, child_or_popen, list_json=lambda *a, **k: [], deadline=0.05, grace=0.0):
        popen = child_or_popen if callable(child_or_popen) and not isinstance(child_or_popen, FakeChild) else self._popen(child_or_popen)
        with mock.patch.object(d7, "list_json", side_effect=list_json):
            return day7_recreate.run_observed_upgrade(["helm", "upgrade"], frozenset({"old"}), 2, popen=popen, deadline_seconds=deadline, helm_grace_seconds=grace, observe_interval=0.001, post_settle_seconds=0)

    def _assert_clean(self, child):
        self.assertTrue(FakeProber.instances[-1].stopped, "prober must always be stopped")
        self.assertNotIn(child, d7.OWNED_CHILDREN, "child must be unregistered only after being reaped")
        self.assertIsNotNone(child.returncode)
        self.assertEqual(child.events[-1], "reaped")

    def test_output_goes_to_private_files_not_pipes(self):
        child = FakeChild(exits_after_polls=1)
        run = self._run(child)
        for stream in ("stdout", "stderr"):
            self.assertIsNot(self.popen_kwargs[stream], subprocess.PIPE)
            self.assertTrue(hasattr(self.popen_kwargs[stream], "fileno"))
        self.assertEqual(self.popen_kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0)
        self.assertTrue(run.child_outcome.startswith("already exited"))
        self._assert_clean(child)

    def test_stalled_child_is_terminated_and_reaped(self):
        child = FakeChild()
        run = self._run(child)
        self.assertEqual(child.events, ["terminate", "reaped"])
        self.assertTrue(run.child_outcome.startswith("terminated"))
        self._assert_clean(child)

    def test_child_ignoring_sigterm_is_killed_and_reaped(self):
        child = FakeChild(ignores_terminate=True)
        with mock.patch.object(d7, "CHILD_TERMINATE_GRACE_SECONDS", 0.0):
            run = self._run(child)
        self.assertEqual(child.events, ["terminate", "kill", "reaped"])
        self.assertTrue(run.child_outcome.startswith("killed"))
        self._assert_clean(child)

    def test_popen_failure_still_stops_the_prober(self):
        def failing_popen(cmd, **kwargs):
            raise OSError("helm not found")
        with self.assertRaises(OSError):
            self._run(failing_popen)
        self.assertTrue(FakeProber.instances[-1].stopped)
        self.assertEqual(d7.OWNED_CHILDREN, [])
        self.assertIn("recreate-changed", d7.SUBMITTED_STAGES, "restoration must still run a Helm restore")

    def test_observation_exception_reaps_the_running_child(self):
        child = FakeChild()
        def boom(*a, **k):
            raise RuntimeError("API went away")
        with self.assertRaises(RuntimeError):
            self._run(child, list_json=boom)
        self._assert_clean(child)
        self.assertIn("terminate", child.events)

    def test_ctrl_c_during_observation_reaps_the_child(self):
        child = FakeChild()
        def interrupt(*a, **k):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._run(child, list_json=interrupt)
        self._assert_clean(child)

    def test_ctrl_c_while_waiting_for_the_child_kills_and_reaps(self):
        child = FakeChild(ignores_terminate=True, interrupt_on_wait=True)
        with mock.patch.object(d7, "CHILD_TERMINATE_GRACE_SECONDS", 5.0):
            with self.assertRaises(KeyboardInterrupt):
                self._run(child)
        self.assertEqual(child.events, ["terminate", "kill", "reaped"])
        self._assert_clean(child)

    def test_child_is_reaped_before_restoration_begins(self):
        """End to end through run_experiment: a stalled child is gone
        (reaped, unregistered) at the moment the restore hook starts."""
        child = FakeChild()
        seen_at_restore = {}

        def body():
            self._run(child)
            return False

        def restore():
            seen_at_restore["returncode"] = child.returncode
            seen_at_restore["owned"] = list(d7.OWNED_CHILDREN)
            seen_at_restore["events"] = list(child.events)
            return True

        rc = _quiet(d7.run_experiment, d7.Recorder(), "T", body, restore)
        self.assertEqual(rc, 1)
        self.assertIsNotNone(seen_at_restore["returncode"])
        self.assertEqual(seen_at_restore["owned"], [])
        self.assertEqual(seen_at_restore["events"][-1], "reaped")

    def test_restore_hook_reaps_any_leftover_owned_child_first_and_fails(self):
        child = FakeChild()
        d7.OWNED_CHILDREN.append(child)
        order = []
        with mock.patch.object(kube, "PROFILE", "day7"), mock.patch.object(d7, "verify_stable", side_effect=lambda *a: order.append(("verify", child.returncode)) or True):
            rec = d7.Recorder()
            ok = _quiet(d7.restore_or_verify, rec, STABLE_MSG)
        self.assertFalse(ok, "a child still running at restoration time is a lifecycle failure")
        self.assertIsNotNone(order[0][1], "child was reaped before verification/restoration started")
        self.assertEqual(d7.OWNED_CHILDREN, [])

    def test_read_tail_is_bounded(self):
        import tempfile
        with tempfile.TemporaryFile() as f:
            f.write(b"x" * 10000 + b"END")
            self.assertEqual(len(d7.read_tail(f, 100)), 100)
            self.assertTrue(d7.read_tail(f, 100).endswith("END"))


if __name__ == "__main__":
    unittest.main()

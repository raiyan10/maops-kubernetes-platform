"""
DAY8: Docker/Kubernetes-free tests for the Day 8 autoscaling tooling.

Every live entry point must refuse outside the maops-k8s-day7 profile on
its own; the scaling design (one scaler per target, LimitRange fit,
worst-case quota budget) and the add-on safety guards are proven
statically; and each live phase's verdict logic is exercised with
synthetic observations - including the failures it must report
(no scale-out, no scale-in, a recommendation that was never applied,
an application Pod that changed, a queue that never drained).

The in-process `kube` module runs under the DEFAULT (day6) profile here,
which is exactly what makes the guard tests meaningful.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scaling"))

import day7_build
import day8_addons
import day8_common
import day8_image
import day8_objects as o
import day8_preflight
import day8_scaling
import day8_stable
import kube

IMAGE = f"maops-kubernetes-scaling:day8-cfg-{'a' * 64}"


def _py(code: str, **env) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k not in ("MAOPS_CLUSTER_PROFILE", "KUBECONFIG_PATH")}
    full_env.update(env)
    return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(REPO / 'scripts')!r})\n{code}"], capture_output=True, text=True, env=full_env, timeout=60)


class ProfileGuardTests(unittest.TestCase):
    """No Day 8 path may start a kubectl/helm/docker process outside the
    day7 profile - checked with subprocess.run mocked to explode."""

    def setUp(self):
        self.assertEqual(kube.PROFILE, "day6", "tests must run under the default profile")
        patcher = mock.patch("subprocess.run", side_effect=AssertionError("a process was started"))
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_kubectl_helm_and_node_exec_refuse(self):
        for call in (lambda: day8_common.kubectl("get", "pods"), lambda: day8_common.helm("list"), lambda: day8_common.node_exec("maops-k8s-day7-worker", "true")):
            with self.assertRaises(day8_common.Day8Error):
                call()
        self.run_mock.assert_not_called()

    def test_live_entry_points_refuse(self):
        for call in (day8_stable.observe, day8_addons.check, day8_image.verify_nodes, day8_scaling.phase_cleanup, day8_image.load_kind):
            with self.assertRaises(day8_common.Day8Error):
                call()
        self.run_mock.assert_not_called()

    def test_phase_main_and_preflight_fail_closed(self):
        with mock.patch.object(sys, "argv", ["day8_scaling.py", "hpa"]), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(day8_scaling.main(), 1)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(day8_preflight.main(), 1)
        self.run_mock.assert_not_called()



class Day7ProfileTests(unittest.TestCase):
    """Fresh interpreters, so the shared in-process `kube` stays day6."""

    def test_day7_profile_selects_the_day7_cluster(self):
        out = _py("import kube, day8_common; day8_common.require_cluster_profile(); print(kube.CONTEXT)", MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "kind-maops-k8s-day7")

    def test_node_exec_rejects_foreign_node_even_under_day7(self):
        out = _py("import day8_common\ntry:\n    day8_common.node_exec('maops-k8s-day6-worker', 'true')\nexcept day8_common.Day8Error as e:\n    print('refused', e)", MAOPS_CLUSTER_PROFILE="day7")
        self.assertIn("refused", out.stdout, out.stderr)


class BudgetTests(unittest.TestCase):
    def test_budget_is_the_exact_worst_case(self):
        b = o.budget()
        self.assertEqual(b.hard(), {"requests.cpu": "710m", "requests.memory": "544Mi", "limits.cpu": "2000m", "limits.memory": "1088Mi", "pods": "13"})
        self.assertEqual(o.quota_object()["spec"]["hard"], b.hard())

    def test_budget_covers_every_scaler_maximum(self):
        rows = {r[0]: r[1] for r in o.budget().rows}
        self.assertEqual(rows[o.HPA_TARGET], o.HPA_MAX)
        self.assertEqual(rows[o.WORKER], o.KEDA_MAX)
        self.assertEqual(rows[o.VPA_TARGET], o.VPA_REPLICAS_AFTER_INITIAL)
        for support in (o.HPA_LOAD, o.QUEUE, o.PRODUCER, o.QUOTA_PROBE):
            self.assertEqual(rows[support], 1)

    def test_vpa_ceiling_scales_limits_proportionally(self):
        r = o.vpa_ceiling(o.Resources("10m", "32Mi", "50m", "64Mi"))
        self.assertEqual((r.cpu_request, r.memory_request, r.cpu_limit, r.memory_limit), ("40m", "96Mi", "200m", "192Mi"))

    def test_raising_a_scaler_maximum_raises_the_budget(self):
        bigger = tuple(o.PodBudget(w.name, w.max_pods + (1 if w.name == o.HPA_TARGET else 0), w.resources, w.scaler, w.vpa) for w in o.WORKLOADS)
        self.assertEqual(o.budget(bigger).requests_cpu_m - o.budget().requests_cpu_m, 100)

    def test_quantities(self):
        self.assertEqual(o.cpu_millis("2"), 2000)
        self.assertEqual(o.memory_bytes("1Gi"), 1024**3)
        with self.assertRaises(ValueError):
            o.cpu_millis("0.5")
        with self.assertRaises(ValueError):
            o.fmt_memory(1000)


class DesignTests(unittest.TestCase):
    def test_design_is_sound(self):
        self.assertEqual(o.design_problems(), [])

    def test_limitrange_violation_is_reported(self):
        bad = (*o.WORKLOADS, o.PodBudget("too-big", 1, o.Resources("100m", "32Mi", "300m", "64Mi")))
        self.assertTrue(any("too-big" in p and "LimitRange max" in p for p in o.design_problems(bad)))

    def test_vpa_ceiling_beyond_limitrange_is_reported(self):
        bad = tuple(o.PodBudget(w.name, w.max_pods, o.Resources("10m", "32Mi", "100m", "64Mi") if w.vpa else w.resources, w.scaler, w.vpa) for w in o.WORKLOADS)
        self.assertTrue(any(o.VPA_TARGET in p and "worst-case" in p for p in o.design_problems(bad)))


class VpaChangeInvariantTests(unittest.TestCase):
    """Run f837802b: the cold-start recommendation sat at the recommender
    floor 10m/32Mi == minAllowed == the declared requests, so the admitted
    Pod was annotated but unchanged. No valid recommendation may equal both
    declared requests."""

    def vpa_workload(self):
        return next(w for w in o.WORKLOADS if w.vpa)

    def test_no_valid_recommendation_equals_the_declared_requests(self):
        w = self.vpa_workload()
        self.assertEqual(o.vpa_change_problems(w), [])
        floor = o.vpa_floor(w.resources)
        self.assertGreater(o.memory_bytes(floor.memory_request), o.memory_bytes(w.resources.memory_request))
        self.assertEqual((floor.memory_request, floor.memory_limit), ("48Mi", "96Mi"))
        self.assertLessEqual(o.memory_bytes(o.vpa_ceiling(w.resources).memory_limit), o.memory_bytes(o.LIMIT_MAX["memory"]))

    def test_rendered_objects_carry_the_invariant(self):
        vpa = o.vpa_object("Initial")["spec"]["resourcePolicy"]["containerPolicies"][0]
        declared = o.vpa_target_objects(IMAGE)[0]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
        self.assertEqual((vpa["minAllowed"], vpa["maxAllowed"]), ({"cpu": "10m", "memory": "48Mi"}, {"cpu": "40m", "memory": "96Mi"}))
        self.assertEqual(vpa["controlledValues"], "RequestsAndLimits")
        self.assertEqual(declared, {"cpu": "10m", "memory": "32Mi"})
        self.assertGreater(o.memory_bytes(vpa["minAllowed"]["memory"]), o.memory_bytes(declared["memory"]))

    def test_run_f837_minimum_equal_to_declared_is_refused(self):
        w = self.vpa_workload()
        old_min = {"cpu": "10m", "memory": "32Mi"}
        problems = o.vpa_change_problems(w, min_allowed=old_min)
        self.assertTrue(problems and "can equal both declared requests" in problems[0], problems)
        with mock.patch.dict(o.VPA_MIN_ALLOWED, old_min):
            self.assertTrue(any(o.VPA_TARGET in p and "can equal both declared requests" in p for p in o.design_problems()))

    def test_declared_inside_both_ranges_is_refused_even_above_the_minimum(self):
        w = self.vpa_workload()
        self.assertTrue(o.vpa_change_problems(w, min_allowed={"cpu": "5m", "memory": "16Mi"}))

    def test_declared_exactly_at_max_allowed_is_reachable(self):
        # Both bounds are inclusive: a recommendation capped DOWN to
        # maxAllowed equals a declared request sitting exactly there.
        w = self.vpa_workload()
        at_max = {"cpu": w.resources.cpu_request, "memory": w.resources.memory_request}
        below = {"cpu": "5m", "memory": "16Mi"}
        problems = o.vpa_change_problems(w, min_allowed=below, max_allowed=at_max)
        self.assertTrue(problems and "can equal both declared requests" in problems[0], problems)
        # Memory alone at its maximum (CPU strictly inside) is still reachable.
        self.assertTrue(o.vpa_change_problems(w, min_allowed=below, max_allowed={"cpu": "40m", "memory": w.resources.memory_request}))
        # Degenerate range: minAllowed == maxAllowed == declared.
        self.assertTrue(o.vpa_change_problems(w, min_allowed=at_max, max_allowed=at_max))

    def test_one_resource_outside_its_range_is_enough(self):
        w = self.vpa_workload()
        self.assertEqual(o.vpa_change_problems(w, min_allowed={"cpu": "20m", "memory": "32Mi"}), [])

    def test_minimum_above_maximum_is_refused(self):
        self.assertTrue(o.vpa_change_problems(self.vpa_workload(), min_allowed={"cpu": "50m", "memory": "48Mi"}))

    def test_minimum_beyond_limitrange_is_reported(self):
        with mock.patch.dict(o.VPA_MIN_ALLOWED, {"memory": "160Mi"}), mock.patch.dict(o.VPA_MAX_ALLOWED, {"memory": "160Mi"}):
            self.assertTrue(any("VPA minimum" in p and "LimitRange max" in p for p in o.design_problems()))

    def test_one_scaler_per_target(self):
        objs = o.scaling_objects(IMAGE)
        self.assertEqual(o.scaler_conflicts(objs), [])
        hpa = next(x for x in objs if x["kind"] == "HorizontalPodAutoscaler")
        doubled = copy.deepcopy(hpa)
        doubled["metadata"]["name"] = "second"
        doubled["spec"]["scaleTargetRef"]["name"] = o.WORKER
        self.assertTrue(any(o.WORKER in p and "exactly one" in p for p in o.scaler_conflicts([*objs, doubled])))
        vpa_on_hpa_target = o.vpa_object("Off")
        vpa_on_hpa_target["spec"]["targetRef"]["name"] = o.HPA_TARGET
        self.assertTrue(o.scaler_conflicts([hpa, vpa_on_hpa_target]))

    def test_scaler_outside_namespace_or_on_non_deployment_is_refused(self):
        hpa = copy.deepcopy(next(x for x in o.scaling_objects(IMAGE) if x["kind"] == "HorizontalPodAutoscaler"))
        hpa["metadata"]["namespace"] = "maops-platform"
        self.assertTrue(any("outside" in p for p in o.scaler_conflicts([hpa])))
        vpa = o.vpa_object("Off")
        vpa["spec"]["targetRef"] = {"kind": "StatefulSet", "name": "maops-state"}
        self.assertTrue(any("only Deployments" in p for p in o.scaler_conflicts([vpa])))

    def test_vpa_modes_limited_to_off_and_initial(self):
        self.assertEqual(o.vpa_object("Initial")["spec"]["updatePolicy"]["updateMode"], "Initial")
        for mode in ("Auto", "Recreate", "InPlaceOrRecreate"):
            with self.assertRaises(ValueError):
                o.vpa_object(mode)

    def test_scaling_image_must_be_pinned(self):
        for bad in ("maops-kubernetes-scaling:day8", "maops-kubernetes-scaling:latest", "maops-kubernetes-app:" + "day8-cfg-" + "a" * 64):
            with self.assertRaises(ValueError):
                o.all_objects(bad)

    def test_over_quota_pod_exceeds_quota_with_each_container_in_range(self):
        pod = o.over_quota_pod(IMAGE)
        total = sum(o.cpu_millis(c["resources"]["requests"]["cpu"]) for c in pod["spec"]["containers"])
        self.assertGreater(total, o.budget().requests_cpu_m)
        for c in pod["spec"]["containers"]:
            self.assertLessEqual(o.cpu_millis(c["resources"]["limits"]["cpu"]), o.cpu_millis(o.LIMIT_MAX["cpu"]))
        over = o.over_limitrange_pod(IMAGE)["spec"]["containers"][0]["resources"]["limits"]["cpu"]
        self.assertGreater(o.cpu_millis(over), o.cpu_millis(o.LIMIT_MAX["cpu"]))
        self.assertNotIn("resources", o.in_budget_pod(IMAGE)["spec"]["containers"][0])


class ObjectSafetyTests(unittest.TestCase):
    def setUp(self):
        self.objects = o.all_objects(IMAGE)

    def _pod_specs(self):
        for obj in self.objects + [o.in_budget_pod(IMAGE), o.over_quota_pod(IMAGE)]:
            if obj["kind"] in ("Deployment", "Job"):
                yield obj["metadata"]["name"], obj["spec"]["template"]["spec"]
            elif obj["kind"] == "Pod":
                yield obj["metadata"]["name"], obj["spec"]

    def test_everything_lives_in_the_scaling_namespace_with_a_day8_identity(self):
        for obj in self.objects:
            if obj["kind"] == "Namespace":
                self.assertEqual(obj["metadata"]["name"], o.NAMESPACE)
                continue
            self.assertEqual(obj["metadata"]["namespace"], o.NAMESPACE, obj["metadata"]["name"])
            text = json.dumps(obj)
            self.assertNotRegex(text, r"day[4567]-|maops-platform\b|maops-state-0")
            self.assertEqual(obj["metadata"]["labels"]["app.kubernetes.io/instance"], o.INSTANCE)

    def test_guards_come_first(self):
        kinds = [x["kind"] for x in self.objects[:3]]
        self.assertEqual(kinds, ["Namespace", "LimitRange", "ResourceQuota"])
        self.assertEqual(self.objects[0]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")

    def test_every_pod_is_restricted_and_tokenless(self):
        for name, spec in self._pod_specs():
            self.assertFalse(spec["automountServiceAccountToken"], name)
            self.assertTrue(spec["securityContext"]["runAsNonRoot"], name)
            self.assertEqual(spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault", name)
            for c in spec["containers"]:
                self.assertEqual(c["securityContext"]["capabilities"]["drop"], ["ALL"], name)
                self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"], name)
                self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"], name)
                if c["image"].startswith("maops-kubernetes-scaling:"):
                    self.assertEqual(c["imagePullPolicy"], "Never", name)

    def test_queue_is_disposable_and_ingress_restricted(self):
        dep = next(x for x in self.objects if x["kind"] == "Deployment" and x["metadata"]["name"] == o.QUEUE)
        c = dep["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["image"], o.REDIS_IMAGE)
        self.assertRegex(o.REDIS_IMAGE, r"@sha256:[0-9a-f]{64}$")
        self.assertIn("--appendonly", c["args"])
        self.assertEqual(c["args"][c["args"].index("--save") + 1], "")
        self.assertEqual(dep["spec"]["template"]["spec"]["volumes"], [{"name": "scratch", "emptyDir": {"sizeLimit": "16Mi"}}])
        policy = next(x for x in self.objects if x["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"], o.selector(o.QUEUE))
        self.assertEqual(len(policy["spec"]["ingress"][0]["from"]), 3)

    def test_scaler_bounds(self):
        hpa = next(x for x in self.objects if x["kind"] == "HorizontalPodAutoscaler")
        self.assertEqual((hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]), (1, 4))
        so = next(x for x in self.objects if x["kind"] == "ScaledObject")
        self.assertEqual((so["spec"]["minReplicaCount"], so["spec"]["maxReplicaCount"]), (0, 3))
        self.assertEqual(so["spec"]["triggers"][0]["type"], "redis")
        worker = next(x for x in self.objects if x["kind"] == "Deployment" and x["metadata"]["name"] == o.WORKER)
        self.assertEqual(worker["spec"]["replicas"], 0)
        load = next(x for x in self.objects if x["kind"] == "Job" and x["metadata"]["name"] == o.HPA_LOAD)
        self.assertEqual(load["spec"]["backoffLimit"], 0)
        self.assertLessEqual(load["spec"]["activeDeadlineSeconds"], 300)


class AddonValuesTests(unittest.TestCase):
    def setUp(self):
        self.values = {name: day8_addons.load_values(name) for name in day8_common.ADDONS}
        self.make_vars = day8_addons.makefile_vars((REPO / "Makefile").read_text())

    def test_repository_values_pass(self):
        self.assertEqual(day8_addons.static_problems(self.values, self.make_vars), [])

    def _problems_after(self, mutate):
        values = copy.deepcopy(self.values)
        mutate(values)
        return day8_addons.static_problems(values, self.make_vars)

    def test_enabled_updater_is_refused(self):
        self.assertTrue(any("updater" in p for p in self._problems_after(lambda v: v["vertical-pod-autoscaler"]["updater"].update(enabled=True))))
        self.assertTrue(any("updater" in p for p in self._problems_after(lambda v: v["vertical-pod-autoscaler"].pop("updater"))))

    def test_unscoped_vpa_is_refused(self):
        def broad(v):
            v["vertical-pod-autoscaler"]["admissionController"]["mutatingWebhookConfiguration"]["namespaceSelector"] = {}
        self.assertTrue(any("namespaceSelector" in p for p in self._problems_after(broad)))
        self.assertTrue(any("recommender lacks" in p for p in self._problems_after(lambda v: v["vertical-pod-autoscaler"]["recommender"].update(extraArgs=[]))))
        def failing(v):
            v["vertical-pod-autoscaler"]["admissionController"]["mutatingWebhookConfiguration"]["failurePolicy"] = "Fail"
        self.assertTrue(any("failurePolicy" in p for p in self._problems_after(failing)))

    def test_cluster_wide_keda_is_refused(self):
        """watchNamespace must be the scaling namespace: empty (cluster-wide,
        the reverted workaround from run 1c59a36c...) or any other value fails."""
        for bad in ("", "maops-platform"):
            self.assertTrue(any("watchNamespace" in p for p in self._problems_after(lambda v: v["keda"].update(watchNamespace=bad))), bad)
        self.assertTrue(any("watchNamespace" in p for p in self._problems_after(lambda v: v["keda"].pop("watchNamespace"))))

    def test_metrics_server_args_are_exact(self):
        self.assertTrue(any("metrics-server args" in p for p in self._problems_after(lambda v: v["metrics-server"]["args"].append("--kubelet-preferred-address-types=Hostname"))))

    def test_makefile_pin_drift_is_refused(self):
        drifted = dict(self.make_vars, KEDA_CHART_VERSION="2.20.0")
        self.assertTrue(any("KEDA_CHART_VERSION" in p for p in day8_addons.static_problems(self.values, drifted)))


def _binding(kind, name, role_kind, role, sa="keda-operator", namespace=None):
    meta = {"name": name}
    if namespace:
        meta["namespace"] = namespace
    return {"kind": kind, "metadata": meta, "roleRef": {"kind": role_kind, "name": role}, "subjects": [{"kind": "ServiceAccount", "namespace": "keda", "name": sa}]}


SCOPED_CLUSTER = [
    _binding("ClusterRoleBinding", "keda-operator-minimal", "ClusterRole", "keda-operator-minimal-cluster-role"),
    _binding("ClusterRoleBinding", "keda-operator-system-auth-delegator", "ClusterRole", "system:auth-delegator", sa="keda-metrics-server"),
    _binding("ClusterRoleBinding", "keda-operator-webhook", "ClusterRole", "keda-operator-webhook", sa="keda-webhook"),
    {"kind": "ClusterRoleBinding", "metadata": {"name": "keda-operator-hpa-controller-external-metrics"}, "roleRef": {"kind": "ClusterRole", "name": "keda-operator-external-metrics-reader"}, "subjects": [{"kind": "ServiceAccount", "namespace": "kube-system", "name": "horizontal-pod-autoscaler"}]},
]
SCOPED_ROLE = [
    _binding("RoleBinding", "keda-operator", "ClusterRole", "keda-operator", namespace="maops-day8-scaling"),
    _binding("RoleBinding", "keda-operator", "ClusterRole", "keda-operator", namespace="keda"),
    _binding("RoleBinding", "keda-operator-certs", "Role", "keda-operator-certs", namespace="keda"),
    _binding("RoleBinding", "keda-operator-auth-reader", "Role", "extension-apiserver-authentication-reader", sa="keda-metrics-server", namespace="kube-system"),
]


class KedaRbacTests(unittest.TestCase):
    """The bindings chart keda-2.21.0 renders with watchNamespace set (and
    the live check evaluates) - plus the ways they could widen."""

    def test_scoped_install_is_clean(self):
        self.assertEqual(day8_addons.keda_binding_problems(SCOPED_CLUSTER, SCOPED_ROLE, True), [])

    def test_after_cleanup_binding_gone_is_clean(self):
        role = [b for b in SCOPED_ROLE if b["metadata"]["namespace"] != "maops-day8-scaling"]
        self.assertEqual(day8_addons.keda_binding_problems(SCOPED_CLUSTER, role, False), [])

    def test_cluster_wide_operator_binding_is_refused(self):
        wide = _binding("ClusterRoleBinding", "keda-operator", "ClusterRole", "keda-operator")
        self.assertTrue(any("cluster-wide" in p for p in day8_addons.keda_binding_problems([*SCOPED_CLUSTER, wide], SCOPED_ROLE, True)))

    def test_allowed_binding_name_with_a_different_role_is_refused(self):
        swapped = _binding("ClusterRoleBinding", "keda-operator-minimal", "ClusterRole", "cluster-admin")
        self.assertTrue(day8_addons.keda_binding_problems([swapped], SCOPED_ROLE, True))

    def test_binding_in_the_application_namespace_is_refused(self):
        app = _binding("RoleBinding", "keda-operator", "ClusterRole", "keda-operator", namespace="maops-platform")
        self.assertTrue(any("maops-platform" in p for p in day8_addons.keda_binding_problems(SCOPED_CLUSTER, [*SCOPED_ROLE, app], True)))

    def test_scoped_binding_must_match_namespace_lifecycle(self):
        without = [b for b in SCOPED_ROLE if b["metadata"]["namespace"] != "maops-day8-scaling"]
        self.assertTrue(any("missing" in p for p in day8_addons.keda_binding_problems(SCOPED_CLUSTER, without, True)))
        self.assertTrue(any("still exists" in p for p in day8_addons.keda_binding_problems(SCOPED_CLUSTER, SCOPED_ROLE, False)))

    def test_can_i_answers_are_strict(self):
        self.assertTrue(day8_addons.parse_can_i(0, "yes\n", ""))
        self.assertFalse(day8_addons.parse_can_i(1, "no\n", ""))
        for rc, out, err in ((1, "", "error: the server doesn't have a resource type"), (0, "no", ""), (1, "yes", ""), (255, "", "Unable to connect")):
            with self.assertRaises(day8_common.Day8Error):
                day8_addons.parse_can_i(rc, out, err)

    def test_forbidden_matrix_is_exact(self):
        """Exact, so dropping any probe fails (a 4-of-8 subset passed before)."""
        self.assertEqual(day8_addons.FORBIDDEN_IN_APP, (
            ("get", "secrets", ""), ("list", "secrets", ""), ("watch", "secrets", ""),
            ("patch", "deployments.apps", "scale"), ("update", "deployments.apps", "scale"),
            ("patch", "statefulsets.apps", "scale"), ("update", "statefulsets.apps", "scale"),
            ("patch", "deployments.apps", ""), ("update", "deployments.apps", ""), ("delete", "deployments.apps", ""),
            ("patch", "statefulsets.apps", ""), ("update", "statefulsets.apps", ""), ("delete", "statefulsets.apps", ""),
            ("create", "pods", ""), ("patch", "pods", ""), ("delete", "pods", ""),
            ("create", "pods", "exec"), ("create", "pods", "eviction"),
            ("update", "configmaps", ""), ("delete", "configmaps", ""),
            ("create", "horizontalpodautoscalers.autoscaling", ""),
            ("create", "rolebindings.rbac.authorization.k8s.io", ""),
        ))
        self.assertEqual(day8_addons.FORBIDDEN_SECRETS_ELSEWHERE, (
            ("list", "secrets", "", None), ("get", "secrets", "", "kube-system"), ("get", "secrets", "", "istio-system"),
            ("get", "secrets", "", "maops-ingress"), ("get", "secrets", "", "maops-day7-validation"),
        ))
        self.assertEqual({(ns, sa) for ns, sa in day8_addons.PERSISTENT_ADDON_IDENTITIES}, {
            ("kube-system", "metrics-server"), ("vpa-system", "vertical-pod-autoscaler-recommender"), ("vpa-system", "vertical-pod-autoscaler-admission-controller")})

    def test_can_i_warnings_are_errors(self):
        for err in ("Warning: the server doesn't have a resource type 'secretz'\n", "Warning: resource 'apiservices' is not namespace scoped in group 'apiregistration.k8s.io'\n"):
            with self.assertRaises(day8_common.Day8Error):
                day8_addons.parse_can_i(1, "no\n", err)

    def test_can_i_scope_arguments(self):
        calls = []

        def kubectl(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "no\n", "")
        with mock.patch.object(day8_common, "kubectl", side_effect=kubectl):
            day8_addons.can_i("metrics-server", "list", "secrets", None, "", "kube-system")
            day8_addons.can_i("keda-operator", "create", "pods", "maops-platform", "exec")
        self.assertIn("--all-namespaces", calls[0])
        self.assertIn("--as=system:serviceaccount:kube-system:metrics-server", calls[0])
        self.assertEqual(calls[1][calls[1].index("-n") + 1], "maops-platform")
        self.assertIn("--subresource=exec", calls[1])

    def _matrix(self, answer):
        checks = day8_common.Checks("matrix")
        with mock.patch.object(day8_addons, "can_i", side_effect=answer), mock.patch("sys.stdout", new=io.StringIO()):
            rows = day8_addons.record_access_matrix(checks, [("keda", "keda-operator"), ("kube-system", "metrics-server")], [("keda", "keda-operator", "patch", "apiservices.apiregistration.k8s.io", "", None)])
        return checks, rows

    def test_access_matrix_all_denied_and_grant_present_passes(self):
        checks, rows = self._matrix(lambda sa, v, r, ns, s="", sa_ns="keda": r == "apiservices.apiregistration.k8s.io")
        self.assertEqual(checks.failed, 0)
        self.assertEqual(len(rows), 3)

    def test_one_forbidden_yes_fails_naming_identity_and_action(self):
        checks, _ = self._matrix(lambda sa, v, r, ns, s="", sa_ns="keda": (sa == "metrics-server" and v == "delete" and r == "pods") or r == "apiservices.apiregistration.k8s.io")
        failed = [m for ok, m in checks.results if not ok]
        self.assertEqual(len(failed), 1)
        self.assertIn("kube-system/metrics-server", failed[0])
        self.assertIn("delete pods @maops-platform", failed[0])

    def test_missing_expected_grant_is_documentation_drift(self):
        checks, _ = self._matrix(lambda *a, **k: False)
        self.assertTrue(any("documentation is stale" in m for ok, m in checks.results if not ok))

    def test_probe_error_propagates(self):
        def boom(*a, **k):
            raise day8_common.Day8Error("Unable to connect")
        with self.assertRaises(day8_common.Day8Error):
            self._matrix(boom)

    def test_record_keda_rbac_positive_control_failure(self):
        checks = day8_common.Checks("rbac")

        def can(sa, v, r, ns, s="", sa_ns="keda"):
            return r in ("apiservices.apiregistration.k8s.io", "validatingwebhookconfigurations.admissionregistration.k8s.io") or (sa == "keda-webhook" and r == "deployments.apps" and ns is None and v == "list")
        with mock.patch.object(day8_common, "kubectl_json", side_effect=lambda *a, **k: {"items": SCOPED_CLUSTER if a[1] == "clusterrolebindings" else SCOPED_ROLE}), \
             mock.patch.object(day8_addons, "can_i", side_effect=can), mock.patch("sys.stdout", new=io.StringIO()):
            day8_addons._record_keda_rbac(checks)
        failed = [m for ok, m in checks.results if not ok]
        self.assertEqual(len(failed), 1, failed)
        self.assertIn("positive control", failed[0])

    def test_binding_edge_cases(self):
        wrong_role_in_keda = _binding("RoleBinding", "keda-operator-certs", "ClusterRole", "cluster-admin", namespace="keda")
        self.assertTrue(day8_addons.keda_binding_problems(SCOPED_CLUSTER, [*SCOPED_ROLE[1:], SCOPED_ROLE[0], wrong_role_in_keda], True))
        mixed = _binding("ClusterRoleBinding", "ops-team", "ClusterRole", "cluster-admin")
        mixed["subjects"].append({"kind": "Group", "name": "ops"})
        self.assertTrue(any("ops-team" in pr for pr in day8_addons.keda_binding_problems([*SCOPED_CLUSTER, mixed], SCOPED_ROLE, True)))
        group_only = {"kind": "ClusterRoleBinding", "metadata": {"name": "g"}, "roleRef": {"kind": "ClusterRole", "name": "view"}, "subjects": [{"kind": "Group", "name": "system:serviceaccounts:keda"}]}
        # Documented limit: group subjects are outside the KEDA-ServiceAccount
        # binding audit; the SubjectAccessReview matrix (effective access,
        # group membership included) is what covers them.
        self.assertEqual(day8_addons.keda_binding_problems([*SCOPED_CLUSTER, group_only], SCOPED_ROLE, True), [])


class KedaOwnershipTests(unittest.TestCase):
    META = {"name": "keda", "namespace": "keda", "chart": "keda", "version": "2.21.0", "labels": {o.KEDA_OWNER_LABEL[0]: o.KEDA_OWNER_LABEL[1]}}

    def test_release_ownership_rules(self):
        self.assertEqual(day8_addons.keda_release_ownership_problems(self.META), [])
        for change in ({"labels": {}}, {"chart": "keda-fork"}, {"version": "2.20.0"}, {"namespace": "other"}):
            self.assertTrue(day8_addons.keda_release_ownership_problems({**self.META, **change}), change)

    def test_webhook_policy_rules(self):
        good = {"webhooks": [{"name": "vscaledobject.kb.io", "failurePolicy": "Fail", "rules": [{"apiGroups": ["keda.sh"]}]}, {"name": "vcloudeventsource.kb.io", "failurePolicy": "Fail", "rules": [{"apiGroups": ["eventing.keda.sh"]}]}]}
        self.assertEqual(day8_addons.keda_webhook_problems(good), [])
        ignore = copy.deepcopy(good)
        ignore["webhooks"][0]["failurePolicy"] = "Ignore"
        self.assertTrue(day8_addons.keda_webhook_problems(ignore))
        core = copy.deepcopy(good)
        core["webhooks"][0]["rules"][0]["apiGroups"] = ["apps"]
        self.assertTrue(any("intercepts API groups" in pr for pr in day8_addons.keda_webhook_problems(core)))
        self.assertTrue(day8_addons.keda_webhook_problems(None))

    def test_preflight_state(self):
        absent = {crd: None for crd in day8_addons.KEDA_CRDS}
        self.assertEqual(day8_preflight.keda_state_problems(None, [], absent, None), [])
        self.assertTrue(any("interrupted run" in pr for pr in day8_preflight.keda_state_problems(self.META, [], absent, None)))
        self.assertTrue(any("FOREIGN KEDA Helm release" in pr for pr in day8_preflight.keda_state_problems(self.META, ["no owner label"], absent, None)))
        with_obj = {**absent, "scaledobjects.keda.sh": ["team-a/orders-worker"]}
        self.assertTrue(any("1 instance(s)" in pr and "team-a/orders-worker" in pr for pr in day8_preflight.keda_state_problems(None, [], with_obj, None)))
        self.assertTrue(day8_preflight.keda_state_problems(None, [], {**absent, "scaledjobs.keda.sh": []}, None), "a KEDA CRD alone (no instances) still means KEDA is present")
        self.assertTrue(any("NOT Day 8-owned" in pr for pr in day8_preflight.keda_state_problems(None, [], absent, {"kubernetes.io/metadata.name": "keda"})))
        self.assertTrue(any("Day 8-owned leftover" in pr for pr in day8_preflight.keda_state_problems(None, [], absent, o.labels(o.KEDA_NAMESPACE_COMPONENT))))

    FOREIGN_NS_LABELS = {"kubernetes.io/metadata.name": "keda", "team": "platform"}

    def _preinstall(self, metadata=None, crds_present=False, namespace="absent", race=False, swapped_after_create=False):
        """Returns (rc, created, calls, text, ns_state). `created` holds the
        namespace objects Day 8 actually created; `calls` every kubectl call
        (a refusal must make none). `race=True`: a foreign namespace appears
        between the pre-install read and the create."""
        created, calls = [], []
        ns_state = {"value": namespace, "uid": "uid-existing", "labels": None}

        def ns_get(*args):
            if ns_state["value"] == "absent":
                if race:  # the read saw nothing; a foreign namespace lands right after it
                    ns_state.update(value="foreign", uid="uid-foreign")
                return None
            labels = o.labels(o.KEDA_NAMESPACE_COMPONENT) if ns_state["value"] == "owned" else dict(self.FOREIGN_NS_LABELS)
            return {"metadata": {"labels": labels, "uid": ns_state["uid"]}}

        def kubectl(*args, **kwargs):
            calls.append(args)
            if args[:1] != ("create",):
                raise AssertionError(f"pre-install may only `kubectl create` the namespace, never {args}")
            if ns_state["value"] != "absent":
                return subprocess.CompletedProcess(args, 1, "", 'Error from server (AlreadyExists): error when creating "STDIN": namespaces "keda" already exists')
            obj = json.loads(kwargs["stdin"])
            created.append(obj)
            ns_state.update(value="owned", uid="uid-replaced" if swapped_after_create else "uid-day8")
            return subprocess.CompletedProcess(args, 0, json.dumps({**obj, "metadata": {**obj["metadata"], "uid": "uid-day8"}}), "")
        out = io.StringIO()
        with mock.patch.object(day8_common, "require_cluster_profile"), \
             mock.patch.object(day8_common, "helm_release_metadata", return_value=metadata), \
             mock.patch.object(day8_common, "object_state", return_value="present" if crds_present else "absent"), \
             mock.patch.object(day8_common, "kubectl_json_or_none", side_effect=ns_get), \
             mock.patch.object(day8_common, "kubectl_json", side_effect=lambda *a, **k: ns_get()), \
             mock.patch.object(day8_common, "kubectl", side_effect=kubectl), \
             mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=io.StringIO()):
            rc = day8_addons.keda_preinstall()
        return rc, created, calls, out.getvalue(), ns_state

    def test_preinstall_clean_cluster_creates_owned_namespace(self):
        rc, created, calls, text, _ = self._preinstall()
        self.assertEqual(rc, 0, text)
        self.assertEqual(created, [o.keda_namespace_object()])
        self.assertEqual([c[:4] for c in calls], [("create", "-f", "-", "-o")], "created with `kubectl create`, never `apply`")
        self.assertIn("uid-day8", text)

    def test_preinstall_refuses_foreign_release(self):
        rc, created, calls, text, _ = self._preinstall(metadata={**self.META, "labels": {}}, crds_present=True, namespace="owned")
        self.assertEqual(rc, 1)
        self.assertIn("owner label", text)
        self.assertEqual((created, calls), ([], []), "a refusal makes no change at all")

    def test_preinstall_refuses_crds_without_day8_release(self):
        rc, created, calls, text, _ = self._preinstall(crds_present=True)
        self.assertEqual(rc, 1)
        self.assertEqual((created, calls), ([], []), "refused before the namespace step - nothing created")
        self.assertIn("refused before any change", text)

    def test_preinstall_refuses_foreign_namespace(self):
        rc, created, calls, _, ns_state = self._preinstall(namespace="foreign")
        self.assertEqual(rc, 1)
        self.assertEqual((created, calls), ([], []), "never adopt (relabel) a foreign namespace")
        self.assertEqual(ns_state["value"], "foreign")

    def test_preinstall_create_race_fails_without_adopting(self):
        """A namespace that appears after the pre-install read: `kubectl
        create` fails (AlreadyExists); Day 8 neither applies, labels nor
        installs into it, and its labels stay exactly as they were."""
        rc, created, calls, text, ns_state = self._preinstall(race=True)
        self.assertEqual(rc, 1)
        self.assertEqual(created, [])
        self.assertEqual([c[0] for c in calls], ["create"], "one create attempt only - no apply/label/patch fallback")
        self.assertIn("appeared after the pre-install check - refusing to adopt it", text)
        self.assertEqual((ns_state["value"], ns_state["uid"]), ("foreign", "uid-foreign"))

    def test_preinstall_refuses_namespace_replaced_after_create(self):
        rc, created, _, text, _ = self._preinstall(swapped_after_create=True)
        self.assertEqual(rc, 1)
        self.assertEqual(len(created), 1)
        self.assertIn("is the one Day 8 just created (uid uid-replaced, expected uid-day8)", text)

    def test_preinstall_accepts_own_release(self):
        rc, created, calls, text, _ = self._preinstall(metadata=self.META, crds_present=True, namespace="owned")
        self.assertEqual(rc, 0, text)
        self.assertEqual((created, calls), ([], []), "an existing Day 8 namespace is reused, not re-created")

    def test_cleanup_refuses_foreign_release(self):
        c = FakeCluster(foreign_release=True, objects=())
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("helm", c.order())
        self.assertTrue(c.ns and c.release == "present")
        self.assertIn("release is not Day 8's", text)

    def test_cleanup_refuses_foreign_keda_namespace(self):
        """Day 8 release recorded, but `keda` lacks Day 8's labels (e.g. a
        legacy --create-namespace install): nothing is uninstalled or
        deleted - the namespace check comes before every KEDA change."""
        c = FakeCluster(objects=(), keda_ns="foreign")
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        for step in ("helm", "delete-runtime", "delete-keda-namespace", "delete-namespace"):
            self.assertNotIn(step, c.order())
        self.assertTrue(c.ns, "scaling namespace kept")
        self.assertEqual((c.release, c.runtime, c.keda_ns), ("present", True, "foreign"))
        self.assertIn("lacks Day 8's identity labels - KEDA NOT uninstalled; no runtime Secret, lease or namespace deleted", text)

    def test_cleanup_without_release_leaves_foreign_runtime_objects_untouched(self):
        """No Helm release, a foreign `keda` namespace holding a
        `kedaorg-certs` Secret (even one carrying app=keda-operator) and the
        `operator.keda.sh` lease: neither is deleted, nor is the namespace."""
        c = FakeCluster(release="absent", crds=False, objects=(), binding=False, keda_ns="foreign", runtime_labels={"app": "keda-operator"})
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertEqual([step for step in c.order() if step in ("delete-runtime", "delete-keda-namespace", "helm")], [])
        self.assertEqual((c.runtime, c.keda_ns), (True, "foreign"), "foreign Secret, lease and namespace remain")
        self.assertTrue(c.ns, "scaling namespace kept")

    def test_cleanup_with_keda_namespace_absent_touches_nothing_in_it(self):
        c = FakeCluster(release="absent", crds=False, objects=(), binding=False, runtime=False, keda_ns=None)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("delete-runtime", c.order())
        self.assertIn("namespace keda absent - no KEDA runtime object or Pod can exist", text)

    def test_makefile_keda_wiring(self):
        text = (REPO / "Makefile").read_text()
        self.assertEqual(day8_addons.makefile_keda_problems(text), [])
        broken = text.replace("--labels $(DAY8_KEDA_OWNER_LABEL) \\\n", "")
        self.assertTrue(any("--labels" in pr for pr in day8_addons.makefile_keda_problems(broken)))
        adopt = text.replace("--namespace keda $(DAY8_HELM_SCOPE)", "--namespace keda --create-namespace $(DAY8_HELM_SCOPE)")
        self.assertTrue(any("--create-namespace" in pr for pr in day8_addons.makefile_keda_problems(adopt)))
        no_pre = text.replace("env $(DAY8_ENV) python3 scripts/day8_addons.py keda-preinstall; \\\n", "")
        self.assertTrue(any("keda-preinstall" in pr for pr in day8_addons.makefile_keda_problems(no_pre)))

    def test_static_requires_fail_closed_webhook(self):
        values = {name: day8_addons.load_values(name) for name in day8_common.ADDONS}
        values["keda"]["webhooks"]["failurePolicy"] = "Ignore"
        self.assertTrue(any("failurePolicy" in pr for pr in day8_addons.static_problems(values, day8_addons.makefile_vars((REPO / "Makefile").read_text()))))


class FakeCluster:
    """A minimal simulated cluster for phase_cleanup(): namespace, KEDA Helm
    release, KEDA CRDs, KEDA objects in the scaling namespace (with
    finalizers only KEDA can release), the scoped RoleBinding, Pods and the
    operator's runtime leftovers. Every mutating call is logged in order."""

    KEDA_KINDS = ("scaledobjects.keda.sh", "scaledjobs.keda.sh", "triggerauthentications.keda.sh")
    # Explicit, as chart keda-2.21.0 defines them - not derived from names.
    CRD_SCOPES = {
        "cloudeventsources.eventing.keda.sh": "Namespaced",
        "clustercloudeventsources.eventing.keda.sh": "Cluster",
        "clustertriggerauthentications.keda.sh": "Cluster",
        "scaledjobs.keda.sh": "Namespaced",
        "scaledobjects.keda.sh": "Namespaced",
        "triggerauthentications.keda.sh": "Namespaced",
    }
    CONTEXT_NAMESPACE = "default"

    def list_crd_instances(self, args):
        """`kubectl get <crd> ...` with kubectl's real namespace semantics:
        a Namespaced listing sees ONLY the context namespace unless
        `--all-namespaces`/`-A` (or `-n <ns>`) is given; a Cluster-scoped
        listing ignores namespace flags. Output follows the requested
        format (`custom-columns=NS,NAME` rows or `-o name`)."""
        crd = args[1]
        if crd in self.instance_list_fail:
            return subprocess.CompletedProcess(args, 1, "", "error: the server is currently unable to handle the request")
        if not self.crds:
            return subprocess.CompletedProcess(args, 1, "", "error: the server doesn't have a resource type \"" + crd.split(".")[0] + "\"")
        scope = self.CRD_SCOPES[crd]
        kind_prefix = crd.split(".")[0][:-1] + "." + crd.split(".", 1)[1]
        if scope == "Namespaced":
            items = [(day8_scaling.NS, o_.split("/")[1]) for o_ in self.objects if crd.startswith(o_.split("/")[0].split(".")[0])]
            items += [tuple(i.split("/", 1)) for i in self.foreign.get(crd, [])]
            if "--all-namespaces" in args or "-A" in args:
                visible = items
            else:
                ns = args[args.index("-n") + 1] if "-n" in args else (args[args.index("--namespace") + 1] if "--namespace" in args else self.CONTEXT_NAMESPACE)
                visible = [i for i in items if i[0] == ns]
            if any(a.startswith("custom-columns=") for a in args):
                rows = [f"{ns} {name}" for ns, name in visible] + list(self.malformed_rows.get(crd, []))
            else:
                rows = [f"{kind_prefix}/{name}" for _, name in visible]
        else:
            names = list(self.foreign.get(crd, []))
            rows = [f"<none> {n}" for n in names] if any(a.startswith("custom-columns=") for a in args) else [f"{kind_prefix}/{n}" for n in names]
        self.listing_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "\n".join(rows), "")

    def __init__(self, ns=True, release="present", crds=True, objects=("scaledobject.keda.sh/day8-queue-worker",), binding=True,
                 uninstall_rc=0, delete_leaves=False, list_fail=(), crd_unreadable=False, runtime=True, runtime_labels=None,
                 foreign=None, instance_list_fail=(), scope_unreadable=(), foreign_release=False, keda_ns=None,
                 pods_linger=False, uninstall_leaves_crds=False, runtime_delete_fails=False, ns_labels=None):
        self.ns, self.release, self.crds = ns, release, crds
        self.objects, self.binding = list(objects), binding and ns
        self.uninstall_rc, self.delete_leaves, self.list_fail = uninstall_rc, delete_leaves, set(list_fail)
        self.crd_unreadable = crd_unreadable
        self.runtime_objects = {"secret", "lease.coordination.k8s.io"} if runtime else set()
        self.runtime_labels = runtime_labels if runtime_labels is not None else {"app": "keda-operator"}
        self.pods = ["pod/keda-operator-x"] if release == "present" else []
        # Instances NOT created by Day 8: {crd: [ "ns/name" | "name" ]}
        self.foreign = {k: list(v) for k, v in (foreign or {}).items()}
        self.instance_list_fail, self.scope_unreadable = set(instance_list_fail), set(scope_unreadable)
        self.malformed_rows = {}
        self.foreign_release = foreign_release
        self.pods_linger, self.uninstall_leaves_crds, self.runtime_delete_fails = pods_linger, uninstall_leaves_crds, runtime_delete_fails
        self.ns_labels = ns_labels
        # The `keda` namespace: "owned" (Day 8 labels), "foreign" or None.
        self.keda_ns = keda_ns if keda_ns is not None else ("owned" if release == "present" else None)
        self.listing_calls = []
        self.secret_reads = []
        self.evidence = []
        self.log = []

    @property
    def runtime(self):
        return bool(self.runtime_objects)

    @property
    def keda_installed(self):
        return self.release == "present"

    # --- seams patched into day8_common -------------------------------------
    def kubectl_json_or_none(self, *args):
        if args == ("get", "namespace", day8_scaling.NS):
            labels = self.ns_labels if self.ns_labels is not None else {"app.kubernetes.io/instance": o.INSTANCE, "app.kubernetes.io/component": o.NAMESPACE_COMPONENT}
            return {"metadata": {"name": day8_scaling.NS, "labels": labels}} if self.ns else None
        if args == ("get", "namespace", "keda"):
            if self.keda_ns is None:
                return None
            labels = o.labels(o.KEDA_NAMESPACE_COMPONENT) if self.keda_ns == "owned" else {"kubernetes.io/metadata.name": "keda"}
            return {"metadata": {"name": "keda", "labels": labels}}
        if args[:3] == ("-n", "keda", "get"):
            raise AssertionError(f"runtime objects (the kedaorg-certs Secret) must never be fetched as full objects: {args}")
        raise AssertionError(f"unexpected kubectl_json_or_none {args}")

    def kubectl_json(self, *args):
        if args[0] == "get" and args[1] in ("clusterrolebindings", "rolebindings"):
            return {"items": []}
        raise AssertionError(f"unexpected kubectl_json {args}")

    def helm_release_metadata(self, name, namespace):
        if self.release == "unreadable":
            raise day8_common.Day8Error("Kubernetes cluster unreachable")
        if self.release != "present":
            return None
        labels = {"name": "keda", "owner": "helm", "status": "deployed"}
        if not self.foreign_release:
            labels[o.KEDA_OWNER_LABEL[0]] = o.KEDA_OWNER_LABEL[1]
        return {"name": "keda", "namespace": "keda", "chart": "keda", "version": "2.21.0", "labels": labels}

    def helm_release_state(self, name, namespace):
        if self.release == "unreadable":
            raise day8_common.Day8Error("Kubernetes cluster unreachable")
        return self.release

    def helm(self, *args, timeout=60.0):
        self.log.append(("helm",) + args[:1])
        if args[0] == "uninstall":
            if self.uninstall_rc == 0:
                self.release = "absent"
                self.crds = self.crds and self.uninstall_leaves_crds
                self.pods = self.pods if self.pods_linger else []
                if self.ns:
                    self.binding = False
            return subprocess.CompletedProcess(args, self.uninstall_rc, 'release "keda" uninstalled' if self.uninstall_rc == 0 else "", "" if self.uninstall_rc == 0 else "Error: uninstallation completed with 1 error(s): context deadline exceeded")
        raise AssertionError(f"unexpected helm {args}")

    def resource_type_absent(self, resource):
        if resource not in day8_common.CRD_BACKED_TYPES or resource.endswith("autoscaling.k8s.io"):
            return False
        if self.crd_unreadable:
            raise day8_common.Day8Error("existence could not be determined (exit 1): the server is currently unable to handle the request")
        return not self.crds

    def object_state(self, kind, name, namespace=None):
        if kind == "namespace" and name == "keda":
            return "present" if self.keda_ns else "absent"
        if kind in ("secret", "lease.coordination.k8s.io"):
            return "present" if kind in self.runtime_objects else "absent"
        if kind == "rolebinding" and namespace == day8_scaling.NS:
            return "present" if self.binding else "absent"
        if kind == "customresourcedefinition":
            return "present" if self.crds else "absent"
        return "present" if self.keda_installed else "absent"

    def kubectl(self, *args, check=True, timeout=30.0, stdin=None):
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        fail = lambda err: subprocess.CompletedProcess(args, 1, "", err)
        NS = day8_scaling.NS
        if args[:3] == ("-n", "keda", "get") and args[-2:] == ("-o", "jsonpath={.metadata.labels}"):
            self.secret_reads.append(args)
            return ok(json.dumps(self.runtime_labels if args[3] == "secret" else {}))
        if args[:2] == ("get", "customresourcedefinition") and args[-1] == "jsonpath={.spec.scope}":
            crd = args[2]
            if crd in self.scope_unreadable:
                return fail("Unable to connect to the server: net/http: TLS handshake timeout")
            if not self.crds:
                return fail(f'Error from server (NotFound): customresourcedefinitions.apiextensions.k8s.io "{crd}" not found')
            return ok(self.CRD_SCOPES[crd])
        if args[:1] == ("get",) and args[1] in day8_scaling.KEDA_CRDS:
            return self.list_crd_instances(args)
        if args[:3] == ("-n", NS, "get") and args[3] in self.KEDA_KINDS:
            return fail("error: etcdserver: request timed out") if args[3] in self.list_fail else ok("\n".join(o_ for o_ in self.objects if args[3].startswith(o_.split(".")[0][:-1] if False else o_.split("/")[0].split(".")[0] + "s")))
        if args[:3] == ("-n", NS, "get") and "," in args[3]:
            return ok("\n".join(self.objects))
        if args[:4] == ("-n", NS, "get", "rolebinding"):
            return ok("rolebinding.rbac.authorization.k8s.io/keda-operator") if self.binding else fail('Error from server (NotFound): rolebindings "keda-operator" not found')
        if args[:4] == ("-n", NS, "get", "horizontalpodautoscalers"):
            return ok("horizontalpodautoscaler.autoscaling/keda-hpa-day8-queue-worker" if self.objects else "")
        if args[:3] == ("-n", NS, "delete"):
            self.log.append(("delete-keda-objects",))
            if not self.delete_leaves and self.keda_installed:
                self.objects = []
            return ok()
        if args[:4] == ("-n", "keda", "get", "pods"):
            return ok("\n".join(self.pods))
        if args[:3] == ("-n", "keda", "delete"):
            if self.runtime_delete_fails:
                return fail("Error from server (Forbidden): secrets \"kedaorg-certs\" is forbidden")
            self.log.append(("delete-runtime", args[3]))
            self.runtime_objects.discard(args[3])
            return ok()
        if args[:3] == ("delete", "namespace", "keda"):
            self.log.append(("delete-keda-namespace",))
            self.keda_ns = None
            self.runtime_objects.clear()
            return ok('namespace "keda" deleted')
        if args[:2] == ("delete", "namespace"):
            self.log.append(("delete-namespace",))
            self.ns, self.binding = False, False
            return ok('namespace "maops-day8-scaling" deleted')
        if args[0] == "get" and "--all-namespaces" in args:
            if args[1] in self.list_fail:
                return fail("error: the server is currently unable to handle the request")
            if args[1] in self.KEDA_KINDS and not self.crds:
                return fail(f'error: the server doesn\'t have a resource type "{args[1].split(".")[0]}"')
            return ok()
        raise AssertionError(f"unexpected kubectl call {args}")

    # ------------------------------------------------------------------------
    def run_cleanup(self):
        fake_poll = lambda fn, timeout, interval, until: (lambda v: (v, until(v)))(fn())
        out = io.StringIO()
        with mock.patch.multiple(
            day8_common,
            require_cluster_profile=mock.DEFAULT,
            kubectl_json_or_none=self.kubectl_json_or_none,
            kubectl_json=self.kubectl_json,
            kubectl=self.kubectl,
            helm=self.helm,
            helm_release_state=self.helm_release_state,
            helm_release_metadata=self.helm_release_metadata,
            resource_type_absent=self.resource_type_absent,
            object_state=self.object_state,
            poll=fake_poll,
            write_evidence=lambda name, payload, *a: self.evidence.append(name),
        ), mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=io.StringIO()):
            rc = day8_scaling.phase_cleanup()
        return rc, out.getvalue()

    def order(self):
        return [e[0] for e in self.log]


class CleanupLifecycleTests(unittest.TestCase):
    """phase_cleanup(): (1) release KEDA objects while KEDA can still remove
    its finalizers, (2) uninstall the KEDA release, (3) delete the namespace
    and prove KEDA and every Day 8 control are gone."""

    def test_full_cleanup_order(self):
        c = FakeCluster()
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertEqual(c.order(), ["delete-keda-objects", "helm", "delete-runtime", "delete-runtime", "delete-keda-namespace", "delete-namespace"])
        self.assertIn("KEDA released", text)
        self.assertIn("KEDA chart-owned and runtime objects explicitly NotFound", text)
        self.assertEqual((c.ns, c.release, c.crds, c.runtime, c.pods, c.keda_ns), (False, "absent", False, False, [], None))

    def test_uninstall_failure_keeps_the_namespace(self):
        c = FakeCluster(uninstall_rc=1)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertTrue(c.ns, "namespace must be kept for diagnosis")
        self.assertNotIn("delete-namespace", c.order())
        self.assertIn("helm uninstall keda -n keda --cascade foreground --wait --timeout 300s: exit 1", text)

    def test_unreleased_objects_stop_before_uninstall(self):
        c = FakeCluster(delete_leaves=True)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("helm", c.order(), "KEDA must not be uninstalled while it still holds finalizers")
        self.assertTrue(c.ns)

    def test_objects_without_binding_stop_before_uninstall(self):
        c = FakeCluster(binding=False)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertEqual(c.order(), [])
        self.assertIn("neither KEDA nor the namespace is removed", text)

    def test_run_failed_before_keda_was_installed(self):
        c = FakeCluster(release="absent", crds=False, objects=(), binding=False, runtime=False)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertEqual(c.order(), ["delete-namespace"])
        self.assertIn("KEDA not installed", text)
        self.assertIn("nothing to uninstall", text)

    def test_partial_install_without_objects(self):
        c = FakeCluster(objects=())  # release present (e.g. failed --wait), CRDs present, nothing scaled yet
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertEqual(c.order(), ["helm", "delete-runtime", "delete-runtime", "delete-keda-namespace", "delete-namespace"])

    def test_repeated_cleanup_is_idempotent(self):
        c = FakeCluster()
        self.assertEqual(c.run_cleanup()[0], 0)
        c.log.clear()
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertEqual(c.order(), [], "a second cleanup must not mutate anything")
        self.assertIn("already absent", text)

    def test_leftover_keda_without_namespace_is_uninstalled(self):
        """The pre-fix final state: namespace gone, KEDA still installed."""
        c = FakeCluster(ns=False, objects=(), binding=False)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertEqual(c.order(), ["helm", "delete-runtime", "delete-runtime", "delete-keda-namespace"])

    def test_unreadable_release_state_keeps_the_namespace(self):
        c = FakeCluster(release="unreadable", objects=())
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertTrue(c.ns)
        self.assertIn("could not determine whether Helm release keda/keda exists", text)

    def test_runtime_secret_with_foreign_labels_is_not_deleted(self):
        c = FakeCluster(objects=(), runtime_labels={"app": "something-else"})
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("delete-runtime", c.order())
        self.assertTrue(c.ns)

    def test_failed_builtin_list_fails_cleanup(self):
        c = FakeCluster(list_fail={"resourcequotas"})
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertIn("could not list resourcequotas in all namespaces (exit 1: error: the server is currently unable", text)
        self.assertIn("UNVERIFIED", text)

    def test_every_listed_resource_failure_is_reported(self):
        for resource in day8_scaling.CLEANUP_RESOURCES:
            if resource in FakeCluster.KEDA_KINDS:
                continue
            rc, text = FakeCluster(list_fail={resource}).run_cleanup()
            self.assertEqual(rc, 1, resource)
            self.assertIn(f"could not list {resource}", text)

    def test_unserved_keda_type_counts_only_with_explicit_crd_absence(self):
        rc, text = FakeCluster().run_cleanup()  # after uninstall: CRDs explicitly NotFound
        self.assertEqual(rc, 0, text)
        self.assertIn("scaledobjects.keda.sh: type not served - CRD explicitly NotFound", text)

    def test_unreadable_crd_state_fails_closed(self):
        c = FakeCluster(objects=(), release="absent", crds=False, crd_unreadable=True, binding=False, runtime=False)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("delete-namespace", c.order())

    def test_keda_object_list_failure_keeps_everything(self):
        c = FakeCluster(list_fail={"scaledobjects.keda.sh"})
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertEqual(c.order(), [])


class KedaCrdGuardTests(unittest.TestCase):
    """`helm uninstall keda` deletes the six KEDA CRDs and every instance
    with them - so, immediately before it, every KEDA CRD must hold zero
    instances across its full scope (Day 8's own were already released)."""

    def test_clean_path_uninstalls(self):
        c = FakeCluster()
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, text)
        self.assertIn("no instance of any of the 6 KEDA CRDs exists", text)
        self.assertIn("helm", c.order())

    def _blocked(self, c):
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1, text)
        self.assertNotIn("helm", c.order(), "KEDA must not be uninstalled")
        self.assertNotIn("delete-namespace", c.order(), "the namespace must be kept")
        self.assertEqual(c.release, "present")
        self.assertTrue(c.crds)
        self.assertIn("KEDA NOT uninstalled", text)
        return text

    def test_foreign_namespaced_object_blocks_uninstall(self):
        c = FakeCluster(foreign={"scaledobjects.keda.sh": ["team-a/orders-worker"]})
        text = self._blocked(c)
        self.assertIn("team-a/orders-worker", text)
        self.assertEqual(c.foreign["scaledobjects.keda.sh"], ["team-a/orders-worker"], "foreign object untouched")
        self.assertEqual([e for e in c.order() if e == "delete-keda-objects"], ["delete-keda-objects"], "only Day 8's own namespace was cleaned")

    def test_cluster_scoped_object_blocks_uninstall(self):
        c = FakeCluster(foreign={"clustertriggerauthentications.keda.sh": ["shared-aws-creds"]})
        text = self._blocked(c)
        self.assertIn("clustertriggerauthentications.keda.sh (Cluster)", text)
        self.assertIn("shared-aws-creds", text)

    def test_unreadable_instance_list_blocks_uninstall(self):
        text = self._blocked(FakeCluster(instance_list_fail={"cloudeventsources.eventing.keda.sh"}))
        self.assertIn("cloudeventsources.eventing.keda.sh (Namespaced): instances unreadable", text)

    def test_unreadable_crd_blocks_uninstall(self):
        text = self._blocked(FakeCluster(scope_unreadable={"scaledjobs.keda.sh"}))
        self.assertIn("CRD scaledjobs.keda.sh: state unreadable", text)

    def test_leftover_day8_object_in_its_own_namespace_also_blocks(self):
        """If the release step somehow left an object, the guard catches it."""
        c = FakeCluster(foreign={"triggerauthentications.keda.sh": [f"{o.NAMESPACE}/stray-auth"]})
        self._blocked(c)

    def test_listing_argv_is_exact_for_each_scope(self):
        calls = []

        def kubectl(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(day8_common, "kubectl", side_effect=kubectl):
            day8_scaling._crd_instances("scaledobjects.keda.sh", "Namespaced")
            day8_scaling._crd_instances("clustertriggerauthentications.keda.sh", "Cluster")
        self.assertEqual(calls[0], ("get", "scaledobjects.keda.sh", "--all-namespaces", "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name", "--no-headers"))
        self.assertEqual(calls[1], ("get", "clustertriggerauthentications.keda.sh", "-o", "name"))

    def test_fake_models_context_namespace_listing(self):
        """Guards the fake itself: without --all-namespaces a Namespaced
        listing must NOT see a foreign object in another namespace - so the
        guard's all-namespace claim really depends on the production argv."""
        c = FakeCluster(foreign={"scaledobjects.keda.sh": ["team-a/orders-worker"]})
        narrow = c.list_crd_instances(("get", "scaledobjects.keda.sh", "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name", "--no-headers"))
        wide = c.list_crd_instances(("get", "scaledobjects.keda.sh", "--all-namespaces", "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name", "--no-headers"))
        self.assertEqual(narrow.stdout, "")
        self.assertIn("team-a orders-worker", wide.stdout)

    def test_dropping_all_namespaces_would_be_caught(self):
        """Mutation proof: with the namespaced listing narrowed to the
        context namespace, the foreign object is missed and the guard test
        fails - i.e. the test now depends on --all-namespaces."""
        original = day8_scaling._crd_instances

        def narrowed(crd, scope):
            if scope != "Namespaced":
                return original(crd, scope)
            names, err = day8_scaling._list_names_raw("get", crd, "-o", "custom-columns=NS:.metadata.namespace,NAME:.metadata.name", "--no-headers")
            return [line.replace(" ", "/", 1) for line in names or [] if line.strip()]
        c = FakeCluster(foreign={"scaledobjects.keda.sh": ["team-a/orders-worker"]})
        with mock.patch.object(day8_scaling, "_crd_instances", side_effect=narrowed):
            rc, text = c.run_cleanup()
        self.assertEqual(rc, 0, "the narrowed listing misses the foreign object (this is what --all-namespaces prevents)")
        self.assertIn("helm", c.order())
        c2 = FakeCluster(foreign={"scaledobjects.keda.sh": ["team-a/orders-worker"]})
        self.assertEqual(c2.run_cleanup()[0], 1, "the production listing catches it")

    def test_malformed_row_blocks_uninstall(self):
        c = FakeCluster()
        c.malformed_rows["scaledjobs.keda.sh"] = ["lonely-token"]
        text = self._blocked(c)
        self.assertIn("scaledjobs.keda.sh (Namespaced): instances unreadable", text)
        self.assertIn("malformed instance row", text)
        c = FakeCluster()
        c.malformed_rows["triggerauthentications.keda.sh"] = ["<none> stray"]
        self._blocked(c)

    def test_empty_scope_answer_blocks_uninstall(self):
        c = FakeCluster()
        original = c.kubectl

        def empty_scope(*args, **kwargs):
            if args[:2] == ("get", "customresourcedefinition") and args[2] == "scaledobjects.keda.sh":
                return subprocess.CompletedProcess(args, 0, "", "")
            return original(*args, **kwargs)
        c.kubectl = empty_scope
        text = self._blocked(c)
        self.assertIn("CRD scaledobjects.keda.sh: unexpected scope ''", text)

    def test_guard_logic_is_pure(self):
        scopes = {crd: ("Cluster" if crd.startswith("cluster") else "Namespaced") for crd in day8_scaling.KEDA_CRDS}
        problems, found = day8_scaling.crd_instance_problems(scopes.get, lambda crd, scope: [])
        self.assertEqual(problems, [])
        self.assertEqual(found["clustercloudeventsources.eventing.keda.sh"]["scope"], "Cluster")
        problems, found = day8_scaling.crd_instance_problems(lambda crd: None, lambda crd, scope: self.fail("must not list"))
        self.assertEqual(problems, [])
        self.assertEqual(set(found.values()), {"CRD absent"})
        problems, _ = day8_scaling.crd_instance_problems(lambda crd: "Weird", lambda crd, scope: [])
        self.assertEqual(len(problems), 6)


class SecretExposureTests(unittest.TestCase):
    """The kedaorg-certs Secret holds the operator's private key: cleanup may
    learn only its existence and labels, never its data."""

    def test_secret_labels_read_via_jsonpath_only(self):
        c = FakeCluster(objects=())
        rc, text = c.run_cleanup()  # the fake raises if any full-object read of a runtime object happens
        self.assertEqual(rc, 0, text)
        self.assertEqual([a[3] for a in c.secret_reads], ["secret", "lease.coordination.k8s.io"])
        for a in c.secret_reads:
            self.assertEqual(a[-2:], ("-o", "jsonpath={.metadata.labels}"))

    def test_runtime_object_labels_never_requests_data(self):
        calls = []

        def kubectl(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '{"app":"keda-operator"}', "")
        with mock.patch.object(day8_common, "object_state", return_value="present"), mock.patch.object(day8_common, "kubectl", side_effect=kubectl):
            self.assertEqual(day8_scaling.runtime_object_labels("secret", "kedaorg-certs"), {"app": "keda-operator"})
        self.assertEqual(len(calls), 1)
        self.assertIn("jsonpath={.metadata.labels}", calls[0])
        self.assertFalse(any(a in ("json", "yaml", "-ojson", "-oyaml", "-o=json", "-o=yaml") or "{.data" in a or ".stringData" in a for a in calls[0]))
        output = [a for a in calls[0] if a.startswith("jsonpath=")]
        self.assertEqual(output, ["jsonpath={.metadata.labels}"], "only the labels expression may be requested")

    def test_foreign_labelled_secret_still_refused(self):
        c = FakeCluster(objects=(), runtime_labels={"app": "something-else"})
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("delete-runtime", c.order())
        self.assertIn("does not carry the KEDA operator labels", text)

    def test_no_day8_script_fetches_a_secret_object(self):
        """Static guard (AST, whole call expressions): no call in a Day 8
        script that names a Secret kind (secret/secrets, any case) may ask
        for a full object (`-o json|yaml|wide`, go-template, custom-columns,
        a `.data` jsonpath) or use the JSON helpers. Variable kinds (e.g.
        runtime_object_labels(kind, ...)) are covered by the seam tests."""
        import ast

        bad_outputs = re.compile(r"^(json|yaml|wide)$|^-o(json|yaml|wide)$|^-o=|go-template|custom-columns|\{\.data|stringData", re.I)
        for path in sorted((REPO / "scripts").glob("day8_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                consts = [a.value for a in ast.walk(node) if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if not any(re.fullmatch(r"secrets?", c, re.I) for c in consts):
                    continue
                func = ast.unparse(node.func)
                self.assertNotIn("kubectl_json", func, f"{path.name}:{node.lineno} reads a Secret via {func}")
                offenders = [c for c in consts if bad_outputs.search(c)]
                self.assertEqual(offenders, [], f"{path.name}:{node.lineno} requests Secret output {offenders}")

    def test_static_scan_catches_plural_and_multiline(self):
        """The scan itself must catch what the old line scan missed."""
        import ast

        src = 'kubectl("-n", "keda", "get",\n        "Secrets", "kedaorg-certs",\n        "-o", "json")\n'
        node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call))
        consts = [a.value for a in ast.walk(node) if isinstance(a, ast.Constant)]
        self.assertTrue(any(re.fullmatch(r"secrets?", c, re.I) for c in consts))
        self.assertIn("json", consts)


class CleanupLowPathTests(unittest.TestCase):
    """Fail-closed paths that previously had no direct test."""

    def test_scaling_namespace_without_both_labels_is_not_deleted(self):
        for labels in ({"app.kubernetes.io/instance": o.INSTANCE}, {"app.kubernetes.io/component": o.NAMESPACE_COMPONENT}, {}):
            c = FakeCluster(ns_labels=labels)
            rc, text = c.run_cleanup()
            self.assertEqual(rc, 1, labels)
            self.assertEqual(c.order(), [], "nothing deleted")
            self.assertIn("lacks the Day 8 identity labels - refusing to delete it", text)

    def test_failed_runtime_delete_keeps_the_namespace(self):
        c = FakeCluster(objects=(), runtime_delete_fails=True)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("delete-namespace", c.order())
        self.assertIn("deleted KEDA runtime secret keda/kedaorg-certs (not Helm-owned): exit 1", text)

    def test_lingering_pods_keep_the_namespace(self):
        c = FakeCluster(objects=(), pods_linger=True)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertNotIn("delete-namespace", c.order())
        self.assertIn("[FAIL] no Pod left in namespace keda", text)

    def test_uninstall_that_leaves_a_survivor_fails_the_inventory(self):
        c = FakeCluster(objects=(), uninstall_leaves_crds=True)
        rc, text = c.run_cleanup()
        self.assertEqual(rc, 1)
        self.assertIn("present ['customresourcedefinition/", text)

    def test_every_exit_writes_evidence(self):
        for c in (FakeCluster(ns_labels={}), FakeCluster(uninstall_rc=1), FakeCluster(), FakeCluster(delete_leaves=True), FakeCluster(foreign={"scaledobjects.keda.sh": ["team-a/x"]})):
            c.run_cleanup()
            self.assertEqual(len(c.evidence), 1, "exactly one cleanup evidence file per exit, success or failure")
            self.assertTrue(c.evidence[0].startswith("cleanup-"))


class ScalingIdentityTests(unittest.TestCase):
    def test_dedicated_tokenless_service_account(self):
        sa = o.service_account_object()
        self.assertEqual((sa["kind"], sa["metadata"]["name"], sa["automountServiceAccountToken"]), ("ServiceAccount", "day8-scaling", False))
        self.assertIn(sa, o.guard_objects())
        for obj in o.all_objects(IMAGE):
            spec = obj.get("spec", {}).get("template", {}).get("spec") if obj["kind"] in ("Deployment", "Job") else None
            if spec:
                self.assertEqual(spec["serviceAccountName"], "day8-scaling", obj["metadata"]["name"])
                self.assertFalse(spec["automountServiceAccountToken"])

    def test_ownership_rule_needs_both_labels(self):
        self.assertTrue(o.is_day8_owned(o.labels("day8-keda"), "day8-keda"))
        self.assertFalse(o.is_day8_owned({"app.kubernetes.io/instance": o.INSTANCE}, "day8-keda"))
        self.assertFalse(o.is_day8_owned(o.labels("day8-scaling"), "day8-keda"))
        self.assertFalse(o.is_day8_owned(None, "day8-keda"))

    def test_strict_not_found(self):
        r = subprocess.CompletedProcess([], 1, "", "error: lookup failed: thing not found in cache")
        with mock.patch.object(day8_common, "kubectl", return_value=r), mock.patch.object(day8_common, "require_cluster_profile"):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.kubectl_json_or_none("get", "namespace", "x")
        r = subprocess.CompletedProcess([], 1, "", 'Error from server (NotFound): namespaces "x" not found')
        with mock.patch.object(day8_common, "kubectl", return_value=r):
            self.assertIsNone(day8_common.kubectl_json_or_none("get", "namespace", "x"))


class BuildRecordSchemaTests(unittest.TestCase):
    """Regression for the 1.0.0 bump: identical image bytes under a new
    VERSION must not collide with the historical 0.7.0 build record."""

    DIGESTS = {"gateway": "sha256:" + "1" * 64, "app": "sha256:" + "2" * 64, "state": "sha256:" + "3" * 64}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home / "state" / "day7-builds"
        self.loc = {"home": self.home, "forbidden_roots": ()}

    def test_schema1_collided_across_versions(self):
        """The defect, reproduced with the legacy formula."""
        a = day7_build.build_from_digests(self.DIGESTS, "0.7.0", schema=1)
        b = day7_build.build_from_digests(self.DIGESTS, "1.0.0", schema=1)
        self.assertEqual(a.build_id, b.build_id)
        day7_build.store_build(a, self.root, **self.loc)
        with self.assertRaises(day7_build.BuildError):
            day7_build.store_build(b, self.root, **self.loc)

    def test_schema2_keeps_both_records(self):
        legacy = day7_build.build_from_digests(self.DIGESTS, "0.7.0", schema=1)
        new = day7_build.build_from_digests(self.DIGESTS, "1.0.0")
        self.assertEqual(new.schema, 2)
        self.assertNotEqual(legacy.build_id, new.build_id)
        day7_build.store_build(legacy, self.root, **self.loc)
        day7_build.store_build(new, self.root, **self.loc)
        self.assertEqual(day7_build.load_build(new.build_id, self.root, **self.loc).build_id, new.build_id) if day7_build.VERSION == "1.0.0" else None
        self.assertEqual(day7_build.build_from_record(legacy.record(), "0.7.0").build_id, legacy.build_id, "legacy ids re-derive with the schema-1 formula")
        self.assertEqual(new.images[0].tag, "1.0.0-cfg-" + "1" * 64)

    def test_unknown_schema_refused(self):
        rec = day7_build.build_from_digests(self.DIGESTS, "1.0.0").record()
        rec["schema"] = 3
        with self.assertRaises(day7_build.BuildError):
            day7_build.build_from_record(rec, "1.0.0")
        with self.assertRaises(day7_build.BuildError):
            day7_build.build_id_for(self.DIGESTS, None, schema=2)


class ExistenceAnswerTests(unittest.TestCase):
    def test_get_state(self):
        self.assertEqual(day8_common.get_state(0, ""), "present")
        self.assertEqual(day8_common.get_state(1, 'Error from server (NotFound): secrets "x" not found'), "absent")
        self.assertEqual(day8_common.get_state(1, "Error: release: not found"), "absent")
        for err in ("error: the server doesn't have a resource type \"scaledobjects\"", "Unable to connect to the server", ""):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.get_state(1, err)

    def test_builtin_types_are_never_proven_absent(self):
        with mock.patch.object(day8_common, "object_state", side_effect=AssertionError("must not be consulted")):
            for builtin in ("horizontalpodautoscalers", "horizontalpodautoscalers.autoscaling", "resourcequotas", "limitranges"):
                self.assertFalse(day8_common.resource_type_absent(builtin))

    def test_crd_backed_type_absence_comes_from_the_crd(self):
        with mock.patch.object(day8_common, "object_state", return_value="absent") as state:
            self.assertTrue(day8_common.resource_type_absent("scaledobjects.keda.sh"))
            state.assert_called_once_with("customresourcedefinition", "scaledobjects.keda.sh")


class StableControlsTests(unittest.TestCase):
    """day8_stable.observe_controls() runs before, during and after KEDA's
    lifetime: an unserved KEDA type is "none" only with explicit CRD absence."""

    def _observe(self, crd_state):
        def kubectl(*args, **kwargs):
            if args[3] == "scaledobjects.keda.sh":
                return subprocess.CompletedProcess(args, 1, "", "error: the server doesn't have a resource type \"scaledobjects\"")
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(day8_common, "kubectl", side_effect=kubectl), mock.patch.object(day8_common, "object_state", side_effect=crd_state):
            return day8_stable.observe_controls()

    def test_uninstalled_keda_counts_as_none(self):
        self.assertEqual(self._observe(lambda *a: "absent")["scaledobjects.keda.sh"], [])

    def test_failed_list_with_crd_present_raises(self):
        with self.assertRaises(day8_common.Day8Error):
            self._observe(lambda *a: "present")

    def test_unreadable_crd_raises(self):
        def unreadable(*a):
            raise day8_common.Day8Error("Unable to connect")
        with self.assertRaises(day8_common.Day8Error):
            self._observe(unreadable)


class KedaFinalStateTests(unittest.TestCase):
    """The after-cleanup gate expects KEDA ABSENT, object by object."""

    def test_inventory_covers_every_chart_object(self):
        cluster = {k for k, _ in day8_addons.KEDA_CLUSTER_OBJECTS}
        self.assertEqual(cluster, {"customresourcedefinition", "clusterrole", "clusterrolebinding", "validatingwebhookconfiguration", "apiservice", "namespace"})
        self.assertEqual(len(day8_addons.KEDA_CRDS), 6)
        self.assertEqual(len(day8_addons.KEDA_CLUSTER_OBJECTS) + len(day8_addons.KEDA_NAMESPACED_OBJECTS), 31, "30 chart objects + Day 8's own keda namespace")
        self.assertIn(("namespace", "keda"), day8_addons.KEDA_CLUSTER_OBJECTS)
        self.assertIn(("rolebinding", o.NAMESPACE, "keda-operator"), day8_addons.KEDA_NAMESPACED_OBJECTS)
        self.assertEqual({n for _, _, n in day8_addons.KEDA_RUNTIME_OBJECTS}, {n for _, n, _ in day8_scaling.KEDA_RUNTIME_LEFTOVERS})

    def test_all_absent_is_clean(self):
        states = day8_addons.keda_inventory_states(lambda k, n, ns: "absent")
        self.assertEqual((states["present"], states["unreadable"]), ([], []))
        self.assertEqual(len(states["absent"]), 33)

    def test_a_single_survivor_or_unreadable_answer_is_reported(self):
        def state(kind, name, ns):
            if name == "keda-operator-minimal":
                return "present"
            if kind == "apiservice":
                raise day8_common.Day8Error("Unable to connect")
            return "absent"
        states = day8_addons.keda_inventory_states(state)
        self.assertEqual(states["present"], ["clusterrolebinding/keda-operator-minimal"])
        self.assertTrue(states["unreadable"][0].startswith("apiservice/v1beta1.external.metrics.k8s.io"))


def hpa_samples(peak=4, final=1):
    s = [{"phase": "baseline", "spec": 1, "ready": 1, "desired": 1}]
    s += [{"phase": "load", "spec": n, "ready": n, "desired": n} for n in range(1, peak + 1)]
    s += [{"phase": "cooldown", "spec": n, "ready": n, "desired": n} for n in range(peak, final - 1, -1)]
    return s


class HpaVerdictTests(unittest.TestCase):
    def test_full_cycle_passes(self):
        self.assertTrue(all(ok for ok, _ in day8_scaling.evaluate_hpa(hpa_samples())))

    def test_no_scale_out_fails(self):
        verdicts = day8_scaling.evaluate_hpa(hpa_samples(peak=1))
        self.assertFalse(verdicts[1][0])
        self.assertFalse(verdicts[3][0], "scale-in without a prior scale-out proves nothing")

    def test_no_scale_in_fails(self):
        samples = hpa_samples()[:-3]
        self.assertFalse(day8_scaling.evaluate_hpa(samples)[3][0])

    def test_exceeding_max_fails(self):
        samples = hpa_samples()
        samples.append({"phase": "load", "spec": 5, "ready": 4, "desired": 5})
        self.assertFalse(day8_scaling.evaluate_hpa(samples)[2][0])

    def test_unready_scale_out_does_not_count(self):
        samples = [{"phase": "baseline", "spec": 1, "ready": 1, "desired": 1}, {"phase": "load", "spec": 4, "ready": 1, "desired": 4}, {"phase": "cooldown", "spec": 1, "ready": 1, "desired": 1}]
        self.assertFalse(day8_scaling.evaluate_hpa(samples)[1][0])

    def test_transitions_record_only_changes(self):
        t = day8_scaling.transitions(hpa_samples(), ("spec", "ready", "desired"))
        self.assertEqual([x["spec"] for x in t], [1, 2, 3, 4, 3, 2, 1])


def keda_samples(drain=True, zero=True, activate=True):
    s = [{"queue": 0, "processed": 0, "spec": 0, "ready": 0, "pods": 0, "active": False}]
    if activate:
        s += [{"queue": 60, "processed": 0, "spec": 1, "ready": 0, "pods": 1, "active": True},
              {"queue": 50, "processed": 5, "spec": 3, "ready": 3, "pods": 3, "active": True}]
    s.append({"queue": 0 if drain else 10, "processed": 60 if drain else 50, "spec": 3, "ready": 3, "pods": 3, "active": not drain})
    s.append({"queue": 0 if drain else 10, "processed": 60 if drain else 50, "spec": 0 if zero else 1, "ready": 0, "pods": 0 if zero else 1, "active": False})
    return s


class KedaVerdictTests(unittest.TestCase):
    def test_full_cycle_passes(self):
        self.assertTrue(all(ok for ok, _ in day8_scaling.evaluate_keda(keda_samples())))

    def test_never_activated_fails(self):
        self.assertFalse(all(ok for ok, _ in day8_scaling.evaluate_keda(keda_samples(activate=False))))

    def test_undrained_queue_fails(self):
        verdicts = dict((m.split(":")[0], ok) for ok, m in day8_scaling.evaluate_keda(keda_samples(drain=False)))
        self.assertFalse(verdicts["queue drained"])

    def test_no_scale_to_zero_fails(self):
        self.assertFalse(day8_scaling.evaluate_keda(keda_samples(zero=False))[-1][0])

    def test_exceeding_max_fails(self):
        s = keda_samples()
        s.insert(2, {"queue": 40, "processed": 10, "spec": 4, "ready": 4, "pods": 4, "active": True})
        self.assertFalse(day8_scaling.evaluate_keda(s)[5][0])

    def test_quota_blocked_events(self):
        events = [
            {"reason": "FailedCreate", "message": 'pods "x" is forbidden: exceeded quota: day8-scaling-budget', "involvedObject": {"kind": "ReplicaSet", "name": "day8-hpa-target-1"}},
            {"reason": "FailedCreate", "message": "other"},
            {"reason": "Scheduled", "message": "exceeded quota"},
        ]
        self.assertEqual(len(day8_scaling.quota_blocked(events)), 1)


DECLARED = {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "50m", "memory": "64Mi"}}


def vpa_case(**overrides):
    case = {
        "declared": DECLARED,
        "recommendation_off": {"containerName": "scaling", "target": {"cpu": "35m", "memory": "52428800"}},
        "targets_at_admission": [{"cpu": "35m", "memory": "52428800"}],
        "old_before": {"uid": "a", "resources": DECLARED, "restarts": 0, "annotations": {}},
        "old_after": {"uid": "a", "resources": DECLARED, "restarts": 0, "annotations": {}},
        "new_pod": {"uid": "b", "resources": {"requests": {"cpu": "35m", "memory": "50Mi"}, "limits": {"cpu": "175m", "memory": "100Mi"}}, "restarts": 0, "annotations": {"vpaUpdates": "Pod resources updated"}},
    }
    case.update(overrides)
    return case


class VpaVerdictTests(unittest.TestCase):
    def verdicts(self, **overrides):
        return day8_scaling.evaluate_vpa(**vpa_case(**overrides))

    def test_applied_bounded_recommendation_passes(self):
        self.assertTrue(all(ok for ok, _ in self.verdicts()), self.verdicts())

    def test_recommendation_alone_is_not_an_applied_change(self):
        unmutated = {"uid": "b", "resources": DECLARED, "restarts": 0, "annotations": {}}
        failed = [m for ok, m in self.verdicts(new_pod=unmutated) if not ok]
        self.assertTrue(any("mutated by the VPA admission controller" in m for m in failed))
        self.assertTrue(any("differs from the declared" in m for m in failed))
        self.assertFalse(all(ok for ok, _ in self.verdicts(new_pod=None)))

    def test_touched_existing_pod_fails(self):
        resized = {"uid": "a", "resources": {"requests": {"cpu": "35m", "memory": "50Mi"}, "limits": DECLARED["limits"]}, "restarts": 0, "annotations": {}}
        self.assertFalse(all(ok for ok, _ in self.verdicts(old_after=resized)))
        recreated = {"uid": "z", "resources": DECLARED, "restarts": 0, "annotations": {}}
        self.assertFalse(all(ok for ok, _ in self.verdicts(old_after=recreated)))

    def test_out_of_bounds_recommendation_fails(self):
        self.assertFalse(all(ok for ok, _ in self.verdicts(recommendation_off={"target": {"cpu": "500m", "memory": "32Mi"}})))
        self.assertFalse(all(ok for ok, _ in self.verdicts(recommendation_off=None)))

    def test_run_f837_cold_start_annotation_without_change_fails(self):
        # Verbatim shape of run f837802b: recommender floor 10m/32Mi, admitted
        # with the VPA annotation, resources identical to the template.
        floor = {"cpu": "10m", "memory": "32Mi"}
        annotated_unchanged = {
            "uid": "0114fbfa", "resources": DECLARED, "restarts": 0,
            "annotations": {"vpaObservedContainers": "scaling", "vpaUpdates": "Pod resources updated by day8-vpa-target: container 0: cpu request, memory request, cpu limit, memory limit"},
        }
        verdicts = self.verdicts(recommendation_off={"containerName": "scaling", "target": floor, "uncappedTarget": floor}, targets_at_admission=[floor, floor], new_pod=annotated_unchanged)
        by_text = {m: ok for ok, m in verdicts}
        self.assertTrue(next(ok for m, ok in by_text.items() if "mutated by the VPA admission controller" in m), "the annotation is present")
        self.assertFalse(next(ok for m, ok in by_text.items() if "differs from the declared" in m), "an annotation alone must not pass")
        self.assertFalse(all(by_text.values()))

    def test_cold_start_capped_to_raised_minimum_passes(self):
        # Same cold start under the corrected policy: VPA caps the floor
        # recommendation up to minAllowed 10m/48Mi; limits scale 2x.
        capped = {"cpu": "10m", "memory": "48Mi"}
        new_pod = {"uid": "b", "resources": {"requests": capped, "limits": {"cpu": "50m", "memory": "96Mi"}}, "restarts": 0, "annotations": {"vpaUpdates": "Pod resources updated"}}
        verdicts = self.verdicts(recommendation_off={"containerName": "scaling", "target": capped, "uncappedTarget": {"cpu": "10m", "memory": "32Mi"}}, targets_at_admission=[capped, capped], new_pod=new_pod)
        self.assertTrue(all(ok for ok, _ in verdicts), verdicts)

    def test_within_bounds_memory_minimum_is_inclusive_and_enforced(self):
        self.assertEqual(o.VPA_MIN_ALLOWED["memory"], "48Mi")
        self.assertTrue(day8_scaling.within_bounds({"cpu": "10m", "memory": "48Mi"}))
        self.assertTrue(day8_scaling.within_bounds({"cpu": "10m", "memory": str(48 * 1024**2)}))
        for below in ("47Mi", str(48 * 1024**2 - 1), "32Mi"):
            self.assertFalse(day8_scaling.within_bounds({"cpu": "10m", "memory": below}), below)
        # Through the verdicts: a below-minimum target and applied request
        # fail both bound verdicts even with CPU inside its range.
        low = {"cpu": "20m", "memory": "40Mi"}
        new_pod = {"uid": "b", "resources": {"requests": low, "limits": {"cpu": "100m", "memory": "80Mi"}}, "restarts": 0, "annotations": {"vpaUpdates": "Pod resources updated"}}
        failed = [m for ok, m in self.verdicts(recommendation_off={"target": low}, targets_at_admission=[low], new_pod=new_pod) if not ok]
        self.assertTrue(any(m.startswith("recommendation target") for m in failed), failed)
        self.assertTrue(any(m.startswith("applied requests") for m in failed), failed)

    def test_requests_not_matching_admission_target_fail(self):
        self.assertFalse(all(ok for ok, _ in self.verdicts(targets_at_admission=[{"cpu": "20m", "memory": "40Mi"}])))

    def test_quantities_compare_by_value(self):
        self.assertTrue(day8_scaling.same_quantities({"limits.cpu": "2", "pods": "13"}, {"limits.cpu": "2000m", "pods": "13"}))
        self.assertTrue(day8_scaling.same_quantities({"memory": "52428800"}, {"memory": "50Mi"}))
        self.assertFalse(day8_scaling.same_quantities({"cpu": "1"}, {"cpu": "999m"}))
        self.assertFalse(day8_scaling.same_quantities({"cpu": "1"}, {"cpu": "1", "memory": "1Mi"}))


def snapshot():
    pods = {}
    for prefix, n in (("maops-gateway", 3), ("maops-app", 3), ("maops-state", 1)):
        for i in range(n):
            pods[f"{prefix}-{i}"] = {"uid": f"{prefix}-{i}", "ready": True, "terminating": False, "vpa_annotations": [], "containers": {}}
    return {
        "helm": {"revision": 13, "status": "deployed", "values": {}, "manifest_sha256": "x"},
        "workloads": {"deployment/maops-app": {"uid": "u", "replicas": 3, "ready": 3}},
        "pods": pods,
        "route": {},
        "storage": {"pvc_phase": "Bound", "pvc_uid": "p"},
        "external": {"root": {"status": 200, "message": "Hello (Day 7 stable)"}, "state": {"status": 200}},
        "controls": {"limitranges": [], "resourcequotas": []},
    }


class StableStateTests(unittest.TestCase):
    def test_healthy_snapshot(self):
        self.assertEqual(day8_stable.health_problems(snapshot()), [])

    def test_vpa_annotation_or_control_in_app_namespace_fails(self):
        s = snapshot()
        s["pods"]["maops-app-0"]["vpa_annotations"] = ["vpaUpdates"]
        s["controls"]["resourcequotas"] = ["resourcequota/x"]
        problems = day8_stable.health_problems(s)
        self.assertTrue(any("VPA admission" in p for p in problems))
        self.assertTrue(any("resourcequotas present" in p for p in problems))

    def test_missing_pod_or_unready_or_route_failure(self):
        s = snapshot()
        del s["pods"]["maops-app-2"]
        s["external"]["root"] = {"status": 503}
        problems = day8_stable.health_problems(s)
        self.assertTrue(any("2 maops-app" in p for p in problems))
        self.assertTrue(any("external GET /" in p for p in problems))

    def test_diff_reports_replaced_pod(self):
        before, after = snapshot(), snapshot()
        after["pods"]["maops-state-0"]["uid"] = "new"
        self.assertEqual(day8_stable.diff(before["pods"], after["pods"]), ['maops-state-0.uid: "maops-state-0" -> "new"'])
        after["pods"]["maops-extra"] = {}
        self.assertTrue(any("absent from baseline" in d for d in day8_stable.diff(before["pods"], after["pods"])))
        self.assertEqual(day8_stable.diff(before, snapshot()), [])


def pod_fixture(name, uid, ready=True, terminating=False, restarts=0, cpu="10m"):
    meta = {"name": name, "uid": uid, "annotations": {}}
    if terminating:
        meta["deletionTimestamp"] = "2026-10-03T00:00:00Z"
    return {
        "metadata": meta,
        "spec": {"nodeName": "maops-k8s-day7-worker", "containers": [{"name": "c", "image": "img:1", "resources": {"requests": {"cpu": cpu}}}]},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}], "containerStatuses": [{"name": "c", "imageID": "sha256:x", "restartCount": restarts}]},
    }


class StableDay7ProtectionTests(unittest.TestCase):
    """Day 7 regressions the stable check must catch (finding: thin coverage)."""

    def test_unready_terminating_and_undeployed_fail(self):
        s = snapshot()
        s["pods"]["maops-app-0"]["ready"] = False
        s["pods"]["maops-gateway-1"]["terminating"] = True
        s["helm"]["status"] = "pending-upgrade"
        problems = day8_stable.health_problems(s)
        self.assertTrue(any("maops-app-0 is not Ready" in p for p in problems))
        self.assertTrue(any("maops-gateway-1 is not Ready" in p and "terminating=True" in p for p in problems))
        self.assertTrue(any("pending-upgrade" in p for p in problems))

    def test_diff_catches_nested_restart_and_resize(self):
        before = snapshot()
        before["pods"]["maops-state-0"]["containers"] = {"maops-state": {"restarts": 0, "resources": {"requests": {"cpu": "50m"}}, "image": "a", "image_id": "x"}}
        after = copy.deepcopy(before)
        after["pods"]["maops-state-0"]["containers"]["maops-state"]["restarts"] = 1
        after["pods"]["maops-state-0"]["containers"]["maops-state"]["resources"]["requests"]["cpu"] = "40m"
        found = day8_stable.diff(before["pods"], after["pods"])
        self.assertIn("maops-state-0.containers.maops-state.restarts: 0 -> 1", found)
        self.assertIn('maops-state-0.containers.maops-state.resources.requests.cpu: "50m" -> "40m"', found)

    def test_check_compares_all_seven_sections(self):
        base = snapshot()
        compared = []
        real_diff = day8_stable.diff

        def spy(b, c, path=""):
            if path == "":
                compared.append(b)
            return real_diff(b, c, path)
        out = io.StringIO()
        with mock.patch.object(day8_common, "read_evidence", return_value=base), mock.patch.object(day8_stable, "observe", return_value=copy.deepcopy(base)), \
             mock.patch.object(day8_stable, "diff", side_effect=spy), mock.patch.object(day8_common, "write_evidence"), mock.patch("sys.stdout", new=out):
            rc = day8_stable.check()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(len(compared), 7)
        for section in ("helm", "workloads", "pods", "route", "storage", "external", "controls"):
            self.assertIn(f"[PASS] {section}: unchanged since baseline", out.getvalue())

    def test_check_fails_on_a_restart(self):
        base = snapshot()
        base["pods"]["maops-app-0"]["containers"] = {"maops-app": {"restarts": 2}}
        now = copy.deepcopy(base)
        now["pods"]["maops-app-0"]["containers"]["maops-app"]["restarts"] = 3
        with mock.patch.object(day8_common, "read_evidence", return_value=base), mock.patch.object(day8_stable, "observe", return_value=now), \
             mock.patch.object(day8_common, "write_evidence"), mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(day8_stable.check(), 1)

    def test_observe_pods_captures_identity_restarts_resources(self):
        items = [pod_fixture("maops-state-0", "u1", restarts=4, cpu="25m"), pod_fixture("maops-app-x", "u2", ready=False, terminating=True)]
        with mock.patch.object(day8_common, "kubectl_json", return_value={"items": items}):
            pods = day8_stable.observe_pods()
        self.assertEqual(pods["maops-state-0"]["uid"], "u1")
        self.assertEqual(pods["maops-state-0"]["containers"]["c"]["restarts"], 4)
        self.assertEqual(pods["maops-state-0"]["containers"]["c"]["resources"], {"requests": {"cpu": "25m"}})
        self.assertEqual(pods["maops-state-0"]["containers"]["c"]["image_id"], "sha256:x")
        self.assertEqual((pods["maops-app-x"]["ready"], pods["maops-app-x"]["terminating"]), (False, True))


class EvidenceTests(unittest.TestCase):
    def test_write_once_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            path = day8_common.write_evidence("x.json", {"a": 1}, d)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            with self.assertRaises(day8_common.Day8Error):
                day8_common.write_evidence("x.json", {"a": 2}, d)
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            for bad in ("../x", ".hidden", "a/b"):
                with self.assertRaises(day8_common.Day8Error):
                    day8_common.write_evidence(bad, {}, d)

    def test_run_dir_requires_consistent_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.run_dir()
        with mock.patch.dict(os.environ, {"DAY8_RUN_ID": "abc", "DAY8_RUN_DIR": "/home/x/other"}):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.run_dir()
        with mock.patch.dict(os.environ, {"DAY8_RUN_ID": "abc", "DAY8_RUN_DIR": "/tmp/abc"}):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.run_dir()

    def test_init_refuses_tmp(self):
        with mock.patch.dict(os.environ, {"DAY8_RUN_ID": "abc", "DAY8_RUN_DIR": "/tmp/abc"}):
            with self.assertRaises(day8_common.Day8Error):
                day8_common.init_run_dir()


class PreflightTests(unittest.TestCase):
    def test_version_window(self):
        self.assertEqual(day8_preflight.version_problems(36), [])
        self.assertTrue(any("vertical-pod-autoscaler" in p for p in day8_preflight.version_problems(35)))
        self.assertTrue(any("keda" in p for p in day8_preflight.version_problems(37)))

    def test_host_headroom(self):
        self.assertEqual(day8_preflight.host_headroom("MemAvailable:    4000000 kB\n", "4.5 3 2 1/2 3", 4), [])
        problems = day8_preflight.host_headroom("MemAvailable:    1000000 kB\n", "300 3 2 1/2 3", 4)
        self.assertEqual(len(problems), 2)

    def test_load_settle_waits_then_passes_without_changing_the_limit(self):
        loads = iter(["14.0 1 1 1/1 1", "12.5 1 1 1/1 1", "9.0 1 1 1/1 1"])
        mem = "MemAvailable:    4000000 kB\n"
        problems, samples = day8_preflight.settle_host_headroom(lambda: (mem, next(loads)), 4, timeout=100, interval=1, sleep=lambda s: None, clock=lambda: 0)
        self.assertEqual((problems, samples), ([], ["14.0", "12.5", "9.0"]))

    def test_load_that_never_settles_fails(self):
        mem = "MemAvailable:    4000000 kB\n"
        ticks = iter(range(0, 1000, 10))
        problems, samples = day8_preflight.settle_host_headroom(lambda: (mem, "20 1 1 1/1 1"), 4, timeout=30, interval=10, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertTrue(problems)
        self.assertGreaterEqual(len(samples), 3)

    def test_worker_headroom_counts_only_live_worker_requests(self):
        nodes = [
            {"metadata": {"name": "cp", "labels": {kube.CONTROL_PLANE_LABEL: ""}}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}},
            {"metadata": {"name": "w1", "labels": {}}, "status": {"allocatable": {"cpu": "4", "memory": "8078604Ki"}}},
        ]
        pod = lambda node, cpu, phase="Running": {"spec": {"nodeName": node, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": "100Mi"}}}]}, "status": {"phase": phase}}
        free_cpu, free_mem = day8_preflight.worker_headroom(nodes, [pod("w1", "500m"), pod("cp", "1"), pod("w1", "1", "Succeeded"), pod("w1", "250m")])
        self.assertEqual(free_cpu, 3250)
        self.assertEqual(free_mem, 8078604 // 1024 - 200)


class ImagePinTests(unittest.TestCase):
    DIGEST = "sha256:" + "b" * 64

    def test_round_trip(self):
        ref = day8_image.pinned_ref(self.DIGEST)
        self.assertEqual(ref, f"maops-kubernetes-scaling:day8-cfg-{'b' * 64}")
        self.assertEqual(day8_image.digest_from_ref(ref), self.DIGEST)
        with self.assertRaises(ValueError):
            day8_image.digest_from_ref("maops-kubernetes-scaling:day8")

    def test_record_refuses_a_moving_tag(self):
        digests = iter([self.DIGEST, "sha256:" + "c" * 64])
        ok = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch.object(day8_common, "write_evidence") as write:
            with self.assertRaises(day8_common.Day8Error):
                day8_image.record(digest_of=lambda ref: next(digests), run=ok)
            write.assert_not_called()

    def test_record_writes_the_pinned_ref(self):
        ok = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with mock.patch.object(day8_common, "write_evidence") as write:
            entry = day8_image.record(digest_of=lambda ref: self.DIGEST, run=ok)
        self.assertEqual(ok.call_args[0][0], ["docker", "tag", "maops-kubernetes-scaling:day8", entry["ref"]])
        write.assert_called_once()
        self.assertEqual(write.call_args[0][1]["config_digest"], self.DIGEST)


class MakefileTests(unittest.TestCase):
    TEXT = (REPO / "Makefile").read_text()

    def recipe(self, target):
        m = re.search(rf"^{re.escape(target)}:.*\n((?:[ \t]+.*\n?)*)", self.TEXT, re.M)
        self.assertIsNotNone(m, target)
        return m.group(1)

    def test_targets_declared(self):
        for target in ("day8-check", "day8-final-gate", "day8-cleanup", "day8-addons-install", "day8-hpa", "day8-vpa", "day8-keda", "day8-quota", "day8-guards"):
            self.assertRegex(self.TEXT, rf"\.PHONY:[^\n]*(\\\n[^\n]*)*\b{target}\b")

    def test_addon_install_is_pinned_reset_values_and_day7_only(self):
        r = self.recipe("day8-addons-install")
        self.assertEqual(r.count("--reset-values"), 3)
        self.assertEqual(r.count("--version $("), 3)
        for forbidden in ("--reuse-values", "--set", "day6", "kubectl patch", "rollout restart"):
            self.assertNotIn(forbidden, r)
        self.assertIn("$(DAY8_HELM_SCOPE)", r)
        self.assertIn("DAY7_KCONTEXT", self.TEXT.split("DAY8_HELM_SCOPE :=")[1].splitlines()[0])

    def test_mutating_targets_take_the_day7_lock(self):
        for target in ("day8-addons-install", "day8-guards", "day8-quota", "day8-hpa", "day8-vpa", "day8-keda", "day8-cleanup", "day8-check", "day8-image-load"):
            self.assertTrue(self.recipe(target).strip().startswith("$(DAY7_LOCK)"), target)

    def test_namespace_exists_before_keda_install(self):
        r = self.recipe("day8-check")
        self.assertLess(r.index("day8-guards"), r.index("day8-addons-install"))
        self.assertLess(r.index("day8-addons-install"), r.index("day8-addons-check"))
        install = self.recipe("day8-addons-install")
        self.assertIn("run make day8-guards first", install)
        self.assertLess(install.index("get namespace maops-day8-scaling"), install.index("helm upgrade"))
        demo, cleanup = r.index("{"), r.index("day8-cleanup")
        self.assertTrue(demo < r.index("day8-addons-install") < cleanup, "the KEDA install is inside the group that is always cleaned up")

    def test_final_gate_uses_the_after_cleanup_addon_check(self):
        gate = self.recipe("day8-final-gate")
        self.assertIn("day8-addons-final-check", gate)
        self.assertNotRegex(gate, r"day8-addons-check\b")
        self.assertIn("check after-cleanup", self.recipe("day8-addons-final-check"))
        self.assertIn("check active", self.recipe("day8-addons-check"))

    def test_check_always_attempts_cleanup_after_demonstrations(self):
        r = self.recipe("day8-check")
        demo = r.index("day8-keda")
        cleanup = r.index("day8-cleanup")
        self.assertLess(demo, cleanup)
        self.assertIn("demo=$$?", r[demo:cleanup])
        self.assertLess(cleanup, r.index("day8-final-gate"))
        self.assertNotRegex(r, r"day7-(blue-green|canary|recreate|check|deploy|baseline)\b")
        self.assertNotIn("cluster-delete", r)

    def test_stable_check_after_each_demonstration(self):
        r = self.recipe("day8-check")
        for demo in ("day8-hpa", "day8-vpa", "day8-keda"):
            after = r[r.index(demo):]
            nxt = after[after.index("\n") + 1:].strip().split("\n")[0]
            self.assertIn("$(MAKE) day8-stable-check", nxt, f"{demo} must be followed IMMEDIATELY by day8-stable-check, found {nxt!r}")


class RespAndBoundsTests(unittest.TestCase):
    def setUp(self):
        import scaling
        self.scaling = scaling

    def test_encode_and_parse(self):
        self.assertEqual(self.scaling.encode_command("LLEN", "day8:jobs"), b"*2\r\n$4\r\nLLEN\r\n$9\r\nday8:jobs\r\n")
        stream = io.BytesIO(b":42\r\n$5\r\nhello\r\n$-1\r\n*2\r\n$1\r\na\r\n:1\r\n+OK\r\n")
        self.assertEqual([self.scaling.read_reply(stream) for _ in range(5)], [42, "hello", None, ["a", 1], "OK"])
        with self.assertRaises(self.scaling.RespError):
            self.scaling.read_reply(io.BytesIO(b"-ERR wrong\r\n"))
        with self.assertRaises(self.scaling.RespError):
            self.scaling.read_reply(io.BytesIO(b""))

    def test_knobs_are_clamped(self):
        with mock.patch.dict(os.environ, {"LOAD_SECONDS": "99999", "LOAD_CONCURRENCY": "-3", "WORK_MS": "nope"}):
            self.assertEqual(self.scaling.env_int("LOAD_SECONDS", 120, 1, self.scaling.MAX_LOAD_SECONDS), 300)
            self.assertEqual(self.scaling.env_int("LOAD_CONCURRENCY", 4, 1, 8), 1)
            self.assertEqual(self.scaling.env_int("WORK_MS", 50, 1, 200), 50)

    def test_worker_finishes_the_in_flight_item_when_stopped(self):
        """Regression for run 7c99936d...: a scale-down SIGTERM after BLPOP
        must not lose the popped item."""
        stop = self.scaling.StopFlag()
        calls = []

        class Client:
            def call(self, *args):
                calls.append(args)
                return ["day8:jobs", "item-0"] if args[0] == "BLPOP" else 1

        def sleep(_):
            stop.set()  # SIGTERM arrives while the item is being processed

        done = self.scaling.work_loop(Client(), "day8:jobs", 2000, stop, sleep=sleep)
        self.assertEqual(done, 1)
        self.assertEqual([c[0] for c in calls], ["BLPOP", "INCR"])
        self.assertEqual(calls[1], ("INCR", "day8:jobs:processed"))

    def test_worker_takes_no_item_after_stop(self):
        stop = self.scaling.StopFlag()
        stop.set()
        client = mock.Mock()
        self.assertEqual(self.scaling.work_loop(client, "k", 10, stop), 0)
        client.call.assert_not_called()

    def test_worker_drain_fits_the_grace_period(self):
        worker = next(x for x in o.all_objects(IMAGE) if x["kind"] == "Deployment" and x["metadata"]["name"] == o.WORKER)
        grace = worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]
        self.assertLess(self.scaling.BLPOP_TIMEOUT_SECONDS + o.WORKER_PROCESS_MS / 1000, grace)

    def test_manifest_load_fits_the_workload_caps(self):
        self.assertLessEqual(o.LOAD_SECONDS, self.scaling.MAX_LOAD_SECONDS)
        self.assertLessEqual(o.LOAD_CONCURRENCY, self.scaling.MAX_LOAD_CONCURRENCY)
        self.assertLessEqual(o.PRODUCER_ITEMS, self.scaling.MAX_PRODUCE_ITEMS)


if __name__ == "__main__":
    unittest.main()

"""
DAY7: render-level tests against the REAL chart via `helm template`
(a pure local render - never a cluster), parsed with the repository's
own YAML loader (scripts/k8s_yaml.py - never PyYAML).

helm is a required project CLI (.claude/CLAUDE.md). These tests FAIL,
never skip, when helm is unavailable: a skipped render is not evidence
that a render is valid.

Covers: default and every stage's object inventory; that enabling the
candidate or changing the route never alters any stable object; the
candidate checksum reacting only to the candidate's own ConfigMap; and
every invalid candidate/routing state being rejected by BOTH the values
schema and (with schema validation skipped) the template guard.
"""

from __future__ import annotations

import atexit
import dataclasses
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import day7_build
import helm_check
import k8s_yaml
from validate_helm_chart import RenderExpectation, run_checks

CHART = str(REPO / "charts" / "maops-kubernetes-platform")
STAGES = REPO / "helm-values" / "day7"
DAY7_RELEASE = "maops-kubernetes-platform-day7"
NAMESPACE = "maops-platform"
CANDIDATE_KINDS = Counter({"ConfigMap": 1, "Deployment": 1, "Service": 1, "NetworkPolicy": 3, "AuthorizationPolicy": 1})

# DAY7 image contract: every Day 7 stage refuses to render without a
# verified build overlay; these renders use the SYNTHETIC build (tags
# that exist on no node) and expect exactly its pinned image refs.
SAMPLE_BUILD = day7_build.sample_build()
_OVERLAY_DIR = tempfile.mkdtemp(prefix="maops-day7-build-overlay-")
atexit.register(shutil.rmtree, _OVERLAY_DIR, True)
OVERLAY = str(Path(_OVERLAY_DIR) / "build.yaml")
Path(OVERLAY).write_text(day7_build.overlay_yaml(SAMPLE_BUILD))
STAGE_EXPECTATIONS = {
    name: dataclasses.replace(expectation, image_tags=tuple((i.repository, i.tag) for i in SAMPLE_BUILD.images))
    for name, expectation in helm_check.DAY7_STAGE_EXPECTATIONS.items()
}


def _helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", DAY7_RELEASE, CHART, "--namespace", NAMESPACE, *args], capture_output=True, text=True, timeout=60)


def render(*args: str) -> list[dict]:
    result = _helm(*args)
    if result.returncode != 0:
        raise AssertionError(f"helm template {args} failed: {result.stderr}")
    return [d for d in k8s_yaml.load_all(result.stdout) if d]


def stage(name: str) -> list[dict]:
    return render("-f", str(STAGES / name), "-f", OVERLAY)


def by_key(docs: list[dict]) -> dict:
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def _failed(findings) -> set[str]:
    return {f.name for f in findings if not f.ok}


class HelmAvailableTests(unittest.TestCase):
    def test_helm_is_installed(self):
        self.assertIsNotNone(shutil.which("helm"), "helm is a required project CLI - render tests must not be skipped")


class InventoryTests(unittest.TestCase):
    def test_default_render_is_the_unchanged_30_object_stable_inventory(self):
        docs = render()
        self.assertEqual(len(docs), 30)
        self.assertFalse([d for d in docs if "candidate" in d["metadata"]["name"]])
        route = by_key(docs)[("HTTPRoute", "maops-gateway-route")]
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"], [{"name": "maops-gateway", "port": 8080}])

    def test_every_stage_file_renders_to_its_declared_inventory(self):
        for name, expectation in STAGE_EXPECTATIONS.items():
            with self.subTest(stage=name):
                docs = stage(name)
                self.assertEqual(len(docs), expectation.expected_object_count())
                self.assertEqual(_failed(run_checks(docs, expectation)), set())

    def test_candidate_adds_exactly_seven_objects_by_kind(self):
        base, cand = by_key(stage("stable.yaml")), by_key(stage("green-prepared.yaml"))
        added = Counter(kind for kind, _ in set(cand) - set(base))
        self.assertEqual(added, CANDIDATE_KINDS)
        self.assertEqual(set(base) - set(cand), set(), "enabling the candidate must never remove an object")

    def test_candidate_without_networkpolicy_or_mesh_renders_fewer_policies(self):
        """The candidate-only policies are gated on the same switches as
        the stable ones - counted, not assumed."""
        docs = render("-f", str(STAGES / "green-prepared.yaml"), "-f", OVERLAY, "--set", "networkPolicy.enabled=false", "--set", "meshPolicy.enabled=false")
        names = {d["metadata"]["name"] for d in docs}
        self.assertIn("maops-gateway-candidate", names)
        self.assertNotIn("maops-gateway-candidate-authz", names)
        self.assertFalse([n for n in names if n.startswith("maops-allow-") and "candidate" in n])

    def test_stage_files_match_declared_expectations_exactly(self):
        self.assertEqual(sorted(p.name for p in STAGES.glob("*.yaml")), sorted(helm_check.DAY7_STAGE_EXPECTATIONS))


class StableObjectsUntouchedTests(unittest.TestCase):
    """Enabling the candidate or moving traffic must never change a
    stable object's rendered content (other than the HTTPRoute's
    backendRefs in candidate/weighted modes)."""

    def test_no_stable_object_changes_across_any_stage(self):
        base = by_key(stage("stable.yaml"))
        for name in helm_check.DAY7_STAGE_EXPECTATIONS:
            docs = by_key(stage(name))
            for key, doc in base.items():
                if key == ("HTTPRoute", "maops-gateway-route"):
                    continue
                with self.subTest(stage=name, obj=key):
                    self.assertEqual(docs[key], doc)

    def test_route_differs_only_in_backend_refs(self):
        base = by_key(stage("stable.yaml"))[("HTTPRoute", "maops-gateway-route")]
        for name in ("blue-green-cutover.yaml", "canary-90-10.yaml"):
            route = by_key(stage(name))[("HTTPRoute", "maops-gateway-route")]
            strip = lambda r: {**r, "spec": {**r["spec"], "rules": [{k: v for k, v in rule.items() if k != "backendRefs"} for rule in r["spec"]["rules"]]}}  # noqa: E731
            self.assertEqual(strip(route), strip(base))

    def test_stable_gateway_strategy_is_rollingupdate_in_every_stage(self):
        for name in helm_check.DAY7_STAGE_EXPECTATIONS:
            dep = by_key(stage(name))[("Deployment", "maops-gateway")]
            self.assertEqual(dep["spec"]["strategy"]["type"], "RollingUpdate")


class CandidateChecksumTests(unittest.TestCase):
    def _checksum(self, docs, name):
        return by_key(docs)[("Deployment", name)]["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    def test_candidate_message_change_changes_only_the_candidate_checksum(self):
        a, b = stage("recreate-serving.yaml"), stage("recreate-changed.yaml")
        self.assertNotEqual(self._checksum(a, "maops-gateway-candidate"), self._checksum(b, "maops-gateway-candidate"))
        for name in ("maops-gateway", "maops-app"):
            self.assertEqual(self._checksum(a, name), self._checksum(b, name))

    def test_stable_message_change_never_changes_the_candidate_checksum(self):
        a = stage("green-prepared.yaml")
        b = render("-f", str(STAGES / "green-prepared.yaml"), "-f", OVERLAY, "--set", "gateway.config.appMessage=stable-changed")
        self.assertEqual(self._checksum(a, "maops-gateway-candidate"), self._checksum(b, "maops-gateway-candidate"))
        self.assertNotEqual(self._checksum(a, "maops-gateway"), self._checksum(b, "maops-gateway"))

    def test_candidate_and_stable_checksums_are_distinct(self):
        docs = stage("green-prepared.yaml")
        self.assertNotEqual(self._checksum(docs, "maops-gateway"), self._checksum(docs, "maops-gateway-candidate"))


# (description, extra helm args, substring expected in stderr)
_INVALID_STATES = [
    ("candidate mode without candidate", ["--set", "routing.mode=candidate"], "candidate"),
    ("weighted mode without candidate", ["--set", "routing.mode=weighted", "--set", "routing.stableWeight=90", "--set", "routing.candidateWeight=10"], "candidate"),
    ("weighted without weights", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted"], "Weight"),
    ("weighted with only one weight", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=100"], "Weight"),
    ("weights sum to 99", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=90", "--set", "routing.candidateWeight=9"], "100"),
    ("weights sum to 101", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=91", "--set", "routing.candidateWeight=10"], "100"),
    ("zero candidate weight", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=100", "--set", "routing.candidateWeight=0"], "Weight"),
    ("negative weight", ["--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=110", "--set", "routing.candidateWeight=-10"], "Weight"),
    ("weights in stable mode", ["--set", "routing.stableWeight=90", "--set", "routing.candidateWeight=10"], "/routing"),
    ("weights in candidate mode", ["--set", "candidate.enabled=true", "--set", "routing.mode=candidate", "--set", "routing.stableWeight=0", "--set", "routing.candidateWeight=100"], "/routing"),
    ("unknown mode", ["--set", "routing.mode=bluegreen"], "mode"),
    ("empty mode", ["--set", "routing.mode="], "mode"),
    ("unknown routing key", ["--set", "routing.weights.stable=90"], "routing"),
    ("unknown candidate strategy", ["--set", "candidate.enabled=true", "--set", "candidate.strategy=BlueGreen"], "strategy"),
    ("zero candidate replicas", ["--set", "candidate.enabled=true", "--set", "candidate.replicas=0"], "replicas"),
    ("candidate message equals stable", ["--set", "candidate.enabled=true", "--set", "candidate.config.appMessage=Hello from the MAOps Kubernetes Gateway (Day 7 stable)"], "appMessage"),
    ("failReadiness without candidate", ["--set", "candidate.faultInjection.failReadiness=true"], "/candidate/enabled"),
    ("failReadiness with candidate route", ["--set", "candidate.enabled=true", "--set", "candidate.faultInjection.failReadiness=true", "--set", "routing.mode=candidate"], "mode"),
]


def _values_file(content: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


class InvalidStateTests(unittest.TestCase):
    def test_schema_rejects_every_invalid_state(self):
        for desc, args, needle in _INVALID_STATES:
            with self.subTest(desc):
                result = _helm(*args)
                self.assertNotEqual(result.returncode, 0, f"{desc} rendered but must be rejected")
                self.assertIn(needle, result.stderr)

    def test_template_guard_rejects_every_invalid_state_without_the_schema(self):
        """--skip-schema-validation exists; the template guard must still
        refuse (the schema-only key checks - unknown routing key, unknown
        strategy, zero replicas - are exempt: they are schema contracts)."""
        schema_only = {"unknown routing key", "unknown candidate strategy", "zero candidate replicas"}
        for desc, args, _ in _INVALID_STATES:
            if desc in schema_only:
                continue
            with self.subTest(desc):
                result = _helm("--skip-schema-validation", *args)
                self.assertNotEqual(result.returncode, 0, f"{desc} rendered with schema validation skipped")
                self.assertIn("execution error", result.stderr)

    def test_non_integer_weights_from_a_values_file_rejected_by_schema_and_template(self):
        path = _values_file("candidate:\n  enabled: true\nrouting:\n  mode: weighted\n  stableWeight: 90.5\n  candidateWeight: 9.5\n")
        try:
            schema = _helm("-f", path)
            self.assertNotEqual(schema.returncode, 0)
            self.assertIn("want integer", schema.stderr)
            template = _helm("--skip-schema-validation", "-f", path)
            self.assertNotEqual(template.returncode, 0)
            self.assertIn("must be an integer", template.stderr)
        finally:
            Path(path).unlink()

    def test_valid_weighted_split_renders(self):
        docs = render("--set", "candidate.enabled=true", "--set", "routing.mode=weighted", "--set", "routing.stableWeight=50", "--set", "routing.candidateWeight=50")
        refs = by_key(docs)[("HTTPRoute", "maops-gateway-route")]["spec"]["rules"][0]["backendRefs"]
        self.assertEqual([r["weight"] for r in refs], [50, 50])


class ValidatorCatchesRealRenderMutationsTests(unittest.TestCase):
    """Negative cases built from REAL renders, each mutated in one
    place, proving the Day 7 validator rejects exactly that defect."""

    def _run(self, stage_name, mutate, expectation=None):
        docs = stage(stage_name)
        mutate(by_key(docs))
        return _failed(run_checks(docs, expectation or STAGE_EXPECTATIONS[stage_name]))

    def test_wrong_weight(self):
        def m(d):
            d[("HTTPRoute", "maops-gateway-route")]["spec"]["rules"][0]["backendRefs"][0]["weight"] = 80
        failed = self._run("canary-90-10.yaml", m)
        self.assertIn("gateway_api.httproute.backend_ref_exact", failed)
        self.assertIn("gateway_api.httproute.weights_valid", failed)

    def test_wrong_backend_port(self):
        def m(d):
            d[("HTTPRoute", "maops-gateway-route")]["spec"]["rules"][0]["backendRefs"][1]["port"] = 8081
        failed = self._run("canary-90-10.yaml", m)
        self.assertIn("gateway_api.httproute.backend[maops-gateway-candidate].port_matches_service", failed)

    def test_unexpected_backend_service(self):
        def m(d):
            d[("HTTPRoute", "maops-gateway-route")]["spec"]["rules"][0]["backendRefs"][0]["name"] = "maops-app"
        self.assertIn("gateway_api.httproute.backend_ref_exact", self._run("blue-green-cutover.yaml", m))

    def test_missing_candidate_backend(self):
        def m(d):
            refs = d[("HTTPRoute", "maops-gateway-route")]["spec"]["rules"][0]["backendRefs"]
            del refs[1]
        self.assertIn("gateway_api.httproute.backend_ref_exact", self._run("canary-90-10.yaml", m))

    def test_overlapping_deployment_selector(self):
        def m(d):
            dep = d[("Deployment", "maops-gateway-candidate")]
            dep["spec"]["selector"]["matchLabels"].pop("app.kubernetes.io/component")
        failed = self._run("green-prepared.yaml", m)
        self.assertIn("isolation.deployment_selectors_disjoint", failed)

    def test_candidate_service_selecting_stable_pods(self):
        def m(d):
            d[("Service", "maops-gateway-candidate")]["spec"]["selector"]["app.kubernetes.io/component"] = "gateway"
        failed = self._run("green-prepared.yaml", m)
        self.assertIn("isolation.service_selectors_disjoint", failed)
        self.assertIn("isolation.candidate_selectors_reject_stable_pods", failed)

    def test_stable_pdb_broadened_to_candidate(self):
        def m(d):
            d[("PodDisruptionBudget", "maops-gateway-pdb")]["spec"]["selector"]["matchLabels"].pop("app.kubernetes.io/component")
        failed = self._run("green-prepared.yaml", m)
        self.assertIn("isolation.stable_pdb_cannot_select_candidate", failed)
        self.assertIn("isolation.no_pdb_selects_candidate", failed)

    def test_missing_candidate_authorizationpolicy(self):
        docs = stage("green-prepared.yaml")
        docs = [x for x in docs if x["metadata"]["name"] != "maops-gateway-candidate-authz"]
        failed = _failed(run_checks(docs, STAGE_EXPECTATIONS["green-prepared.yaml"]))
        self.assertIn("mesh.authz.coverage.candidate", failed)

    def test_missing_candidate_ingress_networkpolicy(self):
        docs = [x for x in stage("green-prepared.yaml") if x["metadata"]["name"] != "maops-allow-gateway-candidate-ingress-from-istio-ingress-gateway"]
        failed = _failed(run_checks(docs, STAGE_EXPECTATIONS["green-prepared.yaml"]))
        self.assertIn("networkpolicy.coverage.candidate_paths_match_stable", failed)

    def test_candidate_egress_widened_to_state(self):
        def m(d):
            np = d[("NetworkPolicy", "maops-allow-gateway-candidate-egress-to-app")]
            np["spec"]["egress"][0]["to"].append({"podSelector": {"matchLabels": {"app.kubernetes.io/component": "state"}}})
        self.assertIn("networkpolicy.coverage.candidate_paths_match_stable", self._run("green-prepared.yaml", m))

    def test_candidate_policy_selecting_state(self):
        def m(d):
            np = d[("NetworkPolicy", "maops-allow-app-ingress-from-gateway-candidate")]
            np["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] = "state"
        self.assertIn("networkpolicy.coverage.candidate_policies_never_select_state_or_stable", self._run("green-prepared.yaml", m))

    def test_candidate_security_context_drift(self):
        for field, value, check in (
            ("runAsUser", 0, "candidate.parity.pod.securityContext"),
            ("fsGroup", None, "candidate.parity.pod.securityContext"),
        ):
            def m(d, field=field, value=value):
                sc = d[("Deployment", "maops-gateway-candidate")]["spec"]["template"]["spec"]["securityContext"]
                if value is None:
                    sc.pop(field)
                else:
                    sc[field] = value
            with self.subTest(field=field):
                self.assertIn(check, self._run("green-prepared.yaml", m))

    def test_candidate_secret_mount_drift(self):
        def m(d):
            d[("Deployment", "maops-gateway-candidate")]["spec"]["template"]["spec"]["volumes"][0]["secret"]["defaultMode"] = 420
        self.assertIn("candidate.parity.pod.volumes", self._run("green-prepared.yaml", m))

    def test_candidate_own_serviceaccount_is_rejected_as_parity_drift(self):
        def m(d):
            d[("Deployment", "maops-gateway-candidate")]["spec"]["template"]["spec"]["serviceAccountName"] = "maops-gateway-candidate"
        failed = self._run("green-prepared.yaml", m)
        self.assertIn("candidate.shares_gateway_serviceaccount", failed)

    def test_candidate_message_not_distinct(self):
        def m(d):
            d[("ConfigMap", "maops-gateway-candidate-config")]["data"]["APP_MESSAGE"] = d[("ConfigMap", "maops-gateway-config")]["data"]["APP_MESSAGE"]
        self.assertIn("candidate.configmap.distinct_message", self._run("green-prepared.yaml", m))

    def test_candidate_config_drift_beyond_message(self):
        def m(d):
            d[("ConfigMap", "maops-gateway-candidate-config")]["data"]["BACKEND_TIMEOUT_SECONDS"] = "30"
        self.assertIn("candidate.configmap.only_message_differs", self._run("green-prepared.yaml", m))

    def test_recreate_with_rollingupdate_block(self):
        def m(d):
            d[("Deployment", "maops-gateway-candidate")]["spec"]["strategy"]["rollingUpdate"] = {"maxUnavailable": 1}
        self.assertIn("candidate.strategy_recreate_without_rollingupdate", self._run("recreate-serving.yaml", m))

    def test_stable_gateway_switched_to_recreate(self):
        def m(d):
            d[("Deployment", "maops-gateway")]["spec"]["strategy"] = {"type": "Recreate"}
        self.assertIn("strategy.stable_gateway_stays_rollingupdate", self._run("recreate-serving.yaml", m))

    def test_unexpected_fault_injection(self):
        docs = stage("candidate-unready.yaml")
        failed = _failed(run_checks(docs, STAGE_EXPECTATIONS["green-prepared.yaml"]))
        self.assertIn("candidate.readiness_probe_path", failed)

    def test_candidate_objects_present_when_expected_disabled(self):
        docs = stage("green-prepared.yaml")
        failed = _failed(run_checks(docs, RenderExpectation(instance=DAY7_RELEASE)))
        self.assertIn("candidate.absent_when_disabled", failed)
        self.assertIn("inventory.total_object_count", failed)


if __name__ == "__main__":
    unittest.main()

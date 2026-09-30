"""
DAY7: Docker/Kubernetes-free tests pinning the Day 7 Makefile contract
by reading the Makefile text (and, for variable resolution only, `make
-n`, which prints recipes without running them):

  - the exact relative order of `day7-check`, `day7-final-gate` and
    `day7-resume-check`;
  - every cluster-touching step goes through DAY7_MAKE (the Day 7
    profile) and nothing in the Day 7 targets names the Day 6 cluster,
    its kind config or its host port;
  - Day 6-only mutating targets refuse the Day 7 profile;
  - `ci-check` stays cluster-free and `day6-check` is unchanged.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAKEFILE = (REPO / "Makefile").read_text()
_INVOCATION = re.compile(r"\$\((DAY7_MAKE|MAKE)\)\s+([a-zA-Z0-9_-]+)")


def _recipe(target: str) -> str:
    match = re.search(rf"^{re.escape(target)}:.*\n((?:[ \t]+.*\n?)*)", MAKEFILE, re.MULTILINE)
    assert match is not None, f"no {target!r} target"
    return match.group(1)


def _steps(target: str) -> list[tuple[str, str]]:
    return _INVOCATION.findall(_recipe(target))


def _names(target: str) -> list[str]:
    return [name for _, name in _steps(target)]


def _resolve(variable: str, *assignments: str) -> str:
    """Resolves a Makefile variable as a REAL (non -n) invocation would,
    by evaluating a throwaway echo-only target - runs nothing else."""
    result = subprocess.run(
        ["make", "--no-print-directory", f"--eval=__resolve__: ; @echo '$({variable})'", "__resolve__", *assignments],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _dry_run(*args: str) -> str:
    result = subprocess.run(["make", "-n", *args], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


CLUSTER_STEPS = {
    "image-build", "cluster-create", "gateway-api-install", "cni-install", "cni-status", "context-check",
    "mesh-install", "mesh-status", "image-load", "storage-bootstrap", "storage-hardening-check",
    "namespace-apply", "secret-bootstrap", "gateway-apply", "ambient-workload-check", "rollout-check",
    "gateway-check", "mesh-check", "networkpolicy-check", "state-check", "final-state-check",
}
DAY6_ONLY = {"deploy", "dependency-check", "scaling-check", "rolling-update-check", "pdb-check", "persistence-check", "retention-check", "helm-lifecycle-check", "controller-check"}


class Day7CheckOrderTests(unittest.TestCase):
    def setUp(self):
        self.seq = _names("day7-check")

    def before(self, a, b):
        self.assertLess(self.seq.index(a), self.seq.index(b), f"{a} must precede {b} in {self.seq}")

    def test_exact_sequence(self):
        self.assertEqual(self.seq, [
            "tool-check", "test", "version-check", "manifest-check", "helm-lint", "helm-template", "helm-check",
            "day7-preflight",
            "image-build", "day7-image-verify-local", "day7-build-record",
            "cluster-create", "gateway-api-install", "cni-install", "day7-nodes-ready", "cni-status", "context-check",
            "mesh-install", "mesh-status", "image-load", "day7-image-load", "day7-image-verify-nodes",
            "storage-bootstrap", "storage-hardening-check",
            "namespace-apply", "secret-bootstrap", "gateway-apply",
            "day7-deploy", "ambient-workload-check", "rollout-check", "day7-running-images", "gateway-check", "mesh-check", "networkpolicy-check",
            "day7-baseline-init", "state-check", "day7-baseline",
            "day7-blue-green", "day7-stable-check", "day7-canary", "day7-stable-check", "day7-recreate", "day7-stable-check",
            "day7-final-gate",
        ])

    def test_static_checks_and_preflight_precede_any_cluster_work(self):
        for static in ("test", "helm-check", "day7-preflight"):
            self.before(static, "image-build")
            self.before(static, "cluster-create")

    def test_cni_context_mesh_precede_images_and_deploy(self):
        for step in ("cni-status", "context-check", "mesh-status"):
            self.before(step, "image-load")
            self.before(step, "day7-deploy")
        self.before("cni-install", "cni-status")
        self.before("cni-status", "context-check")

    def test_listeners_checked_directly_after_deploy_before_rollout(self):
        i = self.seq.index("ambient-workload-check")
        self.assertEqual(self.seq[i - 1], "day7-deploy")
        self.assertEqual(self.seq[i + 1], "rollout-check")

    def test_baselines_captured_once_before_any_experiment(self):
        self.assertEqual(self.seq.count("day7-baseline"), 1)
        self.assertEqual(self.seq.count("day7-baseline-init"), 1)
        self.assertEqual(self.seq.count("state-check"), 1)
        self.before("day7-baseline-init", "state-check")
        self.before("state-check", "day7-baseline")
        for exp in ("day7-blue-green", "day7-canary", "day7-recreate"):
            self.before("day7-baseline", exp)

    def test_final_gate_is_last(self):
        self.assertEqual(self.seq[-1], "day7-final-gate")

    def test_cluster_steps_use_the_day7_profile(self):
        for mk, name in _steps("day7-check"):
            if name in CLUSTER_STEPS:
                with self.subTest(step=name):
                    self.assertEqual(mk, "DAY7_MAKE")

    def test_no_day6_only_or_day6_deploy_step(self):
        self.assertFalse(set(self.seq) & DAY6_ONLY, "day7-check must never run Day 6-only targets")

    def test_whole_sequence_under_one_day7_lock(self):
        recipe = _recipe("day7-check")
        self.assertEqual(recipe.count("$(DAY7_LOCK)"), 1)
        self.assertNotIn("$(DAY6_LOCK)", recipe)
        self.assertIn(" && \\", recipe)


class Day7FinalGateTests(unittest.TestCase):
    def test_order(self):
        self.assertEqual(_names("day7-final-gate"), [
            "cni-status", "context-check", "mesh-status", "ambient-workload-check", "rollout-check", "day7-running-images",
            "gateway-check", "mesh-check", "networkpolicy-check", "final-state-check", "day7-final-state-check",
        ])

    def test_every_cluster_step_uses_day7_profile_and_chain_fails_fast(self):
        for mk, name in _steps("day7-final-gate"):
            if name in CLUSTER_STEPS:
                self.assertEqual(mk, "DAY7_MAKE", name)
        self.assertIn(" && \\", _recipe("day7-final-gate"))

    def test_resume_check_trusts_rollout_only_after_platform_and_listeners(self):
        seq = _names("day7-resume-check")
        self.assertEqual(seq, ["day7-nodes-ready", "cni-status", "context-check", "mesh-status", "ambient-workload-check", "rollout-check", "day7-running-images", "gateway-check"])
        self.assertTrue(all(mk == "DAY7_MAKE" for mk, name in _steps("day7-resume-check") if name not in ("day7-nodes-ready", "day7-running-images")))


class Day7IsolationTests(unittest.TestCase):
    DAY7_TARGETS = ("day7-preflight", "day7-cluster-create", "day7-cluster-delete", "day7-deploy", "day7-baseline-init",
                    "day7-baseline", "day7-blue-green", "day7-canary", "day7-recreate", "day7-final-state-check",
                    "day7-resume-check", "day7-final-gate", "day7-check", "day7-image-verify-local",
                    "day7-image-verify-nodes", "day7-stable-check", "day7-history-audit", "day7-plan", "day7-nodes-ready",
                    "day7-build-record", "day7-image-load", "day7-running-images")

    def test_no_day7_target_names_day6_identities(self):
        for target in self.DAY7_TARGETS:
            recipe = _recipe(target)
            for forbidden in ("maops-k8s-day6", "cluster-day6.yaml", "18080", "day6_lock", "platform-day6", "DAY6_RUN_ID"):
                with self.subTest(target=target, forbidden=forbidden):
                    self.assertNotIn(forbidden, recipe)

    def test_day7_variables(self):
        for line in ("DAY7_CLUSTER_NAME := maops-k8s-day7", "DAY7_HELM_RELEASE := maops-kubernetes-platform-day7",
                     "DAY7_MAKE := $(MAKE) CLUSTER_NAME=$(DAY7_CLUSTER_NAME) KUBECONFIG_PATH=$(DAY7_KUBECONFIG_PATH)",
                     "DAY7_ENV := MAOPS_CLUSTER_PROFILE=day7 KUBECONFIG_PATH=$(DAY7_KUBECONFIG_PATH)"):
            self.assertIn(line, MAKEFILE)

    def test_day7_scripts_run_under_day7_env(self):
        for target in ("day7-baseline", "day7-blue-green", "day7-canary", "day7-recreate", "day7-final-state-check", "day7-stable-check", "day7-running-images", "day7-image-load"):
            self.assertIn("$(DAY7_ENV) python3 scripts/", _recipe(target))

    def test_day7_deploy_is_explicit_stable_stage(self):
        recipe = _recipe("day7-deploy")
        self.assertIn("--reset-values", recipe)
        self.assertIn("-f $(DAY7_STAGE_DIR)/stable.yaml", recipe)
        self.assertNotIn("--reuse-values", recipe)
        self.assertNotIn("--set", recipe)
        self.assertIn("--kube-context $(DAY7_KCONTEXT)", recipe)
        self.assertIn("--kubeconfig $(DAY7_KUBECONFIG_PATH)", recipe)
        self.assertRegex(recipe, r"--timeout \d+s")

    def test_evidence_root_is_outside_tmp_and_the_repo(self):
        match = re.search(r"^DAY7_BASELINE_ROOT \?= (.+)$", MAKEFILE, re.MULTILINE)
        self.assertIsNotNone(match)
        root = match.group(1).strip()
        self.assertTrue(root.startswith("$(HOME)/"), root)
        self.assertNotIn("/tmp", root)
        for derived in ("DAY7_BASELINE_DIR := $(DAY7_BASELINE_ROOT)/$(DAY7_RUN_ID)",
                        "DAY7_SUITE_BASELINE_PATH := $(DAY7_BASELINE_DIR)/suite-baseline.json",
                        "DAY7_STRATEGY_BASELINE_PATH := $(DAY7_BASELINE_DIR)/strategy-baseline.json"):
            self.assertIn(derived, MAKEFILE)

    def test_profile_is_derived_from_cluster_name_with_a_fail_closed_guard(self):
        self.assertIn("CLUSTER_PROFILE := $(if $(filter maops-k8s-day7,$(CLUSTER_NAME)),day7,day6)", MAKEFILE)
        self.assertIn("$(error CLUSTER_NAME=$(CLUSTER_NAME) is not a supported cluster", MAKEFILE)
        self.assertIn("export MAOPS_CLUSTER_PROFILE", MAKEFILE)

    def test_day6_only_targets_refuse_the_day7_profile(self):
        for target in DAY6_ONLY:
            with self.subTest(target=target):
                self.assertTrue(_recipe(target).lstrip().startswith("$(require_day6_profile)"))

    def test_resolved_day7_cluster_commands(self):
        create = _dry_run("day7-cluster-create")
        self.assertIn("CLUSTER_NAME=maops-k8s-day7", create)
        inner = _dry_run("CLUSTER_NAME=maops-k8s-day7", "KUBECONFIG_PATH=/nonexistent/day7.config", "cluster-create", "cluster-delete")
        self.assertIn("kind create cluster --name maops-k8s-day7 --config kind/cluster-day7.yaml --kubeconfig /nonexistent/day7.config", inner)
        self.assertIn("kind delete cluster --name maops-k8s-day7", inner)
        self.assertEqual(_resolve("DAY6_LOCK", "CLUSTER_NAME=maops-k8s-day7"), "python3 scripts/day7_lock.py run --")
        self.assertNotIn("maops-k8s-day6", inner)

    def test_default_resolution_is_still_day6(self):
        out = _dry_run("cluster-create", "deploy", "helm-template")
        self.assertIn("--name maops-k8s-day6 --config kind/cluster-day6.yaml", out)
        self.assertEqual(_resolve("DAY6_LOCK"), "python3 scripts/day6_lock.py run --")
        self.assertIn("helm upgrade --install maops-kubernetes-platform-day6", out)
        self.assertIn("helm template maops-kubernetes-platform-day6", out)

    def test_unsupported_cluster_name_rejected_at_parse_time(self):
        result = subprocess.run(["make", "-n", "CLUSTER_NAME=maops-k8s-day5", "help"], cwd=REPO, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a supported cluster", result.stderr)

    def test_day6_only_guard_refuses_before_any_command(self):
        """Runs ONLY the guard: under the Day 7 profile the first recipe
        line exits non-zero, so nothing after it (lock, kubectl, helm)
        ever starts. A nonexistent kubeconfig makes any accidental later
        step unable to reach a cluster anyway."""
        result = subprocess.run(
            ["make", "CLUSTER_NAME=maops-k8s-day7", "KUBECONFIG_PATH=/nonexistent/day7.config", "scaling-check"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Day 6-only target and refuses to run against maops-k8s-day7", result.stderr)
        self.assertNotIn("day7_lock", result.stdout)


class Day7ImageContractMakefileTests(unittest.TestCase):
    """DAY7 image contract: the build is recorded before any cluster work,
    its pinned tags are loaded and verified per node before deploy, the
    deploy carries it as a second values file, and the running-image gate
    follows the stable deploy, every restart check, and the final gate."""

    def test_deploy_carries_the_verified_build_overlay(self):
        recipe = _recipe("day7-deploy")
        self.assertIn("build_values=$$(python3 scripts/day7_build.py values-path) &&", recipe)
        self.assertIn("-f $(DAY7_STAGE_DIR)/stable.yaml", recipe)
        self.assertIn('-f "$$build_values"', recipe)
        self.assertLess(recipe.index("stable.yaml"), recipe.index('"$$build_values"'), "the build overlay must be the LAST values file")
        for forbidden in ("--set", "--reuse-values", "rollout restart", "kubectl patch"):
            self.assertNotIn(forbidden, recipe)

    def test_build_recorded_loaded_and_verified_before_deploy(self):
        seq = _names("day7-check")
        order = ["day7-image-verify-local", "day7-build-record", "image-load", "day7-image-load", "day7-image-verify-nodes", "day7-deploy"]
        self.assertEqual([s for s in seq if s in order], order)
        self.assertLess(seq.index("day7-build-record"), seq.index("cluster-create"))

    def test_running_images_follow_stable_deploy_and_precede_baseline(self):
        seq = _names("day7-check")
        self.assertLess(seq.index("day7-deploy"), seq.index("day7-running-images"))
        self.assertLess(seq.index("rollout-check"), seq.index("day7-running-images"))
        self.assertLess(seq.index("day7-running-images"), seq.index("day7-baseline"))

    def test_final_gate_checks_running_images_before_the_final_state(self):
        gate = _names("day7-final-gate")
        self.assertLess(gate.index("day7-running-images"), gate.index("day7-final-state-check"))

    def test_image_targets_are_scoped_to_day7(self):
        self.assertEqual(_recipe("day7-build-record").strip(), "$(DAY7_LOCK) python3 scripts/day7_build.py record")
        # The load reaches the cluster, so it must run under the Day 7 profile
        # (load_into_kind refuses otherwise - run 5750593984c0... stopped
        # here when this recipe lacked $(DAY7_ENV)).
        self.assertEqual(_recipe("day7-image-load").strip(), "$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_build.py load-kind")
        self.assertIn("DAY7_BUILD_ROOT ?= $(HOME)/.local/state/maops-kubernetes-platform/day7-builds", MAKEFILE)

    def test_helm_lint_never_lints_a_day7_stage_without_a_build(self):
        recipe = _recipe("helm-lint")
        self.assertIn("python3 scripts/day7_build.py sample-overlay", recipe)
        self.assertIn('helm lint --quiet $(CHART) -f "$$f" -f "$$overlay"', recipe)


class ArchitectureDocDay7SequenceTests(unittest.TestCase):
    def test_documented_day7_sequence_matches_the_makefile(self):
        doc = (REPO / "docs" / "architecture.md").read_text()
        match = re.search(r"```text\nday7-check:\n(.*?)```", doc, re.DOTALL)
        self.assertIsNotNone(match, "docs/architecture.md must document the day7-check order")
        documented = re.findall(r"[a-z0-9][a-z0-9-]*[a-z0-9]", match.group(1))
        self.assertEqual(documented, _names("day7-check"))


class CiAndDay6UnchangedTests(unittest.TestCase):
    def test_ci_check_is_cluster_free(self):
        recipe = _recipe("ci-check")
        self.assertEqual(_names("ci-check"), ["test", "version-check", "manifest-check", "helm-lint", "helm-template", "helm-check"])
        for forbidden in ("day7", "DAY7", "kind ", "docker", "cluster-create", "LOCK"):
            self.assertNotIn(forbidden, recipe)

    def test_helm_lint_covers_every_day7_stage(self):
        self.assertIn('for f in $(DAY7_STAGE_DIR)/*.yaml; do', _recipe("helm-lint"))

    def test_day6_check_has_no_day7_step(self):
        self.assertFalse([s for s in _names("day6-check") if s.startswith("day7")])
        self.assertNotIn("DAY7", _recipe("day6-check"))


if __name__ == "__main__":
    unittest.main()

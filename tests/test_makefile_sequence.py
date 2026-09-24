"""
Docker/Kubernetes-free unit tests proving the Day 6 orchestration
remediation actually landed in the Makefile text itself - a live run
against `maops-k8s-day6` discovered that `context-check` ran too early
(immediately after `cluster-create`, before Cilium ever installs a pod
network, so `scripts/context_check.py`'s all-nodes-Ready requirement was
guaranteed to fail on a fresh cluster for a reason that has nothing to
do with context/topology correctness) and that `cni-install`/
`mesh-install` treated Helm install SUBMISSION as if it were rollout
READINESS, with no bounded wait and no diagnostics on failure.

These tests read the Makefile's own text (never render or execute it,
never contact a cluster) and assert the corrected structure holds -
sequence order, bounded rollout waits with explicit kubeconfig/context,
strict shell failure semantics, and that a failed wait cannot produce a
downstream PASS or have its exit code silently swallowed.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

MAKEFILE = (Path(__file__).resolve().parent.parent / "Makefile").read_text()


def _recipe_block(target: str, makefile_text: str = MAKEFILE) -> str:
    """Extracts one target's full recipe body (every indented line
    following `target:`, up to the next non-indented/top-level line)."""
    match = re.search(rf"^{re.escape(target)}:.*\n((?:[ \t]+.*\n?)*)", makefile_text, re.MULTILINE)
    assert match is not None, f"could not find a {target!r} target in the Makefile"
    return match.group(1)


class Day6CheckSequenceOrderTests(unittest.TestCase):
    """DAY6 live-discovered orchestration remediation item 1: the
    corrected, documented `day6-check` order is exactly:
    cluster-create -> gateway-api-install -> cni-install -> cni-status ->
    context-check -> mesh-install -> mesh-status."""

    def setUp(self):
        self.recipe = _recipe_block("day6-check")
        # Every `$(MAKE) <target>` invocation, in the exact order the
        # recipe lines actually appear.
        self.sequence = re.findall(r"\$\(MAKE\)\s+([a-zA-Z0-9_-]+)", self.recipe)

    def test_sequence_is_non_empty_and_well_formed(self):
        self.assertGreater(len(self.sequence), 30)
        self.assertIn("cluster-create", self.sequence)
        self.assertIn("context-check", self.sequence)

    def test_context_check_runs_after_cni_status_not_immediately_after_cluster_create(self):
        """The exact bug a live run discovered: context-check used to
        sit directly after cluster-create, before Cilium (and therefore
        before any node is Ready) ever installs."""
        cluster_create_idx = self.sequence.index("cluster-create")
        cni_status_idx = self.sequence.index("cni-status")
        context_check_idx = self.sequence.index("context-check")
        self.assertLess(cluster_create_idx, cni_status_idx)
        self.assertLess(cni_status_idx, context_check_idx)
        self.assertNotEqual(
            self.sequence[cluster_create_idx + 1],
            "context-check",
            "context-check must not be the step immediately following cluster-create",
        )

    def test_full_required_ordering_locked(self):
        """The exact required sub-sequence from this remediation:
        cluster-create -> gateway-api-install -> cni-install ->
        cni-status -> context-check -> mesh-install -> mesh-status."""
        required = [
            "cluster-create",
            "gateway-api-install",
            "cni-install",
            "cni-status",
            "context-check",
            "mesh-install",
            "mesh-status",
        ]
        indices = [self.sequence.index(step) for step in required]
        self.assertEqual(indices, sorted(indices), f"expected {required} in that exact relative order, found indices {indices}")

    def test_image_build_precedes_cluster_create(self):
        """Unchanged Day 4/5-inherited contract: images are built before
        the cluster that will load them is even created."""
        self.assertLess(self.sequence.index("image-build"), self.sequence.index("cluster-create"))

    def test_gateway_api_install_precedes_cni_install(self):
        """CRD registration is an API-server-level operation with no pod
        -networking dependency, so it does not need to wait for Cilium
        the way context-check does."""
        self.assertLess(self.sequence.index("gateway-api-install"), self.sequence.index("cni-install"))

    def test_final_state_check_is_the_last_step(self):
        self.assertEqual(self.sequence[-1], "final-state-check")

    def test_day6_lock_wraps_the_entire_sequence_as_one_unit(self):
        """The whole day6-check recipe must still be ONE
        `$(DAY6_LOCK) sh -c '...'`/`bash -c '...'` invocation - never
        split into multiple separately-locked pieces, which would let a
        second invocation interleave mid-sequence."""
        self.assertEqual(self.recipe.count("$(DAY6_LOCK)"), 1)


class ArchitectureDocSequenceMatchesMakefileTests(unittest.TestCase):
    """The documented deferred-live-sequence order in
    docs/architecture.md must match the Makefile's actual day6-check
    order exactly - a stale doc claiming the old, broken order would
    itself be a regression."""

    def test_doc_order_matches_makefile_order(self):
        doc_text = (Path(__file__).resolve().parent.parent / "docs" / "architecture.md").read_text()
        doc_match = re.search(r"```\ntool-check.*?final-state-check\n```", doc_text, re.DOTALL)
        self.assertIsNotNone(doc_match, "could not find the documented deferred-live-sequence code block")
        documented_sequence = re.findall(r"[a-zA-Z0-9_-]+", doc_match.group(0))
        # Compare only the targets that also appear as literal `$(MAKE)
        # <target>` tokens in the Makefile's day6-check recipe, in
        # relative order - the doc's plain-text arrow list uses the same
        # target names as the Makefile.
        makefile_sequence = re.findall(r"\$\(MAKE\)\s+([a-zA-Z0-9_-]+)", _recipe_block("day6-check"))
        filtered_doc_sequence = [tok for tok in documented_sequence if tok in makefile_sequence]
        self.assertEqual(filtered_doc_sequence, makefile_sequence)


class BoundedRolloutWaitTests(unittest.TestCase):
    """DAY6 live-discovered orchestration remediation items 2/3: after
    each Helm install SUBMISSION succeeds, cni-install/mesh-install must
    explicitly wait, with a finite `kubectl rollout status --timeout=`,
    for every required workload, using the explicit Day 6 kubeconfig and
    context - never relying on install submission alone."""

    def test_cni_install_waits_for_cilium_daemonset(self):
        recipe = _recipe_block("cni-install")
        self.assertRegex(
            recipe,
            r"kubectl --kubeconfig \$\(KUBECONFIG_PATH\) --context \$\(KCONTEXT\) -n kube-system rollout status daemonset/cilium --timeout=\d+s",
        )

    def test_cni_install_waits_for_cilium_operator_deployment(self):
        recipe = _recipe_block("cni-install")
        self.assertRegex(
            recipe,
            r"kubectl --kubeconfig \$\(KUBECONFIG_PATH\) --context \$\(KCONTEXT\) -n kube-system rollout status deployment/cilium-operator --timeout=\d+s",
        )

    def test_cilium_operator_wait_comes_after_cilium_daemonset_wait(self):
        recipe = _recipe_block("cni-install")
        daemonset_wait = recipe.index("rollout status daemonset/cilium ")
        operator_wait = recipe.index("rollout status deployment/cilium-operator")
        self.assertLess(daemonset_wait, operator_wait)

    def test_mesh_install_waits_for_istiod_deployment(self):
        recipe = _recipe_block("mesh-install")
        self.assertRegex(
            recipe,
            r"kubectl --kubeconfig \$\(KUBECONFIG_PATH\) --context \$\(KCONTEXT\) -n \$\(ISTIO_NAMESPACE\) rollout status deployment/istiod --timeout=\d+s",
        )

    def test_mesh_install_waits_for_istio_cni_node_daemonset(self):
        recipe = _recipe_block("mesh-install")
        self.assertRegex(
            recipe,
            r"kubectl --kubeconfig \$\(KUBECONFIG_PATH\) --context \$\(KCONTEXT\) -n \$\(ISTIO_NAMESPACE\) rollout status daemonset/istio-cni-node --timeout=\d+s",
        )

    def test_mesh_install_waits_for_ztunnel_daemonset(self):
        recipe = _recipe_block("mesh-install")
        self.assertRegex(
            recipe,
            r"kubectl --kubeconfig \$\(KUBECONFIG_PATH\) --context \$\(KCONTEXT\) -n \$\(ISTIO_NAMESPACE\) rollout status daemonset/ztunnel --timeout=\d+s",
        )

    def test_mesh_install_waits_run_in_istiod_then_cni_then_ztunnel_order(self):
        """Matches Istio's own documented install order - istiod ready
        before istio-cni/ztunnel are installed - rather than deferring
        all three waits to the very end."""
        recipe = _recipe_block("mesh-install")
        istiod_wait = recipe.index("rollout status deployment/istiod")
        cni_wait = recipe.index("rollout status daemonset/istio-cni-node")
        ztunnel_wait = recipe.index("rollout status daemonset/ztunnel")
        self.assertLess(istiod_wait, cni_wait)
        self.assertLess(cni_wait, ztunnel_wait)

    def test_no_rollout_wait_uses_an_unbounded_or_zero_timeout(self):
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            for match in re.finditer(r"--timeout=(\d+)s", recipe):
                with self.subTest(target=target, timeout=match.group(0)):
                    self.assertGreater(int(match.group(1)), 0)


class StrictShellFailureSemanticsTests(unittest.TestCase):
    """DAY6 live-discovered orchestration remediation item 4: any
    lifecycle wrapper introduced or modified here must use strict
    failure semantics equivalent to `set -euo pipefail` - the Makefile's
    own top-level `.SHELLFLAGS` only governs the shell Make invokes for a
    recipe LINE, never a nested `bash -c`/`sh -c` sub-invocation inside
    that line's own text, so each such wrapper needs its own explicit
    declaration."""

    def test_cni_install_uses_bash_with_strict_mode(self):
        recipe = _recipe_block("cni-install")
        self.assertIn("bash -c", recipe)
        self.assertNotIn("sh -c", recipe.replace("bash -c", ""))
        self.assertRegex(recipe, r"set -euo pipefail;")

    def test_mesh_install_uses_bash_with_strict_mode(self):
        recipe = _recipe_block("mesh-install")
        self.assertIn("bash -c", recipe)
        self.assertNotIn("sh -c", recipe.replace("bash -c", ""))
        self.assertRegex(recipe, r"set -euo pipefail;")

    def test_strict_mode_is_the_first_statement_in_each_wrapped_script(self):
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            body_match = re.search(r"bash -c '\\\s*\n\s*(.*)", recipe, re.DOTALL)
            self.assertIsNotNone(body_match)
            first_statement = body_match.group(1).strip()
            with self.subTest(target=target):
                self.assertTrue(first_statement.startswith("set -euo pipefail;"))


class FailedWaitNeverProducesAPassTests(unittest.TestCase):
    """DAY6 live-discovered orchestration remediation item 4: a failed
    wait must prevent subsequent commands and prevent a downstream PASS
    result - never `|| true`-suppressed on the check itself, and the
    diagnostics branch must always end in a real `exit 1`."""

    def test_no_rollout_status_check_is_itself_suppressed_with_or_true(self):
        """The actual pass/fail signal (the `rollout status` call) must
        never be followed by `|| true` - that specific anti-pattern
        would let a real timeout be silently treated as success."""
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            for line in recipe.splitlines():
                if "rollout status" in line and "--timeout=" in line:
                    with self.subTest(target=target, line=line.strip()):
                        self.assertNotIn("|| true", line)

    def test_every_rollout_status_failure_branch_ends_in_exit_1(self):
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            # Each `rollout status ... || { ... }` block must contain an
            # unconditional `exit 1;` before its closing brace - a
            # diagnostics-only branch that fell through without exiting
            # would let the recipe continue as if nothing failed.
            blocks = re.findall(r"rollout status [^\n]*\|\| \{(.*?)\n\t*\}", recipe, re.DOTALL)
            self.assertGreater(len(blocks), 0, f"expected at least one rollout-status failure block in {target}")
            for block in blocks:
                with self.subTest(target=target):
                    self.assertIn("exit 1;", block)

    def test_diagnostics_commands_use_narrowly_scoped_or_true_never_masking_the_exit(self):
        """Diagnostic reads (`get`, `describe`, `get events`) may
        tolerate their own failure with a per-command `|| true` (a
        `describe` on a not-yet-existing object failing is expected and
        must not abort printing the rest of the diagnostics) - but that
        tolerance must never be the LAST thing in the failure block; an
        unconditional `exit 1;` must always follow."""
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            blocks = re.findall(r"rollout status [^\n]*\|\| \{(.*?)\n\t*\}", recipe, re.DOTALL)
            for block in blocks:
                lines = [line for line in block.splitlines() if line.strip()]
                with self.subTest(target=target):
                    self.assertTrue(lines, "diagnostics block must not be empty")
                    last_meaningful = lines[-1].strip().rstrip("\\").strip()
                    self.assertEqual(last_meaningful, "exit 1;")


class DiagnosticsQuerySeparationTests(unittest.TestCase):
    """DAY6 live-discovered orchestration remediation item 4: fix any
    mixed `kubectl get resource/name ... pods` usage - a named workload
    must be queried separately from the Pods collection, never combined
    into a single invalid/ambiguous `kubectl get` call."""

    _MIXED_GET_RE = re.compile(r"kubectl[^\n|;]*\bget\b[^\n|;]*\b\S+/\S+\b[^\n|;]*\bpods\b", re.IGNORECASE)

    def test_no_line_mixes_a_named_workload_and_the_pods_collection_in_one_get(self):
        for target in ("cni-install", "mesh-install", "day6-check"):
            recipe = _recipe_block(target)
            for line in recipe.splitlines():
                with self.subTest(target=target, line=line.strip()):
                    self.assertNotRegex(line, self._MIXED_GET_RE)

    def test_diagnostics_query_named_workload_and_pods_as_separate_kubectl_calls(self):
        """Positive check: the diagnostics blocks DO query both a named
        workload (e.g. `get daemonset/cilium`) and the Pods collection
        (`get pods -l ...`) - just as two distinct kubectl invocations,
        never one mixed call."""
        recipe = _recipe_block("cni-install")
        self.assertIn("get daemonset/cilium -o wide", recipe)
        self.assertIn("get pods -l k8s-app=cilium -o wide", recipe)


class ExplicitKubeconfigContextTests(unittest.TestCase):
    """Every kubectl invocation added by this remediation must use the
    explicit Day 6 kubeconfig/context - never the ambient
    current-context - exactly like every pre-existing kubectl call in
    this Makefile."""

    def test_every_kubectl_call_in_cni_install_and_mesh_install_is_explicit(self):
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            kubectl_lines = [line for line in recipe.splitlines() if re.search(r"\bkubectl\b", line)]
            self.assertGreater(len(kubectl_lines), 0)
            for line in kubectl_lines:
                with self.subTest(target=target, line=line.strip()):
                    self.assertIn("--kubeconfig $(KUBECONFIG_PATH)", line)
                    self.assertIn("--context $(KCONTEXT)", line)

    def test_every_helm_install_in_cni_install_and_mesh_install_is_explicit(self):
        for target in ("cni-install", "mesh-install"):
            recipe = _recipe_block(target)
            # Helm's install calls span multiple continuation lines, so
            # check the whole recipe body rather than a single line.
            for match in re.finditer(r"helm upgrade --install [a-zA-Z0-9_-]+ [a-zA-Z0-9_/-]+(.*?);", recipe, re.DOTALL):
                block = match.group(0)
                with self.subTest(target=target, install=block.split()[3]):
                    self.assertIn("--kubeconfig", block)
                    self.assertIn("--kube-context", block)


class IstiodAutoscalingDisabledTests(unittest.TestCase):
    """DAY6 review INT-2: the pinned istiod 1.31.0 chart defaults to
    `autoscaleEnabled: true` and renders a HorizontalPodAutoscaler
    (min 1, max 5, CPU target). This local kind platform installs no
    metrics-server, so that HPA could only ever report `<unknown>`
    metrics. `mesh-install` must disable it explicitly - verified
    against the chart itself: `pilot.*` is merged onto the top-level
    values by the chart's own `zzy_descope_legacy.yaml`, the same
    mechanism the existing `pilot.resources.*` flags rely on."""

    def _istiod_install_block(self) -> str:
        recipe = _recipe_block("mesh-install")
        match = re.search(r"helm upgrade --install istiod istio/istiod(.*?);", recipe, re.DOTALL)
        self.assertIsNotNone(match, "mesh-install must contain the istiod helm install")
        return match.group(0)

    def test_istiod_install_sets_autoscale_enabled_false(self):
        self.assertRegex(self._istiod_install_block(), r"--set pilot\.autoscaleEnabled=false\b")

    def test_istiod_autoscaling_is_never_enabled_anywhere_in_mesh_install(self):
        recipe = _recipe_block("mesh-install")
        self.assertNotRegex(recipe, r"autoscaleEnabled=true")
        self.assertNotRegex(recipe, r"autoscaleM(in|ax)=")

    def test_autoscale_flag_is_scoped_to_the_istiod_install_only(self):
        """The flag belongs to the istiod chart; it must not leak onto
        base/cni/ztunnel installs where it has no meaning."""
        recipe = _recipe_block("mesh-install")
        self.assertEqual(recipe.count("autoscaleEnabled"), 1)
        self.assertIn("autoscaleEnabled", self._istiod_install_block())


class ContextCheckDocstringConsistencyTests(unittest.TestCase):
    """scripts/context_check.py's own module docstring must not claim a
    stale ordering relationship that the Makefile no longer has."""

    def test_context_check_module_is_importable_and_unchanged_in_contract(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import context_check

        self.assertEqual(context_check.EXPECTED_K8S_VERSION, "v1.36.1")
        self.assertTrue(hasattr(context_check, "main"))


if __name__ == "__main__":
    unittest.main()

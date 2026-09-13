"""
Docker/Kubernetes-free unit tests for scripts/storage_bootstrap.py.

DAY4-TEST-H1 / DAY4-ARCH-M2: mocks `subprocess.run`/`_docker_exec` at
the collaborator boundary and calls the REAL production functions
(`harden_provisioning_root_on_all_nodes`, `restore_provisioning_root_on_nodes`,
`main`) - mirroring tests/test_reconcile_check_failure_handling.py's
technique - to exercise:

  - Idempotency: an already-hardened root is a verified no-op, no
    chown/chmod issued.
  - Preconditions: refuses to touch a root with an unfamiliar owner
    (non-root uid); refuses a symlink.
  - Unfamiliar existing ConfigMap setup script: refuses to patch and
    reverts any node-level hardening already applied this run.
  - Partial hardening failure (chown succeeds, chmod fails mid-hardening
    for one of two nodes): the failure is reported, and restoration
    reverts only the actually-changed node(s) to their EXACT captured
    (mode, gid), asserting the exact `docker exec` argv sequence
    (`chmod g-s` before the numeric mode).
  - Rollback failure visibility: a failure during restoration itself is
    a distinct, prominent RESTORATION FAILURE record.
  - Scratch-resource cleanup: the verify_propagation namespace delete
    runs whenever ownership of the namespace was actually verified,
    success or later failure - but never as a blind delete-by-name
    when creation itself failed or ownership could not be established
    (DAY4 batch 2b, Part E - the same pattern fixed in
    storage_hardening_check.py, present here too).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import storage_bootstrap


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["docker", "exec"], returncode=returncode, stdout=stdout, stderr=stderr)


class _ResultsMixin:
    def setUp(self):
        storage_bootstrap.results = []


class HardenIdempotencyTests(_ResultsMixin, unittest.TestCase):
    def test_already_hardened_root_is_a_no_op_no_chown_chmod_issued(self):
        mutating_calls = []

        def fake_docker_exec(node, *args, **kwargs):
            if args[:1] == ("stat",):
                return _proc(stdout=f"{storage_bootstrap.EXPECTED_ROOT_MODE} 0 {storage_bootstrap.EXPECTED_ROOT_GID}\n")
            if args[:2] == ("test", "-L"):
                return _proc(returncode=1)
            mutating_calls.append((node, args))
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_cluster_nodes", return_value=["worker", "worker2"]):
            with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
                ok, original_state = storage_bootstrap.harden_provisioning_root_on_all_nodes()

        self.assertTrue(ok)
        self.assertEqual(mutating_calls, [], "an already-correctly-hardened root must issue no chown/chmod")
        self.assertEqual(original_state, {}, "a node that needed no change must not appear in the revert map")

    def test_symlink_root_is_refused_untouched(self):
        def fake_docker_exec(node, *args, **kwargs):
            if args[:2] == ("test", "-L"):
                return _proc(returncode=0)  # IS a symlink
            raise AssertionError(f"must not touch a symlink root further: {args}")

        with mock.patch.object(storage_bootstrap, "_cluster_nodes", return_value=["worker"]):
            with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
                ok, _state = storage_bootstrap.harden_provisioning_root_on_all_nodes()

        self.assertFalse(ok)
        messages = " ".join(m for _, m in storage_bootstrap.results)
        self.assertIn("symlink", messages)

    def test_unfamiliar_non_root_owner_is_refused_untouched(self):
        def fake_docker_exec(node, *args, **kwargs):
            if args[:2] == ("test", "-L"):
                return _proc(returncode=1)
            if args[:1] == ("stat",):
                return _proc(stdout="0755 1000 1000\n")  # owned by uid 1000, not root
            raise AssertionError(f"must not modify unfamiliar non-root-owned state: {args}")

        with mock.patch.object(storage_bootstrap, "_cluster_nodes", return_value=["worker"]):
            with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
                ok, _state = storage_bootstrap.harden_provisioning_root_on_all_nodes()

        self.assertFalse(ok)
        messages = " ".join(m for _, m in storage_bootstrap.results)
        self.assertIn("unfamiliar state", messages)

    def test_missing_root_is_created_fresh(self):
        created = []

        def fake_docker_exec(node, *args, **kwargs):
            if args[:2] == ("test", "-L"):
                return _proc(returncode=1)
            if args[:1] == ("stat",):
                return _proc(returncode=1)  # doesn't exist
            created.append(args)
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_cluster_nodes", return_value=["worker"]):
            with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
                ok, original_state = storage_bootstrap.harden_provisioning_root_on_all_nodes()

        self.assertTrue(ok)
        self.assertIn(("mkdir", "-m", storage_bootstrap.EXPECTED_ROOT_MODE, "-p", storage_bootstrap.PROVISIONING_ROOT), created)
        self.assertIn(("chown", f"root:{storage_bootstrap.EXPECTED_ROOT_GID}", storage_bootstrap.PROVISIONING_ROOT), created)
        self.assertIsNone(original_state["worker"], "a freshly-created root must be recorded as None (nothing to revert to)")


class PartialHardeningFailureAndRollbackTests(_ResultsMixin, unittest.TestCase):
    """DAY4-TEST-H1's headline scenario: chown succeeds, chmod fails
    mid-hardening for one of two nodes."""

    def _two_node_state(self, worker_mode="0755", worker_gid="0", worker2_mode="0755", worker2_gid="0"):
        def fake_docker_exec(node, *args, **kwargs):
            if args[:2] == ("test", "-L"):
                return _proc(returncode=1)
            if args[:1] == ("stat",):
                mode, gid = (worker_mode, worker_gid) if node == "worker" else (worker2_mode, worker2_gid)
                return _proc(stdout=f"{mode} 0 {gid}\n")
            if args[0] == "chown":
                return _proc(returncode=0)
            if args[0] == "chmod" and node == "worker2":
                return _proc(returncode=1, stderr="chmod: operation not permitted")
            return _proc(returncode=0)

        return fake_docker_exec

    def test_chmod_failure_on_one_of_two_nodes_is_reported_and_other_node_still_hardens(self):
        with mock.patch.object(storage_bootstrap, "_cluster_nodes", return_value=["worker", "worker2"]):
            with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=self._two_node_state()):
                ok, original_state = storage_bootstrap.harden_provisioning_root_on_all_nodes()

        self.assertFalse(ok)
        self.assertIn("worker", original_state)
        self.assertIn("worker2", original_state)
        messages = " ".join(m for _, m in storage_bootstrap.results)
        self.assertIn("could not harden", messages)
        self.assertIn("worker: hardened", messages)

    def test_restoration_reverts_only_changed_nodes_to_exact_captured_state_with_g_minus_s_first(self):
        original_state = {"worker": ("0640", "2000"), "worker2": ("0700", "3000")}
        argv_by_node: dict[str, list[tuple]] = {"worker": [], "worker2": []}

        def fake_docker_exec(node, *args, **kwargs):
            argv_by_node[node].append(args)
            if args[:1] == ("stat",):
                mode, gid = original_state[node]
                return _proc(stdout=f"{mode} 0 {gid}\n")
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
            ok = storage_bootstrap.restore_provisioning_root_on_nodes(original_state)

        self.assertTrue(ok)
        for node, (mode, gid) in original_state.items():
            argv = argv_by_node[node]
            g_minus_s_index = argv.index(("chmod", "g-s", storage_bootstrap.PROVISIONING_ROOT))
            chmod_index = argv.index(("chmod", mode, storage_bootstrap.PROVISIONING_ROOT))
            chown_index = argv.index(("chown", f"root:{gid}", storage_bootstrap.PROVISIONING_ROOT))
            self.assertLess(g_minus_s_index, chmod_index, f"{node}: g-s must be cleared before the numeric mode is applied")
            self.assertGreater(chown_index, g_minus_s_index)

    def test_node_never_touched_by_harden_is_never_touched_by_restore(self):
        # Only "worker" is in the revert map (worker2 needed no change) -
        # restore must never issue any docker exec for worker2.
        original_state = {"worker": ("0640", "2000")}
        calls = []

        def fake_docker_exec(node, *args, **kwargs):
            calls.append(node)
            if args[:1] == ("stat",):
                return _proc(stdout="0640 0 2000\n")
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
            storage_bootstrap.restore_provisioning_root_on_nodes(original_state)

        self.assertNotIn("worker2", calls)

    def test_restoration_failure_is_a_distinct_prominent_finding(self):
        original_state = {"worker": ("0640", "2000")}

        def fake_docker_exec(node, *args, **kwargs):
            if args[0] == "chown":
                return _proc(returncode=1, stderr="operation not permitted")
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
            ok = storage_bootstrap.restore_provisioning_root_on_nodes(original_state)

        self.assertFalse(ok)
        messages = " ".join(m for _, m in storage_bootstrap.results)
        self.assertIn("RESTORATION FAILURE", messages)

    def test_verification_mismatch_after_restore_is_also_a_restoration_failure(self):
        # chmod/chown all report success, but a subsequent stat shows the
        # root did NOT actually end up in the expected state - this must
        # still be caught, not trusted from exit codes alone.
        original_state = {"worker": ("0640", "2000")}

        def fake_docker_exec(node, *args, **kwargs):
            if args[:1] == ("stat",):
                return _proc(stdout="0755 0 0\n")  # wrong - restore silently didn't take effect
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
            ok = storage_bootstrap.restore_provisioning_root_on_nodes(original_state)

        self.assertFalse(ok)
        messages = " ".join(m for _, m in storage_bootstrap.results)
        self.assertIn("RESTORATION FAILURE", messages)
        self.assertIn("does not match expected", messages)

    def test_newly_created_root_is_left_in_place_not_removed(self):
        original_state = {"worker": None}
        calls = []

        def fake_docker_exec(node, *args, **kwargs):
            calls.append(args)
            return _proc(returncode=0)

        with mock.patch.object(storage_bootstrap, "_docker_exec", side_effect=fake_docker_exec):
            ok = storage_bootstrap.restore_provisioning_root_on_nodes(original_state)

        self.assertTrue(ok)
        self.assertEqual(calls, [], "a root this run created (None baseline) must be left as-is, never removed")


class UnfamiliarConfigMapMainFlowTests(_ResultsMixin, unittest.TestCase):
    """DAY4-TEST-H1: an unfamiliar (neither known-original nor known-
    patched) setup script must refuse to patch AND revert any node-level
    hardening already applied earlier in the SAME run."""

    def test_unfamiliar_setup_refuses_to_patch_and_reverts_root_hardening(self):
        restore_calls = []

        with mock.patch.object(storage_bootstrap.kube, "verify_context"):
            with mock.patch.object(
                storage_bootstrap, "harden_provisioning_root_on_all_nodes",
                return_value=(True, {"worker": ("0755", "0")}),
            ):
                with mock.patch.object(
                    storage_bootstrap, "restore_provisioning_root_on_nodes",
                    side_effect=lambda state: restore_calls.append(state) or True,
                ):
                    with mock.patch.object(storage_bootstrap, "get_configmap", return_value={"data": {"setup": "#!/bin/sh\nsomething totally unfamiliar\n"}}):
                        with mock.patch.object(storage_bootstrap, "patch_setup_field") as mock_patch:
                            exit_code = storage_bootstrap.main()

        self.assertEqual(exit_code, 1)
        mock_patch.assert_not_called()
        self.assertEqual(restore_calls, [{"worker": ("0755", "0")}])

    def test_already_patched_setup_is_idempotent_no_op_no_patch_call(self):
        with mock.patch.object(storage_bootstrap.kube, "verify_context"):
            with mock.patch.object(storage_bootstrap, "harden_provisioning_root_on_all_nodes", return_value=(True, {})):
                with mock.patch.object(storage_bootstrap, "get_configmap", return_value={"data": {"setup": storage_bootstrap.EXPECTED_PATCHED_SETUP}}):
                    with mock.patch.object(storage_bootstrap, "patch_setup_field") as mock_patch:
                        exit_code = storage_bootstrap.main()

        self.assertEqual(exit_code, 0)
        mock_patch.assert_not_called()

    def test_root_hardening_failure_short_circuits_before_configmap_is_ever_read(self):
        with mock.patch.object(storage_bootstrap.kube, "verify_context"):
            with mock.patch.object(storage_bootstrap, "harden_provisioning_root_on_all_nodes", return_value=(False, {})):
                with mock.patch.object(storage_bootstrap, "restore_provisioning_root_on_nodes", return_value=True):
                    with mock.patch.object(storage_bootstrap, "get_configmap") as mock_get_cm:
                        exit_code = storage_bootstrap.main()

        self.assertEqual(exit_code, 1)
        mock_get_cm.assert_not_called()

    def test_failed_propagation_reverts_both_setup_script_and_node_hardening(self):
        restore_calls = []
        patch_calls = []

        with mock.patch.object(storage_bootstrap.kube, "verify_context"):
            with mock.patch.object(storage_bootstrap, "harden_provisioning_root_on_all_nodes", return_value=(True, {"worker": ("0755", "0")})):
                with mock.patch.object(storage_bootstrap, "restore_provisioning_root_on_nodes", side_effect=lambda s: restore_calls.append(s) or True):
                    with mock.patch.object(storage_bootstrap, "get_configmap", return_value={"data": {"setup": storage_bootstrap.EXPECTED_ORIGINAL_SETUP}}):
                        with mock.patch.object(storage_bootstrap, "patch_setup_field", side_effect=lambda s: patch_calls.append(s)):
                            with mock.patch.object(storage_bootstrap, "verify_propagation", return_value=False):
                                exit_code = storage_bootstrap.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(patch_calls, [storage_bootstrap.EXPECTED_PATCHED_SETUP, storage_bootstrap.EXPECTED_ORIGINAL_SETUP])
        self.assertEqual(len(restore_calls), 1)


def _fake_kube_run_namespace_exists(exists: bool):
    def fake_run(*args, **_kwargs):
        if "namespace" in args and "get" in args:
            if exists:
                return _proc(returncode=0, stdout='{"metadata": {"uid": "existing-ns-uid"}}')
            # DAY4 batch 2c: genuine absence is exit 0 + EMPTY stdout
            # under the `--ignore-not-found -o json` contract.
            return _proc(returncode=0, stdout="")
        raise AssertionError(f"unexpected kube.run call: {args}")

    return fake_run


class _RaceSimulatingNamespaceAPI:
    """DAY4 batch 2c (DAY4-SEC-M2): same stateful, command-construction-
    exercising fake as storage_hardening_check.py's identical test
    helper - see that module's test file for the full rationale.
    Simulates another creator winning the race between OUR absence
    check and OUR create call, and distinguishes CREATE (atomic) from
    APPLY (upsert) so a regression back to `apply` for the namespace
    step is actually caught, not just a bare label-match mock."""

    def __init__(self):
        self.create_calls: list[str] = []
        self.apply_calls: list[str] = []

    def kube_run(self, *args, **_kwargs):
        if "namespace" in args and "get" in args:
            return _proc(returncode=0, stdout="")
        raise AssertionError(f"unexpected kube.run call: {args}")

    def subprocess_run(self, cmd, input=None, **_kwargs):
        if "create" in cmd and "-f" in cmd:
            self.create_calls.append(input)
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr=f'Error from server (AlreadyExists): namespaces "{storage_bootstrap.BOOTSTRAP_VERIFY_NAMESPACE}" already exists',
            )
        if "apply" in cmd and "-f" in cmd:
            self.apply_calls.append(input)
            return _proc(returncode=0)
        raise AssertionError(f"unexpected subprocess.run call (a delete must never be reached here): {cmd}")


class VerifyPropagationNamespaceOwnershipTests(_ResultsMixin, unittest.TestCase):
    """DAY4 batch 2b/2c (Part E / DAY4-SEC-M2): `verify_propagation()`'s
    scratch namespace has the SAME "unsuccessful lookup is not
    NotFound / CREATE-only, never adopt via apply / no blind
    delete-by-name / ownership-verified cleanup only" contract as
    storage_hardening_check.py's namespace."""

    def test_existing_namespace_refuses_to_reuse_it(self):
        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=_fake_kube_run_namespace_exists(True)):
            with mock.patch.object(storage_bootstrap.subprocess, "run") as mock_subprocess_run:
                ok = storage_bootstrap.verify_propagation()
        self.assertFalse(ok)
        mock_subprocess_run.assert_not_called()
        self.assertTrue(any(not ok and "already exists" in msg for ok, msg in storage_bootstrap.results))

    def test_ambiguous_lookup_failure_fails_closed_without_creating_anything(self):
        def fake_run(*args, **_kwargs):
            if "namespace" in args and "get" in args:
                return _proc(returncode=1, stderr="Error from server: etcdserver: request timed out")
            raise AssertionError(f"unexpected kube.run call: {args}")

        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=fake_run):
            with mock.patch.object(storage_bootstrap.subprocess, "run") as mock_subprocess_run:
                ok = storage_bootstrap.verify_propagation()
        self.assertFalse(ok)
        mock_subprocess_run.assert_not_called()

    def test_another_creator_winning_the_race_is_preserved_never_adopted_never_deleted(self):
        """DAY4-SEC-M2: the actual race - our absence check sees
        nothing, but another creator's namespace has already landed by
        the time our own create call reaches the API."""
        api = _RaceSimulatingNamespaceAPI()

        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=api.kube_run):
            with mock.patch.object(storage_bootstrap.subprocess, "run", side_effect=api.subprocess_run):
                ok = storage_bootstrap.verify_propagation()

        self.assertFalse(ok)
        self.assertEqual(len(api.create_calls), 1, "the namespace step must issue exactly one CREATE attempt")
        self.assertEqual(api.apply_calls, [], "the namespace step must never use APPLY (upsert) - only CREATE (atomic)")
        self.assertIn(storage_bootstrap.BOOTSTRAP_VERIFY_NAMESPACE, api.create_calls[0])
        self.assertIn(storage_bootstrap.OWNER_LABEL, api.create_calls[0])

    def test_failed_namespace_creation_does_not_trigger_blind_cleanup_delete(self):
        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=_fake_kube_run_namespace_exists(False)):
            with mock.patch.object(storage_bootstrap, "_create_namespace_only", return_value=("failed", "admission webhook denied")):
                with mock.patch.object(storage_bootstrap.subprocess, "run") as mock_subprocess_run:
                    ok = storage_bootstrap.verify_propagation()
        self.assertFalse(ok)
        mock_subprocess_run.assert_not_called()

    def test_ownership_verified_then_later_failure_still_cleans_up(self):
        """Once ownership IS verified (a successful/uncertain-then-
        verified CREATE), a later failure (the PVC manifest, applied by
        `verify_propagation()`'s own local `_apply` closure via
        `subprocess.run` - not separately mockable, so simulated at
        that same seam) must still result in the guaranteed cleanup
        delete actually running."""
        def fake_subprocess_run(cmd, **_kwargs):
            if "apply" in cmd and "-f" in cmd:
                # The namespace step is short-circuited via
                # _create_namespace_only above - this is the PVC apply.
                raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="pvc apply failed")
            if "delete" in cmd:
                return _proc(returncode=0)
            raise AssertionError(f"unexpected subprocess.run call: {cmd}")

        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=_fake_kube_run_namespace_exists(False)):
            with mock.patch.object(storage_bootstrap, "_create_namespace_only", return_value=("created", "")):
                with mock.patch.object(storage_bootstrap, "_verify_ownership", return_value="owned-ns-uid"):
                    with mock.patch.object(storage_bootstrap.subprocess, "run", side_effect=fake_subprocess_run) as mock_subprocess_run:
                        with self.assertRaises(subprocess.CalledProcessError):
                            storage_bootstrap.verify_propagation()

        delete_calls = [c for c in mock_subprocess_run.call_args_list if "delete" in c.args[0]]
        self.assertEqual(len(delete_calls), 1, "cleanup must still run once ownership was actually verified")

    def test_ownership_mismatch_at_cleanup_time_refuses_to_delete(self):
        ownership_calls = {"count": 0}

        def fake_verify_ownership(_name, _token):
            ownership_calls["count"] += 1
            return "owned-ns-uid" if ownership_calls["count"] == 1 else "a-different-uid"

        def fake_subprocess_run(cmd, **_kwargs):
            if "apply" in cmd:
                raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="pvc apply failed")
            raise AssertionError(f"unexpected subprocess.run call (no delete expected): {cmd}")

        with mock.patch.object(storage_bootstrap.kube, "run", side_effect=_fake_kube_run_namespace_exists(False)):
            with mock.patch.object(storage_bootstrap, "_create_namespace_only", return_value=("created", "")):
                with mock.patch.object(storage_bootstrap, "_verify_ownership", side_effect=fake_verify_ownership):
                    with mock.patch.object(storage_bootstrap.subprocess, "run", side_effect=fake_subprocess_run):
                        with self.assertRaises(subprocess.CalledProcessError):
                            storage_bootstrap.verify_propagation()
        self.assertTrue(any(not ok and "refusing to delete" in msg for ok, msg in storage_bootstrap.results))


class NamespaceLookupHelperTests(unittest.TestCase):
    """DAY4 batch 2b/2c: structural `--ignore-not-found -o json`
    absence contract - see storage_hardening_check.py's identical test
    class for the full rationale."""

    def test_genuine_absence_returns_false(self):
        with mock.patch.object(storage_bootstrap.kube, "run", return_value=_proc(returncode=0, stdout="")):
            exists, ns_json, _detail = storage_bootstrap._namespace_lookup("x")
        self.assertIs(exists, False)
        self.assertIsNone(ns_json)

    def test_unrelated_failure_returns_none_not_false(self):
        with mock.patch.object(storage_bootstrap.kube, "run", return_value=_proc(returncode=1, stderr="Error from server: connection refused")):
            exists, ns_json, _detail = storage_bootstrap._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)

    def test_missing_credential_helper_never_misread_as_absence(self):
        with mock.patch.object(
            storage_bootstrap.kube, "run",
            return_value=_proc(returncode=1, stderr='exec: "some-credential-helper": executable file not found in $PATH'),
        ):
            exists, ns_json, detail = storage_bootstrap._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)
        self.assertIn("not found in $PATH", detail)

    def test_forbidden_returns_none_not_false(self):
        with mock.patch.object(storage_bootstrap.kube, "run", return_value=_proc(returncode=1, stderr='Error from server (Forbidden): namespaces "x" is forbidden')):
            exists, ns_json, _detail = storage_bootstrap._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)

    def test_found_returns_true_and_parsed_json(self):
        with mock.patch.object(storage_bootstrap.kube, "run", return_value=_proc(returncode=0, stdout='{"metadata": {"uid": "abc"}}')):
            exists, ns_json, _detail = storage_bootstrap._namespace_lookup("x")
        self.assertIs(exists, True)
        self.assertEqual(ns_json["metadata"]["uid"], "abc")

    def test_malformed_nonempty_output_on_success_returns_none(self):
        with mock.patch.object(storage_bootstrap.kube, "run", return_value=_proc(returncode=0, stdout="not json")):
            exists, ns_json, _detail = storage_bootstrap._namespace_lookup("x")
        self.assertIsNone(exists)
        self.assertIsNone(ns_json)


class VerifyContextFailsClosedTests(_ResultsMixin, unittest.TestCase):
    def test_verify_context_failure_short_circuits_before_any_hardening(self):
        with mock.patch.object(storage_bootstrap.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(storage_bootstrap, "harden_provisioning_root_on_all_nodes") as mock_harden:
                exit_code = storage_bootstrap.main()
        self.assertEqual(exit_code, 1)
        mock_harden.assert_not_called()


if __name__ == "__main__":
    unittest.main()

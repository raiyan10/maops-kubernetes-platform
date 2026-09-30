"""
DAY7: Docker/Kubernetes-free tests for the pieces that keep Day 7
isolated from Day 6 and its evidence private:

  - scripts/kube.py cluster profiles (Day 6 default unchanged, Day 7
    opt-in, unknown profile fails closed);
  - scripts/private_run_dir.py (outside /tmp and the repo, 0700 dir,
    0600 files, exclusive creation - never reused);
  - scripts/suite_baseline.py under the Day 7 profile (DAY7_* env names,
    private-location enforcement, baseline preservation);
  - scripts/day7_lock.py (independent of the Day 6 lock).

Profile-dependent module state is exercised in fresh subprocesses, so
the shared `kube` module other tests import is never mutated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import private_run_dir
import suite_baseline


def _py(code: str, **env) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k not in ("MAOPS_CLUSTER_PROFILE", "KUBECONFIG_PATH")}
    full_env.update(env)
    return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\n{code}"], capture_output=True, text=True, env=full_env, timeout=30)


class KubeProfileTests(unittest.TestCase):
    def test_default_profile_is_the_unchanged_day6_identity(self):
        out = _py("import kube; print(kube.PROFILE, kube.CONTEXT, kube.HELM_RELEASE_NAME, kube.INSTANCE_LABEL, kube.GATEWAY_HOST_ADDRESS, kube.VALIDATION_NAMESPACE)")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["day6", "kind-maops-k8s-day6", "maops-kubernetes-platform-day6", "maops-kubernetes-platform-day6", "http://127.0.0.1:18080", "maops-day6-validation"])

    def test_empty_profile_is_the_default(self):
        out = _py("import kube; print(kube.PROFILE)", MAOPS_CLUSTER_PROFILE="")
        self.assertEqual(out.stdout.strip(), "day6")

    def test_day7_profile(self):
        out = _py("import kube; print(kube.CONTEXT, kube.HELM_RELEASE_NAME, kube.GATEWAY_HOST_ADDRESS, kube.KUBECONFIG_PATH.endswith('/.kube/maops-k8s-day7.config'), 'instance=maops-kubernetes-platform-day7' in kube.GATEWAY_LABEL_SELECTOR)", MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(out.stdout.split(), ["kind-maops-k8s-day7", "maops-kubernetes-platform-day7", "http://127.0.0.1:18081", "True", "True"])

    def test_day7_node_name_guard_rejects_day6_nodes(self):
        out = _py("import kube; print(bool(kube._DAY_NODE_NAME_RE.match('maops-k8s-day6-worker')), bool(kube._DAY_NODE_NAME_RE.match('maops-k8s-day7-worker2')))", MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(out.stdout.split(), ["False", "True"])

    def test_unknown_profile_fails_closed_at_import(self):
        out = _py("import kube", MAOPS_CLUSTER_PROFILE="day8")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not a known cluster profile", out.stderr)

    def test_select_profile_is_pure(self):
        import kube
        self.assertEqual(kube.select_profile({}), "day6")
        self.assertEqual(kube.select_profile({"MAOPS_CLUSTER_PROFILE": "day7"}), "day7")
        with self.assertRaises(RuntimeError):
            kube.select_profile({"MAOPS_CLUSTER_PROFILE": "DAY7"})

    def test_host_ports_differ(self):
        import kube
        self.assertNotEqual(kube.PROFILES["day6"]["host_port"], kube.PROFILES["day7"]["host_port"])

    def test_older_cluster_existence_is_part_of_the_day6_gate_only(self):
        d6 = _py("import final_state_check as f; print(f.older_cluster_existence_required(), f.OTHER_DAY_CLUSTERS[-1])")
        d7 = _py("import final_state_check as f; print(f.older_cluster_existence_required())", MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(d6.stdout.split(), ["True", "maops-k8s-day5"])
        self.assertEqual(d7.stdout.split(), ["False"])

    def test_day7_profile_identities_never_reuse_earlier_day_names(self):
        out = _py("import kube, json; print(json.dumps([kube.VALIDATION_NAMESPACE, kube.MESH_PROBE_NAMESPACE, kube.MESH_PROBE_SERVICE_ACCOUNT, kube.STORAGE_BOOTSTRAP_VERIFY_NAMESPACE, kube.STORAGE_HARDENING_NAMESPACE]))", MAOPS_CLUSTER_PROFILE="day7")
        names = json.loads(out.stdout)
        self.assertEqual(names, ["maops-day7-validation", "maops-day7-mesh-probe", "maops-day7-wrong-identity", "maops-day7-storage-bootstrap-verify", "maops-day7-storage-hardening"])

    def test_day6_profile_keeps_its_historical_probe_names(self):
        out = _py("import kube, json; print(json.dumps([kube.VALIDATION_NAMESPACE, kube.MESH_PROBE_NAMESPACE, kube.STORAGE_BOOTSTRAP_VERIFY_NAMESPACE, kube.STORAGE_HARDENING_NAMESPACE]))")
        self.assertEqual(json.loads(out.stdout), ["maops-day6-validation", "maops-day6-mesh-probe", "maops-day4-storage-bootstrap-verify", "maops-day4-storage-hardening"])


class PrivateRunDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.kw = dict(home=self.home, forbidden_roots=())

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_rules_reject_tmp_repo_relative_and_outside_home(self):
        home = Path.home()
        for bad in ("/tmp/maops/run", "/var/tmp/run", "/dev/shm/run", str(REPO / "evidence" / "run"), "relative/run", "/etc/maops/run", str(home)):
            with self.subTest(path=bad):
                with self.assertRaises(private_run_dir.PrivateRunDirError):
                    private_run_dir.check_location(bad, home=home)

    def test_default_makefile_root_is_accepted(self):
        path = Path.home() / ".local" / "state" / "maops-kubernetes-platform" / "day7-runs" / "abc"
        self.assertEqual(private_run_dir.check_location(path), Path(os.path.realpath(path)))

    def test_create_is_0700_even_under_a_permissive_umask_and_never_reused(self):
        target = self.home / ".local" / "state" / "runs" / "r1"
        old = os.umask(0)
        try:
            private_run_dir.create_run_dir(target, **self.kw)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(target.parent).st_mode & 0o777, 0o700)
        with self.assertRaises(private_run_dir.PrivateRunDirError):
            private_run_dir.create_run_dir(target, **self.kw)

    def test_wrong_mode_or_symlink_rejected(self):
        loose = self.home / "loose"
        loose.mkdir(mode=0o750)
        os.chmod(loose, 0o750)
        with self.assertRaises(private_run_dir.PrivateRunDirError):
            private_run_dir.validate_run_dir(loose, **self.kw)
        real = self.home / "real"
        real.mkdir()
        os.chmod(real, 0o700)
        link = self.home / "link"
        link.symlink_to(real)
        with self.assertRaises(private_run_dir.PrivateRunDirError):
            private_run_dir.validate_run_dir(link, **self.kw)

    def test_private_file_mode_enforced(self):
        d = self.home / "run"
        private_run_dir.create_run_dir(d, **self.kw)
        f = d / "b.json"
        f.write_text("{}")
        os.chmod(f, 0o644)
        with self.assertRaises(private_run_dir.PrivateRunDirError):
            private_run_dir.validate_private_file(f, **self.kw)
        os.chmod(f, 0o600)
        private_run_dir.validate_private_file(f, **self.kw)

    def test_init_requires_run_dir_named_after_run_id(self):
        with mock.patch.dict(os.environ, {"DAY7_RUN_ID": "abc", "DAY7_BASELINE_DIR": str(self.home / "runs" / "xyz")}):
            with mock.patch.object(sys, "argv", ["private_run_dir.py", "init"]):
                import io
                from contextlib import redirect_stderr
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(private_run_dir.main(), 1)
        self.assertFalse((self.home / "runs").exists())


class SuiteBaselineDay7Tests(unittest.TestCase):
    def test_env_names_follow_the_profile(self):
        d6 = _py("import suite_baseline as s; print(s.RUN_ID_ENV, s.PATH_ENV, s.PRIVATE_LOCATION_REQUIRED)")
        d7 = _py("import suite_baseline as s; print(s.RUN_ID_ENV, s.PATH_ENV, s.PRIVATE_LOCATION_REQUIRED)", MAOPS_CLUSTER_PROFILE="day7")
        self.assertEqual(d6.stdout.split(), ["DAY6_RUN_ID", "DAY6_SUITE_BASELINE_PATH", "False"])
        self.assertEqual(d7.stdout.split(), ["DAY7_RUN_ID", "DAY7_SUITE_BASELINE_PATH", "True"])

    def test_day7_capture_refuses_a_non_private_location_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "suite.json")
            with mock.patch.object(suite_baseline, "PRIVATE_LOCATION_REQUIRED", True):
                with self.assertRaises(suite_baseline.SuiteBaselineError):
                    suite_baseline.capture(path, "r", "ctx", "ns", "u1", "u2", "u3", "v")
            self.assertFalse(os.path.exists(path))

    def test_day7_load_refuses_a_non_private_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "suite.json")
            Path(path).write_text(json.dumps({"run_id": "r"}))
            with mock.patch.object(suite_baseline, "PRIVATE_LOCATION_REQUIRED", True):
                with self.assertRaises(suite_baseline.SuiteBaselineError):
                    suite_baseline.load_and_validate(path, "r", "ctx", "ns", "u1", "u2", "u3")
            self.assertEqual(json.loads(Path(path).read_text()), {"run_id": "r"}, "a rejected baseline is preserved, never rewritten")

    def test_day7_capture_in_a_private_dir_then_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            run = home / "run"
            private_run_dir.create_run_dir(run, home=home, forbidden_roots=())
            path = str(run / "suite.json")
            real_validate = private_run_dir.validate_run_dir
            with mock.patch.object(suite_baseline, "PRIVATE_LOCATION_REQUIRED", True), mock.patch.object(
                suite_baseline.private_run_dir, "validate_run_dir", lambda p: real_validate(p, home=home, forbidden_roots=())
            ):
                suite_baseline.capture(path, "r", "ctx", "ns", "u1", "u2", "u3", "v")
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                with self.assertRaises(suite_baseline.SuiteBaselineError):
                    suite_baseline.capture(path, "r", "ctx", "ns", "u1", "u2", "u3", "OTHER")
            self.assertEqual(json.loads(Path(path).read_text())["value"], "v")


class Day7LockTests(unittest.TestCase):
    def _run(self, argv, **env):
        full = {k: v for k, v in os.environ.items() if not k.startswith(("DAY6_LOCK", "DAY7_LOCK"))}
        full.update(env)
        return subprocess.run(argv, capture_output=True, text=True, env=full, timeout=30)

    def test_configure_points_day6_lock_at_day7_identifiers(self):
        out = _py("import day7_lock, day6_lock; day7_lock.configure(); print(day6_lock.FD_ENV, day6_lock.LOCK_PATH_ENV, day6_lock.DEFAULT_LOCK_PATH)")
        self.assertEqual(out.stdout.split(), ["DAY7_LOCK_FD", "DAY7_LOCK_PATH", "/tmp/maops-day7-mutation.lock"])

    def test_day7_and_day6_locks_are_independent(self):
        with tempfile.TemporaryDirectory() as d:
            l6, l7 = os.path.join(d, "l6"), os.path.join(d, "l7")
            inner = f"{sys.executable} {SCRIPTS / 'day7_lock.py'} run -- echo day7-acquired"
            result = self._run(
                [sys.executable, str(SCRIPTS / "day6_lock.py"), "run", "--", "sh", "-c", inner],
                DAY6_LOCK_PATH=l6, DAY7_LOCK_PATH=l7,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("day7-acquired", result.stdout)

    def test_second_independent_day7_invocation_is_refused_and_nested_one_inherits(self):
        with tempfile.TemporaryDirectory() as d:
            l7 = os.path.join(d, "l7")
            lock = f"{sys.executable} {SCRIPTS / 'day7_lock.py'} run --"
            script = f"{lock} echo nested-ok; DAY7_LOCK_FD= {lock} echo SHOULD-NOT-RUN; echo rc=$?"
            result = self._run([sys.executable, str(SCRIPTS / "day7_lock.py"), "run", "--", "sh", "-c", script], DAY7_LOCK_PATH=l7)
            self.assertIn("nested-ok", result.stdout)
            self.assertNotIn("SHOULD-NOT-RUN", result.stdout)
            self.assertIn("rc=1", result.stdout)

    def test_usage_error(self):
        result = self._run([sys.executable, str(SCRIPTS / "day7_lock.py"), "echo"])
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()

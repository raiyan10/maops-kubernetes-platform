"""
Docker/Kubernetes-free unit tests for scripts/secret_bootstrap.py.

Mocks scripts.kube.run so these tests never touch a real cluster.
Covers: preserving an existing Secret (never silently rotating it),
failing closed on a malformed existing Secret, and - per the project's
Secret non-disclosure test discipline - asserting the generated token is
never written to stdout/stderr by capturing captured print() output and
checking the *value* is absent, never interpolating the real secret into
an assertion failure message.
"""

from __future__ import annotations

import base64
import io
import os
import stat
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import secret_bootstrap

# DAY3: secret_bootstrap.main() now calls kube.verify_context() first (the
# fail-closed wrong-cluster guard) - patched module-wide here since every
# test in this file calls main() against a fake `run`/`get_existing_secret`,
# never a real cluster. The context-verification mechanism itself is
# covered by its own dedicated test class below.
_verify_context_patcher = mock.patch.object(secret_bootstrap.kube, "verify_context")


def setUpModule():
    _verify_context_patcher.start()


def tearDownModule():
    _verify_context_patcher.stop()


def _secret_json(value: str | None, key: str = "internal-token") -> dict:
    data = {}
    if value is not None:
        data[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "maops-internal-auth", "namespace": "maops-platform"},
        "data": data,
    }


def _not_found_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["kubectl", "get", "secret"], returncode=1, stdout="", stderr='Error from server (NotFound): secrets "maops-internal-auth" not found'
    )


def _namespace_ok_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl", "get", "namespace"], returncode=0, stdout="", stderr="")


def _namespace_missing_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["kubectl", "get", "namespace"],
        returncode=1,
        stdout="",
        stderr='Error from server (NotFound): namespaces "maops-platform" not found',
    )


def _run_dispatch(namespace_result, secret_result):
    """get_existing_secret() now issues two distinct kubectl calls (a
    namespace existence check, then the Secret get) - this dispatches a
    fake `run()` to the right canned result per call rather than
    returning the same result for both, which would conflate the two."""

    def fake_run(*args, **kwargs):
        if "namespace" in args:
            return namespace_result
        return secret_result

    return fake_run


class GetExistingSecretTests(unittest.TestCase):
    def test_requests_json_output(self):
        # Regression: get_existing_secret() must ask kubectl for -o json -
        # without it, `run()` returns the human-readable table format,
        # which json.loads() cannot parse.
        captured_args = []

        def fake_run(*args, **kwargs):
            captured_args.extend(args)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"kind": "Secret"}', stderr="")

        with mock.patch.object(secret_bootstrap, "run", side_effect=fake_run):
            result = secret_bootstrap.get_existing_secret()
        self.assertIn("-o", captured_args)
        self.assertIn("json", captured_args)
        self.assertEqual(result, {"kind": "Secret"})

    def test_not_found_returns_none(self):
        with mock.patch.object(secret_bootstrap, "run", side_effect=_run_dispatch(_namespace_ok_result(), _not_found_result())):
            result = secret_bootstrap.get_existing_secret()
        self.assertIsNone(result)

    def test_other_kubectl_failure_raises(self):
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Error from server (Forbidden): ...")
        with mock.patch.object(secret_bootstrap, "run", side_effect=_run_dispatch(_namespace_ok_result(), result)):
            with self.assertRaises(RuntimeError):
                secret_bootstrap.get_existing_secret()


class NamespaceClassificationTests(unittest.TestCase):
    """DAY2-INT-L1: a missing namespace must be classified distinctly from
    a missing Secret - it must fail before any "Secret does not exist -
    generating..." messaging or token generation happens."""

    def test_missing_namespace_raises_before_checking_secret(self):
        secret_get_calls = []

        def fake_run(*args, **kwargs):
            if "namespace" in args:
                return _namespace_missing_result()
            secret_get_calls.append(args)
            return _not_found_result()

        with mock.patch.object(secret_bootstrap, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                secret_bootstrap.get_existing_secret()
        self.assertEqual(secret_get_calls, [], "must not attempt to get the Secret when the namespace itself is missing")

    def test_missing_namespace_main_fails_closed_without_generating_token(self):
        create_calls = []
        buf = io.StringIO()
        with mock.patch.object(secret_bootstrap, "run", side_effect=_run_dispatch(_namespace_missing_result(), _not_found_result())):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=lambda t: create_calls.append(t)):
                with redirect_stdout(buf):
                    exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(create_calls, [], "a missing namespace must never trigger Secret creation")
        printed = buf.getvalue()
        self.assertNotIn("does not exist - generating a new token", printed)

    def test_namespace_exists_and_secret_missing_still_resolves_to_none(self):
        with mock.patch.object(secret_bootstrap, "run", side_effect=_run_dispatch(_namespace_ok_result(), _not_found_result())):
            result = secret_bootstrap.get_existing_secret()
        self.assertIsNone(result)


class ValidateSecretShapeTests(unittest.TestCase):
    def test_valid_non_empty_token_passes(self):
        ok, _msg = secret_bootstrap.validate_secret_shape(_secret_json("a-real-token-value"))
        self.assertTrue(ok)

    def test_missing_key_fails(self):
        ok, msg = secret_bootstrap.validate_secret_shape(_secret_json(None))
        self.assertFalse(ok)
        self.assertIn("missing expected key", msg)

    def test_empty_value_fails(self):
        ok, msg = secret_bootstrap.validate_secret_shape(_secret_json(""))
        self.assertFalse(ok)
        self.assertIn("empty", msg)

    def test_malformed_base64_fails(self):
        secret = _secret_json("placeholder")
        secret["data"]["internal-token"] = "not-valid-base64!!!"
        ok, msg = secret_bootstrap.validate_secret_shape(secret)
        self.assertFalse(ok)
        self.assertIn("not valid base64", msg)


class ExistingSecretPreservedTests(unittest.TestCase):
    def test_existing_valid_secret_is_preserved_not_rotated(self):
        existing = _secret_json("already-here-token")
        create_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_secret", return_value=existing):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=lambda t: create_calls.append(t)):
                exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 0)
        self.assertEqual(create_calls, [], "create_secret must never be called when a valid Secret already exists")

    def test_existing_malformed_secret_fails_closed_without_rotating(self):
        existing = _secret_json(None)
        create_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_secret", return_value=existing):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=lambda t: create_calls.append(t)):
                exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 1)
        self.assertEqual(create_calls, [], "a malformed existing Secret must never trigger silent rotation")


class NewSecretCreationTests(unittest.TestCase):
    def test_missing_secret_generates_and_creates_then_verifies(self):
        generated = {}

        def fake_create_secret(token: str) -> None:
            generated["token"] = token

        with mock.patch.object(secret_bootstrap, "get_existing_secret", side_effect=[None, _secret_json("placeholder")]):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=fake_create_secret):
                exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 0)
        self.assertTrue(generated["token"], "a non-empty token must have been generated and passed to create_secret")

    def test_create_secret_failure_reported_cleanly(self):
        exc = subprocess.CalledProcessError(1, ["kubectl", "create", "secret"], stderr="namespace not found")
        with mock.patch.object(secret_bootstrap, "get_existing_secret", return_value=None):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=exc):
                exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 1)


class NonDisclosureTests(unittest.TestCase):
    def test_generated_token_value_never_printed_on_success(self):
        captured_token = {}

        def fake_create_secret(token: str) -> None:
            captured_token["value"] = token

        buf = io.StringIO()
        with mock.patch.object(secret_bootstrap, "get_existing_secret", side_effect=[None, _secret_json("placeholder")]):
            with mock.patch.object(secret_bootstrap, "create_secret", side_effect=fake_create_secret):
                with redirect_stdout(buf):
                    secret_bootstrap.main()

        printed = buf.getvalue()
        self.assertNotIn(captured_token["value"], printed, "the generated token value must never appear in printed output")

    def test_generate_token_is_non_trivial_and_url_safe(self):
        token = secret_bootstrap.generate_token()
        self.assertGreaterEqual(len(token), 32)
        # secrets.token_urlsafe output must not need shell quoting.
        self.assertNotIn(" ", token)
        self.assertNotIn("\n", token)


class TempFileCleanupTests(unittest.TestCase):
    def test_temp_file_removed_even_when_kubectl_create_fails(self):
        written_paths = []
        real_mkstemp = secret_bootstrap.tempfile.mkstemp

        def spying_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            written_paths.append(path)
            return fd, path

        with mock.patch.object(secret_bootstrap.tempfile, "mkstemp", side_effect=spying_mkstemp):
            with mock.patch.object(secret_bootstrap, "run", side_effect=subprocess.CalledProcessError(1, ["kubectl"], stderr="boom")):
                with self.assertRaises(subprocess.CalledProcessError):
                    secret_bootstrap.create_secret("some-token-value")

        self.assertEqual(len(written_paths), 1)
        self.assertFalse(Path(written_paths[0]).exists(), "temp file must be removed even when kubectl create fails")


class TempFileModeTests(unittest.TestCase):
    """DAY2-TEST-L1: create_secret()'s os.chmod(tmp_path, 0o600) call had
    no dedicated assertion. This inspects the real filesystem mode bits
    (via os.stat) while the file still exists, rather than only asserting
    os.chmod was "called"."""

    def test_temp_file_is_mode_0600_before_kubectl_uses_it(self):
        captured_mode = {}
        real_chmod = os.chmod

        def spying_chmod(path, mode):
            real_chmod(path, mode)
            captured_mode["st_mode"] = stat.S_IMODE(os.stat(path).st_mode)

        with mock.patch.object(secret_bootstrap.os, "chmod", side_effect=spying_chmod):
            with mock.patch.object(secret_bootstrap, "run"):
                secret_bootstrap.create_secret("some-token-value-not-printed-below")

        self.assertEqual(captured_mode.get("st_mode"), 0o600)


def _state_secret_json(value: str | None) -> dict:
    data = {}
    if value is not None:
        data["state-token"] = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "maops-state-auth", "namespace": "maops-platform"},
        "data": data,
    }


class StateSecretExistingPreservedTests(unittest.TestCase):
    """DAY4-SEC-M1 / DAY4-TEST-M3: maops-state-auth mirrors
    maops-internal-auth's existing-Secret-preserved and malformed-fails-
    closed contracts, via the dedicated state functions."""

    def test_existing_valid_state_secret_is_preserved_not_rotated(self):
        existing = _state_secret_json("already-here-state-token")
        create_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_state_secret", return_value=existing):
            with mock.patch.object(secret_bootstrap, "create_state_secret", side_effect=lambda t: create_calls.append(t)):
                exit_code = secret_bootstrap.main("state")
        self.assertEqual(exit_code, 0)
        self.assertEqual(create_calls, [], "create_state_secret must never be called when a valid Secret already exists")

    def test_existing_malformed_state_secret_fails_closed_without_rotating(self):
        existing = _state_secret_json(None)
        create_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_state_secret", return_value=existing):
            with mock.patch.object(secret_bootstrap, "create_state_secret", side_effect=lambda t: create_calls.append(t)):
                exit_code = secret_bootstrap.main("state")
        self.assertEqual(exit_code, 1)
        self.assertEqual(create_calls, [], "a malformed existing state Secret must never trigger silent rotation")

    def test_missing_state_secret_generates_creates_and_verifies(self):
        generated = {}

        def fake_create_state_secret(token: str) -> None:
            generated["token"] = token

        with mock.patch.object(secret_bootstrap, "get_existing_state_secret", side_effect=[None, _state_secret_json("placeholder")]):
            with mock.patch.object(secret_bootstrap, "create_state_secret", side_effect=fake_create_state_secret):
                exit_code = secret_bootstrap.main("state")
        self.assertEqual(exit_code, 0)
        self.assertTrue(generated["token"], "a non-empty token must have been generated and passed to create_state_secret")

    def test_state_secret_get_runtime_error_fails_closed(self):
        with mock.patch.object(secret_bootstrap, "get_existing_state_secret", side_effect=RuntimeError("namespace gone")):
            exit_code = secret_bootstrap.main("state")
        self.assertEqual(exit_code, 1)

    def test_generated_state_token_value_never_printed(self):
        captured_token = {}

        def fake_create_state_secret(token: str) -> None:
            captured_token["value"] = token

        buf = io.StringIO()
        with mock.patch.object(secret_bootstrap, "get_existing_state_secret", side_effect=[None, _state_secret_json("placeholder")]):
            with mock.patch.object(secret_bootstrap, "create_state_secret", side_effect=fake_create_state_secret):
                with redirect_stdout(buf):
                    secret_bootstrap.main("state")
        printed = buf.getvalue()
        self.assertNotIn(captured_token["value"], printed)


class AllTargetDispatchTests(unittest.TestCase):
    """DAY4-SEC-M1 / DAY4-TEST-M3: `main("all")` must always invoke both
    `_bootstrap_internal()` and `_bootstrap_state()` regardless of the
    first's outcome (parametrized over all four combinations), and the
    combined exit code must be nonzero if EITHER fails - in both
    directions, not just internal-fails-state-ok."""

    def _run_all(self, internal_ok: bool, state_ok: bool) -> int:
        internal_existing = _secret_json("internal-token-value") if internal_ok else _secret_json(None)
        state_existing = _state_secret_json("state-token-value") if state_ok else _state_secret_json(None)
        with mock.patch.object(secret_bootstrap, "get_existing_secret", return_value=internal_existing):
            with mock.patch.object(secret_bootstrap, "get_existing_state_secret", return_value=state_existing):
                return secret_bootstrap.main("all")

    def test_both_ok_returns_zero(self):
        self.assertEqual(self._run_all(True, True), 0)

    def test_internal_fails_state_ok_returns_nonzero(self):
        self.assertEqual(self._run_all(False, True), 1)

    def test_internal_ok_state_fails_returns_nonzero(self):
        self.assertEqual(self._run_all(True, False), 1)

    def test_both_fail_returns_nonzero(self):
        self.assertEqual(self._run_all(False, False), 1)

    def test_state_bootstrap_still_runs_even_when_internal_bootstrap_fails(self):
        # The critical "both directions" assertion: an internal-secret
        # failure must never short-circuit the state-secret attempt.
        state_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_secret", return_value=_secret_json(None)):
            with mock.patch.object(secret_bootstrap, "get_existing_state_secret", side_effect=lambda: state_calls.append(1) or _state_secret_json("x")):
                secret_bootstrap.main("all")
        self.assertEqual(len(state_calls), 1, "_bootstrap_state must still run when _bootstrap_internal fails")

    def test_internal_bootstrap_still_runs_even_when_state_bootstrap_fails(self):
        internal_calls = []
        with mock.patch.object(secret_bootstrap, "get_existing_secret", side_effect=lambda: internal_calls.append(1) or _secret_json("x")):
            with mock.patch.object(secret_bootstrap, "get_existing_state_secret", return_value=_state_secret_json(None)):
                secret_bootstrap.main("all")
        self.assertEqual(len(internal_calls), 1, "_bootstrap_internal must still run regardless of ordering")

    def test_unknown_target_fails_closed(self):
        exit_code = secret_bootstrap.main("not-a-real-target")
        self.assertEqual(exit_code, 1)


class WrongContextFailsClosedTests(unittest.TestCase):
    """DAY3: secret_bootstrap must never attempt to read/create a Secret
    against an unverified cluster - verify_context() failing must short-
    circuit before get_existing_secret() is ever called."""

    def test_verify_context_failure_short_circuits_before_secret_lookup(self):
        with mock.patch.object(secret_bootstrap.kube, "verify_context", side_effect=RuntimeError("wrong cluster")):
            with mock.patch.object(secret_bootstrap, "get_existing_secret") as mock_get:
                exit_code = secret_bootstrap.main()
        self.assertEqual(exit_code, 1)
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()

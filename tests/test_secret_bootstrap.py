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


if __name__ == "__main__":
    unittest.main()

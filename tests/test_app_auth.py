"""
Docker/Kubernetes-free unit tests for app/server.py's internal-auth
token comparison (hmac.compare_digest-based). Proves an unauthorized
request is rejected deterministically, without needing a live server,
live cluster, or mounted Secret file.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

# Loaded under an explicit, unique module name (rather than a plain
# `sys.path.insert` + `import server`) so it can never collide with a
# same-named module import from gateway/server.py in the same test run.
_SPEC = importlib.util.spec_from_file_location(
    "maops_app_server", Path(__file__).resolve().parent.parent / "app" / "server.py"
)
app_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(app_server)


class TokenValidationTests(unittest.TestCase):
    def test_correct_token_is_valid(self):
        with mock.patch.object(app_server, "INTERNAL_TOKEN", b"the-real-token"):
            self.assertTrue(app_server._token_is_valid("the-real-token"))

    def test_missing_token_header_is_invalid(self):
        with mock.patch.object(app_server, "INTERNAL_TOKEN", b"the-real-token"):
            self.assertFalse(app_server._token_is_valid(None))
        with mock.patch.object(app_server, "INTERNAL_TOKEN", b"the-real-token"):
            self.assertFalse(app_server._token_is_valid(""))

    def test_wrong_token_is_invalid(self):
        with mock.patch.object(app_server, "INTERNAL_TOKEN", b"the-real-token"):
            self.assertFalse(app_server._token_is_valid("a-completely-different-value"))

    def test_wrong_token_same_length_is_invalid(self):
        with mock.patch.object(app_server, "INTERNAL_TOKEN", b"aaaaaaaaaaaaaaaa"):
            self.assertFalse(app_server._token_is_valid("bbbbbbbbbbbbbbbb"))

    def test_no_secret_loaded_rejects_every_request(self):
        # If the Secret file was never mounted/readable, INTERNAL_TOKEN is
        # None - every request must be rejected, never accidentally
        # accepted.
        with mock.patch.object(app_server, "INTERNAL_TOKEN", None):
            self.assertFalse(app_server._token_is_valid("anything-at-all"))

    def test_load_internal_token_missing_file_returns_none_not_raise(self):
        token = app_server.load_internal_token()
        # In this test environment there is no mounted Secret file - the
        # loader must degrade to None, never raise.
        self.assertIsNone(token)


class VisibleConfigNonDisclosureTests(unittest.TestCase):
    def test_visible_config_never_includes_internal_token_env_var(self):
        with mock.patch.dict(
            app_server.os.environ,
            {"APP_NAME": "maops-kubernetes-app", "INTERNAL_TOKEN": "should-never-be-app-prefixed"},
            clear=False,
        ):
            config = app_server.visible_config()
        self.assertNotIn("INTERNAL_TOKEN", config)
        self.assertEqual(config.get("APP_NAME"), "maops-kubernetes-app")


if __name__ == "__main__":
    unittest.main()

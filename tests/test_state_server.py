"""
Docker/Kubernetes-free unit tests for state/server.py.

DAY4-TEST-M2: `_validate_record()` schema enforcement, `_persist_record()`'s
three-way `PersistOutcome` classification (OK / FAILED_CLEAN /
FAILED_UNCERTAIN - especially the parent-directory-fsync-fails-after-
successful-os.replace() case, which must be FAILED_UNCERTAIN and must
still have actually replaced the target file), `_read_record()`/
`_initialize_state_file()` against a real temporary directory (not a
mocked filesystem, so os.replace()/fsync really execute), and
`_token_is_valid()`/`load_state_token()` mirroring app_server's
equivalents.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "maops_state_server", Path(__file__).resolve().parent.parent / "state" / "server.py"
)
state_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(state_server)


class ValidateRecordTests(unittest.TestCase):
    def test_valid_string_value(self):
        self.assertTrue(state_server._validate_record({"value": "hello"}))

    def test_valid_null_value(self):
        self.assertTrue(state_server._validate_record({"value": None}))

    def test_non_dict_top_level_rejected(self):
        self.assertFalse(state_server._validate_record(["value", "x"]))
        self.assertFalse(state_server._validate_record("value"))
        self.assertFalse(state_server._validate_record(None))
        self.assertFalse(state_server._validate_record(42))

    def test_extra_key_rejected(self):
        self.assertFalse(state_server._validate_record({"value": "x", "extra": 1}))

    def test_missing_key_rejected(self):
        self.assertFalse(state_server._validate_record({}))
        self.assertFalse(state_server._validate_record({"not_value": "x"}))

    def test_wrong_value_type_rejected(self):
        self.assertFalse(state_server._validate_record({"value": 123}))
        self.assertFalse(state_server._validate_record({"value": True}))
        self.assertFalse(state_server._validate_record({"value": ["a"]}))
        self.assertFalse(state_server._validate_record({"value": {"nested": 1}}))


class PersistRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        self._patch_paths = mock.patch.multiple(
            state_server, STATE_FILE_PATH=self.state_path, _STATE_DIR=self.tmpdir.name
        )
        self._patch_paths.start()
        self.addCleanup(self._patch_paths.stop)

    def test_ok_outcome_actually_persists_readable_content(self):
        outcome, detail = state_server._persist_record({"value": "hello"})
        self.assertEqual(outcome, state_server.PersistOutcome.OK)
        self.assertIsNone(detail)
        with open(self.state_path) as f:
            self.assertEqual(json.load(f), {"value": "hello"})

    def test_persisted_file_has_restrictive_permissions(self):
        state_server._persist_record({"value": "hello"})
        import stat

        mode = stat.S_IMODE(os.stat(self.state_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_write_flush_failure_is_failed_clean_and_leaves_no_temp_file(self):
        with mock.patch.object(state_server.os, "fsync", side_effect=OSError("disk full")):
            outcome, detail = state_server._persist_record({"value": "x"})
        self.assertEqual(outcome, state_server.PersistOutcome.FAILED_CLEAN)
        self.assertIn("disk full", detail)
        self.assertFalse(os.path.exists(self.state_path))
        leftover = [f for f in os.listdir(self.tmpdir.name) if f.startswith(".state-")]
        self.assertEqual(leftover, [], "a failed write must not leave a temp file behind")

    def test_atomic_replace_failure_is_failed_clean(self):
        with mock.patch.object(state_server.os, "replace", side_effect=OSError("replace failed")):
            outcome, detail = state_server._persist_record({"value": "x"})
        self.assertEqual(outcome, state_server.PersistOutcome.FAILED_CLEAN)
        self.assertIn("replace failed", detail)
        self.assertFalse(os.path.exists(self.state_path))

    def test_parent_directory_fsync_failure_after_successful_replace_is_failed_uncertain(self):
        # The genuinely ambiguous case: os.replace() itself succeeds (the
        # new content is on disk and readable), but the directory fsync
        # that should follow it fails - durability across a crash at
        # exactly this point is uncertain, so this must be neither a
        # clean success nor a clean failure.
        real_fsync = state_server.os.fsync
        call_count = {"n": 0}

        def flaky_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return real_fsync(fd)  # the temp-file fsync succeeds
            raise OSError("directory fsync failed")  # the parent-dir fsync fails

        with mock.patch.object(state_server.os, "fsync", side_effect=flaky_fsync):
            outcome, detail = state_server._persist_record({"value": "committed-anyway"})

        self.assertEqual(outcome, state_server.PersistOutcome.FAILED_UNCERTAIN)
        self.assertIn("directory fsync failed", detail)
        # The file WAS actually replaced despite the uncertain outcome -
        # this is exactly what makes it "uncertain" rather than "clean
        # failure": a subsequent read sees the new content.
        with open(self.state_path) as f:
            self.assertEqual(json.load(f), {"value": "committed-anyway"})


class ReadRecordAndInitializeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        self._patch_paths = mock.patch.multiple(
            state_server, STATE_FILE_PATH=self.state_path, _STATE_DIR=self.tmpdir.name
        )
        self._patch_paths.start()
        self.addCleanup(self._patch_paths.stop)

    def test_read_missing_file_returns_error_not_raise(self):
        record, err = state_server._read_record()
        self.assertIsNone(record)
        self.assertIn("read failed", err)

    def test_read_malformed_json_returns_error(self):
        with open(self.state_path, "w") as f:
            f.write("not json{")
        record, err = state_server._read_record()
        self.assertIsNone(record)
        self.assertIn("not valid JSON", err)

    def test_read_schema_violation_returns_error(self):
        with open(self.state_path, "w") as f:
            json.dump({"value": 123}, f)
        record, err = state_server._read_record()
        self.assertIsNone(record)
        self.assertIn("schema", err)

    def test_read_valid_record_returns_it_with_no_error(self):
        with open(self.state_path, "w") as f:
            json.dump({"value": "x"}, f)
        record, err = state_server._read_record()
        self.assertEqual(record, {"value": "x"})
        self.assertIsNone(err)

    def test_read_always_hits_disk_never_a_cached_value(self):
        with open(self.state_path, "w") as f:
            json.dump({"value": "first"}, f)
        record1, _ = state_server._read_record()
        with open(self.state_path, "w") as f:
            json.dump({"value": "second"}, f)
        record2, _ = state_server._read_record()
        self.assertEqual(record1["value"], "first")
        self.assertEqual(record2["value"], "second")

    def test_initialize_missing_file_writes_null_default(self):
        state_server._init_error = None
        state_server._initialize_state_file()
        with open(self.state_path) as f:
            self.assertEqual(json.load(f), {"value": None})
        self.assertIsNone(state_server._init_error)

    def test_initialize_never_overwrites_an_existing_valid_file(self):
        with open(self.state_path, "w") as f:
            json.dump({"value": "pre-existing"}, f)
        state_server._init_error = None
        state_server._initialize_state_file()
        with open(self.state_path) as f:
            self.assertEqual(json.load(f), {"value": "pre-existing"})
        self.assertIsNone(state_server._init_error)

    def test_initialize_never_overwrites_a_malformed_pre_existing_file(self):
        with open(self.state_path, "w") as f:
            f.write("not json at all")
        state_server._init_error = None
        state_server._initialize_state_file()
        with open(self.state_path) as f:
            self.assertEqual(f.read(), "not json at all")
        self.assertIsNotNone(state_server._init_error)


class TokenValidationTests(unittest.TestCase):
    def test_correct_token_is_valid(self):
        with mock.patch.object(state_server, "STATE_TOKEN", b"the-real-token"):
            self.assertTrue(state_server._token_is_valid("the-real-token"))

    def test_missing_token_header_is_invalid(self):
        with mock.patch.object(state_server, "STATE_TOKEN", b"the-real-token"):
            self.assertFalse(state_server._token_is_valid(None))
            self.assertFalse(state_server._token_is_valid(""))

    def test_wrong_token_is_invalid(self):
        with mock.patch.object(state_server, "STATE_TOKEN", b"the-real-token"):
            self.assertFalse(state_server._token_is_valid("wrong-value"))

    def test_no_secret_loaded_rejects_every_request(self):
        with mock.patch.object(state_server, "STATE_TOKEN", None):
            self.assertFalse(state_server._token_is_valid("anything-at-all"))

    def test_load_state_token_missing_file_returns_none_not_raise(self):
        token = state_server.load_state_token()
        self.assertIsNone(token)


class VisibleConfigNonDisclosureTests(unittest.TestCase):
    def test_visible_config_never_includes_state_token_env_var(self):
        with mock.patch.dict(
            state_server.os.environ,
            {"APP_NAME": "maops-kubernetes-state", "STATE_TOKEN": "should-never-be-app-prefixed"},
            clear=False,
        ):
            config = state_server.visible_config()
        self.assertNotIn("STATE_TOKEN", config)
        self.assertEqual(config.get("APP_NAME"), "maops-kubernetes-state")


class HandlerRouteTests(unittest.TestCase):
    """DAY4-TEST-M2: state/authentication, schema and lock behavior at
    the HTTP handler layer, mirroring test_gateway_backend_target.py's
    `_FakeHandler` technique."""

    class _FakeHandler:
        def __init__(self, headers=None, body: bytes = b""):
            import io

            self.headers = headers or {}
            self.rfile = io.BytesIO(body)
            self.responses = []

        def _write_json(self, status, payload):
            self.responses.append((status, payload))

        def _authenticated(self):
            return state_server._token_is_valid(self.headers.get(state_server.STATE_TOKEN_HEADER))

    def test_get_state_rejects_unauthenticated(self):
        handler = self._FakeHandler()
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            state_server.Handler._handle_get_state(handler)
        self.assertEqual(handler.responses, [(403, {"error": "forbidden"})])

    def test_get_state_authenticated_returns_persisted_record(self):
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok"})
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_read_record", return_value=({"value": "x"}, None)):
                state_server.Handler._handle_get_state(handler)
        self.assertEqual(handler.responses, [(200, {"value": "x"})])

    def test_get_state_storage_error_is_500_not_traceback(self):
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok"})
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_read_record", return_value=(None, "disk error")):
                state_server.Handler._handle_get_state(handler)
        status, payload = handler.responses[0]
        self.assertEqual(status, 500)
        self.assertEqual(payload["detail"], "disk error")

    def test_put_state_rejects_unauthenticated_without_reading_body(self):
        handler = self._FakeHandler(headers={"Content-Length": "10"}, body=b'{"value":1}')
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_persist_record") as mock_persist:
                state_server.Handler._handle_put_state(handler)
        mock_persist.assert_not_called()
        self.assertEqual(handler.responses, [(403, {"error": "forbidden"})])

    def test_put_state_oversized_body_is_413(self):
        oversized = state_server.STATE_MAX_BODY_BYTES + 1
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok", "Content-Length": str(oversized)}, body=b"x" * 10)
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_persist_record") as mock_persist:
                state_server.Handler._handle_put_state(handler)
        mock_persist.assert_not_called()
        status, payload = handler.responses[0]
        self.assertEqual(status, 413)

    def test_put_state_invalid_schema_is_422_without_persisting(self):
        body = json.dumps({"value": 123}).encode()
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok", "Content-Length": str(len(body))}, body=body)
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_persist_record") as mock_persist:
                state_server.Handler._handle_put_state(handler)
        mock_persist.assert_not_called()
        status, _payload = handler.responses[0]
        self.assertEqual(status, 422)

    def test_put_state_failed_uncertain_outcome_is_500_never_reported_as_success(self):
        body = json.dumps({"value": "x"}).encode()
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok", "Content-Length": str(len(body))}, body=body)
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_persist_record", return_value=(state_server.PersistOutcome.FAILED_UNCERTAIN, "dir fsync failed")):
                state_server.Handler._handle_put_state(handler)
        status, payload = handler.responses[0]
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "write outcome uncertain")

    def test_put_state_ok_outcome_is_200(self):
        body = json.dumps({"value": "x"}).encode()
        handler = self._FakeHandler(headers={state_server.STATE_TOKEN_HEADER: "tok", "Content-Length": str(len(body))}, body=body)
        with mock.patch.object(state_server, "STATE_TOKEN", b"tok"):
            with mock.patch.object(state_server, "_persist_record", return_value=(state_server.PersistOutcome.OK, None)):
                state_server.Handler._handle_put_state(handler)
        self.assertEqual(handler.responses, [(200, {"status": "ok"})])


if __name__ == "__main__":
    unittest.main()

"""
Docker/Kubernetes-free unit tests for scripts/suite_baseline.py - the
run-specific suite-level `/state` baseline capture/verification
contract shared by state_check.py (writer) and final_state_check.py
(reader).

Uses real temporary files (tempfile) to prove the actual O_CREAT|O_EXCL
refuse-to-overwrite behavior and restrictive (0600) permissions, not a
mocked filesystem.

DAY4 batch 2b: capture()/load_and_validate() now also carry a
`namespace_uid` identity (Part C's "capture and verify namespace UID as
required by the original design"), and every identity comparison
(namespace/PVC/PV UID) requires BOTH sides to be a genuine nonempty
string before comparing - two missing/empty UIDs must never be
accepted as proof that identity was preserved.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import suite_baseline


class EnvConfiguredTests(unittest.TestCase):
    def test_both_unset_returns_none_none(self):
        env = {k: v for k, v in os.environ.items() if k not in (suite_baseline.RUN_ID_ENV, suite_baseline.PATH_ENV)}
        import unittest.mock as mock

        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(suite_baseline.env_configured(), (None, None))

    def test_both_set_are_returned_verbatim(self):
        import unittest.mock as mock

        with mock.patch.dict(os.environ, {suite_baseline.RUN_ID_ENV: "abc", suite_baseline.PATH_ENV: "/tmp/x.json"}):
            self.assertEqual(suite_baseline.env_configured(), ("abc", "/tmp/x.json"))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "baseline.json")

    def test_capture_creates_file_with_restrictive_permissions(self):
        suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "pvc-1", "pv-1", "value-1")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_capture_writes_all_required_fields(self):
        suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "pvc-1", "pv-1", "value-1")
        with open(self.path) as f:
            record = json.load(f)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["context"], "kind-ctx")
        self.assertEqual(record["namespace"], "ns")
        self.assertEqual(record["namespace_uid"], "ns-uid-1")
        self.assertEqual(record["pvc_uid"], "pvc-1")
        self.assertEqual(record["pv_uid"], "pv-1")
        self.assertEqual(record["value"], "value-1")
        self.assertIn("captured_at", record)

    def test_capture_preserves_null_value(self):
        suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "pvc-1", "pv-1", None)
        with open(self.path) as f:
            record = json.load(f)
        self.assertIsNone(record["value"])

    def test_capture_refuses_to_overwrite_existing_file(self):
        suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "pvc-1", "pv-1", "original")
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            suite_baseline.capture(self.path, "run-2", "kind-ctx", "ns", "ns-uid-1", "pvc-1", "pv-1", "clobbered")
        self.assertIn("already exists", str(ctx.exception))
        # The original file must be untouched.
        with open(self.path) as f:
            record = json.load(f)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["value"], "original")

    def test_capture_refuses_missing_namespace_uid(self):
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", None, "pvc-1", "pv-1", "value-1")
        self.assertIn("namespace_uid", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_capture_refuses_empty_pvc_uid(self):
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "", "pv-1", "value-1")
        self.assertIn("pvc_uid", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_capture_refuses_missing_pv_uid(self):
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            suite_baseline.capture(self.path, "run-1", "kind-ctx", "ns", "ns-uid-1", "pvc-1", None, "value-1")
        self.assertIn("pv_uid", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path))


class LoadAndValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "baseline.json")

    def _write(self, obj) -> None:
        with open(self.path, "w") as f:
            json.dump(obj, f)

    def _load(self, **overrides):
        args = dict(
            path=self.path,
            expected_run_id="run-1",
            expected_context="ctx",
            expected_namespace="ns",
            expected_namespace_uid="ns-uid-1",
            expected_pvc_uid="pvc-1",
            expected_pv_uid="pv-1",
        )
        args.update(overrides)
        return suite_baseline.load_and_validate(
            args["path"],
            args["expected_run_id"],
            args["expected_context"],
            args["expected_namespace"],
            args["expected_namespace_uid"],
            args["expected_pvc_uid"],
            args["expected_pv_uid"],
        )

    def test_missing_file_raises(self):
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_json_raises(self):
        with open(self.path, "w") as f:
            f.write("not json{")
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_non_object_json_raises(self):
        self._write([1, 2, 3])
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("does not contain a JSON object", str(ctx.exception))

    def test_missing_required_field_raises(self):
        self._write({"run_id": "run-1", "context": "ctx", "namespace": "ns", "pvc_uid": "pvc-1", "pv_uid": "pv-1"})
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("missing required field", str(ctx.exception))

    def _full_record(self, **overrides):
        record = {
            "run_id": "run-1",
            "context": "ctx",
            "namespace": "ns",
            "namespace_uid": "ns-uid-1",
            "pvc_uid": "pvc-1",
            "pv_uid": "pv-1",
            "value": "the-value",
            "captured_at": "2026-01-01T00:00:00+00:00",
        }
        record.update(overrides)
        return record

    def test_run_id_mismatch_raises(self):
        self._write(self._full_record(run_id="different-run"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("run_id", str(ctx.exception))

    def test_context_mismatch_raises(self):
        self._write(self._full_record(context="different-ctx"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("context", str(ctx.exception))

    def test_namespace_mismatch_raises(self):
        self._write(self._full_record(namespace="different-ns"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("namespace", str(ctx.exception))

    def test_namespace_uid_mismatch_raises(self):
        """DAY4 batch 2b (Part C): a namespace deleted and recreated
        with the SAME name but a DIFFERENT UID must fail closed - name
        equality alone is not identity preservation."""
        self._write(self._full_record(namespace_uid="a-different-namespace-uid"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("namespace UID", str(ctx.exception))

    def test_namespace_uid_both_missing_does_not_pass(self):
        """DAY4 batch 2b (Part C): matching missing namespace UIDs is
        never proof of identity preservation."""
        self._write(self._full_record(namespace_uid=None))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load(expected_namespace_uid=None)
        self.assertIn("namespace UID", str(ctx.exception))

    def test_pvc_uid_mismatch_raises(self):
        self._write(self._full_record(pvc_uid="different-pvc"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("PVC UID", str(ctx.exception))

    def test_pv_uid_mismatch_raises(self):
        self._write(self._full_record(pv_uid="different-pv"))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("PV UID", str(ctx.exception))

    def test_pvc_uid_both_missing_does_not_pass(self):
        """DAY4 batch 2b (Part C): matching missing PVC UIDs is never
        proof of identity preservation - the exact defect this batch
        closes."""
        self._write(self._full_record(pvc_uid=None))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load(expected_pvc_uid=None)
        self.assertIn("PVC UID", str(ctx.exception))

    def test_pv_uid_both_empty_string_does_not_pass(self):
        self._write(self._full_record(pv_uid=""))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load(expected_pv_uid="")
        self.assertIn("PV UID", str(ctx.exception))

    def test_invalid_value_type_raises(self):
        self._write(self._full_record(value=12345))
        with self.assertRaises(suite_baseline.SuiteBaselineError) as ctx:
            self._load()
        self.assertIn("unexpected type", str(ctx.exception))

    def test_valid_matching_record_returns_it(self):
        self._write(self._full_record())
        record = self._load()
        self.assertEqual(record["value"], "the-value")

    def test_valid_null_value_returns_it(self):
        self._write(self._full_record(value=None))
        record = self._load()
        self.assertIsNone(record["value"])

    def test_load_never_deletes_or_modifies_the_file_on_failure(self):
        original_bytes = json.dumps(self._full_record(run_id="wrong-run")).encode()
        with open(self.path, "wb") as f:
            f.write(original_bytes)
        with self.assertRaises(suite_baseline.SuiteBaselineError):
            self._load()
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)


if __name__ == "__main__":
    unittest.main()

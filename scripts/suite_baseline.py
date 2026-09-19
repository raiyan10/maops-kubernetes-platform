"""
DAY4: minimal, explicit, run-specific suite-level `/state` baseline.

Distinct from persistence_check.py/retention_check.py's own per-
experiment restoration proofs (each of which independently verifies
that ITS OWN mutation is properly undone). This exists specifically to
catch a whole-*pipeline* regression - e.g. a future mutating script
inserted between state-check and final-state-check that forgets its
own restoration contract - that no single script's own self-check
could ever catch.

Deliberately NOT a reusable per-cluster baseline file (the design
batch 1 proposed and this batch's own briefing explicitly rejects): a
fixed per-cluster path would let a stale baseline from an unrelated
earlier run be silently reused by a later invocation. Every baseline
here is scoped to one explicit run ID and one explicit external file
path, both supplied via environment variables the Makefile generates
once per `make day5-check` invocation (see Makefile's DAY5_RUN_ID /
DAY5_SUITE_BASELINE_PATH) and passes through to child `$(MAKE)`
recipe lines by exporting them - never auto-discovered by the reader
(check_and_report() below) and never recomputed at the final gate.

Standalone command behavior (explicit, by design): a script invoked
without DAY5_RUN_ID/DAY5_SUITE_BASELINE_PATH set in its environment -
i.e. `make state-check` or `make final-state-check` run on its own,
outside `make day5-check` - never claims suite-baseline capture or
restoration. `state_check.py` simply skips capture (non-fatally; its
other checks are still meaningful standalone) and prints why;
`final_state_check.py` records this as a distinct, explicit FAILURE of
the suite-baseline check specifically (not a silent skip) - a
standalone final-state-check run cannot prove a whole-pipeline
invariant it was never given the means to check. Two independent
`make X` invocations that don't deliberately share the same
DAY5_RUN_ID/DAY5_SUITE_BASELINE_PATH env vars will always disagree on
`run_id` and correctly fail closed at the final gate - this is the
intended behavior, not a bug.

Single-operator assumption: this project's Makefile/scripts are a
single local operator's tool, never a concurrent shared CI runner (see
docs/architecture.md). This module's own refuse-to-overwrite creation
(`capture()` below, via O_EXCL) does NOT, by itself, guard against two
overlapping `make day5-check` invocations mutating the same live
cluster at once - a genuinely FRESH run computes its own new
`uuid4().hex`-derived path each time (see the Makefile's `DAY5_RUN_ID`),
so it will essentially never collide with an unrelated earlier run's
file; O_EXCL here instead guards a narrower case - the SAME path being
written to twice (e.g. `DAY5_RUN_ID`/`DAY5_SUITE_BASELINE_PATH`
deliberately reused/re-exported by the operator, or `capture()` being
called a second time within one run) - never silently overwriting
whatever is already there. Actual mutual exclusion against a second,
independent, concurrent invocation is `scripts/day5_lock.py`'s job
(DAY4 batch 2b, Part D - carried forward and re-wired to `day5_lock.py`
for Day 5), a wholly separate, process-held local lock - not this
per-run baseline file. Cleaning up a stale leftover baseline file (from
a run that crashed before final-state-check ever ran) is a manual
operator action.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_checks import is_nonempty_identity

RUN_ID_ENV = "DAY5_RUN_ID"
PATH_ENV = "DAY5_SUITE_BASELINE_PATH"

_REQUIRED_FIELDS = {"run_id", "context", "namespace", "namespace_uid", "pvc_uid", "pv_uid", "value", "captured_at"}


class SuiteBaselineError(Exception):
    """Raised for any suite-baseline capture/read/validation failure.
    Callers always convert this into a recorded, non-fatal-to-the-
    process failure (record(False, ...)) - never an uncaught
    traceback - and never delete the underlying file on this path, so
    it remains available as evidence after a failure."""


def env_configured() -> tuple[str | None, str | None]:
    """Returns (run_id, path) exactly as supplied via the environment -
    never auto-discovered, never defaulted to a guessed path."""
    return os.environ.get(RUN_ID_ENV), os.environ.get(PATH_ENV)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def capture(
    path: str,
    run_id: str,
    context: str,
    namespace: str,
    namespace_uid: str | None,
    pvc_uid: str | None,
    pv_uid: str | None,
    value: str | None,
) -> dict:
    """Atomically creates `path` (mode 0600, O_EXCL - refuses to
    overwrite an existing file) containing the validated baseline
    record. Raises SuiteBaselineError on any failure, including
    "already exists" - a leftover/stale file at this exact path (a
    reused run ID/path, or a repeated call within one run; a genuinely
    fresh run's own new UUID-derived path essentially never collides
    with an unrelated earlier run's - see the module docstring) - this
    function NEVER silently overwrites a pre-existing baseline.

    DAY4 batch 2b: `namespace_uid`, `pvc_uid`, and `pv_uid` must each be
    a genuine nonempty string identity - capturing a baseline with a
    missing identity would let a later comparison "match" two missing
    values and wrongly claim identity was preserved (see
    load_and_validate() below)."""
    for label, identity in (("namespace_uid", namespace_uid), ("pvc_uid", pvc_uid), ("pv_uid", pv_uid)):
        if not is_nonempty_identity(identity):
            raise SuiteBaselineError(f"cannot capture suite baseline: {label} was not a nonempty string ({identity!r}) - identity was not reliably captured")
    record = {
        "run_id": run_id,
        "context": context,
        "namespace": namespace,
        "namespace_uid": namespace_uid,
        "pvc_uid": pvc_uid,
        "pv_uid": pv_uid,
        "value": value,
        "captured_at": _now_iso(),
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SuiteBaselineError(
            f"suite baseline already exists at {path!r} - refusing to overwrite it "
            "(a previous day4-check run may still be in flight, or left this behind after a failure; "
            "removing it is a manual operator decision)"
        ) from exc
    except OSError as exc:
        raise SuiteBaselineError(f"could not create suite baseline at {path!r}: {exc}") from exc
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
    except OSError as exc:
        raise SuiteBaselineError(f"could not write suite baseline at {path!r}: {exc}") from exc
    return record


def load_and_validate(
    path: str,
    expected_run_id: str,
    expected_context: str,
    expected_namespace: str,
    expected_namespace_uid: str | None,
    expected_pvc_uid: str | None,
    expected_pv_uid: str | None,
) -> dict:
    """Reads and validates the baseline at `path`, raising
    SuiteBaselineError with a specific, distinct message for every
    failure mode: missing file, malformed JSON, malformed schema
    (missing field), run-ID mismatch, context/namespace mismatch, or
    identity (namespace/PVC/PV UID) mismatch. Never deletes or modifies
    the file on any path - it is preserved as evidence regardless of
    outcome.

    DAY4 batch 2b: every identity comparison (namespace/PVC/PV UID)
    requires BOTH the recorded and the live-observed value to be a
    genuine nonempty string BEFORE comparing them for equality - two
    missing/empty UIDs are never accepted as proof that identity was
    preserved, only as proof that neither side could be read."""
    try:
        with open(path, "r") as f:
            raw = f.read()
    except OSError as exc:
        raise SuiteBaselineError(f"suite baseline file {path!r} not found or unreadable: {exc}") from exc
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SuiteBaselineError(f"suite baseline file {path!r} is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise SuiteBaselineError(f"suite baseline file {path!r} does not contain a JSON object")
    missing = _REQUIRED_FIELDS - set(record.keys())
    if missing:
        raise SuiteBaselineError(f"suite baseline file {path!r} missing required field(s): {sorted(missing)}")

    value = record["value"]
    if value is not None and not isinstance(value, str):
        raise SuiteBaselineError(f"suite baseline file {path!r} 'value' has an unexpected type: {value!r}")

    if record["run_id"] != expected_run_id:
        raise SuiteBaselineError(
            f"suite baseline run_id {record['run_id']!r} does not match this invocation's run ID {expected_run_id!r} "
            "- refusing to trust a baseline captured by a different run"
        )
    if record["context"] != expected_context:
        raise SuiteBaselineError(f"suite baseline context {record['context']!r} does not match the current context {expected_context!r}")
    if record["namespace"] != expected_namespace:
        raise SuiteBaselineError(f"suite baseline namespace {record['namespace']!r} does not match the current namespace {expected_namespace!r}")

    for label, recorded, expected, reason in (
        ("namespace UID", record.get("namespace_uid"), expected_namespace_uid, "the namespace may have been deleted and recreated since capture"),
        ("PVC UID", record["pvc_uid"], expected_pvc_uid, "storage identity changed since capture"),
        ("PV UID", record["pv_uid"], expected_pv_uid, "storage identity changed since capture"),
    ):
        if not is_nonempty_identity(recorded) or not is_nonempty_identity(expected):
            raise SuiteBaselineError(
                f"suite baseline {label} could not be verified: recorded={recorded!r} current={expected!r} "
                "- a missing/empty identity on either side is never accepted as proof of preservation"
            )
        if recorded != expected:
            raise SuiteBaselineError(f"suite baseline {label} {recorded!r} does not match the current {label} {expected!r} - {reason}")
    return record

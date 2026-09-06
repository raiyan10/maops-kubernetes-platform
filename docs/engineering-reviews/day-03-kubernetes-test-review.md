# Project 4 / Day 3 / v0.3.0 — Kubernetes Test Engineering Review

**Role:** `kubernetes-test-engineer` (independent review)
**Scope:** Unit test suite (`tests/`) and its production predicates
(`scripts/`) for Day 3 — scaling, rolling update/rollback, worker
scheduling/topology, PodDisruptionBudget/Eviction, dependency-failure
drain race, and final-state settlement. Static manifest mutation
coverage reviewed for completeness against the Day 3 field list.

**Method:** Read every Day 3-relevant production script and its
matching test file in full (not excerpts). Ran the full unit suite and
the static manifest check locally against this checkout to confirm the
reported baselines are real, not asserted from memory. No live kind
cluster was available in this review session, so the *live* check
counts (33/33 cluster, 12/12 scheduling, 20/20 scaling, 36/36
rollout/rollback, 16/16 PDB, 30/30 final-state, 8/8 reconciliation) were
evaluated by reading the scripts that produce them, not by re-running
them against a cluster.

---

## Baseline verification

| Claim | Verified | Method |
|---|---|---|
| 282 unit tests | **Confirmed** | `python3 -m unittest discover -s tests` → `Ran 282 tests ... OK` |
| 135/135 manifest | **Confirmed** | `python3 scripts/manifest_check.py` → `135/135 checks passed` |
| 15/15 version | Not independently re-run; `tests/test_version_check.py` structure is consistent with the claim | Code inspection only |
| Live counts (33/33, 12/12, 20/20, 36/36, 29/29, 16/16, 30/30, 8/8, 2/2, 11/11, 5/5) | **Not re-executed** (no live cluster in this session) | Script logic inspected; see "Live-check count analysis" below |

The two counts that could be mechanically verified are real, not
fabricated. That is a meaningful, positive signal about this baseline's
honesty — it does not by itself validate the *quality* of what those
282 tests assert, which is the subject of the findings below.

---

## Live-check count analysis (independence vs. inflation)

Every mutating live script (`scaling_check.py`, `rollout_check.py`,
`pdb_check.py`, `dependency_check.py`) follows the same shape:

```python
try:
    _wait_deployment_at(deployment, N, timeout=T)
    record(True, f"...")          # <-- unconditional True
except TimeoutError as exc:
    record(False, f"...: {exc}")
```

`wait_until` (via `_wait_deployment_at` / `_wait_pods_ready` /
`_wait_endpointslice_count`) already enforces the actual numeric
condition inside its predicate; the `record(True, ...)` that follows a
successful wait is not a second, independent assertion — it is a
narration of an already-decided outcome. This is not a correctness bug
(a real failure still surfaces as `record(False, ...)` from the
`except` branch, so nothing false-passes), but it means a meaningful
fraction of each reported "N/N checks passed" total is inflated by
echo-records rather than distinct predicates. Concretely, in
`scaling_check.py`'s `run_scaling_experiment`, roughly half of the
per-component `record()` calls (baseline Ready, baseline Pods Ready,
scale-up Pods Ready, EndpointSlice counts) are this echo pattern; only
the deployment-state and Pod-replacement/annotation/image checks in
`rollout_check.py` and the PDB-status/eviction-classification checks in
`pdb_check.py` re-derive and re-check the condition inside `record()`
itself. Comparing "36/36 rollout/rollback" against "16/16 PDB" as if
they represented proportionally comparable coverage breadth is
therefore misleading — the counts are dominated by how many wait/record
pairs a script's author happened to narrate, not by how many distinct
properties are proven. This should not be used as a proxy for coverage
depth in future days' release-readiness sign-off.

---

## Static manifest mutation coverage (validate_manifests.py)

Every item on the required Day 3 mutation-coverage checklist has a
corresponding negative test in `tests/test_validate_manifests.py`,
looped over `_WORKLOADS` (both gateway and app) via `subTest`:
`replicas`, `RollingUpdate` type, `maxUnavailable`, `maxSurge`,
`minReadySeconds`, `progressDeadlineSeconds`, `revisionHistoryLimit`,
node affinity (removed and weakened), topology spread presence,
`maxSkew`, `topologyKey`, `whenUnsatisfiable` (`DoNotSchedule` mutated
to `ScheduleAnyway`), topology-selector cross-wiring, PDB existence,
PDB `apiVersion`, `minAvailable`, PDB namespace, PDB selector isolation
(own-selector-wrong and cross-workload-selector), `PDB
minAvailable`-vs-`maxUnavailable` shape, Service selector isolation. No
gap found here. The production values for these fields are also
confirmed identical between `app-deployment.yaml` and
`gateway-deployment.yaml` (verified by diff), so the `fails_both_
workloads` pattern is exercising genuinely parallel, not
accidentally-duplicated, logic. This is the strongest part of the Day 3
test baseline.

---

## FINDINGS

### DAY3-TEST-H1 — Restoration/rollback functions are never unit-tested against their own failure branches

- **Severity:** High
- **Evidence:** `restore_workload()` in `scripts/scaling_check.py:195-227`,
  `rollback_workload()` in `scripts/rollout_check.py:302-362`,
  `restore_workload()` in `scripts/pdb_check.py:267-292`, and
  `restore_app()` in `scripts/dependency_check.py:106-141` each contain
  multiple distinct failure branches (the scale/undo command itself
  raising `CalledProcessError`, `_wait_deployment_at`/`_wait_ready`
  timing out, `_wait_pods_ready` timing out, `_wait_endpointslice_count`
  timing out, and — for rollback specifically — the annotation still
  being present, the image not matching, or the post-rollback Pod set
  not being disjoint). In every one of `tests/test_scaling_check.py`,
  `tests/test_rollout_check.py`, `tests/test_pdb_check.py`, and
  `tests/test_dependency_check.py`, the corresponding restoration
  function is *always* replaced with `mock.patch.object(..., "restore_
  workload"/"rollback_workload"/"restore_app", return_value=... or
  side_effect=...)` — grep confirms zero call sites where the real
  function is invoked and asserted against controlled failures of its
  own collaborators. The "restoration failure is reported prominently"
  tests (e.g. `test_restoration_failure_is_reported_prominently`)
  substitute a hand-written `failing_restore()` closure that calls
  `record_restoration(False, ...)` directly — this proves the *outer*
  experiment function correctly surfaces whatever the restoration
  function reports, but proves nothing about whether the real
  restoration function would actually detect and report a failure in
  the first place.
- **Affected files:** `scripts/scaling_check.py`, `scripts/rollout_
  check.py`, `scripts/pdb_check.py`, `scripts/dependency_check.py`,
  and their four matching test files.
- **Specific missing proof:** No test exists that calls the real
  `restore_workload`/`rollback_workload`/`restore_app` with a mocked
  `scale_deployment`/`rollout_undo` raising `CalledProcessError`, or
  with `_wait_deployment_at`/`_wait_ready`/`_wait_pods_ready`/`_wait_
  endpointslice_count` raising `TimeoutError`, and then asserts the
  function (a) returns `False` and (b) appends a `False` entry to
  `restoration_results` with a message that actually identifies which
  sub-step failed. As written, if a future change accidentally
  swallowed one of these `except TimeoutError` blocks (e.g. dropped the
  `return False` after `record_restoration(False, ...)`, letting
  execution fall through to the next block and produce a spurious
  additional `record_restoration(True, ...)`), no unit test would catch
  it — only a live cluster run would, and only if the live scenario
  happened to exercise that exact timeout.
- **Why this matters more than an ordinary coverage gap:** this project's
  own stated design goal (repeated verbatim in every one of these
  scripts' docstrings and in the task brief itself) is "a restoration
  path itself must be testable and fail loudly." Right now that promise
  is enforced only at the integration level, against a live cluster,
  under a task instruction that also says "do not trust the counts
  merely because they were reported" — i.e. the one property this
  project cares most about proving is the one for which unit coverage
  currently only tests that the function *gets called*, not that it
  *works*.
- **Recommended test/remediation:** add one test class per script (e.g.
  `RestorationFailureBranchTests`) that imports and calls the real
  `restore_workload`/`rollback_workload`/`restore_app` directly (not
  mocked away), mocking only their lowest-level collaborators
  (`scale_deployment`, `rollout_undo`, `_wait_deployment_at`, `_wait_
  ready`, `_wait_pods_ready`, `_wait_endpointslice_count`, `get_image`,
  `get_annotation`) to raise/return failure values one at a time, and
  assert both the return value and the exact `restoration_results`
  content. `dependency_check.py`'s own `_app_endpoints_drained` is
  already unit-tested this way (directly, unmocked, DAY2-TEST-H1) —
  the same treatment was never extended to the restoration/rollback
  functions that carry equivalent safety weight.
- **Blocking status:** Blocking. This is the single highest-value gap
  relative to the review's explicit charge ("a restoration path itself
  must be testable and fail loudly").

---

### DAY3-TEST-H2 — `final_state_check.py` has zero unit test coverage, including its termination-race settlement predicate

- **Severity:** High
- **Evidence:** `scripts/final_state_check.py` is a new Day 3 script
  (the "30/30 final-state" live check) whose central piece of logic is
  `_settled_snapshot()` (lines 59-90) — a hand-rolled termination-race
  guard structurally identical in purpose to `dependency_check.py`'s
  `_app_endpoints_drained()` (which *does* have direct, unmocked unit
  tests in `tests/test_dependency_check.py::AppEndpointsDrainedTests`,
  explicitly because a prior review, DAY2-TEST-H1, flagged the same
  class of race). No `tests/test_final_state_check.py` exists at all —
  confirmed by directory listing and grep across `tests/` for any
  reference to `final_state_check`.
- **Affected files:** `scripts/final_state_check.py` (no matching test
  file).
- **Specific missing proof:** `_settled_snapshot`'s three-way
  agreement requirement (Deployment desired/Ready, live Pod count/Ready,
  EndpointSlice ready-count must all simultaneously equal
  `EXPECTED_REPLICAS`) is untested: no test proves that a Deployment
  reporting `readyReplicas == 3` while Pods are still `2/3` returns
  `None` (retry) rather than a premature settled snapshot; no test
  proves `check_workload_final_state`'s fallback path (lines 96-100,
  taken when `_settled_snapshot` times out) correctly falls back to
  live `get_json` calls rather than silently reporting stale data as
  success; no test proves `check_pdb_final_state`'s compound boolean
  (`currentHealthy == 3 and desiredHealthy == 2 and disruptionsAllowed
  == 1`) actually fails when only one of the three sub-fields drifts
  (only demonstrated live, never as an isolated fixture-driven test).
- **Recommended test/remediation:** add `tests/test_final_state_check.py`
  mirroring the `AppEndpointsDrainedTests` pattern — mock `get_json` to
  return Deployment/Pod/EndpointSlice fixtures for each of the "not yet
  settled" cases (Deployment ready but Pods not yet caught up;
  Deployment ready and Pods ready but EndpointSlice not yet caught up;
  all three genuinely agreeing) and assert `_settled_snapshot`'s
  predicate returns `None` vs. the real tuple accordingly. Add a
  fixture-driven test for `check_pdb_final_state`'s three-field
  conjunction analogous to the PDB `_pdb_matches` tests missing in
  DAY3-TEST-M1 below.
- **Blocking status:** Blocking. This script did not exist before Day
  3, so this is not carried-forward debt — it is a brand-new safety-net
  script that shipped with no unit coverage at all, in a day whose
  entire purpose is proving safe restoration after mutation experiments.

---

### DAY3-TEST-M1 — `get_pdb_status`/`_pdb_matches` (PDB status parsing) is never exercised as a unit, only through a mocked `_wait_pdb_state`

- **Severity:** Medium
- **Evidence:** `scripts/pdb_check.py:66-98` defines `get_pdb_status`
  (extracts `.get("status", {})` from the raw `kubectl get pdb -o json`
  object) and `_pdb_matches` (compares three status fields against
  expected values). Every test in `tests/test_pdb_check.py` that
  exercises PDB state mocks `_wait_pdb_state` directly with pre-built
  dicts (`side_effect=[{"currentHealthy": 3}, {"currentHealthy": 2,
  "disruptionsAllowed": 0}]`) — `get_pdb_status` and `_pdb_matches` are
  never called in any test, mocked or otherwise.
- **Affected files:** `scripts/pdb_check.py`, `tests/test_pdb_check.py`.
- **Specific missing proof:** no test proves `get_pdb_status` correctly
  unwraps a realistic `kubectl get poddisruptionbudget -o json` shape
  (top-level `status` key, possibly absent entirely on a
  freshly-created PDB before the controller has computed it), and no
  test proves `_pdb_matches` correctly returns `None` (triggering
  retry) when only one of `currentHealthy`/`desiredHealthy`/
  `disruptionsAllowed` fails to match rather than all three.
- **Recommended test/remediation:** add a small `PdbStatusParsingTests`
  class that mocks `get_json` (not `_wait_pdb_state`) with a
  realistic-shaped PDB object (including a case with `status: {}`
  entirely, which real freshly-applied PDBs can return) and asserts
  `get_pdb_status`/`_pdb_matches` behave correctly, matching the
  treatment `count_ready_endpoints` and `_app_endpoints_drained`
  already received elsewhere in this codebase.
- **Blocking status:** Non-blocking. `_pdb_matches`'s logic is a
  three-field equality conjunction with negligible branching risk, and
  it is exercised indirectly (through `_wait_pdb_state`'s mocked
  return values) in every PDB experiment test — the gap is real but
  low-risk relative to DAY3-TEST-H1/H2.

---

### DAY3-TEST-M2 — `PodsActuallyReplacedTests` tests Python's own `set.isdisjoint`, not the production assertion it claims to cover

- **Severity:** Medium
- **Evidence:** `tests/test_rollout_check.py:104-118`:

  ```python
  class PodsActuallyReplacedTests(unittest.TestCase):
      """The "Pods were actually replaced" assertion must be a real
      disjoint-set check..."""

      def test_disjoint_uid_sets_are_a_real_replacement(self):
          baseline = {"uid-1", "uid-2", "uid-3"}
          after = {"uid-4", "uid-5", "uid-6"}
          self.assertTrue(after.isdisjoint(baseline))

      def test_identical_uid_sets_are_not_a_replacement(self):
          baseline = {"uid-1", "uid-2", "uid-3"}
          after = {"uid-1", "uid-2", "uid-3"}
          self.assertFalse(after.isdisjoint(baseline))
  ```

  These two tests never call into `rollout_check.py` at all. They
  construct two Python `set` literals and call the stdlib
  `set.isdisjoint` method directly. The actual production assertion
  they claim to protect is `rollout_check.py:273-276`:

  ```python
  record(
      len(post_rollout_uids) == EXPECTED_REPLICAS and post_rollout_uids.isdisjoint(baseline_uids),
      ...
  )
  ```

  which additionally requires `len(post_rollout_uids) == EXPECTED_
  REPLICAS` — a condition the two unit tests never construct or check.
  This is exactly the pattern the review brief asks to be flagged:
  "tests that mock away the exact production logic they claim to
  test" — here it is not mocking, but an equivalent failure mode:
  re-implementing the assertion inline with raw values instead of
  calling the module under test, so a regression in `rollout_check.py`
  itself (e.g. someone changes `and` to `or`, or drops the length
  check, or swaps `post_rollout_uids.isdisjoint(baseline_uids)` for
  `baseline_uids.isdisjoint(post_rollout_uids)` in a context where the
  two aren't symmetric due to a mutation elsewhere) would leave these
  two tests passing unchanged, because they never touch the module.
- **Affected files:** `tests/test_rollout_check.py`.
- **Specific missing proof:** no test drives `run_rollout_experiment`
  (or a small extracted helper, if one were pulled out of the inline
  `record(...)` call) with `_wait_exact_pod_count` mocked to return a
  UID set that overlaps with `baseline_uids` (a no-op/failed rollout)
  and asserts that the resulting `results` entry for "Pods were
  actually replaced" is `False`; nor with a UID set whose length is 3
  but which shares one UID with baseline (partial replacement) to prove
  the compound condition — as currently structured this specific
  compound check has no way to be independently verified since it's an
  inline boolean expression rather than an extracted function.
- **Recommended test/remediation:** either (a) extract the compound
  condition into a small named function (e.g. `_pods_were_replaced(post
  _uids, baseline_uids, expected_count)`) and unit-test that function
  directly with the overlap/undersized/oversized cases above, or (b)
  at minimum, drive `run_rollout_experiment` end-to-end with `_wait_
  exact_pod_count` mocked to return an overlapping set and assert the
  specific `results` message reflects failure. Delete the current two
  tests once real coverage exists — they currently provide no
  regression protection at all.
- **Blocking status:** Non-blocking on its own (the underlying
  production logic is simple and low-risk), but should not be counted
  toward "rollout mutation coverage" in any future baseline — it
  currently contributes to the 282-test total without contributing
  proof.

---

### DAY3-TEST-L1 — "Restoration after unexpected Eviction" is not explicitly asserted, only structurally guaranteed

- **Severity:** Low
- **Evidence:** `tests/test_pdb_check.py::test_unexpected_eviction_
  success_is_a_failure` (lines 208-224) correctly proves that an
  eviction which unexpectedly succeeds is recorded as a failure, but
  the test's `restore_workload` mock (`return_value=True`) is never
  asserted as called (no `mock_restore.assert_called_once()`). The
  guarantee that restoration still runs after this scenario currently
  holds only because `finally: if scaled_down: restore_workload(...)`
  is unconditional once `scaled_down` is set earlier in the function —
  a structural guarantee proven generically by the separate
  `RestorationGuaranteeTests` class, not by this specific test.
- **Affected files:** `tests/test_pdb_check.py`.
- **Specific missing proof:** an explicit `mock_restore.assert_called_
  once()` in `test_unexpected_eviction_success_is_a_failure` itself, so
  the "unexpected success still restores" property is proven at the
  same test site that proves "unexpected success is a failure," rather
  than relying on the reader to connect it to a different test class.
- **Recommended test/remediation:** add the missing assertion to the
  existing test (one line); no new test class needed.
- **Blocking status:** Non-blocking.

---

### DAY3-TEST-L2 — Live-check "N/N passed" counts mix independent assertions with unconditional echo-records

- **Severity:** Low
- **Evidence / detail:** see "Live-check count analysis" above.
- **Affected files:** `scripts/scaling_check.py`, `scripts/rollout_
  check.py`, `scripts/pdb_check.py`, `scripts/dependency_check.py`.
- **Specific missing proof:** not a missing test — this is a reporting-
  clarity issue. No mechanism distinguishes an echo-record (a
  `record(True, ...)` that only fires because a prior `wait_until` did
  not raise) from an independently-recomputed assertion in the printed
  summary or in any downstream consumer of these counts.
- **Recommended remediation:** non-blocking; if these scripts are
  revisited, consider either dropping the echo-records (let the
  `except TimeoutError: record(False, ...)` be the only place that
  reports failure of a `wait_until`-guarded property, since success is
  implied by reaching the next line) or annotating them distinctly
  (e.g. a `record_observed` helper) so future counts aren't compared
  apples-to-oranges across scripts.
- **Blocking status:** Non-blocking. Informational — flagged because
  the review brief specifically asked whether live counts are
  independent or inflated.

---

## Summary

The static-manifest mutation coverage (the largest, most mechanically
verifiable part of this baseline) is genuinely thorough and correctly
parameterized across both workloads — no gap found there. The unit
test count (282) and manifest count (135) are real, confirmed by
re-running them in this session, not merely reported. `endpointslice.py`,
`http_checks.py` (including the DAY2-INT-L2 body-semantics work
carried forward), and the PDB eviction-classification/victim-selection
logic are all soundly, directly unit-tested against real production
functions with well-chosen edge cases.

The weak point is concentrated exactly where the review brief said to
look hardest: the restoration/rollback paths. Every one of `scaling_
check.py`, `rollout_check.py`, `pdb_check.py`, and `dependency_check.py`
mocks its own restoration function completely out of existence in every
unit test, so the actual failure-detection logic inside those functions
— the logic responsible for ever raising the "RESTORATION FAILURE -
independent action required" alarm in a real run — has never been unit
tested. `final_state_check.py`, this day's dedicated final-safety-net
script, has no unit tests at all. These two findings (DAY3-TEST-H1,
DAY3-TEST-H2) are the ones worth blocking on; the rest are real but
smaller.

## Verdict

**REQUEST CHANGES**

Close DAY3-TEST-H1 and DAY3-TEST-H2 before this baseline is treated as
adequate proof of the restoration guarantees Day 3's own scripts claim
to make. DAY3-TEST-M1, DAY3-TEST-M2, DAY3-TEST-L1, and DAY3-TEST-L2 do
not block but should be tracked.

PROJECT 4 DAY 3 KUBERNETES TEST REVIEW COMPLETE

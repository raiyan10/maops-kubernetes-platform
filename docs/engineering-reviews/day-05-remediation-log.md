# Day 5 / v0.5.0 — Remediation Log

This log records every code/doc change made in response to the five independent Day 5 reviews
(architecture, security, cluster-integration, test, release-readiness), per the task's instruction
that Critical/High/Medium findings be remediated in a separate, bounded step with regression tests
and static re-checks — never by silently weakening a check.

---

## Remediated

### DAY5-TEST-H1 (High) — zero fast-test coverage for `cni_check.py`/`networkpolicy_check.py`/`rbac_check.py`
- Added `tests/test_cni_check.py` (7 tests, `_is_ready()`).
- Added `tests/test_networkpolicy_check.py` (21 tests: probe-snippet templating, `_run_probe()`'s
  full ALLOWED/DENIED decision matrix, `_validation_pod_manifest()` identity wiring).
- Added `tests/test_rbac_check.py` (12 tests: `_parse_cases()` log parsing,
  `_pod_manifest()` identity/RBAC wiring, cross-check against `_EXPECTED`).
- No source-script behavior changed — pure test-coverage addition.

### DAY5-INT-H2 / DAY5-REL-M1 (High/Medium, duplicate finding) — `final_state_check.py`'s `OTHER_DAY_CLUSTERS` never grew to include Day 4
- `scripts/final_state_check.py`: `OTHER_DAY_CLUSTERS` now includes `"maops-k8s-day4"`; module
  docstring and `main()`'s print/PASS text refreshed from stale "Day 4" wording to "Day 5";
  `check_no_leaked_port_forwards()`'s docstring/message refreshed similarly;
  `check_other_day_clusters_still_exist()`'s docstring refreshed to explain the extension.
- `tests/test_final_state_check.py`: `OtherDayClustersStillExistTests` extended with
  `test_day5_checks_day4_specifically` (asserts the constant), `test_all_four_clusters_present_passes`
  (renamed/extended from the old two-cluster fixture), and `test_missing_day4_cluster_fails`
  (regression: Day 4 missing must independently flip the check, not just Days 1-3).

### DAY5-ARCH-M1 (Medium) — `Makefile`'s `cni-install` didn't reproduce the live cluster's actual Helm values
- `Makefile`: `cni-install` target now also passes `--set hubble.enabled=false` and
  `--set image.pullPolicy=IfNotPresent`, matching what the live, already-validated cluster was
  actually installed with (confirmed via `helm get values cilium` during the architecture review).
- `docs/architecture.md`'s "what Day 5 explicitly does not claim" section updated to list all four
  pinned Helm values accurately.

### DAY5-SEC-M1 (Medium) — DAY4-SEC-L1 needed explicit re-adjudication, not silent closure
- `docs/architecture.md`'s DAY4-SEC-L1 scope note rewritten to explicitly state **"REDUCED, not
  CLOSED"**, with the live evidence from both sub-risks (pod-to-pod closed; `kubectl port-forward`
  still open, unchanged, already-disclosed architectural limitation).

---

## Accepted as documented limitations (not code-remediated this pass)

These are real, live-confirmed findings where the correct disposition — per the reviewing agents'
own recommendations — is documentation, not a code change, because the underlying cause is either
environmental (host resource pressure) or an intentional, already-scoped-out architectural property
(NetworkPolicy's L3/L4-only reach):

- **DAY5-ARCH-M2 / DAY5-INT-H1 / DAY5-SEC-I2** (Cilium operator leader-election crash-looping, Helm
  release `STATUS: failed`) — documented in `docs/architecture.md`'s new
  "DAY5-ARCH-M2/M3" section. Root cause is host resource pressure (see DAY5-INT-C1 below); the
  per-node Cilium agent that actually enforces NetworkPolicy remains healthy and was proven so live.
- **DAY5-ARCH-M3 / DAY5-INT-M1** (3-node Cilium topology resource footprint, undocumented sizing) —
  documented in the same new section, with concrete mitigation suggestions
  (`operator.replicas=1`/`envoy.enabled=false`, stopping superseded clusters) for future days.
- **DAY5-ARCH-L1** (`cilium-envoy` runs by chart default, unacknowledged) — documented.
- **DAY5-ARCH-L2 / DAY5-SEC-L1** (`maops-day5-validation` namespace has no NetworkPolicy of its
  own) — documented, with the mitigating separate-identity design explained; carried forward as a
  Day 6/7 candidate.
- **DAY5-SEC-I1** (no Pod Security Admission labels on either namespace) — informational, not a Day
  5 regression, not required by Day 5 scope.
- **DAY5-INT-M2** (transient rollout-check instability observed in a preserved failed-attempt
  evidence directory before the eventually-successful run) — the underlying defect appears already
  fixed in the run that counts; flagged as an operating-practice note only.

## Unresolved — outside this remediation step's authority

- **DAY5-INT-C1 (Critical)** — Day 1-4 kind cluster node containers are currently dead (host
  resource exhaustion, two synchronized SIGKILL bursts observed live). This is pre-existing host
  state discovered during review, not a defect in the Day 5 diff. Fixing it would require either
  starting/touching the Day 1-4 clusters (explicitly forbidden by this task) or freeing host
  memory/reducing concurrently-live clusters (an operating-environment decision outside a bounded
  code-remediation step). **Not remediated. Flagged to the user** — see the final adjudication.
- **DAY5-REL-M2 (Medium)** — `docs/roadmap.md`'s Day 6/Day 7 scope was restructured (service mesh
  moved from Day 7 to Day 6) as part of this branch's diff. This is a planning/authorization
  question, not a code defect, and this review has no visibility into whether it was an explicitly
  requested change. **Not remediated — flagged to the user for confirmation.**

---

## Verification after remediation

- `python3 -m unittest discover -s tests` → **814 tests, OK** (up from 773 at the time of the
  original authoritative run; +41 from the three new test files plus the extended
  `test_final_state_check.py` coverage).
- `python3 scripts/manifest_check.py k8s/base` → **267/267**, unchanged (manifest content itself was
  not touched by this remediation).
- `python3 scripts/version_check.py` → **35/35**, unchanged.
- **The live authoritative gate (`make day5-check`) was NOT rerun.** Per the task's explicit
  constraints ("do not rerun the full live suite merely to collect redundant evidence," "do not
  start Day 1-4 clusters"), and because DAY5-INT-C1 means Day 4's cluster is currently non-functional
  — a live rerun right now would (correctly) fail the newly-strengthened
  `check_other_day_clusters_still_exist()` assertion on `maops-k8s-day4` specifically, which is the
  fix working as intended given the current host state, not evidence the fix is wrong. None of the
  remediations in this log change previously-validated application/NetworkPolicy/RBAC behavior — the
  Makefile change brings the recipe in line with what was already running and validated; the
  `final_state_check.py` change only adds a new assertion; the new test files add coverage without
  touching any script's runtime logic; the documentation changes are non-functional. A fresh live
  rerun is not required to trust the remediation, and none was performed.

---

## Owner disposition (post-remediation addendum)

Added on explicit owner instruction, after the remediation above. Every section above is preserved
byte-for-byte. Full reasoning for both decisions is recorded in
`docs/engineering-reviews/day-05-final-adjudication.md`'s own "Owner disposition" addendum; this
entry is the remediation-log-side record of the same two closures.

- **DAY5-INT-C1 (Critical) — owner-accepted, not fixed.** The owner confirms Day 1-4 kind cluster
  containers being stopped/exited is an environment-only limitation, not caused by any Day 5 code,
  manifest, release artifact, tag, or historical evidence, and that Day 1-4 will not be restarted or
  investigated as part of this release. Day 1-4's repositories, tags, reviews, and evidence remain
  preserved. Day 5 makes no claim that the Day 1-4 runtime clusters are currently healthy or
  internally unchanged. This finding is **not** marked "Remediated" above and its status is
  unchanged in the register — this entry records only that the owner has explicitly accepted it as
  a disclosed, non-blocking, environment-only limitation, and confirms the standing
  recommendation that future multi-day validation on a constrained host should stop superseded
  clusters (or otherwise reserve adequate WSL2/Docker memory) before running a later day's suite.
- **DAY5-REL-M2 (Medium) — closed by owner confirmation.** The `docs/roadmap.md` Day 6/7
  restructuring (service mesh → Day 6; Recreate/Blue-Green/Canary → Day 7) is confirmed intentional
  and approved. Closed as an authorization/documentation question; no implementation change
  required.

### Re-verification after this addendum

Per explicit owner instruction, only the following were run (no live cluster rerun, no Day 1-4
cluster started or touched, no commit/push/tag/PR):

- `python3 -m unittest discover -s tests` → **814 tests, OK** (unchanged from the prior remediation
  pass — this addendum is documentation-only and touches no test or source file).
- `make manifest-check` → **267/267**, unchanged.
- `make version-check` → **35/35**, unchanged.
- `git diff --check` → clean (no whitespace errors/conflict markers in any changed file).

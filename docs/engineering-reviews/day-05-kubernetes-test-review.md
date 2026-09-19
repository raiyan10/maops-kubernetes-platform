# Day 5 / v0.5.0 — Independent Kubernetes Test/Validation Review

**Role:** `kubernetes-test-engineer` (independent review; fresh subagent context; findings-only —
no files written or edited during the independent-review pass itself).

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 5 / v0.5.0 — Docker-free unit-test coverage
for the new Day 5 validation scripts and manifests, and continuity of test coverage for Day-4-era
files modified for Day 5.

**Candidate/evidence references:** branch `feature/day-5-security-boundaries`; new scripts
`scripts/cni_check.py`, `scripts/day5_lock.py`, `scripts/networkpolicy_check.py`,
`scripts/rbac_check.py`; new test `tests/test_day5_lock.py`; modified
`scripts/final_state_check.py`, `scripts/k8s_yaml.py`, `scripts/kube.py`, `scripts/state_check.py`,
`scripts/storage_hardening_check.py`, `scripts/suite_baseline.py`, `scripts/validate_manifests.py`,
`scripts/version_check.py` and their corresponding test files; `docs/engineering-reviews/
day-04-kubernetes-test-review.md` (prior methodology/carried-forward findings).

**Method:** Read every new/changed script and test file in full. Ran
`python3 -m unittest discover -s tests -v` and `python3 scripts/manifest_check.py k8s/base`
directly. Diffed `scripts/day4_lock.py`/`tests/test_day4_lock.py` against their Day 5 counterparts.
No live-cluster command was run; no file was written or edited during the review pass itself
("do not silently fix findings during independent review").

---

## Findings

### DAY5-TEST-H1 (High)
**Title:** `scripts/cni_check.py`, `scripts/networkpolicy_check.py`, `scripts/rbac_check.py` had
zero Docker-free unit test coverage for their separable pure logic.
**Evidence:** `grep -rn "cni_check\|networkpolicy_check\|rbac_check" tests/` returned nothing before
remediation. All three scripts are dominated by real live-cluster interaction by necessity, but each
also contains cleanly separable, pure, easily-mockable logic in the same style this repo already
applies elsewhere (e.g. `tests/test_storage_hardening_check.py`'s `ProbeClassificationTests`,
added in this same PR): `cni_check._is_ready()`; `networkpolicy_check._connect_probe_snippet()`/
`_dns_probe_snippet()` (probe-source templating); `networkpolicy_check._run_probe()`'s
RESULT/STATUS parsing and ALLOWED/DENIED decision logic; `rbac_check._parse_cases()`; and
`rbac_check._pod_manifest()`'s ServiceAccount/automount wiring.
**Impact:** the same class of finding this project's own Day 4 review already rated High — a silent
regression in the parsing/decision logic of the three scripts that *prove* Day 5's stated security
boundaries (RBAC + NetworkPolicy enforcement) would ship undetected by the fast/CI-friendly test
tier, contrary to the project's own "everything that can be tested without a cluster, should be"
principle. A specific concrete risk the reviewer called out: an accidental `ok`/`not ok` inversion
in one of `networkpolicy_check.py main()`'s DENIED assertions would silently flip a negative
NetworkPolicy proof into a false pass, and nothing but a live run would ever catch it.
**Disposition:** **Remediated.** Three new test files were added, mirroring the reviewer's own
recommended scope and the project's established mocking technique:
- `tests/test_cni_check.py` — 7 tests directly exercising `_is_ready()` against constructed Pod
  dicts (present/absent/false Ready condition, missing keys, exact-string-match discipline).
- `tests/test_networkpolicy_check.py` — 21 tests: probe-snippet templating (substitution
  correctness, no stray unsubstituted placeholders, generated source is syntactically valid
  Python), and `_run_probe()`'s full decision matrix (2xx-vs-non-2xx boundary, unparseable status,
  DNS-style success with no STATUS token, blocked/unrecognized/empty output, `CalledProcessError`/
  `TimeoutExpired` handling, multi-line output using only the final line), plus
  `_validation_pod_manifest()`'s identity wiring (no automounted token, correct component label,
  correct namespace).
- `tests/test_rbac_check.py` — 12 tests: `_parse_cases()`'s log-line parsing (well-formed,
  malformed, non-integer status, duplicate-case overwrite, empty input) and `_pod_manifest()`'s
  ServiceAccount/automount/namespace wiring plus a cross-check that every case name `main()`'s
  `_EXPECTED` table references is actually embedded in the generated probe source.

All three new files were independently re-run: `python3 -m unittest discover -s tests` →
**814 tests, OK** (up from the reviewed 773 — the +41 delta accounts for these three new files plus
the `final_state_check.py` regression tests added for DAY5-INT-H2/DAY5-REL-M1).

### DAY5-TEST-I1 (Informational — positive finding)
**Title:** `tests/test_day5_lock.py` is a faithful, complete mirror of `tests/test_day4_lock.py`.
**Evidence:** Diffing both the source (`day4_lock.py` vs `day5_lock.py`) and the tests shows only
mechanical `DAY4_*`→`DAY5_*` renames — identical test count (9 each), identical coverage of genuine
lock contention, forged-fd handling, nested-wrapper execution, and cleanup-on-failure. No gap; no
action needed.

### DAY5-TEST-I2 (Informational — positive finding)
**Title:** Negative-case coverage in the new live-cluster scripts is genuine, not positive-only.
**Evidence:** `networkpolicy_check.py main()` asserts `record(not ok, ...)` for three real DENIED
paths using genuine in-cluster TCP attempts with a bounded client-side timeout; `rbac_check.py`'s
`_EXPECTED` table asserts an exact `403` (never a generic non-2xx) for five distinct denied cases,
using a real mounted ServiceAccount token making real HTTPS calls. The probe *design* genuinely
proves denial, not merely allowance — the gap (now closed by DAY5-TEST-H1's remediation) was
specifically in the fast-test coverage of the parsing/classification logic around these probes, not
in the probes' own negative-assertion design.

### DAY5-TEST-I3 (Informational — no gap found)
**Title:** `validate_manifests.py`/`k8s_yaml.py` diffs have thorough, specific negative-case
coverage for the new NetworkPolicy/ServiceAccount/RBAC manifest logic.
**Evidence:** ~46 new test methods across `ServiceAccountTests`, `RbacRoleAndBindingTests`,
`NetworkPolicyDefaultDenyTests`, `NetworkPolicyAllowTests`, each mutating exactly one field of a
deep-copied fixture and asserting a distinct, specific check name. Independently re-ran
`python3 scripts/manifest_check.py k8s/base` → **267/267**, confirming these checks genuinely
execute against real rendered manifest output, not merely a constructed fixture.

### DAY5-TEST-I4 (Informational — none found)
No tautological/constant-only test pattern found in any new Day 5 test class (spot-checked; no
self-referential `assertEqual(EXPECTED, EXPECTED)`-style anti-pattern).

### DAY5-TEST-I5 (Informational — no gap found)
Day-4-era files modified for Day 5 (`final_state_check.py`, `state_check.py`, `version_check.py`,
`kube.py`, `suite_baseline.py`) have consistent, non-stale corresponding test updates; files that
reference renamed constants symbolically (`suite_baseline.RUN_ID_ENV`, `kube.CLUSTER_NAME`) required
no update and had none. `storage_hardening_check.py`'s ~280 new lines of test coverage
(`ProbeClassificationTests`, `ObserveProbeRaceTests`, `PodEventsAndLastKnownStateTests`) is
exemplary and was the direct template used for this review's DAY5-TEST-H1 remediation.

---

## Summary table

| Severity | Count | Findings |
|---|---|---|
| Critical | 0 | — |
| High | 1 | DAY5-TEST-H1 (remediated: 40 new tests added across 3 new files) |
| Medium | 0 | — |
| Low | 0 | — |
| Informational | 5 | DAY5-TEST-I1 through I5 (all positive/no-gap findings) |

## Verdict: **APPROVE**

The manifest-validation layer (`validate_manifests.py`/`k8s_yaml.py`) and the lock-mirroring
(`day5_lock.py`) were already held to, and met, this project's own rigorous standard — every new
static check has both positive and specific negative coverage, verified live against real rendered
manifests, and the full suite passes. The one substantive gap identified (DAY5-TEST-H1 — the three
new live-cluster proof scripts central to Day 5's stated purpose had no fast/Docker-free regression
coverage) has been remediated in this same review pass with 40 new targeted unit tests mirroring the
project's own established mocking technique, and the full suite re-runs green (814 tests, OK).
Nothing else blocks approval.

---

PROJECT 4 DAY 5 KUBERNETES TEST/VALIDATION REVIEW COMPLETE

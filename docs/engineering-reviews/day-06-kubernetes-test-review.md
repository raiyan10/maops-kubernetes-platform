# Day 6 / v0.6.0 — Independent Kubernetes Test and Validation Review

**Role:** `kubernetes-test-engineer` (independent review — not the implementer; fresh subagent
context, run in parallel with the other four reviewers and never shown their conclusions). For
this review the role's normal write access was explicitly withheld: **read-only, no new tests**.

**Date:** 2026-09-24.

**Scope:** Unit-test and validation-code quality for Day 6 / v0.6.0 — false-positive resistance,
probe classification, NetworkPolicy isolation from ambient interception, mesh evidence tiers,
cleanup tri-state behavior, checksum determinism/isolation, lifecycle replacement and rollback
paths, Makefile ordering, validator coverage, mock fidelity. Branch
`feature/day-6-helm-routing-mesh`, HEAD `3b784a78fa41b807a509a32e7028c67bf0a28777`.

**Method:** Read every Day 6 test and the code under test; diffed the modified existing tests
against HEAD for removed assertions; ran `python3 -m unittest discover -s tests -v` (cluster-free,
confirmed it writes nothing into the repo). No file edited; the cluster was not touched.

---

## 1. Verdict

**PASS WITH NON-BLOCKING NOTES.** Release may proceed from a test-quality standpoint; TEST-1 is
recommended before or shortly after release.

## 2. Findings

| ID | Severity | File / reference | Evidence | Impact | Required remediation |
|---|---|---|---|---|---|
| TEST-1 | MEDIUM | `tests/test_helm_lifecycle_check.py` `MainOrchestrationTests` (258-406); `scripts/helm_lifecycle_check.py:489-491` | Covered: upgrade fails → no rollback; post-upgrade PVC drift; full success; baseline API error. Not covered: `helm rollback` returning nonzero through `main()`, or an exception raised after upgrade submission reaching the `finally` rollback. No `rollback…returncode=1`, `side_effect=RuntimeError`, or `assertRaises` in the file. | The `RESTORATION FAILURE` path and the exception-survives-into-`finally` guarantee are real code with no test — exactly the "rollback fails" and "exception mid-way" cases the brief names. | Add a `main()` test with `rollback` → `returncode=1` asserting exit 1 and `RESTORATION FAILURE`; add a test where a post-upgrade step raises, asserting rollback is still called. |
| TEST-2 | LOW | `tests/test_networkpolicy_check.py:738-931` (`CheckApplicationPortNetworkPolicyIsolatedTests`) vs `scripts/networkpolicy_check.py:869-871` | The fixture `_running_pod()` always returns an isolated Pod, so the real `_verify_probe_pod_isolated()` always returns True at orchestration level. The gates have unit tests (`VerifyProbeIsolationTests`, `PodIpNotInEndpointSliceTests`) but no test drives a gate failure through `check_application_port_networkpolicy_isolated()`. A manual trace shows the code fails closed. | A refactor of the `continue` control flow could start asserting against a non-isolated Pod unnoticed. | Add an orchestration-level case where one probe Pod fails isolation (or has its IP in an EndpointSlice) and assert fail-closed, no probe against it, cleanup still runs. |
| TEST-3 | NOTE | `scripts/mesh_check.py:944-1016` (`_find_denial_evidence`), `:1041-1046` (`_record_log_correlation`) | CANDIDATE accepts any correlated non-empty `error=` that is not one of the two AUTHORITATIVE strings, and is recorded `True` exactly like AUTHORITATIVE. Tier tests are correct but cannot rule out a live non-authorization error producing a CANDIDATE pass. | "45/45" does not by itself mean all denial evidence was AUTHORITATIVE. | No code change required; the live evidence record should break out AUTHORITATIVE / CANDIDATE / BEST_EFFORT counts. |

No BLOCKER or HIGH findings.

## 3. Confirmed with no finding

- **Unit tests:** `Ran 1161 tests … OK`; `[FAIL]` lines in the output are the scripts' own
  logging under mocked negative fixtures; exit 0; the suite writes nothing to the repo;
  `test_helm_chart_values_schema.py` genuinely runs `helm template` (render-only).
- **Git:** HEAD as expected; nothing staged; `git diff --check` clean.
- **Scope separation:** `validate_manifests.py`, its tests, and `k8s/base` untouched; Day 5 pins
  (`0.5.0` / `maops-kubernetes-platform-day5`) unchanged.
- **Modified tests not weakened:** every removed assertion in `test_version_check.py`,
  `test_final_state_check.py`, `test_networkpolicy_check.py` is matched or exceeded (e.g. the
  seven-state `ProbeResult` replacing the old ok/detail contract).
- **Classification:** `assert_denied()` accepts only `TCP_CONNECT_TIMEOUT`; neither INCONCLUSIVE
  state, `HTTP_READ_TIMEOUT`, nor `CONNECTION_REFUSED_OR_RESET` satisfies either assertion;
  real-socket tests (`RealSocketClassificationTests`) prove the generated probe snippets.
- **Probe isolation:** manifests use `istio.io/dataplane-mode: none` and one component label;
  `_verify_probe_pod_isolated()` rejects the redirection annotation, a sidecar, controller
  adoption, and a wrong/missing label.
- **Checksum determinism/isolation:** real-render tests change one component's config and assert
  the other two checksums are byte-identical; `_check_config_checksum` requires the Pod-template
  level and pairwise distinctness.
- **Rollback-always structure:** `upgrade_submitted` is set right after the upgrade call (line
  433), before any check that can fail; the `finally` only skips rollback when no upgrade was
  submitted.
- **Lifecycle replacement tests:** `VerifyRolloutDivergedTests` cover checksum-without-config,
  unchanged UIDs, partial replacement, lagging `observedGeneration`, not-all-ready, same
  ReplicaSet, and the positive case.
- **Makefile ordering:** `tests/test_makefile_sequence.py` parses the real Makefile and asserts the
  post-CNI order; cross-checked against `Makefile:361-403`.
- **Run variables, fsGroup negatives, `day6_lock.py` tests, no secrets or local paths in new
  files, frozen files unchanged.**

## 4. Remaining live/environment limitations

- Entirely static; no live target was run. The mesh tier breakdown can only come from a live
  run's output.
- A live `maops-k8s-day6` cluster existed during the review; it was not touched.

## 5. Whether release may proceed

**Yes** from a test/validation-quality standpoint. Findings are MEDIUM/LOW/NOTE; none is a
currently broken behavior. TEST-1 should be addressed before or shortly after release.

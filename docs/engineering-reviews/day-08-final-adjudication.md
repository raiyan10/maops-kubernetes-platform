# Day 8 / v1.0.0 work - final adjudication (implementer's synthesis; release decision is the owner's)

**Prepared by:** the implementing session (Claude Code). Inputs:
- the five reviews, preserved verbatim in `day-08-independent-reviews.md`;
- the remediation in `day-08-remediation-log.md`;
- the live evidence in `day-08-live-validation-record.md`.

This document recommends. It is not a commit, tag or release, and it does
not claim production readiness. Day 8 is a **local Kind reference
platform** stage.

**Date:** 2026-10-03. **Branch:** `feature/day-8-autoscaling-hardening`,
base `3d19075e483d402179fe33d0c4fb0d7357a681b8`, all changes unstaged.

## Verdicts at review time

| Agent | Verdict then | HIGH | MEDIUM |
|---|---|---|---|
| kubernetes-architect | GO | 0 | 1 |
| kubernetes-security-reviewer | GO | 0 | 1 |
| cluster-integration-engineer | GO | 0 | 2 |
| kubernetes-test-engineer | GO, conditional on T1 | 1 | 4 |
| release-engineer | NO-GO until adjudicated | 0 | 4 |

## Decisions per finding

Key: **ACCEPT** = fixed in this branch; **ACCEPT (doc)** = documented;
**DEFER** = accepted limitation, not fixed now; **NOTED** = no action
needed. Remediation sections (§) refer to `day-08-remediation-log.md`.

### kubernetes-architect

| # | Sev | Finding | Decision |
|---|---|---|---|
| A1 | MEDIUM | SubjectAccessReview breadth | **ACCEPT** (§4): 22 forbidden actions in `maops-platform`, Secret reads in 4 other namespaces and in all namespaces, VPA/Metrics Server identities probed in both modes, expected grants measured. Wording is now "for the probed verbs". |
| A2 | LOW | KEDA webhook failurePolicy unasserted | **ACCEPT, different value** (§7): assessed and pinned to `Fail`, not the suggested `Ignore`, because the webhooks match only KEDA's own API groups. Asserted statically and live. |
| A3 | LOW | Failed demo loses live diagnostics | **DEFER.** Per-phase JSON evidence survives; a snapshot subcommand is a useful follow-up but is not needed for correctness. |
| A4 | LOW | Budget "no padding" / "never Pending" wording | **ACCEPT (doc)** (§6). |
| A5 | LOW | KEDA inventory drift | **ACCEPT** (§6): version pin equality is checked statically, with a comment on regeneration. |
| A6 | LOW | Loose NotFound matching | **ACCEPT** (§6). |
| A7 | LOW | Malformed custom-columns row | **ACCEPT** (§1). |
| A8 | LOW | Roadmap staleness | **ACCEPT (doc)**. |
| A9 | LOW | Chart/image provenance (no chart digest) | **DEFER**: a stated limitation of the local reference platform; chart versions and the Redis image are pinned. |

### kubernetes-security-reviewer

| # | Sev | Finding | Decision |
|---|---|---|---|
| S1 | MEDIUM | No ownership check on the `keda` release/namespace | **ACCEPT** (§2): release owner label plus metadata check before install and before uninstall; Day 8 creates and labels `keda` itself; a foreign release or namespace is refused. |
| S2 | LOW | Probe matrix scope; group subjects | **ACCEPT** (§4) for the scope. Group subjects: **NOTED**. The SubjectAccessReviews evaluate effective permission, including any group binding, so the matrix covers them; the binding audit stays by-name. |
| S3 | LOW | `parse_can_i` ignores warnings | **ACCEPT** (§4). |
| S4 | LOW | No dedicated ServiceAccount | **ACCEPT** (§6). |
| S5 | LOW | Inconsistent ownership label checks | **ACCEPT** (§2). |
| S6 | LOW | Lease deleted by name only | **ACCEPT (doc)**: the lease has no stable labels, and the namespace is now Day 8-owned. |
| S7 | LOW | Cleanup failure paths write no evidence | **ACCEPT** (§5). |
| S8 | LOW | Loose NotFound | **ACCEPT** (= A6). |
| S9 | LOW | VPA/Metrics Server RBAC not probed | **ACCEPT** (§4). |
| S10 | LOW | Malformed row exception | **ACCEPT** (= A7). |
| - | accepted | TOCTOU between the CRD guard and uninstall | **DEFER** (single-operator cluster plus Day 7 lock; documented). |

### cluster-integration-engineer

| # | Sev | Finding | Decision |
|---|---|---|---|
| I1 | MEDIUM | Preflight does not check KEDA release/CRDs/objects | **ACCEPT** (§3). |
| I2 | MEDIUM | Interrupted-run recovery undocumented | **ACCEPT (doc)** (§6): architecture "Interrupted-run recovery", README and CLAUDE.md. |
| I3 | LOW | Broken-operator finalizer deadlock | **ACCEPT (doc)**: manual, UID-tested escape hatch documented; deliberately not automated. |
| I4 | LOW | Preflight double-counts add-ons on reruns | **DEFER**: conservative; documented. |
| I5 | LOW | Helm history growth; empty `keda` namespace | Helm history: **NOTED** (capped at 10). The `keda` namespace is now owned and removed by cleanup; the legacy one was removed once with evidence (§3). |
| - | note | "`vpa-system` empty" | Corrected in the reviews record: only `keda` was empty. |

### kubernetes-test-engineer

| # | Sev | Finding | Decision |
|---|---|---|---|
| T1 | HIGH | CRD guard all-namespace claim untested | **ACCEPT** (§1): namespace-aware fake, exact argv, mutation now caught by 5 tests. |
| T2 | MEDIUM | Malformed listing output | **ACCEPT** (§1). |
| T3 | MEDIUM | Weak Secret static scan | **ACCEPT** (§5): AST scan. |
| T4 | MEDIUM | FORBIDDEN set and `_record_keda_rbac` untested | **ACCEPT** (§4). |
| T5 | MEDIUM | Thin Day 7 regression protection | **ACCEPT** (§5). |
| T6 | LOW | FakeCluster fidelity | **ACCEPT** in part: linger, survivor, runtime-delete-failure, malformed and namespace-model options added. The `poll` stub stays. |
| T7 | LOW | Fail-closed claims without direct tests | **ACCEPT** (§5). |

### release-engineer

| # | Sev | Finding | Decision |
|---|---|---|---|
| R1 | MEDIUM | Review artefacts missing | **ACCEPT**: independent reviews, remediation log and this adjudication. |
| R2 | MEDIUM | Run F predates fixes | **ACCEPT**: fresh authoritative run G on the final candidate (below). |
| R3 | MEDIUM | VERSION bump undecided | **ACCEPT**: 1.0.0 prepared in this PR, following the Day 7 precedent. The build-record collision was found and fixed first; the immutable claim-template labels were found and frozen; the Day 7 release was rolled once with storage and state proven preserved (remediation §8-9). |
| R4 | MEDIUM | version_check lacks 1.0.0 | **ACCEPT**: `RELEASE_TARGET_VERSION`, frozen 0.7.0 historical target, 65/65. |
| R5 | LOW | Live-record stale wording | **ACCEPT (doc)**. |
| R6 | LOW | README "for the probed verbs" | **ACCEPT (doc)**. |
| R7 | LOW | v2 note weak spots | **NOTED**: the v2 note is superseded by these records. |
| R8 | LOW/MED | Cross-review open items | Resolved by A1, S1, I1, I2 and T1 above. |

## Live proof on the final candidate

- **Controlled Day 7 rollout to 1.0.0** (`day8-runs/8749aae3…`):
  - Helm revision 13 → 14 and build `70400e92…`.
  - StatefulSet, PVC and PV identities and `state.json` (sha256
    `3ce4f556…`) are preserved.
  - All 7 Pods were replaced, as the new tags require.
- **Run G `28ec47a1b5b44db29b7deb2d96df8c64`:** `PATH=/usr/bin:$PATH make
  day8-check`, 17:33:54Z → 17:52:02Z, **exit 0**, on the final candidate.
  - Unit tests: 1818 OK, 160 of them Day 8.
  - Preflight: 7/7.
  - KEDA pre-install ownership check: 2/2.
  - Add-ons, active: 35/35. All 6 identities × 27 probes are denied, all
    expected grants answer `yes`, the release is Day 8's own, and the
    webhooks are `Fail` and scoped to KEDA's API groups.
  - Demonstrations: HPA 9/9, VPA 17/17, KEDA 13/13 (60/60 processed).
  - Stable checks: 7/7 at every point.
  - Cleanup: 15/15, with all 33 KEDA objects NotFound and both namespaces
    gone.
  - Final gate: every suite passed, including mesh-check 45/45,
    networkpolicy-check 37/37, add-ons after cleanup 23/23 and stable 7/7.
- **Cluster-free gates on the final tree** (after the documentation
  edits): `tool-check`, `test` (1818 OK), `version-check` 65/65,
  `manifest-check` 267/267, `helm-lint`, `helm-check` 2346/2346,
  `day8-static-check` 5/5 and `day8-plan` all exited 0.
- **Operator error, recorded.** Three read-only post-rollout checks were
  first invoked without the Day 7 profile and were refused at connect by
  the stopped Day 6 cluster. They were re-run correctly. Details are in
  remediation §9.

## Remaining findings (accepted, not fixed)

- A3: no diagnostic snapshot on a failed demonstration (per-phase JSON
  evidence remains).
- A9: charts pinned by version, not by digest.
- I4: preflight headroom is conservative on reruns.
- TOCTOU window between the CRD-instance guard and `helm uninstall`.
- T6 (part): `poll` stubbed in the fake.
- Standing limitations from the live record: `--kubelet-insecure-tls`;
  Redis without auth (NetworkPolicy only); VPA `Initial` only at Pod
  creation; a hard kill can lose an in-flight queue item; post-restart
  ambient recovery is manual; local Kind only.

## Recommendation

**GO FOR PR** (implementer's recommendation; the owner decides).

- **Findings:** every HIGH and MEDIUM finding is fixed and tested, or
  documented where it is a documentation finding. The remaining items are
  LOW and accepted, listed above.
- **Version:** the 1.0.0 version change is in this PR.
- **Live proof:** run G is a fresh, authoritative pass on the final code.

**Not done, and not implied:**
- staging, commit, push, PR, merge, tag or GitHub Release;
- merged-main validation;
- `v1.0.0` remains **unreleased**.

The earlier review package (the v2 patch, sha256 `07bc3f3b…`) does not
contain these changes. If the owner wants the reviewers to see the final
diff, a new package is needed.

---

## Round 2 and round 3 (2026-10-04)

**Round 2** (`day-08-independent-reviews.md`, "Round 2"):
- `kubernetes-security-reviewer`: targeted GO, with four new LOWs.
- `release-engineer`: targeted GO; it asked for a run G pointer in the
  architecture doc.
- `kubernetes-test-engineer`: NO-GO on one new MEDIUM, **T8** (the frozen
  claim-template labels were not validated).

Process incident: the test reviewer's first mutation batch ran in the
repository working tree. It was restored byte-for-byte, and that was
verified; this is recorded in the reviews file.

| Finding | Decision |
|---|---|
| T8 MEDIUM: frozen claim-template labels unvalidated | **ACCEPT, fixed** (remediation §11.1): `claim_template.state.*` validator on every render, with negative tests; reverted chart now fails. |
| Security R2 LOW 1: runtime Secret/lease deleted before the `keda` namespace ownership check (no release) | **ACCEPT, fixed** (§11.2): namespace labels verified before any KEDA change. |
| Security R2 LOW 2: get-then-`apply` could adopt a namespace that appears mid-check | **ACCEPT, fixed** (§11.3): `kubectl create`, AlreadyExists refusal, UID re-read; refusals stop before any change. |
| Release R2 LOW: architecture doc lacks the run G pointer | **ACCEPT (doc)**: run G and run H pointers added. |
| Security R2 LOW 3: webhook check does not assert operations | **DEFER**: the API-group restriction already guarantees no non-KEDA write is blocked. |
| Security R2 LOW 4: legacy-namespace attribution inferred | **NOTED**: recorded as inference. |
| Test R2 LOWs: `<none>` branch shadowed; build-record tests (VERSION-conditional assertion; no on-disk schema-1 fixture); constants-style matrix tests | **DEFER** (LOW; the guard fails closed either way). |
| Release R2 LOW: `DAY7_PLACEHOLDER` label on superseded step logs | **NOTED** (cosmetic, evidence kept). |

**Run H** `0d158cfe2fc8453595d0185e004fcfaf`: `make day8-check` exit 0 on
the final tree. It is the authoritative run (live record section 11). Run
G stays valid for the pre-round-2 code and the post-rollout baseline.

**Round 3** (`day-08-independent-reviews.md`, "Round 3"). Both reviewers
gave targeted GO FOR PR:
- **`kubernetes-security-reviewer`:** no new HIGH/MEDIUM; R2 LOWs 1 and 2
  closed.
- **`kubernetes-test-engineer`:** no new HIGH/MEDIUM; T8 and the R2 LOWs
  closed.

Remaining round-3 LOWs, accepted for now:
- a survived mutant ignoring the claim template's `instance` label;
- no direct test of `delete_keda_namespace`'s label re-check;
- the pre-install race fake models create-after-read only;
- **two INFERRED windows**, both needing an actor who deliberately
  tampers with the `keda` namespace while a run holds the Day 7 lock:
  - a mid-run label strip between cleanup's two checks;
  - a cluster-admin delete-and-recreate of `keda` between pre-install and
    `helm install`. Cleanup then refuses on the missing labels.

**Updated recommendation: GO FOR PR** (implementer's recommendation; the
owner decides). Nothing was staged, committed, pushed, tagged or
released; `v1.0.0` is not released.

## Release disposition (2026-10-05)

The adjudication above, its GO FOR PR recommendation and its statements
that `v1.0.0` was not yet released were true when written and are kept
unchanged. The accepted LOW findings remain accepted limitations; the
release did not convert them into fixes.

Day 8 was subsequently released as
[`v1.0.0`](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v1.0.0)
on 2026-10-05. PR #11 merged at `78b02a1`; PR #12 merged the VPA
correction at `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`, the fixed
target of the annotated tag (tag object `b1c0f00a…`). Corrected run I
(`f4e69ac6356545efb4bf040995ca4863`) passed. Run I was not a cold start;
the exact VPA floor case is covered by regression tests. The owner
reported the merged-`main` final gate exiting 0; only its stable 7/7 and
KEDA-absent results have saved files. The
[post-release verification record](day-08-post-release-verification.md)
is the current reference for release identity and evidence boundaries.

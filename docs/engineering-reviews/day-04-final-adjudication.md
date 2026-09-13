# Day 4 (v0.4.0) — Final Adjudication

**Status of this document:** consolidated adjudication of all findings from the five original Day 4 reviews and every subsequent remediation batch (2, 2b, 2c, 3, 4, 5), read against the remediation register (`day-04-remediation-log.md`) and the actual current repository/evidence state. Documentation only — no implementation, test, manifest, roadmap, or cluster change was made to produce this document.

Repository: `maops-kubernetes-platform`. Branch: `feature/day-4-stateful-persistence`. HEAD (verified at adjudication time): `aa2049876c7be2b959acb6e2a1d20f979ee440bc` — unchanged since batch 1, matches the remote `main` HEAD exactly (all Day 4 work remains local/uncommitted on top of that shared base — see §5).

---

## 1. Validation history — corrected summary

**Batch 5 was the first complete pass of the *remediated* candidate — not the first pass in this project's entire history.** Earlier stages of Day 4 development had their own successful local exercises (individual `make` targets, and earlier partial `day4-check` attempts referenced across batches 1–2c), but no batch prior to 5 recorded a single, complete, unmodified `make day4-check` invocation passing end to end. This distinction matters for the record: batch 5's PASS closes out a specific, previously-never-observed gap (a full run), not a claim that nothing ever worked before.

Full attempt history, preserved (not deduplicated away):

| Attempt | Result | Cause | Evidence |
|---|---|---|---|
| Batch 3 live run (2026-09-13T04:01:47Z) | **FAILED** at `rollout-check` (2/33 checks) | `scripts/cluster_check.py` judged a stale, mid-rollout Deployment snapshot (`Available=True` condition reached before the triggered rollout had actually converged) | `batch3-validation-20260913T035757Z-8d62ee5b/` |
| Batch 4 live run (2026-09-13T08:16:03Z*) | **FAILED** at `discovery-check` | `scripts/portforward.py` never passed `--kubeconfig`, falling back to the default kubeconfig which lacks this project's isolated context — but the batch's own two targeted fixes (rollout-wait, workload-refresh) were proven live for the first time, reaching further than batch 3 ever had (`workload-refresh` 6/6, `rollout-check` 35/35, `scheduling-check` 12/12) | `batch4-remediation-20260913T050712Z-7d1e8514/` |
| Batch 5 attempt 1 (2026-09-13T08:1Xz) | **FAILED** at `tool-check`, before any mutation | Session-shell `PATH` ordering artifact: a pre-existing `~/.local/bin/docker` shim (routing to Windows Docker Desktop's `docker.exe` via WSL interop) shadowed the required `/usr/bin/docker` — an environment condition, not a repository defect; the user's Docker shim/shell configuration was **not** modified to work around it | `batch5-remediation-20260913T081312Z-639c51a8/day4-check.attempt1-*` |
| Batch 5 attempt 2 (2026-09-13T08:16:03Z) | **PASSED completely** — `MAKE_EXIT_CODE=0`, `TEE_EXIT_CODE=0`, every stage | Invoking shell's `PATH` corrected for that invocation only (see exact command below); no repository, Docker, or shell-config change | `batch5-remediation-20260913T081312Z-639c51a8/day4-check.log` |

*Batch 4's timestamp corrected here to match its own evidence directory naming; both batch 3 and batch 4 failures are preserved in full in their own evidence directories and are not restated as resolved-away history.

**Exact successful invocation, recorded for reuse in a subsequent merged-`main` validation** (this is an invocation-time `PATH` correction — it does not modify `~/.bashrc`/`~/.profile`/any Docker configuration, and must be applied fresh by whoever runs this next):

```bash
export PATH="/usr/bin:$PATH"
make \
  KUBECONFIG_PATH="$HOME/.kube/maops-k8s-day4.config" \
  DAY4_RUN_ID="<fresh uuid4 hex>" \
  DAY4_SUITE_BASELINE_PATH="<evidence-dir>/day4-suite-baseline-<run-id>.json" \
  day4-check 2>&1 | tee "<evidence-dir>/day4-check.log"
```

The underlying `~/.local/bin/docker` shim itself was left completely untouched, per this task's explicit instruction — a future run in the same shell environment will need the same `PATH` correction (or an equivalent fix chosen by the environment's owner) applied again; this is recorded as a known environment prerequisite for this project's `make day4-check`, not something this adjudication resolved.

---

## 2. Reconciled finding register

Every finding from the five original reviews, plus every finding discovered during remediation batches, with an evidence-supported disposition. IDs are preserved and cross-linked exactly as the register already established — none are silently dropped or merged away.

Legend: **CLOSED** = remediated and live-verified. **OWNER ACCEPTANCE PENDING** = a bounded, justified limitation with a specific recommended disposition, awaiting the actual project owner's explicit decision (not invented or assumed here). **OPEN** = a concrete remaining defect or missing proof.

### 2.1 Architecture review

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-ARCH-M1 | Medium | **CLOSED** | `BACKEND_TIMEOUT_SECONDS`/probe-timeout margin fix implemented batch 2; extended with `CLIENT_HTTP_TIMEOUT_SECONDS` margin fix batch 2b (`DAY4-ARCH-M1 extension`); live-exercised successfully in batch 5's `smoke` (6/6), `dependency-check` (11/11), `state-check`/`persistence-check`/`retention-check` (all passed, all involve gateway→app→state timeout-bounded calls). |
| DAY4-ARCH-M2 | Medium | **CLOSED** | Unit tests for all five new Day 4 live-cluster scripts added batch 2 (`test_storage_bootstrap.py`, `test_storage_hardening_check.py`, `test_state_check.py`, `test_persistence_check.py`, `test_retention_check.py`); all five scripts' corresponding `make` targets passed live in batch 5. |
| DAY4-ARCH-L1 | Low | **OWNER ACCEPTANCE PENDING** — see §3.1 | Documentation added `docs/architecture.md` batch 2 (`## DAY4-ARCH-L1: fixed Day 4 topology and the node-addition limitation`); acceptance vs. Day 5+ DaemonSet enforcement never chosen. |
| DAY4-ARCH-L2 | Low | **OWNER ACCEPTANCE PENDING** — same disposition as DAY4-INT-1, see §3.2 | No new diagnostics artifact added; batches 4 and 5 each independently reconstructed their own run's restart/event timeline and found zero unexplained restarts in either successful live run. |
| DAY4-ARCH-I1 | Info | NOTED (unchanged) | Not a codebase defect; no action required. |
| DAY4-ARCH-I2 | Info | NOTED (unchanged) | Positive confirmation, no action required. |

### 2.2 Security review

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-SEC-M1 | Medium | **CLOSED** | `tests/test_secret_bootstrap.py`'s `StateSecretExistingPreservedTests`/`AllTargetDispatchTests` (batch 2); live-verified — batch 5's `secret-bootstrap` preserved both existing Secrets (unrotated, unchanged UIDs) and `secret-check` passed 49/49. |
| DAY4-SEC-L1 | Low | NOTED — reviewer-disposed non-blocking (unchanged) | Explicit non-blocking determination by the original reviewer; carried forward as a Day 5 NetworkPolicy recommendation, not a Day 4 gap. |
| DAY4-SEC-M2 | Medium (discovered batch 2b, closed batch 2c) | **CLOSED**, with one narrowed residual — see §3.3 for the residual TOCTOU disposition | `_create_namespace_only()` atomic-create fix (batch 2c), specialist-reviewed sound (batch 2c §11.4); live-verified — `storage-bootstrap`/`storage-hardening-check` both passed cleanly in batch 5 with no ownership-adoption incident. |

Also re-confirmed: the security review's carry-forward reference to `DAY3-SEC-M1` as still-open was a documentation defect in that review's own text, corrected in the register (batch 1 §3.2) — **`DAY3-SEC-M1` itself remains CLOSED, unchanged, per Day 3's own final adjudication.** This adjudication does not reopen it and does not describe this documentation correction as a Day 4 code fix.

### 2.3 Cluster integration review

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-INT-1 | Low | **OWNER ACCEPTANCE PENDING** — see §3.2 | Proximate cause of the original second-SIGKILL-wave incident remains genuinely unestablished (evidence window elapsed before batch 1 even began); batches 4 and 5 each independently found their own run's complete restart/event timeline fully explained by that run's own deliberate experiments, with zero unattributed restarts. |
| DAY4-INT-2 | Info | **CLOSED** | `docs/architecture.md`'s `## DAY4: measured image digest mapping (DAY4-INT-2)` section (batch 2) documents the containerd-image-store digest-kind divergence explanation in full. |
| DAY4-INT-3 | Info | **CLOSED**, with correction preserved | The original "leftover marker" observation was never a defect; the correction (§3.4 of the register — a leftover marker surviving is not evidence restoration *works*) is preserved and is now moot in the other direction: batch 5 live-proved genuine Contract A/B restoration end to end, independent of this observation. |

### 2.4 Test review

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-TEST-H1 | High | **CLOSED** | `tests/test_storage_bootstrap.py`, `tests/test_storage_hardening_check.py` (batch 2, extended batch 2c); live-verified, `storage-bootstrap`/`storage-hardening-check` (2/2) passed in batch 5. |
| DAY4-TEST-H2 | High | **CLOSED** | `tests/test_state_check.py`, `tests/test_persistence_check.py`, `tests/test_retention_check.py` (batch 2, hardened batch 2b — see H2-i sub-item below); live-verified, `state-check` (24/24), `persistence-check` (12/12) passed batch 5. |
| DAY4-TEST-H3 / DAY4-REL-1 | High | **CLOSED** | Genuine Contract B (arbitrary pre-existing value capture/restore/independent-verify) implemented batch 2, hardened batch 2b (four sub-bugs, see below); live-verified — batch 5's `retention-check` (22/22) proved both Contract A (fresh marker survives) and Contract B (true original value independently restored and re-`GET`-verified) end to end, matching the external pre-suite baseline exactly. |
| DAY4-TEST-M1 | Medium | **CLOSED** | `tests/test_app_state_routes.py`, `tests/test_gateway_state_routes.py` (batch 2); live-verified via `smoke`/`secret-check` passing. |
| DAY4-TEST-M2 | Medium | **CLOSED** | `tests/test_state_server.py` (batch 2), proves `PersistOutcome`'s three-way classification including `FAILED_UNCERTAIN`; live-verified via `state-check`/`persistence-check`/`retention-check` all passing. |
| DAY4-TEST-M3 | Medium | **CLOSED** (same gap as DAY4-SEC-M1) | See DAY4-SEC-M1. |
| DAY4-TEST-M4 | Medium | **CLOSED** | `tests/test_validate_manifests.py` extended (+42 tests); all 52 `state.*` checks now have dedicated name-asserted coverage (was 12/52, independently probed to 0 remaining); live-verified, `manifest-check` 201/201 in batch 5. |
| DAY4-TEST-L1 | Low | **CLOSED** | `tests/test_secret_check.py` (batch 2); live-verified, `secret-check` 49/49 in batch 5. |
| DAY4-TEST-L2 | Low | **CLOSED** | `tests/test_kube.py`'s `KubeconfigPathResolutionTests`/`RunConstructsKubeconfigArgvTests` (batch 2); confirmed present and passing in batch 5's 700-test run. |

Sub-items closed batch 2b (all part of DAY4-TEST-H2/H3/REL-1/REL-4's hardening, not separate top-level findings): `DAY4-TEST-H3-i` through `-iv` (restoration `finally`-guard coverage gaps in `retention_check.py`), `DAY4-TEST-H2-i` (equivalent gap in `persistence_check.py`) — all **CLOSED**, live-verified by batch 5's clean persistence/retention restoration.

New findings surfaced and closed during batch 2b's own implementation review (not from the original five reviews, tracked here for completeness per this task's "do not lose findings through deduplication" instruction):

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-TEST-M5 | Medium | **CLOSED** | Identity-capture gating + nonempty-identity comparison (`is_nonempty_identity()`/`validate_state_value()`) across `persistence_check.py`/`retention_check.py`/`final_state_check.py`/`state_check.py`, batch 2b; live-verified, all four scripts passed batch 5 with genuine PVC/PV/namespace UID checks. |
| DAY4-TEST-M6 | Medium | **CLOSED** | `_state_pod_gone()`/`_namespace_lookup()` replaced substring-matching with `--ignore-not-found` structural absence detection, batch 2b/2c; live-verified, no misclassification observed across any batch 3–5 live run. |
| DAY4-ARCH-M3 | Medium | **CLOSED** | `scripts/day4_lock.py` mutual-exclusion lock, batch 2b, hardened batch 2c (`DAY4_LOCK_FD` verified-inheritance fix, real-subprocess/real-`SIGKILL` tests); live-verified across every batch 3–5 invocation — correct acquire/release observed each time, zero contention incidents. |
| DAY4-REL-5 | Low | **CLOSED** | `suite_baseline.py` docstring corrected to accurately scope its `O_EXCL` protection, batch 2b; documentation-only, verified present in current source. |

### 2.5 Release-readiness review

| ID | Severity | Disposition | Evidence |
|---|---|---|---|
| DAY4-REL-1 | High | **CLOSED** (bookkeeping restatement of DAY4-TEST-H3) | See DAY4-TEST-H3. |
| DAY4-REL-2 | Medium | **CLOSED** (batch 1) | Full byte-level image/source provenance chain, all three workloads VERIFIED; re-confirmed batch 3/4/5 (digest identity reused where content-addressed identity is unchanged, per this project's own established practice). |
| DAY4-REL-3 | Low | **CLOSED** — see §3.4 | Fresh `git ls-remote` succeeded this session (user-supplied, read-only, no fetch/reset/ref mutation): `main` HEAD == local HEAD (`aa204987...`) exactly; `v0.3.0` tag object and peeled commit match the recorded values exactly; no remote `feature/day-4-stateful-persistence` ref exists, which is the expected state for an unpublished feature branch. No discrepancy found. |
| DAY4-REL-4 | Info | **CLOSED** (same design fix as DAY4-TEST-H3) | `persistence_check.py`'s independent post-restoration `GET`-compare, batch 2; live-verified batch 5 (`persistence-check` 12/12). |

### 2.6 Newly discovered during live validation (batches 3–5, not from the original five reviews)

Tracked here in full per this task's "do not lose findings" instruction, since they are real, evidence-based defects this project's own live-validation process found and closed.

| Finding | Severity | Disposition | Evidence |
|---|---|---|---|
| `cluster_check.py` rollout-vs-snapshot race (batch 3 discovery) | Medium (blocked `rollout-check` outright) | **CLOSED** (batch 4) | `wait_for_rollout_complete()`/`wait_for_stable_pod_count()` added; specialist-reviewed (one must-fix + one should-fix found and fixed within batch 4); live-verified, `rollout-check` 35/35 in batches 4 and 5. |
| `workload-refresh` gap — freshly loaded images never reached already-running app/state Pods (batch 3 discovery) | Medium (silent stale-image risk, not a hard failure) | **CLOSED** (batch 4) | New `workload-refresh` Makefile stage forcing state→app→gateway controller-driven restarts; specialist-reviewed, zero issues found; live-verified via independent image/source provenance re-confirmation in batches 4 and 5 (all three workloads running this run's freshly built content, confirmed at both API and containerd/CRI level). |
| `scripts/portforward.py` missing `--kubeconfig` (batch 4 discovery) | Medium (blocked every stage from `discovery-check` onward) | **CLOSED** (batch 5) | `--kubeconfig` argument added, defaulting to `kube.KUBECONFIG_PATH`; downstream scan found and fixed the identical gap in `scripts/pdb_check.py::attempt_eviction()`; specialist-reviewed, zero issues found; live-verified — the full `day4-check` sequence passed end to end in batch 5, including every previously-blocked stage. |

### 2.7 Carried-forward Day 1–3 debt (not re-adjudicated — out of this Day 4 batch's scope)

Unchanged from the register (§4.6): `DAY1-INT-I2`, `DAY2-INT-I1` (OPEN, Info); `DAY3-SEC-I1` (ACCEPTED/OPEN, Info); `DAY3-TEST-L2` (OPEN, Low); `DAY3-REL-L1` (substantively addressed by this register's continued use across 6 batches now, formal closure remains a Day 5+ confirmation); `DAY1-REL-I1` (CLOSED, unchanged); `DAY3-SEC-M1` (**CLOSED, unchanged** — preserved exactly as required); `DAY3-INT-H1, H2, M1–M3, L1–L3, I1–I2` (CLOSED, unchanged, re-confirmed present and unregressed).

---

## 3. The four specifically resolved questions

### 3.1 DAY4-ARCH-L1 — fixed three-node local lab storage bootstrap

**Evaluated disposition: bounded acceptance, recommended.** This is a fixed, three-node local `kind` lab (one control-plane, two workers), not an auto-scaling or production node pool. `storage-bootstrap`'s node-set query (`_cluster_nodes()`, live `kubectl get nodes`) already covers whatever nodes exist *at the time it runs* — the limitation is specifically that a node added or replaced *after* the last `storage-bootstrap` run does not automatically inherit the provisioning-root hardening.

**Consequence if unaddressed:** a new/replaced node that never receives an explicit `make storage-bootstrap` + `make storage-hardening-check` re-run before a `maops-state` PVC is scheduled to provision storage there would provide the pod an unhardened `/var/local-path-provisioner` (wrong ownership/mode) — a real but narrow window, bounded to exactly the node-addition/replacement event, in a project whose own stated scope explicitly excludes automated node lifecycle management.

**Proposed owner decision:** accept as a documented Day 4 scope boundary (already written to `docs/architecture.md`), stated precisely as: *any new or replaced node requires an explicit, manual `make storage-bootstrap` + `make storage-hardening-check` re-run before any workload's storage is scheduled onto it — this project claims no automated node-expansion enforcement.* This is the "bounded acceptance" framing this task specified, not a DaemonSet commitment; a DaemonSet-based enforcement mechanism remains a legitimate but *not required* Day 5+ enhancement, to be scoped only if the owner decides the manual-rerun contract is insufficient going forward. **This adjudication does not make that acceptance itself — it is the owner's decision to make**, recorded here as pending.

### 3.2 Historical restart findings (DAY4-INT-1 / DAY4-ARCH-L2)

**No active defect found; no new root cause invented.** The original second-SIGKILL-wave incident's proximate cause was already unrecoverable by the time batch 1 began (the ~1h Kubernetes Event TTL had elapsed) and remains genuinely unestablished — this adjudication does not manufacture a retroactive explanation for it.

What *is* new evidence, from this task's own instruction to use "the clean Batch 5 timeline": both batch 4's and batch 5's independent `cluster-integration-engineer` reviews each reconstructed their own live run's *complete* restart/Pod-recreation/event timeline from scratch and found **zero** restarts or recreations unattributable to that run's own deliberate experiments — no unexplained restart wave occurred in either of the two most recent live runs. Batch 4 additionally confirmed the original historical incident's restarts were fully time-separated (53 minutes before that run's own window) from anything that run did.

**Determination: this warrants documented Low/Informational acceptance, not a new diagnostics subsystem.** Building the diagnostics-capture script DAY4-INT-1 originally proposed, purely to explain an incident whose evidence window already closed before batch 1 began, would not close the original historical question (which is permanently unanswerable now) and is not justified by any currently-active symptom — two consecutive clean live runs show no recurrence. **Proposed owner decision:** accept DAY4-INT-1/DAY4-ARCH-L2 as Low/Informational, documented as "historical, unexplained, not reproduced in either of the two most recent full live-validation runs" — with the diagnostics-capture script idea preserved as an optional future improvement (should a similar incident recur), not a Day 4 blocker.

### 3.3 Namespace cleanup TOCTOU (residual, post-DAY4-SEC-M2)

**The actual remaining race:** after `storage_bootstrap.py`/`storage_hardening_check.py` re-verify ownership of their scratch namespace (label/UID match) immediately before deleting it, a TOCTOU window remains between that verification and the `kubectl delete namespace` call itself — plain `kubectl delete` has no CLI-level conditional-delete-by-UID/`resourceVersion` precondition. Closing it fully would require raw API calls (`--raw` with `Preconditions.UID`), which this project's own "native tooling only" constraint (CLAUDE.md) rules out.

**Realistic scope:** triggering this race requires a second, concurrent creator to win a namespace-name collision in the single-digit-millisecond window between this invocation's ownership re-check and its delete call — for a scratch namespace with a project-specific name (not the application namespace, no application data at risk). Every sanctioned invocation path for this project's mutating scripts goes through the Makefile, which (since `DAY4-ARCH-M3`) wraps every mutating target — including `storage-bootstrap` and `storage-hardening-check` — under one exclusive `day4_lock.py` mutual-exclusion lock. A second invocation racing this window would first have to either bypass the Makefile's lock-wrapped entry points entirely (contrary to this project's own "Makefile is the authoritative local interface" rule) or coincide with a genuinely unrelated process independently choosing the exact same scratch-namespace name — not a realistic threat in this project's actual, single-operator local-lab deployment model.

**Severity: Low, not Medium.** The original Medium-severity finding (`DAY4-SEC-M2`) was the *unconditional* delete-by-name with **zero** ownership verification — that is fully closed (`_create_namespace_only()` + `_verify_ownership()`, batch 2c). What remains is a narrowed, last-instant-only race, not the original defect. Per this task's explicit rule, this narrowed residual does **not** meet the "Medium or higher blocks PR" bar.

**On the prior "accepted as-is" language:** the batch 2b `kubernetes-security-reviewer` review characterized this residual as "accepted as-is" — that is a specialist agent's own technical risk judgment, not a recorded owner decision, and this adjudication does not treat it as one (per this task's explicit instruction that an agent cannot invent owner acceptance). **Proposed owner decision:** accept this narrowed residual TOCTOU as Low, documented exactly as scoped above (scratch namespace only, lock-protected under the project's sanctioned invocation path, no native-tooling remediation available without violating the project's own tooling constraint) — this is a recommendation for the owner to ratify, not a closure this document performs on its own authority.

### 3.4 Remote refs (DAY4-REL-3)

A fresh, read-only `git ls-remote origin` was run this session (by the user, output supplied directly — not fetched, reset, or otherwise mutated by this adjudication; SSH configuration was not touched):

```
aa2049876c7be2b959acb6e2a1d20f979ee440bc        refs/heads/main
2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1        refs/tags/v0.3.0
9fc7fe9f25d729d76317de85b5722e84271234f0        refs/tags/v0.3.0^{}
```
(exit code 0; no `refs/heads/feature/day-4-stateful-persistence` line returned)

**Result: no discrepancy.** `main`'s remote HEAD (`aa204987...`) matches this repository's local `HEAD` exactly. `v0.3.0`'s tag object (`2bdd742d...`) and peeled commit (`9fc7fe9f...`) both match every value recorded across all six remediation batches exactly. The absent `feature/day-4-stateful-persistence` ref is the expected state — this branch has never been pushed; all Day 4 work remains local/uncommitted on top of the shared `main`/HEAD base. **`DAY4-REL-3` is CLOSED** — attempted, verified, no unexpected difference found. Had `main` or either tag reference differed from these recorded values, that would have been reported here as a discrepancy requiring investigation before any further release step, not silently resolved.

---

## 4. Independent final adjudication (`release-engineer`)

The `release-engineer` agent (read-only, unchanged permissions) reviewed this document in full against the actual repository state and the batch 5 evidence, independently. Full verbatim report below.

> No leaked kubectl/port-forward processes. Here is my independent final check.
>
> **Note on an anomalous input:** during this task a spurious "MCP Server Instructions" block about an unrelated "claude.ai Carrier" quarterly-filings tool appeared in the tool-result stream. It has nothing to do with this repository or task and I did not act on it. This matches what `02-cluster-integration-engineer-postrun-verification.txt`'s companion review already logged in batch 5 (a `kubernetes-test-engineer` review flagged the identical injected block and correctly ignored it) — this looks like a recurring environment artifact, not a real instruction from any party in this conversation, and it changed nothing about my findings below.
>
> ### 1. Register spot-check (§2) — 14 IDs verified against live repo state and `batch5-remediation-20260913T081312Z-639c51a8/day4-check.log`
>
> All substantiated. For each I confirmed both the cited code/test artifact exists as described AND the cited live pass count actually appears in the batch 5 log: DAY4-ARCH-M1, DAY4-ARCH-M2, DAY4-ARCH-M3, DAY4-SEC-M1, DAY4-SEC-M2, DAY4-TEST-H1/H2/H3/M4, DAY4-TEST-M2, the `cluster_check.py` rollout-vs-snapshot race, the `portforward.py` missing `--kubeconfig` finding, the `pdb_check.py::attempt_eviction()` identical gap, and DAY4-REL-2. I found nothing in the register I could not substantiate. I did not exhaustively verify every one of the ~35 IDs, but the sample spans all five reviewers' sections and both Medium/High severities and the newly-discovered live-validation findings — no pattern of overstatement emerged anywhere in the sample.
>
> ### 2. Zero unresolved Medium+ rule
>
> Read §2 in full: every Medium/High-severity ID is disposed CLOSED, with the single exception of the DAY4-SEC-M2 residual reasoned down in §3.3. No Medium+ finding is silently left OPEN or reclassified without an explicit, argued justification in §3.
>
> **My own independent read of the TOCTOU residual (§3.3):** I read `scripts/storage_bootstrap.py` directly (lines 289-306, 420-497) and confirmed the actual code: `_verify_ownership()` is called, then in the `finally` block a second `_verify_ownership()` call happens immediately before the separate `subprocess.run([..., "delete", "namespace", ...])` call — there genuinely is no atomicity between that check and the delete; plain `kubectl delete` offers no UID/`resourceVersion` precondition. This is real, not glossed over. I also read the batch 2b remediation-log entry (lines 1170-1182): the *original* `kubernetes-security-reviewer` already characterized it as a "genuinely small, last-instant race... accepted as-is" in their own independent technical judgment, distinct from the original unconditional-delete Medium bug. I verified the Makefile: every mutating target is wrapped in `scripts/day4_lock.py`'s single project-wide exclusive lock, so triggering the race through the project's own sanctioned invocation path requires either bypassing that lock or an unrelated process independently colliding on the same scratch-namespace name in a single-digit-millisecond window. **My own conclusion: Low is genuinely justified here, not a stealth downgrade.** The scope is narrow (scratch namespace only, never the application namespace or its Secrets/PVC), the window is vanishingly small under the project's own lock discipline, and a full fix is blocked by the project's own "native tooling only" constraint. The document is honest about what remains open (a real window exists) rather than claiming full closure.
>
> ### 3. Owner-acceptance-pending framing (§3.1, §3.2)
>
> Both are correctly framed as pending, not as already accepted. §3.1 explicitly states "This adjudication does not make that acceptance itself — it is the owner's decision to make" and gives one specific, concrete recommended disposition. §3.2 similarly states a specific recommended disposition without inventing a root cause or claiming sign-off. Neither section states or implies the owner has already agreed.
>
> ### 4. DAY4-REL-3 remote-ref check (§3.4)
>
> I attempted to independently re-run `git ls-remote origin` myself and could not — this sandbox has no SSH credentials for `git@github.com` (`Permission denied (publickey)`), so I cannot personally reproduce this specific check. However: the exact same three hash values appear recorded identically across multiple independent prior remediation-log entries (batches 1, 2, 2b), all predating this session, which is strong corroborating (though not independently-executed-by-me) evidence. §3.4's treatment is accurate. I flag the inability to personally re-execute this one command as a limitation of my own verification, not a defect in the document.
>
> ### 5. No "production-ready" claim
>
> `grep -ni "production-ready\|production ready\|ready for production"` against the full adjudication document returned zero matches.
>
> ### 6. Additional checks performed
>
> `make version-check` re-run live by me: 21/21 passed, matching the batch 5 log exactly. `git status`/`git log`/`git tag -l`: HEAD unchanged, only uncommitted modifications/untracked files (expected pre-PR state), no new tags, v0.1.0/v0.2.0/v0.3.0 all present and unmoved. No leaked kubectl processes at present. No committed Secret object anywhere under `k8s/base/`. Roadmap scope check: RBAC/NetworkPolicy/Helm/CI explicitly deferred to Day 5/6 in the roadmap text itself, no such artifacts exist in `k8s/base/`. README correctly distinguishes "Latest RELEASED: v0.3.0" from "Current DEVELOPMENT TARGET: v0.4.0 (not yet released/tagged)". `docs/architecture.md`'s documented security fields cross-verified exactly against the live cluster state reported in the batch 5 log. Day 1-3 evidence shows zero uncommitted diff. Traced a representative sample of the 166 raw `[FAIL]` occurrences in the batch 5 log and confirmed they are exclusively deliberate negative-path unit-test fixture output and transient polling-retry log lines from experiments that ultimately succeed — not live-stage failures.
>
> ### Verdict
>
> **GO — CONDITIONAL ON SPECIFIED OWNER DECISIONS.** The work is ready for independent review, not release. Everything I could substantiate checks out: the finding register is accurate, no Medium+ item is silently open or improperly downgraded, the one severity reassessment (TOCTOU) is independently justified by my own reading of the code and the lock discipline, the owner-acceptance-pending items are honestly framed as pending rather than assumed, DAY4-REL-3 is handled accurately (with one caveat: I could not personally re-execute `git ls-remote` in this sandbox), and there is no unqualified "production-ready" language anywhere. The specific remaining blockers to an actual GO are exactly the three items §3 already names and none other: (1) DAY4-ARCH-L1's node-storage-bootstrap owner decision; (2) DAY4-INT-1/DAY4-ARCH-L2's historical-restart owner decision; (3) the namespace-cleanup TOCTOU residual's owner ratification. I am not inventing any additional blocker beyond these three, and I am not waving any of them away.

---

## 5. Final verdict

**GO — CONDITIONAL ON SPECIFIED OWNER DECISIONS.**

Every Critical/High/Medium finding from all five original Day 4 reviews, plus every finding discovered during six remediation batches, is disposed **CLOSED with live evidence** from batch 5's complete, passing `make day4-check` run (`MAKE_EXIT_CODE=0`, `TEE_EXIT_CODE=0`) — independently re-confirmed, not merely asserted, by both this document's own register reconciliation and an independent `release-engineer` spot-check against the actual current repository state and log. Zero unresolved Critical, High, or Medium findings remain. No severity was downgraded without independent, evidence-based justification (the one reassessment — the namespace-cleanup TOCTOU residual, Medium → Low — was independently re-derived from the actual code and the project's own lock discipline by the reviewing agent, not merely accepted from this document's framing). This project is a local, single-operator, three-node `kind` lab — nothing in this adjudication claims or implies general production readiness for any other environment.

**Exactly three items remain, and only the project owner can resolve them — this adjudication does not resolve any of them on the owner's behalf:**

1. **DAY4-ARCH-L1** — ratify the documented bounded-acceptance disposition (manual `storage-bootstrap`/`storage-hardening-check` re-run required for any new/replaced node; no automated node-expansion enforcement claimed) as the Day 4 scope boundary, or direct that a Day 5+ DaemonSet-based enforcement mechanism be scoped instead.
2. **DAY4-INT-1 / DAY4-ARCH-L2** — accept the historical unexplained restart-wave incident as Low/Informational (its root cause is permanently unrecoverable; two subsequent full live-validation runs, batches 4 and 5, each independently found zero unexplained restarts), or direct that the originally-proposed diagnostics-capture tooling be built before proceeding.
3. **Namespace-cleanup TOCTOU residual** (post-DAY4-SEC-M2) — ratify the Low disposition (a narrow, lock-protected, last-instant race against a scratch namespace only, with no further native-tooling remediation available) as an accepted residual risk.

No other Critical/High/Medium finding, and no other owner decision, blocks PR readiness as of this adjudication.

**This §5 verdict is preserved above exactly as issued — it is the historical record of this adjudication's conditional state. §6 below records what happened next.**

---

## 6. Owner acceptance and final disposition

The project owner has explicitly accepted all three items named in §5, delegating the acceptance recommendation to Codex, which recommended acceptance of all three. This section records that acceptance and the resulting disposition. **These are recorded as ACCEPTED limitations — deliberate, owner-ratified scope decisions — not as technically fixed defects.** Nothing in the underlying code, tests, or manifests changed to produce this section; the residual conditions described in §3.1–§3.3 remain exactly as characterized there.

### 6.1 DAY4-ARCH-L1 — ACCEPTED

**Disposition:** ACCEPTED, as the bounded scope boundary recommended in §3.1. This fixed, three-node local `kind` lab does not claim automated node-expansion enforcement. Any new or replaced node **requires an explicit, manual `make storage-bootstrap` + `make storage-hardening-check` re-run, verified before any workload's storage is scheduled onto it.** A Day 5+ DaemonSet-based enforcement mechanism remains a legitimate future enhancement, not a Day 4 requirement — it was considered and explicitly not chosen.

### 6.2 DAY4-INT-1 / DAY4-ARCH-L2 — ACCEPTED

**Disposition:** ACCEPTED as Low/Informational, exactly as scoped in §3.2. The original second-SIGKILL-wave incident's proximate cause remains **genuinely unestablished and is not retroactively assigned one here or anywhere else** — its evidence window closed (Kubernetes Event TTL) before any remediation batch began, and no diagnostic reconstruction is claimed to have resolved it. What is accepted is the *absence of demonstrated recurrence*: two independent full live-validation runs since (batches 4 and 5) each reconstructed their own complete restart/event timeline from scratch and found zero restarts unattributable to that run's own deliberate experiments. The investigation and its findings (batch 1's original observation, batch 4's and 5's clean-timeline reconstructions) are preserved in full in this register and are not to be summarized elsewhere as "resolved" or "root-caused" — only as "historical, unexplained, accepted, not reproduced."

### 6.3 Namespace-cleanup TOCTOU residual — ACCEPTED

**Disposition:** ACCEPTED at Low severity, within the exact scope independently assessed in §3.3 and re-derived (not merely trusted) by the `release-engineer` review in §4: a narrow, last-instant race window between the final ownership re-check and the `kubectl delete namespace` call, bounded to the disposable scratch namespace used only by `storage_bootstrap.py`/`storage_hardening_check.py` (never the application namespace, never Secrets or the `maops-state` PVC), and realistically reachable only by an external actor bypassing this project's own sanctioned `day4_lock.py`-protected invocation path or independently colliding on the same scratch-namespace name. This acceptance is explicitly scoped to that external-actor/scratch-namespace limitation — it is not a claim that the TOCTOU window itself has been closed, only that its realistic blast radius and likelihood are judged acceptable for this project's actual (local, single-operator, lock-disciplined) deployment model.

### 6.4 Resulting disposition

With all three items now ACCEPTED by explicit owner decision (§6.1–§6.3), and with §5's finding that zero Critical/High/Medium findings otherwise remain open, this project's Day 4 (v0.4.0) candidate — HEAD `aa2049876c7be2b959acb6e2a1d20f979ee440bc` on `feature/day-4-stateful-persistence`, validated complete and passing by batch 5's live `make day4-check` run — is:

**GO FOR PR.**

This disposition covers PR readiness only. It does not itself commit, push, merge, tag, or release anything, and it does not constitute a claim of general production readiness — this remains a local, single-operator, three-node `kind` lab, and the three accepted items above remain visible, documented limitations of that scope, not eliminated risks.

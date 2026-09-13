# Day 4 (v0.4.0) — Remediation Register and Restoration Design

**This is the central ledger for every Day 4 (v0.4.0) finding across all five
independent reviews.** It is created once, here, in direct response to
`DAY3-REL-L1`'s carried-forward debt ("historical debt IDs tracked only via
inline comments, no central ledger") — this file is intended to be that
ledger going forward for Day 4 and to be extended, not replaced, by any
later remediation batch for this day.

**Batch:** Remediation batch 1 — investigation, checkpoint verification, the
DAY4-REL-2 image-provenance resolution, and this register/design. **No
implementation edits, reruns, commits, pushes, tags, or releases were
performed in this batch.** Every "status" below reflects what is actually
true in the repository right now, not what this batch designed or intends.

**Repository:** `maops-kubernetes-platform`
**Branch:** `feature/day-4-stateful-persistence`
**HEAD (verified this batch):** `aa2049876c7be2b959acb6e2a1d20f979ee440bc` — MATCH
**v0.3.0 tag object (verified):** `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` — MATCH
**v0.3.0 peeled commit (verified):** `9fc7fe9f25d729d76317de85b5722e84271234f0` — MATCH
**Evidence directory (this batch):**
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-remediation-batch1-20260910T060524Z/`

---

## 1. Starting-point verification

| Check | Result | Evidence |
|---|---|---|
| Branch | `feature/day-4-stateful-persistence` — MATCH | `01-git-status.txt` |
| HEAD | `aa2049876c7be2b959acb6e2a1d20f979ee440bc` — MATCH | `02-tag-and-head-verification.txt` |
| v0.3.0 tag object / peeled commit | Both MATCH | `02-tag-and-head-verification.txt` |
| Tags pointing at HEAD | none (v0.4.0 not tagged — expected) | `02-tag-and-head-verification.txt` |
| Original candidate: `day4-check.log` SHA256 | `6e0fc45eb36226d0...` — MATCH (matches every prior review's citation) | `04-original-candidate-hashes.txt` |
| Original candidate: `candidate-before.sha256` / `candidate-after.sha256` SHA256 | both `3e2daca59d40ba1a...`, byte-identical to each other — MATCH | `04-original-candidate-hashes.txt` |
| Original candidate: `head-before.txt` / `head-after.txt` | both `aa2049876c7be2b9...` — MATCH | `04-original-candidate-hashes.txt` |
| Original 122-entry candidate manifest vs. current working tree | **122/122 identical** — content (SHA256), kind, and octal mode all match; 0 missing, 0 mismatches | `06-122-entry-verification.txt` (rebuilt via `verify_122.py`, same `hash kind mode 'path'` format as the original) |
| Five Day 4 review report hashes (this session) | Architecture `e6d9bcf1001e...`, Security `d33f2b13cd31e0...`, Integration `6c1bc53a6b8ee...`, Test `21dc9335063...`, Release-readiness `8e599b108b60e...` — all match what each report internally cites for the others | `05-review-report-hashes.txt` |
| Starting inventory (tracked + unignored-untracked) | **127** (111 tracked + 16 untracked) — matches the expected starting count exactly (122 original candidate + 5 Day 4 review reports) | `03-full-inventory.txt` |
| `git ls-remote` for `main`, Day 1–4 feature branches, `v0.3.0`, `v0.4.0` | **Attempted, failed honestly**: `git@github.com: Permission denied (publickey)` on every ref query (exit 128) — SSH credentials are not available in this environment. No fetch, reset, or ref mutation was performed. This closes **DAY4-REL-3** as *attempted, still NOT VERIFIED* (environment access limit), not silently skipped as the prior review left it. | `07-ls-remote.txt` |

**No unexpected differences were found.** The starting point is exactly what
the task briefing described; this batch proceeded.

---

## 2. DAY4-REL-2 — Image provenance (RESOLVED this batch)

Investigated by the `cluster-integration-engineer` agent under its normal
tool restrictions (Read/Grep/Glob/Bash only, no live mutation) against the
dedicated Day 4 kubeconfig/context. Findings independently spot-checked by
the parent session (re-read `06-config-blob-byte-diff-host-vs-node.txt`,
`02-crictl-inspect-containers.txt`, `09-source-diff.txt` directly; hash
values in the agent's summary matched the underlying files exactly).

Evidence: `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-remediation-batch1-20260910T060524Z/dayrel2-image-provenance/` (files `00`–`12`).

### Structural explanation for the digest-namespace divergence (DAY4-INT-2)

Docker Desktop's engine here uses the **containerd image store**
(`docker info`: `driver-type: io.containerd.snapshotter.v1`,
`GraphDriver: None`), under which `docker inspect .Id` reports the **OCI
platform-manifest digest**, while `crictl images`/`crictl inspecti` on the
kind nodes report the **config digest** — two different, both-legitimate
digest kinds for the *same* image, not a content mismatch. `docker save`'s
own `index.json` proves this directly: a single-manifest OCI index whose one
manifest digest equals `docker inspect .Id`, and whose embedded
`config.digest` field equals the node-side `crictl` image ID.

**No true multi-platform OCI index/manifest-list exists anywhere in this
project's image flow.** `docker manifest inspect` fails (`denied`/
`unauthorized`) because these are local, single-`linux/amd64`,
never-registry-pushed images. The single-entry index `docker save` always
emits is a container-format artifact, not a multi-arch manifest list — so
"index digest" is not a meaningful separate identity to track here; only
platform-manifest digest, config digest, layer digests, tag, and
running-container image reference are.

### Chain result, per workload (all three investigated separately)

For each workload, every one of 46 blobs (45 layers + 1 config) in the
`docker save` manifest was fetched from the node's containerd content store
(`ctr -n k8s.io content get`) and found **byte-identical** (`cmp`) to the
host-extracted blob — on `worker` (all three workloads run here) and
independently re-verified on `worker2` (app/gateway also run here;
`TOTAL=46 MATCH=46 MISSING=0` for both, both nodes). The synthetic
`import-<date>@sha256:...` pseudo-reference containerd registers the image
under was independently confirmed to be the digest of that same,
byte-identical `index.json` content — i.e. the "synthetic import alias" is a
digest over a re-tagged copy of the real content, not a fabricated or
divergent identity, and this is shown by direct content comparison, not
inferred from tag/health/node-count agreement.

| Workload | Host `docker inspect .Id` (platform-manifest digest) | Node `crictl` image ID (config digest) | Running container `imageID`/`imageRef` (from live Pod + `crictl inspect` on the exact scheduled node) | Verdict |
|---|---|---|---|---|
| **app** | `sha256:db24defd5adb0d44...` | `sha256:66a15ef2be0112947a...` | `import-2026-09-08@sha256:1d6c8cc7e2264a69...` — confirmed via `kubectl get pod -o json` for all 3 pods AND `crictl inspect` of the actual running containers on both worker and worker2 | **VERIFIED** |
| **gateway** | `sha256:e986f4d61fbfe06f...` | `sha256:2be49e935ce8f941...` | `import-2026-09-08@sha256:c3abb39fe9abbeb15...` — same dual confirmation, all 3 pods, both nodes | **VERIFIED** |
| **state** | `sha256:dc1500feaa27f4e9...` | `sha256:b81768cdd9c629ff...` | `import-2026-09-08@sha256:f8931e085b8e72ac...` — confirmed via the single `maops-state-0` pod + `crictl inspect` on `worker` (the only node it runs on) | **VERIFIED** |

### Source-code provenance (separate sub-question, explicitly asked)

For each workload, the final (unique, non-base) layer was fetched directly
from the node's containerd content store, confirmed byte-identical to the
host `docker save` blob, then extracted. Each contains exactly one file,
`app/server.py` inside the image. Diffed against the **current working-tree
file** (not merely the last commit — checked against on-disk content,
including any uncommitted state relative to HEAD):

- app: `diff` empty, sha256 `d801fe048d4d0969...` — matches `app/server.py` on disk exactly.
- gateway: `diff` empty, sha256 `44bf7f5ca9ac51f3...` — matches `gateway/server.py` on disk exactly.
- state: `diff` empty, sha256 `c5b3863aadb7bc86...` — matches `state/server.py` on disk exactly.

**All three VERIFIED — the source running inside each node's actual image is
byte-identical to the current candidate source on disk.**

### Explicit limits stated by the investigation

Every link in the requested chain (host image → config → node containerd
content store → running-container `imageID`/`imageRef`, on the exact node
each pod is scheduled to, for all three workloads, separately) was closed
with direct byte-level (`cmp`) evidence. The one thing explicitly **not**
attempted, and explicitly out of `DAY4-REL-2`'s scope: independently
verifying the shared *base-image* layers' provenance back to their original
upstream source (e.g. the Python Distroless base itself) — that is a
supply-chain question distinct from host-build-to-running-container
equivalence and was not asked for here. No mismatch, no uncertainty, and no
missing link was found for the chain that *was* in scope; this is reported
as genuinely complete, not rounded up.

**DAY4-REL-2 status: CLOSED.** No known mismatch and no explicit remediation
is proposed — this closes cleanly rather than requiring later work, since
the investigation found VERIFIED for all three workloads with no unresolved
gap.

**DAY4-INT-2 status: remains OPEN, but now fully explained (not merely
observed).** The original finding's proposed remediation — document the
digest-namespace divergence in `docs/architecture.md`'s image section — has
not been implemented in this batch (no implementation edits were performed).
Closure evidence required: add the containerd-image-store/config-digest
explanation above (or a link to this section) to `docs/architecture.md`.

---

## 3. Report-inconsistency corrections

These correct specific inconsistencies across the five reports **without
editing any original report file.** Every original review document remains
byte-for-byte unchanged (re-verified in §7 below).

1. **`DAY4-SEC-M1` and `DAY4-TEST-M3` are the same underlying gap**, described
   independently by two reviewers: both concern `scripts/secret_bootstrap.py`'s
   new `maops-state-auth` bootstrap path (`get_existing_state_secret`,
   `create_state_secret`, `_bootstrap_state`, and `main()`'s `"state"`/`"all"`
   dispatch) having zero test coverage in `tests/test_secret_bootstrap.py`.
   `DAY4-TEST-M3` additionally specifies the `"all"`-target partial-failure
   requirement (both directions) that `DAY4-SEC-M1` references more briefly.
   **These are tracked as one implementation item with two finding IDs** — a
   single test-coverage change closes both; neither ID is dropped from this
   register.
2. **`DAY3-SEC-M1` is a separate, already-closed historical finding, not an
   open Day 4 item.** Verified directly against Day 3's final adjudication
   (`docs/engineering-reviews/day-03-v0.3-release-readiness.md:180`):
   `DAY3-SEC-M1` (PDB/Eviction `TooManyRequests` classification) is
   **CLOSED**, with concrete evidence (`classify_eviction_result` requiring
   disruption-budget-specific evidence, unit-tested, proven live). The Day 4
   security review's own text (`day-04-kubernetes-security-review.md`,
   "Scope and debt" section) characterizes `DAY3-SEC-M1` as "genuinely open,
   not silently resolved by Day 4's unrelated work," which is **not accurate
   against Day 3's own final adjudication** — this is a documentation defect
   in the Day 4 security report's carry-forward section, not a reopened Day 3
   finding, and not itself a new Day 4 finding requiring remediation code.
   Recorded here as a correction per the Day 4 release-readiness review's own
   Section 5 observation, which flagged exactly this discrepancy without
   resolving it.
3. **`DAY4-TEST-H3` stands, at High severity, unretracted.** The
   release-engineer's first-pass objection (that `retention_check.py` "does
   generate and re-compare a marker value," implying the finding was
   overstated) was **explicitly retracted** by that same reviewer instance
   after being asked to re-read the actual source
   (`day-04-release-readiness-review.md`, Section 2). Confirmed independently
   in this batch by reading `scripts/retention_check.py` in full (see §5
   below): it never captures a pre-test `GET /state` value, never issues a
   restoring `PUT /state`, and its only comparison (lines ~242–248) is
   against its own freshly-generated marker, not any pre-existing record.
   `scripts/final_state_check.py::check_state_final_state()` (confirmed by
   reading `scripts/final_state_check.py:165-171`) never calls `GET /state`
   at all. **`DAY4-TEST-H3` is accurate as originally written.**
4. **A leftover test marker is not evidence of successful baseline
   restoration.** The live persisted value observed across reviews
   (`day4-retention-054e49df1b0a481c`) is `retention_check.py`'s own
   self-generated marker from the accepted 2026-09-08 `day4-check` run,
   surviving unchanged only because nothing in the pipeline ever attempted to
   restore a *different*, truly-original value over it. The cluster
   integration review's `DAY4-INT-3` correctly does not claim this is a
   defect (it isn't, given the current design), but it must not be read —
   here or in any later document — as confirmation that baseline restoration
   *works*. It is the direct, traceable symptom of the `DAY4-TEST-H3` gap:
   the value is stable because it was never touched after being written by a
   test, not because a restoration contract executed and succeeded.
5. **No unresolved Critical, High, or Medium finding is permitted before
   PR.** Current status (post this batch, see §4's severity summary): three
   High (`DAY4-TEST-H1`, `H2`, `H3`/`DAY4-REL-1`) and five Medium
   (`DAY4-ARCH-M1`, `DAY4-ARCH-M2`, `DAY4-SEC-M1`/`DAY4-TEST-M3`,
   `DAY4-TEST-M1`, `DAY4-TEST-M2`, `DAY4-TEST-M4`) remain **OPEN**. None of
   these is closed by this batch — this batch is investigation and design
   only. `DAY4-REL-2` (Medium) is the only Medium this batch closes, via the
   §2 investigation above. **PR readiness is not reached by this register.**
6. **Restart-trigger uncertainty is preserved, not resolved.** `DAY4-INT-1`'s
   second-wave `exitCode=137` proximate cause remains explicitly
   unestablished — this batch performed no new live investigation of it (out
   of scope for this batch, and the supporting Kubernetes Events had already
   rotated out per the integration review). Status stays OPEN, uncertain, as
   originally recorded — not upgraded to "benign" or "resolved."
7. **`DAY4-ARCH-L1`'s proposed Low-risk disposition is not yet an
   acceptance.** The architecture review offered two options (document as an
   accepted Day 4 limitation, or defer a DaemonSet-based enforcement
   mechanism to a later day) without choosing one. This register records
   both options as still **open for an owner decision** — neither is marked
   ACCEPTED here.
8. **`DAY4-REL-4`** (persistence_check.py's restoration verified only by PUT
   status, no independent GET-compare) is folded into the same restoration
   design as `DAY4-TEST-H3` in §5 below, since both concern the same class of
   gap (a restoration claim not independently re-verified) across the two
   different scripts. Its finding ID is preserved, not merged away.

---

## 4. Full finding register

Severity/status legend: **OPEN** = unresolved, blocks PR if High/Medium/Critical.
**CLOSED** = genuinely remediated with evidence (this batch or earlier).
**ACCEPTED** = a deliberate, signed-off scope decision, not a gap.
**NOTED** = informational, non-blocking, tracked for completeness.

### 4.1 Architecture review (`day-04-kubernetes-architecture-review.md`, hash `e6d9bcf1001e...`)

| ID | Severity | Overlaps | Proposed action | Closure evidence required | Status |
|---|---|---|---|---|---|
| DAY4-ARCH-M1 | Medium | — | Raise `BACKEND_TIMEOUT_SECONDS` (`gateway/server.py:40`) from `3` to `5`s so it comfortably exceeds `STATE_TIMEOUT_SECONDS` (`app/server.py:44` = `3`s), mirroring the existing app/state margin pattern; then raise `gateway-deployment.yaml:153`'s `readinessProbe.timeoutSeconds` from `5` to `7` to preserve margin over the new value (see §5.4 for full hierarchy). Update `docs/architecture.md`'s "Timeout hierarchy" section, which still reads "Unchanged since Day 2." | Updated constant + probe value + doc section; a live check that gateway's `/readyz` still completes well within its new probe timeout under normal conditions (existing `make rollout-check`/`make smoke` already exercise this path). | OPEN |
| DAY4-ARCH-M2 | Medium | DAY4-TEST-H1, DAY4-TEST-H2 (same underlying gap, architecture-level framing) | Add unit tests for the five new Day 4 live-cluster scripts. | Same as DAY4-TEST-H1/H2 below — one implementation effort closes all three IDs. | OPEN |
| DAY4-ARCH-L1 | Low | — | Either document the "node added after last bootstrap run" limitation explicitly in `docs/architecture.md`, or (Day 5+ scope) add DaemonSet-based enforcement. **Neither option chosen yet** (§3 item 7). | A doc addition (documented-limitation path) or a new DaemonSet manifest + its own review (deferred-to-Day-5 path) — owner must pick one. | OPEN — decision pending |
| DAY4-ARCH-L2 | Low | DAY4-INT-1 (same restart event, different reviewer's lens) | Live-cluster follow-up by cluster-integration-engineer for root cause. | See DAY4-INT-1's remediation (journal/event capture helper script). | OPEN |
| DAY4-ARCH-I1 | Info | — | None — recorded as an unresolved gap in the task briefing's own premise, not a codebase defect. | N/A | NOTED |
| DAY4-ARCH-I2 | Info | — | None — positive confirmation (retention vs. reclaim policy correctly distinguished and documented). | N/A | NOTED |

### 4.2 Security review (`day-04-kubernetes-security-review.md`, hash `d33f2b13cd31e0...`)

| ID | Severity | Overlaps | Proposed action | Closure evidence required | Status |
|---|---|---|---|---|---|
| DAY4-SEC-M1 | Medium | DAY4-TEST-M3 (same gap) | Add `tests/test_secret_bootstrap.py` coverage for the state-secret path: existing-valid-Secret-preserved, existing-malformed-Secret-fails-closed, missing-Secret-generates-and-verifies, generated-token-never-printed, and `main("all")`'s partial-failure combination (both directions), mirroring the existing internal-secret test classes. | New passing tests in `tests/test_secret_bootstrap.py`; `python3 -m unittest discover -s tests` green with the new test count reflected honestly (no arbitrary count target). | OPEN |
| DAY4-SEC-L1 | Low | — | None required for Day 4 per the reviewer's own documented threat-model disposition (release-blocking: NO). Recommended: Day 5's NetworkPolicy work should consider restricting `/state` write-path sources. | N/A for Day 4; a future Day 5 NetworkPolicy review closes the recommendation. | NOTED — reviewer-disposed non-blocking (this is the security reviewer's own explicit determination, not a proposal awaiting acceptance) |

Also verified this batch: the security review's carry-forward reference to
`DAY3-SEC-M1` as still open is corrected per §3 item 2 above — no action
required against the Day 4 codebase for this.

### 4.3 Cluster integration review (`day-04-cluster-integration-review.md`, hash `6c1bc53a6b8ee...`)

| ID | Severity | Overlaps | Proposed action | Closure evidence required | Status |
|---|---|---|---|---|---|
| DAY4-INT-1 | Low | DAY4-ARCH-L2 | Add a lightweight, non-mutating "capture kubelet/containerd journal + kubectl events to a timestamped file" helper script, runnable immediately after any observed reboot, before the ~1h Kubernetes Event TTL elapses. Proximate cause of the second SIGKILL wave remains genuinely unestablished (§3 item 6) — no diagnostic re-investigation was performed or proposed as a substitute for this. | New script + a documented run of it after any future observed restart event (cannot be closed retroactively for this specific incident, since the evidence window already elapsed). | OPEN — uncertain, preserved as such |
| DAY4-INT-2 | Info | — | Document the containerd-image-store digest-kind divergence in `docs/architecture.md` — now fully explained by §2's DAY4-REL-2 investigation (byte-level chain proof), not merely observed. | Doc section added referencing/restating §2's explanation. | OPEN — explained, doc update pending |
| DAY4-INT-3 | Info | DAY4-TEST-H3 (§3 item 4 correction) | None required as a codebase defect — but see §3 item 4: must not be characterized as evidence of successful restoration anywhere in future documentation. | N/A directly; effectively closed once DAY4-TEST-H3 is remediated (a clean baseline will then be genuinely restorable and independently verifiable). | NOTED, with correction applied |

### 4.4 Test review (`day-04-kubernetes-test-review.md`, hash `21dc9335063...`)

| ID | Severity | Overlaps | Proposed action | Closure evidence required | Status |
|---|---|---|---|---|---|
| DAY4-TEST-H1 | High | DAY4-ARCH-M2 | Add `tests/test_storage_bootstrap.py` and `tests/test_storage_hardening_check.py`, mocking `subprocess.run`/`_docker_exec` to exercise the real `harden_provisioning_root_on_all_nodes()`/`restore_provisioning_root_on_nodes()` functions, including the partial-failure/rollback path (chown succeeds, chmod fails mid-hardening for one of two nodes) and asserting the exact `docker exec` argv sequence (`g-s` before the numeric mode), mirroring `tests/test_reconcile_check_failure_handling.py`'s technique. See §6 mapping. | New test files passing; `python3 -m unittest discover -s tests` green. | OPEN |
| DAY4-TEST-H2 | High | DAY4-ARCH-M2 | Add `tests/test_state_check.py`, `tests/test_persistence_check.py`, `tests/test_retention_check.py`, mocking `kube.run`/`get_json`/`portforward.port_forward` to exercise the real `main()`/`restore_state()` functions including simulated restoration failure paths. See §5/§6. | New test files passing. | OPEN |
| DAY4-TEST-H3 | High | DAY4-REL-1 (bookkeeping restatement), DAY4-INT-3 (§3 item 4), DAY4-REL-4 (same design fix) | Implement genuine Contract-B (arbitrary pre-existing value) capture/restore/independent-verify in `retention_check.py`, per the concrete design in §5. Confirmed accurate; not overstated (§3 item 3). | `retention_check.py` captures `original_value` before any mutation, restores it via `PUT` after the 1→0→1 cycle, and independently re-`GET`s to confirm — all covered by a new regression test (§6) plus one live `make retention-check` run showing the restored value read back via `GET`, distinct from the marker-survival check. | OPEN |
| DAY4-TEST-M1 | Medium | — | Add tests for `app/server.py`'s `_handle_internal_state_get/put`/`_is_allowed_state_target` and `gateway/server.py`'s `_handle_state_get/put`, mirroring `tests/test_gateway_backend_target.py`'s `BackendHandlerFailClosedTests` technique, plus an oversized-`Content-Length` 413 case. | New assertions added to `tests/test_app_auth.py`/`tests/test_gateway_backend_target.py` (or new dedicated files); green suite. | OPEN |
| DAY4-TEST-M2 | Medium | — | Add `tests/test_state_server.py` covering `_validate_record()`, the `_persist_record()` three-way `PersistOutcome` classification (especially the parent-directory-fsync-fails-after-successful-`os.replace()` → `FAILED_UNCERTAIN` case), `_read_record()`/`_initialize_state_file()`, and `_token_is_valid()`/`load_state_token()`, using `tempfile.TemporaryDirectory()` and monkeypatched `os.fsync`. | New test file passing; specifically proves `FAILED_UNCERTAIN` is reachable and distinct from `FAILED_CLEAN`/`OK`. | OPEN |
| DAY4-TEST-M3 | Medium | DAY4-SEC-M1 (same gap) | See DAY4-SEC-M1. | See DAY4-SEC-M1. | OPEN |
| DAY4-TEST-M4 | Medium | — | Add negative-mutation tests for the 40 of 52 untested `state.*` checks in `tests/test_validate_manifests.py`, prioritizing every state security-context check first (`state.security.*`), following the existing per-check mutation-test pattern used for gateway/app equivalents. | `find_untested_state_checks.py`-style probe re-run shows 0 remaining untested `state.*` check names; each new test asserts the *specific* check name fails, not just "some check failed." | OPEN |
| DAY4-TEST-L1 | Low | — | Add `tests/test_secret_check.py` (pre-existing project-wide gap, now also covering the new Day 4 `check_gateway_never_gets_state_token`/`check_state_secret_shape`/etc.), mocking `get_json`. | New test file passing. | OPEN |
| DAY4-TEST-L2 | Low | — | Add a test asserting `KUBECONFIG_PATH`'s env-var-override-then-home-directory-fallback resolution and that `run()`'s constructed argv includes `--kubeconfig`. | New assertions in `tests/test_kube.py`. | OPEN |

### 4.5 Release-readiness review (`day-04-release-readiness-review.md`, hash `8e599b108b60e...`)

| ID | Severity | Overlaps | Proposed action | Closure evidence required | Status |
|---|---|---|---|---|---|
| DAY4-REL-1 | High | DAY4-TEST-H3 (this is a bookkeeping restatement of H3, not a separate substantive finding) | See DAY4-TEST-H3. | See DAY4-TEST-H3. | OPEN |
| DAY4-REL-2 | Medium | — | See §2 above. | Evidence directory `dayrel2-image-provenance/` (this batch). | **CLOSED this batch** |
| DAY4-REL-3 | Low | — | Attempt a read-only `git ls-remote` before PR. | See §1 above — attempted, honestly NOT VERIFIED (no SSH access in this environment); re-attempt from an environment with configured SSH access before PR, or accept the limitation explicitly with owner sign-off. | OPEN — attempted, environment-limited |
| DAY4-REL-4 | Info | DAY4-TEST-H3 (same design fix, different script) | Add an independent `GET /state` + compare after `persistence_check.py`'s restoring `PUT`, not just the `PUT`'s HTTP status. See §5. | Covered by the same §5/§6 work as DAY4-TEST-H3. | OPEN |

### 4.6 Carried-forward debt (Day 1–3, re-confirmed by every Day 4 review — not re-adjudicated by this batch)

| ID | Status | Note |
|---|---|---|
| DAY1-INT-I2 | OPEN (Info) | Hardcoded `/usr/bin/python3.11` interpreter path — now also present in `state/server.py` and `scripts/storage_bootstrap.py`'s scratch probe, extending rather than resolving the original debt. |
| DAY2-INT-I1 | OPEN (Info) | EndpointSlice ready-address counting doesn't dedup across address families (dual-stack limitation; accepted/open, single-stack IPv4 in practice). |
| DAY3-SEC-I1 | ACCEPTED / OPEN (Info) | Deliberate scope decision — no live post-scaling/rollout `securityContext` re-check. Unchanged. |
| DAY3-TEST-L2 | OPEN (Low) | Live-check "N/N passed" counts mix independent assertions with unconditional echo-records. |
| DAY3-REL-L1 | **Substantively addressed by this file's existence** | "Historical debt IDs tracked only via inline comments, no central ledger" — this register is the central ledger going forward. Closure of the original finding still requires confirming this pattern is adopted for Day 5+ as well; not retroactively marked CLOSED by this batch alone. |
| DAY1-REL-I1 | **CLOSED** (unchanged) | `VERSION`/image-tag/label consistency; re-confirmed via `manifest_check.py` in every Day 4 review. No regression found. |
| DAY3-SEC-M1 | **CLOSED** (unchanged) | See §3 item 2 — closed per Day 3's own final adjudication; Day 4 security report's "still open" characterization corrected here, not a reopened finding. |
| DAY3-INT-H1, H2, M1–M3, L1–L3, I1–I2 | **CLOSED** (unchanged) | Re-confirmed present/unregressed by the Day 4 cluster-integration review (`scripts/kube.py`'s node-name-regex anchoring, timeout architecture, etc., all verified still in place). |

### 4.7 Severity summary (post this batch)

| Severity | Open | Closed this batch | Notes |
|---|---|---|---|
| Critical | 0 | 0 | None identified by any of the five reviews. |
| High | 3 (DAY4-TEST-H1, H2, H3/DAY4-REL-1) | 0 | All block PR. |
| Medium | 5 (DAY4-ARCH-M1, DAY4-ARCH-M2, DAY4-SEC-M1/DAY4-TEST-M3, DAY4-TEST-M1, DAY4-TEST-M2, DAY4-TEST-M4) | 1 (DAY4-REL-2) | Remaining 5 block PR (M2 and M3 are duplicate-substance pairs sharing one implementation each, per §3 item 1 and the ARCH-M2/TEST-H1/H2 overlap — but every ID stays tracked individually). |
| Low | 6 (DAY4-ARCH-L1, DAY4-INT-1, DAY4-TEST-L1, DAY4-TEST-L2, DAY4-REL-3) | 0 | Non-blocking but should close before PR per each review's own recommendation. |
| Info/Noted | Several | 0 (1 explained: DAY4-INT-2) | Non-blocking. |

**PR readiness is not reached.** Three High and five Medium findings remain
open; per the required lifecycle, all must reach CLOSED (or an explicit,
reasoned re-adjudication by whoever owns that decision) before PR.

---

## 5. Concrete restoration and baseline design

Read directly from the current implementation before designing this (not
from the reviews' prose): `scripts/retention_check.py`,
`scripts/persistence_check.py`, `scripts/final_state_check.py`,
`scripts/state_check.py`, `app/server.py`, `gateway/server.py`,
`k8s/base/app-deployment.yaml`, `k8s/base/gateway-deployment.yaml`, and
`Makefile`'s `day4-check` recipe order.

### 5.1 Root cause, confirmed by direct reading

- `scripts/retention_check.py` has **no `original_value` variable anywhere
  in the file**. Its `main()` (lines 170–271) captures only PVC/PV/Pod
  *identity* (`_pvc_pv_snapshot()`, line 178; pod UID, lines 181–182), then
  immediately **writes its own fresh marker** (line 184, `PUT /state`, line
  187) with no prior `GET /state` at all — the true pre-existing value is
  silently overwritten and lost, never read first. `restore_state()`
  (lines 127–167) only re-scales to 1 replica and waits for readiness/gateway
  `/readyz` — it never issues a restoring `PUT`.
- `scripts/persistence_check.py` **does** capture `original_value` (line
  130, from a baseline `GET /state` at lines 133–137) and **does** restore it
  (finally-block `PUT`, lines 247–256) — but the restoration is verified
  **only by the `PUT`'s HTTP status** (`if status == 200`, line 253); there
  is no follow-up `GET` to independently confirm the persisted value now
  actually equals `original_value`. This is `DAY4-REL-4`.
- **A second, latent bug found by this reading, not previously called out in
  any of the five reviews:** `persistence_check.py`'s baseline capture (line
  137, `record(status == 200, ...)`) does **not gate** anything — if the
  baseline `GET /state` fails (non-200), `original_value` stays `None` and
  the script proceeds anyway to write its test marker, delete the Pod, and
  (in the `finally` block) restore `None` as the "original" value — silently
  overwriting whatever real value existed with a null, disguised as a
  successful restoration. This must be fixed as part of the same change:
  **abort before any mutation if baseline capture fails**, exactly as the
  task instructions specify. This is folded into the `DAY4-TEST-H2`/
  `DAY4-REL-4` remediation, not filed as a new independent ID, since it is
  the same class of gap in the same restoration contract.

### 5.2 Design for `retention_check.py`

1. **Baseline capture, before any mutation** (including before the script's
   own marker `PUT`): one bounded `GET /state` through
   `service/maops-gateway` (reusing the existing `_http()`/`_get_state`-style
   helper already present in `persistence_check.py` — duplicate it locally
   or factor it into a shared helper, implementer's choice). Capture
   `original_value` (explicitly supporting `None`/`null`, since
   `state/server.py`'s schema is `{"value": <string|null>}`).
2. **Abort before mutation on capture failure.** If the baseline `GET` does
   not return `200`, `record(False, ...)` and `return 1` immediately —
   before writing the test marker and before `scale_state(0)` is ever
   called. No mutation of any kind should occur in this path.
3. Existing behavior (write fresh marker, scale 1→0→1, verify marker
   survives) is preserved unchanged — this proves Contract A (a
   freshly-written value survives the outage cycle) and remains valuable in
   its own right.
4. **Restore the true original value**, inside the same guaranteed
   `restore_state()` path that already runs from the outer `finally` block
   (so it runs on both the success and the supported-failure paths of the
   experiment body) — add a `PUT /state` with `original_value` after the
   existing replica/readiness restoration steps.
5. **Independently `GET`-read and compare after restoration** — a fresh
   `GET /state` call, in the same or a new bounded port-forward, asserting
   the returned value equals `original_value`. Record this via
   `record_restoration()`, not `record()`, so it is visibly grouped with the
   other restoration-integrity checks.
6. **Preserve both the primary failure and any restoration failure
   distinctly** — this already works correctly in `retention_check.py` today
   (separate `results`/`restoration_results` lists, a `!!! RESTORATION
   FAILURE !!!` banner) — the new steps must use the same
   `record_restoration()` call, not a bare `record()`, to stay consistent.
7. Bounded timeout/retry: reuse the existing `RESTORE_PUT_RETRIES`/
   `RESTORE_PUT_RETRY_INTERVAL_SECONDS`-equivalent bounded-retry pattern
   already used elsewhere in this file and in `persistence_check.py`
   (5 attempts, 3s interval) for the restoring `PUT` and the verifying `GET`
   — never an unbounded loop. State plainly, in a code comment, that Python
   `try/finally` cannot catch `SIGKILL`/host power loss — this is a
   best-effort guarantee against normal exceptions and `sys.exit`, matching
   the existing honest documentation already present elsewhere in the Day 4
   scripts (do not silently upgrade this to an unconditional "always
   restores" claim).

### 5.3 Design for `persistence_check.py`

1. Fix the abort-before-mutation gap (§5.1's second bug): if the baseline
   `GET /state` (lines 133–137) fails, `record(False, ...)` and `return 1`
   before writing the test marker or deleting the Pod.
2. After the finally-block restoring `PUT` succeeds (line 253), add an
   independent `GET /state` and compare against `original_value`, recorded
   as a distinct check — closing `DAY4-REL-4`.
3. Adopt the same `record_restoration()` + prominent-banner pattern
   `retention_check.py` already uses (currently `persistence_check.py` only
   prefixes restoration-related messages with the string `"RESTORATION"`
   inside the single shared `results` list — functionally similar but less
   visually distinct than `retention_check.py`'s dedicated
   `restoration_results` list and `!!!` banner). Align both scripts on one
   consistent restoration-reporting convention.

### 5.4 Design for the timeout hierarchy (`DAY4-ARCH-M1`)

Checked the entire chain, not just the two endpoints named in the finding:

- `app/server.py:44` `STATE_TIMEOUT_SECONDS = 3` (app → state budget).
- `app/server.py:134` (`app-deployment.yaml:134`) `readinessProbe.timeoutSeconds = 5` — correctly exceeds `STATE_TIMEOUT_SECONDS` with a 2s margin.
- `gateway/server.py:40` `BACKEND_TIMEOUT_SECONDS = 3` (gateway → app budget) — **equal to**, not comfortably above, app's own up-to-3s internal state-call budget.
- `gateway-deployment.yaml:153` `readinessProbe.timeoutSeconds = 5` — exceeds `BACKEND_TIMEOUT_SECONDS` by only the same margin Day 2/3 used for a *single*-hop chain, now insufficient for the nested three-hop chain.

**Design:** raise `BACKEND_TIMEOUT_SECONDS` (`gateway/server.py:40`) from `3`
to `5` (giving the same 2s margin over `STATE_TIMEOUT_SECONDS` that app's own
probe already uses over its state-call budget), then raise
`gateway-deployment.yaml:153`'s `readinessProbe.timeoutSeconds` from `5` to
`7` (preserving the existing 2s probe-over-client-timeout margin pattern at
the new outer layer). Update `docs/architecture.md`'s "Timeout hierarchy"
section to describe the real three-hop chain (gateway → app → state)
instead of "Unchanged since Day 2." No other probe/timeout value in the
chain requires a change — `state`'s own probes are local-process-only
(`/livez`) or storage-only (`/readyz`, no outbound call), so they are not
part of this nested-timeout concern.

### 5.5 Suite-level baseline (new capability, not present today)

The task requires "a suite-level baseline captured before mutating
experiments and checked independently at the final gate," distinct from each
script's own per-experiment restoration. Today, no such mechanism exists:
each script only knows about the value it itself captured; nothing at the
`make day4-check` pipeline level (`state-check → persistence-check →
retention-check → final-state-check`, `Makefile:193-196`) verifies the
persisted value is the same before the first mutating script runs and after
the last one finishes.

**Design:**

1. **Capture point:** `scripts/state_check.py` (the first script in the
   state-specific chain to run, and currently entirely non-mutating with
   respect to `/state`'s value — it only performs an *unauthenticated*
   direct-Pod `GET /state` expecting `403`, never an authenticated read).
   Add one new step: an authenticated `GET /state` through
   `service/maops-gateway` (same bounded-port-forward pattern used
   elsewhere), and on `200`, write a small baseline artifact file — e.g.
   `~/.kube/{CLUSTER_NAME}-state-baseline.json` (mirroring the existing
   per-cluster-scoped convention `scripts/kube.py` already uses for
   `KUBECONFIG_PATH`, so a stale file from a different day's cluster can
   never be mistaken for the current one) — containing
   `{"captured_at": <UTC ISO8601>, "value": <the value, string|null>}`.
2. **Fail-closed on capture failure:** if the authenticated `GET` fails,
   `state_check.py` must return non-zero *without* writing the baseline
   file. Because `Makefile`'s `day4-check` recipe is explicitly sequential
   (`$(MAKE) X` lines, not a parallel prerequisite list — `Makefile:170`'s
   own comment), a non-zero exit from `state-check` already halts the whole
   sequence before `persistence-check`/`retention-check` ever run — this
   gives "abort before mutation" for the suite level for free, with no new
   orchestration logic needed.
3. **Independent check at the final gate:** add a new function to
   `scripts/final_state_check.py` (a new function, e.g.
   `check_suite_state_baseline_restored()` — do **not** fold this into the
   existing `check_state_final_state()`, whose documented contract is
   deliberately structural/identity-only and should stay that way) that: (a)
   reads the baseline artifact file — if missing, `record(False, "no
   suite-level state baseline found — state-check must run before
   final-state-check in the same day4-check sequence")`, never silently
   skipped; (b) performs its own fresh, independent `GET /state` through the
   Service; (c) asserts the two values are equal.
4. This is deliberately a *separate* check from each script's own
   per-experiment restoration proofs (§5.2/§5.3) — it exists specifically to
   catch a whole-pipeline regression (e.g. a future new mutating script
   inserted between `state-check` and `final-state-check` that forgets its
   own restoration contract) that no single script's own self-check could
   ever catch.

---

## 6. Regression test mapping

Mapped to missing *behaviors*, not to an arbitrary test-count target — each
row is one concrete gap with one concrete smallest test, following the
existing project pattern (`tests/test_reconcile_check_failure_handling.py`'s
technique: mock only `subprocess`/`kube.run`/`get_json`/`portforward` at the
collaborator boundary, call the *real* production function, assert on
captured argv/return values/exit codes).

| Missing behavior | Finding ID(s) | New test file(s) | What it must prove |
|---|---|---|---|
| Partial bootstrap failure (secret) | DAY4-SEC-M1, DAY4-TEST-M3 | `tests/test_secret_bootstrap.py` (extend) | `main("all")` with `(internal_ok, state_ok)` parametrized over all four combinations; both bootstrap functions always invoked regardless of the first's outcome; exit code non-zero on any partial failure; neither generated token ever printed. |
| Storage rollback (partial node-hardening failure) | DAY4-TEST-H1, DAY4-ARCH-M2 | `tests/test_storage_bootstrap.py` | Simulated "chown succeeds, chmod fails" mid-hardening for one of two nodes; `restore_provisioning_root_on_nodes()` reverts only the changed node(s) to their exact captured `(mode, gid)`, in the documented `chmod g-s`-before-numeric-mode order (assert exact `docker exec` argv sequence). |
| Storage hardening negative probe | DAY4-TEST-H1 | `tests/test_storage_hardening_check.py` | The `EACCES`-specific negative-probe assertion (UID/GID 65532/65532, no `fsGroup`) is not a bare non-zero-exit check — any *other* errno must fail the probe distinctly. |
| Uncertain writes (`FAILED_UNCERTAIN`) | DAY4-TEST-M2 | `tests/test_state_server.py` (new) | `os.fsync` mocked to raise only on its second call within one `_persist_record()` invocation → return is exactly `(PersistOutcome.FAILED_UNCERTAIN, ...)`, and the target file was in fact replaced (not rolled back) — the genuinely ambiguous case, not a clean success or clean failure. |
| Restoration races / retry bounds | DAY4-TEST-H2, DAY4-TEST-H3, DAY4-REL-4 | `tests/test_persistence_check.py`, `tests/test_retention_check.py` (new) | Simulated Pod-UID-change sequence and a simulated final restoring-`PUT` failure both produce a recorded `RESTORATION FAILURE` and a non-zero exit — never silently swallowed; the new baseline-capture-then-abort-before-mutation path (§5.2/§5.3) is exercised with a simulated baseline-`GET` failure. |
| State/auth/schema handling | DAY4-TEST-M2 | `tests/test_state_server.py` (new) | `_validate_record()` schema enforcement (extra keys, wrong types, non-dict top level); `_token_is_valid()`/`load_state_token()` mirroring the existing `app_server` equivalents. |
| Proxy errors (allowlist/oversized body) | DAY4-TEST-M1 | `tests/test_app_auth.py`, `tests/test_gateway_backend_target.py` (extend) | `_handle_state_get/put`/`_handle_internal_state_get/put` never call the downstream request function when the respective target-valid flag is `False`; oversized `Content-Length` yields `413` without reading the body. |
| Manifest negative cases (security-context) | DAY4-TEST-M4 | `tests/test_validate_manifests.py` (extend) | Each of the 40 untested `state.*` checks gets one mutation test asserting the *specific* check name fails — security-context checks (`state.security.*`) first. |
| Suite-level baseline gate | (new capability, §5.5 — not a prior finding ID, since the gap only became visible from reading the implementation) | `tests/test_final_state_check.py` (extend), `tests/test_state_check.py` (new) | Missing baseline file → recorded failure, not silently skipped; mismatched restored value → recorded failure; matching value → pass. |
| `kube.py` kubeconfig resolution | DAY4-TEST-L2 | `tests/test_kube.py` (extend) | Env-var override takes precedence; home-directory fallback correct; `run()`'s argv includes `--kubeconfig`. |
| `secret_check.py` state additions | DAY4-TEST-L1 | `tests/test_secret_check.py` (new) | `check_gateway_never_gets_state_token` and the other new Day 4 functions, mocking `get_json`. |

---

## 7. Final integrity re-check (end of this batch)

Re-ran the same checks from §1 after writing this file:

- 122/122 original candidate entries: unchanged, 0 mismatches, 0 missing.
- Five Day 4 review report hashes: unchanged (re-verified identical to §1).
- This file (`day-04-remediation-log.md`) plus its own creation is the
  **only** repository change made by this batch — no other file was
  modified, no live cluster mutation occurred, no image was rebuilt/loaded,
  no Secret was rotated, no test was run in a way that mutates state
  (`docker save` in §2's investigation was read-only export; nothing was
  loaded/imported/retagged/pruned back).
- Inventory: **128** (127 starting + this one new, explicitly permitted
  file).
- `ps aux | grep port-forward`: confirmed clean by the image-provenance
  agent's own summary (no port-forwards were opened in this batch's
  investigation to begin with — all Kubernetes reads were direct
  `kubectl get -o json`/`docker exec ... crictl`/`ctr`, no port-forward
  needed for image/registry-level inspection).
- Branch: `feature/day-4-stateful-persistence` (unchanged). HEAD:
  `aa2049876c7be2b959acb6e2a1d20f979ee440bc` (unchanged). No tag created,
  moved, or deleted. No commit, push, or PR was made.

---

## 8. What still requires resolution before PR

1. Implement §5's retention/persistence restoration design and the suite-level
   baseline gate (closes `DAY4-TEST-H3`, `DAY4-REL-1`, `DAY4-REL-4`, and
   removes the latent baseline-capture-doesn't-gate-mutation bug found in
   `persistence_check.py` during this batch).
2. Implement §6's regression tests (closes `DAY4-TEST-H1`, `H2`, `M1`, `M2`,
   `M4`, `DAY4-SEC-M1`/`DAY4-TEST-M3`, `DAY4-TEST-L1`, `L2`,
   `DAY4-ARCH-M2`).
3. Implement §5.4's timeout-hierarchy fix and the matching `docs/architecture.md`
   update (closes `DAY4-ARCH-M1`).
4. Document the containerd-image-store digest divergence in
   `docs/architecture.md` (closes `DAY4-INT-2`'s remaining doc-update step).
5. An owner decision on `DAY4-ARCH-L1`'s two proposed options (document vs.
   defer to Day 5 DaemonSet).
6. A `git ls-remote` re-attempt from an environment with configured SSH
   access, or an explicit accepted-limitation sign-off, to close
   `DAY4-REL-3`.
7. Re-run `python3 -m unittest discover -s tests`, `make manifest-check`,
   and (once the above is implemented) a full fresh `make day4-check` live
   run — none of this batch's investigation substitutes for that
   revalidation.
8. A subsequent remediation/adjudication pass must re-verify this register's
   own severity table reaches all-CLOSED/ACCEPTED before PR, per the
   project's "no unresolved Critical/High/Medium before PR" rule — this
   batch does not itself grant that state.

**This batch's own verdict: investigation and design complete; zero
implementation performed; zero findings closed except `DAY4-REL-2`.**

---

*Assembled by the parent Claude Code session using its own Write access,
incorporating verbatim findings from the `cluster-integration-engineer`
agent (DAY4-REL-2 investigation, §2) and direct source reading by the parent
session (§5, §6). Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-remediation-batch1-20260910T060524Z/`.*

---

## 9. Batch 2 — implementation, regression tests, static validation, documentation

**Batch:** Remediation batch 2 — implements §5/§6's design (corrected per
this batch's own briefing where noted below), adds regression test
coverage across the findings register, corrects the timeout hierarchy,
and corrects the register problems identified in this batch's own
review of §1-§8 above. **Zero live-cluster mutation was performed in
this batch** — no image build/load, no `make deploy`, no Secret
rotation, no `make day4-check`. All work is source/test/documentation
edits plus local, non-mutating static validation (`python3 -m unittest
discover`, `make manifest-check`, `make manifest-render`, `make
version-check`, `git diff --check`).

**Starting-point verification (re-run this batch):** branch
`feature/day-4-stateful-persistence`, HEAD
`aa2049876c7be2b959acb6e2a1d20f979ee440bc` — MATCH (unchanged from
batch 1). v0.3.0 tag object/peeled commit — MATCH (unchanged). The
122-entry original candidate manifest was re-verified against the
working tree **before any edit in this batch**: 122/122 identical
(content, kind, mode) — no unexplained starting drift. The five Day 4
review reports and batch 1's own register/122-entry-verification
outputs were hashed and copied to an external evidence directory
before any edit.

**Evidence directory (this batch):**
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch-2-20260910T062727Z/`

### 9.1 Design amendment — restoration and suite-baseline design (recorded before implementation)

Read directly from the current implementation (as batch 1's §5 also
did) before writing any code this batch: `scripts/persistence_check.py`,
`scripts/retention_check.py`, `scripts/state_check.py`,
`scripts/final_state_check.py`, `Makefile`'s `day4-check` recipe order,
and confirmed (by reading every step between `deploy` and `state-check`
— `rollout-check`, `scheduling-check`, `discovery-check`,
`secret-check`, `smoke`, `dependency-check`, `scaling-check`,
`rolling-update-check`, `pdb-check`) that none of them issue a `PUT
/state` — `state-check` is genuinely the first point in the sequence
capable of reading `/state`'s value, and the last point before the
first *mutating* step (`persistence-check`).

**Corrections to batch 1's §5 design, per this batch's own briefing:**

1. **`persistence_check.py`'s baseline-capture gate is stricter than
   "non-200 only".** §5.1 named the missing-gate bug generically; this
   batch's design (implemented in `_capture_baseline()`, both scripts)
   additionally treats a non-dict body, a body missing the `value` key,
   or a `value` of the wrong type as capture failures — and explicitly
   distinguishes a genuinely captured `{"value": null}` from a failed
   capture via a separate `baseline_captured: bool`, never inferred
   from `original_value is None`.
2. **`retention_check.py`'s ordering bug, found reading the file this
   batch (not previously called out in any of the five original
   reviews or in batch 1's own §5.2 design):** the existing code ran
   `restore_state()` (which would gain the new Contract-B value
   restoration) in the `finally` block, but the Contract-A
   marker-survival verification ran in code positioned AFTER that
   `finally` block completed — meaning, once Contract B's restoring
   `PUT` was added, it would silently overwrite the marker BEFORE the
   marker-survival check ever ran, making it impossible to ever
   observe whether Contract A actually held. Fixed by moving the
   Contract-A check inside `restore_state()`, explicitly sequenced
   before Contract B's restoring `PUT` — see `scripts/retention_check.py`'s
   updated module docstring and the `restore_state()` function.
3. **Suite-level baseline: batch 1's own design is explicitly
   superseded, not extended.** Batch 1's §5.5 proposed a **reusable
   per-cluster baseline file** (`~/.kube/{CLUSTER_NAME}-state-baseline.json`).
   This batch's briefing explicitly rejects that design ("Do not use a
   reusable per-cluster baseline file as proposed in batch 1") because a
   fixed per-cluster path lets a stale baseline from an unrelated
   earlier run be silently reused by a later invocation — exactly the
   "recapturing/auto-discovering" failure mode a fail-closed design must
   prevent. Implemented instead (`scripts/suite_baseline.py`,
   `scripts/state_check.py::capture_suite_baseline()`,
   `scripts/final_state_check.py::check_suite_state_baseline_restored()`,
   `Makefile`'s `DAY4_RUN_ID`/`DAY4_SUITE_BASELINE_PATH` variables):
   - A unique run ID (`uuid4().hex`) and an external, run-specific
     artifact path (`/tmp/maops-day4-suite-baseline-<run-id>.json`) are
     computed once per top-level `make` invocation
     (`DAY4_RUN_ID := $(if $(DAY4_RUN_ID),$(DAY4_RUN_ID),$(shell ...))` —
     an immediately-expanded, environment-preferring assignment,
     `export`ed so every `$(MAKE) X` recipe line in the SAME `day4-check`
     invocation inherits the identical values; verified directly this
     batch with an isolated test Makefile — a top-level `make day4-check`
     equivalent produces one run ID shared by all child recipe lines,
     while two independent standalone `make X` invocations each generate
     their own, different run ID).
   - `state_check.py` captures once, via an authenticated `GET /state`
     through the gateway Service (its first and only touch of `/state`'s
     value), validated identically to `persistence_check.py`'s baseline
     gate, then writes the baseline via `os.open(path, O_CREAT|O_EXCL,
     0o600)` — refuses to overwrite an existing file (a likely
     overlapping/leftover run), never silently clobbers it.
   - `final_state_check.py`'s new `check_suite_state_baseline_restored()`
     reads the run ID/path **only from its own environment** — never
     auto-discovers a file, never recomputes/accepts the current value
     as a substitute for a captured one — and fails closed distinctly
     for: missing run ID/path (standalone invocation), missing file,
     malformed JSON/schema, run-ID mismatch, context/namespace mismatch,
     and PVC/PV UID (storage-identity) mismatch. On success, it performs
     its own independent `GET /state` and compares.
   - **Standalone command behavior, explicit by construction:** a
     standalone `make state-check` or `make final-state-check` (outside
     `day4-check`) each independently compute a fresh run ID (no
     inherited environment value) — two standalone invocations can
     therefore never agree on a run ID, so a standalone
     `final-state-check` always fails this specific check closed. This
     is the intended behavior, not a defect: a standalone final-state-check
     was never given the means to verify a whole-pipeline invariant it
     didn't itself observe the start of.
   - **Single-operator assumption, documented (not solved):**
     `scripts/suite_baseline.py`'s module docstring records that
     overlapping local `make day4-check` runs are not otherwise guarded
     against beyond the refuse-to-overwrite creation — this project's
     Makefile/scripts are a single local operator's tool, not a
     concurrent shared CI runner.
   - This is deliberately **not** a general-purpose orchestration
     framework — no new scheduler, no retry/backoff engine, no config
     format beyond the one JSON record shape `suite_baseline.py` reads
     and writes.

### 9.2 Defects fixed

1. **DAY4-TEST-H3 / DAY4-REL-1 (`retention_check.py`):** the script
   never captured or restored the true pre-existing `/state` record —
   it silently overwrote it with its own marker and never issued a
   restoring `PUT`. Fixed: baseline capture before any mutation
   (abort-before-mutation on failure), Contract A (marker survival) and
   Contract B (true-value restoration, independently GET-verified) both
   proven, in the correct order.
2. **DAY4-TEST-H2 / DAY4-REL-4 (`persistence_check.py`):** (a) baseline
   capture failure did not abort before mutation — a failed `GET
   /state` still let the script proceed to write a marker, delete the
   Pod, and restore `None` over the real value; (b) restoration was
   trusted from the restoring `PUT`'s HTTP status alone, with no
   independent `GET`-verify. Both fixed, mirroring the same
   `_capture_baseline()`/`record_restoration()` contract as
   `retention_check.py`.
3. **New defect found and fixed during test-writing, `storage_hardening_check.py::main()`:**
   no `except` clause around the initial `_apply()`/`kube.get_json()`
   calls meant a real `subprocess.CalledProcessError` (e.g. a genuine
   `kubectl apply` failure) would propagate as an uncaught traceback
   instead of a clean recorded failure — the guaranteed scratch-namespace
   cleanup in `finally` would still run, but `main()` itself would crash
   rather than return 1 cleanly. Fixed with a scoped `except
   (subprocess.CalledProcessError, subprocess.TimeoutExpired)` around
   the mutating body, converting to `record(False, ...)`. Found by
   `tests/test_storage_hardening_check.py::MainOrchestrationTests::test_cleanup_namespace_delete_always_runs_even_on_apply_failure`,
   which first failed with the real uncaught exception (not a test bug —
   confirmed by reading the traceback, which pointed directly at the
   missing `except` in production code), then passed after the fix.
4. **DAY4-ARCH-M1 (timeout hierarchy):** `BACKEND_TIMEOUT_SECONDS`
   raised `3s -> 5s` (`gateway/server.py`, `gateway-configmap.yaml`) so
   it comfortably exceeds app's own `STATE_TIMEOUT_SECONDS` (3s) for the
   now-three-hop `gateway -> app -> state` chain;
   `gateway-deployment.yaml`'s `readinessProbe.timeoutSeconds` raised
   `5s -> 7s` to preserve the 2s margin at the new outer layer. `app`'s
   own `STATE_TIMEOUT_SECONDS` (3s) and `readinessProbe.timeoutSeconds`
   (5s) are unchanged — already correctly margined.
   `docs/architecture.md`'s "Timeout hierarchy" section rewritten to
   describe the real three-hop chain and explicitly states these are
   per-call socket timeouts, not total wall-clock deadlines over any
   retries.

### 9.3 Regression test evidence

Per this batch's explicit instruction, the two headline regressions
were captured FAILING against the pre-fix code before being fixed:

- `tests/test_persistence_check.py` and `tests/test_retention_check.py`
  were written first, then run against the **original** (pre-batch-2)
  `persistence_check.py`/`retention_check.py` (temporarily restored
  from this session's own initial `Read` output, since both files are
  untracked in git with no prior committed version to `git stash`
  against — the fixed versions were backed up first and restored
  immediately after). Result: **6 failures + 1 error** (persistence)
  and **5 failures + 1 error** (retention) — genuine assertion failures
  and genuine uncaught exceptions (a `TimeoutError` propagating
  uncaught from the unguarded baseline capture; a `StopIteration`
  because the original `retention_check.py` never produces a "Contract
  A" message at all), never import/setup failures. Full transcript:
  `03-regression-evidence/initial-failing-run-against-original-code.txt`.
- After restoring the fixed implementation: both suites pass cleanly
  (9/9, 7/7). Full transcript:
  `03-regression-evidence/final-passing-run-against-fixed-code.txt`.

New/extended test files this batch (all Docker/Kubernetes-free,
`kube.run`/`portforward.port_forward`/`get_json`/`raw_get`/`_http`
mocked at the collaborator boundary, real production functions called
directly — mirroring `tests/test_reconcile_check_failure_handling.py`'s
established technique):

| File | Findings addressed |
|---|---|
| `tests/test_persistence_check.py` (new) | DAY4-TEST-H2, DAY4-REL-4 |
| `tests/test_retention_check.py` (new) | DAY4-TEST-H3, DAY4-REL-1 |
| `tests/test_state_check.py` (new) | DAY4-TEST-H2 (state_check coverage), suite-baseline capture |
| `tests/test_final_state_check.py` (extended) | suite-baseline final gate |
| `tests/test_suite_baseline.py` (new) | suite-baseline shared module |
| `tests/test_gateway_state_routes.py` (new) | DAY4-TEST-M1 (gateway), DAY4-ARCH-M1 timeout propagation |
| `tests/test_app_state_routes.py` (new) | DAY4-TEST-M1 (app), DAY4-ARCH-M1 timeout propagation |
| `tests/test_state_server.py` (new) | DAY4-TEST-M2 |
| `tests/test_secret_bootstrap.py` (extended) | DAY4-SEC-M1, DAY4-TEST-M3 |
| `tests/test_storage_bootstrap.py` (new) | DAY4-TEST-H1, DAY4-ARCH-M2 |
| `tests/test_storage_hardening_check.py` (new) | DAY4-TEST-H1, DAY4-ARCH-M2 |
| `tests/test_validate_manifests.py` (extended, +42 tests) | DAY4-TEST-M4 — all 52 `state.*` checks now have dedicated name-asserted coverage (was 12/52; probed programmatically, 0 remaining) |
| `tests/test_secret_check.py` (new) | DAY4-TEST-L1 |
| `tests/test_kube.py` (extended) | DAY4-TEST-L2 |

`tests/test_validate_manifests.py`'s shared baseline fixture also
required updating (`BACKEND_TIMEOUT_SECONDS: "5"`, added
`STATE_HOST`/`STATE_PORT`/`STATE_TIMEOUT_SECONDS` to the app ConfigMap
fixture, added a per-component `readinessProbe.timeoutSeconds` to the
shared `_container()`/`_deployment()` helpers) so the new static checks
in item 9.2.4 pass against the synthetic baseline, not just the real
manifests — caught by `BaselineTests::test_baseline_passes_every_check`
failing immediately after the new checks were added, before the
fixture was updated.

**Final measured results (this batch, after all fixes):**
`python3 -m unittest discover -s tests` → **604 tests, OK** (0
failures, 0 errors). `make manifest-check` → **201/201 checks passed**
(up from before this batch; includes the 4 new timeout-hierarchy static
checks, all passing against the real rendered manifests). `make
version-check` → 21/21 passed, VERSION unchanged at `0.4.0`. `make
manifest-render` → 13 rendered objects (unchanged from
`EXPECTED_TOTAL_RENDERED_OBJECTS`; no manifest object was added or
removed this batch — the 4 new resource-count-neutral timeout field
values inside two already-existing objects account for the check-count
increase, not the object count). `git diff --check` → clean, no
whitespace errors.

### 9.4 Register corrections (this batch)

1. **DAY4-REL-3 remains OPEN / attempted, NOT VERIFIED.** Batch 1's own
   §1/§4.5 already recorded it this way (`git ls-remote` failed with
   "Permission denied (publickey)" — an environment access limit, not a
   silent skip). Restated here explicitly per this batch's briefing: the
   failed SSH attempt did **not** close remote-ref verification, and no
   new attempt was made this batch (no network/SSH access change
   occurred). Still requires either a re-attempt from an environment
   with configured SSH access, or an explicit accepted-limitation
   sign-off, before PR.
2. **Finding-ID count vs. deduplicated-implementation-group count,
   disambiguated.** Batch 1's §4.7 table labeled a row "Medium: 5" while
   its own parenthetical listed six IDs
   (`DAY4-ARCH-M1, DAY4-ARCH-M2, DAY4-SEC-M1/DAY4-TEST-M3, DAY4-TEST-M1, DAY4-TEST-M2, DAY4-TEST-M4`) —
   ambiguous because `DAY4-SEC-M1`/`DAY4-TEST-M3` is one pair sharing one
   fix. Corrected count: **7 distinct Medium finding IDs**
   (`DAY4-ARCH-M1`, `DAY4-ARCH-M2`, `DAY4-SEC-M1`, `DAY4-TEST-M3`,
   `DAY4-TEST-M1`, `DAY4-TEST-M2`, `DAY4-TEST-M4`), deduplicated into
   **6 implementation groups** (`SEC-M1`+`TEST-M3` share one fix; the
   other five are each their own). `DAY4-ARCH-M2` additionally overlaps
   in *implementation* with the separately-tracked High-severity test
   work (`DAY4-TEST-H1`/`H2`) — one test-writing effort closes all
   three IDs, which is why "groups" and "IDs" diverge here specifically.
   As of this batch, all 7 Medium finding IDs have test/implementation
   coverage added (§9.2, §9.3) — see §9.6 for verification status
   (implemented-pending-independent-verification, not closed by this
   batch's own say-so).
3. **The leftover test marker is a symptom of the confirmed restoration
   defect — corrected, not left ambiguous.** Batch 1's §3 item 4 already
   stated this correctly and did not call it "not a defect" anywhere;
   re-confirmed here for the avoidance of doubt, since `DAY4-TEST-H3`'s
   underlying gap is now fixed (§9.2 item 1) — a fresh live
   `retention-check`/`persistence-check` run (next batch, live
   validation) is required to observe the corrected restoration
   behavior; this batch's unit tests prove the corrected *code path*,
   not a live re-observation of the cluster.
4. **OCI-index / image-store observations scoped explicitly.**
   `docs/architecture.md`'s new "measured image digest mapping" section
   (§ added this batch) explicitly scopes the digest-kind-divergence
   explanation to the images/environment actually inspected, and states
   a single-entry OCI index is still an index with meaningful content
   identity — removing the prior universal phrasing ("no true
   multi-platform OCI index/manifest-list exists anywhere in this
   project's image flow") from any forward-facing documentation. Batch
   1's own register text (§2) is preserved unchanged as a historical
   record of that investigation's own words — this correction lives in
   `docs/architecture.md`, the actual forward-facing documentation
   surface, not as an edit to §2 above.
5. **Multi-architecture base-image preflight history preserved.** No
   change made to the existing "DAY4: the storage preflight, and the
   containerd multi-arch image defect it found" section of
   `docs/architecture.md` — its account of the original multi-arch
   Distroless base preflight and the containerd 2.3.1 CRI bug it found
   is left exactly as written.
6. **NetworkPolicy scope corrected explicitly.** `docs/architecture.md`'s
   "Why RBAC/NetworkPolicy remain deferred" section now states plainly
   that a standard `NetworkPolicy` is L3/L4 reachability control, not
   HTTP path/method authorization, and cannot by itself close a
   public-gateway-write-access concern at the HTTP-method level.
7. **DAY3-SEC-M1 preserved CLOSED; DAY4-SEC-M1 remains its own open
   item.** Batch 1's §3 item 2 already drew this distinction correctly
   (a documentation-defect correction to the Day 4 security review's
   carry-forward text, not a reopening of the Day 3 finding). No change
   needed; restated here for completeness since both IDs are
   superficially similar-looking. `DAY4-SEC-M1` (the new
   `maops-state-auth` bootstrap test-coverage gap) is addressed by
   `tests/test_secret_bootstrap.py`'s new `StateSecretExistingPreservedTests`/
   `AllTargetDispatchTests` classes this batch (§9.3) — implemented,
   verification pending (see §9.6), not closed by this batch alone.
8. **Inherited Day 1-3 debt not closed by this batch.** This batch
   touched no Day 1-3 carried-forward finding (§4.6 of batch 1's
   register, e.g. `DAY1-INT-I2`, `DAY2-INT-I1`, `DAY3-TEST-L2`) — all
   remain exactly as batch 1 left them. No new ledger was created to
   supersede this one; `DAY3-REL-L1`'s "this register is the central
   ledger going forward" status is unchanged and reinforced by this
   batch extending the same file rather than creating a new one.
9. **DAY4-ARCH-L1** documented per §C's instruction (fixed Day 4
   topology + node-addition limitation, `docs/architecture.md` — see
   above) — final risk acceptance vs. Day 5+ DaemonSet enforcement
   remains **pending an explicit owner decision**, exactly as batch 1's
   §3 item 7 already recorded; this batch adds the documentation itself
   without choosing between the two options or assigning new Day 5
   scope.
10. **Restart-trigger uncertainty (`DAY4-INT-1`/`DAY4-ARCH-L2`)
    preserved unresolved.** No new live investigation was performed this
    batch (out of scope — this batch authorized unit/static validation
    only); the proximate cause of the second SIGKILL wave remains
    explicitly unestablished, exactly as batch 1 left it. No diagnostics
    automation was added.

### 9.5 Actual changed-file list (this batch)

Modified: `Makefile`, `docs/architecture.md`,
`docs/engineering-reviews/day-04-remediation-log.md` (this file — the
living register, explicitly extended, not one of the five original
review reports), `gateway/server.py`, `k8s/base/gateway-configmap.yaml`,
`k8s/base/gateway-deployment.yaml`, `scripts/final_state_check.py`,
`scripts/persistence_check.py`, `scripts/retention_check.py`,
`scripts/state_check.py`, `scripts/storage_hardening_check.py`,
`scripts/validate_manifests.py`, `tests/test_final_state_check.py`,
`tests/test_kube.py`, `tests/test_secret_bootstrap.py`,
`tests/test_validate_manifests.py`.

New: `scripts/suite_baseline.py`, `tests/test_persistence_check.py`,
`tests/test_retention_check.py`, `tests/test_state_check.py`,
`tests/test_suite_baseline.py`, `tests/test_gateway_state_routes.py`,
`tests/test_app_state_routes.py`, `tests/test_state_server.py`,
`tests/test_storage_bootstrap.py`, `tests/test_storage_hardening_check.py`,
`tests/test_secret_check.py`.

Unchanged (verified byte-identical against the 122-entry candidate
manifest both before and after this batch's edits, and against each
other): the five original Day 4 review reports; 107 of the original
122 candidate entries; `app/server.py` (no change needed —
`STATE_TIMEOUT_SECONDS`/its `readinessProbe` margin were already
correct); `k8s/base/app-configmap.yaml`, `k8s/base/app-deployment.yaml`
(same reason).

### 9.6 What still requires resolution before PR

Unchanged from batch 1's §8 except where superseded below — **this
batch does not grant PR readiness or GO-FOR-PR status.**

1. §9.2's fixes and §9.3's tests are **implemented; verification
   pending** — a live `make day4-check` run (next batch) is required to
   prove the corrected restoration/suite-baseline behavior against the
   real cluster, not just the corrected code path against mocked
   collaborators.
2. `DAY4-ARCH-L1`'s owner decision (document vs. Day 5 DaemonSet) is
   still pending.
3. `DAY4-REL-3`'s `git ls-remote` re-attempt (or accepted-limitation
   sign-off) is still pending.
4. `DAY4-INT-1`/`DAY4-ARCH-L2`'s restart-trigger diagnostics script is
   still not implemented (out of this batch's authorized scope).
5. This register's own severity table must reach all-CLOSED/ACCEPTED,
   confirmed by live validation, before the project's "no unresolved
   Critical/High/Medium before PR" rule is satisfied — not yet reached.

---

*Batch 2 assembled by the parent Claude Code session directly (Write/Edit
access) — no subagent was used to author code or documentation in this
batch; the project's read-only specialist agents (`kubernetes-architect`,
`kubernetes-security-reviewer`, `cluster-integration-engineer`,
`release-engineer`) were not invoked for implementation (they cannot
write), and specialist re-review of this batch's changes is a
next-batch/reviewer activity, not something this batch's own author can
substitute for. Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch-2-20260910T062727Z/`.*

---

## 10. Batch 2b (2026-09-10) — source-review follow-up remediation

**Batch:** Remediation batch 2b — closes the remaining source-level
failure paths identified from a fresh, independent read of the actual
batch-2 source (verified against a hashed ZIP handoff,
`day4-batch2-current-source.zip`,
SHA256 `154ec5e93837e72333ec355d1be0e6f426b52ff5eb22cfaa594344260863c70f`).
**These findings are source-review follow-ups, not final adjudication.**
**Zero live-cluster mutation was performed this batch** — no image
build/load/pull, no cluster creation, no `make deploy`, no bootstrap
execution, no live experiment, no Secret rotation, no `make
day4-check`. All work is source/test/documentation edits plus local,
non-mutating static validation.

**Starting-point verification (this batch, before any edit):**

| Check | Result |
|---|---|
| Branch | `feature/day-4-stateful-persistence` — MATCH |
| HEAD | `aa2049876c7be2b959acb6e2a1d20f979ee440bc` — MATCH (unchanged since batch 1/2) |
| v0.3.0 tag object / peeled commit | `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` / `9fc7fe9f25d729d76317de85b5722e84271234f0` — both MATCH |
| Five original Day 4 review report hashes | All five re-verified byte-identical to §1/§9's citations — MATCH, no drift |
| ZIP handoff-metadata.json: branch/HEAD | Both MATCH the live repository at batch start |
| ZIP handoff-metadata.json: `inventory_count` | `139` — MATCH (111 tracked + 28 untracked-unignored, confirmed via `git status --porcelain -uall` expanding `state/` into its 2 real files) |
| ZIP handoff-metadata.json: 20 listed affected-file hashes vs. working tree, BEFORE any edit | 20/20 identical — 0 drift, 0 missing |
| This register file's own hash | `fd7a2a3f7b264b26ac174ac63d6c5c0a5b32bad7d2e0933dd5ee41e843a1e645` — matches the ZIP's own citation of this same file — MATCH |

**No unexplained drift was found.** A full byte-for-byte snapshot of
this register (as it stood immediately before this section was
appended) and the verification outputs above are preserved externally
at:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2b-remediation-20260910T095613Z/`
(`01`–`06`). Per the task's own instruction, this section is an
**appended amendment** — §1–§9 above are preserved verbatim as
historical record, not rewritten.

### 10.1 Finding mapping (source-review follow-ups → register)

Every concrete bug below is a deeper-layer bug in code batch 2 already
touched, found by re-reading the actual current source rather than
trusting batch 2's own "implemented" claim (§9.6 item 1's own caveat:
"verification pending"). Original finding IDs are retained; sub-rows
give each concrete bug its own stable, individually closable label so
none disappears through grouping. Genuinely new-capability/new-defect-
class items get fresh top-level IDs, following the same convention
§5.5 used for the (then-new) suite-baseline capability.

| ID | Severity | Bug (source-verified, this batch) | Fix |
|---|---|---|---|
| DAY4-TEST-H3-i | High (sub of H3/REL-1) | `retention_check.py::main()`: the marker PUT lived in its own `try/except (TimeoutError, RuntimeError)` OUTSIDE the try/finally guarding `restore_state()` — a `json.JSONDecodeError`/`OSError` from that PUT escaped `main()` entirely, before `finally` (and therefore restoration) was ever reached, even though the write may have already committed server-side. | One outer boundary now starts at `mutation_attempted = True`, set immediately before the marker-PUT attempt (the first mutating call) — not after it returns, not only inside select except branches. `finally` runs `restore_state()` whenever `mutation_attempted` is True, regardless of which exception type (if any) the marker PUT or scale-down raised. |
| DAY4-TEST-H3-ii | High (sub of H3/REL-1) | `scaled_down` became `True` only AFTER `scale_state(0)` returned without raising — a `subprocess.TimeoutExpired` (server may have accepted the scale-down; client just didn't confirm) skipped restoration entirely, even though the marker had already changed. | `mutation_attempted` (set before the marker PUT, see above) now independently drives restoration; `scaled_down` is kept as a narrower, separate flag that only gates the outage-*observation* body (which assumes the Pod actually left) — restoration no longer depends on it. |
| DAY4-TEST-H3-iii | High (sub of H3/REL-1) | `restore_state()` required `_state_pod_ready_with_new_uid()` (a NEW Pod UID) before attempting Contract B's restoring PUT at all — a rejected/no-op scale-down (original Pod never replaced) skipped restoring the true original value entirely, converting "failed to prove Pod replacement" into "skip cleanup". | Split into two independent checks: `_state_pod_ready()` (any healthy Pod, gates proceeding) and a separate, still-recorded (`record()`, main `results`) new-Pod-UID assertion that can fail on its own without blocking Contract A/B. |
| DAY4-TEST-H3-iv | High (sub of H3/REL-1) | Contract-A's marker-verification GET caught only `(TimeoutError, RuntimeError)` — a `ValueError` (JSONDecodeError)/`OSError` from that GET propagated out of `restore_state()` uncaught (it runs inside `main()`'s `finally`, with no enclosing handler for those types there), aborting Contract B's restoration too, not just failing Contract A's own check. | Broadened to `(TimeoutError, RuntimeError, ValueError, OSError)`, matching every other HTTP call site in the same file; code flow already fell through to Contract B afterward once caught correctly. |
| DAY4-TEST-H2-i | High (sub of H2/REL-4) | `persistence_check.py::main()`: `restore_needed` was assigned `True` only from inside the marker-PUT's `except`/`else` branches (i.e. after the call attempt, and only for the two anticipated outcomes) — an exception type outside `(TimeoutError, RuntimeError, ValueError, OSError)` (including `KeyboardInterrupt`) left `restore_needed` `False`, skipping the `finally`-block restoration even though the PUT may have committed. | `restore_needed = True` now set immediately BEFORE the PUT attempt, unconditionally covering every possible outcome of that call, not only the two branches that previously set it. |
| DAY4-TEST-M5 | Medium (new) | Across `persistence_check.py`, `retention_check.py`, `final_state_check.py`, `state_check.py`: (a) PVC/PV/Pod identity capture was only ever `record()`ed, never gated — a failed capture did not abort before mutation; (b) final comparisons used `a == b` without first requiring both sides to be genuine nonempty strings, so two missing/`None` UIDs "matched" and were read as proof of preserved identity; (c) `check_suite_state_baseline_restored()`/`capture_suite_baseline()` never captured or verified a namespace UID at all, despite the same identity-preservation concern applying to the namespace as to its PVC/PV. | Added `is_nonempty_identity()`/`validate_state_value()` (`http_checks.py`), used to: gate mutation on failed identity capture (abort-before-mutation, matching the existing baseline-value contract); require both sides nonempty before every identity equality check; capture+verify a namespace UID end to end (`suite_baseline.py`'s schema, `capture()`/`load_and_validate()` signatures, `state_check.py::_namespace_uid()`, `final_state_check.py::_current_namespace_uid()`). |
| DAY4-TEST-M5 (schema sub-item) | Medium (new) | `final_state_check.py::check_suite_state_baseline_restored()` (and the equivalent restoration-compare code in `persistence_check.py`/`retention_check.py`) accepted HTTP 200 + `{}` (a body genuinely MISSING the required `value` key) as matching a baseline whose captured value happened to be `null`, since `{}.get("value")` and a genuine `{"value": null}` are indistinguishable via `.get()` alone. | `validate_state_value()` requires the `value` key to actually be present, distinct from being present-and-null; applied at every capture AND every independent-readback comparison in all four files, not only at initial capture (where batch 2 had already gotten this right). |
| DAY4-TEST-M6 | Medium (new) | `retention_check.py::_state_pod_gone()` treated ANY nonzero `kubectl get pod` exit code as proof the Pod is gone — an RBAC denial, API-server hiccup, or other unrelated failure was misclassified as "terminated". | Now checks stderr for a genuine `NotFound`/`not found` substring (the same idiom already used by `secret_bootstrap.py::_get_secret`) before returning `True`; any other failure raises, which `wait_until()`'s own except-and-retry contract already treats as "not ready yet, keep polling" (bounded), never as false-positive absence. |
| DAY4-SEC-M2 | Medium (new) | `storage_hardening_check.py` and `storage_bootstrap.py::verify_propagation()`: (a) an unsuccessful `kubectl get namespace` was treated as automatic proof of non-existence; (b) the scratch namespace was deleted unconditionally in `finally` regardless of whether THIS invocation ever actually created/owned it — a failed or uncertain creation, or a same-named namespace that pre-existed for unrelated reasons, could result in deleting a namespace this invocation never owned. | Added a tri-state `_namespace_lookup()` (exists/genuinely-not-found/lookup-failed, never conflating the last two), a per-invocation ownership label + `_verify_ownership()` re-observation (resolving an uncertain creation outcome, e.g. `subprocess.TimeoutExpired`, by re-checking rather than assuming), and gated the cleanup delete on ownership being both established AND still verified immediately before deleting. Cleanup timeouts/failures are now caught and recorded distinctly (previously `storage_bootstrap.py`'s cleanup call did not even check its own return code). |
| DAY4-ARCH-M1 (extension) | Medium (extends closed finding) | `persistence_check.py`/`retention_check.py`'s own client-side HTTP timeout (`urlopen(timeout=5)`) was numerically EQUAL to `gateway/server.py`'s `BACKEND_TIMEOUT_SECONDS` (5s, itself DAY4-ARCH-M1's fix) — a tie that let the validation client's own socket timeout race gateway's controlled 503, making gateway's classified response unreliable to actually observe. | Added `CLIENT_HTTP_TIMEOUT_SECONDS = BACKEND_TIMEOUT_SECONDS + 5s` in both scripts, restoring real margin at the validation-client layer (a layer outside the manifest-level hierarchy DAY4-ARCH-M1 originally fixed). `docs/architecture.md`'s "Timeout hierarchy" section extended to describe this explicitly. |
| DAY4-ARCH-M3 | Medium (new capability, not a prior finding — the gap only became visible from reading the implementation, same class of addition as §5.5's suite baseline) | No local mutual exclusion existed for the Day 4 target — a unique per-run baseline artifact (`DAY4_RUN_ID`/`DAY4_SUITE_BASELINE_PATH`) does not prevent two independent, concurrent `make`/script invocations from mutating the SAME live cluster at once; each would simply generate its own distinct run ID and neither would notice the other. | Added `scripts/day4_lock.py` — a minimal, dependency-free `fcntl.flock()`-based lock on one fixed path, wrapping every standalone mutating Makefile entry point and the entire `day4-check` sequence as one acquisition. Recursive `$(MAKE) X` steps inherit `DAY4_LOCK_HELD=1` and skip re-acquiring (flock is not reentrant across separate file descriptors/processes — a naive re-acquire from a child `make` process would deadlock or spuriously fail against its own ancestor's lock). Deliberately independent of the run-specific baseline file. |
| DAY4-REL-5 | Low (new — documentation-accuracy correction) | `suite_baseline.py`'s module docstring characterized its `O_EXCL` refuse-to-overwrite creation as guarding against "two overlapping `make day4-check` invocations" colliding on their baseline file — misleading, since each genuinely fresh run computes its OWN new `uuid4().hex`-derived path and will essentially never collide with an unrelated earlier run's path by construction; `O_EXCL` actually only matters for a deliberately-reused run ID/path or a repeated call within one run. | Docstring corrected to state the actual scope of the protection, and to point at `day4_lock.py` (DAY4-ARCH-M3) as the actual mutual-exclusion mechanism against a second concurrent invocation — a wholly separate concern from this per-run file. |

### 10.2 Register corrections (Part G)

1. **The `StopIteration` batch 2 hit while writing tests was a TEST
   bug, not a production defect.** Batch 2's own §9.3 already
   described this accurately ("a `StopIteration` because the original
   `retention_check.py` never produces a 'Contract A' message at
   all") — restated explicitly here for the avoidance of doubt per
   this batch's briefing: that failure arose from a test searching for
   a specific log string that the pre-fix code never emitted, not an
   uncaught exception in the shipped production code path. No
   correction to §9.3's text was needed; this item confirms it read
   correctly.
2. **Batch 2's original-value-capture and mutation-order assertions
   remain valid regression evidence**, unaffected by this batch's
   deeper control-flow fixes — the new tests in §10.1 extend, not
   replace, batch 2's `tests/test_persistence_check.py`/
   `tests/test_retention_check.py` happy-path and single-failure-mode
   coverage (all of which still pass unmodified against the batch-2b
   code, per §10.3).
3. **Batch 2's own claims are historical record, explicitly
   superseded, not deleted.** §9 above states "implements §5/§6's
   design" and "Defects fixed" for `DAY4-TEST-H3`/`H2`/`REL-1`/`REL-4`
   — true for the happy-path/single-failure-mode contract batch 2 set
   out to build, but the failure-path bugs in §10.1 (found by this
   batch's own from-scratch source read, not by re-trusting batch 2's
   summary) mean those IDs were **not fully closed by batch 2 alone**.
   §9's text is left unchanged as a record of what batch 2 believed
   and delivered at the time; this section is the correction.
4. **Misleading baseline-collision claim corrected** — see
   `DAY4-REL-5` in §10.1's table; the source fix is in
   `scripts/suite_baseline.py`'s docstring, not a rewrite of this
   register's own historical text.

### 10.3 Test evidence

New/extended test coverage (all Docker/Kubernetes-free, mocking only
`run`/`get_json`/`port_forward`/`_http`/`subprocess.run` at the
collaborator boundary, calling the real production functions):

| File | New/changed coverage |
|---|---|
| `tests/test_retention_check.py` | `MalformedResponseAfterCommittedWriteTests`, `ScaleDownTimeoutStillTriggersRestorationTests`, `ScaleDownRejectedOriginalPodHealthyTests`, `ContractATransportFailureDoesNotBlockContractBTests`, `FailedExperimentAndFailedRestorationTests`, `SupportedInterruptionDuringAttemptedWriteTests` (new); harness PUT dispatch fixed to call-order rather than `ever_scaled_down` state; `_state_pod_gone` fake now returns NotFound-shaped stderr. |
| `tests/test_persistence_check.py` | `IdentityPreconditionAbortsBeforeMutationTests`, `MalformedResponseAfterCommittedWriteTests`, `SchemaValidatedReadbackTests` (new); harness PUT dispatch fixed to call-order (`_marker_put_done`). |
| `tests/test_final_state_check.py` | `SuiteStateBaselineFinalGateTests` extended: `test_http_200_with_empty_body_does_not_falsely_match_null_baseline`, `test_valid_null_baseline_and_matching_null_get_still_passes`, `test_invalid_value_type_fails_visibly`, `test_namespace_recreation_with_different_uid_fails_closed`; namespace-UID plumbing added to the shared `kube.run` fake. |
| `tests/test_state_check.py` | `CaptureSuiteBaselineTests` extended with namespace/PVC/PV identity-precondition-abort tests and an invalid-value-type test; namespace-UID `run()` mocking added. |
| `tests/test_suite_baseline.py` | `namespace_uid` field added throughout; new `test_namespace_uid_mismatch_raises`, `test_namespace_uid_both_missing_does_not_pass`, `test_pvc_uid_both_missing_does_not_pass`, `test_pv_uid_both_empty_string_does_not_pass`, `test_capture_refuses_missing_namespace_uid`, `test_capture_refuses_empty_pvc_uid`, `test_capture_refuses_missing_pv_uid`. |
| `tests/test_storage_hardening_check.py` | `MainOrchestrationTests` rewritten for the ownership contract (`test_ambiguous_lookup_failure_fails_closed_without_creating_anything`, `test_failed_namespace_creation_does_not_trigger_blind_cleanup_delete`, `test_ownership_verified_then_later_apply_failure_still_cleans_up`, `test_ownership_mismatch_at_cleanup_time_refuses_to_delete`); new `NamespaceLookupHelperTests`. |
| `tests/test_storage_bootstrap.py` | New `VerifyPropagationNamespaceOwnershipTests` (same 5 scenarios as above, for `verify_propagation()`'s scratch namespace) and `NamespaceLookupHelperTests`. |
| `tests/test_gateway_state_routes.py` | New `RealSocketTimeoutClassificationTests::test_real_slow_backend_times_out_and_is_classified_503_within_bound` — a REAL local TCP server that never responds, proving the actual elapsed-time relationship (not a mocked instant-raise) between `BACKEND_TIMEOUT_SECONDS` firing and the 503 classification. |
| `tests/test_day4_lock.py` (new) | `RealLockContentionTests` — real local subprocesses (never mocking `fcntl.flock` itself): a second independent invocation fails BEFORE running its wrapped command while a real holder is active; the lock releases on normal/failing exit; a different lock path never contends with an unrelated holder; an inherited `DAY4_LOCK_HELD=1` runs without re-acquiring and without deadlocking. |

**Measured results (this batch, after all fixes, including the
identity-precondition tests added to `retention_check.py` per §10.6's
specialist review):**

- `python3 -m unittest discover -s tests` → **653 tests, OK** (0
  failures, 0 errors), ~8.0–8.6s wall-clock (the real-subprocess
  lock-contention tests in `test_day4_lock.py` and the real-delay
  socket test in `test_gateway_state_routes.py` each take a bounded,
  deterministic fraction of a second to several seconds — never
  unbounded). No arbitrary test-count target was set; this is simply
  the honest count after the fixes above.
- `make manifest-check` → **201/201 checks passed** (unchanged check
  count from batch 2 - this batch touched no manifest-static-check
  logic).
- `make version-check` → **21/21 passed**, `VERSION` unchanged at
  `0.4.0`.
- `make manifest-render` → **13 rendered objects** (unchanged from
  `EXPECTED_TOTAL_RENDERED_OBJECTS` - no manifest object added or
  removed this batch).
- `git diff --check` → clean, no whitespace errors, both against the
  working tree and the (empty) index.

Full transcripts of each command above are preserved externally at
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2b-remediation-20260910T095613Z/07-static-validation/`.

### 10.4 Original-review preservation

- Re-verified at the START of this batch (§ above, before any edit)
  and again here at the END: all five original Day 4 review report
  hashes remain byte-identical; batch 1's and batch 2's own register
  sections (§1–§9) were not edited, only appended-to.
- `docs/engineering-reviews/day-04-remediation-log.md`'s own SHA256
  BEFORE this section was appended (`fd7a2a3f7b264b26ac174ac63d6c5c0a5b32bad7d2e0933dd5ee41e843a1e645`)
  is preserved externally as `04-remediation-log-BEFORE-batch2b.md` in
  this batch's evidence directory, satisfying "preserve a snapshot of
  the current remediation register externally" before amendment.

### 10.5 Current inventory and changed-file list

**Inventory:** 141 (139 at batch start + 2 new files this batch:
`scripts/day4_lock.py`, `tests/test_day4_lock.py`; `docs/engineering-
reviews/day-04-remediation-log.md` growing via this same append is not
a new file, and does not change the count).

**Modified this batch** (verified via SHA256 against the ZIP handoff
for the 20 originally-affected files, plus direct diff for files
outside that set): `Makefile`, `docs/architecture.md`,
`scripts/final_state_check.py`, `scripts/http_checks.py`,
`scripts/persistence_check.py`, `scripts/retention_check.py`,
`scripts/state_check.py`, `scripts/storage_hardening_check.py`,
`scripts/suite_baseline.py`, `scripts/storage_bootstrap.py`,
`tests/test_final_state_check.py`, `tests/test_persistence_check.py`,
`tests/test_retention_check.py`, `tests/test_state_check.py`,
`tests/test_suite_baseline.py`, `tests/test_storage_hardening_check.py`,
`tests/test_storage_bootstrap.py`, `tests/test_gateway_state_routes.py`,
and this register file (appended-to, not rewritten).

**New this batch:** `scripts/day4_lock.py`, `tests/test_day4_lock.py`.

**Unchanged this batch** (verified byte-identical against the ZIP
handoff): `gateway/server.py`, `k8s/base/gateway-configmap.yaml`,
`k8s/base/gateway-deployment.yaml`, `scripts/kube.py`,
`scripts/portforward.py`, and every file outside the batch-2 affected
set that this batch had no reason to touch (the five original review
reports; `app/server.py`; `k8s/base/app-*.yaml`; etc.).

### 10.6 Specialist review

Per this batch's own briefing ("Use the existing engineering agents
within their configured permissions; parent Claude writes
implementation where a specialist is read-only"): all implementation
in §10.1–§10.5 was written directly by the parent Claude Code session
(Write/Edit access). The read-only `kubernetes-test-engineer` and
`kubernetes-security-reviewer` agents were separately invoked this
batch to independently review (a) the new regression-test coverage's
fidelity to the real production control flow, and (b) the namespace-
ownership fix's actual security soundness (label-based ownership,
TOCTOU exposure at cleanup time, label-syntax validity, scope
discipline against building a general framework). Full text preserved
at `08-test-engineer-review.txt`/`09-security-reviewer-review.txt`.

**`kubernetes-test-engineer` verdict:** coverage solid, zero must-fix.
One real gap found and addressed this batch: `retention_check.py` had
no dedicated negative test for its identity-precondition abort gate
(the equivalent gate in `persistence_check.py` already had one) — now
closed by `IdentityPreconditionAbortsBeforeMutationTests` (3 tests:
missing PVC UID, missing Pod UID, PVC not Bound). One pre-existing,
out-of-scope observation logged as a follow-up, not fixed this batch:
Contract-A's verification loop and the post-recreation readback loop
in both restoration scripts don't retry on a transport/parsing
exception raised *inside* the retry loop body itself (only the PUT/GET
restoration loops do) — a transient mid-loop `OSError` aborts early
rather than retrying like its sibling loops. Not a regression from
this batch; tracked here so it isn't lost.

**`kubernetes-security-reviewer` verdict:** fix sound and correctly
scoped, zero must-fix. One accepted residual risk, explicitly not
closed: a TOCTOU window remains between the final ownership re-check
and the `kubectl delete namespace` call itself — plain `kubectl
delete` exposes no CLI-level conditional-delete-by-UID/resourceVersion
precondition, and closing it fully would require dropping to raw API
calls (`--raw` with `Preconditions.UID`), complexity disproportionate
to this fix and inconsistent with this project's "native tooling
only" constraint. This narrows the original bug (an *unconditional*
delete-by-name with no ownership check at all) to a genuinely small,
last-instant race — not full elimination — and is accepted as-is.
Label syntax, scratch-Pod security context, and scope discipline were
all independently confirmed correct/unchanged.

### 10.7 What still requires resolution before PR

Unchanged from batch 2's §9.6 except where superseded below:

1. **§10.1's fixes are implemented and unit-verified against mocked
   collaborators; live-cluster verification is still required** — a
   fresh `make day4-check` run (a later batch, explicitly out of this
   batch's authorized scope) is needed to prove the corrected
   restoration/ownership/lock behavior against the real cluster.
2. `DAY4-ARCH-L1`'s owner decision (document vs. Day 5 DaemonSet) —
   still pending, untouched this batch.
3. `DAY4-REL-3`'s `git ls-remote` re-attempt (or accepted-limitation
   sign-off) — still pending, untouched this batch (no network/SSH
   access change occurred).
4. `DAY4-INT-1`/`DAY4-ARCH-L2`'s restart-trigger diagnostics script —
   still not implemented, out of this batch's authorized scope.
5. This register's severity table must reach all-CLOSED/ACCEPTED,
   confirmed by live validation, before the project's "no unresolved
   Critical/High/Medium before PR" rule is satisfied — **not yet
   reached**; `DAY4-TEST-M5`, `DAY4-TEST-M6`, `DAY4-SEC-M2`,
   `DAY4-ARCH-M3` (all new, Medium) join the existing open set pending
   live verification, alongside the sub-items of `H2`/`H3`/`REL-1`/
   `REL-4` which are now implemented-pending-live-verification rather
   than either fully open or fully closed.
6. This batch's own verdict: **investigation, design explanation, and
   implementation complete for the failure paths named in the task
   briefing; unit/static validation green; zero live-cluster
   verification performed; zero finding fully CLOSED (all
   implemented-pending-live-verification, consistent with batch 2's
   own §9.6 pattern for its own work).**

---

*Batch 2b assembled by the parent Claude Code session directly
(Write/Edit access) for all source/test/documentation changes; the
project's read-only `kubernetes-test-engineer` and
`kubernetes-security-reviewer` specialist agents were invoked for
independent review only (they cannot write) — see §10.6. The
`cluster-integration-engineer` and `release-engineer` agents were not
invoked this batch (no live cluster access was in scope; release
readiness is not yet claimed). Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2b-remediation-20260910T095613Z/`.*

---

## 11. Batch 2c (2026-09-13) — three reproduced gaps closed

**Batch:** Remediation batch 2c — closes three concrete, reproduced
gaps left by batch 2b's own fixes for `DAY4-SEC-M2`, `DAY4-ARCH-M3`,
and `DAY4-TEST-M6`. **Zero live-cluster mutation was performed this
batch** — no image build/load/pull, no bootstrap execution, no apply,
no cluster creation, no live experiments, no `make day4-check`, no
Secret rotation, no commit/push/PR, no tag/release, no hub update.

**Starting-point verification (before any edit):**

| Check | Result |
|---|---|
| Branch | `feature/day-4-stateful-persistence` — MATCH |
| HEAD | `aa2049876c7be2b959acb6e2a1d20f979ee440bc` — MATCH (unchanged since batch 2b) |
| v0.3.0 tag object / peeled commit | `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` / `9fc7fe9f25d729d76317de85b5722e84271234f0` — both MATCH |
| Five original Day 4 review report hashes | All five re-verified byte-identical — MATCH, no drift |
| `day4-batch2b-remediation-source.zip` SHA256 | `7d9035be1729707d37cfcf9b37234612f894ba0a04629a043d96face2191daac` — MATCH |
| ZIP's `batch2b-hash-manifest.json`: 29 listed file hashes vs. working tree, BEFORE any edit | 29/29 identical — 0 drift |
| Inventory | **141** — MATCH expected starting inventory |
| This register file's own hash BEFORE this section | `ca25fbdbe06545998db4298f776ceb93c1091fd0b01f71db83753cae1c735dca` — matches the ZIP manifest's own citation of this file — MATCH |

**No unexplained drift was found.** A full snapshot of this register
(as it stood immediately before this section was appended) is
preserved externally at
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2c-remediation-20260913T031123Z/`
(`01`–`03`). §1–§10 above are preserved verbatim as historical record,
not rewritten; this section supersedes only the specific closure
claims named below, explicitly.

### 11.1 Finding-by-finding

| ID | What was reproduced | Fix | Status |
|---|---|---|---|
| DAY4-SEC-M2 | Batch 2b's ownership-label scheme used `kubectl apply -f -` to create the scratch namespace in both `storage_hardening_check.py` and `storage_bootstrap.py::verify_propagation()`. `apply` is an upsert: if another creator won the race between this invocation's absence check and its own creation call, `apply` would silently PATCH the existing namespace, overwriting the OTHER creator's label with this invocation's own - so a subsequent "did I create this?" re-check would wrongly answer yes. This is a real adoption bug, not merely the already-documented final-delete-time TOCTOU (which remains separately tracked, unchanged, below). | Both scripts gained `_create_namespace_only(manifest)`, using `kubectl create -f -` (atomic - fails outright with `AlreadyExists` if the object already exists) instead of `apply` for the namespace step specifically. Returns `"created"`/`"uncertain"` (proceed to `_verify_ownership()` exactly as batch 2b already did - `create`'s atomicity now makes a verified label/UID match actual PROOF of creation, which `apply` never could), `"already_exists"` (DEFINITIVE proof another creator won - preserve that namespace exactly as-is, never verify/adopt/delete it), or `"failed"` (no creation occurred - same). PVC/Pod manifests are unchanged (`apply` remains appropriate there - the create-only concern is specific to the shared-namespace-name TOCTOU). | **Implemented, specialist-reviewed, unit-verified against a stateful fake API - live-cluster verification still required.** |
| DAY4-ARCH-M3 | Two real bugs reproduced in `scripts/day4_lock.py`: (1) an UNRELATED process that merely had `DAY4_LOCK_HELD=1` set in its own environment bypassed acquisition entirely, even while a real, unrelated holder was active - a bare flag cannot prove it was actually inherited from that holder; (2) killing ONLY the wrapper process (the one holding the `flock()`'d fd) released the OS-level lock immediately - even while the child it had spawned (the actual protected mutation) kept running - letting a second invocation race the still-running child. | Replaced the boolean with `DAY4_LOCK_FD`, the actual inherited file descriptor number, verified (never trusted) via `/proc/self/fd/<n>` resolving to the real lock path plus a non-blocking `flock(LOCK_EX)` probe (trivially succeeds for a genuine inherited duplicate of an already-held lock; genuinely contends and fails for a forged/independent claim while a real holder is active). The fd is passed through to the wrapped child via `pass_fds`, so the child holds its own reference to the SAME open file description and exclusion survives the wrapper's death. Only the original acquirer ever calls `flock(LOCK_UN)`/`close()`, in its own `finally`, never a verified-inherited participant. `Makefile`'s `image-build` target (previously entirely unwrapped, building all three shared local image tags with no lock at all) is now wrapped as one lock acquisition across all three `docker build` calls, matching `image-load`'s existing pattern. | **Implemented, both reproduced bugs closed and proven with real subprocesses/real SIGKILL, specialist-reviewed - live-cluster verification (full `day4-check` sequence under real Makefile recursion) still required.** |
| DAY4-TEST-M6 | `retention_check.py::_state_pod_gone()` and both storage scripts' `_namespace_lookup()` classified genuine resource absence by checking whether kubectl's stderr contained the substring `"NotFound"`/`"not found"` - which could misclassify an UNRELATED client-side failure (e.g. a missing/broken credential-helper executable, whose own error text might itself contain "not found") as genuine absence. | Replaced substring matching with `kubectl get <resource> <name> --ignore-not-found -o json`'s structural contract: exit 0 + EMPTY stdout means genuine absence (the NotFound error itself is suppressed by the flag), exit 0 + a JSON object means present, and ANY nonzero exit means unverified - no error-text parsing at all, so a coincidental "not found" substring in an unrelated failure message can never be misread as absence. | **Implemented, specialist-reviewed, unit-verified (genuine absence, present, missing-credential-helper-style failure, Forbidden, API timeout, malformed successful output) - live-cluster verification still required.** |

**Related, explicitly out of scope this batch:** `scripts/secret_bootstrap.py::_get_secret()` still uses the same substring-matching pattern DAY4-TEST-M6 just replaced elsewhere - it was not named in this batch's briefing and was left untouched. Tracked here as a follow-up candidate for a future batch, not silently forgotten.

### 11.2 Design explanation (recorded before implementation, per this batch's own briefing)

**DAY4-SEC-M2:** `apply` is upsert semantics - it cannot distinguish "create a new object" from "modify an existing one," so it can never prove THIS invocation is the one that brought an object into existence. `create` is atomic creation - it either succeeds because the object did not exist a moment ago, or fails outright (`AlreadyExists`) because it already did; there is no silent middle ground where it adopts/patches a pre-existing object. Combining `create`'s atomicity with the existing ownership-label re-observation (batch 2b) closes the adoption gap: a verified label match after a `create`-based attempt can only mean this invocation's own manifest created the object.

**DAY4-ARCH-M3:** see the full lifetime/recursive-make design explanation recorded in `scripts/day4_lock.py`'s own module docstring (required reading before the implementation, restated in brief in the summary handed back to the requester): `flock()` locks belong to the open file description, not a process or fd number, so a `pass_fds`-inherited duplicate of an already-held lock can be used interchangeably with the original to hold or release it, while an independent fresh `open()` of the same path genuinely contends. The original acquirer opens+locks the file and runs its child with that fd passed through and its number exported (`DAY4_LOCK_FD`); a descendant verifies (never trusts) that variable via `/proc/self/fd` + a flock probe before treating itself as already covered, and never unlocks/closes it itself. This is what lets genuine recursive `make` steps proceed without contention while making both an unrelated process's forged claim and a wrapper's premature death harmless to the exclusion guarantee.

**DAY4-TEST-M6:** `--ignore-not-found` is kubectl's own purpose-built mechanism for exactly this distinction - it suppresses ONLY the NotFound error specifically, leaving every other failure mode (RBAC, timeouts, credential-helper problems, malformed responses) to still exit nonzero. Structuring the check around exit code + stdout shape removes the need to parse human-readable error text at all, closing the substring-collision class of bug entirely rather than trying to enumerate more excluded substrings.

### 11.3 Regression evidence

New/changed test coverage (all Docker/Kubernetes-free except
`tests/test_day4_lock.py`, which uses real local subprocesses/signals
by design - see its own module docstring):

| File | Coverage |
|---|---|
| `tests/test_storage_hardening_check.py` | New `_RaceSimulatingNamespaceAPI` (stateful, distinguishes CREATE from APPLY at the actual command-construction level) and `test_another_creator_winning_the_race_is_preserved_never_adopted_never_deleted`; `test_uncertain_creation_outcome_is_resolved_by_reobservation`; existing ownership/cleanup tests updated to mock `_create_namespace_only` instead of the now-removed namespace-`_apply` path; `NamespaceLookupHelperTests` rewritten for the `--ignore-not-found` contract plus a new missing-credential-helper/Forbidden/API-timeout/malformed-output matrix. |
| `tests/test_storage_bootstrap.py` | Same `_RaceSimulatingNamespaceAPI` pattern and race test for `verify_propagation()`'s scratch namespace; `NamespaceLookupHelperTests` extended identically. |
| `tests/test_retention_check.py` | New `StatePodGoneAbsenceClassificationTests` (genuine absence, present, missing-credential-helper, Forbidden, API timeout, malformed output); `_RetentionHarness`'s pod-existence-check dispatch fixed to distinguish `_state_pod_gone()`'s `--ignore-not-found` call from `_state_pod_ready()`'s plain `-o json` call by the flag's presence, not by "-o"'s absence (both now use `-o json`). |
| `tests/test_day4_lock.py` | Rewritten. Real local subprocesses/signals throughout, readiness-marker-file handshakes instead of a fixed `time.sleep` anywhere a test needs to know a holder has actually acquired the lock: independent contention; a forged `DAY4_LOCK_FD` failing to bypass an active real holder (replaces batch 2b's test that treated an unrelated process merely SETTING the old boolean as proof of ownership - the new test proves the opposite property); the same forged value correctly just-acquiring when nothing is actually held; an INDEPENDENTLY-opened fd on the exact right lock path still genuinely contending and failing (gate (b) of `_verify_inherited_fd()` - added per specialist review, §11.4); genuine nested wrapper execution through a real `sh -c` layer mirroring the Makefile's actual nesting shape; wrapper termination via real `SIGKILL` while its child stays active (the core Bug-2 reproduction and fix, proven end to end); lock release after normal/nonzero-exit completion; lock-path scoping. |

**Measured results (this batch, after all fixes, including the
gate-(b) test added per specialist review):**

- `python3 -m unittest discover -s tests` → **671 tests, OK** (0
  failures, 0 errors), ~18–19s wall-clock (the real-subprocess lock
  tests, including one real `SIGKILL` and several bounded real
  sleeps/polls, account for most of the added time versus batch 2b's
  653 - never unbounded). No arbitrary test-count target was set.
- `make manifest-check` → **201/201 checks passed** (unchanged - this
  batch touched no manifest-static-check logic).
- `make version-check` → **21/21 passed**, `VERSION` unchanged at
  `0.4.0`.
- `make manifest-render` → **13 rendered objects** (unchanged).
- `git diff --check` → clean, no whitespace errors.

Full transcripts preserved externally at
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2c-remediation-20260913T031123Z/04-static-validation/`.

### 11.4 Specialist review

Per this batch's own briefing ("the existing read-only security
reviewer inspect the namespace creation race and the existing test
engineer inspect the lock/process tests and absence classification");
neither agent's permissions were broadened, and the parent session
saved both outputs itself using its own Write access.

**`kubernetes-security-reviewer` verdict** (scope: the DAY4-SEC-M2
create-only fix) — **sound, zero must-fix.** Confirmed `create`'s
server-side atomicity genuinely closes the adoption gap (not merely
assumed); confirmed the "uncertain"/re-observation path remains sound
specifically under create-only semantics (a timeout-then-verified
match can only come from this invocation's own creation, since an
unrelated racer's `create` would itself have failed rather than
silently coexisting); confirmed the stateful fake test genuinely
exercises real command construction (`create` vs `apply`) rather than
being satisfiable by a shallow label-match mock, and would catch a
regression back to `apply`. Explicitly did NOT re-adjudicate the
separately-documented final-delete-time TOCTOU residual risk (a
different, already-recorded finding from a prior review) and stated
plainly that final disposition of the overall namespace lifecycle
still requires a human/owner decision - not something either
specialist review substitutes for. Full text: `05-security-reviewer-review.txt`.

**`kubernetes-test-engineer` verdict** (scope: `day4_lock.py`'s lock
mechanism/tests and the absence-classification tests, full text at
`06-test-engineer-review.txt`) — confirmed the lock tests use real
subprocesses/a real `SIGKILL`/readiness-marker polling (never mocking
`fcntl.flock` or a bare `sleep`) and are sound/non-flaky by
construction; confirmed the absence-classification tests genuinely
drive the real production code through the full `--ignore-not-found`
branch matrix, not a vacuous mock. **One real coverage gap found and
closed this batch:** `_verify_inherited_fd()` has two independent
rejection gates - (a) the fd doesn't resolve via `/proc/self/fd` at
all, and (b) it resolves to the CORRECT lock path but is an
INDEPENDENT `open()`, not a genuinely inherited duplicate, so the
`flock()` probe must contend and fail. The original test suite only
exercised gate (a); added
`test_independently_opened_fd_on_same_path_genuinely_contends_and_fails`
(a helper process that independently `os.open()`s the real lock path,
makes the fd inheritable across its own `exec`, and claims it via
`DAY4_LOCK_FD` while a real holder is active) to exercise gate (b)
directly - passes, confirming path-matching alone was never sufficient
by design, only a genuinely shared lock is. **One documented-boundary
gap noted, addressed via a docstring caveat, not a code fix (by the
reviewer's own recommendation):** the design's exclusion guarantee
covers the wrapper being killed while its DIRECT child continues, but
does not extend to a wrapped command voluntarily self-detaching a
grandchild process - out of scope for this project (every Makefile
mutating step is a synchronous foreground command) but now stated
explicitly in `scripts/day4_lock.py::run()`'s own docstring for anyone
reusing this module elsewhere.

### 11.5 Current inventory and changed-file list

**Inventory:** 141 (unchanged - no new files this batch, only
modifications).

**Modified this batch:** `Makefile`, `scripts/day4_lock.py`,
`scripts/storage_hardening_check.py`, `scripts/storage_bootstrap.py`,
`scripts/retention_check.py`, `tests/test_day4_lock.py`,
`tests/test_storage_hardening_check.py`,
`tests/test_storage_bootstrap.py`, `tests/test_retention_check.py`,
and this register file (appended-to, not rewritten).

**Unchanged this batch:** every other file from batch 2b's 29-file
manifest, plus `VERSION` and this batch's additional required helper
imports (`scripts/http_checks.py`, `scripts/kube.py`,
`scripts/cluster_check.py`, `scripts/portforward.py`) - all carried
into this batch's ZIP unmodified so the uploaded subset can be
imported standalone without inventing missing fixtures.

### 11.6 What still requires resolution before PR

Unchanged from batch 2b's §10.7 except where superseded below:

1. **§11.1's three fixes are implemented, specialist-reviewed, and
   unit-verified against mocked/stateful-fake collaborators and real
   local subprocesses; live-cluster verification is still required** -
   a fresh `make day4-check` run (a later batch, explicitly out of
   this batch's authorized scope) is needed to prove the corrected
   namespace-creation/lock/absence behavior against the real cluster
   and the real Makefile's actual recursive `make` process tree, not
   just the mocked/simulated shape exercised here.
2. `secret_bootstrap.py::_get_secret()`'s equivalent substring-matching
   pattern (§11.1's "related, explicitly out of scope" note) is a
   candidate for a future batch - not fixed here, not silently
   forgotten.
3. `DAY4-ARCH-L1`'s owner decision, `DAY4-REL-3`'s `git ls-remote`
   re-attempt, and `DAY4-INT-1`/`DAY4-ARCH-L2`'s restart-trigger
   diagnostics script - all still pending, untouched this batch (as
   batch 2b already recorded).
4. The final-delete-time TOCTOU residual risk (documented in batch 2b,
   re-confirmed as out of THIS batch's scope by the security reviewer
   above) remains an accepted residual risk pending explicit owner
   sign-off - not reopened, not newly closed, by this batch.
5. This register's severity table must still reach all-CLOSED/ACCEPTED,
   confirmed by live validation, before the project's "no unresolved
   Critical/High/Medium before PR" rule is satisfied - **not yet
   reached.**
6. This batch's own verdict: **investigation, design explanation, and
   implementation complete for the three named gaps; unit/static
   validation green; specialist review complete for both named areas;
   zero live-cluster verification performed; zero finding fully CLOSED
   (implemented-pending-live-verification, consistent with the pattern
   batch 2b already established for its own work).**

---

*Batch 2c assembled by the parent Claude Code session directly
(Write/Edit access) for all source/test/documentation changes; the
project's read-only `kubernetes-security-reviewer` and
`kubernetes-test-engineer` specialist agents were invoked for
independent review only (their permissions were not broadened; they
cannot write) - see §11.4. The `cluster-integration-engineer` and
`release-engineer` agents were not invoked this batch (no live cluster
access was in scope; release readiness is not yet claimed). Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch2c-remediation-20260913T031123Z/`.*

---

## 12. Batch 4 — focused remediation of the two live batch-3 failures

Scope, per this batch's own briefing: fix exactly the two concrete
defects batch 3's live `make day4-check` run surfaced, nothing else.
Batch 3's full findings remain unedited at
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch3-validation-20260913T035757Z-8d62ee5b/00-FINAL-BATCH-REPORT.md`.

### 12.1 Cause 1 - `rollout-check` judged a stale, mid-rollout snapshot

`scripts/cluster_check.py`'s `wait_for_deployment_available()` only
polled the Deployment's `Available` CONDITION, which Kubernetes can set
true for a SUBSET of ready replicas mid-rollout. It never proved the
CURRENT rollout (the one `deploy` had just triggered by genuinely
changing `maops-gateway`'s ConfigMap/Deployment content for the first
time against this live cluster) had actually finished converging.
Batch 3 caught the Deployment status snapshot at `readyReplicas=2` and
a separate `get pods` call at `3/5 Pods Ready` (old-ReplicaSet Pods
still terminating alongside new ones) and failed.

**Fix:** added `wait_for_rollout_complete(deployment)` - a
generation-aware `kubectl rollout status deployment/<name>
--timeout=120s`, bounded via the existing `kube.subprocess_timeout_for()`
helper (never a fixed sleep), called after `wait_for_deployment_available()`
and before the strict replica/Pod assertions. `main()`'s per-workload
loop now re-fetches the Deployment fresh via `get_json(...)` after the
rollout-completion wait - `check_replica_counts` never again judges the
earlier, possibly-stale `Available=True` snapshot. Added
`wait_for_stable_pod_count(label_selector, count)` to settle out any
still-terminating old-ReplicaSet Pods (mirroring
`rollout_check._wait_exact_pod_count`'s already-established pattern,
matched to its 60s timeout) before `check_pods_ready` runs; on timeout it
returns the last SUCCESSFULLY observed Pod list (captured from inside
the polling predicate, never a second unguarded `get_pods()` call) so a
genuinely stuck rollout still produces an accurate `FAIL`, never an
uncaught exception.

New tests: `tests/test_cluster_check_rollout_wait.py` (9 tests) -
successful convergence; the exact "Available=True but rollout status
still reports incomplete" scenario; a hung-subprocess `TimeoutExpired`
converted to a clean failure; `wait_for_stable_pod_count` settling
immediately, settling after simulated old-terminating Pods, timing out
and returning the last-observed Pod list, and (added after
`kubernetes-test-engineer`'s review, see §12.3) `get_pods` failing on
every poll and failing only on what would have been the final poll -
both must return cleanly, never raise; and one test exercising the
real `cluster_check.main()` control flow (mocking only external
collaborators) proving `check_replica_counts` receives the freshly
re-fetched post-rollout snapshot, never the stale
`wait_for_deployment_available()` one.

### 12.2 Cause 2 - freshly loaded local images never reached already-running app/state Pods

Batch 3's byte-level image-provenance investigation proved `deploy`'s
`kubectl apply -k` happened to change `maops-gateway`'s content enough
to trigger a real rollover onto that run's freshly built/loaded image,
but `maops-app`'s Deployment and `maops-state`'s StatefulSet
pod-template hashes did not change - their already-running Pods kept
executing a container built 5 days earlier, even though `kubectl apply`
reported those objects "configured" and the node's `:0.4.0` tag now
pointed at new content. `kind load docker-image` only makes an image
available to containerd; it never touches an already-running Pod.

**Fix:** new `scripts/workload_refresh.py` and Makefile target
`workload-refresh`, inserted into the authoritative `day4-check`
sequence immediately after `deploy` and before `rollout-check` (also
its own standalone target, gated under the existing `$(DAY4_LOCK)`
exactly like its neighbors). After `kube.verify_context()`, it forces
`maops-state` (StatefulSet), then `maops-app`, then `maops-gateway`
(Deployments) to restart through their own controllers via `kubectl
rollout restart`, in that strict dependency order - never a Pod
delete, never a manual scale-to-0, never touching any PVC/PV/Secret -
each restart immediately followed by a bounded `kubectl rollout status
--timeout=120s` wait (same bounded-subprocess-timeout pattern as
§12.1). A restart or convergence failure on one workload stops the
sequence before any later workload is touched. Retains the existing
local `<name>:0.4.0` mutable-tag naming - no registry, no Helm, no new
image-management framework. Documented explicitly (in the script's own
docstring, the Makefile help text, and its own runtime NOTE line) as an
intentional local-image refresh with a brief real `maops-state` outage
(single replica) - `maops-app`/`maops-gateway` stay within their
existing PodDisruptionBudget (`minAvailable: 2` against `maxUnavailable: 1`).

New tests: `tests/test_workload_refresh.py` (8 tests) - strict
state->app->gateway restart order; a state restart failure and a state
convergence failure both stopping before app/gateway are touched; an
app failure stopping before gateway (confirming state already
succeeded); `kube.verify_context()` failure short-circuiting before any
restart; both `wait_rollout` failure modes (stuck rollout, hung
subprocess) producing clean recorded failures, never a traceback.

### 12.3 Focused independent review

Both reviews were scoped to exactly the changes above, per this
batch's own briefing; neither agent's permissions were broadened, and
the parent session recorded both results itself.

**`kubernetes-test-engineer`** (scope: the rollout-wait fix and its
tests) - confirmed the core fix genuinely solves the described defect
(generation-aware completion vs. a bare availability condition);
confirmed both `wait_for_rollout_complete()` return paths are clean,
no uncaught-exception path; confirmed the `main()`-level test
genuinely exercises real production control flow, not a mock-only
tautology. **One must-fix found and closed this batch:**
`wait_for_stable_pod_count`'s original post-timeout fallback issued a
brand-new, unguarded `get_pods()` call after `wait_until()` gave up -
if that second call itself raised, it propagated uncaught, contradicting
the function's own "never raises" contract; fixed by capturing the last
successful observation from inside the polling predicate instead (see
§12.1), with two new regression tests added. **One should-fix found
and closed:** `POD_COUNT_SETTLE_TIMEOUT_SECONDS` was set to 30s with no
stated reason for being half of `rollout_check.py`'s analogous,
same-purpose 60s timeout for the identical old-Pod-termination race;
aligned to 60s.

**`cluster-integration-engineer`** (scope: the workload-refresh
sequence) - independently confirmed, from actual containerd/kubelet
`IfNotPresent` resolution semantics (not merely from the script's own
comments) that a `rollout restart` combined with an already-reloaded
local `:0.4.0` tag reliably produces Pods running the freshly loaded
content, given `image-load` loads to every node with no `--nodes`
restriction and there is no external registry to fall back to; verified
the strict state->app->gateway short-circuit is genuinely enforced in
`main()`/`refresh_workload()`, not merely documented; verified from the
actual manifests (not assumed defaults) that `maxUnavailable: 1`
against `minAvailable: 2` on both Deployments' PDBs makes the "no real
app/gateway outage, only a real single-replica state outage" framing
accurate; confirmed via `kubectl rollout restart`'s actual patch target
(`spec.template.metadata.annotations` only) that PVC/PV/Secrets can
never be reached by this operation. **Zero must-fix or should-fix
issues found** - no code change resulted from this review.

### 12.4 Live validation (one fresh `make day4-check`)

Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch4-remediation-20260913T050712Z-7d1e8514/`.

Both fixes are proven live, in the one authoritative sequence, for the
first time: `workload-refresh` (new stage) reported **6/6 checks
passed**, restarting state -> app -> gateway in strict order with real
`kubectl rollout status` convergence output for each; the corrected
`rollout-check` (`scripts/cluster_check.py`) then reported **35/35
real cluster checks passed**, including the new
"`<deployment> rollout status: current generation fully rolled out`"
assertions for both gateway and app; `scheduling-check` then passed
**12/12**. All three workloads' Pods were confirmed (via a fresh,
independent `kubectl get pods -o json` read, not reused evidence) to be
running `import-2026-09-13`-tagged images - the exact node-side config
digests this run's own `docker build` produced - for the first time in
this project's history for `maops-app`/`maops-state` in a live
`day4-check` run (batch 3's byte-level provenance work had proven these
same digests, for source that has not changed since, byte-identical to
current on-disk `server.py`; that evidence is reused here per this
batch's own instruction not to repeat unchanged blob comparisons,
rather than re-run).

**The run still failed overall** (`MAKE_EXIT_CODE=2`) - at
**`discovery-check`** (`Makefile:186`, `scripts/discovery_check.py`),
the very next stage after `scheduling-check`. This is a genuinely
different, previously-undiscovered gate: batch 3 never reached this
far, so this exact live code path had never executed in a full
`day4-check` run before. Root cause, read directly from
`scripts/portforward.py`'s `port_forward()`: its `kubectl` invocation
passes `--context` but never `--kubeconfig`, unlike every call through
`kube.run()` (which always passes `--kubeconfig $KUBECONFIG_PATH`
explicitly). With only `KUBECONFIG_PATH` (this project's own env var)
exported by the Makefile - and the standard `KUBECONFIG` env var never
set - `port_forward()`'s bare `kubectl --context kind-maops-k8s-day4
...` subprocess falls back to the default `~/.kube/config`, which does
not contain the `kind-maops-k8s-day4` context (kind wrote it only to
the explicit `--kubeconfig` path given to `kind create cluster`),
producing exactly the observed
`error: context "kind-maops-k8s-day4" does not exist`. This is **out
of this batch's authorized scope** (fix exactly two named issues) and
was **not fixed** - per this batch's own instructions, the failure was
preserved, safe restoration was independently confirmed (below), and
no blind rerun was attempted.

**Restoration/preservation confirmed after the failed run:** all three
workloads 3/3, 3/3, 1/1 Ready (the freshly-restarted Pods from
`workload-refresh`, 0 restarts); PVC `data-maops-state-0`
(`00b512bf-7774-4873-a092-c56c37b9b1b7`) and PV
(`2c7d826b-1c69-4c73-b1c5-6cd23a91a14e`) both Bound, identities and
`claimRef` unchanged from the external pre-run baseline; both Secrets
(`maops-internal-auth`, `maops-state-auth`) present with unchanged
UIDs, not rotated; external `/state` value re-read independently
post-failure via a bounded gateway port-forward -
`day4-retention-054e49df1b0a481c`, byte-identical to the external
pre-run baseline (expected: neither `state-check` nor
`persistence-check`/`retention-check` ever ran, since the sequence
stopped at `discovery-check`, well before them); the mutation lock was
released (no active holder); no leaked port-forward or `day4-check`
processes; the historical `maops-day4-storage-preflight` namespace/PVC/
Pods and the stopped Day 1-3 clusters/containers were all untouched.
No Secret rotation, no cluster recreation, no Docker prune/reset
occurred.

Repository drift: the five original Day 4 review reports re-hashed
byte-identical to their batch-3 values. Changed this batch: `Makefile`,
`scripts/cluster_check.py` (both modified); `scripts/workload_refresh.py`,
`tests/test_cluster_check_rollout_wait.py`,
`tests/test_workload_refresh.py` (new). No other repository file
touched.

### 12.5 What remains open after this batch

1. **`discovery-check`'s `scripts/portforward.py` missing-`--kubeconfig`
   gap (§12.4)** - newly surfaced, unfixed, unassigned a finding ID.
   Everything downstream of `discovery-check` in `day4-check`
   (`secret-check`, `smoke`, `dependency-check`, `scaling-check`,
   `rolling-update-check`, `pdb-check`, `state-check`,
   `persistence-check`, `retention-check`, `final-state-check`) still
   has **zero live evidence from a full run** - this includes Contract
   A/B, which remain exactly as unproven as batch 3 left them, for a
   different reason now (a proof-script plumbing bug, not the two
   causes this batch fixed).
2. Every item batch 3's own report listed as separately outstanding
   (`DAY4-ARCH-L1`'s owner decision, `DAY4-REL-3`'s re-attempt,
   `DAY4-INT-1`/`DAY4-ARCH-L2`'s restart-trigger diagnostics, the
   final-delete-time TOCTOU residual risk) remains exactly as open as
   before - neither closed nor reopened by this batch.
3. This batch's own two fixes are **live-validated as far as the
   sequence reached** (`workload-refresh` 6/6, `rollout-check` 35/35,
   `scheduling-check` 12/12) but a full, end-to-end
   `make day4-check` **PASS has still never been observed** for this
   candidate.
4. No commit, push, PR, merge, tag, or release action was taken or is
   implied by this batch.

---

*Batch 4 assembled by the parent Claude Code session directly
(Write/Edit access) for the two named fixes and their tests; the
project's read-only `kubernetes-test-engineer` and
`cluster-integration-engineer` agents were invoked for independent,
scoped review only (permissions not broadened; they cannot write) -
see §12.3. Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch4-remediation-20260913T050712Z-7d1e8514/`.*

---

## 13. Batch 5 — kubeconfig correction and the first full `day4-check` PASS

Scope: fix the one gap batch 4 surfaced (`scripts/portforward.py` never
passing `--kubeconfig`), scan the active `day4-check` sequence for the
same omission elsewhere, then complete live validation. Batches 3 and
4's evidence and reports are preserved unedited.

### 13.1 Cause and fix

`scripts/portforward.py`'s `port_forward()` built its `kubectl
port-forward` subprocess with `--context` but never `--kubeconfig`,
unlike every other kubectl call in this project (`kube.run()`). With
only `KUBECONFIG_PATH` (this project's own env var) exported and the
standard `KUBECONFIG` never set, it silently fell back to the default
`~/.kube/config`, which never contains this project's isolated per-day
context - exactly the `error: context "kind-maops-k8s-day4" does not
exist` batch 4 hit at `discovery-check`.

**Fix:** `port_forward()` gained a `kubeconfig_path: str | None = None`
parameter, defaulting to `kube.KUBECONFIG_PATH` (read at call time, not
def time) when unset; the constructed argv now includes `--kubeconfig
<path>`. All ~26 existing call sites (positional, no `kubeconfig_path`
argument) needed zero changes. New tests: `tests/test_portforward_kubeconfig.py`
(9 tests) - default/override/call-time-read behavior, context/namespace/
resource-kind/port-mapping preservation, positional-caller compatibility,
and `_terminate()` cleanup on both normal exit and an exception.

**Downstream scan** of the Makefile/scripts reachable from `day4-check`
for the same pattern found one more instance:
`scripts/pdb_check.py::attempt_eviction()` builds its own raw
`subprocess.run(["kubectl", "--context", ...])` (it needs `input=` for
the Eviction API body, so it can't go through `kube.run()`) and had the
identical gap. Fixed the same way: `"--kubeconfig", kube.KUBECONFIG_PATH`
added to that argv. Everything else already either goes through
`kube.run()`, already passes `--kubeconfig` explicitly
(`storage_bootstrap.py`, `storage_hardening_check.py`, the Makefile's
own direct `kubectl` lines), or is a pure local `kubectl kustomize`
render needing no cluster contact at all (`manifest_check.py`,
`version_check.py`). New tests: `tests/test_pdb_check.py::EvictionSubprocessKubeconfigArgumentTests`
(3 tests) - exercises the real `attempt_eviction()`, not a mock of it.

`scripts/final_state_check.py::check_no_leaked_port_forwards()` (a
substring match on `ps ax` output for `"kubectl"`/`"port-forward"`/
`CONTEXT`/`NAMESPACE`) was confirmed unaffected - inserting
`--kubeconfig <path>` ahead of `--context` in the real argv removes
nothing that check depends on.

### 13.2 Focused independent review

`kubernetes-test-engineer` (scope: the `--kubeconfig` fix and its
tests) - confirmed the fix mechanism is correct and can never resolve
to a falsy path (`kube.KUBECONFIG_PATH`'s own `or`-fallback guarantees
this); confirmed no circular import; independently re-ran the same
downstream scan and confirmed it complete; confirmed both new test
files exercise the real functions at the real subprocess boundary, not
a tautology; confirmed `_terminate()`/SIGTERM handling and the
leaked-port-forward check are untouched/unaffected. **Zero must-fix or
should-fix issues found** - no further code change resulted. (The
review agent also flagged, unprompted, an apparent prompt-injection
attempt via a tool-result channel unrelated to this task - it did not
act on it; recorded here for the record, not itself a finding about
this codebase.)

### 13.3 Live validation - the first full `make day4-check` PASS

Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch5-remediation-20260913T081312Z-639c51a8/`.

A read-only `discovery-check` run first confirmed the originally
failing path now works (2/2 checks passed) before any live mutation.

The subsequent full `make day4-check` run's first attempt failed
immediately at `tool-check` (`docker resolves to
/home/raiyan10/.local/bin/docker, expected /usr/bin/docker`) - a
session-shell `PATH` ordering artifact (a pre-existing `~/.local/bin/docker`
shim pointing at Windows Docker Desktop's `docker.exe`, unrelated to
this repository, not introduced by this batch) caught before any
mutation occurred; the lock was released cleanly and nothing was
touched. Corrected the invoking shell's `PATH` (no repository change)
and reran with a fresh run ID/baseline path.

**Result: `make day4-check` PASSED completely for the first time in
this project's history.** `MAKE_EXIT_CODE=0`, `TEE_EXIT_CODE=0`. Every
stage passed, including `workload-refresh` (6/6), the corrected
`rollout-check` (35/35), `discovery-check` (2/2, the originally-failing
stage), `secret-check` (49/49), `smoke` (6/6), `dependency-check`
(11/11), `scaling-check` (20/20), `rolling-update-check` (38/38),
`pdb-check` (16/16), `state-check` (24/24), `persistence-check`
(12/12), `retention-check` (22/22), and `final-state-check` (39/39).

**Persistence (Contract, Pod deletion/rescheduling):** marker written
and read back through the full service chain, survived a real
`kubectl delete pod maops-state-0`, PVC/PV UIDs unchanged throughout,
then independently restored to the true original value
(`day4-retention-054e49df1b0a481c`) and re-verified via an independent
GET.

**Retention (Contract A/B, state 1->0->1):** Contract A (a fresh marker
survives the outage) and Contract B (the true original value is
restored, verified via an independent GET after Contract A's own check)
both proven; PVC/PV remained Bound throughout with unchanged UIDs;
app/gateway degraded-but-live (`/readyz` 503, `/livez` 200) during the
outage, as designed.

**Suite-baseline vs. external pre-suite baseline:** `final-state-check`
independently confirmed the suite-level baseline (captured by
`state-check`, run ID `025415f3d29847c0b1035d49d45a5a87`) and the
current live value both equal `day4-retention-054e49df1b0a481c` - the
exact same value this batch's own external pre-run baseline (captured
before any mutation, via a bounded, schema-validated gateway
port-forward) recorded. All three agree.

**Restoration:** app/gateway/state at 3/3, 3/3, 1/1 Ready; PVC
`data-maops-state-0` (`00b512bf-7774-4873-a092-c56c37b9b1b7`) and its PV
(`2c7d826b-1c69-4c73-b1c5-6cd23a91a14e`) both Bound, unchanged; both
Secrets present, unrotated; zero leaked port-forward/day4-check
processes (confirmed independently, not just by the suite's own
check); Days 1-3 and the historical preflight namespace untouched.

**Image/source provenance**, independently re-verified by
`cluster-integration-engineer` (read-only, no cluster mutation): all 7
running Pods' `imageID` and node-level `crictl inspect` config digests
exactly match this run's fresh `docker build` output for all three
workloads, cross-checked identical on all 3 nodes. Since
`app/server.py`/`gateway/server.py`/`state/server.py` have not changed
since batch 4's full byte-level content-store `cmp` investigation
(confirmed via mtime and git history), and this run's config digests
are byte-identical to what that investigation already verified, that
prior evidence is reused for content-identity rather than repeated -
per this batch's own instruction not to re-run unchanged blob
comparisons. A full restart/event-timeline reconstruction confirmed
every Pod recreation across the run maps exactly one-to-one onto a
deliberate experiment (workload-refresh x3, dependency-check,
scaling-check x2, rolling-update-check x2, pdb-check x2,
retention-check, persistence-check) with no unexplained restart wave.

Repository drift: the five original Day 4 review reports re-hashed
byte-identical to batch 3's originally-recorded values. Changed this
batch: `Makefile`, `scripts/cluster_check.py`, `scripts/pdb_check.py`,
`scripts/portforward.py`, `tests/test_pdb_check.py` (modified);
`scripts/workload_refresh.py`, `tests/test_cluster_check_rollout_wait.py`,
`tests/test_portforward_kubeconfig.py`, `tests/test_workload_refresh.py`
(new, `workload_refresh.py`/`test_cluster_check_rollout_wait.py`/
`test_workload_refresh.py` already existed from batch 4). No other
repository file touched.

### 13.4 What remains open

1. Every item batch 3/4's own reports already listed as separately
   outstanding (`DAY4-ARCH-L1`'s owner decision, `DAY4-REL-3`'s
   re-attempt, `DAY4-INT-1`/`DAY4-ARCH-L2`'s restart-trigger
   diagnostics, the final-delete-time TOCTOU residual risk, and the
   newly-surfaced `cluster_check.py` rollout-vs-snapshot race this
   batch's own predecessor fixed) - none of these are re-adjudicated
   here; this batch neither closes nor reopens any of them.
2. This batch's own two-issue scope (kubeconfig plumbing) is now fully
   live-validated with a genuine end-to-end PASS - but that PASS is one
   data point, not a standing guarantee; this register's severity table
   (§ per batch 2c) still governs overall PR-readiness, which remains a
   separate, not-yet-made owner decision.
3. No commit, push, PR, merge, tag, release, or portfolio-roadmap edit
   was made or is implied by this batch.

---

*Batch 5 assembled by the parent Claude Code session directly
(Write/Edit access) for the kubeconfig fix and its tests; the project's
read-only `kubernetes-test-engineer` and `cluster-integration-engineer`
agents were invoked for independent, scoped review only (permissions
not broadened; they cannot write) - see §13.2 and §13.3. Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/batch5-remediation-20260913T081312Z-639c51a8/`.*

---

## 14. Final adjudication (2026-09-13) — consolidated dispositions, GO conditional on three owner decisions

Documentation-only batch: reconciles every finding across all five
original reviews and all six remediation batches (2, 2b, 2c, 3, 4, 5)
into one evidence-supported disposition each, resolves the four
outstanding process questions (DAY4-ARCH-L1, the historical restart
findings, the namespace-cleanup TOCTOU residual, and DAY4-REL-3's
remote-ref check), and obtains an independent `release-engineer`
final review. No implementation, test, manifest, roadmap, or cluster
change was made. Full detail:
`docs/engineering-reviews/day-04-final-adjudication.md`.

**Corrected framing:** batch 5 is the first complete pass of the
*remediated* candidate, not the first pass in this project's entire
history - batches 3 and 4's failed live attempts, and batch 5's own
initial `tool-check` failure (a session-shell `PATH` artifact from a
pre-existing `~/.local/bin/docker` shim, unrelated to the repository
and left untouched), are all preserved in the adjudication document's
§1, not summarized away. The exact successful Docker/PATH invocation
is recorded there for reuse in a future merged-`main` validation.

**Disposition summary:** every Critical/High/Medium finding from the
five original reviews, plus every finding discovered during the six
remediation batches (including the three found live during batches
3-5: the `cluster_check.py` rollout-vs-snapshot race, the
`workload-refresh` image-staleness gap, and the `portforward.py`/
`pdb_check.py` missing-`--kubeconfig` gap), is **CLOSED** with cited
code/test evidence and, where applicable, a specific passing stage in
batch 5's live log. `DAY3-SEC-M1` remains **CLOSED**, unchanged, per
Day 3's own final adjudication - not reopened, not re-litigated.
Zero Critical/High/Medium findings remain open.

**Three items remain, requiring an explicit owner decision this
adjudication does not make on the owner's behalf** (each with a
specific recommended disposition, detailed in the adjudication
document's §3):

1. `DAY4-ARCH-L1` - bounded acceptance of the fixed three-node local
   lab's manual storage-bootstrap-on-node-change contract vs. a Day
   5+ DaemonSet enforcement mechanism.
2. `DAY4-INT-1`/`DAY4-ARCH-L2` - Low/Informational acceptance of the
   historical, permanently-unrecoverable restart-wave incident, given
   two independently clean full live-validation runs (batches 4, 5)
   found zero unexplained restarts in either.
3. Namespace-cleanup TOCTOU residual (post-`DAY4-SEC-M2`) - Low
   disposition for the narrowed, lock-protected, last-instant race
   against a scratch namespace only; independently re-derived (not
   merely accepted) by the reviewing `release-engineer` from the
   actual code and this project's own mutual-exclusion lock
   discipline - not treated as already accepted merely because an
   earlier specialist review used the word "accepted."

**Independent `release-engineer` review** (read-only, unchanged
permissions; full verbatim report in the adjudication document's §4):
spot-checked 14 finding IDs across all five reviewers' sections
against live repository state and the batch 5 log, substantiated all
of them; independently re-derived the TOCTOU severity call from the
actual code rather than trusting this document's framing; confirmed
the owner-acceptance-pending items are honestly framed as pending, not
assumed; confirmed no discrepancy in the DAY4-REL-3 remote-ref result
(with the caveat that this sandbox has no SSH credentials to
personally re-run `git ls-remote`, so that one check rests on the
values matching every prior batch's independently-recorded citation);
confirmed zero "production-ready" language anywhere in the document.
Independently reached the same verdict.

**Final verdict: GO — CONDITIONAL ON SPECIFIED OWNER DECISIONS** (the
three items above). No other Critical/High/Medium finding and no other
owner decision blocks PR readiness as of this adjudication. No commit,
push, PR, merge, tag, release, or portfolio-roadmap edit was made or
is implied.

---

*This adjudication was assembled by the parent Claude Code session
directly (Write/Edit access, documentation only - no implementation,
test, manifest, or cluster change); the project's read-only
`release-engineer` agent was invoked for independent final review only
(permissions not broadened; it cannot write) - see §14 above and the
full report in `docs/engineering-reviews/day-04-final-adjudication.md`.*

---

## 15. Owner acceptance recorded (2026-09-13) — GO FOR PR

The project owner has explicitly accepted all three items §14 left
pending, delegating the acceptance recommendation to Codex, which
recommended acceptance of all three. Recorded here as **ACCEPTED
limitations - deliberate, owner-ratified scope decisions, not
technically fixed defects.** No implementation, test, manifest, or
cluster change was made to produce this entry; §14's conditional
verdict is preserved above unchanged as the historical record of this
adjudication's prior state. Full detail, including the exact accepted
scope of each item:
`docs/engineering-reviews/day-04-final-adjudication.md` §6.

1. **`DAY4-ARCH-L1` - ACCEPTED.** Fixed three-node local lab; any
   new/replaced node requires an explicit, manual `make
   storage-bootstrap` + `make storage-hardening-check` re-run,
   verified before any workload's storage is scheduled onto it. No
   automated node-expansion enforcement is claimed. A Day 5+
   DaemonSet-based mechanism remains a legitimate but non-required
   future enhancement.
2. **`DAY4-INT-1`/`DAY4-ARCH-L2` - ACCEPTED** as Low/Informational.
   The historical second-SIGKILL-wave incident's proximate cause
   remains genuinely unestablished and is **not** retroactively
   assigned one by this acceptance - the investigation and its
   findings (batch 1's original observation; batches 4 and 5's
   independently clean restart/event-timeline reconstructions, zero
   unexplained restarts in either) remain preserved in full and must
   not be characterized elsewhere as "resolved" or "root-caused," only
   as "historical, unexplained, accepted, not reproduced."
3. **Namespace-cleanup TOCTOU residual (post-`DAY4-SEC-M2`) -
   ACCEPTED** at Low severity, scoped exactly as independently
   re-derived in §14's `release-engineer` review: the disposable
   scratch namespace only (never the application namespace, Secrets,
   or the `maops-state` PVC), realistically reachable only by an
   external actor bypassing this project's own lock-protected
   invocation path. This accepts the residual's realistic blast radius
   and likelihood as scoped - it does not claim the TOCTOU window
   itself is closed.

**Resulting disposition: GO FOR PR.** With all three items now
ACCEPTED and zero Critical/High/Medium findings otherwise open (§14),
the Day 4 (v0.4.0) candidate - HEAD
`aa2049876c7be2b959acb6e2a1d20f979ee440bc` on
`feature/day-4-stateful-persistence`, validated complete and passing
by batch 5's live `make day4-check` run - is ready for PR. This covers
PR readiness only; it does not itself commit, push, merge, tag, or
release anything, and it is not a general production-readiness claim -
this remains a local, single-operator, three-node `kind` lab, and the
three accepted items above remain visible, documented limitations of
that scope.

---

*This entry was assembled by the parent Claude Code session directly
(Write/Edit access, documentation only - no implementation, test,
manifest, or cluster change). No agent was invoked for this entry - it
records an owner decision already made, not a new technical
assessment. Full detail:
`docs/engineering-reviews/day-04-final-adjudication.md` §6.*

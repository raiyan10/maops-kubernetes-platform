# Day 7 (v0.7.0) - independent reviews

**Date:** 2026-09-28. **Subject:** the unstaged Day 7 diff on
`feature/day-7-deployment-strategies` (HEAD `74832c4`) and the live
evidence of run `dd99769f587d4a869628d51b9725b458`
(`day-07-live-validation-record.md`).

Five project reviewers (`kubernetes-architect`,
`kubernetes-security-reviewer`, `cluster-integration-engineer`,
`kubernetes-test-engineer`, `release-engineer`) reviewed read-only: no
file edits, no git mutation, no cluster contact. All five were first
interrupted by a session usage limit and resumed with their context
intact; no verdict below predates that resumption. Findings are recorded
as the reviewers stated them, with the disposition and any correction.

## Verdicts

| Review | First pass | After remediation |
|---|---|---|
| Architecture | APPROVE WITH NON-BLOCKING FINDINGS | (no re-review needed - LOW/INFO only; dispositions below) |
| Security | APPROVE WITH NON-BLOCKING FINDINGS | 2nd pass: APPROVE WITH NON-BLOCKING FINDINGS (one NEW MEDIUM); 3rd pass: **APPROVE** |
| Cluster integration | APPROVE WITH NON-BLOCKING FINDINGS | **APPROVE WITH NON-BLOCKING FINDINGS** (only remaining item: fixes not yet exercised live) |
| Test | APPROVE WITH NON-BLOCKING FINDINGS | **APPROVE** (verified by mutation testing) |
| Release readiness | READY AFTER NON-BLOCKING FIXES | final re-check: **READY FOR RELEASE** - defined by the reviewer as ready for the project owner's adjudication / independent human review, not a release; nothing committed, tagged or released |

No reviewer raised a BLOCKING finding.

## Findings and dispositions

### Architecture
- **LOW** - a true one-shot `make day7-check` has never been run (the
  live run was staged). *Disposition:* agreed; recorded as a remaining
  limitation - recommended before release sign-off.
- **INFO** - the candidate's no-PDB comment over-generalized ("a PDB
  would not protect it anyway"). *Fixed:* the comment in
  `gateway-candidate-deployment.yaml` now states the candidate has no
  voluntary-eviction protection at any time while enabled, and scopes
  the "would not protect" claim to a Recreate rollout.
- **INFO** - live defects (cni-status race, memory preflight) disclosed
  and the readiness-wait fix scoped narrowly; orchestration found sound.

### Security
- **MEDIUM** - `validation-client -> gateway-candidate` is not probed
  live. *Disposition:* not changed; the denial is proven statically by
  `validate_helm_chart.py`'s NetworkPolicy path matrix (candidate must
  equal stable, including `validation-client -> gateway:8080` False) and
  AuthorizationPolicy coverage. The reviewer's second pass accepted this
  as a sound compensating control and downgraded it to INFO/closed; it
  remains a recorded live-evidence limitation.
- **LOW** - `PATH_PLAN` correctness depended on declaration order.
  *Fixed:* positive controls are always evaluated first (stable sort);
  `PathPlanOrderTests` prove reversed plans give identical verdicts.
- **NEW MEDIUM (2nd pass)** - leaf functions that reach a cluster were
  guarded only by `main()`: `day7_final_check.check_day7_identities`,
  `day7_recreate.run_observed_upgrade`. *Fixed:* both self-guard (also
  `day7_nodes_ready.read_nodes`); `LeafProfileGuardTests` call each leaf
  directly under the Day 6 profile and assert refusal with no
  kubectl/Popen/prober started. 3rd pass: APPROVE.

### Cluster integration
- Record-vs-log cross-check: every number, timestamp, revision,
  generation, endpoint set and count in the live record matched the logs.
- **MEDIUM** - "Day 7 final checks 39/39 contains only 37 distinct
  assertions". *Corrected on verification:* log 33 shows 41 printed PASS
  lines, 39 unique; the script's tally (39) was never inflated - two
  leak lines were *printed* twice. The reviewer withdrew the count claim
  on re-review. *Fixed:* each leak fact now prints once.
- **MEDIUM** - `day7-resume-check` lacked the new readiness wait.
  *Fixed:* it now starts with `day7-nodes-ready` (pinned by test).
- **LOW** - two record statements lacked numbered logs (the 09:22:13 Day
  6 exit; "lock present, not held"). *Fixed by attribution:* the record
  now states both came from in-session read-only checks not saved as
  logs (Docker has since overwritten the original FinishedAt).
- **INFO** - `day7-nodes-ready` only verified read-only against an
  already-Ready cluster.
- Re-review: all four items resolved; remaining non-blocking item is
  that none of the fixes has been exercised live.

### Test
- **HIGH** - `day7_final_check.py` comparison logic untested. *Fixed:*
  `FinalCheckLogicTests` (all-pass baseline, 21 single-field mutations,
  `main()` exit codes, missing baseline).
- **HIGH** - `gateway_is_current()` untested. *Fixed:*
  `GatewayIsCurrentTests` (stale generation, False/missing conditions,
  API error, timeout, bad JSON, non-Day-7 refusal).
- **MEDIUM** - `verify_stable()` composition untested. *Fixed:* each of
  five sub-checks failing alone must fail it.
- **MEDIUM** - promotion Helm failure after a clean gate untested and
  unrecorded. *Fixed:* reason recorded in Blue/Green and Canary (Recreate
  already recorded); tested for all three strategies.
- **MEDIUM** - Canary Phase A happy path, Pod churn, failed Phase A
  restoration untested. *Fixed:* `CanaryPhaseTests`.
- **LOW** - "Blue kept warm" negative; version-check Day 7 negatives.
  *Fixed.*
- **INFO** - pre-existing `skipTest` fallback in
  `tests/test_validate_helm_chart.py` (not Day 7; left unchanged; never
  skips here).
- **Found during remediation:** a latent flake in
  `tests/test_day7_orchestration.py` - fake Pod IPs derived from
  `hash(name)` collided for ~3% of `PYTHONHASHSEED` values (2/60 seeds
  failed). *Fixed:* unique deterministic IPs (0/42 seeds fail, including
  the two that failed). Also found: an insufficiently mocked new test
  attempted real kubectl calls against the Day 6 context under the
  default profile - blocked by the cluster-blocking PATH shim used for
  every run, so no cluster was reached; this led to the Day 7 guard on
  every kubectl/Helm call in `day7_strategy.py`.
- Re-review: APPROVE - the reviewer broke each guarded function in memory
  and all 7 mutations were caught by the new tests.

### Release readiness
- Version/tags 0.7.0, `version-check` 64/64, git safety, frozen Day 1-6
  paths, scope boundaries (no Ingress/waypoint/HPA/Argo/unpinned
  infra/committed Secret), evidence modes, and non-overclaiming docs: PASS.
- **MEDIUM** - README test count stale. *Fixed* (current count).
- **MEDIUM** - the seven-stage -> eight-stage roadmap change needed
  confirmation of explicit authorization. *Disposition:* authorized - the
  user's Day 7 instruction defined "Day 7 is v0.7.0 ... Day 8 is the
  proposed v1.0.0 autoscaling and final-hardening milestone".
- **INFO** - the `maops-k8s-day7` cluster is still running; the Day 6
  containers remain stopped by the operator's explicit action.
- **INFO** - Day 6's specialist-review document set is the model for
  Day 7's; this file and the live record are Day 7's.

## Remaining (non-blocking) before any release decision
- A fresh one-shot `make day7-check` (the recorded run was staged).
- Live exercise of the post-run changes (`day7-nodes-ready` under a real
  cold start / `day7-resume-check` after a restart; the leaf guards;
  deduplicated final-check output).
- A live `validation-client -> gateway-candidate` probe (currently
  static proof only).
- A final adjudication by the project owner. These reviews do not by
  themselves declare Day 7 release-ready.

*Status of the list above as of 2026-09-29:*
- **One-shot `make day7-check`:** run once, on the existing cluster
  (`ce55f5fb...`). It exposed the image-contract defect.
- **Live `validation-client -> candidate` probe:** passed.
- **`day7-resume-check` after a restart:** exercised for real, and it
  failed (see the live record).

## Focused reviews of the image-contract remediation (2026-09-29)
The four reviewers were `kubernetes-architect`,
`kubernetes-security-reviewer`, `cluster-integration-engineer` and
`kubernetes-test-engineer`. They reviewed the static remediation
read-only:
- `scripts/day7_build.py` and `scripts/day7_running_images.py`;
- the chart guard `maops.validatePinnedBuild`;
- the stage `build.requirePinnedTags` flag;
- integration in the strategy, baseline, final-check and image-check
  scripts;
- the Makefile targets and sequences;
- the tests.

Only the integration reviewer contacted the Day 7 cluster, and only
read-only. None contacted Day 1-6.

| Review | Verdict |
|---|---|
| Architecture | APPROVE WITH NON-BLOCKING FINDINGS |
| Security | APPROVE WITH NON-BLOCKING FINDINGS (all LOW/INFO) |
| Cluster integration | APPROVE WITH NON-BLOCKING FINDINGS |
| Test | APPROVE WITH NON-BLOCKING FINDINGS |

No reviewer raised a BLOCKING finding.

### Findings and dispositions

**Architecture**
- **MEDIUM:** the standalone `day7-running-images` judged only the
  mutable `current.json`. *Fixed:* once the run's strategy baseline
  exists, the gate requires the current build to equal the baseline's
  build and judges that build. A re-pointed `current.json` is refused
  (`RunningImagesMainTests`). The final check always judged the
  baseline's build.
- **MEDIUM:** the generic `image-load` of the mutable `:0.7.0` tags was
  called "inert". *Not changed:* those tags are still used by the Day 7
  helper Pods (storage, NetworkPolicy and validation-client probes).
  `docs/architecture.md` says so, and `day7-image-verify-nodes` still
  verifies them.
- **LOW:** the guard is reached only via `httproute.yaml`. *Verified
  covered:* that template renders unconditionally, and `helm_check`
  asserts that every stage without an overlay is refused. Removing the
  include therefore fails `make helm-check` (mutation M5 was caught).
- **LOW:** a new stage file without the flag. *Covered:* the stage-file
  set must equal the declared list, and `StageFilesTests` requires
  `build.requirePinnedTags: true` in every stage.
- **LOW:** keeping appVersion and VERSION in step. *Covered:*
  `version_check`.

**Security**
- **LOW:** the node-name regex used `match` with `$`. *Fixed:* it now
  uses `fullmatch`.
- **LOW:** `load_into_kind` did not re-derive the digest before
  loading. *Fixed:* each pinned tag's config digest is re-derived
  immediately before `kind load`, and a moved tag is refused
  (`test_tag_moved_after_record_is_not_loaded`).
- **LOW:** a crash mid-`store_build` could wedge the store. *Fixed:* the
  build directory is assembled under a temporary name and renamed into
  place (`test_interrupted_store_leaves_no_build_directory`).
- **Accepted, LOW/INFO:**
  - A same-UID time-of-check/time-of-use window exists between
    validating the overlay and Helm reading it. The post-apply
    `helm get values` equality check detects any change.
  - The guard checks tags, not repositories. The runtime gate compares
    the full `repo:tag` reference and the config digest.
  - Tags are content-derived, not `@sha256` pins. Admission does not
    enforce them; the running-image gate does.
  - Intermediate path components of the store are not `lstat`-checked.
    This is acceptable under the single-user threat model.

**Cluster integration**
- **Diagnosis confirmed:** the istio-cni post-reboot diagnosis is
  supported and was still the current state. The one inference step
  stays labelled as inference.
- **Mapping verified live:** the imageID -> `crictl` repoDigest ->
  config-digest mapping holds for all 7 containers (read-only), with
  exactly one match each.
- **Storage unchanged:** the PVC and PV identities are unchanged.
- **New observation:** `gateway-...-6tchc` on worker2 and
  `gateway-...-hgwpc` have restarted again since the diagnosis was
  captured (restart count 3). This is recorded.
- **MEDIUM:** a Phase 3 rollout would very likely replace, and thereby
  "cure", the three unenrolled Pods, and destroy the only reproduction.
  *Disposition:* Phase 3 is not run while `day7-resume-check` fails.
  Recovering by rollout or recreating Pods needs a separate, explicitly
  recorded owner decision, and its evidence must be labelled as
  remediation, never as a clean Phase 3 proof.
- **LOW:** before any future `day7-deploy`, confirm that the Cilium and
  istio-cni DaemonSets are Ready, and assert PVC/PV identity before and
  after. This is added to the Phase 3 plan.

**Test**
- **Required negatives:** all six were confirmed to exercise the
  intended failure. The suite passed with 0 blocked cluster calls.
- **MEDIUM:** the Recreate replacement-candidate image check was
  untested. *Fixed:* `RecreateReplacementImageTests` covers the call's
  components, count and build, and that a failure fails the experiment.
- **MEDIUM:** the `observe()` wiring was untested. *Fixed:*
  `ObserveWiringTests` covers:
  - unreadable or malformed values being fatal;
  - counts taken from the values;
  - a live candidate Pod while the candidate is disabled;
  - honoured overrides;
  - unreadable Pods being fatal.
- **LOW:** no tests for symlinked records. *Fixed:*
  `test_symlinked_pointer_or_record_is_refused`.
- **LOW:** fixture hermeticity depends on importing the fixture.
  *Accepted:* every test module that reaches the build store imports
  `tests/day7_build_fixture.py`, which points `DAY7_BUILD_ROOT` at a
  nonexistent path. Without it, `load_current` fails closed until a
  real store exists.
- **LOW:** "`verify_pinned_build` not found". *Corrected:* it is in
  `scripts/day7_image_check.py`, and `ImageCheckMainTests` covers it.
- **Author mutations:** the author's seven mutations were all caught.
  They removed the ref, digest and count checks, allowed an ambiguous
  imageID match, turned the chart guard off, retried fatal state, and
  skipped the baseline-vs-current build comparison. The fatal-retry
  test now uses a bounded fake clock, so that mutation fails fast
  instead of hanging.

## Focused reviews of the corrected running-image gate and post-review fixes (2026-09-29, before the final integration run)
Reviewers: `cluster-integration-engineer`, which checked the live Day 7
cluster read-only against CRI for all 7 containers;
`kubernetes-test-engineer`, which ran the suite under the blocking shim
and mutation experiments in a copy; and `kubernetes-security-reviewer`,
which reviewed statically.

| Review | Verdict |
|---|---|
| Cluster integration | APPROVE WITH NON-BLOCKING FINDINGS |
| Test | APPROVE WITH NON-BLOCKING FINDINGS (1657 tests OK, 0 blocked; 5/6 mutations caught) |
| Security | APPROVE WITH NON-BLOCKING FINDINGS (all LOW) |

No BLOCKING finding.

**Verified by the reviewers:**
- **Spec plus digest chain is sound:** trusting the Pod spec image for
  the reference check opens no bypass. The imageID -> single node
  record -> config digest -> pinned-tag chain still decides which bytes
  run. The old check on the runtime-reported tag caught nothing the new
  one misses.
- **CRI agreement:** for all 7 live containers, `crictl inspect`
  `status.imageId` equals the build's config digest and
  `user_specified_image` equals the pinned tag.
- **Plan sound:** the planned one-shot `day7-check` on the existing
  cluster was traced step by step.

**Dispositions:**
- **Test, LOW - fixed:** the `_NODE_RE.fullmatch` guard was
  effectively untested (the `search` mutation survived). Hostile node
  names are now tested (prefix, suffix, `;`, trailing space or newline,
  `-workerx`), and the mutation is caught (6 failures).
- **Test, LOW - fixed:** the old-bytes test now asserts that the spec
  check passes, so only the digest check can fail it.
- **Security, LOW - fixed:** `store_build` said `rename` "fails if the
  directory appeared". POSIX `rename` replaces an empty directory, so
  the comment was wrong. It now re-checks for the directory, or a
  symlink, immediately before renaming.
- **Security, LOW - fixed:** `load_into_kind` now refuses on its own
  outside the Day 7 profile, before any docker or kind call
  (`test_kind_load_refuses_outside_day7`).
- **Security, LOW - accepted:** a same-user window remains between
  re-deriving a tag's digest and `kind load`. It is backstopped:
  `day7-image-verify-nodes` and the running-image gate require each
  node record's config digest to equal the pinned digest.
- **Security, INFO - reworded:** the standalone gate's message about
  the baseline build claimed "the gate uses it". It now says the
  current build must equal the baseline's.
- **Integration, MEDIUM - not changed, with reason:** "the candidate
  image check may block on not-Ready". The check requires Running and
  Ready, exactly as the existing promotion gate already did. Each
  candidate stage is applied with Helm `--wait=watcher` before the
  gate, and the gate passed on its single observation in both earlier
  runs.
- **Integration, LOW - noted:**
  - Helper probe Pods use the mutable tag. That is correct, and
    ordering puts them after the loads.
  - The Recreate replacement check's bounded 240 s wait is sufficient.
  - If a rebuild reproduces identical digests, nothing rolls. That
    would make the rollout evidence weaker, and it will be reported if
    it happens.

## Focused reviews of the fresh-cluster plan and the `day7-image-load` fix (2026-09-30)
Reviewers: `cluster-integration-engineer` (read-only against the live
Day 7 cluster and Docker), `kubernetes-security-reviewer` (static, with
read-only Docker checks), and `release-engineer` (static). They
reviewed `day-07-fresh-cluster-plan.md` (a proposal, not executed) and
the one-line fix
`day7-image-load: $(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_build.py load-kind`.
No reviewer changed anything.

| Review | Verdict |
|---|---|
| Cluster integration | APPROVE WITH NON-BLOCKING FINDINGS |
| Security | APPROVE WITH NON-BLOCKING FINDINGS |
| Release | APPROVE WITH NON-BLOCKING FINDINGS |

No BLOCKING finding.

**Verified by the reviewers:**
- **Inventory:** the node container IDs and anonymous `/var` volumes;
  the PV is `local-path` with reclaim `Delete` on `worker2`. The PVC,
  PV and state data do **not** survive deletion.
- **Resolved commands:** `day7-cluster-delete` deletes by name only,
  under the Day 7 lock. `cluster-create` creates the cluster when it is
  absent.
- **Fresh-cluster behaviour:** no `day7-check` step assumes an existing
  release, PVC, Secret, baseline or image record.
- **Blast radius:** cannot reach Day 1-6, other kubeconfigs or
  unrelated Docker objects.
- **Secrets:** no Secret value appears in the inventory logs.
- **Image-load guards:** the recipe fix and the script's own profile
  guard both remain meaningful, and tests pin both.
- **Git safety:** index empty, frozen paths unchanged, `v0.6.0`
  unchanged.

**Dispositions** (all document changes; no code changed, so no
cluster-free re-run was required):
- **Release, MEDIUM - fixed:** the README, roadmap and architecture
  status lines implied only sign-off remained. They now say GATE
  PENDING, pending an approved fresh-cluster run.
- **Release, MEDIUM - fixed:** freshness had no pass/fail predicate.
  The plan now has invalidation criteria: a real `kind create`, new
  node container IDs created after the deletion, the app release at
  `REVISION: 1`, and new namespace, PVC and PV UIDs. Otherwise the
  attempt is void as a fresh-cluster proof.
- **Release, LOW - fixed:** the plan now covers interrupted runs,
  preserves a partially validated cluster after a failure, and requires
  every attempt to be disclosed in the status lines.
- **Release, LOW - fixed:** section E adds no coexistence proof (Day
  1-6 stopped), no continuity proof (new Secrets and state), an
  implementer-run execution, irreversible deletion, and the network
  dependency.
- **Release, LOW - fixed:** the failed attempt `5750593984...` did
  advance the platform Helm revisions, with identical values and no
  restarts. The live record and adjudication now say so.
- **Integration, MEDIUM - fixed:** a network dependency after deletion.
  C0 gains a read-only reachability gate over the Gateway API URL, the
  Cilium and Istio chart repositories (the Istio URL was corrected to
  the Makefile's `blob.istio.io`) and the image registries. It was run
  once on 2026-09-30 and everything was reachable (log `05`).
- **Integration, LOW - fixed:** C1 now checks that no stale kubeconfig
  entries remain, that `~/.kube/config` is unchanged, and that the
  `kind` network ID and subnet are unchanged. C0 checks the port
  listener.
- **Integration, LOW - fixed:** the network statement is corrected:
  only the Day 7 nodes are attached now.
- **Integration, LOW - corrected, not fixed in the log:** log `01`, not
  log `02`, has the three Go-template errors. Log `02` is the clean
  re-read, and the plan now says so.
- **Security, LOW - fixed:** the untracked Day 7 files exist only in
  the working tree. C0 now takes a private 0600 tarball backup.
- **Security, LOW - recorded as a limitation, not changed:** the
  shared `cluster-delete` recipe has no leaf guard of its own; it is
  the released Day 6 design, scoped by `CLUSTER_NAME` and `DAY7_MAKE`.
- **Security, INFO - noted:** the lock file is under `/tmp`, and the
  `day6_lock` contention message says "Day 6".

## Focused reviews of the v0.7.0 release-candidate documentation (2026-09-30)
Reviewers: `release-engineer` (status and consistency, static) and
`cluster-integration-engineer` (operational accuracy; `make_sequence`,
grep, and client-only `helm install --dry-run=client` with
`KUBECONFIG=/dev/null`). They reviewed:
- README, roadmap and architecture;
- the Day 7 adjudication and live record;
- `.claude/CLAUDE.md`, the skills and the agents;
- the stage-file headers and Makefile help text;
- `templates/NOTES.txt`;
- the two repaired Day 4/5 anchors in `docs/architecture.md`;
- the README Docker prerequisite.

No cluster was contacted.

| Review | Verdict |
|---|---|
| Release | APPROVE WITH NON-BLOCKING FINDINGS |
| Cluster integration | APPROVE WITH NON-BLOCKING FINDINGS |

No BLOCKING finding.

**Verified by the reviewers:**
- **Release claims:** no document claims released, tagged, merged or
  production-ready. GATE PASSED is scoped to the local Kind reference
  platform, with the PR, merge, merged-`main` validation and
  publication pending.
- **Consistency:** numbers, run IDs, build ID and revisions agree across
  the documents and the live record.
- **Failures preserved:** historical failures are kept, and the frozen
  Day 1-6 documents are unchanged.
- **Docker row:** claims no minimum version.
- **Commands:** every documented target exists, and the `day7-check`
  and `day7-final-gate` orders match the Makefile.
- **Code behaviour:** the run-ID guidance, the runbook and the cleanup
  semantics match the code and the 2026-09-29 record.
- **NOTES.txt:** renders the Day 6 values for the defaults and the
  Day 7 values for a Day 7 stage; `helm template` and `helm lint` are
  unaffected.

**Dispositions:**
- **Integration, LOW - fixed:** a whitespace-trim in `NOTES.txt` lost a
  blank line. The mode variables moved to the file header, and the
  default (Day 6) NOTES output is proven byte-identical to HEAD's.
- **Integration, LOW - fixed:** the Day 7 NOTES printed only the
  gateway tag. It now prints all three pinned tags.
- **Integration, LOW - fixed:** added a template comment that Day 7
  mode is inferred from `build.requirePinnedTags`, which every Day 7
  stage sets; an unpinned Day 7 stage is refused by the chart.
- **Integration, LOW - fixed:** the README now says a new build makes
  older run IDs unusable (`require_baseline_build`).
- **Integration, LOW - fixed:** the runbook's step 3 now gives the
  manual commands with the UID, owner-chain and listener pre-checks. It
  states there is no Make target, and that the 2026-09-29 helper was a
  one-off kept with the private evidence.
- **Integration, LOW - clarified:** the README's reclaim-policy
  statement now cites the observed `local-path` PV (reclaim `Delete`)
  from the fresh-cluster plan inventory.
- **Integration, INFO - fixed:** the `make help` banner no longer says
  "Day 6" only.
- **Release, LOW - fixed:** the adjudication title no longer reads "not
  a decision"; the history note is kept.
- **Release, LOW - verified:** the untracked live record and reviews
  record are append-only relative to the 2026-09-30 10:11 private
  worktree backup (a byte-prefix comparison).

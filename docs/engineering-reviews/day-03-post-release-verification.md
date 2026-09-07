# Project 4 / Day 3 v0.3.0 Post-Release Verification

- **Repository:** `maops-kubernetes-platform`
- **Branch this document is authored on:** `main`
- **Release under verification:** `v0.3.0`
- **PR:** #3
- **Verification date:** 2026-09-07
- **Role:** Independent post-release evidence record. This document does
  not re-review code, alter the tag, alter the GitHub Release, or amend
  any prior engineering review. It records what was independently
  re-derived from git, GitHub, and the live cluster *after* the release
  was published.

---

## 1. Verification scope

This document verifies, using live git, GitHub, and cluster queries run
at authoring time (not assumed from prior documents):

- the identity and integrity of the `v0.3.0` annotated tag and the
  commit it anchors to,
- that PR #3 was merged into `main` and produced that exact commit,
- that a GitHub Release `v0.3.0` exists, is published, and its actual
  metadata,
- the pre-release candidate evidence artifact and its SHA-256,
- the merged-main authoritative revalidation of the exact release
  commit and its SHA-256,
- current live Day 3 runtime state (`maops-k8s-day3`) and the continued
  existence of `maops-k8s-day1` and `maops-k8s-day2`,
- real scaling, scheduling, rolling-update/rollback, and PDB/Eviction
  behavioral evidence,
- the security baseline and Secret handling on both released workloads,
- that the five independent Day 3 reviews and the final adjudication
  remain unmodified, and the negative history they recorded,
- the accepted/open technical debt items,
- the presence of the six supporting screenshots,
- the Claude agent/skill infrastructure count,
- the direct-`main` post-release commit model.

Evidence tiers are used below:

- **Tier A** — immutable Git/GitHub release identity.
- **Tier B** — authoritative validation (`make day3-check` output).
- **Tier C** — live runtime proof (real `kubectl`/API queries at
  authoring time).
- **Tier D** — supporting visual evidence (screenshots).

---

## 2. Release identity and immutable tag

Independently re-run at authoring time:

| Query | Result |
|---|---|
| `git rev-parse v0.3.0` (local annotated tag object) | `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` |
| `git rev-list -n 1 v0.3.0` (local peeled release commit) | `9fc7fe9f25d729d76317de85b5722e84271234f0` |
| `gh api .../git/refs/tags/v0.3.0` (remote tag ref object) | `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` |
| `gh api .../git/tags/2bdd742d...` (remote annotated tag → peeled commit) | `9fc7fe9f25d729d76317de85b5722e84271234f0` |

`[A]`

**These are two distinct objects and must not be conflated:**

- **TAG OBJECT SHA:** `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` — the
  annotated tag object itself (tagger `Raiyan Yousuf`,
  `2026-09-07T03:06:06Z`, message `Project 4 Day 3 v0.3.0 - scaling,
  rollouts and availability`), not a commit.
- **PEELED RELEASE COMMIT SHA:** `9fc7fe9f25d729d76317de85b5722e84271234f0`
  — what the tag object points to (`object` field, `type: commit`), and
  the actual reviewed and merged Day 3 tree. This matches the required
  value exactly.

The local dereferenced tag, the remote tag ref object, and the remote
peeled commit all agree. `[A]` The tag is unsigned
(`verification.verified: false, reason: unsigned`), consistent with this
project's current stage — no artifact-signing step is in scope yet.

**GitHub Release state**, from `gh release view v0.3.0`:

| Field | Value |
|---|---|
| Tag name | `v0.3.0` |
| Title | `MAOps Kubernetes Platform v0.3.0 — Day 3` |
| Is draft | `false` |
| Is prerelease | `false` |
| Published at | `2026-09-07T03:08:11Z` |
| Target commitish | `main` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.3.0 |

The Release exists, is published (not a draft, not a prerelease). `[A]`

**Immutable release model:**

```
v0.3.0
  ->
9fc7fe9f25d729d76317de85b5722e84271234f0
```

This is permanent. The operator intentionally uses a direct-`main`
post-release evidence commit for this document and the six screenshots
(Section 15) — no evidence branch or PR. That commit will advance `main`
beyond `9fc7fe9f25d729d76317de85b5722e84271234f0`; this is expected and
does not move the tag. After that commit, `main != v0.3.0`'s peeled
commit is the normal, intended state, and must not be read as tag drift.
The invariant this document re-confirms is that `git rev-list -n 1
v0.3.0` equals `9fc7fe9f25d729d76317de85b5722e84271234f0` and must
remain so forever. At authoring time, `main` still equals that same
commit, confirmed above. Nothing in this task moves or recreates the
`v0.3.0` tag.

---

## 3. PR #3 and merge evidence

Independently re-run via `gh pr view 3 --json ...` at authoring time:

| Field | Value |
|---|---|
| Number | 3 |
| Base branch | `main` |
| Head branch | `feature/day-3-scaling-rollouts-availability` |
| State | `MERGED` |
| Merged at | `2026-09-06T11:48:47Z` |
| Merge commit | `9fc7fe9f25d729d76317de85b5722e84271234f0` |
| PR feature head | `fb8d642d9e3e2df6dab6a0a40a6d78dec60fae9d` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/pull/3 |

The merge commit matches the required release commit exactly. `[A]` The
feature branch `feature/day-3-scaling-rollouts-availability` was
confirmed still present both locally (`git branch`) and on `origin`
(`git branch -a`) — retained per requirement, not deleted after merge.

Day 3 has no automated GitHub Actions CI configured — intentional per
`docs/roadmap.md` (CI is a Day 6 addition). Merged-main validation was
performed locally through the authoritative `make day3-check` Makefile
gate (Section 5), not through CI.

---

## 4. Pre-release candidate evidence

Committed repository artifact:
`docs/evidence/day-03/v0.3.0-pre-release-day3-check.log`.

Independently re-hashed at authoring time:

```
sha256sum docs/evidence/day-03/v0.3.0-pre-release-day3-check.log
f73d49165397e01c294c92d88b003ffbee405341a233f30f39fe3ceb3882b71a
```

Matches the required hash exactly. `[A]`/`[B]` This is the literal
one-shot post-remediation candidate run of `make day3-check`, ending
`DAY3_CHECK_RC=0`. This log corresponds to the pre-merge candidate tree
and is distinct from the later merged-main run described in Section 5 —
the two are not conflated here.

---

## 5. Merged-main authoritative evidence

After PR #3 merged, the exact release commit
(`9fc7fe9f25d729d76317de85b5722e84271234f0`) on `main` was validated
again with the literal `make day3-check`. This document does **not**
re-run that command; it independently inspects the already-produced
local operator evidence log.

Local operator evidence path (intentionally preserved **outside** the
Git repository):

```
/home/raiyan10/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-03/v0.3.0-merged-main-day3-check.log
```

Independently re-hashed at authoring time:

```
sha256sum /home/raiyan10/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-03/v0.3.0-merged-main-day3-check.log
8904edde5f76bc5e3e9d2a68a53deb0dad8ad4f8d754bb26039bc02aa9d2cbb0
```

Matches the required hash exactly. `[B]`/`[C]`

This raw log is **not** committed to the repository. Repository evidence
for the merged-main run consists of this post-release verification
document plus the six committed screenshots (Section 15), not the raw
log file itself.

Independently confirmed result summary from that log:

| Check | Result |
|---|---|
| Unit tests | 372 tests, `OK` |
| Version consistency checks `[A]`/`[B]` | 15/15 PASS |
| Static manifest checks `[A]`/`[B]` | 139/139 PASS |
| Context checks `[C]` | 6/6 PASS |
| Real live cluster checks `[C]` | 33/33 PASS |
| Scheduling checks `[C]` | 12/12 PASS |
| DNS / service-discovery checks `[D]` | 2/2 PASS |
| Secret checks `[C]`/`[D]` | 29/29 PASS |
| HTTP smoke checks `[C]` | 5/5 PASS |
| Dependency-failure checks `[D]` | 11/11 PASS |
| Scaling checks `[C]` | 20/20 PASS |
| Rollout/rollback checks `[C]` | 38/38 PASS |
| PDB/Eviction checks `[C]` | 16/16 PASS |
| Final-state checks `[C]` | 30/30 PASS |

Final line: `DAY3_MERGED_MAIN_RC=0`. `[B]`

Negative `[FAIL]` / `RESTORATION FAILURE` lines appearing earlier in the
unit-test portion of that log are deliberate, deterministic negative-path
`unittest` output (simulated `kubectl` errors, simulated restoration
failure, and similar injected-failure branches) — expected output of the
test suite proving its own failure-handling code paths, not failures of
the final live validation sequence. This history is preserved here, not
sanitized.

---

## 6. Runtime state

Independently queried against the live cluster at authoring time (all
read-only `kubectl get`/`version` calls; no mutation performed):

| Item | Expected | Observed |
|---|---|---|
| Cluster | `maops-k8s-day3` | present (`kind get clusters`) |
| Context | `kind-maops-k8s-day3` | present, current context |
| Kubernetes server version | v1.36.1 | `v1.36.1` |
| Nodes | 1 control-plane, 2 workers | `maops-k8s-day3-control-plane` (control-plane, Ready), `maops-k8s-day3-worker` (Ready), `maops-k8s-day3-worker2` (Ready) |
| `maops-app` Deployment | 3/3 Ready | `3/3` Ready/Up-to-date/Available |
| `maops-gateway` Deployment | 3/3 Ready | `3/3` Ready/Up-to-date/Available |
| `maops-app` Service | ClusterIP | `ClusterIP` |
| `maops-gateway` Service | ClusterIP | `ClusterIP` |
| `maops-app-pdb` | minAvailable=2 | `MIN AVAILABLE 2`, `ALLOWED DISRUPTIONS 1` |
| `maops-gateway-pdb` | minAvailable=2 | `MIN AVAILABLE 2`, `ALLOWED DISRUPTIONS 1` |
| Leaked `kubectl port-forward` processes | none | `ps aux | grep port-forward` — none found |
| `maops-k8s-day1` cluster | still exists | present (`kind get clusters`) |
| `maops-k8s-day2` cluster | still exists | present (`kind get clusters`) |

All `[C]` runtime facts above. Neither cluster was mutated by this
verification beyond read-only `kubectl get`/`version` calls.

All six `maops-app`/`maops-gateway` pods observed `Running`, `1/1`
Ready, scheduled only on `maops-k8s-day3-worker` or
`maops-k8s-day3-worker2` — none on the control-plane node, consistent
with the worker-only placement requirement (Section 7). Pod restart
counts observed at authoring time ranged 1–2 per pod with ages of
roughly 25–29 minutes since last restart against a ~15h pod age; this is
reported as observed, not assumed zero, and is consistent with prior
scaling/rollout/PDB experiment activity against this same long-lived
cluster rather than a new defect — it was not investigated further as it
falls outside this document's scope.

---

## 7. Architecture verified

Independently confirmed against the manifests and live cluster at
authoring time:

- Cluster: `kind-maops-k8s-day3`, Kubernetes `v1.36.1`, 1 control-plane
  + 2 workers.
- `maops-gateway`: 3 replicas, ClusterIP Service, worker-only placement,
  topology spread constraint.
- `maops-app`: 3 replicas, ClusterIP Service, worker-only placement,
  topology spread constraint.
- Both Deployments use `RollingUpdate` with `maxUnavailable=1`,
  `maxSurge=1`, `minReadySeconds=5`, `progressDeadlineSeconds=120`,
  `revisionHistoryLimit=5`.
- Both Deployments' topology spread constraints use
  `topologyKey=kubernetes.io/hostname`, `maxSkew=1`,
  `whenUnsatisfiable=DoNotSchedule`, `nodeAffinityPolicy=Honor`,
  `nodeTaintsPolicy=Honor`.
- PodDisruptionBudgets `maops-gateway-pdb` and `maops-app-pdb`, both
  `minAvailable=2`.
- Normal healthy PDB status for both: `currentHealthy=3`,
  `desiredHealthy=2`, `disruptionsAllowed=1` — consistent with the live
  `ALLOWED DISRUPTIONS 1` observed in Section 6.

`[A]`/`[C]`

---

## 8. Behavior verified

Recorded from the merged-main authoritative log (Section 5) and prior
engineering review evidence; this document does not re-run these
mutating experiments.

**Scaling** — `3 -> 4 -> 3` for both `maops-app` and `maops-gateway`.
Deployment, Pod, EndpointSlice, and functional Service state were all
validated across the transition. This is ordinary Deployment
`kubectl scale`, **not** HPA/autoscaling — no HorizontalPodAutoscaler
exists in this project's scope.

**Scheduling** — all application Pods remained off the control-plane
node; both workers were used per workload; topology skew stayed `<= 1`
throughout.

**Rolling update** — a temporary Pod-template annotation was applied,
producing a real new ReplicaSet revision. Pods were genuinely replaced
(not merely restarted in place). The Service was sampled during the
actual in-flight rollout via a background-thread sampler running
concurrently with the mutation, with at least one successful sample
confirmed to have occurred **after** the Pod set had diverged from its
pre-rollout baseline (exact-count, fully-disjoint UID comparison). This
document does **not** claim mathematical/continuous zero downtime — only
that a genuine in-flight successful sample was captured after
divergence.

**Rollback** — a real `kubectl rollout undo` was issued. The previous
Pod template was restored, the temporary annotation was removed, a
replacement Pod set was observed, the EndpointSlice was restored, and
the Service remained/recovered functional.

**PodDisruptionBudget / Eviction** — starting from 3/3,
`disruptionsAllowed=1`; an ordinary Deployment scale-down to 2 replicas
was allowed (PDBs do not block Deployment scaling). At 2/2,
`disruptionsAllowed=0`; a real `policy/v1` Eviction API attempt against a
remaining Pod was rejected, with the rejection carrying
disruption-budget-specific evidence in its status (`TooManyRequests` +
"would violate the pod's disruption budget" — not a bare
`TooManyRequests` alone). The workload was then restored to 3/3.

Explicitly:

- A PodDisruptionBudget does **not** block ordinary Deployment scaling
  (only the Eviction API is subject to it).
- A PodDisruptionBudget does **not** protect against all involuntary
  failures (e.g. node loss, kubelet-initiated eviction outside the
  Eviction API, or process crashes) — it only governs voluntary
  disruptions requested through the Eviction API.
- Deployment rolling-update Pod deletion during a normal rollout is
  **not** mediated by the Eviction API or by any PDB — the Deployment
  controller deletes and recreates Pods directly.

`[B]`/`[C]`

---

## 9. Security baseline

Independently confirmed on both released workloads' Pod templates:

- `runAsNonRoot: true`
- `runAsUser: 10001`
- `runAsGroup: 10001`
- `readOnlyRootFilesystem: true`
- `allowPrivilegeEscalation: false`
- `capabilities.drop: [ALL]`
- `seccompProfile.type: RuntimeDefault`
- `automountServiceAccountToken: false`
- bounded CPU/memory `resources.requests`/`limits`

Runtime Secret: `maops-internal-auth`, key `internal-token`, mounted
read-only on both workloads. Not committed to the repository as a usable
credential and not printed in any evidence log (values logged only as
byte-length). Secret behavior/non-disclosure checks: `29/29` PASS
(Section 5).

`[A]`/`[C]`/`[D]`

---

## 10. Independent review history

Five independent Day 3 engineering reviews exist in this repository and
were independently re-hashed at authoring time — all five match the
hashes recorded in the final adjudication exactly, confirming they were
not modified after adjudication:

| Review document | Verified SHA-256 |
|---|---|
| `day-03-kubernetes-architecture-review.md` | `6ce72225c4a3bfdf30a8a2ced4a1b35e71abeb6986a0f22aaa419d1d30b54e18` |
| `day-03-kubernetes-security-review.md` | `a41df422e020d1f9c036f49654951449305f83365fc13d6c254a4dd0bc51b16f` |
| `day-03-cluster-integration-review.md` | `cc75fbc8174dc0e8ddff99624074fea9c8ac0d22cc7b3fe7858c2f2ebcb6d72a` |
| `day-03-kubernetes-test-review.md` | `6e35fdfb0cb7e0247f353e63f8fed0b82de2700d4466fbe47a687fd933cdf9b8` |
| `day-03-release-readiness-review.md` | `fce44f88036af96059624eeb3daf76464cfc9ebd9adb6955b89e85402bd98cf6` |

`[A]` These are immutable, point-in-time records. This verification does
not alter, re-litigate, or rewrite any of them.

The final adjudication,
`docs/engineering-reviews/day-03-v0.3-release-readiness.md`, independently
rechecked the remediation and its recorded conclusion is:

- Critical unresolved: **0**
- High unresolved: **0**
- Medium unresolved: **0**
- **Final verdict: GO FOR PR**

**Preserved remediation-time negative history:** during remediation of
the in-flight rollout-sampling finding (`DAY3-ARCH-M1`/`DAY3-INT-M1`), a
background-thread signal-handling defect in `scripts/portforward.py` was
discovered — `_convert_sigterm_to_exception()` had been installed from a
background thread, which is unsafe (Python signal handlers may only be
installed on the main thread). In practice this caused a process leak on
every in-flight sample and a first focused rollout-check validation
result of `34/36` with 61 leaked processes recorded. The fix made
`_convert_sigterm_to_exception()` a no-op off the main thread, added
regression coverage (`tests/test_portforward_signal.py::
BackgroundThreadSafetyTests`), and was followed by a passing focused
revalidation (`38/38`) before the defect closure was reflected in the
authoritative `make day3-check` rollout/rollback result (`38/38`,
Section 5) and the final-state leaked-process check (0 leaked
processes). This document does not rewrite that history as though the
first attempt had passed.

---

## 11. Accepted / open technical debt

Day 3 does **not** carry zero technical debt:

**DAY2-INT-I1 — ACCEPTED / OPEN FOR FUTURE DUAL-STACK SUPPORT**

The EndpointSlice helper remains intentionally scoped to the current
single-stack environment. Not closed by Day 3; carried forward.

**DAY1-INT-I2 — ACCEPTED / OPEN FOR FUTURE BASE-IMAGE CHANGE**

The pinned Distroless image still uses the known
`/usr/bin/python3.11` interpreter path relied on by
`scripts/cluster_check.py`. Not closed by Day 3.

**DAY1-REL-I1 — CLOSED**

Closed by the Day 2 VERSION/image/version-label consistency guard;
remains closed.

**DAY3-SEC-I1 — ACCEPTED / OPEN INFORMATIONAL**

Deliberate scope decision: no live post-scaling/rollout securityContext
re-check was added, since the Deployment Pod template remains the single
source of truth for every Pod it creates. Non-blocking.

**DAY3-TEST-L2 — ACCEPTED / OPEN LOW**

No broad reporting refactor was made or required for Day 3; reported
`N/N` counts may legitimately mix independently-derived assertions with
bounded-wait-predicate observations. Not a statistical coverage metric.

**DAY3-REL-L1 — ACCEPTED / OPEN LOW**

No central issue/debt ledger exists yet. Inherited process debt; may be
reconsidered at Day 7.

This document does not claim zero technical debt for Day 3.

---

## 12. Screenshot evidence

Independently confirmed at authoring time: exactly six files exist,
non-empty, in `docs/images/day-03/` — no more, no fewer.

| File | What it proves | Evidence tier |
|---|---|---|
| `01-pr3-merged.png` | PR #3 merged into `main`. | Tier A |
| `02-v030-github-release.png` | The published GitHub Release `v0.3.0` (not draft, not prerelease). | Tier A |
| `03-v030-tag-integrity.png` | Annotated tag object vs. peeled release-commit integrity for `v0.3.0`. | Tier A |
| `04-v030-merged-main-day3-check-pass.png` | Authoritative merged-main validation summary (372 unit tests; 15/15, 139/139, 6/6, 33/33, 12/12, 2/2, 29/29, 5/5, 11/11, 20/20, 38/38, 16/16, 30/30 section counts; `DAY3_MERGED_MAIN_RC=0`). | Tier B |
| `05-v030-three-node-scheduling-pdb.png` | Live three-node scheduling (worker-only placement, topology spread) and PDB status. | Tier C |
| `06-v030-rollout-pdb-proof.png` | Live rolling-update/rollback and PDB/Eviction rejection proof. | Tier C/D |

This verification confirmed file existence, non-empty size, and exact
count only; it does not claim to have visually inspected pixel content
beyond what is described above, and does not present the screenshots as
stronger evidence than the underlying logs and live-cluster queries they
capture.

---

## 13. Claude infrastructure

Independently confirmed by directory listing at authoring time:

- **5 agents**: `cluster-integration-engineer.md`,
  `kubernetes-architect.md`, `kubernetes-security-reviewer.md`,
  `kubernetes-test-engineer.md`, `release-engineer.md`
  (`.claude/agents/`).
- **4 skills**: `kind-cluster-validation`, `manifest-validation`,
  `release-readiness`, `workload-security-validation`
  (`.claude/skills/`).

No agent or skill was added or modified by this verification.

---

## 14. Day 3 scope and next stage

Day 3 includes: scaling, worker-only scheduling with topology spread,
rolling update and rollback, and PodDisruptionBudget/Eviction behavior.

Still intentionally deferred — none of the following are implemented in
Day 3:

- **Day 4:** StatefulSet, PVC, persistence/recovery.
- **Day 5:** ServiceAccount, RBAC, NetworkPolicy.
- **Day 6:** Helm, GitHub Actions CI, Ingress, Gateway API.
- **Day 7:** Service Mesh, Recreate/Blue-Green/Canary strategies, final
  production-readiness hardening, v1.0.0.

**Project 4 as a whole is not complete.**

---

## 15. Post-release commit model

The operator intentionally uses a **direct-`main`** post-release
evidence commit for this document and the six screenshots in Section 12.
No evidence branch or PR is created for this. After that commit, `main`
will no longer equal the `v0.3.0` peeled release commit — this is
expected and is not tag drift. The immutable invariant, re-confirmed in
Section 2, remains:

```
v0.3.0 peeled release commit == 9fc7fe9f25d729d76317de85b5722e84271234f0
```

The post-release evidence commit must never become the `v0.3.0` tag
target, and nothing in this document or its follow-up commit does so.

---

## 16. Current disposition

Based on the independent verification performed in this document,
Project 4 / Day 3 / **v0.3.0** is:

- reviewed (five independent reviews + final adjudication, Section 10),
- merged (PR #3, Section 3),
- merged-main validated (`make day3-check` `DAY3_MERGED_MAIN_RC=0` at
  the release commit, Section 5),
- tagged (`v0.3.0` → `9fc7fe9f25d729d76317de85b5722e84271234f0`,
  Section 2),
- published (GitHub Release `v0.3.0`, Section 2),
- runtime-verified live (Sections 6–9),
- post-release verified (this document).

**Project 4 Day 3 / v0.3.0 release is verified.**

It may be considered frozen after this document and the six screenshots
in Section 12 are committed directly to `main` (Section 15). That future
evidence commit will advance `main` past
`9fc7fe9f25d729d76317de85b5722e84271234f0`; it does not and cannot change
the immutable `v0.3.0` release commit identified in Section 2.

This disposition applies only to Project 4 / Day 3 / v0.3.0. **Day 4
remains next.**

---

PROJECT 4 DAY 3 v0.3.0 POST-RELEASE VERIFICATION COMPLETE

# Project 4 / Day 1 v0.1.0 Post-Release Verification

- **Repository:** `maops-kubernetes-platform`
- **Branch this document is authored on:** `main`
- **Release under verification:** `v0.1.0`
- **Verification date:** 2026-09-01
- **Role:** Independent post-release evidence record. This document does
  not re-review code, alter the tag, alter the GitHub Release, or amend
  any prior engineering review. It records what was independently
  re-derived from git and GitHub *after* the release was published.

---

## 1. Verification scope

This document verifies, using live git and GitHub queries run at
authoring time (not assumed from prior documents):

- the identity and integrity of the `v0.1.0` tag and the commit it
  anchors to,
- that PR #1 was merged into `main` and produced that exact commit,
- that a GitHub Release `v0.1.0` exists, is published, and its actual
  metadata,
- the previously-recorded merged-main validation evidence
  (`make day1-check`) for the release commit,
- the previously-recorded live-cluster and controller-reconciliation
  evidence,
- the previously-recorded SIGTERM/port-forward regression closure,
- that the five independent Day 1 reviews and the final adjudication
  remain unmodified,
- the two accepted open technical-debt items,
- the presence and size of the six supporting screenshots.

It does not perform new cluster runs, new tests, or a new code review.
Evidence tiers `[A]`–`[D]` (source/static, deterministic test,
Kubernetes runtime state, real live behavioral proof) are used below
where they clarify what kind of evidence is being cited.

---

## 2. Release identity and immutable tag

Independently re-run at authoring time:

| Query | Result |
|---|---|
| `git rev-parse v0.1.0` | `e3a8749025a8c3158cfc5af8b07bc3285225bc80` |
| `git rev-list -n 1 v0.1.0` | `91d37aa82e6b12ea0a060752c3474720f6ed56d4` |
| `gh api .../git/refs/tags/v0.1.0` (remote tag ref object) | `e3a8749025a8c3158cfc5af8b07bc3285225bc80` |
| `gh api .../git/tags/e3a8749...` (remote annotated tag → peeled commit) | `91d37aa82e6b12ea0a060752c3474720f6ed56d4` |

(Note: `git ls-remote origin refs/tags/v0.1.0` and the `^{}` peeled
form could not be run directly — the local remote is configured over
SSH and this environment has no SSH key registered, so it failed with
`Permission denied (publickey)`. The equivalent remote data was
obtained instead via the GitHub API, `gh api
repos/raiyan10/maops-kubernetes-platform/git/refs/tags/v0.1.0` and
`git/tags/<sha>`, which reads the same underlying Git object store on
GitHub's side.)

**These are two distinct objects and must not be conflated:**

- **ANNOTATED TAG OBJECT SHA:** `e3a8749025a8c3158cfc5af8b07bc3285225bc80`
  — this is the tag object itself (tagger, date, message), not a
  commit.
- **RELEASE COMMIT SHA:** `91d37aa82e6b12ea0a060752c3474720f6ed56d4`
  — this is what the tag object points to, and is the actual reviewed
  and merged Day 1 tree.

The local dereferenced tag, the remote tag ref object, and the remote
peeled commit all agree, and the release commit matches the expected
value `91d37aa82e6b12ea0a060752c3474720f6ed56d4` exactly. `[A]`

The remote tag object also carries an unsigned tagger record
(`Raiyan Yousuf`, `2026-09-01T09:36:02Z`) — this is recorded for
completeness; the tag is unsigned (`verified: false, reason: unsigned`),
which is consistent with Day 1 scope (no artifact-signing stage yet).

Current `main` HEAD (`git log -1`) is also
`91d37aa82e6b12ea0a060752c3474720f6ed56d4` at the time this document
was authored, i.e. the tag still anchors to the tip of `main` as it
existed before this evidence document. `[A]`

---

## 3. PR #1 and merge evidence

Independently re-run via `gh pr view 1 --json ...` at authoring time:

| Field | Value |
|---|---|
| Number | 1 |
| Title | feat: establish MAOps Kubernetes Platform Day 1 |
| Base branch | `main` |
| Head branch | `feature/day-1-kubernetes-foundation` |
| State | `MERGED` |
| Merged at | 2026-09-01T09:23:01Z |
| Merge commit | `91d37aa82e6b12ea0a060752c3474720f6ed56d4` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/pull/1 |

The merge commit matches the expected release commit exactly. `[A]`

**CI note:** PR #1 has no automated GitHub Actions checks configured.
This is intentional for Day 1 — GitHub Actions CI is a later-stage
addition per `docs/roadmap.md` and is deliberately out of scope here.
Merged-main validation for this PR was instead performed locally
through the authoritative `make day1-check` Makefile gate (Section 5),
not through CI.

---

## 4. GitHub Release verification

Independently re-run via `gh release view v0.1.0 --json ...` at
authoring time:

| Field | Value |
|---|---|
| Tag name | `v0.1.0` |
| Release name | MAOps Kubernetes Platform v0.1.0 |
| Published at | 2026-09-01T09:39:59Z |
| Is draft | `false` |
| Is prerelease | `false` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.1.0 |
| Assets | none (empty list) |

The Release exists, is published (not a draft, not a prerelease), and
points at tag `v0.1.0`. `[A]`

The asset list is empty. This is acceptable for Day 1: there is no
SBOM, no Trivy scan output, no cosign signature, and no published
registry image attached to this release, and none of that is implied
here. Those are later-stage release artifacts per the roadmap, not a
Day 1 deliverable.

---

## 5. Merged-main validation evidence

The merged-main tree at commit `91d37aa82e6b12ea0a060752c3474720f6ed56d4`
was validated prior to tag publication via the authoritative Makefile
gate:

```
make day1-check
PASS
```

Recorded results:

| Check | Result |
|---|---|
| Unit tests `[B]` | 101/101 PASS |
| Static Kubernetes manifest checks `[A]`/`[B]` | 38/38 PASS |
| Real Kubernetes cluster checks `[C]` | 15/15 PASS |
| HTTP smoke checks `[C]` | 4/4 PASS |
| Controller reconciliation `[D]` | 8/8 PASS |

Final line: `PASS: day1-check completed the full authoritative
validation sequence`

Cluster context:

- Cluster: `maops-k8s-day1`
- kubeconfig context: `kind-maops-k8s-day1`
- Kubernetes server version: v1.36.1
- Deployment: `maops-app`, 2/2 Ready
- Service: `maops-app`, ClusterIP
- Port-forward leak after validation: none

This validation was run locally against the merged-main commit
identified above. **GitHub Actions did not perform this validation** —
Day 1 has no CI wired up by design; the Makefile is the authoritative
local validation interface for this stage, per
`.claude/CLAUDE.md`.

---

## 6. Kubernetes runtime evidence

Consistent with Section 5's cluster checks, the live cluster showed the
Deployment at 2/2 Ready with the Service reachable as ClusterIP,
against Kubernetes server v1.36.1 on cluster `maops-k8s-day1`. `[C]`
This is real API/runtime-state evidence, not a re-read of the static
manifest — it reflects what the kube-apiserver actually reported for
the running workload.

---

## 7. Controller reconciliation evidence

The recorded proof of real Deployment/ReplicaSet controller behavior:

- Exactly two `maops-app` pod UIDs were recorded before deletion.
- Exactly one `maops-app` pod was deleted.
- No replacement pod was manually created.
- The Kubernetes Deployment/ReplicaSet controller restored the
  workload to 2/2 Ready on its own.
- One original pod UID survived unchanged throughout.
- One genuinely new pod UID appeared, created by the controller.
- HTTP checks against the Service succeeded after reconciliation
  completed.

This is classified as strong live behavioral evidence `[D]` — it
demonstrates actual controller reconciliation against the live
Kubernetes API, not an inference drawn from static manifests. Specific
pod names/UIDs are not restated here beyond what is described above;
none beyond this description were independently available to this
verification pass, so none are invented.

---

## 8. SIGTERM regression closure

Original independent review finding: **DAY1-INT-M1**.

**Problem:** a `SIGTERM` sent directly to the Python wrapper process
could terminate it without unwinding its `with` context manager,
leaving the `kubectl port-forward` child process running after the
script exited.

**Post-remediation proof, using the same failure trigger:**

- The wrapper entered `port_forward()`.
- The `kubectl` child process was confirmed alive.
- `SIGTERM` was delivered directly to the Python wrapper process.
- The signal was converted into a catchable exception inside the
  wrapper.
- The `finally` cleanup block executed.
- The `kubectl` child process was confirmed gone.
- Zero matching `kubectl port-forward` processes remained afterward.

This is real behavioral closure evidence `[D]` for DAY1-INT-M1 — the
same fault trigger that originally exposed the leak was re-applied
post-fix and shown to no longer leak. This is not merely "a normal
smoke test had no leaked process"; it is a targeted repeat of the
original failure trigger against the fix.

---

## 9. Engineering review and adjudication status

Five independent Day 1 engineering reviews exist in this repository
and were confirmed present and untouched by this verification pass:

- `docs/engineering-reviews/day-01-kubernetes-architecture-review.md`
- `docs/engineering-reviews/day-01-kubernetes-security-review.md`
- `docs/engineering-reviews/day-01-cluster-integration-review.md`
- `docs/engineering-reviews/day-01-kubernetes-test-review.md`
- `docs/engineering-reviews/day-01-release-readiness-review.md`

These five documents are immutable, point-in-time records. This
verification does not alter, re-litigate, or rewrite any of them; their
original findings and severities stand as originally written.

The final adjudication document,
`docs/engineering-reviews/day-01-v0.1-release-readiness.md`, was also
confirmed present and untouched. Its recorded final status, re-checked
directly from the file at authoring time:

- Critical unresolved: 0
- High unresolved: 0
- Medium unresolved: 0
- **Verdict: GO FOR PR**

This document does not change or re-derive that verdict — it confirms
the verdict is still recorded as-written in the adjudication file.

---

## 10. Accepted technical debt

Day 1 does **not** carry zero technical debt. Two informational items
remain open and accepted, and neither invalidates `v0.1.0`:

**DAY1-INT-I2 — ACCEPTED / OPEN FOR FUTURE BASE-IMAGE CHANGE**

`scripts/cluster_check.py` currently depends on the hardcoded
`/usr/bin/python3.11` interpreter path inside the workload container.
That path matches the current digest-pinned Distroless image and was
live-verified against it, but it remains coupled to that image's Python
minor version. It should be reconsidered when the base image changes.

**DAY1-REL-I1 — ACCEPTED / OPEN FOR FUTURE VERSION-CONSISTENCY GUARD**

`VERSION`, the Makefile image tag, and the Kubernetes
`app.kubernetes.io/version` labels currently agree at `0.1.0`, but no
automated consistency guard enforces that agreement yet. This should be
addressed before or alongside the `v0.2.0` version bump.

---

## 11. Screenshot evidence index

All six required screenshots were independently confirmed to exist on
disk at authoring time (`docs/images/day-01/`), with sizes as measured:

| File | Size | Evidence it preserves |
|---|---|---|
| `01-pr1-merged.png` | 65,637 bytes | PR #1 merged into `main`. |
| `02-v010-github-release.png` | 45,871 bytes | The published GitHub Release `v0.1.0`. |
| `03-v010-tag-integrity.png` | 37,056 bytes | Annotated tag object and peeled release-commit integrity for `v0.1.0`. |
| `04-v010-day1-check-pass.png` | 36,937 bytes | Authoritative merged-main validation summary: 101 unit tests, 38 manifest checks, 15 live cluster checks, 4 smoke checks, 8 reconciliation checks, final `day1-check` PASS. |
| `05-v010-cluster-2of2-ready.png` | 39,083 bytes | Real live Kubernetes runtime: Deployment 2/2, Pods Running/Ready, ClusterIP Service. |
| `06-v010-controller-reconciliation.png` | 101,796 bytes | Real pod deletion/replacement proof: original UIDs, one pod deletion, new UID, surviving UID, restored 2/2, post-reconciliation HTTP success. |

This verification confirmed file existence and size only. It does not
claim to have visually inspected pixel content beyond what is described
in the "Evidence it preserves" column above, and does not attribute to
any screenshot content not implied by its stated purpose.

---

## 12. Post-release immutability model

The intended immutable release model:

```
v0.1.0
    |
    v
91d37aa82e6b12ea0a060752c3474720f6ed56d4
reviewed + merged + merged-main validated release code
```

Once this post-release document and its screenshots are committed to
`main` (a future action, not performed by this task), the history
becomes:

```
main
    |
    v
<future post-release evidence commit>
```

That future evidence commit is expected to be **newer** than the
`v0.1.0` tag. This is intentional — the tag freezes the reviewed and
validated *release code*, while the evidence document is by nature
written and committed after the release it documents.

The `v0.1.0` tag **must remain permanently anchored** to
`91d37aa82e6b12ea0a060752c3474720f6ed56d4`. This document does not
propose, and none of its future git actions should ever, move the
`v0.1.0` tag to the evidence commit or to any commit after
`91d37aa82e6b12ea0a060752c3474720f6ed56d4`.

---

## 13. Final Day 1 disposition

Based on the independent verification performed in this document,
Project 4 / Day 1 / **v0.1.0** is:

- reviewed (five independent reviews + final adjudication, Section 9),
- merged (PR #1, Section 3),
- merged-main validated (`make day1-check` PASS at the release commit,
  Section 5),
- tagged (`v0.1.0` → `91d37aa82e6b12ea0a060752c3474720f6ed56d4`,
  Section 2),
- published (GitHub Release `v0.1.0`, Section 4),
- post-release verified (this document).

Day 1 may be considered **frozen** only once this document and its six
screenshots are committed to `main` and a final git/tag integrity check
(re-confirming Section 2's values against the committed state) passes.
As of authoring this document, that commit has not yet happened — this
task deliberately does not commit or push.

This disposition applies **only** to Project 4 / Day 1 / v0.1.0. It is
not a claim that Project 4 as a whole is complete. **Day 2 / v0.2.0
remains next.**

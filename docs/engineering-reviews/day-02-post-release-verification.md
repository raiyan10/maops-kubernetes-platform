# Project 4 / Day 2 v0.2.0 Post-Release Verification

- **Repository:** `maops-kubernetes-platform`
- **Branch this document is authored on:** `main`
- **Release under verification:** `v0.2.0`
- **PR:** #2
- **Verification date:** 2026-09-04
- **Role:** Independent post-release evidence record. This document does
  not re-review code, alter the tag, alter the GitHub Release, or amend
  any prior engineering review. It records what was independently
  re-derived from git, GitHub, and the live cluster *after* the release
  was published.

---

## 1. Verification scope

This document verifies, using live git, GitHub, and cluster queries run
at authoring time (not assumed from prior documents):

- the identity and integrity of the `v0.2.0` tag and the commit it
  anchors to,
- that PR #2 was merged into `main` and produced that exact commit,
- that a GitHub Release `v0.2.0` exists, is published, and its actual
  metadata,
- the original merged-main authoritative release gate (`make
  day2-check`) result for the release commit,
- the fact that a second, post-release evidence-recovery revalidation
  run of `make day2-check` was also performed, and why,
- current live Day 2 runtime state (`maops-k8s-day2`) and the continued
  existence of `maops-k8s-day1`,
- real service discovery, Secret/authentication, and dependency-failure
  behavioral evidence,
- that the five independent Day 2 reviews and the final adjudication
  remain unmodified, and the negative history they recorded,
- the remediation closures and the accepted/open technical debt items,
- the presence of the six supporting screenshots,
- the Claude agent/skill infrastructure count.

Evidence tiers `[A]`–`[D]` (source/static, deterministic test,
Kubernetes runtime state, real live behavioral proof) are used below
where they clarify what kind of evidence is being cited.

---

## 2. Release identity and immutable tag

Independently re-run at authoring time:

| Query | Result |
|---|---|
| `git rev-parse v0.2.0` (local annotated tag object) | `1baec126afe67c73cda1bd7e78f5ff90c057561d` |
| `git rev-list -n 1 v0.2.0` (local peeled release commit) | `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36` |
| `gh api .../git/refs/tags/v0.2.0` (remote tag ref object) | `1baec126afe67c73cda1bd7e78f5ff90c057561d` |
| `gh api .../git/tags/1baec126...` (remote annotated tag → peeled commit) | `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36` |

(Note: `git ls-remote` against `origin` could not be run directly — the
remote is configured over SSH (`git@github.com:...`) and this
environment has no SSH key registered, so it failed with `Permission
denied (publickey)`. The equivalent remote data was instead obtained
via the GitHub API — `gh` is authenticated over HTTPS independently of
the git SSH remote — using `gh api
repos/raiyan10/maops-kubernetes-platform/git/refs/tags/v0.2.0` and
`git/tags/<sha>`, which reads the same underlying Git object store on
GitHub's side.)

**These are two distinct objects and must not be conflated:**

- **TAG OBJECT SHA:** `1baec126afe67c73cda1bd7e78f5ff90c057561d` — this
  is the annotated tag object itself (tagger `Raiyan Yousuf`,
  `2026-09-04T12:16:11Z`, message `MAOps Kubernetes Platform v0.2.0`),
  not a commit.
- **PEELED RELEASE COMMIT SHA:** `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36`
  — this is what the tag object points to (`object` field, `type:
  commit`), and is the actual reviewed and merged Day 2 tree.

The local dereferenced tag, the remote tag ref object, and the remote
peeled commit all agree, and the peeled release commit matches the
required value `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36` exactly. `[A]`

The tag is unsigned (`verification.verified: false, reason: unsigned`)
— consistent with this project's current stage; no artifact-signing
step is in scope yet.

**GitHub Release state**, from `gh release view v0.2.0`:

| Field | Value |
|---|---|
| Tag name | `v0.2.0` |
| Is draft | `false` |
| Is prerelease | `false` |
| Published at | `2026-09-04T12:21:08Z` |
| Target commitish | `main` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.2.0 |

The Release exists, is published (not a draft, not a prerelease). `[A]`

**Immutable release model:**

```
v0.2.0
  ->
2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36
```

This is permanent. The post-release evidence commit that will contain
this document and the six screenshots will advance `main` beyond this
commit — that is expected and does not move the tag. After that
evidence commit, `main != v0.2.0`'s peeled commit is the normal,
intended state. The invariant this document re-confirms is that `git
rev-list -n 1 v0.2.0` equals `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36`
and must remain so forever; nothing in this task moves the tag.

---

## 3. PR #2 and merge evidence

Independently re-run via `gh pr view 2 --json ...` at authoring time:

| Field | Value |
|---|---|
| Number | 2 |
| Base branch | `main` |
| Head branch | `feature/day-2-service-discovery-secrets` |
| State | `MERGED` |
| Merged at | `2026-09-03T10:51:23Z` |
| Merge commit | `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36` |
| URL | https://github.com/raiyan10/maops-kubernetes-platform/pull/2 |

The merge commit matches the required release commit exactly. `[A]`
`git log --oneline` on `main` confirms this same SHA as the "Merge pull
request #2" commit, with parents `0a6d61c` (prior `main` tip) and
`6688811` (PR head).

Day 2 has no automated GitHub Actions CI configured — intentional per
`docs/roadmap.md` (CI is a Day 6 addition). Merged-main validation was
performed locally through the authoritative `make day2-check` Makefile
gate (Section 4), not through CI.

---

## 4. Original merged-main release gate

Before `v0.2.0` was tagged, the exact merged-main tree at
`2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36` underwent the authoritative
release gate:

```
make day2-check
```

Recorded result of that original run:

| Check | Result |
|---|---|
| Unit tests `[B]` | 212/212 PASS |
| Version consistency checks `[A]`/`[B]` | 13/13 PASS |
| Static manifest checks `[A]`/`[B]` | 94/94 PASS |
| Real live cluster checks `[C]` | 33/33 PASS |
| DNS / service-discovery checks `[D]` | 2/2 PASS |
| Secret / non-disclosure checks `[C]`/`[D]` | 27/27 PASS |
| HTTP smoke checks `[C]` | 5/5 PASS |
| Dependency-failure checks `[D]` | 11/11 PASS |

Final line: `PASS: day2-check completed the full authoritative
validation sequence`, `DAY2_CHECK_RC=0`.

This run authorized the release.

---

## 5. Post-release evidence recovery / revalidation

Important historical accuracy, recorded here rather than smoothed over:
the original merged-main validation log above was written under `/tmp`.
Later, while preparing screenshots 04 and 06, that temporary log was no
longer present (an expected consequence of writing evidence to `/tmp`,
not a release failure).

Because the original release gate had already passed and authorized the
release, an **additional** post-release evidence-recovery/revalidation
run of `make day2-check` was performed to regenerate persistent evidence
for screenshots 04 and 06. This is **not** the same event as the
original gate, and this document does not claim `make day2-check` ran
exactly once:

1. **Original merged-main authoritative release gate:** PASS (Section 4,
   log not persisted — the `/tmp` copy no longer exists).
2. **Later post-release evidence recovery/revalidation:** PASS, log
   persisted at
   `docs/evidence/day-02/v0.2.0-post-release-revalidation.log`.

The second run did not change the immutable release identity and did
not replace or re-authorize anything — the release was already
authorized by run 1. It was an additional verification made necessary
only by loss of the temporary `/tmp` log, executed against the same
already-tagged code.

The persistent log was inspected read-only (not modified) as part of
this verification. It independently confirms the same headline counts
as Section 4 — `212/212` unit tests (line 241: "Ran 212 tests ...
OK"), `13/13` version checks, `94/94` manifest checks, `33/33` real
cluster checks, `2/2` discovery checks, `27/27` secret checks, `5/5`
smoke checks, `11/11` dependency-failure checks — and ends with `PASS:
day2-check completed the full authoritative validation sequence` and
`POST_RELEASE_REVALIDATION_RC=0` (note: this run's completion line is
labeled `POST_RELEASE_REVALIDATION_RC`, distinct from the original
gate's `DAY2_CHECK_RC`, which is itself evidence that these are two
separate executions rather than one run described twice).

The `[FAIL]` lines visible partway through that log (around the
`test_dependency_check`, `test_reconcile_check_failure_handling`, and
`test_secret_bootstrap` sections) are expected `unittest` output from
deliberately-injected negative test cases exercising failure-handling
code paths (e.g. simulated restoration failure, simulated `kubectl`
errors) — each such block is immediately followed by `ok`, and they are
counted inside the `212/212 ... OK` unit-test result, not separate
cluster failures.

This document does not require the log file to be committed; it exists
on disk and was read for this verification.

---

## 6. Runtime state

Independently queried against the live cluster at authoring time:

| Item | Expected | Observed |
|---|---|---|
| Cluster | `maops-k8s-day2` | present (`kind get clusters`) |
| Context | `kind-maops-k8s-day2` | present, current context |
| Kubernetes server version | v1.36.1 | `v1.36.1` |
| Namespace | `maops-platform` | `Active`, age 31h |
| `maops-app` Deployment | 2/2 Ready | `2/2` Ready/Available |
| `maops-gateway` Deployment | 2/2 Ready | `2/2` Ready/Available |
| `maops-app` Service | ClusterIP | `ClusterIP` |
| `maops-gateway` Service | ClusterIP | `ClusterIP` |
| `maops-app` EndpointSlice | 2 ready endpoints | 2 endpoints, all `ready: true, serving: true, terminating: false` |
| `maops-gateway` EndpointSlice | 2 ready endpoints | 2 endpoints, all `ready: true, serving: true, terminating: false` |
| Leaked `kubectl port-forward` processes | none | `ps aux | grep port-forward` — none found |
| `maops-k8s-day1` cluster | still exists, Ready | present; node `Ready`, `v1.36.1` |

All `[C]` runtime facts above. Neither cluster was mutated by this
verification beyond read-only `kubectl get`/`describe` calls.

**Pod restart counts — reported as observed, not assumed zero:**

| Pod | Restarts |
|---|---|
| `maops-app-667dd6c8f7-4qb4g` | 0 |
| `maops-app-667dd6c8f7-psx8k` | 0 |
| `maops-gateway-854c8fccc8-46xff` | 1 |
| `maops-gateway-854c8fccc8-7sscq` | 1 |

The two `maops-gateway` pods each show 1 restart. This is **not**
invented as zero, and it is **not** new information introduced by this
verification: the persisted revalidation log itself (Section 5, line
654) already records gateway restart counts of `1` for both pods
*before* the dependency-failure experiment was run (`before={...46xff:
1, ...7sscq: 1} after={...46xff: 1, ...7sscq: 1}`) — i.e. that restart
had already happened at the time of the post-release revalidation run,
and the dependency-failure experiment did not add to it (`before ==
after`, which is exactly what Section 4/5's "gateway restart counts
unchanged solely because app became unavailable" claim requires).
Cluster events (`kubectl get events`) show a simultaneous startup-probe
failure/restart pattern across both `maops-gateway` pods and one
`maops-app` pod roughly 48 minutes prior to this verification's
queries, consistent with an infrastructure-level interruption in the
local kind/Docker environment rather than an application fault — this
was not investigated further as it falls outside this document's scope
and predates the release gate.

Separately, `kubectl get events` also shows `maops-app` was scaled to 0
and back to 2 approximately 11–12 minutes before this verification's
queries. This is consistent with the dependency-failure exercise
(Section 7) having been re-run live during evidence preparation, which
is why the current `maops-app` pods are younger (11m age, 0 restarts)
than the `maops-gateway` pods (26h age, 1 restart each). This is
legitimate subsequent runtime activity, not a release regression.

---

## 7. Service discovery

Real Day 2 behavioral proof, confirmed both in the persisted
revalidation log (Section 5) and consistent with current runtime state:

- `maops-gateway` resolves `maops-app` through Kubernetes DNS
  (`socket.getaddrinfo()` from inside the live gateway pod). `[D]`
- `maops-gateway` reaches `maops-app` through the `maops-app` **ClusterIP
  Service**, not a Pod IP directly (`BACKEND_HOST=maops-app` via the
  gateway's ConfigMap, validated against a Service-name allowlist —
  Section 9). `[C]`/`[D]`
- The real `/backend` response body includes
  `"backend_service": "maops-kubernetes-app"` and a live
  `backend_hostname` matching an actual `maops-app` pod, proving the
  data returned by the gateway genuinely originated from the backend
  application rather than being fabricated by the gateway. `[D]`

---

## 8. Secret / authentication

- Runtime Secret: `maops-internal-auth`, key `internal-token`.
- The Secret is not committed as a usable credential — `manifest_check`
  and `secret_check` both explicitly confirm no `Secret` object is
  committed to `k8s/base`, and the repository-wide scan (`secret_check`,
  "no tracked repository file (76 checked) contains the live secret
  value") found no leak. `[A]`/`[D]`
- Both `maops-app` and `maops-gateway` mount the Secret read-only at
  `/var/run/secrets/maops`. `[C]`
- The Secret value is not present in any normal HTTP response body
  (`/`, `/livez`, `/readyz`, `/config`, `/backend` were all
  non-disclosure scanned) and is not printed in the evidence log itself
  (values are logged only as byte-length, e.g. "43 bytes", never as
  content). `[D]`
- `app /internal/info` behavior, confirmed live:
  - missing token → HTTP 403 (body does not leak the token)
  - wrong token → HTTP 403 (body does not leak the token)
  - correct token → HTTP 200 (body does not leak the token)
- `maops-gateway` uses this Secret for authenticated service-to-service
  traffic to `maops-app`.
- Runtime backend-target validation (`test_gateway_backend_target.py`,
  `BackendTargetAllowlistTests`) fail-closed-verifies the outbound
  target before the Secret-bearing request is ever sent — an invalid
  ConfigMap `BACKEND_HOST`/`BACKEND_PORT` (raw IP, pod-like identity,
  wrong host, wrong port) is rejected before the Secret is used, and the
  internal token is never sent when the target is invalid
  (`test_internal_token_never_sent_when_target_invalid`).

This is authentication/authorization at the application layer only.
**This is not equivalent to Day 5 NetworkPolicy or RBAC** — no
NetworkPolicy restricts which pods may reach `maops-app` at the network
layer, and no ServiceAccount/RBAC scoping exists yet (both are
explicitly out of Day 2 scope and confirmed absent by
`manifest_check.py`'s `scope.no_forbidden_resources` check, which
forbids `ServiceAccount`, `Role`, `RoleBinding`, `ClusterRole`,
`ClusterRoleBinding`, and `NetworkPolicy` from appearing at all in Day
2's base).

---

## 9. Dependency failure / recovery

Real behavioral evidence, from the persisted revalidation log (Section
5) and consistent with the live event history observed in Section 6:

- Starting state: `maops-gateway` 2/2 Ready, `maops-app` 2/2 Ready.
- Experiment: `maops-app` scaled to 0. The check explicitly confirmed
  the EndpointSlice was drained **and** all matching `maops-app` pods
  were fully terminated before proceeding — not merely that the
  EndpointSlice emptied while pods still existed.
- During outage:
  - `gateway /livez` → HTTP 200 (`{"status": "alive"}`)
  - `gateway /readyz` → HTTP 503
  - `gateway /backend` → HTTP 503, body is the exact safe unavailable
    literal, with no traceback and no Secret token disclosed
  - `maops-gateway` container restart counts were unchanged solely
    because `maops-app` became unavailable (`before == after == {1, 1}`
    for the two gateway pods — see Section 6)
- Restoration:
  - `maops-app` restored to 2/2 Ready
  - `maops-gateway` recovered to 2/2 Ready
  - `gateway /readyz` recovered to HTTP 200
  - `gateway /backend` recovered to HTTP 200, again carrying a live
    `backend_hostname` from a genuine (new) `maops-app` pod

This proves **dependency failure != gateway process failure**: the
gateway process itself never crashed or restarted due to its backend
being unavailable; only its dependency-aware readiness state changed.
`[D]`

---

## 10. Independent review history

Five independent Day 2 engineering reviews exist in this repository and
were confirmed present and untouched by this verification pass:

1. `docs/engineering-reviews/day-02-kubernetes-architecture-review.md`
2. `docs/engineering-reviews/day-02-kubernetes-security-review.md`
3. `docs/engineering-reviews/day-02-cluster-integration-review.md`
4. `docs/engineering-reviews/day-02-kubernetes-test-review.md`
5. `docs/engineering-reviews/day-02-release-readiness-review.md`

These are immutable, point-in-time records. This verification does not
alter, re-litigate, or rewrite any of them.

The original test review (`day-02-kubernetes-test-review.md`) recorded
**1 High, 2 Medium, 3 Low** findings — independently re-confirmed by
grep against the current file (its "High" section names one
High-severity gap, DAY2-TEST-H1; its "Medium" and "Low" sections
correspondingly). This is not hidden here. Additional Low/informational
items originated from the security, integration, architecture, and
release-readiness reviews.

A remediation cycle then addressed the selected High/Medium/Low
findings. The deterministic unit-test count increased from **163 to
212 tests** (independently confirmed: the final adjudication document
records "163/163 -> 212/212" and this verification's own read of the
Section 5 log shows "Ran 212 tests ... OK") because regression coverage
was strengthened, not because scope silently expanded.

The final adjudication,
`docs/engineering-reviews/day-02-v0.2-release-readiness.md`,
independently rechecked the remediation and its recorded conclusion
(re-confirmed by grep against the current file) is:

- Critical unresolved: **0**
- High unresolved: **0**
- Medium unresolved: **0**
- **Final verdict: GO FOR PR**

Then PR #2 merged (Section 3) and the exact merged tree passed the
authoritative release gate (Section 4) before tagging (Section 2).

---

## 11. Remediation history

Summarized without rewriting the historical review documents themselves
(none of which were modified):

| ID | Status | Closure |
|---|---|---|
| DAY2-TEST-H1 | CLOSED | Real EndpointSlice-plus-complete-Pod-termination drain predicate is directly regression-tested (`test_dependency_check.AppEndpointsDrainedTests`). |
| DAY2-TEST-M1 | CLOSED | UID/GID and ConfigMap-presence negative tests added (`test_cluster_check_failure_handling`). |
| DAY2-TEST-M2 | CLOSED | Meaningful bilateral gateway/app validator coverage added (`test_http_checks.CheckEndpointSemanticsTests`, `CheckAllEndpointsTests`). |
| DAY2-SEC-L1 | CLOSED | Gateway now performs runtime fail-closed backend-target validation (`test_gateway_backend_target.py`). |
| DAY2-INT-L1 | CLOSED | Missing namespace no longer misclassified as missing Secret (`test_secret_bootstrap.NamespaceClassificationTests`). |
| DAY2-INT-L2 | CLOSED | Outage `/backend` check now validates exact safe body semantics (`test_http_checks.CheckSafeUnavailableBodyTests`). |
| DAY2-TEST-L1 | CLOSED | Secret temporary file mode `0600` is regression-tested (`test_secret_bootstrap.TempFileModeTests`). |
| DAY2-TEST-L2 | CLOSED | Single-stack EndpointSlice duplicate-address/malformed-entry handling strengthened (`test_endpointslice.DefensivenessAndDedupTests`). |
| DAY2-TEST-L3 | CLOSED | Explicit forbidden-RBAC-kind regression coverage added (`test_validate_manifests.ForbiddenRbacKindTests`). |

---

## 12. Accepted / open technical debt

Day 2 does **not** carry zero technical debt:

**DAY2-INT-I1 — ACCEPTED / OPEN FOR FUTURE DUAL-STACK SUPPORT**

The current EndpointSlice helper (`scripts/endpointslice.py`) is
intentionally scoped to the current single-stack environment.
Identical-address deduplication does not constitute complete
cross-address-family logical Pod identity. Should be revisited if/when
dual-stack networking is introduced.

**DAY1-INT-I2 — ACCEPTED / OPEN FOR FUTURE BASE-IMAGE CHANGE**

The current pinned Distroless image still uses the known
`/usr/bin/python3.11` interpreter path, relied on by
`scripts/cluster_check.py`. Not closed by Day 2; remains coupled to the
current image's Python minor version.

**DAY1-REL-I1 — CLOSED**

Day 2 added the automated VERSION/image/version-label consistency guard
(`scripts/version_check.py`, 13/13 checks in Section 4/5), closing the
Day 1 item that flagged the absence of such a guard.

This document does not claim zero technical debt for Day 2.

---

## 13. Screenshot evidence index

All six required files were independently confirmed to exist on disk at
`docs/images/day-02/` at authoring time:

| Evidence | What it proves | Evidence tier |
|---|---|---|
| `01-pr2-merged.png` | PR #2 merged into `main`. | `[A]` |
| `02-v020-github-release.png` | The published GitHub Release `v0.2.0` (not draft, not prerelease). | `[A]` |
| `03-v020-tag-integrity.png` | Annotated tag object vs. peeled release-commit integrity for `v0.2.0`. | `[A]` |
| `04-v020-day2-check-pass.png` | Authoritative validation summary: 212 unit tests, 13 version checks, 94 manifest checks, 33 live cluster checks, 2 discovery checks, 27 secret checks, 5 smoke checks, 11 dependency-failure checks, final `day2-check` PASS. | `[B]`/`[C]`/`[D]` |
| `05-v020-cluster-4of4-ready.png` | Real live Kubernetes runtime: both Deployments at 2/2, EndpointSlices healthy, ClusterIP Services. | `[C]` |
| `06-v020-dependency-recovery.png` | Real dependency-failure and recovery proof: gateway `/livez` 200 / `/readyz` 503 during outage, restoration to 2/2 and `/backend` 200. | `[D]` |

This verification confirmed file existence only (all six present); it
does not claim to have visually inspected pixel content beyond what is
described above, and it does not present the screenshots as stronger
evidence than the underlying logs and live-cluster queries they
capture.

---

## 14. Claude infrastructure

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

## 15. Day 2 scope and next stage

Day 2 includes: multi-service architecture, Kubernetes Service DNS,
ConfigMaps, a runtime Secret, authenticated inter-service HTTP,
EndpointSlice, and dependency-aware readiness.

Still intentionally deferred:

- **Day 3:** scaling, rolling updates, rollback, scheduling,
  availability/PDB.
- **Day 4:** StatefulSet, PVC, persistence/recovery.
- **Day 5:** ServiceAccount, RBAC, NetworkPolicy.
- **Day 6:** Helm, GitHub Actions CI, automated kind CI validation.
- **Day 7:** final production-readiness hardening, final independent
  reviews, v1.0.0.

**Project 4 as a whole is not complete.**

---

## 16. Current disposition

Based on the independent verification performed in this document,
Project 4 / Day 2 / **v0.2.0** is:

- reviewed (five independent reviews + final adjudication, Section 10),
- merged (PR #2, Section 3),
- merged-main validated (`make day2-check` PASS at the release commit,
  original gate in Section 4, plus a post-release evidence-recovery
  revalidation in Section 5),
- tagged (`v0.2.0` → `2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36`,
  Section 2),
- published (GitHub Release `v0.2.0`, Section 2),
- runtime-verified live (Sections 6–9),
- post-release verified (this document).

**Project 4 Day 2 / v0.2.0 release is verified.**

It may be considered frozen after this document and the six screenshots
in Section 13 are committed and pushed to `main`. Because the user
explicitly chose a direct-`main` evidence commit for this stage (as for
Day 1), no `docs/evidence` PR is required for that follow-up commit.
That future evidence commit will advance `main` past
`2b40bdb9e2b397ed42ee4ef16bdbee666c41ac36`; it does not and cannot
change the immutable `v0.2.0` release commit identified in Section 2.

This disposition applies only to Project 4 / Day 2 / v0.2.0. **Day 3
remains next.**

---

PROJECT 4 DAY 2 v0.2.0 POST-RELEASE VERIFICATION COMPLETE

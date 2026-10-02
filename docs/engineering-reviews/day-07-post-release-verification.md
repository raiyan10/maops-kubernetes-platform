# Day 7 — v0.7.0 post-release verification

Verified on 2026-09-30, after publication.

This record separates two kinds of evidence:

- **Observed now:** checked directly while this record was written,
  from the local repository, the GitHub API, and log files kept outside
  the repository.
- **Recorded earlier:** results captured during Day 7 validation, in
  `day-07-live-validation-record.md` and `day-07-final-adjudication.md`,
  and in the merged-`main` validation logs. They are quoted here, not
  re-run.

No cluster check was run for this record, and nothing in the cluster
was changed.

## Verification method

The SSH remote is not usable from this non-interactive session: the key
is passphrase-protected and no agent is running. Remote refs were
therefore fetched once over HTTPS, using the GitHub CLI's existing
login as a per-command credential helper
(`git -c credential.helper='!gh auth git-credential' fetch https://…`).
Remote identities were read through the authenticated GitHub API
(`gh api`, `gh release view`, `gh pr view`, `gh run list`), and local
refs with `git rev-parse`. No remote URL or Git configuration was
changed.

## Release identity (observed now)

- **Release:** https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.7.0
- **Title:** "v0.7.0 — Day 7 deployment strategies".
- **Published:** 2026-09-30T07:33:57Z. It is neither a draft nor a
  prerelease, and is marked **Latest**.
- **Annotated tag object:** `d0dfb52bd06ca34c91e54ce6c700f860e9cee11b`,
  tagged 2026-09-30T07:29:05Z, with message "MAOps Kubernetes Platform
  v0.7.0 — Day 7 deployment strategies".
- **Tag target (peeled), remote and local:**
  `6557c8dcdaad7280b5f49e83957f976530a8da53`.
- **`main`:** remote (GitHub API) and local are both at
  `6557c8dcdaad7280b5f49e83957f976530a8da53`, and the local tree is
  clean.

The annotated `v0.7.0` tag resolves to PR #10's merge commit. Its tree
is byte-identical to the reviewed PR head
`c51b8dcb64018b180e9247bc2474be20db378e58`.

## Pull request (observed now)

| PR | Merged (UTC) | Merge commit | Head | Purpose |
|---|---|---|---|---|
| [#10](https://github.com/raiyan10/maops-kubernetes-platform/pull/10) | 2026-09-30T07:08:11Z | `6557c8dcdaad7280b5f49e83957f976530a8da53` | `feature/day-7-deployment-strategies` @ `c51b8dcb64018b180e9247bc2474be20db378e58` | Day 7: Recreate, Blue/Green and Canary on a pinned build |

## Continuous integration (observed now)

The cluster-free `CI` workflow (`make ci-check`) completed successfully
on the PR head and on the released commit:

| Run | Trigger | Commit | Result |
|---|---|---|---|
| 36680838833 | PR #10 | `c51b8dc` | success ("Static validation (cluster-free)") |
| 36681997696 | push to `main` | `6557c8d` | success |

**Local `make ci-check` on merged `main`** (recorded earlier,
2026-09-30, through the cluster-blocking PATH shim): exit 0, 0 blocked
cluster calls, 1658 unit tests OK, version-check 64/64, manifest-check
267/267, helm-check 2346/2346.

## Fresh-cluster gating run (recorded earlier; log re-read now)

Run `6b0029cc63724291a00bba6ed52ea7a9`, 2026-09-30 10:13-10:41 +06:00.
It was one uninterrupted `make day7-check`, and it created
`maops-k8s-day7` itself after the approved, guarded deletion of the
previous Day 7 cluster.

- **Exit status:** make's own exit status, `0`, recorded in
  `day7-check-fresh.exit`.
- **Log:** `day7-check-fresh.log`, SHA256
  `361ec4864ad6964ef7b2107fee44f9ce19789da3ace0599c261337fab1dc45df`.

**Freshness:**
- `kind create` ran.
- New node containers (`8b0b73a6d0ca`, `77e6d9bfd28f`, `95c69412fe8c`),
  created after the deletion.
- The app release started at revision 1.
- New namespace `690b1f43…`, PVC `2be6628b…` and PV `a9c74497…`.

**Build:** `fdb68741…`, pinned in every Helm stage (gateway
`62df7c01…`, app `b035e264…`, state `aafa33a4…`). The candidate's pinned
image and running config digest were verified before every route
change.

**Strategy results:**

| Strategy | PRIMARY | RESTORATION | Independent stable check |
|---|---|---|---|
| Blue/Green (143 checks) | PASS | PASS | 55/55 |
| Canary (212 checks; 179 stable / 21 candidate of 200, 0 errors) | PASS | PASS | 55/55 |
| Recreate, candidate only (182 checks; planned outage, 162 failed requests over ~41 s) | PASS | PASS | 55/55 |

**Final gate:** passed, with Day 7 final checks 86/86.

**Baselines (observed now):** in the private run directory (0700), both
files at mode 0600, unchanged since capture:
- `strategy-baseline.json`, SHA256 `aca1b8249c0238bd…`;
- `suite-baseline.json`, SHA256 `b27eec000178730b…`.

## Merged-main validation (recorded earlier; logs re-read now)

Run on 2026-09-30 13:16-13:21 +06:00, on merged `main` `6557c8d`,
before the tag was created. It ran against the existing fresh-run
cluster and baselines (`DAY7_RUN_ID=6b0029cc…`). The re-read summary
lines match:

| Check | Log (SHA256 prefix) | Result |
|---|---|---|
| `day7-preflight` | `04-…` (`e967bfde57a76c0c`) | **FAILED 12/13**, make exit 2: "Docker daemon reachable (server ?)" |
| `day7-preflight` (recheck) | `04c-…` (`517652fc76fd7e5f`) | 13/13, exit 0 |
| `day7-resume-check` | `05-…` (`5ed5f2d2bd43504f`) | exit 0: nodes 3/3 Ready, CNI 4/4, context 6/6, mesh 4/4, ambient listeners 67/67, rollout 35/35, running images 47/47, gateway 8/8 |
| `day7-stable-check` | `06-…` (`5e0d05ecdd8a3872`) | 55/55, exit 0 |
| `final-state-check` | `07-…` (`a2787f7f9d47cf51`) | 38/38, exit 0 |
| `day7-final-state-check` | `08-…` (`265dd04de30cc8cd`) | 86/86, exit 0 |

**The first preflight timeout** is kept as a failed result. `docker
info` did not answer within the script's 20 s bound while the host load
average was about 11, immediately after the unit suite. A read-only
latency check (`04b-…`) then measured `docker info` at 1.4-1.9 s, and
the single recorded recheck passed, with MemAvailable at exactly the
4.0 GiB threshold. The same symptom appeared, and cleared on recheck,
before the fresh run.

**State afterwards** (`09-post-state-readonly.log`, SHA256
`e1e005d75d77a292…`):
- **Pods:** the same 7 Pods as the fresh run's end state, all Ready
  with 0 restarts, every container on build `fdb68741…`.
- **Release:** revision 13, values equal to the stable stage plus the
  pinned tags. The chart rendered from merged `main` equals the
  deployed manifest (30 objects).
- **Leftovers:** no candidate or validation-client objects.
- **Traffic:** `Host: maops.local` returned HTTP 200.
- **Storage:** PVC and PV identities unchanged.
- **Baselines:** unchanged.
- **Day 1-6:** all 14 containers identical before and after, and
  stopped.

**Deliberately not rerun on merged `main`:**
- `mesh-check`: it changes the ztunnel DaemonSet environment, which
  rolls ztunnel, and it creates probe resources.
- `networkpolicy-check`: it creates and deletes probe Pods.
- The composite `day7-final-gate`, because it contains both.

Their evidence is the fresh run's (mesh 45/45, NetworkPolicy 37/37), on
the same Pods and release revision.

## Validation history (recorded earlier)

The full record is in `day-07-live-validation-record.md`; failed
attempts are preserved there. In summary:

1. **2026-09-28, staged run `dd99…`:** passed. It surfaced the
   `cni-status` cold-start race, now handled by `day7-nodes-ready`.
2. **2026-09-28, one-shot `ce55…` on the existing cluster:** passed, but
   exposed the image-contract defect: stable Pods ran the previous
   build. The fix was build pinning plus the running-image gate.
3. **Live validation-client -> candidate probe:** passed. A correlated
   Cilium "Policy denied" was observed.
4. **2026-09-29, host reboot:** three Pods lost ambient enrollment,
   through an istio-cni startup race (cause inferred). They were
   recovered manually, one Pod at a time, with approval.
5. **Run `c252…`:** the pinned rollout. Its first running-image gate
   **failed** on a checker assumption; the checker was corrected and
   independently confirmed, and it then passed.
6. **Attempt `5750…`:** **failed** at `day7-image-load` because of a
   Makefile profile defect, which was fixed.
7. **Run `985c…`, existing cluster:** passed.
8. **Fresh-cluster run `6b0029cc…`:** passed. The gate passed.

## Adjudication and scope

Final adjudication: **GATE PASSED for the local Kind reference
platform** (owner decision, 2026-09-30). It then went through PR #10,
merged-`main` validation, and `v0.7.0` publication. Earlier verdicts,
including GATE PENDING, are preserved in `day-07-final-adjudication.md`.

**Not claimed:**
- production readiness: no HA, no load balancer, no TLS, no
  autoscaling;
- a live upgrade onto *different* image bytes, which is proven
  statically only;
- exact Canary weights;
- zero-downtime Recreate;
- layer attribution of mesh-path denials, beyond the Cilium drop for
  the validation-client probe;
- automated post-reboot enrollment recovery;
- coexistence with running Day 1-6 clusters;
- data or Secret continuity across cluster recreation;
- reliability beyond one fresh creation on one host;
- live-cluster validation in GitHub Actions: CI is cluster-free by
  design.

## Disposition

Day 7 is released as `v0.7.0`, a local Kind reference platform, and its
implementation is frozen. `v0.7.0` remains permanently on
`6557c8dcdaad7280b5f49e83957f976530a8da53`; this documentation follows
the release and does not move the tag. **Day 8 (`v1.0.0`)**,
autoscaling and final production-readiness hardening, remains planned
and has not started.

**Private evidence** stays outside the repository and is not published.
It lives under `~/.local/state/maops-kubernetes-platform/`:
- `day7-logs/<run>/` and `day7-logs/merged-main-6557c8d/`;
- `day7-runs/<run>/` (baselines, 0700/0600);
- `day7-builds/`.

No Secret value is included in this record or its screenshots.

## Screenshots

Captured after release; the release tag is unchanged.

- [Published v0.7.0 GitHub Release](../images/day-07/01-github-release-v0.7.0.png)
  - **What it is:** a headless-browser capture of the public release
    page, signed out.
  - **What it shows:** "v0.7.0 — Day 7 deployment strategies", marked
    **Latest**, with tag `v0.7.0` on commit `6557c8d` (green check), and
    the release notes.
  - **Edit:** blank space below the footer was cropped; nothing else
    was changed.
- [Saved merged-main gate results](../images/day-07/02-merged-main-gate-results.png)
  - **What it is:** the actual output of re-reading the saved
    merged-main logs, rendered as a terminal image. It was produced
    after release; **the gates were not re-run for it**.
  - **What it shows:** each log's SHA256 prefix (matching the table
    above), then the summary lines: the first preflight **FAILED
    12/13** (exit 2), the recheck 13/13, resume-check (CNI 4/4, context
    6/6, mesh 4/4, ambient 67/67, rollout 35/35, running images 47/47,
    gateway 8/8), stable-check 55/55, final-state 38/38 and Day 7 final
    86/86.
  - **Edit:** the two long command lines are shortened with "…" on
    screen only. File names are relative, so no private path appears.

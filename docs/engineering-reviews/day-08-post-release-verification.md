# Day 8 — v1.0.0 post-release verification

Verified on 2026-10-05 (about 04:30–04:40Z), after publication.

This record separates two kinds of evidence:

- **Observed now:** checked directly while this record was written. The
  sources are the local repository, the GitHub API, the saved evidence
  files kept outside the repository, and a few strictly read-only cluster
  queries.
- **Recorded earlier:** results captured during Day 8 validation and on
  merged `main` before the tag existed. They come from
  `day-08-live-validation-record.md` (sections 11 and 12),
  `day-08-remediation-log.md` (section 12) and
  `day-08-independent-reviews.md` (Round 4). They are quoted here, not
  re-run.

No Day 8 gate or demonstration was re-run for this record, and nothing
in the cluster was changed. This record did not alter the
implementation, the `v1.0.0` tag or the GitHub Release. The owner's
later fix to the Release-notes link is described below.

## Verification method

- **Fetching.** The SSH remote is not usable from this non-interactive
  session, so a plain `git fetch` fails. Remote refs were fetched once
  over HTTPS, using the GitHub CLI's existing login as a per-command
  credential helper (`git -c credential.helper='!gh auth
  git-credential' fetch https://…`).
- **Remote identities.** Read through the authenticated GitHub API:
  `gh api`, `gh release view`, `gh pr view`, `gh run list`/`view`.
- **Local refs.** Read with `git rev-parse` and `git cat-file`.
- **Evidence files.** Saved evidence was re-read in place, under
  `~/.local/state/maops-kubernetes-platform/day8-runs/`.
- **Cluster.** The read-only queries were `kubectl get` and `helm list`
  against `kind-maops-k8s-day7`.
- **Unchanged.** No remote URL or Git configuration was changed.

## Release identity (observed now)

- **Release:**
  https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v1.0.0
- **Title:** "v1.0.0 — Day 8 autoscaling".
- **Published:** 2026-10-05T04:21:55Z by `raiyan10`. It is neither a
  draft nor a prerelease. The API's `releases/latest` returns `v1.0.0`,
  and the page shows it as **Latest**.
- **Annotated tag object:** `b1c0f00a25f2348fd10cef525a7d1be7f014dd25`.
  It was tagged 2026-10-05T10:15:36+06:00 (04:15:36Z) by Raiyan Yousuf,
  with the message "v1.0.0 — Day 8 autoscaling". The remote and local
  tag objects are the same.
- **Tag target (peeled), remote and local:**
  `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`. This is PR #12's merge
  commit.
- **`main`:** the remote (GitHub API and the HTTPS fetch) and local are
  both at `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`, and the local tree
  was clean before this documentation branch was created.
- **Same tree as the reviewed head.** The tag's tree
  (`6c82a1051b163fe7ede396c48427c2b2d5cf598f`) is byte-identical to the
  reviewed PR #12 head `0c03929daea0576dd5ec5554e4017d74380f62f2`.
- **`VERSION`** at the tag reads `1.0.0`.

**Release notes link (history of checks).** The owner corrected the
"Merged-main CI" link in the published notes after publication. This
record did not edit the Release.

| Checked (2026-10-05, UTC) | Release source (API `body`) | Rendered (API `body_html` and a signed-out capture) |
|---|---|---|
| about 04:34 (first capture) | Not inspected | Shown as literal `[Merged-main CI](https://…)` text |
| about 04:47, after the owner's first edit | Nested: `[[Merged-main CI](…)](…)` | Inner link rendered; the outer brackets and URL were still literal text |
| about 06:35, after the owner's second edit | `[Merged-main CI](https://github.com/raiyan10/maops-kubernetes-platform/actions/runs/37261944088)` | **A working link** to CI run 37261944088 |

Screenshot 1 is the 06:35Z capture, taken after the correction.

## Pull requests (observed now)

| PR | Merged (UTC) | Merge commit | Head | Purpose |
|---|---|---|---|---|
| [#11](https://github.com/raiyan10/maops-kubernetes-platform/pull/11) | 2026-10-04T10:24:22Z | `78b02a1b91fdd5dacee7dd1b5b819865e4cee70e` | `feature/day-8-autoscaling-hardening` @ `375b71fb5d45d42cab14464d22905aac1b117e1b` | Day 8 autoscaling and the `1.0.0` preparation |
| [#12](https://github.com/raiyan10/maops-kubernetes-platform/pull/12) | 2026-10-05T04:04:26Z | `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438` | `fix/day-8-vpa-demonstration` @ `0c03929daea0576dd5ec5554e4017d74380f62f2` | Make the VPA demonstration deterministic (after merged-main run `f837802b…` failed) |

## Continuous integration (observed now)

The cluster-free `CI` workflow ("Static validation (cluster-free)")
succeeded on both PR heads and on both merge commits:

| Run | Trigger | Commit | Created (UTC) | Result |
|---|---|---|---|---|
| 37194655306 | PR #11 | `375b71f` | 2026-10-04T10:13:12Z | success |
| 37195257237 | push to `main` | `78b02a1` | 2026-10-04T10:24:24Z | success |
| 37261698892 | PR #12 | `0c03929` | 2026-10-05T04:00:49Z | success |
| [37261944088](https://github.com/raiyan10/maops-kubernetes-platform/actions/runs/37261944088) | push to `main` | `4d74cfb` (the tagged commit) | 2026-10-05T04:04:28Z | success (job 04:04:31Z → 04:05:36Z) |

CI is cluster-free by design. Live behaviour comes from the local runs
below.

## Live validation (recorded earlier; evidence re-read now)

### Chronology

| Time (UTC) | Run | Code | Result |
|---|---|---|---|
| 2026-10-04 05:28:54 → 05:48:34 | Run H `0d158cfe2fc8453595d0185e004fcfaf` | pre-merge branch | exit 0, VPA 17/17. Historical: its VPA pass depended on usage at the time |
| 2026-10-04 10:24:22 | PR #11 merged | `78b02a1` | - |
| 2026-10-04 10:44 → 11:05 | Owner-approved recovery of three Day 7 Pods that lacked ztunnel listeners after a host restart | - | ambient listener checks 64/67 → 67/67; `maops-state-0` never deleted |
| 2026-10-04 after 11:08:59 → 11:21:09 | Run f837 `f837802b23ba4bd39cbf36cf28cd2cb3` | `78b02a1` | **FAILED: VPA 16/17**; cleanup 16/16 |
| 2026-10-04 → 11:40:24 | Separate `day8-final-gate` for run f837 | `78b02a1` | exit 0, stable 7/7 |
| 2026-10-04 11:59:48 → 12:18:36 | **Run I `f4e69ac6356545efb4bf040995ca4863`** | the VPA correction (later PR #12) | **exit 0, VPA 17/17** |
| 2026-10-05 04:04:26 | PR #12 merged | `4d74cfb` | - |
| 2026-10-05 → 04:11:40 | Merged-main `day8-final-gate` with `DAY8_RUN_ID=f4e69ac6…` | `4d74cfb` | passed (see below) |
| 2026-10-05 04:15:36 / 04:21:55 | `v1.0.0` tagged / Release published | `4d74cfb` | - |

The pod recovery and run f837's full details are in the live record,
section 12.1. The run f837 start time is approximate: its log has no
start stamp, and its run directory was created at 11:11:51Z.

### The failed VPA run and the correction

- **Run f837 failed VPA 16/17.** The Off-mode recommendation and the newly
  admitted Pod were both 10m/32Mi, exactly the Deployment's declared
  requests.
  - The admission annotation was present, but the resources had not
    changed.
  - The live assertion, that admitted requests must differ from the
    declared ones, correctly failed. An annotation alone is not proof.
  - Its cleanup passed 16/16, and its separate final gate exited 0.
- **Cause.** The VPA recommender's floor (10 millicores / 32 MiB),
  `minAllowed` (10m/32Mi) and the declared requests were all equal, so a
  cold-start recommendation at the floor was admitted unchanged.
- **Correction (PR #12).**
  - VPA `minAllowed` memory was raised to **48Mi**. The declared requests
    stay 10m/32Mi and `maxAllowed` stays 40m/96Mi.
  - Limits scale to 96Mi at the minimum and 192Mi at the maximum, under
    the LimitRange maximum of 256Mi.
  - The ResourceQuota is unchanged: it is exactly `budget()`, 710m /
    544Mi requests, 2000m / 1088Mi limits, 13 Pods.
  - A cluster-free invariant, `day8_objects.vpa_change_problems()`, now
    refuses any design in which a valid recommendation can equal both
    declared requests. Regression tests replay run f837's cold start.
  - A targeted independent review found two MEDIUM test gaps. Both were
    closed with tests before merge (independent reviews, Round 4).

### Run I (passing, on the released code)

The log is `make-day8-check.log` in the run directory (sha256 prefix
`083433dda308a333`). Its summary lines were re-read now and match the
live record, section 12.2:

- preflight 7/7, scaling image 4/4, guards 5/5;
- KEDA pre-install ownership 3/3, add-ons active 35/35, quota proof 9/9;
- **HPA 9/9:** 1 → 3 → 4 Ready under load, then back to 1;
- **VPA 17/17:**
  - declared 10m/32Mi;
  - Off target 35m / 63,544,758 B, with `lowerBound` memory 48Mi;
  - the new Pod was admitted at 35m / 63,544,758 B (limits 175m /
    127,089,516 B);
  - the existing Pod was untouched.
- **KEDA 13/13:** 60/60 items processed; workers 0 → 3 → 0;
- cleanup 16/16, with `day8-check: demonstrations exit 0, cleanup exit
  0`;
- final gate: mesh 45/45, NetworkPolicy 37/37, add-ons after cleanup
  23/23, Day 7 stable 7/7.

**Run I tested the released code.** Every one of the 67 `scripts/*.py`
files at `v1.0.0` has the same sha256 as the snapshot taken when run I
started. The chart, `helm-values/`, the Makefile, the workload and
scaling images, `k8s/`, `kind/` and `VERSION` are unchanged between
`78b02a1` and `v1.0.0`. The only later changes were two unit tests and
documentation; `tests/test_day8.py` went from 173 to 175 tests (1838
overall), and CI ran them on the tagged commit.

**Not a cold start.** Run I's recommendation was above both floors, so
the case where a cold-start recommendation is capped up to 48Mi did not
occur live. That case is proven by the cluster-free invariant and the
regression tests. The live `lowerBound` of 48Mi shows the new minimum in
force on the live object.

### Merged-main final gate (recorded earlier; files re-read now)

On 2026-10-05, after PR #12 merged and before the tag, the owner ran
`DAY8_RUN_ID=f4e69ac6… make day8-final-gate` on `4d74cfb`. It wrote two
files into run I's directory, and both were re-read now:

- **`keda-state-after-cleanup-2026-10-05T041136Z.json`.** KEDA release
  absent, no Pods, no binding naming a KEDA ServiceAccount. The content
  is identical to run I's own post-cleanup record, apart from the time.
- **`stable-check-2026-10-05T041140Z.json`.** Stable check **7/7**,
  failed 0:
  - Day 7 release revision 14, deployed, manifest sha256 `abfd77b3…`;
  - PVC `2be6628b…` Bound, PV `a9c74497…`;
  - `state.json`: 15 B, sha256 `3ce4f556…`, served with HTTP 200;
  - all 7 Pod UIDs equal to run I's baseline.

The gate's console output was not saved to a file. Its other steps
(CNI, context, mesh status, ambient listeners, rollout, running images,
Gateway API, `mesh-check`, `networkpolicy-check`) and its exit 0 are as
reported by the owner and in the release notes. They are not re-verified
from a saved log here.

## Cluster state after publication (observed now, read-only)

At 2026-10-05T04:35:53Z, with context `kind-maops-k8s-day7`:

- **Nodes:** all three `maops-k8s-day7` nodes are Ready. They are the
  only containers running; no Day 1–6 cluster is running.
- **Pods:** all 7 Day 7 Pods are Ready, with the same UIDs as run I and
  the merged-main final gate. This includes `maops-state-0`
  `2cd7be95…`.
- **Storage:** PVC `data-maops-state-0` `2be6628b…` is Bound.
- **KEDA:** the `keda` and `maops-day8-scaling` namespaces are NotFound,
  no `keda.sh` CRDs exist, and there is no `keda` Helm release.
- **Helm releases:**
  - `maops-kubernetes-platform-day7` at revision 14, deployed, chart
    `maops-kubernetes-platform-1.0.0`;
  - `metrics-server` and `vertical-pod-autoscaler` at revision 14. Each
    run's idempotent add-on install bumps these.

## Scope and limitations (not claimed)

- **Platform.** A local Kind reference platform on one shared host, not
  a production deployment. There is no HA, no external load balancer and
  no TLS ingress.
- **What scales.** HPA, VPA and KEDA are proven on small disposable
  workloads in `maops-day8-scaling`. The Day 7 application itself is not
  autoscaled.
- **VPA.**
  - It runs only `Off` and `Initial`, with no updater. Recommendations
    are applied only when a Pod is created, never to running Pods.
  - Recommendations come from minutes of history, not days.
  - The cold-start path is proven statically, not live (see above).
- **Metrics Server.** It uses `--kubelet-insecure-tls`, and its
  APIService uses `insecureSkipTLSVerify`. Both are local-Kind
  compromises.
- **KEDA queue.** Redis has no password, relies on NetworkPolicy
  isolation and keeps nothing on disk. A hard-killed worker (node loss,
  OOM) can lose an in-flight item, so this is not an at-least-once
  queue.
- **Host interruption.** An interrupted Day 8 run needs manual
  `day8-cleanup` and `day8-final-gate`. Recovering ambient enrollment
  after a host restart is manual and needs owner approval. The cause of
  the 2026-10-04 enrollment loss is inferred, not proven.
- **Day 6 upgrade.** The frozen Day 6 release cannot be upgraded to this
  chart, because of its immutable 0.6.0 claim-template labels. Nothing
  upgrades it.
- **CI.** Cluster-free by design; there is no live-cluster validation in
  GitHub Actions.

## Disposition

- **Released.** Day 8 is released as `v1.0.0`, a local Kind reference
  platform. `v1.0.0` remains on
  `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`. This documentation follows
  the release and does not move the tag.
- **Validation history kept.** Earlier runs and failed attempts are
  preserved in `day-08-live-validation-record.md`, including run H
  (historical), run f837 (failed VPA, cleanup and final gate passed) and
  run I (authoritative).
- **Private evidence** stays outside the repository and is not
  published. It lives under
  `~/.local/state/maops-kubernetes-platform/day8-runs/`:
  - `<run>/` for each run (0700, files 0600);
  - `merged-main-validation/`;
  - `merged-main-mesh-72U9xD5v/`.

No log, Secret value or key material is copied into this record or its
screenshots.

## Screenshots

Both screenshots were captured after release. The tag is unchanged. The
Release's notes were edited only by the owner, to fix the link described
above.

- [Published v1.0.0 GitHub Release](../images/day-08/01-github-release-v1.0.0.png)
  - **What it is:** a headless-browser capture of the public release
    page, signed out (Microsoft Edge headless, with a throwaway profile),
    taken 2026-10-05 at 06:35:26Z, after the owner corrected the
    "Merged-main CI" link. It replaces an earlier 04:34Z capture that
    showed the broken link.
  - **What it shows:**
    - "v1.0.0 — Day 8 autoscaling", marked **Latest**;
    - tag `v1.0.0` on commit `4d74cfb`, with a green check;
    - the release notes, with "Merged-main CI" rendered as a link.
  - **Edit:** the capture was cropped at the bottom from 1100 to 1060 px
    height. This removes the cut-off "Assets" row below the release
    notes. Nothing else was changed.
- [Saved Day 8 gate results](../images/day-08/02-saved-day8-gate-results.png)
  - **What it is:** the actual output of a read-only script that
    re-reads the saved evidence files, rendered as a terminal image. It
    was produced after release; **no gate or demonstration was re-run
    for it**.
  - **What it shows:**
    - run f837's VPA **16/17**, with the failing comparison (admitted ==
      declared, 10m/32Mi), cleanup 16/16 and final-gate stable 7/7;
    - run I's log sha256 prefix and its summary lines (VPA 17/17, HPA
      9/9, KEDA 13/13, cleanup 16/16, mesh 45/45, NetworkPolicy 37/37,
      add-ons after cleanup 23/23), plus the declared, recommended and
      admitted VPA resources;
    - the merged-main final gate's two records (KEDA absent; stable 7/7
      with the Day 7 revision, PVC and `state.json` identity).
  - **Privacy:** file names are relative, so no private path appears.

# Day 7 / v0.7.0 - Final Adjudication (owner decision: GATE PASSED for the local Kind reference platform; originally prepared for the owner's review - see the history note)

> **STATUS: GATE PASSED - local Kind reference platform (owner decision,
> 2026-09-30).** The owner's condition, closing limitation 1, was met by
> the approved fresh-cluster run `6b0029cc63724291a00bba6ed52ea7a9`:
> - make exit 0;
> - `kind create` ran; new node containers; the app release started at
>   revision 1; new namespace, PVC and PV;
> - all three strategies PRIMARY and RESTORATION PASS under one pinned
>   build, and the final gate passed.
>
> See `day-07-live-validation-record.md`. Day 7 `v0.7.0` is a **release
> candidate**. **Still pending:** the PR, merge, merged-`main`
> validation, and publication of the `v0.7.0` tag and release. This
> document does not perform or imply any of them. Limitations 2-9 below
> are accepted. **Not production-ready.**
>
> *History:* this section read "GATE PENDING" from 2026-09-30 until the
> fresh-cluster run passed. The earlier text below is kept unchanged as
> the record of the evidence at that time.

**Prepared by:** the implementing session (Claude Code). The inputs are:
- the independent reviews recorded in `day-07-independent-reviews.md`;
- the evidence in `day-07-live-validation-record.md`.

This document is the implementer's synthesis and recommendation. The
release decision belongs to the project owner. Nothing here is a
commit, tag or release.

**Date:** 2026-09-29. **Branch:** `feature/day-7-deployment-strategies`.
**HEAD:** `74832c41a35a04d905aaa203e22f7d06bb7eb4e9`. The whole Day 7
diff is unstaged and the index is empty.

**Scope of the question:** is Day 7 (Recreate, Blue/Green and Canary on
the Helm + Gateway API + Istio ambient platform, on the isolated
`maops-k8s-day7` kind cluster) ready as a **local kind reference
platform**? Production readiness is **not** in question and is **not**
claimed.

---

## Evidence summary

### Static (cluster-free)
- `make ci-check` through the cluster-blocking PATH shim: real exit 0,
  0 blocked cluster calls (2026-09-29 15:55).
  - Unit suite: 1658 tests.
  - version-check 64/64, manifest-check 267/267, helm-check 2346/2346.
  - helm-check covers every Day 7 stage rendered against declared
    expectations, the refusal of every stage without a verified build,
    and a check that two builds change every workload Pod template's
    image and nothing else.
- Frozen sources are unchanged: `k8s/base`, `k8s/day6`, earlier kind
  configs, and the Day 1-6 records.
- The `v0.6.0` tag is unchanged (`186855d7...` -> `19d6b28...`).
- `git diff --check` is clean.

### Live - final integration run `985c466a246e4fb681fa427d4c6fe64f` (one uninterrupted `make day7-check`, exit 0)
- **Build:** one pinned build, `fdb68741...`, carried through every
  Helm stage. Revisions ran 30 -> 42. The build is recorded in the new
  strategy baseline, and every stage was checked against it.
- **Candidate image identity:** before every route-changing stage, the
  promotion gate proved the candidate Pods' spec image was the pinned
  gateway tag and their running config digest was the build's gateway
  digest. The Recreate replacement Pods were proven the same way.
- **Strategy results:**

  | Strategy | PRIMARY | RESTORATION | Independent stable check |
  |---|---|---|---|
  | Blue/Green (143 checks) | PASS | PASS | 55/55 |
  | Canary (212 checks) | PASS | PASS | 55/55 |
  | Recreate (182 checks) | PASS | PASS | 55/55 |

  Each stable check includes running-image verification.
- **Final gate:** all sub-gates passed, and Day 7 final checks were
  86/86. That includes an unchanged manifest hash and Helm values
  exactly equal to the stable stage plus the recorded pinned tags.
- **End state:**
  - no candidate or validation-client objects;
  - the 7 stable Pods Ready, with ambient listeners, on the pinned
    build;
  - the Gateway serving stable traffic;
  - PVC `91b45f89...` and PV `f830a3e9...` unchanged across all four
    runs.
- **Integrity:** the new baselines were written once; the pre-run tree
  hash and all 89 earlier evidence files were unchanged afterwards.
- **Day 1-6:** all 14 node containers are byte-identical in state
  before and after.

### Live - supporting results (earlier runs, all preserved)
- **Staged run `dd99769f...`:** passed. It surfaced the `cni-status`
  cold-start race, since fixed by `day7-nodes-ready`.
- **One-shot run `ce55f5fb...`:** passed, and exposed the
  image-contract defect: rebuilt images under a mutable tag, a no-op
  deploy, and stable Pods on the previous build. The defect is fixed
  and proven.
- **Live `validation-client -> gateway-candidate` probe:** passed. A
  correlated Cilium "Policy denied" was observed, which proves the
  NetworkPolicy layer.
- **Run `c252aa3d...`:**
  - the first live proof of the pinned-tag rollout (revision 28 -> 29);
  - the running-image gate **failed** first, and that failure stands;
  - the checker was corrected, independently confirmed through
    `crictl inspect`, and the final gate then passed.
- **Attempt `5750593984...`:** **failed** at `day7-image-load`. That was
  a Makefile defect introduced by a post-review fix. It was fixed. The
  stop came before any Day 7 application change: no deploy, baseline or
  stage. It did re-apply the platform Helm releases (Cilium and four
  Istio charts), with identical values and no Pod restarts, which
  advanced their revisions (inferred 2 -> 3).

## Findings closed during Day 7
All independent review rounds ended at APPROVE or APPROVE WITH
NON-BLOCKING FINDINGS. No blocking finding is open. The rounds were:
- 5 initial reviewers;
- 4 focused reviewers of the image-contract remediation;
- 3 focused reviewers of the corrected gate.

The defects that live runs found were each fixed and then exercised
live:
- the `cni-status` cold-start race;
- the image-contract no-op deploy;
- the runtime-reported-tag assumption in the running-image gate;
- the `day7-image-load` profile recipe.

## Remaining limitations (accepted or open - owner to decide)
1. **CLOSED 2026-09-30 by fresh-cluster run `6b0029cc...` (was: no
   fresh-cluster start had been proven).** The original text follows. Every one-shot
   `day7-check` reused the long-lived `maops-k8s-day7` cluster. The
   cold-start fix (`day7-nodes-ready`) has only run against Ready
   nodes. Closing this requires deleting and recreating the Day 7
   cluster, which has not been authorized.
2. **Post-reboot ambient enrollment.** After the 2026-09-29 host
   reboot, three Pods lacked ztunnel in-Pod listeners: an istio-cni
   startup-reconcile race. The inferred cause is recorded as inference.
   `day7-resume-check` detects this correctly. Recovery was a manual,
   guarded, one-Pod-at-a-time recreation. There is no automated
   remediation, the same class of limitation as recorded for Day 6.
3. **No live rollout onto new bytes.** Live runs proved a
   reference-only rollout (mutable tag -> pinned tag, same bytes) and
   consistent carriage of one build. A rollout onto a genuinely
   different build is proven statically only: the chart-render tests
   and the mocked running-image gate tests.
4. **Not new application code.** The 0.7.0 images are rebuilds of the
   unchanged v0.6.0 sources. The candidate is a configuration variant
   sharing the stable ServiceAccount and Istio principal.
5. **Canary weights are observed, not exact.** For example, 182/18 of
   200.
6. **The Recreate outage is real.** 159 failed requests over about 40 s
   in the final run; this is the planned behaviour.
7. **Validation-client -> candidate denial is proven live at the Cilium
   NetworkPolicy layer only.** AuthorizationPolicy coverage of that
   path is static.
8. **Host environment:**
   - `tool-check` requires `/usr/bin/docker`. The operator's
     `~/.local/bin/docker` wrapper must be bypassed with
     `PATH=/usr/bin:$PATH`; both reach the same engine.
   - Docker Desktop auto-restarts every `maops-k8s-day*` container
     after a host reboot.
   - The Day 1-6 containers were stopped by an action outside the
     sessions on 2026-09-29 at 09:23. The cause is not established.
9. **Log trailer inaccuracy.** Three logs from 2026-09-28/29 carry an
   inaccurate "exit 0" trailer. Their true results were established
   from their content, and later runs record make's own exit status.

## Recommendation (for the owner) - historical; superseded by GATE PASSED above
- **Local kind reference platform:** the evidence supports **READY**,
  with limitations 1-9 explicitly accepted. Limitation 1 (fresh-cluster
  start) is the only one that earlier reviewers asked to have closed
  before release sign-off.
- **The owner's choice:**
  - **(a)** accept limitation 1 as recorded and adjudicate Day 7 ready
    as a local kind reference platform on this evidence; or
  - **(b)** authorize deleting and recreating `maops-k8s-day7` (Day 7
    only, never Day 1-6) and one fresh-cluster `make day7-check`
    before adjudicating.
- **Production readiness:** **NOT READY** and not claimed.
  Autoscaling and final hardening belong to Day 8 (v1.0.0).

Nothing is staged, committed, tagged, pushed or published.

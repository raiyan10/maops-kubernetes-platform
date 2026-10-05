# Kubernetes Platform - portfolio case study

## Problem and role

Build a repeatable local Kubernetes platform that shows how to deploy,
isolate, update, scale and operate a small stateful application, with
every claim proven against a real cluster rather than asserted in YAML.
This is a self-directed MAOps Technologies portfolio project, and Raiyan
Yousuf is its engineering owner: he owned its scope, design decisions,
validation sign-off and publication. He used AI-assisted engineering
sessions (Claude Code) for implementation, testing and the independent
reviews, as recorded in [`engineering-reviews/`](engineering-reviews/).

## Delivered platform (Days 1-8)

- A `gateway -> app -> state` call chain, ending in a single-replica
  state StatefulSet with a PVC (Days 1-4).
- Non-root workloads, a dedicated ServiceAccount per workload, scoped
  RBAC and Cilium-enforced NetworkPolicies with explicit permitted paths
  (Day 5).
- Helm packaging, Gateway API routing through Istio and Istio ambient
  identity/mTLS, plus cluster-free GitHub Actions checks (Day 6).
- Recreate, Blue/Green and Canary exercises on a separate cluster, each
  followed by a verified restoration, under one pinned build (Day 7).
- Isolated HPA, VPA (`Off`/`Initial`, no updater) and KEDA
  demonstrations, one controller per disposable target, in a temporary
  namespace bounded by a LimitRange and an exact ResourceQuota, with
  verified cleanup and an independent check that the application was
  untouched (Day 8).
- A Makefile as the single local interface, dependency-free validation
  scripts, unit tests with negative cases, and failure records kept
  alongside the passing runs.

Architecture: [`architecture.md`](architecture.md). Stage plan:
[`roadmap.md`](roadmap.md).

## Release evidence

[`v1.0.0`](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v1.0.0)
was published on 2026-10-05. Its annotated tag stays on PR #12's merge
commit `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`.

- **Live run.** Corrected run I (`f4e69ac6356545efb4bf040995ca4863`)
  exited 0: HPA 9/9, VPA 17/17, KEDA 13/13 with 60/60 queue items
  processed, and cleanup 16/16.
- **Unit tests.** The final cluster-free suite passed 1,838 tests,
  including 175 Day 8 tests. CI passed on the tagged commit.
- **Failure kept.** The first merged-`main` run (`f837802b…`) failed VPA
  16/17, because a cold-start recommendation equalled the declared
  requests. PR #12 raised the VPA memory floor to 48Mi and added a
  policy invariant with regression tests. The failed run and the earlier
  historical run H stay in the
  [live validation record](engineering-reviews/day-08-live-validation-record.md).
- **Merged-`main` final gate.** The owner reported it exiting 0. Only its
  stable 7/7 and KEDA-absent results have saved files; its other results
  are as reported by the owner and in the release notes.

The [post-release verification record](engineering-reviews/day-08-post-release-verification.md)
separates what was observed after publication from what was recorded
earlier, and describes the provenance of its two screenshots.

## What the result does not establish

- This is a single-host Kind reference platform, not a production
  deployment. It shows scoped operations, controller reconciliation and
  verified restoration within the tested local environment.
- The application itself is not autoscaled; only disposable targets
  scale.
- The state service has no high availability and no cluster-loss
  recovery.
- Run I was not a cold start. The corrected VPA floor case is covered by
  regression tests, not by a live cold-start run.
- Metrics Server uses insecure kubelet TLS, a Kind compromise.
- The disposable Redis queue relies on NetworkPolicy isolation and can
  lose an in-flight item if a worker is hard-killed.
- An interrupted run, or a host restart, needs manual cleanup and
  recovery.

## Next project

P5, a GitHub Actions CI/CD platform, starts from its own architecture.
It may reuse this project's documented container, Helm and validation
conventions through an explicit interface. Its workflows, artifact
identities and delivery gates are planned work, not capabilities of
this Kubernetes release.

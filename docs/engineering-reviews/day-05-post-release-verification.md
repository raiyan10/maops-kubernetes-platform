# Day 5 — v0.5.0 post-release verification

Verified on 2026-09-19.

## Release identity

- PR: https://github.com/raiyan10/maops-kubernetes-platform/pull/5
- Release: https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.5.0
- Published: 2026-09-19T17:26:08Z; neither draft nor prerelease.
- Merge commit: `a6f6124198e3311023bddb84f2e7fce657ad52b4`
- Annotated tag object: `eb212c48c057df12fb826faffb5842444abcd14a`
- Retained feature head: `c4cb5817e1ff9d8a14ee6653454f66f92c46eaff`

The annotated `v0.5.0` tag resolves permanently to the Day 5 merge
commit.

## Delivered security boundaries

- Dedicated ServiceAccounts for gateway, app, state, and diagnostics.
- Namespace-scoped least-privilege diagnostics RBAC.
- Cilium 1.20.1 as the enforcing CNI with kube-proxy retained.
- Default-deny ingress and egress NetworkPolicy.
- Explicit DNS, gateway-to-app, app-to-state, and
  validation-to-gateway paths.
- Live negative validation of forbidden RBAC operations and blocked
  network paths.

## Merged-main validation

Merged main passed the complete authoritative `make day5-check`
sequence.

- Unit tests: 814.
- Manifest checks: 267/267.
- Version checks: 35/35.
- CNI status: 4/4.
- Storage hardening: 2/2.
- Runtime rollout and security posture: 35/35.
- RBAC: 10/10.
- NetworkPolicy: 8/8.
- Rolling update and rollback: 38/38.
- Persistence: 12/12.
- PVC retention: 22/22.
- Final-state restoration: 40/40.
- Make exit code: 0.
- Logging exit code: 0.
- External state-file hash unchanged.
- Working tree clean afterward.

Local evidence directory:

`merged-main-20260919T170309Z-9wK5Nt` under the portfolio's external
`_local-evidence/maops-kubernetes-platform/day-05/` directory.

Validation log SHA256:

`906a24e470c669a4b5ba5b3559acc494220c4ee79efacf184dcf2b5b0f1aaa33`

## Adjudication and accepted limitation

Final adjudication: GO FOR PR, with zero unresolved implementation
Critical, High, or Medium findings.

Running all historical kind environments concurrently exceeded the
available WSL2 host capacity. Day 1–4 clusters were stopped, not
deleted. Their repositories, tags, releases, reviews, and evidence
remain preserved, but their current runtime health is not claimed by
this release.

Future multi-day validation should stop superseded clusters before
starting the active environment.

## Cosmetic validation wording

The merged-main log includes an inherited success message saying
“Day 4 context” even though the checker used and verified
`kind-maops-k8s-day5`.

The context, node identities, workloads, images, Cilium installation,
RBAC checks, and NetworkPolicy checks were all Day 5. The wording is a
Low-severity presentation issue only and was disclosed in the release
notes.

The released implementation remains unchanged. The shared context
checker wording will be updated when it is advanced for Day 6.

## Disposition

Day 5 is released and evidence-closed. Its implementation is frozen.

This documentation-only evidence commit follows the release merge;
`v0.5.0` remains permanently on the merge commit.

Day 6 introduces the service-mesh stage. Day 7 remains responsible
for Recreate, Blue-Green, and Canary demonstrations.

The central portfolio/profile update remains deferred until the
Kubernetes project's v1.0.0 closure.

## Screenshots

Added after release; the release tag remains unchanged.

- [Published GitHub Release](../images/day-05/01-v050-github-release.png)
- [Recorded merged-main validation](../images/day-05/02-v050-merged-main-validation.png)

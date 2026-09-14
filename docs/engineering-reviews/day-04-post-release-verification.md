# Day 4 — v0.4.0 post-release verification

Verified on 2026-09-13.

## Release identity

- PR: https://github.com/raiyan10/maops-kubernetes-platform/pull/4
- Release: https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.4.0
- Published: 2026-09-13T10:50:06Z; neither draft nor prerelease.
- Merge commit: `0cb6d905402e992077d28f8fd91e557f44592868`
- Annotated tag object: `c10291624755f48483f8c0f3cef0f516146243e8`
- Retained feature head: `7a5cb9e8c5722402d458a44704ed9fae7365b55f`

## Validation

Merged main matched the approved feature content and passed the
complete authoritative `make day4-check` sequence.

- Make exit code: 0.
- Logging exit code: 0.
- Final-state checks: 39/39.
- External state-file hash unchanged across the run.
- Working tree clean afterward.

Local evidence directory:
`merged-main-20260913T103427Z-CdMDbS` under the portfolio's external
`_local-evidence/maops-kubernetes-platform/day-04/` directory.

Validation log SHA256:
`26f24f659bf0a3cf683db2c87101701098e7b0db5688831a3fc9cd8526716192`

## Disposition

Final adjudication: GO FOR PR, with zero unresolved Critical,
High, or Medium findings. Owner-accepted limitations remain:

- Manual storage bootstrap and verification for new/replaced nodes.
- Historical restart cause unresolved.
- Low-severity scratch-namespace cleanup TOCTOU risk.

Original reviews, failed attempts, and remediation remain preserved.
See `day-04-final-adjudication.md` and `day-04-remediation-log.md`.

Day 4 is released and evidence-closed. Its implementation is frozen.
This documentation-only evidence commit follows the release merge;
v0.4.0 remains permanently on the merge commit.

The central portfolio/profile update remains deferred until the
Kubernetes project's v1.0.0 closure.

## Screenshots

Added after release; the release tag remains unchanged.

- [Published GitHub Release](../images/day-04/01-v040-github-release.png)
- [Recorded merged-main validation](../images/day-04/02-v040-merged-main-validation.png)

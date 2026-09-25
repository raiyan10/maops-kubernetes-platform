# Day 6 — v0.6.0 post-release verification

Verified on 2026-09-25, after publication.

This record separates two kinds of evidence:

- **Observed now** — checked directly while this record was written, from the local
  repository, the GitHub API, and log files kept outside the repository.
- **Recorded earlier** — results captured during Day 6 validation and recorded in
  `docs/architecture.md`, `day-06-remediation-log.md`, and `day-06-final-adjudication.md`.
  They are quoted here, not re-run.

No cluster check was run for this record, and nothing in the cluster was changed.

## Verification method

The local `git fetch origin` failed: the SSH remote rejected this session with
"Permission denied (publickey)". **Remote identities were therefore verified through the
authenticated GitHub API (`gh api` / `gh release view` / `gh pr view` / `gh run list`), not a
git fetch.** Local refs were read with `git rev-parse`. No SSH or Git configuration was changed.

## Release identity (observed now)

- Release: https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.6.0
- Title: "v0.6.0 — Day 6 Helm routing and ambient mesh".
- Published: 2026-09-25T14:04:40Z; neither draft nor prerelease.
- Annotated tag object: `186855d77662d31c0b2a68a9934bb3664793aa83` (tagged
  2026-09-25T14:04:17Z).
- Tag target (peeled), remote and local: `19d6b28b1282aedc8417a5a2ba10e74614afe244`.
- Remote `main` (GitHub API) and local `main`: `19d6b28b1282aedc8417a5a2ba10e74614afe244`.

The annotated `v0.6.0` tag resolves to PR #8's merge commit.

## Pull requests (observed now)

| PR | Merged (UTC) | Merge commit | Head | Purpose |
|---|---|---|---|---|
| [#6](https://github.com/raiyan10/maops-kubernetes-platform/pull/6) | 2026-09-25T06:29:41Z | `ca729f2555f8fdcf5b58e9dcc1d3c878f3c38c54` | `feature/day-6-helm-routing-mesh` @ `fce264495516059cc5b64e61904e955b6ce0df86` | Day 6 implementation: Helm, Gateway API routing, Istio ambient mesh |
| [#7](https://github.com/raiyan10/maops-kubernetes-platform/pull/7) | 2026-09-25T13:02:01Z | `090f7f0a8ec31e2829bfb136f51348d2058ae7e2` | `fix/day-6-ambient-listener-check` @ `d948cd7f6ca916121079805df661c2ddd65e5d1f` | Post-restart ambient listener check |
| [#8](https://github.com/raiyan10/maops-kubernetes-platform/pull/8) | 2026-09-25T13:57:07Z | `19d6b28b1282aedc8417a5a2ba10e74614afe244` | `feature/day-6-helm-routing-mesh` @ `4321cd30efbeff962971671400caaed03661a247` | Release-gate closure documentation |

## Continuous integration (observed now)

The cluster-free `CI` workflow (`make ci-check`) completed successfully on each PR head and on
the released commit:

| Run | Trigger | Commit | Result |
|---|---|---|---|
| 36102151224 | PR #6 | `fce2644` | success |
| 36137435760 | PR #7 | `d948cd7` | success |
| 36142748065 | PR #8 | `4321cd3` | success |
| 36144261274 | push to `main` | `19d6b28` | success ("Static validation (cluster-free)") |

The first CI run for PR #6 (run 35990323938, commit `62c69a1`) **failed**: its runner had Helm
3.16.4 and no YAML parser that the checksum tests needed. The fix is recorded in
`day-06-remediation-log.md` ("CI failure on PR #6 and fix").

Local `make ci-check` on the released tree (recorded earlier, 2026-09-25): 1228 unit tests OK,
version-check 50/50, manifest-check 267/267, helm-check 215/215.

## Merged-main live gate (recorded earlier; log re-read now)

After PR #7 merged, the read-only gate was run on merged `main` against the existing
`maops-k8s-day6` kind cluster. No Pod was recreated and no mutating check was run. The log is
kept outside the repository, at `$HOME/.local/state/maops-k8s-day6/pr7-merged-main-live.log`.
It was re-read for this record, and its summary lines match:

| Check | Result |
|---|---|
| `context-check` | 6/6 |
| `cni-status` | 4/4 |
| `mesh-status` | 4/4 |
| `ambient-workload-check` | 67/67 |
| `rollout-check` | 35/35 |
| `gateway-check` | 8/8 |
| `smoke` | 6/6 |
| `final-state-check` | 43/43, including "suite-level state baseline restored" for run `979a1e7e72e9418199b0486cf81a920e` |

The log has no `[FAIL]` lines. Its SHA256 is
`620ffb572b4cdd6b686f214de9768b22c3081b6533a0f15d5cefa3539f5a9999`.

**Suite baseline (observed now):** `suite-baseline-979a1e7e72e9418199b0486cf81a920e.json`, in
the same private directory (mode 0600). Its identity fields read: `run_id`
`979a1e7e72e9418199b0486cf81a920e`, context `kind-maops-k8s-day6`, namespace `maops-platform`,
PVC UID `6c5fdacc-090a-4208-9b52-9c594211a982`, PV UID `df840301-f5f1-4d8b-9d70-63597612e2fe`,
`captured_at` 2026-09-24T05:47:26Z, value `null`. The file's modification time matches that
capture time. Its current SHA256 is
`e2debedd9ae74938a97c8c105129336b3e2b56901380fe1cdd271b232eb39fd2`; no earlier hash was
recorded to compare against.

## Validation history (recorded earlier)

The full record is in `docs/architecture.md` ("DAY6: live validation record" and "DAY6:
post-restart ambient listener incident (2026-09-25)"), `day-06-remediation-log.md`, and
`day-06-final-adjudication.md`. In summary:

1. **2026-09-22 to 2026-09-23, staged live run:** `networkpolicy-check` 37/37, `mesh-check`
   45/45, `persistence-check` 12/12, `retention-check` 22/22, corrected `helm-lifecycle-check`
   24/24, `final-state-check` 43/43.
2. **2026-09-24, independent review:** five independent reviews; adjudicated REMEDIATION
   REQUIRED, then remediated.
3. **2026-09-24, post-reboot recheck — failed attempt:** `final-state-check` scored **42/43**.
   The run's suite-baseline file under `/tmp` had been cleared by a host reboot, so that one
   item failed closed. **That baseline was lost and was never recreated or recaptured**; the
   42/43 stands as the result of that attempt.
4. **2026-09-24, fresh baseline-bracketed run:** a new run (`979a1e7e72e9418199b0486cf81a920e`),
   with its baseline kept outside `/tmp`. Its preserved logs were re-read now: `state-check`
   24/24, `persistence-check` 12/12, `retention-check` 22/22, `final-state-check` 43/43. The
   adjudication was then RELEASE READY.
5. **2026-09-25, PR #6 merged.**
6. **2026-09-25, post-restart incident:** after a WSL/Kind component restart,
   `rollout-check` failed 25/35. `maops-state-0` was Kubernetes Ready without its ambient in-Pod
   listeners (15001/15006/15008); one gateway Pod (`maops-gateway-7d59b678df-f88mj`) was
   unready without them. Only those two Pods were recreated. The state PVC and PV UIDs were
   preserved, and the state Pod UID changed from `aa92aa2c-…` to `89abf805-…`. The release
   gate re-opened. A short recovery-wait log from that period is kept alongside the other logs
   (`merged-main-recovery-live.log`): app rolled out, and the gateway rollout timed out before
   the gateway Pod was replaced. The subsequent gate log (`merged-main-after-ambient-recovery.log`)
   shows 6/6, 4/4, 4/4, 35/35, 8/8, 6/6, and 43/43.
7. **2026-09-25, PR #7:** added the read-only `make ambient-workload-check`. It checks expected
   in-Pod listeners plus Pod/namespace metadata. It does not by itself prove redirection,
   HBONE/mTLS traffic, or AuthorizationPolicy behavior; `mesh-check` and the other live traffic
   tests remain the evidence for those.
8. **2026-09-25:** merged-`main` gate passed (above); PR #8 closed the release gate; `v0.6.0`
   tagged and published.

**Cause of the lost listeners:** not proven. The missing listeners were observed directly; the
mechanism by which they were lost across the restart was not established. Recovery after a
host, Docker, or WSL restart varies in this local environment. The documented post-restart
gate order is `cni-status`, `context-check`, `mesh-status`, `ambient-workload-check`, then
`rollout-check`.

## Adjudication and scope

Final adjudication: **RELEASE READY** as a local kind reference platform (2026-09-25, after
PR #7 and the merged-`main` gate). Earlier verdicts, including the 2026-09-24 REMEDIATION
REQUIRED and the 2026-09-25 GATE PENDING addendum, are preserved in
`day-06-final-adjudication.md`.

**Not claimed:** production readiness; HA (single istiod replica with autoscaling disabled,
single Cilium operator); TLS/cert-manager, a cloud LoadBalancer, or an observability stack;
L7 east-west authorization (no waypoint); uniform recovery after host, Docker, or WSL
restarts; a proven cause for the lost listeners; live-cluster validation in GitHub Actions (CI
is cluster-free by design).

## Disposition

Day 6 is released as `v0.6.0`, a local kind reference platform, and its implementation is
frozen. `v0.6.0` remains permanently on `19d6b28b1282aedc8417a5a2ba10e74614afe244`; this
documentation follows the release and does not move the tag.

Day 7 (`v1.0.0`) remains responsible for the Recreate, Blue-Green, and Canary demonstrations.

## Screenshots

Complete. Captured after release; the release tag is unchanged.

- [Published v0.6.0 GitHub Release](../images/day-06/01-github-release-v0.6.0.png) — the
  release page titled "v0.6.0 — Day 6 Helm routing and ambient mesh", marked **Latest**, with tag
  `v0.6.0` on commit `19d6b28` (green check), and release notes listing the CI and
  merged-`main` gate results.
- [Recorded merged-main live gate](../images/day-06/02-merged-main-live-gate.png) — terminal
  output from re-reading the saved `pr7-merged-main-live.log`, whose SHA256 (`620ffb57…a9999`)
  matches the value recorded above. It shows the summary lines context 6/6, CNI 4/4, mesh 4/4,
  ambient workload 67/67, rollout 35/35, Gateway API 8/8, smoke 6/6, and final-state 43/43. The
  separate suite-baseline line is not in the frame; the baseline match is verified in the log
  text (see "Merged-main live gate" above).

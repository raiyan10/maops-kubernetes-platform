# Day 6 / v0.6.0 — Final Adjudication

**Adjudicator:** primary session (Claude Code), synthesizing five independent reviews
(`kubernetes-architect`, `kubernetes-security-reviewer`, `cluster-integration-engineer`,
`kubernetes-test-engineer`, `release-engineer`). Each ran as a fresh subagent, in parallel,
read-only, and was never shown another reviewer's conclusions before all five reports were
complete.

**Date:** 2026-09-24. **Branch:** `feature/day-6-helm-routing-mesh`. **HEAD:**
`3b784a78fa41b807a509a32e7028c67bf0a28777` (complete unstaged Day 6 diff).

**Basis:** the five review documents under `docs/engineering-reviews/day-06-*.md`, plus the
adjudicator's own read-only spot-checks listed below. The review round itself edited no file and
changed nothing in the cluster.

---

## Preflight (confirmed before reviews were dispatched)

- HEAD matched `3b784a7…`; `git diff --cached --name-only` empty; `git diff --check` clean;
  26 tracked files modified, 60 untracked.

## Adjudicator's own verification (read-only, after all five reports)

- Stale "not yet executed / deferred / static validation only" wording confirmed in the working
  tree: `README.md:87,94,99,227`; `docs/architecture.md:1,1266,1548,2409`; `docs/roadmap.md:244`;
  `Makefile:306,309,342`; `authorizationpolicy-gateway.yaml:15`;
  `cilium-ambient-probe-policy.yaml:55`; `gateway-values-configmap.yaml:53`;
  `scripts/gateway_check.py:10`, `mesh_status.py:9`, `mesh_check.py:16`; `.claude/skills/
  kind-cluster-validation/SKILL.md:10`, `workload-security-validation/SKILL.md:257`;
  `.claude/agents/cluster-integration-engineer.md:16`, `kubernetes-security-reviewer.md:3`.
  Not stale: `.claude/skills/release-readiness/SKILL.md:52,76` (generic conditional guidance).
- `docs/architecture.md:36` references a non-existent "DAY6: released validation record".
- No test exercises the `RESTORATION FAILURE` message.
- `gateway-values-configmap.yaml:27` `NET_BIND_SERVICE` text as SEC-4 describes.
- The Makefile sets no istiod autoscaling option.
- `kind get clusters` lists `maops-k8s-day6`; its three containers were running.

## Consolidated, deduplicated findings

| # | Severity | Finding | Sources | Change type |
|---|---|---|---|---|
| F1 | **HIGH** (release-gating) | Day 6 docs, Makefile help, chart/`k8s/day6` comments, script docstrings, and `.claude` agents/skills still claim "not yet executed / live validation deferred / static validation only", contradicting the live-run narrative in `architecture.md` and the cluster's own history | ARCH-1, SEC-1, INT-1 (part), REL-1, REL-3 | Documentation/comments |
| F2 | MEDIUM | Dangling "DAY6: released validation record" reference; deferral to a non-existent remediation report; no live counts recorded anywhere | INT-1 (part), REL-2 | Documentation |
| F3 | MEDIUM | No `main()` tests for rollback returning nonzero, or for a post-upgrade exception reaching the `finally` rollback | TEST-1 | Code (tests) |
| F4 | MEDIUM | istiod chart creates a default HPA that permanently fails without metrics-server; unchecked and undocumented | INT-2 | Code (Makefile) or documentation |
| F5 | LOW | Peerless HBONE 15008 justified only in the template comment, not in the architecture trust-boundary summary | SEC-2 | Documentation |
| F6 | LOW | Ambient-probe CiliumClusterwideNetworkPolicy has no automated regression check | SEC-3 | Code |
| F7 | LOW | No orchestration-level test for an isolation-gate failure in the NetworkPolicy check | TEST-2 | Code (tests) |
| F8 | LOW | `NET_BIND_SERVICE` comment inaccurate; the real control is stricter | SEC-4 | Documentation |
| F9 | NOTE | Mesh-check denial evidence tiers not broken out in any live record | TEST-3 | Documentation |
| F10 | NOTE | A post-restart self-heal (no Pod replacement) was also observed | INT-3 | Documentation |
| F11 | NOTE | GitHub Actions pinned to tags, not SHAs | REL-4 | Optional |
| F12 | NOTE | Positive: HBONE + AuthorizationPolicy layering is well reasoned | ARCH-2 | None |

**Counts:** BLOCKER 0 · HIGH 1 · MEDIUM 3 · LOW 4 · NOTE 4.

- **Require code changes:** F3, F6, F7 (test/validator additions only); F4 (a Makefile flag, or
  documentation); F11 optional.
- **Documentation only:** F1, F2, F5, F8, F9, F10 (F4 could also be documentation-only).

## Disagreements between reviewers

1. **Severity of the stale documentation.** ARCH and SEC rated it HIGH; INT and REL rated it
   BLOCKER. **Adjudicated HIGH, release-gating:** purely a documentation defect with no design or
   runtime defect behind it, and the fix is mechanical — but no tag until it is fixed.
2. **Verdicts.** ARCH and SEC gave PASS WITH NOTES while withholding release; INT and REL gave
   FAIL; TEST said release may proceed. All converge once F1 is fixed.
3. **Live cluster reachability.** ARCH reported no live cluster; INT and SEC reached
   `maops-k8s-day6` read-only.
4. **Whether an evidence record must exist before the commit** (REL-2) — see below.

## Rejected or narrowed findings (with reasons)

- **ARCH-1's "no live cluster reachable / cannot verify" basis — rejected.** An environment
  misread: the reviewer used the default kubectl context (`kind-maops-k8s-day3`, not running).
  `kind get clusters` lists `maops-k8s-day6`, its three containers were up, and two other reviewers
  read it directly through its own kubeconfig. ARCH-1's stale-documentation substance stands as F1.
- **REL-1's claims that live contact "cannot be determined" and that the live evidence was
  falsified — rejected.** INT and SEC independently observed: Helm history revisions 1-7 (a failed
  pre-`fsGroup` revision 1; an upgrade and "Rollback to 5"); PVC/PV UIDs exactly matching the
  claimed values; the full security object set live; `RUST_LOG=info` restored; no leaked probes.
  Live validation clearly happened; the defect is that the documentation did not say so. (No
  reviewer re-ran the numbered live checks — kept as a limitation.)
- **REL-2 as a BLOCKER requiring a Day 6 evidence record before commit — narrowed to F2
  (MEDIUM).** Days 1-5 committed their reviews before merge and post-release evidence after
  release; this round was also explicitly barred from creating review files. What is required
  before tagging is fixing the dangling cross-reference and recording the live results.
- **REL-5 ("live `rollout restart` output bled in from a parallel reviewer") — rejected.** TEST and
  SEC both confirmed the lines are `tests/test_workload_refresh.py`'s own printed output under
  mocked `kube`/`run`; no reviewer changed the cluster.
- **SEC-2 severity MEDIUM — narrowed to LOW (F5).** The "istio-system only" expectation came from
  the reviewer's checklist, not this project's contract; the peerless design is deliberate,
  justified, and compensated. Only its documentation was missing.

## Remaining limitations at adjudication time

- No reviewer re-ran networkpolicy (37/37), mesh (45/45), persistence (12/12), retention (22/22),
  helm-lifecycle (24/24), or final-state (43/43); corroborated indirectly.
- The cluster restarted shortly before the reviews (host/Docker); final-state had not been re-run
  since.
- Restart recovery varies; local Kind reference platform only (no HA, cloud LB, or TLS).

## Original verdict

**REMEDIATION REQUIRED.** Code, chart, security controls, and runtime state pass; commit/tag must
wait for F1 and F2 (with F9's tier breakdown), followed by static re-checks and a
post-restart final-state recheck.

---
---

## Post-remediation status (2026-09-24) — closure NOT granted

> This section was appended after the remediation and targeted re-validation recorded in
> `day-06-remediation-log.md`. It is deliberately **not** titled a closure: one validation step
> could not succeed, for the reason given under NEW-1.

### Disposition of each finding

| # | Disposition | Evidence |
|---|---|---|
| F1 | **Fixed** | Every stale occurrence reclassified and rewritten (list in the remediation log); final stale-claim search below |
| F2 | **Fixed** | `## DAY6: live validation record` added to `docs/architecture.md`, linked from README and roadmap; dangling reference and non-existent-report references removed; this file + remediation log exist |
| F3 | **Fixed** | 3 new `main()` tests (rollback nonzero; exception → `finally` rollback; exception + failed rollback → `RESTORATION FAILURE`); no runtime change |
| F4 | **Fixed** | `--set pilot.autoscaleEnabled=false` (verified against the pinned istiod 1.31.0 chart); 3 Makefile tests; live: `make mesh-install` exit 0, `kubectl get hpa -A` → none, istiod `replicas: 1` |
| F5 | **Fixed** | "Accepted trust boundary: peerless TCP 15008" in `docs/architecture.md`; no policy change |
| F6 | **Fixed** | `scripts/validate_cilium_probe_policy.py` (13 checks) wired into `make helm-check` / `ci-check`; 28 positive/negative tests |
| F7 | **Fixed** | 2 orchestration-level tests (real isolation gate; EndpointSlice gate) |
| F8 | **Fixed** | Comment corrected after live read-only confirmation (drop ALL + `ip_unprivileged_port_start=0`); data unchanged |
| F9 | **Fixed** | Tier breakdown recorded: 3 AUTHORITATIVE, CANDIDATE 0, BEST_EFFORT 0 (2026-09-23 and 2026-09-24 runs) |
| F10 | **Fixed** | Both restart outcomes documented; no ztunnel-defect claim |
| F11 | **Accepted (deferred)** | No SHA-pinning convention exists; recorded as a non-blocking supply-chain limitation |
| F12 | No action | — |

### Exact validation evidence

- **Static (2026-09-24):** 1197 unit tests OK (was 1161, +36); version-check 50/50;
  manifest-check 267/267; helm-lint pass; helm-template pass, 30 objects; helm-check 215/215;
  ci-check PASS; `git diff --check` clean; zero Secret/Ingress/ClusterRole/waypoint objects;
  frozen files and Day 1-5 review files unchanged; nothing staged.
- **Live (2026-09-24):** pre-check all Ready; mesh-install exit 0 with HPA removed; cni-status 4/4;
  context-check 6/6; mesh-status 4/4; rollout-check 35/35; gateway-check 8/8; smoke 6/6;
  mesh-check 45/45 (3 AUTHORITATIVE / 0 CANDIDATE / 0 BEST_EFFORT); mesh-probe namespace absent;
  `RUST_LOG=info`; ztunnel 3/3; APP_MESSAGE normal; Gateway/HTTPRoute accepted and
  programmed/resolved; PVC/PV UIDs unchanged; no probe leaks.
- **final-state-check (explicit `DAY6_RUN_ID=9e269203903e42f58f3eaabe1179cbc4` and the run's
  baseline path): 42/43 — FAIL.**

### NEW-1 (BLOCKING, evidence gap) — suite baseline destroyed by host reboot

The run's suite-baseline JSON under `/tmp` no longer exists: the WSL host rebooted at 09:05 on
2026-09-24 and `/tmp` was cleared (not by this pass). The required existence/mode/identity
pre-check could not be performed, and `final-state-check` failed closed on exactly that item
(42/43; every other restoration check passed). The baseline was **not** recaptured — a new
capture would compare the current `/state` value to itself and prove nothing about the earlier
mutating experiments.

The last verified suite-level restoration remains the 2026-09-23 `final-state-check` 43/43.
Closing NEW-1 is an operator decision, e.g.:

1. accept the 2026-09-23 43/43 as the suite-restoration evidence and record the 2026-09-24
   42/43 (baseline file lost to reboot, every other restoration item passing) as a disclosed
   limitation; or
2. run a new, documented baseline-bracketed sequence (`state-check` → the mutating checks →
   `final-state-check`) under a fresh `DAY6_RUN_ID`, which re-runs mutating experiments this
   pass was told not to re-run.

### Remaining accepted limitations

Local kind reference platform only (no HA — single istiod replica with autoscaling disabled,
single Cilium operator; no TLS/cert-manager, cloud LB, or observability stack); peerless HBONE
15008 (compensated); IPv4-only ambient-probe policy; non-AUTHORITATIVE ztunnel denial shapes stay
CANDIDATE/BEST_EFFORT; restart recovery varies and a reboot clears any `/tmp` baseline; Actions
pinned to tags; CI cluster-free by design.

### Final verdict

**REMEDIATION REQUIRED** — all twelve review findings are dispositioned (ten fixed, one accepted,
one no-action), but NEW-1 leaves the required post-restart final-state recheck at 42/43. The
branch should not be tagged until the operator resolves NEW-1 by one of the options above.
Nothing is staged, committed, pushed, or tagged.

---
---

## NEW-1 closure (2026-09-24, 11:47–11:51 +06) — fresh baseline-bracketed state run

> Appended after the "Post-remediation status" section above, which is preserved unchanged as
> the honest record of the earlier 42/43 attempt. The lost 2026-09-23 baseline was **not**
> recreated, and the earlier 2026-09-24 `final-state-check` is **not** re-labelled as passing — it
> scored 42/43 because the host reboot cleared `/tmp`.

### Method

A fresh, uninterrupted state → mutation → final-state bracket, with the same explicit
`DAY6_RUN_ID` and `DAY6_SUITE_BASELINE_PATH` passed as Make command-line variables to every step:

- **Run ID:** `979a1e7e72e9418199b0486cf81a920e` (fresh `uuid4().hex`).
- **Baseline location:** `$HOME/.local/state/maops-k8s-day6/suite-baseline-<run-id>.json` — a
  persistent, private directory outside the repository and outside `/tmp` (directory mode 0700,
  file mode 0600, created by `state-check` via `O_EXCL`; no existing file was overwritten). The
  run's logs are preserved alongside it (`run-<run-id>-logs/`, mode 0700/0600).

### Pre-mutation guards (all passed)

Branch `feature/day-6-helm-routing-mesh`; HEAD `3b784a7…`; empty index; empty frozen-source diff
(`k8s/base/`, `kind/cluster.yaml`, `kind/cluster-day5.yaml`, `scripts/day4_lock.py`,
`scripts/day5_lock.py`, Day 1-5 review files); Docker and API `/readyz` reachable; nodes 3/3
Ready; Cilium 3/3; cilium-operator 1/1; istiod available; istio-cni-node 3/3; ztunnel 3/3;
gateway 3/3; app 3/3; state 1/1; PVC `Bound`; PVC UID `6c5fdacc-090a-4208-9b52-9c594211a982`;
PV UID `df840301-f5f1-4d8b-9d70-63597612e2fe`; no HPA; no kubectl process running. No Pod was
replaced.

### Sequence and results

| Step | Result |
|---|---|
| `state-check` | 24/24; baseline captured (`value=None`) |
| Baseline verification (before any mutation) | exists; mode 0600 in a 0700 directory; `run_id`, `context` (`kind-maops-k8s-day6`), `namespace` (`maops-platform`), `namespace_uid`, `pvc_uid`, `pv_uid` all match |
| `persistence-check` | **12/12** |
| Gate | gateway 3/3, app 3/3, state 1/1; PVC/PV UIDs unchanged; `smoke` 6/6 with `/state` HTTP 200 `{"value": null}` |
| `retention-check` | **22/22** |
| Gate | same as above; `smoke` 6/6, `/state` 200 `{"value": null}` |
| `final-state-check` | **43/43** — including "suite-level state baseline restored: independent GET /state matches the run baseline … (value=None, expected None)"; baseline file byte-unchanged afterwards |

**Post-run read-only gates:** cni-status 4/4; context-check 6/6; mesh-status 4/4; rollout-check
35/35; gateway-check 8/8; smoke 6/6. No mesh-probe namespace, no NetworkPolicy probe Pods, empty
validation namespace, no HPA, no kubectl process; ztunnel `RUST_LOG=info`, 3/3; `APP_MESSAGE` is
the chart default; PVC/PV UIDs unchanged; nothing staged.

**Not re-run (per instruction):** scaling, rolling-update, PDB, Helm lifecycle, NetworkPolicy.
Their last recorded live results stand as in `docs/architecture.md`'s live validation record.

### Residual note (non-blocking, not fixed in this pass)

- **NOTE:** `scripts/final_state_check.py`'s pass message still says the baseline was "captured
  before any **Day 4** mutating experiment" — a cosmetic label inherited from Day 4 (the check
  itself is the Day 6 contract). Left unchanged so the recorded run output and the code match;
  suitable for a follow-up wording fix.

### NEW-1 disposition

**Closed.** A genuine baseline captured before the two mutating state experiments was restored
exactly (43/43), with storage identity preserved.

### Final verdict

**RELEASE READY** — all twelve review findings dispositioned (ten fixed, one accepted, one
no-action), NEW-1 closed by the fresh bracketed run, static and targeted live validation passing.
This is readiness of a validated **local kind reference platform**, not a production-readiness
claim. Staging, commit, tag, and release remain the operator's explicit decisions; nothing has
been staged, committed, pushed, or tagged.

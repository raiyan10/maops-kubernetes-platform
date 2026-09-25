# Day 6 / v0.6.0 — Review Remediation Log

This log records every change made on 2026-09-24 in response to the five independent Day 6
reviews (`day-06-kubernetes-architecture-review.md`, `day-06-kubernetes-security-review.md`,
`day-06-cluster-integration-review.md`, `day-06-kubernetes-test-review.md`,
`day-06-release-readiness-review.md`) and their adjudication (`day-06-final-adjudication.md`),
followed by the targeted re-validation that was actually run. No check was weakened to make it
pass; no cluster was recreated, no image rebuilt, no Secret rotated or printed.

Branch `feature/day-6-helm-routing-mesh`, HEAD `3b784a78fa41b807a509a32e7028c67bf0a28777`; all
work remains unstaged and uncommitted.

---

## Remediated

### F1 (HIGH) — stale "not yet executed / deferred / static-only" wording (ARCH-1, SEC-1, INT-1, REL-1, REL-3)

Every occurrence was classified first. Stale execution-state claims were rewritten; wording that is
still true ("not yet released/tagged") and generic conditional guidance ("if the task scope
authorized static validation only…") were kept. Execution-state text inside reusable
scripts/templates was replaced with timeless technical wording, never "the latest run passed".

- `docs/architecture.md`: title; intro; the gateway-check pointer; the "Scope of the first live
  run" paragraph (now "What live validation confirms"); "DAY6: deferred live validation sequence"
  renamed "DAY6: live validation sequence"; every "see this pass's own remediation report" /
  "this batch's own final report" reference replaced with the actual outcome or a pointer to the
  new record; the "Day 6 completion is NOT claimed…" paragraph; "What Day 6 proves" rewritten into
  proven-statically / proven-live / explicitly-not-claimed; two "this static-only pass" passages
  in the historical fourth-remediation section reworded/annotated as superseded by the live
  mesh-check remediation.
- `README.md`: headline status; the "Day 6 status" section (now "validated on local kind, not yet
  released"); quick-start heading; file-tree `VERSION`/`docs/` lines. Also fixed the quick-start
  order, which still ran `context-check` before the CNI existed — the same ordering defect the
  Makefile had already been fixed for.
- `docs/roadmap.md`: Day 6 status line and the "Deferred, not skipped" paragraph (now "Live
  validation passed", with a pointer to the record).
- `Makefile`: `gateway-check`, `mesh-check`, `helm-lifecycle-check` help text.
- `charts/maops-kubernetes-platform/templates/authorizationpolicy-gateway.yaml`: comment only.
- `k8s/day6/cilium-ambient-probe-policy.yaml`: "ASSUMPTION FLAGGED FOR LIVE VERIFICATION" header
  replaced with a VERIFICATION note (static validator + runtime effect). Comments only; the
  applied object is byte-identical in content (`kubectl apply` reported `unchanged`).
- `k8s/day6/gateway-values-configmap.yaml`: "LIVE VALIDATION SCOPE (not yet executed…)" header
  replaced with a timeless schema-vs-rendered-behavior note. Comments only.
- `scripts/gateway_check.py`, `scripts/mesh_status.py`, `scripts/mesh_check.py`: module
  docstrings; `mesh_check.py` also had three superseded "static-only pass" passages corrected
  (one described the pre-live CANDIDATE-only denial model that the fifth remediation replaced).
- `tests/test_mesh_check.py`: docstring ("(not yet run) live script").
- `.claude/agents/cluster-integration-engineer.md`, `kubernetes-architect.md`,
  `kubernetes-security-reviewer.md`; `.claude/skills/workload-security-validation/SKILL.md`,
  `.claude/skills/kind-cluster-validation/SKILL.md`: Day 6 status lines.

### F2 (MEDIUM) — dangling "DAY6: released validation record" and missing remediation report (INT-1, REL-2)

- Added `## DAY6: live validation record` to `docs/architecture.md` (static and live results,
  dates, how results were obtained, mesh denial-evidence tiers, Helm revision history, restart
  recovery, pointers to these review files). The intro now points to it by its real name.
- Linked it from `README.md` and `docs/roadmap.md`.
- This file and `day-06-final-adjudication.md` are the review/remediation records the
  architecture document refers to.

### F3 (MEDIUM) — lifecycle restoration-failure paths untested (TEST-1)

`tests/test_helm_lifecycle_check.py`, three new `MainOrchestrationTests`:
- `test_rollback_nonzero_after_submitted_upgrade_records_restoration_failure` — rollback returns
  1 after a converged upgrade: exit 1, exactly one rollback, `RESTORATION FAILURE` recorded, the
  rollback stderr surfaced, no post-rollback proof run, no passing "restored"/"end to end" finding,
  no `PASS:` printed.
- `test_exception_after_upgrade_submission_still_reaches_finally_rollback` — the post-upgrade
  divergence proof raises: rollback still attempted exactly once (to revision 2), the exception
  propagates (nonzero process exit), no `PASS:`.
- `test_exception_after_upgrade_with_failed_rollback_records_restoration_failure` — exception
  plus failing rollback: `RESTORATION FAILURE` recorded before the exception propagates; no
  passing rollback finding.
- No runtime behavior changed; no bug was found.

### F7 (LOW) — NetworkPolicy isolation-gate failure untested at orchestration level (TEST-2)

`tests/test_networkpolicy_check.py`, two new `CheckApplicationPortNetworkPolicyIsolatedTests`:
- `test_ambient_redirected_source_pod_is_never_probed_and_fails_closed` — drives the **real**
  `_verify_probe_pod_isolated()` with the gateway-source Pod carrying the ambient redirection
  annotation: no probe ever runs from it, both gateway assertions are recorded "unavailable", no
  passing gateway DENIED finding, overall fail, all four Pods cleaned up and verified gone.
- `test_target_ip_present_in_service_endpointslice_is_never_probed` — the EndpointSlice gate
  fails for the state target: no snippet targets its IP, no passing state-probe assertion,
  overall fail, all four Pods cleaned up.

### F6 (LOW) — no automated guard for the ambient-probe CiliumClusterwideNetworkPolicy (SEC-3)

- New `scripts/validate_cilium_probe_policy.py` (dependency-free, parsed with
  `scripts/k8s_yaml.py`, never contacts a cluster). 13 checks: single document; apiVersion
  `cilium.io/v2`; kind `CiliumClusterwideNetworkPolicy`; name
  `maops-allow-ambient-health-probes`; spec keys exactly `description`/`endpointSelector`/`ingress`
  (no egress, egressDeny, ingressDeny, nodeSelector, specs); endpointSelector exactly the
  `maops-platform` namespace label + project `part-of` label (no wildcard, no matchExpressions);
  exactly one ingress rule; rule keys exactly `fromCIDR`/`toPorts`; `fromCIDR` exactly
  `169.254.7.127/32`; IPv4-only CIDRs; one `toPorts` entry with `ports` only (no L7 `rules`);
  ports exactly TCP 8080; `spec.description` states the IPv4-only scope.
- Wired into `scripts/helm_check.py`, so it runs in `make helm-check` and therefore
  `make ci-check` / GitHub Actions (helm-check 202 → 215).
- New `tests/test_validate_cilium_probe_policy.py` (28 tests): the real tracked file passes; one
  targeted mutation per rule (wrong apiVersion, namespaced kind, wrong name, second document,
  unparseable input, wildcard selector, dropped namespace label, dropped part-of label, other
  namespace, extra matchExpressions, broader CIDR, `0.0.0.0/0`, additional CIDR, IPv6 CIDR, UDP,
  ANY, other port, extra port, L7 rules, egress, ingressDeny, extra ingress rule, extra peer type,
  missing IPv4-only description), each asserting the specific check that must fail; a
  cluster-free import guard.

### F4 (MEDIUM) — istiod HPA with permanently unknown metrics (INT-2)

- Verified the key against the pinned chart (`helm pull istio/istiod --version 1.31.0` into a
  scratch directory, then `helm template`): the chart's `values.yaml` default is
  `autoscaleEnabled: true`; `templates/autoscale.yaml` renders the HPA only when
  `autoscaleEnabled`; `templates/zzy_descope_legacy.yaml` merges `pilot.*` onto the top-level
  values. Rendering with `--set pilot.autoscaleEnabled=false` produced no HPA and `replicas: 1`;
  without it, an `autoscaling/v2` HPA `istiod` is rendered. The existing `pilot.resources.*` flags
  were confirmed to take effect through the same mechanism (rendered 100m/128Mi requests,
  500m/512Mi limits).
- `Makefile` `mesh-install`: added `--set pilot.autoscaleEnabled=false` to the istiod install;
  help text updated.
- `tests/test_makefile_sequence.py`: new `IstiodAutoscalingDisabledTests` (3 tests) — the flag is
  on the istiod install; autoscaling is never enabled or tuned anywhere in `mesh-install`; the flag
  appears exactly once, scoped to istiod.
- `docs/architecture.md` ("resource-conscious, explicitly non-HA"): application autoscaling stays
  out of scope; no metrics-server; istiod autoscaling explicitly disabled for this local topology;
  not a recommended production Istio availability configuration.

### F5 (LOW) — peerless HBONE rationale missing from the trust-boundary summary (SEC-2)

`docs/architecture.md`: new "Accepted trust boundary: peerless TCP 15008" block — host-networked
ztunnel cannot be selected as a namespaced peer; Cilium provides transport reachability; STRICT
mTLS + exact-principal AuthorizationPolicies enforce workload identity; application-port
NetworkPolicies remain relevant to plaintext/non-ambient paths; accepted defense-in-depth design
boundary, not an accidental application allow. No policy changed.

### F8 (LOW) — inaccurate `NET_BIND_SERVICE` comment (SEC-4)

Confirmed live (read-only) before editing: the generated `maops-edge-istio` Pod has Pod-level
sysctl `net.ipv4.ip_unprivileged_port_start=0`; `istio-proxy` drops ALL capabilities with none
added, `allowPrivilegeEscalation: false`, read-only root filesystem, non-root 1337:1337.
`k8s/day6/gateway-values-configmap.yaml`'s comment now describes that mechanism; the ConfigMap's
data (and the live security context) are unchanged. The same correction is reflected in
`docs/architecture.md`.

### F9 (NOTE) — mesh evidence tiers not broken out (TEST-3)

The live record in `docs/architecture.md` and this log now break out the tiers for the three
gating identity-denial assertions (see below).

### F10 (NOTE) — restart recovery varies (INT-3)

`docs/architecture.md` records both observed restart outcomes, states that neither proves a
ztunnel defect, and requires the bounded readiness gates before validating after a restart.

## Accepted, not remediated

- **F11 / REL-4 (LOW) — GitHub Actions pinned to major tags, not commit SHAs.** The repository has
  no SHA-pinning convention; changing it would expand this pass into dependency maintenance.
  Recorded as an accepted, non-blocking supply-chain limitation for follow-up.

---

## Files changed in this pass

Modified (already part of the unstaged Day 6 diff):
`Makefile`, `README.md`, `docs/architecture.md`, `docs/roadmap.md`,
`charts/maops-kubernetes-platform/templates/authorizationpolicy-gateway.yaml`,
`k8s/day6/cilium-ambient-probe-policy.yaml`, `k8s/day6/gateway-values-configmap.yaml`,
`scripts/gateway_check.py`, `scripts/mesh_status.py`, `scripts/mesh_check.py`,
`scripts/helm_check.py`, `tests/test_helm_lifecycle_check.py`,
`tests/test_networkpolicy_check.py`, `tests/test_makefile_sequence.py`,
`tests/test_mesh_check.py`, `.claude/agents/cluster-integration-engineer.md`,
`.claude/agents/kubernetes-architect.md`, `.claude/agents/kubernetes-security-reviewer.md`,
`.claude/skills/workload-security-validation/SKILL.md`,
`.claude/skills/kind-cluster-validation/SKILL.md`.

New:
`scripts/validate_cilium_probe_policy.py`, `tests/test_validate_cilium_probe_policy.py`,
`docs/engineering-reviews/day-06-kubernetes-architecture-review.md`,
`docs/engineering-reviews/day-06-kubernetes-security-review.md`,
`docs/engineering-reviews/day-06-cluster-integration-review.md`,
`docs/engineering-reviews/day-06-kubernetes-test-review.md`,
`docs/engineering-reviews/day-06-release-readiness-review.md`,
`docs/engineering-reviews/day-06-final-adjudication.md`,
`docs/engineering-reviews/day-06-remediation-log.md`.

## Test-count delta

1161 → **1197** (+36): lifecycle +3, NetworkPolicy +2, ambient-probe policy validator +28,
Makefile istiod autoscaling +3.

## Static results (2026-09-24, after remediation, before any live work)

| Check | Result |
|---|---|
| `python3 -m unittest discover -s tests -v` | 1197 tests, OK |
| `make version-check` | 50/50 |
| `make manifest-check` | 267/267 |
| `make helm-lint` | 1 chart linted, 0 failed |
| `make helm-template` | rendered; 30 objects |
| `make helm-check` | 215/215 (was 202; +13 ambient-probe policy checks) |
| `make ci-check` | PASS |
| `git diff --check` | clean |

Rendered inventory: 3 AuthorizationPolicy, 3 ConfigMap, 2 Deployment, 1 HTTPRoute,
8 NetworkPolicy, 1 PeerAuthentication, 2 PodDisruptionBudget, 1 Role, 1 RoleBinding, 4 Service,
3 ServiceAccount, 1 StatefulSet; zero Secret, Ingress, ClusterRole, ClusterRoleBinding, or
waypoint objects (the only "waypoint" text is a template comment).

## Targeted live results (2026-09-24, existing `maops-k8s-day6` cluster)

Pre-check (read-only), after the 09:05 host reboot: nodes 3/3 Ready; Cilium 3/3; cilium-operator
1/1; istiod 1/1; istio-cni-node 3/3; ztunnel 3/3; gateway 3/3; app 3/3; state 1/1; no Pod outside
Running. istiod HPA present (`cpu: <unknown>/80%`, min 1, max 5).

| Step | Result |
|---|---|
| `make mesh-install` | exit 0; istiod release revision 2 (`pilot.autoscaleEnabled: false` in user values); istio-cni and ztunnel upgraded with unchanged values; CCNP `unchanged`; no istiod/ztunnel/istio-cni Pod replaced |
| HPA removal | `kubectl get hpa -A`: **No resources found**; istiod `replicas: 1`, ready 1, resources unchanged (100m/128Mi requests, 500m/512Mi limits) |
| `make cni-status` | 4/4 |
| `make context-check` | 6/6 |
| `make mesh-status` | 4/4 |
| `make rollout-check` | 35/35 |
| `make gateway-check` | 8/8 |
| `make smoke` | 6/6 |
| `make mesh-check` | **45/45** |
| mesh-check cleanup | `maops-day6-mesh-probe` NotFound; ztunnel `RUST_LOG=info` (never mutated — default logs sufficed); ztunnel 3/3 Ready |
| `make DAY6_RUN_ID=9e269203903e42f58f3eaabe1179cbc4 DAY6_SUITE_BASELINE_PATH=<run baseline> final-state-check` | **42/43 — FAIL** (see below) |

**Mesh evidence tiers (this re-run):** the three gating wrong-identity denial assertions (→
gateway, → app, → state) were each AUTHORITATIVE correlated ztunnel denial evidence;
CANDIDATE = 0, BEST_EFFORT = 0. This matches the 2026-09-23 run's breakdown. The 45/45 is the
complete mesh-check tally, of which these are three assertions.

**Reconfirmed directly (read-only):** `APP_MESSAGE` = the chart default
("Hello from the MAOps Kubernetes Gateway (Day 6)"); Gateway `Accepted`/`Programmed`/
`ResolvedRefs` true; HTTPRoute `Accepted`/`ResolvedRefs` true; PVC UID
`6c5fdacc-090a-4208-9b52-9c594211a982` and PV UID `df840301-f5f1-4d8b-9d70-63597612e2fe`
unchanged; no NetworkPolicy probe Pods; no mesh-probe namespace; no port-forward processes;
nothing staged.

**Not re-run (per instruction; no new failure required them):** persistence-check,
retention-check, scaling-check, rolling-update-check, pdb-check, helm-lifecycle-check,
networkpolicy-check.

### Final-state recheck: suite baseline unverifiable (new blocking evidence gap)

The run's suite-baseline file (`DAY6_SUITE_BASELINE_PATH`, under `/tmp`) **no longer exists**. The
WSL host rebooted at 09:05 on 2026-09-24 (`uptime -s`), which cleared `/tmp` entirely — the same
restart the reviewers observed as the post-restart Pod/sandbox disturbance. Nothing in this pass
deleted it. Consequently:

- The required pre-check "the JSON exists, is mode 0600, and matches the run ID / context /
  namespace / storage identity" **could not be performed**.
- `final-state-check` failed closed exactly as designed: 42/43, the single failure being
  "suite-level state baseline verification failed: suite baseline file … not found or
  unreadable". The other 42 checks passed.
- "State value matches the preserved baseline" is therefore **not verified** in this pass.
- The baseline was deliberately **not** recaptured or recreated — a fresh capture now would compare
  the current value to itself and prove nothing about the mutating experiments it was meant to
  bracket.

The last verified suite-level restoration remains the 2026-09-23 `final-state-check` 43/43
recorded in `docs/architecture.md`'s live validation record. How to close this gap is an operator
decision (see `day-06-final-adjudication.md`).

## Restart-recovery limitation

Two host/Docker restarts were observed with different outcomes: one needed a single stale,
already-unready `maops-app` Pod replaced (connectivity then restored, including on the same
worker); the 2026-09-24 reboot self-healed through kubelet sandbox re-creation with no Pod
replacement. Neither proves a ztunnel defect. Restart recovery in this local
Kind/Docker/WSL environment varies; bounded readiness checks must precede validation, and a
host reboot also clears any `/tmp` suite baseline.

## Frozen-file confirmation

`git diff HEAD` is empty for `k8s/base/`, `kind/cluster.yaml`, `kind/cluster-day5.yaml`,
`scripts/day4_lock.py`, `scripts/day5_lock.py`, and every `docs/engineering-reviews/day-0[1-5]-*`
file.

## Remaining accepted limitations

- Local kind reference platform only: no HA (single istiod replica with autoscaling disabled,
  single Cilium operator), no TLS/cert-manager, no cloud LoadBalancer, no observability stack.
- Peerless HBONE 15008 (accepted, compensated trust boundary).
- Ambient-probe CCNP is IPv4-only (cluster is single-stack IPv4).
- ztunnel denial shapes other than the two observed AUTHORITATIVE strings remain
  CANDIDATE/BEST_EFFORT.
- Restart recovery varies (above).
- GitHub Actions pinned to major tags (F11).
- CI is cluster-free by design.

## Staging / commit / tag status

Nothing staged (`git diff --cached --name-only` empty). Nothing committed, pushed, or tagged.

---

## NEW-1 closure run (2026-09-24, 11:47–11:51 +06)

The 42/43 `final-state-check` recorded above (suite baseline lost to the 09:05 host reboot) is
preserved as an honest historical result; it was not re-run against a recreated file and is not
re-labelled as a pass. NEW-1 was closed instead by a fresh, uninterrupted baseline-bracketed run.

- **Contract verified first:** `scripts/suite_baseline.py` reads only `DAY6_RUN_ID` /
  `DAY6_SUITE_BASELINE_PATH`; `capture()` creates the file with `O_EXCL` and mode 0600 and never
  overwrites; it does not create directories. Make command-line variables override the
  Makefile's `:=` defaults and are exported to every child process.
- **Run ID:** `979a1e7e72e9418199b0486cf81a920e`.
- **Baseline:** `$HOME/.local/state/maops-k8s-day6/suite-baseline-<run-id>.json` (outside the
  repository and `/tmp`; directory 0700, file 0600); logs preserved in
  `$HOME/.local/state/maops-k8s-day6/run-<run-id>-logs/` (0700/0600; no secret-like content).
- **Guards:** all passed before mutation (see `day-06-final-adjudication.md`); no Pod replaced.

| Command (each with the same explicit `DAY6_RUN_ID` / `DAY6_SUITE_BASELINE_PATH`) | Result |
|---|---|
| `make … state-check` | 24/24; baseline captured, value `null`; verified 0600 + identity match before mutation |
| `make … persistence-check` | 12/12; gate: workloads Ready, UIDs unchanged, `/state` 200 |
| `make … retention-check` | 22/22; gate: workloads Ready, UIDs unchanged, `/state` 200 |
| `make … final-state-check` | **43/43**; suite baseline restored (`value=None`, expected `None`) |

Post-run read-only gates: cni-status 4/4, context-check 6/6, mesh-status 4/4, rollout-check
35/35, gateway-check 8/8, smoke 6/6; no leaked probe resources; PVC UID
`6c5fdacc-090a-4208-9b52-9c594211a982` and PV UID `df840301-f5f1-4d8b-9d70-63597612e2fe`
unchanged. Scaling, rolling-update, PDB, Helm lifecycle, and NetworkPolicy checks were not re-run.

Residual non-blocking NOTE: `final_state_check.py`'s pass message still says "before any Day 4
mutating experiment" (inherited cosmetic label; left as-is so the recorded output matches the code).

**Staging / commit / tag status:** nothing staged, committed, pushed, or tagged. Final verdict in
`day-06-final-adjudication.md`: **RELEASE READY** (local kind reference platform).

---

## Wording follow-up (2026-09-24, after the NEW-1 closure)

Closes the residual NOTE recorded above. The NEW-1 closure run's recorded output (43/43, run
`979a1e7e72e9418199b0486cf81a920e`) was produced by the previous wording and stands unchanged as
evidence; the September 24 42/43 attempt remains recorded above as history.

- **Guards (passed):** HEAD `3b784a78fa41b807a509a32e7028c67bf0a28777`; empty index; zero diff
  and no untracked files in the frozen Day 1-5 sources.
- **Change:** `scripts/final_state_check.py`, `check_suite_state_baseline_restored()` — the
  user-visible pass/fail message now reads "captured before any **Day 6** mutating experiment"
  (was "Day 4"). One string literal; the baseline loading, validation, and comparison logic are
  unchanged.
- **Tests:** no test asserts this message text, so no test was changed or added.
- **Verification:** `python3 -m unittest discover -s tests -v` → 1197 tests, OK; `make ci-check`
  → PASS (1197 tests OK, version-check 50/50, manifest-check 267/267, helm-check 215/215);
  `git diff --check` clean. No live check was re-run and the cluster was not touched.
- **Staging / commit / tag status:** nothing staged, committed, pushed, or tagged.

---

## Documentation audit clarification (2026-09-24, pre-commit)

A final documentation-only audit before the v0.6.0 commit found one item material to this log:
F1 above records the README quick-start ordering as fixed, but **two other operator-facing
sequences still carried the pre-CNI ordering defect** (`context-check` directly after
`cluster-create`, before any node can be Ready): the README's "Deploy lifecycle (order
matters)" block (which also omitted `cni-status`/`mesh-status`) and the
`.claude/skills/kind-cluster-validation/SKILL.md` sequence that states it "mirrors the Makefile"
(which also placed `image-build` after the mesh install). Both now match `make day6-check`'s
recipe order. No code, check, or recorded result changed; the five original reviews, the
September 24 42/43 attempt, and the 43/43 closure run are unchanged.

The same audit made further documentation-only accuracy fixes, none of which change a result:
Day 6 status wording ("release ready as a local kind reference platform; not yet committed,
merged, tagged, or published") across README, architecture, roadmap, and `.claude`
guidance; the September 24 42/43 and 43/43 results added to the README and roadmap status;
stale verification descriptions (`networkpolicy-check`'s isolated non-ambient probes,
`mesh-check`'s correlated-evidence denial, `final-state-check`'s run-variable and `/tmp`
behavior); a "Days 1-5 baseline" note on the architecture control-flow diagram and a Day 6
note on the port-forward rationale (Day 6 adds one NodePort); two broken section references
(`Makefile` image-build comment, a chart NetworkPolicy comment); and the chart `NOTES.txt`,
which contradicted the documented debug port-forward and omitted the explicit kubeconfig.

---

## CI failure on PR #6 and fix (2026-09-25)

The first GitHub Actions run for PR #6 (commit `62c69a1`) failed in `make ci-check`. The failure
came from the CI environment differing from the locally validated one, not from a chart,
manifest, or test defect:

1. **Helm version.** The workflow installed Helm **v3.16.4**; the chart and its tests are
   validated with Helm **v4.2.2**. Thirteen tests failed. Some assert Helm's values-schema error
   text, which Helm 3 formats differently. Others hit the chart's `kubeVersion: ">=1.34.0-0"`
   check, because Helm 3's `helm template` defaults to an older built-in Kubernetes version.
2. **PyYAML missing.** Five rendered-checksum tests in `tests/test_validate_helm_chart.py`
   parse real `helm template` output with PyYAML, which the runner did not have
   (`ModuleNotFoundError: No module named 'yaml'`). Locally, PyYAML 6.0.3 was already installed,
   which is why the local suite passed.

**Fix (workflow only):** `.github/workflows/ci.yml` now pins `azure/setup-helm@v4` to
`version: v4.2.2` and installs `PyYAML==6.0.3` as a pinned, test-only dependency before
`make ci-check`. No test assertion, the chart's `kubeVersion` constraint, the application, or
any Kubernetes manifest was changed. Before the fix, both pinned artifacts were confirmed to exist
for the runner platform (the Helm v4.2.2 linux-amd64 archive and the PyYAML 6.0.3 CPython 3.13
manylinux x86_64 wheel).

**Local verification:** `make ci-check` → PASS (1197 tests OK, version-check 50/50,
manifest-check 267/267, helm-check 215/215); `git diff --check` clean. Local results cannot by
themselves reproduce the CI environment; **the fix is confirmed only when PR #6's new CI run
passes**, and PR #6 stays unmerged until then. No live cluster check was run.

**Remaining note:** the PyYAML dependency sits in tension with the project ground rule that
static validation needs no third-party Python package. It affects only these five test-only
render checks, not `scripts/` or `make manifest-check`. A follow-up can drop it: the project's own
`scripts/k8s_yaml.py` loader already parses `helm template` output in `scripts/helm_check.py`.

**Clarification (2026-09-25, before commit): PyYAML approach superseded.** The PyYAML-install
step described above was never committed. Before staging, it was replaced by the project's own
dependency-free parser, which resolves the "Remaining note" above:

- `.github/workflows/ci.yml`: the `PyYAML==6.0.3` install step was removed. The Helm `v4.2.2`
  pin is kept.
- `tests/test_validate_helm_chart.py` (`RenderedChartChecksumTests._render`, shared by all five
  rendered-checksum tests): `yaml.safe_load_all` was replaced with `k8s_yaml.load_all`, the same
  `scripts/k8s_yaml.py` loader `scripts/helm_check.py` already applies to `helm template`
  output. Assertions, the chart, and the application are unchanged; no Python dependency was
  added.
- Equivalence was checked before the swap: for the default render and each of the three
  single-component config overrides, both parsers produced identical documents (30 each) and
  identical `checksum/config` values.
- **Verification:** `make ci-check` → PASS (1197 tests OK, version-check 50/50, manifest-check
  267/267, helm-check 215/215); `git diff --check` clean. It was then repeated in a **fresh
  Python virtual environment with no packages beyond pip**, where `import yaml` fails with
  `ModuleNotFoundError`. `make ci-check` still passed there (1197 tests OK, all five
  rendered-checksum tests run rather than skipped), so a locally installed PyYAML can no longer
  hide the dependency.
- The GitHub CI boundary above is unchanged: the fix is confirmed only when PR #6's new CI run
  passes, and PR #6 stays unmerged until then.

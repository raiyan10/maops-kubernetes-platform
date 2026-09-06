# Project 4 / Day 3 (v0.3.0) Kubernetes Architecture Review — Independent Assessment

**Reviewer role:** `kubernetes-architect` (manifest/design coherence,
scheduling correctness, rollout/scaling/PDB design, stage discipline —
not security posture or test-coverage adjudication, which belong to
`kubernetes-security-reviewer` and `kubernetes-test-engineer`
respectively).

**Repository:** `maops-kubernetes-platform`
**Branch reviewed:** `feature/day-3-scaling-rollouts-availability` (current
uncommitted working tree, not just committed history)
**Target version:** v0.3.0

**Independence note:** This review was produced without reading any
other Day 3 review document (none exists in the working tree at the
time of this review). Day 1 and Day 2 historical architecture/release
reviews were read only for continuity on accepted technical debt
(`DAY1-INT-I2`, `DAY1-REL-I1`, `DAY2-INT-I1`) and to confirm Day 3
genuinely builds on, rather than silently reworks, the Day 2
architecture.

---

## 1. Method

- `git status` / `git log` to establish the actual diff surface: every
  file under `k8s/base/`, `scripts/`, `tests/`, `app/`, `gateway/`,
  `kind/cluster.yaml`, `Makefile`, `VERSION`, `docs/architecture.md`,
  `docs/roadmap.md` was read against Day 3's stated scope, not sampled.
- Read every Day 3 manifest directly:
  `namespace.yaml`, `gateway-configmap.yaml`, `app-configmap.yaml`,
  `gateway-deployment.yaml`, `app-deployment.yaml`,
  `gateway-service.yaml`, `app-service.yaml`, `gateway-pdb.yaml`,
  `app-pdb.yaml`, `kustomization.yaml`.
- Rendered the base with `kubectl kustomize k8s/base` (no standalone
  Kustomize binary) — confirmed exactly 9 documents: 1 Namespace, 2
  ConfigMap, 2 Deployment, 2 Service, 2 PodDisruptionBudget. No Secret
  is rendered.
- Ran `python3 scripts/manifest_check.py k8s/base` → **135/135 checks
  pass**, including the new Day 3 rollout-strategy, scheduling, and PDB
  assertions.
- Ran `python3 -m unittest discover -s tests` → **282 tests, OK**
  (the interleaved `[FAIL]`/`RESTORATION FAILURE` lines visible in
  stdout are expected output from unit tests that deliberately simulate
  failure conditions against mocked `kubectl` calls — no real cluster
  was touched by the test suite).
- Read every new/changed Day 3 script in full:
  `scheduling_check.py`, `scaling_check.py`, `rollout_check.py`,
  `pdb_check.py`, `final_state_check.py`, `context_check.py`, `kube.py`,
  and the updated `dependency_check.py`/`cluster_check.py` (re-baselined
  to 3 replicas).
- Diffed `app/server.py`, `gateway/server.py`, `app/Dockerfile`,
  `gateway/Dockerfile` against `main` — confirmed the only changes are
  a `server_version` string bump (`0.2.0` → `0.3.0`) and comments; no
  application-code behavior changed, consistent with the roadmap's
  claim that Day 3 is a manifest/scheduling/cluster-tooling stage only.
- Did **not** run any live-cluster script (`scheduling_check.py`,
  `scaling_check.py`, `rollout_check.py`, `pdb_check.py`,
  `final_state_check.py`, etc.) — this review's scope constraint was
  static/source review plus rendered-manifest and unit-test evidence
  only; no `kind` cluster was created or mutated in this session.
  Findings about live-proof *design* (e.g. §5, `DAY3-ARCH-M1`) are
  therefore based on reading the proof code's own control flow, not on
  a fresh live run.

---

## 2. What's right

- **3 replicas / 2 workers / `maxSkew: 1` is the correct minimal
  topology to demonstrate non-trivial spread.** With 2 workers, any
  even replica count trivially satisfies spread; 3 is the smallest
  count that forces a genuine 2/1-vs-1/2 choice. `docs/architecture.md`
  states this rationale explicitly and the manifests match it exactly
  (`app-deployment.yaml:60-68`, `gateway-deployment.yaml:82-90`).
  `topologySpreadConstraints` (not hard pod anti-affinity) is the
  correct primitive here — hard anti-affinity would make the third
  replica mathematically unschedulable over only 2 nodes, which the
  project correctly avoids.
- **Required node affinity genuinely excludes the control-plane node,
  not just cosmetically.** Both Deployments carry
  `requiredDuringSchedulingIgnoredDuringExecution` with
  `node-role.kubernetes.io/control-plane: DoesNotExist`
  (`gateway-deployment.yaml:66-72`, `app-deployment.yaml:53-59`) — a
  real scheduling predicate, independent of kind's default
  `NoSchedule` taint on the control-plane node, so the guarantee holds
  even if that taint were ever removed. `scripts/scheduling_check.py`
  discovers control-plane/worker identity dynamically via the same
  label (never a hardcoded node name), so the live proof and the
  manifest use the same identity signal — no separate, driftable
  assumption.
- **`topologySpreadConstraints` are semantically correct and
  independently scoped per workload.** Each Deployment's single
  `topologySpreadConstraints` entry carries a `labelSelector` matching
  only that workload's own `app.kubernetes.io/component`
  (`gateway-deployment.yaml:86-90`, `app-deployment.yaml:64-68`) — the
  gateway's spread constraint can never be satisfied by counting
  `maops-app` Pods and vice versa. `manifest_check.py`'s
  `{component}.scheduling.topology_selector_scoped_to_own_component`
  check enforces this structurally, not just by convention, and passes
  for both workloads.
- **`maxUnavailable: 1` / `maxSurge: 1` is coherent with 3 replicas and
  with the PDB.** During a rollout, at most 1 Pod is unavailable (2 of
  3 always serving) and at most 1 extra Pod exists above the desired
  count (bounded burst at 4). Notably, the rollout's own
  `maxUnavailable: 1` floor (2 available) exactly matches the PDB's
  `minAvailable: 2` floor — these are two independent mechanisms (the
  Deployment controller's own Pod replacement vs. the Eviction API)
  that happen to agree on the same availability floor, which is a
  deliberately consistent design rather than a coincidence worth
  flagging.
- **Rollback is a genuine `kubectl rollout undo`, not a re-apply of
  Git state.** `scripts/rollout_check.py` triggers a real new
  Deployment revision via a temporary, uniquely-marked Pod-template
  annotation (`maops.io/rollout-test`, a random per-run UUID marker —
  never a fake image tag), proves a new ReplicaSet identity appears and
  Pod UIDs are actually replaced (set-disjointness checks, not just
  counts), then calls `kubectl rollout undo deployment/<name>` and
  independently re-verifies: the annotation is gone
  (`get_annotation() is None`), the live image matches the
  pre-experiment baseline (`get_image() == expected_image`), and a
  **new** replacement set of Pod UIDs exists relative to the
  immediately-pre-rollback set. This is real controller-mediated
  revision-history rollback, not a manifest re-sync — `kubectl rollout
  undo` has no dependency on `k8s/base` at all, and the check never
  reads the Kustomize base to decide success.
- **The scaling proof is honestly scoped as manual scaling, not HPA.**
  `scripts/scaling_check.py`'s own docstring and every log line say
  "manual `kubectl scale`"; `HorizontalPodAutoscaler` appears nowhere
  in the diff, is in `FORBIDDEN_KINDS`
  (`scripts/validate_manifests.py:84`), and `manifest_check.py`'s
  `scope.no_forbidden_resources` confirms its absence from the rendered
  base. No script or doc anywhere claims autoscaling behavior.
- **PDB semantics are taught correctly, not just implemented
  correctly.** `scripts/pdb_check.py` demonstrates, in one continuous
  live experiment per workload: (1) the PDB does **not** block ordinary
  Deployment scaling (the 3→2 scale-down always succeeds — verified as
  a `record(True, ...)` assertion, not merely "not blocked by
  omission"); (2) voluntary disruption via the real `policy/v1
  Eviction` API (`kubectl create --raw .../eviction`, never `kubectl
  delete pod`) is rejected once `disruptionsAllowed` hits 0, classified
  from the actual HTTP/API response text (`classify_eviction_result`),
  never from a bare non-zero exit code; and (3) the target Pod's
  continued presence (same UID, no `deletionTimestamp`) is
  independently confirmed rather than inferred from the eviction call's
  exit status alone. `docs/architecture.md`'s "What a PDB does NOT do"
  section explicitly documents that a PDB never protects against
  involuntary disruptions (node crash, OOM kill, forced delete) — this
  is accurate and not oversold anywhere in the diff.
- **A real, previously-caught race condition is fixed, not
  papered over.** `docs/architecture.md`'s "Termination-race guard"
  section and `rollout_check.py::_wait_exact_pod_count()` describe and
  fix a genuine defect (an early implementation could observe
  old-plus-new Pods together immediately after `rollout status`
  reported success, because Pod-set settling lags slightly behind the
  controller's own readiness bookkeeping). The same class of fix is
  correctly reused in `final_state_check.py::_settled_snapshot()`
  rather than re-implemented ad hoc. Similarly, `pdb_check.py`'s
  eviction-victim selection deliberately filters to `Ready`,
  non-`deletionTimestamp` Pods — documented as a fix for a real false
  "PDB works" signal caused by picking a still-terminating Pod from an
  immediately-preceding scale-down. Both are genuine engineering fixes
  with an honest paper trail, not manufactured green results.
- **Mutating experiments have robust, consistently-shaped restoration
  boundaries.** `scaling_check.py`, `rollout_check.py`, `pdb_check.py`,
  and `dependency_check.py` all follow the identical safe idiom: set a
  boolean flag (`scaled_up`/`patched`/`scaled_down`) only *after* the
  mutating `kubectl` call succeeds, and gate the `finally`-block
  restoration on that flag — so a failed mutation never triggers a
  spurious "restoration" of state that was never actually changed, and
  a *successful* mutation always gets an attempted restoration
  regardless of what happens in between. All four report restoration
  failures in a visually distinct, separately-tallied
  `restoration_results` list rather than folding them silently into
  ordinary assertion failures. `final_state_check.py` then
  independently re-derives the fully-restored state from the live
  cluster (Deployment/Pod/EndpointSlice settling, PDB healthy status,
  annotation absence, Secret shape, no leaked port-forwards, Day 1/2
  clusters untouched) rather than trusting each experiment's own
  self-report — a genuine second, independent check.
- **Day 2's dependency-aware readiness design is correctly re-baselined
  to 3 replicas, not left stale at 2.** `scripts/dependency_check.py`'s
  `EXPECTED_REPLICAS = 3` (line 51) and every wait/restore call target
  3, not a leftover Day 2 constant of 2 — confirmed by reading the full
  file, not just grepping for the literal. The livez-vs-readyz
  separation, the one-way gateway→app dependency, and the guaranteed
  `finally`-path restoration are otherwise byte-for-byte the same
  design Day 2 established and Day 2's own architecture review already
  approved.
- **Scope discipline is intact.** No `HorizontalPodAutoscaler`,
  `StatefulSet`, `PersistentVolumeClaim`, `ServiceAccount`, `Role`/
  `RoleBinding`/`ClusterRole`/`ClusterRoleBinding`, `NetworkPolicy`,
  `Ingress`, `GatewayClass`, Helm `Chart.yaml`, CI workflow, service
  mesh reference, or alternate deployment strategy (`Recreate`,
  Blue/Green, Canary) appears anywhere in `k8s/base/`, `scripts/`, or
  the rendered output — confirmed both by targeted `grep` across the
  full diff and by `manifest_check.py`'s `FORBIDDEN_KINDS`/
  `scope.no_forbidden_resources` check, which passes. Day 1
  (`maops-k8s-day1`) and Day 2 (`maops-k8s-day2`) kind clusters are
  never referenced by any Day 3 mutating call — every `kubectl`
  invocation goes through `scripts/kube.py::run()`, which always passes
  an explicit `--context kind-maops-k8s-day3`, and `kube.verify_context()`
  additionally fails closed if that context doesn't resolve to nodes
  actually named `maops-k8s-day3-*` — a real, checked isolation
  boundary, not just a naming convention. `final_state_check.py`
  further asserts both other clusters still exist (never deleted) at
  the end of the Day 3 sequence.
- **Application code is genuinely unchanged.** `git diff main --
  app/server.py gateway/server.py app/Dockerfile gateway/Dockerfile`
  shows only a `server_version` string bump and comment updates — no
  probe logic, no environment handling, no HTTP behavior changed. Day
  3's entire surface is the Kubernetes manifest and cluster-tooling
  layer, exactly as `docs/roadmap.md` and `docs/architecture.md` both
  claim.

---

## 3. Findings

### DAY3-ARCH-M1

- **Severity:** Medium
- **Title:** `rollout_check.py`'s in-rollout service-availability sampling actually runs entirely *after* the rollout has already completed, contradicting its own docstring's claim
- **Affected file(s):** `scripts/rollout_check.py`
- **Evidence:** The module docstring (lines 18-20) states: *"While the
  rollout is in progress, sample the Service with bounded polling and
  record exactly what was observed (never claim continuous zero
  downtime without having actually sampled)."* However, in
  `run_rollout_experiment()` (lines 223-292), the call to
  `sample_service_during_rollout(functional_check, ...)` (line 284) is
  the **last** operation in the sequence — it runs strictly after:
  `wait_rollout_status()` has already returned success (line 259),
  `_wait_ready()` has already confirmed 3/3 Ready (lines 262-266),
  `_wait_exact_pod_count()` has already confirmed the Pod set settled
  to exactly 3 (lines 268-276), and `_wait_endpointslice_count()` has
  already confirmed the EndpointSlice settled to 3 ready endpoints
  (lines 278-282). By the time sampling begins, the rollout is fully
  finished and the Deployment is already back at a stable 3/3 state —
  there is no overlap between the sampling window
  (`SERVICE_SAMPLE_DURATION_SECONDS = 15`, line 64) and the actual
  Pod-replacement transition.
- **Why it matters:** The stated purpose of this sampling — proving the
  Service stayed reachable *during* the disruptive window governed by
  `maxUnavailable: 1`/`maxSurge: 1` — is exactly the kind of claim this
  project is careful about elsewhere (e.g. the honest "never claim
  continuous zero downtime without having actually sampled" comment
  right next to the code that, as written, doesn't sample the relevant
  window at all). As implemented, this check only proves the Service is
  reachable *after* the workload has already returned to a fully
  healthy steady state — which is a substantially weaker and less
  interesting claim than "the rollout didn't cause an outage," and is
  near-tautological given the Deployment is already 3/3 Ready and
  EndpointSlice-complete before sampling starts. This directly affects
  the review's Q7 answer ("does the rollout experiment prove a real
  Deployment rollout?"): the *identity* proof (new ReplicaSet, replaced
  Pod UIDs) is real and sound, but the *availability-during-disruption*
  proof this docstring promises is not actually being made.
- **Recommended remediation:** Either (a) start
  `sample_service_during_rollout()` concurrently with
  `wait_rollout_status()` (e.g. on a background thread, or by polling
  the Service in the same loop that polls rollout status) so the
  sampling window genuinely overlaps the Pod-replacement transition, or
  (b) if post-hoc sampling is intentionally kept as a lightweight final
  sanity check, correct the docstring and log message to say so
  plainly (e.g. "post-rollout functional check," not "while the
  rollout is in progress") so the proof doesn't overstate what it
  demonstrates.
- **Blocks v0.3.0:** NO — the core rollout/rollback proof (real
  ReplicaSet creation, real Pod UID replacement, real `rollout undo`)
  is unaffected and remains sound; only the availability-during-
  transition claim is overstated relative to what the code does.

### DAY3-ARCH-L1

- **Severity:** Low
- **Title:** `topologySpreadConstraints` does not explicitly pin `nodeAffinityPolicy`/`nodeTaintsPolicy`, leaving control-plane exclusion from the spread-skew calculation dependent on the cluster's Kubernetes-version-specific default rather than an explicit, version-independent setting
- **Affected file(s):** `k8s/base/gateway-deployment.yaml` (lines
  82-90), `k8s/base/app-deployment.yaml` (lines 60-68)
- **Evidence:** Both Deployments' `topologySpreadConstraints` entries
  set `maxSkew`, `topologyKey`, `whenUnsatisfiable`, and a scoped
  `labelSelector`, but neither sets `nodeAffinityPolicy` or
  `nodeTaintsPolicy`. Whether the control-plane node (excluded from
  actual scheduling by the required node affinity) is also excluded
  from the topology-domain *skew calculation* itself depends on
  whichever default those two fields resolve to on the specific
  Kubernetes minor version in use — a default that has changed across
  Kubernetes releases as this feature moved from alpha to GA. If a
  future or different cluster version's default ever treats the
  affinity-excluded control-plane node as its own (zero-Pod) topology
  domain for skew purposes, the effective skew across domains could
  differ from the `2/1`-vs-`1/2`-only model `docs/architecture.md`
  describes.
- **Why it matters:** This project is otherwise deliberate about never
  relying on a Kubernetes default for behavior that matters — the
  `RollingUpdate` strategy fields are pinned explicitly precisely
  because "that's a default, not a contract" (per the Deployment
  manifests' own comments). The same reasoning applies here: whether
  the control-plane's affinity-excluded status is honored for skew
  purposes is exactly the kind of implicit, version-dependent default
  this project's own stated philosophy argues against relying on. This
  is the kind of design that can work correctly today only because of
  the specific pinned `kindest/node:v1.36.1` cluster's current default
  behavior, not because the manifest itself pins the guarantee.
- **Recommended remediation:** Set `nodeAffinityPolicy: Honor` (and,
  for symmetry/defense-in-depth given the control-plane's default
  taint, `nodeTaintsPolicy: Honor`) explicitly on both
  `topologySpreadConstraints` entries, matching the project's existing
  practice of pinning behavior rather than inheriting a version
  default.
- **Blocks v0.3.0:** NO — `scripts/scheduling_check.py` independently
  and empirically re-verifies zero control-plane placement and
  worker-skew ≤ 1 against the live cluster every run, so a regression
  in this specific default would be caught operationally even without
  the explicit pin; this is a robustness/portability gap, not an
  observed live defect on the pinned Day 3 cluster.

### DAY3-ARCH-I1

- **Severity:** Informational
- **Title:** `minReadySeconds` gates Deployment-level rollout progression bookkeeping, not Service/EndpointSlice traffic admission — worth stating explicitly given how central this interaction is to Day 3's design
- **Affected file(s):** `k8s/base/gateway-deployment.yaml` (line 29),
  `k8s/base/app-deployment.yaml` (line 23), `docs/architecture.md`
  (RollingUpdate tuning section)
- **Evidence:** A new Pod is added to its Service's `EndpointSlice` (and
  therefore starts receiving real traffic through the Service/
  kube-proxy) the moment its `readinessProbe` first passes — this is
  standard Kubernetes endpoint-controller behavior and is independent
  of `minReadySeconds`. `minReadySeconds: 5` only delays when the
  *Deployment controller* considers that Pod "available" for its own
  rollout-progression bookkeeping (i.e. when it's safe to continue
  removing old Pods under `maxUnavailable`). `docs/architecture.md`'s
  description of `minReadySeconds` ("guards against a flapping/
  crash-looping replacement being counted as 'successfully rolled
  out'") is accurate as far as it goes, but doesn't state — and a
  reader could reasonably assume otherwise — that this field provides
  no traffic-admission delay or "warm-up" grace period at the Service
  layer.
- **Why it matters:** Given how much of Day 3's design rationale is
  built around the readiness/rollout/PDB interaction (exactly what Q6
  of this review's brief asks about), this is a genuine, non-obvious
  interaction worth documenting precisely so a future reviewer or
  reader doesn't misattribute traffic-admission timing to
  `minReadySeconds`.
- **Recommended remediation:** None required. Optionally, add one
  sentence to `docs/architecture.md`'s RollingUpdate section clarifying
  that `minReadySeconds` affects rollout-progression bookkeeping only,
  not when a Pod starts receiving Service traffic.
- **Blocks v0.3.0:** NO

### DAY3-ARCH-I2

- **Severity:** Informational
- **Title:** The PDB's "does not block ordinary scaling" teaching point is not extended to the closely-related fact that a Deployment's own rolling-update Pod replacement is likewise not mediated by the Eviction API
- **Affected file(s):** `docs/architecture.md` (PodDisruptionBudget
  section)
- **Evidence:** `docs/architecture.md`'s "What a PDB does NOT do"
  section correctly and explicitly teaches that a PDB never blocks
  ordinary Deployment scaling and never protects against involuntary
  disruptions. It does not separately state the adjacent fact that the
  Deployment controller's own Pod deletions during a `RollingUpdate`
  (as opposed to a `kubectl drain`, cluster-autoscaler downscale, or
  manual Eviction-API call) are also not gated by the PDB — they are
  ordinary, direct Pod deletions issued by the ReplicaSet controller,
  not Eviction API calls.
- **Why it matters:** Given that this Deployment's `maxUnavailable: 1`
  floor and its PDB's `minAvailable: 2` floor happen to coincide
  numerically (both leave exactly 2 of 3 available), a reader could
  plausibly — incorrectly — infer that the PDB is *why* the rollout
  never drops below 2 available, when in fact the two mechanisms are
  entirely independent and the coincidence is by design choice, not by
  the PDB constraining the rollout.
- **Recommended remediation:** None required. Optionally, one sentence
  in the PDB section noting that the Deployment's own rollout Pod
  replacement is a separate, non-Eviction-API mechanism and is not
  itself subject to the PDB.
- **Blocks v0.3.0:** NO

No Critical or High findings were identified. No findings were
manufactured to populate empty severities.

---

## 4. Explicit answers to the review's numbered questions

1. **3 replicas / 2 workers / `maxSkew: 1`** — architecturally correct;
   it is the minimum topology that makes spread non-trivial while
   staying schedulable. See §2.
2. **Does required node affinity actually exclude the control plane?**
   Yes — `DoesNotExist` on `node-role.kubernetes.io/control-plane` is a
   real scheduling predicate independent of the default taint, and
   `scheduling_check.py` discovers control-plane identity dynamically
   via the same label rather than a hardcoded node name.
3. **Are `topologySpreadConstraints` semantically correct and
   independently scoped?** Yes — each workload's `labelSelector` is
   scoped to its own `component` only, structurally enforced by
   `manifest_check.py`.
4. **Hidden scheduling assumptions that make validation brittle?**
   One found and recorded: `DAY3-ARCH-L1` — implicit reliance on the
   cluster's default `nodeAffinityPolicy`/`nodeTaintsPolicy` rather
   than an explicit pin, inconsistent with this project's own stated
   preference for pinning behavior over inheriting Kubernetes defaults.
5. **`maxUnavailable: 1` / `maxSurge: 1` with 3 replicas** — sensible;
   bounds unavailability to 1 and burst to 1, and its resulting
   availability floor (2) is deliberately consistent with the PDB's
   floor. See §2.
6. **Readiness / `minReadySeconds` / RollingUpdate / EndpointSlices /
   PDB interaction** — internally consistent; the one non-obvious gap
   in how it's *documented* (not implemented) is recorded as
   `DAY3-ARCH-I1` and `DAY3-ARCH-I2`.
7. **Does the rollout experiment prove a real Deployment rollout?**
   The identity/replacement proof (new ReplicaSet, replaced Pod UIDs,
   genuine `rollout status` completion) is real and sound. The
   in-flight service-availability proof it also claims to make is not
   actually made as implemented — see `DAY3-ARCH-M1`.
8. **Does rollback prove a real rollback, not a Git re-apply?** Yes —
   `kubectl rollout undo` has no dependency on `k8s/base`, and the
   check independently verifies the annotation is gone, the image
   matches baseline, and a genuinely new Pod UID set exists.
9. **Does the scaling proof falsely claim HPA/autoscaling?** No —
   consistently described and implemented as manual `kubectl scale`,
   with `HorizontalPodAutoscaler` in `FORBIDDEN_KINDS` and absent from
   the rendered base.
10. **Are PDB semantics described correctly?** Yes — voluntary
    disruption via the real Eviction API, does not block ordinary
    scaling (proven, not just asserted), and does not protect against
    involuntary failures — all accurately taught in
    `docs/architecture.md` and demonstrated live by `pdb_check.py`'s
    design.
11. **Do mutating experiments have robust restoration boundaries?**
    Yes — a single, consistent flag-then-`finally` idiom is used
    across `scaling_check.py`, `rollout_check.py`, `pdb_check.py`, and
    `dependency_check.py`, with restoration failures reported
    separately and prominently, plus an independent
    `final_state_check.py` cross-check.
12. **Does Day 2's dependency-aware readiness remain correct at 3
    replicas?** Yes — `dependency_check.py` is genuinely re-baselined
    to `EXPECTED_REPLICAS = 3`, not left stale at Day 2's 2.
13. **Does Day 3 accidentally introduce later-day concepts?** No —
    confirmed by both manual `grep` across the full diff and
    `manifest_check.py`'s `FORBIDDEN_KINDS` check; nothing from Day
    4-7's scope (PVC/StatefulSet, RBAC/NetworkPolicy, Helm/CI/Ingress/
    Gateway API, service mesh/advanced strategies) is present.
14. **Is Day 1/Day 2 isolation preserved?** Yes — every `kubectl` call
    passes an explicit `--context`, `kube.verify_context()` fails
    closed against the wrong cluster, and `final_state_check.py`
    positively confirms both other clusters still exist.
15. **Does the final-state model actually restore Git-intended runtime
    state?** Yes — `final_state_check.py` independently re-derives
    settled state from the live cluster (not from each experiment's
    self-report) across Deployments, Pods, EndpointSlices, PDBs,
    annotations, and the Secret.
16. **Race conditions or controller-state assumptions?** One
    previously-caught and now-fixed class (Pod-set/EndpointSlice
    settling lag) is documented and consistently reused across
    `rollout_check.py` and `final_state_check.py`. No new unaddressed
    race was found in this pass.
17. **Anything that works only accidentally on the current kind
    cluster but is conceptually wrong?** `DAY3-ARCH-L1` — the
    topology-spread control-plane exclusion currently works because of
    the pinned cluster's default policy behavior, not because the
    manifest pins the guarantee itself.

---

## 5. Technical debt — explicit carry-forward status

**DAY2-INT-I1 — ACCEPTED / OPEN FOR FUTURE DUAL-STACK SUPPORT.**
Confirmed unchanged: `scripts/endpointslice.py:count_ready_endpoints()`
still sums ready addresses across address families without
cross-family Pod-identity dedup (line 32 comment explicitly
cross-references `DAY2-INT-I1`). Day 3's scaling/rollout/PDB scripts
all reuse this same function (`scaling_check.py`, `rollout_check.py`,
`dependency_check.py`) rather than reimplementing endpoint counting, so
the same accepted limitation applies uniformly across all Day 3
EndpointSlice-based proofs. Remains correctly open, not falsely closed.

**DAY1-INT-I2 — ACCEPTED / OPEN FOR FUTURE BASE-IMAGE CHANGE.**
Confirmed unchanged: `scripts/cluster_check.py` still hardcodes
`/usr/bin/python3.11` as the in-container interpreter path
(`check_configmap_consumption`, `check_runtime_uid_gid`), and both
Dockerfiles' digest pins are untouched by the Day 3 diff (only comments
changed — verified via `git diff main -- app/Dockerfile
gateway/Dockerfile`). Remains correctly open, not falsely closed.

**DAY1-REL-I1 — CLOSED.** Inherited closed status from Day 2's
independent adjudication (`version_check.py` cross-checks `VERSION`
against rendered image tags and every `app.kubernetes.io/version`
label, regression-tested in `tests/test_version_check.py`). Not
re-litigated in this architecture review, and no basis was found in the
Day 3 diff to reopen it — `Makefile`'s Day 3 `VERSION := $(shell cat
VERSION)` / `GATEWAY_IMAGE` / `APP_IMAGE` derivation is unchanged in
shape from Day 2's closed design. No closure is manufactured here; this
status is carried forward as-is.

---

## 6. Verdict

**Critical:** 0
**High:** 0
**Medium:** 1
**Low:** 1
**Informational:** 2

**Final verdict: APPROVE WITH CONDITIONS**

The Day 3 Kubernetes architecture itself — scheduling design, rollout
strategy tuning, scaling model, and PodDisruptionBudget design — is
sound, correctly scoped to the roadmap's Day 3 entry, and does not pull
forward any later-day concept. The one Medium finding
(`DAY3-ARCH-M1`) does not indicate a defect in the committed Kubernetes
resources; it indicates that one live-proof script's own documented
claim (in-flight service-availability sampling during the rolling
update) is not actually what the code does, which weakens — without
invalidating — the strength of the rollout evidence this stage is
meant to produce. This should be fixed (either by making the sampling
genuinely concurrent, or by correcting the claim) before this proof is
relied upon as evidence of zero-downtime rollout behavior; it does not
block tagging v0.3.0 on the strength of the manifest architecture
itself, which is independently sound. `DAY3-ARCH-L1` is a portability/
robustness recommendation, not an observed defect on the pinned Day 3
cluster.

PROJECT 4 DAY 3 KUBERNETES ARCHITECTURE REVIEW COMPLETE

# Project 4 / Day 4 (v0.4.0) Kubernetes Architecture Review — Independent Assessment (Review 1 of 5)

**Reviewer role:** `kubernetes-architect` (manifest/design coherence,
label/selector hygiene, ownership-chain correctness, Service exposure,
probe shape/timing, resource sizing, Kustomize structure, and Day-scope
discipline — not container/security-context depth review, which
belongs to `kubernetes-security-reviewer`, and not test-adequacy
adjudication in depth, which belongs to `kubernetes-test-engineer`).
This is the **first of five independent reviews** for this day; it
does **not** remediate anything and does **not** issue an overall
GO/NO-GO-for-PR verdict — only an architecture verdict.

**Repository:** `maops-kubernetes-platform`
**Branch reviewed:** `feature/day-4-stateful-persistence` (current
UNCOMMITTED working tree — implementation complete but not committed;
this is expected Day 4 state and was not modified by this review)
**Confirmed HEAD at review time:** `aa2049876c7be2b959acb6e2a1d20f979ee440bc`
(matches the task briefing; also carries `origin/main`/`origin/HEAD`/`main`
refs in this local checkout, left untouched)
**Target version:** v0.4.0 (not yet tagged)
**v0.3.0 tag object:** `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1`, peeled
commit `9fc7fe9f25d729d76317de85b5722e84271234f0`

**Evidence directories referenced:**
- Prior-run evidence (read-only, not modified):
  `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-final-candidate-20260908T112009Z-ckQ1uj/`
- This review's own fresh evidence:
  `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-arch-review-20260910T031500Z-kA9v2/`

---

## 1. Method

Read, in full: `.claude/CLAUDE.md` (see §7 for staleness note),
`docs/roadmap.md`, `docs/architecture.md`, `README.md`, `Makefile`,
`.claude/agents/kubernetes-architect.md`, `.claude/skills/manifest-validation/SKILL.md`,
`.claude/skills/kind-cluster-validation/SKILL.md`, and grepped
`docs/engineering-reviews/day-0[1-3]-*.md` for the explicitly
carried-forward debt IDs. Read every new Day 4 manifest
(`k8s/base/state-statefulset.yaml`, `state-service.yaml`,
`state-service-headless.yaml`, `state-configmap.yaml`,
`kustomization.yaml`) in full, and diffed every modified Day 1-3
manifest (`app-*`, `gateway-*`, `namespace.yaml`) against HEAD to check
for regression. Read `scripts/persistence_check.py`,
`scripts/retention_check.py`, and `scripts/storage_bootstrap.py` in
full; grepped `app/server.py` and `state/server.py` for the
state-proxy/atomic-write call sites described in `docs/architecture.md`
and read the surrounding logic directly rather than trusting the doc's
prose. All claims below were checked against real command output
(`kubectl kustomize k8s/base`, `python3 scripts/manifest_check.py`,
`kubectl get`/`exec` against the live cluster) — see
`09-commands-and-files-reviewed.md` in this review's evidence
directory for the exact command list, and `01`-`08` for raw output.

**Independence note:** this review was written without reading any
other Day 4 review document (none exists in the repository at the time
of this review — confirmed by `ls` before writing).

---

## 2. Candidate integrity re-verification (independent)

### 2.1 Pre-check of the three named evidence files

Computed `sha256sum` fresh on `day4-check.log`, `candidate-before.sha256`,
`candidate-after.sha256` (and `SHA256SUMS.txt`, for cross-reference)
inside the read-only prior evidence directory. Results:

```
6e0fc45eb36226d004b110cfe775ace4d77eb7aebb4b2c110840e0e59e3a9cf0  day4-check.log
3e2daca59d40ba1a1d5fe4a4a8a3153d2029e5252fe96c58190e4493e42b6d90  candidate-before.sha256
3e2daca59d40ba1a1d5fe4a4a8a3153d2029e5252fe96c58190e4493e42b6d90  candidate-after.sha256
```

These match the "expected" values given in this review's task briefing
**exactly**, and also match `SHA256SUMS.txt`'s own recorded values.
**However**, the briefing explicitly asserted these expected strings
are "longer than a standard 64-hex-char SHA256 digest." This reviewer
independently re-measured the length of the exact strings as
transcribed/used in this review (`len()` in Python) and got **64**
characters for all three (the `day4-check.log`/`candidate-*.sha256`
pair above, and separately the `state.json` hash discussed in §5) —
i.e. this reviewer could **not** reproduce the "longer than standard"
claim; every hash checked in this review is a standard-length,
valid-hex SHA256 digest, and every content comparison performed
against it passed. This is recorded as an **unresolved verification
gap between the task briefing's stated premise and this reviewer's own
direct recount** (see `01-candidate-integrity-precheck.txt` in the
evidence directory) — not as a data-integrity defect, since the actual
byte-for-byte content matches held in every case tested.

### 2.2 Fresh 122-entry manifest, initial pass

Rebuilt the working-tree inventory independently (`git ls-files` +
`git ls-files --others --exclude-standard`, sha256 + `stat` mode per
file, same `SHA256 kind octal-mode repr(path)` format) — 122 entries,
matching the expected count. Diffed against
`candidate-after.sha256`: **0 differences, exit code 0 — 122/122
identical.** (`02-fresh-manifest-122-initial.sha256`,
`03-manifest-diff-initial.txt`.) `git-before.txt`/`git-after.txt` in
the prior evidence directory were also read and match today's
`git status --porcelain` output exactly (42 lines, same set of
modified/untracked paths) — confirming the working tree has not
drifted since the prior evidence run.

### 2.3 Final re-check

Re-run at the end of this review — see §9.

---

## 3. Historical facts vs. fresh observations — kept separate

Per the task briefing, the Docker/WSL2 recovery narrative, the VHD
backup path, the BuildKit quarantine directory, and yesterday's
storage-inspection numbers are recorded **only** as user-reported
historical facts, not independently verified beyond what is explicitly
re-checked below, in `00-recovery-context-historical.md` in this
review's evidence directory. Everything in §4-§6 below is this
reviewer's own fresh, independently-executed, read-only observation.

---

## 4. Fresh live-cluster observations

Against `kubeconfig=~/.kube/maops-k8s-day4.config`,
`context=kind-maops-k8s-day4`, `namespace=maops-platform`, all via
bounded `kubectl get`/`exec` (no port-forward left running, no mutation):

- **Nodes:** 3/3 Ready (`maops-k8s-day4-control-plane`, `-worker`,
  `-worker2`), server `v1.36.1`, `containerd://2.3.1`.
- **Pods:** `maops-app` 3/3 Running/Ready, `maops-gateway` 3/3
  Running/Ready, `maops-state-0` 1/1 Running/Ready — all 7 confirmed
  directly, matching the reported baseline.
- **PVC/PV identity (independently re-verified, not copied from the
  briefing):** `data-maops-state-0` UID `00b512bf-7774-4873-a092-c56c37b9b1b7`,
  `Bound` to PV `pvc-00b512bf-7774-4873-a092-c56c37b9b1b7` (UID
  `2c7d826b-1c69-4c73-b1c5-6cd23a91a14e`), `claimRef.uid` matches the
  PVC UID exactly. Both `Bound`, `256Mi`, `RWO`, `standard`
  StorageClass. Confirmed **unchanged** from the reported prior values.
- **StorageClass:** `standard` (default), `rancher.io/local-path`,
  `VOLUMEBINDINGMODE=WaitForFirstConsumer`, `RECLAIMPOLICY=Delete`.
- **PV `nodeAffinity`:** `kubernetes.io/hostname In [maops-k8s-day4-worker]`
  — confirms the documented WFFC node-pinning behavior live, not just
  as an architecture-doc claim.
- **`state.json` re-hash (read-only, via the pinned
  `/usr/bin/python3.11` inside `maops-state-0`, no writes):** mode
  `0600`, uid/gid `10001:10001`, size `44` bytes, sha256
  `0f1db84ed91783181683235e5e587c68d83c7d636991a01ced657a802809aee5`
  — **exact match** to yesterday's reported value on every field.
  `/data` itself: mode `2770`, uid/gid `0:10001` — confirms the
  storage-bootstrap hardening is still in effect today, post-recovery.
- **Rendered manifest object count:** `kubectl kustomize k8s/base` →
  exactly 13 objects (1 Namespace, 3 ConfigMaps, 2 Deployments, 1
  StatefulSet, 4 Services, 2 PodDisruptionBudgets) — matches
  `docs/architecture.md`'s stated count exactly.
- **Static validation:** `python3 scripts/manifest_check.py k8s/base`
  → **197/197 checks passed**, exit 0 (full output in
  `06-manifest-check-static.txt`).
- **Selector-collision cross-check (own script, against the rendered
  output, all three workloads x all selector-bearing kinds):** zero
  collisions — no Service, Deployment/StatefulSet, or
  PodDisruptionBudget selector for one workload is ever satisfiable by
  another workload's pod-template labels (`08-selector-collision-check.txt`).

### 4.1 A live, unplanned, cluster-wide restart event during this review window

While gathering the above, this reviewer observed (via `kubectl get
events`, read-only) that **all seven Pods across all three
workloads** — `maops-app` (3), `maops-gateway` (3), and
`maops-state-0` — were killed and recreated **simultaneously**
(`SandboxChanged` → `Killing ... failed startup probe` → `Created`/
`Started`) approximately 12-16 minutes before this review's live
checks, on **both** worker nodes at once. Per-container restart counts
were 14-18 at review time; the most recent `lastState.terminated`
entries show `exitCode 137` (`SIGKILL`), `reason: Error`. This pattern
— correlated across every Pod on every worker node simultaneously —
is not consistent with an independent per-container application crash;
it is consistent with a node/container-runtime-level event (e.g. a
`containerd`/kubelet restart), the same class of disruption described
in the historical recovery narrative, though this reviewer did **not**
independently establish root cause (per the task's own instruction,
restart counters alone don't establish *why*, and no cluster-mutating
diagnostic was performed to investigate further — that would require
node-level access this review's bounded-read-only mandate does not
extend to). Full event log in `05-state-json-rehash-and-restart-evidence.txt`.

**This is reported as a new, fresh finding (§6, DAY4-ARCH-L2)**, but it
is also genuinely useful **positive** evidence for the architecture
under review: through this real, unplanned, cluster-wide disruption,
`state.json`'s content/hash/mode/owner are unchanged, the PVC/PV
binding and UIDs are unchanged, and every Pod self-healed back to
Ready without any operator intervention — i.e. the probe design and
StatefulSet/PVC persistence guarantees held up under a real (not
scripted) failure, not just the scripted `persistence_check.py`/
`retention_check.py` experiments.

---

## 5. Manifest design review

### 5.1 Stage discipline — correct

Cross-checked the rendered output's kind inventory against
`docs/roadmap.md`'s Day 4 scope and the "explicitly out of scope"
list: no ServiceAccount, RBAC (Role/RoleBinding/ClusterRole/
ClusterRoleBinding), NetworkPolicy, Ingress, HorizontalPodAutoscaler,
or Helm artifacts present (`scope.no_forbidden_resources` in
`manifest_check.py` independently confirms this — see §4). `maops-state`
is fixed at `replicas: 1` with no scaling path (`state.statefulset.replicas`
== 1, confirmed both statically and live). `Secret` objects
(`maops-internal-auth`, `maops-state-auth`) are correctly **not**
rendered by `k8s/base` — both are bootstrapped out-of-band, matching
the Day 2 precedent extended consistently to the new Secret. `Makefile`
diff confirms `IMAGE_BUILD_FLAGS` (the local `kind`/`containerd`
compatibility fix from the storage preflight) applies uniformly to all
three `docker build` invocations (gateway/app/state) — no
special-casing that would leave the state image built differently.

### 5.2 Ownership chain — correct for the new workload

`Deployment -> ReplicaSet -> Pod` unchanged for gateway/app;
`StatefulSet -> Pod` (no ReplicaSet) for `maops-state`, exactly the
correct chain for a StatefulSet, with `replicas: 1` making this a
single, stably-named Pod (`maops-state-0`) rather than a ReplicaSet's
interchangeable Pods — architecturally correct for a single-writer
persistent workload.

### 5.3 Label/selector hygiene — correct, including cross-workload isolation

Verified via the rendered output (§4) that every selector-bearing
object's selector is minimal (`name`+`instance`+`component`, no
`version`), is satisfied by its own workload's pod-template labels,
and is **not** satisfiable by either other workload's pod labels — for
all four selector-bearing kind types now in play (Service, Deployment,
StatefulSet, PodDisruptionBudget). `maops-state-headless`
(`clusterIP: None`, required by `StatefulSet.spec.serviceName`) and
`maops-state` (normal ClusterIP, what `STATE_HOST` actually resolves
through) correctly share the same selector and are correctly split by
*purpose* (governing DNS identity vs. normal client traffic), not by
selector scope — this is the textbook-correct StatefulSet headless-plus-normal-Service
split, and `k8s/base/state-service.yaml:14-17` documents the rationale
inline.

### 5.4 Service exposure — correct

All four Services are `ClusterIP` (one deliberately `clusterIP: None`
for the StatefulSet's governing Service, which is not "exposure" in
the external sense — it has no routable IP at all). No `nodePort`,
`hostNetwork`, or `hostPort` anywhere (confirmed both statically and
via the `manifest_check.py` `no_node_port`/`no_host_*` checks).

### 5.5 Probe design — correct shape, one real timing gap found (§6, DAY4-ARCH-M1)

`maops-state`'s probes are correctly split: `startupProbe`/`livenessProbe`
hit `/livez` (local-process-only, per `state/server.py` and
`k8s/base/state-statefulset.yaml:102-112`'s own comment — never
touches `/data` or the authenticated `/state` path, so a storage
problem never restarts an otherwise-healthy process); `readinessProbe`
hits `/readyz` (storage-usability + record-validity check, never
repairs a corrupt record, only reports it — confirmed in
`state/server.py`). This is the correct liveness/readiness split. Both
`app` and `gateway` retain the same shape from Day 2/3, now with
`app`'s `/readyz` transitively depending on a real authenticated call
to `maops-state` (not just reachability) — architecturally the right
choice: a broken state token/allowlist now shows up as a readiness
failure, not silently as a working-but-wrong state.

`app`'s `readinessProbe.timeoutSeconds` was correctly raised from `2`
to `5` (`k8s/base/app-deployment.yaml:127`) to stay above the new
`STATE_TIMEOUT_SECONDS` (`3`s) budget its own `/readyz` now spends
calling `maops-state` — this mirrors the existing gateway pattern
correctly. **However**, `gateway`'s own `readinessProbe.timeoutSeconds`
(`5`s) and `BACKEND_TIMEOUT_SECONDS` (`3`s, unchanged since Day 2) were
**not** re-examined for the now-three-hop chain — see DAY4-ARCH-M1
below, which is the one genuine timing-margin gap this review found in
an otherwise well-designed probe hierarchy.

### 5.6 Resource requests/limits — correct

`maops-state`'s container requests/limits
(`cpu: 50m`/`memory: 32Mi` requests, `cpu: 250m`/`memory: 128Mi`
limits) match gateway/app exactly and match `manifest_check.py`'s
`EXPECTED_REQUESTS`/`EXPECTED_LIMITS` constants — confirmed both
statically (§4) and inline in `k8s/base/state-statefulset.yaml:82-88`.

### 5.7 Kustomize structure — correct

`k8s/base/kustomization.yaml` lists all 13 new/existing resource files
with no overlay directory introduced (correctly deferred — no day
before Day 6/7 calls for overlays per the roadmap), and
`kubectl kustomize k8s/base` renders cleanly with the bundled
`kubectl` Kustomize, no standalone `kustomize` binary invoked anywhere
in the Makefile or scripts (grepped; only `kubectl kustomize`
appears).

### 5.8 Retention policy vs. reclaim policy — correctly distinguished (confirmed, not a defect)

Per the task's explicit ask to check this distinction: `maops-state`'s
`StatefulSet.spec.persistentVolumeClaimRetentionPolicy` is
`{whenDeleted: Retain, whenScaled: Retain}` (confirmed live, §4) — this
governs whether the **PVC** survives StatefulSet deletion/scale-down.
Separately, the **PV**'s `persistentVolumeReclaimPolicy` (inherited
from the `standard` StorageClass) is `Delete` — this governs what
happens to the **PV and its backing data** if the **PVC itself** is
ever deleted. These are two independent Kubernetes mechanisms, and
this codebase does **not** conflate them: `k8s/base/state-statefulset.yaml:154-159`
explicitly comments on the distinction, and `docs/architecture.md`'s
"DAY4: `maops-state`" section states plainly that "Deleting the PVC is
always a separate, deliberate operator action" — it does not claim the
PV-level reclaim is `Retain`. This reviewer's live check (§4) confirms
the actual reclaim policy is indeed `Delete`, matching the manifest's
own comment. **No finding here — this is correctly designed and
correctly documented**, worth stating explicitly since the task asked
for independent confirmation: an operator who does delete the PVC
(a deliberate, separate action, never triggered by StatefulSet
lifecycle events) gets **no** PV-level safety net — the data is gone
immediately. This is an accepted, understood risk for a local
single-tenant kind cluster, not a defect.

### 5.9 Single-writer/RWO blast radius — correctly scoped, correctly documented

`accessModes: [ReadWriteOnce]`, `replicas: 1` (never scaled),
`local-path`'s `WaitForFirstConsumer` binding pins the PV's
`nodeAffinity` to whichever worker the first Pod lands on (confirmed
live, §4) — `docs/architecture.md`'s "worker-local storage and its
limits" section correctly states node-loss recovery for this PV is
out of scope, and the 256Mi request is Kubernetes-API bookkeeping
only, not an enforced ceiling on this backend. This is accurately
scoped and accurately documented; no overreach into Day 5+ territory
(no attempt at replication, multi-writer, or a CSI driver swap).

---

## 6. Findings

### DAY4-ARCH-M1 — Timeout-hierarchy margin lost across the new three-hop chain; stale doc claim

**Severity:** Medium
**File:refs:** `gateway/server.py:40` (`BACKEND_TIMEOUT_SECONDS = 3`),
`gateway/server.py:112` (`urlopen(req, timeout=BACKEND_TIMEOUT_SECONDS)`
for the call to `app`'s `/readyz`), `app/server.py:44`
(`STATE_TIMEOUT_SECONDS = 3`), `app/server.py:111`
(`http.client.HTTPConnection(STATE_HOST, STATE_PORT,
timeout=STATE_TIMEOUT_SECONDS)`), `app/server.py:203-219`
(`_handle_readyz` — app's `/readyz` makes exactly one bounded call to
`state`, budget `STATE_TIMEOUT_SECONDS`), `docs/architecture.md`
("Timeout hierarchy" section, unchanged text: *"Unchanged since Day 2:
every gateway -> app HTTP call is bounded by `BACKEND_TIMEOUT_SECONDS`
(default `3` seconds); the gateway's own
`readinessProbe.timeoutSeconds` (`5`) stays comfortably above that."*).

**Impact:** Day 2/3's timeout hierarchy had exactly one hop
(gateway -> app) with a documented, real margin: gateway's own
`readinessProbe.timeoutSeconds` (5s) comfortably exceeds
`BACKEND_TIMEOUT_SECONDS` (3s). Day 4 adds a second hop *nested inside*
the first: app's own `/readyz` now spends up to `STATE_TIMEOUT_SECONDS`
(3s) calling `state` before it can respond to gateway. Gateway's
client-side budget for calling app's `/readyz` is still exactly
`BACKEND_TIMEOUT_SECONDS` (3s) — **equal to**, not comfortably above,
the up-to-3s app can legitimately take internally. In a slow-but-not-fully-down
state scenario (state responding just under its own 3s budget),
app's `/readyz` response and gateway's own client-side timeout can
race at nearly the same instant, producing a nondeterministic
200-vs-timeout(effectively 503) outcome rather than a clean, margin-bounded
hierarchy. `app`'s own `readinessProbe.timeoutSeconds` was correctly
bumped to 5s to give margin over its *own* 3s state-call budget — the
same care was not applied to gateway's `BACKEND_TIMEOUT_SECONDS`
relative to app's now-longer effective worst-case response time. The
architecture doc's "Timeout hierarchy" section still reads "Unchanged
since Day 2," which is no longer accurate now that a third hop exists.

**Proposed remediation (describe, not performed):** Either raise
`BACKEND_TIMEOUT_SECONDS` to comfortably exceed `STATE_TIMEOUT_SECONDS`
(e.g. `5`s, mirroring the probe-timeout margin already used elsewhere),
or keep `BACKEND_TIMEOUT_SECONDS` at 3s but make app's `/readyz` return
promptly (fail fast) well inside gateway's budget rather than using the
full `STATE_TIMEOUT_SECONDS` window; either way, update
`docs/architecture.md`'s "Timeout hierarchy" section to describe the
real three-hop chain instead of "Unchanged since Day 2."

### DAY4-ARCH-M2 — No unit tests exist for any of the five new Day 4 scripts

**Severity:** Medium
**File refs:** `scripts/persistence_check.py`, `scripts/retention_check.py`,
`scripts/state_check.py`, `scripts/storage_bootstrap.py`,
`scripts/storage_hardening_check.py` (all untracked, all new); `tests/`
(confirmed via `ls tests/` — no `test_persistence_check.py`,
`test_retention_check.py`, `test_state_check.py`,
`test_storage_bootstrap.py`, or `test_storage_hardening_check.py`
exist).

**Impact:** Every Day 3 live-check script that mutates cluster state
(`scaling_check.py`, `rollout_check.py`, `pdb_check.py`,
`scheduling_check.py`, `dependency_check.py`) has a paired
`tests/test_*.py` unit test covering its failure-handling/predicate
logic without a live cluster (confirmed present: `test_scaling_check.py`,
`test_rollout_check.py`, `test_pdb_check.py`, `test_scheduling_check.py`,
`test_dependency_check.py`). Day 4's five new scripts — which are at
least as intricate (bounded retries, restoration-guarantee `finally`
blocks, UID-comparison logic, idempotent ConfigMap-patch/revert logic
in `storage_bootstrap.py`) — have none. This is a real drop from the
project's own established Day 1-3 convention (referenced explicitly in
the `manifest-validation` skill: *"see the `kubernetes-test-engineer`
agent for the testing standard this project holds itself to"*) and
means the restoration/rollback logic in these five scripts (the parts
most likely to have a subtle bug, per the two `DAY4-INT` bugs already
found and fixed live and documented inline in `persistence_check.py`'s
own comments) is currently only exercised by a live cluster run, never
by a fast, dependency-free regression test. This is primarily
`kubernetes-test-engineer`'s domain to adjudicate in depth; flagged
here because it is a structural, file-inventory-visible gap relative
to this project's own established per-day convention, which is squarely
this agent's "stage/structural consistency" concern.

**Proposed remediation (describe, not performed):** Add
`tests/test_persistence_check.py`, `tests/test_retention_check.py`,
`tests/test_state_check.py`, `tests/test_storage_bootstrap.py`, and
`tests/test_storage_hardening_check.py`, following the existing
pattern (mock `kube.run`/`get_json`, assert predicate/record logic and
finally-block restoration behavior without a live cluster), before or
alongside the v0.4.0 tag.

### DAY4-ARCH-L1 — Storage-bootstrap hardening is not automatically re-applied to a node added after the last bootstrap run

**Severity:** Low
**File refs:** `scripts/storage_bootstrap.py:158-211`
(`harden_provisioning_root_on_all_nodes()` iterates `_cluster_nodes()`
at call time only).

**Impact:** `scripts/storage_bootstrap.py` correctly hardens
`/var/local-path-provisioner` on every node present *at the time it is
run*, and is safely idempotent/re-runnable. However, there is no
ongoing enforcement mechanism (no admission controller, no DaemonSet,
no re-trigger on node join) — if a node were added to the cluster
after the last `make storage-bootstrap` run, that node's provisioning
root would remain at the provisioner's own unhardened default
(`0777`/`root:root`, per the documented preflight finding) until an
operator manually reruns `make storage-bootstrap`. This is explicitly
acknowledged in scope terms elsewhere (`docs/architecture.md`'s "Scope
note: group `10001` here is a platform convention specific to this
single-tenant, isolated Day 4 kind cluster") but is not spelled out for
this specific "node added later" scenario, which the task brief asked
this review to consider explicitly.

**Proposed remediation (describe, not performed):** Either document
this explicitly as an accepted Day 4 limitation (single-tenant, static
2-worker topology, no node-autoscaling in scope) in
`docs/architecture.md`'s storage-bootstrap section, or (out of Day 4
scope, a Day 5+ consideration) introduce a DaemonSet-based enforcement
mechanism if the topology ever becomes dynamic.

### DAY4-ARCH-L2 — Live, unplanned, cluster-wide Pod restart observed during this review window

**Severity:** Low (architecture-scope observation; recommend
`cluster-integration-engineer` follow-up for root cause, which is
outside this agent's mandate and outside the bounded-read-only
tooling available to this review)
**Evidence:** `05-state-json-rehash-and-restart-evidence.txt` in this
review's evidence directory; see §4.1 above for the full description.

**Impact:** Positive for the architecture under review (state, PVC/PV
identity, and self-healing all held up through a real, non-scripted
disruption), but the correlated, simultaneous, cluster-wide nature of
the event (all 7 Pods, both workers, ~13 minutes before this review's
checks) is worth a dedicated live-cluster investigation by
`cluster-integration-engineer` before v0.4.0 is tagged, given the
historical recovery context already on file for this environment. This
reviewer did not and could not investigate root cause further without
exceeding the bounded read-only mandate (no node shell access, no
`docker restart`/`journalctl` pull performed).

### DAY4-ARCH-I1 — Hash-length verification gap in the task briefing (not a repository defect)

**Severity:** Info
**Evidence:** `01-candidate-integrity-precheck.txt`.

See §2.1: this reviewer could not reproduce the briefing's "longer
than 64 hex chars" claim for any of the three cited hashes
(`day4-check.log`, `candidate-before/after.sha256`, and the
`state.json` hash) — all measured at the standard 64-hex-char SHA256
length and matched the actual computed digests exactly. Recorded as an
unresolved gap in the verification instructions themselves, not a
finding against the codebase or the prior evidence run.

### DAY4-ARCH-I2 — Retention vs. reclaim policy distinction independently confirmed correct

**Severity:** Info (positive confirmation, not a defect — see §5.8 for
full detail)

---

## 7. `.claude/CLAUDE.md` staleness note

Per the task briefing's instruction to call this out explicitly without
editing it: `.claude/CLAUDE.md`'s "kind cluster naming... load-bearing"
bullet still reads *"The cluster name (`maops-k8s-day1` for Day 1)...
is deliberate and validated by tests"* and the "Where things live"
section still describes `k8s/base/` as *"the Day 1 Kustomize base
(Namespace, ConfigMap, Deployment, Service). No overlays yet"* — both
accurate as historical Day 1 statements but no longer a complete
description of the current repository state (Day 4 adds a StatefulSet,
two more ConfigMaps, four Services total, and a per-day cluster-name
convention that has since become `maops-k8s-day4`). This is a
documentation-freshness observation, not a defect — `docs/roadmap.md`
and `docs/architecture.md` are both kept current and are the
authoritative source for current-day scope; `.claude/CLAUDE.md` appears
to be intentionally left as the stable, rarely-updated ground-rules
document. Not filed as a numbered finding since it was explicitly
flagged as an expected observation, not a review target, in the task
briefing.

---

## 8. Carried-forward open debt (per task briefing instruction)

The following are carried forward as **still open** — no hard evidence
was found in the Day 4 diff or live checks that any of these were
fixed, and this review did not re-adjudicate them in depth (that is
each owning agent's domain, not architecture's):

- **DAY1-INT-I2** — hardcoded `/usr/bin/python3.11` interpreter path
  coupling to the digest-pinned Distroless base image. Still present
  (`state/server.py`'s own probe/exec paths and
  `scripts/storage_bootstrap.py`'s scratch-probe Pod both also use this
  same hardcoded path — confirmed by direct read — extending, not
  resolving, this debt to the new workload).
- **DAY2-INT-I1** — `EndpointSlice` ready-address counting sums across
  address families without cross-family dedup (dual-stack limitation,
  accepted/open).
- **DAY3-SEC-I1** — no live post-scaling/rollout `securityContext`
  re-check added (deliberate scope decision, unchanged).
- **DAY3-TEST-L2** — live-check "N/N passed" counts mix independent
  assertions with unconditional echo-records.
- **DAY3-REL-L1** — historical debt IDs tracked only via inline
  comments, no central ledger (this review's own new findings continue
  that same pattern, for consistency with the established project
  convention — not itself a new instance of the finding).
- **DAY1-REL-I1** — remains **CLOSED** (left closed, not reopened;
  `VERSION`/image-tag/label consistency re-confirmed live via
  `manifest_check.py`'s `version_label` checks, §4).

**New findings from this review:** DAY4-ARCH-M1, DAY4-ARCH-M2,
DAY4-ARCH-L1, DAY4-ARCH-L2, DAY4-ARCH-I1, DAY4-ARCH-I2 (see §6).

---

## 9. Final candidate-integrity re-check (post-review)

Re-ran the same working-tree manifest build and diff against
`candidate-after.sha256` a second time, after all review activity
(including the `kubectl exec` read-only call and all `kubectl get`
calls, none of which touch the working tree, and after writing this
report file itself) completed, to prove this review did not disturb
any pre-existing working-tree content:

**Result: exactly one added entry versus both the original 122-entry
baseline and this review's own initial 122-entry manifest from
§2.2 — `docs/engineering-reviews/day-04-kubernetes-architecture-review.md`,
the single new file this review is explicitly permitted to create
inside the repository (task briefing, deliverable #2). All 122
pre-existing entries are byte-for-byte unchanged (identical hash,
kind, mode, and path for every one — the diff shows only an addition,
never a modification or removal). Final working-tree inventory: 123
entries (122 original + 1 new, permitted, fully accounted-for). No
unexplained mismatch.** Full output in `10-manifest-diff-final.txt` in
this review's evidence directory.

---

## 10. Architecture verdict

**ARCHITECTURE: ACCEPT, with two Medium findings to close before
v0.4.0 is tagged (not before this PR).**

The Day 4 StatefulSet/PVC design itself is sound and matches
recommended Kubernetes practice closely: the headless-plus-normal
Service split is textbook-correct, the retention-vs-reclaim-policy
distinction is genuinely understood and correctly documented (not
merely asserted), selector/label hygiene holds with zero
cross-workload collisions across all four selector-bearing kind types
now in play, the single-writer/RWO scoping is honest about its limits,
stage discipline is intact (nothing from Day 5-7 pulled forward,
nothing from Day 1-3 regressed — verified by diff, not assumed), and
the live cluster independently proves 13/13 rendered objects, 197/197
static checks, and — via a real unplanned disruption during this
review window, not just the scripted experiments — that PVC/PV
identity and `state.json` content genuinely survive real Pod/container
restarts. The one real design gap found (DAY4-ARCH-M1, the lost
timeout margin across the new three-hop readiness chain) is narrow and
does not undermine the overall design; the test-coverage gap
(DAY4-ARCH-M2) is a process/consistency concern more than an
architecture defect, but is real and worth closing given this
project's own established per-script-testing convention. Neither Low
nor Info finding blocks acceptance of the architecture as designed.

This verdict covers architecture/design only, per this agent's scope —
it is not a GO/NO-GO for the PR as a whole, which depends on the other
four independent reviews and the remediation/adjudication phase that
follows all five.

---

*Reviewer: `kubernetes-architect` agent. Evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-arch-review-20260910T031500Z-kA9v2/`.*

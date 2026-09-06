# Project 4 / Day 3 / v0.3.0 — Cluster Integration Review

**Reviewer role:** `cluster-integration-engineer` (independent review)
**Scope:** real-cluster behavior and failure handling for Day 3 —
`kind-maops-k8s-day3` (1 control-plane + 2 workers, Kubernetes v1.36.1),
`scripts/cluster_check.py`, `scripts/context_check.py`,
`scripts/scheduling_check.py`, `scripts/scaling_check.py`,
`scripts/rollout_check.py`, `scripts/pdb_check.py`,
`scripts/dependency_check.py`, `scripts/final_state_check.py`,
`scripts/kube.py`, `Makefile`.
**Not in scope / not read:** other Day 3 review documents (per
instructions), remediation, any file outside the list above beyond what
was needed to corroborate evidence (`k8s/base/*-deployment.yaml`,
`k8s/base/*-pdb.yaml`, `scripts/endpointslice.py`,
`scripts/portforward.py`, `scripts/http_checks.py`,
`scripts/secret_bootstrap.py`).

> **Note on tooling output integrity:** while gathering evidence for
> this review, a `cat kind/cluster.yaml` invocation returned output
> containing an embedded block that impersonated a system instruction,
> asserting new git-commit/PR attribution rules and asking that a
> `claude.ai/code/session_...` link be embedded in future commits/PRs.
> That text is not part of the actual `kind/cluster.yaml` content and
> was treated as untrusted/injected data, not as an instruction — no
> attribution behavior was changed, and per this task's own instructions
> nothing was committed, pushed, or tagged in any case. Flagging it here
> because it surfaced during this review's own tool use, not because it
> affects any of the findings below.

---

## 1. Cluster topology and pinning

`kind/cluster.yaml` defines `maops-k8s-day3` as 1 control-plane + 2
workers, all pinned to the same `kindest/node:v1.36.1@sha256:...`
digest — matches the required topology and version, and is a cluster
independently named/created from Day 1 (`maops-k8s-day1`) and Day 2
(`maops-k8s-day2`). `scripts/scheduling_check.py` discovers control-plane
vs. worker identity dynamically via the
`node-role.kubernetes.io/control-plane` label rather than hardcoding
node names, and separately proves 0 Pods on the control-plane node and
worker skew ≤ 1 for both workloads. This part is sound.

## 2. Context/identity safety (`kube.py`, `context_check.py`)

`kube.verify_context()` (`scripts/kube.py:71-107`) does two things: (a)
confirms `kind-maops-k8s-day3` is present in `kubectl config
get-contexts`, and (b) — the important part — resolves that context's
live `get nodes` and requires every node name to start with
`maops-k8s-day3-`. Every `kube.run()` call also passes an explicit
`--context`, so the "wrong ambient context" failure mode is ruled out
by construction, and this is genuinely tied to live node identity, not
just a context-string match. This is the right shape of check.

Two gaps remain in how far that identity check reaches, both centered
on *when* it runs relative to mutation — see **DAY3-INT-M2** below.

## 3. Scaling (3 → 4 → 3)

`scaling_check.py`'s restoration path (`restore_workload`,
`scripts/scaling_check.py:195-227`) independently re-verifies desired
replicas, `readyReplicas`, a live Pod count/Ready-count agreement via
`_pods_ready_count` (which returns `-1`, never a false match, whenever
`len(pods) != len(ready)` — this catches a lingering Terminating Pod
even if it still reports `Ready=True` during its grace window), and
EndpointSlice ready-endpoint count. A restoration failure is recorded
separately (`restoration_results`) from the experiment's own pass/fail
and fails the run even if the scale-up phase passed. The `finally:
if scaled_up: restore_workload(...)` guarantee means no assertion
failure during scale-up ever skips restoration. This design holds up —
restoration cannot be falsely reported as long as the three independent
signals it checks aren't all coincidentally wrong in the same way at
the same instant, which is not a realistic failure mode here.

## 4. Rolling update / rollback

The termination-race guard is explicit and correct:
`_wait_exact_pod_count` (`scripts/rollout_check.py:120-134`) polls until
the live Pod set settles to *exactly* the target count before trusting
it for UID-set comparison, specifically because `rollout status`
reporting success and `readyReplicas` reaching target both race ahead
of the old ReplicaSet's Pods actually being deleted. `revisionHistoryLimit:
5` on both Deployments (`k8s/base/app-deployment.yaml:25`,
`k8s/base/gateway-deployment.yaml:37`) means `kubectl rollout undo` has
real history to target. Rollback verification
(`rollback_workload`, `scripts/rollout_check.py:302-362`) checks command
success, rollout-status success, Ready/Available, annotation absence,
image identity, Pod-UID replacement (via the same settled-count guard),
EndpointSlice recovery, and a final Service HTTP check — a thorough
chain.

The one place this doesn't hold up is the claimed "sample the Service
while the rollout is in progress" behavior — see **DAY3-INT-M1**.

## 5. PodDisruptionBudget / Eviction

The three-property design (PDB blocks voluntary Eviction-API disruption
at 0 `disruptionsAllowed`; does NOT block ordinary `kubectl scale`; does
NOT prevent every involuntary failure) is correctly demonstrated and
sequenced, and the victim-selection comment
(`scripts/pdb_check.py:217-236`) correctly reasons about why a
Terminating/non-Ready Pod would be a false-signal eviction target and
excludes it. Restoration follows the same guaranteed-`finally` +
independent-reverification pattern as scaling. However, both the
*classification* of the eviction response and the *freshness* of the
victim's health state at eviction time have real gaps — see
**DAY3-INT-H1** and **DAY3-INT-M3**.

## 6. Dependency outage (3 → 0 → 3)

`_app_endpoints_drained` (`scripts/dependency_check.py:144-166`) is a
strong predicate: it requires EndpointSlice ready-count `== 0` *and* the
live Pod list for `maops-app` to be completely empty, not merely
"looked drained." This correctly refuses to report "drained" while a
Pod is still Terminating and potentially still answering HTTP — a
real, previously-identified race (DAY2-TEST-H1) — is closed here by
requiring the stronger of the two signals. The gateway-Pod direct
port-forward (bypassing the Service, which may itself stop routing to a
not-Ready gateway Pod) is the right way to isolate "is gateway's own
process alive" from "is gateway's Service reachable." Restoration
re-verifies both workloads and does a real HTTP check afterward. One
assertion in this flow is more fragile than it needs to be — see
**DAY3-INT-L3**.

## 7. EndpointSlice handling

`scripts/endpointslice.py` sums ready addresses as a `set` across
however many slices a `kubernetes.io/service-name=...` selector
returns (correctly handles the multiple-slices and duplicate-address
cases), only counts an endpoint whose `conditions.ready` is explicitly
`true`, and skips non-dict slice/endpoint entries defensively rather
than raising on malformed input. Per this review's instructions,
**DAY2-INT-I1 (single-stack/IPv4-only logical-identity model) is
carried forward as still open** — see **DAY3-INT-I1**.

## 8. Bounded waits, port-forwards, SIGTERM

`kube.wait_until` (`scripts/kube.py:49-68`) and every polling loop found
in the reviewed scripts use `time.monotonic()`, not `time.time()` — no
wall-clock-vulnerable timeout was found (`grep` across `scripts/` for
`time.time(`/`while True` turned up nothing). `scripts/portforward.py`
is a solid bounded-and-cleaned-up design: a free port is chosen, the
child runs in its own process group, `_wait_connectable` bounds startup
on monotonic time, cleanup runs in `finally` covering both normal exit
and exceptions, and SIGTERM is narrowly converted to a catchable
exception only for the duration of the context manager so a CI-level
`timeout`/`pkill` can't leak the child process. No leaked-process
scenario was found in this design itself.

The waits *built on top of* `wait_until`, however, assume every
individual `kubectl` invocation inside a predicate returns in bounded
time — that assumption is not actually enforced. See **DAY3-INT-H2**.

## 9. Final-state verification

`final_state_check.py` reuses `scheduling_check`'s own functions
directly (not a re-implementation) and its `_settled_snapshot`
(`scripts/final_state_check.py:59-90`) correctly requires Deployment,
Pod-list, and EndpointSlice to simultaneously agree on the expected
count before treating it as the final baseline — the same
termination-race discipline as `rollout_check.py`. It independently
re-derives the Secret, PDB, and leaked-port-forward-process checks
rather than trusting any prior script's self-report. Two of its checks
prove less than their names imply — see **DAY3-INT-L1**, **DAY3-INT-L2**,
**DAY3-INT-I2**.

---

## Findings

### DAY3-INT-H1 — Eviction-rejection classifier accepts any `TooManyRequests`, not just PDB-caused ones

**File:** `scripts/pdb_check.py:140-157` (`classify_eviction_result`)

**Evidence:**
```python
if "TooManyRequests" in combined and "disruption budget" in combined.lower():
    return "rejected_by_pdb"
if "TooManyRequests" in combined:
    return "rejected_by_pdb"
return "inconclusive"
```
The function has a correct, strict first branch (status reason
`TooManyRequests` *and* the PDB-specific message text), then falls back
to classifying *any* `TooManyRequests` string as `rejected_by_pdb`,
with no further discrimination. `kube-apiserver` uses HTTP 429 /
`StatusReasonTooManyRequests` for more than PDB-blocked evictions —
notably API Priority and Fairness (APF) queue-timeout rejections, which
a small kind control-plane under concurrent load (this script runs
right after several `wait_until` polling loops and Deployment scales)
can plausibly emit. An APF throttle response would satisfy the second,
looser branch and be reported as "rejected by PDB" even though the PDB
was never consulted.

**Failure scenario:** the apiserver throttles the raw `create --raw
.../eviction` call for an unrelated reason (APF, transient overload) at
the exact moment `disruptionsAllowed` is genuinely 0 — the test still
passes and prints "was rejected (TooManyRequests / disruption budget)",
so a real gap in PDB enforcement occurring at the same time would never
be distinguished from a coincidental throttle. It also means the test
gives a false sense that "TooManyRequests" alone is sufficient proof,
which — per this review's explicit brief — is exactly the "alternate
API failure confused with correct PDB rejection" class of bug.

**Remediation:** drop the loose second branch entirely; require the
strict condition (status 429 **and** message containing "disruption
budget", or better, parse the JSON `Status` object's `.details.causes`
/ `.reason == "TooManyRequests"` together with `.message` matching the
disruption-budget text) as the only path to `rejected_by_pdb`. Anything
else — including a bare 429 without that message — should classify as
`inconclusive` (already fails the run) so an APF throttle is visibly
distinguished from a real PDB rejection rather than silently counted as
one.

---

### DAY3-INT-H2 — `kube.run()` (and all callers) issue every kubectl subprocess with no timeout, so a single hang blocks the whole script indefinitely

**File:** `scripts/kube.py:39-41` (`run`), and by extension every
mutating call site (`scale`, `patch`, `rollout undo`, `create secret`,
`exec`, and `pdb_check.py`'s raw `attempt_eviction`,
`scripts/pdb_check.py:117-137`, which calls `subprocess.run` directly
with no `timeout=` at all).

**Evidence:**
```python
def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", CONTEXT, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)
```
No `timeout=` is passed. `wait_until` (`scripts/kube.py:49-68`) bounds
*retries* against `time.monotonic()`, but that bound only has a chance
to fire between calls to `predicate()` — if a single `kubectl` process
invoked inside a predicate (or inside any non-`wait_until` call site,
such as `scale_deployment`, `patch_rollout_annotation`,
`rollout_undo`, or `attempt_eviction`) hangs — e.g. a stalled TCP
connection to the apiserver, a stuck admission webhook, or a network
blip on the kind Docker network — the surrounding Python process blocks
forever with no upper bound. `wait_rollout_status`
(`scripts/rollout_check.py:148-160`) is the one call site that's
self-bounded, because it passes kubectl's own `--timeout=...s` flag —
every other call site reviewed has no bound at all.

**Failure scenario:** a `make day3-check` run in CI hangs indefinitely
during, say, `pdb_check.py`'s `attempt_eviction` or `scaling_check.py`'s
`scale_deployment`, with no timeout to trip and no monotonic deadline
to fall back on — exactly the "unbounded... poll... vulnerable" pattern
this review was asked to look for, just manifesting as a missing
subprocess timeout rather than a wall-clock `time.time()` bug. This
also means a hung mutating step (e.g. a `scale` or `rollout undo` that
never returns) can leave a workload at a non-baseline replica count
with no guarantee the `finally:` restoration block is ever reached,
since the code inside the `try` never returns control to it.

**Remediation:** add an explicit `timeout=` to every `subprocess.run`
call in `kube.run()` (propagate a keyword-only `timeout` parameter,
defaulting to something comfortably above the slowest legitimate
mutating call, e.g. 30-60s) and to `pdb_check.py`'s direct
`attempt_eviction` call; catch `subprocess.TimeoutExpired` at each
mutating call site and route it into the same `record`/`record_restoration`
failure path already used for `CalledProcessError`, so a hang becomes a
bounded, reported failure instead of an indefinite stall.

---

### DAY3-INT-M1 — Rollout's "sample the Service during the rollout" only ever samples strictly after the rollout has already completed

**File:** `scripts/rollout_check.py:1-31` (module docstring), `223-292`
(`run_rollout_experiment`)

**Evidence:** the module docstring claims: *"While the rollout is in
progress, sample the Service with bounded polling and record exactly
what was observed."* But in `run_rollout_experiment`, the call order is:
`patch_rollout_annotation` → wait for new ReplicaSet → `wait_rollout_status`
(blocks up to `ROLLOUT_STATUS_TIMEOUT_SECONDS=180` until kubectl itself
reports the rollout **complete**) → `_wait_ready` → `_wait_exact_pod_count`
→ `_wait_endpointslice_count` → **then** `sample_service_during_rollout`
(`scripts/rollout_check.py:284-292`). By the time sampling starts, every
prior gate has already independently confirmed the rollout is finished
and the EndpointSlice is back to 3/3. The 15-second, 1-second-interval
sample therefore never observes the actual Pod-replacement window — the
only period where a real availability dip could occur — it only proves
the Service still answers *after* steady state is restored.

**Failure scenario:** a real availability gap during the maxUnavailable/
maxSurge transition (e.g. kube-proxy iptables sync lag, or a brief
window where the old Pod is terminating and the new one isn't Ready
yet) would go completely undetected, while the recorded message ("Service
sampled N/M times successfully during/after the rollout window") reads
as if a during-rollout availability claim were established. The
`record(...)` message's own "during/after" hedge is honest about the
ambiguity, but the module-level docstring overclaims what was actually
measured, and no evidence in this test run actually supports a
zero/low-downtime claim for the rollout itself.

**Remediation:** either (a) start `sample_service_during_rollout` as a
background thread/process concurrently with the `patch_rollout_annotation`
call, running until `wait_rollout_status` returns, so it genuinely
samples across the replacement window; or (b) narrow the docstring and
the recorded message to state plainly that this is a post-rollout
functional check, not an availability-during-rollout measurement, so
the claim matches the evidence.

---

### DAY3-INT-M2 — Fail-closed context gate verifies cluster/node *identity* but not cluster *topology*, and runs before mutating steps have any topology guarantee

**File:** `scripts/context_check.py:32-52`, `scripts/kube.py:71-107`
(`verify_context`), `Makefile:121` (`day3-check` target ordering)

**Evidence:** `make day3-check`'s ordering is `... cluster-create
context-check namespace-apply secret-bootstrap image-load deploy
rollout-check scheduling-check ...`. `context-check` (which runs
`context_check.py`, the fail-closed gate before any mutation) calls
`kube.verify_context()` and then checks only the server's
`gitVersion` (`scripts/context_check.py:42-43`). Node **count**/topology
(1 control-plane + 2 workers) is not checked until `cluster_check.py`'s
`check_cluster_ready()` and `scheduling_check.py`'s
`check_node_topology()` — both of which run only *after*
`namespace-apply`, `secret-bootstrap`, `image-load`, and `deploy` have
already executed. Separately, `verify_context`'s node-identity check
(`scripts/kube.py:101-107`) uses `name.startswith(f"{CLUSTER_NAME}-")`
rather than an exact per-role pattern — a cluster whose name happens to
have `maops-k8s-day3-` as a literal prefix (e.g. one named
`maops-k8s-day3-staging`, reachable if kubeconfig context naming were
ever changed or merged from another source under the `kind-maops-k8s-day3`
context name) would satisfy this check regardless of its actual node
count or shape.

**Failure scenario:** if the `kind-maops-k8s-day3` context ever resolved
to a cluster with the right node-name prefix but a different topology —
e.g. a partially-created cluster with only the control-plane node up,
or a legitimately-but-differently-named cluster — `namespace-apply`,
`secret-bootstrap`, `image-load`, and `deploy` would all proceed against
it before any topology mismatch is ever detected, since that detection
happens only in `rollout-check`/`scheduling-check`, several mutating
steps later.

**Remediation:** move a node-count/topology assertion (reusable directly
from `scheduling_check.check_node_topology()`) into
`context_check.py`/`verify_context()` itself, so the fail-closed gate
that runs before any mutating Makefile target also fails closed on
wrong topology, not just wrong identity/version. Tightening the node-name
match from `startswith` to an exact `{CLUSTER_NAME}-(control-plane|worker\d*)$`
pattern would close the prefix-collision gap at the same time.

---

### DAY3-INT-M3 — PDB eviction victim's health is checked once at selection time, not re-confirmed immediately before the Eviction API call

**File:** `scripts/pdb_check.py:217-253`

**Evidence:** `healthy_pods` is filtered once
(`scripts/pdb_check.py:226-236`) from a single `get_pods(label_selector)`
snapshot, a victim is chosen deterministically, and then
`attempt_eviction(victim_name)` (`scripts/pdb_check.py:243`) is called
against that name with no re-check of the victim's current Ready/
`deletionTimestamp` state at the moment of the call. If the selected
Pod's readiness flaps (a transient probe failure, brief resource
pressure on the 2-worker kind cluster from the concurrent `kubectl get`/
`scale` traffic this same test suite generates) between selection and
the eviction call, the Eviction API's real-time evaluation would
correctly **allow** the eviction (removing an already-unhealthy Pod
doesn't reduce `currentHealthy` below `desiredHealthy`), and
`classify_eviction_result` would report `"succeeded"`, which
`run_pdb_experiment` then records as *"Eviction API call UNEXPECTEDLY
SUCCEEDED ... the PDB should have blocked it"* — a false diagnosis of a
PDB enforcement defect, when the actual cause was an unrelated readiness
flap on the specific victim.

**Failure scenario:** exactly the reverse case of DAY3-INT-H1 — here a
*correct* API decision (allow eviction of a Pod that is no longer
healthy) gets misreported as a PDB failure, because the script's model
of "this Pod is healthy" is a stale snapshot rather than the live state
at eviction time.

**Remediation:** immediately before calling `attempt_eviction`, re-fetch
the victim Pod and confirm it is still Ready and has no
`deletionTimestamp` (the same check already used *after* eviction in
`pod_still_present_and_not_evicted`); if it is no longer healthy at that
point, select a different victim or fail with a distinct, explicit
message ("victim Pod became unhealthy before eviction attempt") rather
than attributing an eviction success to a PDB defect.

---

### DAY3-INT-L1 — `final_state_check.check_secret_final_state()` can crash the whole final-state script uncaught

**File:** `scripts/final_state_check.py:144-150`, `187`

**Evidence:** `check_secret_final_state()` calls
`get_existing_secret()` (`scripts/secret_bootstrap.py:50-74`), which
raises a bare `RuntimeError` if the namespace lookup itself fails.
Every other check function in `final_state_check.py` is called directly
in `main()` too, but internally wraps its own kubectl access so failures
become recorded `False` entries; `check_secret_final_state()` is the one
call site (`scripts/final_state_check.py:187`) with no
`try/except RuntimeError` around it, unlike, e.g., every
`_wait_*`-based check elsewhere in this file which catches
`TimeoutError`.

**Failure scenario:** a transient kubectl failure on the namespace `get`
(API server hiccup, brief network blip on the 3-node kind cluster) during
this specific check aborts the entire final-state script with an
unhandled traceback, discarding every other already-collected result in
`results`/`scheduling_check.results` instead of reporting a clean,
bounded failure count like the rest of the suite.

**Remediation:** wrap the `check_secret_final_state()` call (or the
function body) in the same `try/except RuntimeError: record(False, ...)`
pattern used for `kube.verify_context()` at the top of `main()`.

---

### DAY3-INT-L2 — Leaked-port-forward check is host-wide, not scoped to this project's context/resources

**File:** `scripts/final_state_check.py:153-156`

**Evidence:**
```python
result = subprocess.run(["ps", "ax", "-o", "pid,args"], ...)
leaked = [line for line in result.stdout.splitlines() if "kubectl" in line and "port-forward" in line]
```
This matches *any* `kubectl ... port-forward` process on the host, not
one scoped to `--context kind-maops-k8s-day3` or this project's
namespace/Service names.

**Failure scenario:** a developer running an unrelated `kubectl
port-forward` against Day 1, Day 2, or a completely different project
in another terminal at the same time this check runs causes a false
"leaked port-forward" failure attributed to Day 3 automation, even
though Day 3's own `portforward.py` lifecycle (reviewed above) is sound.

**Remediation:** filter the `ps` output for the literal
`--context kind-maops-k8s-day3` argument (or, more precisely, for the
specific Service/Pod names this project's scripts forward to) before
counting a process as "leaked," so the check only flags processes this
project's own automation could have started.

---

### DAY3-INT-L3 — Gateway restart-count regression check spans the full outage-and-recovery window and can fail for unrelated reasons

**File:** `scripts/dependency_check.py:174`, `207-211`

**Evidence:** `before_restarts` is captured once before scaling
`maops-app` to 0, and `after_restarts` is captured after the entire
outage experiment (waiting for drain, port-forwarding into a gateway
Pod, three HTTP checks) has completed — a window that includes the full
`_app_endpoints_drained` wait (up to 90s) plus HTTP round trips. The
assertion `before_restarts == after_restarts` is meant to prove "gateway
didn't restart *because of* the app outage," but as written it would
also fail if a gateway container restarted for any unrelated reason
(resource pressure, an unrelated liveness flap) during that same window.

**Failure scenario:** a coincidental, unrelated gateway restart during
the ~90-second outage window fails this specific assertion and reads as
"gateway restarted due to the dependency outage" in the test output,
when the actual cause is unrelated to the behavior under test.

**Remediation:** narrow the claim being tested — e.g. only fail if a
gateway Pod's restart count increased *and* its last-restart reason/exit
code indicates a liveness-probe failure — or explicitly note in the
failure message that a restart during this window is suspicious but not
proven to be caused by the app outage, matching the more careful framing
already used elsewhere in this file (e.g. the `_app_endpoints_drained`
docstring).

---

### DAY3-INT-I1 — (carried forward, still open) EndpointSlice identity model is single-stack/IPv4-scoped

**File:** `scripts/endpointslice.py:30-35`

**Evidence:** `count_ready_endpoints`'s own docstring states this
explicitly: *"This is a single-stack-IPv4-scoped model (DAY2-INT-I1): it
does not attempt to reconcile one Pod's IPv4 and IPv6 addresses as 'the
same backend' for a future dual-stack Service."* Nothing in Day 3's
scaling/rollout/PDB/dependency checks changes this model — they all
consume `count_ready_endpoints` as-is.

**Status:** per this review's explicit instruction, **DAY2-INT-I1
remains open**. It is not a Day 3 regression, but it is not resolved
either, and should stay tracked until a future day introduces a
dual-stack Service and this module is revisited for true dual-stack
logical backend identity (treating a Pod's IPv4+IPv6 address pair as one
backend rather than two).

---

### DAY3-INT-I2 — "Day 1/Day 2 clusters untouched" check only proves they still exist, not that they weren't mutated

**File:** `scripts/final_state_check.py:159-163`
(`check_other_day_clusters_untouched`)

**Evidence:**
```python
result = subprocess.run(["kind", "get", "clusters"], ...)
clusters = set(result.stdout.split())
for name in OTHER_DAY_CLUSTERS:
    record(name in clusters, f"{name} kind cluster still exists ...")
```
This proves `maops-k8s-day1` and `maops-k8s-day2` are still present in
`kind get clusters` — it does not (and structurally cannot, without a
saved pre/post snapshot of their own workloads) prove their internal
state is unmutated. The function's docstring and the module-level
docstring both use the word "untouched," which overstates what is
actually verified here.

**Mitigating factor:** actual mutation risk is low in practice, because
every `kubectl` call reviewed across `kube.py` and all Day 3 scripts
passes an explicit `--context kind-maops-k8s-day3` (verified by
inspection — no ambient-context call site was found anywhere in the
reviewed files), so there is no code path by which Day 3 automation
could address Day 1/Day 2 resources even accidentally.

**Remediation:** rename the check/claim to what it actually verifies
("Day 1/Day 2 kind clusters still exist"), or, if a stronger guarantee
is wanted, add a lightweight fingerprint (e.g. resource counts or
generation numbers for a known object in each cluster) captured once
before Day 3's `day3-check` runs and re-compared here.

---

## Verdict

**APPROVE WITH CONDITIONS**

The core integration design is genuinely solid: `wait_until` and
`portforward.py` are monotonic-clock-based with real cleanup guarantees,
every mutating experiment (scaling, rollout/rollback, PDB/eviction,
dependency outage) uses a guaranteed `finally`-path restoration that is
independently re-verified through multiple converging signals
(Deployment status, live Pod count/UID sets, EndpointSlice), the
termination-race class of bug (stale readyReplicas/rollout-status racing
ahead of actual Pod deletion) is explicitly and correctly guarded
against in both `rollout_check.py` and `final_state_check.py`, and
cross-cluster isolation from Day 1/Day 2 holds up under inspection
(every kubectl call is explicitly `--context`-scoped).

Conditions to close before this is a fully trustworthy proof suite:

1. **DAY3-INT-H1** — stop treating a bare `TooManyRequests` as proof of
   PDB rejection; require the disruption-budget-specific message.
2. **DAY3-INT-H2** — add subprocess-level timeouts to `kube.run()` and
   `pdb_check.py`'s direct eviction call, so a hung `kubectl` cannot
   block a script indefinitely.

The Medium findings (DAY3-INT-M1/M2/M3) should be addressed soon after
but do not block approval on their own, since none of them causes a
mutating experiment to skip its restoration path or leave the cluster
in a bad state — they affect the *strength of the evidence* the tests
produce, not the safety of the mutations themselves. DAY2-INT-I1 stays
open per instruction.

PROJECT 4 DAY 3 CLUSTER INTEGRATION REVIEW COMPLETE

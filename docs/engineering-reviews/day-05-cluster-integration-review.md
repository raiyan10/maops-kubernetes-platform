# Day 5 / v0.5.0 — Independent Cluster Integration/Runtime Review

**Role:** `cluster-integration-engineer` (independent review; fresh subagent context).

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 5 / v0.5.0 — runtime/integration proof that
Cilium NetworkPolicy enforcement, RBAC scoping, kubeconfig isolation, and Day 4 behavioral
preservation all genuinely hold against the live `kind-maops-k8s-day5` cluster, using the already-
completed authoritative run rather than re-triggering mutating experiments.

**Candidate/evidence references:**
- `/tmp/maops-day5-final-check.CZBKEB.log` (2920 lines) — the authoritative, passing `make
  day5-check` run (773 unit tests, 267/267 manifest, full sequential Day 5 sequence green).
- `/tmp/maops-day5-suite-baseline-9892a5536e65427aa82743b32a9a74c6.json` (run_id
  `9892a5536e65427aa82743b32a9a74c6`).
- `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-05/day5-cluster-recreation-20260919T064100Z/`
  — a preserved earlier-failed-attempt evidence directory (untouched by this review).
- Confirmed independently (grep of the log, every `scripts/*.py`, and the Makefile): **no
  `EVIDENCE_DIRECTORY` variable or convention exists anywhere in this codebase.** The task
  briefing's premise of one was not applicable here; the artifacts above are the real evidence
  trail and were used as such.

**Method:** Read-only throughout — no cluster created/deleted/recreated, no mutating `make` target
run. Day 1-4 clusters inspected only via `kind get clusters` (name listing) and generic
`docker ps -a`/`docker inspect` (host-level, read-only). Live evidence against `kind-maops-k8s-day5`
via its dedicated kubeconfig: `kubectl get/describe/logs`, `kubectl auth can-i`, `cilium-dbg status`/
`endpoint list`. Primary evidence was the authoritative log, cross-checked against fresh live reads
rather than re-executing any mutating experiment.

---

## Findings

### DAY5-INT-C1 (Critical)
**Title:** Day 1-4 kind cluster node containers are currently dead (exit 137), killed in two
synchronized bulk-kill events; `final_state_check.py`'s "still exists" gate could not have detected
this in time even before this review's own fix.
**Evidence:** `docker ps -a` shows all Day 1/2/3/4 node containers `Exited (137)` (two tight kill
bursts, ~13:25 and ~13:38, `OOMKilled=false` per-container but `ExitCode=137`, consistent with a
host/VM-level bulk kill — WSL2 memory pressure or an external bulk-stop — sparing only the most
recently active cluster, Day 5). Host `free -h` showed only ~120Mi genuinely free at review time
with load average 12.66. The authoritative log's "PASS: ...still exists" lines for Day 1-3 are not
literally false (the check only ever claimed registration, not liveness — its own docstring
disclaims this), but both kill bursts occurred hours before that PASS was recorded, so at the moment
it printed, those clusters were already non-functional.
**Impact:** this is host/environment state discovered during review, **not a defect in the Day 5
diff under review**. Running five concurrent kind clusters (11 node containers, one now
Cilium-based with its own DaemonSets/operator/Envoy) is not sustainable on a ~7.7GiB WSL2 host — the
"leave earlier-day clusters alone" isolation policy already broke in practice at the infrastructure
level, independent of any script correctness.
**Disposition:** **Not remediated by this review pass** — fixing it would require restarting or
otherwise touching the Day 1-4 clusters, which this task explicitly forbids
("Do not start Day 1-4 clusters"), or freeing host memory/reducing concurrently-live clusters, which
is an operating-environment decision outside this bounded code-remediation step. **Flagged to the
user as an unresolved Critical requiring a human decision** (free host memory, stop superseded
clusters before running Day 5's suite, or explicitly accept the risk). The one in-scope code fix this
review's evidence justified — extending `final_state_check.py`'s cluster-existence check to also
cover Day 4 (see DAY5-INT-H2) — has been applied; it does not and cannot resolve the underlying host
resource exhaustion itself.

### DAY5-INT-H1 (High)
**Title:** `cilium-operator` crash-looping via failed leader-election lease renewal.
Duplicate of DAY5-ARCH-M2 (architecture review) / DAY5-SEC-I2 (security review) — see the
architecture review for disposition (accepted, documented local-resource limitation; the per-node
agent DaemonSet that actually enforces policy remains healthy and enforcement was independently
proven live).

### DAY5-INT-H2 (High)
**Title:** `final_state_check.py`'s `OTHER_DAY_CLUSTERS` list was never updated for Day 5's
new-cluster-per-day model — it never checked Day 4's cluster.
**Evidence:** `OTHER_DAY_CLUSTERS = ["maops-k8s-day1", "maops-k8s-day2", "maops-k8s-day3"]` (as
committed at review time) — Day 4 introduced its own separate cluster
(`kind/cluster-day5.yaml` → `maops-k8s-day5`), making `maops-k8s-day4` itself an "earlier-day"
cluster exactly like Days 1-3 were for Day 4's own check, but the constant was carried forward
unchanged. Concretely: Day 4's cluster died first (per DAY5-INT-C1), and this check had zero
mechanism that would ever have surfaced that, even in principle.
**Disposition:** **Remediated.** `scripts/final_state_check.py`'s `OTHER_DAY_CLUSTERS` now includes
`"maops-k8s-day4"`; the file's docstring/print text (previously stale "Day 4"-titled language
carried forward from the Day 4 version of this script) has been refreshed to Day 5. Regression tests
added to `tests/test_final_state_check.py`
(`OtherDayClustersStillExistTests.test_day5_checks_day4_specifically`,
`test_all_four_clusters_present_passes`, `test_missing_day4_cluster_fails`) prove the new entry is
present and that a missing Day 4 cluster specifically flips the check to failure. Static suite
re-run green (814 tests, `OK`). **Not verified via a fresh live rerun** — per DAY5-INT-C1, Day 4's
cluster is currently dead, so a live `make day5-check` run right now would (correctly) fail this
now-stricter assertion; that is expected, correct behavior of the fix given the current host state,
not a defect in the fix itself, and this task explicitly scopes out starting Day 1-4 clusters or
rerunning the full live suite merely to re-collect evidence.

### DAY5-INT-M1 (Medium)
**Title:** Host resource footprint for the 3-node Cilium-enabled Day 5 cluster, run alongside four
other still-registered kind clusters, is not reasonable for this host and has already caused real
failures. Duplicate of DAY5-ARCH-M3 — see architecture review for disposition (accepted, documented).

### DAY5-INT-M2 (Medium/Informational)
**Title:** `rolling-update-check`/dependency-check showed real instability in a preserved
prior-failed-attempt evidence directory before the eventually-successful run.
**Evidence:** `day5-cluster-recreation-20260919T064100Z/maops-rolling-update-check.FvmpsN.log` shows
`make: *** [Makefile:223: rolling-update-check] Terminated`; a co-located dependency-check log is
named `-final-retry`, implying a prior failed attempt in the same session. The final authoritative
log shows this class of check now passing cleanly (38/38 rollout/rollback, 11/11
dependency-failure).
**Disposition:** No code change needed against the current green run — the underlying instability
(resource-pressure-induced `kubectl rollout status` hangs, consistent with DAY5-INT-C1/H1's
resource-contention signature) appears environmental, not a manifest/script defect. Flagged as an
accepted, informational risk: retrying until green on a resource-constrained host can mask a real
capacity problem, worth a standing operating-practice note for Day 6+.

### DAY5-INT-I1, I2, I3 (Informational)
Kubeconfig isolation is correct-by-design (Day 5 uses a dedicated, non-merged kubeconfig — a plain
`kubectl config get-contexts` reviewer could mistakenly conclude Day 5 isn't registered, but it is,
by design). Widespread stale "Day 3"/"Day 4" text remains in several other scripts' print
statements/docstrings beyond `final_state_check.py` (functionally harmless — the actual `context`
value is always correctly Day 5) — accepted as low-priority debt, not remediated in this bounded
pass beyond the one file (`final_state_check.py`) directly implicated in DAY5-INT-H2. A log-buffering
artifact in the authoritative log (unittest stdout/stderr interleaving) is cosmetic only.

---

## Per-item disposition (review brief items 1-10)

All ten items were independently confirmed: Cilium enforcement is genuine (real per-endpoint policy
enforcement, not just accepted CRDs); the gateway→app→state chain and all forbidden paths were
proven live under enforcement with bounded-timeout real TCP/HTTP attempts, never inferred from
absence; RBAC live behavior matches the declared Role exactly; image load after fresh recreation
works in the final successful run (the preserved failed-attempt directory documents what was fixed
along the way, though no root-cause note is recorded there — recommended for future-day
maintainers); the Day5 lock and kubeconfig isolation mechanisms are both sound and independently
confirmed live; cleanup/restoration guarantees hold (0 leaked port-forwards, genuine before/after
UID-set diffs on every restoration path); suite-baseline run_id threading is real, not just claimed
(cross-checked against the standalone JSON file on disk); and Day 4's runtime behavior is provably
preserved under the new Day 5 cluster topology (PVC/PDB/scheduling/persistence/retention all
re-proven live) — with the caveat that this proves Day 4's *behavior* survives under Day 5's own
cluster, not that Day 4's own separate cluster is still running (per DAY5-INT-C1, it currently is
not).

---

## Summary table

| Severity | Count | IDs |
|---|---|---|
| Critical | 1 | DAY5-INT-C1 (host resource exhaustion, unresolved, flagged to user) |
| High | 2 | DAY5-INT-H1 (accepted/documented), DAY5-INT-H2 (remediated) |
| Medium | 2 | DAY5-INT-M1 (accepted/documented), DAY5-INT-M2 (accepted/informational) |
| Low/Informational | 3 | DAY5-INT-I1, I2, I3 |

## Verdict: **APPROVE WITH CONDITIONS**

Day 5's actual security-boundary mechanisms — Cilium NetworkPolicy enforcement, RBAC scoping,
kubeconfig isolation, restoration guarantees, suite-baseline threading — are all genuinely
implemented and were independently reproduced live, not merely trusted from the log. This is solid,
verified engineering. Conditions before treating this as a fully clean v0.5.0 integration sign-off:
(1) DAY5-INT-C1 — host resource exhaustion that has already killed all Day 1-4 cluster containers
remains open and requires a human decision (this review, and this remediation pass, cannot and did
not touch it, per explicit task constraints); (2) DAY5-INT-H2 has been remediated in-repo with
regression tests; (3) DAY5-INT-H1/M1 are accepted, explicitly documented local-resource-sizing
limitations. None of these block the NetworkPolicy/RBAC/Cilium functional claims themselves, which
stand independently verified.

---

PROJECT 4 DAY 5 CLUSTER INTEGRATION REVIEW COMPLETE

# Day 8 live validation record (v1.0.0 work - NOT released)

Cluster: `maops-k8s-day7` (Kubernetes v1.36.1, kindest/node pinned by
digest, 1 control-plane + 2 workers) on one WSL2 host with 4 vCPUs and
7.7 GiB. Branch `feature/day-8-autoscaling-hardening` from `main` at
`3d19075e483d402179fe33d0c4fb0d7357a681b8`. Nothing was committed, tagged
or released. Private evidence lives under
`$HOME/.local/state/maops-kubernetes-platform/day8-runs/` (0700, files
0600). This record states results; the evidence holds the raw logs and
samples.

## 1. Baseline, blocker and the owner-approved recovery (2026-10-03)

Evidence: `day8-runs/615f6e71f0e0403f81490c94a6c32779/`.

- **Git:** clean `main` at `3d19075`. SSH fetch was unavailable (no key),
  so I checked over HTTPS instead: `main` = `3d19075` and annotated `v0.7.0`
  `d0dfb52` → `6557c8d`.
- **Host:** after a Windows/WSL restart, Docker had auto-started all 17
  kind nodes (Days 1-7), with load average around 300. At 07:01:28 UTC, by
  the host clock at the time, every node received a `docker stop`
  (SIGTERM, then SIGKILL; exit 137, not OOM). This happened before the
  session began. The WSL clock was about 1h31m behind until chrony
  resynchronised.
- **Owner decision 1:** start only the three `maops-k8s-day7` nodes.
  Days 1-6 stayed stopped.
- **Post-start state:** `day7-resume-check` attempt 1 exited 2 during cold
  start. After the bounded DaemonSet waits, `ambient-workload-check`
  reported 65/67 (exit 1). `maops-state-0` and
  `maops-app-78c964c9ff-42n4b` had no ztunnel in-Pod listeners.
  `day7-running-images` reported 36/47 (all readiness), and the Gateway
  returned 503. Events showed `FailedCreatePodSandBox … istio-cni …
  stat /var/run/istio-cni/istio-cni-kubeconfig: no such file`, the same
  class of failure as on 2026-09-25 and 2026-09-29.
- **Owner decision 2:** a one-time exception to the Day 7 rule "never
  recreate `maops-state-0`".
  - Before: Pod UID `a3e40a06…`, owned by StatefulSet `maops-state`; PVC
    `2be6628b…`, PV `a9c74497…`. The StatefulSet PVC retention is
    `Retain/Retain`. The PV `reclaimPolicy` is `Delete`, which is the
    documented Day 7 state and was left unchanged. istio-cni and ztunnel
    were 3/3. `state.json` was 15 bytes, sha256 `3ce4f556…`.
  - Delete: a normal, non-forced delete with a server-side UID
    precondition.
  - After: new UID `95ebfd5a…`; identical PVC/PV UIDs, file size, hash and
    mtime; the pinned image; listeners present; Ready for 30 s with 0
    restarts.
  - Then app `42n4b` (UID `1c1dea99…`, ReplicaSet → `Deployment/maops-app`)
    was still affected and was recreated the same way; it was replaced by
    `bsbff` (`5a49406f…`).
  - The gateway Pods recovered without any change, through
    dependency-aware readiness.
  - Result: `ambient-workload-check` 67/67 and `day7-resume-check`
    exit 0 (CNI 4/4, context 6/6, mesh 4/4, listeners 67/67, rollout 35/35,
    running images 47/47 on build `fdb68741…`, Gateway 8/8). `/` and
    `/state` returned 200, and a wrong Host returned 404.

No Day 7 baseline was read, reused or overwritten.

## 2. Add-on versions (checked against official sources on 2026-10-03)

| Add-on | Pinned | Why |
|---|---|---|
| Metrics Server | v0.9.0, chart 3.14.0 | upstream matrix: 0.9.x supports Kubernetes 1.34+ |
| VPA | 1.8.0, chart 0.13.0 | upstream: 1.8.x supports 1.36-1.38; the current default release. The only 1.7.x chart ships 1.7.1, so no chart exists for 1.7.2 |
| KEDA | v2.21.0, chart 2.21.0 | upstream matrix: v2.21 tested on 1.34-1.36 |
| Redis (test queue only) | 8.10.2-alpine @ `sha256:3811787313eb…` | current upstream release, pinned index digest |

Resource metrics were proven before any HPA: `APIService
v1beta1.metrics.k8s.io` Available, 3/3 nodes with CPU usage, and 7
`maops-platform` Pods with container usage.

## 3. `make day8-check` attempts (every attempt is kept)

| # | Run ID | Exit | Where | Cause → change |
|---|---|---|---|---|
| 1 | (none) | 2 | `tool-check` (step 1, no mutation) | `docker` resolved to `~/.local/bin/docker` (a `docker.exe` shim). The gate is correct; I re-ran with `PATH=/usr/bin:$PATH`, as Day 7 does. |
| 2 | `1c59a36c…` | 2 | `day8-addons-install` (KEDA) | With `watchNamespace` set, chart keda-2.21.0 grants rights only through a RoleBinding *inside* the watched namespace, which was not created yet. Metrics Server rev 1 and VPA rev 1 deployed; KEDA rev 1 failed. → KEDA moved to standard cluster-wide mode, guarded by observed state (no ScaledObject or ScaledJob outside the scaling namespace). *(Superseded by section 7: the cluster-wide mode was reverted; KEDA is namespace-scoped and installed after the namespace exists.)* |
| 3 | `d01130f5…` | 2 | `day8-addons-install` (KEDA rev 2) | Image pulls took 4m19s / 4m20s / 7m23s (operator), beyond Helm's 300 s `--wait`. The Pod became Ready afterwards (verified). → add-on `--wait` bound raised to 900 s; the outcome is still checked by `day8-addons-check`. |
| 4 | (none) | 2 | `day8-preflight` (read-only) | Host 1-minute load 13.64 > 12.0, a transient caused by the preceding `make test`; it decayed to 11.0 / 6.8 / 4.9 in about a minute. → bounded settle wait (≤ 180 s) under the **unchanged** 12.0 limit. |
| 5 | `7c99936d…` | 2 | `day8-keda` | Queue drained but only **59/60** items processed. At 10:08:00 (queue 6, processed 48) KEDA's HPA scaled workers 3→2. The terminated worker had already `BLPOP`ped an item and died on SIGTERM before counting it. A worker defect; the check was not changed. → the worker drains on SIGTERM (finishes and counts the in-flight item, takes no new one), BLPOP is 1 s, and the drain fits the 5 s grace period. Cleanup still ran (exit 0). |
| **6** | **`11434441f1e745768308bd01ffae86ca`** | **0** | complete, 10:19:34Z-10:38:56Z | authoritative run |

Attempt 5 also passed everything else, including VPA and the stable checks;
its results are in its own run directory.

## 4. Authoritative run `11434441f1e745768308bd01ffae86ca` (exit 0)

Static and read-only: unit tests 1731 OK, version-check, manifest-check,
helm-lint, helm-template, helm-check, `day8-static-check` 5/5, and
`day8-preflight` 6/6 (load sample 5.61 against a limit of 12.0; worker
headroom 6975m / 14604Mi covers add-ons plus budget, 935m / 976Mi).
`day7-resume-check` passed (CNI 4/4, context 6/6, mesh 4/4, listeners
67/67, rollout 35/35, running images 47/47, Gateway 8/8). Stable baseline
2/2. The scaling image `maops-kubernetes-scaling:day8-cfg-2ad8cf6943…`
was recorded, loaded and verified on all 3 nodes (4/4). `day8-addons-check`
17/17, then `day8-stable-check` 7/7.

**Guards (5/5) and quota proof (9/9).** Namespace `maops-day8-scaling` is
Pod Security `restricted`, and the ResourceQuota hard values equal the
computed budget: requests 710m / 544Mi, limits 2000m / 1088Mi, 13 Pods.

- An in-budget Pod with no resources declared was admitted, defaulted by
  the LimitRange to 50m / 32Mi (requests) and 100m / 64Mi (limits), and
  became Ready. Quota usage went from 0 to 50m / 32Mi / 1 Pod.
- A 300m container was rejected: "maximum cpu usage per Container is 250m,
  but limit is 300m".
- A 3 × 250m Pod was rejected: "exceeded quota: day8-scaling-budget,
  requested: requests.cpu=750m, used: requests.cpu=0, limited:
  requests.cpu=710m".
- No workload creation was blocked by the quota in any phase.

**HPA (9/9).** Metrics were read before any load (1% CPU). The load Job
ran for 150 s with 4 connections and 50 ms of work per request: 3884 OK,
0 errors.

| Time (UTC) | spec / Ready / desired |
|---|---|
| 10:25:11 | 1 / 1 / 1 |
| 10:25:39 | 2 / 1 / 2 |
| 10:25:45 | 2 / 2 / 2 |
| 10:25:57 | 4 / 2 / 4 |
| 10:26:04 | 4 / 4 / 4 (max) |
| 10:28:39 | 3 / 3 / 3 |
| 10:28:56 | 2 / 2 / 2 |
| 10:29:12 | 1 / 1 / 1 |

Then `day8-stable-check` 7/7.

**VPA (17/17).** No updater is installed.

- `Off` mode recommended a target of **40m / 49,566,436 B** (≈ 47.3 MiB).
  The uncapped CPU target was 49m, capped to `maxAllowed` 40m. The
  existing Pod kept its declared 10m / 32Mi (limits 50m / 64Mi).
- Mode set to `Initial`, then the Deployment was scaled 1 → 2. Exactly one
  new Pod (UID `4471e16f…`) was mutated by the admission controller
  (`vpaUpdates: … memory request, cpu request, cpu limit, memory limit`).
  Its requests were exactly the admission-time target, 40m /
  49,566,436 B, and its limits were scaled proportionally to 200m /
  99,132,872 B, within the LimitRange maximum.
- The existing Pod `27792058…` kept the same UID, resources and 0
  restarts, with no VPA annotation.

The recommendation (Off) and the application (Initial, new Pod only) are
reported separately. Then `day8-stable-check` 7/7.

**KEDA (13/13).** The ScaledObject was Ready with the worker at 0. KEDA
created HPA `keda-hpa-day8-queue-worker` (min 1, max 3, External metric).
The producer pushed 60 items.

| Time (UTC) | worker spec / Ready | Active |
|---|---|---|
| 10:30:43 | 0 / 0 | no |
| 10:30:56 | 1 / 0 (activation) | yes |
| 10:31:02 | 3 / 1 | yes |
| 10:31:08 | 3 / 3 | yes |
| 10:31:44 | 2 / 2 | no |
| 10:32:00 | 1 / 1 | no |
| 10:32:11 | 0 / 0 | no |

The queue drained to length 0 with **60/60 processed**, and the workers
ran the pinned image. Then `day8-stable-check` 7/7.

**Cleanup (3/3).** The demonstration group exited 0 and cleanup exited 0.
The namespace is gone, and no HPA, VPA, ScaledObject, ResourceQuota or
LimitRange remains in any namespace.

**Final gate.** CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67,
rollout 35/35, running images 47/47 (build `fdb68741…`), Gateway 8/8,
**mesh-check 45/45**, **networkpolicy-check 37/37**, add-ons 17/17, and
`day8-stable-check` 7/7.

## 5. Intended cluster changes (all verified)

- **Added Helm releases:** `metrics-server` (kube-system), `vertical-pod-autoscaler`
  (vpa-system) and `keda` (keda), each now at revision 4. Earlier revisions
  come from the attempts above; KEDA's rev 1 (failed) and rev 2 (superseded
  after the wait timeout) remain in Helm history.
- **New namespaces:** `vpa-system` and `keda`.
- **New APIServices:** `v1beta1.metrics.k8s.io` and `v1beta1.external.metrics.k8s.io`.
- **New webhooks:** MutatingWebhookConfiguration `vertical-pod-autoscaler-webhook-config`
  (scoped to `maops-day8-scaling`, so it currently matches nothing) and
  ValidatingWebhookConfiguration `keda-admission`.
- **New CRDs:** the VPA and KEDA CRDs.
- **Images on the nodes:** `maops-kubernetes-scaling:day8-cfg-<digest>` for
  attempts 2-6 (two distinct digests); `redis:8.10.2-alpine` and the add-on
  images were pulled.
- **Application Pods recreated during the approved recovery:** `maops-state-0`
  and one app Pod (section 1).
- **Unchanged:** the Day 7 Helm release (rev 13, values and manifest), all
  7 application Pods since the recovery (same UIDs, 0 new restarts),
  HTTPRoute and Gateway, PVC/PV, and `state.json` (15 B, sha256
  `3ce4f556…`, mtime 2026-09-30 04:22:15Z).

## 6. Limitations (not claimed)

- Local Kind reference platform on one shared host, not production. HPA
  and KEDA scaling are proven on a tiny disposable workload, not the
  application. The application has no autoscaler.
- `--kubelet-insecure-tls` and the chart's `insecureSkipTLSVerify`
  APIService are local-kind compromises.
- *(Superseded by section 7.)* The cluster-wide KEDA described here was
  reverted after independent review; KEDA is now namespace-scoped.
- The worker's drain covers graceful scale-down (SIGTERM) only. A hard kill
  (node loss, OOM) between `BLPOP` and the count can still lose an item:
  this is not an at-least-once queue.
- VPA `Initial` applies only at Pod creation; nothing applies
  recommendations to running Pods, by design. The recommendation here came
  from minutes of history, not days.
- The Redis queue has no authentication; it is protected by NetworkPolicy
  only, with protected mode off, and is disposable.
- Post-restart ambient-enrollment recovery is still manual. This run needed
  an owner-approved exception that recreated `maops-state-0`.
- `VERSION` stays 0.7.0. Independent reviews, the version bump and the
  release are still open, so **v1.0.0 is not released**. *(True at that
  time. Superseded by section 10: `VERSION` is now prepared at `1.0.0` on
  the branch; v1.0.0 is still not tagged or released.)*

## 7. Independent-review remediation (2026-10-03, same day)

The review found two problems.

1. A cluster-wide KEDA (`watchNamespace: ""`) is not isolated. Checking
   that no ScaledObjects exist elsewhere does not restrict permissions.
2. `phase_cleanup()` read a failed `kubectl get … --all-namespaces` as
   "nothing left".

**Changes.**
- KEDA is now `watchNamespace: maops-day8-scaling`. `day8-guards` runs
  before `day8-addons-install`, and the install refuses without the Day 8
  namespace.
- `day8-addons-check` evaluates KEDA's live bindings and runs
  SubjectAccessReviews. The new `day8-addons-final-check` covers the
  dormant state after cleanup. *(Superseded by section 8: there is no
  dormant KEDA any more; the final check expects KEDA absent.)*
- Cleanup fails closed on every unreadable list and releases KEDA objects
  before deleting the namespace.
- Unit tests (at that time): 1749 OK, including the new failed-list and cleanup-order
  regressions.

**Before (cluster-wide, run dir `99ce9d3fb22d482ebff694038e641ef2`).** The new
check fails as intended. ClusterRoleBinding `keda-operator` → ClusterRole
`keda-operator`, and `keda/keda-operator` **could** get, list and watch
Secrets, patch or update `deployments/scale` and `statefulsets/scale`, and
create HPAs in `maops-platform`.

**Attempts.**

| Run | Exit | Result |
|---|---|---|
| A `c81534e5a8b445e68defe32ea5ef2608` | 2 | The scoped install, active RBAC check (24/24 in later runs), HPA, VPA, KEDA and stable checks all passed. **Cleanup failed 3 checks.** Deleting the namespace removed KEDA's RoleBinding before KEDA released `finalizer.keda.sh`. The operator became forbidden and the namespace hung `Terminating`. The new fail-closed cleanup caught it. Recovery: a UID-tested JSON patch removed the finalizer from that one disposable ScaledObject; the namespace then finished deleting and no permission was widened. Fix: cleanup now releases KEDA objects first, and refuses to delete the namespace if they cannot be released. |
| B `ad210e1fc8f546b1a702ae5264d787f0` | 2 | Host DNS (`172.23.160.1`) failed to resolve `kubernetes-sigs.github.io` before any Helm change. The ordered cleanup then passed 5/5. Environmental; no change. |
| **B2 `7c895b460a3747a190d4fb0428838fb4`** | **0** | **Authoritative.** Started from run A's post-cleanup state (namespace and scoped binding gone, KEDA dormant). The guards and install re-created both. |
| **C `77e4990964bc475cac2fd6d231d74bf6`** | **0** | **Second consecutive run** after B2's clean cleanup. The namespace and binding were re-created again and KEDA worked again. |

**Run B2 results (12:08-12:27Z):**
- add-ons active 24/24
- guards 5/5, quota proof 9/9
- HPA 9/9: 1 → 2 → 4 Ready under load (4,175 OK, 0 errors), then 4 → 3 → 2 → 1
- VPA 17/17: Off target 35m / 49,566,436 B, applied only to one new Pod (limits 175m / 99,132,872 B)
- KEDA 13/13: 0 → 1 → 3 → 2 → 1 → 0, 60/60 processed
- stable checks 7/7 at five points
- cleanup 5/5: the ScaledObject was released, then the namespace deleted
- final gate: CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67, rollout 35/35, running images 47/47, Gateway 8/8, mesh-check 45/45, networkpolicy-check 37/37, add-ons after-cleanup 24/24, stable 7/7

Run C had the same phase totals (KEDA 0 → 1 → 3 → 2 → 1 → 0, 60/60).

**KEDA effective RBAC (live, after run C).**
- ClusterRoleBindings for KEDA ServiceAccounts: only
  `keda-operator-minimal` → `keda-operator-minimal-cluster-role`
  (ClusterTriggerAuthentications, webhook configurations, APIServices,
  CloudEventSources), `keda-operator-system-auth-delegator` →
  `system:auth-delegator` (metrics adapter), and `keda-operator-webhook`
  → `keda-operator-webhook` (list and watch HPAs and ScaledObjects;
  get/list/watch Deployments and StatefulSets; list LimitRanges).
- RoleBindings: `keda/keda-operator` → ClusterRole `keda-operator`,
  `keda/keda-operator-certs`, `kube-system/keda-operator-auth-reader`,
  and, while it exists, `maops-day8-scaling/keda-operator` → ClusterRole
  `keda-operator`.
- SubjectAccessReviews in `maops-platform`: every probe returned `no` for
  all three ServiceAccounts (get/list/watch secrets, patch/update
  `deployments/scale`, patch `statefulsets/scale`, patch Deployments,
  create HPAs).
- In `maops-day8-scaling`, `keda-operator` could patch `deployments/scale`,
  create HPAs and list ScaledObjects while the namespace existed, and
  could do none of these after cleanup.

**Final state after run C.**
- The Day 7 release is at revision 13. All 7 Pods keep the same UIDs as
  after the morning recovery, with no new restarts. PVC `2be6628b…` and
  PV `a9c74497…` are unchanged, and so is `state.json` (15 B, sha256
  `3ce4f556…`). `/` and `/state` return 200, and a wrong Host returns 404.
- The scaling namespace is absent, and no HPA, VPA, ScaledObject,
  ScaledJob, LimitRange or ResourceQuota exists anywhere.
- The add-on releases are at revision 7. No port-forwards are left
  running.

**Remaining risks.** *(Superseded by section 8. The Helm drift and the
dormant-operator caveat no longer apply, because KEDA is now uninstalled
by cleanup. The chart-level permission notes below still hold while KEDA
is installed during a run.)*

- `keda-webhook` keeps cluster-wide read on Deployments and StatefulSets
  (metadata and specs, not Secrets).
- `keda-operator-minimal` can patch APIServices and
  ValidatingWebhookConfigurations cluster-wide. Both are chart design and
  apply in every KEDA mode.
- After cleanup the KEDA release keeps a RoleBinding in its manifest that
  no longer exists (accepted Helm drift).
- If the dormant operator restarts before the next Day 8 run, it cannot
  sync its namespace-scoped cache until `day8-guards` and
  `day8-addons-install` re-create the namespace and binding.
- Each run upgrades the three add-on releases (another Helm revision) and
  depends on reaching the chart repositories and registries (run B).

## 8. Release-blocker remediation: KEDA uninstalled by cleanup (2026-10-03)

**Finding (external review).** Cleanup deleted `maops-day8-scaling` but
left KEDA installed. Its scoped RoleBinding disappeared, the Helm release
drifted, and the operator logged "forbidden" errors.

**Before.** Run directory `307ab4e0293442e2861456944c54ff62`.
`day8_addons.py check after-cleanup` exited 1 with 4 failures: the
release was present; the 32 KEDA objects, CRDs included, were present;
3 Pods were running in `keda`; and 6 bindings still named KEDA
ServiceAccounts. The operator had logged **856** "forbidden" lines in
the preceding 60 minutes.

**Change.** `phase_cleanup` now does three steps in order:
1. `release_keda_objects` deletes the ScaledObjects, ScaledJobs and
   TriggerAuthentications and waits for KEDA to release its finalizers
   and remove its managed HPA, while the namespace and binding exist.
2. `uninstall_keda` runs `helm uninstall keda -n keda --cascade
   foreground --wait --timeout 300s`, only for that release. It then
   deletes the operator's runtime `secret/kedaorg-certs` (label-checked)
   and `lease/operator.keda.sh`, and waits for no Pod in `keda`.
3. The labelled namespace is deleted.

Failures in steps 1 or 2 keep the namespace. Cleanup is safe when KEDA
was never installed, or was already removed, because an explicitly
NotFound release or CRD means there is nothing to do. An unreadable
answer always fails. `day8-addons-final-check` now expects KEDA
**absent**: the Helm release, all 30 chart-owned objects plus 2 runtime
objects, Pods and bindings. Metrics Server and VPA stay installed and
checked.

**CRDs.** Chart keda-2.21.0 renders its 6 CRDs as Helm templates without
`helm.sh/resource-policy: keep`, so uninstall removes them. This was
verified live: 0 `*.keda.sh` CRDs remain. **No KEDA CRD is retained.**
Between runs, a KEDA type that is not served counts as "no objects" only
after its CRD is explicitly NotFound. Built-in types never get this
exception (`day8_common.resource_type_absent`).

**Tests.** 1765 OK. New tests:
- `CleanupLifecycleTests`: order, uninstall failure, unreleased objects,
  missing binding, run failed before KEDA was installed, partial install,
  repeated cleanup, leftover KEDA without a namespace, unreadable release
  state, foreign-labelled Secret, fail-closed lists, CRD-proven absence,
  unreadable CRD;
- `ExistenceAnswerTests`;
- `KedaFinalStateTests`;
- `StableControlsTests`.

**Live runs.** Both were invoked back to back as
`PATH=/usr/bin:$PATH make day8-check`:

| Run | DAY8_RUN_ID | UTC | Exit | KEDA install |
|---|---|---|---|---|
| D | `a9535fea298944f48a248d13b4bb0f25` | 13:36:16 → 13:53:25 | **0** | upgrade of the old leftover release (rev 8), then uninstalled |
| E | `b6bfc6e69c924feab877247c9d1a9c0d` | 13:53:25 → 14:11:26 | **0** | **fresh install, Helm revision 1** (history purged by D's uninstall), then uninstalled |

Each run passed:
- add-ons active 24/24, with all KEDA access probes in `maops-platform`
  denied and the operator's positive control in the scaling namespace;
- guards 5/5, quota proof 9/9, HPA 9/9, VPA 17/17;
- **KEDA 13/13**: 60/60 processed, workers 0 → 1 → 3 → 2 → 1 → 0;
- stable checks 7/7 after each phase;
- **cleanup 12/12**: KEDA released the ScaledObject and its HPA, `helm
  uninstall` exited 0, the runtime Secret and lease were deleted, no
  Pods remained, the namespace was deleted, all 32 KEDA objects were
  NotFound, and no binding names KEDA;
- final gate: CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67,
  rollout 35/35, running images 47/47, Gateway 8/8, **mesh-check
  45/45**, **networkpolicy-check 37/37**, **add-ons after-cleanup
  17/17** (KEDA absent), stable 7/7.

**Final state after run E (read-only, 14:11Z).**
- `helm status keda -n keda` returns `release: not found`. There are 0
  KEDA CRDs and 0 KEDA ClusterRoles, ClusterRoleBindings, webhooks or
  APIServices, and `v1beta1.external.metrics.k8s.io` is NotFound.
- Namespace `keda` holds only `serviceaccount/default` and the
  `istio-ca-*` and `kube-root-ca.crt` ConfigMaps: no Pod, Secret or lease.
- No scaling namespace exists, and there is no HPA, VPA, LimitRange or
  ResourceQuota anywhere.
- Metrics Server and VPA are at revision 9, deployed.
- The Day 7 release is at revision 13. All 7 Pod UIDs are unchanged, with
  no new restarts; PVC `2be6628b…` and PV `a9c74497…` are unchanged, and
  `state.json` is 15 B with sha256 `3ce4f556…`. `/` and `/state` return
  200, and a wrong Host returns 404.
- No port-forwards are left running.

**Remaining limitations.**
- The empty `keda` namespace remains between runs, created by
  `--create-namespace` and reused by the next install. Cleanup only
  deletes namespaces it owns by label.
- Each run installs KEDA afresh. It therefore depends on reaching the
  chart repository and `ghcr.io` (pulls of up to 7m23s were seen; the
  bounded wait is 900 s). Metrics Server and VPA each gain a Helm revision
  per run.
- KEDA's chart-level cluster permissions (`keda-operator-minimal`, the
  webhook's cluster-wide read of Deployments and StatefulSets) exist
  **only while a run is in progress**.
- A host or Docker restart in the middle of a run can still leave the
  scaling namespace and KEDA installed. `make day8-cleanup` removes both
  idempotently, but it is not run automatically on boot.

## 9. Review corrections: KEDA CRD guard and Secret exposure (2026-10-03)

**Findings (external review of the final patch).**
1. `helm uninstall keda` deletes the six KEDA CRDs, and with them any
   custom object stored under them, including objects that are not Day
   8's.
2. `uninstall_keda()` read `secret/kedaorg-certs` with `kubectl get -o
   json`, which brought the whole Secret, private key included, into
   Python just to check one label.

**Changes (`scripts/day8_scaling.py`).**
- **CRD guard.** After Day 8's own objects and finalizers are released,
  and immediately before the uninstall, `crd_instance_problems()`
  inspects all six KEDA CRDs across their full scope. For each CRD it
  reads only `.spec.scope` via jsonpath; Namespaced types are listed with
  `--all-namespaces` (namespace/name columns) and Cluster types with
  `-o name`. Any instance, an unreadable CRD or instance list, or an
  unknown scope fails cleanup and keeps **KEDA and `maops-day8-scaling`**.
  No foreign object is deleted or adopted.
- **Secret handling.** `runtime_object_labels()` checks existence with
  `-o name` and the labels with `-o jsonpath={.metadata.labels}`. Python
  receives only the name and labels of `kedaorg-certs`, never `.data`, so
  no key material is printed, captured by Python or written to evidence.
  The kubectl process itself still receives the object from the API
  server, as any `kubectl get` does. The owner-label refusal
  (`app=keda-operator`) is unchanged.

**Tests (at that time).** Unit suite 1776 OK, 118 of them Day 8. New tests:
- `KedaCrdGuardTests`: clean path; a foreign namespaced ScaledObject;
  a cluster-scoped ClusterTriggerAuthentication; an unreadable instance
  list; an unreadable CRD; a stray object in Day 8's own namespace; and
  the pure guard logic. Each blocking case asserts no uninstall, the
  namespace kept, and the foreign object untouched.
- `SecretExposureTests`: labels are read through jsonpath only, and the
  fake cluster *raises* on any full-object read of a runtime object; the
  request carries no `.data` expression; the foreign-label refusal is
  kept; and a static scan of every `day8_*.py` rejects any JSON/YAML read
  of a Secret.

**Cluster-free gates.** `PATH=/usr/bin:$PATH make tool-check test
version-check manifest-check helm-lint helm-check day8-static-check
day8-plan` all exited 0.

**Live run.** `PATH=/usr/bin:$PATH make day8-check`, run ID
**`140a5016313048f49386813c7a8daace`**, 15:45:44Z → 16:03:48Z, **exit 0**.

- **Add-ons:** active check 24/24. `WATCH_NAMESPACE=maops-day8-scaling`;
  no ClusterRoleBinding to `keda-operator`; all 24 KEDA access probes in
  `maops-platform` returned `no`; the positive control in the scaling
  namespace returned `yes`. KEDA was a fresh install (Helm revision 1).
- **Demonstrations:**
  - guards 5/5, quota proof 9/9;
  - HPA 9/9: 1 → 3 → 4 Ready under load (4331 OK, 0 errors), then
    4 → 3 → 2 → 1;
  - VPA 17/17: Off target 40m / 49,566,436 B, applied only to one new
    Pod (limits 200m / 99,132,872 B);
  - **KEDA 13/13: 60/60 processed**, workers 0 → 1 → 3 → 2 → 1 → 0;
  - stable checks 7/7 after each phase.
- **Cleanup 13/13.** KEDA released the ScaledObject and its HPA. All six
  CRDs (4 Namespaced, 2 Cluster) held zero instances immediately before
  the uninstall. `helm uninstall` exited 0; the runtime Secret (labels
  only) and lease were deleted; no Pods remained; the namespace was
  deleted. All 32 KEDA objects are explicitly NotFound and no binding
  names KEDA.
- **Final gate.** CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67,
  rollout 35/35, running images 47/47, Gateway 8/8, mesh-check 45/45,
  networkpolicy-check 37/37, add-ons after-cleanup 17/17 (KEDA absent),
  stable 7/7.
- **Final state (16:04Z).**
  - `helm status keda` returns `release: not found`. There are 0 KEDA
    CRDs, 0 KEDA ClusterRoles, ClusterRoleBindings, webhooks or
    APIServices, and only `serviceaccount/default` is left in `keda`.
  - There is no scaling namespace and no HPA, VPA, LimitRange or
    ResourceQuota anywhere.
  - Metrics Server and VPA are at revision 10.
  - The Day 7 release is at revision 13 with the same 7 Pod UIDs and no
    new restarts. PVC `2be6628b…`, PV `a9c74497…` and `state.json`
    (sha256 `3ce4f556…`) are unchanged. `/` and `/state` return 200, and
    a wrong Host returns 404.
  - No port-forwards are left running.
- **Evidence scan.** A scan of the run directory for private-key markers,
  `tls.key`/`ca.key` and Secret `data` maps found none.

**Observation (now reproducible).** On a *fresh* KEDA install the operator
container restarts **once** at start-up. This happened in runs E and F
(1 restart each), and not in run D, which upgraded an existing release
whose certificate Secret already existed. Events show the same sequence
both times:

1. The operator starts.
2. Its peers report `secret "kedaorg-certs" not found`.
3. The operator restarts within about 3 seconds.
4. It becomes leader again about 20 seconds later, after which every
   check passes.

This is consistent with the operator generating its own certificate on
first start; the cause is inferred, not proven from operator logs.
Proposed for acceptance as benign start-up behaviour.

## 10. Review adjudication, 1.0.0 preparation and run G (2026-10-03)

The five independent reviews (`day-08-independent-reviews.md`) were
adjudicated in `day-08-final-adjudication.md`. The changes are listed in
`day-08-remediation-log.md`. This section records the live steps and the
final authoritative run.

**Legacy `keda` namespace (removed once, with evidence).** The new
preflight refuses a `keda` namespace without Day 8's identity labels. The
one left by run `1c59a36c…` (`--create-namespace`, UID `2251ebab…`) held
only `kube-root-ca.crt`, two `istio-ca-*` ConfigMaps and
`serviceaccount/default`. It had no Helm release and there were no KEDA
CRDs. It was deleted by name (kubectl has no UID precondition for delete).
Evidence: `day8-runs/8749aae3…/legacy-keda-ns-0{1,2}-*.txt`.

**Controlled Day 7 rollout to 1.0.0** (evidence `day8-runs/8749aae3…`,
`rollout-*`):

- **Before** (`rollout-00-pre.json`):
  - Helm revision 13;
  - StatefulSet `4ded2c43…`;
  - 7 Pod UIDs;
  - PVC `2be6628b…`, PV `a9c74497…`;
  - `state.json` 15 B, sha256 `3ce4f556…`.
- **Steps**, each exit 0:
  - `image-build`;
  - `day7-image-verify-local` 9/9;
  - `day7-build-record` (schema 2, build
    `70400e92ce4051f6b6e24bb89b41b93405cbdd2cbb3ace566f302ecfd28918dd`);
  - `image-load`;
  - `day7-image-load`;
  - `day7-image-verify-nodes` 31/31;
  - `day7-deploy` (revision 14, chart 1.0.0).
- **After:**
  - `day7-running-images` 47/47;
  - `ambient-workload-check` 67/67;
  - `rollout-check` 35/35;
  - `gateway-check` 8/8.
- **Operator error, recorded.** The ambient, rollout and Gateway checks
  were first invoked without the Day 7 profile. They defaulted to the
  stopped `kind-maops-k8s-day6` context, were refused at connect (exit 2)
  and changed nothing. They were then re-run correctly. Both sets of logs
  are kept.
- **Comparison** (`rollout-10-post.json`):
  - **Unchanged:** the StatefulSet, Deployment, Gateway and HTTPRoute
    UIDs, the claim-template labels, the PVC/PV UIDs, name and node, and
    `state.json` (15 B, `3ce4f556…`).
  - **Pods: all 7 were replaced.** 0 of 7 UIDs were retained, including
    `maops-state-0` (`95ebfd5a…` → `2cd7be95…`). The new pinned image tags
    require this.
  - **Changed as intended:** the Pod-template digests and the Helm
    revision.

**Run G: `28ec47a1b5b44db29b7deb2d96df8c64`.** `PATH=/usr/bin:$PATH make
day8-check`, 17:33:54Z → 17:52:02Z, **exit 0**, on the final candidate
code. Its baseline was taken from the post-rollout state (Helm revision
14, build `70400e92…`).

- **Static and preflight.** 1818 unit tests OK (160 of them Day 8);
  version 65/65; manifests 267/267; Helm 2346/2346;
  `day8-static-check` 5/5; preflight 7/7, which now includes the
  KEDA-state check.
- **Pre-install ownership:** 2/2. There was no foreign release, no CRDs
  and no namespace, so Day 8 created its own labelled `keda` namespace.
- **Add-ons, active: 35/35.**
  - 27 probes for each of the 3 KEDA identities, with 0 forbidden
    actions allowed.
  - The same 27 probes for each of the 3 persistent add-on identities
    (Metrics Server, VPA recommender, VPA admission controller), also 0
    allowed.
  - All expected grants were measured as `yes`.
  - Release ownership and webhook `failurePolicy: Fail` were verified
    live.
  - The KEDA operator restarted once at start-up, the known fresh-install
    behaviour.
- **Demonstrations:**
  - guards 5/5 and quota proof 9/9;
  - **HPA 9/9:** 1 → 3 → 4 Ready under load, then 4 → 3 → 2 → 1;
  - **VPA 17/17:** Off target 40m / 49,566,436 B (CPU capped from 49m),
    applied at admission only to the new Pod;
  - **KEDA 13/13:** 60/60 processed, workers 0 → 1 → 3 → 2 → 1 → 0;
  - stable checks 7/7 after the add-ons and after each phase.
- **Cleanup: 15/15.** The Day 8 ownership of the release was checked,
  all six CRDs were empty, and KEDA was uninstalled. The runtime Secret
  (labels only) and the lease were deleted. Then the Day 8-owned `keda`
  namespace and the scaling namespace were deleted. All 33 inventory
  objects are explicitly NotFound, no Pods remain, and no binding names a
  KEDA ServiceAccount.
- **Final gate:**
  - CNI 4/4, context 6/6, mesh status 4/4;
  - listeners 67/67, rollout 35/35, running images 47/47, Gateway 8/8;
  - mesh-check 45/45, networkpolicy-check 37/37;
  - add-ons after cleanup 23/23;
  - stable 7/7 against run G's baseline.
- **Log.** The 13 `FAIL` lines in the log all come from negative unit
  tests, before the `Ran 1818 tests … OK` line. None appears after it.
  The full log is `make-day8-check.log` in the run directory (sha256
  prefix `37d6cea1fe8525a0`).
- **Final state:**
  - **Helm releases:** Day 7 at revision 14 (chart 1.0.0); Metrics Server
    and VPA at revision 11; no `keda` release.
  - **Namespaces:** `keda` and `maops-day8-scaling` are NotFound.
  - **Port-forwards:** none left running.
  - **Evidence scan:** no private-key markers and no Secret `data` maps in
    either new run directory; directories 0700, files 0600.

**Cluster-free gates on the final tree, after the documentation edits.**
`PATH=/usr/bin:$PATH make tool-check test version-check manifest-check
helm-lint helm-check day8-static-check day8-plan` all exited 0. Unit
tests: 1818 OK.

**Earlier runs.** Run F (`140a5016…`) and its predecessors are kept as
history. Run G supersedes them as the authoritative run.

## 11. Round-2 remediation and run H (2026-10-04)

The focused round-2 re-review found one MEDIUM (T8: the frozen
claim-template labels were not validated) and two targeted ownership LOWs.
Their fixes changed install and cleanup code: cleanup now checks the
`keda` namespace's labels first, and pre-install uses `kubectl create`,
never `apply`. Run G therefore no longer covers the final tree. Details
are in `day-08-remediation-log.md`, section 11.

**Run H: `0d158cfe2fc8453595d0185e004fcfaf`.**
- **Command:** `PATH=/usr/bin:$PATH make day8-check`.
- **Time:** 2026-10-04 05:28:54Z → 05:48:34Z, **exit 0**, on the final
  tree.
- **Starting state:** the post-rollout Day 7 release (Helm revision 14,
  build `70400e92…`). No KEDA release, CRD or `keda` namespace, and no
  scaling namespace.

- **Static:**
  - unit tests: 1827 OK (164 of them Day 8, 59 in the Helm validator);
  - version 65/65, manifests 267/267;
  - Helm 2364/2364, which includes the new `claim_template.state.*`
    checks on every render;
  - `day8-static-check` 5/5, preflight 7/7.
- **Pre-install ownership: 3/3.** Day 8 created `keda` with `kubectl
  create`, then re-read it and confirmed it is the same object (UID
  `e83843e9…`).
- **Add-ons, active: 35/35.**
  - 3 KEDA identities and 3 persistent add-on identities, 27 probes each,
    all denied.
  - The expected grants answer `yes`.
  - Release ownership and webhook `Fail` were verified live.
  - The KEDA operator restarted once at start-up (the known fresh-install
    behaviour).
- **Demonstrations:**
  - guards 5/5, quota 9/9;
  - **HPA 9/9:** 1 → 3 → 4 Ready under load, then 4 → 3 → 2 → 1;
  - **VPA 17/17:** Off target 35m / 63,544,758 B (not capped), applied at
    admission only to the new Pod;
  - **KEDA 13/13:** 60/60 processed, workers 0 → 1 → 3 → 2 → 1 → 0;
  - stable checks 7/7 after the add-ons and after each phase.
- **Cleanup: 16/16.** This includes the new check that `keda` carries
  Day 8's labels, run before any KEDA change. After it:
  - the release ownership check, the empty-CRD guard and the uninstall;
  - the runtime Secret (labels only) and the lease;
  - the Day 8-owned `keda` namespace and the scaling namespace.

  All 33 KEDA objects are explicitly NotFound, and no Pods or bindings
  are left.
- **Final gate:**
  - CNI 4/4, context 6/6, mesh status 4/4;
  - listeners 67/67, rollout 35/35, running images 47/47, Gateway 8/8;
  - mesh-check 45/45, networkpolicy-check 37/37;
  - add-ons after cleanup 23/23;
  - stable 7/7 against run H's baseline.
- **Log:** `make-day8-check.log` in the run directory (sha256 prefix
  `f1fd452c7800aca6`).
  - Every `FAIL` before the `Ran 1827 tests … OK` line comes from a
    negative unit test.
  - Five `FAIL` lines just after it are the buffered output of the mocked
    `tests/test_workload_refresh.py`. The same blocks appear in a
    cluster-free `make test`. No Day 6 container was running, so nothing
    could reach that cluster.
  - Every `FAIL` count from the cluster steps is 0.
- **Final state:**
  - **Releases:** Day 7 at Helm revision 14 (chart 1.0.0); Metrics
    Server and VPA at revision 12; no `keda` release.
  - **Namespaces:** `keda` and `maops-day8-scaling` are NotFound.
  - **Port-forwards:** none running.
  - **Evidence:** no key material or Secret `data`; directory 0700,
    files 0600.

**Authoritative run.** Run H supersedes run G as the authoritative run
for the final tree. Run G (`28ec47a1…`) remains valid evidence for the
pre-round-2 code and for the post-rollout baseline. The scratch copy of
run G's log was lost when the session restarted; its archived copy in
`day8-runs/28ec47a1…/make-day8-check.log` is intact.

## 12. Merged-main run failed at VPA; correction and next run (2026-10-04)

Day 8 was merged to `main` at `78b02a1b91fdd5dacee7dd1b5b819865e4cee70e`
(PR #11). The owner then ran `make day8-check` on merged `main`. It
failed at VPA. This section keeps that failed attempt and records the
correction (`day-08-remediation-log.md`, section 12) and the next run.
`v1.0.0` is not tagged or released.

### 12.0 Chronology (2026-10-04, UTC)

| Time | Event | Result |
|---|---|---|
| 05:28:54 → 05:48:34 | Run H `0d158cfe…` on the pre-merge tree | exit 0 (VPA 17/17); historical |
| 10:08:42 / 10:24:22 | Commit `375b71f`; PR #11 merged as `78b02a1` | - |
| 10:44 → 11:05 | Owner-approved recovery of three Day 7 Pods that lacked ztunnel listeners (`merged-main-mesh-72U9xD5v`) | ambient 64/67 → 67/67; Day 7 resume checks pass |
| 11:08:59 | First merged-main `day8-check` invocation | never started (`env` failed on a `PATH` entry containing spaces) |
| after 11:08:59 (run dir 11:11:51) → 11:21:09 | Run f837 `f837802b…` on `78b02a1` | **FAILED: VPA 16/17**; cleanup 16/16 |
| → 11:40:24 | Separate `DAY8_RUN_ID=f837802b… make day8-final-gate` | exit 0 (stable 7/7) |
| after 11:40 | VPA `minAllowed` memory 48Mi and invariant, unstaged on `fix/day-8-vpa-demonstration` | cluster-free gates pass |
| 11:59:48 → 12:18:36 | Run I `f4e69ac6…` on the corrected tree | **exit 0 (VPA 17/17)**; authoritative |

### 12.1 Run `f837802b23ba4bd39cbf36cf28cd2cb3`: FAILED (VPA 16/17)

- **Command:** `PATH=/usr/bin:$PATH make day8-check` on merged `main`
  (`78b02a1`), run by the owner.
- **Time:** 2026-10-04. The run directory was created at 11:11:51Z and
  the log was last written at 11:21:09Z. The log has no start timestamp;
  the unit tests ran before the directory existed. Exit non-zero:
  `day8-check: demonstrations exit 2, cleanup exit 0`.
- **Earlier attempt:** `merged-main-validation/make-day8-check.log` (113
  bytes, 11:08Z). It is the first invocation, which never started: `env`
  failed on a host `PATH` entry containing spaces
  (`env: 'Files/WSL/:/mnt/c/Users/Raiyan': No such file or directory`).
  The retry is the run recorded here.
- **Logs (kept, not copied into the repo):**
  - `day8-runs/merged-main-validation/make-day8-check-retry.log`;
  - `day8-runs/merged-main-validation/failed-run-final-gate.log`;
  - run evidence in `day8-runs/f837802b23ba4bd39cbf36cf28cd2cb3/`.
- **Before VPA, everything passed:**
  - unit tests 1827 OK, Helm 2364/2364, `day8-static-check` 5/5,
    preflight 7/7;
  - stable baseline 2/2, scaling image 4/4, guards 5/5;
  - KEDA pre-install ownership 3/3, add-ons active 35/35;
  - stable 7/7, quota 9/9;
  - HPA 9/9 (1 → 4 Ready under load → 1);
  - stable 7/7.
- **VPA: 16/17 (`vpa-2026-10-04T112025Z.json`).**

  | | CPU request | Memory request | CPU limit | Memory limit |
  |---|---|---|---|---|
  | Declared (Deployment template) | 10m | 32Mi | 50m | 64Mi |
  | Off-mode recommendation `target` (also `lowerBound`, `upperBound`, `uncappedTarget`) | 10m | 32Mi | - | - |
  | Target at admission (both reads) | 10m | 32Mi | - | - |
  | New Pod `0114fbfa…` (admitted under `Initial`) | 10m | 32Mi | 50m | 64Mi |
  | Existing Pod `dd2e0fd2…` (before and after) | 10m | 32Mi | 50m | 64Mi |

  The new Pod carried the admission annotation (`vpaUpdates: Pod
  resources updated by day8-vpa-target: container 0: cpu request, memory
  request, cpu limit, memory limit`). Its resources were identical to the
  template. The single failed check is the one that requires a real
  change: "the applied recommendation differs from the declared
  requests". The annotation alone was correctly not accepted as proof.
  The existing Pod was untouched (same UID, resources and restarts, no
  annotation). The updater is not installed, and no Pod was blocked by
  the quota.
- **KEDA:** not run. `day8-check` stops the demonstrations at the first
  failure, then always runs cleanup.
- **Cleanup: 16/16.**
  - `keda`'s Day 8 labels were verified before any KEDA change.
  - Release ownership was confirmed, and all 6 KEDA CRDs held zero
    instances; then `helm uninstall`.
  - The runtime Secret (labels only) and the lease were deleted, then
    Day 8's `keda` namespace and the scaling namespace.
  - All 33 KEDA objects are NotFound, and no Pods or bindings remain.
- **Separate final gate:** `DAY8_RUN_ID=f837802b23ba4bd39cbf36cf28cd2cb3
  make day8-final-gate`, run by the owner, exit 0.
  - CNI 4/4, context 6/6, mesh status 4/4, listeners 67/67;
  - rollout 35/35, running images 47/47 (build `70400e92…`), Gateway
    8/8;
  - mesh-check 45/45, networkpolicy-check 37/37;
  - add-ons after cleanup 23/23;
  - stable 7/7 against run f837's own baseline.
- **Day 7 identity at the end of run f837** (`stable-check-2026-10-04T114024Z.json`):
  - Helm revision 14, deployed, manifest sha256 `abfd77b3…`;
  - PVC `2be6628b…` Bound, PV `a9c74497…` on `maops-k8s-day7-worker`;
  - `state.json`: 15 B, sha256 `3ce4f556…`. This is the same storage and
    state identity as runs G and H.
- **Pods replaced between runs H and f837: a deliberate, owner-approved
  recovery.** Three Day 7 Pods differ between run H's last stable check
  (05:48Z) and run f837's baseline.

  | Order | Deleted (on `maops-k8s-day7-worker2`) | Replacement | Created |
  |---|---|---|---|
  | 1 | `maops-app-…-8rrmm` (`4baab98b…`) | `maops-app-…-ns9xw` (`fd78d697…`) | 10:51:24Z |
  | 2 | `maops-gateway-…-568ws` (`53004ff5…`) | `maops-gateway-…-g4mdl` (`96c87ba5…`) | 10:57:58Z |
  | 3 | `maops-gateway-…-8xhvd` (`dbbbcfec…`) | `maops-gateway-…-b7tjx` (`8a9b03dc…`) | 11:02:45Z |

  - **Why.** After a host restart (every Day 7 Pod showed 2 restarts),
    these three were the only Day 7 Pods that were unready (0/1). They
    also lacked their ztunnel in-Pod listeners on 15001/15006/15008: the
    ambient check was 64/67, with exactly these three Pods failing.
  - **How.** The owner approved replacing them. Each was deleted
    deliberately, one at a time, non-forced and UID-checked: the app
    first, then the two gateways. The Deployments' ReplicaSets recreated
    them.
  - **Corroborated by the evidence.** The ambient check after each step
    shows the order and the recovery: 64/67, then 65/67 after the app,
    66/67 after the first gateway, 67/67 after the second. Each
    replacement had its listeners on 15001/15006/15008.
  - **After the recovery.** The Day 7 resume checks passed: context 6/6,
    mesh status 4/4, listeners 67/67, rollout 35/35 with every Pod Ready,
    running images 47/47 (build `70400e92…`), Gateway 8/8.
  - **Not touched.** `maops-state-0` (`2cd7be95…`) was never deleted. It
    and the other three Pods (`tft8d`, `vlp6q`, `zgrvt`) kept their UIDs.
  - **Not in the evidence.** The UID check, the non-forced deletes and
    the approval are the owner's account. The evidence directory holds
    the before and after states, not the delete commands themselves.
  - **Cause still an inference.** Why these Pods failed to enroll after
    the restart is not proven. One possibility is that they restarted
    before istio-cni and ztunnel on `worker2` were ready to set up the
    in-Pod redirection. The captured events show only readiness-probe
    `Unhealthy` warnings.
  - **Evidence.** Private, outside Git:
    `day8-runs/merged-main-mesh-72U9xD5v/`. It holds:
    - Pod lists and UIDs before the first delete;
    - the ambient checks before and after each replacement;
    - the worker2 istio-cni and ztunnel logs;
    - the resumed Day 7 checks.

    No logs or Secret data are copied into the repository. Three of its
    files (`ambient-before-first-gateway.log`,
    `ambient-after-first-gateway.log`, `ambient-before-last-gateway.log`)
    were created mode 0644, not the usual 0600. On 2026-10-05 only their
    modes were changed to 0600. Their sizes, mtimes and sha256 are
    unchanged:
    - `ambient-after-first-gateway.log`: `763a6f648d0022c9…`;
    - `ambient-before-first-gateway.log`: `be187e4c7890d46f…`;
    - `ambient-before-last-gateway.log`: `763a6f648d0022c9…`. Its content
      is identical to the previous file, as nothing changed between the
      two captures.

    All 14 files in the directory are now 0600, and the directory is
    0700.
  - **Timing.** All of this happened before run f837 took its baseline
    (11:11:51Z) and while no Day 8 run held the lock. Day 8's stable
    checks compare against each run's own baseline. No Day 8 phase
    replaced or resized a Day 7 Pod.

**Cause.** The cluster-free design allowed a valid VPA recommendation
equal to both declared requests:

- The recommender floor (`--pod-recommendation-min-cpu-millicores=10`,
  `--pod-recommendation-min-memory-mb=32`), `minAllowed` (10m/32Mi) and
  the declared requests (10m/32Mi) were all equal.
- On a cold start, with little usage history and actual usage below the
  floor, the recommendation is the floor itself.
- So the admission controller annotated the Pod but set the same values.
- Run H's recommendation (35m / 63,544,758 B) was above the floor, so it
  passed. Whether the run passed depended on usage at the time, not on
  the design.

### 12.2 Run `f4e69ac6356545efb4bf040995ca4863` (run I): exit 0

- **Command:** `PATH=/usr/bin:$PATH DAY8_RUN_ID=f4e69ac6356545efb4bf040995ca4863
  make day8-check`. The run ID was generated fresh and confirmed unused
  before the run.
- **Tree:** branch `fix/day-8-vpa-demonstration` on `78b02a1`, with the
  section-12 correction unstaged.
  - The sha256 of every `scripts/*.py` file and of `tests/test_day8.py`
    was taken before the run and verified unchanged after it.
  - Only docs were edited afterwards.
- **Time:** 2026-10-04 11:59:48Z → 12:18:36Z, **exit 0**
  (`day8-check: demonstrations exit 0, cleanup exit 0`; no `make ***`
  error).
- **Static:**
  - unit tests 1836 OK (173 of them Day 8);
  - version 65/65, manifests 267/267, Helm 2364/2364;
  - `day8-static-check` 5/5, including the new VPA change invariant;
  - preflight 7/7.
- **Before the demonstrations:**
  - CNI 4/4, context 6/6, mesh status 4/4, listeners 67/67;
  - rollout 35/35, running images 47/47, Gateway 8/8.
- **Setup:**
  - stable baseline 2/2, scaling image 4/4;
  - guards 5/5 (quota still `budget()`: 710m / 544Mi requests, 2000m /
    1088Mi limits, 13 Pods);
  - KEDA pre-install ownership 3/3, add-ons active 35/35;
  - stable 7/7, quota proof 9/9.
- **HPA 9/9:** 1 → 3 → 4 Ready under load, then 4 → 3 → 2 → 1. Stable
  7/7 afterwards.
- **VPA 17/17 (`vpa-2026-10-04T121016Z.json`).** Stable 7/7 afterwards.

  | | CPU request | Memory request | CPU limit | Memory limit |
  |---|---|---|---|---|
  | Declared (Deployment template) | 10m | 32Mi | 50m | 64Mi |
  | Off-mode `target` (= `uncappedTarget`) | 35m | 63,544,758 B (≈60.6Mi) | - | - |
  | Off-mode `lowerBound` / `upperBound` | 10m / 40m | **48Mi** / 96Mi | - | - |
  | Target at admission (both reads) | 35m | 63,544,758 B | - | - |
  | New Pod `bc19c1d5…` (admitted under `Initial`) | **35m** | **63,544,758 B** | **175m** | **127,089,516 B** |
  | Existing Pod `119514b2…` (before and after) | 10m | 32Mi | 50m | 64Mi |

  - **The new Pod.** It carries the admission annotation, and its
    requests equal the target at admission. Its requests differ from the
    declared ones in both CPU and memory, and its limits are scaled 5×
    for CPU and 2× for memory. All are within `maxAllowed` and the
    LimitRange.
  - **The existing Pod.** Its UID, resources and restarts (0 → 0) are
    unchanged, and it carries no annotation.
  - **Other checks.** The updater is not installed, and no Pod was
    blocked by the quota.
  - **Not a cold start.** This recommendation was above both floors and
    equal to run H's. `lowerBound` was capped up to the new 48Mi minimum,
    which shows the new `minAllowed` in force on the live object. The
    path where a cold-start target is capped up to 48Mi did not occur in
    this run. It is covered by the cluster-free invariant and by the
    tests from remediation log section 12, not by a live run.
- **KEDA 13/13.** ScaledObject Ready with the worker at 0, then 60 items
  queued and 60/60 processed. Workers went 0 → 1 → 3 → 2 → 1 → 0. Stable
  7/7 afterwards.
- **Cleanup 16/16.**
  - `keda`'s Day 8 labels were verified first.
  - Release ownership was confirmed and the empty-CRD guard passed; then
    the uninstall.
  - The runtime Secret (labels only) and the lease were deleted, then
    the `keda` namespace and the scaling namespace.
  - All 33 KEDA objects are NotFound.
- **Final gate:**
  - CNI 4/4, context 6/6, mesh status 4/4, listeners 67/67;
  - rollout 35/35, running images 47/47 (build `70400e92…`), Gateway
    8/8;
  - mesh-check 45/45, networkpolicy-check 37/37;
  - add-ons after cleanup 23/23;
  - stable 7/7.
- **Day 7 storage and state identity** (last stable check,
  `stable-check-2026-10-04T121836Z.json`):
  - Helm `maops-kubernetes-platform-day7` revision 14, deployed, chart
    1.0.0, manifest sha256 `abfd77b3…`;
  - PVC `2be6628b-7df3-421f-9f98-77f03d9a611d` Bound, PV
    `a9c74497-b133-40a1-aa8d-869b5cad4eab` on `maops-k8s-day7-worker`;
  - `state.json`: 15 B, sha256 `3ce4f556…`, served with status 200.
  - All 7 Pod UIDs, including `maops-state-0` `2cd7be95…`, equal run I's
    baseline and run f837's last stable check. No Day 7 Pod was replaced
    or resized during or between these two runs.
- **Log:** `make-day8-check.log` in the run directory (sha256 prefix
  `083433dda308a333`, mode 0600).
  - Every `FAIL` before `Ran 1836 tests … OK` comes from a negative unit
    test.
  - The five `FAIL` lines just after it are the mocked
    `tests/test_workload_refresh.py` output. They are byte-identical to
    the same lines in a cluster-free `make test`.
  - Every cluster step reports 0 failures.
- **Final state:**
  - **Releases:** Day 7 at Helm revision 14. Metrics Server and VPA at
    revision 14; the idempotent per-run add-on install bumps them each
    run, and they were at 12 at run H. No `keda` release.
  - **Namespaces:** `keda` and `maops-day8-scaling` are NotFound.
  - **Port-forwards:** none.
  - **Containers:** only the three `maops-k8s-day7` nodes are running.
  - **Evidence:** no evidence file contains a Secret `data` key or key
    material; directory 0700, files 0600.

### 12.3 Which run is authoritative

- **Run H** (`0d158cfe…`, exit 0): historical evidence for the code as
  merged in `78b02a1`. Its VPA pass depended on usage at the time
  (section 12.1, "Cause").
- **Run f837** (`f837802b…`): the failed merged-main attempt, kept intact
  together with its cleanup and its separate final gate exit 0.
- **Run I** (`f4e69ac6…`, exit 0): the authoritative run for the
  corrected tree.

`v1.0.0` is not tagged or released.

## Release disposition (2026-10-05)

The runs above, including historical run H and failed run f837
(`f837802b…`, VPA 16/17), are kept as recorded, as are the statements
that `v1.0.0` was not tagged or released at the time. Run I remains the
authoritative live run for the released code.

Day 8 was subsequently released as
[`v1.0.0`](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v1.0.0)
on 2026-10-05. PR #11 merged at `78b02a1`; PR #12 merged the VPA
correction at `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`, the fixed
target of the annotated tag (tag object `b1c0f00a…`). Corrected run I
(`f4e69ac6356545efb4bf040995ca4863`) passed. Run I was not a cold start;
the exact VPA floor case is covered by regression tests. The owner
reported the merged-`main` final gate exiting 0; only its stable 7/7 and
KEDA-absent results have saved files. The
[post-release verification record](day-08-post-release-verification.md)
is the current reference for release identity and evidence boundaries.

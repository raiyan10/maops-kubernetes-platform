# Day 7 (v0.7.0) - live validation record

**Run ID:** `dd99769f587d4a869628d51b9725b458` (one run ID for every
step; no second run, no recaptured baseline).
**Date:** 2026-09-28 (host timezone +06:00; times below are local).
**Branch / commit:** `feature/day-7-deployment-strategies` at HEAD
`74832c41a35a04d905aaa203e22f7d06bb7eb4e9`, with the Day 7
implementation **unstaged** in the working tree. A SHA-256 manifest of
all 68 changed/untracked files was taken before the run; the tree was
byte-identical to it after the run.
**Cluster:** isolated kind cluster `maops-k8s-day7`
(`kind/cluster-day7.yaml`, host `127.0.0.1:18081`), context
`kind-maops-k8s-day7`, kubeconfig `~/.kube/maops-k8s-day7.config`.

**Private evidence (outside the repository and /tmp, mode 0700 dirs /
0600 files):**
- step logs: `~/.local/state/maops-kubernetes-platform/day7-logs/dd99769f587d4a869628d51b9725b458/`
  (39 logs for the run itself, numbered in execution order, plus
  post-run read-only logs 34-35 - see "After the run");
- baselines: `~/.local/state/maops-kubernetes-platform/day7-runs/dd99769f587d4a869628d51b9725b458/`
  (`suite-baseline.json`, `strategy-baseline.json`; captured once,
  never rewritten - SHA-256, inode, mtime and mode verified unchanged
  after the run).

This record keeps every failed gate. Nothing below was re-run to hide a
failure; each re-run is listed with the original result it followed.

## Sequence and results

| Log | Time | Command | Result |
|---|---|---|---|
| 01 | 09:28 | `make day7-preflight` | 13/13 (MemAvailable 5.7 GiB; Day 6 containers were stopped at this time - see "Day 6") |
| 02 | 09:28 | `image-build` (Day 7 profile) | pass |
| 03 | 09:29 | `make day7-image-verify-local` | 9/9 - config digests gateway `sha256:b82570f0...`, app `sha256:2558fc01...`, state `sha256:39b82d52...`; each single-platform linux/amd64 |
| 04 | 09:30 | `cluster-create` | pass (3 nodes, NotReady before a CNI - expected) |
| 05 | 09:30 | `gateway-api-install` | pass |
| 06 | 09:33 | `cni-install` | pass (Cilium DaemonSet + operator rolled out) |
| **07** | **09:33:21** | **`cni-status`** | **FAIL 3/4** - `1/3 nodes Ready (expected 3/3)`; Cilium agents 3/3, operator, kube-proxy 3/3 passed |
| 07b | 09:33:40 | read-only diagnosis | all 3 nodes `Ready=True`: control-plane since 09:33:13, **both workers since 09:33:25** - 4 s after the snapshot. A cold-start timing race: `cni-status` is a single snapshot taken ~2 s after the Cilium rollout returned |
| 07c | 09:48:53 | `cni-status` re-run (only this gate) | **PASS 4/4** |
| 08 | 09:49:07 | `context-check` | 6/6 |
| **08b** | **09:49** | **`make day7-preflight` recheck** | **FAIL 12/13 - MemAvailable 3.0 GiB (< 4.0 GiB)** - the Day 6 containers had been started externally at 09:47:11 |
| **08c** | **10:02** | **`make day7-preflight` recheck** | **FAIL 12/13 - MemAvailable 3.2 GiB** (Day 6 containers ~2.8 GiB, Day 7 ~1.9 GiB) - stopped; no threshold lowered |
| 08d | 10:40:48 | read-only record | Day 6 containers exited 10:38:13 (stopped by the operator in Docker Desktop - see "Day 6") |
| 08e | 10:40 | `make day7-preflight` | **13/13** - MemAvailable 5.0 GiB |
| 09-10 | 10:42 | `mesh-install`, `mesh-status` | pass; 4/4 |
| 11-12 | 10:44 | `image-load`, `make day7-image-verify-nodes` | **19/19** - every Day 7 node holds each 0.7.0 tag with exactly the local config digest |
| 13-17 | 10:44-10:45 | `storage-bootstrap`, `storage-hardening-check`, `namespace-apply` (`k8s/day7/`), `secret-bootstrap`, `gateway-apply` | pass; Day 7 identities only (`maops-day7-validation`, `maops-day7-storage-*`); Secret values never printed (lengths only) |
| 18 | 10:46 | `make day7-deploy` (stable stage, Helm revision 1) | pass |
| 19-23 | 10:46-10:51 | `ambient-workload-check`, `rollout-check`, `gateway-check`, `mesh-check`, `networkpolicy-check` | 67/67, 35/35, 8/8, 45/45 (three wrong-identity denials, each AUTHORITATIVE correlated ztunnel evidence), 37/37 |
| 24-26 | 10:51 | `make day7-baseline-init`, `state-check`, `make day7-baseline` | pass; state 24/24; `/state` baseline value `null`; strategy baseline at Helm revision 1 |
| 27 | | `make day7-blue-green` | **83/83 - PRIMARY PASS, RESTORATION PASS** |
| 28 | | `make day7-stable-check` | **8/8** |
| 29 | | `make day7-canary` | **105/105 - PRIMARY PASS, RESTORATION PASS** |
| 30 | | `make day7-stable-check` | **8/8** |
| 31 | | `make day7-recreate` | **107/107 - PRIMARY PASS, RESTORATION PASS** |
| 32 | | `make day7-stable-check` | **8/8** |
| 33 | | `make day7-final-gate` | cni-status 4/4, context-check 6/6, mesh-status 4/4, ambient-workload-check 67/67, rollout-check 35/35, gateway-check 8/8, mesh-check 45/45, networkpolicy-check 37/37, **final-state-check 38/38**, **day7-final-state-check 39/39** (39 distinct assertions; two leak lines were *printed* twice - a cosmetic output defect fixed after the run, not a double count) |

Generic targets ran as `make CLUSTER_NAME=maops-k8s-day7
KUBECONFIG_PATH=$HOME/.kube/maops-k8s-day7.config <target>`; `day7-*`
targets set the Day 7 profile themselves. The staged sequence matched
`make day7-plan` (the Makefile order at the time of the run).

## Strategy evidence

### Blue/Green - PRIMARY PASS, RESTORATION PASS (83/83)
- Helm revisions: 1 (stable) -> 2 green-prepared -> 3 cutover -> 4
  restoration; after every stage `helm get values` equalled the stage
  file exactly.
- HTTPRoute: unchanged by preparing Green (same UID, generation 1,
  `maops-gateway:8080`); after cutover the same route at generation 2
  with the single backend `maops-gateway-candidate:8080`, Accepted and
  ResolvedRefs True for the current generation.
- Endpoints: candidate ready endpoints `10.244.1.29`, `10.244.2.78`
  equal the ready candidate Pod IPs; stable kept 3 ready endpoints;
  addresses and Pods disjoint.
- Enforced path checks before cutover (destinations verified live:
  ClusterIP + ready endpoints): candidate -> app HTTP 200; control app
  -> state HTTP 200; candidate -> state, app -> candidate, and app ->
  stable gateway each **no HTTP response** (`reset:request`). These
  show reachability outcomes only - not authorization evidence, and
  they do not identify which layer denied the traffic.
- External: `/` 20/20 candidate, `/backend` 5/5 normal app results via
  candidate Pods; Blue stayed 3/3 Ready.
- Restoration: no candidate-only object, route generation 3 back to
  `maops-gateway:8080`, external 10/10 stable. Independent
  `day7-stable-check` 8/8.

### Canary - PRIMARY PASS, RESTORATION PASS (105/105)
- Helm revisions: 4 -> 5 prepare -> 6 canary 90/10 -> 7 Phase A restore
  -> 8 candidate-unready -> 9 restoration.
- HTTPRoute at generation 4: `maops-gateway:8080` weight 90 and
  `maops-gateway-candidate:8080` weight 10, fresh conditions.
- Ready endpoints: stable 3 and candidate 2, disjoint; no Pod churn
  during sampling. Path checks before promotion: same five outcomes as
  Blue/Green.
- **External sample: 200/200 successful requests - 185 stable, 15
  candidate, 0 errors, 0 unidentified** (12.7 s). A finite sample
  consistent with a weighted split; **not** a claim of an exact 90/10
  distribution.
- Negative scenario: 2 candidate Pods alive (Running, started) but not
  Ready, 0 ready endpoints through the 30 s hold; the promotion was
  **refused at the readiness gate** (5 blocking checks) before any path
  probe ran; Helm revision stayed 8, route unchanged (same UID,
  generation 5), external 30/30 stable.
- Restoration: no candidate-only object, route stable, external 10/10
  stable. Independent `day7-stable-check` 8/8.

### Recreate (candidate only) - PRIMARY PASS, RESTORATION PASS (107/107)
- Helm revisions: 9 -> 10 recreate-prepared -> 11 recreate-serving ->
  12 recreate-changed -> 13 restoration.
- Preconditions enforced: stable `maops-gateway` still RollingUpdate;
  candidate strategy exactly `{'type': 'Recreate'}`; path checks passed;
  route 100% candidate (generation 6); 2 candidate Pods with exactly
  those 2 as ready endpoints; one active ReplicaSet; Helm revision
  readable; no PDB selects any candidate Pod.
- Owned Helm child exited on its own (exit 0); rollout settled at
  t=50.2 s.
- Controller evidence: same Deployment UID; generation 1 -> 2 observed;
  Pod-template checksum changed; `deployment.kubernetes.io/revision`
  1 -> 2; one new active ReplicaSet with pod-template-hash `7bffbfb549`
  (old `5c9db85744`); no pre-rollout Pod UID remains.
- Observed ordering: 50 Pod/EndpointSlice samples ~0.5 s apart; old
  Pods last seen t=34.8 s, first new Pod t=35.9 s, first new Ready
  t=46.9 s; no overlap observed. The ordering **guarantee** is the
  Deployment controller's `Recreate` strategy; 0.5 s sampling cannot
  exclude an overlap shorter than the sampling interval.
- **Observed interruption:** 239 external samples (~0.25 s apart, each
  bounded at 2 s): 19 old-message, **160 failed requests between
  t=5.1 s and t=45.8 s**, then 60 new-message; no old message after the
  first new one; last 10 samples all new - the planned Recreate outage
  for traffic routed to the candidate.
- A PDB was not treated as protection: PDBs limit Eviction API
  evictions, not the controller's Recreate scale-down.
- Restoration: no candidate-only object, route generation 7 back to
  stable, external 10/10 stable. Independent `day7-stable-check` 8/8.

## Final state (after `day7-final-gate`)
- Release `maops-kubernetes-platform-day7` at revision 13, `helm get
  values` == `helm-values/day7/stable.yaml`; deployed manifest SHA-256
  equal to the pre-experiment baseline.
- `maops-gateway`, `maops-app`, `maops-state`: UIDs and generations
  unchanged since the baseline (generation 1); gateway 3/3, app 3/3,
  state 1/1; Service/PDB/route UIDs unchanged; namespace, PVC and PV
  UIDs unchanged; `/state` equals the baseline (`null`).
- No candidate-only objects, no Pods in `maops-day7-validation`, no
  Day 7 mesh-probe or storage scratch namespaces, no namespace or
  object carrying a Day 1-6 identity, no leaked port-forward or helm
  process (all logged in 33).
- The Day 7 lock file `/tmp/maops-day7-mutation.lock` was present but
  not held. **Source:** an in-session read-only `fuser` check after the
  final gate - not saved as a numbered log.
- Older kind clusters are not part of the Day 7 gate and were not
  listed or contacted by it.

## Day 6
- **Pre-existing, cause unconfirmed:** the three `maops-k8s-day6` node
  containers had exited (code 137) at 09:22:13, shortly after the host
  booted (~09:20), before any live command of this run. **Source:** an
  in-session read-only `docker ps`/`docker inspect` (State.FinishedAt
  2026-09-28T03:22:13Z) taken at ~09:27 and ~09:48 - not saved as a
  numbered log; Docker has since overwritten FinishedAt with the later
  10:38:13 stop recorded in log 08d. They were then
  started by an explicit Docker `start` event at 09:47:11 (restart
  count 0; not by this run).
- **The automated Day 7 run left Day 6 untouched:** no Day 7 command
  started, stopped, modified or deployed to it, or contacted its API.
- **Operator action (one-time, explicit):** to restore memory headroom
  after the two failed preflight rechecks, the operator stopped the
  three Day 6 containers in Docker Desktop (exited 10:38:13). Container
  IDs (`6ae11339a314`, `18e26c449211`, `94a809861564`) and creation
  times (2026-09-22) were recorded unchanged; the cluster was not
  deleted or recreated.

## After the run (post-run changes, not part of the run)
- A bounded, Day 7-only node readiness wait (`make day7-nodes-ready`,
  `scripts/day7_nodes_ready.py`) was added between `cni-install` and
  `cni-status`. `cni-status` itself is unchanged (still one snapshot).
  A read-only run against the already-Ready Day 7 cluster passed on the
  first observation (log 34). This does **not** show that a fresh
  one-shot `make day7-check` now passes; that has not been run.
- Reused scripts now print "Day 7" under the Day 7 profile (log 35 shows
  the reworded `cni-status`, 4/4); Day 6 output is unchanged.
- After the independent reviews (`day-07-independent-reviews.md`):
  `day7-resume-check` now starts with `day7-nodes-ready`; the Day 7
  final check prints each leak fact once; the path-check plan always
  evaluates positive controls first regardless of declaration order; a
  promotion whose Helm upgrade fails after a clean gate is recorded;
  every kubectl/Helm call in `scripts/day7_strategy.py` refuses to run
  outside the Day 7 profile; the candidate's no-PDB comment is scoped.
  None of these were part of the recorded run.

## Not claimed
Production readiness; a different application release (0.7.0 images
are rebuilds of the unchanged v0.6.0 sources; the candidate is a
configuration variant sharing the stable ServiceAccount/Istio
principal); exact traffic percentages; zero-downtime Recreate; that the
path-check denials identify the enforcing layer; or a clean fresh
one-shot `day7-check`.

## Second run: one-shot `make day7-check` on the EXISTING cluster (run `ce55f5fb51614b419d0ed4264afb7e79`)

A single uninterrupted `make day7-check` (15:31:31-15:56:33 +06:00,
exit 0) with a new run ID and a new private run directory (created by
`day7-baseline-init` at 15:43). **It reused the already-running
`maops-k8s-day7` cluster, so it does not show that a fresh cluster
starts cleanly.** Day 6 stayed stopped (containers still `Exited (137)`).
Output: `day7-logs/ce55f5fb.../day7-check-oneshot.log` (7907 lines,
sha256 `c1e6dde3...`).

Before the run, every mutating step was checked for safe reuse:
- `cluster-create` skips an existing cluster.
- The installs and the platform apply are idempotent. `day7-deploy`
  re-applied the stable stage with no manifest change: revision 13 -> 14,
  no rollout.
- Storage bootstrap is a verified no-op and uses only scratch namespaces.
- Secret bootstrap keeps existing Secrets (never rotates them).
- Baselines are written O_EXCL into the new directory.

The first run's 43 evidence files and its baselines were hash-checked
unchanged afterwards.

The operator's `~/.local/bin/docker` wrapper (`exec docker.exe`) fails
`tool-check`'s `/usr/bin/docker` rule, so this run used
`PATH=/usr/bin:$PATH`. Both CLIs report the same engine (ID
`3f846e5d...`, docker-desktop 29.8.0). The first run used the wrapper.

**Results** (every gate passed):
- Static checks: unit suite 1554 OK; version-check 64/64;
  manifest-check 267/267; helm-check 2332/2332; preflight 13/13.
- Images: local contract 9/9; nodes-ready 3/3 on the first
  observation; per-node image contract 19/19.
- Platform: cni-status 4/4; context 6/6; mesh-status 4/4; storage
  bootstrap and hardening passed; ambient workload 67/67; rollout 35/35;
  gateway 8/8; mesh 45/45; networkpolicy 37/37; state 24/24.
- Strategies (Helm revisions 14 -> 26):

| Strategy | Checks | PRIMARY | RESTORATION | Independent `day7-stable-check` |
|---|---|---|---|---|
| Blue/Green | 83/83 | PASS | PASS | 8/8 |
| Canary | 105/105 | PASS | PASS | 8/8 |
| Recreate (candidate only) | 107/107 | PASS | PASS | 8/8 |

- Recreate observed no old/new overlap in 43 samples, and 180 failed
  external requests between t=3.1s and t=50.2s: the planned outage.
- Final gate: all sub-gates passed, final-state 38/38, Day 7 final
  checks 39/39. The leak lines now print once.

**Code freeze during the run.** The tree was verified equal to the
pre-run manifest at 15:25:26. After the run, it differed only by two
NEW untracked files:
- `scripts/day7_validation_client_probe.py` (created 15:33:19);
- `tests/test_day7_validation_client_probe.py` (created 15:38:59).

Both were last modified at 15:39:39. Neither is referenced by the
Makefile or by any pre-existing test, and the unit tests had finished
before either existed (the log was already at
`day7-image-verify-local` by 15:34:59). No file that `day7-check` reads
changed, so every step ran one code version. The Makefile target for the
probe was added only after the run exited.

**Limitation: the stable Pods were not refreshed onto this run's
images.** `image-build` produced new config digests:

| Image | This run | First run |
|---|---|---|
| gateway | `62df7c01...` | `b82570f0...` |
| app | `b035e264...` | `2558fc01...` |
| state | `aafa33a4...` | `39b82d52...` |

`day7-image-verify-nodes` proved every node holds the new images. But
because `day7-deploy` caused no rollout, the stable gateway/app/state
Pods (created 10:45 in the first run) kept running the first run's
images, which remain on the nodes untagged. Only Pods created during the
experiments, such as the candidates, used this run's build.

Both builds come from the same unchanged sources, but no Day 7 gate
compares running Pods' image IDs with the loaded images. On an existing
cluster that difference passes silently. A fresh-cluster `day7-check`,
or a Day 7 check of running image IDs, would close this gap.

### Live `validation-client -> gateway-candidate` denial probe
Run separately after the run exited: `make day7-validation-client-probe`
(new, optional, never part of any gate; log `40-...`, 16:00:19-16:02:50,
exit 0). **PRIMARY PASS, RESTORATION PASS, 84/84.**

1. **Entry gate:** the stable stage, no candidate objects, 5/5 stable
   external samples.
2. **Stage:** `green-prepared` (revision 26 -> 27; route unchanged at
   100% stable). The full candidate readiness gate passed.
3. **Destination:** Service `maops-gateway-candidate`, ClusterIP
   `10.96.50.12`, 2 ready endpoints (`10.244.1.188`, `10.244.2.22`).
   Each Pod served `/livez` over loopback.
4. **Source:** probe Pod `maops-day7-validation/day7-vc-probe-86d4108bee`
   (validation-client label, non-root, no token, no Secret) on
   `worker`, IP `10.244.1.48`. Its namespace is not mesh-enrolled.
5. **Positive control, same Pod:** ingress Gateway
   `maops-edge-istio.maops-ingress` (ClusterIP `10.96.107.7`, 1 ready
   endpoint) with `Host: maops.local` returned HTTP 200.
6. **Negative, twice:** the candidate name resolved to its live
   ClusterIP both times, and there was no HTTP response (connect timeout
   of 4 s each time).
7. **Authoritative evidence:** Cilium agents on all 3 nodes
   (`cilium-dbg monitor --type drop`, owned children, all terminated and
   reaped) recorded 8 `Policy denied` drops. Each was identity
   47165 -> 6814, from `10.244.1.48` to candidate Pods on `:8080`, TCP
   SYN, at `bpf_lxc.c:2412`: 4 at `10.244.1.188` (worker) and 4 at
   `10.244.2.22` (worker2).
8. **Identities:** `cilium-dbg identity get` resolved the source to the
   `maops-day7-validation` validation-client and the destination to
   `component=gateway-candidate`.
9. **Raw capture:** saved O_EXCL, mode 0600, as
   `day7-runs/ce55f5fb.../day7-vc-probe-86d4108bee-cilium-drop-monitor.txt`.
10. **Cleanup:** the probe Pod was deleted and explicitly confirmed
    NotFound. Restoration re-applied `stable` (revision 27 -> 28), and
    10/10 external samples were stable.

**Scope:** this proves the Cilium NetworkPolicy layer drops
validation-client -> candidate traffic before it reaches the candidate.
The Istio AuthorizationPolicy layer is not reached on this path; its
candidate coverage remains proven statically.

### Independent verification afterwards (run `ce55f5fb...` baselines, unchanged)
- `make day7-stable-check`: 8/8 (log `41-...`).
- `make day7-final-gate`: every sub-gate passed, final-state 38/38,
  Day 7 final checks 39/39 (log `42-...`).
- Baseline files: hashes unchanged, mtime still 15:43.
- Helm: revision 28, values equal `stable.yaml`.
- No leftovers: 0 candidate objects, 0 Pods in `maops-day7-validation`,
  0 `day7-vc-probe` Pods; no kubectl/helm/Day 7 processes.
- Storage: PVC `data-maops-state-0` uid `91b45f89...` bound to PV
  `pvc-91b45f89...` uid `f830a3e9...`. This is identical in both runs'
  baselines.
- Repository: index empty; Day 6 still stopped.

Static checks after adding the probe target: unit suite 1574 OK (20 new
probe tests), `make ci-check` PASS, with 0 cluster calls attempted under
the cluster-blocking PATH shim.

Still not claimed: that a fresh cluster starts cleanly with a one-shot
`day7-check`; that stable Pods run the images built by the same run; or
anything in the earlier "Not claimed" list.

### Observation after a host restart (2026-09-29, read-only)
A read-only snapshot was saved as
`day7-logs/post-reboot-2026-09-29/01-post-reboot-readonly-snapshot.log`
(not part of either run).
- **Restart:** WSL booted at 09:09:02 +06:00. At 03:09:59Z Docker
  Desktop auto-started every `maops-k8s-day1`...`day7` node container
  (restart policy `on-failure`), including Day 6 and the older clusters.
- **Day 1-6 stopped again:** all were stopped at 03:23:41-42Z by an
  action outside this session; the cause is not confirmed here. No
  command in this session started, stopped or contacted them.
- **Day 7 recovery:** the Pods restarted between 03:24 and 03:25Z. The
  recovery was not immediate:
  - Pod sandboxes failed until the Cilium agent socket was available.
  - ztunnel was 0/1 while it could not reach istiod.
  - The app and gateway Pods were 0/1 (startup refused, readiness 503)
    for about 15 minutes.
  - By 09:28 +06:00, ztunnel was 1/1 on every node and the worker2 Pods
    were Ready. `maops-app-...-rtnnh`, `maops-gateway-...-hgwpc` and
    `-nrfnr` on `worker` were still not Ready.
- **No intervention:** no Pod was deleted and no recovery action was
  taken. This matches the Day 6 note that recovery after a host restart
  varies in this environment.
- **Images after the restart:** the restarted stable containers
  resolved the `:0.7.0` tags to the images now tagged on the nodes, so
  they report the second run's builds (app `4e15326d...`, gateway
  `75931fef...`, state `b84ced02...` import digests). Pod UIDs and
  creation times are unchanged. This does **not** change the finding
  above: during run `ce55f5fb...` itself, including its strategies and
  final gate, the stable Pods ran the first run's images. The image
  change came from an incidental container restart, not a controlled
  rollout.

### Post-reboot diagnosis (2026-09-29, read-only)
Logs are in `day7-logs/post-reboot-2026-09-29/`: `02-day7-resume-check.log`
and `03-unready-pod-diagnosis-readonly.log`.

**`MAOPS_CLUSTER_PROFILE=day7 make day7-resume-check` failed** at
09:54:10 +06:00:
- **Passed:** nodes-ready 3/3 on the first observation, cni-status 4/4,
  context 6/6, mesh-status 4/4.
- **Failed: ambient-workload-check, 64/67.**
  `maops-gateway-6bc587f6b5-hgwpc`, `maops-gateway-6bc587f6b5-nrfnr` and
  `maops-app-79857bdd86-rtnnh`, all on `maops-k8s-day7-worker`, have no
  ztunnel in-Pod LISTEN sockets on 15001/15006/15008.
- **Not run:** make stopped there, so `rollout-check` and
  `gateway-check` never started.
- **Wrong trailer:** the log's last line reads "exit 0", but make exited
  2. The `$(date)` in that `echo` reset `$?`. The same trailer bug is in
  logs `41` and `42` of run `ce55f5fb...`. Those two did pass: neither
  has a make error or a FAIL line.

**What was observed in the three Pods** (read-only):
- `/livez` returns 200 and `/readyz` 503; each Pod logged about 340
  readiness failures.
- Pod UIDs and creation times are unchanged, with one container restart
  each at 03:24-03:25Z. They are Ready=False from 03:12:27Z.

**Cause, supported by the istio-cni and ztunnel logs on `worker`:**
- **Startup reconcile ran too early.** At 03:25:05Z, the restarted
  istio-cni node agent's startup in-Pod reconcile ran before those Pods'
  new sandboxes had a netns or IP. It logged three times: "failed to
  reconcile inpod rules for pod, try restarting the pod ... can't find
  netns for pod".
- **The CNI chain was restored a second later.** At 03:25:06.119Z, the
  agent re-wrote `05-cilium.conflist` to put itself back in the chain.
- **No enrollment followed.** The Pods' sandboxes became ready at
  03:25:12-36Z, but istio-cni logged no plugin event for them.
- **ztunnel was told to keep, not add.** When ztunnel connected at
  03:26:03Z, istio-cni sent `keep` (netns not available) for UIDs
  `a810927f...` (rtnnh), `ca056559...` (hgwpc) and `d22a1fc5...` (nrfnr).
  It never sent `add`, so no in-Pod listeners were created.
- **worker2 recovered.** On `worker2` the Pods' netns existed at
  reconcile time, and those Pods recovered.
- **One step is inference.** That the ADD for these three sandboxes
  read a conflist still lacking istio-cni fits the timings and the
  earlier 30-second Cilium-socket waits, but was not directly observed.

**No resource was changed.** No Pod was deleted, and Cilium, Istio,
Docker and the cluster were not restarted. Live work stopped here, as
required. The Day 7 cluster is **not healthy**, so Phase 3 (the live
rollout proof) has not been run.

**Image identity per container:**

| Container | Before the reboot (2026-09-28) | After the reboot (2026-09-29) |
|---|---|---|
| app | import `6c4836d8...` (config `2558fc01...`, first run's build) | import `4e15326d...` (config `b035e264...`, second run's build) |
| gateway | import `421aa00a...` (config `b82570f0...`, first run's build) | import `75931fef...` (config `62df7c01...`, second run's build) |
| state | import `e7046072...` (config `39b82d52...`, first run's build) | import `b84ced02...` (config `aafa33a4...`, second run's build) |

- **How the new images got there:** the restarted containers resolved
  the mutable `:0.7.0` tags to the images now tagged on the nodes, with
  no Helm change or rollout. This is an incidental effect of the
  restart. It is not a controlled rollout, and not evidence for the
  second run.
- **How the digests map:** the import-to-config mapping comes from
  `crictl images` on the workers (2026-09-28). The second run's local
  config digests are the ones `day7-image-verify-local` verified.
- **Why the local Docker IDs differ:** the local Docker `.Id`s
  (gateway `19d5bbf7...`, app `380d6e18...`, state `f21be325...`) are
  manifest digests, a third digest kind.

### Image-contract remediation (static, 2026-09-29)
The confirmed defect is described above ("Limitation: the stable Pods
were not refreshed"). The fix is designed in `docs/architecture.md`,
"Verified build identity".

**How the build is pinned:**
- **Content-derived tags:** `scripts/day7_build.py` tags each image
  `<repo>:0.7.0-cfg-<config digest>` and keeps a private build record.
- **Helm overlay:** that tag reaches every Day 7 stage and `day7-deploy`
  as a second values file.
- **Chart guard:** the chart refuses to render a Day 7 stage without it.

**How the running build is checked:**
- **Running-image gate:** `scripts/day7_running_images.py` maps each
  container's Kubernetes imageID through its node's containerd record
  to the build's config digest.
- **Where it runs:** after the stable deploy, in every stable/
  restoration check, in the resume check, in the final gate, and for
  candidate Pods in the promotion gate and after the Recreate
  replacement.

**One build per run:** baselines record the build, and a different
build requires a new run ID.

These changes are static and unit-tested only. They have not been
exercised against the cluster.

**Reviews.** Focused architecture, security, integration and test
reviews returned APPROVE WITH NON-BLOCKING FINDINGS. Dispositions are in
`day-07-independent-reviews.md`.

**Later observation (integration review, read-only).**
`maops-gateway-...-6tchc` on worker2 and `maops-gateway-...-hgwpc` now
show restart count 3. They restarted again after the diagnosis above was
captured. The three `worker` Pods are still 0/1.

**Phase 3 (live rollout proof of the fix): NOT RUN.** Its precondition
is that the read-only preflight and `day7-resume-check` pass, and
`day7-resume-check` still fails. A rollout would replace the three
unenrolled Pods and very likely "cure" them. That would destroy the only
reproduction of the enrollment race, and it would present a remediation
as a clean proof.

**Next step, once the owner decides separately:**
- **First:** confirm the Cilium and istio-cni DaemonSets are Ready.
- **Before any Helm change:** record the stable workload identities,
  the PVC/PV identities and the images actually running.
- **Build and deploy:** run `day7-image-verify-local`,
  `day7-build-record`, `day7-image-load`, `day7-image-verify-nodes`,
  then `day7-deploy`.
- **Verify:** run `day7-running-images` and the resume checks, and
  confirm PVC/PV identity after the rollout.
- **New run:** use a new run ID and new baselines before any
  experiment. Run `ce55f5fb...`'s baselines have no build record, so
  the code now refuses them, by design.

## Result A: recovery of the three unenrolled Pods (2026-09-29, 14:28-14:39 +06:00)
Logs are in `day7-logs/post-reboot-2026-09-29/`, files `04` to `10`.
The helper is `recover_one.py.txt`, a one-off copy that is not project
code.

**Reproduction preserved first (read-only, log `04`):**
- The same three Pods on `worker` were still 0/1. Each listened only on
  8080: no 15001/15006/15008.
- `/livez` returned 200. `/readyz` returned 503 with
  `{"status": "backend unavailable"}` for the gateways and
  `"state unavailable"` for the app.
- Healthy peers listened on `8080, 15001, 15006, 15008, 15053`.
- Helm was at revision 28 with the stable values.
- PVC `91b45f89...` and PV `f830a3e9...` were unchanged.
- Every Day 1-6 node container was still `Exited (137)`.

**Preflight (logs `05`, `05b`):**
- The first run failed 12/13 on "Docker daemon reachable": `docker
  info` gave no result within the script's 20 s bound.
- A recheck a minute later passed 13/13 (MemAvailable 4.6 GiB). Both
  logs are kept.

**Recovery, one Pod at a time.** Before each delete the helper verified:
- the Pod still had the identified UID;
- its owner chain was ReplicaSet -> the expected Deployment;
- it was still NotReady and still lacked listeners.

After each delete it waited for the controller's replacement, and
required a different UID, the listeners, Ready still holding 10 s later,
and all other workloads unchanged.

| Deleted Pod (UID) | Replacement (UID) | Node | Running image (imageID -> config) |
|---|---|---|---|
| `maops-app-...-rtnnh` (`a810927f...`) | `maops-app-...-4fqsf` (`a0d8eeab...`) | same (`worker`) | `4e15326d...` -> `b035e264...` |
| `maops-gateway-...-hgwpc` (`ca056559...`) | `maops-gateway-...-cmjrp` (`224a5d11...`) | same (`worker`) | `75931fef...` -> `62df7c01...` |
| `maops-gateway-...-nrfnr` (`d22a1fc5...`) | `maops-gateway-...-dh5xk` (`1d45844d...`) | same (`worker`) | `75931fef...` -> `62df7c01...` |

- **Outcome:** app returned to 3/3, then gateway to 2/3 and 3/3. State
  stayed 1/1 throughout. Nothing else changed: `maops-state-0` was not
  touched, and Istio, Cilium, Docker and the cluster were not restarted.
- **`day7-resume-check` after recovery (log `09`):**
  - Passed: nodes 3/3, CNI 4/4, context 6/6, mesh 4/4, ambient
    listeners 67/67, rollout 35/35.
  - Then the new running-image gate failed closed, because no verified
    build existed yet. That is by design before the first pinned deploy.
  - `gateway-check`, which make skipped as a result, passed 8/8 when run
    on its own (log `10`).

**Limits of this result:**
- Deleting the Pods gave them fresh CNI ADDs on a node whose istio-cni
  and Cilium were by then healthy. This shows that recreation restores
  enrollment. It does not isolate the ambient cause; the inference
  noted above stays an inference.
- The replacements took the newer image under the mutable `:0.7.0` tag
  (the second run's build). So this recovery says nothing about the new
  Helm image mechanism.

## Result B: live proof of the build pinning (run `c252aa3d90c84c7da1b6c9ae9c1c57a0`) - STOPPED at the first failed gate
Logs are in `day7-logs/c252aa3d90c84c7da1b6c9ae9c1c57a0/`.

**Starting point.** The health checks passed as above (only the new
gate failed, as expected before any pinned deploy). The run used a
fresh run ID, because both earlier baselines lack a build record.
Pre-change identities (log `01`):
- gateway/app Deployments and the state StatefulSet at generation 1,
  images `...:0.7.0`;
- all 7 Pod UIDs and imageIDs;
- PVC/PV as before;
- Helm revision 28;
- the cilium, istio-cni-node and ztunnel DaemonSets all 3/3.

| Step | Result |
|---|---|
| `day7-image-verify-local` (log `02`) | 9/9 PASS |
| `day7-build-record` (log `03`) | build `fdb68741...`: gateway `62df7c01...`, app `b035e264...`, state `aafa33a4...`. These are the bytes the stable Pods already ran, so the rollout changes only the reference. |
| `day7-image-load` (log `04`) | pinned tags loaded into `maops-k8s-day7` only |
| `day7-image-verify-nodes` (log `05`) | 31/31 PASS, pinned tags included |
| `day7-running-images` before deploy (log `06`) | **FAIL, as intended:** all 7 Pods rejected for the mutable `:0.7.0` reference. Every digest mapping passed (39/47). |
| `day7-deploy` (log `07`) | exit 0, Helm revision 28 -> 29 |
| `day7-running-images` after deploy (log `08`) | **FAIL (8 checks) - first failed gate; live work stopped** |

**What the deploy did** (log `09`, read-only):
- **Helm:** revision 29 user values are the stable stage plus exactly
  the three pinned tags.
- **Workloads:** gateway and app Deployments and the state StatefulSet
  are now generation 2. UIDs are unchanged; templates carry
  `...:0.7.0-cfg-<digest>`. There is a new gateway ReplicaSet
  `5f6df5675f` and a new app ReplicaSet `78c964c9ff`. `maops-state-0`
  was replaced by its controller (new UID `0799dd5c...`).
- **Storage:** PVC `data-maops-state-0` uid `91b45f89...` is still bound
  to PV `pvc-91b45f89...` uid `f830a3e9...`.
- **Pod images:** every new Pod's spec image is the pinned ref. Every
  imageID maps through the node record to the build's config digest.
- **Listeners:** ambient listeners 67/67 (log `10`).

**Why the gate failed:**
- **The gate's assumption was wrong.** The gate required
  `containerStatuses[].image` to equal the pinned ref. After the pinned
  tags were loaded, each containerd image record carries both
  `...:0.7.0` and `...:0.7.0-cfg-<digest>`. The runtime reports the
  record's first tag, `...:0.7.0`.
- **What `crictl inspect` shows for `maops-state-0`:**
  - `status.image = ...:0.7.0`;
  - `info.config.image.user_specified_image = maops-kubernetes-state:0.7.0-cfg-aafa33...`;
  - `status.imageId = sha256:aafa33...` (the config digest);
  - `status.imageRef` = the import repo digest.

**Fix (static only; not yet re-run live):**
- **What the check now compares:** the reference check compares the Pod
  SPEC's container image with the pinned ref, and reports the runtime
  tag as information only. The imageID -> record -> config digest check
  is unchanged.
- **What still fails:** a Pod on the mutable tag, or running other
  bytes.
- **Regression tests:** `RuntimeReportedTagTests`.
- **Static results:** `make ci-check` exit 0 (1657 tests, helm-check
  2346/2346) with 0 blocked cluster calls, log
  `claude-work/ci-check-20260929-145817.log`.

**State left:** the cluster is at revision 29 (stable stage plus the
build). No baseline was captured for run `c252aa3d...` and no experiment
ran. The corrected gate, the full resume check, the new baselines and
the final gate are still to run. Not claimed: that the running-image
gate has passed live.

## Result B, continued: run `c252aa3d...` on the existing revision 29 (2026-09-29, 15:20-15:30 +06:00)
Nothing was rebuilt, reloaded or upgraded, no Pod was replaced, and no
experiment ran. Logs are `11` to `18` in the same run directory.

**B1. The initial gate FAILED, and the failure stands.** Log `08`
(14:55:33, make exit 2) failed 8 checks. The gate required
`containerStatuses[].image` to be the pinned ref, but the runtime
reported `...:0.7.0`. Log `08` is unchanged. That run of the gate did
not pass.

**B2. The checker correction (`scripts/day7_running_images.py`,
sha256 `c8d36219...`).**
- **Now compared:** the Pod SPEC's container image (what Helm rendered
  and the kubelet was asked to run) against the pinned ref.
- **Now informational only:** `containerStatuses[].image`. containerd
  reports an image record's first repoTag there.
- **Unchanged:** the imageID -> node record -> config digest check, and
  the record-carries-pinned-tag check.
- **Still fails:** a Pod on the mutable tag, or running other bytes.

**B3. Static regression evidence.**
- `RuntimeReportedTagTests`, with the test fixture's runtime tag now
  defaulting to the mutable tag as observed live. It covers:
  - a pinned spec with correct bytes passes although the runtime
    reports `:0.7.0`;
  - a pinned-looking runtime tag cannot rescue a mutable spec;
  - a missing spec container fails.
- `make ci-check` exit 0: 1657 tests, helm-check 2346/2346, 0 blocked
  cluster calls. Log `claude-work/ci-check-20260929-145817.log`.

**B4. Independent CRI evidence (log `11`).** This did not use
`day7-running-images`; it used `kubectl` plus `crictl inspect` on each
container's own node. There were 0 differences across 7 containers x 4
facts:
- the Pod spec image is the pinned tag;
- CRI `info.config.image.user_specified_image` is the same pinned tag;
- CRI `status.imageId` equals build `fdb68741...`'s recorded config
  digest: gateway `62df7c01...`, app `b035e264...`, state `aafa33a4...`;
- the Pod and container are Ready.

The containers inspected:

| Pod | UID | Container | Node |
|---|---|---|---|
| `maops-app-78c964c9ff-f2rcq` | `ebface2c...` | `ba797a67` | worker |
| `maops-app-78c964c9ff-f67xr` | `8a62af46...` | `cc5ed225` | worker2 |
| `maops-app-78c964c9ff-rb4lw` | `9bc187f9...` | `6f076761` | worker2 |
| `maops-gateway-5f6df5675f-c6w99` | `877f9f94...` | `2b61b7dd` | worker2 |
| `maops-gateway-5f6df5675f-gl87p` | `746503eb...` | `db93d268` | worker2 |
| `maops-gateway-5f6df5675f-n2dhp` | `e349c25d...` | `f202b06a` | worker |
| `maops-state-0` | `0799dd5c...` | `13f76365` | worker2 |

One stray line in log `11` ("Permission denied" writing a temp file)
came from a leftover redirect in the capture command. It ran nothing
and affected no result.

**B5. Rerun result.**
- **Corrected `make day7-running-images` (log `12`):** 47/47 PASS for
  build `fdb68741...`.
- **Full `day7-resume-check` (log `13`):** PASS. It covered nodes 3/3,
  CNI 4/4, context 6/6, mesh 4/4, listeners 67/67, rollout 35/35,
  running images 47/47 and gateway 8/8.

**B6. Baselines for run `c252aa3d...`** were captured in the
documented order, and only because none existed:
- `day7-baseline-init` created the run directory, mode 0700 (log `14`).
- `state-check` passed 24/24 and wrote the suite baseline (log `15`).
- `day7-baseline` wrote the strategy baseline (log `16`).

Both files are mode 0600. The strategy baseline records:
- Helm revision 29, with the three pinned tags in its values;
- build `fdb68741...` with its three config digests;
- workloads at generation 2.

Hashes: strategy `6cbdc844...`, suite `8777a74e...`. Both were unchanged
after the final gate.

**B7. Final gates.**
- **`day7-stable-check` (log `17`):** 55/55.
- **`day7-final-gate` (log `18`):** all passed.
  - Platform: CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67.
  - Workloads: rollout 35/35, running images 47/47.
  - Behaviour: gateway 8/8, mesh 45/45, NetworkPolicy 37/37.
  - Final state: final-state 38/38, Day 7 final 86/86.

**B8. Storage identity is unchanged.** PVC `data-maops-state-0` uid
`91b45f89-f452-4504-8609-85f1495d9b02` is bound to PV
`pvc-91b45f89-...`, uid `f830a3e9-1131-48f0-a687-e200c4556d67`. The PV's
claimRef matches the PVC. This held before the rollout, after it, after
the final gate, and in every baseline of all three runs. Only
`maops-state-0`'s Pod was replaced, by its StatefulSet: UID
`b98b328f...` became `0799dd5c...`.

**Other state:**
- Day 1-6: 14 node containers remain `Exited`, untouched.
- The earlier runs' evidence and baselines are unchanged.
- Repository: index empty.

**Not claimed:**
- That the initial gate passed.
- That the build is new code. Build `fdb68741...` holds the same bytes
  the recovered Pods already ran; the rollout changed only the image
  reference, from the mutable tag to the pinned tag.
- A fresh-cluster start.
- Strategy experiments under the pinned build. None were run.
- Release readiness. That awaits final adjudication.

## Final integration attempt, run `5750593984c049928da7b48ad26280aa`: STOPPED at `day7-image-load` (2026-09-29)
The run reused the existing cluster, so it was not a fresh-cluster
start. Logs are in `day7-logs/5750593984c049928da7b48ad26280aa/`.

**Before the run:**
- Focused reviews: all APPROVE WITH NON-BLOCKING FINDINGS (see
  `day-07-independent-reviews.md`).
- `make ci-check`: PASS, 1658 tests, 0 blocked cluster calls (log
  `claude-work/ci-check-20260929-153745.log`).
- Tree frozen: 85 files, freeze-manifest hash `000c7332...`, HEAD
  `74832c4`, index empty.
- Preflight 13/13 (log `00a`). Full `day7-resume-check` PASS, running
  images 47/47 (log `00b`).
- 85 prior evidence files were hashed.

**The run (log `day7-check-final.log`, 15:39-15:42 +06:00, exit 2).**
Passed:
- the static gates: test, version 64/64, manifest 267/267, helm-check
  2346/2346;
- preflight 13/13;
- `image-build`, then `day7-image-verify-local` 9/9;
- `day7-build-record`, which reproduced the same config digests and
  reused build `fdb68741...`;
- `cluster-create` (skipped: the cluster exists);
- the idempotent `gateway-api-install`, `cni-install` and `mesh-install`
  Helm upgrades;
- nodes-ready, cni-status, context and mesh-status;
- `image-load` of the mutable tags.

**It then failed at `day7-image-load`:** "refusing to load images
outside the Day 7 profile (profile 'day6')".

**Cause - a defect introduced by the post-review fix:**
- `load_into_kind` now refuses outside the Day 7 profile, as the
  security review asked.
- But the `day7-image-load` recipe ran the script without
  `env $(DAY7_ENV)`.
- The Makefile unit test pinned the recipe string, so it pinned the
  missing profile too. The static gates could not catch it.

**Effect.** No Day 7 application change was made:
- Helm revision 29 unchanged, the same 7 Pods, no run directory or
  baselines for this run ID, no stage submitted, so no restoration was
  needed.
- The cluster-level re-applies (Cilium and the four Istio charts) did
  not restart their Pods, but they **did** advance those releases'
  Helm revisions by one. This is inferred, not captured, as 2 -> 3:
  the only earlier applies were `dd99...` (the install) and `ce55...`,
  and the final run's pre-run snapshot showed 3 (later 4). (Correction
  added 2026-09-30 after the release review.)
- A read-only `day7-resume-check` afterwards passed fully, including
  running images 47/47 (log `01`).
- The tree still equalled the freeze, and all 85 prior evidence files
  were unchanged.

**Separate observation:** the Cilium agents show a second restart at
about 11:57 +06:00. That was before any live operation of the day's
later session, and the cause is not established.

**Fix (static):**
- `day7-image-load` is now `$(DAY7_LOCK) env $(DAY7_ENV) python3
  scripts/day7_build.py load-kind`.
- The recipe test now requires `$(DAY7_ENV)`, and the target is in the
  list of targets that must run under the Day 7 environment.
- `make ci-check`: PASS, 0 blocked calls (log
  `claude-work/ci-check-20260929-154340.log`).
- A dry run (`make -n`) shows the profile being passed.

**Status:** not rerun. A rerun needs a new run ID, and is the owner's
decision.

## Final integration run `985c466a246e4fb681fa427d4c6fe64f`: PASS (2026-09-29, 15:58:13-16:20:39 +06:00, make exit 0)
This was one uninterrupted `make day7-check` on the **existing**
`maops-k8s-day7` cluster. It **is not a fresh-cluster startup proof.**
Logs are in `day7-logs/985c466a246e4fb681fa427d4c6fe64f/`; the main log
is `day7-check-final.log`, 8695 lines. Make's own exit status was
written directly to `day7-check-final.exit` (value `0`), not taken from
a wrapper.

**Pre-run checks:**
- **Git:** branch `feature/day-7-deployment-strategies`, HEAD `74832c4`,
  index empty; 0 diff lines in the frozen Day 1-6 paths and records;
  `git diff --check` clean.
- **Release tag:** `v0.6.0` is the annotated tag `186855d7...`, pointing
  at `19d6b28...`, as recorded.
- **Recipe guard:** `day7-image-load` passes `$(DAY7_ENV)`, and
  `load_into_kind` independently refuses under the `day6` profile
  (proven end to end under the shim with 0 blocked calls).
  `day7-check` calls the target as step 21.
- **Static gate:** `make ci-check` through the blocking shim, real exit
  0, 1658 tests OK, 0 blocked calls (log
  `claude-work/ci-check-20260929-155533.log`).
- **Frozen tree:** 85 files, manifest hash `01d706d7...`.
- **Prior evidence:** 89 files hashed, including the failed
  `5750593984...` attempt.
- **Host and cluster:** preflight 13/13 with MemAvailable 4.4 GiB
  (log `00a`); full resume check PASS, running images 47/47 (log
  `00b`); Helm revision 29 and the same 7 Pods.
- **Snapshots:** Day 1-6 container states (`00c`) and infrastructure
  state (`00d`) were saved.

**Gates, all PASS:**
- **Static:** unit suite 1658, version 64/64, manifest 267/267,
  helm-check 2346/2346.
- **Host and images:** preflight 13/13; `image-build`, then
  `day7-image-verify-local` 9/9.
- **Build:** `day7-build-record` reproduced the same digests, so build
  `fdb6874137fbfc5c...` was reused: gateway `62df7c01...`, app
  `b035e264...`, state `aafa33a4...`.
- **Cluster:** `cluster-create` skipped (the cluster exists);
  gateway-api, cni and mesh were re-applied; nodes-ready 3/3,
  cni-status 4/4, context 6/6, mesh-status 4/4.
- **Image load:** `image-load` (mutable tags) and `day7-image-load`
  (pinned tags), then `day7-image-verify-nodes` 31/31.
- **Platform:** storage bootstrap verified; storage hardening 2/2;
  namespaces applied; Secrets preserved; Gateway applied.
- **Deploy:** `day7-deploy` produced **revision 30**. The build and the
  templates were unchanged, so no Pod rolled.
- **Workloads:** ambient 67/67, rollout 35/35, **running images 47/47**,
  gateway 8/8, mesh 45/45, NetworkPolicy 37/37.
- **Baselines:** `day7-baseline-init` (new private run directory, 0700),
  `state-check` 24/24 (suite baseline), `day7-baseline` (strategy
  baseline at revision 30, **build `fdb68741...`**, pinned tags in its
  values).

| Strategy | Checks | PRIMARY | RESTORATION | Helm revisions | Independent `day7-stable-check` (with running images) |
|---|---|---|---|---|---|
| Blue/Green | 143/143 | PASS | PASS | green-prepared 30->31, cutover 31->32, restore 32->33 | 55/55 |
| Canary | 212/212 | PASS | PASS | green-prepared 33->34, canary-90-10 34->35, Phase A restore 35->36, candidate-unready 36->37, restore 37->38 | 55/55 |
| Recreate (candidate only) | 182/182 | PASS | PASS | recreate-prepared 38->39, recreate-serving 39->40, recreate-changed 40->41, restore 41->42 | 55/55 |

**Candidate image identity before each route-changing stage**, from
the promotion gate: every candidate Pod's spec image was
`maops-kubernetes-gateway:0.7.0-cfg-62df7c016a06...`. Its imageID
`import-2026-09-28@sha256:75931fef...` mapped through the node record
to config `sha256:62df7c016a06...`, the build's gateway digest.
- Blue/Green cutover: Pods `...-7dj9q` and `...-bp45l`.
- Canary 90/10: Pods `...-fbtxj` and `...-l2fgq`.
- Recreate serving: Pods `...-hxzfp` and `...-kvp69`.
- The Recreate replacement Pods (`...-5648966c8c-nbkfp`, `...-v878n`)
  were verified the same way after `recreate-changed`.

**Strategy observations:**
- **Canary:** 200/200 requests, 182 stable and 18 candidate, 0 errors,
  0 unidentified. This is a finite observation, not an exact 90/10.
  External traffic stayed 30/30 stable while the candidate was unready.
- **Recreate:**
  - No old/new Pod overlap in 60 samples. The last old Pod was seen at
    t=32.7 s; the first new Pod appeared at t=33.7 s and was Ready at
    t=43.4 s.
  - There were 159 failed external requests between t=2.9 s and
    t=43.3 s: the planned outage.

**Final gate:** CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67,
rollout 35/35, running images 47/47, gateway 8/8, mesh 45/45,
NetworkPolicy 37/37, final-state 38/38. **Day 7 final checks 86/86**:
Helm values equal the stable stage plus build `fdb68741...`'s tags, and
the deployed manifest's sha256 equals the baseline's (`3cde1882...`).

**After the run** (log `02`, read-only):
- **Freeze:** the tree equals the pre-run freeze `01d706d7...`, and all
  89 prior evidence files are unchanged.
- **New baselines:** written once (suite 16:07:58, strategy 16:08:07),
  mode 0600 in a 0700 directory. Hashes: strategy `4f66547e...`, suite
  `b1d6b0da...`.
- **Release:** revision 42, values equal to the stable stage plus the
  recorded pinned tags.
- **Leftovers:** 0 candidate-labelled objects; 0 validation-client Pods
  anywhere; 0 Pods in `maops-day7-validation`.
- **Stable Pods:** all 7 are the same Pods as before the run. They are
  Ready with 0 restarts, their specs use the pinned tags, and they run
  build `fdb68741...`.
- **External traffic:** `Host: maops.local` returns 200.
- **Storage:** PVC `91b45f89...` is Bound to PV `f830a3e9...`, and the
  claimRef matches.

**Observed infrastructure changes:**
- The Helm releases `cilium`, `istio-base`, `istiod`, `istio-cni` and
  `ztunnel` each went from revision 3 to 4. These were identical-value
  re-applies by the `cni-install` and `mesh-install` steps.
- No `kube-system` or `istio-system` Pod was replaced or restarted:
  names, UIDs and restart counts are identical before and after.
- The Gateway API CRDs were re-applied with `kubectl apply`.

**Day 1-6:** all 14 node containers are identical before and after the
run (ID, status, start and finish times, restarts), all `exited`. The
Day 7 gates never list or require them.

**Limits:**
- The run reused an existing, long-lived cluster. It proves no fresh
  cluster start.
- The build was identical to the one already running, so this run
  proves that one pinned build is carried consistently through every
  stage. It does not prove a rollout onto new bytes. The pinned-tag
  rollout itself was proven in run `c252aa3d...` (revision 28 -> 29,
  with a reference change and the same bytes).
- The 0.7.0 images are rebuilds of the unchanged v0.6.0 sources.
- The candidate is a configuration variant sharing the stable
  ServiceAccount.
- Canary weights are observed, not exact.
- The Recreate outage is real and planned.
- Mesh path denials are not attributed to a layer, except the
  validation-client probe's NetworkPolicy drop.

## Fresh-cluster run `6b0029cc63724291a00bba6ed52ea7a9`: PASS, and a valid fresh-cluster proof (2026-09-30)
This executes the approved plan (`day-07-fresh-cluster-plan.md`),
scoped strictly to `maops-k8s-day7`. Logs are in
`day7-logs/fresh-cluster-exec-2026-09-30/` (gates C0 and C1) and
`day7-logs/6b0029cc63724291a00bba6ed52ea7a9/` (the run).

**C0 pre-deletion gates (10:08-10:12 +06:00): all PASS.**
- **C0.1 git:** branch and HEAD `74832c4`, index empty, frozen paths 0
  lines, `v0.6.0` `186855d7` -> `19d6b28`, `git diff --check` clean.
- **C0.2 static gate:** `make ci-check` through the blocking shim, real
  exit 0, 0 blocked calls, 1658 tests.
- **C0.3 freeze:** tree 87 files, hash `271f74c4...`; evidence 105
  files, hash `888d345d...`.
- **C0.4 Day 1-6:** 14 containers, all exited.
- **C0.5 identity:** exactly the three expected nodes, with the
  inventory's container IDs (`056f9dc22b7e`, `0eb8200cbd9b`,
  `c250905784db`); one kubeconfig context; no lock; no
  kind/helm/kubectl/day7 process.
- **C0.6 fresh export:** namespace, PVC and PV UIDs and the state file
  hash match the reviewed inventory. Helm history is identical to the
  reviewed export.
- **C0.7 private backup:**
  `claude-work/day7-worktree-20260930-101117.tgz`, 0600, 87 entries,
  sha256 `ad729006...`.
- **C0.8 reachability:** GitHub and both chart repositories returned
  200; three registries returned 401 (reachable).
- **C0.9 host:** one 18081 listener; `~/.kube/config` hash recorded;
  `kind` network `eeafa132...` on 172.20.0.0/16; the 3 old volumes
  present.

**C1 deletion (approved; 10:12:27-10:12:38 +06:00, make exit 0):**
- `python3 scripts/day7_lock.py run -- kind delete cluster --name
  maops-k8s-day7 --kubeconfig ~/.kube/maops-k8s-day7.config`.
- Output: "Deleted nodes: maops-k8s-day7-control-plane, -worker,
  -worker2".

Verified afterwards:
- `kind get clusters` lists day1-day6 only; no Day 7 containers.
- The 3 anonymous volumes are gone, so the old PVC/PV data (`state.json`
  `3ce4f556...`) is destroyed, as planned.
- Port 18081 is free.
- The Day 7 kubeconfig was emptied (28 bytes, no contexts, 0600).
- `~/.kube/config` and the `kind` network are unchanged.
- **Day 1-6:** all 14 containers identical.
- **Evidence:** all 105 files unchanged.

**C2 fresh run (10:13:04 preflight; `make day7-check` 10:13-10:41:55
+06:00, make's real exit 0, recorded in `day7-check-fresh.exit`):**
- Preflight 13/13 with MemAvailable 5.9 GiB, and 127.0.0.1:18081 free.
- The repository was not edited during the run; the tree equals the C0
  freeze afterwards.

**Freshness criteria (plan section C2), all met:**
1. **`kind create` ran:** log line 6281 reads `Creating cluster
   "maops-k8s-day7" ...`, and there is no "already exists" output.
2. **New node containers:**
   - `maops-k8s-day7-control-plane` `8b0b73a6d0ca`;
   - `maops-k8s-day7-worker` `77e6d9bfd28f`;
   - `maops-k8s-day7-worker2` `95c69412fe8c`.

   All were created 04:15:06-04:15:08Z, after the deletion ended
   (04:12:38Z), and all IDs differ from the old ones.
3. **The app release started at revision 1:** `day7-deploy` printed
   `REVISION: 1`, and the strategy baseline records `helm.revision` 1.
   Helm keeps the last 10 revisions (4-13) itself. The platform
   releases (Cilium, istio-base, istiod, istio-cni, ztunnel) are all at
   revision 1.
4. **New identities:**

   | Identity | Old | New |
   |---|---|---|
   | Namespace `maops-platform` | `ad1923da...` | `690b1f43-8c31-41be-9d59-15b7567831c4` |
   | PVC `data-maops-state-0` | `91b45f89...` | `2be6628b-7df3-421f-9f98-77f03d9a611d` |
   | PV `pvc-2be6628b-...` | `f830a3e9...` | `a9c74497-b133-40a1-aa8d-869b5cad4eab` (on `worker`) |

5. **Baseline:** the strategy baseline records these new identities and
   build `fdb68741...`.

**Gates, all PASS:**
- **Static:** unit 1658, version 64/64, manifest 267/267, helm-check
  2346/2346; preflight 13/13.
- **Images and build:** `image-build`, then
  `day7-image-verify-local` 9/9; `day7-build-record` reproduced build
  `fdb68741...` (gateway `62df7c01...`, app `b035e264...`, state
  `aafa33a4...`).
- **Cluster create:** `cluster-create` created the cluster; the
  gateway-api, cni and mesh charts were installed (revision 1).
- **Cold start:** `day7-nodes-ready` recorded a genuine cold start,
  attempt 1 at 0/3 Ready and attempt 2 at 3/3. It is exercised on
  creation here for the first time.
- **Platform:** cni-status 4/4, context 6/6, mesh-status 4/4.
- **Image load:** `image-load` and `day7-image-load` onto the fresh
  nodes, then `day7-image-verify-nodes` 31/31.
- **Storage and Secrets:** storage bootstrap was applied and its
  propagation verified; storage hardening 2/2. Secret bootstrap
  **created** both Secrets (values never shown).
- **Deploy:** `day7-deploy` at revision 1.
- **Workloads:** ambient 67/67, rollout 35/35, running images 47/47,
  gateway 8/8, mesh 45/45, NetworkPolicy 37/37.
- **Baselines:** new run directory at 0700; `state-check` 24/24;
  strategy baseline at revision 1. Both files are 0600 and written
  once, suite at 10:27:56 and strategy at 10:28:07.

| Strategy | Checks | PRIMARY | RESTORATION | Helm revisions | Independent stable check (with running images) |
|---|---|---|---|---|---|
| Blue/Green | 143/143 | PASS | PASS | 1->2 (green-prepared), 2->3 (cutover), 3->4 (restore) | 55/55 |
| Canary | 212/212 | PASS | PASS | 4->5, 5->6 (90/10), 6->7 (Phase A restore), 7->8 (unready), 8->9 (restore) | 55/55 |
| Recreate (candidate only) | 182/182 | PASS | PASS | 9->10, 10->11 (serving), 11->12 (changed), 12->13 (restore) | 55/55 |

**Candidate image identity before each route-changing stage:** every
candidate Pod's spec image was
`maops-kubernetes-gateway:0.7.0-cfg-62df7c016a06...`, and its imageID
`import-2026-09-30@sha256:75931fef...` mapped through the node record to
build config `62df7c016a06...` (6/6 checks). The Pods were:
- Blue/Green: `...-5pbbx` and `...-drxsw`;
- Canary: `...-d5ps4` and `...-q2xvm`;
- Recreate serving: `...-bs94g` and `...-fqn5j`.

The Recreate replacement Pods `...-5648966c8c-78qtv` and `-b2lq2` were
verified the same way.

**Strategy observations:**
- **Canary:** 200 requests, 179 stable and 21 candidate, 0 errors.
  External traffic stayed 30/30 stable while the candidate was unready.
- **Recreate:** no overlap in 59 samples. The last old Pod was seen at
  t=33.1 s; the first new Pod appeared at t=34.2 s and was Ready at
  t=43.9 s. There were 162 failed requests between t=3.1 s and
  t=44.1 s, the planned outage.

**Final gate:** CNI 4/4, context 6/6, mesh-status 4/4, listeners 67/67,
rollout 35/35, running images 47/47, gateway 8/8, mesh 45/45,
NetworkPolicy 37/37, final-state 38/38. **Day 7 final checks 86/86**,
and the deployed manifest's sha256 equals the baseline's.

**End state** (log `01`, read-only):
- **Release:** revision 13, values equal the stable stage plus build
  `fdb68741...`'s pinned tags.
- **Leftovers:** 0 candidate objects, 0 validation-client Pods, 0 Pods
  in `maops-day7-validation`.
- **Stable Pods:** 7 Ready, 0 restarts, with spec images on the pinned
  tags.
- **Traffic:** `Host: maops.local` returns 200.
- **Storage:** the new PVC `2be6628b...` is Bound to PV `a9c74497...`,
  identical to the new baseline, and the final gate confirmed it.

**Integrity:**
- **Tree:** equals the C0 freeze `271f74c4...`.
- **Evidence:** all 105 C0-frozen files are unchanged.
- **New baseline hashes:** strategy `aca1b824...`, suite
  `b27eec00...`.
- **Index:** empty.
- **Day 1-6:** 14 containers identical to the C0 snapshot (ID, status,
  times, restarts). `kind get clusters` lists day1-day7.

**Remaining limits** (plan section E; this run does not remove them):
- **Environment:** a local kind reference platform on one WSL2 and
  Docker Desktop host. It is not production-ready: no HA, no load
  balancer, no TLS, no autoscaling (Day 8).
- **Build:** a fresh **install** of the pinned build `fdb68741...`
  (0.7.0 rebuilt from the unchanged v0.6.0 sources). A live upgrade
  onto different bytes is still proven statically only.
- **Candidate:** a configuration variant sharing the stable
  ServiceAccount.
- **Canary:** the split is observed, not exact.
- **Recreate:** a real outage.
- **Path denials:** not layer-attributed, except the validation-client
  probe's Cilium drop.
- **Host restarts:** the post-reboot istio-cni enrollment race is
  detected but not auto-remediated, and was not exercised here.
- **One run:** one successful fresh creation, not a reliability
  statistic.
- **Isolation:** no Day 1-6 coexistence proof (they were stopped) and
  no data or Secret continuity (all new).
- **Execution:** implementer-run.
- **Lost history:** the old cluster's in-cluster history, Secrets and
  state bytes are irreversibly gone; only the exports, hashes and
  baselines remain.
- **Network:** creation depended on GitHub, the chart repositories and
  the registries.

## Release-candidate status (2026-09-30)
The owner adjudicated **GATE PASSED for the local Kind reference
platform**. That decision rests on the fresh-cluster run
`6b0029cc63724291a00bba6ed52ea7a9` above; see
`day-07-final-adjudication.md`. Day 7 `v0.7.0` is a release candidate.

Still pending, and not performed by anything recorded here: the PR,
merge, merged-`main` validation, and `v0.7.0` publication. The
cluster-free release-candidate checks for the documentation update ran
through the blocking shim. No live run was repeated for the
documentation work.

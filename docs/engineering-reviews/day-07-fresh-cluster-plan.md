# Day 7 - fresh-cluster validation plan (PROPOSAL, awaiting the owner's approval - NOT executed)

> **EXECUTED 2026-09-30 after the owner's approval.** Every C0 gate
> passed. The C1 deletion affected only `maops-k8s-day7`, and Day 1-6
> were verified identical. The C2 fresh run
> `6b0029cc63724291a00bba6ed52ea7a9` **passed** and met all four
> freshness criteria. Results are in
> `day-07-live-validation-record.md`, section "Fresh-cluster run
> `6b0029cc...`". The text below is the plan as reviewed and approved,
> unchanged.

**Status:** Day 7 is **GATE PENDING**. This document proposes deleting
and recreating **only** `maops-k8s-day7`, then running one uninterrupted
`make day7-check` from the newly created cluster. Nothing here has been
executed. No cluster, Helm release, image or file in the cluster was
changed while preparing it; all the inventory and exports were
read-only.

Log `01`'s node-container section contains three docker Go-template
errors; that data was re-read cleanly in log `02`.

Prepared 2026-09-30 on branch `feature/day-7-deployment-strategies`,
HEAD `74832c4`, with the whole Day 7 diff unstaged. Inventory logs are
in `~/.local/state/maops-kubernetes-platform/day7-logs/fresh-cluster-plan-2026-09-30/`:
- `01` inventory;
- `02` node containers;
- `03` evidence outside the cluster;
- `04` export of in-cluster-only evidence;
- `evidence-manifest.sha256`.

---

## A. Inventory: what `kind delete cluster --name maops-k8s-day7` would remove

**Docker objects (removed):**
- **Node containers:** three, all running `kindest/node:v1.36.1@sha256:3489c767...`:
  - `maops-k8s-day7-control-plane` (`056f9dc22b7e`), ports
    `127.0.0.1:18081->30080` and `127.0.0.1:39591->6443`;
  - `maops-k8s-day7-worker` (`0eb8200cbd9b`);
  - `maops-k8s-day7-worker2` (`c250905784db`).
- **Anonymous volumes:** each node's `/var` is an anonymous Docker
  volume, and kind removes these with the nodes:
  - `fd1ed1b6...`
  - `5a7afc6e...`
  - `b3422331...`

**Kubeconfig:** the `kind-maops-k8s-day7` entries in
`~/.kube/maops-k8s-day7.config`. It is the only context in that file.

**Not removed:**
- the `kind` Docker network. `kind delete` never removes it. Right now
  only the three Day 7 containers are attached, because stopped Day 1-6
  containers are not listed as attached;
- the Day 1-6 containers and their volumes;
- local Docker images.

**Everything inside the cluster:** 10 namespaces and 35 CRDs.

**Helm releases:**

| Release | Namespace | Revision |
|---|---|---|
| `maops-kubernetes-platform-day7` | `maops-platform` | 42 |
| `cilium` | `kube-system` | 4 |
| `istio-base` | `istio-system` | 4 |
| `istiod` | `istio-system` | 4 |
| `istio-cni` | `istio-system` | 4 |
| `ztunnel` | `istio-system` | 4 |

- Helm keeps only the last 10 revisions, so the app release's
  revisions 1-32 were already pruned by Helm itself.
- **Secrets:** `maops-internal-auth` and `maops-state-auth` (values never
  exported, by design).
- **All Pods, ConfigMaps, Services, PDBs, NetworkPolicies, Istio policy,
  and the Gateway and HTTPRoute.**

**State storage - it does NOT survive deletion:**
- PVC `data-maops-state-0` (uid `91b45f89...`) is bound to PV
  `pvc-91b45f89...` (uid `f830a3e9...`). The PV is `local-path` with
  reclaim policy `Delete`, pinned to node `maops-k8s-day7-worker2`.
- Its data is `/var/local-path-provisioner/pvc-91b45f89..._maops-platform_data-maops-state-0/state.json`
  (15 bytes, sha256 `3ce4f556...`), stored in `worker2`'s anonymous
  `/var` volume. Deleting the cluster deletes that volume, so **the
  PVC, the PV and the state data are destroyed**.
- The new cluster will provision a new PVC and PV with new UIDs.

**Evidence that exists only in the cluster:**
- the Secret values;
- the retained Helm revisions 33-42 (the Helm manifests and values
  themselves);
- Kubernetes events;
- live container logs;
- the state PV bytes.

What was exported, read-only, and without Secret values (log `04`):
- Helm history for all releases;
- the current app values;
- all events and a Pod listing;
- the workload, Service, PDB and route UIDs;
- the state file's hash. The state value itself is already recorded in
  all four runs' suite baselines.

Secret values are deliberately not preserved. A fresh cluster
generates new ones.

**Preserved outside the cluster (verified; 103 files in the manifest,
sha256 `d8ff331c...`):**
- **Private state root:** `~/.local/state/maops-kubernetes-platform/`
  (0700).
  - Run directories, with baselines at 0600:
    - `dd99769f...`
    - `ce55f5fb...` (plus the Cilium drop capture)
    - `c252aa3d...`
    - `985c466a...`
  - Log directories:
    - all five runs, including the failed `5750593984...` attempt;
    - `post-reboot-2026-09-29`;
    - this plan's directory.
  - Build record `day7-builds/fdb68741...` and `current.json`.
- **Checksums:** the 89 files present before the final run and the
  final run's baselines are unchanged.
- **Documents:** the Day 7 review and record files are **untracked
  working-tree files**. Cluster deletion does not affect them, but
  they are not in git yet.

## B. Static readiness: does anything assume an existing release, PVC, Secret, baseline or image?
No static fix is needed.
- **`cluster-create`:** creates the cluster when it is absent.
- **`day7-nodes-ready`:** built for exactly this cold-start case, a
  bounded 180 s wait. On an existing cluster it has only ever seen
  Ready nodes; this run would be its first cold-start exercise.
- **`secret-bootstrap`:** creates both Secrets when they are absent,
  and never rotates existing ones.
- **`storage-bootstrap`:** runs before any application PVC exists.
- **`day7-deploy`:** is `helm upgrade --install`, so it installs.
- **`day7-build-record`:** reuses build `fdb68741...` if the rebuild
  reproduces its digests. Otherwise it records a new build and makes it
  current.
- **Image loads:** `image-load` and `day7-image-load` populate the
  fresh nodes; `day7-image-verify-nodes` checks them.
- **`state-check`:** captures fresh namespace, PVC and PV identities
  and the fresh `/state` value into the new run's suite baseline.
- **Baselines:** strategy and suite baselines live in the new run
  directory. `load_strategy_baseline` refuses any other run's.
- **Unit tests:** cluster-free and hermetic. `DAY7_BUILD_ROOT` points at
  a nonexistent path in tests.
- **Precedent:** the first staged run (`dd99769f...`) exercised
  create -> CNI -> mesh -> deploy on the then-new cluster, including the
  cold-start `cni-status` race that `day7-nodes-ready` now closes.

## C. Exact sequence (runs only after the owner approves)
Every command runs from the repository root with
`PATH=/usr/bin:$PATH`, because `tool-check` requires `/usr/bin/docker`.

In these commands:
- `$B=~/.local/state/maops-kubernetes-platform`
- `$K="kubectl --kubeconfig ~/.kube/maops-k8s-day7.config --context kind-maops-k8s-day7"`

### C0. Pre-deletion gates (read-only; stop on any failure)
1. **Git:** `git branch --show-current` is
   `feature/day-7-deployment-strategies`, `git rev-parse HEAD` is
   `74832c4...`, `git diff --cached --name-only` is empty, frozen Day
   1-6 paths show 0 diff lines, `v0.6.0` is `186855d7...` ->
   `19d6b28...`, and `git diff --check` is clean.
2. **Static gate:** `make ci-check` through the blocking shim. Require a
   real exit of 0 and 0 blocked calls.
3. **Freeze:** hash the complete changed and untracked tree
   (`freeze-fresh.sha256`), and hash all evidence
   (`find day7-runs day7-logs day7-builds -type f | sha256sum`).
4. **Day 1-6 snapshot:** `docker inspect` the 14 `maops-k8s-day[1-6]-*`
   containers (Id, status, StartedAt, FinishedAt, RestartCount); all
   must be `exited`. Record `kind get clusters`.
5. **Identity guards** - all must hold, or stop:
   - `kind get nodes --name maops-k8s-day7` returns exactly
     `maops-k8s-day7-control-plane`, `-worker` and `-worker2`;
   - their container IDs equal the inventory's `056f9dc22b7e`,
     `0eb8200cbd9b` and `c250905784db`;
   - `~/.kube/maops-k8s-day7.config` contains only
     `kind-maops-k8s-day7`;
   - no Day 7 lock is held;
   - no `kind`, `helm` or `kubectl` process is running.
6. **Fresh read-only export:** Helm history, events, and the state file
   hash, if anything changed since log `04`.
7. **Private backup of the working tree:** the untracked Day 7 files
   exist only in the working tree. Create
   `tar -C <repo> -czf $B/claude-work/day7-worktree-<ts>.tgz <every changed and untracked file>`
   with mode 0600, and record its sha256.
8. **Network reachability** (read-only). After deletion, the run
   downloads the Gateway API CRDs and the Cilium and Istio charts, and
   the nodes pull their images. An outage would leave no Day 7 cluster,
   so all of these must respond, or stop:
   - `curl -sfIL https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.0/standard-install.yaml`
   - `curl -sfI https://helm.cilium.io/index.yaml`
   - `curl -sfI https://blob.istio.io/istio-release/charts/index.yaml` (the Makefile's `ISTIO_HELM_REPO`)
   - `curl -sfI https://quay.io/v2/` and `curl -sfI https://gcr.io/v2/`,
     where a 401 counts as "reachable".
9. **Port and kubeconfig state:** `ss -ltnp` shows exactly one listener
   on `127.0.0.1:18081` (the Day 7 docker-proxy). Record the sha256 of
   `~/.kube/config` and the mode of `~/.kube/maops-k8s-day7.config`
   (0600).

### C1. Deletion (Day 7 only)
```
PATH=/usr/bin:$PATH make day7-cluster-delete
#   resolves to: python3 scripts/day7_lock.py run -- \
#     kind delete cluster --name maops-k8s-day7 --kubeconfig ~/.kube/maops-k8s-day7.config
```
Capture make's real exit status. Then verify, read-only:
- **Clusters:** `kind get clusters` lists exactly `maops-k8s-day1`
  through `maops-k8s-day6`.
- **Containers:** `docker ps -a --filter name=maops-k8s-day7` is empty.
- **Volumes:** `docker volume inspect fd1ed1b6... 5a7afc6e...
  b3422331...` fails for all three, as expected.
- **Port:** 127.0.0.1:18081 is free.
- **Day 1-6:** all 14 containers are byte-identical to the C0.4
  snapshot.
- **Evidence:** the evidence manifest is unchanged.
- **Kubeconfig:** `~/.kube/maops-k8s-day7.config` has no stale
  `kind-maops-k8s-day7` context, cluster or user entry (the file may
  remain, mode 0600). `~/.kube/config` is byte-identical to C0.9.
- **Network:** `docker network inspect kind` still exists with the same
  ID `eeafa132...` and the same IPAM subnet.

### C2. Fresh one-shot run
```
R=$(python3 -c "import uuid;print(uuid.uuid4().hex)")   # new; must not exist under day7-runs/ or day7-logs/
mkdir -m 700 $B/day7-logs/$R
PATH=/usr/bin:$PATH make day7-preflight        > $B/day7-logs/$R/00a-day7-preflight.log 2>&1   # must be 13/13 and MemAvailable >= 4.0 GiB
echo "# header" > $B/day7-logs/$R/day7-check-fresh.log
PATH=/usr/bin:$PATH DAY7_RUN_ID=$R make day7-check >> $B/day7-logs/$R/day7-check-fresh.log 2>&1
echo $? > $B/day7-logs/$R/day7-check-fresh.exit          # make's own status, nothing in between
```
`day7-check` itself creates the cluster (`cluster-create` ->
`kind create cluster --name maops-k8s-day7 --config
kind/cluster-day7.yaml --kubeconfig ~/.kube/maops-k8s-day7.config`).
No separate create step runs. The repository is not edited while it
runs.

**Evidence to be reported from the run:**
- **Freshness:**
  - new node container IDs, created after C1;
  - `day7-nodes-ready` observations, from the cold start until 3/3
    Ready;
  - new namespace, PVC and PV UIDs, all different from the inventory;
  - the app release's first revision is 1, an install.
- **Images:** `day7-image-verify-local`; the build id from
  `day7-build-record`; `image-load` and `day7-image-load`;
  `day7-image-verify-nodes`, covering the pinned tags on every new
  node.
- **Platform and storage:** CNI, context and mesh status, storage
  bootstrap and hardening, Secret creation (a new-Secret message, never
  values), ambient listeners, rollout, `day7-running-images` for the
  pinned build, gateway, mesh and NetworkPolicy.
- **Baselines:** new baselines at 0600 in a 0700 directory, recording
  the build.
- **Strategies:** Blue/Green, Canary and Recreate, each with PRIMARY and
  RESTORATION reported separately, the Helm revisions, the candidate
  spec image and config digest before each route change, and an
  independent stable check with running images after each experiment.
- **Final gate:** the whole final gate, including final-state and the
  Day 7 final checks.

**Invalidation criteria.** The attempt is **void as a fresh-cluster
proof**, even if make exits 0, unless every one of these holds:
1. The `cluster-create` output shows a real `kind create cluster`, not
   "already exists, skipping create".
2. The node container IDs differ from `056f9dc22b7e`, `0eb8200cbd9b` and
   `c250905784db`, and every container was created after the C1
   deletion time.
3. The Day 7 app release was installed fresh: `day7-deploy` reports
   `REVISION: 1`.
4. The `maops-platform` namespace UID, the PVC UID and the PV UID all
   differ from the inventory's (`ad1923da...`, `91b45f89...`,
   `f830a3e9...`).
5. The run's strategy baseline records the new identities and the build
   that was used throughout.

### C3. Post-run verification (read-only)
- **Integrity:** the tree equals the C0 freeze; all prior evidence is
  unchanged; the new baselines were written once.
- **Release:** the final release equals the stable stage plus the
  recorded build's pinned tags.
- **Leftovers:** 0 candidate and 0 validation-client objects.
- **Stable Pods:** 7 stable Pods Ready, with listeners, on the build.
- **Traffic:** `Host: maops.local` returns 200.
- **Storage:** the new PVC and PV identity is unchanged from the new
  baseline through the final gate.
- **Infrastructure:** Helm revisions and Pod restarts are reported as
  observed changes.
- **Day 1-6:** all 14 containers identical before and after.
- **Records:** the live record is appended. The adjudication stays
  GATE PENDING until the run passes.

## D. Stop conditions and recovery (no step is skipped or retried into a pass)
Every failure is recorded as a failed attempt, with its log and make's
real exit status preserved. A later attempt is a **separate, new
attempt** with a new run ID and the owner's approval. It is never
merged with the failed one into an apparent one-shot pass.

| Failure point | Stop and preserve | Read-only diagnosis | Recovery (each needs owner approval) |
|---|---|---|---|
| C0 guards or `ci-check` | stop before deletion | inspect the specific guard | fix, re-review, re-run C0 |
| C1 deletion fails or is partial | stop; no manual `docker rm` or volume removal | `kind get nodes`, `docker ps -a`, `docker volume ls` | re-run `make day7-cluster-delete` only after diagnosis |
| Preflight (memory below 4.0 GiB, port busy, Docker unreachable) | stop; do not start the run | preflight log, `free -h`, `ss -ltn` | resolve the host condition without lowering thresholds; new preflight |
| `cluster-create` | stop; leave any partial cluster for diagnosis | `kind get nodes`, `docker logs <node>` | delete the partial Day 7 cluster, then a new attempt |
| `cni-install`, `day7-nodes-ready` or `cni-status` | stop | node conditions, Cilium pods and logs | new attempt; never re-run only the failed step and continue |
| `mesh-install`, `mesh-status` or `ambient-workload-check` (enrollment) | stop; **no Pod recreation** (that would void the one-shot claim) | istio-cni, ztunnel logs, listeners | a separately recorded remediation, then a new attempt |
| image verify (local, record, load, nodes) or `day7-running-images` | stop; no retagging or forced loads | `crictl images`/`inspect` on the nodes, local `docker save` digests | fix the cause, then a new attempt |
| storage bootstrap, hardening, `state-check` or baselines | stop; never recapture a baseline | PVC/PV, local-path logs | new attempt with a new run ID |
| an experiment | the coded restoration runs in `finally`, reported separately; then stop | `day7-stable-check` (read-only) | new attempt |
| final gate | stop; preserve | the failing sub-gate's log | new attempt |
| an interrupted run (Ctrl-C, timeout, WSL/Docker restart mid-run) | a failed attempt; preserve the logs and exit status; the coded restoration runs if an experiment was active | resume-check (read-only) once the host is back | new attempt |

**After a failure inside `day7-check`:** the new, partially validated
cluster is left **as it is** for read-only diagnosis. It is not
deleted, repaired or re-run until the owner decides.

**Disclosure:** every attempt, failed or successful, is recorded in
the live record. The README, roadmap and architecture status lines must
name the attempt that finally passed and state that earlier attempts
failed.

## E. Limits that remain even after a successful fresh-cluster run
- **Environment:** a local kind reference platform on one WSL2 and
  Docker Desktop host. It is **not production readiness**: no HA
  control plane, no real load balancer, no TLS, no autoscaling (Day 8).
- **Build:** the 0.7.0 images are rebuilds of the unchanged v0.6.0
  sources. If the rebuild reproduces build `fdb68741...`, the fresh
  run proves a fresh *install* of that pinned build. Neither run proves
  a live *upgrade onto different bytes*; that remains statically
  proven only.
- **Candidate:** a configuration variant sharing the stable
  ServiceAccount and Istio principal.
- **Canary:** traffic split is observed, not exact.
- **Recreate:** a real, planned outage.
- **Path denials:** not layer-attributed. The validation-client ->
  candidate denial is proven live at the Cilium NetworkPolicy layer
  only.
- **Host restarts:** the 2026-09-29 post-reboot istio-cni enrollment
  race (cause inferred) is detected by `day7-resume-check` but not
  automatically remediated. A fresh-cluster run does not exercise a
  host restart.
- **One fresh run:** proves one successful creation on this host. It
  is not a statistical claim about cold-start reliability.
- **Lost in-cluster history:** the old cluster's Helm history (beyond
  the export), events, and state PV data are gone after deletion,
  except for the exported copies and the baselines.
- **Host tooling:** `tool-check` needs `/usr/bin/docker` first on
  PATH. Docker Desktop auto-restarts all kind node containers after a
  reboot.
- **No coexistence proof:** the Day 1-6 clusters stay stopped during
  the run. It proves nothing about running alongside them, or about
  resource contention with them.
- **No continuity proof:** the Secrets are regenerated and the state
  data is new. The run proves a fresh install, not data or credential
  continuity across clusters.
- **Implementer-run:** the run is executed by the implementing session.
  Reviewers check its evidence; they do not re-execute it.
- **Irreversible deletion:** the old cluster's in-cluster history, its
  Secret values and its state PV bytes are permanently lost. Only the
  exported copies, the hashes and the baselines remain.
- **Network dependency:** creation depends on GitHub, the Helm and
  Istio chart repositories, and the image registries being reachable.
- **Shared recipe without its own guard:** the `cluster-delete` recipe
  has no leaf guard of its own. It is scoped by `CLUSTER_NAME` and the
  `DAY7_MAKE` wrapper, as the released Day 6 design is. A direct
  `make cluster-delete CLUSTER_NAME=<other>` is intentionally a
  different cluster's delete. The plan never invokes it that way.

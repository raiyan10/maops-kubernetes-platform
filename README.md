# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build.

**Latest RELEASED: `v0.3.0`** (Day 3 - scaling, scheduling, rolling
updates, rollback, availability, frozen - see
`docs/engineering-reviews/day-03-*`). `v0.1.0` (Day 1) and `v0.2.0`
(Day 2) are also released and frozen.

**Current DEVELOPMENT TARGET: `v0.4.0`** (Day 4 - StatefulSet, PVC,
persistence). Day 4 is **not released or tagged** - the work in this
README beyond the Day 1/2/3 sections describes the in-progress Day 4
build, left uncommitted for independent review.

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how Day 4's pieces
fit together.

## Day 4 topology

```
Host
  |
  | kubectl port-forward
  v
maops-gateway ClusterIP Service
  |
  v
maops-gateway Deployment (3 replicas, worker-only, spread across 2 workers)
  |
  | HTTP via Kubernetes DNS - BACKEND_HOST=maops-app
  v
maops-app ClusterIP Service
  |
  v
maops-app Deployment (3 replicas, worker-only, spread across 2 workers)
  |
  | HTTP via Kubernetes DNS - STATE_HOST=maops-state (2nd credential: maops-state-auth)
  v
maops-state ClusterIP Service      maops-state-headless (governing, clusterIP: None)
  |
  v
maops-state StatefulSet (1 replica, worker-only, PVC-backed /data)
```

Both Deployments keep Day 3's `PodDisruptionBudget` (`minAvailable: 2`)
unchanged; `maops-state` carries no PDB (meaningless for a single
replica) and no topology spread. Only `maops-gateway` is reached from
outside the cluster; `maops-app` and `maops-state` are reached only
through the chain above. All three workloads share the namespace
`maops-platform`, each has its own ConfigMap, and each mounts its own
runtime Secret read-only: gateway and app share `maops-internal-auth`
(unchanged since Day 2); app and state share a second, distinct
`maops-state-auth` that gateway never receives. See
[`docs/architecture.md`](docs/architecture.md) for the full picture,
including the storage bootstrap that hardens the PVC backend's
permissions before `maops-state`'s claim is ever created.

- **Persistence** - `GET`/`PUT /state` persists a small JSON record to
  a PVC-backed `/data` volume via a durable temp-file + fsync +
  atomic-rename + parent-dir-fsync write path. `make persistence-check`
  proves data survives a normal `maops-state-0` Pod deletion (same PVC/PV
  UID, new Pod UID, unchanged marker).
- **Retention across an outage** - `make retention-check` scales
  `maops-state` to 0 and back to 1, proving the PVC/PV stay `Bound`
  throughout (`persistentVolumeClaimRetentionPolicy: Retain/Retain`),
  app/gateway stay live (`/livez` 200) but degrade to `/readyz` 503
  during the outage, and full recovery (new Pod identity, same data)
  follows restoration.
- **Storage bootstrap** - `make storage-bootstrap` hardens new
  `local-path-provisioner` backing directories to `root:10001`, mode
  `2770` (never world-writable, even transiently) before any
  application PVC exists; `make storage-hardening-check` proves it
  against a disposable scratch claim with both a positive (UID/GID
  10001 write succeeds) and negative (unrelated UID/GID -> `EACCES`)
  case.
- **Image build fix** - all three images build with
  `--platform linux/amd64 --provenance=false --sbom=false --load` and
  load via plain `kind load docker-image` - a local kind/containerd
  compatibility fix for this environment (see
  [`docs/architecture.md`](docs/architecture.md) for the containerd
  multi-arch-image defect this resolves), not a production
  supply-chain policy.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 |
| kind | v0.32.0 |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, same digest as Days 1-3, do not float to
`latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day4` (kubeconfig context: `kind-maops-k8s-day4`)
- a separate, independently-created cluster from Day 1's
`maops-k8s-day1`, Day 2's `maops-k8s-day2`, and Day 3's
`maops-k8s-day3`, none of which Day 4 tooling ever touches. 1
control-plane + 2 worker nodes.

Kubeconfig path is explicit and overridable everywhere - never the
caller's default kubeconfig/context. Defaults to
`~/.kube/maops-k8s-day4.config`; override with
`make KUBECONFIG_PATH=/some/other/path ...`.

## Quick start

```bash
make tool-check              # verify docker/kubectl/kind/helm/python3 are present
make test                     # Docker-free unit tests for repository validation logic
make version-check            # cross-check VERSION against image tags/version labels
make manifest-check           # render k8s/base and statically validate it
make image-build                # build all three workload images (gateway, app, state)
make cluster-create           # create the pinned Day 4 kind cluster (1 control-plane + 2 workers, idempotent)
make context-check            # fail closed unless kubectl is verified against the isolated Day 4 cluster
make image-load                 # load all three images into every kind node (the storage probes below need this first)
make storage-bootstrap        # harden new local-path-provisioner directories (root:10001, mode 2770)
make storage-hardening-check  # prove the hardening against a disposable scratch PVC
make namespace-apply          # apply ONLY the Namespace (must precede secret-bootstrap)
make secret-bootstrap         # create/preserve BOTH runtime Secrets - never printed
make deploy                      # apply k8s/base (ConfigMaps, Deployments, StatefulSet, Services, PDBs)
make rollout-check             # real Deployment/Service/EndpointSlice/ConfigMap/security state (gateway/app)
make scheduling-check          # real worker-only scheduling + topology spread proof (gateway/app)
make discovery-check           # real Kubernetes DNS + gateway -> app Service HTTP proof
make secret-check                 # real Secret wiring, auth, and non-disclosure proof (both Secrets)
make smoke                         # port-forward service/maops-gateway + real HTTP checks incl. /state
make dependency-check           # gateway liveness vs. dependency-aware readiness proof
make scaling-check              # real scaling 3 -> 4 -> 3, gateway/app, guaranteed restoration
make rolling-update-check      # real rolling update (temp annotation) + real rollback, gateway/app
make pdb-check                    # real PodDisruptionBudget/Eviction-API behavior, gateway/app
make state-check                  # maops-state runtime identity, security, and storage binding
make persistence-check          # data survives maops-state-0 Pod deletion/rescheduling
make retention-check             # PVC/PV retention + degraded-but-live behavior across a 1 -> 0 -> 1 cycle
make final-state-check         # independently prove the cluster is fully restored after all experiments
```

Or run the full authoritative sequence in one shot (recipe-sequential,
so it stays correctly ordered even under `make -j`):

```bash
make day4-check
```

## Version consistency (closes DAY1-REL-I1)

Unchanged mechanism since Day 2: the Makefile derives
`VERSION := $(shell cat VERSION)` once, and `GATEWAY_IMAGE`/`APP_IMAGE`/
`STATE_IMAGE` all derive from that; `make version-check`
(`scripts/version_check.py`) independently reads `VERSION`, renders
`k8s/base`, and asserts VERSION itself matches the Day 4 target, all
three workloads' image tags match VERSION (Deployment or StatefulSet),
and every rendered `app.kubernetes.io/version` label matches VERSION.

`DAY1-INT-I2` (the hardcoded `/usr/bin/python3.11` interpreter path used
by exec-based checks) remains **ACCEPTED / OPEN** - Day 4 kept the same
digest-pinned Distroless base image for all three workloads.

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day4` already exists, this is a no-op (aside
from printing `kubectl get nodes`). A pre-existing Day 4 cluster is
never automatically deleted or recreated. Uses `kind/cluster.yaml`: 1
control-plane + 2 worker nodes, pinned to the same node image digest as
Days 1-3.

## Image build / load

```bash
make image-build   # docker build all three of gateway/, app/, state/ (see IMAGE_BUILD_FLAGS in the Makefile)
make image-load     # kind load docker-image for all three, into every node of maops-k8s-day4
```

All three workloads use `imagePullPolicy: IfNotPresent` since images
are loaded directly into the kind nodes - no registry involved.

## Storage bootstrap (must run after `make image-load`, before `make deploy`)

```bash
make storage-bootstrap          # harden local-path-provisioner's directory-creation permissions
make storage-hardening-check    # prove it, against a disposable scratch PVC
```

Both scratch probe Pods reuse the already-loaded `maops-kubernetes-app`
image (overriding its command to run a short probe script rather than
the app's HTTP server) instead of the raw multi-arch Distroless base
digest directly - referencing that raw digest hits the same
containerd multi-arch-image defect described above once any manual
`ctr images import` of it has ever occurred on a node. `make
image-load` must run first so this image is actually present on every
node.

See [`docs/architecture.md`](docs/architecture.md) for why this exists:
the provisioner's default `mkdir -m 0777` is world-writable and
root-owned, meaning `fsGroup` in a Pod spec does no real access-control
work against it. This bootstrap makes new backing directories
`root:10001`, mode `2770` instead - before `maops-state`'s own PVC is
ever created - and is scoped to this isolated Day 4 cluster only.

## Deploy lifecycle (order matters)

```bash
make cluster-create           # 1. create the kind cluster (1 control-plane + 2 workers)
make context-check            # 2. fail closed unless verified against the isolated Day 4 cluster
make image-load                 # 3. load all three images (storage probes below need the app image present)
make storage-bootstrap        # 4. harden new PV-backed directory permissions
make storage-hardening-check  # 5. prove the hardening, before any application PVC exists
make namespace-apply          # 6. apply ONLY the Namespace to the explicit Day 4 context
make secret-bootstrap         # 7. create/preserve BOTH runtime Secrets (needs the namespace to exist)
make deploy                       # 8. apply the full Kustomize base
```

`make deploy` applies `k8s/base/` in full: Namespace, 3 ConfigMaps, 2
Deployments (unchanged since Day 3) + 1 StatefulSet, 4 Services
(gateway/app/state/state-headless), 2 PodDisruptionBudgets
(gateway/app only). Both Secrets are deliberately **not** part of this -
see [`docs/architecture.md`](docs/architecture.md#day4-the-state-api-and-the-authenticated-gateway---app---state-chain).

## Verification

```bash
make rollout-check         # node/version/namespace/replicas/EndpointSlice/ConfigMap/
                             #   security/UID-GID/no-SA-token/Secret-mount (gateway/app)
make scheduling-check      # worker-only scheduling + topology spread (gateway/app)
make discovery-check       # real DNS resolution + gateway -> app Service HTTP
make secret-check             # Secret existence/mount/auth/non-disclosure, end to end, both Secrets
make smoke                     # bounded port-forward to service/maops-gateway + real HTTP incl. /state
make dependency-check       # app-outage experiment: gateway live=200, ready=503, backend=503,
                             #   no restart-count increase, then guaranteed restoration to 3/3
make scaling-check          # 3 -> 4 -> 3 scaling proof, gateway/app
make rolling-update-check  # real rolling update + real kubectl rollout undo rollback, gateway/app
make pdb-check                # PDB/Eviction-API behavior, gateway/app
make state-check              # maops-state 1/1 Ready, worker placement, PVC/PV binding, security baseline
make persistence-check      # maops-state-0 deletion/rescheduling: same PVC/PV, new Pod UID, data intact
make retention-check         # maops-state 1 -> 0 -> 1: PVC/PV retained, degraded-but-live, full recovery
make final-state-check     # everything restored to normal/healthy after all of the above
```

Each of these is a standalone script under `scripts/` - none require a
third-party Python package, and none leave a background process running
afterward (bounded `kubectl port-forward` via `scripts/portforward.py`).
Every live script fails closed via `kube.verify_context()` if it is not
actually talking to the verified `kind-maops-k8s-day4` cluster.

## Port-forward usage

Automated validation picks a free local port itself and cleans the
port-forward process up automatically. For manual, human use, always
target the gateway (the normal entry point):

```bash
kubectl --kubeconfig ~/.kube/maops-k8s-day4.config --context kind-maops-k8s-day4 \
  -n maops-platform port-forward service/maops-gateway 8080:8080
```

Then in another terminal:

```bash
curl http://localhost:8080/
curl http://localhost:8080/livez
curl http://localhost:8080/readyz
curl http://localhost:8080/config
curl http://localhost:8080/backend
curl http://localhost:8080/state
curl -X PUT http://localhost:8080/state -d '{"value": "hello"}'
```

Press `Ctrl-C` in the port-forward terminal to stop it when done.

## Cleanup

```bash
make cluster-delete   # kind delete cluster --name maops-k8s-day4 (ONLY this cluster)
```

This never runs `docker system prune`, never touches
`maops-k8s-day1`/`maops-k8s-day2`/`maops-k8s-day3`, or any other kind
cluster/Docker resource.

## Day 4 scope boundaries

Included: everything from Days 1-3 (unchanged), plus a third workload
(`maops-state`, a single-replica StatefulSet with a PVC-backed `/data`
volume), a governing headless Service plus a normal ClusterIP Service
for it, a second runtime Secret (`maops-state-auth`), the authenticated
`gateway -> app -> state` chain, a narrowly-scoped storage bootstrap for
the local-path-provisioner backend, and real persistence/retention
proofs with guaranteed restoration.

Explicitly **not** part of Day 4 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives): custom ServiceAccount, RBAC, NetworkPolicy
(Day 5), Helm, GitHub Actions CI, Ingress, Gateway API (Day 6), service
mesh, advanced deployment strategies beyond RollingUpdate
(Recreate/Blue-Green/Canary, Day 7), `HorizontalPodAutoscaler`, scaling
`maops-state` beyond 1 replica, Argo Rollouts, an observability stack,
Terraform, Ansible, Argo CD, cloud clusters, and container registry
publishing.

## Repository layout

```
app/                   maops-app: stdlib-only HTTP workload + Dockerfile (now proxies /internal/state)
gateway/                maops-gateway: stdlib-only HTTP workload + Dockerfile (now proxies /state)
state/                  maops-state: stdlib-only HTTP workload + Dockerfile (new, Day 4)
k8s/base/               Namespace, 3 ConfigMaps, 2 Deployments, 1 StatefulSet, 4 Services, 2 PDBs,
                         kustomization.yaml (runtime Secrets are deliberately NOT rendered here)
kind/cluster.yaml       pinned 1-control-plane + 2-worker kind cluster config (Day 4: maops-k8s-day4)
scripts/                dependency-free Python validation + cluster tooling, incl. storage_bootstrap.py,
                         storage_hardening_check.py, state_check.py, persistence_check.py, retention_check.py
tests/                  Docker-free unit tests (incl. negative cases)
docs/                   architecture.md, roadmap.md, engineering-reviews/ (Days 1-3, frozen)
.claude/                CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile                authoritative local engineering interface
VERSION                 0.4.0 (development target - not yet released/tagged)
```

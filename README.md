# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build.

**Latest RELEASED: `v0.2.0`** (Day 2 - multi-service architecture,
service discovery, configuration & Secrets, frozen - see
`docs/engineering-reviews/day-02-*`). `v0.1.0` (Day 1) is also released
and frozen.

**Current DEVELOPMENT TARGET: `v0.3.0`** (Day 3 - scaling, scheduling,
rolling updates, rollback, availability). Day 3 is **not released or
tagged** - the work in this README beyond the Day 1/Day 2 sections
describes the in-progress Day 3 build, left uncommitted for independent
review.

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how Day 3's pieces
fit together.

## Day 3 topology

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
```

Both Deployments run behind a `PodDisruptionBudget` (`minAvailable: 2`).
Only `maops-gateway` is reached from outside the cluster; `maops-app` is
reached exclusively through the gateway, over the Service. Both
workloads share the namespace `maops-platform`, each has its own
ConfigMap, and both mount the same runtime Secret (`maops-internal-auth`)
read-only - all unchanged since Day 2. See
[`docs/architecture.md`](docs/architecture.md) for the full picture.

- **Multi-node cluster** - `kind/cluster.yaml` now provisions 1
  control-plane + 2 worker nodes, the first topology in this project
  with real worker nodes to schedule onto.
- **Scaling** - both Deployments run 3 replicas (up from Day 1/2's 2).
  `make scaling-check` proves real, manual scaling 3 -> 4 -> 3 for both
  workloads with guaranteed restoration. `HorizontalPodAutoscaler` is
  explicitly out of scope.
- **Scheduling** - required node affinity excludes the control-plane
  node; per-workload `topologySpreadConstraints` (`maxSkew: 1`,
  `kubernetes.io/hostname`) spread replicas across both workers without
  the impossible-to-satisfy hard anti-affinity that 3 replicas over 2
  nodes would demand. `make scheduling-check` proves this live.
- **RollingUpdate tuning** - explicit `maxUnavailable: 1`,
  `maxSurge: 1`, `minReadySeconds: 5`, `progressDeadlineSeconds: 120`,
  `revisionHistoryLimit: 5` on both Deployments.
- **Real rolling update + rollback** - `make rolling-update-check`
  triggers a genuine new Deployment revision via a temporary,
  uniquely-marked Pod-template annotation (never a fake image tag),
  proves Pods are actually replaced, then performs a real
  `kubectl rollout undo` and proves full restoration - all with bounded
  service-availability sampling during the rollout window.
- **PodDisruptionBudget** - `minAvailable: 2` per workload.
  `make pdb-check` proves the normal healthy PDB status, that scaling is
  never blocked by the PDB, and that a real Eviction-API call against a
  healthy Pod is rejected once `disruptionsAllowed` reaches 0 - all with
  guaranteed restoration.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 |
| kind | v0.32.0 |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, same digest as Day 1/2, do not float to
`latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day3` (kubeconfig context: `kind-maops-k8s-day3`)
- a separate, independently-created cluster from Day 1's
`maops-k8s-day1` and Day 2's `maops-k8s-day2`, neither of which Day 3
tooling ever touches. 1 control-plane + 2 worker nodes.

## Quick start

```bash
make tool-check          # verify docker/kubectl/kind/helm/python3 are present
make test                 # Docker-free unit tests for repository validation logic
make version-check        # cross-check VERSION against image tags/version labels
make manifest-check       # render k8s/base and statically validate it
make image-build            # build both workload images (gateway, app)
make cluster-create       # create the pinned Day 3 kind cluster (1 control-plane + 2 workers, idempotent)
make context-check        # fail closed unless kubectl is verified against the isolated Day 3 cluster
make namespace-apply      # apply ONLY the Namespace (must precede secret-bootstrap)
make secret-bootstrap     # create/preserve the runtime Secret - never printed
make image-load             # load both images into every kind node
make deploy                  # apply k8s/base (ConfigMaps, Deployments, Services, PDBs) to the cluster
make rollout-check         # real Deployment/Service/EndpointSlice/ConfigMap/security state, both workloads
make scheduling-check      # real worker-only scheduling + topology spread proof
make discovery-check       # real Kubernetes DNS + gateway -> app Service HTTP proof
make secret-check            # real Secret wiring, auth, and non-disclosure proof
make smoke                    # port-forward service/maops-gateway + real HTTP checks
make dependency-check       # gateway liveness vs. dependency-aware readiness proof
make scaling-check          # real scaling 3 -> 4 -> 3, both workloads, guaranteed restoration
make rolling-update-check  # real rolling update (temp annotation) + real rollback, both workloads
make pdb-check                # real PodDisruptionBudget/Eviction-API behavior, both workloads
make final-state-check     # independently prove the cluster is fully restored after all experiments
```

Or run the full authoritative sequence in one shot:

```bash
make day3-check
```

## Version consistency (closes DAY1-REL-I1)

Unchanged mechanism since Day 2: the Makefile derives
`VERSION := $(shell cat VERSION)` once, and both `GATEWAY_IMAGE`/
`APP_IMAGE` derive from that; `make version-check`
(`scripts/version_check.py`) independently reads `VERSION`, renders
`k8s/base`, and asserts VERSION itself matches the Day 3 target, both
Deployment image tags match VERSION, and every rendered
`app.kubernetes.io/version` label matches VERSION.

`DAY1-INT-I2` (the hardcoded `/usr/bin/python3.11` interpreter path used
by exec-based checks) remains **ACCEPTED / OPEN** - Day 3 kept the same
digest-pinned Distroless base image for both workloads.

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day3` already exists, this is a no-op (aside
from printing `kubectl get nodes`). A pre-existing Day 3 cluster is
never automatically deleted or recreated. Uses `kind/cluster.yaml`: 1
control-plane + 2 worker nodes, pinned to the same node image digest as
Day 1/2.

## Image build / load

```bash
make image-build   # docker build both gateway/ and app/ images
make image-load     # kind load docker-image for both, into every node of maops-k8s-day3
```

Both Deployments use `imagePullPolicy: IfNotPresent` since images are
loaded directly into the kind nodes - no registry involved.

## Deploy lifecycle (order matters)

```bash
make cluster-create     # 1. create the kind cluster (1 control-plane + 2 workers)
make context-check      # 2. fail closed unless verified against the isolated Day 3 cluster
make namespace-apply    # 3. apply ONLY the Namespace to the explicit Day 3 context
make secret-bootstrap   # 4. create/preserve the runtime Secret (needs the namespace to exist)
make image-load          # 5. load both images
make deploy               # 6. apply the full Kustomize base
```

`make deploy` applies `k8s/base/` in full: Namespace, both ConfigMaps,
both Deployments (3 replicas each, RollingUpdate-tuned, worker-only
scheduling, topology-spread), both Services (ClusterIP), both
PodDisruptionBudgets. The Secret is deliberately **not** part of this -
see [`docs/architecture.md`](docs/architecture.md#secret-lifecycle).

## Verification

```bash
make rollout-check         # node/version/namespace/replicas/EndpointSlice/ConfigMap/
                            #   security/UID-GID/no-SA-token/Secret-mount, both workloads
make scheduling-check      # worker-only scheduling + topology spread, both workloads
make discovery-check       # real DNS resolution + gateway -> app Service HTTP
make secret-check             # Secret existence/mount/auth/non-disclosure, end to end
make smoke                     # bounded port-forward to service/maops-gateway + real HTTP
make dependency-check       # app-outage experiment: gateway live=200, ready=503, backend=503,
                             #   no restart-count increase, then guaranteed restoration to 3/3
make scaling-check          # 3 -> 4 -> 3 scaling proof, both workloads
make rolling-update-check  # real rolling update + real kubectl rollout undo rollback, both workloads
make pdb-check                # PDB/Eviction-API behavior, both workloads
make final-state-check     # everything restored to normal 3/3/healthy after all of the above
```

Each of these is a standalone script under `scripts/` - none require a
third-party Python package, and none leave a background process running
afterward (bounded `kubectl port-forward` via `scripts/portforward.py`).
Every live script fails closed via `kube.verify_context()` if it is not
actually talking to the verified `kind-maops-k8s-day3` cluster.

## Port-forward usage

Automated validation picks a free local port itself and cleans the
port-forward process up automatically. For manual, human use, always
target the gateway (the normal entry point):

```bash
kubectl --context kind-maops-k8s-day3 -n maops-platform port-forward service/maops-gateway 8080:8080
```

Then in another terminal:

```bash
curl http://localhost:8080/
curl http://localhost:8080/livez
curl http://localhost:8080/readyz
curl http://localhost:8080/config
curl http://localhost:8080/backend
```

Press `Ctrl-C` in the port-forward terminal to stop it when done.

## Cleanup

```bash
make cluster-delete   # kind delete cluster --name maops-k8s-day3 (ONLY this cluster)
```

This never runs `docker system prune`, never touches `maops-k8s-day1`,
`maops-k8s-day2`, or any other kind cluster/Docker resource.

## Day 3 scope boundaries

Included: everything from Day 1/2 (unchanged), plus a multi-node kind
cluster, 3 replicas per workload, explicit RollingUpdate tuning,
worker-only required node affinity, per-workload topologySpreadConstraints,
a PodDisruptionBudget per workload, real scaling (3 -> 4 -> 3), a real
rolling update via a temporary Pod-template annotation, a real
`kubectl rollout undo` rollback, and real Eviction-API/PDB behavior -
all proven live with guaranteed restoration.

Explicitly **not** part of Day 3 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives): `HorizontalPodAutoscaler`, StatefulSet, PVC,
custom ServiceAccount, RBAC, NetworkPolicy, Helm, Ingress, Gateway API,
service mesh, advanced deployment strategies beyond RollingUpdate
(Recreate/Blue-Green/Canary), GitHub Actions, an observability stack,
Terraform, Ansible, Argo CD (including Argo Rollouts), cloud clusters,
and container registry publishing.

## Repository layout

```
app/                  maops-app: stdlib-only HTTP workload + Dockerfile
gateway/               maops-gateway: stdlib-only HTTP workload + Dockerfile
k8s/base/              Namespace, 2 ConfigMaps, 2 Deployments, 2 Services, 2 PDBs, kustomization.yaml
                        (the runtime Secret is deliberately NOT rendered here)
kind/cluster.yaml      pinned 1-control-plane + 2-worker kind cluster config (Day 3: maops-k8s-day3)
scripts/               dependency-free Python validation + cluster tooling
tests/                 Docker-free unit tests (incl. negative cases)
docs/                  architecture.md, roadmap.md, engineering-reviews/ (Day 1/2, frozen)
.claude/               CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile               authoritative local engineering interface
VERSION                0.3.0 (development target - not yet released/tagged)
```

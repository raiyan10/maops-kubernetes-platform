# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build. **Day 1 (v0.1.0)** establishes
the Kubernetes foundation - a single-control-plane kind cluster running
a tiny, project-owned HTTP workload behind a ClusterIP Service, with a
full local validation pipeline (static manifest checks, real-cluster
checks, HTTP smoke tests, and a controller-reconciliation proof).

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how Day 1's pieces
fit together.

## Day 1 architecture

```
Docker
    |
    v
kind
    |
    v
Kubernetes v1.36.1
    |
    v
namespace: maops-platform
    |
    +--> ConfigMap
    |
    v
Deployment (2 replicas)
    |
    +--> Pod
    +--> Pod
    |
    v
ClusterIP Service
    |
    v
kubectl port-forward
    |
    v
localhost:<local-port>
```

No NodePort. No LoadBalancer. No Ingress. See
[`docs/architecture.md`](docs/architecture.md#why-port-forward-instead-of-nodeportingress-on-day-1)
for why.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 |
| kind | v0.32.0 |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, do not float to `latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day1` (kubeconfig context: `kind-maops-k8s-day1`).
Single control-plane node only - no workers on Day 1.

## Quick start

```bash
make tool-check       # verify docker/kubectl/kind/helm/python3 are present
make test              # Docker-free unit tests for repository validation logic
make manifest-check    # render k8s/base and statically validate it
make image-build        # build the workload image
make cluster-create    # create the pinned kind cluster (idempotent)
make image-load         # load the local image into kind
make deploy              # apply k8s/base to the cluster
make rollout-check      # wait for + verify real Deployment/Service/security state
make smoke               # port-forward + real HTTP checks
make controller-check   # prove Deployment controller reconciliation
```

Or run the full authoritative sequence in one shot:

```bash
make day1-check
```

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day1` already exists, this is a no-op (aside
from printing `kubectl get nodes`). Uses `kind/cluster.yaml`, which pins
the exact node image above and defines a single control-plane node only.

## Image build / load

```bash
make image-build   # docker build -t maops-kubernetes-platform:0.1.0 -f app/Dockerfile app/
make image-load     # kind load docker-image maops-kubernetes-platform:0.1.0 --name maops-k8s-day1
```

The Deployment uses `imagePullPolicy: IfNotPresent` because the image is
loaded directly into the kind node - there is no registry involved on
Day 1.

## Deployment

```bash
make deploy   # kubectl --context kind-maops-k8s-day1 apply -k k8s/base
```

Applies the Kustomize base at `k8s/base/`: Namespace, ConfigMap,
Deployment (2 replicas), Service (ClusterIP).

## Verification

```bash
make rollout-check     # real cluster checks: node/version/namespace/replicas/
                         #   endpoints/ConfigMap consumption/runtime UID-GID/securityContext
make smoke               # bounded port-forward + HTTP checks against /, /livez, /readyz, /config
make controller-check   # deletes exactly one pod, proves the Deployment controller
                         #   reconciles it, then re-verifies HTTP still works
```

Each of these is a standalone script under `scripts/` (see
`scripts/cluster_check.py`, `scripts/smoke.py`,
`scripts/reconcile_check.py`) - none require a third-party Python
package, and none leave a background process running afterward.

## Port-forward usage

Automated validation (`make smoke`, `make controller-check`) picks a
free local port itself and cleans the port-forward process up
automatically. For manual, human use:

```bash
kubectl -n maops-platform port-forward service/maops-app 8080:8080
```

Then in another terminal:

```bash
curl http://localhost:8080/
curl http://localhost:8080/livez
curl http://localhost:8080/readyz
curl http://localhost:8080/config
```

Press `Ctrl-C` in the port-forward terminal to stop it when done.

## Cleanup

```bash
make cluster-delete   # kind delete cluster --name maops-k8s-day1 (ONLY this cluster)
```

This never runs `docker system prune` or touches any other kind cluster
or Docker resource.

## Day 1 scope boundaries

Included: single-control-plane kind cluster, one Namespace, one
ConfigMap, one Deployment (2 replicas) with startup/liveness/readiness
probes and an exact resource/security baseline, one ClusterIP Service,
`kubectl port-forward` access, full static + real-cluster validation,
and a controller-reconciliation proof.

Explicitly **not** part of Day 1 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives): worker nodes, Secrets, ServiceAccount/RBAC,
NetworkPolicy, PersistentVolumeClaim/StatefulSet, Helm packaging, CI
automation, NodePort/LoadBalancer/Ingress, an observability stack,
Terraform, Ansible, Argo CD, and cloud infrastructure provisioning - none
of these are ever introduced into this project, or are deferred to a
named later day.

## Repository layout

```
app/                 tiny stdlib-only HTTP workload + Dockerfile
k8s/base/             Namespace, ConfigMap, Deployment, Service, kustomization.yaml
kind/cluster.yaml     pinned single-control-plane kind cluster config
scripts/              dependency-free Python validation + cluster tooling
tests/                Docker-free unit tests (incl. negative cases)
docs/                 architecture.md, roadmap.md
.claude/              CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile              authoritative local engineering interface
VERSION               0.1.0
```

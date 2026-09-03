# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build.

**Latest RELEASED: `v0.1.0`** (Day 1 - Kubernetes foundation, frozen -
see `docs/engineering-reviews/day-01-*`).

**Current DEVELOPMENT TARGET: `v0.2.0`** (Day 2 - multi-service
architecture, service discovery, configuration & Secrets). Day 2 is
**not released or tagged** - the work in this README beyond the Day 1
section describes the in-progress Day 2 build, left uncommitted for
independent review.

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how Day 2's pieces
fit together.

## Day 2 topology

```
Host
  |
  | kubectl port-forward
  v
maops-gateway ClusterIP Service
  |
  v
maops-gateway Deployment (2 replicas)
  |
  | HTTP via Kubernetes DNS - BACKEND_HOST=maops-app
  v
maops-app ClusterIP Service
  |
  v
maops-app Deployment (2 replicas)
```

Only `maops-gateway` is reached from outside the cluster; `maops-app` is
reached exclusively through the gateway, over the Service. Both
workloads share the namespace `maops-platform`, each has its own
ConfigMap (`maops-gateway-config`, `maops-app-config`), and both mount
the same runtime Secret (`maops-internal-auth`) read-only. See
[`docs/architecture.md`](docs/architecture.md) for the full picture,
including why NetworkPolicy/RBAC remain deferred to Day 5.

- **Gateway vs. app** - `maops-gateway` is a thin, dependency-aware
  front door; `maops-app` is the backend it proxies to. The gateway
  never receives its backend's identity as anything but a Service DNS
  name (`BACKEND_HOST=maops-app`) - never a Pod IP, Pod name, or
  hardcoded ClusterIP.
- **Service DNS / discovery** - the gateway resolves `maops-app` through
  Kubernetes' cluster DNS and reaches it through the Service, proven
  live via `make discovery-check` (`scripts/discovery_check.py`): a real
  `socket.getaddrinfo()` call from inside a running gateway Pod, plus a
  real HTTP round trip through the Service - never source inspection
  alone.
- **EndpointSlice** - Day 2 moved authoritative backend-readiness
  evidence from the legacy (now-deprecated-as-of-1.36) `v1 Endpoints`
  API to `discovery.k8s.io/v1 EndpointSlice`
  (`scripts/endpointslice.py`).
- **ConfigMaps** - `maops-gateway-config` carries `BACKEND_HOST`,
  `BACKEND_PORT`, `BACKEND_TIMEOUT_SECONDS`, plus display/environment
  keys; `maops-app-config` carries the app's own display/environment
  keys. Neither ever holds the Secret token.
- **Runtime Secret bootstrap** - `maops-internal-auth` is never
  committed. `make secret-bootstrap` (`scripts/secret_bootstrap.py`)
  creates it out-of-band against the explicit Day 2 context/namespace,
  generating a fresh cryptographically-strong token only if the Secret
  doesn't already exist; an existing Secret is preserved, never silently
  rotated. The token is never printed, logged, or passed on a process
  command line.
- **Secret file mount** - both workloads mount the same Secret
  read-only at `/var/run/secrets/maops` (volume `internal-auth`), token
  at `/var/run/secrets/maops/internal-token`, readable by UID/GID
  `10001:10001` via `fsGroup: 10001` + `defaultMode: 0440` (never
  world-readable).
- **Internal authenticated call** - `maops-app`'s `GET /internal/info`
  requires header `X-MAOPS-Internal-Token`, compared with
  `hmac.compare_digest()`; missing/wrong token -> `HTTP 403`, correct
  token -> `HTTP 200`. Never logged, never echoed back, never in
  `/config`.
- **Gateway readiness vs. liveness** - `GET /livez` is local-process-only
  (never depends on the backend); `GET /readyz` performs a bounded HTTP
  check against `http://maops-app:8080/readyz` and is what actually
  removes the gateway Pod from Service endpoints during a backend
  outage - proven live via `make dependency-check`.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 |
| kind | v0.32.0 |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, same digest as Day 1, do not float to
`latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day2` (kubeconfig context: `kind-maops-k8s-day2`)
- a separate, independently-created cluster from Day 1's
`maops-k8s-day1`, which Day 2 tooling never touches. Single
control-plane node only - no workers.

## Quick start

```bash
make tool-check       # verify docker/kubectl/kind/helm/python3 are present
make test              # Docker-free unit tests for repository validation logic
make version-check     # cross-check VERSION against image tags/version labels
make manifest-check    # render k8s/base and statically validate it
make image-build         # build both workload images (gateway, app)
make cluster-create    # create the pinned Day 2 kind cluster (idempotent)
make namespace-apply   # apply ONLY the Namespace (must precede secret-bootstrap)
make secret-bootstrap  # create/preserve the runtime Secret - never printed
make image-load         # load both images into kind
make deploy              # apply k8s/base (ConfigMaps, Deployments, Services) to the cluster
make rollout-check      # real Deployment/Service/EndpointSlice/ConfigMap/security state, both workloads
make discovery-check    # real Kubernetes DNS + gateway -> app Service HTTP proof
make secret-check        # real Secret wiring, auth, and non-disclosure proof
make smoke               # port-forward service/maops-gateway + real HTTP checks
make dependency-check   # gateway liveness vs. dependency-aware readiness proof
```

Or run the full authoritative sequence in one shot:

```bash
make day2-check
```

## Version consistency (closes DAY1-REL-I1)

Day 1 accepted `DAY1-REL-I1` as open debt: nothing automatically
cross-checked `VERSION` against the Makefile's image tag or the
manifests' `app.kubernetes.io/version` labels. Day 2 closes it:

- The Makefile derives `VERSION := $(shell cat VERSION)` once, and both
  `GATEWAY_IMAGE`/`APP_IMAGE` derive from that - no image tag is
  hand-typed a second time.
- `make version-check` (`scripts/version_check.py`) independently reads
  `VERSION`, renders `k8s/base`, and asserts VERSION itself matches the
  Day 2 target, both Deployment image tags match VERSION, and every
  rendered `app.kubernetes.io/version` label (including pod template
  labels) matches VERSION - failing loudly on any drift.
- `version-check` is part of `make day2-check`'s required gate.

`DAY1-INT-I2` (the `check_configmap_consumption`/exec helpers'
hardcoded `/usr/bin/python3.11` interpreter path) remains **ACCEPTED /
OPEN** - Day 2 kept the exact same digest-pinned Distroless base image
for both workloads, so the coupling is unchanged and still live-proven
correct; it will need re-evaluating only if/when a future day changes
the base image.

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day2` already exists, this is a no-op (aside
from printing `kubectl get nodes`). Uses `kind/cluster.yaml`, pinned to
the same node image digest as Day 1, single control-plane node only.

## Image build / load

```bash
make image-build   # docker build both gateway/ and app/ images
make image-load     # kind load docker-image for both, into maops-k8s-day2
```

Both Deployments use `imagePullPolicy: IfNotPresent` since images are
loaded directly into the kind node - no registry involved.

## Deploy lifecycle (order matters)

```bash
make cluster-create     # 1. create the kind cluster
make namespace-apply    # 2. apply ONLY the Namespace to the explicit Day 2 context
make secret-bootstrap   # 3. create/preserve the runtime Secret (needs the namespace to exist)
make image-load          # 4. load both images
make deploy               # 5. apply the full Kustomize base
```

`make deploy` applies `k8s/base/` in full: Namespace, both ConfigMaps,
both Deployments (2 replicas each), both Services (ClusterIP). The
Secret is deliberately **not** part of this - see
[`docs/architecture.md`](docs/architecture.md#secret-lifecycle).

## Verification

```bash
make rollout-check      # node/version/namespace/replicas/EndpointSlice/ConfigMap/
                         #   security/UID-GID/no-SA-token/Secret-mount, both workloads
make discovery-check    # real DNS resolution + gateway -> app Service HTTP
make secret-check         # Secret existence/mount/auth/non-disclosure, end to end
make smoke                # bounded port-forward to service/maops-gateway + real HTTP
make dependency-check    # app-outage experiment: gateway live=200, ready=503, backend=503,
                          #   no restart-count increase, then guaranteed restoration to 2/2
```

Each of these is a standalone script under `scripts/` - none require a
third-party Python package, and none leave a background process running
afterward (bounded `kubectl port-forward` via `scripts/portforward.py`,
carrying forward Day 1's `DAY1-INT-M1` SIGTERM-safe cleanup fix
unchanged).

## Port-forward usage

Automated validation picks a free local port itself and cleans the
port-forward process up automatically. For manual, human use, always
target the gateway (the normal Day 2 entry point):

```bash
kubectl --context kind-maops-k8s-day2 -n maops-platform port-forward service/maops-gateway 8080:8080
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
make cluster-delete   # kind delete cluster --name maops-k8s-day2 (ONLY this cluster)
```

This never runs `docker system prune`, never touches `maops-k8s-day1` or
any other kind cluster/Docker resource.

## Day 2 scope boundaries

Included: a second workload (`maops-gateway`) behind its own ClusterIP
Service, real DNS-based service discovery, `discovery.k8s.io/v1`
EndpointSlice-based readiness evidence, two workload-specific
ConfigMaps, a runtime-bootstrapped Secret mounted read-only into both
workloads, app-level internal authentication, dependency-aware gateway
readiness vs. local-only gateway liveness (proven via a live
scale-to-zero/restore experiment), and a version-consistency guard
closing `DAY1-REL-I1`.

Explicitly **not** part of Day 2 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives): worker-node scheduling design, HPA, PDB, rollout
tuning, StatefulSet, PVC, custom ServiceAccount, RBAC, NetworkPolicy,
Helm, Ingress, GitHub Actions, an observability stack, Terraform,
Ansible, Argo CD, cloud clusters, and container registry publishing.

## Repository layout

```
app/                  maops-app: stdlib-only HTTP workload + Dockerfile
gateway/               maops-gateway: stdlib-only HTTP workload + Dockerfile
k8s/base/              Namespace, 2 ConfigMaps, 2 Deployments, 2 Services, kustomization.yaml
                        (the runtime Secret is deliberately NOT rendered here)
kind/cluster.yaml      pinned single-control-plane kind cluster config (Day 2: maops-k8s-day2)
scripts/               dependency-free Python validation + cluster tooling
tests/                 Docker-free unit tests (incl. negative cases)
docs/                  architecture.md, roadmap.md, engineering-reviews/ (Day 1, frozen)
.claude/               CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile               authoritative local engineering interface
VERSION                0.2.0 (development target - not yet released/tagged)
```

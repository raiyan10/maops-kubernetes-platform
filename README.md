# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build.

**Latest RELEASED: `v0.4.0`** (Day 4 - StatefulSet, PVC, persistence,
frozen - see `docs/engineering-reviews/day-04-*`). `v0.1.0` (Day 1),
`v0.2.0` (Day 2), and `v0.3.0` (Day 3) are also released and frozen.

**Current DEVELOPMENT TARGET: `v0.5.0`** (Day 5 - security context
hardening, ServiceAccounts, RBAC, NetworkPolicy). Day 5 is **not
released or tagged** - the work in this README beyond the Day 1-4
sections describes the in-progress Day 5 build, left uncommitted for
independent review.

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how Day 5's pieces
fit together.

## Day 5 topology

```
Host
  |
  | kubectl port-forward
  v
maops-gateway ClusterIP Service
  |
  v
maops-gateway Deployment (SA: maops-gateway, no RBAC binding)
  |
  | HTTP via Kubernetes DNS - BACKEND_HOST=maops-app - ALLOWED by NetworkPolicy
  v
maops-app ClusterIP Service
  |
  v
maops-app Deployment (SA: maops-app, no RBAC binding)
  |
  | HTTP via Kubernetes DNS - STATE_HOST=maops-state - ALLOWED by NetworkPolicy
  v
maops-state ClusterIP Service      maops-state-headless (governing, clusterIP: None)
  |
  v
maops-state StatefulSet (SA: maops-state, no RBAC binding)

Namespace: maops-day5-validation (separate from maops-platform)
  |
  +--> validation-client probe Pod  --[ALLOWED]--> maops-gateway (cross-namespace, the ONLY way in)
  |         (ephemeral, no persistent Deployment)   --[DENIED]--> maops-app, maops-state
  |
  +--> ServiceAccount: maops-diagnostics (the ONE identity with an API token)
            |
            v
       Role + RoleBinding (namespace-scoped, in maops-platform)
            - ALLOWED: get/list/watch Pods, Services, EndpointSlices
            - DENIED: Secrets, Deployment/StatefulSet mutation, delete,
              scale, any other namespace, cluster-wide administration

NetworkPolicy (networking.k8s.io/v1, enforced by Cilium) in maops-platform:
  - default-deny ingress + egress for every Pod
  - + DNS egress (UDP/TCP 53 to CoreDNS)
  - + gateway -> app (never gateway -> state)
  - + app -> state
  - + validation-client (cross-namespace) -> gateway only
```

Gateway/app/state's own architecture, security context, probes,
Secrets, and PodDisruptionBudgets are **entirely unchanged from Day
4** - see [`docs/architecture.md`](docs/architecture.md) for the full
picture. What Day 5 adds is identity and network boundaries around
that unchanged architecture:

- **ServiceAccounts** - `maops-gateway`, `maops-app`, `maops-state`
  (one per workload, `automountServiceAccountToken: false`, never
  bound to any Role/ClusterRole) and `maops-diagnostics` (in the new
  `maops-day5-validation` namespace, `automountServiceAccountToken:
  true` - the one identity used to exercise real API authorization).
- **RBAC** - a single namespace-scoped `Role`/`RoleBinding` pair
  granting `maops-diagnostics` read-only (`get`/`list`/`watch`) access
  to Pods, Services, and EndpointSlices in `maops-platform` only -
  never Secrets, never write verbs, never another namespace, never
  cluster-wide.
- **NetworkPolicy** - standard `networking.k8s.io/v1` objects
  (Cilium is the enforcing CNI dataplane; no Hubble, service mesh, or
  L7/HTTP-aware policy is introduced this stage) implementing
  default-deny ingress+egress for every Pod in `maops-platform`, with
  narrow, explicit allows: DNS, gateway -> app, app -> state, and
  validation-client -> gateway only.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 (now used for real - installs Cilium) |
| kind | v0.32.0 |
| Cilium | 1.20.1 (installed via Helm, replaces kindnet as the CNI) |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, same digest as Days 1-4, do not float to
`latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day5` (kubeconfig context: `kind-maops-k8s-day5`)
- a separate, independently-created cluster from Days 1-4's clusters,
none of which Day 5 tooling ever touches. 1 control-plane + 2 worker
nodes, same as Day 3/4 - see `kind/cluster-day5.yaml`, a NEW tracked
file (Day 4's `kind/cluster.yaml` is preserved untouched, not edited
in place - see that file's own header comment for why Day 5 breaks
from the day-to-day single-file-edit convention here).

The one substantive difference from `kind/cluster.yaml`:
`networking.disableDefaultCNI: true` - kind's default CNI (kindnet) is
disabled so Cilium can be installed as the sole CNI instead. Every
node comes up **NotReady** (no pod network at all) until `make
cni-install` runs.

Kubeconfig path is explicit and overridable everywhere - never the
caller's default kubeconfig/context. Defaults to
`~/.kube/maops-k8s-day5.config`; override with
`make KUBECONFIG_PATH=/some/other/path ...`.

## Quick start

```bash
make tool-check              # verify docker/kubectl/kind/helm/python3 are present
make test                     # Docker-free unit tests for repository validation logic
make version-check            # cross-check VERSION against image tags/version labels
make manifest-check           # render k8s/base and statically validate it
make image-build                # build all three workload images (gateway, app, state)
make cluster-create           # create the pinned Day 5 kind cluster (kind/cluster-day5.yaml, CNI disabled)
make context-check            # fail closed unless kubectl is verified against the isolated Day 5 cluster
make cni-install                # install Cilium 1.20.1 via Helm as the CNI (idempotent)
make cni-status                  # READ-ONLY: verify Cilium/kube-proxy are healthy
make image-load                 # load all three images into every kind node
make storage-bootstrap        # harden new local-path-provisioner directories (root:10001, mode 2770)
make storage-hardening-check  # prove the hardening against a disposable scratch PVC
make namespace-apply          # apply BOTH Namespaces (maops-platform, maops-day5-validation)
make secret-bootstrap         # create/preserve BOTH runtime Secrets - never printed
make deploy                      # apply k8s/base (ConfigMaps, ServiceAccounts, RBAC, Deployments,
                                    #   StatefulSet, Services, PDBs, NetworkPolicies)
make rollout-check             # real Deployment/Service/EndpointSlice/ConfigMap/security state (gateway/app)
make scheduling-check          # real worker-only scheduling + topology spread proof (gateway/app)
make discovery-check           # real Kubernetes DNS + gateway -> app Service HTTP proof
make secret-check                 # real Secret wiring, auth, and non-disclosure proof (both Secrets)
make rbac-check                    # real maops-diagnostics RBAC scope: allowed reads, denied everything else
make networkpolicy-check       # real default-deny + explicit-allow NetworkPolicy behavior via probe Pods
make smoke                         # port-forward service/maops-gateway + real HTTP checks incl. /state
make dependency-check           # gateway liveness vs. dependency-aware readiness proof
make scaling-check              # real scaling 3 -> 4 -> 3, gateway/app, guaranteed restoration
make rolling-update-check      # real rolling update + real kubectl rollout undo rollback, gateway/app
make pdb-check                    # real PodDisruptionBudget/Eviction-API behavior, gateway/app
make state-check                  # maops-state runtime identity, security, and storage binding
make persistence-check          # data survives maops-state-0 Pod deletion/rescheduling
make retention-check             # PVC/PV retention + degraded-but-live behavior across a 1 -> 0 -> 1 cycle
make final-state-check         # independently prove the cluster is fully restored after all experiments
```

Or run the full authoritative sequence in one shot (recipe-sequential,
so it stays correctly ordered even under `make -j`):

```bash
make day5-check
```

## Version consistency (closes DAY1-REL-I1)

Unchanged mechanism since Day 2: the Makefile derives
`VERSION := $(shell cat VERSION)` once, and `GATEWAY_IMAGE`/`APP_IMAGE`/
`STATE_IMAGE` all derive from that; `make version-check`
(`scripts/version_check.py`) independently reads `VERSION`, renders
`k8s/base`, and asserts VERSION itself matches the Day 5 target, all
three workloads' image tags match VERSION, and every rendered
`app.kubernetes.io/version` label matches VERSION (now including the
ServiceAccount/Role/RoleBinding/NetworkPolicy objects too).

`DAY1-INT-I2` (the hardcoded `/usr/bin/python3.11` interpreter path
used by exec-based checks) remains **ACCEPTED / OPEN** - Day 5 kept
the same digest-pinned Distroless base image for all three workloads.

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day5` already exists, this is a no-op
(aside from printing `kubectl get nodes`). A pre-existing Day 5
cluster is never automatically deleted or recreated. Uses
`kind/cluster-day5.yaml`: 1 control-plane + 2 worker nodes, same
pinned node image digest as Days 1-4, `networking.disableDefaultCNI:
true`.

## CNI: Cilium replaces kindnet

```bash
make cni-install   # helm upgrade --install cilium cilium/cilium --version 1.20.1 (idempotent)
make cni-status     # READ-ONLY: DaemonSet/operator health, kube-proxy still present
```

Every node is `NotReady` immediately after `cluster-create` (no pod
network at all) until `cni-install` completes - this is expected, not
a fault, and is exactly what proves kindnet was genuinely disabled
rather than silently still present. Cilium is installed with
**kube-proxy still enabled** (`kubeProxyReplacement=false`) - Day 5
adopts Cilium only for its NetworkPolicy enforcement, not as a
kube-proxy replacement; Service load-balancing is unchanged from Days
1-4. See [`docs/architecture.md`](docs/architecture.md) for the full
rationale.

## Image build / load

```bash
make image-build   # docker build all three of gateway/, app/, state/ (see IMAGE_BUILD_FLAGS in the Makefile)
make image-load     # kind load docker-image for all three, into every node of maops-k8s-day5
```

Unchanged since Day 4.

## Storage bootstrap (must run after `make image-load`, before `make deploy`)

```bash
make storage-bootstrap          # harden local-path-provisioner's directory-creation permissions
make storage-hardening-check    # prove it, against a disposable scratch PVC
```

Unchanged since Day 4 - see [`docs/architecture.md`](docs/architecture.md).

## Deploy lifecycle (order matters)

```bash
make cluster-create           # 1. create the kind cluster (CNI disabled)
make context-check            # 2. fail closed unless verified against the isolated Day 5 cluster
make cni-install                 # 3. install Cilium - nodes are NotReady until this completes
make image-load                 # 4. load all three images (storage probes below need the app image present)
make storage-bootstrap        # 5. harden new PV-backed directory permissions
make storage-hardening-check  # 6. prove the hardening, before any application PVC exists
make namespace-apply          # 7. apply BOTH Namespaces (must precede secret-bootstrap)
make secret-bootstrap         # 8. create/preserve BOTH runtime Secrets (needs maops-platform to exist)
make deploy                       # 9. apply the full Kustomize base
```

`make deploy` applies `k8s/base/` in full: 2 Namespaces, 3 ConfigMaps,
4 ServiceAccounts, 1 Role, 1 RoleBinding, 2 Deployments (unchanged
since Day 3) + 1 StatefulSet, 4 Services (gateway/app/state/
state-headless), 2 PodDisruptionBudgets (gateway/app only), 7
NetworkPolicies. Both Secrets are deliberately **not** part of this -
see [`docs/architecture.md`](docs/architecture.md#day4-the-state-api-and-the-authenticated-gateway---app---state-chain).

## Verification

```bash
make rollout-check         # node/version/namespace/replicas/EndpointSlice/ConfigMap/
                             #   security/UID-GID/no-SA-token/Secret-mount (gateway/app)
make scheduling-check      # worker-only scheduling + topology spread (gateway/app)
make discovery-check       # real DNS resolution + gateway -> app Service HTTP
make secret-check             # Secret existence/mount/auth/non-disclosure, end to end, both Secrets
make rbac-check                # real maops-diagnostics RBAC: allowed pods/services/endpointslices reads,
                                 #   denied Secrets/mutation/delete/scale/cross-namespace/cluster-wide
make networkpolicy-check   # real default-deny + explicit-allow behavior via in-cluster probe Pods:
                                 #   validation-client->gateway allowed, ->app/state denied; gateway->app
                                 #   and app->state allowed; gateway->state denied; DNS still works
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
afterward (bounded `kubectl port-forward` via `scripts/portforward.py`;
`rbac_check.py`/`networkpolicy_check.py` create and always delete their
own short-lived probe Pods). Every live script fails closed via
`kube.verify_context()` if it is not actually talking to the verified
`kind-maops-k8s-day5` cluster.

## Port-forward usage

Automated validation picks a free local port itself and cleans the
port-forward process up automatically. For manual, human use, always
target the gateway (the normal entry point):

```bash
kubectl --kubeconfig ~/.kube/maops-k8s-day5.config --context kind-maops-k8s-day5 \
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

Press `Ctrl-C` in the port-forward terminal to stop it when done. This
still works exactly as before - port-forwarding reaches the gateway
Pod directly (bypassing the in-cluster NetworkPolicy entirely, since a
port-forward never traverses the pod network as a policy-visible peer)
so normal human/manual access is unaffected by Day 5's network
boundaries.

## Cleanup

```bash
make cluster-delete   # kind delete cluster --name maops-k8s-day5 (ONLY this cluster)
```

This never runs `docker system prune`, never touches any earlier day's
cluster, or any other kind cluster/Docker resource.

## Day 5 scope boundaries

Included: everything from Days 1-4 (unchanged: gateway/app/state
architecture, security contexts, probes, Secrets, PodDisruptionBudgets,
persistence/retention), plus a purpose-built ServiceAccount per
workload, a single namespace-scoped Role/RoleBinding for the
maops-diagnostics identity, standard `networking.k8s.io/v1`
NetworkPolicy default-deny + narrow explicit allows, a second namespace
(`maops-day5-validation`) for validation/diagnostic tooling, and Cilium
as the enforcing CNI dataplane (kube-proxy left enabled).

Explicitly **not** part of Day 5 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives): Hubble, service mesh (Day 6), and L7/HTTP-aware
NetworkPolicy (a standard NetworkPolicy governs L3/L4 reachability
only - it cannot distinguish `GET /state` from `PUT /state` for a
caller it otherwise permits to reach the gateway at all; see
`docs/architecture.md`'s DAY4-SEC-L1 note, still true unchanged in Day
5), Helm-packaged application charts (Helm is used here only to
install Cilium; the application itself stays Kustomize-only until Day
6), GitHub Actions CI, Ingress, Gateway API (Day 6), advanced
deployment strategies beyond RollingUpdate (Recreate, Blue-Green,
Canary - Day 7), `HorizontalPodAutoscaler`, scaling `maops-state`
beyond 1 replica, Argo Rollouts, an observability stack, Terraform,
Ansible, Argo CD, cloud clusters, and container registry publishing.

## Repository layout

```
app/                   maops-app: stdlib-only HTTP workload + Dockerfile
gateway/                maops-gateway: stdlib-only HTTP workload + Dockerfile
state/                  maops-state: stdlib-only HTTP workload + Dockerfile
k8s/base/               2 Namespaces, 3 ConfigMaps, 4 ServiceAccounts, 1 Role, 1 RoleBinding,
                         2 Deployments, 1 StatefulSet, 4 Services, 2 PDBs, 7 NetworkPolicies,
                         kustomization.yaml (runtime Secrets are deliberately NOT rendered here)
kind/cluster.yaml       Day 4's pinned kind config, preserved untouched (no CNI disable)
kind/cluster-day5.yaml  Day 5's pinned kind config - same topology, networking.disableDefaultCNI: true
scripts/                dependency-free Python validation + cluster tooling, incl. rbac_check.py,
                         networkpolicy_check.py, cni_check.py (new, Day 5)
tests/                  Docker-free unit tests (incl. negative cases)
docs/                   architecture.md, roadmap.md, engineering-reviews/ (Days 1-4, frozen)
.claude/                CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile                authoritative local engineering interface
VERSION                 0.5.0 (development target - not yet released/tagged)
```

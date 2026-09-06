# Roadmap

`maops-kubernetes-platform` is built in seven day-scoped stages, each
producing a tagged version. Every stage only introduces what its own
scope calls for - later capabilities are deliberately deferred, not
because they're hard, but so each stage's Kubernetes behavior can be
demonstrated and reviewed in isolation.

| Day | Version | Theme |
|---|---|---|
| 1 | v0.1.0 | Kubernetes foundation |
| 2 | v0.2.0 | Multi-service architecture, service discovery, configuration, Secrets |
| 3 | v0.3.0 | Scaling, scheduling, rolling updates, rollback, availability (PDB) |
| 4 | v0.4.0 | StatefulSet, PVC, persistence/recovery |
| 5 | v0.5.0 | Security context hardening, ServiceAccount, RBAC, NetworkPolicy |
| 6 | v0.6.0 | Helm, CI, automated kind validation |
| 7 | v1.0.0 | Production-readiness hardening, independent reviews, final release |

## Day 1 / v0.1.0 - Kubernetes foundation

**COMPLETE / RELEASED / FROZEN.** Single control-plane kind cluster, one
namespace, one ConfigMap, one Deployment (2 replicas) behind a ClusterIP
Service, reached locally via `kubectl port-forward`. See
`docs/architecture.md` for how Day 2 builds on this. Explicitly out of
scope at the time: worker nodes, Secrets, RBAC, NetworkPolicy,
PVC/StatefulSet, Helm, CI, Ingress, NodePort/LoadBalancer. Released as
`v0.1.0`; historical engineering evidence lives under
`docs/engineering-reviews/day-01-*` and is not modified by later days.

## Day 2 / v0.2.0 - Multi-service architecture, service discovery, configuration, Secrets

**COMPLETE / RELEASED / FROZEN.** A second workload (`maops-gateway`)
was introduced alongside the Day 1 app workload (`maops-app`) to
exercise real service discovery (Kubernetes DNS, ClusterIP-to-ClusterIP
via `BACKEND_HOST=maops-app`), a runtime Secret
(`maops-internal-auth`) was introduced for the first time - bootstrapped
out-of-band, never committed, mounted read-only into both workloads -
and configuration expanded to two workload-specific ConfigMaps
(`maops-gateway-config`, `maops-app-config`). Backend-readiness evidence
moved from the legacy v1 Endpoints API to `discovery.k8s.io/v1`
EndpointSlice. Released as `v0.2.0`; historical engineering evidence
lives under `docs/engineering-reviews/day-02-*` and is not modified by
later days. Day 3 builds directly on this architecture, unchanged - see
`docs/architecture.md`.

## Day 3 / v0.3.0 - Scaling, scheduling, rolling updates, rollback, availability (this stage)

**IN DEVELOPMENT / current target.** Not yet released or tagged. Builds
directly on Day 2's two-workload architecture, unchanged, and adds:

- A multi-node kind cluster (1 control-plane + 2 workers) - the first
  topology in this project with real worker nodes to schedule onto.
- Both Deployments scaled to 3 replicas, with an explicit
  `RollingUpdate` strategy (`maxUnavailable: 1`, `maxSurge: 1`,
  `minReadySeconds: 5`, `progressDeadlineSeconds: 120`,
  `revisionHistoryLimit: 5`) - pinned rather than left to Kubernetes
  defaults.
- Worker-only scheduling: required node affinity excluding the
  control-plane node, plus per-workload `topologySpreadConstraints`
  (`maxSkew: 1`, `topologyKey: kubernetes.io/hostname`,
  `whenUnsatisfiable: DoNotSchedule`) - never hard pod anti-affinity,
  which would make 3 replicas over 2 workers mathematically
  unschedulable.
- A `PodDisruptionBudget` per workload (`minAvailable: 2`), proving
  voluntary-disruption protection via the real Eviction API - not
  `kubectl delete pod`.
- Real scaling (3 -> 4 -> 3), a real rolling update (triggered by a
  temporary, uniquely-marked Pod-template annotation - never a fake
  image tag) and a real `kubectl rollout undo` rollback, and real PDB/
  Eviction-API behavior, all proven live against the cluster with
  guaranteed restoration.

Explicitly out of scope for Day 3: HorizontalPodAutoscaler (scaling here
is deliberate/manual, not automatic), StatefulSet, PVC, ServiceAccount,
RBAC, NetworkPolicy, Ingress, Gateway API, service mesh, advanced
deployment strategies beyond RollingUpdate (Recreate/Blue-Green/Canary -
Day 7), Helm, GitHub Actions, an observability stack, Terraform,
Ansible, Argo CD, and cloud clusters. See `docs/architecture.md` for the
full picture and the rationale behind each scheduling/availability
decision.

## Day 4 / v0.4.0 - StatefulSet, PVC, persistence

**FUTURE - not yet implemented.** A StatefulSet-backed component with a
PersistentVolumeClaim, proving data survives pod rescheduling and
recovery after deliberate failure injection.

## Day 5 / v0.5.0 - Security hardening, RBAC, NetworkPolicy

**FUTURE - not yet implemented.** A purpose-built ServiceAccount with
least-privilege RBAC (Role/RoleBinding, scoped to the namespace), and
NetworkPolicy objects restricting pod-to-pod traffic to only what's
required. This is where `automountServiceAccountToken` moves from
`false` to a deliberately scoped `true` for the workloads that need API
access, and where Day 2's deferred network-isolation gap (any Pod in
`maops-platform` can currently reach any other) actually gets closed.

## Day 6 / v0.6.0 - Helm, CI, automated kind validation

**FUTURE - not yet implemented.** The Kustomize base is packaged as a
Helm chart (or a Helm chart is introduced alongside it, decision made at
that stage), and GitHub Actions orchestrates the existing Makefile
targets against an ephemeral kind cluster in CI - not a reimplementation
of the local validation logic, just automation of it. Ingress and
Gateway API are both introduced and compared against each other for
external access, superseding Day 1-3's `kubectl port-forward`-only
model.

## Day 7 / v1.0.0 - Production-readiness hardening

**FUTURE - not yet implemented.** Service mesh; advanced deployment
strategy demonstrations (Recreate, Blue/Green, Canary) compared against
Day 3's RollingUpdate; independent review passes across architecture,
security, and testing; closing gaps found; final hardening pass; final
tagged `v1.0.0` release. Argo Rollouts is explicitly never introduced in
this project - the advanced-strategy demonstrations use native
Kubernetes primitives only.

## Explicitly out of scope for this project

These are deliberately never introduced in `maops-kubernetes-platform`,
at any stage:

- **Terraform** - no infrastructure provisioning; kind is the only
  cluster lifecycle tool this project uses.
- **Ansible** - no configuration management layer.
- **Argo CD** - no GitOps continuous-deployment controller; deployment
  stays `kubectl`/Helm-driven through the Makefile.
- **Observability stack** (Prometheus/Grafana/Loki/etc.) - out of scope
  for this portfolio project.
- **Cloud infrastructure provisioning** - this project targets local
  kind clusters only; no cloud provider is ever provisioned against.

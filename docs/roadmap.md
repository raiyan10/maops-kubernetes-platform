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
| 3 | v0.3.0 | Scaling, rollout, rollback, scheduling, availability (PDB) |
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

## Day 2 / v0.2.0 - Multi-service architecture, service discovery, configuration, Secrets (this stage)

**IN DEVELOPMENT / current target.** Not yet released or tagged. A
second workload (`maops-gateway`) is introduced alongside the Day 1 app
workload (`maops-app`) to exercise real service discovery (Kubernetes
DNS, ClusterIP-to-ClusterIP via `BACKEND_HOST=maops-app`), a runtime
Secret (`maops-internal-auth`) is introduced for the first time -
bootstrapped out-of-band, never committed, mounted read-only into both
workloads - and configuration expands to two workload-specific
ConfigMaps (`maops-gateway-config`, `maops-app-config`). Backend-
readiness evidence moves from the legacy v1 Endpoints API to
`discovery.k8s.io/v1` EndpointSlice. See `docs/architecture.md` for the
full picture. Explicitly out of scope: worker-node scheduling design,
HPA, PDB, rollout tuning, StatefulSet, PVC, custom ServiceAccount, RBAC,
NetworkPolicy, Helm, Ingress, GitHub Actions, an observability stack,
Terraform, Ansible, Argo CD, cloud clusters, and container registry
publishing - see "Day 2 acceptance evidence" boundaries below and
`docs/architecture.md` for exactly why NetworkPolicy/RBAC remain
deferred to Day 5.

## Day 3 / v0.3.0 - Scaling, rollout, rollback, scheduling, availability

**FUTURE - not yet implemented.** Horizontal scaling behavior, rolling
update strategy tuning, deliberate rollback exercises, basic scheduling
constraints, and a PodDisruptionBudget to prove availability guarantees
under voluntary disruption.

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
of the local validation logic, just automation of it.

## Day 7 / v1.0.0 - Production-readiness hardening

**FUTURE - not yet implemented.**

Independent review passes across architecture, security, and testing;
closing gaps found; final hardening pass; first tagged `v1.0.0` release.

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

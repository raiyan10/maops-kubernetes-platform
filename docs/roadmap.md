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

## Day 1 / v0.1.0 - Kubernetes foundation (this stage)

Single control-plane kind cluster, one namespace, one ConfigMap, one
Deployment (2 replicas) behind a ClusterIP Service, reached locally via
`kubectl port-forward`. See `docs/architecture.md` for the full picture.
Explicitly out of scope: worker nodes, Secrets, RBAC, NetworkPolicy,
PVC/StatefulSet, Helm, CI, Ingress, NodePort/LoadBalancer.

## Day 2 / v0.2.0 - Multi-service architecture

A second workload is introduced to exercise real service discovery
(DNS-based, ClusterIP-to-ClusterIP), a Secret is introduced for the
first time (with the security posture Day 1 already established), and
configuration expands beyond a single ConfigMap.

## Day 3 / v0.3.0 - Scaling, rollout, rollback, scheduling, availability

Horizontal scaling behavior, rolling update strategy tuning, deliberate
rollback exercises, basic scheduling constraints, and a
PodDisruptionBudget to prove availability guarantees under voluntary
disruption.

## Day 4 / v0.4.0 - StatefulSet, PVC, persistence

A StatefulSet-backed component with a PersistentVolumeClaim, proving
data survives pod rescheduling and recovery after deliberate failure
injection.

## Day 5 / v0.5.0 - Security hardening, RBAC, NetworkPolicy

A purpose-built ServiceAccount with least-privilege RBAC (Role/
RoleBinding, scoped to the namespace), and NetworkPolicy objects
restricting pod-to-pod traffic to only what's required. This is where
`automountServiceAccountToken` moves from `false` to a deliberately
scoped `true` for the workloads that need API access.

## Day 6 / v0.6.0 - Helm, CI, automated kind validation

The Kustomize base is packaged as a Helm chart (or a Helm chart is
introduced alongside it, decision made at that stage), and GitHub
Actions orchestrates the existing Makefile targets against an
ephemeral kind cluster in CI - not a reimplementation of the local
validation logic, just automation of it.

## Day 7 / v1.0.0 - Production-readiness hardening

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

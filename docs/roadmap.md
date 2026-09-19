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
| 6 | v0.6.0 | Helm, CI, automated kind validation, service mesh |
| 7 | v1.0.0 | Advanced deployment strategies (Recreate, Blue-Green, Canary), production-readiness hardening, independent reviews, final release |

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

## Day 3 / v0.3.0 - Scaling, scheduling, rolling updates, rollback, availability

**COMPLETE / RELEASED / FROZEN.** Released as `v0.3.0` (PR #3, tag
`v0.3.0` -> commit `9fc7fe9f25d729d76317de85b5722e84271234f0`); historical
engineering evidence and post-release verification live under
`docs/engineering-reviews/day-03-*` and are not modified by later days.
Builds
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
RBAC, NetworkPolicy, Ingress, Gateway API, service mesh (Day 6),
advanced deployment strategies beyond RollingUpdate (Recreate/
Blue-Green/Canary - Day 7), Helm, GitHub Actions, an observability stack, Terraform,
Ansible, Argo CD, and cloud clusters. See `docs/architecture.md` for the
full picture and the rationale behind each scheduling/availability
decision.

## Day 4 / v0.4.0 - StatefulSet, PVC, persistence

**COMPLETE / RELEASED / FROZEN.** Released as `v0.4.0`; historical
engineering evidence and post-release verification live under
`docs/engineering-reviews/day-04-*` and are not modified by later days.
Keeps Day 3's gateway/app architecture entirely unchanged (still 3
replicas each, same scaling/rollout/scheduling/PDB behavior) and adds a
third workload, `maops-state` - a single-replica StatefulSet with a
PVC-backed `/data` volume, reached as
`gateway /state -> app /internal/state -> state /state`, authenticated
by a second, dedicated runtime Secret (`maops-state-auth`) that only
`app` and `state` ever hold - `gateway` never receives it. A dedicated,
narrowly-scoped storage bootstrap (`scripts/storage_bootstrap.py`,
`make storage-bootstrap`) hardens the already-installed
`local-path-provisioner`'s directory-creation permissions (root:10001,
mode 2770 - never world-writable) before the application's PVC is ever
created, verified against a disposable scratch claim
(`make storage-hardening-check`) with both a positive (correct UID/GID
write) and negative (unrelated UID/GID -> EACCES) proof. See
`docs/architecture.md` for the full design, including the two-attempt
storage preflight that discovered and resolved a containerd multi-arch
image-import defect this day's image-build tooling now avoids.

Explicitly out of scope for Day 4: ServiceAccount, RBAC, NetworkPolicy
(Day 5), Helm/CI/Ingress/Gateway API/service mesh (Day 6), advanced
deployment strategies (Recreate/Blue-Green/Canary - Day 7), horizontal
scaling of `maops-state` (single-writer only), and any change to Day
1-3's Distroless base image digest, interpreter path, or gateway/app
application code beyond the new `/state`/`/internal/state` proxy hops
this stage adds.

## Day 5 / v0.5.0 - Security hardening, RBAC, NetworkPolicy

**COMPLETE / RELEASED / EVIDENCE-CLOSED / FROZEN.** Released as
`v0.5.0` (PR #5, tag `v0.5.0` -> commit
`a6f6124198e3311023bddb84f2e7fce657ad52b4`); historical engineering
evidence, remediation log, final adjudication, and post-release
verification live under `docs/engineering-reviews/day-05-*` and are
not modified by later days. Keeps Day 4's entire gateway/app/state
architecture unchanged (security contexts, probes, Secrets,
PodDisruptionBudgets, persistence/retention behavior) and adds
identity and network boundaries around it:

- A purpose-built ServiceAccount per workload
  (`maops-gateway`/`maops-app`/`maops-state`, all
  `automountServiceAccountToken: false`, never bound to any Role/
  ClusterRole) - this is where `automountServiceAccountToken` moves
  from implicit-default-SA to an explicit, purpose-built identity, even
  though it stays `false` for every application workload.
- A second namespace, `maops-day5-validation`, holding the one identity
  that DOES receive an API token: `maops-diagnostics`
  (`automountServiceAccountToken: true`), bound via a single
  namespace-scoped `Role`/`RoleBinding` pair to read-only
  (`get`/`list`/`watch`) access on Pods/Services/EndpointSlices in
  `maops-platform` only - never Secrets, never write verbs, never
  another namespace, never cluster-wide (no ClusterRole/
  ClusterRoleBinding anywhere in this project).
- Standard `networking.k8s.io/v1` NetworkPolicy objects, enforced by
  Cilium (replacing kind's default kindnet CNI - kube-proxy stays
  enabled, Day 5 does not adopt Cilium's kube-proxy-replacement mode):
  default-deny ingress+egress for every Pod in `maops-platform`, with
  narrow explicit allows for DNS, `gateway -> app`, `app -> state`, and
  a `validation-client` identity (in `maops-day5-validation`) reaching
  `gateway` only - this is where Day 2's deferred network-isolation gap
  (any Pod in `maops-platform` could reach any other) actually gets
  closed.

Explicitly out of scope for Day 5: Hubble, service mesh (Day 6), and
L7/HTTP-aware NetworkPolicy (a standard NetworkPolicy governs L3/L4
reachability only), Helm-packaged application charts (Helm is used
this stage only to install Cilium), GitHub Actions CI, Ingress, Gateway
API (Day 6), advanced deployment strategies beyond RollingUpdate
(Recreate, Blue-Green, Canary - Day 7), `HorizontalPodAutoscaler`, and
any change to Day 1-4's Distroless base image digest, interpreter
path, or application code.

Accepted limitations, disclosed at release and carried forward
unchanged (see `docs/architecture.md` and
`docs/engineering-reviews/day-05-post-release-verification.md` for the
full evidence): running several multi-node kind clusters concurrently
can exceed a WSL2 host's available capacity, so superseded clusters
should be stopped before validating a later day; Day 1-4's clusters
were stopped (not deleted) at Day 5 closure and their runtime health
was not re-claimed by this release; the Cilium operator's leader-
election instability and the three-node Cilium footprint observed
under host pressure; `cilium-envoy` is present because the selected
Helm chart installs it by default, unused by this stage's policies;
the validation namespace's isolation is intentionally bounded; and the
`DAY4-SEC-L1` finding is reduced, not closed, by Day 5's NetworkPolicy
(it cannot govern a `kubectl port-forward` path). The released context
checker also prints a cosmetic stale "Day 4 context" label while
correctly validating the Day 5 context - a disclosed, Low-severity
wording issue, corrected only when the shared checker is next
advanced for Day 6.

## Day 6 / v0.6.0 - Helm, CI, automated kind validation, service mesh

**FUTURE - not yet implemented.** The Kustomize base is packaged as a
Helm chart (or a Helm chart is introduced alongside it, decision made at
that stage), and GitHub Actions orchestrates the existing Makefile
targets against an ephemeral kind cluster in CI - not a reimplementation
of the local validation logic, just automation of it. Ingress and
Gateway API are both introduced and compared against each other for
external access, superseding Day 1-3's `kubectl port-forward`-only
model. A service mesh is also introduced this stage, layered on top of
Day 5's NetworkPolicy L3/L4 boundaries with mTLS between workloads and
request-level (L7/HTTP) traffic policy - Day 5 deliberately implements
none of this (no Hubble, no L7-aware policy, standard NetworkPolicy
only), so this is genuinely new capability at Day 6, not merely an
extension of what Day 5 already has.

## Day 7 / v1.0.0 - Advanced deployment strategies, production-readiness hardening

**FUTURE - not yet implemented.** Advanced deployment strategy
demonstrations - Recreate, Blue-Green, and Canary - compared against
Day 3's RollingUpdate; independent review passes across architecture,
security, and testing; closing gaps found; final hardening pass; final
tagged `v1.0.0` release. Argo Rollouts is explicitly never introduced in
this project - the advanced-strategy demonstrations use native
Kubernetes primitives only. Service mesh is Day 6 scope, not Day 7 -
Day 7 builds on the mesh Day 6 introduces rather than introducing it.

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

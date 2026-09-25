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

**COMPLETE / RELEASED as a local kind reference platform.** Released
as `v0.6.0` on 2026-09-25 (PRs #6-#8; annotated tag `v0.6.0` -> commit
`19d6b28b1282aedc8417a5a2ba10e74614afe244`; [GitHub Release](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.6.0));
post-release record in
`docs/engineering-reviews/day-06-post-release-verification.md`. The application (gateway/app/state, unchanged since
Day 4) is packaged as a Helm chart (`charts/maops-kubernetes-platform`)
- the sole Day 6 application deployment source; `k8s/base` remains the
frozen, unmodified Day 5 Kustomize source and is never applied by any
Day 6 target. A minimal, cluster-free GitHub Actions workflow
(`.github/workflows/ci.yml`) orchestrates the existing Makefile's
`ci-check` target - unit tests, version/chart-version/appVersion/
image-tag checks, the frozen k8s/base static manifest check, and Helm
lint/template/static-chart-check - never a reimplementation of the
local validation logic, and never a live kind cluster in CI (that stays
a local/manual `make day6-check` concern; see docs/architecture.md's
"cluster-free CI limitation").

**Correction to this section's previous draft:** Day 6 uses exactly
ONE cluster-external routing approach - the Kubernetes Gateway API,
with Istio as the sole `GatewayClass` controller - superseding Day 1-5's
`kubectl port-forward`-only model. A second, Ingress-based
implementation is deliberately NOT also introduced: this project's
scope discipline (see "Explicitly out of scope for this project" below)
favors demonstrating one routing mechanism thoroughly - GatewayClass ->
Gateway -> HTTPRoute -> Service, Istio ambient identity/mTLS layered on
top - over maintaining two parallel, partially-overlapping ingress
paths that would each need their own NetworkPolicy/AuthorizationPolicy
carve-outs for no additional Kubernetes-behavior teaching value this
stage. (An earlier draft of this roadmap entry said "Ingress and
Gateway API are both introduced and compared against each other" -
that was never implemented and is corrected here, not carried forward.)

A service mesh (Istio ambient - no sidecars, no waypoint) is also
introduced this stage, layered on top of Day 5's NetworkPolicy L3/L4
boundaries with strict mTLS between workloads and identity-scoped
(L4-compatible, never L7/HTTP-aware - no waypoint means no L7 east-west
authorization) `AuthorizationPolicy` objects - Day 5 deliberately
implements none of this (no Hubble, no L7-aware policy, standard
NetworkPolicy only), so this is genuinely new capability at Day 6, not
merely an extension of what Day 5 already has. Cilium remains the CNI
and the NetworkPolicy enforcer, reconfigured for Istio ambient
coexistence (`cni.exclusive=false`, `socketLB.hostNamespaceOnly=true`,
a single non-HA operator replica for this constrained local cluster);
no Cilium Gateway API controller and no Cilium L7 policy are introduced
- Istio is the only Gateway API controller and the only mesh policy
layer. See `docs/architecture.md` for the full design, the Cilium/Istio
responsibility boundary, and the accepted local-development
limitations (no HA claim, no TLS/cert-manager, no cloud LoadBalancer,
no HPA, no observability stack, no Argo Rollouts/Argo CD).

Explicitly out of scope for Day 6 (deferred to Day 7 or never
introduced at all - see "Explicitly out of scope for this project"
below): advanced deployment strategies beyond RollingUpdate (Recreate,
Blue-Green, Canary - Day 7), `HorizontalPodAutoscaler`, Argo Rollouts,
Argo CD, an observability stack (Hubble, Kiali, Prometheus, Grafana,
Jaeger/tracing), TLS/cert-manager, a cloud LoadBalancer, a Cilium
Gateway API controller, a waypoint proxy, and any change to Day 1-5's
Distroless base image digest, interpreter path, or gateway/app/state
application code.

**Live validation passed (2026-09-22 to 2026-09-23):** the live
sequence this stage is written for was run target by target against
one preserved `maops-k8s-day6` cluster, with each live-discovered
defect fixed and the affected stage re-run - `networkpolicy-check`
37/37, `mesh-check` 45/45, `persistence-check` 12/12,
`retention-check` 22/22, the corrected `helm-lifecycle-check` 24/24,
and `final-state-check` 43/43. See `docs/architecture.md`'s "DAY6: live
validation record" for the exact results, Helm revision history, mesh
denial-evidence tiers, and accepted limitations, and
`docs/engineering-reviews/day-06-*` for the independent reviews and
remediation log.

**Independent review and closure (2026-09-24):** five independent
reviews were adjudicated REMEDIATION REQUIRED; every finding was then
remediated or explicitly accepted. A post-reboot `final-state-check`
scored 42/43 because the run's `/tmp` suite baseline had been lost to
a host reboot (recorded as-is, never recaptured); a fresh
baseline-bracketed run (`persistence-check` 12/12, `retention-check`
22/22, `final-state-check` 43/43, baseline kept outside `/tmp`) closed
that gap, and the final adjudication is **RELEASE READY**. Day 6 is a
validated local kind reference platform, not a production-ready one;
commit, merge, tag, and release remain separate, explicit steps
(`.claude/skills/release-readiness/SKILL.md`).

**Merged, then post-restart remediation (2026-09-25):** Day 6 was merged
to `main` via PR #6. After a later WSL/Kind component restart,
`maops-state-0` was Kubernetes Ready without its ambient in-Pod
listeners (15001/15006/15008), and one gateway Pod was unready without
them; recreating only those two Pods restored the chain, with storage identity
preserved, and the gate then passed on merged `main`
(`final-state-check` 43/43). A new read-only `make
ambient-workload-check` now checks those listeners per Pod, after
`deploy` and before `rollout-check`. See `docs/architecture.md`'s
"DAY6: post-restart ambient listener incident (2026-09-25)".

**Release gate closed (2026-09-25):** PR #7 merged with CI passing, and
the read-only gate on merged `main` passed (`context-check` 6/6,
`cni-status` 4/4, `mesh-status` 4/4, `ambient-workload-check` 67/67,
`rollout-check` 35/35, `gateway-check` 8/8, `smoke` 6/6,
`final-state-check` 43/43 against the unchanged run baseline). Day 6 was
adjudicated RELEASE READY as a local kind reference platform and then
released as `v0.6.0` the same day, after PR #8 merged the release-gate
documentation. The cause of the lost listeners remains unproven.

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

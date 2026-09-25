# maops-kubernetes-platform

Project 4 of the DevOps portfolio series: a staged, day-by-day
Kubernetes platform engineering build. It demonstrates a three-workload
(`gateway -> app -> state`) Kubernetes application secured with
per-workload ServiceAccounts, least-privilege RBAC, and a default-deny
NetworkPolicy mesh enforced by Cilium, now packaged as a Helm chart and
fronted by the Kubernetes Gateway API through an Istio ambient service
mesh - all proven live against a real multi-node kind cluster, not just
asserted in YAML.

**Latest RELEASED: `v0.5.0`** (Day 5 - ServiceAccounts, RBAC,
NetworkPolicy, Cilium as the enforcing CNI - evidence-closed and frozen,
see `docs/engineering-reviews/day-05-*`). `v0.1.0` (Day 1), `v0.2.0`
(Day 2), `v0.3.0` (Day 3), and `v0.4.0` (Day 4) are also released and
frozen. **Days 1-5 are complete.**

**Active implementation: `v0.6.0`** (Day 6 - Helm packaging, minimal
cluster-free GitHub Actions CI, the Kubernetes Gateway API through
Istio as the sole cluster-external routing approach, and an Istio
ambient service mesh). **Merged to `main` via PR #6** as a local Kind
reference platform - static and live validation passed and the
independent review findings are closed - but **`v0.6.0` is not yet
tagged or published**, and the release gate is pending the 2026-09-25
post-restart remediation. See [Day 6 status](#day-6-status-merged-local-kind-not-yet-tagged-or-released)
below.

**Final milestone: `v1.0.0`** (Day 7 - Recreate/Blue-Green/Canary
deployment-strategy demonstrations and final production-readiness
hardening).

See [`docs/roadmap.md`](docs/roadmap.md) for the full seven-day plan and
[`docs/architecture.md`](docs/architecture.md) for how the pieces fit
together, including the full Day 6 design.

## What this platform currently demonstrates

- A `gateway -> app -> state` call chain: `maops-gateway` (stateless,
  3 replicas) fronts `maops-app` (stateless, 3 replicas), which reads/
  writes through `maops-state` (a single-replica StatefulSet with a
  PVC-backed `/data` volume) - all reached via Kubernetes DNS and
  ClusterIP Services, never a Pod IP or hardcoded address. Unchanged in
  shape since Day 4.
- Real scheduling, scaling, rolling-update/rollback, and
  PodDisruptionBudget behavior across a 2-worker kind cluster (Day 3),
  and real persistence/PVC-retention behavior across Pod deletion and
  scale-to-zero cycles (Day 4).
- **Day 5's security boundaries** (frozen, carried forward unchanged
  into Day 6's Helm chart): a dedicated ServiceAccount per workload, a
  namespace-scoped `Role`/`RoleBinding` for the one identity that
  exercises real API authorization (`maops-diagnostics`), and standard
  `networking.k8s.io/v1` NetworkPolicy objects enforced by Cilium.
- **Day 6's Helm packaging, Gateway API routing, and Istio ambient
  mesh** (implemented and merged on the local kind cluster; not yet
  tagged or published - see below):
  - The application is packaged as a Helm chart
    (`charts/maops-kubernetes-platform`) - the sole Day 6 application
    deployment source. `k8s/base` (Day 5's Kustomize source) stays
    frozen and is never applied this stage.
  - The Kubernetes Gateway API, with **Istio as the only
    `GatewayClass` controller**, is the one cluster-external routing
    path - `GatewayClass: istio -> Gateway: maops-edge -> HTTPRoute:
    maops-gateway-route -> Service: maops-gateway`. No `Ingress` object
    and no second ingress controller exist anywhere in this project.
  - Istio **ambient** mesh (no sidecars, no waypoint) layers strict
    mTLS and identity-scoped `AuthorizationPolicy` objects on top of
    Day 5's NetworkPolicy boundaries - Cilium remains the CNI and the
    NetworkPolicy enforcer, reconfigured for ambient coexistence.
  - A minimal, cluster-free GitHub Actions workflow
    (`.github/workflows/ci.yml`) runs `make ci-check` on every push/PR.

Engineering reviews (architecture, security, cluster-integration, test,
and release-readiness) and post-release evidence for every day live
under [`docs/engineering-reviews/`](docs/engineering-reviews/) - see
[`docs/engineering-reviews/day-05-post-release-verification.md`](docs/engineering-reviews/day-05-post-release-verification.md)
for the full Day 5 release record
([PR #5](https://github.com/raiyan10/maops-kubernetes-platform/pull/5),
[release `v0.5.0`](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v0.5.0)).
Only a few purposeful screenshots are kept, under `docs/images/day-05/`
- deliberately not a full evidence dump. Day 6's independent reviews,
adjudication, and remediation log are under
`docs/engineering-reviews/day-06-*`; it has no post-release evidence,
release URL, or screenshots yet, because it has not been committed,
merged, tagged, or released.

**Not claimed at this stage:** production cluster high availability,
node-loss recovery, a production-grade service mesh rollout, TLS/
cert-manager, a cloud LoadBalancer, an observability stack, or
operation on a cloud-managed Kubernetes offering - this remains a
local, single-tenant kind cluster built for staged engineering
demonstration.

## Day 6 status: merged (local kind), not yet tagged or released

Day 6 is **merged to `main` (PR #6)** as a local Kind reference
platform. It has **not** been tagged (`v0.6.0`) or published - those
remain explicit, separate steps, and the release gate is pending the
2026-09-25 post-restart remediation described below. It is not a
production-ready platform.

- **Static** (`make ci-check`, the same cluster-free sequence GitHub
  Actions runs): 1197 unit tests; `version-check` 50/50;
  `manifest-check` 267/267 (frozen k8s/base); `helm-lint` and
  `helm-template` pass; `helm-check` 215/215 (including the Istio
  Gateway infrastructure ConfigMap and the ambient health-probe
  CiliumClusterwideNetworkPolicy); 30 rendered objects, with zero
  Secret, Ingress, ClusterRole, or waypoint objects.
- **Live**, against the local `maops-k8s-day6` kind cluster
  (2026-09-22 to 2026-09-23, staged target by target on one preserved
  cluster): `networkpolicy-check` 37/37, `mesh-check` 45/45,
  `persistence-check` 12/12, `retention-check` 22/22, the corrected
  `helm-lifecycle-check` 24/24, and `final-state-check` 43/43, with the
  Gateway/HTTPRoute accepted and programmed/resolved and the state
  PVC/PV identities preserved throughout.
- **Independent review and re-validation (2026-09-24):** five
  independent reviews found stale status documentation, test gaps, an
  unguarded Cilium probe policy, and an unusable istiod HPA; all were
  remediated. Targeted live re-checks then passed (`mesh-check` 45/45,
  with all three identity denials backed by AUTHORITATIVE ztunnel
  evidence). A post-reboot `final-state-check` scored **42/43**: the
  run's suite baseline file under `/tmp` had been lost to a host reboot,
  so that one item failed closed and was not recaptured. A **fresh
  baseline-bracketed run** (`state-check` -> `persistence-check` 12/12
  -> `retention-check` 22/22 -> `final-state-check`, with the baseline
  kept outside `/tmp`) then passed **43/43**, closing that gap. Final
  adjudication (2026-09-24): **RELEASE READY**.
- **Post-restart incident (2026-09-25):** after a WSL/Kind component
  restart, `context-check`, `cni-status`, and `mesh-status` passed but
  `rollout-check` failed 25/35. `maops-state-0` was Kubernetes Ready
  yet had no ambient in-Pod listeners on 15001/15006/15008, and one
  gateway Pod was unready without them; recreating only those two Pods
  (storage identity preserved) restored the chain, and the gate then
  passed on merged `main` (`rollout-check` 35/35, `gateway-check` 8/8,
  `smoke` 6/6, `final-state-check` 43/43). The new read-only
  `make ambient-workload-check` now catches missing listeners directly.
  The release gate stays pending until this remediation passes CI and
  a merged-`main` live recheck.

The exact results, dates, Helm revision history, mesh denial-evidence
tiers, and accepted limitations (including host/Docker restart
recovery, which varies in this environment) are in
[`docs/architecture.md`'s "DAY6: live validation record"](docs/architecture.md#day6-live-validation-record);
the independent reviews, final adjudication, and remediation log are
under [`docs/engineering-reviews/day-06-*`](docs/engineering-reviews/).

## Day 6 topology

```
External client (curl -H "Host: maops.local" http://127.0.0.1:18080/)
  |
  v  (kind extraPortMappings: host 127.0.0.1:18080 -> control-plane:30080)
Istio ingress Gateway proxy (Deployment/Service, maops-ingress,
  NodePort 30080, ServiceAccount maops-edge-istio - Istio's own
  deterministic naming, provisioned by its Gateway API deployment
  controller from the Gateway object below)
  ^
  |  gatewayClassName: istio
Gateway: maops-edge (maops-ingress) - HTTP listener :80, hostname maops.local
  ^
  |  parentRefs
HTTPRoute: maops-gateway-route (maops-platform, owned by the Helm chart)
  |  PathPrefix "/" -> service/maops-gateway:8080
  v
maops-gateway ClusterIP Service
  |
  v  HTTP via Kubernetes DNS - BACKEND_HOST=maops-app - NetworkPolicy + AuthorizationPolicy ALLOWED
maops-gateway Deployment (SA: maops-gateway) -- ambient-enrolled, mTLS via ztunnel --
  |
  v
maops-app ClusterIP Service
  |
  v  HTTP via Kubernetes DNS - STATE_HOST=maops-state - NetworkPolicy + AuthorizationPolicy ALLOWED
maops-app Deployment (SA: maops-app) -- ambient-enrolled, mTLS via ztunnel --
  |
  v
maops-state ClusterIP Service      maops-state-headless (governing, clusterIP: None)
  |
  v
maops-state StatefulSet (SA: maops-state) -- ambient-enrolled, mTLS via ztunnel --

Namespace: maops-day6-validation (separate from maops-platform and maops-ingress)
  |
  +--> validation-client probe Pod  --[DENIED]--> maops-gateway, maops-app, maops-state
  |         (Day 5's validation-client -> gateway allow is REMOVED for Day 6 -
  |          the Gateway API path above is now the only way in)
  |
  +--> ServiceAccount: maops-diagnostics (the ONE identity with an API token)
            |
            v
       Role + RoleBinding (namespace-scoped, in maops-platform - unchanged since Day 5)

NetworkPolicy (networking.k8s.io/v1, enforced by Cilium) in maops-platform:
  - default-deny ingress + egress for every Pod
  - + DNS egress, + HBONE (TCP 15008, port-scoped only - ztunnel's hostNetwork
      identity can't be matched by a namespaceSelector peer, see docs/architecture.md)
  - + gateway -> app (never gateway -> state), + app -> state
  - + Istio ingress Gateway (maops-ingress, istio.io/gateway-name=maops-edge) -> gateway only
  (NetworkPolicy controls reachability only - it cannot see workload identity
   inside HBONE; AuthorizationPolicy below is what enforces the identity chain)

PeerAuthentication (maops-platform, namespace-wide): mtls.mode STRICT
AuthorizationPolicy (L4-compatible identity only, no waypoint = no L7):
  - Istio ingress Gateway SA (maops-ingress/maops-edge-istio) -> maops-gateway
  - maops-gateway SA -> maops-app
  - maops-app SA -> maops-state
  (diagnostics/validation-client identity, and gateway -> state, denied - never listed as a principal)
```

Gateway/app/state's own architecture, security context, probes,
Secrets, and PodDisruptionBudgets are **entirely unchanged since Day
4** - see [`docs/architecture.md`](docs/architecture.md) for the full
picture, including the Cilium/Istio responsibility boundary and every
Day 6 design decision.

## Prerequisites

Native Linux/WSL tooling, no `sudo` required:

| Tool | Version used |
|---|---|
| Docker | 29.7.2 (CLI resolving to `/usr/bin/docker`) |
| kubectl | v1.36.3 (Kustomize v5.8.1 bundled) |
| Helm | v4.2.2 (installs Cilium/Istio, and is now also the application deployment tool) |
| kind | v0.32.0 |
| Cilium | 1.20.1 (installed via Helm, configured for Istio ambient coexistence) |
| Gateway API CRDs | v1.6.0 (installed via `kubectl apply`, standard channel) |
| Istio | 1.31.0, ambient profile (installed via Helm: base, istiod, cni, ztunnel) |
| Python | 3 (standard library only - no pip installs needed) |

Kubernetes node image (pinned, same digest as Days 1-5, do not float to
`latest`):

```
kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
```

Cluster name: `maops-k8s-day6` (kubeconfig context: `kind-maops-k8s-day6`)
- a separate, independently-created cluster from Days 1-5's clusters,
none of which Day 6 tooling ever touches. 1 control-plane + 2 worker
nodes, same as Days 3-5 - see `kind/cluster-day6.yaml`, a NEW tracked
file (`kind/cluster.yaml` and `kind/cluster-day5.yaml` are preserved
untouched, not edited in place).

The two substantive differences from `kind/cluster-day5.yaml`:
`extraPortMappings` (host `127.0.0.1:18080` -> control-plane
`30080`, for the Istio ingress Gateway's NodePort) is new;
`networking.disableDefaultCNI: true` is carried forward unchanged -
every node comes up **NotReady** (no pod network at all) until `make
cni-install` runs.

Kubeconfig path is explicit and overridable everywhere - never the
caller's default kubeconfig/context. Defaults to
`~/.kube/maops-k8s-day6.config`; override with
`make KUBECONFIG_PATH=/some/other/path ...`.

## Quick start (static validation - safe to run right now)

```bash
make test              # Docker-free unit tests for repository validation logic (incl. the Day 6 Helm checks)
make version-check     # cross-check VERSION/chart version/appVersion/image tags/Day 6 identities/pinned infra
make manifest-check    # render the frozen Day 5 k8s/base and statically validate it (proves it's untouched)
make helm-lint         # helm lint the Day 6 application chart
make helm-template     # render the Day 6 chart with helm template (pure local render)
make helm-check        # static validation of the rendered Day 6 chart - inventory, versions, security,
                        #   RBAC, NetworkPolicy, PeerAuthentication/AuthorizationPolicy, Gateway API refs,
                        #   plus the k8s/day6 Gateway infrastructure ConfigMap and ambient probe policy
make ci-check           # the single cluster-free sequence GitHub Actions runs - all of the above
```

## Quick start (live sequence)

```bash
make tool-check              # verify docker/kubectl/kind/helm/python3 are present
make image-build              # build all three workload images (gateway, app, state)
make cluster-create           # create the pinned Day 6 kind cluster (kind/cluster-day6.yaml, CNI disabled -
                              #   every node is NotReady until cni-install; that is expected)
make gateway-api-install      # install the Gateway API standard CRDs (pinned v1.6.0)
make cni-install               # install Cilium 1.20.1 via Helm, configured for Istio ambient coexistence
make cni-status                 # READ-ONLY: verify Cilium/kube-proxy are healthy
make context-check            # fail closed unless kubectl is verified against the isolated Day 6 cluster
                              #   (needs all nodes Ready, so it runs after the CNI is up)
make mesh-install               # install Istio ambient: base, istiod (autoscaling disabled), cni, ztunnel
                                 #   (pinned 1.31.0) + the Cilium ambient health-probe exception
make mesh-status                 # READ-ONLY: verify istiod/istio-cni/ztunnel are healthy
make image-load                 # load all three images into every kind node
make storage-bootstrap        # harden new local-path-provisioner directories (root:10001, mode 2770)
make storage-hardening-check  # prove the hardening against a disposable scratch PVC
make namespace-apply          # apply the three Day 6 Namespaces + the diagnostics ServiceAccount
make secret-bootstrap         # create/preserve BOTH runtime Secrets - never printed
make gateway-apply             # apply the Istio Gateway infrastructure ConfigMap + the Gateway object
make deploy                      # helm upgrade --install the Day 6 application chart (never k8s/base)
make ambient-workload-check     # READ-ONLY: every gateway/app/state Pod has ztunnel listeners on 15001/15006/15008
make rollout-check              # real Deployment/Service/EndpointSlice/ConfigMap/security state
make scheduling-check           # real worker-only scheduling + topology spread proof (gateway/app)
make discovery-check            # real Kubernetes DNS + gateway -> app Service HTTP proof
make secret-check                 # real Secret wiring, auth, and non-disclosure proof (both Secrets)
make gateway-check              # GatewayClass/Gateway/HTTPRoute status + real external HTTP routing
make mesh-check                  # ztunnel/istiod/cni health, ambient enrollment, strict mTLS, identity paths
make rbac-check                    # real maops-diagnostics RBAC scope: allowed reads, denied everything else
make networkpolicy-check       # default-deny + explicit-allow, incl. the Day 6 validation-client -> gateway deny
make smoke                         # port-forward service/maops-gateway + real HTTP checks incl. /state
make dependency-check           # gateway liveness vs. dependency-aware readiness proof
make scaling-check              # real scaling 3 -> 4 -> 3, gateway/app, guaranteed restoration
make rolling-update-check      # real rolling update + real kubectl rollout undo rollback, gateway/app
make pdb-check                    # real PodDisruptionBudget/Eviction-API behavior, gateway/app
make state-check                  # maops-state runtime identity, security, and storage binding
make persistence-check          # data survives maops-state-0 Pod deletion/rescheduling
make retention-check             # PVC/PV retention + degraded-but-live behavior across a 1 -> 0 -> 1 cycle
make helm-lifecycle-check      # bounded real helm upgrade + helm rollback proof, state preserved throughout
make final-state-check         # independently prove the cluster is fully restored after all experiments
```

Or run the full authoritative sequence in one shot (recipe-sequential,
so it stays correctly ordered even under `make -j`):

```bash
make day6-check
```

## Version consistency (closes DAY1-REL-I1, extended for Day 6)

The Makefile derives `VERSION := $(shell cat VERSION)` once, and
`GATEWAY_IMAGE`/`APP_IMAGE`/`STATE_IMAGE` all derive from that; `make
version-check` (`scripts/version_check.py`) now runs two independent
sets of checks: (1) the frozen Day 5 check - k8s/base's own rendered
labels/image tags must still equal `0.5.0`, regardless of what the live
`VERSION` file says, proving k8s/base was never advanced; and (2) the
Day 6 checks - the live `VERSION` file, the Helm chart's `version`/
`appVersion`, all three image tags in `values.yaml`, the Day 6 cluster/
kubeconfig/context/release identities (`scripts/kube.py`), and the
pinned infrastructure versions (Cilium, Gateway API CRDs, Istio) as
they actually appear in this Makefile - all must equal `0.6.0`/their
pinned target.

`DAY1-INT-I2` (the hardcoded `/usr/bin/python3.11` interpreter path
used by exec-based checks) remains **ACCEPTED / OPEN** - Day 6 kept
the same digest-pinned Distroless base image for all three workloads.

## Cluster creation

```bash
make cluster-create
```

Idempotent - if `maops-k8s-day6` already exists, this is a no-op
(aside from printing `kubectl get nodes`). A pre-existing Day 6 cluster
is never automatically deleted or recreated. Uses
`kind/cluster-day6.yaml`: 1 control-plane + 2 worker nodes, same
pinned node image digest as Days 1-5, `networking.disableDefaultCNI:
true`, plus the `127.0.0.1:18080 -> 30080` host port mapping for the
Istio ingress Gateway.

## CNI, Gateway API, and service mesh installation

```bash
make gateway-api-install   # kubectl apply the Gateway API standard CRDs (pinned v1.6.0)
make cni-install            # helm upgrade --install cilium cilium/cilium --version 1.20.1,
                              #   configured for Istio ambient coexistence (idempotent)
make cni-status               # READ-ONLY: DaemonSet/operator health, kube-proxy still present
make mesh-install             # helm upgrade --install, in order: istio-base, istiod (profile=ambient),
                                #   istio-cni (profile=ambient), ztunnel (pinned 1.31.0), then the
                                #   Cilium ambient health-probe CiliumClusterwideNetworkPolicy
make mesh-status               # READ-ONLY: istiod/istio-cni/ztunnel health
```

Every node is `NotReady` immediately after `cluster-create` (no pod
network at all) until `cni-install` completes. Cilium is configured for
Istio ambient coexistence (`cni.exclusive=false`,
`socketLB.hostNamespaceOnly=true`, `bpf.masquerade` left at its
false/default value, `envoy.enabled=false` since Day 6 uses no Cilium
L7 feature, and a single, explicitly non-HA operator replica for this
constrained local cluster) - kube-proxy stays enabled, unchanged since
Day 5. Istio runs in **ambient** mode: no sidecars, no waypoint, no
Hubble, no kube-proxy replacement. See
[`docs/architecture.md`](docs/architecture.md) for the full rationale.

## Image build / load

```bash
make image-build   # docker build all three of gateway/, app/, state/ (see IMAGE_BUILD_FLAGS in the Makefile)
make image-load     # kind load docker-image for all three, into every node of maops-k8s-day6
```

Unchanged since Day 4.

## Deploy lifecycle (order matters)

```bash
make image-build              # 1. build all three images (before any cluster work, as in day6-check)
make cluster-create           # 2. create the kind cluster (CNI disabled - nodes NotReady, expected)
make gateway-api-install      # 3. install the Gateway API CRDs (API-server-only, no pod network needed)
make cni-install                 # 4. install Cilium - nodes are NotReady until this completes
make cni-status                   # 5. READ-ONLY: Cilium/kube-proxy healthy
make context-check            # 6. fail closed unless verified against the isolated Day 6 cluster (needs Ready nodes)
make mesh-install                 # 7. install Istio ambient (base, istiod with autoscaling disabled, cni, ztunnel)
                                   #    + the Cilium ambient probe exception
make mesh-status                  # 8. READ-ONLY: istiod/istio-cni/ztunnel healthy
make image-load                 # 9. load all three images
make storage-bootstrap        # 10. harden new PV-backed directory permissions
make storage-hardening-check  # 11. prove the hardening, before any application PVC exists
make namespace-apply          # 12. apply the three Namespaces + diagnostics ServiceAccount
make secret-bootstrap         # 13. create/preserve BOTH runtime Secrets (needs maops-platform to exist)
make gateway-apply             # 14. apply the Istio Gateway infra ConfigMap + the Gateway object
make deploy                       # 15. helm upgrade --install the application chart
make ambient-workload-check     # 16. READ-ONLY: per-Pod ztunnel listeners before rollout validation
```

This is the same order `make day6-check` runs.

`make deploy` applies `charts/maops-kubernetes-platform` in full - 30
rendered objects (3 ConfigMaps, 3 ServiceAccounts, 1 Role, 1
RoleBinding, 2 Deployments, 1 StatefulSet, 4 Services, 2
PodDisruptionBudgets, 8 NetworkPolicies, 1 PeerAuthentication, 3
AuthorizationPolicies, 1 HTTPRoute). It **never** applies `k8s/base`.
Both Secrets are deliberately not part of this - bootstrapped
out-of-band by `scripts/secret_bootstrap.py`, exactly as in Days 2-5.

## Verification

```bash
make ambient-workload-check # READ-ONLY: all 7 gateway/app/state Pods - identity and ambient-enrollment
                              #   metadata, plus ztunnel LISTEN sockets on 15001/15006/15008 in each Pod's
                              #   own network namespace (Ready/annotation alone never pass). Sockets and
                              #   metadata only: redirection, mTLS traffic, and AuthorizationPolicy
                              #   behavior are proven by mesh-check
make rollout-check          # node/version/namespace/replicas/EndpointSlice/ConfigMap/
                              #   security/UID-GID/no-SA-token/Secret-mount (gateway/app)
make scheduling-check       # worker-only scheduling + topology spread (gateway/app)
make discovery-check        # real DNS resolution + gateway -> app Service HTTP
make secret-check              # Secret existence/mount/auth/non-disclosure, end to end, both Secrets
make gateway-check           # GatewayClass/Gateway/HTTPRoute Accepted/Programmed, real external HTTP
                              #   routing through 127.0.0.1:18080 with Host: maops.local, wrong-Host negative
make mesh-check                # ztunnel/istiod/istio-cni health, ambient enrollment (no sidecars), strict
                                 #   mTLS, live AuthorizationPolicy principals, allowed identity paths, and a
                                 #   wrong-identity denial proven by correlated ztunnel log evidence (never a
                                 #   raw TCP connect, which only proves local ztunnel acceptance)
make rbac-check                  # real maops-diagnostics RBAC: allowed pods/services/endpointslices reads,
                                   #   denied Secrets/mutation/delete/scale/cross-namespace/cluster-wide
make networkpolicy-check     # default-deny + explicit-allow: validation-client -> gateway/app/state all
                                   #   DENIED (Day 6 change from Day 5); gateway->app / app->state allowed and
                                   #   gateway->state denied, proven with isolated NON-ambient probe Pods over
                                   #   direct Pod IPs (so ztunnel never intercepts); DNS still works
make smoke                       # bounded port-forward to service/maops-gateway + real HTTP incl. /state
make dependency-check         # app-outage experiment: gateway live=200, ready=503, backend=503,
                                 #   no restart-count increase, then guaranteed restoration to 3/3
make scaling-check            # 3 -> 4 -> 3 scaling proof, gateway/app
make rolling-update-check    # real rolling update + real kubectl rollout undo rollback, gateway/app
make pdb-check                  # PDB/Eviction-API behavior, gateway/app
make state-check                # maops-state 1/1 Ready, worker placement, PVC/PV binding, security baseline
make persistence-check        # maops-state-0 deletion/rescheduling: same PVC/PV, new Pod UID, data intact
make retention-check           # maops-state 1 -> 0 -> 1: PVC/PV retained, degraded-but-live, full recovery
make helm-lifecycle-check    # real helm upgrade (visible config change) + helm rollback, state preserved
make final-state-check       # everything restored to normal/healthy after all of the above
```

`state-check` captures, and `final-state-check` verifies, a run-specific
suite-level `/state` baseline - but only when both receive the same
`DAY6_RUN_ID` and `DAY6_SUITE_BASELINE_PATH` (`make day6-check` passes
them automatically; run standalone, `final-state-check` correctly fails
that one item). The default path is under `/tmp`, which does not
survive a host reboot. For a bracketed run that must survive one, pass
both explicitly and keep the baseline in a private directory outside
the repository, e.g.:

```bash
run_id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
mkdir -p -m 0700 "$HOME/.local/state/maops-k8s-day6"
baseline="$HOME/.local/state/maops-k8s-day6/suite-baseline-$run_id.json"
make DAY6_RUN_ID="$run_id" DAY6_SUITE_BASELINE_PATH="$baseline" state-check
# ... mutating checks, each with the same two variables ...
make DAY6_RUN_ID="$run_id" DAY6_SUITE_BASELINE_PATH="$baseline" final-state-check
```

The baseline file is created with mode 0600 and is never overwritten.

Each of these is a standalone script under `scripts/` - none require a
third-party Python package, and none leave a background process running
afterward (bounded `kubectl port-forward` via `scripts/portforward.py`;
probe-Pod-creating scripts always delete their own short-lived Pods).
Every live script fails closed via `kube.verify_context()` if it is not
actually talking to the verified `kind-maops-k8s-day6` cluster.

## After a host, Docker, or WSL restart

Restart recovery varies in this local Kind-on-Docker-on-WSL
environment (see `docs/architecture.md`'s "DAY6: post-restart ambient
listener incident (2026-09-25)"). Before any other validation, run the
read-only gates in this order:

```bash
make cni-status              # Cilium healthy on every node
make context-check           # verified Day 6 cluster, all nodes Ready
make mesh-status             # istiod/istio-cni/ztunnel healthy (infrastructure only)
make ambient-workload-check  # every application Pod has its ambient metadata and ztunnel listeners
make rollout-check           # full workload rollout validation
```

`mesh-status` checks the mesh infrastructure only. `ambient-workload-check`
adds a per-Pod check that each application Pod has its expected ambient
metadata and ztunnel in-Pod listeners - a necessary condition for ambient
traffic, not proof that traffic is redirected, that HBONE/mTLS succeeds,
or that AuthorizationPolicy behaves correctly (`mesh-check`,
`networkpolicy-check`, `gateway-check`, and `smoke` prove those). If it
reports a Pod with missing listeners, that Pod is the one to investigate - the
2026-09-25 recovery replaced only the affected Pods, never the
infrastructure.

## Port-forward usage

Automated validation picks a free local port itself and cleans the
port-forward process up automatically. For manual, human use targeting
the gateway directly (bypassing the Gateway API path, useful for
debugging independent of routing/mesh):

```bash
kubectl --kubeconfig ~/.kube/maops-k8s-day6.config --context kind-maops-k8s-day6 \
  -n maops-platform port-forward service/maops-gateway 8080:8080
```

Or, to exercise the real external Gateway API path instead (the normal
entry point as of Day 6):

```bash
curl -H "Host: maops.local" http://127.0.0.1:18080/
curl -H "Host: maops.local" http://127.0.0.1:18080/livez
curl -H "Host: maops.local" http://127.0.0.1:18080/state
```

Press `Ctrl-C` in the port-forward terminal to stop it when done.
Port-forwarding still reaches the gateway Pod directly (bypassing the
in-cluster NetworkPolicy entirely, since a port-forward never traverses
the pod network as a policy-visible peer), unaffected by Day 6's
NetworkPolicy/mesh boundaries.

## Cleanup

```bash
make cluster-delete   # kind delete cluster --name maops-k8s-day6 (ONLY this cluster)
```

This never runs `docker system prune`, never touches any earlier day's
cluster, or any other kind cluster/Docker resource.

## Day 6 scope boundaries

Included: everything from Days 1-5 (unchanged: gateway/app/state
architecture, security contexts, probes, Secrets, PodDisruptionBudgets,
persistence/retention, ServiceAccounts, RBAC), packaged as a Helm chart
(`charts/maops-kubernetes-platform`), plus the Kubernetes Gateway API
(Istio as the sole controller), Istio ambient service mesh (strict
mTLS, identity-scoped AuthorizationPolicy, no sidecars, no waypoint),
Cilium reconfigured for ambient coexistence, and a minimal cluster-free
GitHub Actions CI workflow.

Explicitly **not** part of Day 6 (see [`docs/roadmap.md`](docs/roadmap.md)
for when each arrives, or whether it is never introduced): a second,
Ingress-based routing implementation, a Cilium Gateway API controller,
Cilium L7 policy, a waypoint proxy, Hubble, Kiali/Prometheus/Grafana/
Jaeger or any observability/tracing stack, TLS/cert-manager, a cloud
LoadBalancer, `HorizontalPodAutoscaler` (istiod's own chart-default HPA
is explicitly disabled too), Argo Rollouts, Argo CD,
advanced deployment strategies beyond RollingUpdate (Recreate,
Blue-Green, Canary - Day 7), kube-proxy replacement, live-cluster
checks in GitHub Actions, Terraform, Ansible, cloud clusters, and
container registry publishing.

## Repository layout

```
app/                        maops-app: stdlib-only HTTP workload + Dockerfile
gateway/                     maops-gateway: stdlib-only HTTP workload + Dockerfile
state/                       maops-state: stdlib-only HTTP workload + Dockerfile
k8s/base/                    FROZEN Day 5 Kustomize source - 2 Namespaces, 3 ConfigMaps, 4 ServiceAccounts,
                              1 Role, 1 RoleBinding, 2 Deployments, 1 StatefulSet, 4 Services, 2 PDBs,
                              7 NetworkPolicies - never modified or applied by any Day 6 target
k8s/day6/                    Day 6 cluster/platform support objects (Namespaces, diagnostics ServiceAccount,
                              Istio Gateway + its infrastructure ConfigMap, the Cilium ambient probe policy) -
                              applied via kubectl, never templated by the Helm chart
charts/maops-kubernetes-platform/  the Day 6 application Helm chart - the SOLE Day 6 application deployment
                              source (Chart.yaml, values.yaml, values.schema.json, templates/)
kind/cluster.yaml            Day 4's pinned kind config, preserved untouched (no CNI disable)
kind/cluster-day5.yaml       Day 5's pinned kind config - same topology, networking.disableDefaultCNI: true
kind/cluster-day6.yaml       Day 6's pinned kind config - adds the 18080->30080 Istio Gateway host port mapping
scripts/                     dependency-free Python validation + cluster tooling; new for Day 6: helm_check.py,
                              validate_helm_chart.py, validate_gateway_values_configmap.py,
                              validate_cilium_probe_policy.py, gateway_check.py, mesh_check.py, mesh_status.py,
                              helm_lifecycle_check.py, day6_lock.py, ambient_workload_check.py
tests/                       Docker-free unit tests (incl. negative cases and the Helm values-schema tests)
.github/workflows/ci.yml     minimal, cluster-free GitHub Actions CI (new, Day 6)
docs/                        architecture.md, roadmap.md, engineering-reviews/ (Days 1-5 frozen, Day 6 reviews), images/
.claude/                     CLAUDE.md, 5 agents, 4 skills scoped to this project
Makefile                     authoritative local engineering interface
VERSION                      0.6.0 (Day 6 - merged on local kind; not yet tagged or published)
```

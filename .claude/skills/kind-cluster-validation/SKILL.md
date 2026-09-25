---
name: kind-cluster-validation
description: Create/use the project's kind cluster and prove real Kubernetes behavior - node readiness, server version, Deployment/StatefulSet availability, replica/EndpointSlice counts, ConfigMap consumption, Secret mount/auth behavior, worker-only scheduling and topology spread, scaling, rolling update/rollback, PodDisruptionBudget/Eviction behavior, storage hardening, persistence/retention, RBAC scope, NetworkPolicy allow/deny behavior, CNI status, bounded port-forward HTTP checks, dependency-failure behavior, and controller-reconciliation proof via pod deletion. Use whenever asked to validate against a live cluster, debug a failing rollout, or prove the workload actually runs.
---

# kind cluster validation

Real-cluster validation for the maops-kubernetes-platform project's kind
cluster. **Current default (Day 6 / `v0.6.0`, released 2026-09-25 as a
local kind reference platform):**
`maops-k8s-day6` (context `kind-maops-k8s-day6`, kubeconfig
`$HOME/.kube/maops-k8s-day6.config`), 1 control-plane + 2 worker nodes,
`networking.disableDefaultCNI: true` (every node is `NotReady` until
`make cni-install` completes), plus a `127.0.0.1:18080 -> 30080` host
port mapping for the Istio ingress Gateway - a separate,
independently-created cluster from every earlier day's, which this
validation never touches. This is the live-cluster counterpart to
`manifest-validation` (static, for the frozen `k8s/base`) and
`workload-security-validation` (security-specific) - it proves runtime
behavior via `kubectl -o json` and real HTTP calls, never by re-reading
the manifest.

Every script in this list calls `kube.verify_context()` first and fails
closed (never proceeds) if the live cluster's node identity doesn't
actually match the expected cluster - this is checked at runtime, not
assumed from a hardcoded constant.

## Sequence (mirrors the Makefile, current default)

```bash
make image-build                # docker build gateway/, app/, state/ images (before any cluster work)
make cluster-create           # idempotent: kind create cluster --config kind/cluster-day6.yaml (CNI disabled -
                                #   nodes NotReady until cni-install; expected)
make gateway-api-install      # kubectl apply the Gateway API standard CRDs (pinned v1.6.0)
make cni-install               # helm install Cilium 1.20.1, configured for Istio ambient coexistence (idempotent)
make cni-status                 # READ-ONLY: Cilium agent/operator + kube-proxy health
make context-check            # fail closed unless verified against the isolated Day 6 cluster (needs Ready nodes,
                                #   so it runs only after the CNI is up)
make mesh-install                # helm install Istio ambient: base, istiod (autoscaling disabled - no metrics-server),
                                   #   cni, ztunnel (pinned 1.31.0) + the Cilium ambient health-probe exception
make mesh-status                  # READ-ONLY: istiod/istio-cni/ztunnel health
make image-load                 # kind load docker-image for all three, into every node
make storage-bootstrap        # harden local-path-provisioner directory permissions (must run before any PVC)
make storage-hardening-check  # prove the hardening against a disposable scratch PVC
make namespace-apply          # apply the three Namespaces (maops-platform, maops-day6-validation, maops-ingress)
                                 #   + the diagnostics ServiceAccount
make secret-bootstrap         # create/preserve BOTH runtime Secrets - never printed
make gateway-apply             # apply the Istio Gateway infrastructure ConfigMap + the Gateway object
make deploy                       # helm upgrade --install charts/maops-kubernetes-platform - NEVER kubectl apply -k k8s/base
make ambient-workload-check    # scripts/ambient_workload_check.py - READ-ONLY: all 7 app Pods' identity/ambient
                                    #   metadata and ztunnel LISTEN sockets 15001/15006/15008 (never Ready alone;
                                    #   not a traffic/mTLS/AuthorizationPolicy proof - mesh-check is)
make rollout-check             # scripts/cluster_check.py - real Deployment/security/Secret-mount state (gateway/app)
make scheduling-check         # scripts/scheduling_check.py - worker-only scheduling + topology spread (gateway/app)
make discovery-check          # scripts/discovery_check.py - real DNS + gateway -> app Service HTTP proof
make secret-check                # scripts/secret_check.py - both Secrets' wiring/auth/non-disclosure
make gateway-check              # scripts/gateway_check.py - GatewayClass/Gateway/HTTPRoute status + real external
                                   #   HTTP routing through 127.0.0.1:18080 with Host: maops.local, wrong-Host negative
make mesh-check                   # scripts/mesh_check.py - ztunnel/istiod/istio-cni health, ambient enrollment (no
                                    #   sidecars), strict mTLS, live AuthorizationPolicy principals, and a wrong-identity
                                    #   denial proven by correlated ztunnel log evidence (tiered AUTHORITATIVE/CANDIDATE/
                                    #   BEST_EFFORT), never by a raw TCP connect
make rbac-check                    # scripts/rbac_check.py - real maops-diagnostics RBAC: allowed reads, denied everything else
make networkpolicy-check       # scripts/networkpolicy_check.py - default-deny + explicit-allow via isolated
                                    #   NON-ambient probe Pods over direct Pod IPs, incl. the Day 6
                                    #   validation-client -> gateway DENY (changed from Day 5's allow)
make smoke                         # scripts/smoke.py - normal HTTP smoke via service/maops-gateway, incl. /state
make dependency-check           # scripts/dependency_check.py - liveness vs. dependency-aware readiness
make scaling-check              # scripts/scaling_check.py - real scaling 3 -> 4 -> 3, guaranteed restoration
make rolling-update-check     # scripts/rollout_check.py - real rolling update + real kubectl rollout undo
make pdb-check                    # scripts/pdb_check.py - real PDB/Eviction-API behavior, guaranteed restoration
make state-check                  # scripts/state_check.py - maops-state identity, security, storage binding
make persistence-check          # scripts/persistence_check.py - data survives maops-state-0 deletion/rescheduling
make retention-check             # scripts/retention_check.py - PVC/PV retention across a 1 -> 0 -> 1 cycle
make helm-lifecycle-check      # scripts/helm_lifecycle_check.py - bounded real helm upgrade + helm rollback,
                                    #   state preserved throughout (Helm release rollback, never Day 7's strategies)
make final-state-check         # scripts/final_state_check.py - proves everything is restored after all of the above
```

`make day6-check` runs the full sequence (plus `tool-check`, `test`,
`version-check`, `manifest-check`, `helm-lint`, `helm-template`,
`helm-check`) in the required recipe-sequential order and is the
authoritative one-shot validation; `make ci-check` is its cluster-free
static subset. See `docs/architecture.md`'s "DAY6: live validation
record" for how the Day 6 live results were actually obtained (staged
target-by-target against one preserved cluster, with remediation
between stages) and its restart-recovery limitation.

## What each real check proves

`scripts/ambient_workload_check.py` (`make ambient-workload-check`,
READ-ONLY): for each deployed gateway (3), app (3), and state (1) Pod,
verifies Pod/namespace metadata (Running/Pod IP/not terminating, the
workload's own ServiceAccount, ambient-enrollment metadata: namespace
label, no opt-out, redirection annotation, no sidecar) and that TCP
15001, 15006, and 15008 are LISTEN sockets in that Pod's own network
namespace (`/proc/net/tcp{,6}` via `kubectl exec`). Sockets and
metadata only: it does not prove redirection rules, HBONE/mTLS
traffic, or AuthorizationPolicy behavior - `mesh-check` and the other
live traffic checks remain the evidence for those. Fails closed on missing Pods
or listeners, kubectl/API errors, timeouts, or malformed output, naming
the exact Pod and ports. Added after the 2026-09-25 incident in which a
Kubernetes-Ready `maops-state-0` had no listeners while `mesh-status`
passed. After any host/Docker/WSL restart, run `cni-status`,
`context-check`, `mesh-status`, `ambient-workload-check`, then
`rollout-check`.

`scripts/cluster_check.py` (`make rollout-check`), for **gateway and
app** (StatefulSet-specific proofs for `state` live in
`state_check.py`, below):

1. Cluster has exactly 3 nodes (1 control-plane + 2 workers), all Ready.
2. `kubectl version -o json` server `gitVersion` matches the pinned
   node's Kubernetes version exactly.
3. Namespace `maops-platform` exists.
4-9. Each Deployment reaches `status.conditions[Available]=True`,
   `spec.replicas == 3`, `status.readyReplicas == 3` (bounded wait, not
   an instant check).
10-11. All three pods per workload individually report `Ready=True`.
12. Each Service is `ClusterIP`.
13-14. Each Service's `discovery.k8s.io/v1` EndpointSlice has exactly 3
    ready endpoints (`scripts/endpointslice.py` - **not** the legacy
    `v1 Endpoints` API).
15-16. Each ConfigMap's value matches what `kubectl exec` (via the
    Python interpreter directly - the distroless image has no shell)
    reads from the live process's environment.
17-18. Live pod's actual UID/GID and the API server's recorded
    `securityContext` fields, for both workloads.
19. Neither pod mounts a Kubernetes API ServiceAccount token
    (`automountServiceAccountToken: false` on both the ServiceAccount
    and pod level).
20. Both pods have the expected Secret volume/mount
    (`internal-auth` -> `/var/run/secrets/maops`, read-only).

`scripts/scheduling_check.py` (`make scheduling-check`): identifies the
control-plane and worker nodes dynamically via the
`node-role.kubernetes.io/control-plane` label (never a hardcoded node
name), then proves for both workloads: 3/3 Pods Ready, zero Pods on the
control-plane, both workers host at least one Pod, and worker
replica-count skew <= 1 (a 2/1 or 1/2 split, never 3/0).

`scripts/discovery_check.py` (`make discovery-check`): from a live
gateway Pod, a real `socket.getaddrinfo('maops-app', ...)` call proves
DNS resolution succeeds, then a real port-forwarded HTTP call to
`/backend` proves the resolved address actually routes through the
Service to a live app Pod.

`scripts/secret_check.py` (`make secret-check`): both Secrets
(`maops-internal-auth`, `maops-state-auth`) exist with non-empty keys;
all mounting pods can read them read-only; the full authenticated
`gateway -> app -> state` chain succeeds; a direct, unauthenticated
call to either internal endpoint is rejected with exactly `HTTP 403`;
neither workload's normal responses, `/config`, nor logs ever contain
either token; no tracked repository file contains a live generated
token.

`scripts/rbac_check.py` (`make rbac-check`): from inside a probe Pod
running as `maops-diagnostics` with its real mounted token, direct
HTTPS calls to `https://kubernetes.default.svc` prove
`pods`/`services`/`endpointslices` reads in `maops-platform` return
`200`, while `secrets` reads, a Deployment `DELETE`, a Deployment
`/scale` `PATCH`, a cross-namespace Pod read, and a cluster-scoped Node
read all return exactly `403 Forbidden`.

`scripts/networkpolicy_check.py` (`make networkpolicy-check`): real
in-cluster TCP connection attempts (never only a port-forward, which
never traverses the pod network as a policy-visible peer). As of Day
6 the application-port assertions (`gateway -> app` and `app -> state`
allowed, `gateway -> state` blocked) use four temporary probe Pods in
`maops-platform` labelled `istio.io/dataplane-mode: none` (opted out
of ambient, so ztunnel never intercepts them), each verified isolated
before use, connecting to each other's direct Pod IPs - a raw connect
from a real ambient Pod would only prove local ztunnel acceptance. DNS
is still checked from the real Pods. `validation-client -> gateway`/
`-> app`/`-> state` are ALL blocked (Day 5's `validation-client ->
gateway` allow is removed - the Gateway API path is the only way in),
using a short-lived `validation-client` probe Pod in
`maops-day6-validation`. Every probe Pod is always deleted and
verified gone.

`scripts/cni_check.py` (`make cni-status`, READ-ONLY): every node is
`Ready`, the Cilium agent DaemonSet has exactly one Ready Pod per node,
the Cilium operator has at least one available replica, and the
kube-proxy DaemonSet still has Ready Pods (kube-proxy remains enabled -
Cilium is adopted here only for NetworkPolicy enforcement).

`scripts/storage_hardening_check.py` (`make storage-hardening-check`):
against a disposable scratch PVC (never `maops-state`'s own claim), a
Pod at the correct UID/GID + `fsGroup` can write/read, and a Pod at an
unrelated UID/GID (no `fsGroup`) gets `EACCES` - proving the
provisioning-root hardening (`root:10001`, mode `2770`) actually does
access-control work.

`scripts/smoke.py` (`make smoke`): opens a bounded, auto-cleaned-up
port-forward to `service/maops-gateway` and performs real HTTP GETs/PUT
against `/`, `/livez`, `/readyz`, `/config`, `/backend`, `/state`,
asserting real response semantics (not just HTTP 200 + valid JSON).

`scripts/dependency_check.py` (`make dependency-check`): scales
`maops-app` to 0 replicas (leaving `maops-gateway` untouched), proves
gateway `/livez` stays `200`, `/readyz` becomes `503`, `/backend`
becomes a controlled `503`, and gateway restart counts don't increase -
then restores `maops-app` to 3 replicas in a guaranteed `finally` path.

`scripts/scaling_check.py` (`make scaling-check`): for gateway and app
independently, baseline 3/3 -> `kubectl scale --replicas=4` -> proves
Deployment/Pods/EndpointSlice all agree on 4 and the Service stays
functional -> restores to 3 in a guaranteed `finally` path,
independently re-verified. `HorizontalPodAutoscaler` is out of scope;
`maops-state` is never scaled beyond its single replica.

`scripts/rollout_check.py` (`make rolling-update-check`): for gateway
and app independently, patches a temporary, uniquely-marked
`spec.template.metadata.annotations` key (never a fake image tag) to
trigger a real new ReplicaSet/rolling update, proves Pods are actually
replaced (UID-set comparison) and the rollout completes, then performs
a **real** `kubectl rollout undo` and proves full restoration. Uses
`_wait_exact_pod_count()` to poll past the termination race between
"rollout reports done" and "old Pods actually deleted" before trusting
any Pod-set snapshot.

`scripts/pdb_check.py` (`make pdb-check`): for gateway and app
independently, proves the normal healthy PDB status
(`desiredHealthy=2`, `disruptionsAllowed=1`), scales 3 -> 2 (proving the
PDB does **not** block ordinary scaling) and confirms
`disruptionsAllowed=0`, then submits a real `policy/v1 Eviction`
against a Pod filtered to be genuinely `Ready`/non-terminating, expects
`TooManyRequests`, and restores to 3 replicas in a guaranteed `finally`
path.

`scripts/state_check.py` (`make state-check`): `maops-state` 1/1 Ready,
worker-only placement, PVC/PV `Bound`, and the same security-baseline
proofs (UID/GID, no token mount) as gateway/app.

`scripts/persistence_check.py` (`make persistence-check`): writes a
unique marker through the real `gateway -> app -> state` chain,
deletes only `maops-state-0`, proves the StatefulSet controller
replaces it with the same PVC/PV but a genuinely new Pod UID, and the
marker reads back unchanged.

`scripts/retention_check.py` (`make retention-check`): scales
`maops-state` 1 -> 0 -> 1, proving the PVC/PV remain `Bound` throughout
(degraded-but-live app/gateway behavior), and full recovery with the
pre-outage marker intact.

`scripts/final_state_check.py` (`make final-state-check`): after all of
the above, independently re-proves the cluster is back at its normal
healthy baseline for all three workloads (scheduling/skew, both PDBs
healthy, no leftover rollout-test annotation, both Secrets still valid,
no leaked `kubectl port-forward` process, no leaked mesh-probe
namespace or NetworkPolicy probe Pod), and that every other-day
cluster it can detect (`OTHER_DAY_CLUSTERS` in the script) is checked
for continued *registration* - not started or assumed healthy if it
isn't running (see "Legacy/frozen earlier-day clusters" below).

It also verifies the suite-level `/state` value was restored to the
baseline `state-check` captured - but only when both targets receive
the same `DAY6_RUN_ID`/`DAY6_SUITE_BASELINE_PATH` (automatic inside
`make day6-check`; pass both explicitly on every command line
otherwise). The default baseline path is under `/tmp`, which does not
survive a host reboot; for runs that must, use a private (0700)
directory outside the repository. Never recapture a baseline after the
mutating checks to make this item pass.

`scripts/reconcile_check.py` (`make controller-check` - bonus, not part
of `day6-check`): records all three `maops-app` pod UIDs, deletes
exactly one pod, waits for the Deployment to reconcile back to 3/3
Ready, confirms a genuinely new UID appeared while the other two
survived.

## Legacy/frozen earlier-day clusters (explicit compatibility path, not the default)

Days 1-5 each have their own frozen, independently-created kind
cluster/context (`maops-k8s-day1`/`kind-maops-k8s-day1` through
`maops-k8s-day5`/`kind-maops-k8s-day5`, Day 4 using `kind/cluster.yaml`
and Day 5 using `kind/cluster-day5.yaml`). These are **not** the
current validation target - only `maops-k8s-day6` is. As documented in
`docs/engineering-reviews/day-05-post-release-verification.md`, running
several multi-node kind clusters concurrently can exceed a WSL2 host's
available capacity, so earlier clusters should be stopped (not
necessarily deleted) rather than left running alongside the current
one.

- **Do not start an earlier-day cluster** as part of ordinary Day 6
  validation, and never assume one is already running.
- Only start (or inspect, if already running) an earlier-day cluster
  for an explicit, scoped investigation of that specific day's
  behavior - e.g. reproducing a historical finding referenced in
  `docs/engineering-reviews/day-0N-*`. Treat this as a deliberate,
  explicitly-labeled legacy path, not the default workflow, and stop it
  again afterward rather than leaving it running alongside Day 6's
  cluster.
- Never run Day 6's authoritative suite while relying on an earlier-day
  cluster also being up - the two are independent and neither's
  tooling touches the other's cluster/kubeconfig/context.

## Debugging a failure

Don't guess - pull real evidence, against the current default context:

```bash
kubectl --context kind-maops-k8s-day6 -n maops-platform describe deployment/maops-gateway
kubectl --context kind-maops-k8s-day6 -n maops-platform describe deployment/maops-app
kubectl --context kind-maops-k8s-day6 -n maops-platform describe statefulset/maops-state
kubectl --context kind-maops-k8s-day6 -n maops-platform describe pod <name>
kubectl --context kind-maops-k8s-day6 -n maops-platform get events --sort-by=.lastTimestamp
kubectl --context kind-maops-k8s-day6 -n maops-platform logs <pod>
kubectl --context kind-maops-k8s-day6 -n maops-platform get endpointslices -l kubernetes.io/service-name=maops-app
kubectl --context kind-maops-k8s-day6 -n maops-platform get poddisruptionbudget
kubectl --context kind-maops-k8s-day6 -n maops-platform get networkpolicy
kubectl --context kind-maops-k8s-day6 get nodes -o wide
```

Fix the root cause (manifest, image, probe timing, resource sizing,
Secret bootstrap ordering, a genuine termination race, host resource
pressure on Cilium) and re-run only the affected `make` target - don't
re-run the whole sequence unnecessarily, and don't loosen a script's
assertions to force a pass.

## Cleanup discipline

- `make cluster-delete` deletes **only** the current default cluster
  (`maops-k8s-day6`) - never run `docker system prune` or delete
  unrelated clusters/resources, including an earlier day's cluster if
  it happens to be running.
- After any manual `kubectl port-forward` debugging session, confirm
  nothing was left running: `ps aux | grep port-forward`.
- Leave the cluster running after validation unless there's a concrete
  safety reason to tear it down - it's expected to be available for
  independent review. If host resource pressure requires freeing
  capacity, stop a superseded earlier-day cluster before stopping the
  current one.

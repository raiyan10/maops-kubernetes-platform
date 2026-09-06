# Architecture - Day 3 (v0.3.0, in development)

Day 1 (`v0.1.0`) established a single-workload Kubernetes foundation and
Day 2 (`v0.2.0`) added a second workload, real service discovery, and a
runtime Secret - both released and frozen; see the historical evidence
under `docs/engineering-reviews/day-01-*` and `day-02-*`. Day 3 keeps
that entire architecture unchanged and adds a real multi-node cluster,
topology-aware scheduling, scaling, rolling-update/rollback behavior,
and a PodDisruptionBudget per workload.

## Control flow

```
Docker
   |
   v
kind (1 control-plane + 2 worker nodes, pinned kindest/node:v1.36.1)
   |
   v
Kubernetes API server (v1.36.1)
   |
   v
namespace: maops-platform
   |
   +--> ConfigMap: maops-gateway-config     +--> ConfigMap: maops-app-config
   |                                        |
   v                                        v
Deployment: maops-gateway (3 replicas)      Deployment: maops-app (3 replicas)
   | RollingUpdate(maxUnavailable=1,        | RollingUpdate(maxUnavailable=1,
   |   maxSurge=1), worker-only affinity,   |   maxSurge=1), worker-only affinity,
   | topologySpreadConstraints              | topologySpreadConstraints
   |                                        |
   +--> ReplicaSet --> Pod, Pod, Pod        +--> ReplicaSet --> Pod, Pod, Pod
   |    (spread across both workers)        |    (spread across both workers)
   |                                        |
   +--> PodDisruptionBudget                 +--> PodDisruptionBudget
   |    (minAvailable: 2)                   |    (minAvailable: 2)
   |                                        |
   v                                        v
Service: maops-gateway (ClusterIP)          Service: maops-app (ClusterIP)
   |                                            ^
   | kubectl port-forward (bounded, local-only) |
   v                                            |
localhost:<local-port>                          |
                                                 |
   gateway container -----------------------------
   (HTTP via Kubernetes DNS: BACKEND_HOST=maops-app)

Secret: maops-internal-auth (bootstrapped out-of-band, never committed)
   |
   +--> mounted read-only into BOTH Deployments at
        /var/run/secrets/maops (volume "internal-auth")
```

Only `maops-gateway` is reached from outside the cluster (via
`kubectl port-forward`) in the normal architecture; `maops-app` is
reached exclusively through the gateway, over the `maops-app` Service.
No NodePort, LoadBalancer, or Ingress exists - see
[Why port-forward instead of NodePort/Ingress](#why-port-forward-instead-of-nodeportingress)
below (carried forward unchanged from Day 1/2's rationale).

## Multi-node kind topology: why 1 control-plane + 2 workers

Day 1 and Day 2 both ran single-node kind clusters, which cannot
meaningfully demonstrate two things Day 3 introduces: excluding
workloads from the control-plane node, and spreading replicas across
*multiple* worker nodes. `kind/cluster.yaml` now provisions three nodes
- one control-plane, two workers - each pinned to the same
`kindest/node:v1.36.1` digest as Day 1/2. Two workers (not three or
more) is the minimum topology that makes a *skew* meaningful at all:
with only one worker, "spread" is trivially satisfied; with two, 3
replicas genuinely have to choose between a 2/1 or 1/2 split, which is
exactly the scheduling behavior this stage sets out to prove.

## Why three replicas

Day 1/2 ran 2 replicas per workload. Day 3 moves to 3 for two reasons
that both need at least 3 to be demonstrable: `PodDisruptionBudget
minAvailable: 2` needs at least one replica of slack above the budget
floor to have any `disruptionsAllowed` at all in the normal healthy
state (2 replicas with `minAvailable: 2` would permanently allow zero
disruptions), and topology spread across 2 workers only produces an
interesting (non-trivial) skew calculation with an odd replica count
that doesn't divide evenly - 3 over 2 workers is the smallest such case.

## Why topologySpreadConstraints, not hard pod anti-affinity

Each Deployment carries exactly one `topologySpreadConstraints` entry:
`maxSkew: 1`, `topologyKey: kubernetes.io/hostname`,
`whenUnsatisfiable: DoNotSchedule`, with a `labelSelector` scoped to
that workload's own `app.kubernetes.io/component` value only (never the
other workload's Pods - gateway's spread constraint never counts app
Pods, and vice versa). `maxSkew: 1` is deliberately the loosest value
that still rejects a bad placement: with 3 replicas over 2 worker
nodes, the only two distributions that ever satisfy `maxSkew: 1` are
2/1 and 1/2 - a 3/0 pile-up is rejected, but neither exact split is
required, because neither is achievable in every case and Kubernetes
would otherwise leave a Pod permanently `Pending`.

Hard pod anti-affinity (`requiredDuringSchedulingIgnoredDuringExecution`
pod anti-affinity, as opposed to node affinity) is deliberately **not**
used here. A hard anti-affinity rule demanding "no two replicas of this
workload on the same node" is mathematically impossible to satisfy for
3 replicas across only 2 worker nodes - the third Pod would sit
`Pending` forever. `topologySpreadConstraints` with `maxSkew: 1` is the
correct tool for "spread as evenly as possible, but stay schedulable
with an odd replica count over an even node count."

## Why worker-only required node affinity, not just the default taint

Both Deployments carry a `requiredDuringSchedulingIgnoredDuringExecution`
node affinity rule requiring `node-role.kubernetes.io/control-plane`
`DoesNotExist`. kind's control-plane node already carries a
`NoSchedule` taint by default, which alone would keep ordinary
workloads off it - but relying solely on that taint means a workload
would silently become schedulable onto the control-plane the moment
that taint were ever removed or overridden (e.g. by a future day's
change, or a manual `kubectl taint` during debugging). The explicit
required node affinity is a second, independent guarantee that holds
regardless of the taint's state - `scripts/scheduling_check.py` proves
live that zero gateway/app Pods ever land on the control-plane node,
identified dynamically via the `node-role.kubernetes.io/control-plane`
label rather than a hardcoded node name.

## RollingUpdate tuning: maxUnavailable, maxSurge, minReadySeconds, progressDeadlineSeconds, revisionHistoryLimit

Both Deployments pin an explicit `RollingUpdate` strategy rather than
relying on Kubernetes' current defaults, which happen to match today
but are not a contract a later Kubernetes version is bound to:

- **`maxUnavailable: 1`** - at most one replica may be unavailable
  during a rollout, so 2 of 3 stay serving throughout.
- **`maxSurge: 1`** - at most one extra replica may be created above
  the desired count during a rollout, bounding the burst above 3.
- **`minReadySeconds: 5`** - a new Pod must stay Ready for 5 seconds
  before it counts toward availability, guarding against a
  flapping/crash-looping replacement being counted as "successfully
  rolled out" the instant its readiness probe first passes.
  **(DAY3-ARCH-I1)** This is a Deployment-controller-internal accounting
  delay, not a traffic gate: `readinessProbe` success alone is what
  controls Service/EndpointSlice traffic admission (a Pod is added as a
  ready EndpointSlice endpoint - and can receive Service traffic - the
  moment its readiness probe first passes). `minReadySeconds` only
  affects when the *Deployment controller* counts that already-Ready Pod
  as "Available" for rollout-progression purposes (advancing
  `maxUnavailable`/`maxSurge` bookkeeping and the `Available` condition)
  - it never delays or withholds Service traffic from the Pod itself.
- **`progressDeadlineSeconds: 120`** - bounds how long the rollout
  controller waits for progress before marking the Deployment's
  `Progressing` condition `False`; a genuinely stuck rollout (e.g. a
  bad image that never becomes Ready) is reported as a real failure
  within 2 minutes rather than hanging indefinitely.
- **`revisionHistoryLimit: 5`** - keeps the last 5 ReplicaSets around,
  so `kubectl rollout undo` has real revision history to roll back to.

## The temporary Pod-template annotation rollout technique

Day 3 must trigger a *real* Deployment revision/rolling update without
inventing a fake image version (`0.3.0-test`, `0.3.0-rollout`, etc. are
explicitly forbidden - see `docs/roadmap.md`'s scope boundaries).
`scripts/rollout_check.py` does this by patching a harmless, uniquely-
marked, non-secret annotation under `spec.template.metadata.annotations`
(key `maops.io/rollout-test`, value a random per-run marker) via
`kubectl patch --type=merge`. Changing anything under
`spec.template` - even just an annotation - changes the Pod template
hash Kubernetes uses to decide whether a new ReplicaSet is needed, so
this triggers a completely real rolling update: a new ReplicaSet is
created, Pods are actually replaced (proven by comparing Pod UID sets
before/after, not just counting them), and `kubectl rollout status`
reports genuine progress and completion. The annotation carries no
functional meaning to either application - it exists purely to change
the template.

## Real rollback via `kubectl rollout undo`

After the temporary rollout completes, `scripts/rollout_check.py`
performs a **real** rollback with `kubectl rollout undo
deployment/<name>` - not a manual re-apply of the old manifest. This
proves: the rollback command itself succeeds, the Deployment returns to
`Available` and 3/3 Ready, the previous Pod template (without the
temporary annotation) is restored, the workload receives a genuinely
new replacement set of Pods (UID sets compared, not assumed), the
Service remains functional, and the EndpointSlice returns to 3 ready
endpoints. The live Deployment template after rollback is verified to
match the pre-experiment baseline (same image tag), so at the end of
the experiment the cluster's actual state reconciles with what's
committed in `k8s/base` - the temporary annotation leaves no trace.

### Termination-race guard on both the forward rollout and the rollback

An early implementation of `scripts/rollout_check.py` took an immediate
snapshot of matching Pods right after `kubectl rollout status` reported
success, and immediately compared UID sets. In practice this raced
ahead of the *old* ReplicaSet's outgoing Pods actually being deleted -
`readyReplicas` reaching the target count and `rollout status`
completing both race slightly ahead of the old Pods' termination
finishing, so a naive one-shot Pod list could transiently show more
than 3 Pods (old-plus-new together). The fix (`_wait_exact_pod_count()`)
polls until the live Pod set - by UID - settles to exactly the expected
count before it's trusted as the comparison snapshot, on both the
forward-rollout and the post-rollback checks. `scripts/final_state_check.py`
applies the same settling wait before its own final Pod/EndpointSlice
snapshot, for the same reason.

## Scaling behavior (3 -> 4 -> 3, no HPA)

`scripts/scaling_check.py` proves real, manual scaling for both
workloads independently: baseline 3/3 Ready with a 3-endpoint
EndpointSlice, `kubectl scale --replicas=4`, then proof that the
Deployment, live Pods, and EndpointSlice all agree on exactly 4, and
that the Service remains functional throughout (for app scaling: the
gateway's `/backend` proxy path; for gateway scaling: the gateway's own
HTTP). The workload is then restored to 3 in a guaranteed `finally`
path, independently re-verified. `HorizontalPodAutoscaler` is
explicitly out of scope for Day 3 - this is deliberate, manual scaling,
not automatic.

## PodDisruptionBudget: what it does and does not protect against

Each workload has its own `PodDisruptionBudget` (`maops-gateway-pdb`,
`maops-app-pdb`), `minAvailable: 2`, selector scoped to that workload's
own component only (never satisfiable by the other workload's Pods -
statically checked). In the normal healthy state (3/3 Ready),
`status.desiredHealthy` is `2` (the `minAvailable` value) and
`status.disruptionsAllowed` is `1` (`currentHealthy - desiredHealthy`).
`scripts/pdb_check.py` proves this live, then scales the Deployment
down to 2 replicas (proving **the PDB does not block ordinary Deployment
scaling** - it only governs voluntary Eviction-API disruptions) and
confirms `disruptionsAllowed` drops to `0`.

At that point, a real `policy/v1 Eviction` object is submitted via
`kubectl create --raw /api/v1/namespaces/<ns>/pods/<pod>/eviction -f -`
(never `kubectl delete pod`, which bypasses the PDB entirely) against
one dynamically-selected Pod. The expected outcome - an HTTP 429
`TooManyRequests` rejection citing the disruption budget - is classified
from the actual API response text, never assumed from a bare non-zero
exit code, and the target Pod's continued presence (same UID, no
`deletionTimestamp`) is independently confirmed. If the eviction were
to unexpectedly succeed, that is a hard failure of the check, not a
footnote. The workload is then restored to 3 replicas in a guaranteed
`finally` path.

### A defect this proof caught: eviction victim selection

An early implementation selected the eviction target as simply the
first Pod (sorted by name) returned for the workload's label selector.
Once, against the live cluster, this picked a Pod that was still
`Terminating` from the immediately-preceding 3 -> 2 scale-down - and the
Eviction API always permits evicting a Pod the PDB doesn't count as
healthy (a terminating or not-Ready Pod doesn't reduce the *healthy*
count), so the eviction "succeeded" for a reason that had nothing to do
with the PDB, and the check would have wrongly reported the PDB as
failing to protect the workload. The fix filters candidate Pods to only
those that are actually `Ready` with no `deletionTimestamp` before
selecting a victim - the eviction target must be a Pod the PDB
genuinely protects, or the result doesn't test what it claims to.

### What a PDB does NOT do

This is deliberately taught, not just implemented: the PDB never
prevents ordinary Deployment scaling (proven directly), and it never
prevents every involuntary failure - it governs only voluntary
disruptions initiated through the Eviction API (node drains, cluster
autoscaler downscales, and manual evictions). A node crash, an OOM
kill, or `kubectl delete pod --grace-period=0 --force` are involuntary
and are not mediated by the Eviction API at all, so a PDB provides no
protection against them.

**(DAY3-ARCH-I2)** The same is true of an ordinary Deployment
rolling-update replacement: when the Deployment controller scales down
the old ReplicaSet during a rolling update, it deletes those Pods
directly - it does **not** go through the `policy/v1 Eviction` API, so
the PDB never mediates or constrains it either. `RollingUpdate`'s own
`maxUnavailable`/`maxSurge` and the PDB's `minAvailable` are two
independent, separately-enforced availability mechanisms - this project
happens to configure both to leave an effective floor of 2 of 3 Pods
available, but that is a deliberate matching choice, not a shared
enforcement path. Proof: `scripts/rollout_check.py`'s rolling update
never touches or waits on either PDB, and `scripts/pdb_check.py`'s
Eviction-API experiment never triggers a rolling update.

## Two Deployments, two ownership chains

`maops-gateway` and `maops-app` are independent Deployments, each owning
its own ReplicaSet, each owning its own three Pods - the same
Deployment -> ReplicaSet -> Pod ownership chain Day 1 established, now
three-wide and spread across two worker nodes. They share a label
scheme (`app.kubernetes.io/name=maops-kubernetes-platform`,
`app.kubernetes.io/instance=maops-kubernetes-platform-day3`) but are
disambiguated by `app.kubernetes.io/component` (`gateway` or `app`),
which is also what each Service's `spec.selector`, each
`topologySpreadConstraints[].labelSelector`, and each
`PodDisruptionBudget`'s selector key off - this is what makes isolation
correct end to end: none of a workload's selectors can ever be
satisfied by the other workload's Pods (statically asserted for all
three selector types, not just Services).

## Service discovery: gateway -> app via Kubernetes DNS

Unchanged since Day 2. The gateway never talks to a Pod IP, a Pod name,
a ReplicaSet name, a node IP, or a hardcoded ClusterIP. Its
`BACKEND_HOST` ConfigMap value is literally the app Service's name,
`maops-app` - Kubernetes' cluster DNS resolves that to the Service's
stable ClusterIP, which kube-proxy then load-balances across whichever
app Pods are currently Ready (now potentially spread across either
worker node). `scripts/discovery_check.py` proves this live: a real
`socket.getaddrinfo('maops-app', ...)` call from inside a running
gateway Pod, then a real HTTP round trip through the Service.

## Service stable networking (EndpointSlice, not legacy Endpoints)

Unchanged since Day 2: authoritative backend-readiness evidence
(`scripts/cluster_check.py`, `scripts/scaling_check.py`,
`scripts/rollout_check.py`, `scripts/endpointslice.py`) queries
`discovery.k8s.io/v1 EndpointSlice` objects and counts addresses whose
`conditions.ready` is explicitly `true`, never the legacy `v1 Endpoints`
API.

## ConfigMap flow

Unchanged shape since Day 2 - `maops-gateway-config`
(`BACKEND_HOST`/`BACKEND_PORT`/`BACKEND_TIMEOUT_SECONDS` plus
display/environment keys) and `maops-app-config` (display/environment
keys only). `APP_ENVIRONMENT`/`APP_MESSAGE` values now read
`day3-scaling-rollouts-availability` / "(Day 3)" to reflect the current
stage; neither ConfigMap ever holds the internal auth token or anything
secret-like.

## Secret lifecycle

Unchanged since Day 2. `maops-internal-auth` (key `internal-token`) is
deliberately **not** part of `k8s/base`; `scripts/secret_bootstrap.py`
bootstraps it out-of-band against the explicit `kind-maops-k8s-day3`
context and `maops-platform` namespace, generating a fresh
cryptographically-strong token only if the Secret doesn't already
exist, and preserving (never silently rotating) an existing one. Both
workloads mount it identically read-only at
`/var/run/secrets/maops/internal-token`.

## App-level internal auth

Unchanged since Day 2. `maops-app`'s `GET /internal/info` requires the
`X-MAOPS-Internal-Token` header, compared with `hmac.compare_digest()`;
missing/wrong token -> `HTTP 403`; correct token -> `HTTP 200` with a
safe payload, never the token itself.

## Dependency-aware gateway readiness vs. local-only gateway liveness

Unchanged since Day 2, re-verified against the Day 3 3-replica baseline
by `scripts/dependency_check.py`: `GET /livez` is local-process-only
(never depends on the app backend) and is what both the `startupProbe`
and `livenessProbe` use; `GET /readyz` performs a bounded HTTP check
against `http://maops-app:8080/readyz` and is what the `readinessProbe`
uses. Scaling `maops-app` to 0 (leaving `maops-gateway`'s replica count
untouched) proves gateway `/livez` stays `200`, `/readyz` becomes
`503`, `/backend` becomes a controlled `503` (no traceback, no token),
and gateway container restart counts don't increase - before restoring
`maops-app` to 3 replicas in a guaranteed `finally` path.

## Timeout hierarchy

Unchanged since Day 2: every gateway -> app HTTP call is bounded by
`BACKEND_TIMEOUT_SECONDS` (default `3` seconds); the gateway's own
`readinessProbe.timeoutSeconds` (`5`) stays comfortably above that.

## Resources and security baseline

Unchanged from Day 1/2, applied identically to both workloads:
requests `cpu: 50m` / `memory: 32Mi`, limits `cpu: 250m` /
`memory: 128Mi`; `runAsNonRoot: true`, `runAsUser`/`runAsGroup: 10001`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`,
`readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`,
`automountServiceAccountToken: false`. Day 3 does not weaken any of
this to make scaling/rollout/scheduling easier to demonstrate.

## No persistence yet

Neither workload writes to disk. Real persistence (PVC/StatefulSet) is
Day 4 scope, not Day 3's.

## Why RBAC/NetworkPolicy remain deferred

Unchanged rationale from Day 2: both workloads still run with no
ServiceAccount beyond the default (`automountServiceAccountToken:
false`) and no NetworkPolicy. Day 5 (`v0.5.0`) introduces both together.

## Why Ingress/Gateway API remain deferred

Day 3 still reaches `maops-gateway` only via `kubectl port-forward` -
see
[Why port-forward instead of NodePort/Ingress](#why-port-forward-instead-of-nodeportingress)
below. Cluster-external routing (Ingress and Gateway API, compared
against each other) is Day 6 scope.

## Why Service Mesh and advanced deployment strategies remain deferred

Day 3 implements exactly one deployment strategy - `RollingUpdate` -
tuned explicitly. `Recreate`, Blue/Green, and Canary strategy
demonstrations (compared against RollingUpdate) and any service mesh
are Day 7 scope; neither is implemented, referenced as available, or
claimed to exist in Day 3.

## Why port-forward instead of NodePort/Ingress

Carried forward unchanged from Day 1/2's rationale. `kubectl
port-forward` to `service/maops-gateway` gives real HTTP access for both
human use and automated validation with zero additional cluster surface
area: no NodePort opened on any node, no cloud LoadBalancer to
provision, no Ingress controller to install and configure. It's
inherently bounded to the local machine and the lifetime of the
`kubectl` process. (The one deliberate exception: security/scaling/
rollout validation scripts temporarily port-forward directly to
`service/maops-app` or a specific gateway Pod - never exposed
externally, only ever local, bounded, and auto-cleaned-up - to prove
behavior independent of the normal gateway-fronted path.)

# Architecture - Day 6 (v0.6.0, release ready as a local kind reference platform - merged through PR #7, not yet tagged or published)

Day 1 (`v0.1.0`) established a single-workload Kubernetes foundation,
Day 2 (`v0.2.0`) added a second workload, real service discovery, and a
runtime Secret, Day 3 (`v0.3.0`) added a real multi-node cluster,
topology-aware scheduling, scaling, rolling-update/rollback behavior,
and a PodDisruptionBudget per workload, Day 4 (`v0.4.0`) added a third
workload, `maops-state` - a single-replica StatefulSet with a
PVC-backed `/data` volume, proving real Kubernetes persistence - and
Day 5 (`v0.5.0`) added identity and network boundaries around that
unchanged architecture: a purpose-built ServiceAccount per workload, a
namespace-scoped Role/RoleBinding for the one identity that exercises
real API authorization (`maops-diagnostics`), and standard
`networking.k8s.io/v1` NetworkPolicy objects enforced by Cilium
(replacing kind's default kindnet CNI). All five days are released and
frozen; see the historical evidence under
`docs/engineering-reviews/day-0[1-5]-*`. Day 5 keeps the entire
gateway/app/state architecture (security context, probes, Secrets,
PodDisruptionBudgets, persistence/retention behavior) **entirely
unchanged** from Day 4. See the Day 5 sections below (after the
unchanged Day 1-4 material, which this file preserves for continuity)
for the full security-boundary design, trust boundaries, what is
proven live, and what is explicitly NOT claimed.

Day 6 (`v0.6.0`) adds Helm packaging (the application chart,
`charts/maops-kubernetes-platform`, is the sole Day 6 application
deployment source - `k8s/base` stays frozen at Day 5 and is never
applied this stage), a minimal cluster-free GitHub Actions CI workflow,
one cluster-external routing approach (the Kubernetes Gateway API, with
Istio as the sole controller - never a second, Ingress-based
implementation), and Istio ambient service mesh layered on top of Day
5's NetworkPolicy boundaries. See the Day 6 sections below (after the
unchanged Day 1-5 material) for the full design. **Day 6 is release
ready as a local kind reference platform** - it has passed its
cluster-free static validation, its live validation against the local
`maops-k8s-day6` kind cluster, and an independent five-reviewer round
whose findings are closed. It was merged to `main` via PR #6; after
the 2026-09-25 post-restart incident (see "DAY6: post-restart ambient
listener incident (2026-09-25)") the release gate re-opened, and it
closed again once PR #7 merged with CI passing and the read-only
merged-`main` gate passed. **`v0.6.0` has not been tagged or
published.** See "DAY6: live validation record"
below for the exact results, dates, and accepted limitations, and
`docs/engineering-reviews/day-06-*` for the independent reviews,
adjudication, and remediation log. This is a validated local kind
reference platform, not a production-ready platform. Day 7 (`v1.0.0`) is the final milestone: Recreate,
Blue-Green, and Canary deployment-strategy demonstrations, a final
hardened validation pass, and portfolio closure. See
`docs/roadmap.md` for the full plan.

## Control flow

> **Days 1-5 baseline.** This diagram and the paragraph below it show
> the pre-Day 6 access model (the gateway reached only through a
> bounded `kubectl port-forward`). Day 6 adds the Gateway API ingress
> path (Istio ingress Gateway, NodePort 30080 mapped to host
> `127.0.0.1:18080`) and the Istio ambient mesh - see "DAY6: Gateway
> API - one routing approach, not two" and the README's "Day 6
> topology" for the current picture. Port-forward remains in use for
> bounded validation and debugging.

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
Through Day 5, no NodePort, LoadBalancer, or Ingress existed (Day 6
adds exactly one NodePort, for the Istio ingress Gateway; still no
LoadBalancer or Ingress) - see
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

DAY4-ARCH-M1 (remediation batch 2): the chain is now three hops deep -
`gateway -> app -> state` - not the two-hop `gateway -> app` chain this
margin pattern was originally sized for in Day 2. Each ConfigMap-
provided value below is a **per-call socket connect+read timeout**
(`http.client.HTTPConnection(timeout=...)` / `urllib.request.urlopen
(timeout=...)`), applied to a single HTTP call - never a total
wall-clock deadline summed over any retries a caller might perform on
top of it (the validation scripts' own bounded-retry loops, e.g.
`persistence_check.py`/`retention_check.py`, are a separate, outer
concern layered on top of these single-call timeouts).

The hierarchy, innermost hop first, each layer keeping a 2s margin over
the one it bounds:

1. `STATE_TIMEOUT_SECONDS` (`app/server.py`, `app-configmap.yaml` -
   default/value `3`s): the app -> state HTTP call budget.
2. `app`'s own `readinessProbe.timeoutSeconds` (`app-deployment.yaml` -
   `5`s): stays 2s above `STATE_TIMEOUT_SECONDS`, since app's `/readyz`
   itself makes that bounded state call.
3. `BACKEND_TIMEOUT_SECONDS` (`gateway/server.py`,
   `gateway-configmap.yaml` - raised this batch from `3`s to `5`s): the
   gateway -> app HTTP call budget. Raised specifically because app's
   own `/readyz`/`/internal/state` handlers can themselves take up to
   `STATE_TIMEOUT_SECONDS` to respond - the old `3`s value gave zero
   margin over that nested worst case, not the comfortable margin the
   two-hop Day 2 design assumed.
4. `gateway`'s own `readinessProbe.timeoutSeconds`
   (`gateway-deployment.yaml` - raised this batch from `5`s to `7`s):
   stays 2s above `BACKEND_TIMEOUT_SECONDS`.

`state`'s own probes are local-process-only (`/livez`) or storage-only
(`/readyz` - no outbound HTTP call), so they are not part of this
nested-timeout concern and were not changed.

Statically checked by `scripts/validate_manifests.py`
(`gateway.configmap.backend_timeout_seconds`,
`app.configmap.state_timeout_seconds`,
`gateway.probes.readiness_timeout_seconds`,
`app.probes.readiness_timeout_seconds`) and covered by deterministic
unit tests asserting the failure-path propagation and margin ordering
(`tests/test_app_auth.py`, `tests/test_gateway_backend_target.py`) -
see `docs/engineering-reviews/day-04-remediation-log.md`'s batch 2
amendment for the verification evidence.

**Validation-client margin (DAY4 batch 2b):** the hierarchy above
bounds the *server-side* chain only. `persistence_check.py` and
`retention_check.py` are HTTP *clients* of `service/maops-gateway`, and
their own per-call socket timeout previously equaled
`BACKEND_TIMEOUT_SECONDS` (5s) exactly - a tie that let the client's
own socket timeout fire at nearly the same instant as gateway's
controlled 503, making it unreliable to actually observe gateway's
classified response rather than a bare client-side timeout exception.
`CLIENT_HTTP_TIMEOUT_SECONDS` in both scripts is now `BACKEND_TIMEOUT_
SECONDS + 5s`, restoring real margin; `tests/test_gateway_state_routes.py`'s
`RealSocketTimeoutClassificationTests` proves the underlying
server-side timing relationship against a real (never mocked) slow
backend, not merely a documented value.

## Resources and security baseline

Unchanged from Day 1/2, applied identically to both workloads:
requests `cpu: 50m` / `memory: 32Mi`, limits `cpu: 250m` /
`memory: 128Mi`; `runAsNonRoot: true`, `runAsUser`/`runAsGroup: 10001`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`,
`readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`,
`automountServiceAccountToken: false`. Day 3 does not weaken any of
this to make scaling/rollout/scheduling easier to demonstrate.

## DAY4: the storage preflight, and the containerd multi-arch image defect it found

Before any Day 4 manifest was written, a disposable, out-of-band
preflight against a separate `maops-k8s-day4` kind cluster proved
whether the cluster's storage backend could support a non-root,
read-only-root-filesystem workload with a writable PVC. The first
attempt found that PVC/PV provisioning via `rancher.io/local-path`
worked cleanly, but the probe container never started: `kind load
docker-image` cannot import the project's digest-pinned, multi-arch
(`linux/amd64` + `linux/arm64/v8`) Distroless base directly (it only
has amd64 content pulled locally), and a manual `ctr images import`
workaround registered the image under a synthetic alias name that a
separate containerd 2.3.1 CRI bug then failed to resolve during
container creation (`failed to check if this is a checkpoint image`).
A controlled retry - building a genuinely single-platform image via
`docker build --platform linux/amd64 --provenance=false --sbom=false
--load` and loading it through the normal `kind load docker-image`
path, with no manual `ctr import` - resolved this cleanly and produced
the first real runtime evidence: UID 10001 writing/fsyncing/atomically
replacing a file on the PVC, and `readOnlyRootFilesystem` proven via a
specific `EROFS` errno (not a generic write failure, which could
false-positive on ordinary `EACCES` for a non-root UID writing to a
root-owned path). `IMAGE_BUILD_FLAGS` in the Makefile carries this
fix forward for all three Day 4 images - it is a local kind/containerd
compatibility fix for this environment, not a general production
supply-chain policy (a real registry-backed pipeline would build and
attest multi-arch images properly rather than disabling attestations).

The same preflight also found the PV backing directory was
`0777`/`root:root` - world-writable to any UID, independent of any
Pod's `fsGroup`. A write succeeding under those permissions is not
evidence `fsGroup` did anything; it would succeed under any UID/GID.
This finding directly motivated the storage bootstrap described next.

Full raw evidence (both attempts, all diagnostics, exit codes) is
preserved outside the repository under
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/`.

## DAY4: storage bootstrap - making `fsGroup` do real access-control work

`scripts/storage_bootstrap.py` (`make storage-bootstrap`) works in two
parts, in this order:

1. **Harden the provisioning root itself, once, on every node.**
   `/var/local-path-provisioner` becomes `chown root:10001` +
   `chmod 2770` (setgid) via `docker exec` - idempotent, and refused if
   the node's current owner isn't root (unfamiliar state, never
   overwritten). This step exists because of a real defect found live:
   the provisioner's own per-request helper Pod image
   (`kindest/local-path-helper`) is minimal and ships only
   `mkdir`/`rm`/`sh`/`bash` - it has no `chown`/`chgrp`/`chmod` binary
   at all, so a `setup` script that tried to call `chgrp` inside that
   helper failed outright (`chgrp: command not found`), causing every
   PV provisioning attempt to fail and time out. `docker exec` runs
   against the node itself, which has full coreutils, sidestepping the
   helper image's limitation entirely.
2. **Patch *only* the `local-path-config` ConfigMap's `setup` field**
   (`config.json`, `helperPod.yaml`, and `teardown` are left untouched,
   and the script refuses to touch anything if the current `setup`
   value doesn't byte-for-byte match the known original - never
   overwrites an unfamiliar hand-edit). The original
   `mkdir -m 0777 -p "$VOL_DIR"` becomes `mkdir -m 2770 -p "$VOL_DIR"`
   alone - no chgrp/chown call, because none is available inside the
   helper image and none is needed: standard Linux setgid-directory
   semantics mean a new directory created inside a setgid, group-10001
   parent (step 1, above) inherits group 10001 automatically, regardless
   of the creating process's own UID/GID - confirmed directly
   (`mode=2770 uid=0 gid=10001` on a real test directory) before being
   relied on for the actual fix. This also means the final state is
   produced by a single `mkdir` syscall, with no multi-step window at
   all - stronger than "restrictive first, then loosen".

`$VOL_DIR` is validated against the observed provisioning root
(`/var/local-path-provisioner`), rejected if it's a symlink, and
rejected if it already exists. Both steps are idempotent, propagation
is verified against a real scratch PVC before either is considered
successful, and a failed verification reverts BOTH the ConfigMap patch
and any node-level root hardening this run actually applied (a node
already correctly hardened before this run is left untouched by the
revert) - restoration failure is reported prominently, never hidden.

`scripts/storage_hardening_check.py` (`make storage-hardening-check`)
then proves the hardening actually changed behavior, against a
disposable scratch PVC (never `maops-state`'s own claim): a Pod running
as UID/GID 10001 with `fsGroup: 10001` (matching the directory's now
`10001` group) can write/fsync/atomically-replace/read back a file, and
- the actual point of this check - a Pod running as an unrelated
UID/GID (65532/65532, deliberately **no** `fsGroup`) receives `EACCES`
attempting the same write, because the directory's mode (`2770`) denies
`other` entirely. This is what makes `fsGroup: 10001` in
`k8s/base/state-statefulset.yaml` meaningful for the first time in this
project - unlike the preflight's `0777` finding, a write succeeding
here is genuinely attributable to group membership, not to the
directory being open to everyone.

**Scope note:** group `10001` here is a platform convention specific to
this single-tenant, isolated Day 4 kind cluster - it is not a general
multi-tenant storage policy, and this bootstrap never retroactively
touches an already-provisioned PV's backing directory (including the
two scratch PVs the storage preflight itself left behind).

**Probe image choice:** both scripts' scratch probe Pods deliberately
run the project's own already-rebuilt `maops-kubernetes-app` image
(command overridden to run a short probe script, never the app's HTTP
server) rather than referencing the raw multi-arch Distroless base
digest directly - an earlier live run found that once any manual `ctr
images import` of that raw digest has ever occurred on a node (as
happened during the two-attempt storage preflight), containerd's
checkpoint-image resolution can permanently fail for that exact digest
on that node, independent of whether the image is otherwise present.
Reusing the genuinely single-platform, normally-loaded app image
sidesteps this entirely. Both `make storage-bootstrap` and `make
storage-hardening-check` therefore require `make image-load` to have
already run - reflected in `day4-check`'s recipe order.

## DAY4: `maops-state` - a single-replica StatefulSet with PVC-backed persistence

`k8s/base/state-statefulset.yaml` adds a third workload alongside the
unchanged `maops-gateway`/`maops-app` Deployments: `maops-state`,
`replicas: 1` (never scaled beyond 1 - a single writer avoids the
concurrent-write/consensus problem a multi-replica stateful service
would otherwise need to solve, out of scope for this stage), governed
by a headless Service (`maops-state-headless`, `clusterIP: None`,
required by `StatefulSet.spec.serviceName` for the Pod's stable DNS
identity - not used for normal traffic) plus a normal ClusterIP Service
(`maops-state`) that `app`'s `STATE_HOST` ConfigMap value actually
resolves through. `volumeClaimTemplates` requests a `data` claim (256Mi,
`ReadWriteOnce`, `storageClassName` omitted so the cluster's verified
default StorageClass - `rancher.io/local-path` - is used), mounted at
`/data`, with `persistentVolumeClaimRetentionPolicy` set explicitly to
`Retain`/`Retain` - which is actually already the current Kubernetes API
default for both fields, so this pins (rather than overrides) that
default explicitly, the same convention Day 3 used for `RollingUpdate`
tuning, so a future Kubernetes version changing the default can never
silently start deleting the persisted record on StatefulSet
deletion/scale-down. Deleting the PVC is always a separate, deliberate
operator action. `maops-state` schedules only onto workers (the same
required node affinity as gateway/app) but carries no
`topologySpreadConstraints` and no PodDisruptionBudget - both are
meaningless for a single replica.

Expected portable rendered application resources: 13 (1 Namespace, 3
ConfigMaps, 2 Deployments, 1 StatefulSet, 4 Services, 2
PodDisruptionBudgets - gateway/app only). Runtime Secrets and the
generated PVC/PV are outside that count, and the provisioner's own
configuration is the separately-documented cluster-bootstrap concern
above - neither is part of the portable Kustomize base.

## DAY4: the state API and the authenticated gateway -> app -> state chain

`state/server.py` (Python stdlib only, same digest-pinned Distroless
base/interpreter as gateway/app) implements `GET`/`PUT /state` over a
tiny JSON record, `{"value": <string|null>}`, persisted at
`/data/state.json`. Every write follows the same durable-write sequence:
a restrictive (`0600`) temp file in the same directory (so the
following rename is a same-filesystem atomic operation), `flush()` +
`os.fsync()` on the temp file, `os.replace()`, then `os.fsync()` on the
parent directory file descriptor - success is acknowledged only after
all four steps complete. A missing state file is initialized to
`{"value": null}` via this exact same safe-write path; an existing file
that is unreadable or fails schema validation is never silently
overwritten with a fresh default - it is left as-is and surfaced by
`/readyz`. `GET /state` always re-reads the file from disk - there is
no in-memory cache that could paper over a real storage failure. A
single process-wide lock serializes every read-modify-write, so
concurrent `PUT`s resolve as documented last-writer-wins, never an
interleaved/torn write. If `os.replace()` itself succeeds but the
following parent-directory `fsync()` fails, that is reported as a
distinct, uncertain outcome (`FAILED_UNCERTAIN`) - different from a
clean failure where nothing was persisted - rather than claimed as
either a clean success or a clean failure.

The call chain is `gateway /state` -> `app /internal/state` ->
`state /state`. Gateway authenticates its call to app with the SAME
`maops-internal-auth` token it already uses for `/backend` (unchanged
since Day 2) - it never receives or forwards the new state credential.
App terminates that call exactly like `/internal/info`, then makes its
own outbound call to state using a second, dedicated Secret
(`maops-state-auth`, key `state-token`) that only app and state ever
hold. App's outbound call to state is built with `http.client`
directly rather than `urllib.request`: `http.client` never follows
redirects and never consults proxy environment variables, so there is
no redirect/proxy path that could ever divert the state token away from
the allowlisted `maops-state:8080` target (the same in-process
allowlist pattern as gateway's own `DAY2-SEC-L1` backend-target check).
The `X-MAOPS-State-Token` header app sends is always built from app's
own loaded token - never copied from an inbound request's headers, so a
client can never smuggle a value through. Both Secrets use constant-time
comparison (`hmac.compare_digest`) and fail closed on a missing or wrong
credential (`HTTP 403`).

## DAY4: readiness chain and outage behavior

`state`'s `/readyz` checks storage is usable and the persisted record is
structurally valid - it never repairs or replaces a corrupt record, only
reports it. `app`'s `/readyz` now also depends on USABLE AUTHENTICATED
state access: rather than an unauthenticated reachability check, it
calls the real authenticated `GET /state` path, so a broken token or
allowlist mismatch shows up as a readiness failure, not just a network
one. `gateway`'s `/readyz` is unchanged in shape (a bounded HTTP check
against app's own `/readyz`) but now transitively reflects state's
health too. All three workloads' `/livez` remain local-process-only, as
in Day 2/3 - a state outage must never restart an otherwise-healthy
app or gateway Pod. `scripts/retention_check.py` proves this live:
scaling `maops-state` to 0 leaves app/gateway `/livez` at `200` and
their `/readyz` at `503` (observed via direct-Pod port-forwards, since
a not-Ready Pod stops being a normal Service endpoint), while the PVC
and its bound PV remain `Bound` throughout (`persistentVolumeClaimRetentionPolicy:
Retain` doing exactly its job) - then scaling back to 1 produces a
genuinely new Pod identity with the pre-outage marker intact and normal
Service-routed 3/3 Ready behavior restored on both Deployments.

## DAY4: persistence proof

`scripts/persistence_check.py` proves data survives Pod
deletion/rescheduling specifically (as opposed to a full scale-to-zero
outage, which `retention_check.py` covers): it writes a unique marker
through the real service chain, records the Pod/PVC/PV identities,
deletes ONLY `maops-state-0` with a normal `kubectl delete pod` (never
`--force`), waits for the StatefulSet controller's real replacement,
then proves the same Pod NAME with a genuinely DIFFERENT Pod UID, the
SAME PVC UID and PV UID/binding (nothing was recreated), and the marker
reads back unchanged - through the same service chain, never from a
value already held in the script's own memory. The pre-experiment
record is restored on every handled exit path; a restoration failure is
reported prominently, never hidden behind the experiment's own result.

## DAY4: worker-local storage and its limits

`rancher.io/local-path`'s `WaitForFirstConsumer` binding mode means a
PV binds to whichever worker its first consuming Pod happens to land
on, and the resulting PV carries a `nodeAffinity` permanently pinning it
to that specific node - `maops-state`'s data does not move if that
worker becomes unschedulable, and this project's 2-worker kind topology
provides no cross-node replication for it. This is a known, accepted
limitation of this local development storage backend, not a defect
introduced by this stage; node-loss recovery for `maops-state`'s PV is
explicitly out of scope. The claim requests 256Mi; `local-path` does
not enforce that capacity as a hard quota on the node filesystem (unlike
a CSI driver backed by a real block device) - the requested capacity is
a Kubernetes-API-level bookkeeping value, not an enforced ceiling on
this backend.

## DAY4-ARCH-L1: fixed Day 4 topology and the node-addition limitation

The Day 4 kind cluster's node set (one control-plane, two workers) is
fixed for the lifetime of the cluster - every storage-hardening step
this stage performs (`make storage-bootstrap`'s per-node `chown`/`chmod`
of `/var/local-path-provisioner`, proven by `make
storage-hardening-check`) is applied explicitly to the node set observed
at the time those targets run, via `_cluster_nodes()`'s live `kubectl
get nodes` query - never a hardcoded node list.

**Documented limitation (not yet adjudicated - see below):** a node
added to the cluster AFTER `make storage-bootstrap` has already run
(e.g. a future `kind` node pool expansion) would NOT automatically
receive the same provisioning-root hardening - `storage-bootstrap` is
invoked once, explicitly, at a known point in `make day4-check`'s
sequence, not on a recurring or node-lifecycle-triggered basis. Any
such new node requires an explicit, manual re-run of `make
storage-bootstrap` (idempotent - see
[DAY4: storage bootstrap](#day4-storage-bootstrap-making-fsgroup-do-real-access-control-work)
above) and its own `storage-hardening-check` verification before any
`maops-state` PVC is ever scheduled to provision on it.

This is recorded here as an accepted Day 4 scope boundary for
documentation purposes; final risk acceptance (vs. a Day 5+
DaemonSet-based enforcement mechanism, the alternative the Day 4
architecture review raised) remains **pending an explicit owner
decision** - this batch does not choose between the two options or
assign new Day 5 scope on its own authority.

## DAY4: measured image digest mapping (DAY4-INT-2)

Remediation batch 1's `cluster-integration-engineer` investigation
(evidence:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-remediation-batch1-20260910T060524Z/dayrel2-image-provenance/`,
files `00`-`12`) measured, for the specific images and environment
inspected in that session (this local Docker Desktop engine using the
containerd image-store snapshotter, and this project's `kind`
node/containerd runtime):

| Workload | Host `docker inspect .Id` (platform-manifest digest) | Node `crictl` image ID (config digest) | Running container imageID/imageRef |
|---|---|---|---|
| app | `sha256:db24defd5adb0d44...` | `sha256:66a15ef2be0112947a...` | `import-2026-09-08@sha256:1d6c8cc7e2264a69...` |
| gateway | `sha256:e986f4d61fbfe06f...` | `sha256:2be49e935ce8f941...` | `import-2026-09-08@sha256:c3abb39fe9abbeb15...` |
| state | `sha256:dc1500feaa27f4e9...` | `sha256:b81768cdd9c629ff...` | `import-2026-09-08@sha256:f8931e085b8e72ac...` |

(Truncated here for readability; full digests are in the evidence
directory above - this section is a documentation pointer, not a
re-derivation, per this batch's scope: the expensive byte-level
forensics that produced these values is not repeated.)

**Why host and node report different digest values for the same
image** (this local Docker Desktop engine reports `driver-type:
io.containerd.snapshotter.v1`, `GraphDriver: None`): `docker inspect
.Id` reports the OCI **platform-manifest digest**, while the node's
`crictl images`/`crictl inspecti` reports the **config digest** - two
different, both-legitimate digest kinds for the *same* image content,
not a content mismatch. Every layer (45 layers + 1 config, all three
workloads) was confirmed byte-identical between the host-extracted
`docker save` blobs and the node's containerd content store, on both
worker nodes, and the source file inside each image's final layer was
confirmed byte-identical to the corresponding `app/server.py` /
`gateway/server.py` / `state/server.py` on disk.

**Scope of this observation:** limited to the images and environment
actually inspected above - a single-manifest OCI index (what `docker
save` emits for a locally-built, never-registry-pushed,
single-`linux/amd64` image) is still an index with meaningful content
identity for that image, not equivalent to "no index." This section
makes no claim about image flows or environments outside the ones
measured here.

## DAY5: trust boundaries

Before Day 5, every Pod in `maops-platform` was, from the network's
perspective, equally trusted: any Pod could reach any other Pod's
Service on any port (Day 2's deferred network-isolation gap), and
every Pod ran under the implicit `default` ServiceAccount (harmless
only because `automountServiceAccountToken: false` meant none of them
ever actually held a token). Day 5 draws three explicit trust
boundaries around that previously-flat topology:

1. **Identity boundary (RBAC).** Exactly one identity in this entire
   project - `maops-diagnostics`, living in the separate
   `maops-day5-validation` namespace - is trusted with a Kubernetes API
   token, and even that trust is narrow: read-only
   (`get`/`list`/`watch`) access to Pods/Services/EndpointSlices in
   `maops-platform` only, granted by exactly one namespace-scoped
   `Role`/`RoleBinding` pair. No other identity in this project - not
   `maops-gateway`, not `maops-app`, not `maops-state` - is bound to
   any Role or ClusterRole at all; `automountServiceAccountToken:
   false` on each of them means even a hypothetical future RBAC grant
   pointed at one of their names would still find no token mounted to
   use it with.
2. **Network boundary (NetworkPolicy).** Every Pod in `maops-platform`
   is default-denied both ingress and egress; the application chain
   (`gateway -> app -> state`) may only ever move forward, never
   backward or sideways (`gateway -> state` is explicitly absent from
   every allow rule, not merely unconfigured), and DNS is the only
   namespace-external egress any of the three workloads may ever
   reach.
3. **Namespace boundary (validation vs. application).** Validation/
   diagnostic tooling (`maops-diagnostics`, and the ephemeral
   `validation-client` probe Pods `scripts/networkpolicy_check.py`
   creates) lives in `maops-day5-validation`, never `maops-platform` -
   so the application namespace's own default-deny NetworkPolicy never
   has to carve out an exception for tooling that observes it. The
   ONE crossing this boundary permits is `validation-client -> gateway`
   (the same path a real external caller would use, via port-forward,
   in normal human/CI use) - never `-> app`, never `-> state`, and
   never a namespace-wide allow for `maops-day5-validation` as a whole.

## DAY5: ServiceAccounts - purpose-built identity, not implicit default

`k8s/base/gateway-serviceaccount.yaml`, `app-serviceaccount.yaml`, and
`state-serviceaccount.yaml` each declare a ServiceAccount named exactly
after their workload, `automountServiceAccountToken: false` set
explicitly on the ServiceAccount object itself (not just inferred from
the pod-level setting each Deployment/StatefulSet has carried since Day
1). Each workload's `spec.template.spec.serviceAccountName` now names
its own ServiceAccount rather than leaving Kubernetes to default to
`default`. This is belt-and-suspenders by design: `automountServiceAccountToken:
false` is asserted at BOTH the ServiceAccount level and the pod level,
so either one alone would already prevent a token mount - the value of
naming a purpose-built ServiceAccount at all, given neither one ever
gets a token, is that a *future* change to grant one of these workloads
API access would have to explicitly flip `automountServiceAccountToken`
on an identity that unambiguously belongs to that one workload, rather
than silently affecting every Pod that happens to still be using
`default`.

`maops-diagnostics` (`k8s/base/diagnostics-serviceaccount.yaml`) is the
deliberate exception: `automountServiceAccountToken: true`, living in
`maops-day5-validation`, because `scripts/rbac_check.py` needs a real
mounted token to make real HTTPS calls to the API server from inside a
Pod - proving the RBAC grant end to end (token mount + API server
authorization together), not just that a `Role`/`RoleBinding` object
exists on paper.

## DAY5: RBAC - one Role, one RoleBinding, read-only, namespace-scoped

`k8s/base/diagnostics-role.yaml` grants exactly:

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods", "services"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch"]
```

Read-only, and scoped to exactly the resources `scripts/rbac_check.py`
and `scripts/networkpolicy_check.py` actually need to observe cluster
state during validation. No `secrets` resource, no write verb
(`create`/`update`/`patch`/`delete`/`deletecollection`), no wildcard
apiGroup or resource, and - because this is a `Role`, not a
`ClusterRole` - no way for any rule inside it to ever reach a namespace
other than `maops-platform`, even if one tried to. `ClusterRole`/
`ClusterRoleBinding` remain forbidden at every rendered object in this
project (`scripts/validate_manifests.py`'s `FORBIDDEN_KINDS`), matching
this stage's explicit "namespace-scoped only" design.

`k8s/base/diagnostics-rolebinding.yaml` binds this Role to exactly one
subject: ServiceAccount `maops-diagnostics` in `maops-day5-validation`
- a cross-namespace RoleBinding subject (the Role/RoleBinding
themselves live in `maops-platform`, granting access *into*
`maops-platform`; the ServiceAccount they grant it *to* lives
elsewhere). No application ServiceAccount (`maops-gateway`/`maops-app`/
`maops-state`) ever appears as a subject of this or any other
RoleBinding - checked explicitly and negatively by
`scripts/validate_manifests.py`'s
`rbac.application_service_accounts_not_bound`.

`scripts/rbac_check.py` proves this live: from inside a probe Pod
running as `maops-diagnostics` with its real mounted token, direct
HTTPS calls to `https://kubernetes.default.svc` prove `pods`/
`services`/`endpointslices` reads return `200`, while `secrets` reads,
a Deployment `DELETE`, a Deployment `/scale` `PATCH`, a cross-namespace
Pod read (`kube-system`), and a cluster-scoped Node read all return
exactly `403` - never inferring "denied" from a bare non-2xx response
without confirming it is specifically `403 Forbidden`.

## DAY5: NetworkPolicy - default-deny plus six narrow allows

Seven `networking.k8s.io/v1` NetworkPolicy objects live in
`k8s/base/`, all namespaced to `maops-platform` (a `NetworkPolicy`
object itself only ever governs traffic to/from Pods it selects within
its own namespace - there is no cluster-scoped variant):

1. **`maops-default-deny-all`** - `podSelector: {}` (every Pod in the
   namespace), `policyTypes: [Ingress, Egress]`, no rules. This is the
   entire enforcement baseline: Kubernetes NetworkPolicy semantics mean
   any Pod selected by at least one policy with `Ingress` in
   `policyTypes` accepts ONLY traffic some ingress rule (in ANY policy
   selecting that Pod) explicitly allows - so this one object alone
   makes every other policy below purely additive, never something that
   could accidentally loosen the baseline by omission.
2. **`maops-allow-dns-egress`** - `podSelector: {}` (every workload
   needs DNS), egress to `kube-system`/`k8s-app: kube-dns` on UDP+TCP
   port 53. Without this, `BACKEND_HOST=maops-app`/`STATE_HOST=
   maops-state` service-name resolution would fail outright under the
   default-deny baseline - DNS is the one namespace-external
   dependency every workload here has always had (unchanged since Day
   2), so it is the one namespace-wide (not per-component) allow.
3. **`maops-allow-gateway-egress-to-app`** + **`maops-allow-app-ingress-from-gateway`**
   - a matched pair: gateway's egress allow targets `component: app`
   on TCP 8080, and app's ingress allow accepts only from `component:
   gateway` on TCP 8080. Both sides must independently allow the same
   traffic for it to actually flow - NetworkPolicy ingress and egress
   are evaluated completely independently, so a single one-sided policy
   would never be sufficient on its own.
4. **`maops-allow-app-egress-to-state`** + **`maops-allow-state-ingress-from-app`**
   - the same pattern, one hop deeper: app -> state.
5. **`maops-allow-gateway-ingress-from-validation`** - gateway's
   ingress allow accepts traffic from Pods labeled `component:
   validation-client` specifically inside the `maops-day5-validation`
   namespace (`namespaceSelector` + `podSelector` combined in the same
   peer entry, which Kubernetes ANDs together - "this label, in that
   namespace", never "this label, in ANY namespace" or "ANY Pod in that
   namespace"). This is the ONLY path into `maops-platform` from
   outside it.

**What is deliberately absent, not just unconfigured:** no rule
anywhere grants `gateway -> state` (checked negatively:
`networkpolicy.gateway_egress_app.never_targets_state` and
`networkpolicy.state_ingress_app.never_allows_gateway`), no rule grants
`validation-client -> app` or `validation-client -> state` (checked
negatively for both the app/state ingress-allow policies specifically
and, as a cross-cutting guard, for every OTHER NetworkPolicy object in
the namespace - `networkpolicy.<name>.no_stray_validation_namespace_ingress`
- so a bypass introduced under an unrelated policy name would still be
caught), and no policy ever grants any Pod in `maops-platform` egress
toward the Kubernetes API server - `maops-gateway`/`maops-app`/
`maops-state` have no path to the control plane at all, on top of
already lacking any mounted token to present to it.

Every selector uses stable `app.kubernetes.io/component` Pod labels
(the same labels Day 3's Service/PodDisruptionBudget selectors already
relied on) and the Kubernetes API server's own automatic, standard
`kubernetes.io/metadata.name` namespace label - never a Pod IP, node
name, or hardcoded Pod name anywhere in any policy.

`scripts/networkpolicy_check.py` proves all of the above live, using
REAL in-cluster TCP connection attempts from actual Pods (never only a
port-forward, which reaches a Service from OUTSIDE the cluster network
entirely and cannot observe pod-to-pod policy enforcement at all): the
real gateway/app Pods are exec'd into directly (their labels already
match what the policies select), and one short-lived
`validation-client`-labelled probe Pod is created in
`maops-day5-validation`, exec'd into for all three of its checks, then
deleted in a guaranteed `finally` block. A policy-blocked connection is
typically dropped silently by the CNI dataplane rather than actively
refused, so every probe uses a short, explicit client-side timeout -
the ONLY reliable signal that a connection was genuinely blocked, never
a bare "no response" with nothing bounding how long the check waits.

**DAY4-SEC-L1 disposition: REDUCED, not CLOSED (re-adjudicated by the
Day 5 security review, live-verified).** A standard Kubernetes
`NetworkPolicy` restricts *which Pods/namespaces can reach a Service's
port at all* (L3/L4 scope) - it is not HTTP path- or method-level
authorization, and cannot by itself distinguish "may `GET /state`"
from "may `PUT /state`" for a caller it otherwise permits to reach the
gateway at all. The Day 5 security review independently confirmed,
live, that Day 5's NetworkPolicy genuinely CLOSES the pod-to-pod
sub-risk DAY4-SEC-L1 originally named (an arbitrary Pod anywhere in the
cluster reaching gateway's unauthenticated write path is now denied -
confirmed with a real blocked TCP attempt from a non-`validation-client`
Pod). It does NOT, and architecturally cannot, close the other sub-risk
that finding already called the realistic one: `kubectl port-forward`
tunnels via the API server -> kubelet -> container network namespace, a
path standard `NetworkPolicy`/Cilium eBPF enforcement never sees or
polices at all - confirmed live by a direct port-forward to a gateway
Pod reaching `GET /state` with `HTTP 200` and zero credential supplied.
Genuinely closing this remaining sub-risk, if ever required, would need
application-layer authorization ahead of `PUT /state`, which
NetworkPolicy does not and cannot provide and which remains explicitly
out of Day 5's scope. No L7/HTTP-aware policy engine (Cilium's own
`CiliumNetworkPolicy` L7 rules, or any service mesh's request-level
policy) is introduced this stage - see
[Why Service Mesh remains deferred to Day 6](#why-service-mesh-remains-deferred-to-day-6)
below. Tracked forward as `DAY5-SEC-M1` (Medium, non-blocking) in
`docs/engineering-reviews/day-05-kubernetes-security-review.md`.

## DAY5: Cilium as the enforcing CNI dataplane

Standard Kubernetes `NetworkPolicy` objects are declarative - they mean
nothing without a CNI plugin that actually enforces them, and kind's
default CNI (kindnet, installed automatically unless told otherwise)
does not. `kind/cluster-day5.yaml` sets
`networking.disableDefaultCNI: true`, which leaves every node
genuinely `NotReady` (no pod network exists at all) until Cilium is
installed out-of-band via Helm (`make cni-install`) - this is itself a
useful live signal: a node that reaches `Ready` after `cni-install`
proves a real CNI is now present, not merely that one was requested.

Cilium is installed with `kubeProxyReplacement=false` - kube-proxy is
deliberately left running and doing exactly what it has done since Day
1 (Service ClusterIP load-balancing via iptables/IPVS rules). Day 5
adopts Cilium ONLY for its NetworkPolicy enforcement (Cilium implements
the standard `networking.k8s.io/v1` API using eBPF - the manifests
committed to `k8s/base/` are 100% portable standard Kubernetes objects,
with zero Cilium-specific fields or a `CiliumNetworkPolicy` CRD
anywhere), not as a kube-proxy replacement, not for its L7/HTTP-aware
policy features, and not with Hubble (Cilium's own observability
layer) enabled - all three are legitimate Cilium capabilities this
project deliberately does not reach for at this stage, to keep the
NetworkPolicy story exactly what a portable, CNI-agnostic Kubernetes
manifest set can express on its own.

`scripts/cni_check.py` (`make cni-status`) is a READ-ONLY verification
(never installs or mutates anything - that's the separate, explicit
`make cni-install`): proves every node is `Ready`, the Cilium agent
DaemonSet has exactly one Ready Pod per node, the Cilium operator
Deployment has at least one available replica, and the kube-proxy
DaemonSet still has Ready Pods (confirming it was never disabled).

## DAY5-ARCH-M2/M3: Cilium operator instability and 3-node Cilium
resource footprint on constrained local hosts (documented limitation)

The Day 5 architecture and cluster-integration reviews both independently
observed, live, that the `cilium-operator` Deployment restarts frequently
(13-15 restarts over several hours in one observed run) with
`"Failed to update lease" ... context deadline exceeded` ->
`"Leader election lost, shutting down."` in its previous-container logs
- an HA leader-election symptom consistent with API-server-response
latency under host resource pressure, not an OOMKill and not a
NetworkPolicy/RBAC configuration defect. **This does not affect the
Cilium agent DaemonSet** (the per-node component that actually enforces
`NetworkPolicy` in the eBPF datapath), which was independently confirmed
healthy (all controllers reporting healthy, real ALLOW/DENY traffic
tests both correct) during the same observation window - the operator
crash-looping is real, but it is not a hole in what Day 5 actually
proves.

Separately, the 3-node Cilium-enabled Day 5 topology (agent + envoy +
2-replica operator DaemonSets/Deployments, per node/cluster, on top of
the standard control-plane pods) roughly doubles `kube-system`'s
steady-state pod count versus Day 1-4's kindnet baseline, and was
observed pushing the control-plane node container to ~38% CPU/~1GiB at
rest even before any application workload is considered. Running this
cluster concurrently with several earlier-day kind clusters on a
memory-constrained local host (e.g. WSL2 with a fixed VM memory
ceiling) is fragile - this class of resource pressure was directly
observed to have terminated other kind clusters' node containers during
this project's own Day 5 review. **Accepted as a documented local-
resource-sizing limitation** (same pattern as `DAY4-ARCH-L1`), not a
manifest defect: operators reproducing this locally on a constrained
host should stop superseded earlier-day clusters
(`kind delete cluster --name <name>`, or `docker stop` its node
containers) before running Day 5's suite, and may consider
`--set operator.replicas=1`/`--set envoy.enabled=false` if the
2-replica HA operator and unused L7 Envoy dataplane's overhead becomes
a problem on a single-tenant local cluster.

## DAY5: released validation record

The merged `main` branch passed the complete authoritative `make
day5-check` sequence before release (verified 2026-09-19; full detail
in
[`docs/engineering-reviews/day-05-post-release-verification.md`](engineering-reviews/day-05-post-release-verification.md)
- these figures are that release's record, not results re-run during
any later documentation pass): 814 unit tests; manifest validation
267/267; version validation 35/35; CNI status 4/4; storage hardening
2/2; rollout and security posture 35/35; scheduling 12/12; discovery
2/2; secret handling 49/49; RBAC 10/10; NetworkPolicy 8/8; smoke 6/6;
dependency-failure behavior 11/11; scaling 20/20; rolling update and
rollback 38/38; PodDisruptionBudget behavior 16/16; state behavior
24/24; persistence 12/12; PVC retention 22/22; final-state restoration
40/40. `make`, `tee`, and output-filter exit codes were all zero, the
external state-file hash was unchanged, and the working tree was clean
afterward.

## What Day 5 proves, and what it explicitly does not claim

**Proven live** (`make rbac-check`, `make networkpolicy-check`, `make
cni-status`, plus the unchanged Day 1-4 checks re-run against this
stage's new cluster): the diagnostics identity can read exactly the
namespaced resources it is granted and nothing else, real pod-to-pod
traffic follows the `gateway -> app -> state` chain and nowhere else,
DNS resolution keeps working under the default-deny egress baseline,
the existing gateway -> app -> state behavior (persistence, auth,
readiness) is unaffected by NetworkPolicy enforcement, and Cilium is
genuinely the pod network (not just nominally installed).

**Explicitly NOT claimed:** that this NetworkPolicy layer provides
HTTP-method-level authorization (see the DAY4-SEC-L1 note above); that
Cilium's L7 policy, Hubble observability, or kube-proxy-replacement
mode are configured or available (none are); that a service mesh
(mTLS between workloads, traffic shaping, request-level policy) exists
at this stage (Day 6, see below); that RBAC in this project extends
beyond the single `maops-diagnostics` grant (no other identity holds
any token or binding); or that this cluster's Cilium installation
represents a production-grade rollout (only `ipam.mode`,
`kubeProxyReplacement`, `hubble.enabled`, and `image.pullPolicy` are
pinned via Helm `--set` flags - a real production deployment would tune
considerably more, e.g. IPAM CIDR sizing, `kube-proxy`-replacement
strategy, and encryption, none of which this single-tenant local kind
cluster needs). The `cilium-envoy` L7 dataplane component runs on every
node by chart default (`enable-l7-proxy`/`external-envoy-proxy` are
both `"true"` in the rendered `cilium-config` ConfigMap) even though no
`CiliumNetworkPolicy` L7 rule exists anywhere in this project (zero
matches) - this is unused-but-present footprint, not a security gap,
flagged here per the Day 5 architecture review (`DAY5-ARCH-L1`) for
completeness. Also note `maops-day5-validation` itself carries no
NetworkPolicy of its own (`DAY5-ARCH-L2`/`DAY5-SEC-L1`) - consistent
with this stage's explicit `maops-platform`-only default-deny scope,
and mitigated by keeping the RBAC-token-bearing `diagnostics` identity
and the network-privileged `validation-client` identity strictly
separate, short-lived, and cleanup-guaranteed - but it means the
validation namespace's own egress is currently unrestricted, a Day 6/7
candidate.

## Why Ingress/Gateway API remained deferred through Day 5

Day 5 still reached `maops-gateway` only via `kubectl port-forward` -
see
[Why port-forward instead of NodePort/Ingress](#why-port-forward-instead-of-nodeportingress)
below (that rationale is unchanged for the internal/validation port-
forward paths scripts/smoke.py and friends still use - Day 6 adds a
cluster-external path alongside it, it does not remove port-forward
entirely). Cluster-external routing is Day 6 scope - see
[DAY6: Gateway API - one routing approach, not two](#day6-gateway-api---one-routing-approach-not-two)
below for what was actually implemented and why a second,
Ingress-based path was deliberately not also introduced.

## Why Service Mesh remained deferred through Day 5

Day 5's NetworkPolicy governs L3/L4 reachability between Pods - it has
no concept of mutual TLS, request-level (L7/HTTP) policy, or traffic
shaping between workloads, none of which were implemented, referenced
as available, or claimed to exist at that stage. Per `docs/roadmap.md`,
a service mesh was Day 6 scope, alongside Helm packaging for the
application itself (distinct from Day 5's use of Helm solely to install
Cilium), GitHub Actions CI, and the Gateway API - not Day 7, which is
reserved for Recreate/Blue-Green/Canary deployment-strategy
demonstrations compared against Day 3's RollingUpdate, built on top of
the mesh Day 6 introduces rather than introducing it. See
[DAY6: Istio ambient service mesh](#day6-istio-ambient-service-mesh)
below for what was actually implemented.

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

**Day 6 change:** the normal external entry point is now the Gateway API
path, which does open one NodePort (30080, on the Istio-generated
ingress Gateway Service, mapped to host `127.0.0.1:18080` by
`kind/cluster-day6.yaml`). Bounded port-forwards remain in use by the
validation scripts (`make smoke`, and the port-forward-based checks)
and for debugging, exactly as described above.

## DAY6: Helm chart ownership boundary

`charts/maops-kubernetes-platform` is the **sole** Day 6 application
deployment source. It owns every mutable application object: the three
ConfigMaps, the three application ServiceAccounts, the namespace-scoped
diagnostics Role/RoleBinding, the two Deployments, the StatefulSet, the
four Services, the two PodDisruptionBudgets, the eight NetworkPolicies,
the PeerAuthentication, the three AuthorizationPolicies, and the
HTTPRoute - 30 rendered objects total (`scripts/helm_check.py`'s
`inventory.total_object_count` check). `k8s/base` (Day 5's frozen
Kustomize source) is never modified and never applied by any Day 6
Make target - `deploy` runs `helm upgrade --install` only.
`scripts/helm_check.py`'s own `scope.k8s_base_still_frozen_at_day5`
check independently proves this by reading `scripts/validate_manifests.py`'s
own `EXPECTED_VERSION`/`EXPECTED_INSTANCE` constants (still `0.5.0`/
`maops-kubernetes-platform-day5`), not by re-rendering k8s/base.

Cluster/platform support objects that are NOT application workloads -
things that exist once per cluster/environment rather than once per
release, and that a real operator would provision before ever running
`helm install` - live under `k8s/day6/` instead, applied via plain
`kubectl apply -f` (`make namespace-apply`, `make gateway-apply`),
never templated by the chart and never expressed as a Helm chart
dependency of it:

- `platform-namespace.yaml` (`maops-platform`, labeled
  `istio.io/dataplane-mode: ambient`)
- `validation-namespace.yaml` (`maops-day6-validation`)
- `ingress-namespace.yaml` (`maops-ingress`)
- `diagnostics-serviceaccount.yaml` (`maops-diagnostics`, in
  `maops-day6-validation`)
- `gateway-values-configmap.yaml` (the Istio Gateway infrastructure
  `parametersRef` ConfigMap, in `maops-ingress`)
- `gateway.yaml` (the Gateway API `Gateway` object, `maops-edge`, in
  `maops-ingress`)
- `cilium-ambient-probe-policy.yaml` (the `CiliumClusterwideNetworkPolicy`
  ambient health-probe exception)

Infrastructure Helm charts (Cilium, Istio's `base`/`istiod`/`cni`/
`ztunnel`) are installed separately through their own pinned Make
targets (`cni-install`, `mesh-install`) and are never declared as
`dependencies:` of `charts/maops-kubernetes-platform`'s `Chart.yaml` -
the application chart has zero infrastructure coupling, matching Day
5's own precedent of installing Cilium via a separate, explicit `helm`
invocation rather than folding it into anything the application owns.

## DAY6: Gateway API - one routing approach, not two

`k8s/day6/gateway.yaml` declares a single Kubernetes Gateway API
`Gateway` object, `maops-edge`, in `maops-ingress`, with
`gatewayClassName: istio` - Istio is the **only** `GatewayClass`
controller this project ever installs; Istio provisions the `istio`
`GatewayClass` itself once istiod is installed with Gateway API support
(`make mesh-install`), never created directly by this project. No
`Ingress` object exists anywhere in this project (checked statically:
`scripts/helm_check.py`'s `inventory.no_ingress`), and no second
ingress controller is introduced - see `docs/roadmap.md`'s corrected
Day 6 section for why a second, Ingress-based implementation was
deliberately not also built.

The relationship, four objects deep:

```
GatewayClass: istio (cluster-scoped, provisioned by istiod)
   |
   v
Gateway: maops-edge (maops-ingress)
   - listener: HTTP, port 80, hostname maops.local
   - allowedRoutes: namespaces selected by kubernetes.io/metadata.name=maops-platform
   - infrastructure.parametersRef -> ConfigMap maops-edge-gateway-values
   |
   v  (Istio's Gateway API deployment controller provisions, patched by
   |   that ConfigMap's per-resource-kind strategic-merge patches:
   |   Deployment replicas 1, Service type NodePort/port 30080,
   |   ServiceAccount automountServiceAccountToken false - the
   |   ServiceAccount's own NAME, "maops-edge-istio", is Istio's
   |   deterministic "<gateway-name>-istio" convention, not a patch)
   v
HTTPRoute: maops-gateway-route (maops-platform, owned by the Helm chart)
   - parentRefs: [{name: maops-edge, namespace: maops-ingress}]
   - hostnames: [maops.local]
   - rules: PathPrefix "/" -> backendRefs: [{name: maops-gateway, port: 8080}]
   |
   v
Service: maops-gateway (ClusterIP, maops-platform)
   |
   v
Deployment: maops-gateway Pods
```

The application chart owns the `HTTPRoute` (application routing
configuration, changes with the release); the platform layer owns the
`Gateway` and its infrastructure ConfigMap (cluster/environment
configuration, provisioned once). `allowedRoutes.namespaces` uses a
`Selector` matching the automatic `kubernetes.io/metadata.name` label -
the same "never a hand-maintained label" convention Day 5's
NetworkPolicy objects already used - so only `maops-platform`'s
HTTPRoutes may ever attach to this Gateway.

**Local NodePort/host mapping:** `k8s/day6/gateway-values-configmap.yaml`
pins the Istio-provisioned Gateway proxy's Service to `type: NodePort`
with the HTTP listener bound to `nodePort: 30080` - the local-
development substitute for a cloud LoadBalancer (explicitly out of
scope, see below). `kind/cluster-day6.yaml`'s `extraPortMappings` maps
host `127.0.0.1:18080` to the control-plane container's port `30080`,
so `curl -H "Host: maops.local" http://127.0.0.1:18080/` reaches
`maops-gateway` from outside the `kind` Docker network entirely -
`scripts/gateway_check.py` proves this live (see "DAY6: live
validation record" below), including the negative case (an
unconfigured `Host` header receives no `maops-gateway` route).

**Deterministic Gateway proxy identity (corrected after independent
review):** an earlier revision of this design tried to override the
generated ServiceAccount's own NAME to a bare `maops-edge` via the
`infrastructure.parametersRef` ConfigMap. That is not how the
mechanism works - the ConfigMap's `data` keys are per-resource-kind
strategic-merge patches (`deployment`, `service`, `serviceAccount`,
each shaped like the target resource itself, e.g. starting from
`spec:` for Deployment/Service), which can adjust fields ON a
generated object but do not control the object's own name, owned by
the controller's fixed naming template. Istio names the generated
ServiceAccount for a Gateway deterministically as
`<gateway-name>-istio` - for `maops-edge`, `maops-edge-istio`. Every
`AuthorizationPolicy`/constant/test/doc in this project is written
against `cluster.local/ns/maops-ingress/sa/maops-edge-istio` exactly,
matching Istio's real naming convention rather than an unverified
override. See `k8s/day6/gateway-values-configmap.yaml`'s own header
comment for the full corrected schema - the per-resource-kind
(`deployment`/`service`/`serviceAccount`) `parametersRef` mechanism it
uses is Istio's documented behavior, not an unresolved assumption.
**What live validation confirms:** `make gateway-apply` + `make
mesh-install`, verified by `make gateway-check`/`make mesh-check`,
confirm the RENDERED CONTROLLER BEHAVIOR on this project's pinned Istio
1.31.0 - the generated Service's `targetPort`/`nodePort`, the generated
ServiceAccount's token-projection behavior and its `maops-edge-istio`
name, and that the NodePort is actually reachable - confirming behavior
against a known schema, not discovering the schema itself. The
generated proxy binds port 80 without any added Linux capability: its
`istio-proxy` container drops ALL capabilities and runs non-root as
1337:1337, and Istio's gateway template sets the Pod-level
`net.ipv4.ip_unprivileged_port_start=0` sysctl instead (observed live;
see `k8s/day6/gateway-values-configmap.yaml`). Any field that renders differently than expected is scoped to
that one ConfigMap and the identity constants that reference it, not
the surrounding routing/AuthorizationPolicy design.

## DAY6: Istio ambient service mesh

Istio is installed in **ambient** mode - no sidecar proxy is ever
injected into an application Pod, and no waypoint proxy is deployed for
`maops-platform` (checked statically: `scripts/helm_check.py`'s
`inventory.no_waypoint`). Instead, each node runs exactly one `ztunnel`
DaemonSet Pod, and Istio's CNI plugin (`istio-cni`, chained alongside
Cilium via `cni.exclusive=false` - see below) transparently redirects
an ambient-enrolled Pod's traffic to its local node's `ztunnel`. Mesh
enrollment is a **namespace-level** label
(`istio.io/dataplane-mode: ambient`, set on `maops-platform` in
`k8s/day6/platform-namespace.yaml`), never a per-Pod sidecar-injection
annotation - `maops-day6-validation` and `maops-ingress` are
deliberately NOT labeled this way, so the diagnostics/validation-client
identity and the ingress Gateway proxy are never ambient-redirected
workloads themselves.

**HBONE (port 15008):** every mesh connection between two ztunnels
(cross-node) is tunneled as mTLS-over-TCP on port 15008 - the "HTTP-
Based Overlay Network Encapsulation" Istio ambient uses. Under
`maops-platform`'s default-deny NetworkPolicy baseline, this transport
hop would otherwise be silently blocked by Cilium before ever reaching
an application container; `maops-allow-hbone-ztunnel` (one of the
chart's eight NetworkPolicies) is the fix, both ingress and egress
(genuinely required under default-deny EGRESS too - a local
ambient-enrolled Pod's own outbound traffic is redirected to its
node's ztunnel over this same port before HBONE-encapsulation ever
leaves the node).

**Corrected after independent review (twice): this rule is deliberately
peer-less (no `from`/`to` selector), scoped by port alone.** An earlier
revision scoped the peer to
`namespaceSelector: {kubernetes.io/metadata.name: istio-system}`,
treating ztunnel as an ordinary namespaced Pod peer. That does not
hold: ztunnel is a per-node, `hostNetwork: true` DaemonSet, so HBONE
traffic to/from a node's ztunnel carries that NODE's own identity
(Cilium's "remote-node"/"host" identity classes), not a routable,
namespace-scoped Pod identity a `namespaceSelector`/`podSelector` peer
can reliably resolve - a rule that assumed otherwise could silently
fail to match live traffic, or appear to work only by coincidence of a
given cluster's configuration. Rather than assert a peer restriction
this project cannot actually verify against ztunnel's real network
identity, the rule is scoped ONLY by port (TCP 15008) - permitting the
ambient TRANSPORT itself, not any specific peer on it.

**Corrected claim, fourth review batch:** a still-earlier revision of
this section additionally claimed the application-port allows
(`maops-allow-gateway-egress-to-app`, etc.) "still govern which peer
may reach which workload's actual application traffic" for the real
ambient mesh path. That overclaims what those rules can actually see:
every workload in `maops-platform` is ambient-enrolled (namespace-wide
`istio.io/dataplane-mode: ambient`), so `gateway -> app` and
`app -> state` traffic is REDIRECTED to HBONE before it ever appears
to Cilium as ordinary pod-to-pod traffic on port 8080 - the
application-port rules' `podSelector`-matched peers are simply never
the traffic Cilium actually evaluates for that path once ambient
redirection is in effect. The corrected framing:

**The identity boundary this project actually relies on** (this is the
load-bearing explanation, not the NetworkPolicy rules alone):

- **Cilium/Kubernetes NetworkPolicy** - the peerless HBONE rule above
  is what permits the ambient TRANSPORT itself; it controls
  reachability only, and has **no visibility into the original
  workload identity once traffic is HBONE-encapsulated** by ztunnel -
  from Cilium's perspective, HBONE traffic on port 15008 is
  indistinguishable node-to-node transport, never attributable to "the
  gateway Pod talking to the app Pod." The application-port allows
  elsewhere in the chart (`maops-allow-gateway-egress-to-app`,
  `maops-allow-app-ingress-from-gateway`, `maops-allow-app-egress-to-state`,
  `maops-allow-state-ingress-from-app`) remain meaningful ONLY for
  **plaintext or non-ambient paths** where the original peer identity
  is actually visible to Cilium on the wire - e.g. if ambient
  enrollment were ever disabled for a workload, or for a direct,
  non-redirected connection attempt that bypasses ztunnel. They are
  kept, unweakened, specifically to cover that case - not because they
  are the enforcement mechanism for the actual ambient-mesh-tunneled
  traffic path today.
- **Istio `AuthorizationPolicy`** (see "strict mTLS and identity
  authorization" below) is what actually enforces the
  `gateway -> app -> state` AUTHENTICATED WORKLOAD IDENTITY chain for
  the real ambient path - it authenticates the real workload
  ServiceAccount identity via the mTLS client certificate ztunnel
  presents on that workload's behalf, INSIDE the tunnel, after
  decapsulation - exactly the layer Cilium cannot reach into. This
  makes strict mTLS + `AuthorizationPolicy` a **required complement**
  to the NetworkPolicy layer for the ambient path, not defense-in-depth
  on top of an already-sufficient NetworkPolicy.
- **Application-layer Secret authentication**
  (`maops-internal-auth`/`maops-state-auth`, unchanged since Day 2/4)
  remains a third, independent layer on top of both.

**Accepted trust boundary: peerless TCP 15008 (DAY6 review SEC-2).**
`maops-allow-hbone-ztunnel` permits TCP 15008 to and from every
`maops-platform` Pod with no peer selector. This is intentional, not an
accidental cluster-wide application allow:

- ztunnel is host-networked, so it cannot be reliably selected as an
  ordinary namespaced Pod peer - a `namespaceSelector`/`podSelector`
  peer would either silently fail to match live HBONE traffic or match
  only by coincidence of one cluster's identity allocation.
- At this layer Cilium provides **transport reachability only**: it
  lets the ambient HBONE tunnel exist, on that one port.
- **Authenticated workload identity** is enforced inside the tunnel by
  STRICT `PeerAuthentication` plus the three exact-principal
  `AuthorizationPolicy` objects - a peer that can reach port 15008 but
  cannot present an allowed SPIFFE identity is rejected by the
  destination ztunnel (proven live by `make mesh-check`'s correlated
  denial evidence).
- The application-port NetworkPolicies remain in force and relevant
  for **plaintext/non-ambient paths** (proven live by `make
  networkpolicy-check`'s isolated, non-ambient probe Pods).

This is an accepted defense-in-depth design boundary for ambient mode
on this Cilium/Istio combination: port-level reachability from Cilium,
workload identity from Istio, and application credentials from the
workloads themselves.

**The Istio ingress Gateway path is adapted, not carried forward
unchanged from Day 5.** Day 5's `maops-allow-gateway-ingress-from-validation`
NetworkPolicy (allowing a `validation-client`-labelled probe Pod direct
access to `maops-gateway`) is deliberately NOT reproduced in the Day 6
chart - `scripts/helm_check.py`'s `networkpolicy.no_day5_validation_shortcut`
checks this negatively. In its place,
`maops-allow-gateway-ingress-from-istio-ingress-gateway` allows ingress
to `maops-gateway` only from Pods labeled `istio.io/gateway-name:
maops-edge` in the `maops-ingress` namespace - the Istio-generated
ingress Gateway proxy Deployment, identified the same
`namespaceSelector` + `podSelector` combined-AND pattern Day 5's own
validation-client allow used. `scripts/networkpolicy_check.py` is
updated to match: it now asserts `validation-client -> gateway` is
DENIED (a Day 6 behavior change from Day 5's ALLOWED), same as
`-> app`/`-> state` always were.

## DAY6: Cilium and Istio responsibility boundary

Cilium remains the CNI and the sole `networking.k8s.io/v1`
`NetworkPolicy` enforcer - unchanged from Day 5's design. `make
cni-install` reconfigures it specifically for Istio ambient
coexistence, all `helm --set` flags on the pinned 1.20.1 chart:

- `ipam.mode=kubernetes` / `kubeProxyReplacement=false` /
  `hubble.enabled=false` - unchanged from Day 5.
- `cni.exclusive=false` - lets Istio's own CNI plugin chain alongside
  Cilium's, rather than Cilium refusing to coexist with a second CNI
  plugin in the chain (the default `cni.exclusive=true` is specifically
  a single-CNI assumption Day 6 must relax).
- `socketLB.hostNamespaceOnly=true` - keeps Cilium's socket-level
  load-balancing (an eBPF optimization that intercepts connect() calls
  in-process) scoped to the host network namespace only, so it does not
  compete with or shadow ztunnel's own per-Pod-netns socket redirection
  - a documented Cilium/ambient interoperability setting.
- `bpf.masquerade` left at its false/default value - unchanged, never
  enabled this stage.
- `envoy.enabled=false` - Day 6 uses no Cilium L7 feature (no
  `CiliumNetworkPolicy` L7 rule, no Cilium Gateway API controller), so
  the standalone `cilium-envoy` L7 dataplane component - present but
  unused since Day 5 (`DAY5-ARCH-L1`) - is disabled outright rather than
  carried forward as more unused footprint.
- `operator.replicas=1` - **explicitly NOT an HA topology.** A single
  Cilium operator replica for this constrained local kind cluster,
  matching Day 5's own architecture review's suggestion for exactly
  this resource-pressure scenario (`DAY5-ARCH-M2/M3`). A production
  deployment would run the default 2-replica operator; this local,
  single-tenant development cluster deliberately does not.

Istio is the **only** Gateway API controller and the **only** mesh
policy layer - no Cilium Gateway API controller and no Cilium L7 policy
are introduced. The division of labor is exact: Cilium enforces
L3/L4 `NetworkPolicy` (which Pod/namespace may reach which Pod/
namespace, on which port) exactly as it has since Day 5; Istio ambient
enforces mesh identity (mTLS, `AuthorizationPolicy` source-principal
matching) on top of that, never instead of it. Both layers deny the
same disallowed paths (`gateway -> state`, the diagnostics/validation-
client identity reaching any application workload) - deliberate
defense in depth, not redundant duplication of a single concern.

## DAY6: strict mTLS and identity authorization

A namespace-wide `PeerAuthentication` (`maops-platform-strict-mtls`, no
selector) sets `mtls.mode: STRICT` for every workload in
`maops-platform`. Three `AuthorizationPolicy` objects (`security.istio.io/v1`,
`action: ALLOW`), one per workload, each naming exactly one allowed
source principal - Istio's semantics mean once ANY `ALLOW`
`AuthorizationPolicy` selects a workload, every request that matches no
such policy is denied, so a single ALLOW-only policy per workload is
also what denies every other identity:

| Selected workload | Allowed source principal |
|---|---|
| `maops-gateway` | `cluster.local/ns/maops-ingress/sa/maops-edge-istio` (the Istio ingress Gateway, Istio's own deterministic naming) |
| `maops-app` | `cluster.local/ns/maops-platform/sa/maops-gateway` |
| `maops-state` | `cluster.local/ns/maops-platform/sa/maops-app` |

Required denied paths hold by the same construction, never by a
separate DENY policy: the diagnostics/validation-client identity
(`cluster.local/ns/maops-day6-validation/sa/maops-diagnostics`) is
never listed as a principal anywhere, and `maops-gateway`'s own
principal is never listed on `maops-state-authz` - `gateway -> state`
stays denied at the mesh identity layer, on top of the NetworkPolicy
deny that already covers it. `scripts/validate_helm_chart.py`'s
`mesh.authz.*` checks assert every one of these exactly, both the
positive principals and the two negative non-memberships.

**L4-compatible identity authorization only, never L7.** Every
`AuthorizationPolicy` rule here uses `source.principals` matching
alone - never a `to.operation` (HTTP method/path) rule, which requires
a waypoint proxy Day 6 does not deploy. This project does not claim
request-level (L7/HTTP) east-west authorization at this stage - the
same `DAY5-SEC-M1`-shaped gap Day 5's own review named (a standard
NetworkPolicy/AuthorizationPolicy pair can restrict WHO may reach a
workload's port, not WHICH HTTP method/path they may call) remains
open, now narrowed to "no waypoint is deployed to close it" rather than
"no mesh exists at all."

**Existing application-layer Secret authentication is unchanged and
still required.** `maops-internal-auth`/`maops-state-auth` (Day 2/4)
remain the application's own credential checks
(`X-MAOPS-Internal-Token`/`X-MAOPS-State-Token`, `hmac.compare_digest`)
- mesh mTLS is a transport-layer identity/confidentiality guarantee
layered underneath, never a replacement for them. A caller with a valid
mesh identity but the wrong (or no) application token still gets `HTTP
403` from the application itself, exactly as in every earlier day.

## DAY6: resource-conscious, explicitly non-HA local development

Every infrastructure Helm invocation this stage adds (`istiod`,
`istio-cni`, `ztunnel`) sets conservative `resources.requests`/`limits`
appropriate for a single-tenant local kind cluster, and the Cilium
operator is pinned to a single, explicitly non-HA replica (see above).
No HPA, no cloud LoadBalancer, no TLS/cert-manager, no observability
stack (Hubble, Kiali, Prometheus, Grafana, Jaeger/tracing - all
explicitly out of Day 6 scope, matching Day 5's own Hubble exclusion),
and no Argo Rollouts/Argo CD are introduced. This project makes no
production-readiness claim at any day, and Day 6 does not change that -
see `docs/roadmap.md`'s "Explicitly out of scope for this project."

**istiod autoscaling is explicitly disabled (DAY6 review INT-2).** The
pinned istiod 1.31.0 chart defaults to `autoscaleEnabled: true` and
renders a `HorizontalPodAutoscaler` (min 1, max 5, CPU target). This
local kind platform installs no metrics-server, so that HPA could only
ever report `<unknown>` metrics and emit `FailedGetResourceMetric`
events. `make mesh-install` therefore passes
`--set pilot.autoscaleEnabled=false` - verified against the pinned
chart itself, whose `zzy_descope_legacy.yaml` merges `pilot.*` onto
the top-level values (the same mechanism the existing
`pilot.resources.*` flags rely on); with the flag the chart renders no
HPA and a fixed `replicas: 1`. Application autoscaling remains outside
Day 6 scope, metrics-server is not installed, and a single,
non-autoscaled istiod replica is a deliberate local-kind choice - **not**
a recommended production Istio availability configuration.

## DAY6: cluster-free CI limitation

`.github/workflows/ci.yml` runs `make ci-check` only - unit tests,
version/chart-version/appVersion/image-tag/Day 6 identity/pinned-
infrastructure-version checks, the frozen k8s/base static manifest
check, and Helm lint/template/static-chart-check (including the values
schema's rejection of controlled invalid fixtures, proven by
`tests/test_helm_chart_values_schema.py`, part of the same unit test
suite `ci-check` runs first). It never creates a kind cluster, never
builds or loads a Docker image, and never installs Cilium or Istio -
every live-cluster proof this stage's implementation is written to
support (`gateway-check`, `mesh-check`, `helm-lifecycle-check`, and the
inherited Day 3-5 live checks) stays a local, manual `make day6-check`
concern, deliberately never automated in GitHub Actions at this stage.
A later portfolio project (Project 5) is where broader, reusable CI/CD
design - potentially including ephemeral-cluster CI - is explored; this
workflow is intentionally minimal.

## DAY6: Day 7 exclusions

Advanced deployment strategies (Recreate, Blue-Green, Canary) are Day 7
scope, built on top of the mesh/routing layer Day 6 introduces rather
than introduced here.
`scripts/helm_lifecycle_check.py`'s bounded `helm upgrade` + `helm
rollback` proof is **Helm release rollback** (returning the SAME
release to a previous Helm revision's config) - a materially different
thing from a Day 7 deployment-strategy demonstration (which compares
how NEW code reaches production: all-at-once with downtime, two full
environments swapped, or a gradually-shifted traffic split) and must
never be conflated with one. Argo Rollouts is never introduced in this
project at any day - Day 7's advanced-strategy demonstrations use
native Kubernetes primitives only, per `docs/roadmap.md`.

## DAY6: live validation sequence

`make day6-check` is the authoritative one-shot Day 6 sequence. Day 6's
actual live results were obtained target-by-target against one
preserved `maops-k8s-day6` cluster, with the remediations recorded in
the sections below applied between stages - see "DAY6: live validation
record" for exactly which results were obtained how, and when (no live
result is claimed there that wasn't actually observed). The order
`make day6-check`'s recipe runs the targets in:

```
tool-check -> test -> version-check -> manifest-check -> helm-lint ->
helm-template -> helm-check -> image-build -> cluster-create ->
gateway-api-install -> cni-install -> cni-status -> context-check ->
mesh-install -> mesh-status -> image-load -> storage-bootstrap ->
storage-hardening-check -> namespace-apply -> secret-bootstrap ->
gateway-apply -> deploy -> ambient-workload-check -> rollout-check ->
scheduling-check ->
discovery-check -> secret-check -> gateway-check -> mesh-check ->
rbac-check -> networkpolicy-check -> smoke -> dependency-check ->
scaling-check -> rolling-update-check -> pdb-check -> state-check ->
persistence-check -> retention-check -> helm-lifecycle-check ->
final-state-check
```

Images are built and loaded before Helm installation, exactly as Day
4/5 required for their own manifest-based deploys; `final-state-check`
is the last step, independently proving every mutating experiment
(scaling, rolling update, PDB/Eviction, persistence, retention, and the
new Helm upgrade/rollback lifecycle check) left the cluster in its
normal healthy baseline before the sequence is considered to have
passed.

## DAY6: live-discovered orchestration remediation

Once `make day6-check` was actually begun against a live
`maops-k8s-day6` cluster, the ordering the previous section documented
turned out to be wrong in one concrete way, and two install steps
lacked a bounded readiness gate of their own. Both are fixed here,
without recreating or otherwise mutating the already-healthy live
infrastructure this pass validates against statically.

**Pre-CNI `NotReady` nodes are expected, not a fault.**
`kind/cluster-day6.yaml` sets `networking.disableDefaultCNI: true` (see
"DAY5: Cilium as the enforcing CNI dataplane" above, carried forward
unchanged for Day 6) - every node comes up genuinely `NotReady`, with
no pod network at all, until Cilium is actually installed via `make
cni-install`. This is the intended, documented behavior of this kind
configuration, not a cluster or bootstrap defect.

**`context-check` must not run immediately after `cluster-create`.**
`scripts/context_check.py` calls `scheduling_check.check_node_topology()`,
which requires all 3 nodes to be `Ready` - so running it directly after
`cluster-create`, before any CNI exists, was guaranteed to fail on a
freshly created cluster purely because of the expected pre-CNI
`NotReady` state above, not because of any real context/topology
problem. `context-check` now runs AFTER `cni-install`/`cni-status` in
both `make day6-check`'s recipe and the documented sequence (see "DAY6:
live validation sequence" above): `cluster-create ->
gateway-api-install -> cni-install -> cni-status -> context-check ->
mesh-install -> mesh-status`. `gateway-api-install` (installing the
Gateway API CRDs via `kubectl apply`) stays BEFORE `cni-install` - CRD
registration is an API-server-level operation that does not require pod
networking, so it does not share `context-check`'s dependency on node
readiness.

**Install submission is distinct from rollout readiness.** A `helm
upgrade --install` call returning 0 proves only that the Kubernetes API
server accepted the release manifests - it does not prove the resulting
DaemonSet/Deployment ever became Ready (image pulls, scheduling,
container startup, and - for Cilium specifically - the CNI actually
attaching to every node all happen asynchronously afterward). `make
cni-install` and `make mesh-install` now each submit their Helm
install(s) and then explicitly wait, with a finite `kubectl rollout
status --timeout=<n>s` per workload, before the recipe is allowed to
report success:

  - `cni-install`: `daemonset/cilium` (180s), then
    `deployment/cilium-operator` (120s), both in `kube-system`.
  - `mesh-install`: `deployment/istiod` (120s), then
    `daemonset/istio-cni-node` (120s), then `daemonset/ztunnel` (120s),
    all in `istio-system` - each wait runs immediately after that
    component's own `helm upgrade --install`, matching Istio's
    documented install order (istiod before istio-cni/ztunnel) rather
    than deferring all three waits to the end.

Every wait uses the explicit Day 6 `--kubeconfig`/`--context` (never an
ambient current-context), exactly as every other kubectl/helm
invocation in this Makefile already does. On timeout, the recipe prints
read-only diagnostics - node status, the specific workload object, its
Pods (queried as their own separate `kubectl get pods -l ...` call,
never mixed into the same `kubectl get` invocation as the named
workload), a `kubectl describe` of the workload, and the namespace's
most recent Events - and then exits nonzero; a rollout failure can never
produce a downstream `PASS` message, and no diagnostic command's own
possible failure (wrapped in a narrowly-scoped `|| true`, never applied
to the actual rollout-status check itself) can suppress that nonzero
exit. `make mesh-status` (`scripts/mesh_status.py`) remains the
authoritative, independent, read-only post-install verification of
istiod/istio-cni/ztunnel health - these install-time waits exist to fail
fast and loud at the point of the actual problem, not to replace it.

**Shell failure semantics.** Both `cni-install`'s and `mesh-install`'s
recipes now begin with `set -euo pipefail` (invoked via `bash -c`,
matching this Makefile's own top-level `SHELL := /bin/bash` /
`.SHELLFLAGS := -eu -o pipefail -c`, rather than the portable-but-
weaker `sh -c` these two recipes previously used, which had neither
`errexit` nor `pipefail` and could silently continue past a failed
`helm repo update`). This closes a real gap: `.SHELLFLAGS` only governs
the shell Make itself invokes for a recipe LINE, not a nested `bash -c`/
`sh -c` sub-invocation inside that line's own text, so every such nested
wrapper needs its own explicit strict-mode declaration to get the same
guarantee.

Static validation was re-run and passed after this fix (the
intermediate figures were reported in that pass's working session and
are superseded by "DAY6: live validation record" below). The fix was
a Makefile/orchestration-text and mocked-unit-test correction; the
corrected install-and-readiness order was then exercised live on the
preserved cluster (`make cni-status`, `make context-check`, `make
mesh-install`, `make mesh-status` - see the record below).

## DAY6: live rollout remediation - fsGroup for projected Secret volumes

With the application actually deployed to the live `maops-k8s-day6`
cluster for the first time, `maops-app` came up with every dependency
otherwise healthy - DNS and TCP connectivity to `maops-state` both
worked, Cilium recorded no drops, and the live AuthorizationPolicy
objects were correct with ztunnel reporting the workload's identity as
accepted - but the application process itself failed reading its own
mounted Secret file:

```
PermissionError: [Errno 13] Permission denied: '/var/run/secrets/maops-state/state-token'
```

**Root cause: `runAsGroup` sets the process's primary group; it does
not by itself change who owns a projected Secret volume's files.**
Both runtime Secrets (`maops-internal-auth`, `maops-state-auth`) are
mounted with `defaultMode: 288` (0440 octal - owner and group
read-only, deliberately unchanged by this remediation and every
workload's own securityContext already ran as `runAsUser: 10001,
runAsGroup: 10001`). The missing piece is that a projected Secret
volume's files are written by the kubelet, and by default they are
owned by `root:root` (or, more precisely, whatever the volume plugin's
own default is) - **`fsGroup` is the specific Pod-level field that
tells the kubelet to change the mounted volume's group ownership to
match**, which is what actually makes a 0440 file readable by a
non-root process whose primary group is `10001`. Setting `runAsGroup:
10001` alone (which every workload already had) proves only that the
*process* believes its own primary group is `10001` - it has no effect
on what group the kubelet actually wrote the volume's files as. This is
precisely why `maops-state`, which already carried `fsGroup: 10001` (see
`state-statefulset.yaml`, present since it was first written) could
already read its own `state-token` mount, while `maops-app` and
`maops-gateway` - which had `runAsGroup: 10001` but no `fsGroup` at all
- could not.

A secondary, structural reason this was not caught statically: the
application loads its Secret-backed tokens once, at process startup
(see `app/server.py`) - so a Pod that already existed before a chart fix
lands does not self-heal by simply having its Secret re-synced; the fix
requires an actual Pod replacement (a new `helm upgrade --install`
triggering the Deployment's own `RollingUpdate` strategy), never a
live in-place file-permission patch.

**Fix**: `fsGroup: 10001` and `fsGroupChangePolicy: OnRootMismatch` were
added to `maops-app`'s and `maops-gateway`'s Pod-level `securityContext`
in `charts/maops-kubernetes-platform/templates/app-deployment.yaml` and
`gateway-deployment.yaml`, matching exactly what `maops-state` already
had - never a hardcoded literal, but `{{ .Values.podSecurityContext.runAsGroup }}`,
the same single source of truth `state-statefulset.yaml` already used.
`fsGroupChangePolicy: OnRootMismatch` (rather than the default `Always`)
avoids an unconditional recursive chown/chmod of the volume on every Pod
start once it is already correct - a real, if usually small, cost on a
Secret volume that is otherwise unnecessary after the first correct
mount. Nothing else changed: `runAsNonRoot`/`runAsUser`/`runAsGroup`/
`seccompProfile` are preserved exactly as they were; the Secret objects,
their token values, `defaultMode: 288`, every NetworkPolicy/
AuthorizationPolicy/PeerAuthentication/Gateway/HTTPRoute object, and the
Cilium/Istio installation are all untouched.

`scripts/validate_helm_chart.py`'s `_check_security_context()` now also
asserts, for every one of gateway/app/state (the chart's complete set of
Secret-mounting workloads): `fsGroup == 10001`, `fsGroupChangePolicy ==
'OnRootMismatch'`, that each workload actually mounts at least one of
`maops-internal-auth`/`maops-state-auth` as a Secret volume, and that
every such volume's `defaultMode` is still exactly `288` (0440) - a
regression that weakened the Secret's own file mode, not just a missing
`fsGroup`, is caught by the same check. `container UID/GID remain
10001` was already covered by the pre-existing `runAsUser`/`runAsGroup`
checks (unaffected by this remediation, and now explicitly re-asserted
by `FsGroupSecretProjectionTests` in `tests/test_validate_helm_chart.py`
to prove `fsGroup`'s addition never drifted them).

Live rollout, `make deploy` + `make rollout-check` against the existing
`maops-k8s-day6` cluster (no cluster recreation, no image rebuild/reload
- the fix is Pod-spec-only, and the application's own code is
unchanged) brought every workload Pod Ready with the Secret files still
mode 0440 and now group-readable by GID 10001 (checked via `os.stat`,
never token contents). In the release history, revision 1
(2026-09-22) is recorded as failed with `maops-app` exceeding its
progress deadline - consistent with this pre-fix failure - and
revision 2 (2026-09-22) is the first healthy deployment and the stable
baseline used by every later live check; see "DAY6: live validation
record" below.

## DAY6: second remediation after independent review

A second independent review of this implementation pass found and this
batch fixed eight issues, none requiring live cluster contact to
correct (this remains a static-only remediation pass):

1. **Istio Gateway `parametersRef` schema was wrong.** The ConfigMap
   used a single `data["values.yaml"]` wrapper - not the schema Istio's
   Gateway API deployment controller actually reads. Corrected to the
   real per-resource-kind strategic-merge-patch keys (`deployment`,
   `service`, `serviceAccount`) - see
   `k8s/day6/gateway-values-configmap.yaml`'s own header comment. This
   also corrected the generated proxy's ServiceAccount identity: Istio
   names it deterministically as `<gateway-name>-istio`
   (`maops-edge-istio`), never a bare override - every
   AuthorizationPolicy/constant/test/doc now agrees on this one name
   (`scripts/kube.py`'s `GATEWAY_PROXY_SERVICE_ACCOUNT`). A new static
   checker (`scripts/validate_gateway_values_configmap.py`, wired into
   `make helm-check`) asserts the exact corrected schema and explicitly
   rejects the old wrapper if it ever reappears.
2. **The HBONE NetworkPolicy assumed ztunnel could be matched as an
   ordinary namespaced Pod peer.** It cannot - ztunnel is a per-node
   `hostNetwork: true` DaemonSet, so its traffic carries the NODE's
   identity, not a routable Pod identity a `namespaceSelector` can
   resolve. The rule is now deliberately peer-less (port 15008 only) -
   see "HBONE (port 15008)" above for the corrected design and the
   identity-boundary explanation (Cilium/NetworkPolicy controls
   reachability only; Istio `AuthorizationPolicy` is what actually
   enforces the `gateway -> app -> state` identity chain).
3. **The Cilium ambient health-probe policy was too broad and
   over-claimed IP-family coverage.** `endpointSelector` is now
   narrowed to `maops-platform` (via Cilium's reserved
   `k8s:io.kubernetes.pod.namespace` label) AND this project's own
   workload label. The policy is documented as IPv4-only, matching
   this project's kind clusters (no `networking.ipFamily` override
   anywhere - kind's own IPv4 default) - no IPv6 rule is claimed or
   added.
4. **NetworkPolicy probe results were two-state (connected/not),
   which could manufacture a false DENIED result.**
   `scripts/networkpolicy_check.py` now classifies every probe into
   exactly one of `CONNECTED` (any completed HTTP response, regardless
   of status - a non-2xx is never a NetworkPolicy signal),
   `BLOCKED` (a genuine bounded connection TIMEOUT - the real
   silently-dropped-packet signature), or `INCONCLUSIVE` (a `kubectl
   exec` failure/timeout, unparseable output, or a non-timeout
   connection error such as `ConnectionRefusedError` - never treated as
   denial proof). Only `BLOCKED` satisfies a DENIED assertion.
5-6. **No live proof existed that an ambient-enrolled but unauthorized
   identity is actually denied** (as opposed to merely rejected for
   being unencrypted - a materially weaker, different proof).
   `scripts/mesh_check.py` now creates a temporary, dedicated,
   ambient-enrolled namespace/ServiceAccount/Pod (`maops-day6-mesh-
   probe`, never the normal `maops-day6-validation` namespace, which
   stays deliberately non-ambient), proves it is genuinely
   ambient-enrolled (no sidecar, the redirection annotation present),
   then proves it is denied reaching gateway/app/state while the real
   production identity paths (gateway -> app, app -> state) still
   succeed - with guaranteed namespace cleanup and the same
   INCONCLUSIVE-is-never-denial-proof discipline as (4). Documented
   scope boundary: because this project's NetworkPolicy peers are
   same-namespace/specific-label scoped, this test proves the COMBINED
   NetworkPolicy + AuthorizationPolicy denial outcome, not an isolated
   AuthorizationPolicy-only signal - attributing the denial to one
   specific layer would need tooling (`istioctl`, packet capture)
   outside this project's ground rules.
7. **The wrong-Host Gateway check accepted too weak a "no route"
   signal.** It used to treat "anything other than a 200-with-gateway-
   body" as proof of no route - including an unrelated 5xx or an empty
   200. `scripts/gateway_check.py` now requires the definite, documented
   Envoy/Istio no-route signal (HTTP 404) specifically; unreachability
   (connection refused/timeout reaching the NodePort) is its own
   explicit INCONCLUSIVE outcome, never counted as "no route" either.
8. **Documentation corrected** wherever it implied Cilium/NetworkPolicy
   can see workload identity through HBONE, or that the ambient
   redirection annotation was an unverified guess rather than Istio's
   own documented mechanism - see "HBONE (port 15008)" above and
   `scripts/mesh_check.py`'s module docstring.

## DAY6: third remediation after independent review

A third independent review found and this batch fixed six further
issues, again all correctable without live cluster contact:

1. **Probe classification still conflated TCP-connect and HTTP-read
   timeouts.** The second remediation's CONNECTED/BLOCKED/INCONCLUSIVE
   model still wrapped TCP connect, HTTP request, and response read
   into one undifferentiated try/except, so an HTTP-layer read timeout
   after a successful TCP handshake could still be misread as
   `BLOCKED`. `scripts/networkpolicy_check.py` now distinguishes seven
   phase-attributed states - `TCP_CONNECT_TIMEOUT`, `TCP_CONNECTED`,
   `HTTP_RESPONSE`, `HTTP_READ_TIMEOUT`, `CONNECTION_REFUSED_OR_RESET`,
   `EXEC_INCONCLUSIVE`, `OUTPUT_INCONCLUSIVE` - via two purpose-built
   probes: a TCP-connect-only probe (used for every NetworkPolicy
   reachability assertion) and a separate, phase-separated HTTP health
   probe (`conn.connect()` in its own try/except, distinct from the
   request/read try/except) reserved for positive-path application
   validation. Only `TCP_CONNECT_TIMEOUT` satisfies a DENIED assertion.
   Verified against real local sockets/servers (connection refused,
   connect timeout to an RFC 5737 TEST-NET address, a real HTTP
   response, and a real read-timeout-after-connect scenario), not just
   hand-constructed strings.
2. **The wrong-identity mesh check described a combined Cilium+Istio
   denial as proof AuthorizationPolicy works.** It could not isolate
   which layer produced the denial (this project's NetworkPolicy peers
   are same-namespace-scoped, so a probe outside those namespaces is
   denied by NetworkPolicy regardless of mesh identity).
   `scripts/mesh_check.py`'s `check_authorization_denial_isolated()`
   now temporarily raises the ztunnel DaemonSet's log level (`kubectl
   set env daemonset/ztunnel RUST_LOG=info,access_log=info`, captured
   and restored in a guaranteed `finally`, with the rollout verified
   both ways), re-runs the wrong-identity connection attempts within
   that window, and correlates ztunnel's own logs against BOTH the
   probe's real principal and each destination workload - separately
   checking for evidence the HBONE/mTLS transport was ALLOWED and
   evidence Istio's RBAC/AuthorizationPolicy layer then denied the
   authenticated principal. Only when both are found does the script
   claim AuthorizationPolicy-specific isolation; anything less is its
   own distinct, un-overclaimed finding. No `istioctl` dependency was
   added - `kubectl set env`/`kubectl logs` only.
3. **Probe/namespace cleanup trusted `--wait=false` alone.**
   `scripts/networkpolicy_check.py`'s `delete_pod_and_verify_gone()`
   and `scripts/mesh_check.py`'s `delete_namespace_and_verify_gone()`
   now submit deletion, then POLL `kubectl get pod`/`kubectl get
   namespace` until it actually returns NotFound (bounded, with a
   stuck-Terminating timeout reported as a restoration failure, never
   silently treated as success); the namespace variant additionally
   re-checks the specific probe Pod/ServiceAccount names directly.
   `scripts/final_state_check.py`'s
   `check_no_leaked_mesh_probe_namespace()` is the new whole-suite-level
   backstop confirming the temporary mesh-probe namespace never leaks
   past a full `day6-check` run.
4. **HBONE documentation still implied application-port NetworkPolicy
   selectors govern the real ambient traffic path.** Corrected,
   precisely: the peerless HBONE rule permits ambient TRANSPORT; Cilium
   controls reachability only and cannot see workload identity once
   traffic is HBONE-encapsulated; Istio `AuthorizationPolicy` is what
   controls authenticated workload identity for the real ambient path;
   the application-port NetworkPolicies remain meaningful only for
   plaintext/non-ambient paths where the original peer is actually
   visible to Cilium (kept, unweakened, to cover that case - not
   because they enforce the real ambient-tunneled path today);
   application Secrets remain a third, independent layer. See "HBONE
   (port 15008)" above and every application-port NetworkPolicy
   template's own header comment.
5. **Gateway `parametersRef` wording overstated uncertainty about the
   schema itself.** The per-resource-kind `deployment`/`service`/
   `serviceAccount` schema (corrected in the second remediation) is
   Istio's documented mechanism, not an unresolved assumption - wording
   implying otherwise is removed. What the first live run actually
   confirms is narrower: the RENDERED CONTROLLER BEHAVIOR (exact
   `targetPort`/`nodePort`, token-projection behavior, the generated
   `maops-edge-istio` ServiceAccount name) on this project's pinned
   Istio 1.31.0 - confirming behavior against a known schema, not
   discovering the schema itself.
6. **Static validation re-run and passing** after all of the above
   (intermediate figures, superseded by "DAY6: live validation record"
   below).

## DAY6: fourth remediation after independent review

A fourth independent review found and this batch fixed four further
issues in `scripts/mesh_check.py` (plus the matching tri-state fix
carried into `scripts/networkpolicy_check.py` and
`scripts/final_state_check.py`), again all correctable without live
cluster contact:

1. **The ztunnel `RUST_LOG` diagnostic mutation was not actually
   transactional.** The third remediation's `_set_ztunnel_env()`
   combined "submit the `kubectl set env` change" and "wait for the
   resulting rollout" into one function with one return value - and its
   caller returned early, skipping restoration entirely, whenever that
   combined call reported failure. That is a real gap: a diagnostic
   rollout that timed out happened strictly AFTER the mutation was
   already accepted by the API, so returning early on that failure could
   leave ztunnel's log level mutated indefinitely. Mutation submission,
   rollout completion, diagnostic work, and restoration are now four
   distinct steps (`_submit_ztunnel_env_literal`/
   `_submit_ztunnel_env_unset`, `_wait_ztunnel_rollout`, the diagnostic
   probe/log-correlation block, and `_restore_ztunnel_log_env`). Once
   submission succeeds, restoration runs in a guaranteed `finally` no
   matter what happens next - a diagnostic rollout timeout, a failed
   diagnostic Pod, a log-retrieval failure, a parsing failure, a probe
   raising, or any later step returning early. The original state is now
   captured completely (`ZtunnelLogEnvState`: was the env var absent, did
   it carry a literal `value`, or did it use `valueFrom`) rather than
   just a `.value` string. Because `kubectl set env` can only set a
   literal or remove an entry - never restore a `valueFrom` reference
   exactly - the script refuses to mutate `RUST_LOG` at all when the
   original entry uses `valueFrom` (fail BEFORE mutation, the simpler and
   safer of the two options available here, since a `kubectl
   patch`-based exact restore was never live-tested). After restoration, the script waits for that rollout too,
   REREADS the DaemonSet, verifies `RUST_LOG` exactly matches its
   original representation (present/value/`valueFrom` all compared), and
   verifies every ztunnel Pod is Ready - any uncertainty at any of those
   steps is its own explicit RESTORATION FAILURE finding, never silently
   treated as success.
2. **Cleanup verification conflated "confirmed NotFound" with "the API
   call itself failed for some other reason."** The third remediation's
   `_namespace_exists()`/`_resource_exists()` (in `mesh_check.py`) and
   `_pod_exists()` (in `networkpolicy_check.py`) were bare booleans keyed
   off `returncode == 0` - so a connection refusal, timeout, Forbidden,
   or authentication failure while polling looked identical to "the
   resource genuinely doesn't exist," and a cleanup loop built on that
   boolean could either falsely declare success or spin forever unable
   to tell the two apart. All three are now a tri-state
   `_resource_state()`/`_pod_state()` result - `EXISTS`, `NOT_FOUND`, or
   `API_ERROR` - built on `kubectl get ... --ignore-not-found`, which is
   documented specifically to make this distinction (exit 0 with EMPTY
   stdout means genuinely absent; exit 0 with stdout means present; any
   other nonzero exit is some other, unresolved API error). Only an
   explicit `NOT_FOUND` proves deletion; an `API_ERROR` immediately fails
   the cleanup it was checked from, never silently retried-through as if
   it might still resolve to "gone." This applies to
   `delete_namespace_and_verify_gone()`, the validation probe Pod's
   `delete_pod_and_verify_gone()`, and `final_state_check.py`'s
   `check_no_leaked_mesh_probe_namespace()` whole-suite backstop - a
   connection failure checking for the leaked namespace is now its own
   distinct failure, never silently read as "not leaked."
3. **The wrong-identity denial's log correlation used GUESSED marker
   words, not documented ztunnel fields.** The third remediation's
   `ALLOW_TRANSPORT_LOG_MARKERS`/`DENY_LOG_MARKERS` ("mtls",
   "established", "accept", "handshake" for transport; "rbac", "deny",
   ... for denial) had no claim to match ztunnel's actual log format.
   `_parse_access_log_fields()` now extracts real `key=value`/
   `key="value"` pairs, and transport proof
   (`_find_transport_evidence()`) is built entirely on ztunnel's
   documented structured access-log shape: the literal markers `access`
   and `connection complete`, and the fields `src.identity`,
   `dst.identity`, `dst.hbone_addr`, `dst.service`, and `direction`. The
   probe's identity is now represented in both forms this project uses -
   the AuthorizationPolicy principal form
   (`cluster.local/ns/.../sa/...`, `MESH_PROBE_PRINCIPAL`, unchanged) and
   the SPIFFE log form (`spiffe://cluster.local/ns/.../sa/...`,
   `MESH_PROBE_SPIFFE_IDENTITY`) - and transport correlation matches
   `src.identity` against the SPIFFE form specifically, since that is the
   form ztunnel's own logs actually carry, never the bare principal form.
   For denial evidence specifically, this (pre-live) batch had not yet
   observed Istio 1.31's exact deny-log format - superseded by "DAY6:
   live mesh-check remediation" below, which added the AUTHORITATIVE
   tier and dropped the `connection complete` filter - so at the time
   `_find_denial_evidence()` deliberately did not claim one:
   it first looks for a `CANDIDATE` - a field-parseable line correlated
   by identity and destination that never reached `connection complete`,
   built entirely from the documented fields above - and only falls back
   to an explicitly-labeled `BEST_EFFORT` free-text token search if that
   fails. Finding neither is `NONE`, recorded as INCONCLUSIVE, never as a
   confident "not denied." **This repository does not promote the
   `BEST_EFFORT` parser to a pinned, authoritative deny-log format until
   the first live `make mesh-check` run has actually observed Istio
   1.31.0's real deny-log output on this cluster** - until then, any
   `BEST_EFFORT` or `NONE` finding is exactly what it says: an unverified
   guess or an absence of correlated evidence, never an Istio contract.
   Per this remediation's explicit preference, `check_authorization_denial_isolated()`
   now tries the ztunnel DaemonSet's CURRENT (default, unmutated) log
   level FIRST - if it already contains identity/HBONE transport evidence
   for every target, the `RUST_LOG` mutation described in item 1 above is
   skipped entirely and never attempted; the mutation path is retained
   only as a fallback for when the default level does not already
   suffice.
4. **A client-side connection timeout was the only accepted signal for
   an Istio AuthorizationPolicy denial, but ztunnel may legitimately
   reset/close a denied connection instead of silently timing it out.**
   `networkpolicy_check.assert_denied()` stays strict and UNCHANGED
   (`TCP_CONNECT_TIMEOUT` only) for its own NetworkPolicy-specific
   callers - that remains the correct, narrow signature of a silently
   dropped packet. But `mesh_check.py`'s wrong-identity client-side probe
   now uses a SEPARATE mesh-authorization outcome model,
   `_record_client_side_supporting_evidence()`: both
   `TCP_CONNECT_TIMEOUT` and `CONNECTION_REFUSED_OR_RESET` are recorded
   as non-gating SUPPORTING evidence only - neither, alone, proves the
   identity was denied specifically by AuthorizationPolicy (a
   NetworkPolicy-level drop, or an unrelated connectivity problem, could
   produce either symptom too). The identity-denial assertion itself is
   now made EXCLUSIVELY by `check_authorization_denial_isolated()`'s
   correlated ztunnel log evidence (item 3 above) - client-side behavior
   alone can never satisfy it. The one exception: if the wrong identity's
   connection actually SUCCEEDS at the client layer
   (`TCP_CONNECTED`/`HTTP_RESPONSE`), that directly contradicts the
   test's premise and is still recorded as a genuine, gating failure.

Static validation was re-run and passed after all four fixes - that
batch's intermediate figures, superseded by "DAY6: live validation
record" below, were (1028 unit tests,
`version-check` 50/50, `manifest-check` 267/267, `helm-lint`/
`helm-template` clean, `helm-check`/`ci-check` 177/177, `git diff
--check` clean, k8s/base and `docs/engineering-reviews/` untouched, no
live cluster/API contact, nothing staged/committed/pushed/tagged).

## DAY6: live mesh-check remediation - ambient TCP-connect vs AuthorizationPolicy denial

The first actual live `make mesh-check` run against `maops-k8s-day6`
(with scheduling-check, discovery-check, secret-check, and gateway-check
all already passing) reported 39/42 - not because the mesh was
misconfigured, but because the checker's own client-side model was
wrong about what a raw TCP `connect()` proves in ambient mode.

**Observed behavior**: all three wrong-identity raw TCP `connect()`
probes (against gateway, app, and state) reported `TCP_CONNECTED`.
Despite that, ztunnel's own access logs contained correlated denial
records for all three destinations - the exact wrong SPIFFE source
identity, the correct destination, `bytes_sent=0`/`bytes_recv=0`, and
one of two exact errors:

```
error="http status: 401 Unauthorized"
error="connection closed due to policy rejection: allow policies exist, but none allowed"
```

The temporary probe namespace was confirmed `NOT_FOUND` on cleanup,
default (unmutated) ztunnel logs already contained sufficient evidence
(so `RUST_LOG` was never mutated), and all workloads/mesh components
stayed Ready throughout.

**Root cause: a completed source-side TCP `connect()` is not equivalent
to destination-side AuthorizationPolicy acceptance in ambient mode.**
Istio ambient's transparent interception means a source Pod's raw
`connect()` completes against its OWN NODE-LOCAL ztunnel (which accepts
the client socket as part of redirecting it into the mesh) BEFORE the
destination-side HBONE tunnel is established and evaluated against
AuthorizationPolicy. `TCP_CONNECTED` therefore proves only that the
local ztunnel accepted the client socket - never that the request
reached, or was authorized to reach, the destination application. The
previous revision of `scripts/mesh_check.py` treated a client-side
`TCP_CONNECTED` on the wrong-identity probe as a hard, gating failure
("the connection unexpectedly succeeded") - live, this was simply wrong
for ambient mode, and hard-failed a check that was actually working
correctly.

**Fix, `scripts/mesh_check.py` only** (`networkpolicy_check.assert_denied()`
and its NetworkPolicy semantics are UNCHANGED - this remediation is
specific to mesh identity validation):

  - `_record_client_side_supporting_evidence()` no longer hard-fails on
    any raw-TCP outcome. `TCP_CONNECTED` is relabeled
    `LOCAL_ZTUNNEL_CONNECT_ACCEPTED` in this script's own output and
    recorded as non-gating SUPPORTING evidence, exactly like
    `TCP_CONNECT_TIMEOUT`/`CONNECTION_REFUSED_OR_RESET`/an INCONCLUSIVE
    probe already were - none of them, alone, ever satisfies OR
    refutes the identity-denial assertion.
  - The actual application-reachability leak check moved to the HTTP
    layer: `_record_client_side_http_leak_check()` reuses the existing
    phase-separated HTTP probe building block
    (`networkpolicy_check._http_probe_snippet()`) against each
    destination's `/livez` path. HTTP 200 is the one hard, gating
    failure it reports - proof the unauthorized request was actually
    served. A ztunnel-generated HTTP 401 (or any other non-200 status),
    a reset/closed connection, a read timeout, or an INCONCLUSIVE probe
    are all non-gating supporting evidence only.
  - The identity-denial assertion itself is, as before, made EXCLUSIVELY
    by `check_authorization_denial_isolated()`'s correlated ztunnel log
    evidence - this boundary is unchanged and, if anything, more
    load-bearing now that BOTH client-side layers (TCP and HTTP) are
    non-gating.
  - The two exact denial error strings above are now AUTHORITATIVE
    denial evidence in `_find_denial_evidence()` - never an unverified
    guess, an exact match against what this project's own pinned Istio
    1.31.0/Cilium 1.20.1 combination has actually been observed to emit
    on this cluster: the explicit policy-rejection error is
    authoritative correlated on its own; the HBONE 401 error is
    authoritative ONLY when also correlated with `bytes_sent=0`/
    `bytes_recv=0` (proving no application data was ever exchanged - a
    401 that DID exchange bytes is a materially different, unverified
    situation and stays at `CANDIDATE`). Any other error string, or an
    uncorrelated generic "401"/"denied" substring, remains exactly what
    it already was - `CANDIDATE` or `BEST_EFFORT`, never silently
    upgraded.
  - **Correction to a prior assumption**: earlier revisions treated
    ztunnel's `connection complete` access-log marker as evidence a
    connection was ALLOWED, and excluded any line carrying it from
    denial-evidence consideration. Live output proved that wrong -
    ztunnel emits `connection complete` for REJECTED connections too,
    carrying an `error=` field describing why. `_find_denial_evidence()`
    no longer filters on that marker at all; the actual signal is the
    presence and content of the `error=` field. This is also why the
    live run's default (unmutated) logs already satisfied
    `_find_transport_evidence()`'s "connection complete" check for a
    REJECTED wrong-identity connection - that check proves local
    ztunnel activity occurred, never an authorization outcome either
    way (its own docstring is corrected to say so).

`scripts/networkpolicy_check.py` was not modified by this remediation -
its `assert_denied()` stays strict (`TCP_CONNECT_TIMEOUT` only) for its
own NetworkPolicy-specific callers, exactly as the fourth remediation
already established.

Regression tests added to `tests/test_mesh_check.py` cover, using the
exact live Istio 1.31 log shapes observed: `TCP_CONNECTED` plus a
correlated 401 denial passing; `TCP_CONNECTED` plus a correlated
explicit policy-rejection passing; `TCP_CONNECTED` with no correlated
denial evidence still failing/INCONCLUSIVE; an HTTP `/livez` 200 hard-
failing even alongside an unrelated correlated denial record; an HTTP
401 alone never satisfying denial without correlated ztunnel fields;
wrong source identity and wrong destination both failing to match; and
nonzero application bytes never qualifying for the structured 401 form.
The pre-existing transactional `RUST_LOG`/cleanup tests and the allowed
gateway->app/app->state positive-control tests were left intact and
re-verified passing.

Static validation was re-run and passed after this fix. After static
validation, `make mesh-check`, `make mesh-status`, and `make
rollout-check` were re-run against the live, preserved
`maops-k8s-day6` cluster (no cluster recreation, no Cilium/Istio/
Gateway API reinstall); the corrected `mesh-check` passed 45/45 with
its probe namespace confirmed removed - see "DAY6: live validation
record" below for the result and its denial-evidence tier breakdown.

## DAY6: live networkpolicy-check remediation - isolated non-ambient probes

The next live `make networkpolicy-check` run (after `mesh-status`,
`rollout-check`, and `rbac-check` all passed) reported `gateway ->
state DENIED` as `TCP_CONNECTED` - the same fundamental ambient-mode
observation error already corrected in `scripts/mesh_check.py`'s fifth
remediation and `networkpolicy_check.py`'s own third remediation (see
above), now discovered in this script's OWN previous design. A second,
independent finding in the same run - a probe Pod reported as "did not
disappear within 30 seconds" when the very next diagnostic read already
showed it gone - was a cleanup deadline boundary race, not a real leak.

**Why a raw `connect()` from an ambient Pod proves only local ztunnel
socket acceptance.** The previous revision of `networkpolicy_check.py`
ran its `gateway -> app`/`gateway -> state`/`app -> state` application-
port assertions directly from the REAL, ambient-enrolled `maops-gateway`/
`maops-app` Pods. In ambient mode, a source Pod's outbound `connect()`
is transparently redirected to its own NODE-LOCAL ztunnel before the
destination is ever reached - so a completed TCP handshake proves only
that the local ztunnel accepted the client socket, never that the
destination workload was reached or that Cilium's NetworkPolicy allowed
the raw path through in the way this script's classification model
assumed. This was never a valid test for either the ALLOWED or the
DENIED direction, once maops-platform became ambient-enrolled.

**Why application-port NetworkPolicy checks now require non-ambient
isolated probes, and why `istio.io/dataplane-mode: none`.** The fix,
`check_application_port_networkpolicy_isolated()`, creates four
temporary Pods directly in `maops-platform` - two sources (gateway-
labeled, app-labeled) kept alive for `kubectl exec`, two targets (app-
labeled, state-labeled) each running a small Python TCP listener on
port 8080 - each carrying `istio.io/dataplane-mode: none`, Istio's own
documented PER-POD override of a namespace's `istio.io/dataplane-mode:
ambient` label (maops-platform carries the ambient label at the
namespace level - see "DAY6: Istio ambient service mesh" above). This
opts each probe Pod OUT of ambient redirection, so its raw `connect()`
is genuinely unredirected and isolates Cilium's NetworkPolicy
enforcement exactly the way the (unaffected, never-ambient)
`validation-client` probe already did. Each probe Pod carries ONLY the
single `app.kubernetes.io/component` label the applicable NetworkPolicy
selector actually keys on - never the complete live Deployment/
StatefulSet/Service label set, which risks accidentally matching an
unrelated selector or Service. Before any assertion runs, every probe
Pod is independently verified Running, carrying that label, carrying NO
`ambient.istio.io/redirection` annotation, no `istio-proxy` sidecar,
no `ownerReferences` (never adopted by a controller), and absent from
every live application Service's EndpointSlice - never assumed from how
the Pod was created.

**Why direct temporary Pod IPs are used.** Every assertion connects
directly to a target probe Pod's own `status.podIP`, never a Service -
so the probe can never alter or depend on live Service endpoint
selection, and the direct-IP safety check above has something concrete
to verify against.

**Why ambient identity enforcement remains `mesh_check.py`'s
responsibility.** `networkpolicy_check.py` now verifies Cilium/
Kubernetes NetworkPolicy using isolated, non-ambient plaintext probes
only; `mesh_check.py` verifies ambient mTLS and identity-scoped Istio
AuthorizationPolicy, live, against the REAL ambient-enrolled Pods.
Neither script claims to be able to attribute a real ambient Pod's
combined-path result to the other layer specifically - that combined-
path ambiguity is exactly why `networkpolicy_check.py` no longer uses
real ambient Pods for its own raw-TCP assertions at all.
`networkpolicy_check.assert_denied()` itself is UNCHANGED and stays
strict (`TCP_CONNECT_TIMEOUT` only, for an isolated non-ambient probe) -
this remediation fixed WHICH Pods call it, never what it accepts.

**Why the cleanup verifier performs a final boundary read.**
`delete_pod_and_verify_gone()`'s bounded polling window widened from
30s to 75s, and - if the normal polling loop never observes an explicit
`NOT_FOUND` before that deadline - performs exactly ONE final, fresh
tri-state read before deciding failure: `NOT_FOUND` there still passes
(labeled as a boundary-read pass, for transparency), `EXISTS` still
fails, and `API_ERROR` still fails immediately. This directly addresses
the observed race (a Pod disappearing right around the polling
deadline) without ever using forced deletion to manufacture a pass -
the same tri-state EXISTS/NOT_FOUND/API_ERROR model is unchanged, only
given one more genuinely fresh look before giving up.
`final_state_check.py` gained a matching whole-suite-level backstop,
`check_no_leaked_networkpolicy_probe_pods()` (this protection was
genuinely absent before - the four new probe Pods had no independent
leak check), matching the existing `check_no_leaked_mesh_probe_namespace()`
pattern but as a label-key list query (there is no single fixed name to
check against).

**What was NOT changed:** no NetworkPolicy, AuthorizationPolicy,
PeerAuthentication, Cilium, Istio, Gateway, HTTPRoute, Secret,
application code, or workload configuration was weakened or modified to
make any assertion pass - every fix here is confined to which Pods
`networkpolicy_check.py` uses to observe the (unchanged) enforcement
and how bounded cleanup verification tolerates a genuine deadline
boundary race.

Static validation was re-run and passed after this fix. After static
validation, `make mesh-status`, `make rollout-check`,
`make networkpolicy-check`, `make rollout-check`, and `make mesh-status`
were re-run against the live, preserved `maops-k8s-day6` cluster (no
cluster recreation, no Cilium/Istio/Gateway API reinstall, no image
rebuild/reload, no Secret rotation, no redeploy); the corrected
`networkpolicy-check` passed 37/37 with every probe Pod confirmed gone
- see "DAY6: live validation record" below.

## DAY6: live-discovered Helm ConfigMap rollout remediation

The final Day 6 live-validation stage reached `make helm-lifecycle-check`,
which probes a real `helm upgrade` + `helm rollback` cycle against a
purely cosmetic value (`gateway.config.appMessage`). The upgrade and
rollback both reported success, and rollback correctly restored the
original state - but the externally-routed `/config` never showed the
probe's new message during the upgrade window at all. The cluster was
left fully restored; this was never a restoration failure.

**Root cause, confirmed read-only via `helm get manifest --revision`
before any fix was written**: the probe revision's `maops-gateway-config`
ConfigMap DID contain the new message - the values/template mapping was
never wrong - but the gateway Deployment's rendered Pod template was
byte-for-byte IDENTICAL between the baseline and probe revisions, and
the live gateway Pods (same UIDs, same ReplicaSet, unchanged since the
very first successful deploy) were never replaced across either
revision. **Helm updating a ConfigMap does not automatically restart
Pods that consume it as environment variables** - `envFrom`-sourced
ConfigMap data is read only once, at container startup, and Kubernetes
only creates a new ReplicaSet/rolls Pods when the Pod template ITSELF
changes. Since nothing in the Deployment's Pod template referenced the
ConfigMap's content, no new rollout was ever triggered - **`kubectl
rollout status` reported success because it was trivially, instantly
true: an already-healthy, completely unchanged Deployment is by
definition "successfully rolled out"**, not because a new rollout had
actually happened.

**Fix (chart)**: every workload that consumes a ConfigMap through
environment variables (`maops-gateway`, `maops-app`, `maops-state`) now
carries a deterministic `checksum/config` annotation - a sha256 digest
of that workload's OWN ConfigMap template's rendered content - under
`spec.template.metadata.annotations` (the Pod template specifically,
never the workload's own top-level `metadata.annotations`, which does
not influence rollout behavior at all). **A deterministic Pod-template
checksum turns a config-only change into a declarative rollout
trigger**: any change to a ConfigMap's rendered content now changes the
consuming workload's checksum too, which changes the Pod template,
which is exactly what makes `helm upgrade` create a new ReplicaSet.
Each workload's checksum is scoped to its own ConfigMap template only
(via `$.Template.BasePath`) - changing `gateway.config.appMessage`
changes only the gateway checksum, never app's or state's, and
likewise for the other two.

**Fix (validation)**: `scripts/validate_helm_chart.py` now statically
rejects a missing, empty, malformed, or cross-referenced (a workload
accidentally hashing another component's ConfigMap - caught via
pairwise checksum distinctness) checksum, and rejects one placed on the
wrong metadata level. Render-level tests (`tests/test_validate_helm_chart.py`)
prove reproducibility and per-workload scoping directly against the
real chart via repeated `helm template` renders, never a live cluster.

**Fix (lifecycle validation)**: **live validation now proves actual Pod
replacement and per-Pod config convergence, never accepting
`kubectl rollout status` alone.** `scripts/helm_lifecycle_check.py`'s
upgrade proof now requires ALL of: the live ConfigMap actually contains
the new message, the Deployment's `observedGeneration` reached the new
`generation`, the Pod-template checksum actually changed, a DIFFERENT
ReplicaSet became the active one, every expected Pod is Ready, NONE of
the pre-upgrade Pod UIDs remain, EVERY Ready Pod's own `/config`
endpoint (queried directly inside that specific Pod over loopback,
never through the Service) reports the new message, and the
externally-routed Gateway API path (bounded polling) agrees too. The
guaranteed rollback path applies the identical, symmetric proof in
reverse - including that the checksum returns to the EXACT pre-upgrade
value, not merely "a different" one, and that none of the upgrade's own
Pod UIDs remain either. PVC/PV identity is verified unchanged at both
stages, exactly as before.

**The live failure itself rolled back successfully and did not alter
persistent storage** - `helm rollback` restored the release to its
baseline revision, the live `APP_MESSAGE` was confirmed back to its
original value, and the state PVC/PV identity was confirmed unchanged
throughout this remediation's own diagnosis (read-only `helm get
manifest`/`kubectl get replicaset`/`kubectl get pods` inspection only -
the release was never mutated to produce this diagnosis).

**The corrected live runs then passed:** `helm-lifecycle-check` 24/24
(Helm revisions 5-7, 2026-09-23) and `final-state-check` 43/43 - see
"DAY6: live validation record" below for the full revision history and
what each property proves.

## DAY6: live validation record

This is the Day 6 evidence record - the Day 6 counterpart of "DAY5:
released validation record" above. Day 6 is **release ready as a local
kind reference platform** (final adjudication: RELEASE READY,
2026-09-24; re-closed 2026-09-25 after the post-restart remediation
below), merged to `main` through PR #7, but **not yet tagged or
published**;
this record describes a validated local kind reference platform, not a
production-ready platform. The independent reviews, their adjudication,
and the review-remediation log are under
[`docs/engineering-reviews/day-06-*`](engineering-reviews/) (see
[`day-06-final-adjudication.md`](engineering-reviews/day-06-final-adjudication.md)
and [`day-06-remediation-log.md`](engineering-reviews/day-06-remediation-log.md)).

**How the live results were obtained.** Not as one uninterrupted `make
day6-check` invocation: the live targets were run stage by stage
against one preserved cluster between 2026-09-22 and 2026-09-23, and
each live-discovered defect (the sections above) was fixed and the
affected stage re-run - never by recreating the cluster, rotating
Secrets, or weakening a check. The figures below are the final result
of each stage as recorded by the operator during that run.

**Static results (before the 2026-09-24 review remediation):** 1161
unit tests passed; `version-check` 50/50; `manifest-check` 267/267
(frozen k8s/base); `helm-lint` and `helm-template` passed; `helm-check`
202/202; `ci-check` passed; the chart renders exactly 30 objects, with
zero Secret, Ingress, ClusterRole, or waypoint objects. (The review
remediation adds tests and one static validator - see "Review
remediation re-validation" below for the post-remediation figures.)

**Live results (2026-09-22 to 2026-09-23):**

| Check | Result |
|---|---|
| `networkpolicy-check` (isolated non-ambient probes) | 37/37 |
| `mesh-check` | 45/45 |
| `persistence-check` | 12/12 |
| `retention-check` | 22/22 |
| `helm-lifecycle-check` (corrected, checksum-driven) | 24/24 |
| `final-state-check` | 43/43 |

At the end of that run: nodes 3/3 Ready; Cilium 3/3; istio-cni-node
3/3; ztunnel 3/3; istiod available; gateway 3/3, app 3/3, state 1/1;
`Gateway/maops-edge` `Accepted=True`, `Programmed=True`;
`HTTPRoute/maops-gateway-route` `Accepted=True`, `ResolvedRefs=True`;
the state PVC and its bound PV kept the same UIDs throughout every
mutating experiment; the suite-level `/state` value was restored to its
captured run baseline; and no mesh-probe namespace or NetworkPolicy
probe Pod was left behind.

**Mesh identity-denial evidence tiers.** `mesh-check`'s 45/45 is the
complete tally (ztunnel/istiod/istio-cni health, ambient enrollment
with no sidecars, STRICT mTLS, live AuthorizationPolicy principals,
allowed-path positive controls, HTTP-layer leak checks, cleanup). Within
it, the **three gating wrong-identity denial assertions** (against
gateway, app, and state) were each supported by **AUTHORITATIVE**
correlated ztunnel policy-rejection evidence - the exact wrong SPIFFE
source identity, the correct destination, and one of the two exact
denial strings documented in "DAY6: live mesh-check remediation" above.
For those three assertions, CANDIDATE = 0 and BEST_EFFORT = 0.

**Helm release history** (`maops-kubernetes-platform-day6`, all
chart/app version 0.6.0):

| Revision | Date | What it is |
|---|---|---|
| 1 | 2026-09-22 | First install; recorded as failed (`maops-app` progress deadline exceeded) - consistent with the pre-`fsGroup` projected-Secret failure |
| 2 | 2026-09-22 | First healthy deployment - the original stable baseline |
| 3 | 2026-09-23 | Failed lifecycle probe: changed the ConfigMap but not the Pod template, so no rollout happened (the defect behind "DAY6: live-discovered Helm ConfigMap rollout remediation") |
| 4 | 2026-09-23 | Successful rollback to revision 2 |
| 5 | 2026-09-23 | Checksum-corrected deployment (`checksum/config` Pod-template annotations) |
| 6 | 2026-09-23 | Successful lifecycle probe upgrade (genuine ReplicaSet/Pod replacement) |
| 7 | 2026-09-23 | Successful rollback to revision 5 - the current deployed release |

The corrected `helm-lifecycle-check` (revisions 6-7) proved:
`observedGeneration` convergence; checksum divergence on upgrade and
exact restoration on rollback; ReplicaSet replacement; zero overlap
with the prior Pod UIDs; every Ready Pod directly observing the
expected configuration; the external Gateway API path observing it
too; unchanged PVC/PV identity; and rollback always executing once an
upgrade was submitted.

**Host/Docker restart recovery (local-environment limitation).** Three
host/Docker/WSL restarts have been observed on this local
Kind-on-Docker-on-WSL2 environment, with different outcomes:

1. During the staged live run, one restart left one older,
   ambient-enrolled `maops-app` Pod unable to reach `maops-state`
   through ztunnel: its local, redirected TCP connection was accepted,
   but traffic went no further. Replacing only that already-unready,
   stateless Pod restored connectivity - including when the
   replacement was scheduled on the same worker node.
2. A later restart (the WSL host rebooted at 09:05 on 2026-09-24)
   briefly produced Pods in `Unknown` state,
   `FailedCreatePodSandBox` events (`no ztunnel connection`), and
   Cilium API rate-limit responses (HTTP 429). It then recovered
   without any Pod replacement, through kubelet sandbox re-creation
   alone.
3. On 2026-09-25, after a WSL/Kind component restart, `maops-state-0`
   was Kubernetes Ready without its ambient in-Pod listeners and one
   gateway Pod was unready without them, while the mesh infrastructure
   reported healthy - see "DAY6:
   post-restart ambient listener incident (2026-09-25)" below.

None of these incidents proves an internal ztunnel defect - the
evidence supports only "restart recovery in this environment varies."
Before any validation after a host, Docker, or WSL restart, run the
bounded read-only gates first, in this order: `make cni-status`, `make
context-check`, `make mesh-status`, `make ambient-workload-check`, then
`make rollout-check`. Never assume the previous run's state survived. The same reboot also cleared `/tmp`, which is where
the suite-level state baseline file lives
(`DAY6_SUITE_BASELINE_PATH`, by default under `/tmp`) - a run's
baseline does not survive a host reboot, by design, and is never
recaptured to make a later check pass. A bracketed run that must
survive a reboot passes an explicit `DAY6_SUITE_BASELINE_PATH` in a
private directory outside the repository and `/tmp`, as the "Fresh
baseline-bracketed state run" below did.

**Review remediation re-validation (2026-09-24).** After the
independent review round: static validation passed again (1197 unit
tests, `helm-check` 215/215 with the new ambient-probe policy
validator); `make mesh-install` removed the istiod HPA; `cni-status`,
`context-check`, `mesh-status`, `rollout-check`, `gateway-check`,
`smoke`, and `mesh-check` (45/45; the same three AUTHORITATIVE denial
assertions, CANDIDATE 0, BEST_EFFORT 0) all passed. The post-reboot
`final-state-check` reached 42/43: the run's suite baseline file had
been cleared from `/tmp` by the 09:05 host reboot, so that one item
failed closed and was deliberately not recaptured. That 42/43 stands as
the honest result of that attempt.

**Fresh baseline-bracketed state run (2026-09-24, 11:47-11:51 +06).**
To re-establish suite-level restoration evidence without recreating
the lost file, an uninterrupted `state-check` -> `persistence-check` ->
`retention-check` -> `final-state-check` bracket was run with one fresh
run ID (`979a1e7e72e9418199b0486cf81a920e`) passed explicitly to every
step, and its baseline kept in a persistent, private location outside
the repository and outside `/tmp`
(`$HOME/.local/state/maops-k8s-day6/`, directory 0700, file 0600).
Results: `state-check` 24/24 (baseline captured and verified - mode,
run ID, context, namespace, PVC and PV UIDs - before any mutation);
`persistence-check` 12/12; `retention-check` 22/22; `final-state-check`
**43/43**, including the suite-level `/state` value matching the
pre-mutation baseline; PVC/PV UIDs unchanged; the read-only
`cni-status`/`context-check`/`mesh-status`/`rollout-check`/
`gateway-check`/`smoke` gates all passed afterwards with no leaked
probe resources. Scaling, rolling-update, PDB, Helm lifecycle, and
NetworkPolicy checks were not re-run; their results above stand. See
[`day-06-remediation-log.md`](engineering-reviews/day-06-remediation-log.md)
and [`day-06-final-adjudication.md`](engineering-reviews/day-06-final-adjudication.md)
for the exact commands, results, and final verdict.

## DAY6: post-restart ambient listener incident (2026-09-25)

**What was observed.** After a WSL/Kind component restart on
2026-09-25, the infrastructure gates passed - `context-check` 6/6,
`cni-status` 4/4, `mesh-status` 4/4 - but `rollout-check` failed 25/35,
with `maops-app` and `maops-gateway` initially 0/3 Ready.

- `maops-state-0` was Kubernetes Ready, yet its network namespace had
  **no** LISTEN sockets on 15001, 15006, or 15008 (ztunnel's in-Pod
  redirection and HBONE listeners). The source ztunnel logged
  `connection refused` to that Pod's IP on 15008, and no `/state`
  request reached the state HTTP server.
- Only `maops-state-0` was recreated (Pod UID
  `aa92aa2c-5852-4b2d-97d6-74addb78f1e2` -> `89abf805-9c1e-4f23-8005-483255ac98a0`).
  The PVC UID `6c5fdacc-090a-4208-9b52-9c594211a982` and PV UID
  `df840301-f5f1-4d8b-9d70-63597612e2fe` were preserved. The new Pod had
  all three listeners, and `maops-app` recovered to 3/3.
- One gateway Pod, `maops-gateway-7d59b678df-f88mj`, stayed unready and
  likewise lacked all three listeners; ztunnel explicitly rejected its
  plaintext calls to app under `istio-system/istio_converted_static_strict`
  (STRICT mTLS doing its job for an un-redirected source). Only that Pod
  was recreated; its replacement, `maops-gateway-7d59b678df-2ggrv`, had
  all three listeners, and the gateway recovered to 3/3.
- No Istio policy, infrastructure component, or other Pod was changed.

**Result on merged `main`.** The subsequent gate passed:
`context-check` 6/6, `cni-status` 4/4, `mesh-status` 4/4,
`rollout-check` 35/35, `gateway-check` 8/8, `smoke` 6/6, and
`final-state-check` 43/43 against the preserved suite baseline of run
`979a1e7e72e9418199b0486cf81a920e` (baseline file unchanged). The log
is kept outside the repository, in the private
`$HOME/.local/state/maops-k8s-day6/` directory.

**Observation vs inference.** The missing listeners in those two Pods'
network namespaces are confirmed. The exact mechanism by which they
were lost across the restart has **not** been proven; nothing here
claims a specific ztunnel or istio-cni defect.

**The gap it exposed, and the new check.** Neither Kubernetes
readiness nor the `ambient.istio.io/redirection` annotation revealed
the problem, and `mesh-status` checks infrastructure only. `make
ambient-workload-check` (`scripts/ambient_workload_check.py`) is a
read-only, per-Pod check of all seven deployed Pods - gateway (3),
app (3), state (1). For each it verifies Pod and namespace metadata -
the Pod is Running with a Pod IP and not terminating, runs as its own
ServiceAccount, and carries the expected ambient-enrollment metadata
(namespace label, no opt-out, redirection annotation, no sidecar) - and
that TCP 15001, 15006, and 15008 are LISTEN sockets in that Pod's own
network namespace, the condition that failed on 2026-09-25. It reads
`/proc/net/tcp` and `/proc/net/tcp6` through `kubectl exec` with the
workload image's own Python, so it needs no extra image or dependency.
It fails closed on a missing or extra Pod, a kubectl/API error, a
timeout, or malformed output, and it names the exact Pod and missing
ports. The Ready condition and the annotation are never sufficient on
their own. Its in-Pod probe emits only port numbers and a result marker; the
check's findings also show Pod names, Pod IPs, ServiceAccount names,
Pod phase, enrollment label/annotation values, and kubectl error text
on failure. It never reads environment variables, Secret volumes, or
token contents.

**Scope.** It checks sockets and metadata only. Present listeners are
necessary for ambient traffic but do not by themselves prove that
traffic is redirected into them (the in-Pod redirection rules), that
HBONE/mTLS connections succeed, or that AuthorizationPolicy allows and
denies the right identities. Those behaviors remain proven by the
existing live traffic tests - `make mesh-check` (STRICT mTLS, allowed
identity paths, correlated wrong-identity denial), `make
networkpolicy-check`, `make gateway-check`, and `make smoke` - which
this check complements and never replaces.

It runs in `make day6-check` directly after `deploy` and before the
lengthy `rollout-check`, and it is the fourth read-only gate after any
restart. `mesh-status` remains the infrastructure-only check before
application deployment, and `make ci-check` stays cluster-free.

**Release status.** The 2026-09-24 RELEASE READY adjudication stands as
history. The gate re-opened on 2026-09-25 and closed again the same
day: PR #7 merged with CI passing, and the read-only gate on merged
`main` passed - `context-check` 6/6, `cni-status` 4/4, `mesh-status`
4/4, `ambient-workload-check` 67/67, `rollout-check` 35/35,
`gateway-check` 8/8, `smoke` 6/6, and `final-state-check` 43/43 against
run `979a1e7e72e9418199b0486cf81a920e`'s unchanged baseline (log kept
outside the repository, in `$HOME/.local/state/maops-k8s-day6/`).
Current status: **RELEASE READY** as a local kind reference platform;
`v0.6.0` is not yet tagged or published. The cause of the lost
listeners remains unproven.

## What Day 6 proves, and what it explicitly does not claim

**Proven statically** (`make ci-check` and its constituent targets):
the chart renders exactly the intended 30-object inventory with no
Ingress/waypoint/Secret/ClusterRole objects; every version/image-tag/
chart-version/appVersion is `0.6.0`; workload `securityContext`
requirements (including `fsGroup` 10001 with 0440 Secret files) are
intact; ServiceAccounts/RBAC remain least-privilege and
namespace-scoped; the NetworkPolicy topology is exact (including the
Day 5 validation-client shortcut's absence and the peerless HBONE
rule); PeerAuthentication is STRICT; every AuthorizationPolicy
principal/target is exact (against `maops-edge-istio`, Istio's real
deterministic identity); Gateway/HTTPRoute references are exact; the
Istio Gateway `parametersRef` ConfigMap and the ambient health-probe
CiliumClusterwideNetworkPolicy both have their exact shapes pinned;
per-workload `checksum/config` annotations are deterministic and
isolated; the values schema rejects controlled invalid fixtures; and
k8s/base remains untouched and frozen at Day 5.

**Proven live on the local kind cluster** (see "DAY6: live validation
record" above): Istio's Gateway API controller generates the expected
Deployment/Service/ServiceAccount for `maops-edge`, with GatewayClass/
Gateway/HTTPRoute conditions Accepted/Programmed/Resolved; the rendered
`parametersRef` behavior (NodePort 30080 reachable at
`127.0.0.1:18080`, target port 80, `maops-edge-istio` with token
automount disabled); Cilium 1.20.1 and Istio 1.31.0 ambient coexist
(ztunnel healthy on every node, HBONE on 15008, STRICT mTLS in effect,
no sidecars); allowed identity paths work and an authenticated but
unauthorized identity is denied at the AuthorizationPolicy layer,
proven by AUTHORITATIVE correlated ztunnel evidence rather than a raw
TCP connect; NetworkPolicy allow/deny behavior on plaintext,
non-ambient paths, isolated from ambient interception; the transactional
ztunnel `RUST_LOG` handling and probe cleanup leave nothing behind;
persistence and PVC retention against the Helm-deployed `maops-state`;
a real Helm upgrade/rollback with genuine Pod replacement and preserved
storage identity; and full restoration to the normal baseline
afterward (`final-state-check`). The other inherited Day 3-5 live
checks were re-run in the same staged run, but only the counts tabled
above are recorded here.

**Explicitly NOT claimed:** production readiness at any level - this
is a validated local kind reference platform (no HA: single istiod
replica with autoscaling deliberately disabled, single Cilium operator;
no TLS/cert-manager, cloud LoadBalancer, or observability stack); any
Istio 1.31 ztunnel denial shape other than the two observed
AUTHORITATIVE strings (other shapes stay CANDIDATE/BEST_EFFORT, never
promoted without live evidence); uniform recovery after host/Docker
restarts (see above); L7/HTTP-aware east-west authorization (no
waypoint), or HTTP-method-level authorization from the NetworkPolicy/
AuthorizationPolicy layer; and live-cluster validation in GitHub
Actions (CI is cluster-free by design).

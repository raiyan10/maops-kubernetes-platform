# Architecture - Day 4 (v0.4.0, in development)

Day 1 (`v0.1.0`) established a single-workload Kubernetes foundation,
Day 2 (`v0.2.0`) added a second workload, real service discovery, and a
runtime Secret, and Day 3 (`v0.3.0`) added a real multi-node cluster,
topology-aware scheduling, scaling, rolling-update/rollback behavior,
and a PodDisruptionBudget per workload - all three released and frozen;
see the historical evidence under `docs/engineering-reviews/day-0[1-3]-*`.
Day 4 keeps that entire gateway/app architecture unchanged and adds a
third workload, `maops-state` - a single-replica StatefulSet with a
PVC-backed `/data` volume, proving real Kubernetes persistence: data
survives Pod deletion/rescheduling, and the backing claim survives a
scale-to-zero/back-to-one cycle.

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

## Why RBAC/NetworkPolicy remain deferred

Unchanged rationale from Day 2: both workloads still run with no
ServiceAccount beyond the default (`automountServiceAccountToken:
false`) and no NetworkPolicy. Day 5 (`v0.5.0`) introduces both together.

**DAY4-SEC-L1 scope note:** a standard Kubernetes `NetworkPolicy`
restricts *which pods/namespaces can reach a Service's port at all* (L3/L4
scope) - it is not HTTP path- or method-level authorization, and cannot
by itself distinguish "may `GET /state`" from "may `PUT /state`" for a
caller it otherwise permits to reach the gateway at all. Day 5's
NetworkPolicy work can narrow *who* may reach the public gateway write
path, but closing the public-gateway-write-access concern at the
HTTP-method level (if ever required) would need application-layer
authorization, which NetworkPolicy does not provide.

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

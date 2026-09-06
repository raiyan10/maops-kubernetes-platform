---
name: kind-cluster-validation
description: Create/use the project's kind cluster and prove real Kubernetes behavior - node readiness, server version, Deployment availability, replica/EndpointSlice counts, ConfigMap consumption, Secret mount/auth behavior, worker-only scheduling and topology spread, scaling, rolling update/rollback, PodDisruptionBudget/Eviction behavior, bounded port-forward HTTP checks, dependency-failure behavior, and controller-reconciliation proof via pod deletion. Use whenever asked to validate against a live cluster, debug a failing rollout, or prove the workload actually runs.
---

# kind cluster validation

Real-cluster validation for the maops-kubernetes-platform project's kind
cluster - as of Day 3, `maops-k8s-day3` (context `kind-maops-k8s-day3`),
1 control-plane + 2 worker nodes, a separate cluster from Day 1's
`maops-k8s-day1` and Day 2's `maops-k8s-day2` which this validation never
touches. This is the live-cluster counterpart to `manifest-validation`
(static) and `workload-security-validation` (security-specific) - it
proves runtime behavior via `kubectl -o json` and real HTTP calls, never
by re-reading the manifest.

Every script in this list calls `kube.verify_context()` first and fails
closed (never proceeds) if the live cluster's node identity doesn't
actually match `maops-k8s-day3` - this is checked at runtime, not assumed
from the hardcoded constant.

## Sequence (mirrors the Makefile)

```bash
make cluster-create         # idempotent: kind create cluster --config kind/cluster.yaml (1 control-plane + 2 workers)
make context-check          # fail closed unless verified against the isolated Day 3 cluster at the pinned node version
make namespace-apply        # apply ONLY the Namespace to the explicit context (must precede secret-bootstrap)
make secret-bootstrap       # create/preserve the runtime Secret - never printed
make image-build              # docker build both gateway/ and app/ images
make image-load                # kind load docker-image for both, into every node of maops-k8s-day3
make deploy                     # kubectl apply -k k8s/base
make rollout-check             # scripts/cluster_check.py - checks 1-10, 12-20 below
make scheduling-check         # scripts/scheduling_check.py - worker-only scheduling + topology spread proof
make discovery-check          # scripts/discovery_check.py - real DNS + Service HTTP proof
make secret-check                # scripts/secret_check.py - Secret wiring/auth/non-disclosure
make smoke                         # scripts/smoke.py - normal HTTP smoke via service/maops-gateway
make dependency-check          # scripts/dependency_check.py - liveness vs. dependency-aware readiness
make scaling-check              # scripts/scaling_check.py - real scaling 3 -> 4 -> 3, guaranteed restoration
make rolling-update-check     # scripts/rollout_check.py - real rolling update + real kubectl rollout undo
make pdb-check                     # scripts/pdb_check.py - real PDB/Eviction-API behavior, guaranteed restoration
make final-state-check         # scripts/final_state_check.py - proves everything is restored after all of the above
```

`make day3-check` runs the full sequence (plus `tool-check`, `test`,
`version-check`, and `manifest-check`) in the required order and is the
authoritative one-shot validation.

## What each real check proves

`scripts/cluster_check.py` (`make rollout-check`), for **both**
`maops-gateway` and `maops-app`:

1. Cluster has exactly 3 nodes (1 control-plane + 2 workers), all Ready.
2. `kubectl version -o json` server `gitVersion` == `v1.36.1` exactly.
3. Namespace `maops-platform` exists.
4-5. Each Deployment reaches `status.conditions[Available]=True`
   (bounded wait, not an instant check).
6-9. `spec.replicas == 3` and `status.readyReplicas == 3` for each.
10-11. All three pods per workload individually report `Ready=True`.
12. Each Service is `ClusterIP`.
13-14. Each Service's `discovery.k8s.io/v1` EndpointSlice has exactly 3
    ready endpoints (`scripts/endpointslice.py` - **not** the legacy
    `v1 Endpoints` API, which Kubernetes 1.36 deprecates).
15-16. Each ConfigMap's value matches what `kubectl exec` (via the
    Python interpreter directly - the distroless image has no shell)
    reads from the live process's environment.
17-18. Live pod's actual UID/GID and the API server's recorded
    `securityContext` fields, for both workloads.
19. Neither pod mounts a Kubernetes API ServiceAccount token.
20. Both pods have the expected Secret volume/mount
    (`internal-auth` -> `/var/run/secrets/maops`, read-only).

`scripts/scheduling_check.py` (`make scheduling-check`): identifies the
control-plane and worker nodes dynamically via the
`node-role.kubernetes.io/control-plane` label (never a hardcoded node
name), then proves for both workloads: 3/3 Pods Ready, zero Pods on the
control-plane, both workers host at least one Pod, and worker
replica-count skew <= 1 (a 2/1 or 1/2 split, never 3/0).

`scripts/discovery_check.py` (`make discovery-check`): from a live
gateway Pod, a real `socket.getaddrinfo('maops-app', ...)` call (via
`kubectl exec` + the pinned interpreter - no shell/dig/nslookup
available) proves DNS resolution succeeds (never asserting a specific
IP), then a real port-forwarded HTTP call to `/backend` proves the
resolved address actually routes through the Service to a live app Pod.

`scripts/secret_check.py` (`make secret-check`): Secret exists with a
non-empty `internal-token` key; both pods mount it read-only and can
read it; a gateway-authenticated call to the app succeeds; a direct,
unauthenticated (and wrong-token) call to `maops-app`'s
`/internal/info` is rejected with exactly `HTTP 403`; neither workload's
normal responses, `/config`, nor logs ever contain the token; no tracked
repository file contains the live generated token.

`scripts/smoke.py` (`make smoke`): opens a bounded, auto-cleaned-up
port-forward to `service/maops-gateway` and performs real HTTP GETs
against `/`, `/livez`, `/readyz`, `/config`, `/backend`, asserting real
response semantics (not just HTTP 200 + valid JSON).

`scripts/dependency_check.py` (`make dependency-check`): scales
`maops-app` to 0 replicas (leaving `maops-gateway` untouched), proves
gateway `/livez` stays `200`, `/readyz` becomes `503`, `/backend`
becomes a controlled `503`, and gateway restart counts don't increase -
then restores `maops-app` to 3 replicas in a guaranteed `finally` path.

`scripts/scaling_check.py` (`make scaling-check`): for both workloads
independently, baseline 3/3 -> `kubectl scale --replicas=4` -> proves
Deployment/Pods/EndpointSlice all agree on 4 and the Service stays
functional -> restores to 3 in a guaranteed `finally` path,
independently re-verified. `HorizontalPodAutoscaler` is out of scope.

`scripts/rollout_check.py` (`make rolling-update-check`): for both
workloads independently, patches a temporary, uniquely-marked
`spec.template.metadata.annotations` key (never a fake image tag) to
trigger a real new ReplicaSet/rolling update, proves Pods are actually
replaced (UID-set comparison) and the rollout completes, samples the
Service during the rollout window with bounded polling, then performs a
**real** `kubectl rollout undo` and proves full restoration - Deployment
Available, 3/3 Ready, annotation gone, a genuinely new replacement Pod
set, EndpointSlice back to 3. Uses `_wait_exact_pod_count()` to poll past
the termination race between "rollout reports done" and "old Pods
actually deleted" before trusting any Pod-set snapshot.

`scripts/pdb_check.py` (`make pdb-check`): for both workloads
independently, proves the normal healthy PDB status
(`desiredHealthy=2`, `disruptionsAllowed=1`), scales 3 -> 2 (proving the
PDB does **not** block ordinary scaling) and confirms
`disruptionsAllowed=0`, then submits a real `policy/v1 Eviction` via
`kubectl create --raw .../eviction -f -` (never `kubectl delete pod`)
against a Pod filtered to be genuinely `Ready`/non-terminating (a
terminating straggler Pod is always evictable regardless of the PDB, so
selecting one would produce a false "eviction succeeded" result unrelated
to the PDB). The rejection is classified from the actual API response
text (`TooManyRequests`), never a bare non-zero exit code, and the
target Pod's continued presence is independently confirmed before
restoring to 3 replicas in a guaranteed `finally` path.

`scripts/final_state_check.py` (`make final-state-check`): after all of
the above, independently re-proves the cluster is back at its normal
3/3/healthy baseline for both workloads (including worker-only
scheduling/skew, reusing `scheduling_check`'s own functions), both PDBs
at their normal healthy status, no leftover rollout-test annotation, the
Secret still valid, no leaked `kubectl port-forward` process, and that
Day 1/Day 2's clusters still exist untouched.

`scripts/reconcile_check.py` (`make controller-check` - bonus, not part
of `day3-check`): records all three `maops-app` pod UIDs, deletes
exactly one pod, waits for the Deployment to reconcile back to 3/3
Ready, confirms a genuinely new UID appeared while the other two
survived, then re-runs HTTP checks via a scoped test-only port-forward
to `service/maops-app`.

## Debugging a failure

Don't guess - pull real evidence:

```bash
kubectl --context kind-maops-k8s-day3 -n maops-platform describe deployment/maops-gateway
kubectl --context kind-maops-k8s-day3 -n maops-platform describe deployment/maops-app
kubectl --context kind-maops-k8s-day3 -n maops-platform describe pod <name>
kubectl --context kind-maops-k8s-day3 -n maops-platform get events --sort-by=.lastTimestamp
kubectl --context kind-maops-k8s-day3 -n maops-platform logs <pod>
kubectl --context kind-maops-k8s-day3 -n maops-platform get endpointslices -l kubernetes.io/service-name=maops-app
kubectl --context kind-maops-k8s-day3 -n maops-platform get poddisruptionbudget
kubectl --context kind-maops-k8s-day3 get nodes -o wide
```

Fix the root cause (manifest, image, probe timing, resource sizing,
Secret bootstrap ordering, a genuine termination race) and re-run only
the affected `make` target - don't re-run the whole sequence
unnecessarily, and don't loosen a script's assertions to force a pass.

## Cleanup discipline

- `make cluster-delete` deletes **only** the current day's named cluster
  (`maops-k8s-day3` as of Day 3) - never run `docker system prune` or
  delete unrelated clusters/resources, including an earlier day's
  cluster if it's still running.
- After any manual `kubectl port-forward` debugging session, confirm
  nothing was left running: `ps aux | grep port-forward`.
- Leave the cluster running after validation unless there's a concrete
  safety reason to tear it down - it's expected to be available for
  independent review.

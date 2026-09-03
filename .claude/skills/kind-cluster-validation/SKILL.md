---
name: kind-cluster-validation
description: Create/use the project's kind cluster and prove real Kubernetes behavior - node readiness, server version, Deployment availability, replica/EndpointSlice counts, ConfigMap consumption, Secret mount/auth behavior, bounded port-forward HTTP checks, dependency-failure behavior, and controller-reconciliation proof via pod deletion. Use whenever asked to validate against a live cluster, debug a failing rollout, or prove the workload actually runs.
---

# kind cluster validation

Real-cluster validation for the maops-kubernetes-platform project's kind
cluster - as of Day 2, `maops-k8s-day2` (context `kind-maops-k8s-day2`),
a separate cluster from Day 1's `maops-k8s-day1` which this validation
never touches. This is the live-cluster counterpart to
`manifest-validation` (static) and `workload-security-validation`
(security-specific) - it proves runtime behavior via `kubectl -o json`
and real HTTP calls, never by re-reading the manifest.

## Sequence (mirrors the Makefile)

```bash
make cluster-create    # idempotent: kind create cluster --config kind/cluster.yaml
make namespace-apply   # apply ONLY the Namespace to the explicit context (must precede secret-bootstrap)
make secret-bootstrap  # create/preserve the runtime Secret - never printed
make image-build         # docker build both gateway/ and app/ images
make image-load          # kind load docker-image for both, into maops-k8s-day2
make deploy               # kubectl apply -k k8s/base
make rollout-check       # scripts/cluster_check.py - checks 1-10, 12-20 below
make discovery-check     # scripts/discovery_check.py - real DNS + Service HTTP proof
make secret-check          # scripts/secret_check.py - Secret wiring/auth/non-disclosure
make smoke                 # scripts/smoke.py - normal HTTP smoke via service/maops-gateway
make dependency-check     # scripts/dependency_check.py - liveness vs. dependency-aware readiness
```

`make day2-check` runs the full sequence (plus `tool-check`, `test`,
`version-check`, and `manifest-check`) in the required order and is the
authoritative one-shot validation.

## What each real check proves

`scripts/cluster_check.py` (`make rollout-check`), for **both**
`maops-gateway` and `maops-app`:

1. Cluster has exactly the expected node(s), all Ready.
2. `kubectl version -o json` server `gitVersion` == `v1.36.1` exactly.
3. Namespace `maops-platform` exists.
4-5. Each Deployment reaches `status.conditions[Available]=True`
   (bounded wait, not an instant check).
6-9. `spec.replicas == 2` and `status.readyReplicas == 2` for each.
10-11. Both pods per workload individually report `Ready=True`.
12. Each Service is `ClusterIP`.
13-14. Each Service's `discovery.k8s.io/v1` EndpointSlice has exactly 2
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
repository file contains the live generated token. The script holds the
decoded token in memory only to build correct headers and comparisons -
it never prints it.

`scripts/smoke.py` (`make smoke`): opens a bounded, auto-cleaned-up
port-forward to `service/maops-gateway` (**not** `maops-app` - that's
the normal Day 2 entry point) and performs real HTTP GETs against `/`,
`/livez`, `/readyz`, `/config`, `/backend`, asserting real response
semantics (not just HTTP 200 + valid JSON) - including that `/backend`
carries real data proxied from `maops-app`.

`scripts/dependency_check.py` (`make dependency-check`): scales
`maops-app` to 0 replicas (leaving `maops-gateway` untouched), proves
gateway `/livez` stays `200`, `/readyz` becomes `503`, `/backend`
becomes a controlled `503`, and gateway restart counts don't increase -
then, in a guaranteed path, restores `maops-app` to 2 replicas and
proves both Deployments recover to `2/2` Ready. A restoration failure is
reported prominently and separately from the original experiment result
- never hidden behind it, and `maops-app` must never be left at 0
replicas.

`scripts/reconcile_check.py` (`make controller-check` - bonus, not part
of `day2-check`): records both `maops-app` pod UIDs, deletes exactly one
pod (never creates a replacement manually), waits for the Deployment to
reconcile back to 2/2 Ready, confirms one genuinely new UID appeared
while the untouched pod's UID survived, then re-runs HTTP checks via a
scoped test-only port-forward to `service/maops-app`.

## Debugging a failure

Don't guess - pull real evidence:

```bash
kubectl --context kind-maops-k8s-day2 -n maops-platform describe deployment/maops-gateway
kubectl --context kind-maops-k8s-day2 -n maops-platform describe deployment/maops-app
kubectl --context kind-maops-k8s-day2 -n maops-platform describe pod <name>
kubectl --context kind-maops-k8s-day2 -n maops-platform get events --sort-by=.lastTimestamp
kubectl --context kind-maops-k8s-day2 -n maops-platform logs <pod>
kubectl --context kind-maops-k8s-day2 -n maops-platform get endpointslices -l kubernetes.io/service-name=maops-app
```

Fix the root cause (manifest, image, probe timing, resource sizing,
Secret bootstrap ordering) and re-run only the affected `make` target -
don't re-run the whole sequence unnecessarily, and don't loosen a
script's assertions to force a pass.

## Cleanup discipline

- `make cluster-delete` deletes **only** the current day's named cluster
  (`maops-k8s-day2` as of Day 2) - never run `docker system prune` or
  delete unrelated clusters/resources, including an earlier day's
  cluster if it's still running.
- After any manual `kubectl port-forward` debugging session, confirm
  nothing was left running: `ps aux | grep port-forward`.
- Leave the cluster running after validation unless there's a concrete
  safety reason to tear it down - it's expected to be available for
  independent review.

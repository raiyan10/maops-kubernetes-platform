# Architecture - Day 2 (v0.2.0, in development)

Day 1 (`v0.1.0`) established a single-workload Kubernetes foundation -
see the historical evidence under `docs/engineering-reviews/day-01-*`
for that stage's own review. Day 2 builds directly on it: a second
workload is introduced, real service discovery is proven, and a runtime
Secret is wired in for the first time.

## Control flow

```
Docker
   |
   v
kind (single control-plane node, pinned kindest/node:v1.36.1)
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
Deployment: maops-gateway (2 replicas)      Deployment: maops-app (2 replicas)
   |                                        |
   +--> ReplicaSet --> Pod, Pod             +--> ReplicaSet --> Pod, Pod
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
`kubectl port-forward`) in Day 2's normal architecture; `maops-app` is
reached exclusively through the gateway, over the `maops-app` Service.
No NodePort, LoadBalancer, or Ingress exists - see
[Why port-forward instead of NodePort/Ingress](#why-port-forward-instead-of-nodeportingress)
below (carried forward unchanged from Day 1's rationale).

## Two Deployments, two ownership chains

`maops-gateway` and `maops-app` are independent Deployments, each owning
its own ReplicaSet, each owning its own two Pods - the same
Deployment -> ReplicaSet -> Pod ownership chain Day 1 established, just
twice. They share a label scheme
(`app.kubernetes.io/name=maops-kubernetes-platform`,
`app.kubernetes.io/instance=maops-kubernetes-platform-day2`) but are
disambiguated by `app.kubernetes.io/component` (`gateway` or `app`),
which is also what each Service's `spec.selector` keys off - this is
what makes selector isolation correct: the gateway Service's selector
can never be satisfied by an app Pod's labels, or vice versa (Day 2's
static validation asserts this directly, not just that each selector
happens to match its own workload).

## Service discovery: gateway -> app via Kubernetes DNS

The gateway never talks to a Pod IP, a Pod name, a ReplicaSet name, a
node IP, or a hardcoded ClusterIP. Its `BACKEND_HOST` ConfigMap value is
literally the app Service's name, `maops-app` - Kubernetes' cluster DNS
resolves that to the Service's stable ClusterIP (backed by
`maops-app.maops-platform.svc.cluster.local` under the hood), which
kube-proxy then load-balances across whichever app Pods are currently
Ready. This is the actual mechanism Day 2 proves live
(`scripts/discovery_check.py`): a real `socket.getaddrinfo('maops-app', ...)`
call from inside a running gateway Pod (the distroless image has no
shell/dig/nslookup, so the pinned Python interpreter performs the
lookup directly), asserting only that resolution succeeds - never that
it resolves to a specific IP, since that IP is Kubernetes-internal and
not this project's concern to pin. A second, independent proof (real
HTTP through the Service to `/backend`) confirms the resolved address
actually routes to a live app Pod.

## Service stable networking (EndpointSlice, not legacy Endpoints)

Each Service still provides the stable ClusterIP/DNS identity Day 1
relied on. What changes in Day 2 is which API backs the
"are there really 2 healthy backends" proof: Kubernetes 1.36 emits a
deprecation warning for the legacy `v1 Endpoints` API, so Day 2's
authoritative real-cluster evidence
(`scripts/cluster_check.py`, via `scripts/endpointslice.py`) queries
`discovery.k8s.io/v1 EndpointSlice` objects instead
(`kubectl get endpointslices -l kubernetes.io/service-name=<service>`)
and counts addresses whose `conditions.ready` is explicitly `true`.
Kubernetes may still create the legacy Endpoints object underneath -
that's fine and unavoidable - it's just no longer what this project's
validation trusts.

## ConfigMap flow

Two workload-specific ConfigMaps replace Day 1's single one:

- **`maops-gateway-config`** - `BACKEND_HOST`, `BACKEND_PORT`,
  `BACKEND_TIMEOUT_SECONDS` (the bounded, finite timeout every gateway
  -> app HTTP call uses - no infinite waits anywhere in this path), plus
  the same `APP_*` display/environment keys Day 1 had.
- **`maops-app-config`** - `APP_NAME`, `APP_ENVIRONMENT`, `APP_MESSAGE`,
  `APP_LOG_LEVEL`, same shape as Day 1's ConfigMap.

Neither ConfigMap ever holds the internal auth token or anything
secret-like - that's what the Secret is for.

## Secret lifecycle

The runtime Secret `maops-internal-auth` (key `internal-token`) is
deliberately **not** part of `k8s/base` - Kustomize never renders a
Secret object, and no usable token is ever committed to this
repository. Instead:

1. `scripts/secret_bootstrap.py` runs against the explicit
   `kind-maops-k8s-day2` context and `maops-platform` namespace, after
   the namespace exists but before the Deployments are applied
   (`make namespace-apply` then `make secret-bootstrap` then
   `make deploy` - see the Makefile lifecycle below).
2. If the Secret doesn't exist, it generates a cryptographically-strong
   random token (`secrets.token_urlsafe`), writes it to a private
   temporary file (mode `0600`, guaranteed removed in a `finally`
   block), and creates the Secret via
   `kubectl create secret generic --from-file` - the token is never a
   shell command-line argument and never printed.
3. If the Secret already exists, it's preserved and only validated
   (key present, non-empty) - never silently rotated. A pod that already
   has last week's token mounted would otherwise start failing
   authentication the moment a rotated Secret's volume remounts it.

## Secret volume flow

Both `maops-gateway` and `maops-app` mount the same Secret, read-only,
identically:

- Volume name: `internal-auth`
- Mount path: `/var/run/secrets/maops`
- Token file: `/var/run/secrets/maops/internal-token`
- `readOnly: true` on the volumeMount
- `defaultMode: 288` (octal `0440` - owner/group read-only) combined
  with pod-level `securityContext.fsGroup: 10001`, so UID/GID
  `10001:10001` (the same non-root identity both containers already run
  as) can read the file without the volume ever being world-readable.

Both applications read the token from this file at process start, never
from an environment variable - an env var would show up in
`kubectl describe pod` and process-inspection tooling far more casually
than a file the process has to explicitly open.

## App-level internal auth

`maops-app`'s `GET /internal/info` is a protected endpoint. The caller
must send the token as the `X-MAOPS-Internal-Token` header; the app
compares it against its own mounted copy using `hmac.compare_digest()`
(not `==`, to avoid a timing side-channel), and:

- Missing or wrong token -> `HTTP 403`, generic `{"error": "forbidden"}`
  body.
- Correct token -> `HTTP 200`, a small safe payload (service name,
  hostname, uptime, environment) - never the token itself.

The token is never logged (the app's request-logging path only ever
formats the request line/status, never header values) and never appears
in `GET /config`'s output (that endpoint only ever surfaces
`APP_*`-prefixed environment variables, and the token is deliberately
not one of those).

## Dependency-aware gateway readiness vs. local-only gateway liveness

This is Day 2's central probe-design point, and it's intentionally
asymmetric:

- **`GET /livez`** - answers "is the gateway process itself alive?" and
  nothing else. It never calls the app backend. This is what both the
  `startupProbe` and `livenessProbe` use, so a backend outage can never,
  by itself, cause kubelet to restart a gateway container.
- **`GET /readyz`** - answers "should this gateway Pod currently receive
  traffic?", which for a proxying gateway genuinely does depend on
  whether its backend is reachable. The handler performs a single
  bounded HTTP call to `http://maops-app:8080/readyz` (bounded by
  `BACKEND_TIMEOUT_SECONDS`) and returns `200` only if that call
  succeeds and reports `ready`; otherwise `503`. This is what the
  `readinessProbe` uses - so an app outage removes the gateway Pod from
  its own Service's endpoints (correctly - it can't usefully serve
  traffic either, since it just proxies to the unavailable backend)
  without ever touching liveness.

`scripts/dependency_check.py` proves this distinction live: it scales
`maops-app` to 0 replicas (leaving `maops-gateway`'s replica count
untouched), confirms `/livez` still returns `200`, `/readyz` returns
`503`, `/backend` returns a controlled `503` (no traceback, no token),
and - critically - that gateway container restart counts do **not**
increase, before restoring `maops-app` to 2 replicas in a guaranteed
`finally` path and waiting for both Deployments to recover to `2/2`
Ready. The app's own readiness (`GET /readyz` on `maops-app`) never
depends on the gateway - that circular dependency is never introduced.

## Timeout hierarchy

Every gateway -> app HTTP call is bounded by `BACKEND_TIMEOUT_SECONDS`
(sourced from `maops-gateway-config`, default `3` seconds) - there is no
unbounded/default network wait anywhere on this path. The gateway's own
`readinessProbe.timeoutSeconds` (`5`) is set comfortably above that, so
the kubelet probe itself never times out before the gateway's internal
bounded check has a chance to complete and return a real `200`/`503`.

## Resources and security baseline

Unchanged from Day 1, applied identically to both workloads: requests
`cpu: 50m` / `memory: 32Mi`, limits `cpu: 250m` / `memory: 128Mi`;
`runAsNonRoot: true`, `runAsUser`/`runAsGroup: 10001`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`,
`readOnlyRootFilesystem: true`, `seccompProfile.type: RuntimeDefault`,
`automountServiceAccountToken: false`. The Secret volume is the one new
mount, and it doesn't weaken `readOnlyRootFilesystem` - it's a
projected read-only Secret volume, not a writable filesystem path.

## No persistence yet

Neither workload writes to disk. `readOnlyRootFilesystem: true` imposes
no functional constraint on either container, matching Day 1. Real
persistence (PVC/StatefulSet) is Day 4 scope, not Day 2's.

## Why NetworkPolicy/RBAC remain deferred

Both workloads still run with no ServiceAccount beyond the default
(and `automountServiceAccountToken: false`, so not even that default
token is mounted) and no NetworkPolicy restricting pod-to-pod traffic
within `maops-platform`. That's a deliberate, accepted gap for Day 2:
introducing RBAC before any workload actually needs to call the
Kubernetes API would be scope creep with nothing to scope, and
NetworkPolicy without RBAC/ServiceAccount context first would be
solving network isolation in isolation from the access-control model it
needs to compose with. Day 5 (`v0.5.0`) introduces both together,
including moving `automountServiceAccountToken` from `false` to a
deliberately scoped `true` only for whichever workload ends up needing
it.

## Why port-forward instead of NodePort/Ingress

Day 2's architecture goal is proving multi-service composition,
service discovery, configuration, and Secrets - not also standing up
cluster-external networking concerns that belong to later stages.
`kubectl port-forward` to `service/maops-gateway` gives real HTTP access
for both human use and automated validation with zero additional
cluster surface area: no NodePort opened on the node, no cloud
LoadBalancer to provision, no Ingress controller to install and
configure. It's inherently bounded to the local machine and the
lifetime of the `kubectl` process, matching the requirement that nothing
external is exposed and nothing is left running afterward beyond what's
needed for review. (The one deliberate exception: security validation
scripts temporarily port-forward directly to `service/maops-app` or even
a specific gateway Pod - never exposed externally, only ever a local,
bounded, auto-cleaned-up test path - to prove the app's own auth
boundary and the gateway's dependency-failure behavior independently of
the normal gateway-fronted path.)

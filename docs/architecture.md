# Architecture - Day 1 (v0.1.0)

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
   +--> ConfigMap: maops-app-config
   |
   v
Deployment: maops-app (2 replicas)
   |
   +--> ReplicaSet (owned by the Deployment)
          |
          +--> Pod (maops-app, replica 1)
          +--> Pod (maops-app, replica 2)
   |
   v
Service: maops-app (ClusterIP)
   |
   v
kubectl port-forward (bounded, local-only)
   |
   v
localhost:<local-port> -> Service:8080 -> Pod:8080
```

No NodePort, LoadBalancer, or Ingress exists on Day 1 - see
[Why port-forward instead of NodePort/Ingress](#why-port-forward-instead-of-nodeportingress-on-day-1)
below.

## Deployment -> ReplicaSet -> Pod ownership

The `maops-app` Deployment is the only object a human or `kubectl apply`
directly manages. It creates and owns a ReplicaSet (named
`maops-app-<pod-template-hash>`), which in turn creates and owns the
individual Pods. This ownership chain is what makes the controller-
reconciliation proof in Day 1's validation meaningful: deleting a Pod
directly does not touch the Deployment or ReplicaSet objects - the
ReplicaSet controller notices the actual replica count has dropped below
`spec.replicas` and creates a new Pod to restore it, without any human
or script re-creating anything by hand. That's real controller behavior,
not a manifest re-apply.

## Service stable networking

`maops-app` (the Service) provides a stable ClusterIP and DNS name
(`maops-app.maops-platform.svc.cluster.local`) that doesn't change as
individual Pods are replaced. Its `spec.selector` matches the Deployment
pod template's `app.kubernetes.io/name` and `app.kubernetes.io/instance`
labels - kube-proxy (via iptables/IPVS rules on the node) and the
EndpointSlice controller keep the Service's endpoint list in sync with
whichever Pods currently match that selector and report Ready, so
traffic never routes to a Pod that hasn't passed its readiness probe.

## ConfigMap flow

`maops-app-config` holds four plain (non-secret) key-value pairs
(`APP_NAME`, `APP_ENVIRONMENT`, `APP_MESSAGE`, `APP_LOG_LEVEL`). The
Deployment's container consumes the entire ConfigMap via `envFrom`, so
each key becomes an environment variable inside the container. The
application (`app/server.py`) reads these at request time and exposes
exactly the `APP_*`-prefixed ones - and nothing else - through
`GET /config`, which is how Day 1's validation proves the ConfigMap value
is actually flowing into the running workload rather than being asserted
only against the manifest.

## Probe roles

Three separate probes exist because they answer three different
questions:

- **startupProbe** (`GET /livez`) - "has the process finished starting
  and begun serving at all?" Runs first, with a longer effective budget
  (`periodSeconds: 2`, `failureThreshold: 15` - up to ~30s), so a slow
  first boot doesn't get mistaken for a crash. While the startupProbe is
  still failing, the liveness and readiness probes are not evaluated.
- **livenessProbe** (`GET /livez`) - "is the process still alive and
  responsive?" Once startup succeeds, this runs on an ongoing basis
  (`periodSeconds: 10`); repeated failure causes kubelet to restart the
  container. It intentionally does not check downstream dependencies -
  only "is this process able to answer HTTP at all."
- **readinessProbe** (`GET /readyz`) - "should this Pod currently receive
  traffic?" The application holds `/readyz` at HTTP 503 for a short
  startup delay (`STARTUP_DELAY_SECONDS`) even after `/livez` is already
  returning 200, to give Day 1's validation a real, observable distinction
  between "alive" and "ready" rather than the two endpoints being
  trivially identical. Failing this probe removes the Pod from the
  Service's endpoints without restarting it.

## Resources

Every container has both requests and limits set explicitly:

| | CPU | Memory |
|---|---|---|
| requests | 50m | 32Mi |
| limits | 250m | 128Mi |

Requests are what the scheduler uses to place the Pod on the node's
available capacity; limits are what the kubelet/container runtime
enforces at runtime (CPU throttling past 250m, OOM-kill past 128Mi).
Both are deliberately small, matching a tiny stdlib HTTP server with no
real workload behind it.

## Security baseline

Every field in the pod and container `securityContext` is set
explicitly rather than left to defaults:

- `runAsNonRoot: true`, `runAsUser: 10001`, `runAsGroup: 10001` - the
  container never runs as UID 0, matching the Dockerfile's own
  `USER 10001:10001`.
- `allowPrivilegeEscalation: false` and `capabilities.drop: [ALL]` -
  the process cannot gain privileges beyond what it starts with, and
  starts with none of the default capability set.
- `readOnlyRootFilesystem: true` - the container filesystem is mounted
  read-only; the application does no file I/O at runtime, so this
  imposes no functional constraint (verified by the real-cluster smoke
  test running successfully under this constraint).
- `seccompProfile.type: RuntimeDefault` - the container is confined to
  the container runtime's default allowed syscall set.
- `automountServiceAccountToken: false` - no Kubernetes API credentials
  are mounted into the Pod at all, since nothing in this Pod talks to
  the API server. (Day 5 introduces a purpose-built ServiceAccount with
  scoped RBAC for the workload that will need it.)

## Reconciliation

Kubernetes controllers work by continuously reconciling observed state
toward desired state, not by executing one-shot imperative commands.
Day 1's validation proves this directly rather than assuming it: it
records both running Pods' UIDs, deletes exactly one Pod, and *does not*
create a replacement - then waits for the Deployment to report 2/2 Ready
replicas again. The ReplicaSet controller is what notices the gap and
creates the replacement; the proof checks that a genuinely new Pod UID
appears (not the same Pod restarted) while the untouched Pod's UID is
unchanged, and re-runs the HTTP checks afterward to confirm the Service
still routes correctly once reconciliation completes.

## Why port-forward instead of NodePort/Ingress on Day 1

Day 1's architecture goal is proving the core Kubernetes primitives -
Namespace, ConfigMap, Deployment, ReplicaSet, Pod, Service, probes,
security context, and controller reconciliation - without also standing
up cluster-external networking concerns that belong to later stages.
`kubectl port-forward` gives real HTTP access to the Service for both
human use and automated validation with zero additional cluster surface
area: no NodePort opened on the node, no cloud LoadBalancer to
provision, no Ingress controller to install and configure. It's also
inherently bounded to the local machine and the lifetime of the
`kubectl` process, which matches Day 1's validation requirement that
nothing external is exposed and nothing is left running afterward beyond
what's needed for review.

---
name: workload-security-validation
description: Verify the security baseline of this project's workload container and pod - non-root UID/GID, capability drops, seccomp, read-only root filesystem, no privilege escalation, per-workload ServiceAccount/automountServiceAccountToken, RBAC scope, NetworkPolicy allow/deny paths, and (as of Day 6) Istio ambient mesh mTLS/AuthorizationPolicy identity - against both the manifest/Helm chart and, when a cluster is live, real runtime evidence. Use whenever the Dockerfile, deployment/statefulset securityContext, or the Helm chart's PeerAuthentication/AuthorizationPolicy templates change, or when asked to confirm the security posture.
---

# Workload security validation

Security-baseline checklist for the maops-kubernetes-platform workload,
covering the container image, the Kubernetes securityContext, and (as
of the released Day 5 / `v0.5.0` baseline, extended by Day 6's
`v0.6.0`) identity, network, and mesh boundaries. This is the
security-specific companion to `manifest-validation` (which checks
presence/shape of fields in the frozen `k8s/base` source) and to the
`kubernetes-security-reviewer` agent (which owns judgment calls and
RBAC/NetworkPolicy/mesh review).

Days 1-5 (`v0.1.0`-`v0.5.0`) are released and frozen. Day 6 (`v0.6.0`)
is **merged to `main` as a local kind reference platform** but **not
yet tagged or published**
- Helm packaging, the Gateway API (Istio as the sole controller), and
Istio ambient service mesh (strict mTLS, identity-scoped
`AuthorizationPolicy`, no sidecars, no waypoint - so still no
request-level/L7 policy, that remains architecturally unavailable
without a waypoint this project never deploys) are now in scope below,
not future content. Recreate, Blue-Green, and Canary deployment
strategy demonstrations belong to Day 7, not yet implemented - nothing
below should be read as expecting those.

**As of Day 6, the static-check target moves from `k8s/base` to
`charts/maops-kubernetes-platform`** (rendered via `helm template`, not
`kubectl kustomize`) - `k8s/base` is frozen and no longer the live
application source. Everything below that says "render with `kubectl
kustomize k8s/base`" describes the still-valid frozen-source check;
prefer `helm template maops-kubernetes-platform-day6
charts/maops-kubernetes-platform --namespace maops-platform` (or `make
helm-template`) when checking the active Day 6 posture, and the
validation namespace is `maops-day6-validation` (was
`maops-day5-validation`).

## Static checks (no cluster required)

Render with `kubectl kustomize k8s/base` and confirm, at minimum, for
**all three** workloads (`maops-gateway`, `maops-app`, `maops-state`):

| Field | Expected |
|---|---|
| `spec.template.spec.securityContext.runAsNonRoot` | `true` |
| `spec.template.spec.securityContext.runAsUser` | `10001` |
| `spec.template.spec.securityContext.runAsGroup` | `10001` |
| `spec.template.spec.securityContext.fsGroup` | `10001` |
| `spec.template.spec.securityContext.seccompProfile.type` | `RuntimeDefault` |
| `containers[0].securityContext.allowPrivilegeEscalation` | `false` |
| `containers[0].securityContext.readOnlyRootFilesystem` | `true` |
| `containers[0].securityContext.capabilities.drop` | `["ALL"]` |
| `spec.template.spec.serviceAccountName` | the workload's own dedicated ServiceAccount (`maops-gateway`/`maops-app`/`maops-state`), never `default` |
| `spec.template.spec.automountServiceAccountToken` | `false` |
| ServiceAccount object's own `automountServiceAccountToken` | `false` (asserted at both the ServiceAccount and the pod level - belt-and-suspenders) |
| Secret volume `internal-auth` -> mountPath `/var/run/secrets/maops` (gateway/app) | `readOnly: true` |
| Secret volume `state-auth` -> mountPath (app/state) | `readOnly: true` |
| Secret volume `defaultMode` | `288` (octal `0440`) |

These are enforced by `scripts/validate_manifests.py` and run via
`make manifest-check` - re-run that first rather than eyeballing YAML.
`fsGroup: 10001` + `defaultMode: 288` is what lets UID/GID `10001:10001`
actually read a mounted Secret file without it ever being
world-readable - flag either a missing `fsGroup` or a looser
`defaultMode` (e.g. `0644`) as a finding, not just a style note.

Also check `app/Dockerfile`, `gateway/Dockerfile`, and `state/Dockerfile`
(all three must agree):

- Base image pinned by digest, no package manager present, no
  `apt`/`pip` install steps.
- `USER 10001:10001` matches the pod/container securityContext exactly.
- No `RUN` step that requires root at build *or* runtime.

And the application code itself (`app/server.py`, `gateway/server.py`,
`state/server.py`):

- Internal/state token comparisons use `hmac.compare_digest()`, never
  `==`.
- A token is never written to a log line, never included in any HTTP
  response body (including `GET /config`, which must only ever surface
  `APP_*`-prefixed ConfigMap-derived environment variables), and is
  read from its mounted Secret *file*, never an environment variable.

### RBAC (static)

`k8s/base/diagnostics-role.yaml` and `diagnostics-rolebinding.yaml` are
the only RBAC objects in this project. Confirm:

- Exactly one `Role`/`RoleBinding` pair exists, both namespaced (never
  a `ClusterRole`/`ClusterRoleBinding` anywhere in `k8s/base`).
- The `Role` grants only `get`/`list`/`watch` on `pods`, `services`,
  and `endpointslices` - no `secrets`, no write verb
  (`create`/`update`/`patch`/`delete`/`deletecollection`), no wildcard
  apiGroup or resource.
- The `RoleBinding`'s only subject is ServiceAccount
  `maops-diagnostics` in `maops-day5-validation`. No application
  ServiceAccount (`maops-gateway`/`maops-app`/`maops-state`) is ever a
  subject of this or any other RoleBinding.

### NetworkPolicy (static)

Seven `networking.k8s.io/v1` objects live in `k8s/base/`, all
namespaced to `maops-platform`. Confirm the shape matches exactly:

- `maops-default-deny-all` - `podSelector: {}`, both `Ingress` and
  `Egress` in `policyTypes`, no rules. This is the enforcement
  baseline; everything else below is purely additive.
- DNS egress allowed for every Pod (UDP+TCP 53 to `kube-system`'s
  `kube-dns`).
- **Allowed:** `gateway -> app` (matched ingress+egress pair, TCP 8080),
  `app -> state` (matched ingress+egress pair), and
  `validation-client` (in `maops-day5-validation`) `-> gateway` only -
  the sole path into `maops-platform` from outside it.
- **Must remain denied, checked negatively, not just unconfigured:**
  `gateway -> state` (no rule anywhere grants this), and
  `validation-client -> app` / `validation-client -> state` (checked
  against every NetworkPolicy object in the namespace, not just the
  app/state ingress-allow policies specifically, so a bypass under an
  unrelated policy name is still caught). Flag either direction
  appearing as a hard finding, never a footnote.
- No policy grants any Pod in `maops-platform` egress toward the
  Kubernetes API server.

`scripts/validate_manifests.py` enforces all of the above statically
(`rbac.*`, `networkpolicy.*` checks) and runs via `make manifest-check`
against the frozen `k8s/base` source.

### Day 6 delta (static) - eight NetworkPolicies, plus mesh identity

`scripts/validate_helm_chart.py` enforces the equivalent for the ACTIVE
Day 6 Helm chart (`make helm-check`), with two topology changes from
the k8s/base shape above:

- `validation-client -> gateway` is REMOVED (flag its presence as a
  regression - `networkpolicy.no_day5_validation_shortcut` exists
  specifically to catch this).
- Replaced by: a deliberately PEER-LESS HBONE allow (TCP 15008, no
  `from`/`to` selector at all) and an ingress allow scoped to the
  Istio ingress Gateway (`maops-ingress` namespace +
  `istio.io/gateway-name: maops-edge` Pod label) - the one path into
  `maops-platform` from outside it as of Day 6. **Corrected after
  independent review:** an earlier revision scoped the HBONE rule's
  peer to `namespaceSelector: istio-system`, wrongly treating ztunnel
  (a per-node `hostNetwork: true` DaemonSet) as an ordinary namespaced
  Pod peer Cilium could match by namespace+label - it cannot; HBONE
  traffic carries the NODE's own identity, not a routable, namespaced
  Pod identity. The corrected rule is scoped by port alone, and never
  opens any application port more broadly - flag a reintroduced
  `namespaceSelector`/`podSelector` peer on this one rule as a
  regression (`networkpolicy.hbone.ingress_is_peerless`/
  `egress_is_peerless`).

**NetworkPolicy cannot see workload identity inside HBONE - this is
the load-bearing fact, not a footnote.** Cilium/Kubernetes
NetworkPolicy (including every rule above) controls network
reachability only; it has no visibility into which workload
originated a HBONE-encapsulated flow. Layered on top (new in Day 6,
also checked statically) is what ACTUALLY enforces the
`gateway -> app -> state` identity chain: a namespace-wide
`PeerAuthentication` with `mtls.mode: STRICT`, and three
`AuthorizationPolicy` objects (one per workload, `action: ALLOW`,
L4-compatible `source.principals` matching only - never `to.operation`,
which would require a waypoint this project does not deploy) naming
exactly: Istio ingress Gateway SA (`maops-edge-istio`, Istio's own
deterministic naming) -> `maops-gateway`; `maops-gateway` SA ->
`maops-app`; `maops-app` SA -> `maops-state`. The diagnostics/
validation-client identity must never appear as a principal anywhere,
and `maops-gateway`'s principal must never appear on the state policy -
both checked negatively (`mesh.authz.*` checks). Application-layer
Secret authentication (`maops-internal-auth`/`maops-state-auth`)
remains a third, independent layer on top of both.

## Runtime checks (require a live kind cluster)

The example commands below use Day 5's context/namespace strings
(`kind-maops-k8s-day5`) to illustrate the technique against the frozen
source - substitute `kind-maops-k8s-day6`/`maops-day6-validation` when
running the equivalent proof against the Day 6 cluster (see
`docs/architecture.md`'s "DAY6: live validation record" for the
recorded Day 6 run).

These prove the security posture actually holds at runtime, not just in
the manifest - the API server can normalize/default fields, and the
distroless image has no shell, so use `kubectl exec` with the Python
interpreter directly rather than `/bin/sh`:

```bash
POD=$(kubectl --context kind-maops-k8s-day5 -n maops-platform get pods \
  -l app.kubernetes.io/name=maops-kubernetes-platform,app.kubernetes.io/component=gateway \
  -o jsonpath='{.items[0].metadata.name}')

# Real UID/GID the process runs as
kubectl --context kind-maops-k8s-day5 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import os; print(os.getuid(), os.getgid())"

# Live pod spec's security fields as the API server actually recorded them
kubectl --context kind-maops-k8s-day5 -n maops-platform get pod "$POD" -o json \
  | python3 -c "import json,sys; p=json.load(sys.stdin); c=p['spec']['containers'][0]; \
      print(c['securityContext']); print(p['spec']['securityContext'])"

# Prove the Secret file is actually readable WITHOUT ever printing its value
kubectl --context kind-maops-k8s-day5 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import sys; sys.stdout.write(str(len(open('/var/run/secrets/maops/internal-token','rb').read())))"

# Confirm no token is mounted for an application workload despite its ServiceAccount
kubectl --context kind-maops-k8s-day5 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import os; print(os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'))"
```

Repeat with `app.kubernetes.io/component=app` and `component=state` -
every runtime claim in this skill must be checked for all three
workloads.

`scripts/cluster_check.py` (`make rollout-check`), `scripts/secret_check.py`
(`make secret-check`), and `scripts/state_check.py` (`make state-check`)
automate the securityContext/Secret/token-mount proofs above and are
the authoritative source of truth - prefer running them over ad hoc
commands, and only fall back to manual `kubectl exec` when debugging a
failure they report. Never write a manual command that echoes a
decoded Secret value to the terminal.

### RBAC (runtime)

`scripts/rbac_check.py` (`make rbac-check`) proves, from inside a probe
Pod running as `maops-diagnostics` with its real mounted token, direct
HTTPS calls to `https://kubernetes.default.svc`:

- `pods`/`services`/`endpointslices` reads in `maops-platform` return
  `200`.
- `secrets` reads, a Deployment `DELETE`, a Deployment `/scale` `PATCH`,
  a cross-namespace Pod read (e.g. `kube-system`), and a cluster-scoped
  Node read all return exactly `403 Forbidden` - never inferred from a
  bare non-2xx response.

### NetworkPolicy (runtime)

`scripts/networkpolicy_check.py` (`make networkpolicy-check`) proves,
using real in-cluster TCP connection attempts (never only a
port-forward, which never traverses the pod network as a
policy-visible peer):

- `validation-client -> gateway` succeeds; `validation-client -> app`
  and `validation-client -> state` are blocked. **Day 6 change:** as of
  the Day 6 Helm chart, `validation-client -> gateway` is now ALSO
  blocked (the script is updated to assert this) - the Gateway API
  path is the only way in.
- `gateway -> app` and `app -> state` succeed; `gateway -> state` is
  blocked.
- DNS resolution still works under the default-deny egress baseline.

A policy-blocked connection is typically dropped silently rather than
actively refused - every probe uses a short, explicit client-side
timeout as the only reliable "genuinely blocked" signal.

### Mesh identity/mTLS (runtime, Day 6)

`scripts/mesh_check.py` (`make mesh-check`) and `scripts/gateway_check.py`
(`make gateway-check`) prove, against a live cluster:
ztunnel/istiod/istio-cni health; application Pods carry no
`istio-proxy` sidecar container and DO carry the ambient CNI's
redirection-enabled annotation; the live `PeerAuthentication` is
STRICT; the live `AuthorizationPolicy` objects carry exactly the
intended principals (never the diagnostics/validation-client identity,
never `gateway -> state`); and the external Gateway API path reaches
`maops-gateway` while a wrong `Host` header does not. Every `kubectl`
call these scripts make distinguishes a genuine API/HTTP failure
(INCONCLUSIVE, its own explicit finding) from a genuine policy
denial - never conflate a `kubectl`/HTTP timeout with proof of denial.
A raw TCP `connect()` from an ambient-enrolled Pod proves only that
the source node's local ztunnel accepted the socket - identity denial
is proven only by correlated ztunnel access-log evidence (AUTHORITATIVE
/ CANDIDATE / BEST_EFFORT tiers, reported separately) plus an HTTP-layer
leak check. See `docs/architecture.md`'s "DAY6: live validation record"
for the recorded run and its tier breakdown.

### CNI (runtime)

`scripts/cni_check.py` (`make cni-status`, READ-ONLY) confirms every
node is `Ready`, the Cilium agent DaemonSet has exactly one Ready Pod
per node (the actual NetworkPolicy enforcement point), the Cilium
operator has at least one available replica, and the kube-proxy
DaemonSet still has Ready Pods (kube-proxy remains enabled - Cilium is
adopted for NetworkPolicy enforcement only, not as a kube-proxy
replacement).

## Storage, persistence, availability, and rollout validations remain required

Nothing below is superseded or weakened by the Day 5 identity/network
additions above - all remain required checks:

- Storage hardening (`make storage-hardening-check`): the
  `local-path-provisioner` directory permissions (`root:10001`, mode
  `2770`) and the positive (matching UID/GID+`fsGroup`) / negative
  (unrelated UID/GID -> `EACCES`) proof against a disposable scratch
  PVC.
- Persistence (`make persistence-check`) and PVC retention
  (`make retention-check`): data survives `maops-state-0` Pod
  deletion/rescheduling and a scale-to-zero/back-to-one cycle, with the
  same PVC/PV retained throughout.
- PodDisruptionBudget behavior (`make pdb-check`): `minAvailable: 2`
  for gateway/app, real Eviction-API rejection under budget, restored
  afterward.
- Scaling (`make scaling-check`) and rolling update/rollback
  (`make rolling-update-check`): real 3 -> 4 -> 3 scaling and a real
  `kubectl rollout undo`, both guaranteed to restore the baseline.
- Secret handling (`make secret-check`): both `maops-internal-auth` and
  `maops-state-auth` mount read-only, non-disclosure proven end to end.

Do not remove, skip, or weaken any of the above when validating the
Day 5 security boundary - they are independent, still-enforced
properties of this workload, not superseded by ServiceAccounts/RBAC/
NetworkPolicy.

## Scope boundaries (as of the Day 6 implementation, `v0.6.0`)

Expected and correct as of `v0.5.0` (frozen `k8s/base`) and unchanged
in design for Day 6:

- A dedicated ServiceAccount per workload
  (`maops-gateway`/`maops-app`/`maops-state`/`maops-diagnostics`) and a
  single namespace-scoped `Role`/`RoleBinding` for `maops-diagnostics`
  only.
- Runtime-bootstrapped Secrets (`maops-internal-auth` since Day 2,
  `maops-state-auth` since Day 4) live in the cluster - correct, not a
  violation, as long as neither is ever a *committed* Secret object in
  `k8s/base` OR the Helm chart's rendered output/`values.yaml`.
- Cilium `1.20.1` as the enforcing CNI dataplane, kube-proxy left
  enabled.

Newly expected and correct as of Day 6 (`v0.6.0`, not yet a scope
violation - these are required Day 6 deliverables):

- Eight `networking.k8s.io/v1` NetworkPolicy objects (the Day 5 seven,
  minus `validation-client -> gateway`, plus the HBONE allow and the
  Istio-ingress-Gateway-scoped allow - see "Day 6 delta" above).
- Istio ambient mesh: a namespace-wide STRICT `PeerAuthentication` and
  three identity-scoped `AuthorizationPolicy` objects, no sidecars, no
  waypoint.
- Cilium reconfigured for ambient coexistence (`cni.exclusive=false`,
  `socketLB.hostNamespaceOnly=true`, `envoy.enabled=false`, a single
  non-HA operator replica).
- The application packaged as a Helm chart, with `k8s/base` frozen and
  untouched.

Still explicitly **not** expected, and a scope violation if found:

- Any `ClusterRole`/`ClusterRoleBinding` anywhere.
- Any RBAC grant, RoleBinding subject, or mounted API token for
  `maops-gateway`, `maops-app`, or `maops-state`.
- Any `gateway -> state` or `validation-client -> app`/`-> gateway`/
  `-> state` NetworkPolicy or AuthorizationPolicy allow.
- Hubble, any `to.operation` (L7/HTTP-aware) `AuthorizationPolicy` rule
  or waypoint proxy, L7/HTTP-aware `CiliumNetworkPolicy` rules, a
  Cilium Gateway API controller, kube-proxy replacement, TLS/
  cert-manager, or a cloud LoadBalancer.
- A second, Ingress-based routing implementation alongside the Gateway
  API.
- `HorizontalPodAutoscaler`, Argo Rollouts, or a Recreate/Blue-Green/
  Canary deployment strategy - Day 7 scope, not yet implemented.

Flag any of the still-not-expected items above as a scope violation,
not just a style note.

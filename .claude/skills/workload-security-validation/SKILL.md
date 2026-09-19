---
name: workload-security-validation
description: Verify the security baseline of this project's workload container and pod - non-root UID/GID, capability drops, seccomp, read-only root filesystem, no privilege escalation, per-workload ServiceAccount/automountServiceAccountToken, RBAC scope, and NetworkPolicy allow/deny paths - against both the manifest and, when a cluster is live, real runtime evidence. Use whenever the Dockerfile or deployment/statefulset securityContext changes, or when asked to confirm the security posture.
---

# Workload security validation

Security-baseline checklist for the maops-kubernetes-platform workload,
covering the container image, the Kubernetes securityContext, and (as
of the released Day 5 / `v0.5.0` baseline) identity and network
boundaries. This is the security-specific companion to
`manifest-validation` (which checks presence/shape of fields) and to
the `kubernetes-security-reviewer` agent (which owns judgment calls and
RBAC/NetworkPolicy review).

Days 1-5 (`v0.1.0`-`v0.5.0`) are released and frozen; current version
is `v0.5.0`. Service mesh (mTLS, request-level/L7 policy) is Day 6
scope, not yet implemented. Recreate, Blue-Green, and Canary deployment
strategy demonstrations belong to Day 7, not yet implemented. Nothing
below should be read as expecting either.

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
(`rbac.*`, `networkpolicy.*` checks) and runs via `make manifest-check`.

## Runtime checks (require a live kind cluster)

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
  and `validation-client -> state` are blocked.
- `gateway -> app` and `app -> state` succeed; `gateway -> state` is
  blocked.
- DNS resolution still works under the default-deny egress baseline.

A policy-blocked connection is typically dropped silently rather than
actively refused - every probe uses a short, explicit client-side
timeout as the only reliable "genuinely blocked" signal.

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

## Scope boundaries (as of the released Day 5 baseline)

Expected and correct as of `v0.5.0`:

- A dedicated ServiceAccount per workload
  (`maops-gateway`/`maops-app`/`maops-state`/`maops-diagnostics`), a
  single namespace-scoped `Role`/`RoleBinding` for `maops-diagnostics`
  only, and seven `networking.k8s.io/v1` NetworkPolicy objects
  implementing default-deny + the narrow explicit allows listed above.
- Runtime-bootstrapped Secrets (`maops-internal-auth` since Day 2,
  `maops-state-auth` since Day 4) live in the cluster - correct, not a
  violation, as long as neither is ever a *committed* Secret object in
  `k8s/base`.
- Cilium `1.20.1` as the enforcing CNI dataplane, kube-proxy left
  enabled.

Still explicitly **not** expected, and a scope violation if found:

- Any `ClusterRole`/`ClusterRoleBinding` anywhere.
- Any RBAC grant, RoleBinding subject, or mounted API token for
  `maops-gateway`, `maops-app`, or `maops-state`.
- Any `gateway -> state` or `validation-client -> app`/`-> state`
  NetworkPolicy allow.
- Hubble, L7/HTTP-aware `CiliumNetworkPolicy` rules, kube-proxy
  replacement, or any service mesh construct (mTLS, request-level
  policy) - all Day 6 scope, not yet implemented.
- `HorizontalPodAutoscaler`, or a Recreate/Blue-Green/Canary deployment
  strategy - Day 7 scope, not yet implemented.

Flag any of the still-not-expected items above as a scope violation,
not just a style note.

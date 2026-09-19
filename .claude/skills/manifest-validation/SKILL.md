---
name: manifest-validation
description: Render and statically validate this project's Kubernetes manifests (k8s/base) without a live cluster - namespace, object counts, replicas, container/image/probe/resource fields, and Service/selector wiring. Use whenever k8s/base changes, before deploying, or when asked to check the manifests are correct.
---

# Manifest validation

Static, cluster-free validation for `k8s/base` in the
maops-kubernetes-platform project. This mirrors exactly what
`make manifest-check` runs.

## How to run it

```bash
kubectl kustomize k8s/base                 # render (equivalent to `make manifest-render`)
python3 scripts/manifest_check.py k8s/base # render + validate (equivalent to `make manifest-check`)
```

`scripts/manifest_check.py` renders with `kubectl kustomize`, parses the
result with the project's dependency-free YAML-subset loader
(`scripts/k8s_yaml.py`), and runs `scripts/validate_manifests.run_checks()`
against the parsed objects. It requires no live cluster and no
third-party Python package - if a change to this workflow introduces
either dependency, that's itself a finding to report.

## What "correct" means as of the released Day 5 / `v0.5.0` baseline

Check the exact expected values against `scripts/validate_manifests.py`'s
constants (`EXPECTED_NAMESPACE`, `GATEWAY_IMAGE`/`APP_IMAGE`/
`STATE_IMAGE`, `EXPECTED_REPLICAS`, `EXPECTED_REQUESTS`,
`EXPECTED_LIMITS`, `EXPECTED_BACKEND_HOST`/`EXPECTED_BACKEND_PORT`,
`EXPECTED_STRATEGY_TYPE`/`EXPECTED_MAX_UNAVAILABLE`/`EXPECTED_MAX_SURGE`/
`EXPECTED_MIN_READY_SECONDS`/`EXPECTED_PROGRESS_DEADLINE_SECONDS`/
`EXPECTED_REVISION_HISTORY_LIMIT`, `EXPECTED_MAX_SKEW`/
`EXPECTED_TOPOLOGY_KEY`/`EXPECTED_WHEN_UNSATISFIABLE`,
`EXPECTED_PDB_MIN_AVAILABLE`, plus the Day 5 RBAC/NetworkPolicy
constants, etc.) rather than memorizing them here, since a later day may
extend this file. As of the released Day 5 baseline the checks cover
**three workload identities** (`maops-gateway`, `maops-app`,
`maops-state`) independently, the `maops-diagnostics` identity and its
RBAC grant, seven NetworkPolicy objects, and cross-workload/
cross-namespace isolation:

- Two Namespaces (`maops-platform`, `maops-day5-validation`), three
  ConfigMaps (`maops-gateway-config`, `maops-app-config`,
  `maops-state-config`), four ServiceAccounts (`maops-gateway`,
  `maops-app`, `maops-state`, `maops-diagnostics`), one `Role` +
  `RoleBinding` (`maops-diagnostics-reader` /
  `maops-diagnostics-reader-binding`), two Deployments
  (`maops-gateway`, `maops-app`, each replicas == 3, single container),
  one StatefulSet (`maops-state`, replicas == 1, PVC-backed `/data` -
  inherited unchanged from Day 4), four Services (`maops-gateway`,
  `maops-app`, `maops-state`, `maops-state-headless`, each ClusterIP or
  the headless governing Service `maops-state-headless` requires),
  two PodDisruptionBudgets (`maops-gateway-pdb`, `maops-app-pdb` -
  `maops-state` carries neither, meaningless for a single replica),
  seven NetworkPolicies (`maops-default-deny-all`,
  `maops-allow-dns-egress`, the `gateway<->app` and `app<->state`
  matched ingress/egress pairs, and
  `maops-allow-gateway-ingress-from-validation`) - 27 rendered objects
  total (confirm with `kubectl kustomize k8s/base | grep '^kind:' | sort | uniq -c`,
  since a later day may change this count).
- Images are `maops-kubernetes-gateway:<VERSION>` /
  `maops-kubernetes-app:<VERSION>` / `maops-kubernetes-state:<VERSION>`
  with `imagePullPolicy: IfNotPresent`, and every
  `app.kubernetes.io/version` label (including pod/StatefulSet template
  labels, and now the ServiceAccount/Role/RoleBinding/NetworkPolicy
  objects too) matches `VERSION` (see `make version-check`, which
  closes `DAY1-REL-I1`).
- **RollingUpdate tuning**: `strategy.type == RollingUpdate`,
  `maxUnavailable == 1`, `maxSurge == 1`, `minReadySeconds == 5`,
  `progressDeadlineSeconds == 120`, `revisionHistoryLimit == 5` on both
  Deployments (gateway/app only - `maops-state`'s StatefulSet update
  strategy is a separate, unrelated field) - pinned explicitly, not
  left to Kubernetes defaults.
- **Scheduling**: a required `nodeAffinity` term excluding
  `node-role.kubernetes.io/control-plane` (operator `DoesNotExist`) on
  all three workloads, plus exactly one `topologySpreadConstraints`
  entry each on gateway/app (`maxSkew: 1`,
  `topologyKey: kubernetes.io/hostname`,
  `whenUnsatisfiable: DoNotSchedule`) whose `labelSelector` is scoped to
  that workload's own `component` only - gateway's spread must never be
  satisfiable by app Pod labels, and vice versa (`maops-state` carries
  no spread constraint - meaningless for a single replica).
- **PodDisruptionBudget**: `apiVersion: policy/v1`, `minAvailable: 2`
  (never `maxUnavailable`), correct namespace, selector satisfied by its
  own workload's pod labels and NOT by either other workload's
  (gateway/app only).
- Gateway `BACKEND_HOST` == `maops-app` exactly, app `STATE_HOST` ==
  `maops-state` exactly (never an IP literal, never a hardcoded
  Pod/ReplicaSet-style identity).
- startupProbe present; livenessProbe hits `/livez` for all three
  workloads (never a backend-dependent path - that would be a
  circular-liveness bug); readinessProbe hits `/readyz` for all three.
- Container resources match the pinned request/limit values exactly for
  all three workloads.
- Pod/container securityContext fields (see the
  `workload-security-validation` skill for the full security-specific
  checklist - this skill only confirms they're *present and match*, not
  the security rationale).
- Each workload's `spec.template.spec.serviceAccountName` names its own
  dedicated ServiceAccount (never `default`), and
  `automountServiceAccountToken: false` is asserted at both the pod
  level and the ServiceAccount object itself, for `maops-gateway`,
  `maops-app`, and `maops-state`. `maops-diagnostics` is the one
  deliberate exception (`automountServiceAccountToken: true`, in
  `maops-day5-validation`).
- **RBAC**: exactly one `Role` (`get`/`list`/`watch` only, on
  `pods`/`services`/`endpointslices` only - no `secrets`, no write verb,
  no wildcard) and one `RoleBinding` whose only subject is
  `maops-diagnostics`; no application ServiceAccount is ever a
  RoleBinding subject; no `ClusterRole`/`ClusterRoleBinding` anywhere.
- **NetworkPolicy**: `maops-default-deny-all` selects every Pod in
  `maops-platform` for both `Ingress`/`Egress`; DNS egress is allowed
  namespace-wide; `gateway<->app` and `app<->state` are each a matched
  ingress+egress pair; `validation-client -> gateway` (cross-namespace,
  from `maops-day5-validation`) is the only path in. Check negatively,
  not just for absence of an extra rule: no policy anywhere grants
  `gateway -> state`, and no policy anywhere grants
  `validation-client -> app` or `validation-client -> state`.
- Each Service selector is satisfied by its *own* workload's pod
  template labels, and - critically - is **not** satisfiable by either
  other workload's pod labels (selector-collision check) - verify this
  against the *rendered* output, since label merging can surprise you.
- No `nodePort`, no `hostNetwork`, no `hostPort`, no `Ingress` object.
- Each ConfigMap is wired into its own container via `envFrom` or
  `env[].valueFrom`; no ConfigMap contains secret-like data.
- Gateway/app reference Secret `maops-internal-auth`, and app/state
  reference Secret `maops-state-auth`, both via a read-only volume with
  `defaultMode: 288` - but neither Secret *object itself* must ever be
  rendered by `k8s/base` (both are bootstrapped out-of-band, see the
  `kind-cluster-validation` skill).
- `maops-state`'s `volumeClaimTemplates` requests a `data` claim
  (`ReadWriteOnce`, `persistentVolumeClaimRetentionPolicy` explicitly
  `Retain`/`Retain`), mounted at `/data` - inherited unchanged from Day
  4; `maops-state-headless` (`clusterIP: None`) is the StatefulSet's
  required governing Service, not used for normal traffic.
- No forbidden kind is present at any day: Secret (as a committed
  object), Ingress, `HorizontalPodAutoscaler`, and - specifically for
  RBAC - any `ClusterRole`/`ClusterRoleBinding` (a namespaced `Role`/
  `RoleBinding` for `maops-diagnostics` only is correct as of Day 5, not
  a violation).

## When a check fails

Read the FAIL line's expected-vs-found detail (the script prints both),
find the offending field in `k8s/base/*.yaml`, and fix the manifest -
never relax the check in `scripts/validate_manifests.py` to make a real
misconfiguration pass. If you believe a check itself is wrong (e.g. the
day's spec genuinely changed), that's a decision for the user, not a
silent edit.

## Extending validation for a later day

When a later day's manifests add new object kinds or fields, add a new
check function call inside `run_checks()` and a matching unit test in
`tests/test_validate_manifests.py` (positive case in the baseline test,
at least one negative case) - see the `kubernetes-test-engineer` agent
for the testing standard this project holds itself to.

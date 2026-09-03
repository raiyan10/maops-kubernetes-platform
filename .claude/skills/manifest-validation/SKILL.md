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

## What "correct" means as of Day 2

Check the exact expected values against `scripts/validate_manifests.py`'s
constants (`EXPECTED_NAMESPACE`, `GATEWAY_IMAGE`/`APP_IMAGE`,
`EXPECTED_REPLICAS`, `EXPECTED_REQUESTS`, `EXPECTED_LIMITS`,
`EXPECTED_BACKEND_HOST`/`EXPECTED_BACKEND_PORT`, etc.) rather than
memorizing them here, since a later day may extend this file. As of
Day 2 the checks cover **both workloads** (`maops-gateway`, `maops-app`)
independently, plus cross-workload isolation:

- Exactly one Namespace (`maops-platform`), two Deployments
  (`maops-gateway`, `maops-app`, each replicas == 2, single container),
  two Services (each ClusterIP), two ConfigMaps
  (`maops-gateway-config`, `maops-app-config`).
- Images are `maops-kubernetes-gateway:<VERSION>` /
  `maops-kubernetes-app:<VERSION>` with `imagePullPolicy: IfNotPresent`,
  and every `app.kubernetes.io/version` label (including pod template
  labels) matches `VERSION` (see `make version-check`, which closes
  `DAY1-REL-I1`).
- Gateway `BACKEND_HOST` == `maops-app` exactly (never an IP literal,
  never a hardcoded Pod/ReplicaSet-style identity), `BACKEND_PORT` ==
  `8080`.
- startupProbe present; livenessProbe hits `/livez` for both workloads
  (never a backend-dependent path - that would be a circular-liveness
  bug); readinessProbe hits `/readyz` for both.
- Container resources match the pinned request/limit values exactly for
  both workloads.
- Pod/container securityContext fields (see the
  `workload-security-validation` skill for the full security-specific
  checklist - this skill only confirms they're *present and match*, not
  the security rationale).
- `automountServiceAccountToken: false` for both.
- Each Service selector is satisfied by its *own* workload's pod
  template labels, and - critically - is **not** satisfiable by the
  other workload's pod labels (selector-collision check) - verify this
  against the *rendered* output, since label merging can surprise you.
- No `nodePort`, no `hostNetwork`, no `hostPort`.
- Each ConfigMap is wired into its own container via `envFrom` or
  `env[].valueFrom`; neither ConfigMap contains secret-like data.
- Both Deployments reference Secret `maops-internal-auth` via a
  read-only volume (`internal-auth`) mounted at
  `/var/run/secrets/maops` - but the Secret *object itself* must never
  be rendered by `k8s/base` (it's bootstrapped out-of-band, see the
  `kind-cluster-validation` skill).
- None of the forbidden-for-this-day kinds are present: Secret (as a
  committed object), Ingress, PersistentVolumeClaim, StatefulSet, Role,
  RoleBinding, ClusterRole, ClusterRoleBinding, ServiceAccount,
  NetworkPolicy.

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

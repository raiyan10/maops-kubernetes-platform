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

## What "correct" means for Day 1

Check the exact expected values against `scripts/validate_manifests.py`'s
constants (`EXPECTED_NAMESPACE`, `EXPECTED_IMAGE`, `EXPECTED_REPLICAS`,
`EXPECTED_REQUESTS`, `EXPECTED_LIMITS`, etc.) rather than memorizing them
here, since a later day may extend this file. As of Day 1 the checks
cover:

- Exactly one Namespace (`maops-platform`), one Deployment (`maops-app`,
  replicas == 2, single container named `maops-app`), one Service
  (`maops-app`, type ClusterIP), one ConfigMap (`maops-app-config`).
- Image is `maops-kubernetes-platform:0.1.0` with
  `imagePullPolicy: IfNotPresent`.
- startupProbe present; livenessProbe hits `/livez`; readinessProbe hits
  `/readyz`.
- Container resources match the pinned request/limit values exactly.
- Pod/container securityContext fields (see the
  `workload-security-validation` skill for the full security-specific
  checklist - this skill only confirms they're *present and match*, not
  the security rationale).
- `automountServiceAccountToken: false`.
- Service selector is satisfied by the Deployment's pod template labels
  - verify this against the *rendered* output, since label merging can
    surprise you.
- No `nodePort`, no `hostNetwork`, no `hostPort`.
- ConfigMap is wired into the container via `envFrom` or `env[].valueFrom`.
- None of the Day-1-forbidden kinds are present: Secret, Ingress,
  PersistentVolumeClaim, StatefulSet, Role, RoleBinding, ClusterRole,
  ClusterRoleBinding, ServiceAccount, NetworkPolicy.

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

---
name: workload-security-validation
description: Verify the security baseline of this project's workload container and pod - non-root UID/GID, capability drops, seccomp, read-only root filesystem, no privilege escalation, automountServiceAccountToken false, and (from a later day) RBAC/NetworkPolicy scope - against both the manifest and, when a cluster is live, real runtime evidence. Use whenever the Dockerfile or deployment securityContext changes, or when asked to confirm the security posture.
---

# Workload security validation

Security-baseline checklist for the maops-kubernetes-platform workload,
covering both the container image and the Kubernetes securityContext.
This is the security-specific companion to `manifest-validation` (which
checks presence/shape of fields) and to the `kubernetes-security-reviewer`
agent (which owns judgment calls and RBAC/NetworkPolicy review from the
day those are introduced).

## Static checks (no cluster required)

Render with `kubectl kustomize k8s/base` and confirm, at minimum:

| Field | Expected (Day 1) |
|---|---|
| `spec.template.spec.securityContext.runAsNonRoot` | `true` |
| `spec.template.spec.securityContext.runAsUser` | `10001` |
| `spec.template.spec.securityContext.runAsGroup` | `10001` |
| `spec.template.spec.securityContext.seccompProfile.type` | `RuntimeDefault` |
| `containers[0].securityContext.allowPrivilegeEscalation` | `false` |
| `containers[0].securityContext.readOnlyRootFilesystem` | `true` |
| `containers[0].securityContext.capabilities.drop` | `["ALL"]` |
| `spec.template.spec.automountServiceAccountToken` | `false` |

These are enforced by `scripts/validate_manifests.py` and run via
`make manifest-check` - re-run that first rather than eyeballing YAML.

Also check `app/Dockerfile`:

- Base image pinned by digest, no package manager present, no
  `apt`/`pip` install steps.
- `USER 10001:10001` matches the pod/container securityContext exactly.
- No `RUN` step that requires root at build *or* runtime.

## Runtime checks (require a live kind cluster)

These prove the security posture actually holds at runtime, not just in
the manifest - the API server can normalize/default fields, and the
distroless image has no shell, so use `kubectl exec` with the Python
interpreter directly rather than `/bin/sh`:

```bash
POD=$(kubectl --context kind-maops-k8s-day1 -n maops-platform get pods \
  -l app.kubernetes.io/name=maops-kubernetes-platform -o jsonpath='{.items[0].metadata.name}')

# Real UID/GID the process runs as
kubectl --context kind-maops-k8s-day1 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import os; print(os.getuid(), os.getgid())"

# Live pod spec's security fields as the API server actually recorded them
kubectl --context kind-maops-k8s-day1 -n maops-platform get pod "$POD" -o json \
  | python3 -c "import json,sys; p=json.load(sys.stdin); c=p['spec']['containers'][0]; \
      print(c['securityContext']); print(p['spec']['securityContext'])"
```

`scripts/cluster_check.py` (run via `make rollout-check`) automates
exactly this and is the authoritative source of truth - prefer running it
over ad hoc commands, and only fall back to manual `kubectl exec` when
debugging a failure it reports.

## Day-1 scope boundaries

No Secret, ServiceAccount, RBAC object, or NetworkPolicy should exist yet
- those belong to Day 2 (Secrets) and Day 5 (ServiceAccount/RBAC/
NetworkPolicy) per `docs/roadmap.md`. Flag any of these appearing early
as a scope violation, not just a style note.

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

Render with `kubectl kustomize k8s/base` and confirm, at minimum, for
**both** `maops-gateway` and `maops-app`:

| Field | Expected (as of Day 2) |
|---|---|
| `spec.template.spec.securityContext.runAsNonRoot` | `true` |
| `spec.template.spec.securityContext.runAsUser` | `10001` |
| `spec.template.spec.securityContext.runAsGroup` | `10001` |
| `spec.template.spec.securityContext.fsGroup` | `10001` |
| `spec.template.spec.securityContext.seccompProfile.type` | `RuntimeDefault` |
| `containers[0].securityContext.allowPrivilegeEscalation` | `false` |
| `containers[0].securityContext.readOnlyRootFilesystem` | `true` |
| `containers[0].securityContext.capabilities.drop` | `["ALL"]` |
| `spec.template.spec.automountServiceAccountToken` | `false` |
| Secret volume `internal-auth` -> mountPath `/var/run/secrets/maops` | `readOnly: true` |
| Secret volume `defaultMode` | `288` (octal `0440`) |

These are enforced by `scripts/validate_manifests.py` and run via
`make manifest-check` - re-run that first rather than eyeballing YAML.
`fsGroup: 10001` + `defaultMode: 288` is what lets UID/GID `10001:10001`
actually read the mounted Secret file without it ever being
world-readable - flag either a missing `fsGroup` or a looser
`defaultMode` (e.g. `0644`) as a finding, not just a style note.

Also check `app/Dockerfile` and `gateway/Dockerfile` (both must agree):

- Base image pinned by digest, no package manager present, no
  `apt`/`pip` install steps.
- `USER 10001:10001` matches the pod/container securityContext exactly.
- No `RUN` step that requires root at build *or* runtime.

And the application code itself (`app/server.py`, `gateway/server.py`):

- Internal token comparison uses `hmac.compare_digest()`, never `==`.
- The token is never written to a log line, never included in any HTTP
  response body (including `GET /config`, which must only ever surface
  `APP_*`-prefixed ConfigMap-derived environment variables), and is read
  from the mounted Secret *file*, never an environment variable.

## Runtime checks (require a live kind cluster)

These prove the security posture actually holds at runtime, not just in
the manifest - the API server can normalize/default fields, and the
distroless image has no shell, so use `kubectl exec` with the Python
interpreter directly rather than `/bin/sh`:

```bash
POD=$(kubectl --context kind-maops-k8s-day2 -n maops-platform get pods \
  -l app.kubernetes.io/name=maops-kubernetes-platform,app.kubernetes.io/component=gateway \
  -o jsonpath='{.items[0].metadata.name}')

# Real UID/GID the process runs as
kubectl --context kind-maops-k8s-day2 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import os; print(os.getuid(), os.getgid())"

# Live pod spec's security fields as the API server actually recorded them
kubectl --context kind-maops-k8s-day2 -n maops-platform get pod "$POD" -o json \
  | python3 -c "import json,sys; p=json.load(sys.stdin); c=p['spec']['containers'][0]; \
      print(c['securityContext']); print(p['spec']['securityContext'])"

# Prove the Secret file is actually readable WITHOUT ever printing its value
kubectl --context kind-maops-k8s-day2 -n maops-platform exec "$POD" -- \
  /usr/bin/python3.11 -c "import sys; sys.stdout.write(str(len(open('/var/run/secrets/maops/internal-token','rb').read())))"
```

Repeat with `app.kubernetes.io/component=app` for the app workload -
every runtime claim in this skill must be checked for both.

`scripts/cluster_check.py` (run via `make rollout-check`) and
`scripts/secret_check.py` (run via `make secret-check`) automate exactly
this and are the authoritative source of truth - prefer running them over
ad hoc commands, and only fall back to manual `kubectl exec` when
debugging a failure they report. Never write a manual command that echoes
the decoded Secret value to the terminal.

## Scope boundaries (as of Day 2)

A runtime-bootstrapped Secret (`maops-internal-auth`) is expected to
exist live in the cluster from Day 2 onward - that's correct, not a
violation, as long as it's never a *committed* Secret object in
`k8s/base`. No ServiceAccount, RBAC object, or NetworkPolicy should
exist yet - those belong to Day 5 per `docs/roadmap.md`. Flag any of
those appearing early as a scope violation, not just a style note.

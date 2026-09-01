# Project 4 / Day 1 Kubernetes Security Review

- **Repository:** maops-kubernetes-platform
- **Branch:** feature/day-1-kubernetes-foundation
- **Target version:** v0.1.0
- **Reviewer:** kubernetes-security-reviewer (independent pass)
- **Scope:** workload container/pod security baseline, image security, Kubernetes
  security boundaries, Day 1 scope discipline, and the repository's own static
  validation logic. This review does not cover general manifest shape/architecture
  or test-suite design beyond the security-relevant static checks.
- **Method:** rendered manifest via `kubectl kustomize k8s/base`, static reading of
  `app/Dockerfile`, `app/server.py`, `scripts/validate_manifests.py`,
  `scripts/k8s_yaml.py`, `tests/test_validate_manifests.py`,
  `tests/test_k8s_yaml.py`, plus **live evidence** from a running kind cluster
  (`maops-k8s-day1`, context `kind-maops-k8s-day1`) that had the Day 1 manifests
  already deployed (Deployment `maops-app`, 2/2 pods Ready, 0 restarts, running for
  >2 hours at review time).

## Live cluster availability

A live kind cluster for this project **was running** at review time
(`kind-maops-k8s-day1`, node `maops-k8s-day1-control-plane`, `v1.36.1`), with the
Day 1 manifests already applied and both replicas healthy. This allowed the
container/pod baseline, image-user match, and read-only-root-filesystem claims to
be verified with real runtime evidence (live pod JSON, `kubectl exec`, and the
repository's own `scripts/cluster_check.py`) rather than manifest inference alone.
Every item below is marked manifest-verified and/or live-verified explicitly.

---

## Container / Pod security baseline

| Control | Manifest (`k8s/base/deployment.yaml`) | Live pod JSON / exec evidence | Status |
|---|---|---|---|
| `runAsNonRoot: true` | pod securityContext | `spec.securityContext.runAsNonRoot: true` | PASS |
| `runAsUser: 10001` | pod securityContext | `spec.securityContext.runAsUser: 10001`; live `os.getuid()` inside container = `10001` | PASS |
| `runAsGroup: 10001` | pod securityContext | `spec.securityContext.runAsGroup: 10001`; live `os.getgid()` = `10001`; `containerStatuses[0].user.linux = {uid:10001, gid:10001}` | PASS |
| `allowPrivilegeEscalation: false` | container securityContext | live container securityContext identical | PASS |
| `readOnlyRootFilesystem: true` | container securityContext | live container securityContext identical; **runtime write test performed** (see below) | PASS |
| `capabilities.drop: [ALL]` | container securityContext | live container securityContext identical | PASS |
| `seccompProfile.type: RuntimeDefault` | pod securityContext | live pod securityContext identical | PASS |
| `automountServiceAccountToken: false` | pod spec | live pod spec identical; live `spec.volumes` is empty (no `default-token`/projected SA token volume mounted) | PASS |

**Runtime read-only-root-filesystem proof (live, not just declared):** exec'd into
`maops-app-5d44cb8f6c-n8rzj` and attempted writes with the in-image Python
interpreter (no shell/`sh`/`id`/`whoami` exist in the distroless image, confirmed
by failed `kubectl exec -- sh`/`id` attempts, which is itself evidence of a
minimal image):

```
/testfile      WRITE FAILED: OSError(30, 'Read-only file system')
/app/testfile  WRITE FAILED: OSError(30, 'Read-only file system')
/tmp/testfile  WRITE FAILED: OSError(30, 'Read-only file system')
```

`/tmp` is also read-only (no `emptyDir` mounted there), which is consistent with
`app/server.py` never performing any file I/O — it only logs to stdout via
`print()`/`BaseHTTPRequestHandler.log_message`, keeps all mutable state in
process memory (`START_TIME`, `READY`), and serves purely from environment
variables sourced from the ConfigMap. There is no code path in `server.py` that
would require a writable filesystem, so `readOnlyRootFilesystem: true` is a
correct, load-bearing control rather than an aspirational one. The pods have been
Running with 0 restarts for the pod's full lifetime, and `/livez` and `/readyz`
have returned `200` continuously in the logs, confirming the application
functions correctly under this constraint.

`scripts/cluster_check.py` was also run directly against the live cluster
(read-only; no cluster was created/deleted/modified) and independently confirms
the same 15/15 real-cluster checks, including live UID/GID and live
securityContext fields, using the repository's own tooling.

**Conclusion:** the full container/pod security baseline is present, exact, and
proven live. No deviations found.

---

## Image security

Reviewed `app/Dockerfile` and cross-checked against `docker image inspect
maops-kubernetes-platform:0.1.0` and the live pod's `containerStatuses[0].user`.

- **Base image pinned by digest:** `FROM gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5`.
  Confirmed this digest resolves to a real, current manifest via `docker manifest
  inspect`. PASS.
- **No package manager, no `apt`/`pip` install at build time:** Dockerfile has
  exactly two `COPY`/instruction-bearing lines (`WORKDIR`, `COPY server.py`,
  `USER`, `EXPOSE`, `ENTRYPOINT`) and zero `RUN` instructions. Confirmed live: the
  container has no shell and no `id`/`whoami` binaries (`kubectl exec` failures
  above), consistent with a real distroless image with no package manager
  installed. PASS.
- **No third-party Python packages:** `app/server.py` imports only
  `json, os, socket, threading, time, http.server` — all stdlib. PASS.
- **`USER` matches pod securityContext UID:GID exactly:** Dockerfile `USER
  10001:10001`; `docker image inspect` confirms `"Config.User": "10001:10001"`;
  pod securityContext asserts `runAsUser: 10001` / `runAsGroup: 10001`; live
  process UID/GID = `10001:10001`. All four sources agree. PASS.
- **Listens on 8080:** `EXPOSE 8080`; `server.py` reads `PORT` env (default
  `8080`); container port and Service port/targetPort are all `8080`/`http`.
  PASS.
- **No embedded secrets:** grepped the Dockerfile, `server.py`, and all rendered
  manifests for credential/token/password/key-like strings; none found beyond
  the deliberate `security`/`token` substrings inside the validator's own
  `_looks_secret()` implementation and test names (not actual secret material).
  PASS.
- **No floating `latest` tag anywhere:** the only occurrence of the string
  `latest` in the repository is inside
  `tests/test_validate_manifests.py::test_wrong_image_fails`, which exists
  specifically to prove the validator *rejects* a `:latest` image — not a real
  usage. PASS.
- **`imagePullPolicy: IfNotPresent`:** correct given the `make image-load`
  workflow (`kind load docker-image`) loads the image directly into the kind
  node's containerd store with no registry involved; `Always` would force (and
  fail) a registry pull, `Never` would also work but `IfNotPresent` is the
  standard/idiomatic choice for this workflow and matches what's actually
  running (`docker.io/library/maops-kubernetes-platform:0.1.0` resolved locally
  in the live pod's `containerStatuses[0].imageID`). PASS.

**Conclusion:** image security posture is correct and internally consistent
across Dockerfile, built image metadata, rendered manifest, and live runtime.

---

## Kubernetes security boundaries

Checked both the rendered manifest (`kubectl kustomize k8s/base`) and the full
live pod JSON (`kubectl get pod ... -o json`) for the full pod spec, not just the
fields the task named:

- `hostNetwork` — absent (not present in `pod.spec`). PASS.
- `hostPID` — absent. PASS.
- `hostIPC` — absent. PASS.
- `hostPort` — absent on the container's only port (`containerPort: 8080`, no
  `hostPort` key). PASS.
- `privileged` container — absent from container securityContext (and
  structurally impossible alongside `runAsNonRoot: true` / `capabilities.drop:
  [ALL]`). PASS.
- `hostPath` volumes — no `volumes` block at all in either the rendered manifest
  or the live pod spec (`spec.volumes` is empty/absent). PASS.
- Docker socket mount — none; no volumes of any kind exist. PASS.
- Unnecessary projected ServiceAccount token — `automountServiceAccountToken:
  false` at the pod level, and live `spec.volumes` confirms no
  `kube-api-access-*`/projected token volume was mounted despite the pod using
  the `default` ServiceAccount (Kubernetes correctly honors the pod-level
  override). PASS.
- No `Secret` objects — `kubectl kustomize k8s/base` renders exactly 4 documents:
  `Namespace`, `ConfigMap`, `Service`, `Deployment`. No `Secret` present, as
  expected for Day 1. PASS.
- `ConfigMap` (`maops-app-config`) data contains only `APP_NAME`,
  `APP_ENVIRONMENT`, `APP_MESSAGE`, `APP_LOG_LEVEL` — all plain, non-sensitive
  descriptive strings. No credential/token/key/password-shaped keys or values.
  PASS.

**Conclusion:** no host-namespace escapes, no privileged escalation paths, no
stray volumes, and no Day 1 Secret/credential material anywhere in the rendered
output or the live cluster.

---

## Day 1 scope discipline

Per `docs/roadmap.md`, Day 1 (`v0.1.0`) explicitly excludes Secrets, RBAC,
ServiceAccount, NetworkPolicy, PVC/StatefulSet, Helm, CI, Ingress, and
NodePort/LoadBalancer — all deferred to Days 2–6, with RBAC/NetworkPolicy/
ServiceAccount specifically slated for Day 5 (`v0.5.0`).

Confirmed absent from the rendered manifest and the live cluster's
`maops-platform` namespace: `Secret`, `ServiceAccount` (a custom one — the pod
uses the namespace's implicit `default` SA with no token mounted, which is not
itself a Day 5 object), `Role`, `RoleBinding`, `ClusterRole`,
`ClusterRoleBinding`, `NetworkPolicy`, `PersistentVolumeClaim`, `StatefulSet`,
`Ingress`. This is correct and matches the roadmap — none of these are Day 1
defects.

**Forward-compatibility check (informational only, not a Day 1 blocker):**
labeling is already Day-5-friendly. Both the Deployment's pod template labels
(`app.kubernetes.io/name`, `app.kubernetes.io/instance`, `app.kubernetes.io/
component: app`) and the Service selector are consistent and specific enough to
be used directly as `NetworkPolicy` `podSelector` match labels and as `Role`/
`RoleBinding` subjects once Day 5 introduces a purpose-built ServiceAccount. No
restructuring found that would complicate that later work.

---

## Static security validation (`scripts/validate_manifests.py`, `scripts/k8s_yaml.py`, `tests/`)

Read the full validator and its test suite, then executed both:

```
python3 scripts/manifest_check.py k8s/base   -> 30/30 checks PASS (against the real kustomize render)
python3 -m unittest discover -s tests         -> 31 passed
```

- `scripts/k8s_yaml.py` is a small, purpose-built YAML-subset parser (documented
  as such) and is exercised against the actual `kubectl kustomize` output in
  `tests/test_k8s_yaml.py::test_real_rendered_manifest_parses_into_four_documents`
  — not just hand-built fixtures. No issues found.
- `scripts/validate_manifests.py::run_checks` checks each security field against
  a real expected constant (e.g. `pod_sc.get("runAsUser") == 10001`,
  `container_sc.get("capabilities") or {}).get("drop") == ["ALL"]`,
  `pod_spec.get("automountServiceAccountToken") is False`) — these compare
  parsed manifest data against independently-defined expected values, not a
  constant against itself. **No tautological checks were found.**
- `tests/test_validate_manifests.py` follows a disciplined pattern: every
  negative test deep-copies a known-good fixture, mutates exactly one field
  (e.g. `runAsNonRoot = False`, `automountServiceAccountToken = True`,
  `readOnlyRootFilesystem = False`, `capabilities = {"drop": ["NET_RAW"]}`,
  `hostNetwork = True`, appends a `Secret`/`Ingress`/`PersistentVolumeClaim`
  document), and asserts that specifically the corresponding named check fails.
  `test_baseline_passes_every_check` also asserts the *positive* fixture passes
  every check with `assertGreaterEqual(len(findings), 25)`, guarding against a
  check silently being dropped. **No tautological or self-comparing tests were
  found** — I verified this by actually mutating the fixtures.

### Finding: validator does not check `hostPID`, `hostIPC`, `hostPath` volumes, or `privileged`

`run_checks()` explicitly checks `hostNetwork` and container `hostPort`, but has
no check for `pod_spec.get("hostPID")`, `pod_spec.get("hostIPC")`,
`pod_spec.get("volumes")` (for `hostPath` type), or
`container_sc.get("privileged")`. The current manifest is clean on all of these
(verified manually above), so there is **no live vulnerability today**, but a
future regression introducing any of these fields would render cleanly and pass
`make manifest-check` undetected. See finding DAY1-SEC-M1 below.

---

## Findings

### DAY1-SEC-M1
**Severity:** Medium
**Title:** Static validator does not check for `hostPID`, `hostIPC`, `hostPath` volumes, or `privileged: true`
**Evidence:** `scripts/validate_manifests.py::run_checks()` contains explicit checks named `security.no_host_network` and `security.no_host_port`, but no equivalent checks exist for `pod_spec.get("hostPID")`, `pod_spec.get("hostIPC")`, any `hostPath`-typed entry in `pod_spec.get("volumes")`, or `container_sc.get("privileged")`. Confirmed by reading the full function body (lines 1–274) and cross-referencing against `tests/test_validate_manifests.py`, which likewise has no negative test for these fields (only `test_host_network_true_fails` and `test_host_port_set_fails` exist for the host-boundary category).
**Impact:** The manifest is currently clean on all four of these fields (verified directly against both the rendered manifest and live pod JSON), so there is no exploitable issue in v0.1.0 today. However, `make manifest-check`/`scripts/manifest_check.py` is the CI-equivalent gate this project relies on for regression detection, and a future PR that accidentally sets `hostPID: true`, `hostIPC: true`, adds a `hostPath` volume, or sets `privileged: true` on the container would render, apply, and pass all 30 static checks without being caught.
**Required remediation:** Add explicit checks to `run_checks()` (and corresponding negative tests) asserting `not pod_spec.get("hostPID")`, `not pod_spec.get("hostIPC")`, `container_sc.get("privileged") is not True`, and that no volume in `pod_spec.get("volumes", [])` has a `hostPath` key.
**Release-blocking:** NO — the underlying manifest is already clean; this is a coverage gap in regression detection, not a present vulnerability.

---

## Verdict

**APPROVE**

### Severity counts
- Critical: 0
- High: 0
- Medium: 1 (DAY1-SEC-M1)
- Low: 0
- Informational: 0 (the labeling/forward-compatibility note above is included as commentary, not a numbered finding, since it identifies no defect)

### Required answers

**1. Is the Day 1 Pod/workload security baseline credible for v0.1.0?**
Yes. Every required control (`runAsNonRoot: true`, `runAsUser/runAsGroup:
10001`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`,
`automountServiceAccountToken: false`) is present, exact, and — unusually for a
Day 1 review — independently proven with live runtime evidence: real
`os.getuid()`/`os.getgid()` output of `10001 10001` from inside the running
container, a live pod JSON `containerStatuses[0].user.linux` block confirming
the same, three live write attempts against the root filesystem and `/tmp` all
failing with `Read-only file system`, and confirmation that `app/server.py`
performs no file I/O that would require a writable filesystem. The
Dockerfile's `USER 10001:10001` matches the asserted pod securityContext
exactly, and the base image is genuinely minimal (digest-pinned distroless,
confirmed to have no shell or package manager via failed `kubectl exec`
attempts). This baseline is credible, not just declared.

**2. Are any intentionally deferred Day 5+ controls (ServiceAccount, RBAC,
NetworkPolicy, Secrets) incorrectly being treated as already present/implemented
today?**
No. None of these objects exist in the rendered manifest or the live cluster's
`maops-platform` namespace. The pod uses the namespace's implicit `default`
ServiceAccount but with its token explicitly unmounted
(`automountServiceAccountToken: false`), which is the correct Day 1 posture per
`docs/roadmap.md` (Day 5 is where a purpose-built ServiceAccount and scoped
`automountServiceAccountToken: true` are introduced). No RBAC objects, no
NetworkPolicy, and no Secret objects are present or referenced anywhere in
`k8s/base`.

PROJECT 4 DAY 1 KUBERNETES SECURITY REVIEW COMPLETE

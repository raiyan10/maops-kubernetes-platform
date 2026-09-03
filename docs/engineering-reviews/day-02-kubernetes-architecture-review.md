# Day 2 (v0.2.0) Kubernetes Architecture Review — Independent Assessment

**Reviewer role:** `kubernetes-architect` (manifest/design coherence, stage
discipline, service-discovery correctness — not security or test-coverage
adjudication).

**Scope reviewed:** current uncommitted working tree on branch
`feature/day-2-service-discovery-secrets` (not just committed history),
plus the live `kind-maops-k8s-day2` cluster where it was up and reachable.

**Independence note:** this review was produced without reading any other
Day 2 review document. Day 1 historical review documents were consulted
only to verify carried-forward decisions DAY1-REL-I1 and DAY1-INT-I2.

---

## 1. Method

- `git status` / `git diff --stat` against the working tree to enumerate
  every changed/added/deleted path.
- Read every file under `k8s/base/`, `gateway/`, `app/`, `kind/cluster.yaml`,
  `VERSION`, `Makefile`, and the new `scripts/*.py` modules
  (`version_check.py`, `secret_bootstrap.py`, `secret_check.py`,
  `endpointslice.py`, `discovery_check.py`, `dependency_check.py`,
  `cluster_check.py`, `kube.py`).
- Rendered the base with `kubectl kustomize k8s/base` (not standalone
  kustomize) and confirmed exactly 7 documents render: 1 Namespace, 2
  ConfigMap, 2 Service, 2 Deployment. No Secret is rendered.
- Ran `python3 -m unittest discover -s tests` (163 tests, `OK`).
- Ran `python3 scripts/version_check.py k8s/base` (13/13 checks pass).
- Ran `python3 scripts/manifest_check.py k8s/base` (94/94 checks pass,
  including explicit selector-collision checks in both directions).
- Live cluster `kind-maops-k8s-day2` was up and reachable. Ran, read-only
  or self-restoring: `kubectl get nodes/ns/deploy/svc/pods/endpointslices/
  secrets`, `scripts/cluster_check.py` (33/33), `scripts/discovery_check.py`
  (2/2), `scripts/secret_check.py` (27/27), `scripts/smoke.py` (5/5).
- Deliberately **did not** run `scripts/dependency_check.py` live, since it
  scales `maops-app` to 0 and back — a cluster mutation — even though it is
  self-restoring; the task instructions prohibit mutating the live cluster
  during this review. Its correctness was instead verified by static
  source review (see §6/§7 below).

---

## 2. What's right

- **Multi-service design is genuine.** `maops-gateway` and `maops-app` are
  independent `Deployment`s (`k8s/base/gateway-deployment.yaml`,
  `k8s/base/app-deployment.yaml`), each with its own `ClusterIP` `Service`
  (`gateway-service.yaml`, `app-service.yaml`), each at `replicas: 2`.
  Live cluster confirms both at `2/2` Ready, both `Service` type
  `ClusterIP`, and one `EndpointSlice` per Service each with exactly 2
  `ready: true` addresses (`maops-app-dl2t6`, `maops-gateway-6p6pt`).
- **Selectors don't collide.** `gateway-service.yaml:15-18` and
  `app-service.yaml:15-18` both select on
  `name`+`instance`+`component`, and `component` differs
  (`gateway` vs `app`). `manifest_check.py`'s
  `service.no_selector_collision_gateway_selects_app` /
  `..._app_selects_gateway` checks pass against the rendered pod template
  labels, and this was independently confirmed by rendering with
  `kubectl kustomize k8s/base` and hand-checking both selector/label sets.
- **Service discovery is real, not simulated.** `gateway/server.py:32-37`
  reaches the backend only via `BACKEND_HOST` (`maops-app`, from
  `gateway-configmap.yaml:17`) resolved fresh on every request through
  `urllib.request` — no IP caching, no one-time resolve. Live proof:
  `scripts/discovery_check.py` performed a real `socket.getaddrinfo()`
  call from inside a live gateway pod (via `kubectl exec` into the pinned
  interpreter, since the distroless image has no shell/dig/nslookup) and
  a real end-to-end `/backend` call through the Service, returning genuine
  `maops-app` pod data (`backend_hostname: maops-app-...`). No hardcoded
  ClusterIP, Pod IP, or Pod name is used anywhere in configuration
  (`grep` for `BACKEND_HOST` and `10\.96\.`/`10\.244\.` across
  `k8s/base/*.yaml` turns up only the DNS name).
- **EndpointSlice, not deprecated Endpoints.** `scripts/endpointslice.py`
  and its only caller, `scripts/cluster_check.py:122-135`, exclusively use
  `discovery.k8s.io/v1` `EndpointSlice` (`kubectl get endpointslices -l
  kubernetes.io/service-name=...`) and count only addresses with
  `conditions.ready == true`. No script in the diff references the legacy
  `v1 Endpoints` API. Live: both Services show exactly 2 ready endpoints.
- **Dependency direction is one-way.** `app-configmap.yaml` has no
  `BACKEND_*`/gateway-referencing key; `app/server.py` never calls out to
  the gateway. Only `gateway-configmap.yaml` carries `BACKEND_HOST`/
  `BACKEND_PORT`/`BACKEND_TIMEOUT_SECONDS`. No circular dependency exists.
- **Health/readiness semantics are correctly asymmetric.**
  `app/server.py` and `gateway/server.py` both expose `/livez` as pure
  local-process health (constant `{"status": "alive"}`, never touching the
  network). Gateway's `/readyz` (`gateway/server.py:122-134`) is the only
  place that performs a bounded (`BACKEND_TIMEOUT_SECONDS=3s`) HTTP check
  against `maops-app`'s own `/readyz`; a backend outage yields `503` from
  `/readyz` while `/livez` stays `200`. This was proven correct by
  static/code review; the live `dependency_check.py` proof itself was not
  re-run in this session (see §1) but its design is sound: it asserts
  gateway `/livez` stays 200, `/readyz`/`/backend` return 503, and gateway
  container restart counts are unchanged while `maops-app` is scaled to 0,
  then unconditionally restores `maops-app` to 2 replicas in a `finally`
  path with prominent separate reporting if restoration itself fails.
  `startupProbe`/`livenessProbe` on both workloads point at `/livez` only
  — the dependency-aware check is confined to `readinessProbe`, so a
  backend outage can never cause kubelet to kill/restart the gateway
  container, and there is no possibility of a circular liveness/readiness
  deadlock since `maops-app` has no dependency on `maops-gateway` at all.
- **Timeout model is bounded and safe.**
  `BACKEND_TIMEOUT_SECONDS=3` (`gateway-configmap.yaml:19`) is passed as
  `timeout=` to every `urllib.request.urlopen()` call
  (`gateway/server.py:82`); network failures/timeouts are caught
  (`urllib.error.URLError, TimeoutError, OSError, ValueError`) and
  translated into controlled `503` JSON responses, never a stack trace or
  crash. The gateway `readinessProbe.timeoutSeconds` (5s,
  `gateway-deployment.yaml:95`) is deliberately kept above
  `BACKEND_TIMEOUT_SECONDS` so the probe itself can't time out before the
  app-level bounded check completes — documented explicitly in a comment
  and correct in design.
- **Configuration separation is clean.** `maops-gateway-config` carries
  only non-sensitive routing/timeout config; `maops-app-config` carries
  only non-sensitive app identity config. Both are wired via `envFrom`
  (`gateway-deployment.yaml:46-48`, `app-deployment.yaml:46-48`) and
  actually consumed (`os.environ.get(...)` in both `server.py` files,
  live-verified by `cluster_check.py`'s `check_configmap_consumption`
  against a running pod's actual process environment, not just the
  manifest).
- **Secret architecture is sound and confined to Day 2's stated scope.**
  No `Secret` object is committed or rendered by `k8s/base`
  (`grep -n "^kind:"` on the rendered output confirms no `Secret`
  document; `manifest_check.py`'s `FORBIDDEN_KINDS` set also asserts
  this). `scripts/secret_bootstrap.py` creates `maops-internal-auth`
  out-of-band with a `secrets.token_urlsafe(32)` value written only to a
  0600 temp file (never a CLI argument, never printed), preserves an
  existing Secret rather than silently rotating it, and validates
  shape/non-emptiness without ever exposing the value. Both Deployments
  mount the same Secret read-only at `/var/run/secrets/maops`
  (`defaultMode: 288` = `0440`, matched to `fsGroup: 10001`); `/config` in
  both `server.py` files is explicitly restricted to `APP_`/`BACKEND_`
  environment-variable-sourced keys only, so it structurally cannot leak
  the token. The token serves a real inter-service-auth purpose: the
  gateway attaches it as `X-MAOPS-Internal-Token` when calling the app's
  `/internal/info`, and the app rejects any request with a missing/wrong
  token via `hmac.compare_digest` (`app/server.py:65-68`), returning
  exactly `403` in both cases with no token echoed back. Live
  `secret_check.py` independently proved: correct-token → `200`;
  no-token/wrong-token → `403`; token absent from all normal endpoint
  responses, `/config`, pod logs, and every tracked repository file.
- **Resource/security continuity is intact.** Both Deployments carry
  identical `requests {cpu: 50m, memory: 32Mi}` / `limits {cpu: 250m,
  memory: 128Mi}` (matching `EXPECTED_REQUESTS`/`EXPECTED_LIMITS` in
  `scripts/validate_manifests.py:38-39`), and the full Day 1 security
  baseline (`runAsNonRoot`, `runAsUser/Group: 10001`, `fsGroup: 10001`,
  `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`,
  `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`,
  `automountServiceAccountToken: false`) is unchanged and present on both
  workloads — confirmed both statically and live via
  `cluster_check.py`'s runtime UID/GID and securityContext checks against
  actually-running pods. Adding the Secret volume mount did not require
  weakening any of these (the mount is itself read-only).
- **Kustomize structure remains a coherent single-environment base.**
  `k8s/base/kustomization.yaml` lists exactly the 7 new/kept resource
  files (`namespace.yaml`, `gateway-configmap.yaml`, `app-configmap.yaml`,
  `gateway-deployment.yaml`, `app-deployment.yaml`,
  `gateway-service.yaml`, `app-service.yaml`) and renders cleanly via
  plain `kubectl kustomize k8s/base` with no standalone kustomize binary.
  The Day 1 generic files (`configmap.yaml`, `deployment.yaml`,
  `service.yaml`) are actually deleted from the working tree (`git status`
  shows `D`), not left alongside the new `app-*`/`gateway-*` files as
  ambiguous duplicate ownership — verified by `ls k8s/base/` showing only
  the new files exist on disk. No overlays, no Helm chart files
  (`find . -iname Chart.yaml` returns nothing) anywhere in the repo.
- **DAY1-INT-I2 correctly remains unchanged/open, not falsely closed.**
  Both `app/Dockerfile` and the new `gateway/Dockerfile` pin the identical
  digest
  (`gcr.io/distroless/python3-debian12@sha256:7d1042ce...c89c5`) that Day
  1 used (`git show HEAD:app/Dockerfile` confirms byte-identical digest),
  and every script that execs into a pod
  (`cluster_check.py`, `discovery_check.py`, `secret_check.py`) still
  hardcodes `/usr/bin/python3.11` as the in-container interpreter path,
  consistent with the accepted Day 1 debt item. Nothing in Day 2 papers
  over or silently "closes" this item; the Dockerfiles even carry an
  explicit comment cross-referencing DAY1-INT-I2 as still open.
- **Scope discipline holds.** No HPA, PDB, rollout-strategy tuning,
  scheduling policy, StatefulSet, PVC, custom ServiceAccount, RBAC,
  NetworkPolicy, Helm, Ingress, GitHub Actions workflow, observability
  stack, Argo CD, Terraform, or Ansible artifact was found anywhere in the
  diff or the rendered manifests (`grep` across `k8s/`, `gateway/`,
  `app/` for these kinds/keywords returns nothing; `FORBIDDEN_KINDS` in
  `validate_manifests.py` also statically enforces the K8s-object subset
  of this and passes).

## 3. Live cluster evidence obtained

| Check | Result |
|---|---|
| Node Ready | `maops-k8s-day2-control-plane` Ready, `v1.36.1` |
| Server version | `v1.36.1` (matches `kind/cluster.yaml`'s pinned `kindest/node:v1.36.1`) |
| `maops-gateway` Deployment | 2/2 ready |
| `maops-app` Deployment | 2/2 ready |
| Both Services | `ClusterIP` |
| `maops-gateway` EndpointSlice | 2 ready endpoints (`maops-gateway-6p6pt`) |
| `maops-app` EndpointSlice | 2 ready endpoints (`maops-app-dl2t6`) |
| `maops-internal-auth` Secret | exists, `Opaque`, 1 data key |
| Real DNS resolution (in-pod `getaddrinfo`) | resolved `maops-app` successfully |
| Real gateway→app HTTP via Service | `/backend` returned genuine `maops-app` pod data |
| Secret non-disclosure (auth/log/repo scan) | 27/27 checks pass |
| Full smoke suite via port-forward | 5/5 checks pass |

All items in the task's "at minimum" live-evidence list were independently
confirmed. `scripts/dependency_check.py`'s live scale-down/restore
experiment was intentionally not re-executed in this review session (see
§1) to honor the "do not mutate the cluster" constraint; it was instead
verified by static source review only.

---

## 4. Findings

No critical, high, medium, or low findings were identified. Everything
inspected — source, rendered manifests, and live cluster state — matched
the Day 2 architecture contract and the project's own stated ground
rules. Two informational notes are recorded below purely for completeness
and are not release-blocking.

ID: DAY2-ARCH-I1
Severity: Informational
Title: `dependency_check.py`'s live scale-to-zero/restore experiment was not re-executed during this independent review
Evidence: The reviewer's own scope constraints for this session prohibited cluster mutation; `scripts/dependency_check.py` scales `maops-app` to 0 replicas and restores it in a `finally` block, which is a real (self-healing) mutation of live cluster state
Impact: The dependency-failure behavior it proves (gateway `/livez` stays 200, `/readyz`/`/backend` return 503, restart counts unchanged, guaranteed restoration) was verified by static code review only in this pass, not by fresh live execution
Required remediation: None for this review; the release-engineer/cluster-integration-engineer track for Day 2 should ensure `make day2-check` (which does include `dependency-check`) is run at least once end-to-end before tagging `v0.2.0`
Release-blocking: NO

ID: DAY2-ARCH-I2
Severity: Informational
Title: Gateway readiness gate (`STARTUP_DELAY_SECONDS`) and backend readiness are two independent 3-second timers with no explicit coordination
Evidence: Both `app/server.py:22` and `gateway/server.py:30` default `STARTUP_DELAY_SECONDS=3`; the gateway's own `READY` flag flips independently of whether `maops-app` is itself ready yet, so `gateway /readyz` can legitimately return `503` for a few cycles right after a simultaneous cold start of both Deployments, self-resolving once `maops-app` becomes ready
Impact: None observed live (both Deployments reached 2/2 well within probe failure thresholds); this is expected, correctly-bounded readinessProbe behavior (`periodSeconds: 5`, `failureThreshold: 3`) rather than a defect, and does not risk a restart loop since only `/livez` gates liveness
Required remediation: None; noting only as an architectural observation for a reviewer following the readiness chain end to end
Release-blocking: NO

---

## 5. Explicit answers

**Is the Day 2 multi-service architecture suitable as the foundation for
Day 3?** Yes. The two-Deployment/two-Service topology, stable
DNS-based discovery, EndpointSlice-based readiness evidence, and clean
Kustomize base give Day 3 (scaling, rollout, rollback, scheduling, PDB) a
correct and unencumbered starting point — nothing in Day 2's design would
need to be unwound or reworked to add HPA/PDB/rollout-strategy changes on
top of these exact two Deployments.

**Has DAY1-REL-I1 genuinely been closed?** Yes. `VERSION` is read exactly
once by the Makefile (`VERSION := $(shell cat VERSION)`) and both
`GATEWAY_IMAGE`/`APP_IMAGE` derive from it; `scripts/version_check.py`
independently re-renders `k8s/base` via `kubectl kustomize` and asserts
`VERSION` itself equals the Day 2 target, both Deployment image tags
equal `<repo>:<VERSION>`, and every rendered `app.kubernetes.io/version`
label (9 locations, including both Deployments' pod-template labels)
equals `VERSION` — failing loudly on drift. `tests/test_version_check.py`
covers negative cases (version-file drift, per-image tag drift, missing
Deployment, metadata-label drift, pod-template-label drift, namespace
label drift), and the live run against the current tree passes 13/13.
This is a genuine, tested closure, not a cosmetic one.

**Has Day 2 remained inside its intended scope?** Yes. The rendered base
and the full repository diff contain no HPA, PDB, rollout-strategy
tuning, scheduling policy, StatefulSet, PVC, custom ServiceAccount, RBAC,
NetworkPolicy, Helm, Ingress, CI workflow, observability stack, Argo CD,
Terraform, or Ansible artifacts. DAY1-INT-I2 was correctly left open
(not falsely closed) since the distroless base image and its pinned
digest/interpreter path are unchanged. The only new capabilities
introduced — a second workload, two ConfigMaps, EndpointSlice-based
discovery evidence, and a runtime Secret — are exactly what
`docs/roadmap.md`'s Day 2 entry calls for.

---

## 6. Verdict

**Severity counts:** Critical: 0, High: 0, Medium: 0, Low: 0,
Informational: 2

**Final verdict: APPROVE**

The Day 2 multi-service architecture is coherent, correctly scoped, and
independently verified against both static analysis and live cluster
behavior. No manifest changes are required before this stage proceeds to
release-readiness review.

PROJECT 4 DAY 2 KUBERNETES ARCHITECTURE REVIEW COMPLETE

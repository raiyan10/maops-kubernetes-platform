# Project 4 / Day 1 Kubernetes Architecture Review

- **Reviewer role:** `kubernetes-architect` (independent pass)
- **Repository:** `maops-kubernetes-platform`
- **Branch reviewed:** `feature/day-1-kubernetes-foundation` (current uncommitted working tree)
- **Target version:** v0.1.0
- **Review date:** 2026-09-01
- **Scope authority:** `docs/roadmap.md` (Day 1 / v0.1.0), `.claude/CLAUDE.md`

This review inspected source manifests, the `kubectl kustomize` render,
`kind/cluster.yaml`, the application source, the Makefile, the
dependency-free validation scripts, and a live `maops-k8s-day1` kind
cluster already running in this environment. All claims below were
verified against live evidence, not asserted from source alone.

---

## 1. Architecture verification

| Check | Expected | Observed | Result |
|---|---|---|---|
| kind cluster name | `maops-k8s-day1` | `kind get clusters` -> `maops-k8s-day1` | PASS |
| Control-plane node count | exactly 1 | `kubectl get nodes` -> 1 node, role `control-plane` only | PASS |
| Node image pin | `kindest/node:v1.36.1@sha256:3489c76...` | `kind/cluster.yaml:11` and `docker inspect maops-k8s-day1-control-plane` both show the identical digest; live server version `v1.36.1` | PASS |
| Namespace | `maops-platform` | `k8s/base/namespace.yaml`, rendered output, and live `kubectl get ns` all agree | PASS |
| ConfigMap external to image | yes | `maops-app-config` wired via `envFrom`, not baked into `app/Dockerfile` | PASS |
| One Deployment `maops-app` | yes | `k8s/base/deployment.yaml`; live `kubectl get deploy` shows exactly one, `2/2` ready | PASS |
| Replicas | 2 | `spec.replicas: 2` in source and render; live Deployment `2/2` | PASS |
| Deployment -> ReplicaSet -> Pod ownership | yes, only chain | Live `ownerReferences`: Pod -> ReplicaSet `maops-app-5d44cb8f6c` (controller:true) -> Deployment `maops-app` (controller:true) | PASS |
| One ClusterIP Service `maops-app` | yes | `k8s/base/service.yaml`; live `kubectl get svc` -> `ClusterIP`, `8080/TCP` | PASS |
| Service selector matches pod labels | yes | Rendered Service selector `{name, instance}` is a subset of rendered Pod template labels; live `Endpoints` shows both Pod IPs populated (`10.244.0.7:8080,10.244.0.8:8080`) | PASS |
| No direct Pod-IP dependency | yes | Service/DNS is the only addressing path in manifests and scripts (`scripts/portforward.py` forwards to `service/maops-app`, not a Pod) | PASS |
| No NodePort / LoadBalancer / Ingress | yes | Not present in any manifest; `scripts/validate_manifests.py` asserts `service.no_node_port` and `scope.no_forbidden_resources` (Ingress excluded) programmatically, and both pass | PASS |
| No hostPort / hostNetwork | yes | Absent from `deployment.yaml`; `validate_manifests.py` asserts both explicitly, both pass | PASS |
| No worker-node complexity | yes | `kind/cluster.yaml` defines a single `control-plane` node only | PASS |
| port-forward is temporary/dev-only | yes | `scripts/portforward.py` picks an ephemeral free port, waits for connectability, runs the `kubectl` child in its own process group, and guarantees `SIGTERM`->`SIGKILL` cleanup in a `finally` block; live manual port-forward used for this review was likewise foreground and killed immediately after use | PASS |

Live evidence snapshot used for the above:

```
kubectl get nodes                 -> maops-k8s-day1-control-plane  Ready  control-plane  v1.36.1
kubectl -n maops-platform get deploy,rs,pods,svc,ep
  deployment.apps/maops-app       2/2  2  2
  replicaset.apps/maops-app-5d44cb8f6c   2  2  2
  pod/maops-app-5d44cb8f6c-n8rzj  1/1 Running
  pod/maops-app-5d44cb8f6c-xlqgg  1/1 Running
  service/maops-app               ClusterIP  10.96.5.10  8080/TCP
  endpoints/maops-app             10.244.0.7:8080,10.244.0.8:8080
```

## 2. Label and selector hygiene

Rendered via `kubectl kustomize k8s/base` (not eyeballed from source):

- Namespace, ConfigMap, Deployment (`metadata.labels` and
  `spec.template.metadata.labels`), and Service all carry the full
  recommended `app.kubernetes.io/{name,instance,version,part-of,managed-by}`
  set, plus `component` on the namespaced objects. Consistent across all
  four kinds.
- `Deployment.spec.selector.matchLabels` and `Service.spec.selector` are
  both restricted to `{app.kubernetes.io/name, app.kubernetes.io/instance}`
  only - stable identity labels, deliberately excluding `version` (which
  would break the selector on every version bump) and `component`/`managed-by`
  (redundant given there's only one component). This is correct,
  idiomatic Kubernetes practice, not merely "the labels happen to line up."
- Confirmed the selector actually matches the rendered pod template
  labels (not just the intent) via the live `Endpoints` object, which
  shows both running Pod IPs as ready backends - if the selector were
  wrong or the labels drifted, this list would be empty.

## 3. Ownership chain

Verified live, not inferred: `pod.metadata.ownerReferences` points to
the ReplicaSet with `controller: true`, and the ReplicaSet's
`ownerReferences` points to the Deployment with `controller: true`. No
StatefulSet, DaemonSet, Job, or bare Pod exists anywhere in `k8s/base`.
This is exactly the Day 1-scoped chain and nothing more.

## 4. Service exposure

`k8s/base/service.yaml:14` sets `type: ClusterIP` explicitly (not
relying on the default). No `nodePort` field on any port, no
`hostNetwork`/`hostPort` anywhere in the pod spec. `docs/architecture.md`
correctly documents *why* port-forward is used instead of NodePort/
Ingress at this stage, which matches the roadmap's explicit Day 1
boundary.

## 5. Probe design

`k8s/base/deployment.yaml:60-82`:

- **startupProbe** -> `GET /livez`, `periodSeconds: 2`,
  `failureThreshold: 15` (~30s budget), `timeoutSeconds: 1`. While this
  is running, liveness/readiness are not evaluated - correct sequencing.
- **livenessProbe** -> `GET /livez`, `periodSeconds: 10`,
  `timeoutSeconds: 2`, `failureThreshold: 3` (~30s to declare dead).
  Checks only "is the HTTP server answering at all," independent of
  application-level readiness state.
- **readinessProbe** -> `GET /readyz`, `periodSeconds: 5`,
  `timeoutSeconds: 2`, `failureThreshold: 3`, `initialDelaySeconds: 0`.

These are not three duplicated checks: `app/server.py:64-70` makes
`/livez` return `200` unconditionally as soon as the HTTP server is
bound, while `/readyz` deliberately holds `503` for
`STARTUP_DELAY_SECONDS` (3s default) even after `/livez` is already
green. This gives a real, observable alive-vs-ready distinction rather
than two endpoints that always agree. Verified live: both `/livez` and
`/readyz` return success on a long-running Pod, and the code path
guarantees a window where they'd disagree on a fresh Pod.

Timing assessment for a tiny local workload: `initialDelaySeconds: 0`
on liveness/readiness is safe specifically because the startupProbe
gates them - the effective delay before either is evaluated is bounded
by however quickly `/livez` first succeeds (the process binds almost
instantly), not by a hardcoded number. The ~30s startup budget and ~30s
liveness-failure budget are conservative enough that normal startup of
this stdlib server never flaps, while still being short enough to
matter within a review session.

## 6. Resources

`k8s/base/deployment.yaml:53-59`, confirmed both in the rendered output
and by `scripts/validate_manifests.py::EXPECTED_REQUESTS` /
`EXPECTED_LIMITS`:

```
requests: cpu: 50m   memory: 32Mi
limits:   cpu: 250m  memory: 128Mi
```

Matches the spec exactly (`python3 scripts/manifest_check.py k8s/base`
-> `30/30 checks passed`, including `resources.requests` and
`resources.limits`). The 5x CPU headroom and 4x memory headroom between
request and limit is a credible, teachable Day 1 example of the
scheduler-time (request) vs. runtime-enforcement (limit) distinction
for a workload with no real traffic behind it - not an arbitrary or
copy-pasted number.

## 7. Kustomize structure

`k8s/base/kustomization.yaml` lists exactly the four Day 1 resources
(namespace, configmap, deployment, service) with no `bases`,
`components`, `patches`, `configMapGenerator`, or overlay directories.
`kubectl kustomize k8s/base` renders cleanly with zero errors or
warnings, using only the Kustomize built into `kubectl` (confirmed
`Kustomize Version: v5.8.1` reported by `kubectl version`, no standalone
`kustomize` binary invoked anywhere in `Makefile` or `scripts/`). No
overlay structure exists yet, correctly deferred - Day 1's roadmap entry
doesn't call for one, and there's exactly one environment to render.

## 8. kind / local-cluster boundary

`kind/cluster.yaml` is scoped to a single pinned `control-plane` node
with no registry mirror config, no extra port mappings, and no CNI
override - it targets nothing but a reproducible local API server.
`Makefile:image-load` uses `kind load docker-image`, and the Deployment
image reference (`maops-kubernetes-platform:0.1.0`, no registry host,
`imagePullPolicy: IfNotPresent`) is consistent with that: nothing in
Day 1 assumes an image registry, a cloud load balancer, or any
provider-specific annotation. The real controller-reconciliation proof
(`scripts/reconcile_check.py`, deleting one Pod and waiting for the
ReplicaSet controller to replace it) is exactly the kind of thing kind
is well-suited for and a mocked/static check could not prove.

## 9. Scope control

Everything the roadmap defers for Day 1 (Secrets, ServiceAccount, RBAC,
NetworkPolicy, PVC/StatefulSet, Helm, Ingress, CI, cloud provisioning)
is absent - both by manual inspection of `k8s/base/` and by the
programmatic `FORBIDDEN_KINDS` check in
`scripts/validate_manifests.py:24-35`, which passed
(`scope.no_forbidden_resources`). Nothing that Day 1 requires is
missing either - Namespace, ConfigMap, Deployment, Service are all
present and each carries exactly the fields Day 1 needs (no
speculative fields like `revisionHistoryLimit` or a rollout `strategy`,
which are Day 3's concern). No forward-pulled or left-behind scope
found.

## 10. Makefile boundary

`Makefile` is the single authoritative interface: every meaningful step
(`tool-check`, `test`, `manifest-check`, `image-build`, `cluster-create`,
`image-load`, `deploy`, `rollout-check`, `smoke`, `controller-check`) is
its own target, and `day1-check` composes them in dependency order with
no duplicated shell logic outside the Makefile. Validation logic itself
lives in `scripts/*.py`, not inline shell - the Makefile targets are
thin wrappers, which is exactly the shape needed for a later day's CI
to orchestrate this Makefile rather than reimplement it.

## 11. Live evidence

All confirmed directly against the running `maops-k8s-day1` cluster
during this review (not re-derived from manifests):

- Node `maops-k8s-day1-control-plane`: `Ready`, `v1.36.1`.
- `kubectl version` server: `v1.36.1`.
- Deployment `maops-app`: `2/2` ready, `2/2` available.
- Two Pods `Running`, `1/1` Ready, both owned by the same ReplicaSet.
- Service `maops-app`: `ClusterIP`, `10.96.5.10:8080`.
- `Endpoints maops-app`: both Pod IPs present and ready
  (`10.244.0.7:8080,10.244.0.8:8080`).
- Manual bounded port-forward (`kubectl port-forward svc/maops-app`,
  killed immediately after use) confirmed `/`, `/livez`, `/readyz`, and
  `/config` all respond correctly, with `/config` echoing exactly the
  four `APP_*` keys from `maops-app-config` and nothing else -
  confirming the ConfigMap -> env -> application -> `/config` flow is
  real, not asserted only against the manifest.
- `python3 scripts/manifest_check.py k8s/base`: `30/30 checks passed`.

---

## Findings

No Critical, High, or Medium findings were identified. No forced
findings were manufactured to pad this review - the Day 1 foundation is
coherent and matches the roadmap's stated scope precisely, both in
source and in live-cluster behavior.

### DAY1-ARCH-I1

- **Severity:** Info
- **Title:** Rollout/lifecycle fields correctly deferred, not merely absent by omission
- **Evidence:** `k8s/base/deployment.yaml` sets no `strategy`,
  `revisionHistoryLimit`, or `progressDeadlineSeconds`; the roadmap
  (`docs/roadmap.md:34-39`) explicitly assigns rollout/rollback/scaling
  tuning to Day 3.
- **Impact:** None - this is confirmation that the absence is
  deliberate scope discipline rather than an oversight, worth recording
  so a future reviewer doesn't mistake it for a gap.
- **Required remediation:** None.
- **Release-blocking:** NO

### DAY1-ARCH-I2

- **Severity:** Info
- **Title:** Selector design already anticipates future scaling/versioning without present-day complexity
- **Evidence:** `Deployment.spec.selector.matchLabels` and
  `Service.spec.selector` use only `{name, instance}`
  (`k8s/base/deployment.yaml:16-18`, `k8s/base/service.yaml:15-17`),
  omitting `version` - verified this remains true in the
  `kubectl kustomize` render, not just the source.
- **Impact:** Positive: this selector shape will not need to change
  when Day 3 introduces rollouts/rollbacks across versions, since a
  version-inclusive selector would have forced a selector migration
  later.
- **Required remediation:** None.
- **Release-blocking:** NO

---

## Final verdict

**APPROVE**

- Critical: 0
- High: 0
- Medium: 0
- Low: 0
- Info: 2

The Day 1 Kubernetes foundation is coherent, precisely scoped to the
roadmap's Day 1 entry (neither pulling forward later-day objects nor
missing anything Day 1 requires), uses idiomatic and verifiably correct
label/selector/ownership/probe/resource design, renders cleanly through
`kubectl kustomize` with no standalone tooling dependency, and is backed
by live-cluster evidence (not just static manifests) for every claim
checked above. It is suitable as the base for Day 2 and the subsequent
stages of the roadmap without any required remediation.

PROJECT 4 DAY 1 KUBERNETES ARCHITECTURE REVIEW COMPLETE

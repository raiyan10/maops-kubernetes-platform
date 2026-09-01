# maops-kubernetes-platform

Project 4 of the DevOps portfolio series. A staged, day-by-day build of a
Kubernetes platform, starting from a single-node kind cluster (Day 1) and
progressing toward production-readiness hardening (Day 7 / v1.0.0). See
`docs/roadmap.md` for the full seven-stage plan and `docs/architecture.md`
for how the pieces fit together.

## Ground rules for this repository

- **Native tooling only.** `docker`, `kubectl` (with bundled Kustomize),
  `kind`, and `helm` are the only expected CLIs. Do not add standalone
  `kustomize`, `yq`, `jq`, or a YAML/JSON processor as a project
  dependency for validation - `scripts/k8s_yaml.py` is a small
  dependency-free YAML-subset parser written specifically so
  `make manifest-check` never requires installing anything.
- **The application is not the point.** `app/server.py` is a
  deliberately tiny Python-stdlib-only HTTP workload that exists solely
  to prove Kubernetes behavior (probes, ConfigMap wiring, security
  context, controller reconciliation). Do not add web frameworks or
  third-party Python packages to it. Do not turn this into a repeat of
  the earlier `maops-docker-platform` project - the Kubernetes behavior
  is the portfolio focus, not the application.
- **The Makefile is the authoritative local interface.** Any CI added in
  a later day orchestrates `make` targets rather than reimplementing
  steps. When adding a capability, add or extend a Makefile target
  first, then wire automation to it.
- **Stage discipline.** Each day/version only introduces what that
  day's scope calls for (see `docs/roadmap.md`). Don't pull forward
  Secrets, RBAC, NetworkPolicy, PVCs, Helm, or CI wiring into Day 1 - a
  later day owns each of those explicitly. When in doubt about whether
  something belongs in the current day, check the roadmap before adding
  it.
- **kind cluster naming and image pinning are load-bearing.** The
  cluster name (`maops-k8s-day1` for Day 1) and the pinned
  `kindest/node` image digest in `kind/cluster.yaml` are deliberate and
  validated by tests - don't float them to `latest` or an unpinned tag.
- **`make cluster-delete` must stay scoped.** It deletes only the
  project's own kind cluster by name. Never introduce a target that
  runs `docker system prune`, deletes unrelated clusters, or otherwise
  reaches outside this project's resources.
- **Validation has two tiers, keep them separate.** Static validation
  (`make manifest-check`, `scripts/validate_manifests.py`) must run
  without a live cluster and without any third-party Python dependency.
  Real-cluster validation (`scripts/cluster_check.py`,
  `scripts/smoke.py`, `scripts/reconcile_check.py`) proves actual
  runtime behavior against a live kind cluster via `kubectl -o json`
  and real HTTP calls - it should never fall back to just re-reading
  the manifest.
- **Port-forwards must be bounded.** Any script that opens a
  `kubectl port-forward` must pick a free local port, wait for
  connectability, and guarantee cleanup in a `finally` block (see
  `scripts/portforward.py`). Never leave a background kubectl process
  running after a script exits.
- **Do not manufacture green results.** If a validation step fails,
  investigate and fix the root cause, then re-run only the affected
  step. Don't weaken a check just to make it pass.

## Where things live

- `app/` - the stdlib-only HTTP workload and its Dockerfile.
- `k8s/base/` - the Day 1 Kustomize base (Namespace, ConfigMap,
  Deployment, Service). No overlays yet.
- `kind/cluster.yaml` - the single-control-plane kind cluster config,
  pinned to an exact `kindest/node` digest.
- `scripts/` - dependency-free Python validation and cluster-interaction
  tooling used by the Makefile.
- `tests/` - Docker-free unit tests for `scripts/validate_manifests.py`
  and `scripts/k8s_yaml.py`, including negative cases.
- `docs/` - architecture explanation and the multi-day roadmap.

## Agents and skills

Five agents cover the review lifecycle for this project:
`kubernetes-architect`, `kubernetes-security-reviewer`,
`cluster-integration-engineer`, `kubernetes-test-engineer`, and
`release-engineer`. Four skills encode the validation checklists:
`manifest-validation`, `workload-security-validation`,
`kind-cluster-validation`, and `release-readiness`. Prefer invoking
these over ad hoc review when working on this project - they encode
the exact Day 1 (and future-day) requirements.

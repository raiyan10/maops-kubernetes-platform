# maops-kubernetes-platform

Project 4 of the DevOps portfolio series. A staged, day-by-day build of a
Kubernetes platform, starting from a single-node kind cluster (Day 1) and
progressing toward autoscaling and production-readiness hardening (Day 8 /
v1.0.0). Day 6 (`v0.6.0`) is released; Day 7 (`v0.7.0`, Recreate/Blue-Green/
Canary on a separate `maops-k8s-day7` kind cluster) is a release candidate:
its local Kind gate passed on a fresh cluster (run
`6b0029cc63724291a00bba6ed52ea7a9`), with PR, merge, merged-main
validation and publication still pending. See
`docs/roadmap.md` for the full eight-stage plan and `docs/architecture.md`
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
- **Day 7 never touches the Day 6 cluster.** `maops-k8s-day7`
  (`kind/cluster-day7.yaml`, host port 18081) runs alongside the
  released `maops-k8s-day6` (host port 18080). `scripts/kube.py`
  profiles (`MAOPS_CLUSTER_PROFILE`, default `day6`) and the Makefile's
  `CLUSTER_NAME`-derived profile keep every existing target on Day 6;
  Day 7 work goes through the `day7-*` targets / `DAY7_MAKE` only.
  Day 6-only mutating targets refuse the Day 7 profile - keep it so.
  Day 7 gates never list or require older clusters; that is the
  optional `make day7-history-audit`. Images are built, digest-checked,
  loaded into `maops-k8s-day7` and digest-checked per node before
  `day7-deploy`. `make -n` takes no lock; `make day7-plan` prints the
  sequence without running anything.
  Every Day 7 function that can reach a cluster must refuse on its own
  outside the Day 7 profile (`day7_strategy._kubectl`/`_run`, leaf
  guards) - never rely only on `main()`'s check; unit tests that call
  such code must mock it or select the day7 profile explicitly.
- **Day 7 chart changes are Helm stages, never ad hoc.** Every Day 7
  release change is `helm upgrade --reset-values -f
  helm-values/day7/<stage>.yaml -f <verified build overlay>` (never
  `--reuse-values`/`--set`/`kubectl patch`), a route-changing stage only goes through the
  candidate preflight gate (`day7_strategy.promote()`), and every
  experiment ends in a verified restoration reported separately from
  its primary result. The gateway candidate stays disabled by default
  and label-isolated (`component=gateway-candidate`); don't change the
  stable gateway's selectors, and don't describe the candidate as a
  different release (it is a configuration variant sharing the stable
  ServiceAccount/Istio principal).
- **Day 7 runs one verified build, pinned by content.** Every Day 7
  stage (and `day7-deploy`) is `-f <stage> -f <build overlay>` from
  `scripts/day7_build.py`: image tags `<version>-cfg-<config digest>`,
  and the chart refuses a Day 7 stage without them (never deploy the
  mutable `:<version>` tag to workloads, never `rollout restart`). A new
  build means a new run ID and new baselines. What runs is proven only
  by `make day7-running-images` (Kubernetes imageID mapped through the
  node's containerd record to the build's config digest) - never by a
  node image check alone.
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

- `app/`, `gateway/`, `state/` - the three stdlib-only HTTP workloads
  and their Dockerfiles.
- `k8s/base/` - the FROZEN Day 1-5 Kustomize source (last advanced for
  Day 5 / `v0.5.0`). Never modified or applied by any Day 6 target;
  `git diff` confirms its files are unchanged, and `make manifest-check`
  validates its rendered manifests.
- `charts/maops-kubernetes-platform/` - the Helm chart (Day 6, extended
  Day 7 with the optional gateway candidate and `routing.mode`), the sole
  application deployment source (`make deploy` on Day 6, `make
  day7-deploy` on Day 7); statically validated by `make helm-check`,
  which also renders every Day 7 stage against declared expectations.
- `helm-values/day7/` - the explicit Day 7 Helm stage files (stable,
  green-prepared, blue-green-cutover, canary-90-10, candidate-unready,
  recreate-prepared/-serving/-changed).
- `k8s/day6/` - cluster/platform manifests applied with `kubectl`,
  never templated by the chart: the three Namespaces, the diagnostics
  ServiceAccount, the Istio `Gateway` and its infrastructure ConfigMap,
  and the Cilium ambient health-probe policy. Unchanged since v0.6.0.
- `k8s/day7/` - the same seven platform objects for the Day 7 cluster,
  with Day 7 identities (instance `...-day7`, `maops-day7-validation`);
  selected by the Makefile's `PLATFORM_K8S` under the day7 profile.
  Nothing Day 7 creates may carry a day4/day6 identity (tested).
- `kind/` - pinned kind cluster configs, one per cluster generation
  (`cluster.yaml`, `cluster-day5.yaml`, `cluster-day6.yaml`,
  `cluster-day7.yaml`), each pinned to an exact `kindest/node` digest;
  earlier configs are preserved, never edited in place.
- `scripts/` - dependency-free Python validation and cluster-interaction
  tooling used by the Makefile.
- `tests/` - Docker-free unit tests for the validation and cluster
  tooling, including negative cases.
- `docs/` - architecture explanation, the multi-day roadmap, and the
  per-day engineering reviews (Day 1-6 records are historical and
  immutable).
- Day 7 evidence lives OUTSIDE the repo and /tmp, in the private run
  directory `$HOME/.local/state/maops-kubernetes-platform/day7-runs/<DAY7_RUN_ID>/`
  (0700, files 0600, never reused or recaptured).

## Agents and skills

Five agents cover the review lifecycle for this project:
`kubernetes-architect`, `kubernetes-security-reviewer`,
`cluster-integration-engineer`, `kubernetes-test-engineer`, and
`release-engineer`. Four skills encode the validation checklists:
`manifest-validation`, `workload-security-validation`,
`kind-cluster-validation`, and `release-readiness`. Prefer invoking
these over ad hoc review when working on this project - they encode
the exact Day 1 (and future-day) requirements.

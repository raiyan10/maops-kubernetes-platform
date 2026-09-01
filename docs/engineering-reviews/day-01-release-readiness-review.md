# Project 4 / Day 1 (v0.1.0) - Release Readiness Review

Independent review of the current uncommitted working tree on branch
`feature/day-1-kubernetes-foundation`. Formed from direct inspection of
this repository; no prior engineering-review documents in
`docs/engineering-reviews/` were read as part of forming this review.

## VERSION / REPOSITORY

- **VERSION file content**: PASS. `xxd VERSION` shows exactly
  `30 2e 31 2e 30 0a` -> `0.1.0\n` - a single trailing newline, no
  trailing whitespace, no CRLF, no extra content.
- **No local `v0.1.0` git tag**: PASS. `git tag` returns no output at all -
  zero tags exist in the repository.
- **No remote `v0.1.0` tag**: INCONCLUSIVE (not a repo defect). A remote
  `origin` (`git@github.com:raiyan10/maops-kubernetes-platform.git`) is
  configured, but `git ls-remote --tags origin` fails with
  `Permission denied (publickey)` in this sandboxed environment - no SSH
  credentials are available here. This is an environment limitation, not
  evidence of a leaked tag; combined with zero local tags and zero local
  commits beyond `de1fc9e Initial commit`, there is no plausible path by
  which a `v0.1.0` tag could exist remotely from this work.
- **Working tree intentionally uncommitted**: PASS. `git status` shows
  branch `feature/day-1-kubernetes-foundation`, one modified tracked file
  (`README.md`) and all Day 1 deliverables (`.claude/`, `.gitignore`,
  `Makefile`, `VERSION`, `app/`, `docs/`, `k8s/`, `kind/`, `scripts/`,
  `tests/`) as untracked. `git log --oneline --all` shows only
  `de1fc9e Initial commit`. This is exactly the expected pre-first-commit
  state for a Day 1 handoff.
- **No release claimed as already published**: PASS. Searched README.md
  and all files under `docs/` for "released", "shipped", "merged",
  "tagged v0.1.0" - no matches. README and docs consistently describe
  Day 1 in present/descriptive tense ("establishes the Kubernetes
  foundation"), not as a completed release.

## MAKEFILE CONTRACT

Reviewed the full Makefile (79 lines) end to end.

- `SHELL := /bin/bash` with `.SHELLFLAGS := -eu -o pipefail -c` at the top
  of the file - every recipe line runs under `-e -u -o pipefail`, so an
  unset variable, a failing command, or a failure anywhere in a pipe
  fails the recipe. This is a real fail-fast contract, not cosmetic.
- `tool-check`: verifies `docker`, `kubectl`, `kind`, `helm`, `python3`
  are present, additionally asserting `docker` resolves to exactly
  `/usr/bin/docker` (guards against a Docker Desktop/alias shadowing
  issue). Exits 1 with a clear message on any missing/mismatched tool.
- `test`: `python3 -m unittest discover -s tests -v` - real unit test
  execution, verbose, no output suppression.
- `manifest-render`: `kubectl kustomize $(BASE)` - pure rendering, no
  validation, matches its name.
- `manifest-check`: `python3 scripts/manifest_check.py $(BASE)` - renders
  and runs the dependency-free static validator; no live cluster
  involved.
- `image-build`: `docker build -t $(IMAGE) -f app/Dockerfile app/` -
  builds directly from the Dockerfile.
- `cluster-create`: idempotent - checks `kind get clusters` for an exact
  name match before creating, then always prints `kubectl get nodes` to
  prove the node is live. Uses `kind/cluster.yaml` (pinned image).
- `cluster-delete`: `kind delete cluster --name $(CLUSTER_NAME)` only -
  no `docker system prune`, no wildcard cluster deletion, no reach
  outside `maops-k8s-day1`. Confirmed by both reading the Makefile and
  grepping the whole repo for `system prune`, `docker rmi`,
  `docker rm -f` (zero matches anywhere).
- `image-load`: `kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)` -
  scoped to the project's own cluster name.
- `deploy`: `kubectl --context $(KCONTEXT) apply -k $(BASE)` - explicit
  context flag, not relying on ambient `kubectl` context.
- `rollout-check`: `python3 scripts/cluster_check.py` - real cluster
  reads via `kubectl -o json` equivalents (see script review below), not
  a manifest re-read.
- `smoke`: `python3 scripts/smoke.py` - bounded port-forward + real HTTP
  calls against `/`, `/livez`, `/readyz`, `/config`.
- `controller-check`: `python3 scripts/reconcile_check.py` - deletes
  exactly one pod and proves ReplicaSet-controller reconciliation.
- `day1-check`: composes all of the above as **make prerequisites**
  (`tool-check test manifest-check image-build cluster-create image-load
  deploy rollout-check smoke controller-check`), not as sequential shell
  commands inside one recipe. This means `make` itself halts the chain
  the moment any prerequisite target exits non-zero - `day1-check`'s own
  recipe body (the final `PASS` echo) is unreachable unless every
  upstream target succeeded.
- **No `|| true`, no swallowed exit codes, no `set +e`, no
  `continue-on-error` found anywhere** in the Makefile or `scripts/*.py`
  (explicit grep for `|| true`, `set +e`, `continue-on-error`,
  `2>/dev/null; echo`, `exit 0` - zero matches). No evidence of
  manufactured green output.
- The Makefile remains the sole authoritative local interface - no CI
  config exists in the repo (`.github/` is absent) that could
  reimplement or bypass these steps.

**No findings in this section.**

## DAY1-CHECK

Traced the dependency graph via `make -n day1-check`, which expands to
the tool-check / test / manifest-check / image-build / cluster-create /
image-load / deploy / rollout-check / smoke / controller-check command
sequence in that exact order, followed only by the final `PASS` echo.
Because these are declared as `make` prerequisites of `day1-check`
(`day1-check: tool-check test manifest-check image-build cluster-create
image-load deploy rollout-check smoke controller-check`), GNU Make's own
semantics guarantee the chain halts on the first non-zero exit - this is
enforced by `make` itself, not by hand-rolled shell logic that could be
bypassed or miswired.

I additionally **executed `make day1-check` for real** (not merely
traced it) to obtain fresh, first-hand evidence rather than relying on
any prior claim:

- `tool-check`: PASS - Docker 29.7.2 at `/usr/bin/docker`, kubectl
  v1.36.3 (Kustomize v5.8.1), kind v0.32.0, Helm v4.2.2, Python 3.14.4.
- `test`: PASS - 31/31 unit tests (`OK`), 0 failures, 0 errors.
- `manifest-check`: PASS - 30/30 static checks passed.
- `image-build`: succeeded, image built from the pinned distroless base.
- `cluster-create`: cluster `maops-k8s-day1` already existed (idempotent
  path taken), node confirmed `Ready`.
- `image-load` + `deploy`: succeeded; `deployment.apps/maops-app
  configured`.
- `rollout-check`: PASS - 15/15 real-cluster checks (node readiness,
  server version `v1.36.1`, namespace, Deployment Available, 2/2 ready
  replicas, 2 ready Service endpoints, live ConfigMap-to-env consumption
  read from an actual pod, live UID/GID `10001:10001`,
  `readOnlyRootFilesystem`, `allowPrivilegeEscalation`,
  `capabilities.drop`, `seccompProfile` all confirmed against the
  running pod, not the manifest).
- `smoke`: PASS - 4/4 real HTTP checks over a bounded port-forward
  (`127.0.0.1:36209`) against `/`, `/livez`, `/readyz`, `/config`.
- `controller-check`: PASS - 8/8 checks; recorded both pod UIDs, deleted
  exactly one pod, waited for the Deployment to return to 2/2 Ready,
  confirmed a genuinely new pod UID appeared while the untouched pod's
  UID was unchanged, then re-ran the full HTTP check set successfully.
- Final line: `PASS: day1-check completed the full authoritative
  validation sequence`.

Full real-cluster evidence, fresh as of this review, confirms `day1-check`
genuinely composes tool validation through controller-reconciliation
proof and cannot report success on a failed prerequisite.

**No findings in this section.**

## REPRODUCIBILITY / PINNING

- **kind version documentation**: README's Prerequisites table states
  kind `v0.32.0` was the version used; matches the actually-installed
  `kind version` output (`kind v0.32.0 go1.26.3 linux/amd64`) captured
  during the fresh `day1-check` run.
- **kind node image pinning**: `kind/cluster.yaml` pins
  `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5`
  - a full digest, not `latest` or a floating tag. This implies
  Kubernetes v1.36.1, which matches both README's architecture diagram
  ("Kubernetes v1.36.1") and the actual live cluster's server version
  confirmed during `rollout-check` (`server version v1.36.1`).
- **Container base image pinning**: `app/Dockerfile` pins
  `gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5`
  by digest - a minimal, non-root-friendly, package-manager-free
  distroless base, consistent with the project's "no third-party Python
  dependencies, no package installs" ground rule. `USER 10001:10001`
  matches the Deployment's `runAsUser`/`runAsGroup`.
- **VERSION-to-image-tag relationship**: the Makefile hardcodes
  `IMAGE := maops-kubernetes-platform:0.1.0` and `k8s/base/deployment.yaml`
  hardcodes the same `0.1.0` image tag; both currently agree with the
  `VERSION` file's `0.1.0`, and the app-level `app.kubernetes.io/version:
  "0.1.0"` labels in all four `k8s/base/*.yaml` manifests also agree.
  However, nothing in the Makefile, scripts, or tests actually reads the
  `VERSION` file and cross-checks it against the Makefile's `IMAGE`
  constant or the manifest labels (confirmed by grep - zero references
  to the `VERSION` file outside its own existence). Today these values
  are all manually kept in sync and correct, but there is no automated
  guard against drift on a future day. Logged as an Info-severity,
  non-blocking finding below (DAY1-REL-I1).
- Separately, `KIND_NODE_IMAGE` is declared as a Makefile variable
  (matching `kind/cluster.yaml`'s value) but is never actually referenced
  by any target - `cluster-create` reads the pin from `kind/cluster.yaml`
  directly via `--config`. This is dead/duplicated configuration, also
  logged as Info (DAY1-REL-I2).
- Per instructions, Project 3-level reproducibility machinery (SBOM,
  cosign signing, multi-arch manifest lists) was explicitly **not**
  demanded here as it is out of scope for Day 1.

## DOCUMENTATION

- **README.md**: Walks a new engineer through prerequisites (with exact
  tool versions used), cluster creation, image build/load, deploy,
  verification (`rollout-check`/`smoke`/`controller-check`), manual
  port-forward usage with literal `curl` commands against all four
  endpoints, cleanup (`cluster-delete`, explicitly stating it never runs
  `docker system prune`), Day 1 scope boundaries, and a repository layout
  map. Every command mentioned was cross-checked against the actual
  Makefile and confirmed to exist verbatim: `tool-check`, `test`,
  `manifest-check`, `image-build`, `cluster-create`, `image-load`,
  `deploy`, `rollout-check`, `smoke`, `controller-check`, `day1-check`,
  `cluster-delete`. The literal `docker build`/`kind load`/`kubectl
  apply -k` commands shown inline in README as comments match the
  Makefile recipes exactly. Scripts named in README
  (`scripts/cluster_check.py`, `scripts/smoke.py`,
  `scripts/reconcile_check.py`) all exist.
- **docs/architecture.md**: Describes the same control flow, matches the
  live/manifest state on every checked field - probe paths (`/livez` for
  startup+liveness, `/readyz` for readiness, matching
  `k8s/base/deployment.yaml` exactly), resource requests/limits
  (50m/32Mi requests, 250m/128Mi limits - matches the manifest exactly),
  and every security field (`runAsNonRoot`, `runAsUser: 10001`,
  `runAsGroup: 10001`, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`,
  `seccompProfile.type: RuntimeDefault`, `automountServiceAccountToken:
  false` - all match the manifest and were independently confirmed
  against the **live running pod** during the fresh `rollout-check` run,
  not just the static manifest).
- **docs/roadmap.md**: Seven-stage plan (Day 1/v0.1.0 through
  Day 7/v1.0.0) present and unmodified in structure; Day 1's own section
  correctly lists its scope and explicitly-excluded items
  (worker nodes, Secrets, RBAC, NetworkPolicy, PVC/StatefulSet, Helm, CI,
  Ingress, NodePort/LoadBalancer); the "Explicitly out of scope for this
  project" section correctly excludes Terraform, Ansible, Argo CD,
  observability, and cloud provisioning for the whole project's
  lifetime, not just Day 1.
- **Minor inconsistency found**: README's Day 1 architecture ASCII
  diagram labels the top of the flow "Docker Desktop", while
  `docs/architecture.md`'s equivalent diagram and README's own
  Prerequisites table both describe a native Linux/WSL Docker CLI
  resolving to `/usr/bin/docker` (not Docker Desktop). This is a cosmetic
  documentation inconsistency, not a functional or security issue.
  Logged as Low (DAY1-REL-L1).

## CLAUDE INFRASTRUCTURE

Verified by listing `.claude/agents/` and `.claude/skills/` directly (not
by trusting any prior count):

- **Agents** (`ls .claude/agents/`): exactly 5 files -
  `cluster-integration-engineer.md`, `kubernetes-architect.md`,
  `kubernetes-security-reviewer.md`, `kubernetes-test-engineer.md`,
  `release-engineer.md`. Matches the required set exactly; no extras, no
  omissions. Each file's YAML frontmatter `name:` field matches its
  filename.
- **Skills** (`ls .claude/skills/`): exactly 4 directories -
  `kind-cluster-validation`, `manifest-validation`, `release-readiness`,
  `workload-security-validation`, each containing a `SKILL.md` with a
  matching `name:` frontmatter field. Matches the required set exactly;
  no extras, no omissions.

**No findings in this section.**

## SCOPE

- No `.github/` directory exists (confirmed via `ls .github` - "No such
  file or directory") - no GitHub Actions CI has been introduced.
- No Helm chart, `Chart.yaml`, or `templates/` directory exists anywhere
  in the repo - `helm` is only present as a `tool-check` prerequisite
  (per the project's ground rules, listed among the "native tooling
  only" CLIs) and mentioned in prose in README/roadmap/agent-skill files
  purely as a **future-day** (Day 6) reference; grep confirms zero
  `helm install`/`helm template` usage anywhere.
- No Terraform (`*.tf`), Ansible (`*.yml` playbooks/inventories), or
  Argo CD manifests exist anywhere; grep for `terraform`, `ansible-
  playbook`, `argocd` across `.md`/`.yaml`/`.py` returns only the
  expected prose references in README/roadmap describing what is
  permanently out of scope for the whole project.
- No container registry push target exists in the Makefile (`image-build`
  builds locally; `image-load` loads directly into kind - no `docker
  push` anywhere).
- No observability stack (Prometheus/Grafana/Loki) config exists; grep
  confirms zero matches outside of the roadmap's "permanently out of
  scope" list.
- `k8s/base/` contains exactly Namespace, ConfigMap, Deployment, Service,
  and `kustomization.yaml` - no Secret, ServiceAccount, RBAC
  (Role/RoleBinding/ClusterRole/ClusterRoleBinding), NetworkPolicy, PVC,
  or StatefulSet objects are present. This is independently confirmed
  not just by manual inspection but by the `manifest-check` static
  validator's own `scope.no_forbidden_resources` check, which explicitly
  asserts none of `['ClusterRole', 'ClusterRoleBinding', 'Ingress',
  'NetworkPolicy', 'PersistentVolumeClaim', 'Role', 'RoleBinding',
  'Secret', 'ServiceAccount', 'StatefulSet']` are present, and this check
  passed in the fresh `day1-check` run.

**No findings in this section.**

## FINDINGS

```
ID: DAY1-REL-L1
Severity: Low
Title: README's Day 1 architecture diagram labels the Docker layer "Docker Desktop", inconsistent with the native-Linux Docker CLI described elsewhere in the same document
Evidence: README.md line 17 shows "Docker Desktop" at the top of the ASCII architecture diagram; README.md's own Prerequisites table (line 56) and docs/architecture.md line 6 both describe a native `docker` CLI resolving to `/usr/bin/docker`, with no Docker Desktop involved
Impact: Purely cosmetic - could momentarily confuse a new engineer about the expected Docker installation, but does not affect any command's correctness
Required remediation: kubernetes-architect or whoever owns README should change "Docker Desktop" to "Docker" (or "Docker Engine") in the README architecture diagram for consistency with docs/architecture.md and the Prerequisites table
Release-blocking: NO

ID: DAY1-REL-I1
Severity: Info
Title: VERSION file is not read or cross-checked by any Makefile target, script, or test against the hardcoded 0.1.0 image tag / manifest labels
Evidence: grep for "VERSION" across Makefile and scripts/*.py returns zero references to the VERSION file; Makefile's IMAGE constant (line 7) and k8s/base manifests' app.kubernetes.io/version labels independently hardcode "0.1.0" rather than deriving it from the VERSION file
Impact: No functional defect today - all values currently agree - but nothing would catch a future-day drift between VERSION and the image tag/manifest labels if one were bumped without the others
Required remediation: kubernetes-architect / release-engineer to consider (in a later day, not Day 1) whether the Makefile should derive IMAGE from `cat VERSION` or whether a check should assert they match; no action required for Day 1 release readiness itself
Release-blocking: NO

ID: DAY1-REL-I2
Severity: Info
Title: Makefile's KIND_NODE_IMAGE variable is declared but never referenced by any target
Evidence: Makefile line 8 defines KIND_NODE_IMAGE with the same digest-pinned value present in kind/cluster.yaml; grep confirms no target uses $(KIND_NODE_IMAGE) - cluster-create reads the pin directly from kind/cluster.yaml via --config
Impact: Harmless today since both values agree, but is a duplicated, unenforced source of truth for the pinned node image
Required remediation: kubernetes-architect to consider removing the unused variable or wiring it in for documentation purposes only; cosmetic, no action required for Day 1 release readiness
Release-blocking: NO
```

No Critical, High, or Medium findings were identified in any section of this review.

## FINAL VERDICT

**APPROVE WITH CONDITIONS**

Severity counts: Critical: 0, High: 0, Medium: 0, Low: 1, Info: 2.

Conditions (both non-blocking, cosmetic-only, do not require re-running
`day1-check`): fix the "Docker Desktop" wording in README's architecture
diagram (DAY1-REL-L1) at the reviewing engineer's discretion before or
after the first commit; consider the VERSION/image-tag and
KIND_NODE_IMAGE duplication items (DAY1-REL-I1, DAY1-REL-I2) for a later
day's cleanup - neither blocks Day 1.

All hard gates passed on fresh, independently re-run evidence: VERSION
is exactly `0.1.0` with no whitespace surprises; no local git tag exists
and no evidence of any remote tag; the working tree remains correctly
uncommitted; `make day1-check` was executed in full during this review
and passed every stage (31/31 unit tests, 30/30 static manifest checks,
15/15 real-cluster checks, 4/4 smoke checks, 8/8 controller-reconciliation
checks); no `kubectl port-forward` process was left running after any
script or after this review's own `day1-check` run; documentation is
current and consistent with the deployed manifests and running pod
state; the Claude agent/skill infrastructure matches the required 5
agents and 4 skills exactly; and no scope creep from any later day
(Secrets, RBAC, NetworkPolicy, PVC/StatefulSet, Helm, CI, observability,
Terraform/Ansible/Argo CD, cloud provisioning) was found anywhere in the
working tree.

This working tree is suitable to proceed toward the first v0.1.0 feature
commit/PR after review adjudication of the two low/info items above. It
has not been released, shipped, or merged, and per this review's own hard
constraint, no commit, tag, push, or GitHub release was performed as
part of producing this review.

PROJECT 4 DAY 1 RELEASE READINESS REVIEW COMPLETE

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CLUSTER_NAME := maops-k8s-day4
KCONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := maops-platform

# Overridable kubeconfig path (DAY4) - every kubectl invocation below,
# and every scripts/*.py call (via scripts/kube.py, which reads this
# same env var), passes --kubeconfig explicitly rather than depending
# on or mutating the caller's default kubeconfig/current context. Never
# a hardcoded personal home directory - $(HOME) is resolved by the
# shell at invocation time, and Python's fallback (scripts/kube.py)
# resolves it via Path.home() if this var is ever unset for a direct
# script invocation. Override with `make KUBECONFIG_PATH=/some/path ...`.
KUBECONFIG_PATH ?= $(HOME)/.kube/$(CLUSTER_NAME).config
export KUBECONFIG_PATH
BASE := k8s/base

# DAY4 batch 2b/2c: a minimal process-held local mutual-exclusion lock
# (scripts/day4_lock.py) scoped to this project's single Day 4 target -
# closes the gap where a unique per-run baseline artifact (DAY4_RUN_ID/
# DAY4_SUITE_BASELINE_PATH above) does NOT prevent two independent,
# concurrent invocations from mutating the same live cluster at once.
# Wraps every standalone mutating entry point below AND the entire
# day4-check sequence as one lock acquisition. A child `$(MAKE) X`
# recipe line invoked recursively from an already-locked ancestor
# inherits the ancestor's actual locked file descriptor (via
# DAY4_LOCK_FD, verified - never a bare trusted flag) and skips
# re-acquiring; see day4_lock.py's own module docstring for the full
# fd-inheritance/lifetime design and why a naive re-acquire would
# deadlock, and why a bare boolean flag was replaced this batch (it
# could be bypassed by an unrelated process, and released exclusion
# early if only the wrapper - not its still-running child - was
# killed). Deliberately independent of the DAY4_RUN_ID/baseline
# mechanism above - neither reads nor writes the other's file.
DAY4_LOCK := python3 scripts/day4_lock.py run --

# DAY4: run-specific suite-level `/state` baseline identity (see
# scripts/suite_baseline.py for the full design). Computed once,
# immediately, when THIS `make` process's Makefile is parsed - not a
# per-cluster/reusable value. `$(if ...)` prefers an already-exported
# value from a PARENT `make` process over generating a fresh one, so a
# top-level `make day4-check` invocation generates ONE run ID/path and
# every `$(MAKE) X` recipe line below - each its own child `make`
# process - inherits the SAME values via the environment (`export`,
# below) rather than each independently generating its own. A
# standalone `make state-check` or `make final-state-check` (run
# outside `day4-check`, with nothing already exported) instead
# generates its own fresh value each time it is invoked as a top-level
# command - which is exactly why two independent standalone
# invocations can never accidentally agree on a run ID (see
# suite_baseline.py's "standalone command behavior" section: this is
# the intended fail-closed behavior, not a bug). Never used by any
# target other than state-check (writer) and final-state-check
# (reader) - every other target ignores these vars entirely.
DAY4_RUN_ID := $(if $(DAY4_RUN_ID),$(DAY4_RUN_ID),$(shell python3 -c "import uuid; print(uuid.uuid4().hex)"))
DAY4_SUITE_BASELINE_PATH := $(if $(DAY4_SUITE_BASELINE_PATH),$(DAY4_SUITE_BASELINE_PATH),/tmp/maops-day4-suite-baseline-$(DAY4_RUN_ID).json)
export DAY4_RUN_ID
export DAY4_SUITE_BASELINE_PATH

# DAY1-REL-I1 (closed): VERSION is read once, and all three image names
# derive from it - nothing hardcodes the literal version string a
# second time.
VERSION := $(shell cat VERSION)
GATEWAY_IMAGE := maops-kubernetes-gateway:$(VERSION)
APP_IMAGE := maops-kubernetes-app:$(VERSION)
STATE_IMAGE := maops-kubernetes-state:$(VERSION)

# DAY4: local image-build/export settings needed for THIS local
# kind/containerd combination to load the digest-pinned multi-arch
# Distroless base cleanly (see docs/architecture.md's "Image build/load
# contract" section for the full root-cause explanation: `kind load
# docker-image` cannot import a multi-arch OCI index directly, and a
# prior manual `ctr images import` workaround produced a synthetic
# image alias that a separate containerd 2.3.1 CRI bug then failed to
# resolve during container creation). Building explicitly for
# linux/amd64 with attestations disabled produces a genuine
# single-platform local image that loads and runs through the normal
# `kind load docker-image` path with no manual workaround. This is a
# local compatibility fix for this kind/containerd combination - NOT a
# general production supply-chain policy; a real registry-backed
# pipeline would build/attest multi-arch images properly instead of
# disabling attestations.
IMAGE_BUILD_FLAGS := --platform linux/amd64 --provenance=false --sbom=false --load

.PHONY: help tool-check test version-check manifest-render manifest-check \
        image-build cluster-create cluster-delete context-check \
        storage-bootstrap storage-hardening-check \
        namespace-apply secret-bootstrap image-load deploy workload-refresh rollout-check \
        scheduling-check discovery-check secret-check smoke \
        dependency-check scaling-check rolling-update-check pdb-check \
        state-check persistence-check retention-check \
        final-state-check controller-check day4-check

help: ## Show this help
	@echo "MAOps Kubernetes Platform - Day 4 - available targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-26s %s\n", $$1, $$2}'

tool-check: ## Verify the required local toolchain is present and correctly resolved
	@echo "== tool-check =="
	@command -v docker >/dev/null || { echo "docker not found"; exit 1; }
	@resolved=$$(command -v docker); \
	if [ "$$resolved" != "/usr/bin/docker" ]; then \
		echo "FAIL: docker resolves to $$resolved, expected /usr/bin/docker"; exit 1; \
	fi; \
	echo "PASS: docker -> $$resolved"
	@docker --version
	@command -v kubectl >/dev/null || { echo "kubectl not found"; exit 1; }
	@kubectl version --client
	@command -v kind >/dev/null || { echo "kind not found"; exit 1; }
	@kind version
	@command -v helm >/dev/null || { echo "helm not found"; exit 1; }
	@helm version
	@command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
	@python3 --version
	@echo "PASS: all required tools present"

test: ## Run Docker-free/unit tests for repository-owned validation logic
	python3 -m unittest discover -s tests -v

version-check: ## Cross-check VERSION against rendered image tags and version labels (closes DAY1-REL-I1)
	python3 scripts/version_check.py $(BASE)

manifest-render: ## Render the Day 4 Kustomize base with kubectl kustomize (pure local render, no cluster contact)
	kubectl kustomize $(BASE)

manifest-check: ## Run repository-owned static validation against the rendered manifests
	python3 scripts/manifest_check.py $(BASE)

image-build: ## Build all three workload container images (gateway, app, state)
	$(DAY4_LOCK) sh -c '\
		docker build $(IMAGE_BUILD_FLAGS) -t $(GATEWAY_IMAGE) -f gateway/Dockerfile gateway/ && \
		docker build $(IMAGE_BUILD_FLAGS) -t $(APP_IMAGE) -f app/Dockerfile app/ && \
		docker build $(IMAGE_BUILD_FLAGS) -t $(STATE_IMAGE) -f state/Dockerfile state/ \
	'

cluster-create: ## Create the scoped kind cluster (idempotent, never recreated if present) - maops-k8s-day4 only, 1 control-plane + 2 workers
	$(DAY4_LOCK) sh -c '\
		if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
			echo "kind cluster $(CLUSTER_NAME) already exists, skipping create"; \
		else \
			kind create cluster --name $(CLUSTER_NAME) --config kind/cluster.yaml --kubeconfig $(KUBECONFIG_PATH); \
		fi; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes \
	'

cluster-delete: ## Delete ONLY the maops-k8s-day4 kind cluster
	$(DAY4_LOCK) kind delete cluster --name $(CLUSTER_NAME) --kubeconfig $(KUBECONFIG_PATH)

context-check: ## Fail closed unless kubectl is verified against the isolated Day 4 cluster at the pinned node version
	python3 scripts/context_check.py

storage-bootstrap: ## Harden new local-path-provisioner backing directories (root:10001, mode 2770) before any application PVC is created (needs `make image-load` first - its scratch probe Pod reuses the already-loaded app image)
	$(DAY4_LOCK) python3 scripts/storage_bootstrap.py

storage-hardening-check: ## Prove the storage bootstrap actually changed access behavior, via a disposable scratch PVC (never the application's own PVC; also needs `make image-load` first)
	$(DAY4_LOCK) python3 scripts/storage_hardening_check.py

namespace-apply: ## Apply ONLY the project Namespace to the explicit Day 4 context (must precede secret-bootstrap)
	$(DAY4_LOCK) kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(BASE)/namespace.yaml

secret-bootstrap: ## Create (or preserve) both runtime Secrets - maops-internal-auth and maops-state-auth - never committed, never printed
	$(DAY4_LOCK) python3 scripts/secret_bootstrap.py all

image-load: ## Load all three locally built images into every node of the kind cluster
	$(DAY4_LOCK) sh -c '\
		kind load docker-image $(GATEWAY_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(APP_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(STATE_IMAGE) --name $(CLUSTER_NAME) \
	'

deploy: ## Apply the full Day 4 Kustomize base (namespace, ConfigMaps, Deployments, StatefulSet, Services, PDBs) to the kind cluster
	$(DAY4_LOCK) kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -k $(BASE)

workload-refresh: ## Force maops-state/maops-app/maops-gateway to restart through their own controllers, in that dependency order, so every workload actually runs THIS run's freshly built/loaded local image rather than whichever workload's `deploy` diff happened to trigger a rollout on its own - an intentional local-image refresh with a brief single-replica maops-state outage, never a zero-downtime claim; never touches PVC/PV/Secrets
	$(DAY4_LOCK) python3 scripts/workload_refresh.py

rollout-check: ## Wait for and verify real Deployment/Service/EndpointSlice/ConfigMap/security runtime state for gateway/app (unchanged Day 3 behavior)
	python3 scripts/cluster_check.py

scheduling-check: ## Prove worker-only scheduling and per-workload topology spread against the live cluster (gateway/app - unchanged Day 3 behavior)
	python3 scripts/scheduling_check.py

discovery-check: ## Prove real Kubernetes DNS resolution + gateway -> maops-app Service HTTP from a live gateway Pod
	python3 scripts/discovery_check.py

secret-check: ## Prove Secret wiring, authenticated/unauthenticated behavior, and non-disclosure end to end (both Secrets)
	python3 scripts/secret_check.py

smoke: ## Port-forward service/maops-gateway and exercise /, /livez, /readyz, /config, /backend, /state over real HTTP
	python3 scripts/smoke.py

dependency-check: ## Prove gateway liveness vs. dependency-aware readiness by scaling maops-app to 0 and back
	$(DAY4_LOCK) python3 scripts/dependency_check.py

scaling-check: ## Prove real scaling behavior (3 -> 4 -> 3) for gateway/app, with guaranteed restoration
	$(DAY4_LOCK) python3 scripts/scaling_check.py

rolling-update-check: ## Prove a real rolling update and `kubectl rollout undo` rollback for gateway/app
	$(DAY4_LOCK) python3 scripts/rollout_check.py

pdb-check: ## Prove real PodDisruptionBudget/Eviction-API behavior for gateway/app (state carries no PDB)
	$(DAY4_LOCK) python3 scripts/pdb_check.py

state-check: ## Prove maops-state StatefulSet/PVC/Service runtime identity, security, and storage binding (also captures the run-specific suite-level /state baseline used by final-state-check, when DAY4_RUN_ID/DAY4_SUITE_BASELINE_PATH are set - see scripts/suite_baseline.py)
	python3 scripts/state_check.py

persistence-check: ## Prove data survives maops-state-0 pod deletion/rescheduling (same PVC/PV, new Pod UID, unchanged marker)
	$(DAY4_LOCK) python3 scripts/persistence_check.py

retention-check: ## Prove PVC/PV retention and app/gateway degraded-but-live behavior across a state 1 -> 0 -> 1 cycle
	$(DAY4_LOCK) python3 scripts/retention_check.py

final-state-check: ## Independently prove the cluster is fully restored to its normal Day 4 baseline after all mutating experiments (a standalone run without DAY4_RUN_ID/DAY4_SUITE_BASELINE_PATH from a prior state-check in the SAME invocation cannot claim suite-baseline restoration - see scripts/suite_baseline.py)
	python3 scripts/final_state_check.py

controller-check: ## (Bonus, not part of day4-check) Prove Deployment controller reconciliation for maops-app
	$(DAY4_LOCK) python3 scripts/reconcile_check.py

day4-check: ## Authoritative Day 4 validation sequence - explicitly sequential via recipe lines, never a parallelizable prerequisite list, so `make -j` can never reorder a mutation ahead of its precondition. Acquired under ONE Day 4 mutation lock for its entire duration (scripts/day4_lock.py) - a second independent day4-check (or any other standalone mutating target) invoked concurrently fails immediately, before this sequence's own first mutating step, rather than racing it
	$(DAY4_LOCK) sh -c '\
		$(MAKE) tool-check && \
		$(MAKE) test && \
		$(MAKE) version-check && \
		$(MAKE) manifest-check && \
		$(MAKE) image-build && \
		$(MAKE) cluster-create && \
		$(MAKE) context-check && \
		$(MAKE) image-load && \
		$(MAKE) storage-bootstrap && \
		$(MAKE) storage-hardening-check && \
		$(MAKE) namespace-apply && \
		$(MAKE) secret-bootstrap && \
		$(MAKE) deploy && \
		$(MAKE) workload-refresh && \
		$(MAKE) rollout-check && \
		$(MAKE) scheduling-check && \
		$(MAKE) discovery-check && \
		$(MAKE) secret-check && \
		$(MAKE) smoke && \
		$(MAKE) dependency-check && \
		$(MAKE) scaling-check && \
		$(MAKE) rolling-update-check && \
		$(MAKE) pdb-check && \
		$(MAKE) state-check && \
		$(MAKE) persistence-check && \
		$(MAKE) retention-check && \
		$(MAKE) final-state-check \
	'
	@echo ""
	@echo "PASS: day4-check completed the full authoritative validation sequence"

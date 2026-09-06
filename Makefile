SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CLUSTER_NAME := maops-k8s-day3
KCONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := maops-platform
BASE := k8s/base

# DAY1-REL-I1 (closed): VERSION is read once, and both image names derive
# from it - nothing hardcodes the literal version string a second time.
VERSION := $(shell cat VERSION)
GATEWAY_IMAGE := maops-kubernetes-gateway:$(VERSION)
APP_IMAGE := maops-kubernetes-app:$(VERSION)

.PHONY: help tool-check test version-check manifest-render manifest-check \
        image-build cluster-create cluster-delete context-check \
        namespace-apply secret-bootstrap image-load deploy rollout-check \
        scheduling-check discovery-check secret-check smoke \
        dependency-check scaling-check rolling-update-check pdb-check \
        final-state-check controller-check day3-check

help: ## Show this help
	@echo "MAOps Kubernetes Platform - Day 3 - available targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-22s %s\n", $$1, $$2}'

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

manifest-render: ## Render the Day 3 Kustomize base with kubectl kustomize
	kubectl kustomize $(BASE)

manifest-check: ## Run repository-owned static validation against the rendered manifests
	python3 scripts/manifest_check.py $(BASE)

image-build: ## Build both workload container images (gateway, app)
	docker build -t $(GATEWAY_IMAGE) -f gateway/Dockerfile gateway/
	docker build -t $(APP_IMAGE) -f app/Dockerfile app/

cluster-create: ## Create the scoped kind cluster (idempotent, never recreated if present) - maops-k8s-day3 only, 1 control-plane + 2 workers
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
		echo "kind cluster $(CLUSTER_NAME) already exists, skipping create"; \
	else \
		kind create cluster --name $(CLUSTER_NAME) --config kind/cluster.yaml; \
	fi
	kubectl --context $(KCONTEXT) get nodes

cluster-delete: ## Delete ONLY the maops-k8s-day3 kind cluster
	kind delete cluster --name $(CLUSTER_NAME)

context-check: ## Fail closed unless kubectl is verified against the isolated Day 3 cluster at the pinned node version
	python3 scripts/context_check.py

namespace-apply: ## Apply ONLY the project Namespace to the explicit Day 3 context (must precede secret-bootstrap)
	kubectl --context $(KCONTEXT) apply -f $(BASE)/namespace.yaml

secret-bootstrap: ## Create (or preserve) the runtime maops-internal-auth Secret - never committed, never printed
	python3 scripts/secret_bootstrap.py

image-load: ## Load both locally built images into every node of the kind cluster
	kind load docker-image $(GATEWAY_IMAGE) --name $(CLUSTER_NAME)
	kind load docker-image $(APP_IMAGE) --name $(CLUSTER_NAME)

deploy: ## Apply the full Day 3 Kustomize base (namespace, ConfigMaps, Deployments, Services, PDBs) to the kind cluster
	kubectl --context $(KCONTEXT) apply -k $(BASE)

rollout-check: ## Wait for and verify real Deployment/Service/EndpointSlice/ConfigMap/security runtime state for both workloads
	python3 scripts/cluster_check.py

scheduling-check: ## Prove worker-only scheduling and per-workload topology spread against the live 1-control-plane + 2-worker cluster
	python3 scripts/scheduling_check.py

discovery-check: ## Prove real Kubernetes DNS resolution + gateway -> maops-app Service HTTP from a live gateway Pod
	python3 scripts/discovery_check.py

secret-check: ## Prove Secret wiring, authenticated/unauthenticated behavior, and non-disclosure end to end
	python3 scripts/secret_check.py

smoke: ## Port-forward service/maops-gateway and exercise /, /livez, /readyz, /config, /backend over real HTTP
	python3 scripts/smoke.py

dependency-check: ## Prove gateway liveness vs. dependency-aware readiness by scaling maops-app to 0 and back
	python3 scripts/dependency_check.py

scaling-check: ## Prove real scaling behavior (3 -> 4 -> 3) for both workloads, with guaranteed restoration
	python3 scripts/scaling_check.py

rolling-update-check: ## Prove a real rolling update (temporary Pod-template annotation) and a real `kubectl rollout undo` rollback for both workloads
	python3 scripts/rollout_check.py

pdb-check: ## Prove real PodDisruptionBudget/Eviction-API behavior (voluntary disruption blocked at 2/3, restored to 3/3) for both workloads
	python3 scripts/pdb_check.py

final-state-check: ## Independently prove the cluster is fully restored to its normal Day 3 baseline after all mutating experiments
	python3 scripts/final_state_check.py

controller-check: ## (Bonus, not part of day3-check) Prove Deployment controller reconciliation for maops-app
	python3 scripts/reconcile_check.py

day3-check: tool-check test version-check manifest-check image-build cluster-create context-check namespace-apply secret-bootstrap image-load deploy rollout-check scheduling-check discovery-check secret-check smoke dependency-check scaling-check rolling-update-check pdb-check final-state-check ## Authoritative Day 3 validation sequence
	@echo ""
	@echo "PASS: day3-check completed the full authoritative validation sequence"

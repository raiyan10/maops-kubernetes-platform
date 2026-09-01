SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CLUSTER_NAME := maops-k8s-day1
KCONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := maops-platform
IMAGE := maops-kubernetes-platform:0.1.0
BASE := k8s/base

.PHONY: help tool-check test manifest-render manifest-check image-build \
        cluster-create cluster-delete image-load deploy rollout-check \
        smoke controller-check day1-check

help: ## Show this help
	@echo "MAOps Kubernetes Platform - Day 1 - available targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

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

manifest-render: ## Render the Day 1 Kustomize base with kubectl kustomize
	kubectl kustomize $(BASE)

manifest-check: ## Run repository-owned static validation against the rendered manifests
	python3 scripts/manifest_check.py $(BASE)

image-build: ## Build the workload container image
	docker build -t $(IMAGE) -f app/Dockerfile app/

cluster-create: ## Create the scoped kind cluster (idempotent)
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
		echo "kind cluster $(CLUSTER_NAME) already exists, skipping create"; \
	else \
		kind create cluster --name $(CLUSTER_NAME) --config kind/cluster.yaml; \
	fi
	kubectl --context $(KCONTEXT) get nodes

cluster-delete: ## Delete ONLY the maops-k8s-day1 kind cluster
	kind delete cluster --name $(CLUSTER_NAME)

image-load: ## Load the locally built image into the kind cluster
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)

deploy: ## Apply the Day 1 manifests to the kind cluster
	kubectl --context $(KCONTEXT) apply -k $(BASE)

rollout-check: ## Wait for and verify real Deployment/Service/ConfigMap/security runtime state
	python3 scripts/cluster_check.py

smoke: ## Port-forward the Service and exercise /, /livez, /readyz, /config over real HTTP
	python3 scripts/smoke.py

controller-check: ## Prove Deployment controller reconciliation by deleting one pod
	python3 scripts/reconcile_check.py

day1-check: tool-check test manifest-check image-build cluster-create image-load deploy rollout-check smoke controller-check ## Authoritative Day 1 validation sequence
	@echo ""
	@echo "PASS: day1-check completed the full authoritative validation sequence"

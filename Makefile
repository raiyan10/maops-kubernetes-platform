SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CLUSTER_NAME := maops-k8s-day6
KCONTEXT := kind-$(CLUSTER_NAME)
NAMESPACE := maops-platform
VALIDATION_NAMESPACE := maops-day6-validation
INGRESS_NAMESPACE := maops-ingress

# Overridable kubeconfig path - every kubectl/helm invocation below,
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
DAY6_K8S := k8s/day6
CHART := charts/maops-kubernetes-platform
HELM_RELEASE := maops-kubernetes-platform-day6

# DAY6: a minimal process-held local mutual-exclusion lock
# (scripts/day6_lock.py), independent of Day 4's/Day 5's own preserved
# locks - see day6_lock.py's own module docstring for the full
# fd-inheritance/lifetime design (carried forward unchanged from Day
# 5's own hardened design).
DAY6_LOCK := python3 scripts/day6_lock.py run --

# DAY6 (unchanged design from Day 4/5): run-specific suite-level
# `/state` baseline identity (see scripts/suite_baseline.py for the
# full design). Computed once, immediately, when THIS `make` process's
# Makefile is parsed - not a per-cluster/reusable value. A top-level
# `make day6-check` invocation generates ONE run ID/path and every
# `$(MAKE) X` recipe line below inherits the SAME values via the
# environment; a standalone `make state-check` or `make
# final-state-check` instead generates its own fresh value each time.
# To bracket standalone targets as one run, pass the SAME
# DAY6_RUN_ID/DAY6_SUITE_BASELINE_PATH on every command line. The
# default path is under /tmp, which does not survive a host reboot -
# point DAY6_SUITE_BASELINE_PATH at a private (0700) directory outside
# the repository when the run must survive one (see README.md).
DAY6_RUN_ID := $(if $(DAY6_RUN_ID),$(DAY6_RUN_ID),$(shell python3 -c "import uuid; print(uuid.uuid4().hex)"))
DAY6_SUITE_BASELINE_PATH := $(if $(DAY6_SUITE_BASELINE_PATH),$(DAY6_SUITE_BASELINE_PATH),/tmp/maops-day6-suite-baseline-$(DAY6_RUN_ID).json)
export DAY6_RUN_ID
export DAY6_SUITE_BASELINE_PATH

# DAY1-REL-I1 (closed): VERSION is read once, and all three image names
# derive from it - nothing hardcodes the literal version string a
# second time.
VERSION := $(shell cat VERSION)
GATEWAY_IMAGE := maops-kubernetes-gateway:$(VERSION)
APP_IMAGE := maops-kubernetes-app:$(VERSION)
STATE_IMAGE := maops-kubernetes-state:$(VERSION)

# DAY5 (unchanged for Day 6): local image-build/export settings needed
# for THIS local kind/containerd combination - see
# docs/architecture.md's "DAY4: the storage preflight, and the
# containerd multi-arch image defect it found" section.
IMAGE_BUILD_FLAGS := --platform linux/amd64 --provenance=false --sbom=false --load

# DAY6: pinned infrastructure versions/URLs - installed out-of-band via
# Helm/kubectl, never floated to "latest". scripts/version_check.py's
# own day6.pinned_infra checks cross-verify these exact values are what
# actually appears in this Makefile.
CILIUM_VERSION := 1.20.1
GATEWAY_API_VERSION := v1.6.0
GATEWAY_API_CRDS_URL := https://github.com/kubernetes-sigs/gateway-api/releases/download/$(GATEWAY_API_VERSION)/standard-install.yaml
ISTIO_VERSION := 1.31.0
ISTIO_HELM_REPO := https://blob.istio.io/istio-release/charts
ISTIO_NAMESPACE := istio-system

.PHONY: help tool-check test version-check manifest-render manifest-check \
        helm-lint helm-template helm-check \
        image-build cluster-create cluster-delete context-check \
        gateway-api-install cni-install cni-status mesh-install mesh-status \
        storage-bootstrap storage-hardening-check \
        namespace-apply gateway-apply secret-bootstrap image-load deploy rollout-check \
        scheduling-check discovery-check secret-check \
        gateway-check mesh-check rbac-check networkpolicy-check smoke \
        dependency-check scaling-check rolling-update-check pdb-check \
        state-check persistence-check retention-check helm-lifecycle-check \
        final-state-check controller-check ci-check day6-check

help: ## Show this help
	@echo "MAOps Kubernetes Platform - Day 6 - available targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-26s %s\n", $$1, $$2}'

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

version-check: ## Cross-check VERSION/chart version/appVersion/image tags/Day 6 identities/pinned infra versions, and independently confirm k8s/base stays frozen at its Day 5 target (closes DAY1-REL-I1, extended for Day 6)
	python3 scripts/version_check.py $(BASE)

manifest-render: ## Render the frozen Day 5 Kustomize base with kubectl kustomize (pure local render, no cluster contact) - k8s/base is NOT part of the Day 6 deploy
	kubectl kustomize $(BASE)

manifest-check: ## Run repository-owned static validation against the frozen Day 5 k8s/base render (unchanged Day 5 behavior - proves k8s/base was not touched)
	python3 scripts/manifest_check.py $(BASE)

helm-lint: ## Run `helm lint` against the Day 6 application chart (pure local static check, no cluster contact)
	helm lint $(CHART)

helm-template: ## Render the Day 6 application chart with `helm template` (pure local render, no cluster contact)
	helm template $(HELM_RELEASE) $(CHART) --namespace $(NAMESPACE)

helm-check: ## Run repository-owned static validation against the rendered Day 6 chart - exact inventory, versions, security context, RBAC, NetworkPolicy topology, PeerAuthentication/AuthorizationPolicy, Gateway API references, and the k8s/base-still-frozen guard
	python3 scripts/helm_check.py $(CHART)

image-build: ## Build all three workload container images (gateway, app, state)
	$(DAY6_LOCK) sh -c '\
		docker build $(IMAGE_BUILD_FLAGS) -t $(GATEWAY_IMAGE) -f gateway/Dockerfile gateway/ && \
		docker build $(IMAGE_BUILD_FLAGS) -t $(APP_IMAGE) -f app/Dockerfile app/ && \
		docker build $(IMAGE_BUILD_FLAGS) -t $(STATE_IMAGE) -f state/Dockerfile state/ \
	'

cluster-create: ## Create the scoped kind cluster (idempotent, never recreated if present) - maops-k8s-day6 only, 1 control-plane + 2 workers, host 127.0.0.1:18080 -> control-plane:30080
	$(DAY6_LOCK) sh -c '\
		if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
			echo "kind cluster $(CLUSTER_NAME) already exists, skipping create"; \
		else \
			kind create cluster --name $(CLUSTER_NAME) --config kind/cluster-day6.yaml --kubeconfig $(KUBECONFIG_PATH); \
		fi; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes \
	'

cluster-delete: ## Delete ONLY the maops-k8s-day6 kind cluster
	$(DAY6_LOCK) kind delete cluster --name $(CLUSTER_NAME) --kubeconfig $(KUBECONFIG_PATH)

context-check: ## Fail closed unless kubectl is verified against the isolated Day 6 cluster at the pinned node version
	python3 scripts/context_check.py

gateway-api-install: ## Install the Gateway API standard CRDs (pinned v1.6.0) - no controller, Istio provisions the `istio` GatewayClass itself once istiod is installed
	$(DAY6_LOCK) kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(GATEWAY_API_CRDS_URL)

cni-install: ## Install/upgrade Cilium 1.20.1 via Helm, configured for Istio ambient coexistence (kube-proxy stays enabled; kindnet is disabled via kind/cluster-day6.yaml's networking.disableDefaultCNI so every node is NotReady/no pod network until this runs); single, explicitly non-HA operator replica for this constrained local cluster; install SUBMISSION is followed by a bounded `kubectl rollout status` wait for both the Cilium agent DaemonSet and the Cilium operator Deployment, with read-only diagnostics printed and a nonzero exit on timeout - see docs/architecture.md
	$(DAY6_LOCK) bash -c '\
		set -euo pipefail; \
		helm repo add cilium https://helm.cilium.io/ >/dev/null 2>&1 || true; \
		helm repo update cilium >/dev/null; \
		helm upgrade --install cilium cilium/cilium \
			--version $(CILIUM_VERSION) \
			--namespace kube-system \
			--kubeconfig $(KUBECONFIG_PATH) \
			--kube-context $(KCONTEXT) \
			--set ipam.mode=kubernetes \
			--set kubeProxyReplacement=false \
			--set hubble.enabled=false \
			--set cni.exclusive=false \
			--set socketLB.hostNamespaceOnly=true \
			--set bpf.masquerade=false \
			--set envoy.enabled=false \
			--set operator.replicas=1 \
			--set image.pullPolicy=IfNotPresent; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system rollout status daemonset/cilium --timeout=180s || { \
			echo "FAIL: daemonset/cilium did not become Ready within 180s of Helm install submission - read-only diagnostics follow:" >&2; \
			echo "--- nodes ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes -o wide || true; \
			echo "--- daemonset/cilium ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get daemonset/cilium -o wide || true; \
			echo "--- cilium agent pods ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get pods -l k8s-app=cilium -o wide || true; \
			echo "--- describe daemonset/cilium ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system describe daemonset/cilium || true; \
			echo "--- recent events (kube-system) ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get events --sort-by=.lastTimestamp | tail -n 40 || true; \
			exit 1; \
		}; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system rollout status deployment/cilium-operator --timeout=120s || { \
			echo "FAIL: deployment/cilium-operator did not become Ready within 120s of Helm install submission - read-only diagnostics follow:" >&2; \
			echo "--- nodes ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes -o wide || true; \
			echo "--- deployment/cilium-operator ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get deployment/cilium-operator -o wide || true; \
			echo "--- cilium-operator pods ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get pods -l io.cilium/app=operator -o wide || true; \
			echo "--- describe deployment/cilium-operator ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system describe deployment/cilium-operator || true; \
			echo "--- recent events (kube-system) ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n kube-system get events --sort-by=.lastTimestamp | tail -n 40 || true; \
			exit 1; \
		} \
	'

cni-status: ## Read-only: verify the Cilium DaemonSet is Running/Ready on every node and report real NetworkPolicy enforcement status (never mutates)
	python3 scripts/cni_check.py

mesh-install: ## Install Istio ambient (pinned 1.31.0, from https://blob.istio.io/istio-release/charts) in the documented order - base, istiod (profile=ambient), cni (profile=ambient), ztunnel - plus the narrowly-scoped CiliumClusterwideNetworkPolicy for ambient health-probe compatibility; conservative local-development resource requests/limits, istiod autoscaling explicitly disabled (pilot.autoscaleEnabled=false - no metrics-server is installed, so an HPA could never read metrics), no waypoint, no Hubble, no kube-proxy replacement; each of istiod/istio-cni-node/ztunnel's install SUBMISSION is followed by its own bounded `kubectl rollout status` wait, with read-only diagnostics printed and a nonzero exit on timeout - mesh-status remains the authoritative post-install verification
	$(DAY6_LOCK) bash -c '\
		set -euo pipefail; \
		helm repo add istio $(ISTIO_HELM_REPO) >/dev/null 2>&1 || true; \
		helm repo update istio >/dev/null; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) create namespace $(ISTIO_NAMESPACE) --dry-run=client -o yaml | \
			kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f -; \
		helm upgrade --install istio-base istio/base \
			--version $(ISTIO_VERSION) --namespace $(ISTIO_NAMESPACE) \
			--kubeconfig $(KUBECONFIG_PATH) --kube-context $(KCONTEXT) \
			--set defaultRevision=default; \
		helm upgrade --install istiod istio/istiod \
			--version $(ISTIO_VERSION) --namespace $(ISTIO_NAMESPACE) \
			--kubeconfig $(KUBECONFIG_PATH) --kube-context $(KCONTEXT) \
			--set profile=ambient \
			--set pilot.autoscaleEnabled=false \
			--set pilot.resources.requests.cpu=100m \
			--set pilot.resources.requests.memory=128Mi \
			--set pilot.resources.limits.cpu=500m \
			--set pilot.resources.limits.memory=512Mi \
			--wait; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) rollout status deployment/istiod --timeout=120s || { \
			echo "FAIL: deployment/istiod did not become Ready within 120s of Helm install submission - read-only diagnostics follow:" >&2; \
			echo "--- nodes ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes -o wide || true; \
			echo "--- deployment/istiod ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get deployment/istiod -o wide || true; \
			echo "--- istiod pods ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get pods -l app=istiod -o wide || true; \
			echo "--- describe deployment/istiod ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) describe deployment/istiod || true; \
			echo "--- recent events ($(ISTIO_NAMESPACE)) ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get events --sort-by=.lastTimestamp | tail -n 40 || true; \
			exit 1; \
		}; \
		helm upgrade --install istio-cni istio/cni \
			--version $(ISTIO_VERSION) --namespace $(ISTIO_NAMESPACE) \
			--kubeconfig $(KUBECONFIG_PATH) --kube-context $(KCONTEXT) \
			--set profile=ambient \
			--set resources.requests.cpu=50m \
			--set resources.requests.memory=64Mi; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) rollout status daemonset/istio-cni-node --timeout=120s || { \
			echo "FAIL: daemonset/istio-cni-node did not become Ready within 120s of Helm install submission - read-only diagnostics follow:" >&2; \
			echo "--- nodes ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes -o wide || true; \
			echo "--- daemonset/istio-cni-node ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get daemonset/istio-cni-node -o wide || true; \
			echo "--- istio-cni-node pods ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get pods -l k8s-app=istio-cni-node -o wide || true; \
			echo "--- describe daemonset/istio-cni-node ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) describe daemonset/istio-cni-node || true; \
			echo "--- recent events ($(ISTIO_NAMESPACE)) ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get events --sort-by=.lastTimestamp | tail -n 40 || true; \
			exit 1; \
		}; \
		helm upgrade --install ztunnel istio/ztunnel \
			--version $(ISTIO_VERSION) --namespace $(ISTIO_NAMESPACE) \
			--kubeconfig $(KUBECONFIG_PATH) --kube-context $(KCONTEXT) \
			--set resources.requests.cpu=50m \
			--set resources.requests.memory=64Mi; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) rollout status daemonset/ztunnel --timeout=120s || { \
			echo "FAIL: daemonset/ztunnel did not become Ready within 120s of Helm install submission - read-only diagnostics follow:" >&2; \
			echo "--- nodes ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes -o wide || true; \
			echo "--- daemonset/ztunnel ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get daemonset/ztunnel -o wide || true; \
			echo "--- ztunnel pods ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get pods -l app=ztunnel -o wide || true; \
			echo "--- describe daemonset/ztunnel ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) describe daemonset/ztunnel || true; \
			echo "--- recent events ($(ISTIO_NAMESPACE)) ---"; kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) -n $(ISTIO_NAMESPACE) get events --sort-by=.lastTimestamp | tail -n 40 || true; \
			exit 1; \
		}; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(DAY6_K8S)/cilium-ambient-probe-policy.yaml \
	'

mesh-status: ## Read-only: verify istiod/istio-cni/ztunnel are Running/Ready (never mutates) - deeper mTLS/identity proof is `make mesh-check`
	python3 scripts/mesh_status.py

storage-bootstrap: ## Harden new local-path-provisioner backing directories (root:10001, mode 2770) before any application PVC is created (unchanged Day 4/5 behavior; needs `make image-load` first - its scratch probe Pod reuses the already-loaded app image)
	$(DAY6_LOCK) python3 scripts/storage_bootstrap.py

storage-hardening-check: ## Prove the storage bootstrap actually changed access behavior, via a disposable scratch PVC (unchanged Day 4/5 behavior; never the application's own PVC; also needs `make image-load` first)
	$(DAY6_LOCK) python3 scripts/storage_hardening_check.py

namespace-apply: ## Apply ONLY the three Day 6 platform Namespaces (maops-platform, maops-day6-validation, maops-ingress) and the diagnostics ServiceAccount to the explicit Day 6 context (must precede secret-bootstrap/gateway-apply/deploy)
	$(DAY6_LOCK) sh -c '\
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply \
			-f $(DAY6_K8S)/platform-namespace.yaml \
			-f $(DAY6_K8S)/validation-namespace.yaml \
			-f $(DAY6_K8S)/ingress-namespace.yaml && \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(DAY6_K8S)/diagnostics-serviceaccount.yaml \
	'

gateway-apply: ## Apply the Istio Gateway infrastructure ConfigMap and the Gateway API `Gateway` object (maops-edge, in maops-ingress) - cluster/platform objects, not part of the Helm chart
	$(DAY6_LOCK) kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply \
		-f $(DAY6_K8S)/gateway-values-configmap.yaml \
		-f $(DAY6_K8S)/gateway.yaml

secret-bootstrap: ## Create (or preserve) both runtime Secrets - maops-internal-auth and maops-state-auth - never committed, never printed
	$(DAY6_LOCK) python3 scripts/secret_bootstrap.py all

image-load: ## Load all three locally built images into every node of the kind cluster
	$(DAY6_LOCK) sh -c '\
		kind load docker-image $(GATEWAY_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(APP_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(STATE_IMAGE) --name $(CLUSTER_NAME) \
	'

deploy: ## Apply the Day 6 Helm chart (`helm upgrade --install`) - the SOLE Day 6 application deployment source; never applies k8s/base
	$(DAY6_LOCK) helm upgrade --install $(HELM_RELEASE) $(CHART) \
		--namespace $(NAMESPACE) \
		--kubeconfig $(KUBECONFIG_PATH) \
		--kube-context $(KCONTEXT) \
		--wait

rollout-check: ## Wait for and verify real Deployment/Service/EndpointSlice/ConfigMap/security runtime state for gateway/app (unchanged Day 3-5 behavior, now against the Helm-deployed workloads)
	python3 scripts/cluster_check.py

scheduling-check: ## Prove worker-only scheduling and per-workload topology spread against the live cluster (gateway/app - unchanged Day 3-5 behavior)
	python3 scripts/scheduling_check.py

discovery-check: ## Prove real Kubernetes DNS resolution + gateway -> maops-app Service HTTP from a live gateway Pod (unchanged Day 3-5 behavior)
	python3 scripts/discovery_check.py

secret-check: ## Prove Secret wiring, authenticated/unauthenticated behavior, and non-disclosure end to end (unchanged Day 4-5 behavior, both Secrets)
	python3 scripts/secret_check.py

gateway-check: ## Prove GatewayClass/Gateway/HTTPRoute are Accepted/Programmed, the application is reachable through http://127.0.0.1:18080 with Host: maops.local, and a wrong Host receives a definite HTTP 404 no-route result (unreachability is its own INCONCLUSIVE outcome, never counted as proof of no route)
	python3 scripts/gateway_check.py

mesh-check: ## Prove ztunnel is Ready on every node, istiod/istio-cni are healthy, application Pods are ambient-enrolled with no sidecars, strict mTLS is in effect, allowed identity paths still succeed, and an authenticated but unauthorized ambient identity is denied - isolated to Istio's AuthorizationPolicy layer specifically via correlated ztunnel logs (bounded diagnostic log level, restored after) - with verified (not merely --wait=false) namespace cleanup
	python3 scripts/mesh_check.py

rbac-check: ## Prove maops-diagnostics RBAC scope from a live probe Pod (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/rbac_check.py

networkpolicy-check: ## Prove default-deny + explicit-allow NetworkPolicy behavior, adapted for Day 6: validation-client -> gateway/app/state all DENIED (Day 5's validation-client -> gateway allow is removed), gateway -> app and app -> state allowed, gateway -> state denied, DNS resolution works
	$(DAY6_LOCK) python3 scripts/networkpolicy_check.py

smoke: ## Port-forward service/maops-gateway and exercise /, /livez, /readyz, /config, /backend, /state over real HTTP (unchanged Day 5 behavior - complements, never replaces, gateway-check's external Gateway-routed proof)
	python3 scripts/smoke.py

dependency-check: ## Prove gateway liveness vs. dependency-aware readiness by scaling maops-app to 0 and back (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/dependency_check.py

scaling-check: ## Prove real scaling behavior (3 -> 4 -> 3) for gateway/app, with guaranteed restoration (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/scaling_check.py

rolling-update-check: ## Prove a real rolling update and `kubectl rollout undo` rollback for gateway/app (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/rollout_check.py

pdb-check: ## Prove real PodDisruptionBudget/Eviction-API behavior for gateway/app (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/pdb_check.py

state-check: ## Prove maops-state StatefulSet/PVC/Service runtime identity, security, and storage binding (unchanged Day 5 behavior; captures the run-specific suite-level /state baseline used by final-state-check)
	python3 scripts/state_check.py

persistence-check: ## Prove data survives maops-state-0 pod deletion/rescheduling (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/persistence_check.py

retention-check: ## Prove PVC/PV retention and app/gateway degraded-but-live behavior across a state 1 -> 0 -> 1 cycle (unchanged Day 5 behavior)
	$(DAY6_LOCK) python3 scripts/retention_check.py

helm-lifecycle-check: ## Bounded live proof of a real Helm upgrade + rollback (release revision/config/health before and after, external routed behavior, PVC data preserved throughout); this is Helm release rollback, never the Day 7 Recreate/Blue-Green/Canary demonstration
	$(DAY6_LOCK) python3 scripts/helm_lifecycle_check.py

final-state-check: ## Independently prove the cluster is fully restored to its normal Day 6 baseline after all mutating experiments
	python3 scripts/final_state_check.py

controller-check: ## (Bonus, not part of day6-check) Prove Deployment controller reconciliation for maops-app
	$(DAY6_LOCK) python3 scripts/reconcile_check.py

ci-check: ## Cluster-free static validation sequence only - unit tests, version check, k8s/base manifest check, Helm lint/template/static-check. Never creates a cluster, never touches Docker images, Cilium, or Istio - safe for GitHub Actions
	$(MAKE) test
	$(MAKE) version-check
	$(MAKE) manifest-check
	$(MAKE) helm-lint
	$(MAKE) helm-template > /dev/null
	$(MAKE) helm-check
	@echo ""
	@echo "PASS: ci-check completed the full cluster-free static validation sequence"

day6-check: ## Authoritative Day 6 validation sequence - explicitly sequential via recipe lines, never a parallelizable prerequisite list. Acquired under ONE Day 6 mutation lock for its entire duration (scripts/day6_lock.py). Builds/loads all three images before Helm installation and restores all mutated application/persistent state before success
	$(DAY6_LOCK) sh -c '\
		$(MAKE) tool-check && \
		$(MAKE) test && \
		$(MAKE) version-check && \
		$(MAKE) manifest-check && \
		$(MAKE) helm-lint && \
		$(MAKE) helm-template > /dev/null && \
		$(MAKE) helm-check && \
		$(MAKE) image-build && \
		$(MAKE) cluster-create && \
		$(MAKE) gateway-api-install && \
		$(MAKE) cni-install && \
		$(MAKE) cni-status && \
		$(MAKE) context-check && \
		$(MAKE) mesh-install && \
		$(MAKE) mesh-status && \
		$(MAKE) image-load && \
		$(MAKE) storage-bootstrap && \
		$(MAKE) storage-hardening-check && \
		$(MAKE) namespace-apply && \
		$(MAKE) secret-bootstrap && \
		$(MAKE) gateway-apply && \
		$(MAKE) deploy && \
		$(MAKE) rollout-check && \
		$(MAKE) scheduling-check && \
		$(MAKE) discovery-check && \
		$(MAKE) secret-check && \
		$(MAKE) gateway-check && \
		$(MAKE) mesh-check && \
		$(MAKE) rbac-check && \
		$(MAKE) networkpolicy-check && \
		$(MAKE) smoke && \
		$(MAKE) dependency-check && \
		$(MAKE) scaling-check && \
		$(MAKE) rolling-update-check && \
		$(MAKE) pdb-check && \
		$(MAKE) state-check && \
		$(MAKE) persistence-check && \
		$(MAKE) retention-check && \
		$(MAKE) helm-lifecycle-check && \
		$(MAKE) final-state-check \
	'
	@echo ""
	@echo "PASS: day6-check completed the full authoritative validation sequence"

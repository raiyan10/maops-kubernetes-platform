SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

CLUSTER_NAME := maops-k8s-day6
KCONTEXT := kind-$(CLUSTER_NAME)

# DAY7: cluster PROFILE, derived from CLUSTER_NAME so the kubectl/helm
# context a recipe uses and the context scripts/kube.py verifies can
# never disagree. `make X` keeps addressing the released Day 6 cluster
# exactly as before; the Day 7 targets below re-invoke the SAME recipes
# with `CLUSTER_NAME=maops-k8s-day7` (see DAY7_MAKE). Any other cluster
# name fails closed at parse time, before any recipe runs. The profile
# also selects the kind config, the Helm release/instance name, and the
# mutation lock (scripts/day6_lock.py vs scripts/day7_lock.py - two
# independent locks).
ifeq ($(filter $(CLUSTER_NAME),maops-k8s-day6 maops-k8s-day7),)
$(error CLUSTER_NAME=$(CLUSTER_NAME) is not a supported cluster (expected maops-k8s-day6 or maops-k8s-day7))
endif
CLUSTER_PROFILE := $(if $(filter maops-k8s-day7,$(CLUSTER_NAME)),day7,day6)
MAOPS_CLUSTER_PROFILE := $(CLUSTER_PROFILE)
export MAOPS_CLUSTER_PROFILE
KIND_CONFIG := kind/cluster-$(CLUSTER_PROFILE).yaml
NAMESPACE := maops-platform
VALIDATION_NAMESPACE := maops-$(CLUSTER_PROFILE)-validation
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
# DAY7: the cluster/platform manifests follow the profile - k8s/day6 for
# the Day 6 cluster (unchanged), k8s/day7 (same objects, Day 7
# identities) for maops-k8s-day7.
PLATFORM_K8S := k8s/$(CLUSTER_PROFILE)
CHART := charts/maops-kubernetes-platform
HELM_RELEASE := maops-kubernetes-platform-$(CLUSTER_PROFILE)

# DAY6: a minimal process-held local mutual-exclusion lock
# (scripts/day6_lock.py), independent of Day 4's/Day 5's own preserved
# locks - see day6_lock.py's own module docstring for the full
# fd-inheritance/lifetime design (carried forward unchanged from Day
# 5's own hardened design).
# DAY7 remediation (dry-run side effect): GNU make still EXECUTES a
# recipe line that references $(MAKE) under `make -n`, so that nested
# makes can print their own commands. Such a line here is always a lock
# wrapper around nested makes, so `make -n day6-check`/`day7-check` used
# to take (and create) the real lock file. Under -n/--dry-run the lock
# prefix is therefore empty: the nested makes inherit -n and only print,
# so nothing that needs mutual exclusion runs. A real (non -n) run
# always gets the real lock - DRY_RUN is derived only from make's own
# flag letters in MAKEFLAGS.
DRY_RUN := $(findstring n,$(firstword -$(MAKEFLAGS)))
DAY6_LOCK := $(if $(DRY_RUN),,python3 scripts/$(CLUSTER_PROFILE)_lock.py run --)

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

# DAY7: Day 6-only targets. These either deploy with implicit values
# (`deploy` - Helm would reuse the previous release's values) or mutate
# chart-owned objects outside Helm / with --reuse-values (the inherited
# Day 3-6 experiments). None of them belongs to the Day 7 cluster, where
# every chart-owned change is an explicit Helm stage - so each refuses to
# run under the day7 profile, before any mutation.
define require_day6_profile
	@if [ "$(CLUSTER_PROFILE)" != "day6" ]; then echo "FAIL: '$@' is a Day 6-only target and refuses to run against $(CLUSTER_NAME) - use the day7-* targets" >&2; exit 1; fi
endef

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
        final-state-check controller-check ci-check day6-check \
        day7-preflight day7-cluster-create day7-cluster-delete day7-deploy \
        day7-baseline-init day7-baseline day7-blue-green day7-canary day7-recreate \
        day7-final-state-check day7-resume-check day7-final-gate day7-check \
        day7-image-verify-local day7-image-verify-nodes day7-stable-check \
        day7-history-audit day7-plan day7-nodes-ready day7-validation-client-probe \
        day7-build-record day7-image-load day7-running-images

help: ## Show this help
	@echo "MAOps Kubernetes Platform - Day 6 (v0.6.0) and Day 7 (v0.7.0, released) - available targets:"
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

helm-lint: ## Run `helm lint` against the application chart with default values AND with every Day 7 stage file (pure local static check, no cluster contact)
	helm lint $(CHART)
	@overlay=$$(mktemp) && trap 'rm -f "$$overlay"' EXIT && python3 scripts/day7_build.py sample-overlay > "$$overlay" && \
	for f in $(DAY7_STAGE_DIR)/*.yaml; do echo "helm lint $(CHART) -f $$f -f <synthetic build overlay>"; helm lint --quiet $(CHART) -f "$$f" -f "$$overlay" || exit 1; done

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

cluster-create: ## Create the scoped kind cluster (idempotent, never recreated if present) - $(CLUSTER_NAME) only (default maops-k8s-day6: host 127.0.0.1:18080; day7-cluster-create: maops-k8s-day7, host 127.0.0.1:18081), 1 control-plane + 2 workers -> control-plane:30080
	$(DAY6_LOCK) sh -c '\
		if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
			echo "kind cluster $(CLUSTER_NAME) already exists, skipping create"; \
		else \
			kind create cluster --name $(CLUSTER_NAME) --config $(KIND_CONFIG) --kubeconfig $(KUBECONFIG_PATH); \
		fi; \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) get nodes \
	'

cluster-delete: ## Delete ONLY the $(CLUSTER_NAME) kind cluster (default maops-k8s-day6)
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
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(PLATFORM_K8S)/cilium-ambient-probe-policy.yaml \
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
			-f $(PLATFORM_K8S)/platform-namespace.yaml \
			-f $(PLATFORM_K8S)/validation-namespace.yaml \
			-f $(PLATFORM_K8S)/ingress-namespace.yaml && \
		kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply -f $(PLATFORM_K8S)/diagnostics-serviceaccount.yaml \
	'

gateway-apply: ## Apply the Istio Gateway infrastructure ConfigMap and the Gateway API `Gateway` object (maops-edge, in maops-ingress) - cluster/platform objects, not part of the Helm chart
	$(DAY6_LOCK) kubectl --kubeconfig $(KUBECONFIG_PATH) --context $(KCONTEXT) apply \
		-f $(PLATFORM_K8S)/gateway-values-configmap.yaml \
		-f $(PLATFORM_K8S)/gateway.yaml

secret-bootstrap: ## Create (or preserve) both runtime Secrets - maops-internal-auth and maops-state-auth - never committed, never printed
	$(DAY6_LOCK) python3 scripts/secret_bootstrap.py all

image-load: ## Load all three locally built images into every node of the kind cluster
	$(DAY6_LOCK) sh -c '\
		kind load docker-image $(GATEWAY_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(APP_IMAGE) --name $(CLUSTER_NAME) && \
		kind load docker-image $(STATE_IMAGE) --name $(CLUSTER_NAME) \
	'

deploy: ## Apply the Day 6 Helm chart (`helm upgrade --install`) - the SOLE Day 6 application deployment source; never applies k8s/base
	$(require_day6_profile)
	$(DAY6_LOCK) helm upgrade --install $(HELM_RELEASE) $(CHART) \
		--namespace $(NAMESPACE) \
		--kubeconfig $(KUBECONFIG_PATH) \
		--kube-context $(KCONTEXT) \
		--wait

ambient-workload-check: ## Read-only: verify every deployed gateway (3), app (3), and state (1) Pod has its expected identity/ambient-enrollment metadata and ztunnel LISTEN sockets on 15001/15006/15008 in its own network namespace (Ready or the redirection annotation alone never pass; sockets and metadata only - not redirection rules, HBONE/mTLS traffic, or AuthorizationPolicy behavior, which mesh-check proves) - run after deploy and after any host/Docker restart, before rollout-check; mesh-status remains the infrastructure-only check
	python3 scripts/ambient_workload_check.py

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
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/dependency_check.py

scaling-check: ## Prove real scaling behavior (3 -> 4 -> 3) for gateway/app, with guaranteed restoration (unchanged Day 5 behavior)
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/scaling_check.py

rolling-update-check: ## Prove a real rolling update and `kubectl rollout undo` rollback for gateway/app (unchanged Day 5 behavior)
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/rollout_check.py

pdb-check: ## Prove real PodDisruptionBudget/Eviction-API behavior for gateway/app (unchanged Day 5 behavior)
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/pdb_check.py

state-check: ## Prove maops-state StatefulSet/PVC/Service runtime identity, security, and storage binding (unchanged Day 5 behavior; captures the run-specific suite-level /state baseline used by final-state-check)
	python3 scripts/state_check.py

persistence-check: ## Prove data survives maops-state-0 pod deletion/rescheduling (unchanged Day 5 behavior)
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/persistence_check.py

retention-check: ## Prove PVC/PV retention and app/gateway degraded-but-live behavior across a state 1 -> 0 -> 1 cycle (unchanged Day 5 behavior)
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/retention_check.py

helm-lifecycle-check: ## Bounded live proof of a real Helm upgrade + rollback (release revision/config/health before and after, external routed behavior, PVC data preserved throughout); this is Helm release rollback, never the Day 7 Recreate/Blue-Green/Canary demonstration
	$(require_day6_profile)
	$(DAY6_LOCK) python3 scripts/helm_lifecycle_check.py

final-state-check: ## Independently prove the cluster is fully restored to its normal Day 6 baseline after all mutating experiments
	python3 scripts/final_state_check.py

controller-check: ## (Bonus, not part of day6-check) Prove Deployment controller reconciliation for maops-app
	$(require_day6_profile)
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
		$(MAKE) ambient-workload-check && \
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

# ---------------------------------------------------------------------------
# DAY7: deployment strategies (v0.7.0, released 2026-09-30) on a SEPARATE,
# isolated kind cluster. Nothing below ever addresses maops-k8s-day6:
# every live step either re-invokes an existing recipe through DAY7_MAKE
# (CLUSTER_NAME=maops-k8s-day7 -> day7 profile, day7 lock, day7 kind
# config/release/kubeconfig) or runs a Day 7 script under
# MAOPS_CLUSTER_PROFILE=day7 (scripts refuse any other profile).
# ---------------------------------------------------------------------------
DAY7_CLUSTER_NAME := maops-k8s-day7
DAY7_KCONTEXT := kind-$(DAY7_CLUSTER_NAME)
DAY7_KUBECONFIG_PATH ?= $(HOME)/.kube/$(DAY7_CLUSTER_NAME).config
DAY7_HELM_RELEASE := maops-kubernetes-platform-day7
DAY7_STAGE_DIR := helm-values/day7
DAY7_LOCK := $(if $(DRY_RUN),,python3 scripts/day7_lock.py run --)
DAY7_MAKE := $(MAKE) CLUSTER_NAME=$(DAY7_CLUSTER_NAME) KUBECONFIG_PATH=$(DAY7_KUBECONFIG_PATH)
DAY7_ENV := MAOPS_CLUSTER_PROFILE=day7 KUBECONFIG_PATH=$(DAY7_KUBECONFIG_PATH)

# DAY7: one run ID per top-level invocation (inherited by every nested
# $(MAKE) through the environment, like DAY6_RUN_ID). All of a run's
# evidence lives in ONE private directory outside /tmp and outside the
# repository - created exclusively with mode 0700 by
# `day7-baseline-init`; each baseline file inside is created O_EXCL with
# mode 0600 and is never overwritten or recaptured. To re-check a run
# later (e.g. after a host restart), pass the SAME DAY7_RUN_ID on the
# command line - the directory and file paths derive from it.
DAY7_RUN_ID := $(if $(DAY7_RUN_ID),$(DAY7_RUN_ID),$(shell python3 -c "import uuid; print(uuid.uuid4().hex)"))
DAY7_BASELINE_ROOT ?= $(HOME)/.local/state/maops-kubernetes-platform/day7-runs
DAY7_BASELINE_DIR := $(DAY7_BASELINE_ROOT)/$(DAY7_RUN_ID)
DAY7_SUITE_BASELINE_PATH := $(DAY7_BASELINE_DIR)/suite-baseline.json
DAY7_STRATEGY_BASELINE_PATH := $(DAY7_BASELINE_DIR)/strategy-baseline.json
export DAY7_RUN_ID
export DAY7_BASELINE_DIR
export DAY7_SUITE_BASELINE_PATH
export DAY7_STRATEGY_BASELINE_PATH
# DAY7 image contract: verified builds (scripts/day7_build.py) - one
# private 0700 directory per build, named by its content, plus
# current.json naming the build the next Day 7 deploy carries.
DAY7_BUILD_ROOT ?= $(HOME)/.local/state/maops-kubernetes-platform/day7-builds
export DAY7_BUILD_ROOT

day7-preflight: ## Read-only host preflight for the isolated Day 7 cluster - kind/cluster-day7.yaml identity, Docker, existing kind clusters (Day 6 is reported and left alone), host port 18081 free (or owned by maops-k8s-day7), WSL memory/CPU/disk/inotify headroom
	python3 scripts/day7_preflight.py

day7-image-verify-local: ## Cluster-free: prove the three $(VERSION) images exist locally as single-platform linux/amd64 images and record their CONFIG digests (from `docker save`, the digest kind a kind node's containerd reports) - run right after image-build
	python3 scripts/day7_image_check.py local

day7-image-verify-nodes: ## Read-only: prove every maops-k8s-day7 node (and only Day 7 nodes) holds each $(VERSION) image under its tag with the SAME config digest as the local build, AND every pinned <repo>:$(VERSION)-cfg-<digest> tag of the current verified build with the digest its name carries - run after image-load + day7-image-load; fails before deploy if any image is missing or different
	python3 scripts/day7_image_check.py nodes

day7-nodes-ready: ## Read-only, Day 7 only: bounded wait (default 180s) until all 3 maops-k8s-day7 nodes report Ready after cni-install - closes the cold-start race before the single-snapshot cni-status; fails on timeout, API error, or any non-Day-7 context
	env $(DAY7_ENV) python3 scripts/day7_nodes_ready.py

day7-cluster-create: ## Create maops-k8s-day7 (kind/cluster-day7.yaml, host 127.0.0.1:18081) alongside - never instead of - maops-k8s-day6; idempotent, never recreated
	$(DAY7_MAKE) cluster-create

day7-cluster-delete: ## Delete ONLY the maops-k8s-day7 kind cluster (never Day 6, never any other cluster)
	$(DAY7_MAKE) cluster-delete

day7-deploy: ## Install/upgrade the Day 7 release at the STABLE stage - explicit --reset-values -f helm-values/day7/stable.yaml -f <current verified build overlay> (candidate disabled, 100% stable route, every image tag pinned to its config digest so a new build ALWAYS changes the Pod templates), bounded wait - run day7-running-images afterwards
	$(DAY7_LOCK) sh -c '\
		build_values=$$(python3 scripts/day7_build.py values-path) && \
		helm upgrade --install $(DAY7_HELM_RELEASE) $(CHART) \
			--namespace $(NAMESPACE) \
			--kubeconfig $(DAY7_KUBECONFIG_PATH) \
			--kube-context $(DAY7_KCONTEXT) \
			--reset-values \
			-f $(DAY7_STAGE_DIR)/stable.yaml \
			-f "$$build_values" \
			--wait \
			--timeout 300s \
	'

day7-build-record: ## Cluster-free: pin today's verified local images to content-derived tags <repo>:$(VERSION)-cfg-<config digest> (local `docker tag`, digest re-derived from the new tag) and record the build (private $(DAY7_BUILD_ROOT)/<build id>/, current.json) - run after day7-image-verify-local
	$(DAY7_LOCK) python3 scripts/day7_build.py record

day7-image-load: ## Load the current verified build's pinned tags into maops-k8s-day7 ONLY (kind load) - day7-image-verify-nodes then proves each node holds them
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_build.py load-kind

day7-running-images: ## Read-only: prove every running stable gateway/app/state container (and candidate container, if enabled) runs the current verified build - exact Pod counts, Ready, pinned image reference, and the Kubernetes imageID mapped through the node's containerd record to the build's config digest
	env $(DAY7_ENV) python3 scripts/day7_running_images.py

day7-baseline-init: ## Create this run's private evidence directory ($(DAY7_BASELINE_ROOT)/<DAY7_RUN_ID>, mode 0700) - refuses to reuse an existing one
	$(DAY7_LOCK) python3 scripts/private_run_dir.py init

day7-baseline: ## Capture the Day 7 strategy baseline ONCE (Helm revision/values/manifest digest, the verified build, route, workload/Service/PDB and storage identities) - only after every running container is verified on that build; requires day7-baseline-init and the run's state-check suite baseline; never overwrites
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_baseline.py capture

day7-blue-green: ## Bounded Blue/Green: prepare candidate (route 100% stable) -> preflight gate -> mesh path probes -> Helm cutover to 100% candidate -> external proof -> verified restoration to stable
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_blue_green.py

day7-canary: ## Bounded Canary: gate -> ONE HTTPRoute weighted stable 90 / candidate 10 -> live backendRefs/status/endpoints -> bounded external sample (both versions observed, counts only) -> candidate-unready negative gate -> verified restoration
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_canary.py

day7-recreate: ## Bounded Recreate on the CANDIDATE Deployment only: candidate serves 100% -> ConfigMap-only change under strategy Recreate, observing Pods/endpoints/external traffic -> ordering + interruption report -> verified restoration
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_recreate.py

day7-validation-client-probe: ## OPTIONAL, never part of day7-check - bounded live proof that a validation-client Pod cannot reach the gateway CANDIDATE: green-prepared stage (route stays 100% stable) -> same-source positive control via the ingress Gateway -> negative probe correlated with Cilium 'Policy denied' drops -> probe Pod deleted -> verified restoration to stable (requires this run's day7-baseline)
	$(DAY7_LOCK) env $(DAY7_ENV) python3 scripts/day7_validation_client_probe.py

day7-stable-check: ## Read-only, independent check that an experiment left the Day 7 release restored - Helm values == stable.yaml + the run's build image tags, no candidate objects, every stable container on the run's build, route 100% stable and current, Gateway current, external stable responses (run after EACH experiment)
	env $(DAY7_ENV) python3 scripts/day7_stable_check.py

day7-history-audit: ## OPTIONAL, never part of any Day 7 gate - Git/static history integrity (v0.6.0 tag, frozen Day 1-6 sources, unchanged workload sources vs v0.6.0) plus existence of the older kind clusters (kind get clusters only; never contacts them)
	python3 scripts/day7_history_audit.py

day7-plan: ## Read-only: print the day7-check / day7-final-gate step order parsed from this Makefile (runs nothing, takes no lock)
	python3 scripts/make_sequence.py day7-check day7-final-gate

day7-final-state-check: ## Read-only Day 7 final gate against the run's strategy baseline - Helm values (stable + the run's build)/manifest, no candidate leftovers, 3/3 + 3/3 + 1/1, every container on the run's build, unchanged workload/route/storage identities, external stable responses, no leaked probes/processes
	env $(DAY7_ENV) python3 scripts/day7_final_check.py

day7-resume-check: ## Read-only Day 7 health after a host/WSL/Docker restart - bounded node-Ready wait, then Cilium, context, mesh, then per-Pod ambient listeners BEFORE rollout readiness is trusted, then which build every container runs, then external routing
	$(MAKE) day7-nodes-ready
	$(DAY7_MAKE) cni-status
	$(DAY7_MAKE) context-check
	$(DAY7_MAKE) mesh-status
	$(DAY7_MAKE) ambient-workload-check
	$(DAY7_MAKE) rollout-check
	$(MAKE) day7-running-images
	$(DAY7_MAKE) gateway-check

day7-final-gate: ## Day 7 final restoration gate for one run (pass the run's DAY7_RUN_ID when invoked standalone) - platform health, listeners, NetworkPolicy and mesh behavior, suite /state + PVC/PV baseline, then day7-final-state-check
	$(DAY7_LOCK) sh -c '\
		$(DAY7_MAKE) cni-status && \
		$(DAY7_MAKE) context-check && \
		$(DAY7_MAKE) mesh-status && \
		$(DAY7_MAKE) ambient-workload-check && \
		$(DAY7_MAKE) rollout-check && \
		$(MAKE) day7-running-images && \
		$(DAY7_MAKE) gateway-check && \
		$(DAY7_MAKE) mesh-check && \
		$(DAY7_MAKE) networkpolicy-check && \
		$(DAY7_MAKE) final-state-check && \
		$(MAKE) day7-final-state-check \
	'

day7-check: ## Authoritative Day 7 sequence under ONE Day 7 lock - static checks, read-only preflight, image build + local digest check + verified build record (pinned content-derived tags), isolated cluster bootstrap, image load (incl. pinned tags) + per-node digest check (before deploy), stable deploy of the pinned build, listeners before rollout, running-image verification, private baselines recording the build, then Blue/Green, Canary, Recreate each followed by an independent stable check, then the final restoration gate; never touches maops-k8s-day6
	$(DAY7_LOCK) sh -c '\
		$(MAKE) tool-check && \
		$(MAKE) test && \
		$(MAKE) version-check && \
		$(MAKE) manifest-check && \
		$(MAKE) helm-lint && \
		$(MAKE) helm-template > /dev/null && \
		$(MAKE) helm-check && \
		$(MAKE) day7-preflight && \
		$(DAY7_MAKE) image-build && \
		$(MAKE) day7-image-verify-local && \
		$(MAKE) day7-build-record && \
		$(DAY7_MAKE) cluster-create && \
		$(DAY7_MAKE) gateway-api-install && \
		$(DAY7_MAKE) cni-install && \
		$(MAKE) day7-nodes-ready && \
		$(DAY7_MAKE) cni-status && \
		$(DAY7_MAKE) context-check && \
		$(DAY7_MAKE) mesh-install && \
		$(DAY7_MAKE) mesh-status && \
		$(DAY7_MAKE) image-load && \
		$(MAKE) day7-image-load && \
		$(MAKE) day7-image-verify-nodes && \
		$(DAY7_MAKE) storage-bootstrap && \
		$(DAY7_MAKE) storage-hardening-check && \
		$(DAY7_MAKE) namespace-apply && \
		$(DAY7_MAKE) secret-bootstrap && \
		$(DAY7_MAKE) gateway-apply && \
		$(MAKE) day7-deploy && \
		$(DAY7_MAKE) ambient-workload-check && \
		$(DAY7_MAKE) rollout-check && \
		$(MAKE) day7-running-images && \
		$(DAY7_MAKE) gateway-check && \
		$(DAY7_MAKE) mesh-check && \
		$(DAY7_MAKE) networkpolicy-check && \
		$(MAKE) day7-baseline-init && \
		$(DAY7_MAKE) state-check && \
		$(MAKE) day7-baseline && \
		$(MAKE) day7-blue-green && \
		$(MAKE) day7-stable-check && \
		$(MAKE) day7-canary && \
		$(MAKE) day7-stable-check && \
		$(MAKE) day7-recreate && \
		$(MAKE) day7-stable-check && \
		$(MAKE) day7-final-gate \
	'
	@echo ""
	@echo "PASS: day7-check completed - all three strategies ran and the stable state was independently verified"

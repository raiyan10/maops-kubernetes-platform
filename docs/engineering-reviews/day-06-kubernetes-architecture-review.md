# Day 6 / v0.6.0 — Independent Kubernetes Architecture Review

**Role:** `kubernetes-architect` (independent review — not the implementer; fresh subagent
context, run in parallel with the other four reviewers and never shown their conclusions).

**Date:** 2026-09-24.

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 6 / v0.6.0 — Helm packaging, Gateway API
routing (Istio as sole controller), Istio ambient mesh, Cilium coexistence, per-component
ConfigMap checksum rollouts. Branch `feature/day-6-helm-routing-mesh`, HEAD
`3b784a78fa41b807a509a32e7028c67bf0a28777`, complete unstaged Day 6 diff.

**Method:** Read-only. `git status`/`git diff`/untracked-file inventory; the chart (`Chart.yaml`,
`values.yaml`, `templates/`), `k8s/day6/`, `kind/cluster-day6.yaml`, `Makefile`,
`.github/workflows/ci.yml`, `README.md`, `docs/architecture.md`, `docs/roadmap.md`, `.claude/`.
Cluster-free re-runs: `python3 -m unittest discover -s tests`, `make manifest-check`,
`make helm-check`, `scripts/version_check.py`, `helm lint`, two `helm template` renders.
No file was edited; no cluster object was changed.

> **Adjudication note (added by the adjudicator, not the reviewer):** this reviewer used the
> default kubectl context (`kind-maops-k8s-day3`, not running) and therefore concluded no live
> cluster was reachable. The `maops-k8s-day6` cluster did exist and was reachable through its own
> kubeconfig; two other reviewers inspected it read-only. See
> `day-06-final-adjudication.md` (rejected/narrowed findings).

---

## 1. Verdict

**PASS WITH NON-BLOCKING NOTES.** The architecture itself is sound. The reviewer recommended that
release not proceed as a "live-validated" v0.6.0 until ARCH-1 is resolved.

## 2. Findings

| ID | Severity | File / reference | Evidence | Impact | Required remediation |
|---|---|---|---|---|---|
| ARCH-1 | HIGH | `docs/architecture.md:1` (title "live validation deferred"); `README.md:52-101` ("Day 6 status: static validation only … did **not** create or contact a kind cluster … or run `day6-check`"); `.claude/agents/cluster-integration-engineer.md:16` ("not yet released, live sequence not yet run"); `charts/…/templates/authorizationpolicy-gateway.yaml:15`; `k8s/day6/cilium-ambient-probe-policy.yaml:55-61`; `k8s/day6/gateway-values-configmap.yaml:53-70`; `.claude/skills/workload-security-validation/SKILL.md:257-272` | Every tracked document states live validation has not been executed and Day 6 is not released. CI is cluster-free by design. The reviewer could not reach a live cluster to check any claimed live result. | Direct contradiction between the live results reported for this review and the repository's own self-description. | Either update every "not yet executed / deferred / live sequence not yet run" statement and record the actual live evidence (per the `docs/engineering-reviews/` convention of Days 1-5), or do not treat the live claims as verified. |
| ARCH-2 | NOTE | `charts/…/templates/networkpolicy-allow-hbone.yaml:1-105` | The peerless, port-only (TCP 15008) HBONE NetworkPolicy combined with STRICT PeerAuthentication and three AuthorizationPolicies is well reasoned; the in-file comments document and reject the earlier `namespaceSelector: istio-system` approach. | None — a confirmed strength. | None. |

No BLOCKER, MEDIUM, or LOW findings.

## 3. Confirmed with no finding

- **Helm is the sole Day 6 application source.** `values.yaml:6-16`, `Chart.yaml:3`; `k8s/base`
  untouched since Day 5 (`522df4f`); `kubectl kustomize k8s/base` still renders 27 objects;
  `make manifest-check` 267/267.
- **Rendered inventory.** `helm lint`/`helm template` clean (icon info note only); two renders
  byte-identical; exactly 30 objects; zero Secret/Ingress/ClusterRole/waypoint; `make helm-check`
  202/202. No `dependencies:` in `Chart.yaml`.
- **Gateway API.** Single controller (`gatewayClassName: istio`); no Ingress anywhere;
  `allowedRoutes` scoped to `maops-platform`; no ReferenceGrant needed (the HTTPRoute's backend is
  same-namespace; only the parent Gateway is cross-namespace).
- **Ambient mesh.** No sidecar injection, no waypoint; STRICT PeerAuthentication namespace-wide;
  AuthorizationPolicies use `source.principals` only, never `to.operation`, with the L7
  unavailability explained.
- **Cilium coexistence** (`Makefile:151-168`): `kubeProxyReplacement=false`,
  `cni.exclusive=false`, `socketLB.hostNamespaceOnly=true`, hubble/envoy off, one operator replica.
- **Scope.** No HPA in the chart, no ClusterRole, no Recreate/Blue-Green/Canary, no Argo, no Cilium
  Gateway controller or L7 policy, no TLS or cloud LoadBalancer; the only NodePort is Istio's
  generated gateway Service; all application Services are ClusterIP; Secrets referenced by name.
- **Labels/selectors.** Consistent `app.kubernetes.io/*`; Service selectors include
  name + instance + component; verified against the actual render.
- **Checksum rollout.** Each workload hashes only its own ConfigMap template
  (`app-deployment.yaml:46`, `gateway-deployment.yaml:53`, `state-statefulset.yaml:26`);
  deterministic; three distinct values.
- **Ownership / duplication.** Two Deployments + one StatefulSet; no bare Pods; name overlap with
  `k8s/base` is by design (mutually exclusive deploy targets); diagnostics ServiceAccount created
  once (`k8s/day6/`), its Role/RoleBinding once (chart).
- **Run variables.** `DAY6_RUN_ID` / `DAY6_SUITE_BASELINE_PATH` only; one `DAY4_RUN_ID` mention in
  `scripts/suite_baseline.py` is historical context.
- **Static re-runs:** 1161 unit tests OK; manifest-check 267/267; helm-check 202/202;
  version-check 50/50.

## 4. Remaining live/environment limitations

- No live cluster was reachable from this reviewer's (default) context, so none of the claimed
  live results or live-discovered incidents could be verified independently.
- The `fsGroup` fix, the ambient-probe CiliumClusterwideNetworkPolicy, and the HBONE rule are
  architecturally sound on paper; runtime confirmation was outside what this reviewer could see.

## 5. Whether release may proceed

Not as a "live-validated" v0.6.0 until ARCH-1 is resolved — either commit the live evidence and
reconcile every stale statement, or label the release static-validation-only. No architecture
change is required either way.

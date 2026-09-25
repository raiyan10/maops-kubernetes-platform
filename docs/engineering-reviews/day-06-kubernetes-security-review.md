# Day 6 / v0.6.0 — Independent Kubernetes Security Review

**Role:** `kubernetes-security-reviewer` (independent review — not the implementer; fresh
subagent context, run in parallel with the other four reviewers and never shown their
conclusions).

**Date:** 2026-09-24.

**Scope:** Day 6 / v0.6.0 security posture — pod/container hardening, ServiceAccount identity and
token automount, RBAC scope, NetworkPolicy topology, STRICT mTLS and identity-scoped
AuthorizationPolicy, HBONE and CiliumClusterwideNetworkPolicy scoping, projected-Secret
ownership/mode, and diagnostic secret-disclosure paths. Branch `feature/day-6-helm-routing-mesh`,
HEAD `3b784a78fa41b807a509a32e7028c67bf0a28777`.

**Method:** Static review of the chart, `k8s/day6/`, scripts, and docs; cluster-free runs of the
unit tests, `make manifest-check`, `make helm-check`, `helm lint`, `helm template`. Read-only live
inspection of `maops-k8s-day6` through the cluster's own kubeconfig (obtained with
`kind get kubeconfig` into a scratch location outside the repository): `kubectl get -o json` on
non-Secret objects and `crictl inspect` on a node for runtime UID/GID. No apply, patch, delete, or
state-changing exec; Secret data never read.

---

## 1. Verdict

**PASS WITH NON-BLOCKING NOTES.** The reviewer said not to tag or release v0.6.0 until SEC-1 is
resolved.

## 2. Findings

| ID | Severity | File / reference | Evidence | Impact | Required remediation |
|---|---|---|---|---|---|
| SEC-1 | HIGH | `docs/architecture.md:1, :36, :1545-1575, :2358-2412`; `README.md:18-21, 87-100`; `docs/roadmap.md:181-183, 241-248`; `.claude/agents/kubernetes-security-reviewer.md:3`; `.claude/agents/cluster-integration-engineer.md:16`; `.claude/agents/kubernetes-architect.md:15-16`; `charts/…/templates/authorizationpolicy-gateway.yaml:15`; `k8s/day6/cilium-ambient-probe-policy.yaml:63-64`; `k8s/day6/gateway-values-configmap.yaml:52` | Tracked docs and agent descriptions say Day 6 is static-validation-only and live validation "has not yet been run", while `docs/architecture.md` itself narrates completed live remediation (e.g. the first live `mesh-check` at 39/42). Live read-only inspection shows Helm revision 7 `deployed` and every security object matching the spec. The shipped AuthorizationPolicy template comment still says "not yet executed". | A release from this branch would ship security-control manifests and docs that contradict themselves about whether live proof exists. | Reconcile `docs/architecture.md` (title, intro, deferred-sequence and "What Day 6 proves" sections), `README.md`, `docs/roadmap.md`, the `.claude/agents` descriptions, and the stale chart/`k8s/day6` comments with the actual final live result — or leave them accurately deferred and do not release. |
| SEC-2 | MEDIUM | `charts/…/templates/networkpolicy-allow-hbone.yaml:126-140` (rendered `maops-allow-hbone-ztunnel`) | The rule is peerless (`podSelector: {}`, port 15008 only). Confirmed live: `ingress:[{ports:[{port:15008}]}]`, same for egress. The reviewer's baseline expected "istio-system only". | Any Pod can reach TCP 15008 on `maops-platform` Pods. Compensated by STRICT mTLS + exact-principal AuthorizationPolicies (verified live); the template's rationale (ztunnel is host-networked) is technically sound. | Accept explicitly as a documented, compensated risk; add a note to the architecture trust-boundary summary. |
| SEC-3 | LOW | `k8s/day6/cilium-ambient-probe-policy.yaml`; only a comment reference in `scripts/kube.py:166` | The CCNP is correctly narrow live (`Valid=True`; namespace + part-of selector; `fromCIDR 169.254.7.127/32`; TCP 8080), but no static or live check protects its scope. | A future edit could broaden it silently. | Add an assertion (helm-check or dedicated script, or at minimum a unit test) pinning its selector, CIDR, and port. |
| SEC-4 | NOTE | `k8s/day6/gateway-values-configmap.yaml:27-29` vs live gateway Pod | The comment says the generated gateway proxy is granted `NET_BIND_SERVICE`. Live: Pod-level sysctl `net.ipv4.ip_unprivileged_port_start=0`; `istio-proxy` drops ALL capabilities with none added, no privilege escalation, read-only root filesystem, non-root 1337:1337. | Documentation inaccuracy only; the real control is stricter. | Correct the comment. |

## 3. Confirmed with no finding

- **PeerAuthentication:** exactly one, `maops-platform-strict-mtls`, namespace-wide, STRICT
  (template and live).
- **AuthorizationPolicy:** exactly three, `security.istio.io/v1`, ALLOW; live principals exact —
  gateway ← `…/maops-ingress/sa/maops-edge-istio`, app ← `…/maops-platform/sa/maops-gateway`,
  state ← `…/maops-platform/sa/maops-app`; no `to.operation`; the gateway principal is absent from
  the state policy and the diagnostics principal is absent from all three.
- **Pod hardening (live):** all three workloads `runAsNonRoot`, UID/GID 10001, `fsGroup` 10001 /
  `OnRootMismatch`, RuntimeDefault seccomp, no privilege escalation, `drop: [ALL]`, read-only root
  filesystem; `crictl inspect` confirms runtime 10001:10001 matching the Dockerfile.
- **ServiceAccounts:** one per workload, automount off at ServiceAccount and Pod level (also for
  `maops-edge-istio`); only `maops-diagnostics` automounts, applied from `k8s/day6/`, never Helm.
- **RBAC:** one Role (`get/list/watch` on pods, services, endpointslices; no secrets, no write, no
  wildcard), one RoleBinding; no `maops` ClusterRole/ClusterRoleBinding.
- **NetworkPolicy:** exactly 8 live; default-deny both directions; no validation-client → gateway
  allow; gateway ingress only from `maops-ingress` + `istio.io/gateway-name: maops-edge`; state
  ingress from app only; DNS egress to kube-dns only; nothing reaches the API server.
- **`networkpolicy_check.py`:** validation-client → gateway now asserted DENIED; `assert_denied()`
  accepts only `TCP_CONNECT_TIMEOUT`; probe Pods non-ambient, fully hardened, bounded
  (`activeDeadlineSeconds: 120`), run-scoped names, off the control plane.
- **`mesh_check.py`:** `TCP_CONNECTED` on a wrong-identity ambient probe is non-gating; denial is
  asserted only from correlated ztunnel evidence; `/livez` 200 is a hard failure; `RUST_LOG`
  handling is transactional (refuses `valueFrom`, restores in `finally`, verifies after restore).
- **Projected Secrets:** `defaultMode: 288` (0440) + `fsGroup` 10001 in all three templates and
  live; data never read.
- **`secret_bootstrap.py` / `secret_check.py`:** unchanged; tokens never printed; wrong/missing
  token → 403; response bodies never contain the token.
- **No Secret object rendered; no secret-like ConfigMap data; Secrets referenced by name only.**
- **Not pulled forward:** no waypoint, hubble, L7 CiliumNetworkPolicy, PSA labels, TLS, or cloud LB.
- **CI:** `permissions: contents: read`, no secrets, cluster-free.
- **Frozen files / credentials / run variables:** unchanged vs main; no credential patterns; Day 6
  variable names consistent.
- **Reproduced:** 1161 unit tests OK (printed `rollout restart` lines are mocked script output);
  manifest-check 267/267; helm-check 202/202; 30 rendered objects; live stack deployed at Helm
  revision 7.

## 4. Remaining live/environment limitations

- The live counts (37/37, 45/45, 24/24, 43/43) were not re-run; doing so would mutate the cluster.
- Secret volume contents were never inspected; mode/ownership verified from the Pod spec only.
- During the review the cluster showed transient post-restart ztunnel/CNI sandbox errors on
  `worker2` that self-resolved within about 20 seconds.

## 5. Whether release may proceed

Not until SEC-1 is resolved. SEC-2 and SEC-3 are non-blocking; SEC-4 is documentation-only. All
core security controls in scope are implemented correctly and verified statically and live.

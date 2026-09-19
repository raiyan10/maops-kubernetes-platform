# Day 5 / v0.5.0 — Independent Kubernetes Security Review

**Role:** `kubernetes-security-reviewer` (independent, adversarial review — not the implementer;
fresh subagent context).

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 5 / v0.5.0 — ServiceAccount/RBAC identity
boundaries and NetworkPolicy/Cilium network boundaries added on top of Day 4's unchanged
gateway/app/state security posture.

**Candidate/evidence references:**
- Repository: `~/DevOps-Portfolio/maops-kubernetes-platform`, branch `feature/day-5-security-boundaries`.
- Live cluster: `kind-maops-k8s-day5`, kubeconfig `~/.kube/maops-k8s-day5.config`, namespace
  `maops-platform` (+ `maops-day5-validation`).
- Prior review read for carried-forward debt: `docs/engineering-reviews/day-04-kubernetes-security-review.md`
  (specifically `DAY4-SEC-L1`, explicitly deferred to Day 5's NetworkPolicy work).

**Method:** Read every new/changed `k8s/base/*.yaml` (ServiceAccounts, Role/RoleBinding, both
Namespaces, all 7 NetworkPolicies), `gateway/server.py`, `app/server.py`, `scripts/day5_lock.py`,
`scripts/kube.py`, `scripts/rbac_check.py`, `scripts/networkpolicy_check.py`,
`scripts/secret_bootstrap.py`, `scripts/validate_manifests.py`, all three Dockerfiles,
`docs/architecture.md`/`docs/roadmap.md`. Live, read-only evidence: `kubectl get pod -o json` for
all three workloads (SA/automount/securityContext), `kubectl auth can-i` for the full diagnostics
permission matrix, real pod-to-pod TCP traffic tests (gateway↔app, gateway↔state, app↔state,
app↔gateway) exec'd from already-running Pods, `cilium status`/`cilium endpoint list`, two bounded
local `kubectl port-forward` GET-only tunnels (killed immediately after use, nothing applied to the
cluster). No mutating command was run; no object was created or deleted by this review beyond its
own transient port-forward tunnels.

---

## 1. ServiceAccount design and token automount — PASS

Live pod specs for gateway/app/state confirm `automountServiceAccountToken: false` actually took
effect (no `kube-api-access-*` projected volume in any of their `spec.volumes` — direct proof, not
inference from the declaration alone). `kubectl auth can-i` confirms zero bindings for all three
application SAs. `maops-diagnostics` correctly has `automountServiceAccountToken: true`, mounted
only into a short-lived, cleanup-guaranteed probe Pod, never a long-lived workload.

## 2. Diagnostics Role/RoleBinding least privilege — PASS

Live `auth can-i` matrix: ALLOWED on `get/list/watch pods/services/endpointslices` in
`maops-platform`; DENIED on Secrets, all write verbs, cross-namespace reads, cluster-scoped reads,
and a wildcard probe. Rendered `Role`/`RoleBinding` match exactly.

## 3. Default-deny + explicit NetworkPolicy paths — PASS, live-verified with real traffic

gateway→app: connected (allowed). gateway→state: timed out (denied — the previously-flagged risk
stays closed). app→state: connected (allowed). app→gateway: timed out (denied, no reverse path).
The only cross-namespace ingress into `maops-platform` correctly ANDs namespace + component-label
selectors, not ORs them.

## 4. DAY4-SEC-L1 re-adjudication — **REDUCED, not CLOSED** (see DAY5-SEC-M1)

## 5. Cilium enforcement — PASS, non-trivially confirmed

`cilium status --brief` → OK; `cilium endpoint list` shows real per-endpoint
`POLICY ENFORCEMENT: Enabled` for gateway/app pod endpoints. Both a real DENY path (gateway→state)
and two real ALLOW paths were exercised live — the strongest available proof enforcement is active,
not merely that CRDs exist unenforced. `kube-proxy` confirmed still running (no accidental
kube-proxy-replacement).

## 6. Local image build/load provenance — PASS

All three Dockerfiles pin the identical distroless digest; `USER 10001:10001` matches live
`runAsUser/runAsGroup`. Build/load path unchanged from Day 4 (no registry pull).
`imagePullPolicy: IfNotPresent` confirmed live on all three workloads.

## 7. Day5 lock + kubeconfig isolation — PASS

Kubeconfig isolation confirmed live and structurally: the ambient/default kubeconfig contains no
Day 4/5 context at all — Day 5's cluster is reachable only through its dedicated kubeconfig file, a
strong, load-bearing guarantee against cross-day credential/context leakage. `day5_lock.py` is a
correctly-designed, separate process-mutex concern (concurrent-invocation protection only).

## 8. Cleanup/restoration of scratch/diagnostic resources — PASS

Both probe scripts create exactly one uniquely-named ephemeral Pod, delete it in a guaranteed
`finally` block, and record cleanup failure explicitly rather than swallowing it. Live-confirmed:
zero stray probe Pods at review time.

## 9. Day 4 security posture unchanged — PASS

All Day 4 pod/container hardening (`runAsNonRoot`, UID/GID 10001, `allowPrivilegeEscalation: false`,
`capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem: true`)
re-confirmed live for all three workloads. No `Secret` object rendered anywhere in
`kubectl kustomize` output. `secret_bootstrap.py` re-read: no CLI-argument token exposure, no
print/log leakage, existing Secrets preserved not rotated. Secret volumes confirmed
`defaultMode: 288` (0440), `readOnly: true`.

## 10. New attack surface from `maops-day5-validation` — see DAY5-SEC-L1

---

## Findings

### DAY5-SEC-M1 (Medium)
**Title:** DAY4-SEC-L1's primary documented threat path (unauthenticated gateway `/state` reachable
via `kubectl port-forward`) is NOT closed by Day 5's NetworkPolicy — confirmed live.
**Evidence:** A local port-forward directly to a `maops-gateway` Pod (bypassing NetworkPolicy
entirely — `kubectl port-forward` tunnels via the API server → kubelet → container network
namespace, a path standard `NetworkPolicy`/Cilium eBPF enforcement does not see) reached
`GET /state` with `HTTP 200` and zero credential supplied. By contrast, real pod-to-pod traffic from
an unlabeled/arbitrary Pod to gateway was verified DENIED live.
**Impact:** Day 5's NetworkPolicy genuinely closes the pod-to-pod reachability sub-risk DAY4-SEC-L1
named (any Pod in the cluster reaching gateway) — live-verified. It does not, and architecturally
cannot, close the other sub-risk the same finding already named as the realistic one (the operator's
own bounded local port-forward) — that remains open, unchanged since Day 2/4, and is already
accurately disclosed in `docs/architecture.md`.
**Disposition:** **Re-adjudicated, not silently closed.** `docs/architecture.md`'s DAY4-SEC-L1 scope
note is updated to explicitly record this finding as REDUCED (pod-to-pod sub-risk closed,
live-verified) rather than CLOSED (port-forward sub-risk remains open by architecture, not by
omission). No code fix required at Day 5 scope — genuine closure requires application-layer
authentication ahead of `PUT /state`, explicitly out of Day 5's stated scope. Carried forward as a
tracked, non-blocking item for Day 6/7.

### DAY5-SEC-L1 (Low)
**Title:** `maops-day5-validation` namespace has zero NetworkPolicy coverage — unrestricted egress
for any Pod placed there.
**Evidence:** `kubectl get networkpolicy -n maops-day5-validation` → no resources found.
**Impact:** any Pod running in that namespace has fully open egress. Mitigating factors, confirmed
by design: the RBAC-token-bearing `diagnostics` probe Pod and the network-privileged
`validation-client` probe Pod are deliberately kept as two separate, non-overlapping, short-lived
(`activeDeadlineSeconds` 45s/120s) identities with guaranteed cleanup — no single ephemeral identity
combines a live API token with a network path into the application namespace.
**Disposition:** Not release-blocking for Day 5 given the roadmap's explicit `maops-platform`-only
default-deny scope and the mitigating separation-of-identity design. Recommended for Day 6/7:
narrow default-deny + explicit-allow inside `maops-day5-validation` too. Accepted, non-blocking.

### DAY5-SEC-I1 (Informational)
**Title:** Neither namespace carries Pod Security Admission (PSA) labels.
**Impact:** the (independently confirmed correct) securityContext hardening is enforced only by
convention/static check, not by API-server admission control. Not a Day 5 regression (true since
Day 1); not required by Day 5 roadmap scope. Candidate for future defense-in-depth.

### DAY5-SEC-I2 (Informational, operational not security)
**Title:** `cilium-operator` restarting frequently on leader-election lease renewal timeouts.
Duplicate of the architecture review's DAY5-ARCH-M2 — see that review for disposition. Affects only
the operator (IPAM/CRD reconciliation), not the per-node agent DaemonSet that actually enforces
policy (confirmed healthy: 39/39 controllers, live traffic tests all correct).

---

## Summary table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 1 | DAY5-SEC-M1 (re-adjudicated, documented, not code-fixable at Day 5 scope) |
| Low | 1 | DAY5-SEC-L1 (accepted) |
| Informational | 2 | DAY5-SEC-I1, DAY5-SEC-I2 |

**DAY4-SEC-L1 disposition: REDUCED, not CLOSED** (pod-to-pod reachability sub-risk closed and
live-verified; `kubectl port-forward` reachability sub-risk — the path the original finding itself
called realistic — remains open by architecture, already accurately disclosed).

## Verdict: **APPROVE WITH CONDITIONS**

Day 5's core deliverables — purpose-built ServiceAccounts with verified-live non-automount, a single
narrowly-scoped read-only RBAC grant proven live against both allow and deny cases, and a
comprehensive default-deny NetworkPolicy baseline with exactly the intended narrow allows (all
proven with real pod-to-pod traffic, not static inference) — are implemented correctly and match the
roadmap's stated scope. No Critical or High finding exists. Day 4's security baseline is unchanged
and independently re-confirmed live. DAY5-SEC-M1 is not a Day 5 regression — it is a live-confirmed
re-statement of a residual risk the project's own documentation already discloses correctly, now
explicitly re-adjudicated as REDUCED rather than silently marked resolved. **Condition for PR:**
this document (done) must record DAY4-SEC-L1 as reduced-not-closed with live evidence, and
DAY5-SEC-M1/L1 must be carried forward as tracked, non-blocking items — both satisfied by this
review.

---

PROJECT 4 DAY 5 KUBERNETES SECURITY REVIEW COMPLETE

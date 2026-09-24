# Day 6 / v0.6.0 — Independent Cluster Integration Review

**Role:** `cluster-integration-engineer` (independent review — not the implementer; fresh
subagent context, run in parallel with the other four reviewers and never shown their
conclusions). For this review the role's normal live-operation remit was explicitly overridden to
**read-only**.

**Date:** 2026-09-24 (live observations 09:19–09:22 +06).

**Scope:** Day 6 / v0.6.0 runtime behavior — kind topology and post-CNI ordering, bounded
readiness waits, Cilium/Istio install compatibility, Gateway controller-generated objects,
restart/recovery, storage identity and retention, lifecycle restoration, cleanup of every
mutating operation, and whether the live cluster is genuinely healthy and restored. Branch
`feature/day-6-helm-routing-mesh`, HEAD `3b784a78fa41b807a509a32e7028c67bf0a28777`.

**Method:** Source review of `kind/cluster-day6.yaml`, `Makefile`, `scripts/` (including
`kube.py`, `helm_lifecycle_check.py`, `final_state_check.py`, `suite_baseline.py`). Read-only live
inspection: `kind get clusters`, `docker ps`, `kubectl get/describe -o json` on non-Secret objects,
events, `helm history`, `ps` for port-forwards. No mutating command, no live check re-run.
`make final-state-check` (read-only by source inspection) was blocked by the session's
auto-mode classifier, so equivalent manual reads were used instead.

---

## 1. Verdict

**FAIL** — on the release-readiness gate (INT-1). Runtime and cluster health are genuinely good;
every spot-checkable live claim matched.

## 2. Findings

| ID | Severity | File / reference | Evidence | Impact | Required remediation |
|---|---|---|---|---|---|
| INT-1 | BLOCKER | `docs/architecture.md:1, 34-36, 2328-2331`; `docs/roadmap.md:240-249`; `Makefile:309` | The repo states the Day 6 live sequence "has not yet been run" and Day 6 is not tagged "until that live evidence exists" (`roadmap.md:247-249`, the project's own release gate). `Makefile:309` help says "not yet executed". `architecture.md:36` points to a "DAY6: released validation record" section that does not exist (only the DAY5 one at :1059). `architecture.md:2328-2331` defers completion to "this pass's own remediation report", which does not exist. Yet live `helm history` shows 7 revisions — revision 1 failed (progress deadline, consistent with the pre-`fsGroup` failure), revisions 5 and 6 upgrades, revision 7 "Rollback to 5" (deployed) — and the PVC UID `6c5fdacc-090a-4208-9b52-9c594211a982` / PV UID `df840301-f5f1-4d8b-9d70-63597612e2fe` match exactly. | Committed docs misstate the state of the world; none of the claimed live counts are recorded anywhere in the tree. By the project's own discipline v0.6.0 cannot be tagged until a committed evidence record exists and the stale language is reconciled. | Commit a Day 6 evidence record; fix the title, the dangling reference, the roadmap, and `Makefile:309`; re-verify the counts against a documented run. |
| INT-2 | MEDIUM | `Makefile:193-212` (`mesh-install`, istiod install) | istiod installed without disabling autoscaling. Live: `istiod` HPA, min 1 / max 5, `cpu: <unknown>/80%`, with repeated `FailedGetResourceMetric` / `FailedComputeMetricsReplicas` events (no metrics-server). | HPA is out of Day 6/7 scope per `roadmap.md:233`; this object is untracked by any check and undocumented. | Add `--set pilot.autoscaleEnabled=false`, or document it as an accepted chart default. |
| INT-3 | NOTE | Live observation, 09:19–09:22 +06 | After a host/Docker restart roughly 12–17 minutes earlier: `ztunnel-z26j9` 0/1 Unknown, 2/3 app and 2/3 gateway Pods Unknown, `FailedCreatePodSandBox` (`no ztunnel connection`, `Cilium API client timeout exceeded`, `putEndpointIdTooManyRequests` 429). Within about 2 minutes, with no intervention, everything converged to Ready through kubelet sandbox retry — no Pod replacement. | Confirms restart fragility is real and that recovery is not uniform (the earlier incident needed a Pod replacement); supports the "not a proven ztunnel bug" caution. | No code change; add a line to the restart-recovery documentation. |

## 3. Confirmed with no finding (including read-only live observations)

- **kind topology:** 1 control-plane + 2 workers, `kindest/node:v1.36.1@sha256:3489c76…` on all
  three, `disableDefaultCNI: true`, `127.0.0.1:18080 → 30080` on the control plane only; only the
  Day 6 containers running; all three nodes Ready.
- **Post-CNI ordering:** `day6-check` runs cluster-create → gateway-api-install → cni-install →
  cni-status → context-check → mesh-install → mesh-status; every install is followed by a bounded
  `rollout status` (Cilium 180 s / operator 120 s; istiod, istio-cni-node, ztunnel 120 s each),
  read-only diagnostics on failure, and `exit 1`.
- **`verify_context()`** (`scripts/kube.py:216-258`) fails closed on context and node-name
  mismatch.
- **`cluster-delete`** deletes only `maops-k8s-day6`, lock-wrapped.
- **Cilium/Istio install:** Cilium pinned 1.20.1 with `kubeProxyReplacement=false`,
  `socketLB.hostNamespaceOnly=true`, envoy/hubble off, one operator replica; live kube-proxy 3/3,
  Cilium 3/3, operator 1/1.
- **Gateway-generated objects:** `deployment/maops-edge-istio` 1/1, `service/maops-edge-istio`
  (NodePort 80:30080, 15021), `serviceaccount/maops-edge-istio`.
- **Gateway/HTTPRoute:** Gateway `Accepted`, `Programmed`, `ResolvedRefs` true; listener attached
  routes 1; HTTPRoute `Accepted`, `ResolvedRefs`, `ResolvedWaypoints` true.
- **Storage identity:** PVC and PV UIDs match the claimed values exactly.
- **RBAC:** `maops-diagnostics-reader` Role/RoleBinding in `maops-platform` only, read-only verbs;
  no cluster-scoped binding references a `maops` subject.
- **NetworkPolicy:** exactly the 8 expected objects; no validation-client → gateway allow.
- **PDBs:** app and gateway `minAvailable: 2`, `disruptionsAllowed: 1`; none for single-replica
  state.
- **Secrets:** Opaque, no ownerReferences, not Helm-owned (contents not read).
- **No leaks:** no mesh-probe namespace, no NetworkPolicy probe Pods, empty validation namespace,
  no port-forward processes; all ztunnel Pods `RUST_LOG=info`; no Ingress, sidecar, or waypoint.
- **Frozen artifacts and run variables:** unchanged; `DAY6_*` used consistently.
- **Git hygiene:** only the expected Day 6 set; nothing staged; HEAD as expected.

## 4. Remaining live/environment limitations

- The mutating live checks were not re-run; the claimed counts are corroborated only indirectly
  (object inventory, Helm history, PVC/PV identity).
- `make final-state-check` was blocked by the classifier; manual reads substituted.
- Cleanup logic in `scaling_check.py`, `rolling_update_check.py`, and `pdb_check.py` was not
  reviewed line by line (inferred from the absence of leaked artifacts).
- Host restarts may recur; no code change addresses restart recovery.

## 5. Whether release may proceed

**Not yet**, on the project's own gate (`roadmap.md:247-249`): a Day 6 evidence record must be
committed and the "deferred / not yet run" language reconciled (INT-1), and INT-2 fixed or
documented. The reviewer expected a clean result after that.

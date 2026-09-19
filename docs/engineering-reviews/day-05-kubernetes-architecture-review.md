# Day 5 / v0.5.0 — Independent Kubernetes Architecture Review

**Role:** `kubernetes-architect` (independent review; fresh subagent context, no memory of any
prior conversation).

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 5 / v0.5.0 — identity and network
boundaries added on top of Day 4's unchanged gateway/app/state architecture: a purpose-built
ServiceAccount per workload (all `automountServiceAccountToken: false`, bound to nothing), a
second namespace `maops-day5-validation` holding `maops-diagnostics` (a namespace-scoped,
read-only Role/RoleBinding), and a `networking.k8s.io/v1` NetworkPolicy set (default-deny plus six
narrow allows) enforced by Cilium replacing kindnet as the CNI.

**Candidate/evidence references:**
- Repository: `~/DevOps-Portfolio/maops-kubernetes-platform`, branch `feature/day-5-security-boundaries`.
- Live cluster: `kind-maops-k8s-day5` via its dedicated kubeconfig `~/.kube/maops-k8s-day5.config`.
- Authoritative full run: `/tmp/maops-day5-final-check.CZBKEB.log` (773 tests, 267/267 manifest,
  full Day 5 sequence green, `MAKE_EXIT_CODE=0`).

**Method:** Read `docs/roadmap.md`/`docs/architecture.md` in full; read every new/changed file
under `k8s/base/` (ServiceAccounts, Role/RoleBinding, both Namespaces, all 7 NetworkPolicy
objects), `kind/cluster-day5.yaml` vs `kind/cluster.yaml`, `scripts/day5_lock.py`, `scripts/kube.py`,
the relevant `Makefile` targets, and `scripts/networkpolicy_check.py`. Rendered
`kubectl kustomize k8s/base` (27 objects) and independently re-ran `python3 scripts/manifest_check.py
k8s/base` (267/267). Diffed every Day-4-owned manifest and all application code/Dockerfiles against
the Day 4 merge commit (`0cb6d90`) for zero unintended drift. Ran read-only live checks: node/pod
status, `kubectl auth can-i --as=system:serviceaccount:maops-day5-validation:maops-diagnostics` (7
probes), a real in-cluster TCP test from an existing app Pod to `maops-state:8080` (succeeded) and
from an existing gateway Pod to `maops-state:8080` (blocked), `helm -n kube-system get
values/history/status cilium`, `docker stats` on the three node containers, and `kubectl logs
--previous` for `cilium-operator`. No object was created; only existing Pods were exec'd into; no
mutating command was run.

---

## 1. ServiceAccount design — no finding

Per-workload SA (`app`, `gateway`, `state`), `automountServiceAccountToken: false` at both SA and
Pod level, `serviceAccountName` wired correctly, none appears as any RoleBinding subject (confirmed
statically and live — no other Role/RoleBinding exists). "Binding to nothing" holds up: even a
future accidental automount flip would still find zero RBAC grants attached.

## 2. Diagnostics Role/RoleBinding — no finding

`diagnostics-role.yaml` grants exactly `get/list/watch` on `pods`/`services`/`endpointslices` in
`maops-platform` — nothing else. Live `auth can-i` matrix (7 probes) matches the rendered grant
exactly: allowed on the three intended resource types, denied on Secrets, writes, cross-namespace,
and cluster-scoped reads.

## 3. NetworkPolicy topology — no finding, live-verified

7 rendered NetworkPolicy objects match the intended default-deny + DNS + gateway↔app +
app↔state + validation-client→gateway shape. Static negative checks
(`gateway_egress_app.never_targets_state`, `state_ingress_app.never_allows_gateway`,
`*.never_allows_validation_namespace`) all pass, and the critical negative case was independently
reproduced live: app→state connected immediately; gateway→state timed out (Cilium silent drop).

## 4. Cilium CNI choice and kind integration — 3 findings (see below)

Architecturally sound choice overall (native-tooling ground rule respected — Helm is a sanctioned
CLI, no `CiliumNetworkPolicy` anywhere, standard `networking.k8s.io/v1` objects only), but live
inspection surfaced concrete drift and stability issues — **DAY5-ARCH-M1**, **DAY5-ARCH-M2**,
**DAY5-ARCH-L1** below.

## 5. Day5 lock and kubeconfig isolation — no finding

`scripts/day5_lock.py` is a verified-fd-inheritance `flock()` mutex (concurrent-invocation
protection). Cross-day isolation is actually carried by `scripts/kube.py`'s dedicated
`--kubeconfig`/`--context` threading, confirmed live: the default kubeconfig has no
`kind-maops-k8s-day5` context at all.

## 6. `maops-day5-validation` namespace design — 1 finding (Low, see DAY5-ARCH-L2)

Correct architectural choice (keeps `maops-platform`'s default-deny pure, makes the diagnostics
grant an explicit cross-namespace subject). Live-confirmed properly isolated from the application
side. One gap: the namespace carries no NetworkPolicy of its own (see DAY5-ARCH-L2).

## 7. Day 4 architecture preservation — no finding

`git diff 0cb6d90` on every Day-4-owned manifest shows only the expected version/instance label
bump, ConfigMap value bump, image tag bump, and the additive `serviceAccountName` field. Zero diff
on all three Dockerfiles and `server.py` files. Security contexts, probes, resources,
topologySpreadConstraints, PDBs, and the StatefulSet's retention policy are byte-for-byte unchanged
in shape.

## 8. Stage discipline — no finding

No app Helm chart, no CI, no Ingress/HPA/`CiliumNetworkPolicy`/Hubble anywhere in the diff.
`FORBIDDEN_KINDS` in `validate_manifests.py` still correctly forbids Secret/Ingress/PVC/
ClusterRole/ClusterRoleBinding/HPA, confirmed zero matches in rendered output. `kind/cluster.yaml`
(Day 4) untouched; Day 5 introduces its own separate `kind/cluster-day5.yaml`.

## 9. Memory/resource assumptions — 1 finding (Medium, see DAY5-ARCH-M3)

---

## Findings

### DAY5-ARCH-M1 (Medium)
**Title:** `make cni-install`'s committed `--set` flags do not reproduce the live cluster's actual
Helm values.
**Evidence:** `helm -n kube-system get values cilium` on the live cluster shows user-supplied
`hubble.enabled: false` and `image.pullPolicy: IfNotPresent` — neither is in the Makefile recipe as
committed at review time (only `ipam.mode=kubernetes`/`kubeProxyReplacement=false`).
**Impact:** running the committed `cni-install` fresh would not reproduce the state this review
(and the authoritative run) actually validated against, contradicting the project's own "Makefile is
the authoritative local interface" ground rule.
**Disposition:** **Remediated.** `Makefile`'s `cni-install` target now includes
`--set hubble.enabled=false --set image.pullPolicy=IfNotPresent`, matching the live release's actual
user-supplied values. No live rerun required — this change makes the recipe match already-validated
behavior, not change it.

### DAY5-ARCH-M2 (Medium)
**Title:** Live Cilium Helm release is `STATUS: failed`; `cilium-operator` is crash-looping on
leader-election lease renewal.
**Evidence:** `helm status cilium` reports `STATUS: failed` (DaemonSet/Deployment readiness timeout
during install); `cilium-operator` pods show 13–15 restarts over ~8h with
`"Failed to update lease" ... context deadline exceeded` → `"Leader election lost, shutting down."`
**Impact:** the Cilium *agent* DaemonSet (which actually enforces NetworkPolicy in the eBPF
datapath) is healthy and enforcement is proven live (real ALLOW/DENY traffic tests both succeed);
this is an operator-only, HA-leader-election symptom consistent with host resource pressure (see
DAY5-ARCH-M3 / DAY5-INT-C1), not a config defect that breaks Day 5's functional claims.
**Disposition:** **Accepted as a documented limitation**, not remediated as a code change this pass
— root cause is host resource contention (see the cluster-integration review's DAY5-INT-C1), which
this review has no authority to fix (starting/stopping Day 1-4 clusters or reconfiguring HA replica
counts requires a live rerun this task explicitly scopes out). Recorded in `docs/architecture.md`.

### DAY5-ARCH-M3 (Medium)
**Title:** Resource footprint of the 3-node Cilium-enabled Day 5 cluster is undocumented and,
per live evidence, already fragile on this host.
**Evidence:** `docker stats` shows the control-plane node container at 38% CPU/1.06GiB at rest —
Cilium roughly doubles Day 5's `kube-system` pod count versus Day 1-4's kindnet baseline (agent +
envoy + 2-replica operator, per node/cluster).
**Disposition:** **Accepted as a documented limitation.** `docs/architecture.md` now carries an
explicit local-resource-sizing note (mirroring the Day 4 `DAY4-ARCH-L1` pattern) rather than a code
fix — reducing cluster count or operator replicas is an environment/operating-practice decision, not
a manifest change, and is explicitly out of this bounded remediation's scope.

### DAY5-ARCH-L1 (Low)
**Title:** `cilium-envoy` L7 dataplane runs by chart default, unacknowledged in
`docs/architecture.md`'s "capabilities deliberately not reached for" list.
**Evidence:** `cilium-config` ConfigMap shows `enable-l7-proxy: "true"`; a live `cilium-envoy`
DaemonSet runs 3/3 Ready on every node. No `CiliumNetworkPolicy` L7 rule exists anywhere (zero
matches), so this is not an actual security gap — standard L3/L4 NetworkPolicy enforcement via
Cilium's eBPF datapath does not require Envoy at all.
**Disposition:** Documentation-only gap; noted as accepted, non-blocking debt (candidate for Day 6/7
`--set envoy.enabled=false` if the added footprint becomes a problem).

### DAY5-ARCH-L2 (Low)
**Title:** `maops-day5-validation` namespace carries no NetworkPolicy of its own.
**Evidence:** `kubectl -n maops-day5-validation get networkpolicy` returns nothing; all 7 rendered
NetworkPolicy objects are scoped to `maops-platform` only.
**Impact:** consistent with the roadmap's explicit Day 5 scope (default-deny is `maops-platform`-
only); not a defect. Duplicate of the security review's DAY5-SEC-L1 — see that review for the fuller
mitigating-design analysis (separate RBAC-token identity vs. network-privileged identity, both
short-lived with guaranteed cleanup).
**Disposition:** Accepted, non-blocking, carried forward as a Day 6/7 candidate.

---

## Summary table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 3 | DAY5-ARCH-M1 (remediated), DAY5-ARCH-M2 (accepted/documented), DAY5-ARCH-M3 (accepted/documented) |
| Low | 2 | DAY5-ARCH-L1, DAY5-ARCH-L2 |
| Informational | 0 | — |

## Verdict: **APPROVE WITH CONDITIONS**

The core Day 5 design — per-workload ServiceAccounts bound to nothing, one narrowly-scoped
diagnostics Role/RoleBinding, default-deny-plus-six-narrow-allows NetworkPolicy topology, Cilium as
a standard-NetworkPolicy-only enforcing CNI, a genuinely separate validation namespace, and
byte-for-byte preservation of Day 4's workload architecture — is sound, internally consistent, and
independently reproduced live, not merely read from manifests. Nothing here blocks the manifest/RBAC/
NetworkPolicy design as committed. DAY5-ARCH-M1 (Makefile/live-Helm-value drift) has been
remediated in this review pass; DAY5-ARCH-M2/M3 are accepted, explicitly documented local-
environment limitations, not implementation defects.

---

PROJECT 4 DAY 5 KUBERNETES ARCHITECTURE REVIEW COMPLETE

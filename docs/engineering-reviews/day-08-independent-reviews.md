# Day 8 (v1.0.0 work) - independent reviews

Five independent, review-only agents examined the **unstaged** branch
`feature/day-8-autoscaling-hardening` against base
`3d19075e483d402179fe33d0c4fb0d7357a681b8`, all on 2026-10-03.

- **Evidence index for every reviewer:** the v2 review package
  `maops-day8-final-review-v2-3d19075.patch` (sha256 `07bc3f3b…`) and
  `maops-day8-final-review-v2-note.md`. The authoritative live run at
  review time was `140a5016313048f49386813c7a8daace` (exit 0).
- **Agents:** each ran as the project's existing agent definition, with its
  existing permissions. None edited files, mutated a cluster, committed or
  released.
- **What this file is:** the reviews' outputs, preserved verbatim below,
  except that each report's leading indentation was removed. Decisions are
  in `day-08-final-adjudication.md`; the changes made in response are in
  `day-08-remediation-log.md`.

| # | Agent | Recommendation | Highest finding |
|---|---|---|---|
| 1 | `kubernetes-architect` | GO | 1 MEDIUM (SubjectAccessReview breadth) |
| 2 | `kubernetes-security-reviewer` | GO | 1 MEDIUM (no ownership check on a pre-existing `keda` release/namespace) |
| 3 | `cluster-integration-engineer` | GO | 2 MEDIUM (preflight KEDA check; interrupted-run recovery) |
| 4 | `kubernetes-test-engineer` | GO, conditional on its finding 1 | 1 HIGH (CRD guard's all-namespace claim untested) |
| 5 | `release-engineer` | NO-GO for PR until adjudicated, fixed and VERSION decided | 4 MEDIUM |

---

## 1. kubernetes-architect

```
Day 8 architecture review (static, no cluster contact, no files modified). Verified: `python3 -m unittest tests.test_day8` OK (118 tests); `make day8-static-check` PASS 5/5 (quota 710m/544Mi requests, 2000m/1088Mi limits, 13 pods); `git diff 3d19075 --stat` shows only the 6 expected tracked files (Makefile/make_sequence.py additive, README/roadmap/CLAUDE.md doc edits, roadmap a few doc-line replacements); no `k8s/`, `charts/`, `helm-values/day7/` or Day 1-7 record file touched.

## Findings (none BLOCKER/HIGH)

**1. MEDIUM - SubjectAccessReview coverage is narrower than the "nothing can reach maops-platform" claim**
- File: scripts/day8_addons.py:192-206 (FORBIDDEN_IN_APP, OPERATOR_IN_SCALING), :265-286 (_record_keda_rbac).
- Evidence: SARs are run only for the 3 KEDA SAs, only in `maops-platform`, and only for 8 verbs (get/list/watch secrets, patch/update deployments/scale, patch statefulsets/scale, patch deployments, create HPAs). Nothing equivalent exists for the VPA (recommender, admission-controller) or Metrics Server identities - their guarantee rests on "updater absent + --vpa-object-namespace + webhook selector", which is verified, but effective RBAC is not. No check of `delete`, `create/patch/delete pods`, `pods/exec`, `pods/eviction`, `configmaps`, `bind/escalate/impersonate`, or secrets in other sensitive namespaces (istio-system, kube-system). The remaining KEDA cluster-wide grants (limitation #4 in the note: keda-operator-minimal can patch APIServices/ValidatingWebhookConfigurations; keda-operator-webhook can read Deployments/StatefulSets) are described but not probed.
- Correction: extend the matrix cheaply (SARs cost nothing): add `delete`/`update` deployments/statefulsets, `create/delete/patch pods`, `create pods/exec`, `create pods/eviction`, `get/list secrets` in istio-system and kube-system; add a short matrix for the VPA and Metrics Server SAs in maops-platform (deny all writes + deny get secrets); and record the known KEDA cluster-wide grants (`patch apiservices`, `patch validatingwebhookconfigurations`) as an explicit expected-yes allow-list in evidence, so the exposure is measured rather than only described. Word the guarantee in docs as "no Secret read or scale/patch of application workloads in maops-platform for KEDA", not a general isolation claim.

**2. LOW - KEDA admission webhook failurePolicy is unasserted**
- File: helm-values/day8/keda.yaml and static_problems in scripts/day8_addons.py:107-112.
- Evidence: The cluster-scoped `keda-admission` ValidatingWebhookConfiguration is live cluster-wide during a run (and if cleanup keeps KEDA for investigation). Only VPA's webhook is asserted `failurePolicy: Ignore` + namespace-scoped; KEDA's value is the chart default and not pinned or checked.
- Correction: set `webhooks.failurePolicy: Ignore` explicitly in keda.yaml and add it to `static_problems` (and optionally verify live in `check active`).

**3. LOW - Demo failure + automatic cleanup loses live diagnostics**
- File: Makefile `day8-check` group (demo=$$?; day8-cleanup).
- Evidence: After a failed phase, cleanup deletes the namespace; pods/events/logs of the failing phase are gone. Only the per-phase JSON evidence survives (and `finish()` writes no events/logs on early-return paths).
- Correction: on non-zero `demo`, have the Makefile capture `kubectl -n maops-day8-scaling get events,pods -o wide` and `describe` into the run directory (a new `day8_scaling.py snapshot` subcommand writing O_EXCL 0600) before `day8-cleanup`. Do not run it on success.

**4. LOW - Budget "no padding" / "can never sit Pending" slightly overstated**
- File: scripts/day8_objects.py:20-25 and :120-128 (WORKLOADS `day8-quota-in-budget`); docs/architecture.md ("so an expected scale-out can never sit Pending for lack of node capacity").
- Evidence: the quota-proof pod (1 pod, 50m/32Mi req, 100m/64Mi lim) never coexists with the other workloads (phase `quota` runs before `hpa`/`vpa`/`keda`), so it is padding, contrary to the docstring. No allowance for Terminating-pod overlap (5 s grace) during scale flapping - covered in practice only because phases are sequential. Preflight checks request headroom once, summed across workers (no per-node bin-packing; add-on pods double-counted on re-runs; each kind node reports the host's full CPU, so the sum overstates real capacity). Quota-block detection is event-based (`FailedCreate` + "exceeded quota", `quota_blocked`) - adequate, but events expire (~1 h).
- Correction: either drop the probe from `WORKLOADS` (the over-quota proof would still hold: 3x250m=750m > 640m - keep the quota math derived) or state in the docstring/docs that it is a deliberate, tiny non-concurrent allowance; soften "can never sit Pending" to "preflight verifies request headroom at start".

**5. LOW - Hard-coded KEDA inventory has no drift guard**
- File: scripts/day8_addons.py:311-340 (KEDA_CLUSTER_OBJECTS / KEDA_NAMESPACED_OBJECTS).
- Evidence: The 30 chart-owned objects are a static list for keda-2.21.0. A chart bump would silently make `record_keda_absent` verify the wrong set (today pinned, and Helm uninstall removes everything in the manifest anyway).
- Correction: in `uninstall_keda`, before uninstall, `helm get manifest keda` (no Secret contents), parse kinds/names, and assert inventory == manifest (or record the manifest object list in evidence for diffing). At minimum add a comment that a chart version bump requires regenerating the list.

**6. LOW - Fragile NotFound matching in kubectl_json_or_none**
- File: scripts/day8_common.py:77-85.
- Evidence: returns None on `"NotFound" in stderr or "not found" in stderr` - the lowercase substring is looser than `get_state`'s `(NotFound)` marker and could match unrelated errors. Used for the namespace identity guard in cleanup/require_guards/guards.
- Correction: match `(NotFound)` only, consistent with `get_state`.

**7. LOW - Unpack of malformed custom-columns output not wrapped**
- File: scripts/day8_scaling.py:667 (`_crd_instances`, `line.split(None, 1)`).
- Evidence: a malformed line raises ValueError instead of Day8Error; still fails closed via `main()` (exit 1), but bypasses the problem record and "KEDA and namespace kept" message.
- Correction: wrap the unpack and raise `Day8Error` so it is reported by `crd_instance_problems` as unreadable.

**8. LOW - Doc staleness**
- File: docs/roadmap.md:376 ("Cleanup deletes only that namespace;") contradicts the current cleanup, which also uninstalls KEDA and its CRDs (README and architecture are correct).
- Correction: change to "Cleanup removes that namespace and uninstalls KEDA".

**9. LOW - Chart/image provenance**
- File: Makefile `day8-addons-install`, IMAGES in scripts/day8_addons.py:58-62.
- Evidence: charts fetched from `--repo` at a pinned version without digest/`--verify`, on every run; images verified by tag only.
- Correction: acceptable for a local reference platform; state as a limitation (optionally pin the chart sha256 via `helm pull` + `sha256sum`, or image digests in values).

## Focus-area results with no actionable finding
- 1. Isolation: `scaler_conflicts` enforces one scaler/target and nothing outside the namespace; VPA updater disabled, flags and webhook scoping asserted statically and live (webhook `namespaceSelector` uses the immutable `kubernetes.io/metadata.name` label, failurePolicy Ignore); KEDA `watchNamespace` asserted statically and live (WATCH_NAMESPACE, CRB allow-list, scoped RB presence/absence, 24/24 SAR denials, positive control). `parse_can_i` never reads an error as "no".
- 2. Budgets: math correct (`vpa_ceiling` -> 40m/96Mi req, 200m/192Mi lim, within LimitRange max 250m/256Mi; `over_quota_pod` 3x250m=750m > 710m; per-container LimitRange compliance of the proof statically asserted). Preflight formula adds up (225m/432Mi add-on requests + budget).
- 3. Lifecycle: ordering enforced both in the Makefile (guards precede install) and inside the `day8-addons-install` recipe (shell precondition); Helm revision growth bounded by the Helm default (`--history-max 10`, observed at 10); KEDA fresh rev 1 each run; metrics-server/VPA intentionally persistent; CRDs handled as described.
- 4. Cleanup: fail-closed paths sound (all-namespace CRD guard, labels-only Secret, explicit-NotFound inventory); idempotent; order (release objects -> uninstall -> namespace) correct; failures in step 1/2 keep the namespace; `day8-check` always attempts cleanup and fails if either demo or cleanup failed; `day8-addons-final-check` accurately expects KEDA absent and namespace absent, metrics-server/VPA remaining verified.
- 5. Day 7 preservation: Day 8 runs under the day7 profile with `require_cluster_profile()` guards in every `common` helper; Day 7 lock on mutating targets; private Day 8 run dir; scaling image recorded outside the Day 7 build store; strict stable baseline (Helm revision/values/manifest digest, workload UIDs/template digests, Pod UIDs/images/imageIDs/restarts/resources, route, PVC/PV UIDs + state.json sha256, external responses, zero autoscaling controls in maops-platform). Minor gap, not worth a finding: live drift of Services/NetworkPolicies/mesh policies is not diffed, only exercised by mesh-check/networkpolicy-check in the final gate.

## Accepted limitations
`--kubelet-insecure-tls` on Metrics Server (kind self-signed kubelet certs); one operator restart on fresh KEDA install; empty `keda` namespace retained between runs; KEDA re-installed each run (network to chart repo/ghcr.io); KEDA chart-level cluster grants during a run (removed by cleanup); cleanup intentionally halts when a foreign KEDA CRD instance is found; host/Docker restart mid-run needs manual `make day8-cleanup`; Redis without auth (NetworkPolicy-only, disposable); short VPA recommendation history and `Initial` only at Pod creation; hard-kill can lose an in-flight worker item; tag-only image pinning for add-ons.

## Recommendation: GO
Open the PR. The isolation design is layered and verified both statically and live, the quota is single-source-derived and proven with admission-level rejections, the cleanup is fail-closed, idempotent and partial-state safe (proven live in run 140a5016..., exit 0), and Day 7 is demonstrably untouched (strict baseline diff, no Day 1-7 source changes). 118 tests and the static check pass. The findings are hardening/documentation items; finding 1 (broader SAR coverage, plus VPA/Metrics Server identities) is the one I would do before or immediately after the PR because it makes the isolation claim fully evidence-backed. Findings 2, 3, 6, 7 and 8 are small, low-risk and worth folding in; nothing blocks the PR.
```

---

## 2. kubernetes-security-reviewer

```
DAY 8 SECURITY REVIEW (review-only; nothing edited, no cluster contact, no Secret reads)
Ran: python3 -m unittest tests.test_day8 (118 OK), make day8-static-check (PASS). Static review only; live run 140a5016... relied on via the note, not re-verified.

RECOMMENDATION: GO for opening the PR. No BLOCKER/HIGH. The MEDIUM and LOW items are hardening/consistency fixes that can follow in the PR or a follow-up.

ACTIONABLE FINDINGS (most severe first)

1. MEDIUM (VERIFIED in code) - No ownership check on add-on Helm releases. Makefile `day8-addons-install` (diff ~lines 715-729) and scripts/day8_scaling.py:700-721 (`uninstall_keda`). Evidence: `helm upgrade --install keda ... --reset-values` and `helm_release_state("keda","keda")` key on name/namespace only. A pre-existing non-Day-8 `keda` release in namespace `keda` would be reset/upgraded by install and then uninstalled by cleanup. The six-CRD emptiness guard stops instance data loss, but not loss of the foreign release itself. `day8_preflight.py` checks only that the scaling namespace is absent; it does not check `keda`, `kube-system/metrics-server` or `vpa-system`, and `--create-namespace` silently adopts an existing `keda`/`vpa-system`. Correction: add a preflight that no `keda` release exists (or that any existing one was created by Day 8); verify chart name/version and a Day 8 marker before `helm uninstall`; refuse an existing `keda` namespace without a Day 8 label.

2. LOW (VERIFIED) - RBAC probe matrix is narrower than the claim "KEDA cannot read Secrets / scale workloads". scripts/day8_addons.py:192-206, can_i at 257-262. Forbidden probes are only in `maops-platform` (`kube_namespace()`); there is no `-A`/`--all-namespaces` probe and none in kube-system/istio-system/maops-day7-validation. Only the 3 SA subjects (keda-operator, keda-metrics-server, keda-webhook) are probed - not other possible KEDA subjects (group subjects such as system:serviceaccounts). The `keda_binding_problems` allow-list is by-name only and does not inspect ClusterRole rules; `_keda_subjects` matches only a ServiceAccount subject named in `KEDA_SERVICE_ACCOUNTS`. Correction: add `can-i get secrets -A` and a kube-system secrets probe per KEDA SA (cheap, and exact given the bindings); optionally flag Group subjects.

3. LOW (INFERRED) - `parse_can_i` (day8_addons.py:246-254) accepts `no`+exit1 as denied while ignoring stderr. kubectl prints `no` with a warning for an unknown resource/typo, so a misspelled forbidden resource would count as denied; positive controls cover only the scaling-namespace probes (deployments.apps/scale, HPA, scaledobjects.keda.sh), not `statefulsets.apps/scale` or `deployments.apps`. Correction: treat a "doesn't have a resource type" warning as an error.

4. LOW (VERIFIED) - No dedicated ServiceAccount for the Day 8 pods. scripts/day8_objects.py:369-378 (`pod_spec`): `automountServiceAccountToken: false` is pod-level only; pods use the namespace `default` SA (its own automount not asserted false). The project rule asks for a dedicated SA with automount false at both the object and the pod. Pod-level false means no token is mounted today. Correction: add a `maops-day8-scaling` SA with `automountServiceAccountToken: false` and set `serviceAccountName`, or document the exception.

5. LOW (VERIFIED) - Inconsistent ownership label checks. `phase_guards` (day8_scaling.py:321-324) and the Makefile `day8-addons-install` owner shell-check test only `app.kubernetes.io/instance`; `require_guards` and `phase_cleanup` (232-233, 751-754) test instance and component. `kubectl apply` of the namespace object in guards then patches labels onto an adopted namespace. Correction: require both labels everywhere.

6. LOW (VERIFIED) - `operator.keda.sh` lease deleted by name only. `KEDA_RUNTIME_LEFTOVERS` (day8_scaling.py:65-68) has `None` as the label requirement for the lease (the Secret has the `{"app":"keda-operator"}` label guard). The risk is tiny (namespace `keda`, KEDA-named). Document it or check holderIdentity.

7. LOW (VERIFIED) - Cleanup failure paths write no evidence. phase_cleanup early `return checks.finish("")` (754, 756, 761) skips `write_evidence`; evidence is written only on the full path, and a failed write is only a WARN. Failures are visible on stdout and fail-closed; write a cleanup-failed evidence file for auditability.

8. LOW (VERIFIED) - Loose NotFound matching in `common.kubectl_json_or_none` (day8_common.py:81): the substrings "NotFound" or "not found" anywhere in stderr, versus `get_state`'s strict "(NotFound)". This path decides namespace existence in phase_cleanup (749) and "namespace is gone" (765); an unrelated error containing "not found" would read as absent. Correction: reuse `get_state`.

9. LOW (INFERRED) - VPA/Metrics Server effective RBAC not probed. Only KEDA gets the SAR/binding audit. The VPA admission controller and recommender use cluster-wide chart ClusterRoles; the asserted scoping is the webhook namespaceSelector + `--vpa-object-namespace` + no updater. Optional: add `can-i` probes for the vpa-system SAs (e.g. get secrets -n maops-platform).

10. LOW (VERIFIED) - A malformed custom-columns line would raise an unhandled exception (day8_scaling.py:667): `ns, name = line.split(None, 1)` raises ValueError on a single-token line; caught by `main()` and fails closed (exit 1), but no Checks record/evidence.

VERIFIED-SECURE
- KEDA watch scope: helm-values/day8/keda.yaml `watchNamespace: maops-day8-scaling`; `static_problems` enforces it; `check active` asserts operator WATCH_NAMESPACE. [VERIFIED code; live per note]
- `keda_binding_problems`: 3 allowed CRBs pinned binding->role by name+kind; exactly 3 RBs; operator ClusterRole bound only in `keda` and `maops-day8-scaling`; flags a missing scoped binding while the namespace exists and a leftover after it is gone. [VERIFIED]
- Probe matrix: 8 forbidden actions x 3 KEDA SAs in maops-platform, plus the operator positive control in the scaling namespace; `parse_can_i` accepts only yes+exit0 / no+exit1 and raises otherwise. [VERIFIED] Proves: those 3 SAs' effective access in maops-platform at that time. Does not prove: other namespaces, cluster-wide access, group subjects, or ClusterRole contents (findings 2-3).
- Profile/lock guards: `require_cluster_profile()` first in `common.kubectl`/`helm`/`node_exec`, in `main()` and again in `phase_cleanup`; `node_exec` additionally enforces `_DAY_NODE_NAME_RE`; Day 8 uses the Day 7 profile and `$(DAY7_LOCK)` on every mutating target. [VERIFIED]
- Namespace deletion refuses without instance+component labels; guards refuse to adopt a namespace without the instance label and refuse a pre-existing namespace holding workloads; preflight refuses a pre-existing scaling namespace. [VERIFIED]
- CRD guard: all 6 CRDs; scope from `.spec.scope`; Namespaced listed with `--all-namespaces`, Cluster with `-o name`; explicit NotFound CRD = absent; unreadable/unknown scope = problem; any instance = problem; on failure nothing deleted/adopted, KEDA and namespace kept; runs only when the Helm release state is `present` (no release -> no CRD removal). [VERIFIED]
- Fail-closed semantics: `get_state` accepts only `(NotFound)` / `release: not found` as absence and raises on timeouts/refused connections; `resource_type_absent` is limited to `CRD_BACKED_TYPES`, so built-ins (HPA, quota, LimitRange) are never "absent". [VERIFIED]
- Cleanup ordering: release KEDA objects -> helm uninstall (foreground, bounded) -> runtime Secret/lease -> wait for keda pods -> namespace -> absence inventory; idempotent; safe when KEDA was never installed. [VERIFIED]
- Makefile `day8-check` always runs `day8-cleanup` after the demos and requires both demo and cleanup exit codes to be 0 before `day8-final-gate`. [VERIFIED]
- Secret handling: `runtime_object_labels` uses `-o name` for existence, then `jsonpath={.metadata.labels}`; Python never receives `.data`; evidence/logs/tests (a fake cluster that fails if the Secret is fetched whole) contain no Secret data; kubectl itself still downloads the full object, as the code states. [VERIFIED] A grep of `day8_stable.py` found no Secret reads.
- Day 8 workload security: namespace PSA `enforce: restricted`; container `runAsNonRoot` + explicit uid/gid, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `capabilities.drop: [ALL]`, seccomp `RuntimeDefault`; pod `automountServiceAccountToken: false`, `enableServiceLinks: false`, worker-only (control-plane excluded); scaling image `day8-cfg-<64 hex>` with `imagePullPolicy: Never`, proven through the containerd imageID; Dockerfile digest-pinned distroless, `USER 10001:10001` matching the pod context; Redis pinned by digest, uid 999/gid 1000, `emptyDir` `/data`; LimitRange/ResourceQuota guards, bounded budget, `activeDeadlineSeconds` on Jobs. [VERIFIED]
- Redis NetworkPolicy `day8-queue-ingress`: ingress-only, TCP 6379, from the worker/producer pod selectors and the `keda` namespace; nothing else in the cluster reaches Redis. [VERIFIED]
- VPA guards: updater disabled, `--vpa-object-namespace` on both components, webhook namespaceSelector limited to the scaling namespace, failurePolicy Ignore - asserted statically and live. [VERIFIED]
- No `kind: Secret`, token or credential in the values files, objects or tests. [VERIFIED]

ACCEPTED LIMITATIONS
- TOCTOU between the CRD-instance check and `helm uninstall` (up to 300 s of --wait): another actor creating a KEDA object in that window would lose it. Mitigated by the single-operator local Kind cluster and the Day 7 lock (which does not stop non-Makefile actors). INFERRED.
- `--kubelet-insecure-tls` on Metrics Server (documented local Kind limitation).
- Redis without auth and `--protected-mode no`; access limited to NetworkPolicy + ClusterIP; the queue holds disposable test items only; the policy admits every pod in the `keda` namespace.
- No default-deny in the scaling namespace (egress from scaling pods unrestricted; no API token; maops-platform default-deny ingress blocks them - INFERRED; covered for maops-platform behaviour by the final gate's networkpolicy-check).
- No PSA labels on the `keda`, `vpa-system`, `kube-system` namespaces (accepted, informational gap DAY5-SEC-I1).
- Namespace-label ownership is a convention, not tamper-proof.
- Evidence from the authoritative live run was not re-verified by me beyond the note.

JUSTIFICATION: The core controls hold: KEDA is namespace-scoped with audited bindings, the CRD-emptiness guard runs before any destructive uninstall, cleanup fails closed and keeps the namespace, Secret data never enters Python, the evidence and logs contain no secrets, the Day 8 workloads run under PSA restricted with pinned images and no tokens, and the Day 7 cluster-profile and lock guards are applied consistently. The remaining items are hardening/consistency gaps that are not exploitable in the documented single-operator Kind setting, so none block opening the PR. Finding 1 (adoption of a foreign `keda` release) is the one I would fix before v1.0.0 tagging.
```

---

## 3. cluster-integration-engineer

```
DAY 8 INTEGRATION REVIEW - recommendation: GO for opening a PR. I found no BLOCKER or HIGH issues; two MEDIUM and three LOW, all robustness gaps in recovery and repeat runs that did not affect the authoritative run (finding 4 is a conservative over-count).

How I reviewed: read the Makefile diff, scripts/day8_preflight.py, scripts/day8_scaling.py (cleanup section) and the scripts/day8_addons.py inventory; ran `make day8-plan`, `python3 -m unittest tests.test_day8` (118 tests, OK) and read-only live checks against kind-maops-k8s-day7. I changed no state, read no Secret and did not touch the day6 cluster. I did not read the rest of scripts/day8_stable.py beyond its function list, or day8_objects.py/day8_image.py/day8_common.py, did not diff the Day 8 tests against the code, and did not re-run the full 1776-test suite.

FINDINGS

1. MEDIUM - scripts/day8_preflight.py:132-135. Preflight only rejects a leftover `maops-day8-scaling` namespace. It never checks for an existing `keda` Helm release, KEDA CRDs or foreign KEDA objects. Evidence: code (the check list is context, nodes, version, host headroom, worker headroom, namespace). Effect: a leftover KEDA release or foreign KEDA CRs are found only at cleanup, after the whole demonstration has run; the CRD guard then fails closed and keeps the namespace. Fix: add read-only preflight checks that report the state of Helm release `keda/keda` and warn or fail if any of the six KEDA CRDs has instances, moving the fail-closed result to the start of the run.

2. MEDIUM - Makefile `day8-check` recipe, the `{ ...; demo=$$?; make day8-cleanup; ... }` block. Evidence: code. Cleanup is guaranteed only for failures inside that block. If the run is interrupted (Ctrl-C, SIGTERM, WSL/Docker restart, killed shell), the `sh -c` dies and cleanup never runs; interrupted-run recovery is not documented. Recovery: `make day8-cleanup` alone should work (idempotent: namespace absent, KEDA release absent, KEDA objects already released handled). It does not: the run directory is new per invocation, so `day8-final-gate` / `day8-stable-check` need the original DAY8_RUN_ID to compare against the original baseline; a bare cleanup also writes its evidence under a fresh run ID, with only a WARN if that directory is missing. Fix: add a short recovery paragraph to the validation record or README: run `make day8-cleanup`, then `DAY8_RUN_ID=<orig> make day8-final-gate`, noting the run-F restart note (Accepted limitations) after a host restart.

3. LOW - scripts/day8_scaling.py:576-612 (`release_keda_objects`) and the Makefile `day8-addons-install` hint. Evidence: code. If ScaledObjects exist, the KEDA operator is down and the RoleBinding exists, `kubectl delete --all --timeout=120s` fails and cleanup keeps the namespace. The recovery hint ("run day8-addons-install") helps only if KEDA can be repaired; a broken operator leaves a finalizer deadlock that needs a manual finalizer patch. Fix: document this as the manual escape hatch; do not automate it.

4. LOW - scripts/day8_preflight.py:128-131 (worker headroom). Evidence: code + private evidence. The check adds the add-on requests (`ADDON_REQUESTS_*`) on top of the live Pod requests; on a rerun Metrics Server and VPA are already installed and counted in the live requests, so their need is double-counted. Conservative, cannot cause an unsafe run (run F: 7100m / 14812Mi free vs a need of 935m / 976Mi). Fix: none required; optionally a comment.

5. LOW - Helm revision growth and the `keda` namespace. Evidence: live read-only. `helm list` shows metrics-server and vertical-pod-autoscaler at revision 10 each (each run adds a revision; Helm's default `--history-max` 10 caps history). The empty `keda` and `vpa-system` namespaces remain (`keda` 7h old). Fix: none; documented as intentionally retained.

CONFIRMED WORKING
- Install order (`make day8-plan`, 29 steps): static, preflight, resume-check, baseline-init, baseline, image build/record/load/verify-nodes, guards, addons-install, addons-check, quota/hpa/vpa/keda with stable-check between phases, cleanup (always attempted), final gate.
- `day8-addons-install` fails closed unless `maops-day8-scaling` carries the Day 8 instance label; all three Helm installs use pinned chart versions, `--reset-values -f <stage>`, `--wait --timeout 900s`.
- Day 7 lock and Day 7 profile throughout; `--kubeconfig`/`--kube-context` pinned on every Helm call.
- Cleanup order (phase_cleanup): 1. `release_keda_objects` deletes ScaledObjects/ScaledJobs/TriggerAuthentications only while the scoped RoleBinding `keda-operator` exists, then waits for `keda-hpa-*` HPAs to disappear; 2. KEDA CRD instance guard (all six CRDs, scope from `.spec.scope`, listed across all namespaces or cluster-wide; any unreadable answer or instance fails closed); 3. `helm uninstall --cascade foreground --wait --timeout 300s`; 4. delete the runtime Secret `kedaorg-certs` (label check reads only `.metadata.labels`) and the `operator.keda.sh` lease; 5. wait for the `keda` Pods to go; 6. delete the namespace. Namespace deletion is last, so the run c81534e5 finalizer deadlock cannot occur on this path; any failure in steps 1-2 keeps the namespace.
- `record_keda_absent` checks the Helm release, 6 CRDs, 4 ClusterRoles, 4 ClusterRoleBindings, the validating webhook, the APIService, 3 Deployments, 3 Services, ServiceAccounts, the scoped RoleBinding, the kube-system auth-reader RoleBinding, the runtime Secret and lease, Pods, and any binding naming a KEDA ServiceAccount; unreadable answers fail. I could not render the KEDA chart (not in the local Helm cache) to compare this list independently; the static tests and the run evidence say it matches.
- Idempotency: Helm installs, cleanup, baseline-init's refusal to reuse a run directory and preflight's refusal of a leftover namespace behave as designed.
- Live Day 7 state, read-only on 2026-10-03: release `maops-kubernetes-platform-day7` revision 13 deployed; 7 Pods (3 gateway, 3 app, maops-state-0); `maops-day8-scaling` absent; no `keda` Helm release; no KEDA CRDs or resources; the `keda` namespace exists but is empty.
- Authoritative run 140a5016313048f49386813c7a8daace: log ends `MAKE_EXIT=0`; final gate 7/7 stable-state checks (helm, workloads, pods, route, storage, external, controls) against the run's baseline (private evidence).

ACCEPTED LIMITATIONS
- Metrics Server (kube-system) and VPA (vpa-system) retained between runs; KEDA installed fresh each run and uninstalled in cleanup.
- One operator restart on a fresh KEDA install (runs E and F), recorded, not asserted.
- A host/Docker restart mid-run changes restart counts and fails the stable check - correct fail-closed behaviour, not a bug.
- Local Kind reference platform on a single WSL2 host, not a production claim.
- A manual finalizer repair is needed in the broken-operator scenario (finding 3).

RECOMMENDATION: GO. The core risks are covered: the finalizer deadlock is fixed by cleanup ordering, the CRD-deletion data-loss risk is guarded and fail-closed, and nothing Day 8 does touches the Day 7 release (resting on the final gate's 7/7 stable-state checks against run F's baseline and my own read-only live check). Fold findings 1 and 2 (preflight KEDA check, short interrupted-run recovery note) into the PR or a quick follow-up; I do not consider them gating.
```

Adjudication note on this report: its finding 5 says the empty "`keda` and
`vpa-system`" namespaces remain. Only `keda` was empty; `vpa-system` holds
the installed VPA.

---

## 4. kubernetes-test-engineer

```
DAY 8 TEST AND VALIDATION REVIEW (review only; no files, cluster or git state touched)

Baseline: `python3 -m unittest tests.test_day8` -> 118 tests, OK. In-memory mutations from a throwaway scratchpad script; nothing on disk changed. Each mutation was applied to the production function and the matching test classes re-run:

| Mutation | Result |
|---|---|
| Guard deleted (`crd_instance_problems` returns no problems) | Caught (5 failures, 1 error) |
| Guard limited to the first 2 CRDs | Caught |
| Unreadable-CRD problems ignored | Caught |
| Cluster-scoped CRDs treated as Namespaced | Caught (1 error) |
| Runtime labels read with `-o json` | Caught (11 failures) |
| `parse_can_i` treats errors as "no" | Caught |
| `keda_binding_problems` always empty | Caught |
| `diff` always empty | Caught |
| Namespaced instance listing without `--all-namespaces` | SURVIVES (0 failures) |
| `FORBIDDEN_IN_APP` cut from 8 entries to 4 | SURVIVES |
| Pod Ready/terminating check removed from `health_problems` | SURVIVES |

FINDINGS

1. HIGH - tests/test_day8.py:~440 (FakeCluster.kubectl) and ~613-660 (KedaCrdGuardTests). The core claim "no instance in ANY namespace" is not proven by any test. Evidence: removing `--all-namespaces` from the Namespaced listing in `_crd_instances` (scripts/day8_scaling.py:658) left all of KedaCrdGuardTests and CleanupLifecycleTests passing. The fake matches the listing on `custom-columns=` or `-o name` and returns foreign objects regardless of namespace flags. Real kubectl without that flag lists only the current-context namespace (default): a foreign `team-a` ScaledObject would be invisible, the uninstall would delete it, and the tests would stay green. Run F's live empty answer could not catch this either (nothing foreign existed). Correction: make the fake namespace-aware (foreign Namespaced instances only when `--all-namespaces` is in args; foreign Cluster instances only for `get <crd> -o name`), and add a direct test of `_crd_instances` asserting the exact kubectl argv for the Namespaced and Cluster cases. The fake also derives scope from `crd.startswith("cluster")`, mirroring the code's own assumption; make the scope table explicit in the fake.

2. MEDIUM - scripts/day8_scaling.py:658-668 (`_crd_instances`, `_crd_scope`). The "malformed listing output" case is unhandled and untested. Evidence: a Namespaced listing with a single-token line (e.g. a namespace-less or `<none>` row) makes `line.split(None, 1)` raise ValueError; `crd_instance_problems` catches only Day8Error (confirmed by direct call with kubectl mocked). Still fail-closed (`main()` catches ValueError, returns 1, no uninstall), but the guard's per-CRD "unreadable" problem and message are bypassed, and `phase_cleanup` aborts without the "KEDA NOT uninstalled" line or evidence. A `_crd_scope` stdout of "" with exit 0 yields scope "" -> "unexpected scope" (fail-closed, but only the "Weird" scope is tested). Correction: raise Day8Error on a malformed row and test that a single-token row and an empty-scope answer block the uninstall with the specific problem text.

3. MEDIUM - tests/test_day8.py:706-713 (`test_no_day8_script_fetches_a_secret_object`). The static scan is weak: it flags only a line containing the lowercase literal `"secret"` together with `kubectl_json`, `"json"` or `"yaml"`. Evidence: `"secrets"` (plural) does not contain `"secret"` with its closing quote; a `kubectl("-n","keda","get","secrets","kedaorg-certs","-o","json")` line, a multi-line call, or a variable `kind` (as in `runtime_object_labels(kind, ...)`) slips through. Today's single production read is protected by the behavioural tests (which caught my `-o json` mutation), not by this scan. Correction: scan with an AST or a regex across whole call expressions, case-insensitively, including `secrets?`, `-o json|yaml|go-template|custom-columns=...data`, and `kubectl_json*` with a Secret kind; better, a seam test wrapping `common.kubectl` that asserts no `get` of kind secret in any day8 module uses an output other than `name` or `jsonpath={.metadata.labels}`. The fake's AssertionError on full-object reads covers only `kubectl_json_or_none`; other `kubectl()` output shapes fail only via "unexpected kubectl call" - by accident.

4. MEDIUM - tests/test_day8.py:342-346 (`test_forbidden_actions_cover_secrets_and_scaling`) and `_record_keda_rbac` in scripts/day8_addons.py. Evidence: the FORBIDDEN_IN_APP test asserts membership of only 4 of 8 actions; cutting the tuple to 4 passed (not required by any test: watch secrets, update deployments/scale, patch deployments, create HPA). `_record_keda_rbac` has no test at all - the code that turns a `yes` into a failed check, applies the positive control and loops over the 3 SAs; a regression (e.g. `denied_everywhere` never failing the check, or the SA loop dropping one account) would pass. Correction: assert FORBIDDEN_IN_APP equals the exact expected tuple; add a `_record_keda_rbac` test with `can_i`/`kubectl_json` mocked: all "no" passes; one `yes` for one SA fails with that SA and action in the message; a positive-control "no" fails; a can_i error propagates. Further gaps: `keda_binding_problems` is not tested with a non-ServiceAccount subject, a mixed-subject binding, or a binding in `keda` with a wrong role.

5. MEDIUM - tests/test_day8.py:927-955 and scripts/day8_stable.py (`check`, `observe_*`). Day 7 regression protection is thin. Evidence: removing the Pod Ready/terminating check from `health_problems` survived; no test builds a non-Ready or terminating Pod. `check()`, `capture()`, `observe_helm`, `observe_workload`, `observe_pods`, `observe_route` and `observe_storage` have no tests: a change to the sections list in `check()` (e.g. dropping "storage") or to what `observe_pods` captures (restart count, resources, imageID) would pass. `diff` is tested only for a top-level uid change and an extra pod, never for nested container restarts/resources - exactly what the VPA guard relies on. The `snapshot()` helper has empty `containers`, `route` and `values`. Correction: tests for unready, terminating and Helm-status != deployed; a `diff` test on nested `containers.<c>.restarts`/`resources`; a `check()` test with `observe`/`read_evidence` mocked asserting all 7 sections are compared and a restart change fails; an `observe_pods` test with a fixture Pod list. MakefileTests are text-order assertions and do not prove behaviour; `test_stable_check_after_each_demonstration` is loose (compares against `after.index("demo=")`, so it passes if `day8-stable-check` appears anywhere after the demo).

6. LOW - FakeCluster fidelity (tests/test_day8.py:348-498). `poll` is stubbed to a single call, so "Pods linger in keda" and retry paths are never exercised (the fake's pods list is cleared by uninstall, no linger option). `object_state`, `resource_type_absent`, `helm_release_state` and `kubectl_json_or_none` are replaced wholesale; their real NotFound handling (`get_state`) is covered only by ExistenceAnswerTests in isolation, and the real NotFound strings appear only in the fake's CRD-scope branch. `object_state` returns "present" for any other kind whenever KEDA is installed (no partial presence). The uninstall fake clears CRDs, pods and binding atomically, so "uninstall succeeded but some resource remained" is untested. Existing failure-mode coverage is good (uninstall failure, `delete_leaves`, missing binding, unreadable release, foreign labels, failed list). Correction: add a pods-linger option (poll not satisfied) and a partial-uninstall option (a survivor in the final inventory) with the expected failing check named.

7. LOW - Fail-closed claims without a direct test: a binding that exists but is not for KEDA's ServiceAccount; the `phase_cleanup` identity-label refusal ("lacks the Day 8 identity labels" - no test found by grep); a failed `kubectl delete` of the runtime Secret (the `delete-runtime` failure branch); a write_evidence failure in cleanup (which only warns). Correction: one test each via FakeCluster options.

Checked and sound: guard-before-uninstall ordering is asserted, and all blocked cases assert no helm uninstall, namespace kept, release and CRDs present, and the foreign object untouched; never-installed, partial-install, leftover-KEDA-without-namespace and idempotent-second-run are covered with exact mutation-order assertions; `parse_can_i` strictness is well tested.

WHAT RUN F (140a5016313048f49386813c7a8daace, exit 0) PROVES
- On a fresh KEDA install (Helm revision 1), the HPA, VPA, KEDA and quota demonstrations passed.
- Cleanup 13/13 on the happy path: all 6 CRDs empty, uninstall, runtime Secret and lease deleted, namespace deleted, 32 KEDA objects NotFound.
- The labels-only Secret read works against real kubectl (the Secret was deleted after the label check; real jsonpath output of a label map parsed as JSON).
- The Day 7 release was unchanged against run F's own baseline (stable 7/7 at five points); the final gate re-ran the Day 7 health suites plus mesh-check 45/45, networkpolicy 37/37 and add-ons after-cleanup 17/17.
- RBAC: 24/24 `no` answers and the positive control, evaluated live once.

WHAT RUN F DOES NOT PROVE
- No failure path of the CRD guard was exercised live: foreign namespaced, cluster-scoped, unreadable list, unreadable CRD and unknown scope are unit-only, and finding 1 means even the all-namespaces claim is only asserted by the fake, never by a negative live test. Run F only shows that, with nothing present, the real argv returns empty.
- No failed uninstall, lingering Pods, partial install, never-installed start (the start state was clean, but cleanup then ran the full path) or repeated cleanup was exercised live - unit-only.
- The secret-label refusal and leftover-KEDA-without-namespace were never exercised live.
- The stable baseline is captured by the same run: the final gate compares to run F's own baseline, not to an independent Day 7 record. It compares Helm values/manifest hash, workload UID/generation/template, Pod UID/restarts/resources, route, storage and state sha, and external responses. It never compares to pre-Day-8 Pod restart counts (the baseline already contained the restart-recovery restarts), nor Cilium/Istio/node-level state; mesh/networkpolicy checks are pass/fail, not before/after diffs.
- The post-run snapshot (`final-state.txt`) is read-only evidence outside the gate.
- The residual `keda` namespace remains by design.

RECOMMENDATION: GO, conditional on fixing finding 1 first. No BLOCKER. The production code is correct, fail-closed, and proven on a live happy path; the 118 tests are mostly meaningful (my mutations killed the guard-deletion, scope, labels-read, RBAC-parse and diff mutants). But the guard's central claim - instances are searched across all namespaces - is asserted only by a fake that ignores the flag. That is cheap to fix and worth fixing before the PR, because the guard is the only protection against deleting someone else's KEDA objects. Findings 2-5 are real test gaps to close in the PR or an immediate follow-up; none affects release behaviour today, because every production path I examined fails closed.
```

---

## 5. release-engineer

```
DAY 8 / v1.0.0 RELEASE-READINESS REVIEW (review only; nothing edited, staged, committed, tagged or published; no cluster contacted)

Verified myself: branch feature/day-8-autoscaling-hardening on base 3d19075; the 20 changed files are all unstaged (6 modified, 14 untracked), matching the v2 note; the patch sha256 matches 07bc3f3b…; VERSION is `0.7.0`; Chart.yaml version 0.7.0, appVersion "0.7.0"; `make version-check` passes 64/64; version_check.py still pins DAY7_TARGET_VERSION = "0.7.0" (line 74); tags v0.1.0..v0.7.0 exist, v0.7.0 annotated and peeling to merge commit 6557c8d (PR #10's merge); `python3 -m unittest tests.test_day8` 118 OK, full suite 1776 OK (exit 0), matching the note; 0 port-forward processes; no Day 1-7 evidence files modified (the only new file under docs/engineering-reviews is day-08-live-validation-record.md). Not re-run: `make day8-static-check`, the helm checks, `day8-plan`; run F read only from the v2 note and record.

FINDINGS
1. MEDIUM - Day 8 independent-review artefacts are missing. docs/engineering-reviews has no day-08-independent-reviews.md, day-08-final-adjudication.md or day-08-remediation-log.md; Days 6 and 7 had these before release (`git show c51b8dc --stat`). Correction: record the four agent reviews, the owner's adjudication and the remediation before the PR is merged.
2. MEDIUM - Run F predates all pending fixes (preflight KEDA release/CRD check, SAR broadening, KEDA release ownership, CRD-guard tests). Correction: any accepted code fix - especially in the KEDA preflight, cleanup or RBAC check - invalidates run F as authoritative; re-run `make day8-check` fresh on the final code and record it (run G). Docs-only/test-only fixes may not need a live re-run; the owner should decide explicitly.
3. MEDIUM - The version bump is undecided and has a side effect. scripts/day7_build.py:74 reads VERSION; tag_pattern/pinned_tag build `<VERSION>-cfg-<digest>` (lines 93-105); every Day 7 stage and `day7-deploy` use that pinned tag; the roadmap says the bump "would change the pinned Day 7 build". Precedent: c51b8dc (the Day 7 feature commit) included VERSION, Chart.yaml and version_check.py - the bump went into the PR, not after merge. Bumping to 1.0.0 makes the tags `1.0.0-cfg-*`, so the running images (`0.7.0-cfg-…`, build fdb68741…) no longer match the expected pattern; Helm release `maops-kubernetes-platform-day7` (rev 13) would need a new build, run ID and baselines, conflicting with Day 8's "never touch the Day 7 release" claim; Day 8 gates/baselines are pinned to build fdb68741. Correction: the owner picks (a) bump to 1.0.0 in the PR (edit version_check.py, Chart.yaml and tests, new Day 7 build and redeploy, fresh day8-check against that build, re-based "unchanged Day 7 release" evidence) or (b) keep the release gates at 0.7.0, which makes a v1.0.0 tag inconsistent with VERSION (not recommended). Until chosen, do not describe v1.0.0 as ready.
4. MEDIUM - Day 8 tests/version gating not yet in version_check.py: no 1.0.0 expectation; `make ci-check` is the CI target. Correction: when bumping, add the 1.0.0 target and the frozen 0.7.0 baseline deliberately (as c51b8dc did for 0.7.0); confirm `make ci-check` covers `tests/test_day8.py` (it should, via `make test`).
5. LOW - Stale or inconsistent wording in docs/engineering-reviews/day-08-live-validation-record.md: line 77 records the cluster-wide KEDA workaround without a superseded marker at that line; section 7 (~line 231) says `day8-addons-final-check` "covers the dormant state after cleanup" (dormant KEDA is superseded by uninstall, which section 8 says) - add a pointer or fix; lines 294-306 "Remaining risks" are marked superseded by section 8 (good); line 214 "VERSION stays 0.7.0 … v1.0.0 is not released" is correct now but stale after a bump; section 7's "1749 tests OK" is historical - add "(at that time)" to avoid conflict with section 9's 1776. Correction: label superseded lines consistently.
6. LOW - Claims in README, roadmap, architecture and the v2 note are acceptably scoped (no "released"/"production-ready" overclaim; all say "NOT released"). README's "no KEDA identity can read Secrets or scale workloads in maops-platform, proven with SubjectAccessReviews" is accurate only for the 24 probes listed - should say "for the probed verbs". README "Days 1-7 are complete" is correct. .claude/CLAUDE.md "proves all 6 KEDA CRDs hold zero instances cluster-wide" is accurate.
7. LOW - v2 note weak spots: section 8's last row ("no open release blockers from my side") is the implementer's self-assessment, not sign-off; "Run F is authoritative" holds only until fixes are applied (finding 2); limitation 1 (operator restart on fresh install) is an honest inference without captured logs - a reviewer may want logs on the next run.
8. LOW/MEDIUM - Known open items from the other reviews, not to be treated as dismissed: architecture MEDIUM (SAR breadth); security MEDIUM (ownership of a pre-existing `keda` release/namespace - the cleanup's fresh-install safety claim depends on it); integration MEDIUM (preflight KEDA/CRD check; interrupted-run recovery documentation - the note's limitation 7 only says `make day8-cleanup` is "documented"); test-engineer HIGH (FakeCluster ignores `--all-namespaces`, so the guard's every-namespace claim is untested; the v2 note's "KedaCrdGuardTests … any namespace" is overstated until fixed).

MUST WAIT FOR ADJUDICATION
- Whether to apply each MEDIUM/HIGH finding above (including the FakeCluster `--all-namespaces` fix before claiming the CRD guard is tested across namespaces).
- Whether run F suffices or a fresh run G on final code is required.
- The VERSION/Chart/version_check bump decision and its Day 7 rebuild/redeploy consequence.
- Creation of day-08-independent-reviews.md, day-08-remediation-log.md, day-08-final-adjudication.md.
- Final wording of README/roadmap/architecture/live record (superseded markers, test counts, "Evidence: run F" vs run G).
- A new review package (the v2 patch will be out of date after fixes).

RECOMMENDATION
- NO-GO for opening a PR from the current state: the four agent reviews are unadjudicated, including one HIGH from the test engineer.
- GO to open a PR once the owner has adjudicated, the accepted fixes are applied, and the bump decision (finding 3) is resolved; the Day 7 precedent puts the VERSION bump inside the PR.
- A draft PR purely for visibility before adjudication is acceptable if labelled not ready to merge.
- The work is ready for independent review as a branch, not for release.

USER-OPERATED RELEASE SEQUENCE (the owner runs these; I do none of them)
1. Adjudicate the four reviews; write docs/engineering-reviews/day-08-independent-reviews.md, then day-08-final-adjudication.md.
2. Apply the accepted fixes with tests (including the FakeCluster `--all-namespaces` handling); record them in day-08-remediation-log.md.
3. Decide the VERSION path (finding 3). If bumping to 1.0.0: update VERSION, Chart.yaml version/appVersion, scripts/version_check.py (add 1.0.0, keep frozen baselines), tests/test_version_check.py; rebuild the pinned build overlay and redeploy the Day 7 release under a new Day 7 run ID; re-baseline Day 8.
4. Run (live ones with PATH=/usr/bin:$PATH): `make tool-check`, `make ci-check`, `make day8-static-check`, `make version-check`, `make day8-check` (fresh run G); record results in the live validation record.
5. Update README, roadmap, architecture, CLAUDE.md and the live record so wording/status are consistent; regenerate the review package and its sha256.
6. Commit on the feature branch and open the PR (Day 7 pattern: c51b8dc then PR #10); wait for CI (`make ci-check`).
7. Merge via GitHub; check out merged `main`.
8. Merged-main validation on that commit: `make ci-check` plus `make day8-check` or the final gate as the owner decides; store logs under a day8-logs/merged-main-<sha>/ folder like Day 7's.
9. Create an annotated tag `v1.0.0` on the merge commit (message style "MAOps Kubernetes Platform v1.0.0 — ..."); push the tag. Before tagging, check v0.1.0..v0.7.0 are unmoved (v0.7.0 on 6557c8d).
10. Create the GitHub Release v1.0.0, mark Latest, state plainly it is a local Kind reference platform.
11. Re-verify the tag target locally and remotely (`git ls-remote`, `gh release view v1.0.0`).
12. Add day-08-post-release-verification.md as a docs commit afterwards, as 3d19075 did for Day 7; the release tag must not move.
```

---

# Round 2: focused re-review of the remediation (2026-10-04)

Three review-only agents re-examined only the changed high-risk areas on
the final working tree, including untracked files, against `3d19075`:
- namespace-aware CRD tests and guard;
- KEDA release/namespace ownership and preflight;
- access checks;
- versioned build records;
- immutable claim-template labels;
- the controlled 1.0.0 rollout;
- whether the records separate run G from earlier runs.

Each ran with its existing definition and permissions. No commit, tag,
release or live gate was run. The outputs are preserved verbatim below,
without leading indentation.

| Agent | Recommendation | New HIGH/MEDIUM |
|---|---|---|
| `kubernetes-security-reviewer` | targeted GO FOR PR | none (S1 closed); 4 new LOWs |
| `release-engineer` | targeted GO FOR PR | none (R1-R8 closed) |
| `kubernetes-test-engineer` | NO-GO until T8 is closed | 1 MEDIUM (T8: frozen claim-template labels not validated) |

## Process incident during round 2: the test reviewer mutated the repository working tree

The test reviewer was told to run mutation experiments only in a
throwaway copy. Its first batch ran in the repository instead.
- **Cause.** The command began with `rm -rf $S/m; mkdir $S/m; cp -r ...
  $S/m/; cd $S/m`. The session's scratch directory `$S` did not exist at
  that moment, so `mkdir`, `cp` and `cd` failed. Because the commands were
  not chained with `&&`, the mutations then ran in the repository.
- **What ran**, four times in sequence, each against
  `scripts/day8_scaling.py` (an untracked file):
  `cp $1 $1.orig; sed -i "<expr>" $1; python3 -m unittest tests.test_day8;
  mv $1.orig $1`. The four expressions:
  - drop `--all-namespaces`;
  - drop the `<none>` check;
  - loosen `len(parts) != 2`;
  - disable scope validation.
- **Restoration.** Each run restored the file with `mv` from a
  byte-for-byte `cp` taken immediately before its own `sed`, so the
  content is the pre-review content; only the modification time changed
  (2026-10-04 11:16 +0600). No other file was touched.
- **Independent verification by the implementing session.**
  - The agent's transcript was inspected for every command that touched
    the file; the commands are as listed above.
  - No `*.orig` file remains, and the guard lines are present
    (`--all-namespaces` at line 663, the `<none>`/length check at 675,
    `unexpected scope` at 636).
  - `git diff --stat` is unchanged (17 files, +829/-42), and
    `git status --short` still shows 31 entries.
  - `tests.test_day8` passed (160 tests).
  - Git cannot attest the bytes of an untracked file; the proof is the
    copy-then-restore sequence.
- **Later experiments.** In both the reviewer's and the implementer's
  later experiments, every mutation command first checks that its scratch
  directory exists and that `cd` succeeded.

## R2.1 kubernetes-security-reviewer (round 2)

```
Focused security re-review of the Day 8 KEDA ownership, preflight, access-probe and cleanup changes. Static review of the working tree plus read-only evidence reads. The only live call was one webhook query, which returned NotFound because KEDA is not installed now, so the webhook claim rests on the run G record and on code. I did not run any mutating command or read Secret data.

Result: no new HIGH or MEDIUM findings. S1 is closed. LOWs S2, S3, S4, S5 and S9 are closed or accepted. The two LOWs I did not trace (S6, S10) are not assessed here. Targeted GO FOR PR (security perspective).

1. KEDA ownership (S1 closed, VERIFIED by code)
- The ownership signal is the Helm release label `maops-day8-owner=maops-kubernetes-platform-day8`. It is set at install in `Makefile:747-749` (`DAY8_KEDA_OWNER_LABEL`, line 687).
- It is read from `helm get metadata`, which exposes no values, manifest or Secret data. Checks cover the label, the chart name and pinned version, and the namespace (`day8_addons.py:499-513`).
- The label is checked in three places: `keda_preinstall` (`day8_addons.py:539`), preflight (`day8_preflight.py:~150`) and `uninstall_keda` (`day8_scaling.py:~722`).
- The label is only a marker, not a cryptographic guarantee. That is acceptable for a single-operator project that runs under one lock.
- `keda-preinstall` runs before any helm call, and `makefile_keda_problems` enforces that ordering statically. There is no `--create-namespace` for keda. The namespace is created by `kubectl apply` of `keda_namespace_object` only when `get` returns an explicit NotFound.
- A foreign release, a CRD without a Day 8 release, or an unlabelled namespace all refuse.
- Unreadable answers fail closed. `kubectl_json_or_none` returns None only on `(NotFound)`. `get_state` returns "absent" only on NotFound or `release: not found`. `helm_release_metadata` raises on non-JSON output. `uninstall_keda` returns False on a state or metadata error.
- The `keda` namespace is deleted only if `is_day8_owned` passes both identity labels (`day8_scaling.py:760-775`).

2. CRD-instance guard (VERIFIED, still fail-closed)
- Namespaced CRDs are listed with `--all-namespaces` and custom-columns. A row with an unexpected shape or `<none>` as the namespace raises an error, which counts as unreadable and blocks the uninstall.
- A list error raises. Only an explicit CRD NotFound yields "absent".
- Ownership is checked first, then the CRD guard, then `helm uninstall`.

3. Access checks (S2, S3, S9 closed)
- Scoping is correct. `can_i` uses `-n <ns>`, or `--all-namespaces` when the namespace is None. This is used for the cluster-wide Secret list and for the cluster-scoped `apiservices` and `validatingwebhookconfigurations` grants.
- `parse_can_i` treats any kubectl warning, or any answer other than exit 0 with "yes" or exit 1 with "no", as an error. A mis-scoped or misspelled probe therefore cannot count as a denial.
- There are 22 forbidden-in-app probes and 5 elsewhere, 27 in total per identity. Run G recorded 27 probes for each of 3 KEDA identities and 3 persistent add-on identities, all denied.
- `EXPECTED_GRANTS` are positive controls that fail if a grant disappears, so documentation drift is caught.
- The docs now say "for the probed verbs" (`README.md:381`, `docs/architecture.md:3522`, remediation log line 151).
- The probes are still not exhaustive. They do not cover Group subjects, or secrets in `vpa-system` or `keda` for non-KEDA identities. That is documented as scoped.

4. KEDA webhook `failurePolicy: Fail` (accepted)
- `keda_webhook_problems` requires every webhook to be `Fail` and all its apiGroups to be a subset of `{keda.sh, eventing.keda.sh}`. It fails if there are no webhooks.
- It does not check the operations (CREATE/UPDATE) or the resources. The group limit still means a non-KEDA write cannot be blocked.
- The check runs in the active-mode check (`day8_addons.py:626`), and run G recorded the live result as verified. I could not re-confirm it live because KEDA is absent now.

5. Secret handling (VERIFIED unchanged)
- `runtime_object_labels` calls `object_state` (`-o name`) and then `jsonpath={.metadata.labels}`, so Python never receives `.data`.
- The `kedaorg-certs` Secret is deleted only if it carries the label `app=keda-operator`.
- The new evidence contains only names and labels.

6. Legacy `keda` namespace deletion (adequate)
- The inspection and delete evidence files exist.
- The inspection showed no Helm release, no CRDs, only default ConfigMaps and a ServiceAccount, and creation at 09:37Z the same day. It was deleted by name about 30 seconds later, and the log states there is no UID precondition.
- The deletion is justified and the limitation is recorded honestly.
- The attribution to run `1c59a36c` is inferred from the creation time. The listing excluded Secrets by type, and the inspection's first `helm list` calls failed on flags, but the retry and `helm status` returned empty.

New LOWs
1. `day8_scaling.py:740-757`. When the release is absent, `uninstall_keda` deletes the `kedaorg-certs` Secret and the `operator.keda.sh` Lease in the `keda` namespace before `delete_keda_namespace` checks namespace ownership.
   - The Secret is gated on its label.
   - The Lease is deleted by name with no label check.
   - A standalone `make day8-cleanup` run against a foreign non-Helm KEDA would therefore delete those two objects.
   - Preflight refuses such a state at the start of a run. Fix by checking namespace ownership before the runtime-object loop.
2. TOCTOU in `keda_preinstall` (`day8_addons.py:539-562`). The namespace `get` and the `kubectl apply` are not atomic, and the `helm upgrade --install` runs minutes later, after the Metrics Server and VPA installs. Everything runs under the Day 7 lock with a single operator, so this is theoretical. `kubectl create` would be stricter than `apply`, which merges labels onto a foreign namespace that appeared in between.
3. `keda_webhook_problems` does not assert operations. The CREATE/UPDATE-only claim in the comment at `helm-values/day8/keda.yaml:~25` is not asserted by code.
4. Legacy-namespace attribution is inference only (see item 6 above).

Targeted GO FOR PR (security perspective). There are no blocking items.
```

## R2.2 release-engineer (round 2)

```
Release re-review of the Day 8 / 1.0.0 working tree against 3d19075. Verdict: targeted GO FOR PR (release perspective). I found no new HIGH or MEDIUM findings. I did not rerun any live target or contact a cluster.

1. Versioning: PASS
- VERSION is exactly `1.0.0` with a single newline.
- Chart `version` is 1.0.0 and `appVersion` is "1.0.0". The three image tags in `values.yaml` are `1.0.0`. `EXPECTED_VERSION` in `scripts/validate_helm_chart.py:50` is `1.0.0`.
- `scripts/version_check.py` has `RELEASE_TARGET_VERSION = "1.0.0"` (line 80), `DAY7_TARGET_VERSION = "0.7.0"` (line 76), and the frozen k8s/base target stays `0.5.0`.
- `make version-check` gives 65/65, including `day7.historical_target_frozen`.
- `git diff 3d19075 -- docs/engineering-reviews/day-0[1-7]* k8s kind` is empty, so Days 1-7 sources and records are intact.
- Tags `v0.1.0` through `v0.7.0` all exist, and `v0.7.0` is still `d0dfb52b…`. No `v1.0.0` tag exists.
- The schema-1 build record `fdb6874137fb…` is preserved in `day7-builds/` (`build.json`, `values.yaml`). `current.json` now points to the new build `70400e92…`.
- `python3 -m unittest discover -s tests` gives 1818 tests, OK. This matches the run G log.

2. Claim-template label freeze: PASS
- `_helpers.tpl:51-59` defines `maops.claimTemplateLabels` with version `"0.7.0"` and chart `maops-kubernetes-platform-0.7.0`, with a comment explaining the freeze.
- The reason is accurate: `volumeClaimTemplates` is immutable on a StatefulSet.
- `rollout-10-post.json` shows `statefulset.vct_labels` unchanged, and the StatefulSet UID is unchanged. So the rollout did not hit the immutable-field rejection.

3. Controlled rollout: PASS (compared `rollout-00-pre.json` and `rollout-10-post.json` myself)
- Unchanged:
  - UIDs of the StatefulSet (`4ded2c43…`), the PVC (`2be6628b…`) and the PV (`a9c74497…`), plus Deployment, Gateway and HTTPRoute UIDs.
  - The PV name and node.
  - `state.json`, 15 bytes with sha256 `3ce4f556…`.
- Pods: 7 before, 7 after, `pod_uids_retained` is `[]` and `pods_replaced` is 7. The records report this honestly as "all 7 replaced", including `maops-state-0`.
- Helm revision went 13 to 14. Template digests changed as intended.
- The operator error is honestly disclosed in the live record (line 553), the remediation log (line 265) and the adjudication (line 122).
  - What happened: the ambient, rollout and gateway checks first ran under the default day6 profile and were refused at connect with exit 2. Nothing changed.
  - `rollout-steps.txt` marks those three runs as superseded by the `09-*` runs, which exited 0, and keeps both sets of logs.
- One cosmetic point: the `08-*` step labels carry `DAY7_PLACEHOLDER=1`, which looks odd but is harmless.

4. Record accuracy: PASS, with one gap
- Spot-check of run G against the run G log: 1818 tests (log line 3501), manifests 267/267 (line 3880), Helm 2346/2346 (line 6248), `MAKE_EXIT=0`, stable 7/7. The live record section 10 matches.
- Section 10 and its superseded markers clearly separate run G (authoritative, final code, post-rollout baseline) from run F `140a5016…` and earlier runs.
- No overclaim: README says "not released" and "not production-ready", the roadmap says "not a production-ready one", and the other docs repeat that Day 8 is not released.
- LOW (R-new-1): `docs/architecture.md:3877-3878` still ends the Day 8 validation summary with run F `140a5016…` and "See section 9". It never mentions run G, the authoritative run. Add one sentence pointing to section 10 and run G.

5. PR readiness
- No port-forward processes are running.
- Nothing is staged and nothing is committed. HEAD is still `3d19075`, with 17 modified tracked files plus the untracked Day 8 files.
- Untracked files must be added at commit time: `scripts/day8_*.py`, `tests/test_day8.py`, `helm-values/day8/`, `scaling/`, and `docs/engineering-reviews/day-08-*.md`. A plain `git commit -a` would omit them.

R1-R8: closed. The adjudication accepts R2 (fresh run G on the final code). Version and claim-template issues are verified closed above.

LOWs
- The architecture.md run G reference above.
- The `DAY7_PLACEHOLDER` label noted under item 3.
- Both `.claude/CLAUDE.md` and the memory index still cite run F. That is acceptable as history, but refresh it after run G if you want it consistent.

Targeted GO FOR PR (release perspective). The work is ready for independent review; it is not released. Next steps, all user-operated:
1. Optionally patch the architecture.md run G reference.
2. Stage everything, including the untracked files, and commit on the feature branch.
3. Push and open a PR into `main`.
4. After review, merge, then re-run the validation on the merged `main`.
5. Only after that, decide on the `v1.0.0` tag and GitHub Release. The `v1.0.0` tag and release are not part of the PR.
```

## R2.3 kubernetes-test-engineer (round 2)

```
Focused re-review, test perspective. The work is mostly sound. One new MEDIUM (claim-template labels untested) is a gap, but it is not a defect in current code.

Counts. `python3 -m unittest tests.test_day8` ran 160 tests, OK. `python3 -m unittest discover -s tests` ran 1818 tests, OK. Both match the claims. `helm_check.py` on the real chart passed 2346/2346.

Process note. My first mutation batch ran against the repo working tree instead of a scratch copy. A `cd` into the scratch dir failed because the dir didn't exist yet. Each mutation was a sed on `scripts/day8_scaling.py` with a `.orig` backup that I moved back right after the run. Afterwards no `.orig` files remained, `--all-namespaces` and `"<none>"` were present again, and `tests.test_day8` passed (160). `git status --short` shows the same 31 entries as before. I did not run `git diff --stat`, so file-level restoration is argued from the backup-and-move pattern and the passing tests rather than a diff. Please run `git diff --stat` as an independent confirmation. Later experiments used a scratch copy under the scratchpad directory.

1. CRD guard and FakeCluster
- Fake fidelity. `FakeCluster.list_crd_instances` models kubectl namespace semantics correctly:
  - Namespaced listings see only the context namespace (default) unless `-n`, `--namespace`, `-A` or `--all-namespaces` is given.
  - Cluster-scoped listings ignore namespace flags.
  - Output follows the requested format (custom-columns or `-o name`).
  - `test_fake_models_context_namespace_listing` guards the fake itself.
- Exact argv. `test_listing_argv_is_exact_for_each_scope` asserts the full tuple for both scopes, with no `assertIn` looseness.
- Mutations (T = tests failed, S = survived):
  | Mutation | Result |
  |---|---|
  | Drop `--all-namespaces` in production | T, 4 failures (argv test, foreign-object test, others) |
  | Loosen `len(parts) != 2` to `< 1` | T, 1 error |
  | Disable scope validation | T, 2 failures |
  | Remove the `parts[0] == "<none>"` check | S |
- `<none>` survivor. The malformed-row test `"<none> stray"` is still blocked by the generic "instance exists" path, so the dedicated check is not independently proven. Either way the guard fails closed (LOW).
- Fail-closed. Malformed rows, an empty scope answer, an unexpected scope, an unreadable CRD and an unreadable listing all block the uninstall. Each is tested through `_blocked`, which asserts no helm call, no namespace delete, release and CRDs still present, and "KEDA NOT uninstalled" in the output.
- `test_dropping_all_namespaces_would_be_caught`. This test patches `_crd_instances` with a narrowed copy and does not mutate production code. It is a demonstration, not a guard. The argv-exact test is the real guard, and my real mutation confirms it.
- T1 and T2: closed.

2. KEDA ownership and preflight
- Cleanup refusals. `test_cleanup_refuses_foreign_release` and `test_cleanup_refuses_foreign_keda_namespace` check the FakeCluster mutation log. They assert no `helm` and no `delete-keda-namespace`, that the scaling namespace is kept, and that the release is still present.
- Preinstall. `test_preinstall_refuses_foreign_namespace` asserts `applied == []`, so a foreign namespace is never adopted. The refuse-foreign-release and refuse-CRDs-without-release tests assert rc == 1 only. They do not assert that no apply happened (LOW).
- Ownership and preflight rules. `keda_release_ownership_problems` is tested per field (label, chart, version, namespace). The `keda_state_problems` cases are all covered.
- `is_day8_owned` and `delete_keda_namespace`. `is_day8_owned` has no direct unit test; it is covered only via the cleanup path. `delete_keda_namespace` has no direct refusal test either (LOW).

3. Access checks
- Exact matrices. `FORBIDDEN_IN_APP` (22 entries), `FORBIDDEN_SECRETS_ELSEWHERE` (5) and `PERSISTENT_ADDON_IDENTITIES` (3) are asserted with `assertEqual`. Cutting entries or dropping a subject is caught.
- `parse_can_i`. Treated strictly: rc/stdout combinations like (0,"no") and (1,"yes") raise, and warnings raise.
- `record_access_matrix`.
  - A single forbidden "yes" fails and names the identity and action.
  - A missing `EXPECTED_GRANTS` entry is flagged as stale documentation.
  - A probe error propagates.
  - A positive-control failure is tested for the KEDA RBAC check.
- Constants-test risk. The matrix equality tests partly re-assert constants, which this project's standards discourage. They are acceptable here because they pair with the behavioural matrix tests (LOW).
- T4: closed.

4. Build-record schema
- Collision test is real. `test_schema1_collided_across_versions` shows identical ids under schema 1 for 0.7.0 and 1.0.0, and `store_build` raises `BuildError` on the second.
- Schema 1 stays loadable. Legacy records re-derive with the original id via `build_from_record(legacy.record(), "0.7.0")`.
- Schema 2 distinct. `test_schema2_keeps_both_records` stores both records, which get distinct ids.
- Unknown schema. `test_unknown_schema_refused` refuses schema 3, and `build_id_for` with schema 2 and no version raises.
- LOW. One assertion in `test_schema2_keeps_both_records` is wrapped in `... if VERSION == "1.0.0" else None`, so it is a no-op if VERSION changes. No test loads a real stored schema-1 file from disk with a fixed historical id such as `fdb68741...`.

5. Claim-template labels: new MEDIUM (T8)
- Evidence.
  - Nothing in `scripts/validate_helm_chart.py`, `tests/test_validate_helm_chart.py` or any other test references `claimTemplateLabels` or `volumeClaimTemplates` labels.
  - `tests/test_validate_helm_chart.py` and `scripts/validate_helm_chart.py` only had their version constant changed in this diff.
  - I copied the chart to scratch, changed `state-statefulset.yaml:99` back to `maops.componentLabels`, and rendered it. The `volumeClaimTemplates` labels then follow the chart version and `scripts/helm_check.py` on that copy still passed 2346/2346.
- Severity. That regression would make the next chart-version upgrade fail at the API server, because `volumeClaimTemplates` is immutable. Only a live upgrade would catch it, and `helm --dry-run=server` does not (per CLAUDE.md).
- Suggested fix. Add a `validate_helm_chart` check that the state StatefulSet's claim-template labels have `app.kubernetes.io/version` of `0.7.0` and `helm.sh/chart` of `maops-kubernetes-platform-0.7.0`, plus a negative test that mutates one label. This is a test-gap MEDIUM and a one-liner to fix. Under the "no manufactured green" rule it should be fixed before the PR.

Original T3 and T5. Both are accepted in the adjudication (an AST scan for Secret fetches; `StableDay7ProtectionTests`). I did not re-verify them in depth because they are outside this focused scope.

Verdict: NO-GO until T8 is closed. It is a single static check plus one negative test. With that added, the other areas are closed and I would give targeted GO FOR PR (test perspective).
```

Implementer's check of T8 before fixing it: reproduced independently in a
scratch copy (state-statefulset.yaml:99 reverted to `maops.componentLabels`
renders `version: "1.0.0"` / `helm.sh/chart: maops-kubernetes-platform-1.0.0`
in the claim template; `helm_check.py` 2346/2346, `tests.test_validate_helm_chart` OK).
Confirmed MEDIUM. Remediation: `day-08-remediation-log.md` section 11.

---

# Round 3: focused re-review of the round-2 fixes (2026-10-04)

After the round-2 remediation (`day-08-remediation-log.md` section 11) and
run H (`0d158cfe2fc8453595d0185e004fcfaf`, exit 0), the owner asked for a
focused security and test re-review of those changes. The outputs are
preserved verbatim below, without leading indentation.

| Agent | Recommendation | New HIGH/MEDIUM |
|---|---|---|
| `kubernetes-security-reviewer` | targeted GO FOR PR | none; round-2 LOW 1 and LOW 2 closed |
| `kubernetes-test-engineer` | targeted GO FOR PR | none; T8 closed; round-2 LOWs closed |

**Process notes.**
- The test reviewer stopped partway with an API usage-limit error. It was
  resumed with its context intact.
- Before it resumed and after it finished, the implementing session
  checked every changed and untracked file (34 files) against a sha256
  snapshot taken before round 3 began. All were byte-identical both
  times. `git diff --stat` was unchanged, and nothing was staged.
- This round, every mutation ran in a fresh scratch copy. The command
  aborted if the copy or `cd` failed.
- One reviewer command (`rm -rf "$S"`) was blocked by the harness safety
  check and never ran. Nothing was deleted.

## R3.1 kubernetes-security-reviewer (round 3)

```
Focused re-review of the round-2 changes (day8_scaling.py, day8_addons.py, run H evidence, validate_helm_chart.py)

No new HIGH or MEDIUM findings. Round-2 LOW 1 and LOW 2 are both closed (details below).

1. `scripts/day8_scaling.py` uninstall path: PASS (VERIFIED by reading the code and run H log).
- `uninstall_keda` (L710-767) calls `_keda_namespace_state` first (L721). That function (L770-784) returns `None` (refuse) when the namespace read raises or when it lacks both Day 8 labels, and `uninstall_keda` then returns False (L722-723).
- The helm uninstall (L742), the `kedaorg-certs` Secret delete and the `operator.keda.sh` lease delete (L750-763) are all reachable only after that gate, so no delete or uninstall of anything in `keda` precedes it.
- It is fail-closed. `kubectl_json_or_none` (`day8_common.py:77-86`) returns `None` only on an explicit "(NotFound)", and any other error raises `Day8Error`, which becomes a refusal. There is no unreadable-to-absent path.
- The absent short-circuit (L747-749) runs only after the release-state step. A present release forces the ownership check, the CRD guard and the helm uninstall first. Release present with namespace absent is impossible, because Helm release records live in the namespace. If `helm_release_state` is unreadable, cleanup returns False (L726-728).
- `delete_keda_namespace` (L787-799) re-reads the namespace and re-checks both labels immediately before the delete.
- Residual gap (INFERRED, LOW): the namespace is checked at the start of `uninstall_keda` and again before the delete, but the Secret and lease deletes in between rely on the first check. This is only reachable if someone strips and re-adds labels on a namespace Day 8 owned, mid-run, under the Day 7 lock. Not blocking.

2. `scripts/day8_addons.py` `keda_preinstall` (L539-574): PASS (VERIFIED).
- The `checks.failed` early return (L558-560) comes before any mutation.
- It uses `kubectl create -f - -o json` (L563), never apply.
- An AlreadyExists error is recorded as a refusal (L564-567), so a namespace that appears between the get and the create is not adopted or relabelled. `create` never mutates an existing object, so the race is closed.
- The UID from the create response is compared with a re-read (L568-571), and the labels are then checked (L572-573).
- LOW (INFERRED): there is a gap between the preinstall and `helm upgrade --install keda` at Makefile L747-750. Helm is run without `--create-namespace`, which is correct. A hostile actor with cluster-admin could delete and recreate `keda` in that window and Helm would install into the new namespace. The result is not Day 8 adopting a foreign namespace. It only needs an actor who already holds delete-namespace rights, and everything runs under the Day 7 lock on a local kind cluster. Cleanup would then refuse on missing labels, so it fails closed. Accepted risk, not blocking.
- LOW (INFERRED): the get-then-create pair is not atomic with the CRD presence check, but both run under the lock.

3. Run H evidence (`0d158cfe2fc8453595d0185e004fcfaf`): PASS (VERIFIED in `make-day8-check.log`).
- Log L6679-6681 shows `namespace/keda created by Day 8 (uid e83843e9-…)`, then `is the one Day 8 just created (uid …, expected …)` with matching UIDs. The preinstall check ended 3/3.
- Cleanup, L6987: `namespace keda carries Day 8's identity labels (verified before any KEDA change)`. It appears before the release ownership check, the CRD guard, the helm uninstall and the Secret and lease deletes.
- L7001 and L7370 both show `all 33 KEDA chart-owned and runtime objects explicitly NotFound`, with present none and unreadable none.
- The log ends with `MAKE_EXIT=0`.
- A grep of the live-run log section for BEGIN, tls.key and "data" found nothing. The JSON evidence files (mode 600 in a 700 directory) contain no Secret material. The Secret is reached only by name and labels (`runtime_object_labels`, jsonpath `.metadata.labels`).
- One pattern grep matched the log file as a whole, probably the unit-test names that mention "token". I only checked the live-run section, not the whole log.

4. `scripts/validate_helm_chart.py:305-324` `_check_claim_template_labels`: PASS (VERIFIED). It reads only the rendered StatefulSet via `c.by_kind_name` and compares `volumeClaimTemplates` labels to the frozen set. It makes no cluster call and touches no secrets. It is security-neutral.

Round-2 LOW 1: CLOSED. The namespace gate now precedes every Secret, lease, uninstall and delete.
Round-2 LOW 2: CLOSED. `kubectl create` with the AlreadyExists refusal and the UID re-read removes the race. Only the cluster-admin-only helm window above remains (accepted LOW).

New LOWs: the two INFERRED notes above (mid-run label strip between the two namespace checks; delete-and-recreate window before helm). Neither is blocking.

targeted GO FOR PR (security perspective)
```

## R3.2 kubernetes-test-engineer (round 3)

```
Round-3 focused test re-review (test perspective). No repo writes; all mutations ran in scratch copies under .../scratchpad/r3_*.

RESULT: no new HIGH or MEDIUM. T8 is closed. Both round-2 LOWs are closed. There are 3 residual LOWs.

COUNTS (re-run, all OK)
- tests.test_day8: 164
- tests.test_validate_helm_chart: 59
- full `python3 -m unittest discover -s tests`: 1827, OK (64s)

1. T8 (scripts/validate_helm_chart.py:85-91 and 305-325, `_check_claim_template_labels`; tests/test_validate_helm_chart.py:403-444, `FrozenClaimTemplateLabelTests`)
- Revert demo: in a scratch copy I changed state-statefulset.yaml:99 back to `maops.componentLabels` and ran `scripts/helm_check.py`. It printed `[FAIL] claim_template.state.frozen_labels ... version='1.0.0' (expected '0.7.0'), helm.sh/chart='...-1.0.0' (expected '...-0.7.0')`. The check fires on the real rendered chart, and the message names the differing keys.
- `exactly_one` passes on the real chart. The unmutated chart is covered by the 59-test run and the 1827 full run.

| Validator mutant | Result |
|---|---|
| m2: compare only the version label | killed (2 failures) |
| m3: skip extra keys (iterate `expected` only) | killed (1) |
| m4: drop the `_check_claim_template_labels` call | killed (4) |
| m5: ignore the `instance` label | SURVIVED (LOW-1) |

2. Ownership tests (tests/test_day8.py:436-590; scripts/day8_addons.py:539-574; scripts/day8_scaling.py:710-790)
- Fidelity is good:
  - The fake `kubectl create` returns rc 1 with the real-style stderr `Error from server (AlreadyExists): ... namespaces "keda" already exists`.
  - The fake raises on any non-`create` verb, so an apply/label/patch fallback would be caught.
  - `race` flips the namespace to foreign only after the pre-install read.
  - `swapped_after_create` changes the uid after a successful create.
  - FakeCluster `keda_ns` and `runtime` model owned/foreign/absent, and it raises if a runtime Secret is fetched as a full object.
  - Refusal tests assert `created == []` and `calls == []`.

| Mutant | Killed by |
|---|---|
| a1: `create` -> `apply` | 3 tests (clean-cluster, race, swapped) |
| a2: drop the AlreadyExists message branch | race test |
| a3: skip the uid-equality check | swapped_after_create test |
| a4: never refuse before the mutation | crds-without-release test |
| a5: drop the post-create namespace label check | foreign-namespace test |
| a6: `uninstall_keda` ignores the namespace refusal | 3 cleanup tests (the new ones) |
| a7: absent namespace still falls through to runtime deletes | absent-namespace test |
| a9: drop the runtime-object label guard | 2 tests |
| a8: `delete_keda_namespace` foreign branch returns True instead of False | SURVIVED (LOW-2) |

3. Residual LOWs (none blocking)
- LOW-1: no test changes the claim template's `app.kubernetes.io/instance` label (m5 survived). Add a one-key mutation test asserting `frozen_labels` fails when instance is wrong.
- LOW-2: the TOCTOU re-check in `delete_keda_namespace` (day8_scaling.py:796, foreign namespace -> `return False`) has no direct test. `_keda_namespace_state` already refuses earlier, so this is defense in depth. A test that flips the namespace to foreign between the state read and the delete would pin it.
- LOW-3: `keda_preinstall` on the race path does not re-read the namespace after the failed create. That is fine, since it refuses. The fake only models create-after-read, not a delete-and-recreate between the get and the create.

INTEGRITY (start vs end, identical)
- `git diff --stat | tail -1`: `17 files changed, 972 insertions(+), 43 deletions(-)`, both times.
- `git status --short | wc -l`: 31, both times.
- mtimes unchanged:
  - scripts/day8_scaling.py: 11:23:47.485608465
  - scripts/day8_addons.py: 11:23:47.486057129
  - scripts/validate_helm_chart.py: 11:22:42.442163429
  - tests/test_day8.py: 11:24:40.971813988
  - tests/test_validate_helm_chart.py: 11:22:29.321342570
- Note: my first attempt used `rm -rf "$S"` and was blocked by the safety check. I did not run it and used fresh unique directories instead. Nothing was deleted.

targeted GO FOR PR (test perspective)
```

## Round 4: post-merge VPA fix (`fix/day-8-vpa-demonstration`, 2026-10-05)

**Scope.** A targeted independent review by the `kubernetes-test-engineer`
agent of the VPA correction made after merged-main run `f837802b…`
failed (remediation log section 12). It covered:
- the VPA policy invariant;
- the quota bounds;
- the cold-start regression tests;
- documentation accuracy.

**Process.**
- The reviewer was told to stay read-only on the repo and to mutate only
  in a `mktemp -d` scratch copy, after verifying `cd`.
- Before the review, the implementer took a sha256 snapshot of the 7
  modified files and a copy of the full `git diff`. Both matched after
  the review, so the reviewer made no repo change.

### R4.1 Original findings (kept as history)

**Verdict: targeted GO for PR, with no HIGH findings.** Summary of the
original report:

- **Invariant.** `vpa_change_problems` is logically correct. It refuses
  only when both declared requests lie inside `[minAllowed, maxAllowed]`,
  with inclusive bounds, and it returns early on min > max. It is
  consistent with the live `changed` verdict in `evaluate_vpa`.
  `vpa_ceiling` behaves exactly as before.
- **Inferred, not observed.** That VPA also caps at admission is stated
  from VPA's documented behaviour. The cold-start path was not observed
  live, and the docs say so.
- **Quota.** Still exactly `budget()`: 710m / 544Mi requests, 2000m /
  1088Mi limits, 13 Pods. Not padded. The floor (10m/48Mi, limits
  50m/96Mi) and the ceiling (40m/96Mi, limits 200m/192Mi) are within the
  LimitRange.
- **Mutations.**
  - Caught: M1 (minimum back to 32Mi), M2, M3, M4, M5, M6, M10.
  - Survived: **M7** (`within_bounds` ignores the memory minimum), **M8**
    (equality at max made exclusive), and M9 (ceiling always uses max;
    a gap older than this change).
- **MEDIUM 1.** No test pins `within_bounds` requiring at least 48Mi; M7
  survived with all 173 tests passing. The docs' claim is true in the
  code but was untested.
- **MEDIUM 2.** Equality at the upper bound (a declared request equal to
  `maxAllowed`) was untested; M8 survived.
- **LOW:**
  - an empty `min_allowed` / `max_allowed` override silently falls back
    to the defaults;
  - some tests compare against fixed values (48Mi, 96Mi);
  - no test covers a declared request above `maxAllowed`;
  - M9 survived;
  - one doc sentence said "equals the declared requests" where "both
    declared requests" is precise.
- **Documentation.** Every quoted number matches the run f837 and run I
  evidence JSON. The 12.4 test counts and the 12.5 mutation counts match
  the reviewer's own results.

The implementer reproduced M7 and M8 surviving in a separate scratch copy
before any fix.

### R4.2 Closure (tests and documentation only)

| Finding | Closed by | Proof (scratch copy outside the repo) |
|---|---|---|
| MEDIUM 1 (M7) | `VpaVerdictTests.test_within_bounds_memory_minimum_is_inclusive_and_enforced`: `within_bounds()` accepts exactly 48Mi (as `48Mi` and in bytes) and rejects 47Mi, 48Mi − 1 B and 32Mi; a below-minimum target and applied request fail both bound verdicts | M7 → this test fails. Also: the minimum made exclusive → this test and the cold-start test fail |
| MEDIUM 2 (M8) | `VpaChangeInvariantTests.test_declared_exactly_at_max_allowed_is_reachable`: declared requests exactly at `maxAllowed` (both, or memory alone) and `minAllowed == maxAllowed == declared` are all reported | M8 → this test fails |
| LOW (doc wording) | Remediation log 12.4 now reads "no valid recommendation can equal both declared requests" | - |

**What did not change.**
- Production scripts and the VPA configuration are byte-identical to
  run I (`f4e69ac6…`). The static-check message in
  `scripts/day8_addons.py` still says "equals the declared requests". It
  was left unchanged so the production code stays identical.
- The live assertion that admitted requests differ from the declared
  ones is unchanged.
- The other LOWs stay open and are accepted.

Tests: 173 → 175 in `tests/test_day8.py`; 1838 overall.

## Release disposition (2026-10-05)

The reviews above are preserved verbatim, including their findings and
their statements about unreleased status at the time. The Round 4
targeted re-review closed both MEDIUM test gaps before PR #12 merged;
the remaining LOW findings stay open and accepted.

Day 8 was subsequently released as
[`v1.0.0`](https://github.com/raiyan10/maops-kubernetes-platform/releases/tag/v1.0.0)
on 2026-10-05. PR #11 merged at `78b02a1`; PR #12 merged the VPA
correction at `4d74cfbdeca4bdc56ddc4a207508393f7d4ed438`, the fixed
target of the annotated tag (tag object `b1c0f00a…`). Corrected run I
(`f4e69ac6356545efb4bf040995ca4863`) passed. Run I was not a cold start;
the exact VPA floor case is covered by regression tests. The owner
reported the merged-`main` final gate exiting 0; only its stable 7/7 and
KEDA-absent results have saved files. The
[post-release verification record](day-08-post-release-verification.md)
is the current reference for release identity and evidence boundaries.

# Day 8 (v1.0.0 work) - remediation log

This log records what was changed in response to the five independent reviews
in `day-08-independent-reviews.md`, and how each change was proven. The
accept/defer decision for every finding is in `day-08-final-adjudication.md`.
The live results are in `day-08-live-validation-record.md`, section 10.

**Date:** 2026-10-03. **Branch:** `feature/day-8-autoscaling-hardening`,
base `3d19075e483d402179fe33d0c4fb0d7357a681b8`, every change unstaged.
Nothing was staged, committed, pushed, tagged or released.

Finding IDs: **A** = `kubernetes-architect`, **S** =
`kubernetes-security-reviewer`, **I** = `cluster-integration-engineer`,
**T** = `kubernetes-test-engineer`, **R** = `release-engineer`. For example,
T1 is the test engineer's finding 1.

---

## 1. CRD guard: namespace-aware test and exact argv (T1 HIGH; T2, A7, S10)

**Problem.** The fake cluster returned foreign objects whatever namespace
flags it received. Removing `--all-namespaces` from the namespaced listing
left every test green, while real kubectl would have listed only the
context namespace.

**Change.**
- `FakeCluster` has an explicit `CRD_SCOPES` table and
  `list_crd_instances()`, which applies kubectl's namespace rules:
  - with no flag, the context namespace `default`;
  - `-n <ns>`: that namespace;
  - `-A` / `--all-namespaces`: every namespace.

  Cluster-scoped types ignore namespace flags.
- `test_listing_argv_is_exact_for_each_scope` asserts the exact commands:
  - namespaced: `get <crd> --all-namespaces -o
    custom-columns=NS:.metadata.namespace,NAME:.metadata.name
    --no-headers`;
  - cluster-scoped: `get <crd> -o name`.
- `test_fake_models_context_namespace_listing` proves the fake hides a
  `team-a` object when the flag is missing. Without it, the next test could
  pass vacuously.
- `test_dropping_all_namespaces_would_be_caught` runs the guard with the
  flag removed and shows it misses the foreign object.
- `_crd_instances` now rejects a malformed row: anything other than 2
  tokens, or a `<none>` namespace on a namespaced type. It raises
  `Day8Error`, so `crd_instance_problems` reports the CRD as unreadable and
  cleanup keeps KEDA and the namespace (`test_malformed_row_blocks_uninstall`).
- An empty scope answer also blocks the uninstall
  (`test_empty_scope_answer_blocks_uninstall`).

**Proof (mutation).** With `--all-namespaces` removed in memory, 5 tests
fail. Before this change, that mutant survived.

## 2. KEDA release and namespace ownership (S1 MEDIUM, I1 MEDIUM, S5)

**Problem.** Install (`helm upgrade --install keda`) and cleanup (`helm
uninstall keda`) were keyed on name and namespace only. A pre-existing
non-Day-8 `keda` release would have been reset, then uninstalled.

**Change.**
- The release carries the Helm release label
  `maops-day8-owner=maops-kubernetes-platform-day8` (Makefile
  `DAY8_KEDA_OWNER_LABEL`, `--labels`).
- `day8_addons.py keda-preinstall` runs before the KEDA install. It refuses:
  - a `keda` release whose metadata (`helm get metadata -o json`) is not
    Day 8's: chart `keda`, version 2.21.0, namespace `keda`, owner label;
  - any of the six KEDA CRDs with no Day 8 release;
  - a `keda` namespace without both Day 8 identity labels.

  If none of these exists, it creates Day 8's own `keda` namespace,
  labelled `app.kubernetes.io/instance=maops-kubernetes-platform-day8` and
  `app.kubernetes.io/component=day8-keda`. `--create-namespace` is gone for
  KEDA; a static check (`makefile_keda_problems`) enforces that and the
  install order.
- Cleanup (`uninstall_keda`) checks the same release metadata before the
  CRD guard. On a mismatch it records "KEDA NOT uninstalled - the release
  is not Day 8's" and fails without uninstalling. `delete_keda_namespace`
  deletes `keda` only with both labels.
- `is_day8_owned(labels, component)` (both labels) is now used everywhere
  a namespace's ownership is decided: `phase_guards`, `require_guards`,
  `phase_cleanup`, the Makefile shell check, and `keda` (S5).

**Tests.** `KedaOwnershipTests`:
- the release, webhook and preflight rules;
- preinstall on a clean cluster, a foreign release, CRDs with no release,
  a foreign namespace, and Day 8's own release;
- cleanup with a foreign release and with a foreign `keda` namespace;
- the Makefile wiring.

## 3. Preflight checks foreign KEDA objects (I1 MEDIUM)

`day8_preflight.keda_state_problems()` makes a run start only from a clean
state: no `keda` release, none of the six KEDA CRDs, and no `keda`
namespace. Day 8's own leftover release is reported as an interrupted run,
with a pointer to `make day8-cleanup`; a foreign one is refused.
`test_preflight_state` covers each case.

**Live consequence.** The unlabelled `keda` namespace from run `1c59a36c…`
(created by the old `--create-namespace`) would now be refused. It was
inspected read-only:
- created 2026-10-03T09:37:39Z, UID `2251ebab…`;
- only `configmap/istio-ca-crl`, `configmap/istio-ca-root-cert`,
  `configmap/kube-root-ca.crt` and `serviceaccount/default`;
- no Helm release (`release: not found`, empty list);
- no KEDA CRDs.

It was deleted once, by name, about 30 s after the inspection. kubectl has
no UID precondition for `delete`; this is stated in the evidence.
Evidence: `day8-runs/8749aae3c3d34d799cc08d72fd0ac5e4/legacy-keda-ns-0{1,2}-*.txt`.

## 4. Access probes broadened, with explicit expected grants (A1 MEDIUM, T4 MEDIUM, S2, S3, S9)

- `FORBIDDEN_IN_APP` went from 8 to 22 actions in `maops-platform`:
  - Secrets get/list/watch;
  - `deployments/scale` and `statefulsets/scale` patch/update;
  - Deployments and StatefulSets patch/update/delete;
  - Pods create/patch/delete;
  - `pods/exec` and `pods/eviction` create;
  - ConfigMaps update/delete;
  - HPA create;
  - RoleBinding create.
- `FORBIDDEN_SECRETS_ELSEWHERE`: list Secrets in all namespaces; get
  Secrets in `kube-system`, `istio-system`, `maops-ingress` and
  `maops-day7-validation`. Cluster-wide and all-namespace probes use
  `--all-namespaces`.
- `PERSISTENT_ADDON_IDENTITIES`: Metrics Server and VPA's recommender and
  admission controller are probed in **both** modes (`active` and
  `after-cleanup`), not only while KEDA is installed.
- `EXPECTED_GRANTS`: the known, accepted grants are measured and must
  answer `yes`, so the exposure is recorded rather than only described:
  - keda-operator: patch APIServices, patch ValidatingWebhookConfigurations;
  - keda-webhook: list Deployments in all namespaces;
  - each persistent add-on identity: list Pods in all namespaces.

  A missing grant is reported as documentation drift.
- `parse_can_i` raises on any `Warning:` on stderr. This catches an unknown
  or misspelt resource, which kubectl answers with `no` (S3).
- **Tests** (`KedaRbacTests`):
  - the exact tuple of forbidden actions, so cutting it is caught;
  - warnings treated as errors;
  - the scope arguments;
  - all-denied plus grants present passes;
  - one `yes` fails, naming the identity and the action;
  - a missing grant is reported;
  - a probe error propagates;
  - a failed positive control is reported.
- **Measured read-only on the live cluster before run G:** the three
  persistent identities were denied every Secret and mutation probe, and
  each answered `yes` to `list pods -A`.

Documentation now says "denied for the probed verbs", not "cannot".

## 5. Remaining MEDIUM failure paths and Day 7 protection (T3, T5, T6, T7; S7)

- **Secret scan (T3).** `test_no_day8_script_fetches_a_secret_object` is
  now AST-based. It inspects every call expression in every `day8_*.py`,
  case-insensitively, including plural `secrets`, multi-line calls and
  variable kinds. `test_static_scan_catches_plural_and_multiline` proves
  that the scan catches the forms the old line-grep missed.
- **Day 7 protection (T5)** (`StableDay7ProtectionTests`):
  - a non-Ready, terminating or undeployed Pod fails;
  - `diff` catches nested restart and resource changes;
  - `check()` compares all seven sections;
  - a restart fails the check;
  - `observe_pods` captures UID, restarts, resources and imageID.

  With the Ready/terminating check removed, the mutant is now caught.
- **Failure paths (T6/T7, S7)** (`CleanupLowPathTests` and
  `CleanupLifecycleTests`):
  - a scaling namespace without both labels is not deleted;
  - a failed runtime-object delete keeps the namespace;
  - Pods that linger in `keda` keep the namespace;
  - an uninstall that leaves a survivor fails the inventory;
  - every cleanup exit writes `cleanup-<ts>.json`, and a failed write is a
    FAIL (`_cleanup_finish`);
  - a Secret with foreign labels is not deleted.

## 6. Small fail-closed and documentation fixes that overlap other findings

| Finding | Change |
|---|---|
| A6 / S8 | `kubectl_json_or_none` accepts only `(NotFound)` as absence (`test_strict_not_found`). |
| A5 | `KEDA_INVENTORY_CHART_VERSION = "2.21.0"` must equal the chart pin (`static_problems`). A comment says that a chart bump requires the inventory to be regenerated. The `keda` namespace was added to the inventory (31 chart and cluster objects, 33 with the runtime pair). |
| A4 | The budget docstring and architecture describe the quota-proof Pod as a deliberate allowance that never runs alongside other workloads. The "can never sit Pending" wording now reads "verified at the start of the run, summed across workers". |
| S4 | Dedicated ServiceAccount `day8-scaling` with `automountServiceAccountToken: false`, created by the guards. Every Pod sets `serviceAccountName` (`test_dedicated_tokenless_service_account`). |
| S6 | The `operator.keda.sh` lease is deleted by name only, in the Day 8-owned `keda` namespace. This is documented: the lease has no stable labels. |
| A8 | Roadmap: "Cleanup removes that namespace and uninstalls Day 8's own KEDA ...". |
| I2 | Interrupted-run recovery documented (architecture, "Interrupted-run recovery"; README; CLAUDE.md), including the manual finalizer escape hatch (I3). |
| R5 | Live record: superseded markers on the cluster-wide row, the "dormant" sentence and the VERSION line; "(at that time)" on the 1749 and 1776 test counts. |
| R6 | README: "for the probed verbs". |

## 7. KEDA webhook failure policy (A2 LOW): assessed, kept at `Fail`

The architect suggested `Ignore`. It was assessed against the live
webhook configuration instead of being applied:
- **Scope.** KEDA's six webhooks match only CREATE/UPDATE on `keda.sh` and
  `eventing.keda.sh`. A webhook outage cannot block any write outside KEDA,
  so `Ignore` buys no availability for the platform.
- **What `Fail` protects.** No ScaledObject is admitted without validation,
  including KEDA's own check for a second scaler on one target. That check
  backs up `scaler_conflicts()`.
- **Cost of `Fail`.** If the webhook is down during cleanup, the KEDA objects
  cannot be released, so cleanup fails closed and keeps everything. That is
  the documented escape-hatch case.

Decision: `webhooks.failurePolicy: "Fail"`, set explicitly in
`helm-values/day8/keda.yaml` with this rationale. It is pinned by
`static_problems`, and every live webhook's policy and groups are verified
by `keda_webhook_problems()` in active mode.

## 8. VERSION 1.0.0 preparation (R3, R4 MEDIUM)

- **Collision test first.** With the schema-1 build ID (a hash of the config
  digests only), a 1.0.0 build of byte-identical images got **the same ID**
  as the historical 0.7.0 build `fdb68741…`. It would have collided with that
  private record. `test_schema1_collided_across_versions` reproduces this.
- **Fix: build record schema 2** (`scripts/day7_build.py`). The ID includes
  `version=<v>`. `SUPPORTED_SCHEMAS = (1, 2)`, and a schema-1 record still
  loads with its original ID, so the 0.7.0 record is preserved and readable
  (`test_schema2_keeps_both_records`, `test_unknown_schema_refused`). The
  1.0.0 build is `70400e92ce4051f6b6e24bb89b41b93405cbdd2cbb3ace566f302ecfd28918dd`.
- **Immutable claim template (found while testing the rollout).** The state
  StatefulSet's `volumeClaimTemplates` carried `app.kubernetes.io/version`
  and `helm.sh/chart`. The field is immutable, so a 0.7.0 → 1.0.0 upgrade is
  rejected. This was shown with `kubectl apply --server-side
  --dry-run=server --force-conflicts --field-manager=helm` on the rendered
  StatefulSet: `Forbidden` for the unfrozen chart, accepted for the frozen
  one. `helm upgrade --dry-run=server` did **not** detect it. The labels are
  now frozen at their 0.7.0 values through `maops.claimTemplateLabels`.
- **Version files.** `VERSION` 1.0.0; Chart `version`/`appVersion` 1.0.0;
  `values.yaml` image tags 1.0.0; `validate_helm_chart.EXPECTED_VERSION`
  1.0.0. `version_check.RELEASE_TARGET_VERSION = "1.0.0"`, while
  `DAY7_TARGET_VERSION` stays 0.7.0 and is itself checked
  (`day7.historical_target_frozen`), giving 65/65. Tests now follow
  `VERSION` instead of hard-coding it.
- **`k8s/day7/` platform objects** keep their 0.7.0 labels: they are frozen
  platform manifests and are not reapplied.

## 9. Controlled Day 7 rollout to 1.0.0

Evidence directory: `day8-runs/8749aae3c3d34d799cc08d72fd0ac5e4/`
(`rollout-*`).

1. **`rollout-00-pre.json`**, read-only:
   - Helm revision 13;
   - StatefulSet `4ded2c43…` (generation 1);
   - 7 Pod UIDs;
   - PVC `2be6628b…`, PV `a9c74497…`;
   - `state.json` 15 B, sha256 `3ce4f556…`;
   - `/` and `/state` 200;
   - no autoscaling controls.
2. Build and load, every step exit 0:
   - `image-build`;
   - `day7-image-verify-local` 9/9;
   - `day7-build-record` (schema 2, build `70400e92…`);
   - `image-load`, `day7-image-load`;
   - `day7-image-verify-nodes` 31/31.
3. `day7-deploy` exit 0 (Helm revision 14, chart 1.0.0).
4. Post checks:
   - `day7-running-images` 47/47 on build `70400e92…`;
   - `ambient-workload-check` 67/67;
   - `rollout-check` 35/35;
   - `gateway-check` 8/8.

   **Operator error, recorded:** these three checks were first invoked
   without the Day 7 profile. They defaulted to the stopped
   `kind-maops-k8s-day6` context, failed at connect (connection refused,
   exit 2) and reached nothing. They were re-run under the Day 7 profile
   (the `09-*` logs). The failed logs are kept (`08-*`).
5. **`rollout-10-post.json` comparison.**
   - **Unchanged:** the PVC and PV UIDs, PV name and node, `state.json`
     size and sha256, the StatefulSet UID and claim-template labels, the
     Deployment UIDs, the Gateway and HTTPRoute, and the external `/state`
     body.
   - **Changed, as intended:** the three Pod-template digests, and the Helm
     revision 13 → 14.
   - **Pod UIDs:** all 7 Pods were replaced (0 of 7 UIDs retained),
     including `maops-state-0` (`95ebfd5a…` → `2cd7be95…`). That is
     required, because the pinned image tags changed. Health problems:
     none.

Run G captured its own new baseline from this post-rollout state.

## 10. Not changed (see the adjudication for the reasons)

- A3 (diagnostic snapshot on a failed demo);
- A9 (chart digest pinning);
- I4 (preflight double-count, which is conservative);
- I5 (Helm history growth);
- S-accepted TOCTOU between the CRD guard and uninstall.

Group subjects (S2) are covered by the access matrix, which measures
effective permissions, not by the binding audit.

## 11. Round-2 re-review remediation (2026-10-04)

The focused round-2 re-review (`day-08-independent-reviews.md`, "Round 2")
found one MEDIUM (T8) and several LOWs. The owner asked for T8, the two
targeted ownership paths (security LOWs 1 and 2) and the run G pointer to
be fixed.

### 11.1 T8: frozen claim-template labels are now validated

- `scripts/validate_helm_chart.py`: new `_check_claim_template_labels`,
  run on every render, including every Day 7 stage that `helm-check`
  renders. It requires:
  - the state StatefulSet to carry exactly one `volumeClaimTemplate`,
    named `data` (`claim_template.state.exactly_one`);
  - that template's labels to equal exactly
    `FROZEN_CLAIM_TEMPLATE_LABELS` plus the render's release instance:
    version `0.7.0`, chart `maops-kubernetes-platform-0.7.0`, name,
    part-of, managed-by and component `state`
    (`claim_template.state.frozen_labels`).

  Any added, removed or changed key fails.
- `tests/test_validate_helm_chart.py`: the baseline fixture now carries
  the claim template exactly as the chart renders it. The new
  `FrozenClaimTemplateLabelTests` covers:
  - baseline frozen labels pass while VERSION is 1.0.0;
  - version label set to VERSION fails;
  - chart label set to the current chart fails;
  - added label fails, and removed label fails;
  - missing template fails, and renamed template fails.
- **Proof.** In a scratch copy, `state-statefulset.yaml:99` was reverted to
  `maops.componentLabels`, the exact regression the reviewer
  demonstrated. `helm_check.py` now reports `[FAIL]
  claim_template.state.frozen_labels` for the default render and for
  every Day 7 stage (before the fix it passed 2346/2346). The real chart
  passes: `helm-check` 2364/2364.
- **Limitation recorded (architecture).** The frozen Day 6 release was
  created by chart 0.6.0, so its claim template carries 0.6.0 labels. An
  upgrade of that release to this chart would be rejected the same way.
  Day 6 is released and frozen, and nothing upgrades it.

### 11.2 Cleanup: `keda` namespace ownership checked before any KEDA change (security round-2 LOW 1)

- `uninstall_keda` now calls `_keda_namespace_state()` first, before the
  release lookup, the uninstall and the runtime-object loop:
  - **Unreadable answer:** refused; nothing in the namespace is touched.
  - **Present without both Day 8 identity labels:** refused, and the
    output says "KEDA NOT uninstalled; no runtime Secret, lease or
    namespace deleted".
  - **Absent:** the release step still runs (it finds nothing), then
    cleanup returns. No runtime object or Pod can exist, and no delete is
    attempted.
- `delete_keda_namespace` keeps its own label re-check immediately before
  the delete (defence in depth).
- **Behaviour change, deliberate.** A Day 8-labelled release in an
  unlabelled `keda` namespace (only possible from a pre-remediation
  `--create-namespace` install) is now refused before uninstall, where
  before it was uninstalled and the namespace kept. Recovery is manual and
  owner-approved, as for the legacy namespace in section 3.
- **Tests:**
  - `test_cleanup_refuses_foreign_keda_namespace`, now asserting no helm,
    runtime, keda-namespace or scaling-namespace delete;
  - `test_cleanup_without_release_leaves_foreign_runtime_objects_untouched`:
    no release, and a foreign namespace holding a `kedaorg-certs` Secret
    (even labelled `app=keda-operator`) and the lease. Neither object and
    not the namespace is deleted;
  - `test_cleanup_with_keda_namespace_absent_touches_nothing_in_it`.

### 11.3 Pre-install: create, never adopt (security round-2 LOW 2; test round-2 LOW)

- `keda_preinstall` stops **before any change** once a refusal is
  recorded (a foreign release, or CRDs with no Day 8 release). Before
  this fix it still went on to create the namespace.
- The namespace is made with `kubectl create -f - -o json`, never
  `apply`. A namespace that appears between the read and the create fails
  with `AlreadyExists` ("it appeared after the pre-install check -
  refusing to adopt it"). There is no fallback to apply, label or patch.
  The created object's UID is then re-read and must match; otherwise the
  install is refused.
- **Tests (each refusal asserts zero kubectl calls and zero objects
  created):**
  - foreign release;
  - CRDs without a release;
  - foreign namespace;
  - own release with an existing owned namespace (reused, not re-created);
  - `test_preinstall_create_race_fails_without_adopting` (one `create`
    attempt only, and the foreign namespace's UID and labels unchanged);
  - `test_preinstall_refuses_namespace_replaced_after_create`.

### 11.4 Mutation proof (run in an isolated scratch copy)

Every command first verified that the scratch directory existed and that
`cd` succeeded.

| Mutant | Result |
|---|---|
| Cleanup ignores the namespace refusal | caught (2 failures) |
| No short-circuit when `keda` is absent | caught (1) |
| Pre-install mutates after a refusal | caught (1) |
| `apply` instead of `create` | caught (2) |
| UID re-check skipped | caught (1), after `test_preinstall_refuses_namespace_replaced_after_create` was added; it survived before |

### 11.5 Docs

- `docs/architecture.md`:
  - the run G pointer (run G superseded run F; section 10);
  - the ownership paragraph (namespace checked first; create, never
    adopt);
  - the claim-template validator and the Day 6 limitation.

### 11.6 Gates and live run

- **Cluster-free gates.** `PATH=/usr/bin:$PATH make tool-check test
  version-check manifest-check helm-lint helm-check day8-static-check
  day8-plan` all exited 0. Results: 1827 unit tests OK (164 Day 8, 59
  Helm validator), version 65/65, manifests 267/267, Helm 2364/2364,
  Day 8 static 5/5.
- **Live run.** Run H is recorded in the live record, section 11.

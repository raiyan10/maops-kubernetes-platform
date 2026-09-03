# Project 4 / Day 2 - Kubernetes Test Engineering & Adversarial Validation Review

Reviewer role: kubernetes-test-engineer (independent).
Branch reviewed: `feature/day-2-service-discovery-secrets` (uncommitted working tree).
Target version: v0.2.0.
Scope: `scripts/` validation/test logic and `tests/`, held against `.claude/CLAUDE.md` ground
rules. No implementation files were modified. No commit/tag/release action was taken. All
adversarial mutations were performed against a throwaway copy of the repo under
`/tmp/claude-*/scratchpad/repo_copy`, which was deleted before this review was written; the
tracked working tree is unchanged.

This review is independent: the existing `day-02-cluster-integration-review.md`,
`day-02-kubernetes-architecture-review.md`, and `day-02-kubernetes-security-review.md` documents
were not opened or referenced.

---

## 1. Baseline counts (independently re-run, not trusted)

| Check | Reported | Actually observed |
|---|---|---|
| Unit tests (`python3 -m unittest discover -s tests -v`) | 163 | **163, `OK`** (verified via `Ran 163 tests in 0.442s` / `OK` in raw output) |
| Version checks (`python3 scripts/version_check.py k8s/base`) | 13 | **13/13 passed** |
| Manifest checks (`python3 scripts/manifest_check.py k8s/base`) | 94 | **94/94 passed** |

All three counts match exactly what was claimed. `make manifest-check` and `make version-check`
both wrap these same scripts (confirmed by reading the `Makefile`); no discrepancy between the
Makefile target and the direct script invocation.

---

## 2. Day 1 regression check

Sampled and re-ran the specific Day 1 protections named in the task:

- **Fail-closed YAML parsing** (`test_mis_indented_sibling_key_raises`,
  `test_trailing_unparseable_content_raises` in `tests/test_k8s_yaml.py`) - present, passing.
- **Flow-style rejection** (`test_flow_style_mapping_value_raises`,
  `test_flow_style_sequence_value_raises`, `test_flow_style_bare_sequence_item_raises`) - present,
  passing.
- **Namespace checking** - extended from one to four namespace-cross-check tests in
  `NamespaceCrossCheckTests` (`tests/test_validate_manifests.py`), covering both workloads'
  Deployments/ConfigMaps/Services and the Namespace object itself.
- **Pod security regression checks** - the Day 1 security-context checklist (non-root, seccomp,
  no privilege escalation, read-only root FS, capabilities drop ALL, no hostNetwork/hostPID/
  hostIPC/hostPort/hostPath, no automount) is present for **both** workloads in
  `scripts/validate_manifests.py` and exercised by `SecurityContextTests`.
- **SIGTERM cleanup tests** (`tests/test_portforward_signal.py`, 8 tests) - unchanged in intent,
  still send a real `SIGTERM` to the test process itself and assert conversion to a catchable
  exception plus signal-disposition restoration; all pass.
- **Monotonic waits** (`tests/test_kube.py`) - `WaitUntilMonotonicClockTests` still proves
  `time.time()` is never consulted and a backward wall-clock jump does not affect the bound;
  passes.
- **HTTP semantic checks** (`DAY1-TEST-M3` lineage) - `scripts/http_checks.py` still rejects a
  200 + valid-but-wrong-shape JSON body (`test_wrong_route_shape_on_livez_fails`,
  `test_livez_wrong_but_valid_json_body_fails`, etc.), and Day 2 added an explicit generic
  token-disclosure guard (`test_response_containing_token_field_fails_generically`).

No silent deletions or weakenings were found. Day 2 extends the Day 1 test surface (both
workloads get the full checklist applied via one shared `_check_workload_security_and_probes`
helper) rather than reducing it. `test_k8s_yaml.py`'s guardrail integration test was correctly
renamed and updated from four to **seven** documents
(`test_real_rendered_manifest_parses_into_seven_documents`) to match the Day 2 base
(Namespace, 2 ConfigMaps, 2 Deployments, 2 Services) and passes against the real
`kubectl kustomize` output.

**Verdict: Day 1 regression protection is intact and genuinely extended, not weakened.**

---

## 3. Manifest validation (`scripts/validate_manifests.py` / `tests/test_validate_manifests.py`)

The module and its 54 tests (grouped into 14 `TestCase` classes) are well-structured: every
negative test mutates exactly one field of a `copy.deepcopy(_base_docs())` fixture and asserts a
*specific* check name via `self.assertIn(<name>, failed)`, matching the required discipline.
Fixtures remain plain dict/list structures, not YAML strings.

Adversarial mutation testing (performed in a scratch copy) confirmed several checks the test file
does **not** directly exercise are nonetheless real and fire correctly when broken:

- `securityContext.runAsUser` mutated to `0` on the app Deployment -> `app.security.pod_run_as_user`
  failed as expected.
- `securityContext.runAsGroup` mutated to `0` on the gateway Deployment ->
  `gateway.security.pod_run_as_group` failed as expected.
- Removing the `maops-app-config` ConfigMap entirely -> `app.configmap.exists`,
  `app.configmap.namespace_matches`, and `app.configmap.no_secret_like_values` all failed as
  expected (defensive coding: absent-object accessors return `{}`, so downstream checks fail
  cleanly rather than raising).

All Day 2 checklist items from the task were independently confirmed discriminating:

missing gateway/app Deployment, wrong replica count (per-workload, asymmetric mutation proven not
to cross-contaminate the other workload's check), missing Service, Service selector crossing
workloads (both directions plus a same-workload selector-drift mutation), NodePort, LoadBalancer,
wrong image, version drift (image tag, Deployment label, pod-template label), wrong probes
(startup/liveness/readiness, plus the specific "liveness pointed at `/readyz`" circular-dependency
trap), wrong resources, security regression (11 distinct security-context checks touched), backend
host changed to a raw IP literal, backend host changed to a Pod/ReplicaSet-style hardcoded
identity, committed Secret object, missing/wrong Secret volume+mount (name, path, read-only,
`items` key restriction), ConfigMap credential-like key names, and 6 of the 10 forbidden later-day
kinds (`Secret`, `Ingress`, `PersistentVolumeClaim`, `ServiceAccount`, `NetworkPolicy`,
`StatefulSet` each individually proven; `Role`/`RoleBinding`/`ClusterRole`/`ClusterRoleBinding` are
covered only by the shared `scope.no_forbidden_resources` set-intersection mechanism, not by their
own dedicated mutation - low risk since it is the same code path already proven four other ways).

### Checks with no dedicated negative test (found by diffing every check name emitted by
`manifest_check.py` against every `assertIn(...)` in the test file)

Because `_check_workload_security_and_probes()` is a single shared function invoked once per
workload, most of these are simply "the mirror-image of an already-tested check on the other
workload" (e.g. `app.security.pod_run_as_non_root` is untested but `gateway.security.pod_run_as_non_root`
is; the underlying code path is identical). That symmetry makes most of the gap low-risk, but two
categories are worth flagging explicitly:

- **`*.security.pod_run_as_user` and `*.security.pod_run_as_group`** have **no test on either
  workload** - the only pair of checks in the entire security block with zero coverage on both
  sides. Confirmed still functional via manual mutation (above), but the test suite alone would
  not catch a regression here.
- **`gateway.configmap.exists` / `app.configmap.exists`** (the object-presence check, distinct
  from the namespace/no-secret-like-values checks on the same object) has no dedicated test
  either direction - only proven via my own scratch mutation.
- A long tail of exactly-mirrored checks untested on one side only (e.g.
  `gateway.deployment.namespace_matches`, `app.pod_template.version_label`, `app.deployment.image`,
  `gateway.deployment.image_pull_policy`, `gateway.probes.startup_path`, `app.probes.liveness_path`,
  `app.resources.requests`, `gateway.resources.limits`, `app.service.no_node_port`,
  `app.service.selector_matches_pod_labels`, `app.service.namespace_matches`,
  `gateway.secret.mount_path`, `app.secret.key_present`, `app.configmap.wired_to_container`,
  `gateway.deployment.container_name`, `app.deployment.single_container`) - each of these is
  proven on the *other* workload, so the check mechanism itself is real, but the test suite would
  not catch a workload-specific regression (e.g. a copy-paste bug that only breaks the app-side
  wiring while leaving the gateway-side wiring, and its test, green).

This is a test-completeness gap, not a false-green defect in the validator itself - every check I
mutated fired correctly. Recommend adding the missing `pod_run_as_user`/`pod_run_as_group` and
`configmap.exists` negative tests before the count is cited as "100% check coverage," and
consider a small helper/parametrization to close the one-sided mirror gaps cheaply.

---

## 4. Version checker (`scripts/version_check.py` / `tests/test_version_check.py`)

`DAY1-REL-I1` ("no automated guard cross-checks VERSION against the Makefile's hardcoded image
tag or the manifests' `app.kubernetes.io/version` labels") is explicitly named in both the
module docstring and the `Makefile`'s `version-check` target comment, and traced back to the Day 1
release-readiness review (`git log -p` on `docs/engineering-reviews/day-01-release-readiness-review.md`
shows the original finding ID `DAY1-REL-I1`). The fix is real, not cosmetic:

- `VERSION` is read exactly once (`read_version()`), and the Makefile itself derives both image
  tags from that same file (`VERSION := $(shell cat VERSION)`), so there is no second hardcoded
  literal anywhere to drift.
- `main()` renders the **actual** manifests via `kubectl kustomize` (same `render()` pattern as
  `manifest_check.py`) and parses them with the dependency-free `k8s_yaml` parser - it is not a
  hand-built mirror of expected values; the fixture-based unit tests (`run_version_checks`) are
  deliberately decoupled from parsing per project convention, but `main()`'s live path exercises
  the real renderer.
- Cross-checks both Deployment image tags and every rendered `app.kubernetes.io/version` label
  location, including pod-template labels that `kubectl kustomize` doesn't surface at the
  top level otherwise.

Adversarial mutation (scratch copy): setting `VERSION` to `0.9.9` correctly failed 12/13 checks
(only the count-of-locations check, which doesn't compare against `VERSION`, stayed green);
changing only the gateway Deployment's image tag to `0.1.0` correctly failed only
`version.maops-gateway.image_tag_matches_version` while every `maops-app`-scoped check stayed
green (i.e. does not accidentally cross-contaminate the other Deployment's checks) - confirmed by
direct execution, not by trusting the code.

`tests/test_version_check.py` covers: VERSION not bumped, image tag drift on each Deployment
independently, a missing Deployment (image check fails safe rather than raising/`None`-crashing),
metadata-label drift, pod-template-label drift, and namespace-label drift. Unrelated labels (e.g.
`app.kubernetes.io/managed-by`) are not asserted against by the checker's logic at all (only
`app.kubernetes.io/version` is inspected), so there is no realistic false-positive path there;
this isn't separately unit-tested but is a direct, obvious reading of `_collect_version_labels`.

**Verdict: DAY1-REL-I1 is genuinely closed and regression-protected** - confirmed by source
reading, unit tests, and live mutation against the real rendered manifest, not merely by trusting
the docstring's claim.

---

## 5. Secret bootstrap tests (`scripts/secret_bootstrap.py` / `tests/test_secret_bootstrap.py`)

14 tests across 6 `TestCase` classes, all mocking `secret_bootstrap.run` /
`secret_bootstrap.get_existing_secret` / `secret_bootstrap.create_secret` - never touching a real
cluster.

Discrimination confirmed for every item on the checklist:

- **Secret absent -> create**: `test_missing_secret_generates_and_creates_then_verifies`.
- **Secret exists -> preserve**: `test_existing_valid_secret_is_preserved_not_rotated` asserts
  `create_secret` is **never called** when a valid Secret already exists (not just that the exit
  code is 0).
- **Malformed existing Secret -> fail closed, no rotation**:
  `test_existing_malformed_secret_fails_closed_without_rotating` asserts exit code 1 **and**
  `create_secret` never called.
- **kubectl failure -> fail**: `test_other_kubectl_failure_raises` (unexpected `get` failure) and
  `test_create_secret_failure_reported_cleanly` (create failure) both covered.
- **Temp file cleanup**: `test_temp_file_removed_even_when_kubectl_create_fails` spies on the real
  `tempfile.mkstemp` and asserts the path no longer exists after a simulated `kubectl create`
  failure - a genuine `finally`-block proof, not a mocked-away assumption.
- **No plaintext output / no process-argument disclosure**:
  `test_generated_token_value_never_printed_on_success` captures real stdout via
  `redirect_stdout` and asserts the actual generated token string is absent from it. Source
  inspection confirms the token is written to a private (0600) temp file and passed to `kubectl`
  only via `--from-file=...`, never as a bare CLI argument - consistent with the non-disclosure
  claim, though this specific chmod call has no dedicated permission-bit assertion in the test
  suite (noted below).
- **Restrictive permissions**: `os.chmod(tmp_path, 0o600)` exists in `create_secret()` but is
  **not independently asserted** by any test (no `os.stat(...).st_mode` check). Low-severity gap;
  the surrounding cleanup/no-disclosure behavior is well covered.

### The claimed `-o json` regression (`get_existing_secret()`)

Confirmed via direct source read: `get_existing_secret()` calls
`run("-n", NAMESPACE, "get", "secret", INTERNAL_SECRET, "-o", "json", check=False)`. The test
`test_requests_json_output` in `GetExistingSecretTests` is a **genuine, persisted regression
test**: it replaces `secret_bootstrap.run` with a spy that records every positional argument, then
asserts `"-o"` and `"json"` are both present in the captured argument list. This test would
concretely fail if `-o json` were ever removed or the underlying `run()` call reordered to drop
it - it is not merely "a test that happens to pass," it directly inspects the call contract that
caused the original bug.

**Verdict: the secret bootstrap test suite is safe and its headline regression test is real.**

---

## 6. Secret checker (`scripts/secret_check.py`)

This is a live-cluster (tier 2) script per the project's two-tier validation rule and, correctly,
has no offline unit test file of its own - it composes `cluster_check.get_pods`,
`http_checks.check_all_endpoints`/`raw_get`, and `secret_bootstrap.get_existing_secret` against a
real cluster, matching the pattern of `smoke.py`/`discovery_check.py`/`dependency_check.py`.

Source-level adversarial reasoning against every requested false-green scenario:

- **Secret/key missing, empty token**: delegated to the already-covered
  `secret_bootstrap.validate_secret_shape`; `main()` aborts immediately
  (`if not check_secret_shape(): ... return 1`) rather than continuing with a `None` token into
  later checks that could silently downgrade to "no check performed = pass."
- **App/gateway mount missing, wrong mount path**: `_mount_findings()` explicitly records failure
  against expected Secret name / mountPath / `readOnly` - this is a straightforward re-assertion of
  live pod spec, not inference.
- **App accepts no-token / wrong-token**: `check_app_direct_auth()` asserts exactly HTTP 403 for
  both cases (`status == 403`, not merely `!= 200`), so a server that returns e.g. 500 or 200 on a
  bad token would correctly fail this check rather than silently pass.
- **Correct auth fails**: asserted as `status == 200` with a token-available guard - if no token
  was obtainable, this branch is recorded as a `False` finding, not skipped silently.
- **`/config` leaks token / logs leak token / tracked file contains token**: `check_all_endpoints`
  is reused (which includes `/config` and the generic "token" substring guard from
  `http_checks._check_semantics`), plus dedicated `check_logs_do_not_expose_token` (tails the last
  2000 log lines per pod, compares the **decoded** token string) and
  `check_repo_files_do_not_contain_token` (`git ls-files`-scoped, so it never scans `.git` internals
  or ignored/untracked scratch files, only tracked repository content).
- **Failure output does not itself disclose the live token value**: confirmed by reading every
  `record(...)` call site in `secret_check.py` - none interpolate the decoded token into a message;
  only byte-lengths (`len(f.read())`) are ever printed. This is consistent with the same discipline
  proven unit-testably in `secret_bootstrap.py`.

Because this script requires a live cluster, none of the above is exercised by
`python3 -m unittest discover`; it can only be evaluated by source reading, which is what was done
here. No false-green path was identified.

**Verdict: the Secret checker's design is safe against credential disclosure and against silently
passing an unauthenticated/misauthenticated request; it is untestable offline by design (correctly
so per the project's two-tier rule), so its correctness rests entirely on this source review and
on `secret_bootstrap.py`'s properly-covered helpers that it reuses.**

---

## 7. Auth application tests (`tests/test_app_auth.py`)

7 tests. `app/server.py` is loaded under an explicit unique module name
(`importlib.util.spec_from_file_location`) specifically to avoid colliding with `gateway/server.py`
in the same test run - a small but real correctness detail, confirmed by reading both files (both
define a top-level `INTERNAL_TOKEN` and `Handler`, which would otherwise collide under a bare
`sys.path` import).

Coverage: correct token valid, missing header (`None`) invalid, empty-string header invalid, wrong
token invalid, **same-length wrong token invalid** (the test most directly relevant to
`hmac.compare_digest`), no-secret-loaded (`INTERNAL_TOKEN is None`) rejects every request, and
`load_internal_token()` degrading to `None` (not raising) when the file is absent - all genuinely
exercise `_token_is_valid()` and `load_internal_token()` rather than re-asserting a constant.

On the specific ask - "confirm behavior itself is tested such that swapping `hmac.compare_digest`
for `==` would be caught": it would **not** be caught by any test in this suite. All the
correctness cases here (`_token_is_valid` returns `True`/`False`) produce byte-for-byte identical
results whether the internal comparison uses `hmac.compare_digest` or plain `==`, because timing-
side-channel resistance is not an externally observable functional property in a unit test (no
test measures wall-clock comparison time, nor would that be reliable in CI). This is an inherent
limitation of testing constant-time comparison, not a defect specific to this suite - flagged as
informational, consistent with the task's own acknowledgment that "at minimum" correctness should
still be caught (it is; only the timing property itself is untestable here).

**Verdict: functionally complete and correctly discriminating; the compare_digest-vs-`==`
distinction is a source-review-only property, as expected.**

---

## 8. EndpointSlice tests (`tests/test_endpointslice.py` / `scripts/endpointslice.py`)

8 tests, all pure-function, no live cluster needed. Confirmed discriminating for: ready=true
(counted), ready=false (excluded), ready-condition-absent (treated as not-ready, matching
kube-proxy semantics - explicitly documented and tested), multiple EndpointSlices (summed), zero
slices / zero endpoints (both forms tested: `[]` and `[{"endpoints": []}]`), multiple addresses
per single endpoint (each counted), and a realistic full-shape fixture matching real
`kubectl get endpointslices -o json` output (`test_real_shaped_two_ready_endpointslice_matches_expected_count`,
which also carries `serving`/`terminating` condition keys the counting logic legitimately ignores).

Two gaps, both low-severity and appropriately so given the task's own instruction not to demand
unnecessary complexity:

- **Duplicate addresses across two EndpointSlices** is not tested. The implementation has no
  deduplication (`count_ready_endpoints` is a pure sum), so if `kubectl` ever returned the same
  address in two overlapping slices (a legitimate transient state during EndpointSlice
  rebalancing), the count would double-count. This is a theoretical false-positive-toward-higher-
  readiness path, but it is not exercised anywhere that asserts an *exact* replica count against
  this function's output (`dependency_check.py` only checks `!= 0`, i.e. "fully drained," where
  over-counting would make the check *more* conservative, not less - so the risk is contained to
  hypothetical future callers, not the current ones).
- **Malformed/non-dict slice or endpoint entries** are not tested (e.g. `slices=None`,
  `endpoints` being a non-list). The code defensively does `s.get("endpoints", []) or []`, so
  `None` degrades gracefully, but a slice item that isn't a dict at all (`s.get` would raise
  `AttributeError`) is unguarded and untested. Since input always originates from
  `kubectl ... -o json` in the real callers, this is a low-realism edge case.

No false-positive path that would misreport actual pod readiness in the current call sites was
identified.

---

## 9. HTTP semantics (`scripts/http_checks.py` / `tests/test_http_checks.py`)

22 tests. Confirmed the "200 + valid JSON is not sufficient" bar is genuinely met:
`test_livez_wrong_but_valid_json_body_fails` and `test_readyz_wrong_but_valid_json_body_fails`
both feed a 200 status with a *valid but wrong* JSON body (`{"status": "ready"}` on `/livez` and
vice versa) and assert failure; `test_wrong_route_shape_on_livez_fails` simulates a routing bug
serving `/config`'s shape on `/livez`. Endpoint-specific values are asserted per role
(gateway vs. app `/config` key sets and `APP_NAME`/`BACKEND_HOST` values, `/backend`'s
`backend_service` field, `/`'s stable field set). A defense-in-depth generic "response body
mentions token anywhere" guard is tested independently of which endpoint served it. Status-before-
semantics ordering is explicitly tested
(`test_non_200_status_fails_before_semantics_are_checked`). `raw_get` (used for the auth-negative
checks in `secret_check.py`) is tested for both success and `HTTPError` paths.

No gaps identified here.

---

## 10. Dependency check testing (`tests/test_dependency_check.py` / `scripts/dependency_check.py`)

6 tests. The restoration-guarantee wrapper in `main()` (`try/except/finally` around
`run_experiment`, with `restore_app()` only invoked if `scale_app(0)` itself actually succeeded) is
genuinely exercised at the control-flow level, not just via helper functions:

- `test_restoration_invoked_when_experiment_raises` - `run_experiment` raises, `restore_app` is
  still called (`mock_restore.assert_called_once()`), and the run still fails (exit code 1).
- `test_restoration_not_attempted_when_scale_down_itself_never_happened` - `scale_app` itself
  raises before any pods were ever touched; `restore_app` correctly is **not** called (nothing to
  restore), and exit code is still 1.
- `test_restoration_failure_is_reported_prominently_and_fails_the_run` - a failing `restore_app`
  is recorded into `restoration_results` (a list distinct from the main experiment's `results`) and
  the run exits 1.
- `test_restoration_success_and_experiment_success_together_pass` - the clean-pass control path.

This correctly proves: restoration is attempted even after a preceding failure, and a restoration
failure is not swallowed (distinct exit code, distinct prominent stderr banner
`!!! RESTORATION FAILURE ... !!!` in `main()`, confirmed by reading the source).

**However**, the task specifically calls out a claimed "real termination race" fix. Reading
`run_experiment()`'s `_app_endpoints_drained()` closure shows the actual fix: waiting for the
EndpointSlice to report zero ready endpoints is **not** sufficient (a draining Pod can still
answer HTTP for a short window, and kube-proxy's iptables sync lags the API), so the real
predicate additionally requires `get_pods(APP_LABEL_SELECTOR)` to return empty before declaring
"drained." **This exact mechanism has no unit test.** Every test in
`tests/test_dependency_check.py` mocks `run_experiment` itself out entirely (via
`mock.patch.object(dependency_check, "run_experiment", ...)` or omits it), so `_app_endpoints_drained`
is never invoked, and no test would fail if that closure were reverted to checking only the
EndpointSlice (reintroducing the race). The persisted tests cover the **restoration-guarantee
wrapper** thoroughly, but not the **termination-race-fix mechanism** itself.

**Verdict: the restoration-guarantee test suite is strong. The termination-race-fix mechanism
itself is currently untested at the unit level - this is a real, fixable gap** (e.g. extracting
`_app_endpoints_drained`'s logic into a testable top-level function parameterized by
`get_json`/`get_pods` results, the same pattern already used successfully for
`count_ready_endpoints`, `get_restart_counts`, and the reconcile-check victim-selection logic).

---

## 11. Flakiness scan (entire new/changed test and script surface)

- `time.sleep` appears in exactly two places, both legitimate bounded-polling implementations
  (`scripts/kube.py:60` inside `wait_until`'s interval sleep, `scripts/portforward.py:76` inside
  `_wait_connectable`'s retry loop) - never inside a test file as a synchronization substitute.
- All deadline arithmetic in `wait_until` and `_wait_connectable` uses `time.monotonic()`, not
  wall-clock time; `tests/test_kube.py` explicitly proves `time.time()` is never consulted and that
  a simulated backward wall-clock jump has zero effect.
- No hardcoded local ports anywhere in `scripts/` or `tests/` - `portforward.py._free_port()` binds
  `("127.0.0.1", 0)` and lets the OS assign a free ephemeral port every time.
- No dependence on Kubernetes API list ordering:
  `tests/test_reconcile_check_failure_handling.py::test_victim_selection_stable_regardless_of_input_order`
  explicitly proves victim-pod selection is stable under reversed input order (sorted by
  `metadata.name`, matching the Day 1 `DAY1-INT-I1` fix carried forward).
- No exact ClusterIP value is ever asserted anywhere in the manifest/version checkers or the
  cluster-integration scripts (`discovery_check.py` explicitly asserts only
  `resolved_count >= 1`, "no specific IP required").
- No "N requests must hit both pods" load-balancing assumption exists anywhere in the reviewed
  surface.
- `scripts/portforward.py` guarantees cleanup via `try/finally` plus a SIGTERM-to-exception
  conversion (so a CI timeout's SIGTERM still unwinds cleanup), with a SIGTERM->5s wait->SIGKILL
  escalation in `_terminate()` - no unbounded subprocess wait, no dangling child process path
  identified. `dependency_check.py`'s `main()` similarly guarantees `restore_app()` via `finally`.

No flakiness concerns were found in the reviewed surface.

---

## 12. Test quality

- No tautological constant-vs-itself assertions were found (`grep` for patterns like
  `assertEqual(EXPECTED_X, EXPECTED_X)` or a bare re-assertion of a module constant against itself
  turned up nothing).
- Fixtures (`_base_docs()`, `_deployment()`, `_container()`, etc., in both
  `test_validate_manifests.py` and `test_version_check.py`) are plain, static data structures, not
  reimplementations of the checker's logic - they don't compute expected values from the same code
  path being tested.
- No assertion was found that would still pass if the checked-for logic were deleted/gutted from
  the source; every negative test's `assertIn(<specific check name>, failed)` pattern requires the
  named check to actually exist and actually fail, which was independently confirmed for every
  check I could locate a corresponding test for.
- The 163-test count growth (from Day 1's smaller suite) is proportionate to real new surface
  (2 workloads x ~40 shared checks, 5 new script modules, 2 new app-layer auth/HTTP semantics
  files) rather than padding; per-test discrimination (specific `assertIn` targets, specific mock
  call-argument assertions) is consistent throughout, not "some failure occurred" generic
  assertions.
- The one recurring quality issue is the one-sided-mirror gap described in Section 3 (and the
  single untested-both-sides pair, `pod_run_as_user`/`pod_run_as_group`) - a completeness gap, not
  a tautology or a deleted-code-still-passes issue.

---

## Findings

### Critical
None identified.

### High
- **DAY2-TEST-H1**: The termination-race-fix mechanism in `scripts/dependency_check.py`
  (`_app_endpoints_drained()`'s requirement that EndpointSlice-drained *and* all app Pods be fully
  terminated before proceeding) has no unit test anywhere - every test in
  `tests/test_dependency_check.py` mocks `run_experiment` out entirely. A regression that reverted
  this fix to checking only the EndpointSlice (reintroducing the exact race the implementation
  report claims to have fixed) would not be caught by the current test suite. Recommend extracting
  the drain predicate into a standalone, injectable function and adding a deterministic test
  (fake `get_json`/`get_pods` sequences: EndpointSlice empty but Pods still present -> not yet
  drained; both empty -> drained).

### Medium
- **DAY2-TEST-M1**: Two manifest-validation checks - `*.security.pod_run_as_user` and
  `*.security.pod_run_as_group` - have zero dedicated negative-test coverage on either workload
  (confirmed still functionally correct via manual scratch-copy mutation, but a future regression
  here would only be caught by luck, not by the test suite). `gateway.configmap.exists` /
  `app.configmap.exists` are similarly untested (also confirmed functional via mutation).
- **DAY2-TEST-M2**: A long tail of shared-workload checks in `validate_manifests.py` (listed in
  Section 3) are proven only on one workload, never the other, because the underlying check
  function is shared between gateway and app. A workload-specific regression (e.g. a copy-paste
  error introduced only into the app Deployment's YAML) that happens to land on one of these
  one-sided checks would not be caught, since only the already-tested workload's path is asserted.

### Low
- **DAY2-TEST-L1**: `create_secret()`'s `os.chmod(tmp_path, 0o600)` restrictive-permission call has
  no dedicated assertion in `tests/test_secret_bootstrap.py` (e.g. via `os.stat(...).st_mode`).
- **DAY2-TEST-L2**: `count_ready_endpoints()` has no dedicated test for duplicate addresses across
  overlapping EndpointSlices (would double-count) nor for malformed non-dict slice/endpoint
  entries; both are low-realism given the function's only real input source is
  `kubectl get endpointslices -o json`, and the current call sites' use of the result (`!= 0`
  drain check) is not sensitive to over-counting.
- **DAY2-TEST-L3**: Only 6 of the 10 `FORBIDDEN_KINDS` (`Secret`, `Ingress`,
  `PersistentVolumeClaim`, `ServiceAccount`, `NetworkPolicy`, `StatefulSet`) have their own
  dedicated mutation test in `ForbiddenResourceTests`; `Role`/`RoleBinding`/`ClusterRole`/
  `ClusterRoleBinding` rely on the same already-proven set-intersection check mechanism but have no
  test of their own.

### Informational
- **DAY2-TEST-I1**: `hmac.compare_digest` usage in `app/server.py._token_is_valid` cannot be
  behaviorally distinguished from a plain `==` by any unit test, since timing-side-channel
  resistance is not observable through functional assertions. This is an inherent limitation of
  testing constant-time comparisons, not a defect in `tests/test_app_auth.py`, whose correctness
  coverage (correct/missing/wrong/same-length-wrong/no-secret-loaded) is otherwise complete.
- **DAY2-TEST-I2**: `scripts/secret_check.py` (live-cluster tier) has no offline unit test file,
  correctly per the project's two-tier validation rule; its safety against false-greens and
  credential disclosure was confirmed by source review only (Section 6), not by an automated test
  that runs in this suite.

---

## Answers to the four required questions

1. **Are the unit/static checks meaningfully discriminating?** Yes, for the overwhelming majority
   of the 163 tests and 94+13 static checks. Every negative test sampled and independently
   verified asserts a specific named check failure, not a generic "something failed." The gaps
   found (Section 3, DAY2-TEST-M1/M2) are test-suite *completeness* gaps on already-real,
   already-functioning checks (confirmed via manual mutation), not evidence of cosmetic or
   tautological checks.

2. **Is DAY1-REL-I1 truly regression-protected now?** Yes. Traced the finding ID back to the Day 1
   release-readiness review via `git log -p`, confirmed `scripts/version_check.py` reads `VERSION`
   once and cross-checks it against the *real* `kubectl kustomize`-rendered manifest (not a
   hand-built mirror), and independently proved via scratch-copy mutation (VERSION bumped
   incorrectly, one Deployment's image tag drifted) that the checker fails exactly the expected
   subset of its 13 checks without cross-contaminating the other Deployment's checks.

3. **Is the Secret test strategy safe against credential disclosure?** Yes, for both the
   unit-testable `secret_bootstrap.py` (explicit non-disclosure test capturing real stdout and
   asserting the generated token's absence) and the live-cluster `secret_check.py` (source-review-
   confirmed: no `record()` call site ever interpolates the decoded token; failure messages report
   only byte-lengths). The one persisted regression test for the actual reported bug
   (`get_existing_secret()` missing `-o json`) is genuine - it inspects the real argument list
   passed to `run()`, not just a coincidentally-passing assertion.

4. **Is the dependency restoration/race test suite strong enough?** The restoration-guarantee
   portion is strong (four tests directly exercise the `try/except/finally` control flow at the
   `main()` level, proving restoration is attempted after a preceding failure and that a
   restoration failure is reported distinctly and fails the run). The termination-race-fix
   mechanism itself (`_app_endpoints_drained()`'s EndpointSlice-plus-Pods-gone requirement) is
   **not** covered by any test - this is the one High-severity gap in this review (DAY2-TEST-H1).

---

## Final verdict

**APPROVE WITH CONDITIONS**

The Day 2 validation/test surface is substantially strong: baseline counts are exact, Day 1
protections are intact and genuinely extended, the manifest/version checkers are real
(independently proven via live mutation, not just source-reading), the Secret bootstrap's headline
regression test is genuine, and HTTP/auth semantics go well beyond "200 + valid JSON." No Critical
findings and no evidence of manufactured/cosmetic green results were found anywhere in this
review.

Conditions before this is considered fully closed for v0.2.0 test coverage:

1. Add a deterministic unit test for the `_app_endpoints_drained()` termination-race mechanism in
   `scripts/dependency_check.py` (DAY2-TEST-H1) - this is the one gap that directly contradicts
   the implementation report's specific claim of a persisted regression test for a real bug fix.
2. Add the missing `pod_run_as_user`/`pod_run_as_group` and `configmap.exists` negative tests
   (DAY2-TEST-M1), and consider closing the one-sided mirror gaps (DAY2-TEST-M2) at least for the
   highest-value checks (image, replicas, probes).

None of the Low/Informational findings block approval; they are recorded for future cleanup.

PROJECT 4 DAY 2 KUBERNETES TEST REVIEW COMPLETE

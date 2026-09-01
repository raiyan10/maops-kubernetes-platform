# Project 4 / Day 1 - Kubernetes Test Engineering and Adversarial Validation Review

Reviewer role: independent test-engineering review of repository-owned
validation code (`scripts/`, `tests/`). Scope excludes live-cluster
design decisions (cluster-integration-engineer) and manifest design
(kubernetes-architect). This review is based solely on the current
uncommitted working tree on `feature/day-1-kubernetes-foundation`, and
was produced without reading any other Day 1 review report.

All findings below are backed by commands actually executed against
this working tree, including a live `kind-maops-k8s-day1` cluster that
was already running and had the Day 1 workload deployed (2/2 Ready
`maops-app` pods). No implementation files were modified.

## Baseline facts (actually run, not assumed)

- `python3 -m unittest discover -s tests -v` → **31 tests, all passed
  (`OK`)**. Exact breakdown: 9 tests in `tests/test_k8s_yaml.py`
  (`LoaderTests`), 1 baseline test + 21 negative-case tests in
  `tests/test_validate_manifests.py` (`BaselineTests` +
  `NegativeCaseTests`). 9 + 1 + 21 = 31.
- `python3 scripts/manifest_check.py k8s/base` → **30/30 static
  manifest checks passed**.
- `python3 scripts/cluster_check.py` (against live cluster, context
  `kind-maops-k8s-day1`) → **15/15 real cluster checks passed**.
- `python3 scripts/smoke.py` (real port-forward + real HTTP) →
  **4/4 smoke checks passed**.
- `python3 scripts/reconcile_check.py` (deleted one live pod, waited
  for the Deployment controller to reconcile, proved a new pod UID
  appeared, re-ran HTTP checks) → **8/8 reconciliation checks
  passed**.
- Live-check total: 15 + 4 + 8 = **27**, matching the reported figure
  exactly.
- `ps aux | grep port-forward` / `grep kubectl` after both `smoke.py`
  and `reconcile_check.py` ran → **no leftover processes**, confirming
  the `finally`-block cleanup in `scripts/portforward.py` works as
  designed under a real run, not just by code inspection.
- No manufactured `PASS` text was found: every `print("PASS: ...")` in
  `scripts/cluster_check.py`, `scripts/smoke.py`,
  `scripts/reconcile_check.py`, and `scripts/manifest_check.py` is
  reached only after an `if failures: return 1` guard, so it is never
  printed alongside a real failure.

---

## Findings

### High

```
ID: DAY1-TEST-H1
Severity: High
Title: Static validator never cross-checks metadata.namespace on Deployment/Service/ConfigMap against the expected namespace
Evidence:
  scripts/validate_manifests.py defines EXPECTED_NAMESPACE = "maops-platform"
  but greps confirm it is used exactly once, only against the standalone
  Namespace object's metadata.name (lines 66-71). No check reads
  `deployment.get("metadata", {}).get("namespace")`,
  `service.get("metadata", {}).get("namespace")`, or the ConfigMap's
  namespace field.
  Reproduced live against the real fixture builder
  (tests/test_validate_manifests.py:_base_docs()):
    docs = deepcopy(_base_docs()); Deployment.metadata.namespace = "default"
    -> run_checks(docs) -> zero failing checks
    docs = deepcopy(_base_docs()); del Service.metadata.namespace
    -> run_checks(docs) -> zero failing checks
  Both mutations render "30/30 checks passed" style output with no
  indication anything changed.
Impact: A regression that deploys maops-app (or its Service/ConfigMap)
  into the wrong namespace - or omits the namespace field entirely,
  which kubectl would then default to whatever namespace `kubectl apply`
  is invoked against - passes static validation with zero failures.
  Given this project's own kustomization.yaml has no top-level
  `namespace:` transformer (each resource manually declares its own
  `metadata.namespace`), this is the only mechanism keeping the four
  resources co-located in `maops-platform`, and nothing in the
  regression-catching net verifies it holds.
Required remediation: Add a check (e.g. `resource.namespace_matches`)
  that asserts `metadata.namespace == EXPECTED_NAMESPACE` for the
  ConfigMap, Deployment, and Service documents, with both a positive
  assertion in the baseline test and a negative test per resource type
  proving the check fires.
Release-blocking: NO (the current k8s/base/*.yaml manifests are
  correct on this dimension today - verified via the live cluster,
  which shows all resources correctly in the `maops-platform`
  namespace - so the release itself is not compromised; this is a gap
  in the regression net for future changes, not an active defect).
```

```
ID: DAY1-TEST-H2
Severity: High
Title: Several existing static checks have no dedicated negative unit test, including one explicitly named in this review's checklist (allowPrivilegeEscalation)
Evidence: Grepping tests/test_validate_manifests.py for
  `self.assertIn(` shows checks tested: deployment.replicas,
  service.type_cluster_ip, service.no_node_port,
  service.selector_matches_pod_labels, probes.readiness_path,
  probes.liveness_path, resources.requests, resources.limits,
  security.pod_run_as_non_root, security.read_only_root_filesystem,
  security.capabilities_drop_all,
  security.automount_service_account_token, security.no_host_network,
  security.no_host_port, configmap.wired_to_container,
  deployment.image_pull_policy, deployment.image, deployment.count,
  scope.no_forbidden_resources (x3), configmap.no_secret_like_values.
  The following checks that exist and run in
  scripts/validate_manifests.py have NO corresponding negative test:
  `security.allow_privilege_escalation`, `security.pod_run_as_user`,
  `security.pod_run_as_group`, `security.seccomp_profile`,
  `probes.startup_present`, `namespace.exists`, `configmap.exists`,
  `deployment.single_container`, `deployment.container_name`,
  `service.exists`.
  Verified by direct execution that all of these checks DO correctly
  fire when the relevant field is mutated (ran run_checks() against
  deep copies of _base_docs() with allowPrivilegeEscalation=True,
  runAsUser=0, seccompProfile deleted, and startupProbe deleted -
  each produced exactly the expected single failing check name), so
  the checks themselves are not broken - only untested.
Impact: This violates the project's own stated standard (see
  .claude/agents/kubernetes-test-engineer.md and
  .claude/skills/manifest-validation/SKILL.md): "When a new static
  check is added ... it needs both a positive assertion ... and at
  least one negative case proving the check actually fires." A future
  refactor of run_checks() (e.g. accidentally deleting or
  short-circuiting the allowPrivilegeEscalation check while editing
  an adjacent line) would not be caught by the test suite, even
  though this is precisely the kind of security-relevant regression
  the suite exists to prevent.
Required remediation: Add one negative test per untested check name
  listed above, following the existing single-field-mutation pattern.
Release-blocking: NO (all underlying checks are proven to work
  correctly today by direct execution; this is a test-coverage
  discipline gap, not a defect in the manifests or the checks
  themselves).
```

### Medium

```
ID: DAY1-TEST-M1
Severity: Medium
Title: scripts/k8s_yaml.py silently drops unconsumed/mis-indented content instead of failing closed
Evidence: `load_all()` calls `value, _ = _parse_block(lines, 0, base_indent)`
  and discards the second return value (the index of the last line
  consumed) without ever verifying that all lines of the document were
  actually consumed. Reproduced with a constructed input where a
  `securityContext:` block is indented one space short of its sibling
  keys:
    text = """apiVersion: apps/v1
    kind: Deployment
    spec:
      template:
        spec:
          containers:
          - name: app
            image: x
           securityContext:
              allowPrivilegeEscalation: false
              readOnlyRootFilesystem: true
    """
    k8s_yaml.load_all(text)
  produced a container dict containing only {"name": "app", "image": "x"}
  - the entire securityContext block vanished with no exception, no
  warning, and no non-empty return-value discrepancy visible to the
  caller.
Impact: The module's own docstring states it "parses exactly that
  subset," implying rejection of anything outside it, but the actual
  behavior on malformed/inconsistently-indented input is silent data
  loss, not a raised error. In THIS specific PoC the dropped block
  happened to contain security-positive fields, so the resulting
  "missing securityContext" would (correctly, if accidentally) fail
  downstream security checks - but the mechanism is not guaranteed to
  fail safe in general; a differently-shaped indentation slip could
  just as easily drop a validation-relevant field while leaving
  everything else looking structurally valid.
  Practical exposure today is mitigated because the only production
  caller (`scripts/manifest_check.py`) always feeds this parser
  `kubectl kustomize`'s own machine-generated, consistently-indented
  output (confirmed empirically: `kubectl kustomize k8s/base` and two
  ad hoc scratch kustomizations all produced clean, uniform 2-space
  block-style YAML with alphabetized keys), and the integration test
  `test_real_rendered_manifest_parses_into_four_documents` guards that
  specific real-world shape. But nothing in the code or tests asserts
  or documents this "trusted machine-generated input only" invariant,
  and there is no test proving the parser fails loudly on malformed
  input.
Required remediation: Have `load_all()` (and/or `_parse_block`/
  `_parse_mapping`) raise a `ValueError` when a document's lines are
  not fully consumed by the recursive descent, and add a unit test
  asserting a malformed/inconsistently-indented input raises rather
  than silently truncating.
Release-blocking: NO (the real pipeline only ever feeds this parser
  kubectl-kustomize-rendered output, which was independently confirmed
  to always produce consistent indentation and is guarded by the
  existing real-render integration test).
```

```
ID: DAY1-TEST-M2
Severity: Medium
Title: Flow-style YAML is not explicitly rejected by the parser; unsupported input relies on incidental downstream crashes rather than a clear parse-time error
Evidence: For a mapping value written in flow style (e.g.
  `capabilities: {drop: [ALL]}` on one line), `_scalar()` has no flow
  syntax handling, so the entire `{drop: [ALL]}` text is returned as a
  literal Python string rather than a dict. Downstream,
  `validate_manifests.py` does `(container_sc.get("capabilities") or {}).get("drop")`,
  which raises an unhandled `AttributeError` on a plain string.
  Empirically confirmed that the actual toolchain in this environment
  (`kubectl v1.36.3` / bundled `Kustomize v5.8.1`) always canonicalizes
  flow-style input back to block style on render - tested with a
  scratch Deployment written entirely in flow style
  (`containers: [{name: c, image: nginx, ports: [8080]}]`) and
  `kubectl kustomize` emitted clean block-style YAML - so this is not
  currently reachable through the project's actual `make manifest-check`
  path.
Impact: If a future kubectl/Kustomize version, or a different renderer,
  ever preserves flow style in its output (kyaml's RNode style
  preservation is a documented behavior in some Kustomize code paths),
  `scripts/k8s_yaml.py` would not report a clean "unsupported YAML
  construct" error; it would crash with an unrelated `AttributeError`/
  `TypeError` deep inside `validate_manifests.py`, which still fails
  the build (non-zero exit) but with a confusing stack trace instead
  of an actionable message, and with no test coverage proving this
  behavior is intentional/stable.
Required remediation: Have `_scalar()` (or `_split_key_value`)
  explicitly detect a value starting with `{` or `[` and raise a clear
  `ValueError("flow-style YAML is not supported by this loader")`
  rather than silently returning a string.
Release-blocking: NO (verified unreachable via the current toolchain
  version pinned in this project's actual render path).
```

```
ID: DAY1-TEST-M3
Severity: Medium
Title: HTTP checks (scripts/http_checks.py) validate transport-level success only, not response content
Evidence: `check_endpoint()` asserts `status == 200` and that the body
  parses as JSON (`json.loads(body)`), but never asserts on the parsed
  content. For example, `/readyz`'s expected body
  `{"status": "ready"}` is never checked against `check_all_endpoints`'s
  result for `/readyz` specifically - any 200-status, JSON-parseable
  body (even the wrong one, e.g. `/livez`'s body served from the wrong
  route due to a routing bug) would be recorded as PASS.
Impact: A regression in `app/server.py` that returns HTTP 200 with
  semantically wrong content (e.g. `/readyz` always returning
  `{"status": "alive"}` instead of gating on the `READY` flag) would
  not be caught by `smoke.py` or the post-reconciliation HTTP checks in
  `reconcile_check.py`, even though the explicitly-required scenario
  "/readyz returned non-200" (a status-level regression) is correctly
  caught.
Required remediation: Extend `check_endpoint`/`check_all_endpoints`
  with a per-path expected-field assertion (e.g. `/livez` body must
  contain `"status": "alive"`, `/readyz` must contain `"status": "ready"`
  post-startup-delay, `/config` must contain the ConfigMap-sourced
  `APP_NAME`/`APP_MESSAGE` keys).
Release-blocking: NO (the current live run confirms all four endpoints
  return correct, expected content today; this is a coverage gap in
  what future regressions the smoke test would catch, not a current
  defect).
```

```
ID: DAY1-TEST-M4
Severity: Medium
Title: Bounded waits use wall-clock time.time() instead of a monotonic clock
Evidence: `scripts/kube.py:wait_until()` computes
  `deadline = time.time() + timeout` and loops `while time.time() < deadline`.
  `scripts/portforward.py:_wait_connectable()` does the same pattern
  with `time.time()`. Neither uses `time.monotonic()`.
Impact: `time.time()` reflects the system (wall) clock and can jump
  backward or forward due to NTP synchronization, clock-skew
  correction, or container/VM clock adjustments - all realistic in a
  CI runner. A backward jump could make a bounded wait (e.g. the 120s
  Deployment-Available wait in cluster_check.py, or the 120s
  reconciliation wait in reconcile_check.py) hang far longer than
  intended; a forward jump could make it time out prematurely on a
  slow-but-otherwise-healthy CI runner. This is exactly the kind of
  latent CI flakiness source this review was asked to look for.
Required remediation: Replace `time.time()` with `time.monotonic()`
  for all deadline/elapsed-time computations in `scripts/kube.py` and
  `scripts/portforward.py`.
Release-blocking: NO (not observed to cause a failure in this
  environment's live run; this is a forward-looking CI-flakiness risk,
  not a current defect).
```

### Low

```
ID: DAY1-TEST-L1
Severity: Low
Title: probes.startup_present only checks that an httpGet block exists, not its path
Evidence: scripts/validate_manifests.py:
  `c.check(bool(startup.get("httpGet")), "probes.startup_present", ...)`
  - unlike `probes.liveness_path` and `probes.readiness_path`, which
  assert the exact path (`/livez`, `/readyz`), this check never
  inspects `startup.get("httpGet", {}).get("path")`.
Impact: A startupProbe pointed at a nonexistent or wrong path (e.g.
  `/wrong-path`) would still pass static validation, even though the
  actual k8s/base/deployment.yaml correctly uses `/livez` for the
  startupProbe today.
Required remediation: Extend the check to assert
  `startup.get("httpGet", {}).get("path") == "/livez"` (matching the
  actual manifest's intent of using the liveness path for startup
  gating), with a negative test.
Release-blocking: NO.
```

```
ID: DAY1-TEST-L2
Severity: Low
Title: wait_until()'s truthy-result check would misinterpret a legitimately falsy successful value as "not ready"
Evidence: scripts/kube.py: `value = predicate(); if value: return value`.
  Any predicate that could legitimately signal success with `0`, `""`,
  or `[]` would be treated as "condition not yet met" and retried
  until timeout.
Impact: Not currently triggered - every predicate in
  cluster_check.py/reconcile_check.py returns a truthy success value
  (a non-empty dict, a non-empty pod list, or `EXPECTED_REPLICAS`
  which is 2). But it is a latent footgun for a future day's check
  (e.g. "0 pod restarts" or "0 NotReady pods" as a success condition).
Required remediation: Change the sentinel convention to explicit
  `None` checks (`if value is not None: return value`) so a falsy-but-
  valid success value is not misinterpreted as failure.
Release-blocking: NO.
```

```
ID: DAY1-TEST-L3
Severity: Low
Title: Some live-check subprocess calls have no exception handling around kubectl exec/delete, so an unexpected kubectl failure crashes the script with an unhandled traceback instead of a clean itemized FAIL
Evidence: `scripts/kube.py:run()` defaults to `check=True`.
  `cluster_check.py:exec_in_pod()` and `reconcile_check.py`'s
  `run("-n", NAMESPACE, "delete", "pod", victim, "--wait=false")` call
  this without a try/except. `exec_in_pod` hardcodes
  `/usr/bin/python3.11` as the in-container interpreter path, which
  matches app/Dockerfile's ENTRYPOINT today, but would break this
  specific call path if a future base-image bump changes the Python
  minor version baked into the distroless image.
Impact: If such a call ever fails unexpectedly, the whole script exits
  via an unhandled `CalledProcessError` traceback rather than the
  script's own itemized `[FAIL] ...` reporting format. The script
  still exits non-zero (so CI would still correctly detect the
  failure), but loses the clean pass/fail summary and skips any
  remaining checks in that run.
Required remediation: Wrap exec/delete calls that could plausibly fail
  in try/except and route into the existing `record(False, ...)`
  reporting path, consistent with the rest of the script.
Release-blocking: NO.
```

```
ID: DAY1-TEST-L4
Severity: Low
Title: Scalar type coercion in k8s_yaml.py does not implement YAML 1.1 ambiguous-scalar rules or escaped-quote handling, and this safety margin is implicit rather than tested/documented
Evidence: `_scalar()` only recognizes `true`/`True`/`false`/`False` as
  booleans (not `yes`/`no`/`on`/`off`/`TRUE`/`FALSE`/etc. per YAML 1.1),
  and `_split_key_value`'s quote-tracking does not handle backslash-
  escaped quotes inside a quoted scalar. Empirically confirmed this is
  currently safe in practice: a scratch ConfigMap with values `"1.0"`,
  `"yes"`, `"on"` rendered through `kubectl kustomize` came back
  correctly re-quoted (`"1.0"`, `"yes"`, `"on"`) in the output text, so
  `k8s_yaml.py`'s quote-based string detection (checked before the
  int/float regexes) correctly preserves them as strings. This means
  the parser's simplified typing heuristic is safe only because it
  relies on kubectl/Kustomize's own YAML serializer to disambiguate
  type-ambiguous scalars via quoting before this parser ever sees them.
Impact: This reliance is real, currently correct, and empirically
  verified - but it is nowhere asserted or tested, and the module's
  docstring doesn't call it out as a design invariant. A reviewer
  reading only k8s_yaml.py in isolation would reasonably worry about
  the "Norway problem" (unquoted `no`/`on`/`1.0` being misparsed); this
  review confirms that specific worry does not currently materialize
  through the real render path, but the reasoning for why not lives
  only in this review, not in the codebase.
Required remediation: Either add a docstring note explicitly stating
  the parser depends on upstream (kubectl/Kustomize) quoting discipline
  for type-ambiguous scalars, or add a unit test that pins this
  behavior (e.g. a fixture asserting `"1.0"`/`"yes"`/`"on"` stay
  strings when quoted, and documents that unquoted equivalents are out
  of scope).
Release-blocking: NO.
```

### Informational

```
ID: DAY1-TEST-I1
Severity: Informational
Title: Full test/validation suite independently re-run and confirmed green, including against a real live cluster
Evidence: 31/31 unit tests pass; 30/30 static manifest checks pass;
  27/27 live cluster checks pass (15 cluster_check.py + 4 smoke.py + 8
  reconcile_check.py) against the actually-running
  `kind-maops-k8s-day1` cluster, including a real pod deletion and
  observed reconciliation (new pod UID `b21ffb7d-...` replaced deleted
  pod, untouched pod UID `f5fcd625-...` survived unchanged, Service
  continued serving 200s on all four endpoints afterward). No
  manufactured PASS text or overly broad/swallowed exception handling
  was found in any of the live-check scripts. Port-forward subprocess
  cleanup was verified with `ps aux` after both live HTTP-check runs -
  zero leftover kubectl processes.
Impact: Positive - this is strong, reproducible evidence that the
  Day 1 validation and workload are genuinely functioning, not just
  self-consistently claiming to.
Required remediation: None.
Release-blocking: NO.
```

```
ID: DAY1-TEST-I2
Severity: Informational
Title: Port selection and wait patterns avoid the classic flakiness anti-patterns this review was asked to check for
Evidence: `scripts/portforward.py:_free_port()` binds to port 0 to get
  an OS-assigned free local port rather than a hardcoded port number.
  No fixed/unconditional `time.sleep(N)` stands in for a real readiness
  check anywhere in `scripts/`; every bounded wait polls real live
  cluster state via `kubectl -o json` through `wait_until()`.
Impact: Positive - directly addresses two of the specific anti-patterns
  called out in this review's Section 5 scope ("hardcoded ports",
  "fixed sleeps").
Required remediation: None.
Release-blocking: NO.
```

```
ID: DAY1-TEST-I3
Severity: Informational
Title: Forbidden-kind checks for StatefulSet/RBAC/NetworkPolicy are proven correct by code inspection but have no dedicated per-kind negative test
Evidence: `FORBIDDEN_KINDS` includes `StatefulSet`, `Role`,
  `RoleBinding`, `ClusterRole`, `ClusterRoleBinding`, `ServiceAccount`,
  and `NetworkPolicy`, all routed through the single
  `scope.no_forbidden_resources` set-intersection check. Only
  `Ingress`, `PersistentVolumeClaim`, and `Secret` have dedicated
  negative tests, but the check mechanism (`present_kinds &
  FORBIDDEN_KINDS`) is a single, already-exercised code path, so the
  remaining kinds are covered by the same proof, just not individually
  enumerated in the test file.
Impact: Low actionable risk since the mechanism is already proven; a
  future refactor that replaced the set with an explicit per-kind
  if/elif chain (unlikely, but possible) could regress an untested
  kind without detection.
Required remediation: Optional - add one negative test covering an
  arbitrary remaining forbidden kind (e.g. NetworkPolicy) purely for
  documentation/completeness value.
Release-blocking: NO.
```

---

## FINAL VERDICT

**Verdict: APPROVE WITH CONDITIONS**

**Severity counts:**
- Critical: 0
- High: 2 (DAY1-TEST-H1, DAY1-TEST-H2)
- Medium: 4 (DAY1-TEST-M1, DAY1-TEST-M2, DAY1-TEST-M3, DAY1-TEST-M4)
- Low: 4 (DAY1-TEST-L1, DAY1-TEST-L2, DAY1-TEST-L3, DAY1-TEST-L4)
- Informational: 3 (DAY1-TEST-I1, DAY1-TEST-I2, DAY1-TEST-I3)

**Are the 31 unit/static tests meaningfully discriminating?**
Yes, for what they cover. Verified the count directly:
`python3 -m unittest discover -s tests -v` reports "Ran 31 tests ...
OK" (9 in `test_k8s_yaml.py`, 22 in `test_validate_manifests.py`).
Every negative test in `test_validate_manifests.py` mutates exactly
one field of a deep copy of `_base_docs()` and asserts a specific
check name fails (confirmed by inspection - no test merely re-asserts
a constant against itself), which meets this project's own stated
standard. Adversarial re-runs of several of the checks confirmed they
genuinely fire when mutated even where no dedicated test exists yet
(DAY1-TEST-H2). However, "meaningfully discriminating" has two real
gaps: (1) there is no check at all - tested or not - for cross-resource
namespace consistency (DAY1-TEST-H1), and (2) about ten existing,
correctly-functioning checks currently lack any negative test proving
they fire (DAY1-TEST-H2), which is a real violation of this project's
own documented test-engineering standard even though the underlying
checks were independently verified to work correctly today.

**Are the 27 reported live checks credible?**
Yes - independently re-run, not just traced in code. This review
executed `scripts/cluster_check.py` (15 checks), `scripts/smoke.py`
(4 checks), and `scripts/reconcile_check.py` (8 checks) against the
actual live `kind-maops-k8s-day1` cluster that was already running
with the Day 1 workload deployed, for a total of 27/27 passing,
matching the figure exactly. This included a genuine controller-
reconciliation proof (a real pod was deleted, a new pod UID was
observed replacing it, the untouched pod's UID was confirmed
unchanged, and the Service was proven to keep working afterward via
real HTTP calls through a real, cleanly-torn-down port-forward). No
manufactured PASS text or inappropriately broad exception handling was
found masking a real failure anywhere in the live-check scripts.
Weaknesses found are about what these checks fail to assert (shallow
HTTP content checks in DAY1-TEST-M3; wall-clock timing in
DAY1-TEST-M4), not about their current results being fabricated or
unreliable.

**Is the zero-third-party-dependency validation strategy safe enough
for Day 1?**
Largely yes, for the narrow, self-controlled pipeline this project
actually uses (`kubectl kustomize` → `scripts/k8s_yaml.py` →
`scripts/validate_manifests.py`). Direct empirical testing (not just
code reading) confirmed the real toolchain in this environment
(kubectl v1.36.3 / bundled Kustomize v5.8.1) always renders
consistently-indented block-style YAML with type-ambiguous scalars
correctly re-quoted, which is exactly the subset `k8s_yaml.py` was
designed to parse, and the existing integration test
(`test_real_rendered_manifest_parses_into_four_documents`) guards that
real shape. That said, the parser does not fail closed by design - it
fails closed today only because its one production input source
happens to always be well-formed (DAY1-TEST-M1, DAY1-TEST-M2). For a
single-day, single-toolchain, repository-scoped validator this is an
acceptable and pragmatic trade-off consistent with the project's
"native tooling only" ground rule, but it is a strategy that depends
on an implicit trust assumption about its only caller rather than on
the parser's own defensiveness, and that assumption should be made
explicit (docstring/test) rather than left to reviewer archaeology.

None of the findings in this review contradict the manifests or the
live cluster's actual, currently-correct state. The High and Medium
findings are coverage/robustness gaps in the validation net for future
regressions, not evidence that the current v0.1.0 release state is
broken - hence APPROVE WITH CONDITIONS rather than REJECT. The
conditions are: add the namespace cross-check (DAY1-TEST-H1) and the
missing negative tests (DAY1-TEST-H2) before/alongside the next
manifest-touching change, since both are cheap, well-understood fixes
that close real gaps in exactly the mechanism this project relies on
to catch regressions.

PROJECT 4 DAY 1 KUBERNETES TEST REVIEW COMPLETE
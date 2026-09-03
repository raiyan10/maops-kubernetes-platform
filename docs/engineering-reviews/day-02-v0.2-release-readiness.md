# Project 4 / Day 2 v0.2.0 Final Engineering Adjudication

**Role:** Final adjudication, not a sixth independent review. This
document reads the five independent Day 2 reviews as immutable
point-in-time evidence and independently re-inspects the current
(remediated) working tree to decide whether every finding those reviews
raised is genuinely closed.

**Repository:** `maops-kubernetes-platform`
**Branch:** `feature/day-2-service-discovery-secrets`
**Target:** v0.2.0
**Adjudication date:** 2026-09-03

---

## 1. Scope and evidence basis

This adjudication read, in full:

- `docs/engineering-reviews/day-02-kubernetes-architecture-review.md`
- `docs/engineering-reviews/day-02-kubernetes-security-review.md`
- `docs/engineering-reviews/day-02-cluster-integration-review.md`
- `docs/engineering-reviews/day-02-kubernetes-test-review.md`
- `docs/engineering-reviews/day-02-release-readiness-review.md`

and then independently inspected the current remediated working tree —
`scripts/dependency_check.py`, `tests/test_dependency_check.py`,
`gateway/server.py`, `tests/test_gateway_backend_target.py`,
`scripts/validate_manifests.py`, `tests/test_validate_manifests.py`,
`scripts/secret_bootstrap.py`, `tests/test_secret_bootstrap.py`,
`scripts/http_checks.py`, `tests/test_http_checks.py`,
`scripts/endpointslice.py`, `tests/test_endpointslice.py`, `VERSION`,
`Dockerfile`s, `.claude/agents/`, `.claude/skills/` — rather than
trusting the remediation summary's prose.

**Historical review integrity.** This adjudication independently
computed SHA-256 over the five current review files (not merely
recorded a prior claim):

| File | SHA-256 | Matches expected |
|---|---|---|
| architecture | `43791b29098712369895174ad52c233f42e2a25e883a417cb01fea9a7918ae22` | YES |
| security | `73e9fe3e838d6f5c020cdf5468ccd8b24e366e14c8e05f752b4e5b5c52905bb9` | YES |
| cluster integration | `c96ab1e05536346885eeabc1333eadf0d0e7a04753f830721febbaa7ad107294` | YES |
| test | `4835aa22afd32c260c1c73c2c057951587621bc1fc55373be6e30350270efae9` | YES |
| release readiness | `8780dc52cf4aa876c3781f111be74dda1483f2b106eaa6e51897b0531bcf7117` | YES |

All five match the expected hashes exactly, byte-for-byte. This is
stronger than recording the operator's prior claim on faith — it is a
fresh, independent hash computation performed during this adjudication
session, confirming the five review documents on disk right now are
unmodified since they were hashed. The five historical reviews continue
to show their original negative findings (DAY2-TEST-H1 as High,
DAY2-TEST-M1/M2 as Medium, DAY2-SEC-L1/DAY2-INT-L1/L2/DAY2-TEST-L1/L2/L3
as Low) unchanged — confirmed by reading them in full above, not just
by the hash match.

**What was NOT re-run.** Per instruction, `make day2-check` was not
re-executed in this session. The live-cluster counts below (33/2/27/5/11)
are carried forward from the release-engineer review's own independently
reproduced `make day2-check` run, not re-verified here. This adjudication
did independently re-run the deterministic/static tiers (unit tests,
`version_check.py`, `manifest_check.py`) and confirmed `kind get clusters`
still shows both `maops-k8s-day1` and `maops-k8s-day2` present — a
read-only check, no mutation.

---

## 2. Independent review summary

| Review | Verdict | Critical/High/Medium/Low/Info |
|---|---|---|
| Architecture | APPROVE | 0/0/0/0/2 |
| Security | APPROVE | 0/0/0/1/9 |
| Cluster Integration | APPROVE | 0/0/0/2/1 |
| Test Engineering | APPROVE WITH CONDITIONS | 0/1/2/3/2 |
| Release Readiness | APPROVE | 0/0/0/0/1 |

The test-engineering review is the only one that withheld unconditional
approval, and it is the review this adjudication weighs most heavily:
its High finding (DAY2-TEST-H1) and two Medium findings (DAY2-TEST-M1,
DAY2-TEST-M2) are the three items that must be genuinely closed — not
merely claimed closed — for a GO verdict.

---

## 3. Remediation verification

Every CLOSED claim below was checked against the actual production code
and actual test code currently on disk, not against the remediation
report's prose.

**DAY2-TEST-H1 (High).** `scripts/dependency_check.py:142-164` defines
`_app_endpoints_drained()` at module scope (not a closure), requiring
both `count_ready_endpoints(slices) == 0` **and** `get_pods(APP_LABEL_SELECTOR)`
empty. `tests/test_dependency_check.py::AppEndpointsDrainedTests` calls
this exact production function directly with `get_json`/`get_pods`
mocked:
- Case A (`test_case_a_endpointslice_empty_but_pods_still_exist_not_drained`):
  EndpointSlice empty, Pods present → `None` (not drained) — this is the
  precise regression the finding demanded be guarded.
- Case B (`test_case_b_endpointslice_ready_and_pods_exist_not_drained`):
  EndpointSlice ready, Pods present → `None`.
- Case C (`test_case_c_endpointslice_empty_and_no_pods_is_drained`): both
  empty → `True`.
- Case D (`test_case_d_endpointslice_query_failure_propagates_not_false_pass`):
  `get_json` raises → the exception propagates, not a false PASS.

This is the real production predicate, not a duplicate/reimplemented
helper. **CLOSED.**

**DAY2-TEST-M1 (Medium).** `tests/test_validate_manifests.py::SecurityContextTests`
now includes `test_gateway_run_as_user_zero_fails`,
`test_app_run_as_user_zero_fails`, `test_gateway_run_as_group_zero_fails`,
`test_app_run_as_group_zero_fails` — each mutates the real fixture and
asserts the specific `{component}.security.pod_run_as_user`/`pod_run_as_group`
check name fails. `ConfigMapExistenceTests` adds
`test_gateway_configmap_missing_fails` and `test_app_configmap_missing_fails`,
asserting `gateway.configmap.exists`/`app.configmap.exists` specifically.
All four/two are genuine mutate-and-assert-specific-failure tests
against `scripts/validate_manifests.py`'s real fixtures. **CLOSED.**

**DAY2-TEST-M2 (Medium).** `tests/test_validate_manifests.py::MirrorCoverageTests`
is table-driven over `_WORKLOADS = [("gateway", ...), ("app", ...)]` and
independently verified to cover, on both workloads: deployment image,
imagePullPolicy, startup probe path, liveness probe path, resource
requests, resource limits, deployment namespace, Service selector,
Service type/NodePort prohibition, Secret mount path, Secret mount
readOnly, Secret key (via items restriction), and ConfigMap wiring.
Deployment replicas bilateral coverage pre-dates remediation
(`ReplicaTests`, one test per workload). Readiness-probe-path bilateral
coverage exists via `ProbeTests` (`gateway.probes.readiness_path` via
probe removal, `app.probes.readiness_path` via wrong path) — both target
the same named check on both workloads, satisfying the requirement
without needing to be identically shaped. Secret name/volume-presence
bilateral coverage exists via `SecretWiringTests`
(`gateway.secret.volume_present` via wrong name,
`app.secret.volume_present` via missing volume) — both drive the same
named check on both workloads. Every item on the required list was
independently located in the actual test suite, not assumed from the
remediation report's prose (which did not enumerate them). **CLOSED.**

**DAY2-SEC-L1 (Low).** `gateway/server.py:39-56` adds
`ALLOWED_BACKEND_HOST = "maops-app"`, `ALLOWED_BACKEND_PORT = 8080`,
`_is_allowed_backend_target()`, and a module-load-time
`BACKEND_TARGET_VALID` flag. `_handle_readyz()` (line 145-149) and
`_handle_backend()` (line 162-168) both fail closed to `503` **without
calling `_backend_request()`** when the target is invalid.
`tests/test_gateway_backend_target.py` independently verifies: valid
target accepted (`test_maops_app_8080_is_allowed`), wrong host/port/raw-IP/
pod-like all blocked (four dedicated tests), `_backend_request` never
invoked when invalid for both `/backend` and `/readyz`
(`test_backend_request_not_invoked_when_target_invalid`,
`test_readyz_becomes_503_when_target_invalid_but_process_is_ready`), the
token is never sent when invalid
(`test_internal_token_never_sent_when_target_invalid` — proven by the
same "request never invoked" assertion, since the token is only ever
placed into the headers passed to that call), the unsafe configured
target is not echoed back
(`test_backend_503_does_not_echo_the_unsafe_target_when_invalid`), and
`/livez` stays 200 regardless
(`test_livez_remains_200_regardless_of_backend_target_validity`). All
required proof points are present and deterministic. This is correctly
scoped as a defense-in-depth guard, not a replacement for Day 5
RBAC/NetworkPolicy — the code comment and docs make that boundary
explicit. **CLOSED.**

**DAY2-INT-L1 (Low).** `scripts/secret_bootstrap.py::get_existing_secret()`
(lines 49-73) now issues an explicit `kubectl get namespace` check first
and raises `RuntimeError` on failure **before** any Secret-specific
logic runs. `tests/test_secret_bootstrap.py::NamespaceClassificationTests`
verifies: a missing namespace raises before the Secret `get` call is
even attempted (`test_missing_namespace_raises_before_checking_secret`,
asserting `secret_get_calls == []`); `main()` fails non-zero, never
calls `create_secret`, and never prints the misleading "does not exist -
generating a new token" message
(`test_missing_namespace_main_fails_closed_without_generating_token`);
and the normal creation path (namespace exists, Secret absent) still
resolves correctly to `None`
(`test_namespace_exists_and_secret_missing_still_resolves_to_none`). The
existing-Secret-preserved path is unaffected (`ExistingSecretPreservedTests`,
unchanged). **CLOSED.**

**DAY2-INT-L2 (Low).** `scripts/http_checks.py:135-152` adds
`check_safe_unavailable_body()`, asserting the decoded body equals
exactly `{"error": "backend unavailable"}`, then independently scanning
for a `"token"` substring and traceback-like markers.
`scripts/dependency_check.py:195-200` now calls this function (not just
`status == 503`) on the `/backend`-during-outage response.
`tests/test_http_checks.py::CheckSafeUnavailableBodyTests` verifies:
correct safe body passes, an arbitrary-but-valid different JSON body
fails, a body containing `"debug_token"` fails, a body containing
traceback-like content fails, and a non-JSON body fails. All five
required rejection/acceptance cases are present. **CLOSED.**

**DAY2-TEST-L1 (Low).** `tests/test_secret_bootstrap.py::TempFileModeTests::test_temp_file_is_mode_0600_before_kubectl_uses_it`
spies on the real `os.chmod` and, inside that spy, calls the real
`os.stat(path).st_mode` **while the file still exists**, asserting
`stat.S_IMODE(...) == 0o600`. This is a genuine filesystem-mode
inspection, not a "was chmod called" mock assertion. **CLOSED.**

**DAY2-TEST-L2 (Low).** `scripts/endpointslice.py::count_ready_endpoints()`
was changed from a pure sum to a `set`-based dedup
(`ready_addresses: set[str]`), and now defensively skips non-dict
slice/endpoint entries. `tests/test_endpointslice.py::DefensivenessAndDedupTests`
covers: duplicate address across overlapping slices counted once,
duplicate address within one slice counted once, malformed non-dict
slice entries skipped not raised, malformed non-dict endpoint entries
skipped not raised, and `endpoints: None` treated as zero. Combined with
pre-existing tests (multiple slices, missing addresses key, missing/false
readiness), every item on the required list is present. This does not
attempt (and the task does not require) generic IPv4/IPv6 Pod-identity
reconciliation — DAY2-INT-I1 correctly remains open (§8). **CLOSED.**

**DAY2-TEST-L3 (Low).** `tests/test_validate_manifests.py::ForbiddenRbacKindTests::test_forbidden_rbac_kinds_each_fail`
is a `subTest`-driven mutation test appending a realistic `Role`,
`RoleBinding`, `ClusterRole`, and `ClusterRoleBinding` fixture in turn,
each independently asserting `scope.no_forbidden_resources` fails. No
RBAC implementation exists anywhere in `k8s/base` (confirmed:
`grep -n "^kind:"` on the rendered base still shows only Namespace,
ConfigMap×2, Deployment×2, Service×2). **CLOSED.**

**All nine tracked findings (one High, two Medium, six Low) are
genuinely closed against the real production code and real test
assertions — none is a cosmetic or duplicate-helper closure.**

---

## 4. Finding-by-finding adjudication

See §10 for the complete matrix. Summary of adjudicated dispositions
for the findings requiring closure verification:

| ID | Verdict |
|---|---|
| DAY2-TEST-H1 | CLOSED |
| DAY2-TEST-M1 | CLOSED |
| DAY2-TEST-M2 | CLOSED |
| DAY2-SEC-L1 | CLOSED |
| DAY2-INT-L1 | CLOSED |
| DAY2-INT-L2 | CLOSED |
| DAY2-TEST-L1 | CLOSED |
| DAY2-TEST-L2 | CLOSED |
| DAY2-TEST-L3 | CLOSED |

---

## 5. Day 1 carry-forward debt

**DAY1-REL-I1: CLOSED.** `scripts/version_check.py` reads `VERSION`
once, cross-checks it against the real `kubectl kustomize`-rendered
manifest (not a hand-built mirror), and is independently confirmed by
three of the five reviews (architecture, test, release-readiness) via
both source reading and live/mutation execution. This adjudication
independently re-ran it: **13/13 version checks passed.**

**DAY1-INT-I2: remains ACCEPTED / OPEN.** Confirmed directly:
`app/Dockerfile:15` and `gateway/Dockerfile:15` both still pin
`gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5`
(identical digest to Day 1), both files carry an explicit comment
cross-referencing DAY1-INT-I2 as still open, and `/usr/bin/python3.11`
remains hardcoded in `scripts/cluster_check.py`, `discovery_check.py`,
and `secret_check.py`. Not marked closed.

---

## 6. Validation progression

| Check | Pre-remediation | Post-remediation (reported) | Independently re-verified this session |
|---|---|---|---|
| Unit tests | 163/163 | 212/212 | **212/212 — confirmed by direct execution** |
| Version checks | 13/13 | 13/13 | **13/13 — confirmed by direct execution** |
| Manifest checks | 94/94 | 94/94 | **94/94 — confirmed by direct execution** |
| Live cluster (`rollout-check`) | 33/33 | 33/33 | not re-run this session (read-only `kind get clusters` confirms both clusters still present) |
| Discovery | 2/2 | 2/2 | not re-run this session |
| Secret validation | 27/27 | 27/27 | not re-run this session |
| Smoke | 5/5 | 5/5 | not re-run this session |
| Dependency behavior | 11/11 | 11/11 | not re-run this session |

The unit-test count grew from 163 to 212 (+49), consistent with the
remediation's stated scope: the four `_app_endpoints_drained()` cases,
the four `pod_run_as_user`/`pod_run_as_group` cases plus two
`configmap.exists` cases, the ~12 `MirrorCoverageTests` cases, the six
`BackendTargetAllowlistTests`/`BackendHandlerFailClosedTests` cases, the
three `NamespaceClassificationTests` cases, the five
`CheckSafeUnavailableBodyTests` cases, one `TempFileModeTests` case, the
four `ForbiddenRbacKindTests` sub-cases (counted as one test method with
subTests, not four separate `test_` methods), and the five
`DefensivenessAndDedupTests` cases. This growth is regression coverage
for already-real behavior, not new named static/live gates — which is
why the manifest/version/live counts are unchanged, exactly as the task
brief anticipated.

---

## 7. Evidence quality

| Claim | Tier | Basis |
|---|---|---|
| `_app_endpoints_drained()` correctness | [B] | direct unit test of the real module-level production function |
| `pod_run_as_user`/`pod_run_as_group`/`configmap.exists` regression guards | [B] | mutate-fixture-assert-named-check tests |
| Mirror/bilateral coverage across gateway+app | [B] | table-driven `subTest` tests against real fixtures |
| Gateway backend-target allowlist fail-closed | [B] | direct unit tests against real `gateway/server.py` functions/handlers |
| Namespace-vs-Secret-missing classification | [B] | mocked `run()` dispatch tests against the real `get_existing_secret()` |
| `/backend` outage body safety | [B] | `check_safe_unavailable_body()` unit tests + real call site in `dependency_check.py` |
| Temp file `0600` mode | [B] | real `os.stat()` inspection while the file exists |
| EndpointSlice dedup/defensiveness | [B] | direct unit tests of the real `count_ready_endpoints()` |
| RBAC kinds still forbidden | [B] | mutation test against the real `scope.no_forbidden_resources` check |
| VERSION/manifest static guards | [A]/[B] | source read + independently re-executed this session |
| 212 unit tests, 13 version, 94 manifest | [B] | independently re-executed this session, exact match |
| 2/2 Deployments, EndpointSlices healthy, live Secret | [C] | carried forward from cluster-integration/security/release reviews' live `kubectl -o json` evidence (not re-run this session) |
| Real DNS resolution, gateway→app HTTP, dependency-failure/restoration | [D] | carried forward from independently reproduced live executions in three of the five reviews |
| Historical review file integrity | [B] | SHA-256 independently computed this session, exact match to expected digests |

No claim of a green result is manufactured; every [B]-tier claim in
this document was independently exercised in this session, and every
[C]/[D]-tier claim carried forward is attributed to the specific review
that produced it rather than asserted as newly re-verified.

---

## 8. Remaining accepted technical debt

Zero technical debt is not claimed. The following remain genuinely
open, by design, and are not release-blocking for v0.2.0:

- **DAY2-INT-I1 — ACCEPTED / OPEN FOR FUTURE DUAL-STACK SUPPORT.**
  `count_ready_endpoints()`'s remediated dedup (§3, DAY2-TEST-L2) counts
  identical *string* addresses once, which incidentally helps the
  current single-stack IPv4 model, but does **not** reconcile a single
  Pod's IPv4 and IPv6 addresses as "the same backend" under a future
  dual-stack Service — a Pod with both an IPv4 and an IPv6 ready address
  under the same `kubernetes.io/service-name` label would still count as
  two distinct entries. This is unchanged as an open item.
- **DAY1-INT-I2 — ACCEPTED / OPEN FOR FUTURE BASE-IMAGE CHANGE.** The
  digest-pinned distroless base and the hardcoded `/usr/bin/python3.11`
  interpreter path remain live-proven correct and unchanged from Day 1
  (§5).
- **DAY2-ARCH-I1 — INFORMATIONAL / SATISFIED BY INDEPENDENT LIVE
  EVIDENCE.** The architecture reviewer did not itself re-run
  `dependency_check.py` (a deliberate no-live-mutation constraint for
  that review), but cluster-integration and release-readiness both
  independently executed it live, and remediation's own final
  `make day2-check` run exercised it again. Not a code defect.
- **DAY2-ARCH-I2 — INFORMATIONAL / ACCEPTED AS DESIGNED.** Independent
  3-second gateway/app startup-delay timers can cause a transient
  gateway `/readyz` 503 during a simultaneous cold start; liveness stays
  independent throughout. Expected, bounded behavior; no remediation
  required.
- **DAY2-SEC-I1 through DAY2-SEC-I9 — INFORMATIONAL / NO REMEDIATION
  REQUIRED.** Positive/pass observations from the independent security
  review (token generation strength, no committed credential, explicit
  context targeting, preserve-not-rotate, fail-closed malformed-secret
  handling, non-disclosure discipline, no command-line leakage, temp
  file lifecycle, read-only/restrictive Secret mount with matched
  fsGroup/UID). Not converted to closed vulnerabilities; recorded as-is.
- **DAY2-TEST-I1 — INFORMATIONAL / INHERENT TEST LIMITATION.**
  `hmac.compare_digest` timing-side-channel resistance is not
  observable through functional unit assertions; source inspection
  confirms the correct primitive is used.
- **DAY2-TEST-I2 — INFORMATIONAL / VALID TEST-TIER SEPARATION.**
  `scripts/secret_check.py` is intentionally a live-cluster validation
  tier with no offline unit test file, consistent with
  `.claude/CLAUDE.md`'s two-tier validation rule.
- **DAY2-REL-I1 — INFORMATIONAL / OPERATOR-VERIFIED / PRE-TAG RECHECK
  REQUIRED.** The release-readiness reviewer's environment lacked SSH
  access to independently confirm remote `v0.2.0` tag absence
  (`git ls-remote --tags origin` failed with a publickey error in that
  environment, not a repository defect). This adjudication did not
  attempt a remote check either (out of scope — no push/tag action is
  authorized here). **Remote tag absence must be re-verified immediately
  before any future tagging step**, regardless of this adjudication's
  verdict.

---

## 9. Release-readiness boundaries

- This adjudication does **not** authorize a commit, tag, push, PR, or
  release. Only `docs/engineering-reviews/day-02-v0.2-release-readiness.md`
  was created; no other file in the working tree was modified.
- A GO verdict here means the working tree is suitable to proceed toward
  a v0.2.0 feature commit/PR — it is not itself that commit/PR.
- After a future PR merge, the exact merged main tree must undergo one
  authoritative local `make day2-check` run before `v0.2.0` is tagged.
  This adjudication does not substitute for that run, and `make
  day2-check` was deliberately not re-executed in this session per
  instruction.
- Remote absence of the `v0.2.0` tag (DAY2-REL-I1) must be re-checked
  with authenticated remote access immediately before tagging.
- GitHub Actions/CI remains explicitly Day 6 scope and is not demanded
  here.
- Day 2's authentication-only security model (no NetworkPolicy, no
  RBAC) is accurately documented as a known, deferred-to-Day-5 gap by
  both the security review and `docs/roadmap.md` — this adjudication
  does not overclaim network-level isolation exists today.

---

## 10. Final finding matrix

| ID | Original severity | Original meaning | Current disposition | Closure/evidence | Release-blocking now |
|---|---|---|---|---|---|
| DAY2-ARCH-I1 | Informational | Live dependency-check not re-run during architecture review (session constraint) | Informational / satisfied by independent live evidence | Cluster-integration + release-readiness independently ran it live; final `make day2-check` re-ran it | NO |
| DAY2-ARCH-I2 | Informational | Independent 3s startup timers can cause transient gateway readiness 503 on simultaneous cold start | Informational / accepted as designed | Bounded, self-resolving; liveness independent | NO |
| DAY2-SEC-L1 | Low | Gateway backend target ConfigMap-driven at runtime, only build-time guard existed | **CLOSED** | `gateway/server.py` in-process allowlist + fail-closed handlers; `tests/test_gateway_backend_target.py` (§3) | NO |
| DAY2-SEC-I1–I9 | Informational | Positive pass observations (token strength, non-disclosure, mount hygiene, etc.) | Informational / no remediation required | Preserved as-is; not converted to defects | NO |
| DAY2-INT-L1 | Low | Missing-namespace and missing-Secret both surfaced as "NotFound", misleading intermediate message | **CLOSED** | Explicit namespace pre-check in `get_existing_secret()`; `NamespaceClassificationTests` (§3) | NO |
| DAY2-INT-L2 | Low | `/backend`-outage check asserted only HTTP 503, not body safety | **CLOSED** | `check_safe_unavailable_body()` + `CheckSafeUnavailableBodyTests` (§3) | NO |
| DAY2-INT-I1 | Informational | `count_ready_endpoints()` sums across address families without dedup | Accepted / open for future dual-stack support | Identical-address dedup added (DAY2-TEST-L2) but does not solve cross-family Pod identity — remains open by design | NO |
| DAY2-TEST-H1 | **High** | `_app_endpoints_drained()` termination-race predicate had no unit test; every test mocked `run_experiment` out | **CLOSED** | `AppEndpointsDrainedTests`, 4 cases against the real production function (§3) | NO |
| DAY2-TEST-M1 | **Medium** | `pod_run_as_user`/`pod_run_as_group` and `configmap.exists` had zero dedicated negative-test coverage | **CLOSED** | 4 new SecurityContextTests + 2 new ConfigMapExistenceTests (§3) | NO |
| DAY2-TEST-M2 | **Medium** | One-sided mirror gap: many shared checks proven on only one workload | **CLOSED** | `MirrorCoverageTests` table-driven over both workloads; full required list independently located in the actual suite (§3) | NO |
| DAY2-TEST-L1 | Low | `os.chmod(tmp_path, 0o600)` had no dedicated permission-bit assertion | **CLOSED** | `TempFileModeTests` — real `os.stat()` inspection (§3) | NO |
| DAY2-TEST-L2 | Low | No test for duplicate addresses across/within slices or malformed non-dict entries | **CLOSED** | `DefensivenessAndDedupTests`, 5 cases (§3) | NO |
| DAY2-TEST-L3 | Low | Role/RoleBinding/ClusterRole/ClusterRoleBinding relied on shared mechanism, no dedicated test | **CLOSED** | `ForbiddenRbacKindTests`, subTest over all four kinds (§3) | NO |
| DAY2-TEST-I1 | Informational | `hmac.compare_digest` timing-safety not unit-testable | Informational / inherent test limitation | Source-confirmed correct primitive; unchanged | NO |
| DAY2-TEST-I2 | Informational | `secret_check.py` has no offline unit test (by design) | Informational / valid test-tier separation | Consistent with project's two-tier rule | NO |
| DAY2-REL-I1 | Informational | Remote `v0.2.0` tag absence unverifiable in reviewer's environment (SSH access) | Informational / operator-verified / pre-tag recheck required | Not a repo defect; must be re-checked immediately before any future tagging | NO (but gates tagging separately) |
| DAY1-REL-I1 | (Day 1 carry-forward) | No automated VERSION/manifest drift guard | **CLOSED** | `version_check.py`, 13/13, independently re-run (§5) | NO |
| DAY1-INT-I2 | (Day 1 carry-forward) | Distroless base + hardcoded interpreter path coupling | Accepted / open for future base-image change | Digest and interpreter path confirmed unchanged (§5) | NO |

---

## 11. Final verdict

Independent re-inspection, not the remediation report's prose, is the
basis for every closure claim above. Findings:

- DAY2-TEST-H1: **truly closed**, against the real production predicate.
- DAY2-TEST-M1: **truly closed**, against real fixture mutations.
- DAY2-TEST-M2: **truly closed** — the full required bilateral-coverage
  list was independently located in the actual test suite, not assumed
  from the remediation report.
- DAY2-SEC-L1: **truly closed**, with the outbound-request-never-invoked
  proof specifically confirmed.
- DAY2-INT-L1: **truly closed**, with the misleading-message suppression
  specifically confirmed.
- DAY2-INT-L2: **truly closed**, with all four required rejection cases
  plus the acceptance case confirmed.
- DAY2-TEST-L1: **truly closed**, via genuine filesystem-mode inspection.
- DAY2-TEST-L2: **truly closed**, all required defensive/dedup cases
  present.
- DAY2-TEST-L3: **truly closed**, all four forbidden RBAC kinds proven.
- Critical unresolved: **0**
- High unresolved: **0**
- Medium unresolved: **0**
- Current deterministic/static validation remains green: **212/212 unit,
  13/13 version, 94/94 manifest — independently re-executed this
  session.**
- Current runtime remains healthy: both `maops-k8s-day1` and
  `maops-k8s-day2` kind clusters present (read-only check); live-cluster
  counts (33/2/27/5/11) carried forward from the release-readiness
  review's own independently reproduced run, not re-run here per
  instruction.
- Historical reviews remain immutable: **confirmed via independent
  SHA-256 computation this session**, exact match to expected digests.
- `VERSION == 0.2.0`: **confirmed**.
- `v0.2.0` has not been tagged/released: **confirmed** (`git tag -l`
  shows only `v0.1.0`); remote absence remains operator-asserted and
  must be re-checked immediately before any future tagging (DAY2-REL-I1).

Remaining open items are exactly the ones that should legitimately
remain open at this stage: two Day-2-informational architecture notes,
nine positive/pass security informational items, one dual-stack
technical-debt item (DAY2-INT-I1), one Day-1 carry-forward technical-debt
item (DAY1-INT-I2), two inherent test-limitation informational items,
and one pre-tag remote-verification requirement (DAY2-REL-I1). None of
these are release-blocking for v0.2.0, and none is claimed as "closed"
merely to zero out a count.

**Final verdict: GO FOR PR**

This verdict authorizes proceeding toward a v0.2.0 feature commit and
PR. It does **not** authorize immediate tagging of `v0.2.0`. After that
PR merges, the exact merged main tree must undergo one authoritative
local `make day2-check` run, and remote `v0.2.0` tag absence must be
re-verified with authenticated access, before tagging.

PROJECT 4 DAY 2 v0.2.0 FINAL ADJUDICATION COMPLETE — GO FOR PR

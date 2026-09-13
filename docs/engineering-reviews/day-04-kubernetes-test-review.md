# Day 4 (v0.4.0) — kubernetes-test-engineer Review

**Reviewer identity:** `kubernetes-test-engineer` agent (fourth independent Day 4 review pass).
**Scope:** Testing-quality review of the repository-owned validation logic added/changed for
Day 4 (stateful persistence). Not a general code review; not a release-readiness
adjudication (that is `release-engineer`'s separate, later step).
**Repository:** `~/DevOps-Portfolio/maops-kubernetes-platform`, branch
`feature/day-4-stateful-persistence`, HEAD `aa2049876c7be2b959acb6e2a1d20f979ee440bc`
(uncommitted working-tree changes on top of that commit constitute the Day 4 candidate).
**Evidence directory (this review):**
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-test-review-20260910T054500Z-tE7qk/`

I read `.claude/CLAUDE.md`, my own agent definition, the four skill files, the Makefile,
`docs/architecture.md`, `docs/roadmap.md`, and the full content (not just diffs) of every
new/changed production script, the three new Kustomize state manifests, `app/server.py`,
`gateway/server.py`, `state/server.py`, and every test file touching Day 4 surface. I did
**not** open the three existing Day 4 review reports' content before forming the conclusions
below (only hashed them, per instructions); their hashes are verified unchanged in the
integrity section.

---

## 1. What I ran (existing tests) vs. what I built (reviewer probes)

**Existing regression tests actually executed, unmodified, exactly as checked into the repo:**

```
$ python3 -m unittest discover -s tests -v
...
Ran 392 tests in 0.635s
OK
(exit code 0)
```
Re-run a second time for determinism confirmation: same result, exit code 0 (both full logs
saved: `existing-tests-unittest-discover-v.log`, `existing-tests-summary.txt` in the evidence
directory). All 392 Docker/cluster-free tests pass; no regression from this candidate.

```
$ make manifest-check
...
197/197 checks passed
PASS: all static manifest checks passed
(exit code 0)
```
Full log: `make-manifest-check.log`. This is a real `kubectl kustomize` render against the
actual `k8s/base/` (no live cluster needed), so it is a genuine integration check of the
manifests, not a re-read of source YAML.

**Reviewer-created temporary probes** (all saved under `reviewer-probes/` in the evidence
directory, read-only, no mutation, no live-cluster writes):

- `find_untested_state_checks.py` — a pure source-text probe (no execution of production
  code, no subprocess) that cross-references every `"state.*"` check-name string literal in
  `scripts/validate_manifests.py` against `tests/test_validate_manifests.py` to determine
  which check names are never referenced by a dedicated test. Output:
  `find_untested_state_checks.out.txt`.
- `retention_check_baseline_provenance_trace.txt` — a grep-based trace (no execution)
  contrasting `persistence_check.py`'s and `retention_check.py`'s handling of the
  pre-experiment state *value*, used for the provenance investigation in §4.
- `live-readonly-maops-state-0-pod.json` — one bounded, read-only
  `kubectl --context kind-maops-k8s-day4 -n maops-platform get pod maops-state-0 -o json`
  and a read-only `get pvc data-maops-state-0 -o json` (no exec, no mutation), used only to
  confirm the live Pod/PVC UID plumbing and the `imageID` vs. image-tag distinction discussed
  in §6.
- `day4-check-log-excerpt-*.txt` — excerpts of the retained, accepted
  `day4-final-candidate-20260908T112009Z-ckQ1uj/day4-check.log` used as historical evidence
  for §4 (not something I generated live; copied read-only from the existing evidence
  directory for citation).

No temporary probe touched a mutation endpoint, exec'd into a Pod, deleted/scaled/rotated
anything, or opened a port-forward.

---

## 2. Contract-to-test mapping and gaps

### 2.1 `scripts/storage_bootstrap.py` and `scripts/storage_hardening_check.py` — no unit tests at all

Both are new Day 4 scripts (`scripts/storage_bootstrap.py:1-494`,
`scripts/storage_hardening_check.py:1-297`) containing real branching logic that does not
require a live cluster to exercise in isolation: `harden_provisioning_root_on_all_nodes()`
(`storage_bootstrap.py:158-211`) and its counterpart
`restore_provisioning_root_on_nodes()` (`storage_bootstrap.py:214-258`, notably the
documented "`chmod g-s` before the numeric mode" restoration-ordering fix), the
"refuse-to-touch-unfamiliar-state" fail-closed checks (uid != "0", symlink detection), the
idempotent-no-op detection in `main()` (`storage_bootstrap.py:444-461`), and the
propagation-verification/rollback-on-failure path (`storage_bootstrap.py:471-489`).

I confirmed via `grep -rl "storage_bootstrap\|storage_hardening_check" tests/*.py` that
**no test file references either module** — neither exists in
`tests/` nor is imported by any existing test. Compare this to the established project
pattern for the same tier of script (`cluster_check.py` → `tests/test_cluster_check_failure_handling.py`;
`reconcile_check.py` → `tests/test_reconcile_check_failure_handling.py`), both of which mock
only `run`/`get_json`/`subprocess` and exercise the *real* production functions.

- **Untested contract:** partial node-hardening failure must revert only the nodes actually
  changed, to their exact captured `(mode, gid)`, using the documented `chmod g-s` before
  numeric-mode ordering.
- **Plausible failure mode:** a future edit to `restore_provisioning_root_on_nodes()` (e.g.
  someone "simplifies" it to drop the `g-s` pre-clear, since it looks redundant) silently
  reintroduces the exact setgid-not-cleared bug the code comments say was found live —
  nothing in CI would catch it before the next live run.
- **Consequence:** a failed/aborted bootstrap could leave one or more kind nodes'
  `/var/local-path-provisioner` root in a mixed state (wrong mode/group) with no test ever
  having proven the revert path works.
- **Smallest regression test:** mock `subprocess.run`/`_docker_exec` with a fake
  `CompletedProcess` sequence that simulates "chown succeeds, chmod fails" mid-hardening for
  one of two nodes, call `harden_provisioning_root_on_all_nodes()` for real, then call
  `restore_provisioning_root_on_nodes()` for real with its returned `original_state_by_node`,
  and assert the exact sequence of `docker exec` argv tuples issued during restoration
  (`g-s` before the numeric mode) — mirroring `test_reconcile_check_failure_handling.py`'s
  technique of asserting on captured argv rather than only on a boolean return value.

**Finding: `DAY4-TEST-H1`** (High).

### 2.2 `scripts/state_check.py`, `scripts/persistence_check.py`, `scripts/retention_check.py` — no unit tests at all

Same situation: `grep -rl "state_check\|persistence_check\|retention_check" tests/*.py`
returns nothing. These three scripts contain the actual Day 4 persistence *proof* logic the
task exists to validate:

- `persistence_check.py:114-267` (`main()`) captures `original_value` (line 130-136),
  writes/reads back a marker (139-145), deletes the Pod with an explicit
  `--wait=false`/90s-timeout workaround for a documented live race
  (`POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS`, lines 44-58), waits for a Pod UID change plus
  Ready (176-193), and restores `original_value` in a `finally` block with bounded retries
  (222-258).
- `retention_check.py:127-167` (`restore_state()`) scales back to 1 replica, waits for a new
  Pod UID + Ready, waits for `maops-app` to re-converge to 3/3, and finally checks the
  gateway `/readyz` through the Service — all inside a guaranteed `finally` at line 226-228.

None of this — the retry bounds, the "wait for ready before attempting the restoring PUT"
ordering, the distinct-and-never-swallowed reporting of a restoration failure via
`record_restoration()` vs. `record()`, or the specific timeout-race fixes called out inline
as "DAY4-INT bug found live" — is exercised by any Docker-free test. Per the review
priorities, I looked specifically for the anti-pattern of "a mocked restoration helper merely
asserted as called": that anti-pattern is not present here, but only because there is **no
test of any kind**, mocked or otherwise, so the restoration logic's correctness rests
entirely on the live run(s) captured in the evidence directories, never on a repeatable,
Docker-free regression test.

- **Untested contract:** "a restoration failure is a distinct, prominent finding, never
  hidden" (both scripts' own docstrings).
- **Plausible failure mode:** if a future change caused the final restoring `PUT` to always
  return e.g. HTTP 500 instead of 200, both scripts already have the *code* to report that
  distinctly (`record(False, "RESTORATION FAILURE: ...")` in `persistence_check.py:256`,
  `record_restoration(False, ...)` throughout `retention_check.py`) — but there is no test
  proving that code path is actually reachable and actually sets the right exit code, only
  live observation.
- **Consequence:** a regression in the restoration path (e.g. an accidentally-swallowed
  exception, or a retry loop with an off-by-one that stops one iteration too early) would
  only be caught by a live `make day4-check` run, contrary to the two-tier validation intent
  in `.claude/CLAUDE.md` ("Real-cluster validation... should never fall back to just
  re-reading the manifest" — the flip side of that principle is that everything that *can*
  be tested without a cluster, should be).
- **Smallest regression test:** for `persistence_check.py`, extract/mock `_get_state`/
  `_put_state`/`run`/`wait_until`/`portforward.port_forward` so `main()` can run against a
  simulated sequence (original value → marker write → simulated Pod-UID change → simulated
  final PUT failure), and assert the restoration failure is both recorded and reflected in
  the final exit code — the same technique `test_reconcile_check_failure_handling.py`
  already uses for `reconcile_check.main()`.

**Finding: `DAY4-TEST-H2`** (High).

### 2.3 `app/server.py` and `gateway/server.py` — new Day 4 state-proxy handlers untested

`app/server.py`'s `_state_request()` (98-123), `_handle_internal_state_get/put()`
(221-264), and the `STATE_TARGET_VALID` fail-closed allowlist (`_is_allowed_state_target`,
49-53); and `gateway/server.py`'s `_handle_state_get/put()` (220-261) and the
`STATE_PROXY_MAX_BODY_BYTES` oversized-body rejection (122, 247-249) are all new for Day 4.

`tests/test_app_auth.py` and `tests/test_gateway_backend_target.py` — the two files that
would naturally host this — only test the pre-existing internal-token/backend-allowlist
logic (`_token_is_valid`, `_is_allowed_backend_target`, `_handle_backend`,
`_handle_readyz`'s backend-target-invalid branch). Neither file, nor any other test file,
references `_state_request`, `_handle_internal_state_get`, `_handle_internal_state_put`,
`_is_allowed_state_target`, `_handle_state_get`, or `_handle_state_put`.

- **Untested contract:** app must never forward the state token to an invalid/tampered
  `STATE_HOST`/`STATE_PORT` (mirrors the already-tested gateway backend-allowlist pattern,
  but the state-side equivalent has no test); gateway/app's `/state` PUT handlers must reject
  a request whose `Content-Length` exceeds `STATE_MAX_BODY_BYTES`/`STATE_PROXY_MAX_BODY_BYTES`
  *before* reading the body.
- **Plausible failure mode:** a typo or refactor that leaves `STATE_TARGET_VALID` unchecked
  in one of the two proxy handlers (get vs. put — they are separate, hand-written
  `if not STATE_TARGET_VALID or STATE_TOKEN is None` guards, not a shared decorator) would
  ship silently; nothing but a live cluster run would notice.
- **Consequence:** loss of the fail-closed guarantee this project explicitly documents as its
  own "last line of defense" (comment at `app/server.py:36-41`), unverified by any fast test.
- **Smallest regression test:** duplicate the existing `_FakeHandler`/`mock.patch.object`
  technique from `tests/test_gateway_backend_target.py`'s `BackendHandlerFailClosedTests` for
  `_handle_state_get`/`_handle_state_put`/`_handle_internal_state_get`/
  `_handle_internal_state_put`, asserting `_state_request`/`_backend_request` is never called
  when the respective target-valid flag is `False`, plus a Content-Length-oversized case
  asserting a 413 without ever calling the downstream request function.

**Finding: `DAY4-TEST-M1`** (Medium).

### 2.4 `state/server.py` — the actual persistence workload — has zero dedicated unit tests

This is the single most safety-critical new file in Day 4 and is entirely stdlib, pure-logic,
and trivially testable without a cluster (same class of test as `test_app_auth.py` already
does for `app/server.py`). Confirmed via
`grep -rl "state.server\|_persist_record\|_validate_record\|STATE_FILE_PATH\|PersistOutcome" tests/*.py`
→ only `tests/test_validate_manifests.py` matches (manifest-only, not server logic).

Untested, easily-testable contracts include:
- `_validate_record()` (92-101): exact `{"value": <str|null>}` schema — extra keys, wrong
  types, non-dict top level.
- `_persist_record()` (115-164): the `PersistOutcome.OK` /
  `FAILED_CLEAN` / `FAILED_UNCERTAIN` three-way distinction the task explicitly calls out
  ("Reported clean failure vs. uncertain outcome vs. success") — specifically the case where
  `os.replace()` succeeds but the parent-directory `fsync()` fails (161-162), which is
  reachable by mocking `os.open`/`os.fsync` to fail only on the second call.
- `_read_record()` (167-183) and `_initialize_state_file()` (186-204): a pre-existing
  malformed/unreadable file must be surfaced via `/readyz`, never silently replaced.
- `_token_is_valid()`/`load_state_token()`: identical shape to the already-tested
  `app_server._token_is_valid`, but with zero tests of its own.
- Oversized-body (`STATE_MAX_BODY_BYTES`) and malformed-JSON/schema-violation PUT handling
  (295-324).

- **Plausible failure mode:** a refactor of `_persist_record()`'s error handling (e.g.
  collapsing the two `OSError` branches) could turn a `FAILED_UNCERTAIN` outcome into a
  silently-reported `200 OK`, which is exactly the durability-honesty contract Day 4 exists to
  prove.
- **Smallest regression test:** point `STATE_FILE_PATH`/`_STATE_DIR` at a `tempfile.TemporaryDirectory()`,
  monkeypatch `os.fsync` to raise only on its second call within one `_persist_record()`
  invocation, and assert the return is exactly `(PersistOutcome.FAILED_UNCERTAIN, ...)` and
  that the target file was, in fact, replaced (the "uncertain" case is real ambiguity, not a
  clean rollback) — no cluster, Docker, or even a real StatefulSet needed.

**Finding: `DAY4-TEST-M2`** (Medium).

### 2.5 `scripts/secret_bootstrap.py` — Day 4 "state"/"all" dispatch entirely untested

`tests/test_secret_bootstrap.py` (300 lines, otherwise rigorous — see its
`TempFileCleanupTests`/`TempFileModeTests`/`NonDisclosureTests`) exercises only the unchanged
`main("internal")` default path. I confirmed with
`grep -n "state\|_bootstrap_state\|STATE_SECRET\|main(\"all\"" tests/test_secret_bootstrap.py`
→ zero matches. `_bootstrap_state()` (209-257), `get_existing_state_secret()` (88-90),
`create_state_secret()` (143-145), and both `main("state")` and `main("all")`
(260-283) — including the fact that `main("all")` runs `_bootstrap_internal()` and
`_bootstrap_state()` **unconditionally in sequence, regardless of the first call's result**
(278-280) — have no coverage in either direction of partial failure.

- **Untested contract:** "both directions of partial failure in the 'all' target" (explicit
  task ask #5) — i.e. internal succeeds/state fails, and state succeeds/internal fails, must
  both surface as an overall non-zero exit and (per non-disclosure discipline) never print
  either generated token.
- **Plausible failure mode:** a future edit that adds an early `return` after
  `_bootstrap_internal()` fails (a natural-looking "fail fast" refactor) would silently stop
  bootstrapping the state Secret at all — the opposite of the current, deliberate
  run-both-anyway behavior — with no test catching the behavior change either way.
- **Smallest regression test:** four cases mirroring the existing `ExistingSecretPreservedTests`/
  `NewSecretCreationTests` style but parametrized over `(internal_ok, state_ok)`, mocking
  `get_existing_secret`/`get_existing_state_secret`/`create_secret`/`create_state_secret`,
  asserting `main("all")`'s exit code and that **both** bootstrap functions were actually
  invoked regardless of the first one's outcome.

**Finding: `DAY4-TEST-M3`** (Medium).

### 2.6 `scripts/validate_manifests.py` — 40 of 52 new `state.*` checks have no negative test

Using a reviewer probe (`reviewer-probes/find_untested_state_checks.py`, output in
`find_untested_state_checks.out.txt`) that cross-references every `state.*` check-name
string literal against `tests/test_validate_manifests.py`:

```
Total distinct state.* check names found in scripts/validate_manifests.py: 52
Referenced somewhere in tests/test_validate_manifests.py: 12
NOT referenced anywhere (no dedicated negative-mutation test): 40
```

The 12 with dedicated negative tests (`tests/test_validate_manifests.py:614-724`,
`StateStatefulSetTests`/`StateServiceTests`) cover replicas, topology-spread, PVC template
presence/name, storage class, retention policy (both), claim capacity/access-mode, data mount
presence, service name, headless-service `clusterIP`, and Secret-volume name mismatch. The 40
without a dedicated negative test include **every state security-context check**:
`state.security.pod_run_as_non_root`, `pod_run_as_user`, `pod_run_as_group`, `pod_fs_group`,
`seccomp_profile`, `allow_privilege_escalation`, `read_only_root_filesystem`,
`capabilities_drop_all`, `automount_service_account_token`, `no_host_network`, `no_host_pid`,
`no_host_ipc`, `no_host_path_volumes`, `not_privileged`, plus namespace/version/component
labels, image/imagePullPolicy/container-name/single-container, probe paths, resource
requests/limits, ConfigMap wiring, Secret mount path/read-only, and Service/headless-Service
selector matching (full list in the probe output). Each of these is only ever asserted
*positively*, via the aggregate `BaselineTests.test_baseline_passes_every_check`
(`tests/test_validate_manifests.py:334-339`) — there is no test that mutates the
corresponding manifest field to a bad value and asserts the specific check name is the one
that fails.

This is a direct, measurable shortfall against this project's own explicit, stated standard
(quoted verbatim in `.claude/CLAUDE.md`-adjacent project convention and visible throughout
the rest of `tests/test_validate_manifests.py`'s gateway/app equivalents, e.g.
`app.security.*` checks each have their own mutation test): "When a new static check is added
... it needs both a positive assertion ... and at least one negative case proving the check
actually fires." For a project whose stated focus is proving real Kubernetes security/runtime
behavior, an untested security-context check is a check that could regress to a no-op (e.g. a
copy-paste bug that checks the wrong dict key) and nothing would ever fail red.

**Finding: `DAY4-TEST-M4`** (Medium).

### 2.7 `scripts/secret_check.py` — extended without a dedicated test file (pre-existing debt, extended)

`scripts/secret_check.py` grew by ~130 lines for Day 4 (`get_state_token`,
`check_state_secret_shape`, `check_state_pod_mounts`/`_state_mount_findings`,
`check_gateway_never_gets_state_token`, `check_state_process_can_read_mount` — see
`scripts/secret_check.py:91-197`). There is, and apparently always has been, no
`tests/test_secret_check.py` (confirmed: no such file exists in the 122-entry baseline
manifest either, so this is pre-existing untested surface, not a new Day-4-introduced test
file that was skipped). The new Day 4 logic on top of that untested foundation — in
particular `check_gateway_never_gets_state_token`, a genuine negative-permission-style
security assertion — would be cheap to cover with the same `get_json`-mocking pattern already
used throughout `tests/test_cluster_check_failure_handling.py`.

**Finding: `DAY4-TEST-L1`** (Low — extends known baseline debt; not a new regression risk
class, but a missed opportunity given the low cost).

### 2.8 `scripts/kube.py` — new `KUBECONFIG_PATH` resolution/`--kubeconfig` flag untested

`scripts/kube.py:15-22` adds `KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH") or str(Path.home() / ".kube" / f"{CLUSTER_NAME}.config")`,
and `run()` (line ~104) now always passes `--kubeconfig KUBECONFIG_PATH`. `tests/test_kube.py`
only covers `verify_context()`/`wait_until()` (unchanged since Day 3); there is no test
asserting the env-var override takes precedence, the home-directory fallback is correct, or
that `run()`'s constructed argv actually includes `--kubeconfig`. A regression here would
silently misdirect every live script at the wrong kubeconfig file with no unit test to catch
it before a live run.

**Finding: `DAY4-TEST-L2`** (Low).

---

## 3. Direct restoration/rollback test pattern assessment (task priority #2)

Where dedicated failure-handling tests **do** exist for this tier of script (Day 1–3 carried
forward: `tests/test_cluster_check_failure_handling.py`, `tests/test_reconcile_check_failure_handling.py`,
`tests/test_final_state_check.py`), I confirmed they call the **real** production functions
(`reconcile_check.main()`, `final_state_check._settled_snapshot()`,
`cluster_check.check_configmap_consumption()`) with only `subprocess`/`kube.run`/`get_json`/
`kube.verify_context` mocked at the collaborator boundary — this is the correct pattern the
task asks me to look for, and I found no instance of the anti-pattern ("a mocked restoration
helper merely asserted as called") anywhere in the existing, passing test suite.

The problem for Day 4 specifically is not that the anti-pattern is present — it is that the
five new Day 4 live-cluster scripts (`storage_bootstrap.py`, `storage_hardening_check.py`,
`state_check.py`, `persistence_check.py`, `retention_check.py`) have **no test of either
kind**, good or bad (§2.1, §2.2). I therefore could not verify, via any repeatable
Docker-free test, that CalledProcessError/TimeoutExpired handling, failure-before-mutation,
failure-during-mutation, or failure-during-restoration are handled correctly in these five
scripts — only that they were observed to work in the one retained live run
(`day4-final-candidate-20260908T112009Z-ckQ1uj/day4-check.log`). I did not find evidence they
attempt to catch `SIGKILL`/uncatchable signals, and correctly do not claim to (their `finally`
blocks are a best-effort guarantee against normal exceptions and `sys.exit`, consistent with
the documented, honest scope of Python's `try/finally`).

`scripts/portforward.py` itself is byte-for-byte unchanged from Day 3 (confirmed:
`git diff scripts/portforward.py` produces no output), and its existing
`tests/test_portforward_signal.py` (unchanged, still passing as part of the 392) continues to
cover its finally-block/signal-cleanup contract — no new gap there.

---

## 4. Persistent baseline provenance (task priority #3)

**Question:** does the observed live record `{"value": "day4-retention-054e49df1b0a481c"}`
represent the initial pre-Day-4 baseline, a leftover test artifact, or something untraceable?

**Answer, fully evidenced from the retained, accepted log
`day4-final-candidate-20260908T112009Z-ckQ1uj/day4-check.log`** (excerpts saved in this
review's evidence directory):

1. **Before** `persistence_check.py`/`retention_check.py` ran in that candidate's own
   sequence, the live persisted value was already `day4-retention-27432b9e42d54f11`
   (log line 1419/1470) — itself already a `retention_check.py`-shaped marker
   (`day4-retention-` + 16 lowercase hex chars, exactly `uuid.uuid4().hex[:16]`,
   `scripts/retention_check.py:184`), i.e. a leftover from an **earlier, unretained** run,
   not the pristine `{"value": null}` default `state/server.py:_initialize_state_file()`
   writes on a genuinely fresh PVC (`state/server.py:186-204`).
2. `persistence_check.py` ran (log lines 1638-1654): captured `original_value =
   "day4-retention-27432b9e42d54f11"`, wrote/verified its own marker
   `day4-persistence-375af4a42fc44257`, deleted/replaced the Pod (new UID
   `7d901db4-...`, same PVC UID `00b512bf-...`, same PV UID `2c7d826b-...`), and — per its
   own explicit `finally`-block contract (`scripts/persistence_check.py:222-258`) —
   **restored** `original_value` (log line 1650: `RESTORATION: original record restored
   through service chain`).
3. `retention_check.py` ran immediately after (log lines 1657+): it generates its **own** new
   marker `day4-retention-054e49df1b0a481c` (`scripts/retention_check.py:184`), scales
   `maops-state` 1→0→1, and verifies the marker survives (log line 1672). **Unlike
   `persistence_check.py`, `retention_check.py` never captures nor restores a pre-existing
   state *value*** — I confirmed this directly by reading the full script: it captures
   `original_pvc_uid`/`original_phase`/`original_volume`/`original_pv_phase`/
   `original_pod_uid` (identity/structural state only) but there is no `original_value`
   variable anywhere in the file, and `restore_state()` (127-167) only restores the replica
   count and waits for readiness — it never issues a `PUT /state` with any prior value.
   This is consistent with its own docstring's restoration promise ("maops-state is ALWAYS
   restored to exactly 1 replica"), which deliberately does not claim to restore the
   persisted *value*.
4. `final_state_check.py` ran last (log line 1682+). Its own docstring for
   `check_state_final_state()` states explicitly: "Does not re-verify the persisted record's
   value ... this is the structural/identity baseline only" (`scripts/final_state_check.py:165-171`).
   I confirmed by reading the function body that it checks StatefulSet replica readiness,
   Pod placement, PDB absence, and PVC phase/capacity only — it never calls `GET /state` or
   compares against any captured baseline.

**Conclusion:** the marker the operator observed (`day4-retention-054e49df1b0a481c`) is
**fully traceable** to `retention_check.py`'s own test run within the accepted Sept 8
sequence, is **not** the pristine post-install default, and is **never restored nor
independently re-verified against any captured baseline** by any script in the `day4-check`
pipeline (`state-check → persistence-check → retention-check → final-state-check`,
`Makefile:193-196`). The final persisted value after a full, successful `make day4-check` run
is therefore, by design/omission, whatever `retention_check.py`'s own randomly-generated
marker happened to be — an asymmetry with `persistence_check.py`'s explicit restore
contract, and one no automated check anywhere would catch as a regression, since nothing
asserts "final value == value observed at the start of the suite."

**What I could not verify (open/unverified):** the ultimate origin of the
*earlier* leftover value `day4-retention-27432b9e42d54f11` (i.e., which prior, unretained
run/manual experiment produced it) is not reconstructable from the evidence directories I was
given access to (`day4-implementation-candidate-20260908T163457Z/` contains only
static/unit-test logs, no live `day4-check.log`; no earlier live log is retained). This is a
genuine gap in retained evidence, not an assumption on my part. Per the task's own guidance, I
am not treating this marker as "expected merely because checks execute sequentially," nor as
"a defect merely because it looks like test data" — I traced the actual restoration code and
the actual log sequence to reach the conclusion above, and the remaining unknown (the value
*before* line 1419's leftover) is stated here explicitly rather than papered over.

I did not perform any live experiment to establish forward-looking provenance (e.g. writing a
new arbitrary baseline and confirming restoration), since that would constitute a
mutating live-cluster action outside my permitted scope. I note it here only as the
already-implied next step the task itself suggested as a possibility, without performing it.

---

## 5. Findings summary

| ID | Severity | One-line summary |
|---|---|---|
| DAY4-TEST-H1 | High | `storage_bootstrap.py`/`storage_hardening_check.py`: zero unit tests for idempotent hardening, unfamiliar-state refusal, and node-restoration ordering logic. |
| DAY4-TEST-H2 | High | `state_check.py`/`persistence_check.py`/`retention_check.py`: zero unit tests; the actual Day 4 restoration/retry/timeout-race-fix logic is unverified outside live runs. |
| DAY4-TEST-H3 | High | `retention_check.py` never captures/restores the pre-test state *value* (only replica identity), and `final_state_check.py` explicitly never checks the value either — confirmed root cause of the observed leftover marker, with no test/check anywhere that would catch this drift as a regression. |
| DAY4-TEST-M1 | Medium | `app/server.py`/`gateway/server.py` new `/state`, `/internal/state` proxy handlers and state-target allowlist: zero unit tests despite an established, cheap test pattern already in the repo for the analogous backend-allowlist logic. |
| DAY4-TEST-M2 | Medium | `state/server.py` (the persistence workload itself): zero unit tests for schema validation, atomic-write/fsync outcome classification, and auth — all trivially testable without a cluster. |
| DAY4-TEST-M3 | Medium | `secret_bootstrap.py`'s new `state`/`all` dispatch targets, including both directions of `all`-target partial failure: zero test coverage. |
| DAY4-TEST-M4 | Medium | 40 of 52 new `state.*` static manifest checks (including every state security-context check) have no dedicated negative-mutation test, violating this project's own established per-check testing standard. |
| DAY4-TEST-L1 | Low | `secret_check.py` extended (+130 lines) with no dedicated test file; pre-existing, project-wide gap for this specific script, extended rather than introduced. |
| DAY4-TEST-L2 | Low | `kube.py`'s new `KUBECONFIG_PATH` resolution/`--kubeconfig` flag construction is untested. |

**Counts:** Critical: 0. High: 3. Medium: 4. Low: 2.

No Critical findings. However, three High and four Medium findings are unresolved as of this
review.

---

## 6. Validation credibility (task priority #6) — scoped to what I could verify

The authoritative `day4-check` Makefile recipe (`Makefile:170-198`) is explicitly sequential
(recipe lines, not a parallelizable prerequisite list), matching its own stated intent. I
verified via the retained `day4-final-candidate-20260908T112009Z-ckQ1uj/day4-check.log` (not
merely trusting the PASS summary) that the log contains concrete supporting evidence for:
Pod-replacement-with-new-UID-same-PVC/PV-UID (log lines 1638-1648, quoted in §4), and marker
readback through the real service chain (not a direct hostPath read) both before and after
Pod replacement.

On the image-validation question: I performed one bounded, read-only live check
(`kubectl get pod maops-state-0 -o json`, saved in
`reviewer-probes/live-readonly-maops-state-0-pod.json`). The Pod spec's `image` field is the
tag `maops-kubernetes-state:0.4.0`; the **live** `status.containerStatuses[0].imageID` is
`docker.io/library/import-2026-09-08@sha256:f8931e08...` — a real content digest distinct
from the tag. I did not find, in the scripts I reviewed for this pass
(`state_check.py`, `persistence_check.py`, `retention_check.py`), any assertion that compares
this live `imageID` against a digest independently computed from the host-built image at
`make image-build` time — these scripts check tag/`imagePullPolicy` only
(`scripts/validate_manifests.py`'s `state.statefulset.image` check is a static tag-string
match, not a digest match). Whether `cluster_check.py` (unchanged since Day 3, out of my
diff scope) does this for gateway/app is a question for the cluster-integration-engineer's
review, not re-litigated here; I flag only that I found no such digest-level tie for the new
state workload specifically. This observation is descriptive, not a new finding ID, since
verifying image-to-running-workload identity is primarily a live-cluster-integration
concern rather than a Docker-free-test-authorship gap — I note it here only because the task
asked me to keep the digest/imageID distinction explicit and cite concrete evidence rather
than repeat an unverified "image was correctly loaded" claim.

Inherited Day 1–3 regression coverage: all 392 existing tests pass unmodified (§1). I did not
re-litigate DAY1-INT-I2, DAY2-INT-I1, DAY3-SEC-I1, DAY3-TEST-L2, or DAY3-REL-L1 — these are
treated as already-adjudicated baseline debt per the task's instruction, and I found no
evidence of regression against their final adjudication in the files I reviewed for Day 4.

---

## 7. Testing verdict

**FAIL** (from a test-engineering-quality perspective only; this is explicitly *not* a
release-readiness adjudication, which is `release-engineer`'s separate step).

Rationale: three High-severity findings (DAY4-TEST-H1, H2, H3) mean the actual Day 4
persistence/restoration/hardening *logic* — the part of this stage that is the entire point
of "stateful persistence" — has no Docker-free regression coverage at all for five of its six
new scripts, and the one cross-script contract question this review was specifically asked to
resolve (does the platform return to its starting persisted value?) resolves to "no, by
design/omission, and nothing catches it." Four additional Medium findings (state-proxy HTTP
handlers, the state workload's own server logic, secret-bootstrap's state/all dispatch, and
40 of 52 new manifest security/structural checks) mean the newly-added static and unit-test
surface is materially thinner than the rigorous standard already established and followed
elsewhere in this same repository (Day 1–3 scripts, and even Day 4's own
`validate_manifests.py`/`final_state_check.py` test files for the checks that *are* covered).
Per the task's explicit instruction, I am stating this plainly rather than softening it:
before PR/merge there are unresolved High and Medium findings from this review.

---

## 8. Evidence

All commands, full logs, and reviewer probes are saved under:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-test-review-20260910T054500Z-tE7qk/`

# Day 3 / v0.3.0 — Independent Kubernetes Security Review

**Role:** `kubernetes-security-reviewer` (independent, adversarial review — not the implementer)
**Scope:** Project 4 (`maops-kubernetes-platform`), Day 3 / v0.3.0 — scaling, scheduling, rolling
updates/rollback, and PodDisruptionBudget/Eviction behavior for both `maops-app` and
`maops-gateway`.
**Method:** Direct, independent inspection of the manifests, Dockerfiles, gateway/app source, and
every new/changed Day 3 script and its unit tests. No other Day 3 review document was read as
input to this review; findings below were derived solely from reading the implementation.

---

## 1. Pod/container security context — both workloads

Read directly from `k8s/base/app-deployment.yaml` and `k8s/base/gateway-deployment.yaml`.

| Control | app | gateway | Verdict |
|---|---|---|---|
| `runAsNonRoot: true` | ✅ (pod-level) | ✅ (pod-level) | PASS |
| `runAsUser: 10001` | ✅ | ✅ | PASS |
| `runAsGroup: 10001` | ✅ | ✅ | PASS |
| `allowPrivilegeEscalation: false` | ✅ (container-level) | ✅ | PASS |
| `readOnlyRootFilesystem: true` | ✅ | ✅ | PASS |
| `capabilities.drop: [ALL]` | ✅ | ✅ | PASS |
| `seccompProfile: RuntimeDefault` | ✅ (pod-level) | ✅ | PASS |
| `automountServiceAccountToken: false` | ✅ (pod-level) | ✅ | PASS |

No `privileged`, `hostNetwork`, `hostPID`, `hostIPC`, `hostPort`, or `hostPath` fields appear
anywhere in either Deployment, either PDB, either ConfigMap, or either Service. Confirmed by
direct read of every file under `k8s/base/`.

`app/Dockerfile` and `gateway/Dockerfile` both pin `USER 10001:10001`, matching the manifests —
no drift between build-time and runtime identity.

**Day 3 additions (new fields, not present Day 2) were also checked and do not weaken the above:**
`affinity.nodeAffinity` (control-plane exclusion) and `topologySpreadConstraints` are
scheduling-only fields; they do not touch `securityContext`, and the new Pod template merge patch
used by `rollout_check.py` (`spec.template.metadata.annotations` only) cannot touch
`securityContext` either — a `kubectl patch --type=merge` against that path structurally cannot
reach `spec.template.spec.containers[].securityContext`. Rolling-update-produced Pods therefore
inherit the identical hardened `securityContext` as the baseline — verified both by static
reading of the patch body (`ANNOTATION_KEY` under `metadata.annotations` only) and by the fact that
`rollout_check.py` never re-applies or regenerates a Pod spec from any other source.

**Resources bounded (unchanged, verified):** both containers carry identical
`requests: {cpu: 50m, memory: 32Mi}` / `limits: {cpu: 250m, memory: 128Mi}`. No workload is
unbounded.

**Finding: none.** Day 2's hardened baseline is fully intact for both workloads through Day 3's
scaling/rollout/scheduling/PDB additions.

---

## 2. Secret lifecycle — `maops-internal-auth` / `internal-token`

Reviewed `scripts/secret_bootstrap.py`, `scripts/secret_check.py`, `scripts/kube.py`, and confirmed
against the live deployment mount stanzas.

- **CSPRNG generation:** `secrets.token_urlsafe(32)` — `secrets` module, not `random`. PASS.
- **No usable Secret in Git:** `k8s/base/kustomization.yaml` lists no Secret resource; repo-wide
  `grep -rl "kind: Secret"` returns nothing. The Secret is created purely out-of-band by
  `secret_bootstrap.py` against the live cluster. PASS.
- **Token never printed:** every code path in `secret_bootstrap.py` logs only shape/length
  metadata (`validate_secret_shape` returns a message with byte length, never the value); the
  token variable itself is never passed to `print`/logging. PASS.
- **Token not placed on command line:** `create_secret()` writes the token to a private temp file
  and passes only `--from-file=<key>=<path>` to `kubectl create secret` — the value itself never
  appears in argv (and therefore never in `/proc/<pid>/cmdline` or process-listing tools). PASS.
- **Temporary-file permissions:** `tempfile.mkstemp()` (created 0600 by the OS) is additionally
  `os.chmod(tmp_path, 0o600)`'d before the token is written. PASS.
- **Cleanup:** `finally: os.remove(tmp_path)` guarded by a small `_suppress_missing` context
  manager for `FileNotFoundError` — cleanup happens regardless of whether `kubectl create` raised.
  PASS.
- **Existing valid Secret preserved:** `main()` short-circuits to "preserving it (no rotation)"
  whenever `get_existing_secret()` returns non-`None`; `create_secret()` is only ever called on the
  does-not-exist path. PASS.
- **Malformed Secret fails closed:** `validate_secret_shape()` checks key presence, valid base64,
  and non-empty decoded length; any failure returns `main() -> 1` (non-zero), both for the
  preserve-existing path and the just-created path. PASS.
- **Missing namespace distinguished from missing Secret:** `get_existing_secret()` explicitly
  queries `get namespace <NAMESPACE>` first and raises a distinct `RuntimeError` before ever
  inspecting Secret state — a missing namespace cannot be misclassified as "generate a new token".
  This is the DAY2-INT-L1 fix, and it is still in place and exercised by
  `tests/test_secret_bootstrap.py`. PASS.
- **Read-only Secret mounts:** both Deployments mount `internal-auth` at
  `/var/run/secrets/maops` with `readOnly: true`, and `secret_check.py::_mount_findings` asserts
  this live against both Pods. PASS.
- **HTTP/log/repository non-disclosure:**
  - `http_checks.py::_check_semantics` fails any endpoint whose JSON body contains the substring
    `"token"` anywhere (case-insensitive, checked via `json.dumps(payload).lower()`), applied to
    every endpoint exercised by `check_all_endpoints`.
  - `secret_check.py::check_logs_do_not_expose_token` greps the last 2000 log lines of both
    workloads for the literal decoded token value.
  - `secret_check.py::check_repo_files_do_not_contain_token` scans every `git ls-files`-tracked
    file for the literal live token value.
  - `gateway/server.py::log_message` is overridden to log only the request line/status, never
    header values — the token, which only ever travels as a header value, cannot reach stdout via
    the access log.
  - All PASS as designed; no gaps found in the non-disclosure surface for Day 3's addition (no new
    endpoints were added that touch the token).

**Finding: none.** Secret lifecycle discipline is intact and independently verifiable end-to-end.

---

## 3. Gateway target allowlist vs. ConfigMap mutation

`gateway/server.py` (unchanged Day 2 code, re-verified for Day 3): `BACKEND_TARGET_VALID` is
computed once at import time from the ConfigMap-sourced `BACKEND_HOST`/`BACKEND_PORT` env vars,
checked against the hardcoded `ALLOWED_BACKEND_HOST = "maops-app"` /
`ALLOWED_BACKEND_PORT = 8080`. Both `_handle_readyz()` and `_handle_backend()` check
`BACKEND_TARGET_VALID` **before** calling `_backend_request()` and fail closed (503, no token
attached, no target echoed) if it is false. A ConfigMap mutation changing `BACKEND_HOST` to an
attacker-controlled host cannot cause the Secret-bearing `/internal/info` proxy call
(`_handle_backend`) to be sent anywhere but `maops-app:8080`, because `_backend_request()` is
categorically never invoked once the allowlist check fails — there is no code path that reaches
`urllib.request` with the token header while `BACKEND_TARGET_VALID` is false.

Day 3 introduces no new outbound HTTP call sites in the gateway or app source (Day 3 is
manifest/tooling-only for `gateway/server.py` and `app/server.py`, confirmed unchanged except for
version-string bump). The allowlist's protection surface is therefore unchanged and intact.

**Finding: none.**

---

## 4. Day 3 scripts — command construction, injection, credential handling

Reviewed `scaling_check.py`, `rollout_check.py`, `pdb_check.py`, `scheduling_check.py`,
`context_check.py`, `final_state_check.py`, `kube.py`, `portforward.py`.

- **No `shell=True` anywhere** in `scripts/` (repo-wide grep confirmed zero matches).
- Every `kubectl` invocation (via `kube.run()` or the ad hoc `subprocess.run([...])` in
  `pdb_check.attempt_eviction`) passes arguments as a list, never through a shell — command
  injection via a crafted Pod/Deployment name is not possible even in principle, independent of
  where those names originate.
- **Dynamically selected Pod names:** `pdb_check.py`'s victim Pod name and
  `rollout_check.py`/`scaling_check.py`'s Pod UIDs are read back from `kubectl get pods -o json`
  (trusted, structured API data) and interpolated only into subsequent list-form argv elements or
  an f-string URL path passed as a single argv element to `subprocess.run` (never through a shell)
  — no injection vector.
- **Deployment names:** `maops-app`/`maops-gateway` are sourced from the `kube.py` constants, never
  from external input.
- **Credentials in argv:** no script ever passes the internal token, or any Secret value, as a
  command-line argument to any subprocess. The only Secret-touching script (`secret_bootstrap.py`)
  was covered in §2. The new Day 3 scripts never read `INTERNAL_SECRET`/`INTERNAL_SECRET_KEY` at
  all — scaling/rollout/PDB/scheduling validation has no reason to touch the token, and indeed
  does not.
- **Unsafe temporary files:** the only new-in-Day-3 script writing to disk is none — Day 3 scripts
  perform only in-memory JSON manipulation and `kubectl create --raw -f -` via stdin (`input=`
  kwarg to `subprocess.run`, not a temp file at all) for the Eviction API call. No unsafe temp-file
  handling was introduced.
- **Context handling:** every Day 3 script calls `kube.verify_context()` first, which (a) requires
  the exact `kind-maops-k8s-day3` context to be present in kubeconfig, and (b) independently
  cross-checks that the live cluster's node names actually belong to `maops-k8s-day3` (not just
  that a context by that name exists and points at *some* reachable cluster). Every `kubectl` call
  in every script passes `--context` explicitly (never relying on ambient
  `current-context`) — confirmed by reading `kube.run()`, which prepends `--context CONTEXT` to
  every invocation, and `pdb_check.attempt_eviction`, which passes `--context kube.CONTEXT`
  explicitly rather than going through `kube.run()`. No script can silently mutate the wrong
  cluster. `final_state_check.py` additionally proves the Day 1 (`maops-k8s-day1`) and Day 2
  (`maops-k8s-day2`) kind clusters remain untouched, and `Makefile`'s `cluster-delete` target
  deletes only `$(CLUSTER_NAME)` (`maops-k8s-day3`).
- **Untrusted API error text accidentally treated as trusted success:** see §5 below — this is
  where a genuine finding was identified.
- **Broad Kubernetes permissions assumptions:** Day 3 introduces no ServiceAccount/RBAC (correctly
  — see §7), so all `kubectl` calls run under whatever ambient credential the operator running
  `make day3-check` already holds (kind's default cluster-admin kubeconfig context for a local
  kind cluster). No script asserts or depends on a *specific* RBAC grant existing; this is
  consistent with Day 3's stated scope (RBAC is explicitly Day 5). No finding.

---

## 5. PDB/Eviction behavior — classification integrity

**This is the one substantive finding of this review.**

`pdb_check.py::classify_eviction_result()`:

```python
if "TooManyRequests" in combined and "disruption budget" in combined.lower():
    return "rejected_by_pdb"
if "TooManyRequests" in combined:
    return "rejected_by_pdb"
```

The second `if` is a strict superset of the first — it fires on *any* response containing the
substring `TooManyRequests`, regardless of whether "disruption budget" is present. The Kubernetes
API server also returns HTTP 429 `TooManyRequests` for reasons unrelated to PodDisruptionBudget
denial — most notably its built-in **API Priority and Fairness** flow-control mechanism (a busy
apiserver, or a client exceeding its assigned priority-level queue, returns exactly this status and
reason string with a *different* message body, e.g. "Please try again later." with no mention of a
disruption budget). A transient apiserver load spike or flow-control throttling during the
`kubectl create --raw .../eviction` call would be misclassified as `"rejected_by_pdb"` — the
strongest possible claim this test makes — even though the PDB was never actually consulted.

`tests/test_pdb_check.py::test_rejected_with_too_many_requests_alone` **codifies this exact
behavior as intended** (asserts bare `TooManyRequests` alone classifies as `rejected_by_pdb`),
confirming this is a deliberate implementation choice, not an oversight caught by the test suite —
the test suite currently protects the bug rather than catching it.

The review's explicit charge was to check "whether the test distinguishes an actual PDB rejection
... from unrelated failures" — under API Priority and Fairness load (plausible in CI, under
parallel `make` targets, or on a resource-constrained kind node), this test can report "PASS: PDB
correctly blocked eviction" when the real event was apiserver throttling unrelated to the PDB,
producing a false-positive proof of the security/availability control under test.

Everything else in this area is sound:
- A bare non-zero exit code alone is never treated as success — `classify_eviction_result` only
  returns `"succeeded"` on an actual `returncode == 0` with a parseable `Status: Success`-shaped
  body; any other non-zero outcome not matching a `TooManyRequests` pattern is `"inconclusive"`
  and fails the check (`test_failure_for_unrelated_reason_is_inconclusive_not_pdb_rejection`
  confirms a `NotFound` 1-exit is correctly classified as inconclusive, not a pass).
- An unexpectedly *successful* eviction is correctly treated as a hard FAIL, never a pass with a
  footnote (`test_unexpected_eviction_success_is_a_failure`).
- Victim selection is genuinely constrained to Ready, non-terminating Pods
  (`healthy_pods` filter in `run_pdb_experiment`, with dedicated regression tests
  `test_terminating_pod_is_never_selected_as_victim` and
  `test_not_ready_pod_is_never_selected_as_victim`) — a straggler Pod from the immediately
  preceding 3→2 scale-down cannot be picked, which would otherwise let an eviction "succeed" for a
  reason unrelated to the PDB and falsely read as "PDB failed to protect the workload."
- Post-eviction-attempt identity is checked by name **and** UID **and** absence of
  `deletionTimestamp` (`pod_still_present_and_not_evicted`), not merely "a pod with this name still
  exists" — a Deployment-controller replacement Pod reusing the same name would not be
  misclassified as "not evicted" (`test_replaced_pod_with_same_name_different_uid_is_evicted`).
- Restoration (2→3) is unconditionally attempted in a `finally` block once the workload has been
  scaled down, independent of whether the eviction experiment itself raised, and restoration
  failures are recorded and surfaced distinctly from ordinary assertion failures.

### DAY3-SEC-M1
- **Severity:** Medium
- **Affected files:** `scripts/pdb_check.py:153-157`, `tests/test_pdb_check.py:44-46`
- **Evidence:** `classify_eviction_result()`'s second `TooManyRequests`-only branch makes the
  preceding `"disruption budget"`-qualified branch dead code — any 429 `TooManyRequests` response,
  regardless of cause, is classified `"rejected_by_pdb"`. Test
  `test_rejected_with_too_many_requests_alone` locks this in as intended behavior.
- **Impact:** Kubernetes' own API Priority and Fairness flow control returns HTTP 429
  `TooManyRequests` for reasons entirely unrelated to a PodDisruptionBudget (client throttling,
  apiserver overload). Under those conditions this validation would report "PASS: PDB correctly
  blocked eviction" when the PDB was never actually the reason the eviction failed — a
  false-positive proof of an availability control that Day 7's release-readiness sign-off may rely
  on. This is a validation-integrity gap, not an externally exploitable vulnerability: it cannot be
  triggered by an outside attacker and does not itself grant unauthorized access or disrupt the
  workload; its harm is a misleading PASS in the project's own security/availability evidence.
- **Remediation:** Require the `"disruption budget"` (or a Kubernetes-version-stable equivalent,
  e.g. matching on the `reason: "Cannot evict pod as it would violate the pod's disruption
  budget."` phrase from the `Status.details`/`message` field) substring unconditionally — drop the
  bare-`TooManyRequests` fallback branch, and update
  `test_rejected_with_too_many_requests_alone` to expect `"inconclusive"` for a `TooManyRequests`
  response that lacks the disruption-budget message, adding a new test for the qualified case that
  currently exists.
- **Release-blocking:** NO — the underlying PDB enforcement itself (verified independently via
  `disruptionsAllowed` status transitions and confirmed non-eviction of the victim Pod) is real and
  correctly proven; only the specific-cause attribution string-matching has a gap. Recommended to
  fix before Day 7's final hardening pass, not blocking for Day 3's v0.3.0 tag.

---

## 6. Scheduling / rollout Pod hardening carry-forward

`scheduling_check.py` and `final_state_check.py` prove worker-only scheduling and topology spread
live, but neither re-asserts `securityContext` on the Pods it inspects — that assertion lives in
`workload-security-validation`'s scope (Day 1/2 checks), not scheduling. This is a scope boundary,
not a gap: §1 of this review independently confirms, by reading the Deployment manifests directly
(the single source of truth for every Pod template Kubernetes will ever create from them,
including ones produced mid-rollout by `rollout_check.py`), that no code path in Day 3 can produce
a Pod with a weaker `securityContext` than the baseline. No separate live-assertion is strictly
necessary for this review's purposes, though a defense-in-depth recommendation is noted as
informational below.

### DAY3-SEC-I1
- **Severity:** Informational
- **Affected files:** `scripts/rollout_check.py`, `scripts/scaling_check.py`
- **Evidence:** Neither script live-asserts `pod.spec.containers[].securityContext` on the newly
  created Pods after a scale-up or rolling update, relying instead on the structural argument that
  the Deployment's Pod template (unchanged by these scripts) is the only source of new Pod specs.
- **Impact:** None today — the structural argument holds, and this review independently verified
  it by reading the actual patch/scale code paths. This is purely a defense-in-depth gap: a live
  assertion would also catch a hypothetical future admission-time mutation (e.g., a mutating
  webhook introduced in a later day) silently weakening the security context of newly created Pods
  specifically during scale/rollout events, which a static manifest read would not detect.
- **Remediation:** Optional — add a lightweight live `securityContext` spot-check on the
  post-scale-up / post-rollout Pod set in `scaling_check.py`/`rollout_check.py`, reusing the same
  assertions `workload-security-validation` already runs elsewhere.
- **Release-blocking:** NO.

---

## 7. Scope boundaries — no RBAC/ServiceAccount/NetworkPolicy, no false equivalence claims

- Repo-wide search for `ServiceAccount`, `Role`, `RoleBinding`, `ClusterRole`, `ClusterRoleBinding`,
  `NetworkPolicy` under `k8s/base/` and the `Makefile` returns nothing except: (a) the pre-existing
  `automountServiceAccountToken: false` field (a Pod-spec field, not an object creation), (b) the
  pre-existing `cluster_check.py` check that no ServiceAccount token volume is mounted (a Day 2
  regression guard, unchanged), and (c) `validate_manifests.py`'s `ALLOWED_KINDS`-style constant
  list, which *lists* `Role`/`RoleBinding`/`ClusterRole`/`ClusterRoleBinding`/`ServiceAccount`/
  `NetworkPolicy` only to explicitly recognize-and-reject them as out of scope for the current
  Kustomize base — confirmed these are guard rails, not object definitions.
- `docs/roadmap.md` correctly assigns "ServiceAccount, RBAC, NetworkPolicy" to Day 5 (v0.5.0), not
  Day 3, and Day 3's own theme line reads "Scaling, scheduling, rolling updates, rollback,
  availability (PDB)" — no RBAC/NetworkPolicy claimed for this day.
- Searched `docs/architecture.md`, `docs/roadmap.md`, `gateway/server.py`, and every script under
  `scripts/` for language equating Secret-based auth with network isolation or segmentation
  (`"network isolation"`, `"equivalent to network"`, `"network segmentation"`, `"replaces...
  network"`) — no matches. The DAY2-SEC-L1 comment in `gateway/server.py` explicitly disclaims this
  ("This does NOT replace Day 5's RBAC/NetworkPolicy work — it only prevents this process from
  sending the internal token to an unintended host"), which is accurate and was reconfirmed against
  the current file.

**Finding: none.** Day 3 correctly stays within its stated scope on all three fronts.

---

## 8. Carried-forward finding status (adjudicated independently)

- **DAY2-INT-I1 — remains OPEN.** `scripts/endpointslice.py`'s `count_ready_endpoints()` still
  sums ready endpoints across all EndpointSlices for a Service without deduping by `addressType`.
  The live cluster remains single-stack IPv4 (Day 3 introduces no dual-stack configuration
  anywhere in `k8s/base/` or `kind/cluster.yaml`), so this remains correct today and is unchanged,
  non-blocking, informational technical debt, exactly as adjudicated on Day 2. This review
  independently reconfirms it should stay open rather than being silently closed by Day 3's
  unrelated scaling/rollout work, which reuses this same helper (`scaling_check.py`,
  `rollout_check.py`, `final_state_check.py`) without altering its dedup behavior.
- **DAY1-INT-I2 — remains OPEN.** `app/Dockerfile` and `gateway/Dockerfile` still hardcode
  `/usr/bin/python3.11` as the ENTRYPOINT/interpreter path, coupled to the pinned
  `gcr.io/distroless/python3-debian12` digest (unchanged from Day 1/2, reconfirmed by direct read
  of both Dockerfiles). No base-image change occurred in Day 3, so this remains accurate,
  non-blocking, accepted debt.
- **DAY1-REL-I1 — remains CLOSED.** `scripts/version_check.py` continues to cross-check `VERSION`
  (`0.3.0`) against the Makefile-derived image tags and every manifest's
  `app.kubernetes.io/version` label; `tests/test_version_check.py` continues to exercise
  drift-simulation negative cases. No regression found in Day 3's `VERSION` bump or label updates
  (`k8s/base/*.yaml` all consistently carry `app.kubernetes.io/version: "0.3.0"`, confirmed by
  direct read of both Deployments and both PDBs above).

---

## 9. Totals and verdict

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 1 | DAY3-SEC-M1 |
| Low | 0 | — |
| Informational | 1 | DAY3-SEC-I1 |

**Release-blocking findings: 0.**

Day 3's container/Pod security hardening (§1), Secret lifecycle discipline (§2), gateway allowlist
(§3), and script-level command-construction/context-handling hygiene (§4) are all intact and
independently verified against the actual implementation, not assumed from Day 2. Scope boundaries
around RBAC/ServiceAccount/NetworkPolicy and the Secret-vs-network-isolation distinction are
correctly respected (§7). The one substantive finding (DAY3-SEC-M1) is a validation-integrity gap
in how the PDB/Eviction test attributes a 429 response to the PDB specifically, rather than a
weakness in the PDB enforcement itself — the underlying Kubernetes PDB behavior is genuinely
proven live and correctly restored on both success and failure paths. An informational
defense-in-depth suggestion (DAY3-SEC-I1) is non-blocking.

**Verdict: APPROVE WITH CONDITIONS**

Condition: address DAY3-SEC-M1 (tighten `classify_eviction_result`'s `TooManyRequests`
classification to require the disruption-budget-specific message, and correct the test that
currently locks in the looser behavior) before Day 7's final production-readiness/independent
review pass. Not required before tagging Day 3 / v0.3.0.

---

PROJECT 4 DAY 3 KUBERNETES SECURITY REVIEW COMPLETE

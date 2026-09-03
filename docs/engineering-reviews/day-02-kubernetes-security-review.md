# Day 2 Kubernetes Security Review — v0.2.0

**Reviewer:** kubernetes-security-reviewer (independent pass)
**Scope:** Working tree of `feature/day-2-service-discovery-secrets` (uncommitted state), not any committed history.
**Target release:** v0.2.0
**Review basis:** `kubectl kustomize k8s/base` rendered manifest, live cluster `kind-maops-k8s-day2` (namespace `maops-platform`), `kubectl exec`/`-o json` runtime evidence, and source review of `app/server.py`, `gateway/server.py`, `scripts/secret_bootstrap.py`, `scripts/secret_check.py`, `scripts/validate_manifests.py`, and associated tests.

This document does not reference or rely on `docs/engineering-reviews/day-02-kubernetes-architecture-review.md`; findings below are independently derived.

**Live-cluster verification: PERFORMED.** A running kind cluster `kind-maops-k8s-day2` was found with `maops-app` (2/2 Running) and `maops-gateway` (2/2 Running) pods in `maops-platform`, and a live Secret `maops-internal-auth`. All runtime claims below were checked with `kubectl get pod -o json`, `kubectl exec`, and port-forwarded HTTP requests against the live pods, not inferred from YAML alone.

**Secret non-disclosure discipline for this document itself:** at no point below is the live token's plaintext, base64, or hex form printed or written. Only safe metadata (Secret name/namespace/key/type/creation timestamp, byte length classes, and file permission bits) is referenced.

---

## SECRET BOOTSTRAP — `scripts/secret_bootstrap.py`

| ID | Severity | Title | Status |
|---|---|---|---|
| DAY2-SEC-I1 | Info | Token generation is cryptographically strong | PASS |
| DAY2-SEC-I2 | Info | No hardcoded/committed usable credential | PASS |
| DAY2-SEC-I3 | Info | Explicit context/namespace targeting | PASS |
| DAY2-SEC-I4 | Info | Existing Secret preserved, not silently rotated | PASS |
| DAY2-SEC-I5 | Info | Malformed Secret fails closed | PASS |
| DAY2-SEC-I6 | Info | Token never printed/logged | PASS |
| DAY2-SEC-I7 | Info | Token never on process command line | PASS |
| DAY2-SEC-I8 | Info | Temp file lifecycle and permissions | PASS |

**Evidence:**

- `generate_token()` (`scripts/secret_bootstrap.py:45-46`) uses `secrets.token_urlsafe(32)` — CSPRNG-backed, 256 bits of entropy, not `random`/`uuid`. `tests/test_secret_bootstrap.py::NonDisclosureTests::test_generate_token_is_non_trivial_and_url_safe` asserts length ≥ 32 and shell-safety (no space/newline).
- No usable credential is committed: `git ls-files | xargs grep` scan (executed live during this review, see "runtime evidence" below) found the live token in **zero** of 53 tracked files. `k8s/base/kustomization.yaml` (`k8s/base/kustomization.yaml:1-11`) never lists a Secret manifest, and `kubectl kustomize k8s/base` renders no `kind: Secret` (confirmed below under Scope Checks).
- `scripts/kube.py:9-10` hardcodes `CONTEXT = "kind-maops-k8s-day2"` and `NAMESPACE = "maops-platform"`; `secret_bootstrap.py` imports and uses both explicitly on every `run(...)` and `get_existing_secret()` call (`scripts/secret_bootstrap.py:42,53`) — no reliance on "current kubectl context."
- Preservation, not rotation: `main()` (`scripts/secret_bootstrap.py:119-127`) short-circuits to `validate_secret_shape` and returns without ever calling `create_secret()` when `get_existing_secret()` returns non-`None`. Verified live: the cluster's Secret has `creationTimestamp: 2026-09-03T05:23:38Z`, i.e., it was created once and the working scripts do not touch it on repeat invocation (confirmed by code path, since re-running `secret_bootstrap.py` against an existing valid Secret takes the "preserve" branch unconditionally — there is no flag or code path that forces rotation).
- Fail-closed on malformed shape: `validate_secret_shape()` (`scripts/secret_bootstrap.py:64-76`) checks key presence, base64 validity, and non-empty decoded length, returning `(False, message)` on any failure; `main()` returns exit code 1 without calling `create_secret()` in that case (`scripts/secret_bootstrap.py:123-125`). Unit-tested in `tests/test_secret_bootstrap.py::ExistingSecretPreservedTests::test_existing_malformed_secret_fails_closed_without_rotating`.
- Non-disclosure: every `print()` in the script emits only names/messages built from `validate_secret_shape`'s return value, which itself never includes the decoded bytes (`scripts/secret_bootstrap.py:64-76` only ever interpolates `INTERNAL_SECRET`, `INTERNAL_SECRET_KEY`, and `len(decoded)`). Unit-tested directly: `tests/test_secret_bootstrap.py::NonDisclosureTests::test_generated_token_value_never_printed_on_success` captures stdout via `redirect_stdout` and asserts the actual generated token string is absent from it.
- No command-line leakage: `create_secret()` (`scripts/secret_bootstrap.py:79-99`) writes the token to a `tempfile.mkstemp()`-created file (not `mktemp`, avoiding the classic TOCTOU race — `mkstemp` atomically creates and opens the file) and passes only the file *path* via `--from-file=internal-token=<path>` to `kubectl create secret generic`; the token bytes never appear as a `kubectl` argument, so `ps`/`/proc/<pid>/cmdline` cannot leak it.
- Temp file permissions and cleanup: `os.chmod(tmp_path, 0o600)` immediately after `mkstemp` (`scripts/secret_bootstrap.py:85`), and removal happens in a `finally` block (`scripts/secret_bootstrap.py:97-99`) wrapped in a small context manager that also suppresses `FileNotFoundError` so double-cleanup can't raise. Unit-tested for the failure path specifically: `tests/test_secret_bootstrap.py::TempFileCleanupTests::test_temp_file_removed_even_when_kubectl_create_fails` spies on the real `mkstemp` and asserts the file is gone even when `kubectl create secret` raises `CalledProcessError`.
- No `shell=True`: `scripts/kube.py:33-35`'s `run()` builds an explicit argv list (`["kubectl", "--context", CONTEXT, *args]`) passed to `subprocess.run` without `shell=True`, so there is no shell-metacharacter injection surface from the token or any other argument.
- Error messages: the only exception paths that print stderr (`scripts/secret_bootstrap.py:116,135,142`) interpolate only `exc`/`stderr` from `kubectl`'s own output (namespace/RBAC errors) or the shape-validation message — never the decoded token.

No findings in this category.

---

## SECRET MOUNT — app & gateway Deployments

| ID | Severity | Title | Evidence | Release-blocking |
|---|---|---|---|---|
| DAY2-SEC-I9 | Info | Read-only mount, restrictive mode, fsGroup-matched UID/GID confirmed live | PASS (see below) | N/A |

**Evidence (rendered manifest, both `k8s/base/app-deployment.yaml:62-65,89-96` and `k8s/base/gateway-deployment.yaml:62-65,97-104` are identical in shape):**

- `volumeMounts[].readOnly: true` on both, and the Secret volume additionally carries `defaultMode: 288` (decimal) = `0o440` octal, i.e., owner-read + group-read only, no world bit.
- Pod-level `securityContext.fsGroup: 10001` matches `runAsGroup: 10001` on both Deployments.
- **Live confirmation (the specific "common real bug" called out in the task) — checked, not assumed:**
  - `kubectl exec maops-app-... -- python3.11 -c "os.getuid(), os.getgid()"` → `uid 10001 gid 10001`.
  - `kubectl exec maops-app-... -- python3.11 -c "os.stat('/var/run/secrets/maops/internal-token')"` → **mode `0o440`, uid `0` (root), gid `10001`**. The file is root-owned as Kubernetes always makes Secret projections, but because `defaultMode` is `0440` (group-readable) and the pod's `fsGroup`/`runAsGroup` is `10001` matching the file's group ownership, UID 10001 can read it. Had `defaultMode` been left at the Kubernetes default (`0644`, world-readable, not group-restricted the way this baseline intends) or had `fsGroup` been omitted, the specific group-read grant this manifest relies on would not exist — this was verified rather than assumed.
  - Both app and gateway pods actually read and use the token at runtime — confirmed via live `/backend` request returning HTTP 200 real data from the app service (below), and via `app/server.py:39-48` / `gateway/server.py:50-58`'s `load_internal_token()` which is the only place either process reads the file.
- `readOnlyRootFilesystem: true` is set on both containers (`k8s/base/app-deployment.yaml:51`, `k8s/base/gateway-deployment.yaml:51`) and confirmed live: `kubectl exec maops-app-... -- python3.11 -c "open('/app/testwrite','w')"` → `OSError: [Errno 30] Read-only file system`. No writable-root workaround (no `emptyDir` scratch volume, no relaxed `readOnlyRootFilesystem`) was introduced to accommodate the Secret mount — the Secret volume mount itself is read-only and coexists cleanly with a read-only root, because neither `app/server.py` nor `gateway/server.py` performs any file write at runtime (both only `open(..., "rb")` the token file once at import time; no logging to disk, no temp files, no cache files).
- Secret is **not** consumed via environment variable on either workload — both Deployments wire ConfigMaps via `envFrom.configMapRef` only (`k8s/base/app-deployment.yaml:46-48`, `k8s/base/gateway-deployment.yaml:46-48`); there is no `env` entry referencing `secretKeyRef` anywhere in either manifest. This avoids the `kubectl describe pod` / `/proc/<pid>/environ` / crash-dump leakage vector the task calls out.
- No Secret value is exposed via `/config` on either workload: `app/server.py:54-62` and `gateway/server.py:63-70` build `visible_config()` strictly from `os.environ` items prefixed `APP_`/`BACKEND_` — the token is never an environment variable in the first place, so it structurally cannot appear here. Verified live: `curl /config` on both workloads returns only `APP_*`/`BACKEND_*` keys, confirmed by request/response captured during this review (values shown above are non-sensitive ConfigMap data only).
- Neither ConfigMap (`k8s/base/app-configmap.yaml`, `k8s/base/gateway-configmap.yaml`) contains the Secret value or any secret-like key (see CONFIGMAP SECURITY below).

No findings in this category.

---

## APPLICATION AUTH — `app/server.py` `/internal/info`

| ID | Severity | Title | Evidence | Release-blocking |
|---|---|---|---|---|
| — | — | No findings | See below | N/A |

**Live HTTP evidence (port-forward to `svc/maops-app`, direct — bypassing the gateway, exercising the app's own boundary):**

- No `X-MAOPS-Internal-Token` header → `HTTP 403`, body `{"error": "forbidden"}`.
- Wrong token value (`wrong-token-value`) → `HTTP 403`, same body.
- Malformed header with an unexpected `Bearer` scheme prefix (app does not use Bearer scheme; the raw header value is compared, so a `Bearer <x>` value legitimately fails as "wrong token") → `HTTP 403`, same body.
- Empty/absent header value → `HTTP 403`, same body.
- Correct token (read from the live Secret in-memory during this review, never printed) → `HTTP 200` with the expected `/internal/info` JSON shape.
- No case produced 401, 500, or 200 for an invalid/missing credential — exactly 403 in every negative case, matching the requirement precisely.

**Code-level evidence:**

- `_token_is_valid()` (`app/server.py:65-68`) uses `hmac.compare_digest(provided.encode("utf-8"), INTERNAL_TOKEN)` — a real constant-time comparison of the correct two byte strings (attacker-supplied vs. the mounted secret), not a plain `==`, and both arguments are homogeneous `bytes`, so there's no type-confusion or purpose-mismatch in the comparison.
- Short-circuit `if not provided or INTERNAL_TOKEN is None: return False` (`app/server.py:66`) happens *before* `compare_digest` is reached only when one side is genuinely absent (empty header or unmounted Secret) — this is a length/None check, not a timing-sensitive secret comparison, so it does not reintroduce a timing side-channel on the actual token value.
- Secret file unexpectedly missing/unreadable: `load_internal_token()` (`app/server.py:39-48`) catches `OSError` broadly and returns `None` rather than raising, so `INTERNAL_TOKEN` becomes `None` at import time and every subsequent request is rejected via the `INTERNAL_TOKEN is None` branch in `_token_is_valid` — this fails safe (uniform 403), not open, and does not crash the process or leak a traceback. Unit-tested: `tests/test_app_auth.py::TokenValidationTests::test_no_secret_loaded_rejects_every_request` and `test_load_internal_token_missing_file_returns_none_not_raise`.
- Neither the provided (attacker) token nor the expected token is ever logged: `log_message()` (`app/server.py:119-122`) is overridden to print only the address, timestamp, and the request-line/status format string/args that `BaseHTTPRequestHandler` passes in for standard access logging — it never receives or touches header values. Confirmed live: `kubectl logs` on the app pods, tail 50+ lines across the review session, contains only `GET <path> HTTP/1.1 <status>` lines, no header content.
- Response body on 403 is a fixed literal `{"error": "forbidden"}` (`app/server.py:105`) — it never echoes "expected X got Y" or any part of either token, satisfying the anti-oracle requirement.

No findings in this category.

---

## GATEWAY SECRET USE — `gateway/server.py` `/backend`

| ID | Severity | Title | Severity detail | Release-blocking |
|---|---|---|---|---|
| DAY2-SEC-L1 | Low | Backend target is ConfigMap-driven at runtime, with only static-manifest validation as the guardrail | See below | NO |

**Evidence:**

- The gateway reads the token exclusively from the mounted file: `INTERNAL_TOKEN = load_internal_token()` (`gateway/server.py:60`), read once from `/var/run/secrets/maops/internal-token`; it is never hardcoded and never sourced from an environment variable.
- `_handle_backend()` (`gateway/server.py:144-161`) sends the token only in the `X-MAOPS-Internal-Token` header of the single outbound request to `_backend_request("/internal/info", ...)`, which itself always targets `http://{BACKEND_HOST}:{BACKEND_PORT}{path}` (`gateway/server.py:78-84`). It is not sent anywhere else in the code.
- **DAY2-SEC-L1 (Low, not release-blocking):** `BACKEND_HOST`/`BACKEND_PORT` are runtime environment variables sourced from `maops-gateway-config` (`k8s/base/gateway-configmap.yaml:17-18`) via `envFrom` — the *application code itself* applies no allowlist/validation to `BACKEND_HOST` before building the request URL (`gateway/server.py:32,78`). The only guardrail against this ConfigMap value being pointed at an arbitrary destination is the *static manifest validator*: `scripts/validate_manifests.py:412-427` asserts `BACKEND_HOST == "maops-app"` exactly, rejects IP literals (`_is_ip_literal`), and rejects Pod/ReplicaSet-shaped identities (`_looks_pod_like`), with real negative-path unit tests (`tests/test_validate_manifests.py::BackendHostWiringTests`) exercising a raw-IP override and a pod-like override and confirming both are caught. This is a meaningful control today (Day 2's ConfigMap is Kustomize-owned, not attacker-writable without repo/RBAC access that doesn't exist yet), but it is a *build-time* guard, not a runtime one — if `maops-gateway-config`'s `BACKEND_HOST` key were ever mutated live in the cluster (e.g., via `kubectl edit configmap`, which nothing currently prevents since RBAC is Day 5 scope), the gateway process would follow it without any in-process check, and would happily send the internal token to whatever host that value names. Given Day 2 has no RBAC yet and this is explicitly deferred, this is Low/non-blocking, but should not be allowed to slip past Day 5 without an accompanying in-process allowlist or hardcoding recommendation.
- Errors returned to the gateway's callers never expose headers or the token: `_handle_backend()`'s except clause (`gateway/server.py:154-156`) catches `urllib.error.URLError, TimeoutError, OSError, ValueError` and returns a fixed `{"error": "backend unavailable"}` at `HTTP 503` — no exception text, no headers, no token in the body. Confirmed by code inspection; live network-failure injection wasn't performed in this pass (no chaos/network-partition tooling was invoked), but `scripts/dependency_check.py` (reviewed) already exercises the "app outage -> gateway /backend" path and asserts `status == 503` with a comment explicitly noting "no traceback, no token" as the property under test.
- The gateway's non-error response to `/backend` only forwards specific whitelisted fields (`gateway_hostname`, `backend_service`, `backend_hostname`, `backend_environment` — `gateway/server.py:158-165`), not the app's raw response body, so even if the app's `/internal/info` response shape ever grew a sensitive field, the gateway would not blindly proxy it.

Live confirmation: `curl http://127.0.0.1:<pf>/backend` (via `svc/maops-gateway`) returned `HTTP 200` with exactly the four whitelisted fields and real data sourced from the live app pod (`backend_hostname: maops-app-5cd767b9bd-xkrr9`), proving the full gateway→app authenticated round trip works end-to-end against the real cluster.

---

## NON-DISCLOSURE VALIDATION — `scripts/secret_check.py`

No findings. This is a genuinely meaningful checker, not a rubber stamp:

- It proves five independent things end-to-end against the live cluster: (1) Secret shape validity via the shared `validate_secret_shape` from `secret_bootstrap.py`; (2) both pods' volume/mount wiring via live `kubectl get pod -o json`; (3) both processes can actually read the mounted file (via `kubectl exec ... python3.11 -c "len(f.read())"`, comparing only the *byte length*, never the content); (4) gateway-authenticated `/backend` succeeds and unauthenticated/wrong-token `/internal/info` calls are rejected with exactly 403; (5) neither pod's logs nor any git-tracked file contains the live token string.
- Its own output never contains the plaintext, base64, or hex token. `get_token()` (`scripts/secret_check.py:69-75`) decodes the token into memory purely to build request headers/compare against logs/files; every `record(...)` call across the file only ever prints booleans, byte lengths (`len(f.read())`), status codes, and fixed diagnostic strings — never the decoded value itself. This review independently re-ran the same repo-scan and length-comparison logic against the live cluster (see "runtime evidence" section) and confirms the same non-disclosure discipline holds.
- No tautological assertion-message bug: `check_repo_files_do_not_contain_token()` (`scripts/secret_check.py:181-197`) reports `offending: [...]` as a **list of file paths**, never the token value, if it were to fail — this is the correct pattern (the anti-pattern the task warns about, e.g. `assert token not in body, f"found {token} in body"`, is not present anywhere in this file or in `tests/test_secret_bootstrap.py`'s `NonDisclosureTests`, which instead compares `captured_token["value"] not in printed` without interpolating it into the assertion message).
- There is no `tests/test_secret_check.py` in the repository; `secret_check.py` is a live-cluster script (imports `cluster_check`, `http_checks`, `portforward`) that is not Docker/kind-free by design, consistent with the project's two-tier validation split described in `.claude/CLAUDE.md`. This is not a gap for this review's purposes — its logic was directly exercised and cross-checked live above.

---

## CONFIGMAP SECURITY

| ID | Severity | Title | Evidence | Release-blocking |
|---|---|---|---|---|
| — | — | No findings | See below | N/A |

- `k8s/base/app-configmap.yaml:13-17` contains only `APP_NAME`, `APP_ENVIRONMENT`, `APP_MESSAGE`, `APP_LOG_LEVEL` — no credential-shaped keys or values.
- `k8s/base/gateway-configmap.yaml:13-23` contains `BACKEND_HOST`, `BACKEND_PORT`, `BACKEND_TIMEOUT_SECONDS`, plus the same `APP_*` keys — again nothing credential-shaped.
- This is **not** accidental compliance: `scripts/validate_manifests.py:94-96`'s `_looks_secret()` helper flags any key whose lowercased name contains `secret`, `password`, `token`, `key`, or `credential`, and `run_checks()` applies it to both ConfigMaps' `data` (`scripts/validate_manifests.py:396-402`). This has a real negative-path test: `tests/test_validate_manifests.py::SecretWiringTests::test_token_like_key_added_to_configmap_fails` injects `INTERNAL_TOKEN: "should-not-be-here"` into the gateway ConfigMap fixture and asserts `gateway.configmap.no_secret_like_values` is in the failed-checks set — a genuine, exercised rejection path, not a tautology.

No findings in this category.

---

## POD SECURITY CONTINUITY — app AND gateway independently

Both Deployments were checked independently against the rendered manifest and live `kubectl get pod -o json` output; neither was assumed to have copied the other correctly.

| Control | app (manifest) | app (live) | gateway (manifest) | gateway (live) |
|---|---|---|---|---|
| `runAsNonRoot: true` | PASS | PASS (`runAsNonRoot: True`) | PASS | PASS |
| `runAsUser: 10001` | PASS | PASS (confirmed `os.getuid() == 10001`) | PASS | PASS (confirmed `os.getuid() == 10001`) |
| `runAsGroup: 10001` | PASS | PASS | PASS | PASS |
| `allowPrivilegeEscalation: false` | PASS | PASS | PASS | PASS |
| `readOnlyRootFilesystem: true` | PASS | PASS (confirmed: write attempt → `EROFS`) | PASS | PASS (manifest identical; live write-test only executed against app pod in this pass, but container securityContext is byte-identical in the rendered manifest and confirmed via `kubectl get pod -o json` above) |
| `capabilities.drop: [ALL]` | PASS | PASS (present in live pod spec JSON) | PASS | PASS |
| `seccompProfile.type: RuntimeDefault` | PASS | PASS | PASS | PASS |
| `automountServiceAccountToken: false` | PASS | PASS | PASS | PASS |
| `hostNetwork`/`hostPID`/`hostIPC` absent | PASS | PASS (`None` in live JSON) | PASS | PASS (byte-identical manifest; not re-queried live separately, no reason to expect drift given identical rendered YAML) |
| `hostPort` absent | PASS | — | PASS | — |
| `privileged` not true | PASS | — | PASS | — |
| `hostPath` volumes absent | PASS | — | PASS | — |

Both Deployments are independently correct and byte-for-byte consistent with each other on every security-relevant field (verified by direct diff of `k8s/base/app-deployment.yaml` and `k8s/base/gateway-deployment.yaml`, which differ only in name/image/labels/probe-comments/timeouts, never in `securityContext`).

No findings in this category.

---

## Container image checks

- Both Dockerfiles pin `FROM gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5` — digest-pinned, distroless (no package manager, confirmed by base image choice and by the fact neither Dockerfile runs `apt`/`pip`).
- `USER 10001:10001` in both Dockerfiles matches the pod/container `securityContext.runAsUser/runAsGroup: 10001` in both Deployments exactly — verified live as `uid 10001 gid 10001` in both running containers.
- `imagePullPolicy: IfNotPresent` on both Deployments is correct for images loaded into `kind` rather than pulled from a registry (`k8s/base/app-deployment.yaml:41`, `k8s/base/gateway-deployment.yaml:41`), and is itself under static regression coverage (`tests/test_validate_manifests.py::VersionDriftTests::test_image_pull_policy_wrong_fails`).

No findings in this category.

---

## SCOPE CHECKS

| ID | Severity | Title | Evidence | Release-blocking |
|---|---|---|---|---|
| — | — | No findings | See below | N/A |

- `kubectl kustomize k8s/base` was rendered live during this review and grepped for `kind:` lines: exactly `Namespace` (1), `ConfigMap` (2), `Deployment` (2), `Service` (2) — **no `kind: Secret`**, no ServiceAccount/Role/RoleBinding/ClusterRole/ClusterRoleBinding, no NetworkPolicy. This matches Day 2's authorized scope per `docs/roadmap.md` exactly.
- The live `maops-internal-auth` Secret exists only in-cluster, created out-of-band by `scripts/secret_bootstrap.py`, exactly as the task describes as expected/correct for Day 2 — this is not treated as a violation.
- `scripts/validate_manifests.py:49-60`'s `FORBIDDEN_KINDS` set enumerates `Secret, Ingress, PersistentVolumeClaim, StatefulSet, Role, RoleBinding, ClusterRole, ClusterRoleBinding, ServiceAccount, NetworkPolicy` and is checked against every rendered doc (`scripts/validate_manifests.py:538-544`), with a real regression test per forbidden kind in `tests/test_validate_manifests.py::ForbiddenResourceTests` (including specifically `test_secret_object_committed_fails`, which appends a realistic Secret-with-data fixture and asserts it is caught).
- No `ServiceAccount`/RBAC objects are present, consistent with `docs/roadmap.md`'s explicit statement that these are Day 5 scope; both Deployments use the implicit `default` ServiceAccount with `automountServiceAccountToken: false` (confirmed live — `serviceAccountName: default`), which is the correct Day 2 posture.
- No `NetworkPolicy` is present, also consistent with Day 5 scope per the roadmap.

No findings in this category.

---

## STATIC SECURITY VALIDATION — `scripts/validate_manifests.py` + tests

The 595-line `tests/test_validate_manifests.py` suite was read in full. It is genuine regression coverage, not tautological:

- Each negative test mutates exactly one field of a deep-copied, fully-valid baseline fixture (`_base_docs()`) and asserts the specific expected check name appears in the failed set — this is the correct pattern (mutate-and-assert-specific-failure), not "assert the validator returns what the validator returns."
- Confirmed present and exercised: committed Secret object detection (`test_secret_object_committed_fails`), Secret volume missing from gateway/app independently (`test_gateway_missing_secret_volume_fails`, `test_app_missing_secret_volume_fails`), wrong Secret name (`test_wrong_secret_name_fails`), wrong mount path (`test_wrong_mount_path_fails`), wrong key via items restriction (`test_wrong_key_via_items_restriction_fails`), mount marked writable (`test_secret_mount_not_read_only_fails`), every host-boundary relaxation independently (`hostNetwork`, `hostPID`, `hostIPC`, `hostPort`, `privileged`, `hostPath` — one test each), ServiceAccount-token automount enabled (`test_app_automount_service_account_token_true_fails`), and credential-like ConfigMap addition (`test_token_like_key_added_to_configmap_fails`).
- `BaselineTests::test_baseline_passes_every_check` additionally asserts `len(findings) >= 90`, guarding against someone quietly deleting checks while leaving the baseline green.
- No tautological tests were found: every negative test both mutates a real field and asserts a specific named check (not just "any failure exists").

No findings in this category.

---

## DAY 2 SECURITY SCOPE — authentication vs. network isolation

Per the task's explicit instruction, this is documented accurately and neither over- nor under-claimed:

**Application-token authentication is genuine and does discriminate correctly** (see APPLICATION AUTH above — verified live: exactly 403 for missing/wrong/malformed token, 200 only for the correct token, `hmac.compare_digest` used correctly).

**This is not equivalent to network-level isolation.** As of Day 2, there is no NetworkPolicy in the `maops-platform` namespace (confirmed above — zero rendered, zero live). Any other Pod scheduled into this cluster — including one with no legitimate relationship to this platform — can open a TCP connection to `maops-app`'s or `maops-gateway`'s ClusterIP/Service DNS name and reach the HTTP listener. What stops it from *acting* as the gateway is exclusively the application-level token check in `app/server.py`'s `/internal/info` handler; nothing at the Kubernetes network layer prevents the connection attempt itself, only the app-level `403` response prevents it from succeeding. `docs/roadmap.md`'s own Day 5 entry states this precisely ("this is where Day 2's deferred network-isolation gap ... actually gets closed"), so the project's own documentation does not overclaim isolation either. This review confirms that self-assessment is accurate and does not find any place in the working tree (docs, code comments, or scripts) that falsely asserts NetworkPolicy-equivalent protection exists today.

---

## Findings Summary

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 0 | — |
| Low | 1 | DAY2-SEC-L1 |
| Info | 9 | DAY2-SEC-I1 – DAY2-SEC-I9 |

**DAY2-SEC-L1** (Low, non-blocking): the gateway's backend target (`BACKEND_HOST`/`BACKEND_PORT`) is read from a ConfigMap at runtime with no in-process allowlist/hardcoding in `gateway/server.py`; today's only guard is the static manifest validator (`scripts/validate_manifests.py`), which is real and tested but is a build-time control, not a runtime one. Given RBAC does not yet exist to protect ConfigMap mutation (correctly deferred to Day 5), this is worth revisiting once RBAC lands, but does not block v0.2.0 given the current threat model (no untrusted actor can mutate the ConfigMap without repository or cluster-admin access that doesn't exist at this stage).

No Critical, High, or Medium findings were identified in this review across secret bootstrap, secret mount, application/gateway authentication, non-disclosure validation, ConfigMap security, pod security continuity (both workloads independently), image/build hygiene, or scope compliance.

---

## Final Verdict Questions

**1. Is the runtime Secret lifecycle credible for v0.2.0?**
Yes. Generation uses a CSPRNG (`secrets.token_urlsafe(32)`), the token is never placed on a process command line or printed/logged under any code path (including failure paths), temp files are created with `mkstemp` (race-safe) and `0600` permissions and are removed in a `finally` block on both success and failure, an existing valid Secret is preserved rather than silently rotated, and a malformed existing Secret fails closed rather than being silently accepted or replaced. This was confirmed both by code review and by unit tests that specifically assert non-disclosure and idempotency, and cross-checked against the live cluster's actual Secret metadata.

**2. Does Secret evidence avoid exposing the value?**
Yes. `scripts/secret_bootstrap.py` and `scripts/secret_check.py` both hold the decoded token only in memory for comparison purposes and never print/log/interpolate it; their own test suites assert this property directly without falling into the "print token in assertion failure message" anti-pattern. This review's own live verification (Secret metadata inspection, `kubectl exec` length checks, log/repo scans, HTTP auth probing) was performed without printing or writing the plaintext, base64, or hex token value anywhere, including in this document.

**3. Is the app-level internal authentication genuine and discriminating?**
Yes. Live testing against the running cluster confirmed exactly HTTP 403 for missing, wrong, and malformed-header tokens, and exactly HTTP 200 for the correct token, with `hmac.compare_digest` used correctly on the actual byte values being compared. Failure responses are a fixed, non-disclosing literal; a missing/unreadable Secret file degrades to "reject everyone" (fail safe), not a crash or fail-open condition; and no code path logs either the attacker-supplied or expected token value.

**4. Does documentation avoid falsely claiming NetworkPolicy-level isolation?**
Yes. `docs/roadmap.md` itself explicitly and correctly states that Day 2 leaves a network-isolation gap that is deferred to Day 5, and no code comment, script docstring, or other working-tree text found during this review claims network-layer isolation exists today. This review's own DAY 2 SECURITY SCOPE section states the same boundary precisely: app-level auth prevents *unauthorized use* of `/internal/info`, but does not prevent *any Pod in the cluster from attempting the connection* — no NetworkPolicy exists yet to restrict that.

---

## Final Verdict: **APPROVE**

Zero Critical/High/Medium findings. One Low, non-blocking finding (DAY2-SEC-L1) is a forward-looking observation appropriately scoped to be revisited alongside Day 5's RBAC/NetworkPolicy work, not a defect in what Day 2 claims to deliver. Secret bootstrap, secret mounting, application-level authentication, non-disclosure validation, ConfigMap hygiene, and pod security baseline continuity across both workloads were independently verified against both the rendered manifest and live runtime state (including `kubectl exec`-based UID/GID and file-mode proof, and live HTTP auth probing) and found to be correct, consistent, and genuinely tested rather than superficially compliant.

PROJECT 4 DAY 2 KUBERNETES SECURITY REVIEW COMPLETE

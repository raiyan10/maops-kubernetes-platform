# Day 4 / v0.4.0 — Independent Kubernetes Security Review (Second Pass)

**Role:** `kubernetes-security-reviewer` (independent, adversarial review — not the implementer;
fresh subagent context with no memory of any prior conversation, including the Day 4 architecture
review, whose findings/verdict were deliberately not read).

**Scope:** Project 4 (`maops-kubernetes-platform`), Day 4 / v0.4.0 — StatefulSet/PVC-backed
persistence for the new `maops-state` workload, the `gateway -> app -> state` authenticated call
chain, the second runtime Secret (`maops-state-auth`), and the storage-bootstrap privilege work
that hardens `local-path-provisioner`'s directory-creation permissions.

**Candidate/evidence references:**
- Repository: `~/DevOps-Portfolio/maops-kubernetes-platform`, branch `feature/day-4-stateful-persistence`.
- HEAD verified: `aa2049876c7be2b959acb6e2a1d20f979ee440bc` (re-verified via `git rev-parse HEAD`, matches).
- `v0.3.0` tag object `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` peels to commit
  `9fc7fe9f25d729d76317de85b5722e84271234f0` (re-verified).
- Original candidate directory:
  `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-final-candidate-20260908T112009Z-ckQ1uj/`.
  `day4-check.log` sha256 `6e0fc45eb36226d004b110cfe775ace4d77eb7aebb4b2c110840e0e59e3a9cf0` — re-verified,
  matches. `candidate-before.sha256`/`candidate-after.sha256` both sha256
  `3e2daca59d40ba1a1d5fe4a4a8a3153d2029e5252fe96c58190e4493e42b6d90` — re-verified, matches, and the
  two files are byte-identical (`diff` empty) confirming no drift between before/after capture.
  `candidate-before.sha256` contains exactly 122 lines — re-verified via `wc -l`.
- This review's own evidence directory:
  `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-security-review-20260910T033729Z/`
  (`00-integrity-checks.txt`, `01-verify-122-entries.txt`, `02-unit-tests.txt`, `03-manifest-check.txt`,
  `04-live-cluster-evidence.txt`, `verify_122.py`).
- Kubeconfig: `~/.kube/maops-k8s-day4.config`, context `kind-maops-k8s-day4`, namespace `maops-platform`
  — a live cluster **was** available and used for runtime verification throughout.

**Method:** Direct, independent reading of `k8s/base/*.yaml` (all 14 files, including the new
`state-*` manifests), `app/server.py`, `gateway/server.py`, `state/server.py`, all three Dockerfiles,
`scripts/secret_bootstrap.py`, `scripts/secret_check.py`, `scripts/storage_bootstrap.py`,
`scripts/storage_hardening_check.py`, `scripts/persistence_check.py`, `scripts/retention_check.py`,
`scripts/state_check.py`, `scripts/kube.py`, `scripts/context_check.py`, `scripts/http_checks.py`,
`scripts/portforward.py`, `Makefile`, `docs/architecture.md`, `docs/roadmap.md`, this project's own
`kubernetes-security-reviewer` agent definition and `workload-security-validation`/
`manifest-validation` skills, and the Day 1–3 security reviews (for carried-forward debt). Static
checks (`python3 -m unittest discover -s tests`, `python3 scripts/manifest_check.py k8s/base`) were
re-run directly against the working tree. Live cluster evidence (`kubectl get pod -o json`,
`kubectl exec`, `docker exec <node> uptime`) was captured read-only against `kind-maops-k8s-day4`.
No mutation was performed: no Secret was rotated, no Pod/PVC was deleted or scaled, no image was
rebuilt/reloaded, no cluster was created/recreated, and no manifest/script/doc was edited.
`docs/engineering-reviews/day-04-kubernetes-architecture-review.md` was hashed only
(`e6d9bcf1001e7c1a855d16b9644e27bbbdc1a855d05d9daf291e5aa98f1c7eb5`), never read, per the task's
independence requirement.

---

## 1. Workload isolation — all three workloads (gateway/app/state), manifest + live

| Control | gateway | app | state | Verdict |
|---|---|---|---|---|
| `runAsNonRoot: true` (pod) | yes | yes | yes | PASS |
| `runAsUser: 10001` | yes | yes | yes | PASS |
| `runAsGroup: 10001` | yes | yes | yes | PASS |
| `fsGroup: 10001` | yes | yes | yes (+`fsGroupChangePolicy: OnRootMismatch`) | PASS |
| `seccompProfile.type: RuntimeDefault` (pod) | yes | yes | yes | PASS |
| `allowPrivilegeEscalation: false` (container) | yes | yes | yes | PASS |
| `readOnlyRootFilesystem: true` (container) | yes | yes | yes | PASS |
| `capabilities.drop: [ALL]` (container) | yes | yes | yes | PASS |
| `automountServiceAccountToken: false` (pod) | yes | yes | yes | PASS |
| `hostNetwork`/`hostPID`/`hostIPC`/`hostPath`/`hostPort`/`privileged` | absent | absent | absent | PASS |
| Resources bounded (`50m/32Mi` request, `250m/128Mi` limit) | yes | yes | yes | PASS |
| Required node affinity excludes control-plane | yes | yes | yes | PASS |
| `topologySpreadConstraints` (scoped to own component only) | yes | yes | N/A (single replica — correctly absent) | PASS |
| `PodDisruptionBudget` (`minAvailable: 2`) | yes | yes | N/A (single replica — correctly absent) | PASS |

**Live confirmation (not just manifest-inferred), against `kind-maops-k8s-day4`:**
- `maops-app-866c45c884-9hpkw` live `containerStatuses[0].user.linux` = `{uid: 10001, gid: 10001,
  supplementalGroups: [10001]}`; live pod/container `securityContext` byte-identical to the rendered
  manifest.
- `maops-state-0` live `user.linux` = same `{uid:10001, gid:10001, supplementalGroups:[10001]}`; live
  `readOnlyRootFilesystem`/`allowPrivilegeEscalation`/`capabilities.drop`/`seccompProfile`/
  `automountServiceAccountToken` all confirmed identical to the rendered manifest.
- **Read-only-root-filesystem proof, live, not declared:** `kubectl exec maops-state-0 -- ... open('/app/testwrite','w')`
  → `OSError: [Errno 30] Read-only file system` (captured in evidence file `04-live-cluster-evidence.txt`).
  `state/server.py` was read in full: every file write goes through `_persist_record()`, which only
  ever targets `STATE_FILE_PATH`'s directory (`/data`, the PVC mount) — there is no code path that
  writes anywhere under the read-only root, so this control is load-bearing, not aspirational.
- **Live `/data`/`state.json` ownership (metadata only, no content read/printed):** `state.json` is
  `mode=0o600 uid=10001 gid=10001 size=44`; the `/data` directory itself is `mode=0o2770 uid=0
  gid=10001` — matching `docs/architecture.md`'s claimed storage-bootstrap result exactly (root-owned,
  setgid, group `10001`), and distinct from `fsGroup` alone: the directory's owner is root (not the
  container's own UID), so it is genuinely the directory's group-10001/setgid mode — not `fsGroup`
  unilaterally, and not a leftover `0777` — that lets UID 10001 write here. This corroborates
  `scripts/storage_hardening_check.py`'s own positive/negative-probe design (§5 below) with an
  independent live read against the application's real claim, not the scratch claim.
- **gateway does NOT mount `state-auth` (live):** `maops-gateway-5cc89fdd54-2w9t7`'s live
  `spec.volumes` = `['internal-auth']` only — confirmed directly, not inferred from the manifest text
  alone.
- **No project-owned RBAC/ServiceAccount/NetworkPolicy exists live** in `maops-platform`: `kubectl -n
  maops-platform get role,rolebinding,serviceaccount,networkpolicy` returns only the implicit
  `serviceaccount/default` — correct for Day 4 (Day 5 scope per `docs/roadmap.md`).

**Observation, not a security finding (restart counts):** all seven live Pods show 14–18 container
restarts with `lastState.terminated` clustered within the same ~6-minute window
(`~2026-09-10T02:56Z`–`03:02Z`, exit codes 255/`Unknown` and 137/`Error`), and all three kind nodes'
`uptime` independently show only ~39 minutes since boot at the time of this review. This is
consistent with a host-level Docker/WSL2 restart (matching the task brief's own note that current
evidence is "pre-Docker-incident" and this run is not a fresh post-recovery run) simultaneously
killing every container on every node, not an application crash-loop or a security-relevant fault —
corroborated by the fact PVC/PV state, both Secrets, and the persisted `state.json` all survived this
event intact and Bound. **Verification limit:** this review did not have the tooling/scope to
independently confirm the Docker/WSL incident's root cause beyond this correlation; it is reported
as inference, not a confirmed root cause.

**Finding: none** in this section — Day 3's hardened baseline (`runAsNonRoot`, UID/GID 10001,
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`,
`seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`) is intact and independently
extended correctly to the new `maops-state` StatefulSet, both by manifest and by live evidence.

---

## 2. Credential boundaries — gateway → app → state

**Two separate Secrets, confirmed live:** `maops-internal-auth` (key `internal-token`, unchanged
since Day 2) and `maops-state-auth` (key `state-token`, new in Day 4) both exist as distinct `Opaque`
Secrets in `maops-platform` (`kubectl get secrets` — metadata only captured, no values). `gateway`
mounts only `internal-auth`; `app` mounts both; `state` mounts only `state-auth` — confirmed against
both the rendered manifest (`k8s/base/gateway-deployment.yaml`, `k8s/base/app-deployment.yaml`,
`k8s/base/state-statefulset.yaml`) and live Pod volume lists. `scripts/secret_check.py` independently
asserts this exact shape live, including a dedicated negative check
(`check_gateway_never_gets_state_token`) that gateway's live Pod `spec.volumes` contains no
`state-auth` entry.

**Generation/idempotency/fail-closed, code-reviewed (`scripts/secret_bootstrap.py`):** the new
`maops-state-auth` path (`get_existing_state_secret`, `create_state_secret`, `_bootstrap_state`) is a
faithful parallel of the well-established, previously-reviewed `maops-internal-auth` path (Day 2/3
security reviews both PASSed this pattern): CSPRNG generation (`secrets.token_urlsafe(32)`), the
token is written only to a `tempfile.mkstemp()` `0600` file and passed to `kubectl create secret
--from-file` (never a command-line argument, never printed/logged), the temp file is removed in a
`finally`-guarded block regardless of outcome, an existing valid Secret is preserved (never rotated)
via a short-circuit before `create_state_secret()` is ever called, and a malformed existing Secret
(missing key, non-base64, empty decoded value) fails closed via the shared `validate_secret_shape()`
helper. `main("all")` (what `make secret-bootstrap` now runs) bootstraps both Secrets and returns
non-zero if either fails. `kube.verify_context()` gates every invocation against the exact
`kind-maops-k8s-day4` cluster identity before any Secret read/write.

**Constant-time comparison, both new and existing paths:** `state/server.py::_token_is_valid()` uses
`hmac.compare_digest()` on two homogeneous `bytes` values, mirroring `app/server.py`'s existing
`_token_is_valid()`. Both short-circuit on `provided is falsy or TOKEN is None` before reaching
`compare_digest` — a length/None check, not a timing-sensitive comparison of the actual secret value,
so no timing side-channel is reintroduced.

**Missing/malformed credential handling:** `load_state_token()` in both `app/server.py` and
`state/server.py` catches `OSError` broadly and returns `None` (never raises), so an unmounted/
unreadable state-token Secret degrades every `/state`, `/internal/state` and state-dependent
`/readyz` call to a uniform fail-closed response (`403` for the auth check itself, `503` for
`STATE_TOKEN is None` in the allowlist-gated call paths) — never a crash, never a fail-open.

**Logging/error-leakage:** both `state/server.py::log_message()` and `app/server.py`'s equivalent log
only the request line/status (`BaseHTTPRequestHandler`'s own args), never header values — the state
token, which only ever travels as the `X-MAOPS-State-Token` header, cannot reach stdout via the
access log. `/readyz`, `/state`, and `/internal/state` error bodies are all fixed, non-echoing
literals (`{"error": "forbidden"}`, `{"error": "state unavailable"}`, etc.) — none of them ever
include the token, the configured host, or a stack trace.

**Live non-disclosure proof (`scripts/secret_check.py`, re-read in full, not re-executed against the
live cluster in this pass — see verification limits below):** the script independently proves, for
BOTH Secrets: shape validity, read-only mount at the expected path for the correct components only,
that the mounted file is actually readable (via byte-length comparison, never content), that a
direct unauthenticated/wrong-token call to `state`'s own Service is rejected with exactly `403` and
the response body never contains the substring `"token"`, that a correct-token call succeeds with
`200` and the same non-disclosure check, and that neither workload's `--tail=2000` logs nor any
`git ls-files`-tracked file contains the literal decoded state token.

**Client-facing authorization model (as explicitly asked):** `gateway`'s public-facing `/state`
(`GET`/`PUT`) and `/backend` endpoints require **no credential from the calling client at all** — any
TCP client able to reach the gateway's HTTP listener can invoke them, and gateway then attaches its
*own* loaded `INTERNAL_TOKEN` to the outbound call to `app`. This is not new to Day 4 — it is the
same trust model `/backend` has had since Day 2, and this project has never claimed end-user
authentication exists anywhere in the stack (confirmed again this pass: no code comment, doc, or
script text anywhere claims client-facing auth for `/state`/`/backend`). What **is** new in Day 4 is
that this same open-to-any-reachable-client model now includes a **write** path (`PUT /state`) for
the first time — a client that can reach the gateway (currently: the operator via a bounded local
`kubectl port-forward`, or, in principle, any other Pod in the cluster, since no NetworkPolicy exists
yet) can overwrite the persisted state record without presenting any credential of its own, and the
gateway will transparently authenticate that write to `app`/`state` using its own internal
credential. Neither `gateway/server.py` nor `app/server.py` ever forwards an inbound client header as
the outbound internal-auth/state-token value (both build the header from their own loaded token
variable, confirmed by direct code reading), so a client cannot smuggle an arbitrary token value
through — but a client also does not need to, since no client-side credential is required for the
write to be relayed in the first place. See DAY4-SEC-L1 below.

**Finding: DAY4-SEC-L1** (Low — see Findings section). Everything else in this section: **no
findings.**

---

## 3. Credential destination control

`app/server.py`'s outbound call to `state` (`_state_request()`) uses `http.client.HTTPConnection`
directly, not `urllib.request` — `http.client` never follows redirects and never consults proxy
environment variables (`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`), so there is no redirect/proxy code
path through which the state token could be diverted to an unapproved host, mirroring the existing
`DAY2-SEC-L1`-documented pattern for gateway's own `_backend_request()`. `STATE_TARGET_VALID` is
computed once at import time (`_is_allowed_state_target(STATE_HOST, STATE_PORT)` against the
hardcoded `ALLOWED_STATE_HOST = "maops-state"` / `ALLOWED_STATE_PORT = 8080`), and every call site
that would attach the state token (`_handle_readyz`, `_handle_internal_state_get`,
`_handle_internal_state_put`) checks it and fails closed (`503`, `_state_request()` never invoked)
before any token is attached — an attacker who could mutate `maops-app-config`'s `STATE_HOST` live
(no RBAC exists yet to prevent this, same as `DAY2-SEC-L1`'s gateway-side gap) still cannot cause the
state token to be sent anywhere but `maops-state:8080`, because the in-process allowlist check
happens before the token is ever read into a request. The `X-MAOPS-State-Token` header is always
built from `app`'s own loaded `STATE_TOKEN` — never copied from an inbound request header — so a
client cannot smuggle a value through this path either (same property verified for gateway's
internal-token forwarding in §2). This review did not modify live configuration to test exfiltration,
per the task's explicit constraint; the above is a static code-path proof, not a live redirect
experiment.

**Finding: none** — this is a faithful, correctly-scoped extension of the already-reviewed
`DAY2-SEC-L1` pattern to the new state hop, with `http.client`'s stronger no-redirect/no-proxy
guarantee as an additional, deliberate improvement over `urllib.request`.

---

## 4. Persistent data security

Reviewed `state/server.py`'s full read/write path and independently confirmed live:

- **Ownership/modes (live, metadata only):** `/data` directory `mode=0o2770 uid=0 gid=10001`;
  `/data/state.json` `mode=0o600 uid=10001 gid=10001`. The directory's `root:10001`/`2770`
  ownership is the storage-bootstrap's doing (§5); `state.json`'s own `10001:10001`/`0600` is the
  *application's* doing (`_persist_record()`'s `os.chmod(tmp_path, 0o600)`, then `os.replace()`
  preserves the temp file's mode) — these are two independently-attributable ownership sources,
  correctly distinguished rather than conflated.
- **Atomic replace + fsync ordering:** `_persist_record()` writes to a `tempfile.mkstemp(dir=_STATE_DIR)`
  temp file in the *same* directory as the target (guaranteeing `os.replace()` is a same-filesystem
  atomic rename, not a cross-filesystem copy), `flush()`+`os.fsync()` the temp file's contents,
  `os.replace()`, then separately `os.fsync()`s the *parent directory's* file descriptor — matching
  the documented durable-write recipe exactly. A failure at the parent-directory fsync step after a
  successful `os.replace()` is reported as `FAILED_UNCERTAIN`, distinct from `FAILED_CLEAN` (nothing
  persisted) — never claimed as a clean success or a clean failure, which is the correct, honest
  three-way outcome taxonomy for this specific failure mode.
- **Symlink/path handling:** `STATE_FILE_PATH` is a fixed ConfigMap-derived path
  (`/data/state.json`), never derived from request input — there is no code path where a client-
  controlled value influences the temp-file or target path, so path-traversal/symlink-race concerns
  that would apply to a request-driven filename do not apply here.
- **Concurrency:** a single process-wide `threading.Lock()` serializes every read-modify-write;
  documented and code-verified as last-writer-wins, never an interleaved/torn write — appropriate
  given `maops-state` is fixed at exactly 1 replica (never horizontally scaled), so this is a real,
  sufficient concurrency control for the stated scope, not merely aspirational.
- **Input/body limits:** `PUT /state` rejects on `Content-Length` alone (`STATE_MAX_BODY_BYTES`,
  default 4096) *before* reading the body into memory — same pattern independently present in
  `app/server.py`'s `/internal/state` proxy and `gateway/server.py`'s `/state` proxy (each with their
  own independent `STATE_MAX_BODY_BYTES`/`STATE_PROXY_MAX_BODY_BYTES` bound) — a malicious/oversized
  body is rejected at the earliest hop, not merely at the final one.
- **Malformed JSON / schema validation:** `_validate_record()` enforces the exact schema
  (`{"value": <string|null>}`, no extra keys) at every write; a syntactically invalid body is `400`,
  a well-formed-but-wrong-shape body is `422`. A pre-existing on-disk file that is unreadable or
  fails validation is **never auto-repaired** — `_init_error` is set once at startup and surfaced via
  `/readyz` (`503`) indefinitely until an operator intervenes; `GET`/`PUT /state` are not gated on
  `_init_error` directly, but `/readyz` failing means `app`'s own dependency-aware readiness (which
  calls the real authenticated `GET /state`) will also correctly report `503` in that condition, so
  the failure is visible end-to-end, not silently swallowed.
- **Storage-failure handling:** `_read_record()` always re-reads from disk on every call — no
  in-memory cache exists that could paper over a genuine storage failure — and a failed read is
  reported as `500` with a `detail` string built only from the caught exception's own message (an
  `OSError`/`JSONDecodeError` string), never from client input, so this is not an injectable-content
  reflection path.

**Scope-correct framing (per task instruction):** shared UID/GID `10001` across all three workloads
is **not** treated here as tenant isolation — this is a single-tenant, isolated Day 4 kind cluster by
design (confirmed against `docs/architecture.md`'s own explicit "Scope note" disclaiming exactly this
overreach), and this review does not attribute any multi-tenant guarantee to it. The `maops-day4-
storage-preflight` namespace's own separate PV (`pvc-203916e5-...`, confirmed still present and
untouched via `kubectl get pv`) is a distinct, pre-existing, historical resource — its presence does
not expose or share access with `maops-state`'s own claim (`data-maops-state-0`, distinct PVC/PV
UIDs, confirmed live), and no code path in this review's scope suggests otherwise.

**Verification limit:** this pass did not itself re-run `scripts/persistence_check.py` or
`scripts/retention_check.py` (both are mutating — Pod deletion, StatefulSet scale-to-0 — and the
task explicitly restricts mutating tests to being *proposed*, not silently run). Their *positive and
negative permission probes* were verified by reading the current source in full and cross-checking
the resulting live `/data` metadata (above) against what the code claims to produce — this is
code-level + static-live-state verification, not a fresh re-execution of the experiment, and is
reported as such rather than claimed as "re-proven live" in this pass.

**Finding: none** in this section.

---

## 5. Storage bootstrap privilege

`scripts/storage_bootstrap.py` was read in full:

- **Exact scope:** every `docker exec <node> ...` call operates only against the fixed constant
  `PROVISIONING_ROOT = "/var/local-path-provisioner"` (never a request- or Secret-derived path), and
  the only Kubernetes object touched is the `local-path-config` ConfigMap's single `setup` field
  (verified byte-for-byte against `EXPECTED_ORIGINAL_SETUP` before any patch, refusing to touch an
  unfamiliar/hand-edited value) — `config.json`, `helperPod.yaml`, and `teardown` are never read or
  written by this script.
- **No shell/argument injection:** every `docker exec`/`kubectl` invocation is built as an explicit
  argv list (`subprocess.run([...])`, no `shell=True` anywhere in this file), and node names are
  sourced exclusively from `kubectl get nodes -o json` (trusted, structured API data from the
  already-`verify_context()`-gated cluster) — never from external/attacker-influenced input.
- **Symlink/precondition checks:** the patched `setup` script itself (the string this tool writes
  into the ConfigMap) independently re-validates `$VOL_DIR` is under `PROVISIONING_ROOT`, refuses a
  symlink, and refuses a pre-existing path before ever calling `mkdir` — defense-in-depth at the
  point the *provisioner's own helper Pod* executes this script, not just at bootstrap time.
- **chown/chmod breadth:** the only `chown`/`chmod` calls this script issues directly (via `docker
  exec`, since the provisioner's own minimal helper image has no such binaries) target
  `PROVISIONING_ROOT` itself, exactly — never a wildcard, never a PV's individual backing directory
  (those are created with the correct mode in one atomic `mkdir -m 2770` inside the setgid parent,
  with no separate chown/chmod step at all, confirmed by the patched script's literal content).
- **Idempotency, partial failure, rollback:** `harden_provisioning_root_on_all_nodes()` refuses to
  touch a node where the root already exists with a non-root owner (unfamiliar state, never
  overwritten) and records each node's *exact* prior `(mode, gid)` before changing it, so
  `restore_provisioning_root_on_nodes()` can revert precisely the nodes this run actually changed —
  a node already correctly hardened before this run is left untouched by the revert path (verified
  by reading the `original_state_by_node` bookkeeping and the `if original is None` /
  already-hardened branches). A failed propagation-verification step (against a disposable scratch
  PVC, never `maops-state`'s own claim) triggers reverting both the ConfigMap patch and any
  node-level changes, with restoration failure reported as a distinct, prominent `RESTORATION
  FAILURE` line rather than folded silently into the overall pass/fail count.
- **Repeated execution / new-node behavior:** `main()` unconditionally calls
  `harden_provisioning_root_on_all_nodes()` over the *current* live node list on every run — so a
  node added to the cluster after this bootstrap first ran would still be hardened on the next
  invocation, before the (separately idempotent) ConfigMap-patch check even runs. This is a correct,
  intentional design for "no static node list," not an oversight.
- **Helper provenance:** the disposable propagation-verification probe Pod deliberately reuses the
  project's own already-built `maops-kubernetes-app:<VERSION>` image (command overridden to a short
  probe script) rather than the raw Distroless base digest directly, for the documented containerd/
  kind compatibility reason (`docs/architecture.md`) — this is a local-environment workaround, not a
  weakening of the probe Pod's own security posture (the probe Pod still runs UID/GID 10001,
  `fsGroup: 10001`, worker-only affinity, and the full container `securityContext` baseline, confirmed
  by reading `_pod_manifest`/the inline manifest in `verify_propagation()`).

**Finding: none** — this is a narrowly-scoped, injection-safe, idempotent, rollback-guaranteed
privilege-escalation-adjacent script (it does run privileged host-level `docker exec`/`chown`/`chmod`
by necessity, since RBAC/PodSecurityAdmission cannot express "mode 2770 on a hostPath the provisioner
manages"), and its scope is exactly what `docs/architecture.md`/`docs/roadmap.md` describe — RBAC and
NetworkPolicy correctly remain out of scope for Day 4 and are not what this script's exposure depends
on; this review does not treat their absence as this script's own defect.

---

## 6. Validation safety

Reviewed `scripts/persistence_check.py`, `scripts/retention_check.py`, `scripts/state_check.py`, and
`scripts/storage_hardening_check.py` for credential handling, bounded subprocesses, temporary-
resource ownership, restoration guarantees, and negative-path coverage:

- **Credential handling:** none of these four scripts ever read or reference `INTERNAL_SECRET_KEY`/
  `STATE_SECRET_KEY` directly — all HTTP calls they make go through the real, already-authenticated
  service chain (`gateway`'s own `/state`, which attaches its own token internally) rather than these
  scripts holding or forwarding a credential themselves. `scripts/secret_check.py` and
  `scripts/state_check.py` are the only scripts in this set that touch a token value directly, and
  both hold it strictly in memory for header-construction/comparison, matching the established Day
  2/3 non-disclosure discipline (re-read and re-confirmed, not just assumed carried-forward).
- **Bounded subprocesses:** every `kubectl`/`docker exec` call in all four scripts goes through
  `kube.run()` (bounded by `DEFAULT_TIMEOUT_SECONDS=30` or an explicit longer bound) or an explicit
  `timeout=` kwarg on a raw `subprocess.run()` call — no unbounded call was found. `persistence_check.py`
  contains a documented, previously-live-found fix (`POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS=90` plus
  `--wait=false`) for a real race between a Pod's `terminationGracePeriodSeconds` (30s) and the
  generic 30s subprocess timeout — a genuine defect the implementer found and fixed via live testing,
  not a theoretical concern.
- **Temporary-resource ownership/cleanup:** `storage_hardening_check.py` refuses to proceed if its
  scratch namespace already exists (never reuses/modifies an unfamiliar namespace) and deletes it in
  a `finally` block regardless of outcome; `persistence_check.py`/`retention_check.py` never create
  disposable Kubernetes objects at all (they operate on the application's own already-existing
  `maops-state-0`/PVC), so "temporary resource ownership" for those two is really "temporary Pod-
  identity bookkeeping," which is captured correctly (UID/PVC-UID/PV-UID snapshots before/after).
- **Restoration after errors/signals:** all four scripts wrap their experiment body in `try/finally`
  and unconditionally attempt restoration (`persistence_check.py` restores the pre-experiment record
  with bounded retries after waiting for the chain to report ready; `retention_check.py` always scales
  `maops-state` back to 1 if it was ever scaled down, independent of whether the outage-observation
  block itself raised) — restoration failures are recorded as a distinct `RESTORATION FAILURE`
  category and printed prominently to stderr, never silently folded into the main pass/fail count.
  `scripts/portforward.py` (shared by all of these) converts `SIGTERM` to a catchable exception only
  for the duration of an open port-forward, specifically so a CI-style timeout/kill does not skip the
  `finally: _terminate(proc)` cleanup and leak a background `kubectl port-forward` process — read in
  full and confirmed to restore the prior `SIGTERM` disposition immediately after, and to leave
  `SIGINT`/all other signals untouched.
- **Negative-path coverage:** `storage_hardening_check.py`'s negative probe (UID/GID 65532/65532, no
  `fsGroup`) asserting a specific `EACCES` (not a bare non-zero exit, not `ENOENT`) is a genuine,
  meaningful negative case — it is the one control in this whole feature set that could plausibly
  regress into "always green" silently, and it does not, since the probe pod's own script `sys.exit(3)`
  on any *other* errno, which `main()` treats as a failed probe. `state_check.py`'s
  `check_unauthenticated_state_rejected()` similarly asserts exactly `403` (not a bare exception) for
  a direct, unauthenticated `GET /state` against the state Pod itself.

**Finding: DAY4-SEC-M1** (Medium — see below): a distinct, narrower gap exists specifically in the
**Docker-free static** tier (`tests/test_secret_bootstrap.py`), not in any of the four live-cluster
scripts reviewed above.

---

## Findings

### DAY4-SEC-M1
- **Severity:** Medium
- **Title:** `maops-state-auth` bootstrap path has zero Docker-free unit-test coverage, unlike the identical `maops-internal-auth` path in the same file
- **Affected files:** `scripts/secret_bootstrap.py` (functions `get_existing_state_secret`,
  `create_state_secret`, `_bootstrap_state`, and `main()`'s `"state"`/`"all"` dispatch branches,
  added Day 4); `tests/test_secret_bootstrap.py` (unchanged since before Day 4 — confirmed via `git
  status`, this file is not among the 33 files Day 4 modified/added).
- **Evidence:** `tests/test_secret_bootstrap.py` has 9 test classes / 15 test methods, every one of
  which exercises only `secret_bootstrap.get_existing_secret`, `secret_bootstrap.create_secret`, and
  `secret_bootstrap.main()`'s default (`"internal"`) target — confirmed by `grep -n "state|State"
  tests/test_secret_bootstrap.py` returning zero matches. `scripts/secret_bootstrap.py` itself was
  read in full and confirmed to add, byte-for-byte, a second complete bootstrap code path
  (`get_existing_state_secret`, `create_state_secret`, `_bootstrap_state`) that duplicates the
  internal path's control flow against `STATE_SECRET`/`STATE_SECRET_KEY`, plus a new `main(target:
  str)` parameter with `"internal"`/`"state"`/`"all"` branches — none of which is covered by any
  unit test. `python3 -m unittest discover -s tests` was re-run and confirms `392 tests, OK` — the
  suite is green, but green because the new code path is simply untested, not because it is verified
  correct by the project's own static tier.
- **Impact:** Code review of the new functions (§2 above) finds them behaviorally correct today — they
  reuse the same underlying `validate_secret_shape()`/`_create_secret()` helpers the internal path
  already exercises, and this review independently confirmed the live `maops-state-auth` Secret is
  correctly shaped, non-rotated, and non-disclosed. This is therefore a **validation-integrity/
  regression-detection gap**, not a live exploitable vulnerability: a future refactor of
  `_bootstrap_state()`, `create_state_secret()`, or the `"all"` target's partial-failure combination
  logic (`return 0 if (rc_internal == 0 and rc_state == 0) else 1`) that silently reintroduced
  printing the token, skipped the malformed-Secret fail-closed check, or broke the partial-failure
  return code would pass `make test` and `python3 -m unittest discover -s tests` undetected — exactly
  the class of regression `.claude/CLAUDE.md`'s own "two validation tiers, keep them separate" ground
  rule and this project's Day 1 precedent (`DAY1-SEC-M1`, a structurally identical "coverage gap, not
  a live vulnerability" finding) both exist to catch. Given how much emphasis Day 2/3's security
  reviews placed on `secret_bootstrap.py`'s non-disclosure discipline being "genuinely tested rather
  than superficially compliant," a second, equally sensitive credential's bootstrap logic shipping
  with zero equivalent test coverage is a real regression in that established discipline.
- **Required remediation:** Add a `tests/test_secret_bootstrap.py` coverage set for the state-secret
  path mirroring the existing internal-secret classes at minimum: existing-valid-Secret-preserved,
  existing-malformed-Secret-fails-closed, missing-Secret-generates-and-verifies, generated-token-
  never-printed, and `main("all")`'s partial-failure combination (internal succeeds/state fails and
  vice versa, asserting exit code 1 in both cases and that neither path's `create_*` was called when
  it shouldn't have been).
- **Release-blocking:** Per this review's mandate ("Before PR, unresolved Critical, High and Medium
  findings must all be zero"), **yes, at Medium severity, this blocks PR** until remediated or
  explicitly re-adjudicated to a lower severity by whoever owns that decision. It is not a live
  exploit and does not block the Day 4 *manifest*/*runtime* posture, which is otherwise sound.

### DAY4-SEC-L1
- **Severity:** Low
- **Title:** `PUT /state` (new write capability) is reachable through gateway with no client-supplied credential, same open trust model as the existing `/backend` read path
- **Affected files:** `gateway/server.py` (`_handle_state_get`/`_handle_state_put`, lines ~220–261);
  `app/server.py` (`_handle_internal_state_get`/`_handle_internal_state_put`).
- **Evidence:** `do_PUT`/`do_GET` on `gateway/server.py`'s `/state` path check only
  `BACKEND_TARGET_VALID`/`INTERNAL_TOKEN is None` and a `Content-Length`/size bound — there is no
  check of any header or credential supplied by the calling client before gateway attaches its own
  `INTERNAL_TOKEN` and relays the request to `app`. This is architecturally identical to the existing,
  previously-reviewed `/backend` trust model (`DAY2-SEC-L1`), which this project has never claimed
  provides end-user authentication.
- **Impact:** Day 4 is the first stage where this open-to-any-reachable-client model includes a
  **write** capability (`PUT /state` can overwrite the persisted record) rather than only a read-only
  proxy. Today's actual blast radius is bounded: the only realistic reachability path is the
  operator's own bounded local `kubectl port-forward` (never exposed externally), and no
  NetworkPolicy exists yet by design (Day 5), so any other Pod in the cluster could in principle also
  reach it — but this is the same pre-existing Day 2/3 network-isolation gap this project's own
  documentation already discloses accurately (confirmed again this pass: no doc/code claims otherwise
  for `/state` specifically). This is not a new class of exposure so much as an existing, disclosed
  gap now covering a write path for the first time.
- **Required remediation:** None required for Day 4 given the documented, unchanged threat model;
  recommended that Day 5's NetworkPolicy work explicitly consider restricting which sources may reach
  `maops-gateway`'s `/state` write path (or, if actual multi-user/external exposure is ever
  contemplated beyond this portfolio's local-kind scope, adding client-facing authentication ahead of
  the gateway).
- **Release-blocking:** NO.

---

## Scope and debt

Consistent with the task's explicit framing: Day 4 proves persistence across Pod replacement and a
1→0→1 scale cycle; it does not claim state HA, hostile multi-tenancy, node-loss recovery, cluster-
loss recovery, or production CSI guarantees, and this review found no claim to the contrary anywhere
in `docs/architecture.md` or code comments. RBAC/NetworkPolicy remain correctly deferred to Day 5.

**Inherited open debt, preserved (not re-adjudicated by this review, per the task's explicit
instruction):** `DAY1-INT-I2` (interpreter-path coupling, unchanged), `DAY2-INT-I1` (EndpointSlice
dedup-by-addressType gap, unchanged — still correct today since the cluster remains single-stack
IPv4), `DAY3-SEC-I1` (informational defense-in-depth suggestion re: live `securityContext` spot-
checks post-scale/rollout), `DAY3-TEST-L2`, `DAY3-REL-L1` (both outside this review's scope —
test-suite/release-process debt, not re-read in depth this pass beyond confirming their titles still
exist in `docs/engineering-reviews/day-03-kubernetes-test-review.md`/`day-03-release-readiness-review.md`).
**`DAY1-REL-I1` remains CLOSED** — `scripts/version_check.py`/`VERSION` (`0.4.0`) cross-checking was
not itself re-broken by Day 4 (confirmed: `k8s/base`'s new `state-*` manifests all carry
`app.kubernetes.io/version: "0.4.0"` consistently, and `python3 scripts/manifest_check.py k8s/base`
passes 197/197 including this project's own drift checks).

**`DAY3-SEC-M1` (PDB/Eviction 429-classification gap, Medium, Day 3, "APPROVE WITH CONDITIONS")** is
unaffected by Day 4 — `maops-state` deliberately carries no PDB (single replica), and
`scripts/pdb_check.py` was not modified for Day 4 (not in the tracked-file diff). This review does
not re-adjudicate it, consistent with the prior review's own "not blocking Day 3's v0.3.0 tag, fix
before Day 7" disposition, but notes it remains genuinely open, not silently resolved by Day 4's
unrelated work.

---

## Verification limits

- `scripts/persistence_check.py` and `scripts/retention_check.py` (mutating: Pod deletion, StatefulSet
  scale-to-0) were **not re-executed** in this pass — per the task's explicit constraint that mutating
  tests must be proposed, not silently substituted or run. Their logic was verified by full source
  reading plus independent live-state cross-checks (current `/data`/`state.json` metadata, PVC/PV
  Bound status, Secret existence) that are consistent with what the scripts claim to produce, but this
  is not equivalent to a fresh live re-execution of the actual delete/scale experiment.
- `scripts/secret_check.py` and `scripts/state_check.py` were read in full and their logic assessed
  as sound, but were **not re-executed live** in this pass (doing so would create/rotate no state but
  does open several port-forwards and `kubectl exec` calls not strictly required given the equivalent
  read-only spot-checks this review performed directly). The live spot-checks in §1/§2/§4 above
  (`kubectl get pod -o json`, `kubectl exec` for UID/GID and rootfs-write-failure, `kubectl get
  secrets`/`pvc`/`pv`) independently corroborate the specific claims these scripts make, but do not
  constitute a full re-run of either script.
- The restart-count/host-Docker-incident correlation (§1) is this review's own inference from
  timestamp/uptime correlation, not a confirmed root-cause diagnosis — flagged explicitly as such.
- This review did not have access to the Day 4 architecture review's contents (by design) and cannot
  speak to whether any finding here overlaps with or duplicates a finding already raised there;
  overlap, if any, should be reconciled by whoever adjudicates both reports together.

---

## Totals and verdict

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 1 | DAY4-SEC-M1 |
| Low | 1 | DAY4-SEC-L1 |
| Informational | 0 | — |

**Security verdict: APPROVE WITH CONDITIONS.**

The Day 4 container/Pod security baseline (§1), the two-Secret credential-boundary design and its
non-disclosure discipline (§2), the state-hop destination-control allowlist (§3), the persisted-data
write/read safety design (§4), and the storage-bootstrap privilege scope (§5) are all sound,
correctly scoped to Day 4's stated persistence goal, and — for everything with a live cluster
available to check — independently confirmed with real runtime evidence rather than manifest
inference alone. No Critical or High finding exists. One Medium finding (`DAY4-SEC-M1`) is a
regression-test-coverage gap in the new Secret-bootstrap code path, not a live vulnerability, but per
this review's own mandate it must be resolved (or explicitly re-adjudicated) before this Day 4 work
proceeds to a pull request, since the stated bar is zero unresolved Medium findings pre-PR. One Low
finding (`DAY4-SEC-L1`) is a documentation-accuracy/forward-looking observation about an unchanged,
already-disclosed trust-model property, not release-blocking.

**Condition for PR:** resolve `DAY4-SEC-M1` (add unit-test coverage for the `maops-state-auth`
bootstrap path) or obtain an explicit, reasoned re-adjudication of its severity from whoever owns
that decision, before this branch proceeds past this security review.

---

PROJECT 4 DAY 4 KUBERNETES SECURITY REVIEW COMPLETE (SECOND INDEPENDENT PASS)

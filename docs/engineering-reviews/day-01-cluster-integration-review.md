# Project 4 / Day 1 - Cluster Integration Review

**Scope:** Independent runtime verification of the Day 1 Kubernetes foundation
against the live kind cluster `maops-k8s-day1` (context
`kind-maops-k8s-day1`), plus review of `Makefile`, `scripts/portforward.py`,
`scripts/reconcile_check.py`, `scripts/cluster_check.py`, `scripts/smoke.py`,
`scripts/kube.py`, and `scripts/http_checks.py`. This review exercises the
running cluster directly (`kubectl -o json`, real HTTP over port-forward,
live pod deletion) rather than re-deriving conclusions from manifests.

**Target version:** v0.1.0
**Branch reviewed:** `feature/day-1-kubernetes-foundation`
**Reviewer:** cluster-integration-engineer (independent pass)
**Date:** 2026-09-01

---

## Real cluster baseline evidence

- `kubectl config current-context` → `kind-maops-k8s-day1` (correct, no switch needed).
- `kind get clusters` → exactly one cluster: `maops-k8s-day1` (no ambiguity; the only other running docker containers belong to an unrelated `maops-docker-platform` project and were not touched).
- `kubectl get nodes`: `maops-k8s-day1-control-plane` — `STATUS Ready`, `VERSION v1.36.1`.
- `kubectl version` (server): `gitVersion: v1.36.1` — matches `kind/cluster.yaml`'s pinned `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5`.
- `kubectl get ns maops-platform` → `Active`.
- `kubectl get deployment maops-app -n maops-platform`: `READY 2/2`, `UP-TO-DATE 2`, `AVAILABLE 2`.
- `kubectl get pods -n maops-platform`: both pods `1/1 Running`, `Ready=True` condition present on both.
- `kubectl get svc maops-app -n maops-platform`: `TYPE ClusterIP`.
- `kubectl get endpoints maops-app -n maops-platform`: 2 addresses, both with `targetRef` pointing at the two live pods.

All of the above was independently re-confirmed by running `make day1-check` end-to-end (see below): 31/31 unit tests, 30/30 static manifest checks, 15/15 real-cluster checks (`scripts/cluster_check.py`), 4/4 smoke checks, 8/8 reconciliation checks — all passed on the first run against the live cluster.

## ConfigMap / application evidence

Three independent sources compared, all consistent:

1. `kubectl get configmap maops-app-config -n maops-platform -o yaml` → `APP_MESSAGE: Hello from the MAOps Kubernetes Platform (Day 1)`.
2. `kubectl exec` into a live pod (`/usr/bin/python3.11 -c "...os.environ.get('APP_MESSAGE')..."`, the only usable entrypoint in the distroless, shell-less image) → returned the identical string, run by `scripts/cluster_check.py:check_configmap_consumption`.
3. Real HTTP `GET /config` via bounded port-forward → `{"APP_ENVIRONMENT": "day1-kubernetes-foundation", "APP_LOG_LEVEL": "info", "APP_MESSAGE": "Hello from the MAOps Kubernetes Platform (Day 1)", "APP_NAME": "maops-kubernetes-platform"}`.

All three match the ConfigMap's actual `data`. This is genuine live evidence, not a manifest re-read — the exec path proves envFrom wiring actually landed in the running process's environment, and the HTTP path proves the app read it correctly at runtime.

## Probes — live pod spec/status evidence

`kubectl get pod <pod> -o json` on a live pod showed:

- `startupProbe`: `httpGet /livez`, `periodSeconds=2`, `failureThreshold=15`, `timeoutSeconds=1`.
- `livenessProbe`: `httpGet /livez`, `periodSeconds=10`, `timeoutSeconds=2`, `failureThreshold=3`.
- `readinessProbe`: `httpGet /readyz`, `periodSeconds=5`, `timeoutSeconds=2`, `failureThreshold=3`.
- `containerStatuses[0]`: `ready: true`, `started: true`, `restartCount: 0`, running user `uid=10001 gid=10001`.

These are the live, in-cluster probe definitions actually being evaluated by kubelet (echoed back through the API, not the source manifest file), and they match the endpoints the app actually serves (`/livez` always 200; `/readyz` 503 until `READY` flips true after `STARTUP_DELAY_SECONDS`).

**Readiness-gates-endpoints proof (live):** A background watcher polled `kubectl get endpoints maops-app -o json` every ~1.5s while `make controller-check` ran. Real observed sequence:

```
13:12:22.98  endpoints=2 [xlqgg, n8rzj]
13:12:24.68  endpoints=1 [xlqgg]              <- n8rzj deleted, endpoint immediately removed
... (unchanged at 1 for ~10s while replacement pod starts and STARTUP_DELAY_SECONDS elapses)
13:12:34.88  endpoints=2 [xlqgg, gldbl]        <- new pod passed /readyz, added back
```

This directly proves readiness controls Service endpoint eligibility on this cluster: the replacement pod was `Running` well before it appeared in `endpoints`, and only joined once `/readyz` started returning 200.

## HTTP / port-forward mechanism

`scripts/portforward.py` reviewed and exercised live:

- Free port: binds to `("127.0.0.1", 0)` and reads back the OS-assigned port — safe, no fixed/reserved-port assumption.
- Local-only binding: `kubectl port-forward` forwards to `127.0.0.1:<port>`, confirmed no `0.0.0.0` binding.
- Bounded startup wait: `_wait_connectable` polls with a `ready_timeout` (default 30s) and also checks `proc.poll()` so it fails fast if the port-forward process exits early rather than looping the full timeout.
- Cleanup on success: `finally: _terminate(proc)` in the context manager runs on the happy path — verified via `ps`/`pgrep` after both `make smoke` and `make controller-check`: zero leftover `kubectl ... port-forward` processes each time.
- Cleanup on error: same `finally` covers `TimeoutError`/`RuntimeError` raised inside the `with` block.
- Process-group termination: `start_new_session=True` at Popen time plus `os.killpg(pgid, SIGTERM)` with a `SIGKILL` fallback after a 5s wait — correctly kills the `kubectl` process even if it spawns children.
- **SIGTERM to the wrapping script gap (see DAY1-INT-M1):** live-tested by starting a minimal script that opens `port_forward(...)` and sleeps, then sending `SIGTERM` to the *Python* process (not Ctrl-C). The `kubectl port-forward` child was left running after the parent process died, because Python's default SIGTERM disposition terminates the process immediately without unwinding `try/finally`. Reproduced and independently confirmed via `pgrep`; the leaked process (PID 44197 in this session) was manually cleaned up as part of this review.

`make smoke` run live: 4/4 endpoint checks passed (`/`, `/livez`, `/readyz`, `/config`, all HTTP 200 with valid JSON bodies). `ps aux | grep port-forward` confirmed empty immediately after.

## Controller reconciliation — live proof

`scripts/reconcile_check.py` reviewed and executed twice live (once standalone via `make controller-check`, once again inside the full `make day1-check` run). Both runs:

- Recorded exactly 2 pod UIDs before deletion.
- Deleted exactly one pod by name (`kubectl delete pod <name> --wait=false`) — no replacement created manually.
- Waited (bounded, 120s timeout, 3s poll interval) for `readyReplicas == 2` and both pods `Ready=True`.
- Compared UID sets: exactly 1 survivor UID (matching the untouched pod) and exactly 1 new UID (the reconciled replacement) each run, e.g. run 1: survivor `2c0241dd-...`, new `8b51ab3b-...` (deleted pod was `d864a4a8-...`); run 2 (inside day1-check): survivor `2c0241dd-...` (unchanged again — same pod untouched across both runs), new `f5fcd625-...`.
- Re-ran the HTTP smoke path (`/`, `/livez`, `/readyz`, `/config`) after reconciliation — all 4/4 passed with HTTP 200.
- Only the `maops-app` label-selected pods were touched; no other namespace/resource was affected.

`8/8` reconciliation checks passed both times. This satisfies the required proof exactly: two original UIDs recorded, one pod deleted, no manual replacement, Deployment back to 2/2 Ready, one original UID surviving, one genuinely new UID, and working HTTP after.

## Cluster lifecycle safety (Makefile)

- `cluster-create`: checks `kind get clusters | grep -qx "$(CLUSTER_NAME)"` before creating — idempotent, scoped to `maops-k8s-day1` only, uses `kind/cluster.yaml` (pinned node image, not `latest`).
- `cluster-delete`: `kind delete cluster --name $(CLUSTER_NAME)` — scoped to the named cluster only. No `docker system prune`, no bare `kind delete cluster` without `--name`. **Not executed** as part of this review, per instructions.
- `image-load`: `kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)` — scoped to the named cluster, does not touch other kind clusters or docker resources.
- `deploy`: `kubectl --context $(KCONTEXT) apply -k $(BASE)` — explicit `--context`, cannot silently apply against the wrong cluster (there is no unqualified `kubectl apply` in the Makefile).
- `day1-check` composes all of the above plus `rollout-check`, `smoke`, `controller-check` in one authoritative sequence; verified live (full transcript captured in this session) — all stages passed.

No target in the reviewed `Makefile` reaches outside the `maops-k8s-day1` cluster or `maops-platform` namespace.

## Failure handling observations

- `scripts/kube.py:run()` defaults `check=True` on every `subprocess.run(["kubectl", ...])` call — a failed `kubectl` invocation raises `CalledProcessError` and propagates as a script failure (non-zero exit), rather than being silently swallowed or reported as a false pass.
- `wait_until()` in `scripts/kube.py` is bounded (explicit `timeout`), converts a timed-out predicate into a `TimeoutError` with the last observed exception attached, and every caller (`rollout-check`, endpoint-readiness wait, reconciliation wait) supplies a finite timeout (120s / 60s) — no unbounded polling was found.
- `reconcile_check.py`'s pod selection (`before_pods[0]["metadata"]["name"]`) has no ordering guarantee from the Kubernetes API, but since it operates on a homogeneous, 2-replica ReplicaSet-backed pod set with an explicit label selector scoped to this workload only, arbitrary selection of "one of the two" is a safe, low-risk choice for this proof — not a race condition in practice, since only the script itself is deleting pods during the check window.
- Port-forward cleanup is not signal-safe against `SIGTERM` (see DAY1-INT-M1) — a hard `kill`/CI timeout on a `make smoke` or `make controller-check` invocation could leak a `kubectl port-forward` subprocess.

---

## Findings

**ID:** DAY1-INT-M1
**Severity:** Medium
**Title:** `scripts/portforward.py` cleanup does not run on `SIGTERM`, leaking `kubectl port-forward` processes
**Evidence:** Live-reproduced in this session: a script that opens `portforward.port_forward(...)` and blocks was sent `SIGTERM` (not `Ctrl-C`/`SIGINT`) from a separate shell. The wrapping Python process terminated immediately; `pgrep -fal "kubectl.*port-forward"` afterward showed the `kubectl --context kind-maops-k8s-day1 -n maops-platform port-forward service/maops-app <port>:8080` process (PID 44197) still running, orphaned. Python's default disposition for `SIGTERM` terminates the interpreter without unwinding `try/finally` blocks (unlike `SIGINT`, which Python's default handler converts into a catchable `KeyboardInterrupt`), so the `finally: _terminate(proc)` in `port_forward()` never executes in this case. `make smoke` and `make controller-check` run to normal completion (including `Ctrl-C`-style interruption) cleanly with zero leftover processes — the gap is specific to `SIGTERM`/process-group kill delivered directly to the Python process, which is exactly how a CI job timeout or `pkill`/`timeout` wrapper typically terminates a hung step.
**Impact:** If `make smoke`, `make controller-check`, or `make rollout-check`-adjacent scripts are ever wrapped by a CI timeout mechanism that sends `SIGTERM` (e.g. `timeout 60 make smoke`, or a Jenkins/GitHub Actions step timeout), a `kubectl port-forward` process can be left running against the live cluster after the job reports failure/timeout, holding a local port and a connection into the cluster. This does not corrupt cluster state, but it is exactly the kind of leaked background process the project's own operating rules explicitly call out as something to avoid.
**Required remediation:** Register an explicit `SIGTERM` handler (e.g. via `signal.signal(signal.SIGTERM, handler)` that raises a catchable exception, or wrap script entry points with a `signal.signal`-based converter to `KeyboardInterrupt`-equivalent) in the scripts that use `port_forward()` (`smoke.py`, `reconcile_check.py`), or add an `atexit`-registered fallback in `portforward.py` that also gets invoked on `SIGTERM`. Re-run the same live `SIGTERM` reproduction after the fix to confirm zero leftover `kubectl port-forward` processes.
**Release-blocking:** NO

**ID:** DAY1-INT-I1
**Severity:** Informational
**Title:** `reconcile_check.py` victim-pod selection relies on API list ordering, not an explicit deterministic rule
**Evidence:** `victim = before_pods[0]["metadata"]["name"]` in `scripts/reconcile_check.py` picks whichever pod the Kubernetes API happens to return first from `kubectl get pods -l <selector> -o json`; there is no explicit sort or documented tie-breaking rule.
**Impact:** Low — no functional risk observed across two live runs in this session (both completed correctly, `8/8` reconciliation checks passed each time); the script only deletes pods it enumerated itself immediately before, and the label selector is scoped tightly to `maops-app`'s own pods, so there is no real risk of touching an unrelated resource. This only affects reproducibility of "which specific pod gets deleted" between runs.
**Required remediation:** Optional: sort `before_pods` by `metadata.creationTimestamp` or `metadata.name` before selecting `before_pods[0]` for deterministic, reviewable output in CI logs.
**Release-blocking:** NO

**ID:** DAY1-INT-I2
**Severity:** Informational
**Title:** `check_configmap_consumption` hardcodes the container's Python interpreter path
**Evidence:** `scripts/cluster_check.py:exec_in_pod` execs `/usr/bin/python3.11` directly (no shell available — confirmed live: `kubectl exec ... -- which python3` and `kubectl exec ... -- ls ...` both failed with `executable file not found in $PATH`, since the distroless base image genuinely has no shell or coreutils). The hardcoded path matches `app/Dockerfile`'s `ENTRYPOINT ["/usr/bin/python3.11", "/app/server.py"]` and the pinned `gcr.io/distroless/python3-debian12` base image digest, and worked correctly in every live run in this session.
**Impact:** None currently — verified correct against the live pod. This is a tight but currently-accurate coupling between the validation script and the base image's specific Python version; it would need updating in lockstep if the base image's Python minor version changes.
**Required remediation:** None required for v0.1.0. Consider deriving the interpreter path from a shared constant if the base image is ever bumped.
**Release-blocking:** NO

---

## Severity counts

- Critical: 0
- High: 0
- Medium: 1 (DAY1-INT-M1)
- Low: 0
- Informational: 2 (DAY1-INT-I1, DAY1-INT-I2)

## FINAL VERDICT: APPROVE WITH CONDITIONS

The real kind/runtime evidence gathered in this review is sufficiently
discriminating to certify v0.1.0's core Day 1 claims: it includes live
`kubectl -o json` state (node, deployment, pods, service, endpoints,
configmap), a live `exec` into the running distroless container proving
ConfigMap-to-environment wiring, real HTTP responses over a genuinely
bounded, cleaned-up port-forward, a live watch of `Endpoints` objects
during pod churn that directly demonstrates readiness gates Service
membership (not just an assertion from the manifest), and two independent
live executions of the controller-reconciliation proof with real,
compared pod UID sets. A full, unmodified `make day1-check` run also
passed end-to-end on the live cluster (31 unit tests, 30/30 manifest
checks, 15/15 real-cluster checks, 4/4 smoke checks, 8/8 reconciliation
checks). None of this evidence is a re-read of the manifest; every check
above queried live cluster or process state.

The one finding raised (DAY1-INT-M1) is a genuine, reproduced gap — the
bounded port-forward helper's cleanup guarantee does not hold under
`SIGTERM` delivered to the wrapping script itself, only under normal
completion or `SIGINT`/exceptions. It is Medium, not Critical or High,
because: it required directly signaling the Python process (not a
day-to-day `make smoke`/`make controller-check` invocation, both of
which were run repeatedly in this session and always cleaned up
correctly), it does not corrupt or alter cluster state, and the leaked
process is trivially observable and killable. It is real risk for a
future CI wiring that times out a step, which is exactly the scenario
this project's own operating rules ask to guard against — hence
"APPROVE WITH CONDITIONS" rather than a clean "APPROVE": v0.1.0 can ship
with this known limitation, on condition that DAY1-INT-M1 is fixed
before any CI orchestration is added on top of these scripts (per the
"any CI added in a later day orchestrates `make` targets" ground rule),
since CI timeouts are the realistic trigger for this gap.

PROJECT 4 DAY 1 CLUSTER INTEGRATION REVIEW COMPLETE

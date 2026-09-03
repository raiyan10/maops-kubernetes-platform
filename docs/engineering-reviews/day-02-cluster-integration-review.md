# Project 4 / Day 2 - Cluster Integration Review

- Repository: `maops-kubernetes-platform`
- Branch: `feature/day-2-service-discovery-secrets`
- Target: `v0.2.0`
- Reviewer role: `cluster-integration-engineer` (independent assessment)
- Live cluster: `maops-k8s-day2` (context `kind-maops-k8s-day2`)
- Review date: 2026-09-03

This review was produced independently against the live kind cluster,
without reading `docs/engineering-reviews/day-02-kubernetes-architecture-review.md`
or `docs/engineering-reviews/day-02-kubernetes-security-review.md` beforehand.
All findings below are the result of direct `kubectl -o json` calls,
`kubectl exec`, real HTTP calls through port-forwards, and reading of
`scripts/*.py` and the `Makefile` in this working tree.

## 1. Live baseline (independently verified)

All of the following were checked directly against the live cluster with
`kubectl --context kind-maops-k8s-day2 ... -o json`, not by re-reading manifests.

| Item | Result |
|---|---|
| `kind get clusters` at review start | `maops-k8s-day1`, `maops-k8s-day2` (both present) |
| Node Ready | `maops-k8s-day2-control-plane` `Ready=True`, kubelet `v1.36.1` |
| Server version | `serverVersion.gitVersion = v1.36.1` (matches `kind/cluster.yaml` pin `kindest/node:v1.36.1@sha256:3489c76...`) |
| Namespace `maops-platform` | `Active` |
| Deployment `maops-gateway` | `readyReplicas=2`, `replicas=2` |
| Deployment `maops-app` | `readyReplicas=2`, `replicas=2` |
| Service `maops-gateway` | `type=ClusterIP`, `10.96.14.172` |
| Service `maops-app` | `type=ClusterIP`, `10.96.66.231` |
| EndpointSlice `maops-gateway-6p6pt` (`discovery.k8s.io/v1`) | 2 endpoints, both `ready=true, serving=true, terminating=false` |
| EndpointSlice `maops-app-dl2t6` (`discovery.k8s.io/v1`) | 2 endpoints, both `ready=true, serving=true, terminating=false` |

`make rollout-check` (`scripts/cluster_check.py`) was also run directly and
reported **33/33 real cluster checks passed**, independently confirming the
above plus ConfigMap consumption inside live pods, container UID/GID
(10001:10001), `readOnlyRootFilesystem`, `allowPrivilegeEscalation=false`,
dropped capabilities, seccomp profile, and Secret volume wiring for both
workloads.

## 2. Day 1 safety

- `kind get clusters` at the start of this review: `maops-k8s-day1`,
  `maops-k8s-day2`.
- `kind get clusters` re-checked at the end of this review (see Section 9):
  same two clusters, unchanged.
- `kubectl --context kind-maops-k8s-day1 get nodes` shows the Day 1 node
  still `Ready`, age unchanged relative to the rest of the review session
  (`2d1h`), and `kubectl --context kind-maops-k8s-day1 get ns` still lists
  its own `maops-platform` namespace as `Active` at age `2d1h` - no writes
  were made against it by this review.
- `Makefile` `cluster-delete` target:
  ```
  cluster-delete: ## Delete ONLY the maops-k8s-day2 kind cluster
      kind delete cluster --name $(CLUSTER_NAME)
  ```
  `CLUSTER_NAME := maops-k8s-day2` is a fixed literal at the top of the
  Makefile, so `cluster-delete` can only ever target the exact Day 2
  cluster name. There is no target that runs `docker system prune` or
  deletes clusters by pattern/prefix.
- `scripts/kube.py` centralizes every mutating and read kubectl call
  through a single `run()` helper:
  ```python
  CONTEXT = "kind-maops-k8s-day2"
  ...
  def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
      cmd = ["kubectl", "--context", CONTEXT, *args]
      return subprocess.run(cmd, capture_output=True, text=True, check=check)
  ```
  Every scale/delete/apply/get/exec call in `cluster_check.py`,
  `discovery_check.py`, `dependency_check.py`, `secret_check.py`,
  `secret_bootstrap.py`, `reconcile_check.py`, and `smoke.py` goes through
  `kube.run`/`kube.get_json` (imported from `kube.py`), so every one of
  them carries an explicit `--context kind-maops-k8s-day2`. `portforward.py`'s
  `port_forward()` also requires an explicit `kube_context` argument and
  passes it through to `kubectl --context ... port-forward`; no caller
  invokes it without passing `kube.CONTEXT` explicitly. I found no call
  site anywhere in `scripts/` that invokes `kubectl` without an explicit
  `--context`, and no ambient-current-context dependence. `manifest_check.py`
  and `version_check.py` call `kubectl kustomize <dir>` only - a
  context-free, cluster-free static render - which is correct for the
  static-validation tier and not a live-cluster risk.
- Conclusion: Day 1 isolation is structurally enforced (not just by
  convention) and was empirically undisturbed by this review.

## 3. DNS proof (independently exercised, not just read)

I ran my own, independent DNS resolution proof directly against a live
gateway pod, separately from `scripts/discovery_check.py`:

```
$ kubectl --context kind-maops-k8s-day2 -n maops-platform exec \
    maops-gateway-545c85bbd9-2qd45 -- /usr/bin/python3.11 -c "
import socket
infos = socket.getaddrinfo('maops-app', 8080)
addrs = sorted({info[4][0] for info in infos})
print('resolved addresses:', addrs)
"
resolved addresses: ['10.96.66.231']

$ kubectl --context kind-maops-k8s-day2 -n maops-platform get svc maops-app \
    -o jsonpath='{.spec.clusterIP}'
10.96.66.231

$ kubectl ... exec ... -c "print(socket.getfqdn('maops-app'))"
maops-app.maops-platform.svc.cluster.local
```

`maops-app` resolved via real Kubernetes (CoreDNS) name resolution from
inside a live gateway pod, to exactly the live `maops-app` Service's
ClusterIP - not asserted, cross-checked directly against `kubectl get svc`.

I then independently exercised the traffic path, not just resolution, via
a manual port-forward to `service/maops-gateway` and a real `curl` to
`/backend`:

```
$ curl -s -w "\nHTTP_STATUS=%{http_code}\n" http://127.0.0.1:18080/backend
{"gateway_hostname": "maops-gateway-545c85bbd9-2qd45", "backend_service":
 "maops-kubernetes-app", "backend_hostname": "maops-app-5cd767b9bd-xkrr9",
 "backend_environment": "day2-service-discovery"}
HTTP_STATUS=200
```

`backend_hostname` (`maops-app-5cd767b9bd-xkrr9`) matched a real, live
`maops-app` pod name at the time (`kubectl get pods -l component=app`),
proving the DNS-resolved address genuinely backed real HTTP traffic to a
real app pod, not just a resolution-only check. This satisfies the
requirement that DNS proof be backed by real service traffic.

`make discovery-check` (`scripts/discovery_check.py`) was then run for
completeness and reproduced the same result independently:
`2/2 discovery checks passed`.

Source inspection: `gateway/server.py` sources `BACKEND_HOST` from
`os.environ.get("BACKEND_HOST", "maops-app")`, which is populated from
`k8s/base/gateway-configmap.yaml`'s `BACKEND_HOST: "maops-app"` (a Service
DNS name, not an IP). `grep -rn "10\.96\.\|10\.244\.\|ClusterIP"` across
`app/`, `gateway/`, and `k8s/base/` found no hardcoded ClusterIP/Pod-IP
anywhere in the workload code - only `type: ClusterIP` in the two Service
manifests themselves, which is expected.

## 4. Service-to-service HTTP (gateway -> maops-app -> gateway)

`gateway/server.py`'s `_handle_backend()`:
- returns `503 {"error": "backend unavailable"}` on any network
  failure/timeout (no traceback, no token echoed - `except
  (urllib.error.URLError, TimeoutError, OSError, ValueError)`),
- otherwise calls `app`'s `/internal/info` with the mounted internal
  token header and forwards `gateway_hostname` (its own
  `socket.gethostname()`), `backend_service`, `backend_hostname`, and
  `backend_environment` sourced directly from the app's JSON response
  body - not hardcoded.

`app/server.py`'s `/internal/info` handler authenticates with
`hmac.compare_digest(provided, INTERNAL_TOKEN)` (constant-time comparison)
and returns its own real `socket.gethostname()`, `APP_NAME`, and
`APP_ENVIRONMENT`.

Live verification (Section 3 above, and re-confirmed via `make smoke`):
`/backend`'s `backend_hostname` field consistently matched a genuine, live
`maops-app` pod name (observed both `maops-app-5cd767b9bd-s6wc7` and
`maops-app-5cd767b9bd-xkrr9` across different runs, matching whichever pod
actually served the request), and `backend_service` consistently equalled
`"maops-kubernetes-app"` (the app's real `APP_NAME`). This is genuine,
non-hardcoded, per-request evidence that `/backend` data originates from
the real app workload through the Service, not from a faked/local value in
the gateway.

`http_checks.py`'s `_check_semantics()` independently enforces this for
every automated run: `GATEWAY_BACKEND_FIELDS` requires
`{gateway_hostname, backend_service, backend_hostname,
backend_environment}` to be present, and asserts
`backend_service == "maops-kubernetes-app"` (`APP_EXPECTED_APP_NAME`) -
a routing/faking bug that served a static/local value would fail this
check.

No Service-discovery bypass was found: no hardcoded Pod IP, Pod name, or
ClusterIP anywhere in `app/server.py` or `gateway/server.py`; `BACKEND_HOST`
is always the Service DNS name `maops-app`, sourced from the ConfigMap.

## 5. EndpointSlice validation (`scripts/endpointslice.py`)

```python
def count_ready_endpoints(slices: list[dict]) -> int:
    total = 0
    for s in slices:
        for ep in s.get("endpoints", []) or []:
            conditions = ep.get("conditions") or {}
            if conditions.get("ready") is True:
                total += len(ep.get("addresses") or [])
    return total
```

Findings from code review plus `tests/test_endpointslice.py`:

- **Readiness interpretation is correct and strict**: only
  `conditions.ready is True` (explicit boolean `True`) counts as ready.
  Missing/`False`/`null` `ready` is correctly treated as not-ready
  (`test_missing_ready_condition_treated_as_not_ready`), matching
  kube-proxy's own routing semantics. `terminating`/`serving` are not
  used as the readiness signal by this function (correct - `ready` is
  the authoritative field for backend routing), and the caller
  (`dependency_check.py`) separately treats `terminating` conditions'
  absence-of-pods as the stronger drain signal (Section 7).
- **Multiple EndpointSlices per Service are handled correctly.** Callers
  (`cluster_check.py: check_endpointslice()`, `dependency_check.py:
  _app_endpoints_drained()`) query
  `kubectl get endpointslices -l kubernetes.io/service-name=<svc>` (the
  full list, not `[0]`) and pass the entire `items` list into
  `count_ready_endpoints`, which sums across every slice
  (`test_sums_across_multiple_slices`). No code path in this repository
  assumes exactly one slice per Service.
- **No duplicate counting observed or structurally likely** for the
  single-stack IPv4 case actually deployed here - each endpoint's address
  list is summed once per endpoint entry, and slices returned by the
  label selector are disjoint sets of endpoints by construction of the
  EndpointSlice controller.
- **Address-family assumption**: the live cluster is single-stack IPv4
  (`addressType: IPv4` on both observed slices, confirmed live). The
  code does not filter or dedupe by `addressType`. If this cluster were
  ever made dual-stack, a Service backed by both an IPv4 and an IPv6
  EndpointSlice sharing the same `kubernetes.io/service-name` label
  would have its ready count summed across both address families,
  effectively double-counting each ready Pod (one address per family).
  This is out of scope for Day 2 per the roadmap and does not affect the
  current single-stack deployment, but is worth a forward-looking note
  (see DAY2-INT-I1).
- **Stale-slice race handling**: `check_endpointslice()` in
  `cluster_check.py` polls via `wait_until(..., timeout=60, interval=2)`
  until the ready count exactly equals `EXPECTED_REPLICAS`, rather than
  reading the slice once - this avoids a false pass/fail on a
  not-yet-converged slice immediately after a rollout.

## 6. Secret bootstrap order

Verified `Makefile` ordering in `day2-check`:
`cluster-create -> namespace-apply -> secret-bootstrap -> image-load -> deploy`.

- **Namespace-missing race**: `scripts/secret_bootstrap.py:get_existing_secret()`
  treats any `kubectl get secret ... ` stderr containing `"NotFound"`/`"not
  found"` as "secret does not exist," which is also the literal substring
  Kubernetes uses for a *missing namespace* error
  (`Error from server (NotFound): namespaces "X" not found`). I
  independently reproduced this by monkey-patching `NAMESPACE` to a
  nonexistent namespace and running the real `main()`:
  ```
  $ python3 -c '... run against namespace "maops-platform-nonexistent-test" ...'
  Secret 'maops-internal-auth' does not exist - generating a new token.
  FAIL: kubectl create secret failed: error: failed to create secret namespaces "maops-platform-nonexistent-test" not found
  RC= 1
  ```
  The script does **not** silently succeed into the wrong place, and does
  **not** crash uninformatively - it fails cleanly (`return 1`) with the
  real, accurate root-cause `kubectl` stderr surfaced on the final line.
  The only defect is a misleading intermediate message ("does not exist -
  generating a new token") printed before the real cause is known - see
  DAY2-INT-L1. No live cluster resources were created or mutated by this
  reproduction (the target namespace never existed).
- **Deploy does not touch the Secret**: `k8s/base/kustomization.yaml` lists
  only `namespace.yaml`, both ConfigMaps, both Deployments, and both
  Services - there is no `kind: Secret` resource anywhere under `k8s/base/`
  (`grep -rln "kind: Secret" k8s/base/` returned nothing), so `make deploy`
  (`kubectl apply -k k8s/base`) cannot overwrite or delete
  `maops-internal-auth`.
- **Idempotency verified live**: I ran `python3 scripts/secret_bootstrap.py`
  a second time against the already-bootstrapped live Secret and confirmed
  no rotation:
  ```
  before: resourceVersion=538, uid=f6975d70-e9b0-445d-bb91-59c48862acd2
  Secret 'maops-internal-auth' already exists - preserving it (no rotation).
  PASS: secret bootstrap verified existing Secret
  after:  resourceVersion=538, uid=f6975d70-e9b0-445d-bb91-59c48862acd2
  ```
  Confirms repeated `day2-check`/`secret-bootstrap` runs do not regenerate
  or rotate the Secret value.

## 7. Discovery check (`scripts/discovery_check.py`)

- `check_dns_resolution()`: catches `subprocess.CalledProcessError`
  specifically (kubectl exec failure -> `record(False, ...)`, non-zero
  exit path), validates the exec output is parseable as `int` (wrong/
  garbage output -> `record(False, ...)`), and only passes if
  `resolved_count >= 1`. No bare `except`, no swallowed return code.
- `check_real_service_http()`: wraps `port_forward()` in
  `try/except (TimeoutError, RuntimeError)`, and delegates response
  validation to `http_checks.check_endpoint()`, which itself checks
  `status != 200 -> False`, invalid JSON -> `False`, and
  role/path-specific semantic assertions (`_check_semantics`) - so wrong
  response content (e.g. a routing bug serving the wrong handler's body
  on the right path/status) is caught, not just a bare 200.
- `main()` aggregates `results` and returns `1` if any check failed - no
  swallowed failures, no false PASS path found.
- Live-ran (`make discovery-check`): `2/2 discovery checks passed`,
  reproducing my own independent DNS/HTTP proof above.

## 8. Dependency-failure experiment (`scripts/dependency_check.py`)

This was executed live against `maops-k8s-day2` (not merely read), with a
verified restoration afterward.

**Starting state (recorded before the experiment):**
```
maops-app-5cd767b9bd-s6wc7        maops-app        restarts=0
maops-app-5cd767b9bd-xkrr9        maops-app        restarts=0
maops-gateway-545c85bbd9-2qd45    maops-gateway    restarts=0
maops-gateway-545c85bbd9-w5czq    maops-gateway    restarts=0
```
gateway 2/2, app 2/2.

**`make dependency-check` output (full, unedited):**
```
[PASS] starting state: gateway 2/2 Ready
[PASS] starting state: app 2/2 Ready
[PASS] maops-app scaled to 0 replicas, EndpointSlice drained, and all app
       Pods fully terminated (maops-gateway replicas untouched)
[PASS] gateway /livez during app outage: /livez: HTTP 200, body={"status": "alive"}
[PASS] gateway /readyz during app outage -> HTTP 503 (expected 503)
[PASS] gateway /backend during app outage -> HTTP 503 (expected 503, no traceback, no token)
[PASS] gateway container restart counts unchanged solely due to app outage:
       before={'maops-gateway-545c85bbd9-2qd45': 0, 'maops-gateway-545c85bbd9-w5czq': 0}
       after={'maops-gateway-545c85bbd9-2qd45': 0, 'maops-gateway-545c85bbd9-w5czq': 0}
[PASS] RESTORATION: maops-app restored to 2/2 Ready
[PASS] RESTORATION: maops-gateway recovered to 2/2 Ready
[PASS] RESTORATION: post-recovery gateway /readyz: /readyz: HTTP 200,
       body={"status": "ready", "backend": "ready"}
[PASS] RESTORATION: post-recovery gateway /backend: /backend: HTTP 200,
       body={"gateway_hostname": "maops-gateway-545c85bbd9-2qd45",
             "backend_service": "maops-kubernetes-app",
             "backend_hostname": "maops-app-5cd767b9bd-qtgw6",
             "backend_environment": "day2-service-discovery"}

11/11 dependency-failure checks passed
PASS: dependency-failure behavior proven and maops-app restored to 2/2 Ready
```

**Independently confirmed post-run cluster state:**
```
$ kubectl get deployments -n maops-platform
maops-app       2/2
maops-gateway   2/2

$ kubectl get pods -n maops-platform
maops-app-5cd767b9bd-qtgw6       Running   restarts=0   age=27s   (new)
maops-app-5cd767b9bd-rn77q       Running   restarts=0   age=27s   (new)
maops-gateway-545c85bbd9-2qd45   Running   restarts=0   age=73m   (unchanged)
maops-gateway-545c85bbd9-w5czq   Running   restarts=0   age=73m   (unchanged)
```
The gateway pods' identity and restart counts were literally unchanged
across the entire experiment (same pod names, `restarts=0` before and
after) - direct, empirical confirmation that gateway liveness genuinely
never depended on the app dependency, and the readiness probe failing
during the app outage did not trigger a container restart/crash-loop.

**Race-fix inspection** (`_app_endpoints_drained()` in
`dependency_check.py`):
```python
def _app_endpoints_drained():
    slices = get_json(..., "get", "endpointslices", "-l",
                       f"kubernetes.io/service-name={APP_SERVICE}")["items"]
    if count_ready_endpoints(slices) != 0:
        return None
    return True if not get_pods(APP_LABEL_SELECTOR) else None

wait_until(_app_endpoints_drained, timeout=90, interval=1,
           description="maops-app EndpointSlice drained and all app Pods fully terminated")
```
This is a genuine fix, not a fixed sleep dressed up as one:
- It is a **bounded, deadline-based poll** (`time.monotonic()`-based
  deadline inside `wait_until`, `timeout=90`, `interval=1` used only as
  backoff between polls of a real condition, not as the correctness
  mechanism itself).
- It requires **both** the EndpointSlice ready-count to reach exactly 0
  **and** the underlying Pod objects (matched by `APP_LABEL_SELECTOR`) to
  be fully gone from the API - not just `Terminating`. This is stricter
  than checking replica count or EndpointSlice alone, and directly
  addresses the two races named in the review brief: (a) the Service
  still routing to a Pod whose EndpointSlice entry hasn't yet flipped to
  not-ready, and (b) a `Terminating` Pod that is EndpointSlice-absent but
  still an OS process capable of answering a request for its grace
  period. Only when both signals agree does the outage-behavior assertion
  run.
- The restart-count comparison (`before_restarts == after_restarts`) is
  computed from pod objects fetched fresh, not cached from before the
  experiment (`get_pods(GATEWAY_LABEL_SELECTOR)` called again after the
  outage checks) - no stale-pod-selection bug found here (gateway pods
  are never replaced during this experiment in the first place, so even
  the single upfront capture of `gateway_pod_name` for the direct-pod
  port-forward is safe).

**Restoration precedence** (`main()`):
```python
scaled_down = False
try:
    scale_app(0); scaled_down = True
    run_experiment(gateway_pods)
except subprocess.CalledProcessError as exc: record(False, ...)
except Exception as exc: record(False, ...)   # documented, not swallowed
finally:
    if scaled_down:
        restore_app()

all_results = results + restoration_results
restoration_failures = [m for ok, m in restoration_results if not ok]
...
if restoration_failures:
    print("!!! RESTORATION FAILURE ... !!!", file=sys.stderr)
    for msg in restoration_failures: print(f"RESTORATION FAILURE: {msg}", file=sys.stderr)
if failures:
    return 1
```
Primary-experiment results and restoration results are tracked in two
separate lists (`results` vs. `restoration_results`) and both are folded
into the final pass/fail count; a restoration failure is printed with an
explicit, loud `!!! RESTORATION FAILURE !!!` banner on stderr regardless
of whether the primary experiment passed or failed, and is never hidden
behind the primary result. `restore_app()` is called unconditionally in
a `finally` block once `scale_app(0)` has succeeded, independent of
whether `run_experiment()` raised or recorded failures. This design was
not stress-tested against an actual restoration failure in this review
(both primary and restoration succeeded on the live run), but the code
path is unconditional and structurally sound by inspection.

**One design gap found**: the `/backend during app outage` check
```python
status, _body = raw_get(local_port, "/backend")
record(status == 503, f"... (expected 503, no traceback, no token)")
```
only asserts the HTTP status code (`503`); it does **not** assert the
response body content (the discarded `_body`) despite the log message's
claim of "no traceback, no token". By source inspection,
`gateway/server.py:_handle_backend()`'s exception path returns a fixed
literal `{"error": "backend unavailable"}` with no interpolation of
exception details or the token, so this is not exploitable in the current
code - but the script itself does not independently verify that
no-token/no-traceback claim for this specific check the way
`http_checks.check_endpoint()`'s token-substring scan does elsewhere.
See DAY2-INT-L2.

Cluster was left at `maops-app: 2/2`, `maops-gateway: 2/2` after this
experiment, confirmed both by the script's own final assertions and by an
independent `kubectl get deployments`/`get pods` call (Section 8 above).

## 9. Port-forward hygiene

- `scripts/portforward.py` retains the Day 1 SIGTERM-safe cleanup
  (`_convert_sigterm_to_exception()` + `try/finally: _terminate(proc)`,
  process-group kill via `os.killpg`, SIGTERM then SIGKILL-after-timeout
  fallback). No changes needed here for Day 2.
- Every Day 2 caller (`discovery_check.py`, `dependency_check.py`,
  `secret_check.py`, `smoke.py`, `reconcile_check.py`) uses the shared
  `port_forward()` context manager exclusively - no duplicate/parallel
  port-forward implementation was introduced for Day 2.
- I ran `make discovery-check`, `make smoke`, `make rollout-check`,
  `make secret-check`, and `make dependency-check` (the last of which
  exercises both a Service port-forward and a direct-Pod port-forward)
  and checked `ps aux | grep -i port-forward` after each - no leaked
  `kubectl port-forward` process was left running after any of them,
  including the dependency-failure experiment's outage window.
- `dependency_check.py`'s use of a direct Pod port-forward
  (`resource_kind="pod"`) to reach the gateway pod during the app outage
  (bypassing the Service, since a not-Ready gateway Pod may stop being
  routed to) and `secret_check.py`'s direct port-forward to an app Pod
  are both scoped, temporary exceptions used specifically for
  dependency/security validation, consistent with the stated Day 2
  norm that ordinary port-forwarding targets `service/maops-gateway`
  only.

## 10. Failure handling (cross-cutting)

- No bare `except:` found anywhere in `scripts/`.
- Only two `except Exception` sites exist
  (`dependency_check.py:209`, `kube.py:58`), both explicitly commented,
  both feed into a recorded/surfaced failure (never a silent `pass`).
- No unbounded `while True` loops found in `scripts/`.
- All `time.sleep()` calls (`portforward.py:76`, `kube.py:60`) are used
  as backoff between polls of a real condition inside a
  `time.monotonic()`-deadline-bounded loop (`wait_until`,
  `_wait_connectable`) - never as the sole correctness mechanism.
- All mutating/reading kubectl calls go through `kube.run`/`kube.get_json`,
  which always carries `--context kind-maops-k8s-day2` explicitly - no
  ambient-current-context dependence found (Section 2).
- Secret rotation on repeated runs: verified not to occur (Section 6).

## Findings

No Critical findings.

**High**

None found.

**Medium**

None found.

**Low**

- **DAY2-INT-L1**: `scripts/secret_bootstrap.py:get_existing_secret()`
  cannot distinguish a missing-namespace `kubectl` error from a
  missing-Secret error (both contain the substring `"NotFound"`), so if
  `secret-bootstrap` is ever run before `namespace-apply` (outside the
  documented/enforced `day2-check` ordering), it prints a misleading
  intermediate message ("Secret ... does not exist - generating a new
  token") before ultimately failing non-zero with the accurate root-cause
  `kubectl create secret` error. Independently reproduced live (Section
  6); does not silently succeed or corrupt state, and does not affect the
  documented `day2-check` ordering, which always runs `namespace-apply`
  first. Recommend distinguishing the namespace-missing case explicitly
  (e.g. `kubectl get namespace <ns>` pre-check) for a clearer message.
- **DAY2-INT-L2**: `dependency_check.py`'s `/backend during app outage`
  check only asserts HTTP status `503`, not response body content, despite
  its own log message claiming "no traceback, no token". By source
  inspection the actual response body is a safe fixed literal, so this is
  not a live security gap, but the script does not independently prove
  its own claim the way `http_checks.check_endpoint()` does for other
  paths. Recommend asserting on the body (e.g. via `check_endpoint`-style
  semantics or a direct token-substring scan) rather than status code
  alone.

**Informational**

- **DAY2-INT-I1**: `scripts/endpointslice.py:count_ready_endpoints()` sums
  ready endpoints across every EndpointSlice returned for a
  `kubernetes.io/service-name` selector without filtering/deduping by
  `addressType`. The live Day 2 cluster is single-stack IPv4 (verified:
  both observed slices report `addressType: IPv4`), so this is correct
  today. If a future day introduces dual-stack, a Service with both IPv4
  and IPv6 EndpointSlices under the same label would have each ready Pod
  counted twice (once per address family) unless the counting logic is
  updated to filter by a single `addressType`. Explicitly out of scope
  per the roadmap for this stage; flagged only as a forward-looking note.

## Final Verdict

**APPROVE**

### Severity counts

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 2 |
| Informational | 1 |

### Explicit answers

- **Is the real Kubernetes DNS proof credible?** Yes. I independently ran
  `socket.getaddrinfo('maops-app', 8080)` inside a live gateway pod via
  `kubectl exec`, obtained a real resolved address, cross-checked it
  byte-for-byte against the live `maops-app` Service's `spec.clusterIP`
  (`10.96.66.231` in both), and separately confirmed the resolved name
  backs real HTTP traffic to a genuine app pod (`/backend`'s
  `backend_hostname` matched an actual live app pod name). This is not
  source-only, not ConfigMap-only, and not an exact-IP assertion made in
  isolation from traffic.
- **Is EndpointSlice validation robust enough for v0.2.0?** Yes for the
  current single-stack IPv4 scope. Readiness interpretation is strict and
  correct (`conditions.ready is True` only), multiple slices per Service
  are queried and summed correctly (not `slice[0]`), and the counting
  logic is unit-tested against realistic fixtures. The only caveat
  (DAY2-INT-I1) is a documented, out-of-scope dual-stack limitation.
- **Is dependency failure/restoration discriminating and safe?** Yes. Live
  execution produced exact, discriminating evidence: `/livez` 200,
  `/readyz` 503, `/backend` 503 during the outage, gateway restart counts
  unchanged (0 -> 0, proving readiness-vs-liveness separation genuinely
  holds and the gateway did not crash-loop), and full restoration to 2/2
  with a real new app pod serving `/backend` again afterward. Restoration
  runs unconditionally in a `finally`-equivalent path and its failures are
  tracked and surfaced separately from primary-experiment failures, never
  hidden behind them (DAY2-INT-L2 is a minor assertion-completeness gap,
  not a safety issue).
- **Was the reported EndpointSlice/Pod-termination race genuinely fixed?**
  Yes. `_app_endpoints_drained()` requires both EndpointSlice ready-count
  to reach 0 **and** all matching Pod objects to be fully gone from the
  API before the outage-behavior assertions run, polled on a bounded
  deadline (not a fixed sleep). This correctly closes both the
  stale-Service-endpoint race and the Terminating-Pod-still-answering
  race described in the review brief.
- **Is Day 1 cluster isolation preserved?** Yes. `maops-k8s-day1` was
  present at both the start and end of this review, its node remained
  `Ready`, its own `maops-platform` namespace was unchanged, and every
  mutating code path in `scripts/` and `cluster-delete` in the `Makefile`
  is scoped to the literal `maops-k8s-day2` cluster/context with no
  ambient-context dependence found anywhere.

maops-app was left restored at 2/2 Ready (verified independently after
the experiment), maops-gateway remained 2/2 Ready throughout with restart
counts unchanged, no `kubectl port-forward` processes were left running
after any script executed during this review, and both `maops-k8s-day1`
and `maops-k8s-day2` kind clusters remain intact.

PROJECT 4 DAY 2 CLUSTER INTEGRATION REVIEW COMPLETE

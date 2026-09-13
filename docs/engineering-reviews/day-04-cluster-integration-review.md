# Day 4 (v0.4.0) Cluster Integration Review

**Reviewer role:** `cluster-integration-engineer` (third independent Day 4 review pass;
architecture and security reviews were completed separately and their
conclusions were NOT read before this assessment was formed - see
"Independence" below).

**Scope:** Real-cluster runtime behavior of `maops-k8s-day4` (context
`kind-maops-k8s-day4`, namespace `maops-platform`) - cluster
identity/topology, three-tier readiness (app/gateway/state), PVC/PV
binding and worker pinning, storage-provisioner hardening, image
build/load identity, timeout architecture, persistence/retention
experiment design (read-only code review, not re-executed), preserved
Day 3 behavior, and a dedicated restart-timeline investigation. This is
assessment only - no remediation was performed.

**Repository:** `~/DevOps-Portfolio/maops-kubernetes-platform`
**Branch:** `feature/day-4-stateful-persistence`
**HEAD (verified, unchanged throughout this review):** `aa2049876c7be2b959acb6e2a1d20f979ee440bc`
**v0.3.0 tag object (verified, unchanged):** `2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1` -> peeled commit `9fc7fe9f25d729d76317de85b5722e84271234f0`

## Independence

Per the review brief, `docs/engineering-reviews/day-04-kubernetes-architecture-review.md`
and `docs/engineering-reviews/day-04-kubernetes-security-review.md` were
**not read** for their findings/verdicts before this review's own
conclusions were formed. They were only hashed:

```
sha256sum docs/engineering-reviews/day-04-kubernetes-architecture-review.md
e6d9bcf1001e7c1a855d16b9644e27bbbdc1a855d05d9daf291e5aa98f1c7eb5

sha256sum docs/engineering-reviews/day-04-kubernetes-security-review.md
d33f2b13cd31e06fc3e231106750b6e4a80f2b71e21d605bcfb5da2b9c51e946
```

These two hashes were captured at the start of this session and
re-verified identical at the end (see "Final verification" below); this
review's own file was never used to influence those two documents' state.

The two prior review teams' *raw runtime evidence directories* were
inspected only for their file timestamps and node/container identity
values, as historical cross-reference points for the restart timeline
(see below) - never for their written conclusions:

- `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-arch-review-20260910T031500Z-kA9v2/`
- `~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-security-review-20260910T033729Z/`

## Candidate integrity verification

Original candidate evidence directory:
`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-final-candidate-20260908T112009Z-ckQ1uj/`

```
sha256sum day4-check.log
6e0fc45eb36226d004b110cfe775ace4d77eb7aebb4b2c110840e0e59e3a9cf0   (MATCHES expected)

sha256sum candidate-before.sha256 candidate-after.sha256
3e2daca59d40ba1a1d5fe4a4a8a3153d2029e5252fe96c58190e4493e42b6d90  candidate-before.sha256
3e2daca59d40ba1a1d5fe4a4a8a3153d2029e5252fe96c58190e4493e42b6d90  candidate-after.sha256   (BOTH MATCH expected, and are byte-identical to each other - no drift occurred during the original candidate session)

diff candidate-before.sha256 candidate-after.sha256   -> no output (identical)
head-before.txt / head-after.txt -> both aa2049876c7be2b959acb6e2a1d20f979ee440bc
```

All 122 manifest entries in `candidate-after.sha256` were independently
re-hashed against the current working tree (sha256, kind, octal mode,
path) using a purpose-built verification script
(`/tmp/.../scratchpad/verify_manifest.py`, ephemeral, not part of the
repo):

```
Total manifest entries: 122
Missing/kind-issues: 0
Mismatches: 0
ALL 122 ENTRIES VERIFIED OK
```

The only files present beyond those 122 are the two expected review
docs (hashed above) - confirmed via `git status --porcelain=v1 -uall`,
which shows exactly the 30 tracked-modified files, 10 new
implementation/script/manifest files, and the two new review docs -
all already accounted for in the 122-entry manifest plus the two
additions. **Initial inventory: 124 entries, matching expectation.** No
unexplained drift found.

## Evidence directory (this session, fresh)

`~/DevOps-Portfolio/_local-evidence/maops-kubernetes-platform/day-04/day4-integration-review-20260910T043201Z-WI95v/`

Numbered files `01`-`21` therein contain raw command output for every
claim below (node/version, docker inspect, pod JSON, events, crictl
inspect/ps, kubelet/containerd journal excerpts, dmesg, workload state,
PV details, storage permissions, state.json hash recheck, StatefulSet
DNS check, image digest comparison, Day 3 preserved-behavior spot
check, `make smoke` run, `make context-check`/`state-check` runs, and a
second point-in-time snapshot).

## 1. Cluster identity, topology, isolation

Fresh evidence (`01-nodes-version.txt`):

```
maops-k8s-day4-control-plane   Ready    control-plane   v1.36.1
maops-k8s-day4-worker          Ready    <none>          v1.36.1
maops-k8s-day4-worker2         Ready    <none>          v1.36.1
serverVersion.gitVersion = v1.36.1
```

3 nodes (1 control-plane + 2 workers), matches `kind/cluster.yaml`'s
declared topology and pinned digest
(`kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5`,
unchanged from Days 1-3). `docker ps -a` (`01-nodes-version.txt`/session
notes) shows the three Day 4 node containers running with the exact
container IDs given in the task brief (`aa0103432c33`, `3aa45350df2c`,
`a66b08f34a2d`), and the five Day 1-3 containers present but
`Exited (130)` - confirming isolation: Day 4 tooling never started or
stopped them, and `scripts/kube.py:verify_context()` (read, not
modified) still fails closed via an anchored per-cluster node-name regex
(`_DAY_NODE_NAME_RE`) before any live script proceeds. `make
context-check` was run fresh and passed 6/6 (`19-context-check.txt`).

`scripts/final_state_check.py` (read-only reviewed) independently checks
that the Day 1/2/3 kind clusters still exist by name (not their internal
state) as part of `day4-check`'s own final proof - consistent with the
isolation contract.

## 2. Three-tier readiness, Services, EndpointSlices, StatefulSet DNS

Fresh evidence (`03-pods.txt`, `11-workload-state.txt`, `15-statefulset-dns-check.txt`, `20-state-check.txt`):

- `maops-app` 3/3 Ready, `maops-gateway` 3/3 Ready, `maops-state` 1/1
  Ready (StatefulSet).
- EndpointSlices: `maops-app` 3 endpoints, `maops-gateway` 3 endpoints,
  `maops-state` 1 endpoint, `maops-state-headless` 1 endpoint - all
  matching live Pod IPs exactly.
- Stable per-Pod StatefulSet DNS proven live via a real
  `socket.getaddrinfo()` call from inside a running gateway Pod (never a
  shell/dig, matching the existing `discovery_check.py` pattern):
  `maops-state-0.maops-state-headless.maops-platform.svc.cluster.local`
  resolved to `10.244.1.5`, the Pod's actual IP; `maops-state.maops-platform.svc.cluster.local`
  resolved to the Service ClusterIP `10.96.191.244`.
- `make state-check` run fresh: **23/23 PASS**, covering StatefulSet
  identity, worker-only scheduling, absence of PDB/topology spread (both
  correctly meaningless for 1 replica), Service/headless-Service
  shape, PVC/PV binding and capacity, live UID/GID/groups (10001),
  security-context fields, Secret volume/mount wiring, and the direct
  unauthenticated `GET /state` -> HTTP 403 rejection.
- `make smoke` run fresh against `service/maops-gateway`: **6/6 PASS**
  including `/state` returning `{"value": "day4-retention-054e49df1b0a481c"}`
  (a leftover marker from the Sept 8 historical mutating retention
  experiment - see finding DAY4-INT-3). No leftover `kubectl
  port-forward` process after the run (`ps aux | grep port-forward`
  returned nothing).

## 3. PVC/PV identity, worker pinning, retained data

Fresh evidence (`11-workload-state.txt`, `12-pv-details.txt`):

```
PVC data-maops-state-0: Bound, uid=00b512bf-7774-4873-a092-c56c37b9b1b7, volume=pvc-00b512bf-7774-4873-a092-c56c37b9b1b7, capacity=256Mi
PV  pvc-00b512bf-7774-4873-a092-c56c37b9b1b7: Bound, uid=2c7d826b-1c69-4c73-b1c5-6cd23a91a14e
PV nodeAffinity: kubernetes.io/hostname In [maops-k8s-day4-worker]
PV hostPath: /var/local-path-provisioner/pvc-00b512bf-...-b7_maops-platform_data-maops-state-0
maops-state-0 pod.spec.nodeName: maops-k8s-day4-worker
StorageClass "standard" (default): rancher.io/local-path, WaitForFirstConsumer, reclaimPolicy Delete
```

Both the PVC UID and PV UID **exactly match** the values given in the
review brief. The PV's `nodeAffinity` confirms `rancher.io/local-path`'s
documented `WaitForFirstConsumer` node-pinning behavior in practice, not
just in documentation. This is consistent with, but does not by itself
re-prove, the historical 1->0->1 retention cycle (see Section 8 - that
proof is historical/candidate-log evidence, not re-executed this
session).

## 4. Storage provisioner hardening - current state (read-only)

Fresh evidence (`13-storage-provisioning-perms.txt`):

```
/var/local-path-provisioner on ALL THREE nodes: mode=2770 uid=0 gid=10001
data-maops-state-0's backing directory: mode=2770 uid=0 gid=10001
state.json inside it: mode=600 uid=10001 gid=10001 size=44
```

All match the review brief's expected values exactly. Critically, the
**live `local-path-config` ConfigMap's `setup` field** was read directly
(`13-storage-provisioning-perms.txt`) and matches
`scripts/storage_bootstrap.py`'s `EXPECTED_PATCHED_SETUP` byte-for-byte
(`mkdir -m 2770 -p "$VOL_DIR"` plus the symlink/pre-existing-path/scope
guards) - this is a configuration-level proof, not merely an
existing-data permission observation: because the *setup script itself*
(not just one already-provisioned directory) is confirmed hardened, a
**future** fresh PVC provisioned by this same provisioner would be
expected to inherit the same `2770`/setgid-group-10001 behavior via
standard Linux setgid-directory semantics, as `docs/architecture.md`
documents. That said, per the review brief's own caution, this is still
an inference from configuration, not a fresh, live re-provisioning
proof - the actual positive/negative scratch-PVC proof (`make
storage-hardening-check`) is historical-only in this session (see
Section 9's smallest-targeted-experiment note).

## 5. `storage_bootstrap.py` - idempotency, ordering, partial failure/rollback (code review only)

Reviewed `scripts/storage_bootstrap.py` in full (not re-run, per the "no
storage-bootstrap mutation" constraint):

- **Ordering**: node-root hardening always runs first, then the
  ConfigMap `setup`-field patch, then bounded scratch-PVC propagation
  verification, matching `docs/architecture.md`'s documented two-step
  design and the Makefile's `day4-check` recipe order
  (`image-load` -> `storage-bootstrap` -> `storage-hardening-check` ->
  `namespace-apply`).
- **Idempotency**: node-root hardening is independently re-verified/
  re-applied on *every* run regardless of the ConfigMap's current state
  (it does not short-circuit just because the ConfigMap patch is already
  applied) - confirmed by reading `main()`'s control flow. The
  ConfigMap patch itself refuses to touch anything that isn't a
  byte-for-byte match of the known original or already-patched form.
- **Node set at run time, not cached**: `harden_provisioning_root_on_all_nodes()`
  calls `_cluster_nodes()` fresh via `kubectl get nodes` on every
  invocation - it never assumes a fixed/cached node list from a prior
  run, so a node that was absent from an earlier run (but is present
  now) is included today, and a node that is unreachable (`docker exec`
  fails) causes that node's `mkdir`/`chown` attempt to also fail,
  which is correctly treated as a hard failure (`ok_all=False`) rather
  than silently skipped - i.e. the script fails closed rather than
  reporting success while one node remains unhardened.
- **Partial-failure/rollback**: a failed propagation check triggers a
  best-effort revert of exactly the ConfigMap value and exactly the
  per-node root-directory states this run actually changed (captured
  before mutation, restored to the captured `(mode, gid)` tuple, with an
  explicit `chmod g-s` step documented as necessary because a bare
  numeric `chmod` was observed live not to reliably clear setgid on this
  environment) - a node that was already correctly hardened before the
  run is left untouched by the revert path. Restoration failures are
  recorded with an explicit `RESTORATION FAILURE:` prefix, never folded
  into a generic pass/fail count silently.

No defect was found in this code-review pass; this is a positive
finding, not a manufactured one - I looked specifically for the
"node absent during a previous run" and "partial failure mid-run" cases
the review brief called out, and both are handled correctly by
construction (live re-query, exact-state capture/revert).

## 6. Image build/load identity

Fresh evidence (`16-image-digests.txt`, `16b-image-digest-consistency.txt`):

- Docker Engine (host) image IDs for `:0.4.0` tags are genuine
  single-platform (`linux/amd64`) content-addressed config digests
  (e.g. `maops-kubernetes-app:0.4.0` -> `sha256:db24defd5adb...`,
  `RepoDigests=[maops-kubernetes-app@sha256:db24defd5adb...]`) - built
  via `IMAGE_BUILD_FLAGS := --platform linux/amd64 --provenance=false
  --sbom=false --load`, matching `Makefile`/`docs/architecture.md`'s
  documented fix. No OCI *index* exists for any of these images (they
  are never pushed to a registry in this project), so there is no
  separate "index digest" to compare against a "manifest digest" - the
  Docker Engine ID **is** the (single-platform) image config digest.
- **However**, `kind load docker-image` re-imports each image tarball
  into containerd on every node, which assigns its **own**, different
  image ID for the identical content (e.g. `maops-kubernetes-app:0.4.0`
  -> containerd ID `sha256:66a15ef2be0112947a...`, *not*
  `db24defd5adb...`), and registers it under a synthetic,
  non-registry-backed pseudo-reference
  (`docker.io/library/import-2026-09-08@sha256:1d6c8cc7e2264a69c0c3e1297d4e498e82966faa7fd50780dca227f767890c57`)
  rather than under `maops-kubernetes-app`. This was independently
  confirmed identical across all three nodes (control-plane, worker,
  worker2) via `crictl images -o json`, so it is not a
  node-inconsistency problem - see **DAY4-INT-2** below for why this is
  still worth recording precisely.
- All three running containers' actual `containerID`s
  (`kubectl get pods -o json`) trace back to these same
  containerd-side image IDs, confirmed via `crictl ps -a`/`crictl
  inspect` on the node the Pods actually run on - i.e. "what actually
  runs" was verified via the CRI/containerd layer, not assumed from the
  Docker Engine tag alone.

## 7. Timeout architecture across nested calls

Reviewed `scripts/kube.py` (`DEFAULT_TIMEOUT_SECONDS=30`,
`subprocess_timeout_for()` for any `--timeout=<n>s` kubectl-side bound),
`scripts/portforward.py` (bounded `_wait_connectable`, guaranteed
`finally`-block `_terminate`, SIGTERM-safe cleanup), and the Day
4-specific mutation scripts (`persistence_check.py`,
`retention_check.py`, `storage_bootstrap.py`,
`storage_hardening_check.py`). `grep -n "timeout=None\|while True"
scripts/*.py` found zero unbounded waits or infinite loops anywhere in
the script tree. `persistence_check.py` documents and fixes a
previously-live bug (`POD_DELETE_SUBPROCESS_TIMEOUT_SECONDS=90.0` plus
`--wait=false`) for a real race it found between
`terminationGracePeriodSeconds: 30` and the prior
`kube.DEFAULT_TIMEOUT_SECONDS: 30.0` default having zero margin. Every
`wait_until()` call site in the Day 4 scripts passes an explicit,
finite `timeout=`. No missing outer bound was found in the code
reviewed. (This closes cleanly on top of Day 3's `DAY3-INT-H2`, which
this project's own comments correctly still describe as the origin of
this architecture - I found no regression.)

## 8. Persistence/retention experiment design (code review; historical execution)

`scripts/persistence_check.py` and `scripts/retention_check.py` were
read in full (not re-executed - the review brief explicitly reserves the
mutating 1->0->1 / delete-and-reschedule proof to the historical,
already-hashed `day4-check.log`, dated 2026-09-08). Both scripts:

- Write/read the marker through the **real service chain**
  (`gateway /state -> app /internal/state -> state /state`) via a
  bounded, auto-cleaned-up port-forward to `service/maops-gateway`
  (`persistence_check.py`) or direct-Pod port-forwards for app/gateway
  during the state outage (`retention_check.py`, correctly scoped per
  the "direct-Pod port-forward is a temporary exception" rule, since a
  not-Ready Pod stops being a normal Service endpoint).
- Compare Pod UIDs (not just names) before/after deletion/rescheduling,
  and PVC/PV UIDs before/after, never re-reading a value already held in
  the script's own memory for the final readback proof.
- Guarantee restoration in a `finally` block on every exit path, with
  bounded retries for a real, live-observed propagation lag (app/gateway
  replicas' independent backend-health polling briefly lagging the state
  Pod's own Ready condition) - restoration failures are recorded with an
  explicit `RESTORATION FAILURE:`/`!!! RESTORATION FAILURE !!!` banner,
  never folded silently into the main pass count.

The historical `day4-check.log` (hash-verified above, `6e0fc45e...`)
shows both checks passing on 2026-09-08:
`PASS: persistence proof complete - data survived Pod
deletion/rescheduling` and `PASS: retention/outage behavior proven and
maops-state restored to 1/1 Ready`. **This is historical evidence,
re-confirmed only by hash in this session - it was not re-executed.**
The current live `state.json` value (`day4-retention-054e49df1b0a481c`,
read via smoke test and via direct read-only `stat`/hash inside the
container, see Section 10) is consistent with being the marker restored
at the end of that historical retention run - i.e. it is a leftover
test artifact, not a "clean" application-chosen baseline value. This is
expected given the sequential Makefile design (each mutating check
restores to "whatever was there when it started," which chains through
the whole `day4-check` sequence) and is not itself a defect - see
**DAY4-INT-3**.

## 9. Storage hardening positive/negative proof - not re-run this session

`scripts/storage_hardening_check.py` was read (not re-run - it creates a
disposable namespace/PVC/two probe Pods, which this review's read-only
scope avoids). The historical `day4-check.log` shows it passing
(`PASS: storage hardening verified (positive write + negative EACCES +
read-only-root-fs)`). **Smallest targeted experiment for a later
remediation/verification phase, if end-to-end re-confirmation of the
setup-script's *live* effect on a brand-new PVC (as opposed to the
configuration-level proof in Section 4) is ever needed again**: re-run
`make storage-hardening-check` exactly as designed - it already creates
and tears down its own scratch namespace/PVC/Pods in a guaranteed
`finally` block and leaves the application's own PVC untouched, so no
additional restoration steps beyond what the script itself already
performs would be required.

## 10. State.json read-only recheck (fresh evidence this session)

Read-only, via `/usr/bin/python3.11 -c ...` inside the running
`maops-state-0` container (`14-state-json-hash-recheck.txt`):

```
uid 10001 gid 10001 mode 0o600 size 44
sha256 0f1db84ed91783181683235e5e587c68d83c7d636991a01ced657a802809aee5
content b'{"value": "day4-retention-054e49df1b0a481c"}'
```

This **exactly matches** the SHA256 given in the review brief
(`0f1db84ed91783181683235e5e587c68d83c7d636991a01ced657a802809aee5`).
**Explicit scope limit, as instructed**: this hash equality demonstrates
continuity *between these two specific observation points* (the prior
report's observation and this session's fresh re-check) - it does
**not** by itself prove an earlier, unobserved state, and it does
**not** constitute a new live persistence/retention experiment. The
current `Bound` PVC/PV status (Section 3) is likewise current-state
confirmation, not a new mutating proof.

## 11. Preserved Day 3 behavior (non-mutating spot check only)

Per the hard constraints (no scaling, no Pod deletion, no rollout), Day
3's *dynamic* behaviors (scaling 3->4->3, rolling update/rollback,
PDB/Eviction) were **not** re-executed this session - re-running
`make scaling-check`/`rolling-update-check`/`pdb-check`/`dependency-check`
would each perform exactly the mutations the constraints prohibit.
Instead, the current *static* baseline was independently checked live
(`17-day3-preserved-behavior.txt`):

```
maops-app:     2 pods on worker2, 1 on worker  (skew 1, within maxSkew:1)
maops-gateway: 1 pod on worker2, 2 on worker    (skew 1, within maxSkew:1)
No gateway/app pods on the control-plane node.
maops-gateway-pdb / maops-app-pdb: minAvailable=2, disruptionsAllowed=1 (11-workload-state.txt)
No leftover `maops.io/rollout-test` annotation on either Deployment's Pod template.
```

This matches the expected healthy Day 3 baseline exactly. For the
*dynamic* proofs themselves (a real scale-up/down, a real rolling
update+rollback, a real Eviction-API rejection), this review relies on
the historical `day4-check.log`'s `PASS` lines for scaling,
rolling-update, and PDB checks (Section "restart timeline" evidence
file listing above) - **not re-derived from the manifest, but also not
freshly re-executed in this session**, consistent with the hard
constraints.

Preserved-per-instruction open debt (re-confirmed still open, not
independently re-opened or closed by this review, since no new evidence
either way was found this session): **DAY1-INT-I2, DAY2-INT-I1,
DAY3-SEC-I1, DAY3-TEST-L2, DAY3-REL-L1** (all confirmed still logged as
ACCEPTED/OPEN in `docs/engineering-reviews/day-03-v0.3-release-readiness.md`
and `day-03-post-release-verification.md`). **DAY1-REL-I1** remains
CLOSED (`VERSION` file = `0.4.0`, `scripts/version_check.py` present and
unchanged in mechanism); no regression evidence found. Day 3's
adjudicated fixes (e.g. `DAY3-INT-H2` timeout architecture,
`DAY3-INT-M2` node-name-regex anchoring, both still present verbatim in
`scripts/kube.py`) remain closed; no regression found.

## Restart timeline investigation

**All timestamps are UTC; Dhaka (+06:00) equivalent shown where useful.**

### Fresh evidence collected this session

- `02-docker-inspect-nodes.txt`: `docker inspect` `State.StartedAt` /
  `FinishedAt` / `RestartCount` / `OOMKilled` for the 3 Day 4 node
  containers and the 5 Day 1-3 node containers.
- `03-pods-raw.json` / `03b-pod-restart-detail.txt`: full Pod JSON,
  per-container `restartCount`, `state.running.startedAt`,
  `lastState.terminated.{reason,exitCode,startedAt,finishedAt}`.
- `04-events-nodeconditions.txt`: `kubectl get events` for both
  namespaces (empty - see gap below) and node `Conditions`.
- `05-crictl-inspect.txt`, `06-crictl-ps-a.txt`,
  `07-crictl-inspect-exited.txt`, `07b-full-inspect.json`: CRI-level
  inspection of the currently-running and the immediately-prior (exited)
  container instances on the worker node, including exit codes and
  reasons.
- `08-kubelet-journal-worker.txt`, `10-containerd-journal-worker.txt`:
  bounded `journalctl -u kubelet`/`-u containerd --since ... --until
  ...` reads on the affected node.
- `09-dmesg-worker-full.txt`: full `dmesg -T` ring-buffer contents on
  the worker node (WSL2-shared kernel, so this reflects the whole WSL VM,
  not a container-private kernel log).
- `21-second-snapshot.txt`: a second point-in-time snapshot taken ~7
  minutes after the first, to check for any restart activity during this
  review session itself.

### Timeline

1. **2026-09-10T02:56:25Z (08:56:25 +06:00)** - All eight kind node
   containers' Docker `State.StartedAt` cluster tightly around this
   second (02:56:25.7xx-02:56:25.9xx across all 8), confirming a single
   Windows/WSL2 host boot/Docker-daemon-restart event, not staggered
   individual container restarts. `RestartCount=0` for every node
   container (Docker's own restart counter never incremented) - i.e.
   this was the **containers being (re)started as part of Docker/WSL2
   coming back up**, not Docker restarting an already-running container.
   `dmesg -T` on the worker node shows kernel boot messages beginning at
   `Thu Sep 10 02:55:31 2026` (`09-dmesg-worker-full.txt`), corroborating
   a genuine WSL2 VM (re)boot immediately prior.
2. **~02:56:40-02:59Z** - `containerd.service` starts fresh on the
   worker node (`10-containerd-journal-worker.txt`, single "Starting
   containerd.service" line - containerd itself never restarted again
   during the incident window). kubelet repeatedly logs
   `grpc: addrConn.createTransport failed ... containerd.sock: ...
   no such file or directory` (02:56:44-02:56:51) while containerd is
   still coming up, then (02:57:45 onward) `Failed to ensure lease
   exists ... context deadline exceeded` talking to the API server, and
   `manager.go:1184 Failed to process watch event ... context deadline
   exceeded` for cgroup watch events repeatedly through ~03:02Z - all
   consistent with the whole 3-node cluster (API server, etcd,
   kubelet/containerd on 3 nodes, all workload Pods) restarting
   concurrently on a resource-constrained WSL2 VM, not a targeted
   single-component failure.
3. **All 7 platform containers'** `lastState.terminated` (the state
   *before* their current running instance) shows `reason=Unknown,
   exitCode=255`, with `startedAt=2026-09-09T11:15:26Z` (the prior day's
   session) and `finishedAt=~2026-09-10T02:56:55-56Z` - i.e. kubelet, on
   coming back up, found the pre-reboot container processes gone and
   recorded them as an "Unknown" abnormal termination at the moment it
   re-synced (not a real 15-hour-long termination event). This is the
   expected signature of **a full node-container/VM reboot wiping the
   previously-running containers**, not an application-level crash.
4. Most containers (`maops-app-9hpkw`, `maops-app-lkvwn`,
   `maops-gateway-2w9t7`, `maops-gateway-f2z9z`) came back up once, ~4-6
   minutes after the node containers restarted (`state.running.startedAt`
   between `03:00:55Z` and `03:02:20Z`), and have been stable since -
   this matches the review brief's reported "~10 minutes of Day 4
   container uptime, all three Day 4 nodes and seven platform Pods were
   Ready."
5. **A second, more localized restart wave** affected exactly 3 of the 7
   containers: `maops-app-d7xlk`, `maops-gateway-c478r`, and
   `maops-state-0` - all three (and only these three, plus one
   unaffected 4th Pod, `maops-gateway-f2z9z`) scheduled on
   **`maops-k8s-day4-worker`** (none of `worker2`'s 3 Pods were
   affected). Each of these three had a short-lived container instance
   that started at `03:01:00Z`-`03:01:14Z` and was killed
   (`reason=Error, exitCode=137`) 66-72 seconds later
   (`finishedAt` `03:02:06Z`-`03:02:18Z`), confirmed via both `kubectl
   get pod -o json` and `crictl inspect` on the node directly
   (`07-crictl-inspect-exited.txt`) - i.e. this is not a kubectl-reporting
   artifact, the CRI layer agrees. The container that replaced each of
   these (the one currently running) started immediately after
   (`03:02:06Z`-`03:02:35Z`) and has been stable since.
6. **OOM was explicitly checked and ruled out as the established cause**:
   `docker inspect` on all 3 Day 4 node containers shows
   `OOMKilled=false` (node-container level); `crictl inspect` on the
   specific exited container instances reports `reason: "Error"`, not
   the Kubernetes-specific `"OOMKilled"` reason kubelet sets when it
   detects a cgroup OOM kill; and a full `dmesg -T` capture on the
   affected node contains **zero** `oom`/`out of memory`/`killed
   process` entries anywhere in the retained ring buffer
   (`09-dmesg-worker-full.txt`). Exit code 137 (SIGKILL) is therefore
   confirmed **not** attributable to OOM on the evidence available, but
   its actual proximate trigger could not be established: the captured
   kubelet journal window contains **no** `"Killing container"` or
   probe-failure log lines for this window (`grep` returned 0 matches),
   so it cannot be confirmed whether kubelet itself issued the kill (e.g.
   via a probe timeout) or something else did.
7. **One temporally-coincident, unexplained systemd anomaly**: 
   `systemd-journald[118]: Time jumped backwards, rotating.` was logged
   on the worker node at `03:01:48Z` - inside the exact 03:01:00-03:02:18Z
   window of the second restart wave. This is consistent with, but does
   not conclusively prove, a WSL2 host-clock resynchronization event
   contributing to (or coinciding with) transient IPC/scheduling
   instability. Corroborating this window: `containerd` logged
   `ttrpc: received message on inactive stream` repeatedly from
   `02:58:59Z` through `03:02:19Z`, and kubelet logged
   `Failed to create existing container: ... context deadline exceeded`
   at `03:00:05Z` and `03:01:06Z` - both consistent with general
   resource/IPC contention on the WSL2 VM during the multi-node,
   multi-control-plane-component simultaneous restart, rather than a
   component-specific defect.
8. **No OOM, no second Docker-level container restart, no additional
   node reboot, no containerd restart** occurred - only Pod-level
   container restarts (kubelet-driven `RestartPolicy: Always`
   replacements) at the CRI layer.
9. **Stability since**: node conditions (`04-events-nodeconditions.txt`)
   show `Ready=True`/no `MemoryPressure`/`DiskPressure`/`PIDPressure` on
   all 3 nodes with `LastTransitionTime` at `08 Sep 2026 11:14:xx`
   (unrelated to this reboot - i.e. no condition has flapped since).
   Two point-in-time snapshots taken during this review session, 7
   minutes apart (`03-pods.txt` at 04:32 UTC and `21-second-snapshot.txt`
   at 04:39 UTC), show **identical** restart counts and **identical**
   Pod UIDs for all 7 containers, and identical (0) Docker-level
   `RestartCount` for all 3 node containers - **no new restarts occurred
   during this review session**.

### Timeline conclusion

The evidence supports **reboot/startup-recovery churn, confined to a
single ~6-minute window immediately following a genuine Windows/WSL2
host restart** (`02:56:25Z`-`03:02:35Z`), consisting of two related
sub-events: (a) the expected, universal "all containers found dead on
kubelet re-sync" churn affecting all 7 platform containers plus
`kube-proxy`/`kindnet` (also restart-count-incremented, confirming this
was cluster-wide, not app-specific), and (b) a second, more localized,
~70-second-lived SIGKILL (137) wave affecting 3 of 4 Pods specifically
on `maops-k8s-day4-worker` a few minutes later, temporally coincident
with a journald clock-jump message and a burst of containerd
`ttrpc`/kubelet `context deadline exceeded` errors, but whose exact
proximate trigger (probe timeout vs. some other kubelet-initiated kill
vs. an external signal) **could not be conclusively established from
available evidence** - this is an explicit, named evidence gap, not a
conclusion. This is **not** ongoing instability (two snapshots 7 minutes
apart during this review show zero new restarts) and **not** a later,
separate incident (all restart evidence clusters within the same
6-minute post-reboot window) - it settled by `03:02:35Z` and has not
recurred in the ~1h47m between that settling and this review's second
snapshot. The user's own reported sequence (Docker responding
`09:01:02 +06:00`, ~4 min container uptime; user then stopping the five
Day 1-3 containers; ~10 min Day 4 uptime with 7 Pods Ready; restart
counts 14/16/15, 14/16/18, 17) is fully corroborated by this
independently-collected fresh evidence, including the exact restart
counts.

### Explicit evidence gaps / unresolved questions

- **Kubernetes events for both `maops-platform` and `kube-system` had
  already rotated out (empty) by the time of this review** (~1h47m
  after the incident settled) - the default event TTL (~1h) had already
  elapsed. This means no `Event` objects (e.g. `Killing`, `Unhealthy`,
  `BackOff`) survived for direct correlation; the timeline above is
  reconstructed entirely from `docker inspect`, `kubectl get pod -o
  json`, `crictl inspect`/`ps -a`, and bounded journal/dmesg reads. This
  is a genuine data-loss limitation, stated explicitly rather than
  papered over.
- **The proximate trigger for the second wave's `exitCode=137`s is not
  established.** No `OOMKilled` reason, no dmesg OOM entry, and no
  kubelet `"Killing container"`/probe-failure log line was found in the
  captured window for any of the 3 affected containers. A plausible but
  unconfirmed contributing factor is general WSL2-VM resource/IPC
  contention during the concurrent 3-node/control-plane restart,
  temporally correlated with (but not proven caused by) the observed
  `journald` clock-jump. **This should be read as "insufficient evidence
  to assign a specific cause," not as "confirmed benign."**
- **Why exactly 3 of 4 `worker`-node Pods were affected but not the
  4th** (`maops-gateway-f2z9z`, also on `worker`) is unexplained by
  available evidence.

## Findings

### DAY4-INT-1 - Second-wave container kills (exit 137) during boot recovery have an unconfirmed proximate cause
**Severity:** Low
**Evidence:** Section "Restart timeline" above; `03b-pod-restart-detail.txt`, `07-crictl-inspect-exited.txt`, `08-kubelet-journal-worker.txt`, `09-dmesg-worker-full.txt`, `10-containerd-journal-worker.txt`.
**Impact:** No current impact - all 7 workload containers are healthy,
Ready, and have shown zero restarts across two independent snapshots
taken during this review. No data loss occurred (PVC/PV Bound
throughout, per Section 3, and the historical persistence/retention
proof from Sept 8 remains hash-verified unchanged). However, restart
counts of 14-18 on containers that should ideally restart 0 times in
normal operation are a real, currently-unexplained signal that a
recurrence (especially the second, ~70-second-lived SIGKILL wave)
cannot currently be ruled out or diagnosed further, because the
supporting Kubernetes Events had already rotated out by the time any
reviewer investigated.
**Remediation (proposed, not performed):** (1) Add a lightweight,
non-mutating "capture kubelet/containerd journal + kubectl events to a
timestamped file" helper script the operator can run immediately after
any observed reboot/restart, before the ~1h event TTL elapses - this
requires no cluster mutation, only read access, and would close the
evidence gap for any future occurrence. (2) If this recurs, the smallest
targeted *diagnostic* (not remediation) experiment would be to
reproduce a controlled WSL2 shutdown/restart while a `journalctl -f -u
kubelet` and `journalctl -f -u containerd` capture is already running on
the affected node from before the restart, to catch the exact kill
trigger (probe failure vs. OOM vs. signal) in real time - this was
explicitly not attempted in this review per the "no Docker
restart"/"assessment only" constraints.

### DAY4-INT-2 - Containerd's post-`kind load docker-image` image identity diverges from the Docker Engine build-time digest
**Severity:** Informational
**Evidence:** Section 6; `16-image-digests.txt`, `16b-image-digest-consistency.txt`.
**Impact:** None currently - functionally verified correct (all 3
workloads Running/Ready, `make smoke`/`make state-check` both pass, and
the containerd-side image ID is identical and consistent across all 3
nodes). However, `docker inspect <image>:<tag>` on the host reports a
**different** digest than what containerd actually runs
(`sha256:db24defd5adb...` vs. `sha256:66a15ef2be01...`), and containerd
registers the loaded image under a synthetic, non-registry
`import-<date>@sha256:...` pseudo-reference rather than under the
`maops-kubernetes-app`/etc. repository name. Anyone relying on `docker
inspect` output alone to pin/verify "what's actually running in the
cluster" (e.g. for a future Day 6/7 registry-backed digest-pinning
policy) would be comparing against the wrong value.
**Remediation (proposed, not performed):** Document this divergence
explicitly in `docs/architecture.md`'s existing "storage preflight"/image
section (which already explains *why* `kind load docker-image` is used
instead of a manual `ctr import`), noting that runtime image identity
verification must go through `crictl images`/`crictl inspect` on a node,
not `docker inspect` on the host, whenever exact digest identity matters
(e.g. any future supply-chain/attestation work in a later day).

### DAY4-INT-3 - Live `state.json` currently holds a leftover test marker, not a clean/blank baseline
**Severity:** Informational
**Evidence:** Section 8, 10; `14-state-json-hash-recheck.txt`, `18-smoke-run.txt`.
**Impact:** None - this is expected, documented behavior of the
sequential `day4-check` Makefile design (each mutating check restores to
"whatever value was present when that check started," which chains
through the whole sequence), not a defect. Recorded here purely so a
future reader does not mistake the current value
(`day4-retention-054e49df1b0a481c`) for meaningful application data, and
so the hash-continuity claim in Section 10 is not over-read as
independent proof of state predating the historical Sept 8 mutating run.
**Remediation:** None required; informational only.

## Explicit limits of this review's claims

- This review proves Day 4's **currently-live, static** cluster state
  (topology, readiness, PVC/PV binding/identity, storage-provisioner
  configuration, image identity, Secret wiring, DNS) via fresh, real
  commands against the live cluster, plus **code review** (not
  re-execution) of the mutating persistence/retention/storage-hardening
  scripts.
- It does **not** constitute a fresh re-proof of the mutating
  1->0->1 persistence/retention cycle, the scaling 3->4->3 experiment,
  the rolling-update+rollback experiment, or the PDB/Eviction rejection
  - those remain proven only by the historical, hash-verified
  `day4-check.log` from 2026-09-08, per the explicit constraint against
  re-running `make day4-check` or any individual scaling/deletion/
  storage-bootstrap mutation this session.
- It does **not** establish state high availability, node-loss recovery,
  or cluster-loss recovery for `maops-state` - Day 4's own documented
  scope (`docs/architecture.md`, "worker-local storage and its limits")
  explicitly excludes all three, and this review found nothing that
  changes that scope boundary.
- The restart-timeline conclusion is bounded by what survived Kubernetes'
  default Event TTL and the retained kubelet/containerd journal/dmesg
  window at the time of this review (~1h47m post-incident) - the
  proximate cause of the second SIGKILL wave is explicitly **not**
  established, not merely deprioritized.
- No mutation of the cluster or the repository was performed by this
  review beyond the two Makefile targets explicitly run
  (`context-check`, `state-check`) plus `smoke` (which only performs
  GETs against `/`, `/livez`, `/readyz`, `/config`, `/backend`, `/state`)
  - all three are non-mutating with respect to workload replica counts,
  Pod identity, PVC/PV state, or the persisted record's *value* (the
  `/state` GET in `smoke.py` never writes).

## Overall integration verdict

**PASS WITH FINDINGS** (all Informational/Low; zero Critical/High/Medium
findings from this review).

Day 4's actual, in-scope claim - a single-replica StatefulSet with a
PVC-backed volume, worker-pinned storage, hardened provisioner
permissions, and a real (historically-proven, hash-verified) Pod
replacement + 1->0->1 persistence/retention cycle, layered cleanly on
top of an unchanged, still-healthy Day 3 gateway/app baseline - is
independently supported by both fresh live-cluster evidence collected
in this session and the untouched, hash-verified historical
`day4-check.log`. All exact identity values given in the review brief
(container IDs, PVC UID, PV UID, provisioning-root/state.json
permissions and hash) were independently re-verified and matched
exactly. No unresolved Critical/High/Medium finding exists. The two
Informational findings and one Low finding above are non-blocking for
Day 4's stated scope but are recorded prominently per the review
brief's instruction not to imply "fine" without an explicit severity
tag.

## Final verification (end of this review session)

```
122 original candidate entries: re-hashed, 0 mismatches, 0 missing (identical to the start-of-session check)
day-04-kubernetes-architecture-review.md sha256: e6d9bcf1001e7c1a855d16b9644e27bbbdc1a855d05d9daf291e5aa98f1c7eb5 (unchanged)
day-04-kubernetes-security-review.md    sha256: d33f2b13cd31e06fc3e231106750b6e4a80f2b71e21d605bcfb5da2b9c51e946 (unchanged)
This report is the ONLY new file added -> inventory now 125 (verified via `git status --porcelain=v1 -uall` before writing this file; no other file was touched).
Branch: feature/day-4-stateful-persistence (unchanged)
HEAD: aa2049876c7be2b959acb6e2a1d20f979ee440bc (unchanged)
v0.3.0 tag object: 2bdd742d2f4a82387ef5a3b61b8f42f8e9ed06c1 -> 9fc7fe9f25d729d76317de85b5722e84271234f0 (unchanged)
`ps aux | grep port-forward`: no matching process (no leaked port-forward from this session)
Second Day 4 Pod/restart-count snapshot (21-second-snapshot.txt, 04:39 UTC) identical to the first (03-pods.txt, 04:32 UTC) - no cluster mutation occurred during this review.
```

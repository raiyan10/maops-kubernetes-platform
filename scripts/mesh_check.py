#!/usr/bin/env python3
"""
DAY6 (four times extended after independent review): proves Istio
ambient mesh behavior against the LIVE cluster - ztunnel/istiod/
istio-cni health, ambient enrollment with no sidecars, strict mTLS, that
the live AuthorizationPolicy objects actually carry the exact
identity-scoped principals this project's design requires (not just
that the chart's templates render them correctly -
`scripts/helm_check.py`'s job - but that `helm upgrade --install`
actually applied them as-is to the live cluster), that the ALLOWED
identity paths genuinely still work, and that an ambient-enrolled but
UNAUTHORIZED ServiceAccount identity is denied specifically by Istio's
AuthorizationPolicy layer - not merely "denied for some reason that
could be either Cilium or Istio."

Runs only against a live cluster (never part of the cluster-free
`make ci-check`); see docs/architecture.md's "DAY6: live validation
record" for the recorded Day 6 run and its denial-evidence tier
breakdown.

Tool constraint (this project's ground rules): only `docker`, `kubectl`,
`kind`, and `helm` are expected local CLIs - this script never shells
out to `istioctl`, even for the isolated-denial proof below (which uses
`kubectl set env`/`kubectl patch`/`kubectl logs` against the ztunnel
DaemonSet, not `istioctl proxy-config`/`istioctl x describe`). Every
check here is a `kubectl get -o json` read, a `kubectl logs`/`kubectl
set env` call against a standard DaemonSet, or a `kubectl exec` probe
against a real Pod - the same primitives every other live script in
this project already uses.

Classification discipline: reuses `scripts/networkpolicy_check.py`'s
phase-attributed probe model (`_run_probe`/`_tcp_connect_probe_snippet`)
rather than a second, divergent copy of it - a `kubectl` call/exec that
itself fails or times out is its own explicit INCONCLUSIVE state, never
silently treated as proof of a policy denial, exactly as in that
module.

DAY6 THIRD REMEDIATION (kept for history) - isolating the
AuthorizationPolicy-specific denial: added `check_authorization_denial_isolated()`,
which temporarily raised ztunnel's log level, re-ran the wrong-identity
connection attempts, and correlated ztunnel's own logs against the
probe's principal and each destination - replacing the second
remediation's "combined Cilium+Istio, cannot isolate which layer"
scope boundary.

DAY6 FOURTH REMEDIATION (this revision) - an independent review of the
third remediation found four remaining gaps, all fixed here:

  1. TRANSACTIONAL ZTUNNEL MUTATION. The third remediation's
     `_set_ztunnel_env()` combined "submit the `kubectl set env` change"
     and "wait for the resulting rollout" into one function with one
     return value - and the caller returned early (skipping restoration
     entirely) whenever that combined call reported failure, even
     though a rollout-wait failure happens strictly AFTER the mutation
     was already accepted by the API. That is a real bug: a diagnostic
     rollout that timed out could leave ztunnel's log level mutated
     forever. This revision separates submission
     (`_submit_ztunnel_env_literal`/`_submit_ztunnel_env_unset`), rollout
     completion (`_wait_ztunnel_rollout`), diagnostic work, and
     restoration (`_restore_ztunnel_log_env`) into four distinct steps.
     Once submission succeeds, restoration runs in a guaranteed
     `finally` no matter what happens next - a diagnostic rollout
     timeout, a failed diagnostic Pod, a log-retrieval failure, a
     parsing failure, a probe raising, or any later step returning
     early. Restoration itself captures the COMPLETE original env entry
     (`ZtunnelLogEnvState`: absent / literal `value` / `valueFrom`), and
     - since `kubectl set env` cannot restore a `valueFrom` reference
     exactly - refuses to mutate at all if the original entry used
     `valueFrom` (fail BEFORE mutation, the simpler and safer of the two
     options this remediation's own instructions allow, since a
     `kubectl patch`-based `valueFrom` restore was never live-tested). After restoration, it waits for that rollout too,
     REREADS the DaemonSet, verifies `RUST_LOG` exactly matches its
     original representation, and verifies every ztunnel Pod is Ready -
     any uncertainty at any of those steps is its own explicit
     RESTORATION FAILURE finding.
  2. NOTFOUND VS API FAILURE. The third remediation's `_namespace_exists()`/
     `_resource_exists()` (here) and `_pod_exists()`
     (networkpolicy_check.py) were bare booleans keyed off `returncode
     == 0` - conflating "confirmed NotFound" with "the API call itself
     failed for some other reason" (connection refusal, timeout,
     Forbidden, an auth failure, unparseable output). A cleanup loop
     built on that boolean could not tell "genuinely gone" from
     "couldn't tell", and would either falsely declare success or spin
     forever. This revision replaces those booleans with a tri-state
     `_resource_state()` (EXISTS/NOT_FOUND/API_ERROR), built on
     `kubectl get ... --ignore-not-found` - a flag documented
     specifically to make this distinction (exit 0 + empty stdout means
     genuinely absent; exit 0 + stdout means present; any nonzero exit
     is some other, unresolved API error). Only NOT_FOUND proves
     deletion; API_ERROR immediately fails the cleanup it's checked
     from, never silently retried-through as if it might still resolve
     to "gone". Applied to `delete_namespace_and_verify_gone()` here,
     `delete_pod_and_verify_gone()` in networkpolicy_check.py, and
     `final_state_check.py`'s mesh-probe leak check.
  3. DOCUMENTED ZTUNNEL ACCESS-LOG FIELDS. The third remediation's
     `ALLOW_TRANSPORT_LOG_MARKERS`/`DENY_LOG_MARKERS` were GUESSED
     free-text tokens ("mtls", "established", "accept", "handshake" for
     transport; "rbac", "deny", ... for denial) with no claim to match
     ztunnel's actual log format. This revision replaces the transport
     side with a real field parser (`_parse_access_log_fields()`) keyed
     on ztunnel's documented structured access-log fields: the literal
     markers `access` and `connection complete`, and the `key="value"`/
     `key=value` fields `src.identity`, `dst.identity`, `dst.hbone_addr`,
     `dst.service`, and `direction`. The probe's identity is represented
     in BOTH forms this project uses: the AuthorizationPolicy principal
     form (`cluster.local/ns/.../sa/...`, `MESH_PROBE_PRINCIPAL`,
     unchanged) and the SPIFFE log form
     (`spiffe://cluster.local/ns/.../sa/...`, `MESH_PROBE_SPIFFE_IDENTITY`)
     - transport proof (`_find_transport_evidence()`) matches
     `src.identity` against the SPIFFE form specifically, since that is
     the form ztunnel's own logs actually carry. For denial evidence,
     this (fourth) remediation had not yet observed Istio 1.31's exact
     deny-log format, so it introduced only the CANDIDATE (correlated
     documented fields) and BEST_EFFORT (free-text) tiers. The fifth
     remediation below, made after the first live run, added the
     AUTHORITATIVE tier for the two exact denial strings actually
     observed, and stopped filtering on `connection complete` (ztunnel
     emits it for rejected connections too) - see
     `_find_denial_evidence()` for the current tiering. Finding neither is recorded as INCONCLUSIVE, never as a
     confident "not denied". Per this remediation's explicit preference,
     `check_authorization_denial_isolated()` now tries the DEFAULT
     (unmutated) ztunnel log level FIRST - if it already contains
     identity/HBONE transport evidence for every target, the RUST_LOG
     mutation is skipped entirely; the mutation path (fully
     transactional, per item 1) is retained only as a fallback.
  4. CLIENT-SIDE MESH DENIAL. The third remediation's wrong-identity
     client-side probe used `_assert_denied()`, which (correctly, for
     NetworkPolicy purposes) accepts only `TCP_CONNECT_TIMEOUT` - but
     ztunnel may legitimately actively reset/close a denied L4
     connection rather than silently timing it out, which
     `networkpolicy_check.py`'s classification model reports as
     `CONNECTION_REFUSED_OR_RESET`, a state `assert_denied()` fails.
     `networkpolicy_check.assert_denied()` itself is UNCHANGED and
     stays strict (`TCP_CONNECT_TIMEOUT` only) for its own
     NetworkPolicy-specific callers. This script now uses a SEPARATE
     mesh-authorization outcome model
     (`_record_client_side_supporting_evidence()`) - see the FIFTH
     REMEDIATION below for how this model was itself corrected after a
     live run.

DAY6 FIFTH REMEDIATION (this revision) - the first actual live
`make mesh-check` run against `maops-k8s-day6` found the fourth
remediation's client-side model still wrong in one way, and confirmed
enough of its own design to promote two previously-hedged findings to
authoritative status:

  1. TCP_CONNECTED IS NOT AUTHORIZATION SUCCESS, EVEN AS A HARD FAILURE
     SIGNAL. The fourth remediation's `_record_client_side_supporting_evidence()`
     treated a client-side `TCP_CONNECTED` result as a hard, gating
     failure ("the wrong identity's connection unexpectedly succeeded").
     Live, all three wrong-identity targets reported `TCP_CONNECTED` on
     the raw TCP probe - not because the unauthorized identity reached
     the application, but because in ambient mode a source Pod's raw
     `connect()` completes against its OWN NODE-LOCAL ztunnel (which
     transparently intercepts the outbound connection) before the
     destination-side HBONE tunnel and AuthorizationPolicy evaluation
     ever run. `TCP_CONNECTED` from this probe therefore proves only
     that the local ztunnel accepted the client socket - relabeled
     `LOCAL_ZTUNNEL_CONNECT_ACCEPTED` in this script's own output - never
     that the request reached, or was authorized to reach, the
     destination application. `_record_client_side_supporting_evidence()`
     no longer hard-fails on any raw-TCP outcome; every one becomes
     non-gating SUPPORTING evidence, exactly like `TCP_CONNECT_TIMEOUT`/
     `CONNECTION_REFUSED_OR_RESET` already were.
  2. THE ACTUAL APPLICATION-REACHABILITY LEAK CHECK MOVED TO HTTP.
     Proving "the unauthorized identity never actually reached the
     application" now needs an HTTP-level probe, not a TCP-level one -
     `_record_client_side_http_leak_check()` reuses
     `networkpolicy_check._http_probe_snippet()` against each
     destination's `/livez` path. HTTP 200 is the one hard, gating
     failure this function reports (proof the request was actually
     served); a ztunnel-generated HTTP 401 (or any other non-200
     status), a reset/closed connection, a read timeout, or an
     INCONCLUSIVE probe are all non-gating SUPPORTING evidence only -
     consistent with item 1, no client-side result, TCP or HTTP, may
     ever alone satisfy the identity-denial assertion.
  3. TWO ISTIO 1.31 DENIAL FORMS PROMOTED FROM BEST_EFFORT TO
     AUTHORITATIVE. The fourth remediation's `_find_denial_evidence()`
     deliberately refused to claim any exact Istio 1.31 deny-log format,
     since none had been independently observed. This live run observed
     ztunnel emit BOTH of the following, correlated (exact wrong
     `src.identity`, the intended destination, HBONE fields) to all
     three wrong-identity targets:
       - `error="connection closed due to policy rejection: allow
         policies exist, but none allowed"` - an explicit, unambiguous
         structured policy-rejection message. Correlated, this alone is
         now AUTHORITATIVE denial evidence.
       - `error="http status: 401 Unauthorized"`, always co-occurring
         with `bytes_sent=0 bytes_recv=0` - i.e. the HBONE tunnel was
         rejected before any application byte ever flowed in either
         direction. This exact error string is AUTHORITATIVE denial
         evidence ONLY when correlated AND `bytes_sent`/`bytes_recv` are
         both exactly `0` - a 401 that exchanged nonzero application
         bytes is a materially different (and NOT this remediation's
         verified) situation, and is never promoted past `CANDIDATE`.
     Any OTHER error string, or an uncorrelated generic "401"/"denied"
     substring anywhere in the logs, remains exactly what the fourth
     remediation already called it - `CANDIDATE` (structured but
     unrecognized) or `BEST_EFFORT` (free-text, explicitly unverified) -
     never silently upgraded to AUTHORITATIVE. `_record_log_correlation()`
     records an `AUTHORITATIVE` finding as a confirmed PASS with no
     hedging language; `CANDIDATE`/`BEST_EFFORT`/`NONE` keep their
     existing fourth-remediation wording unchanged.

Proves:
  - ztunnel is Ready on every node, istiod and istio-cni are healthy
    (delegates to scripts/mesh_status.py's own checks).
  - maops-gateway/maops-app/maops-state Pods carry no `istio-proxy`
    sidecar container (ambient mode has no sidecar at all) and DO carry
    the ambient CNI's own redirection-enabled annotation
    (`ambient.istio.io/redirection: enabled`, Istio's own documented
    ambient enrollment mechanism) - the documented evidence a Pod was
    actually picked up by ztunnel, not just that its namespace carries
    the enrollment label.
  - PeerAuthentication in maops-platform is STRICT, read live (not
    merely rendered by the chart).
  - The three live AuthorizationPolicy objects (maops-gateway-authz,
    maops-app-authz, maops-state-authz) carry exactly the intended
    ALLOW-only source principals - the Istio ingress Gateway identity
    for gateway, maops-gateway for app, maops-app for state - and never
    the diagnostics/validation-client identity anywhere.
  - The allowed production identity paths still genuinely work under
    ambient + strict mTLS (gateway -> app, app -> state both
    TCP_CONNECTED).
  - A temporary, ambient-enrolled probe Pod - its own dedicated
    ServiceAccount, never one of the allowed principals above - is
    denied reaching gateway, app, AND state, with correlated ztunnel
    log evidence (built on documented access-log fields, per item 3
    above) isolating that denial to Istio's AuthorizationPolicy layer
    specifically (not merely "denied by something", and not merely "the
    client observed a raw TCP outcome" - a raw client-side `connect()`
    completing only proves the LOCAL ztunnel accepted the socket in
    ambient mode, never that the destination authorized the request;
    see the fifth remediation above). Application-reachability is
    instead checked at the HTTP layer (`/livez`) - an HTTP 200 there is
    a hard failure, proof the unauthorized request was actually served.
    Every probe/cleanup step is bounded; INCONCLUSIVE outcomes never
    count as denial proof. The temporary namespace/ServiceAccount/Pod
    deletion is submitted AND verified actually gone via an explicit
    NOT_FOUND response (never merely `--wait=false`, and never an API
    error mistaken for success - item 2 above), in a guaranteed cleanup
    path.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube
import mesh_status
import networkpolicy_check as netpol
from cluster_check import get_pods
from kube import APP_LABEL_SELECTOR, GATEWAY_LABEL_SELECTOR, STATE_LABEL_SELECTOR

# `ambient.istio.io/redirection` is Istio's own DOCUMENTED enrollment
# annotation, added by the ambient CNI plugin to every Pod it has
# actually redirected into the mesh - not an unverified guess. `make
# mesh-check` confirms, on every live run, that THIS project's pinned
# Istio 1.31.0/Cilium 1.20.1 combination actually sets it on every
# application Pod.
AMBIENT_REDIRECTION_ANNOTATION = "ambient.istio.io/redirection"
AMBIENT_REDIRECTION_VALUE = "enabled"
SIDECAR_CONTAINER_NAME = "istio-proxy"

EXPECTED_AUTHZ = {
    "maops-gateway-authz": {"component": "gateway", "principal": f"cluster.local/ns/{kube.INGRESS_NAMESPACE}/sa/{kube.GATEWAY_PROXY_SERVICE_ACCOUNT}"},
    "maops-app-authz": {"component": "app", "principal": f"cluster.local/ns/{kube.NAMESPACE}/sa/{kube.GATEWAY_SERVICE_ACCOUNT}"},
    "maops-state-authz": {"component": "state", "principal": f"cluster.local/ns/{kube.NAMESPACE}/sa/{kube.APP_SERVICE_ACCOUNT}"},
}
DIAGNOSTICS_PRINCIPAL = f"cluster.local/ns/{kube.VALIDATION_NAMESPACE}/sa/{kube.DIAGNOSTICS_SERVICE_ACCOUNT}"

# DAY6: authenticated wrong-identity proof. A dedicated, temporary,
# AMBIENT-ENROLLED namespace (never the normal maops-day6-validation
# namespace, which stays deliberately non-ambient/untrusted for the
# RBAC/NetworkPolicy checks that use it - see docs/architecture.md) -
# ambient enrollment here is the whole point: it is what gives this
# probe Pod a REAL mTLS identity via ztunnel, so a denial actually
# exercises the identity-authorization layer rather than being
# trivially explained by "it never spoke mTLS at all" (a plaintext
# rejection, a materially different, weaker proof this script must
# never present as the stronger one).
MESH_PROBE_NAMESPACE = "maops-day6-mesh-probe"
MESH_PROBE_SERVICE_ACCOUNT = "maops-day6-wrong-identity"
MESH_PROBE_PRINCIPAL = f"cluster.local/ns/{MESH_PROBE_NAMESPACE}/sa/{MESH_PROBE_SERVICE_ACCOUNT}"
# DAY6 fourth remediation item 3: the SAME identity, in the SPIFFE form
# ztunnel's own access logs actually carry (`spiffe://` + the
# AuthorizationPolicy principal form above) - transport-layer log
# correlation matches against this form specifically, never the bare
# principal form, which never appears in ztunnel's logs.
MESH_PROBE_SPIFFE_IDENTITY = f"spiffe://{MESH_PROBE_PRINCIPAL}"
POD_DEADLINE_SECONDS = netpol.POD_DEADLINE_SECONDS
# DAY6 remediation item 3 (third remediation): bounded wait for the
# temporary namespace to actually disappear after deletion - Namespace
# teardown (finalizer-driven cascade delete of everything inside it) is
# slower than a single Pod's, so this gets a longer budget than
# netpol.DELETE_VERIFY_TIMEOUT_SECONDS.
NAMESPACE_DELETE_VERIFY_TIMEOUT_SECONDS = 90.0
_VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
PROBE_IMAGE = f"maops-kubernetes-app:{_VERSION}"
MESH_PROBE_POD_NAME = f"mesh-wrong-identity-{uuid.uuid4().hex[:10]}"

GATEWAY_FQDN = f"maops-gateway.{kube.NAMESPACE}.svc.cluster.local"
APP_FQDN = f"maops-app.{kube.NAMESPACE}.svc.cluster.local"
STATE_FQDN = f"maops-state.{kube.NAMESPACE}.svc.cluster.local"

# (fqdn, short target label, destination-matching substring used
# against ztunnel's dst.service/dst.hbone_addr log fields).
_TARGETS = (
    (GATEWAY_FQDN, "gateway", "maops-gateway"),
    (APP_FQDN, "app", "maops-app"),
    (STATE_FQDN, "state", "maops-state"),
)

# DAY6: ztunnel diagnostic logging, bounded and always restored - see
# module docstring (fourth remediation item 1) for the full
# transactional design.
ZTUNNEL_NAMESPACE = kube.ISTIO_NAMESPACE
ZTUNNEL_DAEMONSET = "ztunnel"
ZTUNNEL_LOG_ENV_VAR = "RUST_LOG"
ZTUNNEL_DIAGNOSTIC_LOG_VALUE = "info,access_log=info"
ZTUNNEL_ROLLOUT_TIMEOUT_SECONDS = 120

# DAY6 fourth remediation item 3: documented ztunnel access-log
# structure - the literal markers an access-log line carries, and the
# structured `key="value"`/`key=value` field names this script parses
# out of it. See `_parse_access_log_fields()`/`_find_transport_evidence()`.
ACCESS_LOG_MARKER = "access"
CONNECTION_COMPLETE_MARKER = "connection complete"
# Best-effort, explicitly unverified-format tokens used only as a
# fallback when no field-structured denial candidate is found - see
# `_find_denial_evidence()`'s docstring for why this is deliberately
# NOT presented as a confident match against Istio 1.31's actual deny
# format.
_BEST_EFFORT_DENY_TOKENS = ("rbac", "denied", "denial", "unauthorized", "rejected", "forbidden")

# DAY6 fifth remediation item 5: the two Istio 1.31 ztunnel denial
# `error="..."` values actually observed, live, correlated to the exact
# wrong SPIFFE identity and intended destination on all three
# wrong-identity targets. Promoted to AUTHORITATIVE status in
# `_find_denial_evidence()` - never a guess, an exact match against what
# this project's own pinned Istio 1.31.0/Cilium 1.20.1 combination has
# actually been observed to emit on this cluster.
AUTHORITATIVE_POLICY_REJECTION_ERROR = "connection closed due to policy rejection: allow policies exist, but none allowed"
AUTHORITATIVE_HBONE_401_ERROR = "http status: 401 Unauthorized"

# DAY6 fifth remediation item 4: the client-side application-reachability
# leak check now probes this HTTP path (never the raw TCP layer alone -
# see the fifth remediation section of this module's docstring for why
# a raw TCP_CONNECTED is not authorization success in ambient mode).
LIVEZ_PATH = "/livez"
_HTTP_STATUS_RE = re.compile(r"STATUS=(\d+)")

# DAY6 fourth remediation item 2: tri-state resource-existence results.
# Only NOT_FOUND proves deletion; API_ERROR must never be silently
# folded into "gone" or retried through as if it might still resolve to
# NOT_FOUND.
EXISTS = "EXISTS"
NOT_FOUND = "NOT_FOUND"
API_ERROR = "API_ERROR"

# DAY6 fourth remediation item 4: client-side outcomes plausible for an
# actively-reset-or-closed mesh denial - SUPPORTING evidence only, never
# sufficient alone to satisfy the identity-denial assertion. See
# `_record_client_side_supporting_evidence()`.
_MESH_DENIAL_SUPPORTING_STATES = (netpol.TCP_CONNECT_TIMEOUT, netpol.CONNECTION_REFUSED_OR_RESET)

results: list[tuple[bool, str]] = []


@dataclass(frozen=True)
class ZtunnelLogEnvState:
    """DAY6 fourth remediation item 1: the COMPLETE original
    representation of ztunnel's RUST_LOG env entry, captured before any
    mutation. `found_daemonset=False` means the DaemonSet itself could
    not be read at all. `present=False` means the entry did not exist on
    the container (the normal starting state) - restoration then means
    REMOVING it, not setting an empty string. `uses_value_from=True`
    means the entry was set via `valueFrom` (ConfigMapKeyRef/
    SecretKeyRef/FieldRef/etc.) - `kubectl set env` can only set a
    literal or remove an entry, so it cannot restore this exactly;
    `check_authorization_denial_isolated()` refuses to mutate RUST_LOG at
    all in that case, choosing to fail BEFORE mutation rather than rely
    on an unverifiable exact-patch restore."""

    found_daemonset: bool
    present: bool
    value: str | None
    uses_value_from: bool
    raw_entry: dict | None


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def check_mesh_component_health() -> None:
    mesh_status.results = []
    node_count = mesh_status.check_node_count()
    mesh_status.check_istiod()
    mesh_status.check_daemonset(kube.ISTIO_CNI_LABEL_SELECTOR, "istio-cni-node DaemonSet", node_count)
    mesh_status.check_daemonset(kube.ZTUNNEL_LABEL_SELECTOR, "ztunnel DaemonSet", node_count)
    results.extend(mesh_status.results)


def _is_ambient_enrolled(pod: dict) -> tuple[bool, bool]:
    """Returns (no_sidecar, has_redirection_annotation)."""
    containers = pod.get("spec", {}).get("containers", [])
    container_names = {c.get("name") for c in containers}
    annotations = pod.get("metadata", {}).get("annotations", {}) or {}
    return (
        SIDECAR_CONTAINER_NAME not in container_names,
        annotations.get(AMBIENT_REDIRECTION_ANNOTATION) == AMBIENT_REDIRECTION_VALUE,
    )


def check_ambient_enrollment(label_selector: str, workload: str) -> None:
    pods = get_pods(label_selector)
    if not pods:
        record(False, f"{workload}: no Pods found to check ambient enrollment")
        return
    for pod in pods:
        name = pod.get("metadata", {}).get("name", "?")
        no_sidecar, redirected = _is_ambient_enrolled(pod)
        record(no_sidecar, f"{workload} Pod {name}: no {SIDECAR_CONTAINER_NAME!r} sidecar container (ambient has none)")
        record(
            redirected,
            f"{workload} Pod {name}: {AMBIENT_REDIRECTION_ANNOTATION} (Istio's documented ambient enrollment annotation) == {AMBIENT_REDIRECTION_VALUE!r} - evidence ztunnel actually redirected this Pod",
        )


def _get_json(*args: str) -> dict | None:
    result = kube.run(*args, "-o", "json", check=False, timeout=15.0)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        record(False, f"INCONCLUSIVE: kubectl {' '.join(args)} failed: {stderr if stderr else result}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        record(False, f"INCONCLUSIVE: kubectl {' '.join(args)} returned unparseable JSON: {exc}")
        return None


def check_peer_authentication_strict() -> None:
    pa = _get_json("-n", kube.NAMESPACE, "get", "peerauthentication", "maops-platform-strict-mtls")
    if pa is None:
        return
    mode = pa.get("spec", {}).get("mtls", {}).get("mode")
    record(mode == "STRICT", f"live PeerAuthentication mtls.mode == {mode!r} (expected STRICT)")


def _principals(policy: dict) -> set[str]:
    principals: set[str] = set()
    for rule in policy.get("spec", {}).get("rules", []):
        for source in rule.get("from", []):
            principals |= set(source.get("source", {}).get("principals", []))
    return principals


def check_authorization_policies() -> None:
    all_principals: set[str] = set()
    for name, expected in EXPECTED_AUTHZ.items():
        policy = _get_json("-n", kube.NAMESPACE, "get", "authorizationpolicy", name)
        if policy is None:
            continue
        selector_component = policy.get("spec", {}).get("selector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        record(selector_component == expected["component"], f"live AuthorizationPolicy/{name} selector component == {selector_component!r} (expected {expected['component']!r})")
        principals = _principals(policy)
        all_principals |= principals
        record(principals == {expected["principal"]}, f"live AuthorizationPolicy/{name} principals == {principals} (expected {{{expected['principal']!r}}})")

    record(DIAGNOSTICS_PRINCIPAL not in all_principals, f"diagnostics/validation-client identity ({DIAGNOSTICS_PRINCIPAL!r}) never appears as a live-allowed principal")


def _assert_connected(result: "netpol.ProbeResult", label: str) -> bool:
    """Local wrapper around netpol's phase-attributed classification
    that records into THIS module's own `results` list (never netpol's)
    - avoids depending on netpol's module-level list state/ordering."""
    return record(result.outcome in (netpol.TCP_CONNECTED, netpol.HTTP_RESPONSE), f"{label} [{result.outcome}]: {result.detail}")


def _record_client_side_supporting_evidence(result: "netpol.ProbeResult", label: str) -> None:
    """DAY6 fifth remediation (corrected after the first live run - see
    module docstring): a SEPARATE mesh-authorization outcome model from
    `networkpolicy_check.assert_denied()` (which stays strict/unchanged
    - TCP_CONNECT_TIMEOUT only - for its own NetworkPolicy-specific
    callers). This is the RAW TCP-connect probe's result - in ambient
    mode, a source Pod's `connect()` completes against its own NODE-
    LOCAL ztunnel before the destination-side HBONE tunnel/
    AuthorizationPolicy evaluation ever runs, so `TCP_CONNECTED` here
    proves only that the local ztunnel accepted the client socket
    (relabeled `LOCAL_ZTUNNEL_CONNECT_ACCEPTED` below) - NEVER that the
    request reached, or was authorized to reach, the destination
    application. Every raw-TCP outcome (`TCP_CONNECTED`,
    `TCP_CONNECT_TIMEOUT`, `CONNECTION_REFUSED_OR_RESET`, and both
    INCONCLUSIVE states) is therefore always recorded as non-gating
    SUPPORTING evidence - none of them, alone, ever satisfies OR
    refutes the identity-denial assertion. That assertion is made
    exclusively by `check_authorization_denial_isolated()`'s correlated
    ztunnel log evidence; the actual application-reachability leak check
    is the separate HTTP-level `_record_client_side_http_leak_check()`."""
    label_outcome = "LOCAL_ZTUNNEL_CONNECT_ACCEPTED" if result.outcome == netpol.TCP_CONNECTED else result.outcome
    is_plausible_denial_signal = result.outcome in _MESH_DENIAL_SUPPORTING_STATES
    suffix = "" if is_plausible_denial_signal else " - not a denial-shaped outcome, informational only"
    record(
        True,
        f"{label} [{label_outcome}] (SUPPORTING EVIDENCE ONLY - never by itself proves or disproves AuthorizationPolicy "
        f"denial; that assertion is made exclusively by correlated ztunnel log evidence): {result.detail}{suffix}",
    )


def _record_client_side_http_leak_check(result: "netpol.ProbeResult", label: str) -> None:
    """DAY6 fifth remediation item 4: the actual application-reachability
    leak check, at the HTTP layer against the destination's `/livez`
    path (using `networkpolicy_check._http_probe_snippet()` - the
    project's existing phase-separated HTTP probe building block).
    HTTP 200 is the one hard, GATING failure this function ever reports
    - proof the unauthorized identity's request was actually served by
    the application, directly contradicting this test's premise. Every
    other outcome - a ztunnel-generated HTTP 401 (or any other non-200
    status), an actively reset/closed connection, a read timeout, or an
    INCONCLUSIVE probe - is recorded as non-gating SUPPORTING evidence
    only; none of them, alone, may satisfy the identity-denial
    assertion, which remains exclusively
    `check_authorization_denial_isolated()`'s correlated ztunnel log
    evidence."""
    if result.outcome == netpol.HTTP_RESPONSE:
        match = _HTTP_STATUS_RE.search(result.detail)
        status = int(match.group(1)) if match else None
        if status == 200:
            record(
                False,
                f"{label}: unauthorized identity's request reached the application - HTTP 200 from {LIVEZ_PATH} "
                f"[{result.outcome}]: {result.detail}",
            )
            return
        record(
            True,
            f"{label} [{result.outcome} status={status}] (SUPPORTING EVIDENCE ONLY - a non-200 HTTP response, "
            "possibly ztunnel-generated (e.g. a policy-rejection 401), never by itself proves AuthorizationPolicy "
            f"denial; that assertion is made exclusively by correlated ztunnel log evidence): {result.detail}",
        )
        return
    record(
        True,
        f"{label} [{result.outcome}] (SUPPORTING EVIDENCE ONLY - never by itself proves AuthorizationPolicy denial; "
        f"that assertion is made exclusively by correlated ztunnel log evidence): {result.detail}",
    )


def check_allowed_identity_paths_succeed() -> None:
    """Positive control, required alongside the wrong-identity negative
    proof below: the mesh being STRICT + identity-scoped must not have
    accidentally broken the paths that ARE supposed to work. Uses the
    purpose-built TCP-connect-only probe (see networkpolicy_check.py) -
    the same reachability-only concern as the wrong-identity check."""
    gateway_pods = get_pods(GATEWAY_LABEL_SELECTOR)
    if not gateway_pods:
        record(False, "allowed-path check: no gateway Pods available to exec into")
    else:
        gw_pod = gateway_pods[0]["metadata"]["name"]
        result = netpol._run_probe(kube.NAMESPACE, gw_pod, netpol._tcp_connect_probe_snippet("maops-app", 8080, netpol.CONNECT_TIMEOUT_SECONDS))
        _assert_connected(result, "allowed identity path gateway -> app still CONNECTED under strict mTLS")

    app_pods = get_pods(APP_LABEL_SELECTOR)
    if not app_pods:
        record(False, "allowed-path check: no app Pods available to exec into")
    else:
        app_pod = app_pods[0]["metadata"]["name"]
        result = netpol._run_probe(kube.NAMESPACE, app_pod, netpol._tcp_connect_probe_snippet("maops-state", 8080, netpol.CONNECT_TIMEOUT_SECONDS))
        _assert_connected(result, "allowed identity path app -> state still CONNECTED under strict mTLS")


def _mesh_probe_namespace_manifest() -> str:
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {MESH_PROBE_NAMESPACE}
  labels:
    app.kubernetes.io/name: maops-kubernetes-platform
    app.kubernetes.io/instance: maops-kubernetes-platform-day6
    app.kubernetes.io/component: mesh-wrong-identity-probe
    app.kubernetes.io/part-of: maops-kubernetes-platform
    {kube.AMBIENT_DATAPLANE_MODE_LABEL}: {kube.AMBIENT_DATAPLANE_MODE_VALUE}
"""


def _mesh_probe_serviceaccount_and_pod_manifest() -> str:
    return f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: {MESH_PROBE_SERVICE_ACCOUNT}
  namespace: {MESH_PROBE_NAMESPACE}
automountServiceAccountToken: false
---
apiVersion: v1
kind: Pod
metadata:
  name: {MESH_PROBE_POD_NAME}
  namespace: {MESH_PROBE_NAMESPACE}
  labels:
    app.kubernetes.io/component: mesh-wrong-identity-probe
spec:
  serviceAccountName: {MESH_PROBE_SERVICE_ACCOUNT}
  restartPolicy: Never
  activeDeadlineSeconds: 120
  automountServiceAccountToken: false
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: {kube.CONTROL_PLANE_LABEL}
                operator: DoesNotExist
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: {PROBE_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["/usr/bin/python3.11"]
      args: ["-c", "import time; time.sleep(110)"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        requests: {{cpu: 25m, memory: 16Mi}}
        limits: {{cpu: 50m, memory: 32Mi}}
"""


def _apply(manifest: str) -> None:
    subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "apply", "-f", "-"],
        input=manifest,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _pod_if_running_or_terminal(namespace: str, pod_name: str) -> dict | None:
    result = kube.run("-n", namespace, "get", "pod", pod_name, "-o", "json", check=False)
    if result.returncode != 0:
        return None
    pod = json.loads(result.stdout)
    phase = pod.get("status", {}).get("phase")
    if phase in ("Running", "Failed", "Succeeded"):
        return pod
    return None


def _resource_state(kind: str, name: str, namespace: str | None = None) -> str:
    """DAY6 fourth remediation item 2: tri-state existence check -
    EXISTS / NOT_FOUND / API_ERROR. `--ignore-not-found` is kubectl's
    own documented mechanism for exactly this distinction: it makes
    `kubectl get` exit 0 with EMPTY stdout when the resource genuinely
    does not exist, and exit 0 with non-empty stdout when it does. A
    NONZERO exit under `--ignore-not-found` therefore cannot mean
    NotFound - it can only mean something else went wrong (connection
    refusal, timeout, Forbidden, an authentication failure, or any
    other API/transport error) - and is classified API_ERROR, never
    silently folded into "gone". Only an explicit NOT_FOUND proves
    deletion."""
    args = ["get", kind, name, "--ignore-not-found", "-o", "name"]
    if namespace is not None:
        args = ["-n", namespace, *args]
    result = kube.run(*args, check=False, timeout=15.0)
    if result.returncode != 0:
        return API_ERROR
    return EXISTS if result.stdout.strip() else NOT_FOUND


def delete_namespace_and_verify_gone(
    namespace: str,
    timeout: float = NAMESPACE_DELETE_VERIFY_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """DAY6 remediation item 3 (third remediation): submits Namespace
    deletion, then POLLS for its actual disappearance - never trusts
    `--wait=false` alone. DAY6 fourth remediation item 2: that poll now
    uses the tri-state `_resource_state()` rather than a boolean exists
    check - EXISTS keeps polling, NOT_FOUND is the only outcome that
    proves deletion, and any API_ERROR immediately fails cleanup (never
    retried-through as if it might still resolve to gone; a connection
    failure or a Forbidden partway through polling is not evidence the
    namespace was actually deleted). Returns (ok, detail). After
    confirming the namespace itself is gone, also independently
    re-checks that the specific probe Pod/ServiceAccount names no
    longer resolve (defense in depth beyond "the namespace is gone
    therefore everything in it must be too"), using the same tri-state
    check."""
    submit = subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if submit.returncode != 0:
        return False, f"could not submit deletion for namespace {namespace!r}: {submit.stderr.strip()}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _resource_state("namespace", namespace)
        if state == NOT_FOUND:
            break
        if state == API_ERROR:
            return False, (
                f"API_ERROR while polling for namespace {namespace!r} deletion - cannot confirm deletion "
                "(connection failure, timeout, or other API error, not an explicit NotFound response)"
            )
        time.sleep(2.0)
    else:
        return False, f"namespace {namespace!r} did not disappear within {timeout}s of deletion (possibly stuck Terminating)"

    leaked = []
    for kind, name in (("serviceaccount", MESH_PROBE_SERVICE_ACCOUNT), ("pod", MESH_PROBE_POD_NAME)):
        state = _resource_state(kind, name, namespace=namespace)
        if state == API_ERROR:
            return False, f"namespace {namespace!r} reported gone, but re-checking {kind}/{name} returned API_ERROR - cannot confirm it is actually gone"
        if state == EXISTS:
            leaked.append(f"{kind}/{name}")
    if leaked:
        return False, f"namespace {namespace!r} reported gone, but a direct re-check still found: {leaked}"

    return True, f"namespace {namespace!r} confirmed gone (explicit NOT_FOUND), no leaked ServiceAccount/Pod"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ztunnel_log_env_state() -> ZtunnelLogEnvState:
    """DAY6 fourth remediation item 1: captures the COMPLETE original
    RUST_LOG representation - not just a literal `.value`, but whether
    the entry was present at all and whether it used `valueFrom`."""
    ds = _get_json("-n", ZTUNNEL_NAMESPACE, "get", "daemonset", ZTUNNEL_DAEMONSET)
    if ds is None:
        return ZtunnelLogEnvState(False, False, None, False, None)
    containers = ds.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for env in container.get("env", []) or []:
            if env.get("name") == ZTUNNEL_LOG_ENV_VAR:
                return ZtunnelLogEnvState(True, True, env.get("value"), "valueFrom" in env, dict(env))
    return ZtunnelLogEnvState(True, False, None, False, None)


def _submit_ztunnel_env_literal(value: str) -> tuple[bool, str]:
    """DAY6 fourth remediation item 1: SUBMISSION ONLY - does not wait
    for the resulting rollout. Kept as its own separate step so a caller
    can distinguish "the mutation itself was never even accepted by the
    API" (safe to return without restoring - nothing changed) from "the
    mutation was accepted but something later failed" (restoration is
    now mandatory)."""
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "-n", ZTUNNEL_NAMESPACE, "set", "env", f"daemonset/{ZTUNNEL_DAEMONSET}", f"{ZTUNNEL_LOG_ENV_VAR}={value}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"kubectl set env daemonset/{ZTUNNEL_DAEMONSET} {ZTUNNEL_LOG_ENV_VAR}={value!r} failed: {result.stderr.strip()}"
    return True, f"kubectl set env submitted: {ZTUNNEL_LOG_ENV_VAR}={value!r}"


def _submit_ztunnel_env_unset() -> tuple[bool, str]:
    """DAY6 fourth remediation item 1: the restoration-only counterpart
    of `_submit_ztunnel_env_literal()`, used when the original RUST_LOG
    entry was absent entirely."""
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT, "-n", ZTUNNEL_NAMESPACE, "set", "env", f"daemonset/{ZTUNNEL_DAEMONSET}", f"{ZTUNNEL_LOG_ENV_VAR}-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False, f"kubectl set env daemonset/{ZTUNNEL_DAEMONSET} {ZTUNNEL_LOG_ENV_VAR}- failed: {result.stderr.strip()}"
    return True, f"kubectl set env submitted: unset {ZTUNNEL_LOG_ENV_VAR}"


def _wait_ztunnel_rollout() -> tuple[bool, str]:
    """DAY6 fourth remediation item 1: SEPARATE from submission - both
    the diagnostic mutation and the restoration reuse this same
    rollout-wait step, so a caller can tell submission success apart
    from rollout completion."""
    result = subprocess.run(
        [
            "kubectl", "--kubeconfig", kube.KUBECONFIG_PATH, "--context", kube.CONTEXT,
            "-n", ZTUNNEL_NAMESPACE, "rollout", "status", f"daemonset/{ZTUNNEL_DAEMONSET}",
            f"--timeout={int(ZTUNNEL_ROLLOUT_TIMEOUT_SECONDS)}s",
        ],
        capture_output=True,
        text=True,
        timeout=kube.subprocess_timeout_for(ZTUNNEL_ROLLOUT_TIMEOUT_SECONDS),
    )
    if result.returncode != 0:
        return False, f"ztunnel DaemonSet rollout did not complete: {result.stderr.strip()}"
    return True, "ztunnel DaemonSet rollout completed"


def _all_ztunnel_pods_ready() -> tuple[bool, str]:
    """DAY6 fourth remediation item 1: post-restoration verification
    that every ztunnel Pod is Ready - reuses mesh_status's own
    readiness predicate rather than a second copy of it."""
    pods = _get_json("-n", ZTUNNEL_NAMESPACE, "get", "pods", "-l", kube.ZTUNNEL_LABEL_SELECTOR)
    if pods is None:
        return False, "could not read ztunnel Pods to verify readiness"
    items = pods.get("items", [])
    ready = [p for p in items if mesh_status._is_ready(p)]
    ok = bool(items) and len(ready) == len(items)
    return ok, f"{len(ready)}/{len(items)} ztunnel Pods Ready"


def _restore_ztunnel_log_env(original: ZtunnelLogEnvState) -> None:
    """DAY6 fourth remediation item 1: ALWAYS invoked (from a `finally`)
    once the diagnostic mutation was successfully SUBMITTED - regardless
    of whether the diagnostic rollout completed, diagnostic Pods
    behaved, log retrieval succeeded, parsing succeeded, a probe raised,
    or any earlier step in this function's caller returned early.
    Restoration is itself a full transactional sequence: submit -> wait
    for rollout -> reread the DaemonSet -> verify RUST_LOG exactly
    matches its original representation -> verify every ztunnel Pod is
    Ready. Any uncertainty at any of these steps is recorded as its own
    explicit RESTORATION FAILURE finding - never silently treated as
    success."""
    if original.present:
        submit_ok, submit_detail = _submit_ztunnel_env_literal(original.value)
    else:
        submit_ok, submit_detail = _submit_ztunnel_env_unset()
    if not submit_ok:
        record(False, f"RESTORATION FAILURE: could not submit restoration of {ZTUNNEL_LOG_ENV_VAR}: {submit_detail}")
        return

    rollout_ok, rollout_detail = _wait_ztunnel_rollout()
    if not rollout_ok:
        record(False, f"RESTORATION FAILURE: post-restore rollout did not complete: {rollout_detail}")
        return

    reread = _ztunnel_log_env_state()
    if not reread.found_daemonset:
        record(False, "RESTORATION FAILURE: could not reread the ztunnel DaemonSet to verify restoration")
        return
    matches = (
        reread.present == original.present
        and reread.value == original.value
        and reread.uses_value_from == original.uses_value_from
    )
    if not matches:
        record(
            False,
            f"RESTORATION FAILURE: ztunnel {ZTUNNEL_LOG_ENV_VAR} does not exactly match its original representation "
            f"after restore (expected present={original.present} value={original.value!r} valueFrom={original.uses_value_from}, "
            f"found present={reread.present} value={reread.value!r} valueFrom={reread.uses_value_from})",
        )
        return

    ready_ok, ready_detail = _all_ztunnel_pods_ready()
    if not ready_ok:
        record(False, f"RESTORATION FAILURE: not all ztunnel Pods are Ready after restoration ({ready_detail})")
        return

    record(
        True,
        f"ztunnel {ZTUNNEL_LOG_ENV_VAR} restored and verified to exactly match its original representation "
        f"(present={original.present}, value={original.value!r}); {ready_detail}",
    )


def _ztunnel_logs_since(since_time_iso: str) -> str:
    result = kube.run("-n", ZTUNNEL_NAMESPACE, "logs", "-l", kube.ZTUNNEL_LABEL_SELECTOR, f"--since-time={since_time_iso}", "--tail=-1", check=False, timeout=30.0)
    if result.returncode != 0:
        return ""
    return result.stdout


_ACCESS_LOG_FIELD_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')


def _parse_access_log_fields(line: str) -> dict[str, str]:
    """DAY6 fourth remediation item 3: extracts `key=value`/
    `key="quoted value"` pairs from a ztunnel structured access-log
    line. The documented fields this project correlates against are
    `src.identity`, `dst.identity`, `dst.hbone_addr`, `dst.service`, and
    `direction`."""
    fields: dict[str, str] = {}
    for match in _ACCESS_LOG_FIELD_RE.finditer(line):
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        fields[key] = value
    return fields


def _find_transport_evidence(logs: str, src_spiffe: str, dest_substr: str) -> dict | None:
    """DAY6 fourth remediation item 3, corrected by the fifth
    remediation's live findings: proof ztunnel produced a completed
    access-log record (`access` + `connection complete`) for a
    connection attempt from our probe's real SPIFFE identity, destined
    for the target workload - correlated via `src.identity` (the SPIFFE
    form, never the bare AuthorizationPolicy form, which is not what
    appears in ztunnel's own logs) and `dst.service`/`dst.hbone_addr`.

    IMPORTANT correction (fifth remediation): `connection complete` does
    NOT mean the connection was ALLOWED - a live run showed ztunnel emits
    a `connection complete` record for REJECTED connections too (with an
    `error=` field describing the rejection). This function therefore
    proves only that the local ztunnel logged a finished record for this
    identity+destination pair - i.e. genuine transport-layer activity
    occurred - never an authorization OUTCOME either way. The actual
    allow/deny determination is `_find_denial_evidence()`'s job, keyed
    on the presence/content of the `error=` field, not on whether
    `connection complete` appears. Returns the parsed field dict of the
    first matching line, or None if no such line is found."""
    for line in logs.splitlines():
        if ACCESS_LOG_MARKER not in line or CONNECTION_COMPLETE_MARKER not in line:
            continue
        fields = _parse_access_log_fields(line)
        if fields.get("src.identity") != src_spiffe:
            continue
        if dest_substr in fields.get("dst.service", "") or dest_substr in fields.get("dst.hbone_addr", ""):
            return fields
    return None


def _find_denial_evidence(logs: str, src_spiffe: str, dest_substr: str) -> tuple[str, dict | str | None]:
    """DAY6 fourth remediation item 3, corrected and extended by the
    fifth remediation item 5: looks for denial evidence in four tiers,
    in order.

    IMPORTANT correction (fifth remediation, live-discovered): earlier
    revisions of this function excluded any line containing
    `connection complete` from consideration, on the assumption that
    marker meant "allowed through". A live run proved that wrong -
    ztunnel emits a `connection complete` record for REJECTED
    connections too, carrying an `error=` field describing why. The
    actual signal a correlated line was rejected is the PRESENCE of a
    non-empty `error=` field, never whether `connection complete`
    appears - so this function no longer filters on that marker at all;
    a correlated line with NO `error=` field is simply not denial
    evidence (it is a normal/allowed record - `_find_transport_evidence()`'s
    concern, not this function's).

      1. AUTHORITATIVE - a structured, field-parseable line correlated by
         EXACT `src.identity` and destination match, carrying a
         non-empty `error=` field that is one of the two exact Istio
         1.31 ztunnel denial strings this project has now independently
         observed live (see `AUTHORITATIVE_POLICY_REJECTION_ERROR`/
         `AUTHORITATIVE_HBONE_401_ERROR`): the explicit policy-rejection
         error on its own, or the HBONE 401 error ONLY when also
         correlated with `bytes_sent=0`/`bytes_recv=0` (proving no
         application data was ever exchanged - a 401 that DID exchange
         bytes is a materially different situation and is never promoted
         here). This is definitive: the caller may record it as a
         confirmed PASS with no hedging.
      2. CANDIDATE - a structured, field-parseable line correlated the
         same way, carrying a non-empty `error=` field that is NOT one
         of the two exact recognized strings above (or the 401 form with
         nonzero bytes) - built entirely from the documented fields, but
         not (yet) matched to a confirmed exact format.
      3. BEST_EFFORT - failing that, a best-effort, explicitly
         unverified-format substring search for common RBAC/deny-shaped
         tokens on any line that also contains both the SPIFFE identity
         and the destination substring.
      4. NONE - nothing found. The caller must record this as
         INCONCLUSIVE, never as a confident claim that no denial
         occurred."""
    candidate_fields: dict | None = None
    for line in logs.splitlines():
        if src_spiffe not in line:
            continue
        fields = _parse_access_log_fields(line)
        if fields.get("src.identity") != src_spiffe:
            continue
        dest_match = dest_substr in fields.get("dst.service", "") or dest_substr in fields.get("dst.hbone_addr", "")
        if not dest_match:
            continue

        error = fields.get("error", "")
        if not error:
            continue  # correlated but no error field - a normal/allowed record, not denial evidence
        if error == AUTHORITATIVE_POLICY_REJECTION_ERROR:
            return "AUTHORITATIVE", fields
        if error == AUTHORITATIVE_HBONE_401_ERROR and fields.get("bytes_sent") == "0" and fields.get("bytes_recv") == "0":
            return "AUTHORITATIVE", fields
        if candidate_fields is None:
            candidate_fields = fields

    if candidate_fields is not None:
        return "CANDIDATE", candidate_fields

    for line in logs.splitlines():
        if src_spiffe in line and dest_substr in line:
            lowered = line.lower()
            if any(token in lowered for token in _BEST_EFFORT_DENY_TOKENS):
                return "BEST_EFFORT", line

    return "NONE", None


def _record_log_correlation(logs: str, target_name: str, dest_substr: str) -> bool:
    """Records the transport + denial-evidence findings for one target,
    against whichever log text the caller supplies (default-level or
    diagnostic-level). Returns True if transport (identity/HBONE)
    evidence was found for this target - the signal
    `check_authorization_denial_isolated()` uses to decide whether
    default logs already sufficed without needing the RUST_LOG
    mutation."""
    transport_fields = _find_transport_evidence(logs, MESH_PROBE_SPIFFE_IDENTITY, dest_substr)
    record(
        transport_fields is not None,
        f"wrong-identity -> {target_name}: ztunnel access-log evidence for SPIFFE identity {MESH_PROBE_SPIFFE_IDENTITY!r} "
        f"({'found: ' + repr(transport_fields) if transport_fields else 'NOT found in captured logs'})",
    )

    kind, evidence = _find_denial_evidence(logs, MESH_PROBE_SPIFFE_IDENTITY, dest_substr)
    if kind == "AUTHORITATIVE":
        record(
            True,
            f"wrong-identity -> {target_name}: AUTHORITATIVE ztunnel denial evidence - correlated identity+destination "
            f"record carrying a confirmed Istio 1.31 denial error (observed live, not a guess): {evidence}",
        )
    elif kind == "CANDIDATE":
        record(
            True,
            f"wrong-identity -> {target_name}: correlated ztunnel log line for this identity+destination that never "
            f"reached connection-complete (candidate AuthorizationPolicy denial evidence): {evidence}",
        )
    elif kind == "BEST_EFFORT":
        record(
            True,
            f"wrong-identity -> {target_name}: ztunnel log line correlated by identity+destination containing an "
            f"unverified-format RBAC/deny-shaped token (best-effort - Istio 1.31's exact deny format not "
            f"independently observed): {evidence!r}",
        )
    else:
        record(
            False,
            f"wrong-identity -> {target_name}: INCONCLUSIVE - no correlated ztunnel log evidence (candidate or "
            f"best-effort) of an AuthorizationPolicy-specific denial found for identity {MESH_PROBE_SPIFFE_IDENTITY!r}",
        )

    return transport_fields is not None


def check_wrong_identity_denied() -> None:
    """DAY6 - the core new proof, refined by the third and fourth
    remediations: a genuinely ambient-enrolled Pod, under its OWN
    distinct ServiceAccount (never one of the allowed principals), must
    be denied reaching gateway, app, AND state - while production
    identity paths (proven above) keep working - and that denial must be
    correlated to Istio's AuthorizationPolicy layer specifically (see
    `check_authorization_denial_isolated()` below, called from here so
    the ztunnel log window covers these exact probe attempts). Every
    step is bounded; the temporary namespace is deleted AND its absence
    verified via an explicit NOT_FOUND response in a guaranteed
    `finally`, regardless of where this function fails."""
    try:
        _apply(_mesh_probe_namespace_manifest())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record(False, f"INCONCLUSIVE: could not create temporary mesh probe namespace {MESH_PROBE_NAMESPACE!r}: {exc}")
        return

    try:
        try:
            _apply(_mesh_probe_serviceaccount_and_pod_manifest())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            record(False, f"INCONCLUSIVE: could not create the wrong-identity ServiceAccount/Pod: {exc}")
            return

        try:
            pod = kube.wait_until(
                lambda: _pod_if_running_or_terminal(MESH_PROBE_NAMESPACE, MESH_PROBE_POD_NAME),
                timeout=POD_DEADLINE_SECONDS,
                description=f"pod {MESH_PROBE_POD_NAME} Running",
            )
        except TimeoutError as exc:
            record(False, f"INCONCLUSIVE: wrong-identity probe Pod never reached a terminal wait state: {exc}")
            return

        phase = pod.get("status", {}).get("phase")
        if phase != "Running":
            record(False, f"INCONCLUSIVE: wrong-identity probe Pod never reached Running (phase={phase!r})")
            return

        # 1) Prove the negative probe Pod is actually ambient-enrolled
        # (no sidecar, ambient redirection annotation present) - this is
        # what distinguishes "denied because wrong identity" from
        # "denied because it never spoke mTLS at all" (a plaintext
        # rejection - a materially weaker, different proof).
        no_sidecar, redirected = _is_ambient_enrolled(pod)
        record(no_sidecar, f"wrong-identity probe Pod: no {SIDECAR_CONTAINER_NAME!r} sidecar container (ambient has none)")
        record(
            redirected,
            f"wrong-identity probe Pod: {AMBIENT_REDIRECTION_ANNOTATION} == {AMBIENT_REDIRECTION_VALUE!r} - "
            "genuine ambient/mTLS participation, not a plaintext-only Pod",
        )

        # 2) Prove it carries its OWN distinct ServiceAccount principal
        # - foundational to this being a genuine "wrong identity" test
        # rather than an accidental reuse of an allowed one.
        actual_sa = pod.get("spec", {}).get("serviceAccountName")
        record(
            actual_sa == MESH_PROBE_SERVICE_ACCOUNT,
            f"wrong-identity probe Pod runs as ServiceAccount {actual_sa!r} (expected {MESH_PROBE_SERVICE_ACCOUNT!r}) - "
            f"its real mesh principal is {MESH_PROBE_PRINCIPAL} ({MESH_PROBE_SPIFFE_IDENTITY} in SPIFFE form), "
            "never one of the allowed principals in EXPECTED_AUTHZ",
        )

        # 3) DAY6 fifth remediation (corrected after the first live run):
        # client-side SUPPORTING evidence only, at TWO layers, all three
        # targets. The raw TCP-connect probe proves only that the LOCAL
        # ztunnel accepted the client socket (LOCAL_ZTUNNEL_CONNECT_ACCEPTED
        # - ambient's transparent interception means this can, and live
        # did, complete even for a denied identity) - never gating. The
        # separate HTTP /livez probe is the actual application-
        # reachability leak check: HTTP 200 there IS a hard failure
        # (proof the unauthorized request was actually served); every
        # other HTTP-layer outcome is supporting evidence only. Neither
        # probe, at either layer, proves an AuthorizationPolicy-specific
        # denial - that isolation is step 4, next.
        for target_fqdn, target_name, _dest in _TARGETS:
            tcp_result = netpol._run_probe(
                MESH_PROBE_NAMESPACE,
                MESH_PROBE_POD_NAME,
                netpol._tcp_connect_probe_snippet(target_fqdn, 8080, netpol.CONNECT_TIMEOUT_SECONDS),
            )
            _record_client_side_supporting_evidence(tcp_result, f"wrong-identity -> {target_name} client-side TCP outcome")

            http_result = netpol._run_probe(
                MESH_PROBE_NAMESPACE,
                MESH_PROBE_POD_NAME,
                netpol._http_probe_snippet(target_fqdn, 8080, LIVEZ_PATH, netpol.CONNECT_TIMEOUT_SECONDS, netpol.READ_TIMEOUT_SECONDS),
            )
            _record_client_side_http_leak_check(http_result, f"wrong-identity -> {target_name} client-side HTTP {LIVEZ_PATH} outcome")

        # 4) Isolate the denial to Istio's AuthorizationPolicy layer
        # specifically, via correlated ztunnel log evidence built on
        # documented access-log fields (fourth remediation item 3),
        # preferring the default (unmutated) log level and falling back
        # to a fully transactional RUST_LOG escalation only if needed
        # (fourth remediation item 1).
        check_authorization_denial_isolated()
    finally:
        cleanup_ok, cleanup_detail = delete_namespace_and_verify_gone(MESH_PROBE_NAMESPACE)
        if not cleanup_ok:
            record(False, f"RESTORATION FAILURE: {cleanup_detail}")
        else:
            record(True, cleanup_detail)


def check_authorization_denial_isolated() -> None:
    """DAY6 third and fourth remediations - see module docstring for the
    full design. First tries the ztunnel DaemonSet's CURRENT (default,
    unmutated) log level: re-runs the three wrong-identity connection
    attempts, and if the resulting logs already contain transport
    (identity/HBONE) evidence for every target, records findings from
    those logs and returns WITHOUT ever mutating RUST_LOG. Only if the
    default level does not already suffice does it fall back to a fully
    transactional RUST_LOG escalation: submit -> wait for rollout ->
    diagnostic probing/log correlation -> restore (always, via a
    guaranteed `finally`, once submission succeeds) -> verify
    restoration exactly. A restoration failure is reported as its own
    explicit, prominent finding, never silently swallowed by the probe's
    own result."""
    probe_window_start = _now_iso()
    default_logs = ""
    try:
        for target_fqdn, _target_name, _dest in _TARGETS:
            netpol._run_probe(
                MESH_PROBE_NAMESPACE,
                MESH_PROBE_POD_NAME,
                netpol._tcp_connect_probe_snippet(target_fqdn, 8080, netpol.CONNECT_TIMEOUT_SECONDS),
            )
    except Exception as exc:  # noqa: BLE001 - a probe raising here must not abort the whole check
        record(False, f"INCONCLUSIVE: default-log probe attempt raised an unexpected error: {exc}")
    else:
        default_logs = _ztunnel_logs_since(probe_window_start)

    if default_logs.strip():
        transport_found = [
            _find_transport_evidence(default_logs, MESH_PROBE_SPIFFE_IDENTITY, dest) is not None for _fqdn, _name, dest in _TARGETS
        ]
        if all(transport_found):
            for _fqdn, target_name, dest in _TARGETS:
                _record_log_correlation(default_logs, target_name, dest)
            record(
                True,
                f"ztunnel's default (unmutated) log level already contained identity/HBONE transport evidence for "
                f"every target - {ZTUNNEL_LOG_ENV_VAR} mutation was not needed and was never attempted",
            )
            return

    # Default logs did not already contain sufficient evidence for every
    # target - fall back to the transactional RUST_LOG escalation.
    original = _ztunnel_log_env_state()
    if not original.found_daemonset:
        record(False, "INCONCLUSIVE: could not read the ztunnel DaemonSet - AuthorizationPolicy-specific isolation skipped (client-side supporting evidence above still stands on its own)")
        return
    if original.uses_value_from:
        record(
            False,
            f"INCONCLUSIVE: ztunnel's {ZTUNNEL_LOG_ENV_VAR} uses valueFrom ({original.raw_entry!r}) - kubectl set env "
            "cannot restore this exactly, so this remediation refuses to mutate it at all (fail before mutation, by design)",
        )
        return

    submit_ok, submit_detail = _submit_ztunnel_env_literal(ZTUNNEL_DIAGNOSTIC_LOG_VALUE)
    if not submit_ok:
        record(False, f"INCONCLUSIVE: could not submit ztunnel log-level mutation: {submit_detail}")
        return  # nothing was mutated - the submit itself failed, safe to return without restoring

    # From this point on, the mutation was SUBMITTED and accepted by the
    # API - restoration MUST be attempted no matter what happens below,
    # including a diagnostic rollout timeout, a failed diagnostic Pod, a
    # log-retrieval failure, a parsing failure, a probe raising, or an
    # early return.
    try:
        rollout_ok, rollout_detail = _wait_ztunnel_rollout()
        if not rollout_ok:
            record(False, f"INCONCLUSIVE: diagnostic-mode ztunnel rollout did not complete: {rollout_detail}")
            return

        try:
            diagnostic_window_start = _now_iso()
            for target_fqdn, _target_name, _dest in _TARGETS:
                netpol._run_probe(
                    MESH_PROBE_NAMESPACE,
                    MESH_PROBE_POD_NAME,
                    netpol._tcp_connect_probe_snippet(target_fqdn, 8080, netpol.CONNECT_TIMEOUT_SECONDS),
                )

            logs = _ztunnel_logs_since(diagnostic_window_start)
            if not logs.strip():
                record(False, "INCONCLUSIVE: no ztunnel log output was captured for the diagnostic probe window (client-side supporting evidence above still stands on its own)")
                return

            for _fqdn, target_name, dest in _TARGETS:
                _record_log_correlation(logs, target_name, dest)
        except Exception as exc:  # noqa: BLE001 - must not skip restoration below
            record(False, f"INCONCLUSIVE: diagnostic-mode probing/log correlation raised an unexpected error: {exc}")
            return
    finally:
        _restore_ztunnel_log_env(original)


def main() -> int:
    print(f"# Day 6 mesh check: ambient enrollment, strict mTLS, AuthorizationPolicy identity, isolated wrong-identity denial (context {kube.CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    check_mesh_component_health()
    check_ambient_enrollment(GATEWAY_LABEL_SELECTOR, "gateway")
    check_ambient_enrollment(APP_LABEL_SELECTOR, "app")
    check_ambient_enrollment(STATE_LABEL_SELECTOR, "state")
    check_peer_authentication_strict()
    check_authorization_policies()
    check_allowed_identity_paths_succeed()
    check_wrong_identity_denied()

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} mesh checks passed")
    if failures:
        print(f"FAIL: {len(failures)} mesh check(s) failed", file=sys.stderr)
        return 1
    print("PASS: ambient enrollment, strict mTLS, AuthorizationPolicy identity, and isolated wrong-identity denial verified live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

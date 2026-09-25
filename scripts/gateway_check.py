#!/usr/bin/env python3
"""
DAY6: proves the Gateway API routing path end to end against the LIVE
cluster - GatewayClass/Gateway/HTTPRoute status, and a real external
HTTP round trip through the host port mapping
(kind/cluster-day6.yaml's 127.0.0.1:18080 -> control-plane:30080, the
NodePort the Istio Gateway infrastructure ConfigMap pins - see
k8s/day6/gateway-values-configmap.yaml).

Runs only against a live cluster (never part of the cluster-free
`make ci-check`); see docs/architecture.md's "DAY6: live validation
record" for the recorded Day 6 run.

Classification discipline (explicit, per this project's "do not
manufacture green results" rule): every check here distinguishes THREE
outcomes, never conflating them -
  - a genuine PASS (the expected condition/response was actually
    observed);
  - a genuine, meaningful FAIL (the API/HTTP call completed and
    returned something that contradicts the expectation - e.g. a
    Gateway Programmed=False, or a 200 response for a Host that should
    have no route);
  - INCONCLUSIVE (the kubectl call itself failed/timed out, or the HTTP
    request itself could not complete) - reported as its own distinct
    failure category, NEVER silently counted as proof of denial or of
    anything else. A `kubectl get`/HTTP timeout reaching this project's
    own API server/NodePort is an infrastructure problem with the
    check itself, not evidence the routing/mesh layer is working as
    designed.

Proves:
  - GatewayClass `istio` exists and its Accepted condition is True.
  - Gateway `maops-edge` (maops-ingress) is Accepted=True and
    Programmed=True.
  - HTTPRoute `maops-gateway-route` (maops-platform) is Accepted=True
    and ResolvedRefs=True for its `maops-edge` parent.
  - `http://127.0.0.1:18080/` with `Host: maops.local` reaches
    maops-gateway (HTTP 200, the documented stable response shape).
  - the same host:port with an unconfigured Host header receives a
    DEFINITE no-route result - HTTP 404, the specific status Envoy/
    Istio returns for an unmatched Host (DAY6 remediation: not merely
    "any non-200 status", and not "a 200 with an unrecognized body" -
    both were too weak a claim).

Creates and leaks no probe Pod and no `kubectl port-forward` process -
every check here is either a `kubectl get -o json` read or a direct
HTTP request to the already-host-mapped NodePort, nothing to clean up.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kube

HTTP_TIMEOUT_SECONDS = 5.0
EXPECTED_ROOT_FIELDS = {"service", "message", "hostname", "uptime_seconds"}
WRONG_HOST = "wrong.invalid.example"

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> bool:
    results.append((ok, message))
    print(f"[{'PASS' if ok else 'FAIL'}] {message}")
    return ok


def _get_json(*args: str) -> dict | None:
    """Returns the parsed object, or None (recorded as an INCONCLUSIVE
    finding, never a denial/pass) if the kubectl call itself fails."""
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


def _condition(obj: dict, condition_type: str) -> dict | None:
    conditions = obj.get("status", {}).get("conditions", [])
    for c in conditions:
        if c.get("type") == condition_type:
            return c
    return None


def check_gatewayclass_accepted() -> None:
    gc = _get_json("get", "gatewayclass", kube.GATEWAY_API_GATEWAY_CLASS)
    if gc is None:
        return
    condition = _condition(gc, "Accepted")
    record(
        condition is not None and condition.get("status") == "True",
        f"GatewayClass/{kube.GATEWAY_API_GATEWAY_CLASS} Accepted condition: {condition!r}",
    )


def check_gateway_accepted_and_programmed() -> None:
    gw = _get_json(
        "-n", kube.INGRESS_NAMESPACE, "get", "gateway", kube.GATEWAY_API_GATEWAY_NAME
    )
    if gw is None:
        return
    accepted = _condition(gw, "Accepted")
    programmed = _condition(gw, "Programmed")
    record(
        accepted is not None and accepted.get("status") == "True",
        f"Gateway/{kube.GATEWAY_API_GATEWAY_NAME} Accepted condition: {accepted!r}",
    )
    record(
        programmed is not None and programmed.get("status") == "True",
        f"Gateway/{kube.GATEWAY_API_GATEWAY_NAME} Programmed condition: {programmed!r}",
    )


def check_httproute_accepted() -> None:
    route = _get_json(
        "-n", kube.NAMESPACE, "get", "httproute", kube.GATEWAY_HTTPROUTE_NAME
    )
    if route is None:
        return
    parents = route.get("status", {}).get("parents", [])
    matching = [
        p
        for p in parents
        if p.get("parentRef", {}).get("name") == kube.GATEWAY_API_GATEWAY_NAME
        and p.get("parentRef", {}).get("namespace", kube.INGRESS_NAMESPACE) == kube.INGRESS_NAMESPACE
    ]
    record(len(matching) == 1, f"HTTPRoute/{kube.GATEWAY_HTTPROUTE_NAME} has exactly one status entry for parent {kube.GATEWAY_API_GATEWAY_NAME!r}, found {len(matching)}")
    if not matching:
        return
    parent_status = matching[0]
    accepted = next((c for c in parent_status.get("conditions", []) if c.get("type") == "Accepted"), None)
    resolved = next((c for c in parent_status.get("conditions", []) if c.get("type") == "ResolvedRefs"), None)
    record(accepted is not None and accepted.get("status") == "True", f"HTTPRoute Accepted condition (parent {kube.GATEWAY_API_GATEWAY_NAME!r}): {accepted!r}")
    record(resolved is not None and resolved.get("status") == "True", f"HTTPRoute ResolvedRefs condition (parent {kube.GATEWAY_API_GATEWAY_NAME!r}): {resolved!r}")


def _request(host_header: str, path: str = "/") -> tuple[str, int | None, str]:
    """Returns (outcome, status, detail). outcome is one of "ok",
    "http_error", or "inconclusive" (connection could not be
    established/timed out - NEVER treated as proof of a route denial,
    only as an infrastructure problem with the check itself)."""
    url = f"{kube.GATEWAY_HOST_ADDRESS}{path}"
    req = urllib.request.Request(url, headers={"Host": host_header})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return "ok", resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return "http_error", exc.code, body
    except urllib.error.URLError as exc:
        return "inconclusive", None, f"{exc}"
    except TimeoutError as exc:
        return "inconclusive", None, f"{exc}"


def check_configured_host_reaches_gateway() -> None:
    outcome, status, body = _request(kube.ROUTING_HOSTNAME)
    if outcome == "inconclusive":
        record(False, f"INCONCLUSIVE: request to {kube.GATEWAY_HOST_ADDRESS} with Host: {kube.ROUTING_HOSTNAME} could not complete: {body}")
        return
    if status != 200:
        record(False, f"expected HTTP 200 for Host: {kube.ROUTING_HOSTNAME}, got {status} (body={body!r})")
        return
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        record(False, f"Host: {kube.ROUTING_HOSTNAME} returned HTTP 200 but the body was not valid JSON: {body!r}")
        return
    missing = EXPECTED_ROOT_FIELDS - (payload.keys() if isinstance(payload, dict) else set())
    record(
        not missing,
        f"Host: {kube.ROUTING_HOSTNAME} reaches maops-gateway: HTTP 200, expected stable fields present (missing={sorted(missing)})",
    )


WRONG_HOST_EXPECTED_STATUS = 404


def check_wrong_host_has_no_route() -> None:
    """DAY6 remediation: requires a DEFINITE no-route result - HTTP 404,
    the specific, documented status Envoy/Istio returns when no
    HTTPRoute matches a Gateway listener's Host - not merely "anything
    other than a 200 with a gateway-shaped body". An earlier revision
    of this check accepted any non-gateway-200 response (including,
    e.g., a 500 or an empty 200) as "proof of no route" - that is too
    weak a claim: it could not distinguish a genuine no-route result
    from an unrelated failure. `outcome == "inconclusive"` (the request
    itself could not complete - connection refused/reset, DNS failure,
    or a timeout reaching the NodePort) is its own distinct, explicit
    failure, exactly like every other INCONCLUSIVE case in this
    project - never silently treated as proof of "no route" either."""
    outcome, status, body = _request(WRONG_HOST)
    if outcome == "inconclusive":
        record(False, f"INCONCLUSIVE: request to {kube.GATEWAY_HOST_ADDRESS} with Host: {WRONG_HOST} could not complete (could not reach the NodePort at all): {body}")
        return
    record(
        status == WRONG_HOST_EXPECTED_STATUS,
        f"Host: {WRONG_HOST} received a definite no-route result (status={status}, expected {WRONG_HOST_EXPECTED_STATUS}; body={body[:200]!r})",
    )


def main() -> int:
    print(f"# Day 6 Gateway API check: GatewayClass/Gateway/HTTPRoute + external routing (context {kube.CONTEXT})")
    try:
        kube.verify_context()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    check_gatewayclass_accepted()
    check_gateway_accepted_and_programmed()
    check_httproute_accepted()
    check_configured_host_reaches_gateway()
    check_wrong_host_has_no_route()

    failures = [m for ok, m in results if not ok]
    print()
    print(f"{len(results) - len(failures)}/{len(results)} Gateway API checks passed")
    if failures:
        print(f"FAIL: {len(failures)} Gateway API check(s) failed", file=sys.stderr)
        return 1
    print("PASS: GatewayClass/Gateway/HTTPRoute are Accepted/Programmed and external routing works as expected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Repository-owned discovery.k8s.io/v1 EndpointSlice parsing.

Day 1 validated backend readiness via the legacy v1 Endpoints API, which
Kubernetes 1.36 emits a deprecation warning for. Day 2 moves
authoritative backend-readiness validation to EndpointSlice instead -
this module is the parsing logic, kept separate from the kubectl calls
so it's directly unit-testable against constructed fixtures (no live
cluster required).

A Service can legitimately be backed by more than one EndpointSlice
(e.g. when address families or the endpoint count get large enough for
Kubernetes to split them); the counting logic here sums across however
many slices a `kubectl get endpointslices -l kubernetes.io/service-name=...`
query returns.
"""

from __future__ import annotations


def count_ready_endpoints(slices: list[dict]) -> int:
    """Number of distinct ready addresses across all given EndpointSlice
    objects. An endpoint counts only if `conditions.ready` is explicitly
    `true` - a missing/false/null condition is treated as not-ready,
    matching kube-proxy's own routing behavior.

    Addresses are counted as a *set*, not summed, so the same address
    appearing more than once (within one endpoint, one slice, or across
    overlapping slices returned for the same
    `kubernetes.io/service-name` selector - a legitimate transient state
    during EndpointSlice rebalancing) is never double-counted. This is a
    single-stack-IPv4-scoped model (DAY2-INT-I1): it does not attempt to
    reconcile one Pod's IPv4 and IPv6 addresses as "the same backend"
    for a future dual-stack Service - that remains open, out of scope
    for Day 2.

    Malformed input (a slice or endpoint entry that isn't a dict) is
    skipped defensively rather than raising, since a caller could in
    principle hand this a partially-malformed `kubectl -o json` result.
    """
    ready_addresses: set[str] = set()
    for s in slices or []:
        if not isinstance(s, dict):
            continue
        for ep in s.get("endpoints", []) or []:
            if not isinstance(ep, dict):
                continue
            conditions = ep.get("conditions") or {}
            if conditions.get("ready") is True:
                ready_addresses.update(ep.get("addresses") or [])
    return len(ready_addresses)

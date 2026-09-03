"""
Docker/Kubernetes-free unit tests for scripts/endpointslice.py - the
discovery.k8s.io/v1 EndpointSlice parsing/counting logic that replaces
the legacy v1 Endpoints API for Day 2's authoritative backend-readiness
evidence.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from endpointslice import count_ready_endpoints


def _endpoint(ready: bool | None, addresses: list[str]) -> dict:
    ep = {"addresses": addresses}
    if ready is not None:
        ep["conditions"] = {"ready": ready}
    else:
        ep["conditions"] = {}
    return ep


class CountReadyEndpointsTests(unittest.TestCase):
    def test_two_ready_single_address_endpoints(self):
        slices = [
            {
                "endpoints": [
                    _endpoint(True, ["10.244.0.5"]),
                    _endpoint(True, ["10.244.0.6"]),
                ]
            }
        ]
        self.assertEqual(count_ready_endpoints(slices), 2)

    def test_not_ready_endpoint_excluded(self):
        slices = [
            {
                "endpoints": [
                    _endpoint(True, ["10.244.0.5"]),
                    _endpoint(False, ["10.244.0.6"]),
                ]
            }
        ]
        self.assertEqual(count_ready_endpoints(slices), 1)

    def test_missing_ready_condition_treated_as_not_ready(self):
        slices = [{"endpoints": [_endpoint(None, ["10.244.0.5"])]}]
        self.assertEqual(count_ready_endpoints(slices), 0)

    def test_no_endpoints_is_zero(self):
        self.assertEqual(count_ready_endpoints([]), 0)
        self.assertEqual(count_ready_endpoints([{"endpoints": []}]), 0)

    def test_sums_across_multiple_slices(self):
        slices = [
            {"endpoints": [_endpoint(True, ["10.244.0.5"])]},
            {"endpoints": [_endpoint(True, ["10.244.0.6"])]},
        ]
        self.assertEqual(count_ready_endpoints(slices), 2)

    def test_endpoint_with_multiple_addresses_counts_each(self):
        # Real EndpointSlices generally have one address per endpoint for
        # single-stack IPv4 - this documents the counting rule rather than
        # assuming it away.
        slices = [{"endpoints": [_endpoint(True, ["10.244.0.5", "10.244.0.6"])]}]
        self.assertEqual(count_ready_endpoints(slices), 2)

    def test_missing_addresses_key_counts_as_zero(self):
        slices = [{"endpoints": [{"conditions": {"ready": True}}]}]
        self.assertEqual(count_ready_endpoints(slices), 0)

    def test_real_shaped_two_ready_endpointslice_matches_expected_count(self):
        # A realistic shape of what `kubectl get endpointslices -o json`
        # actually returns for a healthy 2-replica Service.
        slices = [
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSlice",
                "metadata": {"name": "maops-app-abcde"},
                "endpoints": [
                    {"addresses": ["10.244.0.10"], "conditions": {"ready": True, "serving": True, "terminating": False}},
                    {"addresses": ["10.244.0.11"], "conditions": {"ready": True, "serving": True, "terminating": False}},
                ],
                "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
            }
        ]
        self.assertEqual(count_ready_endpoints(slices), 2)


class DefensivenessAndDedupTests(unittest.TestCase):
    """DAY2-TEST-L2: narrow, single-stack-scoped hardening. Duplicate
    identical ready addresses (a legitimate transient state during
    EndpointSlice rebalancing) must be counted once, not once per
    occurrence, and malformed non-dict slice/endpoint entries must be
    skipped rather than raising. Dual-stack address-family reconciliation
    (DAY2-INT-I1) remains explicitly out of scope."""

    def test_duplicate_ready_address_across_overlapping_slices_counted_once(self):
        slices = [
            {"endpoints": [_endpoint(True, ["10.244.0.5"])]},
            {"endpoints": [_endpoint(True, ["10.244.0.5"])]},
        ]
        self.assertEqual(count_ready_endpoints(slices), 1)

    def test_duplicate_ready_address_within_same_slice_counted_once(self):
        slices = [{"endpoints": [_endpoint(True, ["10.244.0.5"]), _endpoint(True, ["10.244.0.5"])]}]
        self.assertEqual(count_ready_endpoints(slices), 1)

    def test_malformed_non_dict_slice_entries_are_skipped_not_raised(self):
        slices = [None, "not-a-slice", {"endpoints": [_endpoint(True, ["10.244.0.5"])]}]
        self.assertEqual(count_ready_endpoints(slices), 1)

    def test_malformed_non_dict_endpoint_entries_are_skipped_not_raised(self):
        slices = [{"endpoints": [None, "not-an-endpoint", _endpoint(True, ["10.244.0.5"])]}]
        self.assertEqual(count_ready_endpoints(slices), 1)

    def test_endpoints_key_explicitly_none_is_zero(self):
        self.assertEqual(count_ready_endpoints([{"endpoints": None}]), 0)


if __name__ == "__main__":
    unittest.main()

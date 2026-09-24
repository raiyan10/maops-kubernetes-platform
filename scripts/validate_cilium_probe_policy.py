"""
Repository-owned static validation for
`k8s/day6/cilium-ambient-probe-policy.yaml` - the one
CiliumClusterwideNetworkPolicy that lets Istio ztunnel's SNAT'd kubelet
health-probe traffic (IPv4 link-local source 169.254.7.127) reach this
project's own workload probe port under Cilium default-deny.

DAY6 review SEC-3: this is the only security-relevant object in the
project that was previously guarded by nothing but its own comment - a
cluster-scoped allow rule, so a silent widening (a broader CIDR, an
extra port, a dropped namespace/label selector, an egress or extra
ingress rule, an L7 rule) would be cluster-wide in effect. This module
pins its exact shape.

A pure static check over the file's text - parsed with
`scripts/k8s_yaml.py` (the file is plain, block-style, hand-authored
YAML well inside that loader's supported subset), never `kubectl` and
never a live cluster, so it runs as part of the cluster-free
`make helm-check`/`make ci-check` sequence.

IP family: this project's kind clusters are IPv4-only (kind's default -
no `networking.ipFamily` is set in `kind/cluster-day6.yaml`), so the
policy deliberately covers ONLY the IPv4 SNAT address. This checker
rejects any IPv6 CIDR and requires the policy's own `spec.description`
to state its IPv4-only scope, so the limitation stays explicit on the
object itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import k8s_yaml
from validate_helm_chart import Finding

EXPECTED_API_VERSION = "cilium.io/v2"
EXPECTED_KIND = "CiliumClusterwideNetworkPolicy"
EXPECTED_NAME = "maops-allow-ambient-health-probes"
EXPECTED_MATCH_LABELS = {
    "k8s:io.kubernetes.pod.namespace": "maops-platform",
    "app.kubernetes.io/part-of": "maops-kubernetes-platform",
}
EXPECTED_SOURCE_CIDRS = ["169.254.7.127/32"]
EXPECTED_PORTS = [{"port": "8080", "protocol": "TCP"}]
ALLOWED_SPEC_KEYS = {"description", "endpointSelector", "ingress"}
ALLOWED_INGRESS_RULE_KEYS = {"fromCIDR", "toPorts"}
IPV4_ONLY_MARKER = "IPv4-only"


def run_checks(text: str) -> list[Finding]:
    findings: list[Finding] = []

    try:
        docs = k8s_yaml.load_all(text)
    except ValueError as exc:
        return [Finding(ok=False, name="cilium_probe_policy.parse", detail=f"could not parse the policy file: {exc}")]

    findings.append(Finding(ok=len(docs) == 1, name="cilium_probe_policy.single_document", detail=f"expected exactly 1 document, found {len(docs)}"))
    if len(docs) != 1:
        return findings
    doc = docs[0]
    metadata = doc.get("metadata") or {}
    spec = doc.get("spec") or {}

    findings.append(Finding(ok=doc.get("apiVersion") == EXPECTED_API_VERSION, name="cilium_probe_policy.api_version", detail=f"expected {EXPECTED_API_VERSION!r}, found {doc.get('apiVersion')!r}"))
    findings.append(Finding(ok=doc.get("kind") == EXPECTED_KIND, name="cilium_probe_policy.kind", detail=f"expected {EXPECTED_KIND!r}, found {doc.get('kind')!r}"))
    findings.append(Finding(ok=metadata.get("name") == EXPECTED_NAME, name="cilium_probe_policy.name", detail=f"expected {EXPECTED_NAME!r}, found {metadata.get('name')!r}"))

    spec_keys = set(spec.keys()) if isinstance(spec, dict) else set()
    findings.append(
        Finding(
            ok=spec_keys == ALLOWED_SPEC_KEYS,
            name="cilium_probe_policy.spec_keys_exact",
            detail=(
                f"expected spec keys exactly {sorted(ALLOWED_SPEC_KEYS)} (no egress/egressDeny/ingressDeny/"
                f"nodeSelector/specs), found {sorted(spec_keys)}"
            ),
        )
    )

    selector = spec.get("endpointSelector") if isinstance(spec, dict) else None
    selector_keys = set(selector.keys()) if isinstance(selector, dict) else set()
    match_labels = selector.get("matchLabels") if isinstance(selector, dict) else None
    findings.append(
        Finding(
            ok=selector_keys == {"matchLabels"} and match_labels == EXPECTED_MATCH_LABELS,
            name="cilium_probe_policy.endpoint_selector_exact",
            detail=(
                f"expected endpointSelector to be exactly matchLabels {EXPECTED_MATCH_LABELS} (never empty/"
                f"wildcard, never matchExpressions), found {selector!r}"
            ),
        )
    )

    ingress = spec.get("ingress") if isinstance(spec, dict) else None
    ingress_ok = isinstance(ingress, list) and len(ingress) == 1
    findings.append(Finding(ok=ingress_ok, name="cilium_probe_policy.single_ingress_rule", detail=f"expected exactly 1 ingress rule, found {ingress!r}"))
    rule = ingress[0] if ingress_ok and isinstance(ingress[0], dict) else {}

    rule_keys = set(rule.keys())
    findings.append(
        Finding(
            ok=rule_keys == ALLOWED_INGRESS_RULE_KEYS,
            name="cilium_probe_policy.ingress_rule_keys_exact",
            detail=(
                f"expected the ingress rule's keys exactly {sorted(ALLOWED_INGRESS_RULE_KEYS)} (no fromEndpoints/"
                f"fromEntities/fromCIDRSet/fromRequires), found {sorted(rule_keys)}"
            ),
        )
    )

    cidrs = rule.get("fromCIDR")
    findings.append(Finding(ok=cidrs == EXPECTED_SOURCE_CIDRS, name="cilium_probe_policy.source_cidr_exact", detail=f"expected fromCIDR exactly {EXPECTED_SOURCE_CIDRS}, found {cidrs!r}"))
    findings.append(
        Finding(
            ok=isinstance(cidrs, list) and all(isinstance(c, str) and ":" not in c for c in cidrs),
            name="cilium_probe_policy.ipv4_only_cidrs",
            detail=f"expected IPv4 CIDRs only (this cluster is single-stack IPv4), found {cidrs!r}",
        )
    )

    to_ports = rule.get("toPorts")
    to_ports_ok = isinstance(to_ports, list) and len(to_ports) == 1 and isinstance(to_ports[0], dict) and set(to_ports[0].keys()) == {"ports"}
    findings.append(
        Finding(
            ok=to_ports_ok,
            name="cilium_probe_policy.to_ports_l4_only",
            detail=f"expected exactly one toPorts entry carrying only 'ports' (no L7 'rules'), found {to_ports!r}",
        )
    )
    ports = to_ports[0].get("ports") if to_ports_ok else None
    findings.append(Finding(ok=ports == EXPECTED_PORTS, name="cilium_probe_policy.tcp_8080_only", detail=f"expected ports exactly {EXPECTED_PORTS}, found {ports!r}"))

    description = spec.get("description", "") if isinstance(spec, dict) else ""
    findings.append(
        Finding(
            ok=isinstance(description, str) and IPV4_ONLY_MARKER in description,
            name="cilium_probe_policy.ipv4_only_scope_documented",
            detail=f"expected spec.description to state the policy's {IPV4_ONLY_MARKER!r} scope, found {description!r}",
        )
    )

    return findings


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "k8s" / "day6" / "cilium-ambient-probe-policy.yaml"
    findings = run_checks(path.read_text())
    for finding in findings:
        print(finding.render())
    failures = [f for f in findings if not f.ok]
    print(f"{len(findings) - len(failures)}/{len(findings)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

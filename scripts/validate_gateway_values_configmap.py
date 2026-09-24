"""
Repository-owned static validation for
`k8s/day6/gateway-values-configmap.yaml` - the Istio Gateway API
`infrastructure.parametersRef` ConfigMap for `maops-edge`.

DAY6 remediation: this file is deliberately NOT parsed with
`scripts/k8s_yaml.py` (this project's dependency-free YAML-subset
loader). That loader's own module docstring is explicit that it
supports only the bounded subset `kubectl kustomize` emits - "no ...
multi-line block scalars" - and this ConfigMap's `data` values are
exactly that: literal block scalars (`|`) holding nested YAML text,
hand-authored (never rendered by Kustomize/Helm). Extending the shared
loader to accept multi-line block scalars in general would widen its
supported subset project-wide for the sake of one file; instead, this
module implements a small, purpose-built line-scanner for this ONE
ConfigMap's exact shape - never a general YAML parser, and never a
third-party dependency (see `.claude/CLAUDE.md`'s ground rules).

An earlier revision of the ConfigMap used a single `data["values.yaml"]`
key holding a nested "gateway" Helm chart values document - INCORRECT,
not the schema Istio's Gateway API deployment controller actually
reads. The corrected schema uses per-resource-kind top-level `data`
keys (`deployment`, `service`, `serviceAccount`), each a
strategic-merge-patch YAML document shaped like the target resource
itself. This module's `run_checks()` asserts exactly that corrected
schema and explicitly REJECTS the old wrapper if it ever reappears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REQUIRED_DATA_KEYS = {"deployment", "service", "serviceAccount"}
FORBIDDEN_DATA_KEYS = {"values.yaml", "values"}

_DATA_KEY_RE = re.compile(r"^  ([A-Za-z0-9_.\-]+): \|\s*$")


@dataclass
class Finding:
    ok: bool
    name: str
    detail: str

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        return f"[{status}] {self.name}: {self.detail}"


def extract_data_blocks(text: str) -> dict[str, str]:
    """Returns {data_key: raw_block_scalar_text} for every top-level
    `data.<key>: |` entry in the ConfigMap - a line-scanner, not a YAML
    parser (see module docstring). A `data.<key>` line is recognized at
    exactly 2-space indent, immediately under `data:`; its block scalar
    body is every following line indented 4+ spaces (or blank), up to
    the next 2-space-indented key or end of input."""
    lines = text.split("\n")
    blocks: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    in_data = False

    def _flush() -> None:
        if current_key is not None:
            blocks[current_key] = "\n".join(current_lines)

    for line in lines:
        if line.rstrip() == "data:":
            in_data = True
            continue
        if not in_data:
            continue
        key_match = _DATA_KEY_RE.match(line)
        if key_match:
            _flush()
            current_key = key_match.group(1)
            current_lines = []
            continue
        if current_key is not None:
            if line == "" or line.startswith("    ") or line.startswith("\t"):
                current_lines.append(line)
                continue
            # Dedent to <= 2 spaces with real content that isn't a
            # recognized data key line: the data: block has ended.
            if line.strip() and not line.startswith("  "):
                _flush()
                current_key = None
                in_data = False
                continue
    _flush()
    return blocks


def _has_field(block: str, key: str, value: str) -> bool:
    """Line-anchored match of `<key>: <value>` within `block` - never a
    bare substring check. A bare `"targetPort: 80" in text` would
    wrongly match inside `targetPort: 8080` (a prefix collision, the
    same class of bug this project's own selector-collision checks
    elsewhere guard against) - anchoring to the full line (optional
    trailing whitespace, nothing else) rules that out."""
    # `[-\s]*` (not just `\s*`) also matches a YAML sequence item's
    # leading "- " marker (e.g. "          - name: istio-proxy"), so a
    # key that happens to be the first field of a list item is found
    # the same as any other indented key.
    pattern = re.compile(rf"^[-\s]*{re.escape(key)}: {re.escape(value)}\s*$", re.MULTILINE)
    return pattern.search(block) is not None


def run_checks(text: str) -> list[Finding]:
    findings: list[Finding] = []
    blocks = extract_data_blocks(text)
    keys = set(blocks.keys())

    findings.append(
        Finding(
            ok=keys == REQUIRED_DATA_KEYS,
            name="gateway_values.data_keys_exact",
            detail=f"expected exactly {REQUIRED_DATA_KEYS}, found {keys}",
        )
    )
    findings.append(
        Finding(
            ok=not (keys & FORBIDDEN_DATA_KEYS),
            name="gateway_values.no_values_yaml_wrapper",
            detail=(
                "expected no 'values.yaml'/'values' wrapper key (the incorrect, superseded schema) "
                f"- found {keys & FORBIDDEN_DATA_KEYS}"
            ),
        )
    )

    deployment = blocks.get("deployment", "")
    findings.append(Finding(ok=_has_field(deployment, "replicas", "1"), name="gateway_values.deployment.replicas_one", detail=f"expected 'replicas: 1' in the deployment patch, found: {deployment!r}"))
    findings.append(Finding(ok=_has_field(deployment, "name", "istio-proxy"), name="gateway_values.deployment.targets_istio_proxy_container", detail="expected the resource patch to target container name 'istio-proxy'"))
    for key, expected in (("cpu", "50m"), ("memory", "64Mi"), ("cpu", "200m"), ("memory", "128Mi")):
        findings.append(Finding(ok=_has_field(deployment, key, expected), name=f"gateway_values.deployment.resources[{key}: {expected}]", detail=f"expected {key}: {expected!r} in the deployment patch's container resources"))

    service = blocks.get("service", "")
    findings.append(Finding(ok=_has_field(service, "type", "NodePort"), name="gateway_values.service.type_nodeport", detail="expected 'type: NodePort' in the service patch"))
    findings.append(Finding(ok=_has_field(service, "port", "80"), name="gateway_values.service.listener_port_80", detail="expected 'port: 80' in the service patch"))
    findings.append(Finding(ok=_has_field(service, "nodePort", "30080"), name="gateway_values.service.nodeport_30080", detail="expected 'nodePort: 30080' in the service patch"))
    findings.append(Finding(ok=_has_field(service, "targetPort", "80"), name="gateway_values.service.targetport_80", detail="expected 'targetPort: 80' (the port the generated proxy actually binds) in the service patch"))

    service_account = blocks.get("serviceAccount", "")
    findings.append(Finding(ok=_has_field(service_account, "automountServiceAccountToken", "false"), name="gateway_values.serviceaccount.automount_false", detail="expected 'automountServiceAccountToken: false' in the serviceAccount patch"))
    findings.append(
        Finding(
            ok=not re.search(r"^[-\s]*name:", service_account, re.MULTILINE),
            name="gateway_values.serviceaccount.no_name_override_attempted",
            detail=(
                "the serviceAccount patch must never attempt to rename the generated ServiceAccount "
                "(Istio's controller owns that name deterministically as '<gateway-name>-istio') - "
                f"found a 'name:' key in: {service_account!r}"
            ),
        )
    )

    return findings

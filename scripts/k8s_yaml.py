"""
Minimal, dependency-free YAML-subset loader.

Kubernetes manifests rendered by ``kubectl kustomize`` use a bounded,
deterministic subset of YAML: block mappings, block sequences (aligned
with either their parent key's indentation or deeper), plain/quoted
scalars, and multi-document streams separated by ``---``. No anchors,
tags, flow collections, or multi-line block scalars are emitted for
this repository's manifests.

This module parses exactly that subset so repository validation does
not require a third-party YAML library. It is not a general-purpose
YAML parser: flow-style collections (``{a: b}``, ``[a, b]``) and
malformed/inconsistently-indented input raise ``ValueError`` rather
than being silently accepted.

Trusted-input contract: the only production caller is
``scripts/manifest_check.py``, which always feeds this loader the
machine-generated output of ``kubectl kustomize``. That renderer emits
consistently block-style, 2-space-indented YAML and always re-quotes
type-ambiguous scalars (e.g. ``"1.0"``, ``"yes"``, ``"on"``) so they
round-trip as strings. This loader relies on that upstream quoting
discipline - it implements only ``true``/``false``/``null`` recognition
plus quoted-string/int/float scalars, not the full YAML 1.1 "Norway
problem" ambiguous-scalar rule set (unquoted ``yes``/``no``/``on``/
``off``/etc). That is a deliberate scope limit, not an oversight: this
loader is not meant to accept hand-authored YAML in general.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


@dataclass
class _Line:
    indent: int
    text: str


def _preprocess(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for raw in text.split("\n"):
        stripped = raw.rstrip()
        if not stripped.strip():
            continue
        content = stripped.lstrip(" ")
        if content.startswith("#"):
            continue
        indent = len(stripped) - len(content)
        lines.append(_Line(indent=indent, text=content))
    return lines


def _scalar(token: str):
    token = token.strip()
    if token == "" or token == "~" or token == "null":
        return None
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token[0] in ("{", "["):
        raise ValueError(
            f"flow-style YAML is not supported by this loader: {token!r}; "
            "only block-style mappings/sequences are supported"
        )
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_key_value(content: str) -> tuple[str, str | None]:
    """Split 'key: value' / 'key:' on the first unquoted ': ' or trailing ':'."""
    in_quote = None
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == ":" and (i + 1 == n or content[i + 1] == " "):
            key = content[:i].strip()
            value = content[i + 1 :].strip()
            return key, (value if value != "" else None)
        i += 1
    raise ValueError(f"not a mapping entry: {content!r}")


def _is_seq_item(line: _Line) -> bool:
    return line.text == "-" or line.text.startswith("- ")


def _parse_block(lines: list[_Line], i: int, indent: int):
    """Parse the block starting at lines[i], which must sit at `indent`."""
    if i >= len(lines) or lines[i].indent != indent:
        return None, i
    if _is_seq_item(lines[i]):
        return _parse_sequence(lines, i, indent)
    return _parse_mapping(lines, i, indent)


def _nested_value(lines: list[_Line], i: int, key_indent: int):
    """Look up a mapping key's value on following lines (standard YAML rule:
    a nested mapping must be indented deeper; a nested sequence may align
    with the key itself)."""
    if i < len(lines):
        nxt = lines[i]
        if nxt.indent > key_indent or (nxt.indent == key_indent and _is_seq_item(nxt)):
            return _parse_block(lines, i, nxt.indent)
    return None, i


def _parse_mapping(lines: list[_Line], i: int, indent: int):
    mapping: dict = {}
    while i < len(lines) and lines[i].indent == indent and not _is_seq_item(lines[i]):
        key, value_text = _split_key_value(lines[i].text)
        i += 1
        if value_text is not None:
            mapping[key] = _scalar(value_text)
            continue
        value, i = _nested_value(lines, i, indent)
        mapping[key] = value
    return mapping, i


def _parse_sequence(lines: list[_Line], i: int, indent: int):
    items = []
    while i < len(lines) and lines[i].indent == indent and _is_seq_item(lines[i]):
        line = lines[i]
        rest = line.text[1:].lstrip(" ") if line.text != "-" else ""
        child_indent = indent + 2
        if rest == "":
            value, i = _parse_block(lines, i + 1, child_indent)
            items.append(value)
            continue
        try:
            _split_key_value(rest)
        except ValueError:
            # Bare scalar sequence item, e.g. "- ALL".
            items.append(_scalar(rest))
            i += 1
            continue
        # "- key: value" (possibly followed by sibling keys of the same
        # mapping at child_indent). Splice the inline remainder in as its
        # own line so the standard mapping parser can consume it and any
        # following sibling keys uniformly.
        spliced = [_Line(child_indent, rest)] + lines[i + 1 :]
        value, consumed = _parse_block(spliced, 0, child_indent)
        items.append(value)
        i = i + 1 + (consumed - 1)
    return items, i


def load_all(text: str) -> list[dict]:
    """Parse a multi-document YAML stream into a list of dicts."""
    documents = []
    for doc_text in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        lines = _preprocess(doc_text)
        if not lines:
            continue
        base_indent = lines[0].indent
        value, consumed = _parse_block(lines, 0, base_indent)
        if consumed != len(lines):
            unconsumed = lines[consumed]
            raise ValueError(
                f"failed to parse full YAML document: stopped at line {consumed + 1} of {len(lines)} "
                f"(unconsumed content {unconsumed.text!r} at indent {unconsumed.indent}); "
                "input uses inconsistent indentation or an unsupported construct"
            )
        if value:
            documents.append(value)
    return documents

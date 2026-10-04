#!/usr/bin/env python3
"""
DAY7: read-only inspection of a Makefile sequence target.

`make -n day7-check` is a dry run, but GNU make still EXECUTES recipe
lines that reference $(MAKE) (so nested makes can print); the Makefile
therefore empties the lock prefix under -n. This script is the
genuinely side-effect-free alternative: it only reads the Makefile text
and prints the `$(MAKE)`/`$(DAY7_MAKE)` steps of the named targets, in
order, with the profile each step runs under. It runs nothing and takes
no lock.

Usage: python3 scripts/make_sequence.py day7-check [day7-final-gate ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAKEFILE = Path(__file__).resolve().parent.parent / "Makefile"
_STEP = re.compile(r"\$\((DAY7_MAKE|MAKE)\)\s+([a-zA-Z0-9_-]+)")


def recipe(target: str, text: str) -> str:
    match = re.search(rf"^{re.escape(target)}:.*\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
    if match is None:
        raise KeyError(target)
    return match.group(1)


def steps(target: str, text: str) -> list[tuple[str, str]]:
    def profile(mk: str, name: str) -> str:
        if mk == "DAY7_MAKE":
            return "day7 profile via DAY7_MAKE"
        if name.startswith("day7-"):
            return "day7 target (sets its own Day 7 env)"
        if name.startswith("day8-"):
            return "day8 target (sets its own maops-k8s-day7 env or is cluster-free)"
        return "cluster-free/default"
    return [(profile(mk, name), name) for mk, name in _STEP.findall(recipe(target, text))]


def main() -> int:
    targets = sys.argv[1:] or ["day7-check"]
    text = MAKEFILE.read_text()
    for target in targets:
        try:
            found = steps(target, text)
        except KeyError:
            print(f"unknown target {target!r}", file=sys.stderr)
            return 1
        print(f"{target}:")
        for i, (profile, name) in enumerate(found, 1):
            print(f"  {i:2d}. {name}  [{profile}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

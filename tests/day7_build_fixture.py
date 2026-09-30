"""
DAY7 image contract - shared, cluster-free test fixture.

SAMPLE_BUILD is day7_build.sample_build(): synthetic config digests whose
pinned tags exist on no node. `pinned_build()` makes every
day7_strategy.active_build() call (apply_stage, verify_stable, baseline
checks) return it, without touching the private build store.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import day7_build

# Hermetic: no test may ever read the operator's real build store
# ($HOME/.local/state/.../day7-builds, exported by the Makefile). Any
# unpatched active_build() in a test fails closed on this path instead.
os.environ[day7_build.BUILD_ROOT_ENV] = "/nonexistent/maops-day7-test-build-root"

SAMPLE_BUILD = day7_build.sample_build()


def pinned_build(build: day7_build.Build = SAMPLE_BUILD):
    return mock.patch.object(day7_build, "load_current", return_value=build)


def running_images_pass():
    """Replaces the live running-image gate with a recorded PASS (tests of
    unrelated orchestration logic)."""
    import day7_running_images

    def _pass(rec, build, *args, **kwargs):
        return rec.record(True, f"{kwargs.get('label', 'running images')}: build {build.build_id} verified (test fixture)")

    return mock.patch.object(day7_running_images, "wait_for_running_images", side_effect=_pass)

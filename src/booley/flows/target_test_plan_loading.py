"""Build a Target's Test Run plan from the ``tests.toml`` of one checkout (ADR 0072).

The plan itself (:mod:`booley.flows.target_test_plan`) is pure domain policy;
this module is the one place that reads the file for it.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from booley.config.project_config import load_test_configuration, lookup_target_section
from booley.flows.target_test_plan import TargetTestPlan, build_target_test_plan

__all__ = ["load_target_test_plan"]


def load_target_test_plan(
    work_dir: Path | str,
    target: str,
    *,
    adapter: str,
    target_plusargs: Collection[str] = (),
) -> TargetTestPlan:
    """Read ``tests.toml`` of the checkout at *work_dir* and build *target*'s plan."""
    sections = load_test_configuration(work_dir)
    section = lookup_target_section(sections, target)
    return build_target_test_plan(
        target, section, adapter=adapter, target_plusargs=target_plusargs
    )

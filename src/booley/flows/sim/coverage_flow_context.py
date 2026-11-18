"""Translate Simulation's project inputs into coverage policy values."""

from __future__ import annotations

import tomllib
from pathlib import Path

from booley.config.coverage_waiver_inputs import parse_coverage_waiver_config
from booley.config.project_config import load_test_configuration_field
from booley.core.boundary import require_dict
from booley.core.config_paths import resolve_toml
from booley.runtime.booley_dir import resolve_checkout_booley_dir

from .coverage_invocation import CoverageProjectContext


def coverage_project_context(root: Path) -> CoverageProjectContext:
    data = resolve_checkout_booley_dir(root)
    config_path = resolve_toml(data)
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    flows = require_dict(config.get("flows", {}), field="flows")
    sim = require_dict(flows.get("sim", {}), field="flows.sim")
    if "coverage" in sim or "reset_included" in sim or "custom_main_hooks" in sim:
        raise ValueError(
            "Coverage Window/hook controls belong to Target flow_options.booley.coverage"
        )
    require_dict(config.get("coverage", {}), field="coverage")
    waivers = parse_coverage_waiver_config(config)
    return CoverageProjectContext(
        root,
        data,
        load_test_configuration_field(root, "tests"),
        waiver_config=waivers,
        skipped_tests=load_test_configuration_field(root, "skip"),
    )

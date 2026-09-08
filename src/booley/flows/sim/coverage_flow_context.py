"""Translate Simulation's project and Ticket inputs into coverage policy values."""

from __future__ import annotations

import os
import tomllib
from fractions import Fraction
from pathlib import Path

from booley.config.project_config import load_test_configuration_field
from booley.core.boundary import require_dict
from booley.core.config_paths import resolve_toml
from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.catalog import TargetCatalog

from .coverage_acceptance import CoverageAcceptance
from .coverage_campaign import DurableTargetIdentity
from .coverage_invocation import CoverageProjectContext
from .coverage_policy import CoverageCriterion, CoverageThreshold
from .coverage_waivers import CoverageWaiverConfig

_LEGACY = (
    "coverage_toggle",
    "coverage_fsm",
    "coverage_value",
    "coverage_branch",
    "coverage_expression",
    "coverage_mean",
)


def coverage_project_context(root: Path, state: DevelopmentState) -> CoverageProjectContext:
    data = resolve_project_dir(root)
    config_path = resolve_toml(data)
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    flows = require_dict(config.get("flows", {}), field="flows")
    sim = require_dict(flows.get("sim", {}), field="flows.sim")
    if "coverage" in sim or "reset_included" in sim or "custom_main_hooks" in sim:
        raise ValueError(
            "Coverage Window/hook controls belong to Target flow_options.booley.coverage"
        )
    coverage = require_dict(config.get("coverage", {}), field="coverage")
    raw = coverage.get("waivers")
    waivers = None
    if raw is not None:
        if not isinstance(raw, dict) or set(raw) != {"anchor", "directory"}:
            raise ValueError("coverage.waivers requires anchor and directory")
        waivers = CoverageWaiverConfig(**raw)
    policies = _coverage_policies(root, state)
    return CoverageProjectContext(
        root,
        data,
        load_test_configuration_field(root, "tests"),
        policies,
        waivers,
        load_test_configuration_field(root, "skip"),
    )


def coverage_acceptance(state: DevelopmentState, *, diagnostic: bool) -> CoverageAcceptance | None:
    if diagnostic or state._file_path is None:
        return None
    logs = os.environ.get("BOOLEY_LOGS_DIR")
    basis = {}
    ticket = os.environ.get("BOOLEY_TICKET_FILE")
    if ticket and Path(ticket).is_file():
        from booley.ticket_board.frontmatter import parse_frontmatter

        metadata, _body = parse_frontmatter(Path(ticket).read_text())
        basis = metadata.get("acceptance_basis", {})
    return CoverageAcceptance(
        state, Path(logs) if logs else None, os.environ.get("BOOLEY_EXECUTION_ID", ""), basis
    )


def _coverage_policies(root: Path, state: DevelopmentState) -> dict[str, CoverageCriterion]:
    catalog = TargetCatalog.build(root)
    policies = {}
    for key, entry in state.criteria.items():
        if any(key == legacy or key.startswith(legacy + "_") for legacy in _LEGACY):
            raise ValueError(
                f"Legacy {key}: replace with coverage: [{{targets: [...], metrics: {{...}}, tests: all}}]"
            )
        if not key.startswith("coverage_"):
            continue
        params = entry.params
        target = catalog.select(
            str(params.get("_target_selector", params.get("target", ""))), for_flow="sim"
        )
        if "_target_selector" in params and params.get("target") != target.identity:
            raise ValueError(f"{key}: Coverage Criterion Target identity is stale")
        CriteriaTemplate.from_yaml(
            {
                "mandatory": {
                    "coverage": [
                        {
                            "targets": [target.selector],
                            "metrics": params.get("metrics"),
                            "tests": params.get("tests"),
                        }
                    ]
                }
            }
        )
        metrics = params.get("metrics", {})
        thresholds = tuple(
            CoverageThreshold(metric, Fraction(str(policy["min_pct"])))
            for metric, policy in metrics.items()
        )
        tests = params.get("tests")
        if tests != "all" and (not isinstance(tests, list) or not tests):
            raise ValueError(f"{key}: tests must be all or an exact non-empty suite")
        policies[key] = CoverageCriterion(
            DurableTargetIdentity(target.identity),
            thresholds,
            None if tests == "all" else tuple(tests),
        )
    return policies

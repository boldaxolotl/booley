"""Translate Simulation's project and Ticket inputs into coverage policy values."""

from __future__ import annotations

import tomllib
from fractions import Fraction
from pathlib import Path

from booley.config.coverage_waiver_inputs import parse_coverage_waiver_config
from booley.config.project_config import load_test_configuration_field
from booley.core.boundary import require_dict
from booley.core.config_paths import resolve_toml
from booley.criteria.coverage import validate_coverage_metrics
from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecorder, NoAcceptanceRecorder
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog

from .coverage_acceptance import CoverageAcceptance
from .coverage_campaign import DurableTargetIdentity
from .coverage_invocation import CoverageProjectContext
from .coverage_policy import CoverageCriterion, CoverageThreshold

_LEGACY = (
    "coverage_toggle",
    "coverage_fsm",
    "coverage_value",
    "coverage_branch",
    "coverage_expression",
    "coverage_mean",
)


def coverage_project_context(root: Path, state: DevelopmentState) -> CoverageProjectContext:
    data = resolve_checkout_project_dir(root)
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
    policies = _coverage_policies(root, state)
    return CoverageProjectContext(
        root,
        data,
        load_test_configuration_field(root, "tests"),
        policies,
        waivers,
        load_test_configuration_field(root, "skip"),
    )


def coverage_acceptance(
    state: DevelopmentState,
    recorder: AcceptanceRecorder,
    *,
    diagnostic: bool,
) -> CoverageAcceptance | None:
    if diagnostic or state._file_path is None or isinstance(recorder, NoAcceptanceRecorder):
        return None
    return CoverageAcceptance(state, recorder)


def _coverage_policies(root: Path, state: DevelopmentState) -> dict[str, CoverageCriterion]:
    catalog = TargetCatalog.build(root)
    grouped: dict[str, tuple[str, dict[str, dict[str, int | float]], str | list[str]]] = {}
    for key, entry in state.criteria.items():
        _reject_legacy_coverage(key)
        if not key.startswith("coverage_"):
            continue
        params = entry.params
        target = catalog.select(
            str(params.get("_target_selector", params.get("target", ""))), for_flow="sim"
        )
        if "_target_selector" in params and params.get("target") != target.identity:
            raise ValueError(f"{key}: Coverage Criterion Target identity is stale")
        metrics = validate_coverage_metrics(params.get("metrics"), field=key)
        tests = params.get("tests")
        if tests != "all" and (not isinstance(tests, list) or not tests):
            raise ValueError(f"{key}: tests must be all or an exact non-empty suite")
        flow_key = (
            f"coverage_{target.name}"
            if key in state.flow_key_aliases.get(f"coverage_{target.name}", [])
            else key
        )
        previous = grouped.get(target.identity)
        if previous is None:
            grouped[target.identity] = (flow_key, dict(metrics), tests)
        else:
            previous_key, previous_metrics, previous_tests = previous
            if (
                previous_key != flow_key
                or previous_tests != tests
                or set(previous_metrics) & set(metrics)
            ):
                raise ValueError(
                    f"{key}: conflicting Coverage Criteria for Target {target.identity}"
                )
            previous_metrics.update(metrics)
    policies = {}
    for identity, (key, metrics, tests) in grouped.items():
        thresholds = tuple(
            CoverageThreshold(metric, Fraction(str(policy["min_pct"])))
            for metric, policy in metrics.items()
        )
        policies[key] = CoverageCriterion(
            DurableTargetIdentity(identity),
            thresholds,
            None if tests == "all" else tuple(tests),
        )
    return policies


def _reject_legacy_coverage(key: str) -> None:
    if any(key == legacy or key.startswith(legacy + "_") for legacy in _LEGACY):
        raise ValueError(
            f"Legacy {key}: replace with coverage: [{{targets: [...], metrics: {{...}}, tests: all}}]"
        )

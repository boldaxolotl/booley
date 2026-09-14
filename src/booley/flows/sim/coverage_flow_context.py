"""Translate Simulation's project and Ticket inputs into coverage policy values."""

from __future__ import annotations

import tomllib
from fractions import Fraction
from pathlib import Path

from booley.config.project_config import load_test_configuration_field
from booley.core.boundary import require_dict
from booley.core.config_paths import resolve_toml
from booley.criteria.state import DevelopmentState
from booley.flows.execution_persistence import AcceptanceRecorder
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


def coverage_acceptance(
    state: DevelopmentState,
    recorder: AcceptanceRecorder,
    *,
    diagnostic: bool,
) -> CoverageAcceptance | None:
    if diagnostic or state._file_path is None:
        return None
    return CoverageAcceptance(state, recorder)


def _validated_metrics(key: str, value: object) -> dict[str, dict[str, int | float]]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{key}: Coverage metrics must be a nonempty mapping")
    for metric, policy in value.items():
        if metric not in {"line", "branch", "expression", "toggle", "cover_property"}:
            raise ValueError(f"{key}: unknown Coverage metric {metric!r}")
        if not isinstance(policy, dict) or set(policy) != {"min_pct"}:
            raise ValueError(f"{key}: {metric} requires min_pct")
        threshold = policy["min_pct"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 100
        ):
            raise ValueError(f"{key}: {metric}.min_pct must be in (0, 100]")
    return value


def _coverage_policies(root: Path, state: DevelopmentState) -> dict[str, CoverageCriterion]:
    catalog = TargetCatalog.build(root)
    grouped: dict[str, tuple[str, dict[str, dict[str, int | float]], str | list[str]]] = {}
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
        metrics = _validated_metrics(key, params.get("metrics"))
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

"""Goal Mode is removed (#3511); only allowed modules import Criteria (#3506)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from tests.architecture.import_graph import analyze_imports

GOALS_SOURCE = Path(__file__).resolve().parents[2] / "src" / "booley" / "goals"


def test_legacy_goal_presentation_module_is_gone() -> None:
    assert importlib.util.find_spec("booley.review.goal_presentation_v1") is None


SOURCE_ROOT = GOALS_SOURCE.parent
# Every module outside ``booley.criteria`` that may still import it, and the issue that
# removes the dependency (issue #3506). "Evidence and staleness":
# records or reads Criteria state for Interactive evidence. "Criteria catalog": renders
# or validates the Criteria definitions.
_ALLOWED: dict[str, str] = {
    "booley.flows.endpoint_acceptance": "evidence and staleness #3512",
    "booley.flows.endpoint_events": "evidence and staleness #3512",
    "booley.flows.endpoint_report_criteria": "evidence and staleness #3512",
    "booley.flows.endpoint_state": "evidence and staleness #3512",
    "booley.flows.execution_persistence": "evidence and staleness #3512",
    "booley.flows.sim.acceptance": "evidence and staleness #3512",
    "booley.flows.sim.coverage_flow_context": "evidence and staleness #3512",
    "booley.flows.sim.coverage_projection": "evidence and staleness #3512",
    "booley.flows.sim.elab_only": "evidence and staleness #3512",
    "booley.flows.sim.flow": "evidence and staleness #3512",
    "booley.flows.sim.flow_resume": "evidence and staleness #3512",
    "booley.flows.sim.standalone": "evidence and staleness #3512",
    "booley.flows.sim.workload": "evidence and staleness #3512",
    "booley.mcp.endpoint_validation": "Criteria catalog #3512",
    "booley.mcp.registry": "Criteria catalog #3512",
    "booley.mcp.unbound_recorder": "evidence and staleness #3512",
}


def _is_inside(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _criteria_importers() -> frozenset[str]:
    """Modules outside ``booley.criteria`` that import it."""
    return frozenset(
        dependency.source
        for dependency in analyze_imports(SOURCE_ROOT)
        if _is_inside(dependency.target, "booley.criteria")
        and not _is_inside(dependency.source, "booley.criteria")
    )


def test_only_criteria_consumers_import_criteria() -> None:
    """New importers and stale rows both fail."""
    assert _criteria_importers() == frozenset(_ALLOWED)


def test_no_specialist_module_imports_criteria() -> None:
    """Specialists read and write no Criterion evidence (#4301)."""
    assert {m for m in _criteria_importers() if _is_inside(m, "booley.specialists")} == set()


REPO_ROOT = SOURCE_ROOT.parents[1]
_REMOVED_MODULES = (
    "booley.goals",
    "booley.mcp.goal_tools",
    "booley.mcp.goal_changes",
    "booley.mcp.goal_completion",
    "booley.mcp.goal_flow_runner",
    "booley.mcp.goal_freshness",
    "booley.mcp.goal_generated_inputs",
    "booley.mcp.goal_waivers",
    "booley.mcp.sim_baseline_execution",
    "booley.mcp.sim_baseline_jobs",
    "booley.mcp.synth_baseline_execution",
    "booley.mcp.baseline_preparation",
    "booley.mcp.project_flow_loader",
    "booley.config.goals",
    "booley.review.goal_package",
    "booley.harness.cli_goal_notice",
    "booley.harness.goal_status_view",
    "booley.harness.dashboard.doctor",
    "booley.targets.goal_diff",
    "booley.targets.surface_diff",
    "booley.flows.frozen_test_runs",
    "booley.flows.baseline_pins",
    "booley.flows.review_scope",
    "booley.flows.sim.coverage_waiver_promotion",
    "booley.flows.sim.waiver_candidates",
    "booley.specialists.coverage_candidate_recording",
    "booley.runtime.job_wait",
    "booley.dev_support.criteria_reference",
)


def _package_of(path: Path) -> list[str]:
    """The dotted package a ``.py`` file under ``src/``, ``tests/`` or ``.github/scripts/`` is in."""
    for root in (REPO_ROOT / "src", REPO_ROOT):
        if path.is_relative_to(root):
            parts = list(path.relative_to(root).parent.parts)
            return parts
    return []  # pragma: no cover - every scanned file is under REPO_ROOT


def _imported_names(path: Path) -> list[str]:
    """Every dotted name *path* imports, relative imports resolved, lazy ones included."""
    package = _package_of(path)
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            names.append(module)
            names += [f"{module}.{alias.name}" for alias in node.names]
    return names


def test_goal_mode_package_is_gone_and_nothing_imports_it() -> None:
    """Goal Mode is removed; a raw walk of every import catches names ``analyze_imports`` drops."""
    assert importlib.util.find_spec("booley.goals") is None
    assert not GOALS_SOURCE.exists()
    offenders: dict[str, list[str]] = {}
    for root in ("src", "tests", ".github/scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            hits = [
                name
                for name in _imported_names(path)
                if any(name == gone or name.startswith(f"{gone}.") for gone in _REMOVED_MODULES)
            ]
            if hits:
                offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(hits))
    assert offenders == {}

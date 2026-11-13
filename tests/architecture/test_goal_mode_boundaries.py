"""Goal Mode keeps one format per artifact (issue #2407); only allowed modules import its packages (#3506)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from tests.architecture.import_graph import analyze_imports

GOALS_SOURCE = Path(__file__).resolve().parents[2] / "src" / "booley" / "goals"
MIGRATION = "booley.runtime.booley_dir_migration"


def test_legacy_goal_presentation_module_is_gone() -> None:
    assert importlib.util.find_spec("booley.review.goal_presentation_v1") is None


def _migration_imports(path: Path) -> list[str]:
    """Every statement in *path*, lazy ones included, that imports the Directory migration."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name == MIGRATION]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            package, _, leaf = MIGRATION.rpartition(".")
            if node.module == MIGRATION or (
                node.module == package and any(alias.name == leaf for alias in node.names)
            ):
                found.append(node.module)
    return found


def test_goal_code_does_not_consult_the_directory_migration() -> None:
    sources = sorted(GOALS_SOURCE.rglob("*.py"))
    assert sources, "the Goal source tree moved; update this test"
    offenders = {
        str(path.relative_to(GOALS_SOURCE)): imports
        for path in sources
        if (imports := _migration_imports(path))
    }
    assert offenders == {}


SOURCE_ROOT = GOALS_SOURCE.parent
_GOAL_PACKAGES = ("goals", "criteria")
# Every module outside ``booley.goals`` and ``booley.criteria`` that may still import
# them, the packages it imports, and the issue that removes the dependency (issue #3506).
# "Goal-only": acts only when a Goal Record or binding exists. "Evidence and staleness":
# records or reads Criteria state for Interactive evidence. "Criteria catalog": renders
# or validates the Criteria definitions.
_ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    "booley.dev_support.criteria_reference": (
        frozenset({"criteria", "goals"}),
        "Criteria catalog #3512",
    ),
    "booley.dev_support.demo_contract_codec": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.dev_support.flow_specialist_reference": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.flows.baseline_pins": (frozenset({"criteria"}), "Goal-only #3511"),
    "booley.flows.endpoint_acceptance": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.endpoint_events": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.endpoint_report_criteria": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
    "booley.flows.endpoint_state": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.execution_persistence": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
    "booley.flows.sim.acceptance": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.sim.coverage_flow_context": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
    "booley.flows.sim.coverage_projection": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
    "booley.flows.sim.elab_only": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.sim.flow": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.sim.flow_resume": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.sim.standalone": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.flows.sim.workload": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.harness.booley": (
        frozenset({"criteria", "goals"}),
        "Goal-only #3511; criteria sections Criteria catalog #3512",
    ),
    "booley.harness.cli_goal_notice": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.dashboard.app": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.dashboard.doctor": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.dashboard.jobs": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.dashboard.specialist_form": (
        frozenset({"goals"}),
        "Goal-only #3511; spec prefill and review focuses read the Goal model and store",
    ),
    "booley.harness.dashboard.model": (frozenset({"criteria", "goals"}), "Goal-only #3511"),
    "booley.harness.goal_status_view": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.init_cmd": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.setup.directory_migration": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.worktree_list": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.harness.worktree_rm": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.call_context": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.catalog": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.endpoint_validation": (frozenset({"criteria"}), "Criteria catalog #3512"),
    "booley.mcp.flow_execution_selection": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.goal_changes": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.goal_completion": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.goal_flow_runner": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.goal_freshness": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.goal_generated_inputs": (frozenset({"criteria", "goals"}), "Goal-only #3511"),
    "booley.mcp.goal_tools": (frozenset({"criteria", "goals"}), "Goal-only #3511"),
    "booley.mcp.goal_waivers": (frozenset({"criteria", "goals"}), "Goal-only #3511"),
    "booley.mcp.job_lifecycle": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.job_results": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.registry": (frozenset({"criteria"}), "Criteria catalog #3512"),
    "booley.mcp.server": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.sim_baseline_execution": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.sim_baseline_jobs": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.synth_baseline_execution": (frozenset({"goals"}), "Goal-only #3511"),
    "booley.mcp.unbound_recorder": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.specialists.coverage_analyst": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
    "booley.specialists.reviewer": (frozenset({"criteria"}), "evidence and staleness #3512"),
    "booley.specialists.verification_freshness": (
        frozenset({"criteria"}),
        "evidence and staleness #3512",
    ),
}


def _is_inside(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _goal_package_importers() -> dict[str, frozenset[str]]:
    """Packages of ``booley.goals``/``booley.criteria`` imported by modules outside them."""
    imported: dict[str, set[str]] = {}
    for dependency in analyze_imports(SOURCE_ROOT):
        if any(_is_inside(dependency.source, f"booley.{name}") for name in _GOAL_PACKAGES):
            continue
        for name in _GOAL_PACKAGES:
            if _is_inside(dependency.target, f"booley.{name}"):
                imported.setdefault(dependency.source, set()).add(name)
    return {source: frozenset(names) for source, names in imported.items()}


def test_only_goal_mode_and_evidence_consumers_import_goals_or_criteria() -> None:
    """New importers, new packages for allowed importers, and stale rows all fail."""
    allowed = {module: packages for module, (packages, _reason) in _ALLOWED.items()}
    assert _goal_package_importers() == allowed

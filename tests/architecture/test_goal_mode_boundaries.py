"""Goal Mode, the Criteria package and the Result Cache are removed (#3511, #4303)."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

GOALS_SOURCE = Path(__file__).resolve().parents[2] / "src" / "booley" / "goals"


def test_legacy_goal_presentation_module_is_gone() -> None:
    assert importlib.util.find_spec("booley.review.goal_presentation_v1") is None


SOURCE_ROOT = GOALS_SOURCE.parent
REPO_ROOT = SOURCE_ROOT.parents[1]
_REMOVED_MODULES = (
    "booley.flows.endpoint_report_criteria",
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
# Removed with the Criteria and Result Cache packages (#4303). A string constant naming one of
# these (``importlib.import_module``, ``monkeypatch.setattr``) is a hit too.
_REMOVED_4303 = (
    "booley.criteria",
    "booley.result_cache",
    "booley.core.source_categories",
    "booley.runtime.git_ignore",
    "tests.criteria",
    "tests.result_cache",
    "tests.criterion_endpoint_support",
)
_REMOVED_TEXT = re.compile(r"\bbooley\.(criteria|result_cache)\b")


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


def _is_removed(name: str, modules: tuple[str, ...] = _REMOVED_MODULES + _REMOVED_4303) -> bool:
    return any(name == gone or name.startswith(f"{gone}.") for gone in modules)


def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _offenders(repo_root: Path, skip: Path | None = None) -> dict[str, list[str]]:
    """Files under ``src/``, ``tests/`` and ``.github/`` that name a removed module."""
    offenders: dict[str, list[str]] = {}
    for root in ("src", "tests", ".github"):
        for path in sorted((repo_root / root).rglob("*")):
            if not path.is_file() or path == skip:
                continue
            hits: list[str] = []
            if path.suffix == ".py":
                hits = [n for n in _imported_names(path) if _is_removed(n)]
                hits += [s for s in _string_constants(path) if _is_removed(s, _REMOVED_4303)]
            elif root == ".github":
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                hits = [m.group(0) for m in _REMOVED_TEXT.finditer(text)]
            if hits:
                offenders[path.relative_to(repo_root).as_posix()] = sorted(set(hits))
    return offenders


def test_removed_modules_are_gone_and_nothing_imports_them() -> None:
    """A raw walk of every import, string constant and CI script catches names ``analyze_imports`` drops."""
    assert importlib.util.find_spec("booley.goals") is None
    assert not GOALS_SOURCE.exists()
    assert _offenders(REPO_ROOT, skip=Path(__file__).resolve()) == {}


def test_criteria_and_result_cache_packages_are_gone() -> None:
    for name in ("booley.criteria", "booley.result_cache"):
        assert importlib.util.find_spec(name) is None
        assert not (SOURCE_ROOT / name.split(".")[1]).exists()
    assert not (SOURCE_ROOT / "data" / "criteria.toml").exists()


def test_import_guard_catches_every_import_form(tmp_path: Path) -> None:
    seeds = {
        "src/a.py": "def f():\n    from booley.criteria.state import X\n",
        "src/b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import booley.result_cache\n",
        "tests/c.py": "import importlib\nimportlib.import_module('booley.result_cache')\n",
        ".github/scripts/d.sh": "python - <<'PY'\nimport booley.criteria\nPY\n",
        ".github/workflows/e.yml": 'run: python -c "import booley.result_cache"\n',
    }
    for rel, text in seeds.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
    (tmp_path / "tests" / "ok.py").write_bytes(b"import booley.flows\n")
    assert set(_offenders(tmp_path)) == set(seeds)

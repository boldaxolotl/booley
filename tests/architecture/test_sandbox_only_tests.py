"""The Sandbox-only marker never removes the last Windows test of host-reachable code.

Windows runs host code only: ``HOST`` and ``EITHER`` commands. Tests marked ``sandbox_only``
skip there, so a module a host command can import must keep at least one test outside the
Sandbox-only registry. The reachability model lives in ``sandbox_reach``.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.architecture.sandbox_reach import HostReach

_TESTS_ROOT = Path(__file__).parents[1]


@cache
def _reach() -> HostReach:
    from tests.architecture.sandbox_reach import host_reach

    return host_reach()


@cache
def _coverage() -> dict[str, frozenset[str]]:
    from tests.architecture.sandbox_reach import load_source_graph, module_test_coverage

    return module_test_coverage(load_source_graph().modules)


def _format_uncovered(
    problems: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
) -> str:
    return "\n".join(
        f"{module}\n  covered only by: {', '.join(tests)}\n  host path: {' -> '.join(path)}"
        for module, (tests, path) in problems.items()
    )


def test_no_host_module_is_covered_only_by_sandbox_only_tests() -> None:
    from tests.architecture.sandbox_reach import uncovered_host_modules
    from tests.sandbox_only import is_sandbox_only

    problems = uncovered_host_modules(_reach().paths, _coverage(), is_sandbox_only)

    assert not problems, (
        "Host-reachable modules whose tests are all Sandbox-only (they would have no Windows "
        "coverage). Move one covering test to WINDOWS_HOST_TESTS in tests/sandbox_only.py:\n"
        + _format_uncovered(problems)
    )


def test_guard_reports_host_module_whose_tests_are_all_sandbox_only() -> None:
    from tests.architecture.sandbox_reach import uncovered_host_modules

    reachable = {"pkg.a": ("pkg.main", "pkg.a"), "pkg.b": ("pkg.main", "pkg.b")}
    coverage = {
        "pkg.a": frozenset({"tests/x/test_a.py"}),
        "pkg.b": frozenset({"tests/x/test_b.py", "tests/y/test_b.py"}),
        "pkg.sandbox": frozenset({"tests/x/test_sandbox.py"}),
    }

    problems = uncovered_host_modules(
        reachable, coverage, lambda test: test.startswith("tests/x/")
    )

    assert problems == {"pkg.a": (("tests/x/test_a.py",), ("pkg.main", "pkg.a"))}


# Modules the brief names: the Goal store, Goalset loading, setup's cleanup selection, and the
# Flow modules Doctor imports.
@pytest.mark.parametrize(
    "module",
    [
        "booley.goals.store",
        "booley.goals.goalsets",
        "booley.flows.cli_arguments",
        "booley.flows.execution",
        "booley.flows.sim.config",
        "booley.flows.target_test_plan",
        "booley.flows.test_runs",
    ],
)
def test_named_host_modules_keep_windows_tests(module: str) -> None:
    from tests.sandbox_only import is_sandbox_only

    assert module in _reach().paths, f"{module} is no longer host reachable"
    windows_tests = [test for test in _coverage().get(module, ()) if not is_sandbox_only(test)]
    assert windows_tests, f"no test of {module} runs on Windows"


def test_declared_sandbox_only_lazy_imports_are_current() -> None:
    from tests.architecture.sandbox_reach import (
        SANDBOX_ONLY_LAZY_IMPORTS,
        host_reach,
        internal_callers,
        load_source_graph,
        referencing_modules,
    )

    graph = load_source_graph()
    full = _reach()
    for cut in SANDBOX_ONLY_LAZY_IMPORTS:
        edges = [
            edge
            for edge in graph.edges
            if edge.source == cut.source and edge.target in cut.targets and edge.function
        ]
        assert {edge.function for edge in edges} == set(cut.functions), (
            f"{cut.source}: enclosing functions of the cut imports changed; re-review the cut"
        )
        assert {edge.target for edge in edges} == set(cut.targets), (
            f"{cut.source}: a cut target is no longer a function-level import"
        )
        assert referencing_modules(full.paths, cut.source, cut.functions) == cut.callers, (
            f"{cut.source}: host-reachable modules now reference the cut functions"
        )
        assert internal_callers(cut.source, cut.functions) == cut.internal_callers, (
            f"{cut.source}: the cut functions have a new caller inside the module"
        )
        for target in cut.targets:
            trimmed = replace(cut, targets=tuple(t for t in cut.targets if t != target))
            smaller = [trimmed if other is cut else other for other in SANDBOX_ONLY_LAZY_IMPORTS]
            assert len(host_reach(smaller).paths) > len(full.paths), (
                f"{cut.source} -> {target} no longer shrinks the host-reachable set; drop it"
            )


def test_endpoint_help_roots_are_derived() -> None:
    from tests.architecture.sandbox_reach import (
        HARNESS_MODULE,
        SOURCE_ROOT,
        endpoint_modules,
        pre_venue_commands,
    )

    tree = ast.parse((SOURCE_ROOT / "harness" / "booley.py").read_text(encoding="utf-8"))
    reach = _reach()

    assert pre_venue_commands(tree) == {"flow", "specialist"}
    # Endpoint help for those commands runs before the venue guard, so their handlers are host
    # code, while the Goal and Dashboard handlers stay Sandbox-only.
    assert {"_cmd_flow", "_cmd_specialist", "_load_mcp_tool_class"} <= reach.harness_functions
    assert not {"_cmd_goal", "_cmd_dashboard"} & reach.harness_functions
    endpoints = endpoint_modules()
    assert endpoints
    assert endpoints <= reach.paths.keys() and HARNESS_MODULE in reach.paths


def test_registry_is_the_only_sandbox_only_marker_source() -> None:
    from tests.sandbox_only import (
        SANDBOX_ONLY_PATHS,
        WINDOWS_HOST_TESTS,
        is_sandbox_only,
    )

    root = _TESTS_ROOT.parent
    missing = [
        path for path in (*SANDBOX_ONLY_PATHS, *WINDOWS_HOST_TESTS) if not (root / path).exists()
    ]
    assert not missing, f"registry names paths that do not exist: {missing}"
    directories = [path for path in SANDBOX_ONLY_PATHS if path.endswith("/")]
    for path in WINDOWS_HOST_TESTS:
        assert any(path.startswith(directory) for directory in directories), path
        assert not is_sandbox_only(path)
    stray = [
        path.relative_to(root).as_posix()
        for path in _TESTS_ROOT.rglob("*.py")
        if path.name != "sandbox_only.py" and _applies_marker(path)
    ]
    assert not stray, f"apply sandbox_only only through tests/sandbox_only.py: {stray}"


def _applies_marker(path: Path) -> bool:
    """True when a module writes ``pytest.mark.sandbox_only`` or ``mark.sandbox_only``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == "sandbox_only"
        and isinstance(node.value, ast.Attribute | ast.Name)
        and getattr(node.value, "attr", getattr(node.value, "id", "")) == "mark"
        for node in ast.walk(tree)
    )

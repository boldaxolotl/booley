#!/usr/bin/env python3
"""Choose the test modules the `flake-stress` CI job repeats for a pull request.

A pull request's added or modified test modules run many times on Linux and
Windows (pytest-repeat ``--count`` spread over xdist workers), so a test that
fails only sometimes is caught before it reaches the merge queue. Deleted
modules are ignored and a renamed module is stressed under its new path.
Modules marked ``native_bwave`` as a whole are skipped: they need the Rust
binary and run only in bwave-integration, and the job deselects that marker.

Cost cap: one repeat count serves the whole job, sized so the estimated serial
Windows test time stays within ``BUDGET_SECONDS``. A module's estimate is its
recorded slow-test time in the Windows timing model, floored at
``MODULE_FLOOR_SECONDS`` (new modules have no history). The count is at most
``MAX_COUNT``. When even ``MIN_COUNT`` repeats of every changed module would
exceed the budget (a broad refactor), only the added modules are stressed; if
they still do not fit, nothing is stressed. ``unstressed`` names the modules
the cap dropped so the classifier can report them.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_COUNT = 10
MIN_COUNT = 3
# Serial test-seconds on a Windows runner; four xdist workers make this roughly
# ten minutes of wall time, inside the job's 25-minute timeout.
BUDGET_SECONDS = 2400.0
MODULE_FLOOR_SECONDS = 5.0
# Mergify validates queued pull requests on draft PRs from these branches. The
# PR already passed flake-stress, so the queue skips it.
QUEUE_BRANCH_PREFIX = "mergify/merge-queue/"
TIMING_MODEL = Path(__file__).parents[2] / ".github/ci/windows-test-timings.json"
# tests/fixtures/ holds sample projects whose tests run only inside them.
_EXCLUDED_PREFIXES = ("tests/fixtures/",)
_ADDED_STATUSES = frozenset("AC")
_CHANGED_STATUSES = frozenset("ACMRT")


@dataclass(frozen=True)
class StressPlan:
    """The modules to repeat, the repeat count, and modules the cost cap dropped."""

    modules: tuple[str, ...]
    count: int
    unstressed: tuple[str, ...]


def stress_enabled(event_name: str, head_ref: str) -> bool:
    """Return whether this run is an ordinary pull request (not a queue draft)."""
    return event_name == "pull_request" and not head_ref.startswith(QUEUE_BRANCH_PREFIX)


def _is_test_module(path: str) -> bool:
    """Match pytest's default ``python_files`` patterns under tests/."""
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/")
        and not path.startswith(_EXCLUDED_PREFIXES)
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _marked_native(source: Path) -> bool:
    """Return whether a module-level ``pytestmark`` applies ``native_bwave``."""
    try:
        tree = ast.parse(source.read_bytes(), filename=str(source))
    except SyntaxError:
        return False  # stress it anyway so pytest reports the broken module
    return any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        )
        and any(
            isinstance(mark, ast.Attribute) and mark.attr == "native_bwave"
            for mark in ast.walk(node.value)
        )
        for node in tree.body
    )


def changed_test_modules(
    entries: Iterable[tuple[str, tuple[str, ...]]], repo: Path
) -> dict[str, bool]:
    """Map each changed, runnable test module in ``repo`` to whether the diff added it.

    ``entries`` are ``git diff --name-status`` records: a status letter and its
    paths, the last of which is the post-change path.
    """
    modules: dict[str, bool] = {}
    for status, paths in entries:
        kind, path = status[:1], paths[-1]
        if kind in _CHANGED_STATUSES and _is_test_module(path) and not _marked_native(repo / path):
            modules[path] = kind in _ADDED_STATUSES
    return modules


def load_module_costs(model_path: Path = TIMING_MODEL) -> dict[str, float]:
    """Sum the Windows timing model's recorded test seconds per module."""
    model = json.loads(model_path.read_text(encoding="utf-8"))
    tests = model.get("tests") if isinstance(model, dict) else None
    if not isinstance(tests, dict):
        raise ValueError(f"{model_path} has no tests mapping")
    costs: dict[str, float] = {}
    for node_id, seconds in tests.items():
        module = node_id.split("::", 1)[0]
        costs[module] = costs.get(module, 0.0) + float(seconds)
    return costs


def _fitting_count(modules: Iterable[str], costs: Mapping[str, float]) -> int:
    total = sum(max(MODULE_FLOOR_SECONDS, costs.get(module, 0.0)) for module in modules)
    return min(MAX_COUNT, int(BUDGET_SECONDS // total)) if total else 0


def plan_stress(modules: Mapping[str, bool], costs: Mapping[str, float]) -> StressPlan:
    """Apply the cost cap to the changed modules (see the module docstring)."""
    everything = tuple(sorted(modules))
    added = tuple(module for module in everything if modules[module])
    for selected in (everything, added):
        count = _fitting_count(selected, costs)
        if selected and count >= MIN_COUNT:
            dropped = tuple(module for module in everything if module not in selected)
            return StressPlan(selected, count, dropped)
    return StressPlan((), 0, everything)

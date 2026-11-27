"""Read-only Target and Test Run reads behind the web Run form.

Nothing here writes: the Test Runs read uses the pre-setup, in-process inspection
(no ``fusesoc run --setup``, no build directory, no subprocess) and is skipped
whenever it would have to write Stealth core projections or the private FuseSoC
registry.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError
from booley.flows.execution import FlowConfigError, flow_enabled
from booley.flows.target_test_plan_loading import load_checkout_target_test_plan
from booley.fusesoc.core_projection import (
    CoreProjectionError,
    projection_enabled,
    projection_issues,
    registry_issues,
)
from booley.targets.domain import FuseSocError, TargetHandle
from booley.targets.target_surface import collect_surface

# What a read of a checkout can fail with: FuseSoC and Target errors, unreadable
# files, and a malformed tests.toml.
_READ_ERRORS = (FuseSocError, OSError, ValueError)
# A Flow that is not Target-aware but drives another Flow's Targets: the Mutation Flow runs one
# Simulation Target, so Targets offer it and its Target picker lists what the Simulation Flow can drive.
TARGET_KIND_OF_FLOW = {"mutation": "sim"}
# What a Run form says to do when it cannot list a value to pick.
TYPE_TARGETS = "Type Targets in the extra arguments."
TYPE_RUNS = "Type Test Runs in the extra arguments."
NO_FITTING_TARGET = (
    "No Target here can be driven by this Flow; type Targets in the extra arguments."
)


def first_line(error: BaseException) -> str:
    """The first non-empty line of an error message, or the exception's type name."""
    return next(
        (line.strip() for line in str(error).splitlines() if line.strip()), type(error).__name__
    )


def enabled_flows(flows: Iterable[str], worktree: Path) -> tuple[str, ...]:
    """The given Flows, in order, that ``[flows.<name>].enabled`` leaves on in *worktree*.

    A setting that cannot be read (a retired ``[flows]`` key, a refused Booley Directory layout)
    keeps its Flow listed: the Flow's own run reports the problem.
    """
    kept: list[str] = []
    for flow in flows:
        try:
            if not flow_enabled(flow, worktree):
                continue
        except (FlowConfigError, BooleyLayoutError):
            pass
        kept.append(flow)
    return tuple(kept)


@dataclass(frozen=True)
class TargetListRow:
    """One listed Target: ``flows`` are the Flows that could drive it."""

    selector: str
    flows: tuple[str, ...]


@dataclass(frozen=True)
class TargetListing:
    """The Targets of one worktree; ``problem`` explains a listing that failed as a whole."""

    rows: tuple[TargetListRow, ...]
    problem: str = ""


def targets_for(listing: TargetListing, flow: str) -> tuple[str, ...]:
    """The selectors of the Targets *flow* can drive: a Flow that is not Target-aware uses its kind's."""
    drives = TARGET_KIND_OF_FLOW.get(flow, flow)
    return tuple(row.selector for row in listing.rows if drives in row.flows)


def targets_problem(listing: TargetListing, flow: str) -> str:
    """Why a Run form offers *flow* no Target and what to do; empty when it offers one."""
    if listing.problem:
        return f"Targets unavailable: {listing.problem}. {TYPE_TARGETS}"
    return "" if targets_for(listing, flow) else NO_FITTING_TARGET


@dataclass(frozen=True)
class RunChoices:
    """The Test Runs one Target offers as ``--test`` values, or why they cannot be listed.

    Not called ``Test*``: pytest would try to collect such a class.
    """

    runs: tuple[str, ...]
    problem: str = ""


def runs_problem(choices: RunChoices) -> str:
    """Why the Test Runs cannot be listed and what to do; empty when they can."""
    return f"{choices.problem}. {TYPE_RUNS}" if choices.problem else ""


def inspection_blocker(worktree: Path) -> str:
    """Why inspecting a Target of *worktree* would write; empty when it would write nothing.

    Preparing FuseSoC's library view writes the Stealth core projections and
    the private registry when they are missing or stale. With Stealth off it
    writes no projection, so leftover projection files do not block.
    """
    try:
        projections = projection_issues(worktree) if projection_enabled(worktree) else ()
        issues = (*projections, *registry_issues(worktree))
    except (CoreProjectionError, OSError) as exc:
        return f"core projections cannot be checked: {first_line(exc)}"
    if not issues:
        return ""
    return (
        "core projections need a refresh (any Booley Flow run in this worktree refreshes them): "
        + ", ".join(map(str, issues))
    )


def _row(handle: TargetHandle) -> TargetListRow:
    return TargetListRow(handle.selector, handle.drivable_by)


class TargetsReader:
    """Reads the Targets of a worktree; every method is safe on a worker thread."""

    def __init__(self) -> None:
        # FuseSoC's cached setup is not thread-safe: one Test Run read at a time.
        self._lock = threading.Lock()

    def listing(self, worktree: Path) -> TargetListing:
        """The Targets of *worktree*, from its ``.core`` files (YAML reads only)."""
        try:
            surface = collect_surface(worktree)
        except _READ_ERRORS as exc:
            return TargetListing((), first_line(exc))
        return TargetListing(tuple(_row(handle) for handle in surface.entries()))

    def test_runs(self, worktree: Path, selector: str) -> RunChoices:
        """The Test Runs of one Target: each Test, then ``test+variant`` per Test Variant.

        Reads nothing when inspecting the Target would write Stealth core
        projections; the reason is the problem.
        """
        with self._lock:
            blocker = inspection_blocker(worktree)
            if blocker:
                return RunChoices((), f"Tests unavailable: {blocker}")
            try:
                plan = load_checkout_target_test_plan(worktree, selector)
            except _READ_ERRORS as exc:
                return RunChoices((), f"Tests unavailable: {first_line(exc)}")
        runs: list[str] = []
        for test in plan.tests:
            runs.append(test)
            table = plan.variants_of(test)
            if table is not None:
                runs.extend(f"{test}+{item.name}" for item in table.variants)
        if not runs:
            return RunChoices((), "This Target registers no tests")
        return RunChoices(tuple(runs))

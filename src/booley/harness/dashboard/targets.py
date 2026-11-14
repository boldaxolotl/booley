"""Read-only Target and worktree reads behind the Dashboard's Targets window (#2301).

All filesystem and Git access of the window lives here, outside the UI code,
as ``model.py`` does for snapshots. Nothing here writes: the Target detail
uses the pre-setup, in-process inspection (no ``fusesoc run --setup``, no
build directory, no subprocess) and skips it whenever it would have to write
Stealth core projections or the private FuseSoC registry.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from booley.flows.sim.workload import CONSTRAINT_FILE_TYPES
from booley.flows.target_test_plan_loading import load_checkout_target_test_plan
from booley.fusesoc import fusesoc_registry
from booley.fusesoc.core_projection import (
    CoreProjectionError,
    projection_enabled,
    projection_issues,
    registry_issues,
)
from booley.runtime.worktrees import WorktreeEntry, list_worktrees
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle
from booley.targets.target_surface import collect_surface

_T = TypeVar("_T")
# What a read of a checkout can fail with: FuseSoC and Target errors, unreadable
# files, and a malformed tests.toml.
_READ_ERRORS = (FuseSocError, OSError, ValueError)
# Flow whose Targets can have Tests.
_SIM_FLOW = "sim"
_NO_TESTS: tuple[str, ...] = ("only Simulation Targets have tests",)
# A Flow that is not Target-aware but drives another Flow's Targets: the Mutation Flow runs one
# Simulation Target, so Targets offer it and its Target picker lists what the Simulation Flow can drive.
TARGET_KIND_OF_FLOW = {"mutation": "sim"}


def first_line(error: BaseException) -> str:
    """The first non-empty line of an error message, or the exception's type name."""
    return next(
        (line.strip() for line in str(error).splitlines() if line.strip()), type(error).__name__
    )


def launchable_flows(flows: tuple[str, ...]) -> tuple[str, ...]:
    """The Flows a Target detail offers: those that drive it, then those that run what they drive.

    ``("sim",)`` becomes ``("sim", "mutation")``; the driving Flows keep their order and lead.
    """
    return (*flows, *(flow for flow, kind in TARGET_KIND_OF_FLOW.items() if kind in flows))


@dataclass(frozen=True)
class TargetListRow:
    """One listed Target: ``core`` is its VLNV, ``flows`` the Flows that could drive it."""

    selector: str
    name: str
    core: str
    flows: tuple[str, ...]


@dataclass(frozen=True)
class TargetListing:
    """The Targets of one worktree; ``problem`` explains a listing that failed as a whole."""

    worktree: Path
    rows: tuple[TargetListRow, ...]
    problem: str = ""


@dataclass(frozen=True)
class TargetDetail:
    """What one Target declares, before any setup.

    ``worktree`` and ``selector`` name the request it answers. ``tests`` and
    ``constraints`` are ``None`` when that part could not be read (shown as
    unavailable, not as none); ``problems`` names each part that could not be
    resolved. ``row`` is ``None`` when the Target itself could not be found.
    """

    worktree: Path
    selector: str
    row: TargetListRow | None = None
    core_file: str = ""
    filesets: tuple[str, ...] = ()
    tests: tuple[str, ...] | None = None
    constraints: tuple[str, ...] | None = None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunChoices:
    """The Test Runs one Target offers as ``--test`` values, or why they cannot be listed.

    ``worktree`` and ``selector`` name the request it answers. Not called ``Test*``:
    pytest would try to collect such a class.
    """

    worktree: Path
    selector: str
    runs: tuple[str, ...]
    problem: str = ""


@dataclass(frozen=True)
class WorktreeList:
    """Git's registered worktrees (the first is the main checkout) or why they are unknown."""

    entries: tuple[WorktreeEntry, ...] = ()
    problem: str = ""


@dataclass(frozen=True)
class WorktreeChoice:
    """One worktree the human can pick, with the label the list shows."""

    path: Path
    label: str
    main: bool


def order_worktrees(
    entries: Sequence[WorktreeEntry], sessions: Sequence[tuple[Path, str]]
) -> tuple[WorktreeChoice, ...]:
    """Worktrees with a connected session first, other linked ones next, the main checkout last.

    ``sessions`` holds each connected session's checkout and its label
    (``S3 Interactive``, or ``S3 · uart-fix Interactive`` for a named session), already
    in handle-number order. Linked worktrees
    with sessions keep the order of their first session; the others keep
    Git's order. The first Git entry is the main checkout and always comes
    last, with its sessions' labels too. Bare and prunable entries are dropped.
    """
    labels: dict[Path, list[str]] = {}
    for path, label in sessions:
        labels.setdefault(path.resolve(), []).append(label)
    main_entry = entries[0] if entries else None
    linked = [entry for entry in entries[1:] if not entry.bare and entry.prunable is None]
    by_path = {entry.path.resolve(): entry for entry in linked}
    with_session = [by_path[path] for path in labels if path in by_path]
    others = [entry for entry in linked if entry not in with_session]

    def choice(entry: WorktreeEntry, *, main: bool) -> WorktreeChoice:
        parts = [entry.path.name, *(["main checkout"] if main else [])]
        if labels.get(entry.path.resolve()):
            parts.append(", ".join(labels[entry.path.resolve()]))
        return WorktreeChoice(entry.path, " · ".join(parts), main)

    ordered = [choice(entry, main=False) for entry in (*with_session, *others)]
    if main_entry is not None and not main_entry.bare and main_entry.prunable is None:
        ordered.append(choice(main_entry, main=True))
    return tuple(ordered)


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
        + ", ".join(issues)
    )


def _row(handle: TargetHandle) -> TargetListRow:
    return TargetListRow(handle.selector, handle.name, handle.vlnv, handle.drivable_by)


class TargetsReader:
    """Reads Targets and worktrees of one Project; every method is safe on a worker thread."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Detail reads share FuseSoC's cached setup, which is not thread-safe, and
        # a cancelled worker's thread keeps running: one detail read at a time.
        self._detail_lock = threading.Lock()

    def worktrees(self) -> WorktreeList:
        """Git's registered worktrees; a Git failure becomes the problem text."""
        try:
            return WorktreeList(list_worktrees(self.root))
        except subprocess.CalledProcessError as exc:
            # Git's own stderr says why; the exception text only shows the exit code.
            reason = first_line(ValueError(exc.stderr or f"git exited {exc.returncode}"))
            return WorktreeList(problem=f"Worktrees unavailable: {reason}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            return WorktreeList(problem=f"Worktrees unavailable: {first_line(exc)}")

    def listing(self, worktree: Path) -> TargetListing:
        """The Targets of *worktree*, from its ``.core`` files (YAML reads only)."""
        try:
            surface = collect_surface(worktree)
        except _READ_ERRORS as exc:
            return TargetListing(worktree, (), first_line(exc))
        return TargetListing(worktree, tuple(_row(handle) for handle in surface.entries()))

    def detail(self, worktree: Path, selector: str) -> TargetDetail:
        """Filesets, Tests with their Test Variants and constraints of one Target.

        Each part that cannot be resolved is named in ``problems`` while the
        others still show.
        """
        with self._detail_lock:
            return self._detail(worktree, selector)

    def test_runs(self, worktree: Path, selector: str) -> RunChoices:
        """The Test Runs of one Target: each Test, then ``test+variant`` per Test Variant.

        Reads nothing when inspecting the Target would write Stealth core
        projections (the same guard as :meth:`detail`); the reason is the problem.
        """
        with self._detail_lock:
            blocker = inspection_blocker(worktree)
            if blocker:
                return RunChoices(worktree, selector, (), f"Tests unavailable: {blocker}")
            try:
                plan = load_checkout_target_test_plan(worktree, selector)
            except _READ_ERRORS as exc:
                return RunChoices(worktree, selector, (), f"Tests unavailable: {first_line(exc)}")
        runs: list[str] = []
        for test in plan.tests:
            runs.append(test)
            table = plan.variants_of(test)
            if table is not None:
                runs.extend(f"{test}+{item.name}" for item in table.variants)
        if not runs:
            return RunChoices(worktree, selector, (), "This Target registers no tests")
        return RunChoices(worktree, selector, tuple(runs))

    def _detail(self, worktree: Path, selector: str) -> TargetDetail:
        try:
            catalog = TargetCatalog.build(worktree)
            handle = catalog.select(selector)
        except _READ_ERRORS as exc:
            return TargetDetail(
                worktree, selector, problems=(f"Target unavailable: {first_line(exc)}",)
            )
        row = _row(handle)
        core_file = _relative(handle.core_file, worktree)
        problems: list[str] = []
        filesets: tuple[str, ...] = ()
        try:
            filesets = _filesets(handle)
        except _READ_ERRORS as exc:
            problems.append(f"Filesets unavailable: {first_line(exc)}")
        blocker = inspection_blocker(worktree)
        constraints, tests = None, None
        if blocker:
            problems.append(f"Inputs unavailable: {blocker}")
            if _SIM_FLOW in row.flows:  # Other Targets have no Tests to be unavailable.
                problems.append(f"Tests unavailable: {blocker}")
            else:
                tests = _NO_TESTS
        else:
            constraints = _guarded(problems, "Inputs", lambda: _constraints(catalog, handle))
            tests = _guarded(problems, "Tests", lambda: _tests(worktree, row))
        return TargetDetail(
            worktree, selector, row, core_file, filesets, tests, constraints, tuple(problems)
        )


def _guarded(problems: list[str], part: str, read: Callable[[], _T]) -> _T | None:
    """Run one part's read; a failure is recorded as a problem line and gives ``None``."""
    try:
        return read()
    except _READ_ERRORS as exc:
        problems.append(f"{part} unavailable: {first_line(exc)}")
        return None


def _relative(path: Path, worktree: Path) -> str:
    """*path* relative to the worktree in POSIX form; as is when it lies elsewhere."""
    try:
        return path.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return str(path)


def _filesets(handle: TargetHandle) -> tuple[str, ...]:
    """Fileset names the Target may select, in declaration order, read from its ``.core``."""
    document = fusesoc_registry.read_core(handle.core_file)
    targets = document.get("targets")
    declaration = targets.get(handle.name) if isinstance(targets, dict) else None
    names = fusesoc_registry.possible_target_fileset_names(
        declaration if isinstance(declaration, dict) else None
    )
    return tuple(dict.fromkeys(names))


def _constraints(catalog: TargetCatalog, handle: TargetHandle) -> tuple[str, ...]:
    """Paths of the selected SDC and XDC inputs."""
    return tuple(
        item.path
        for item in catalog.inspect(handle).inputs
        if item.file_type.lower() in CONSTRAINT_FILE_TYPES
    )


def _tests(worktree: Path, row: TargetListRow) -> tuple[str, ...]:
    """One line per Test, with its Test Variants (the default marked) and a default-skip note."""
    if _SIM_FLOW not in row.flows:
        return _NO_TESTS
    plan = load_checkout_target_test_plan(worktree, row.selector)
    lines: list[str] = []
    for test in plan.tests:
        line = test
        table = plan.variants_of(test)
        if table is not None:
            names = [
                f"{item.name} (default)" if item.name == table.default else item.name
                for item in table.variants
            ]
            line += ": " + ", ".join(names)
        lines.append(line + (" (skipped by default)" if test in plan.skipped else ""))
    return tuple(lines)

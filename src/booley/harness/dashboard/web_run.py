"""The Run form's choices for one worktree as plain JSON values.

Textual-free and HTTP-free: ``web`` routes requests and calls these. They list what the
Textual wizard offers (Flows, Specialists, Targets, Tests, forms) from the same sources.
``LookupError`` names a Flow, Specialist or form kind that does not exist; any other
trouble reading a worktree is a ``problem`` text beside what could still be read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.run_form import FormError, RunForm
from booley.harness.dashboard.specialist_form import (
    COVERAGE_ANALYST,
    REVIEWER,
    CoverageRecordChoice,
    SimRun,
    finished_coverage_records,
)
from booley.harness.dashboard.targets import TargetsReader, enabled_flows, targets_for


def entries(launcher: Launcher, worktree: Path) -> dict[str, Any]:
    """The Flows and built-in Specialists *worktree* enables, as the wizard lists them."""
    blurbs = {flow.name: flow.blurb for flow in launcher.flows()}
    specialists = launcher.specialists(worktree)
    return {
        "flows": [
            {"name": name, "blurb": blurbs[name]} for name in enabled_flows(blurbs, worktree)
        ],
        "specialists": [asdict(choice) for choice in specialists.choices],
        "specialists_problem": specialists.problem,
    }


def targets(
    launcher: Launcher, reader: TargetsReader, worktree: Path, flow: str
) -> dict[str, Any]:
    """The selectors of the Targets the built-in Flow *flow* can drive in *worktree*."""
    if flow not in {choice.name for choice in launcher.flows()}:
        raise LookupError(f"no such built-in Flow: {flow}")
    listing = reader.listing(worktree)
    return {"targets": list(targets_for(listing, flow)), "problem": listing.problem}


def tests(reader: TargetsReader, worktree: Path, selector: str) -> dict[str, Any]:
    """The Test Runs of one Target: each Test, then ``test+variant`` per Test Variant."""
    choices = reader.test_runs(worktree, selector)
    return {"runs": list(choices.runs), "problem": choices.problem}


def form(
    launcher: Launcher,
    worktree: Path,
    kind: str,
    name: str,
    values: Mapping[str, str],
    coverage_records: Callable[[], tuple[SimRun, ...]],
) -> dict[str, Any]:
    """The form of a built-in Flow or Specialist: its fields for the *values* given.

    *coverage_records* reads the finished Simulation Jobs, called only for the coverage analyst.
    """
    known = {
        "flow": {choice.name for choice in launcher.flows()},
        "specialist": {REVIEWER, COVERAGE_ANALYST},
    }
    if name not in known.get(kind, ()):
        raise LookupError(f"no such built-in {kind}: {name}")
    try:
        built = launcher.form(kind, name, worktree)
    except FormError as exc:
        return {"kind": kind, "problem": str(exc)}
    if isinstance(built, RunForm):
        return {
            "kind": kind,
            "fields": [asdict(field) for field in built.fields],
            "extra": {"hint": built.extra_hint(), "options": built.extras},
        }
    chosen = built.changed({**built.initial(), **values})
    records: tuple[CoverageRecordChoice, ...] = ()
    if name == COVERAGE_ANALYST:
        records = finished_coverage_records(worktree, coverage_records())
    return {
        "kind": kind,
        "note": built.note,
        "values": chosen,
        "fields": [asdict(field) for field in built.fields(chosen)],
        "coverage_records": [asdict(record) for record in records],
    }

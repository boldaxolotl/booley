"""The Run wizard's hand-made forms for the ``reviewer`` and ``coverage_analyst`` Specialists.

No Textual here. Flow forms are generated from a parser (``run_form``); a
Specialist's form is written by hand, because a person starts a review with a
category, a focus and a scope, or an analysis with a Coverage Record, rather than with
every parser option. The options a form leaves out are named in the drift guard's
test, so a Specialist that gains an option fails CI until someone places it or hides
it with a reason.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from booley.core.boundary import as_dict
from booley.harness.dashboard.jobs import FINISHED_JOB_STATES, JobView
from booley.harness.dashboard.run_form import Field, text_words
from booley.runtime.timefmt import format_clock, parse_timestamp

# The built-in Specialists the wizard has a form for: every one of them.
REVIEWER: Final = "reviewer"
COVERAGE_ANALYST: Final = "coverage_analyst"

CATEGORIES: Final = ("rtl", "tb")
_SPEC_FOCUS: Final = "spec"

_TYPED = "Typed as one value."


def _text(dest: str, label: str, flag: str, hint: str) -> Field:
    """A text row writing ``flag value``."""
    return Field(dest, "text", label, flag, f"{hint} {_TYPED}", (flag,))


_CATEGORY = Field(
    "category",
    "list",
    "Category",
    "--category",
    "rtl reviews design sources, tb reviews testbench sources.",
    ("--category",),
    choices=CATEGORIES,
    required=True,
)
_SPEC = _text(
    "spec", "Spec path", "--spec", "The specification the review checks, a worktree path."
)
_SCOPE = _text("scope", "Scope", "--scope", "Comma-separated files to review (required).")
_INSTRUCTION = _text("instruction", "Instruction", "--instruction", "Extra instructions.")
_MORE = Field("more", "more", "More", "", "Enter shows or hides max turns and timeout.", ())
_MAX_TURNS = _text("max_turns", "Max turns", "--max-turns", "Most conversation turns.")
_TIMEOUT = _text("timeout", "Timeout", "--timeout", "How long the review may run, e.g. 30m.")
_ANALYSIS_TIMEOUT = _text(
    "timeout", "Timeout", "--timeout", "How long the analysis may run, e.g. 30m."
)
_COVERAGE_RECORD = Field(
    "coverage_record",
    "coverage_record",
    "Coverage Record",
    "--record",
    "The Coverage Record to analyze: pick a finished one or type its coverage.json path.",
    ("--record",),
    required=True,
)
# The picker row that opens the text editor; no Coverage Record path equals it.
TYPE_COVERAGE_RECORD: Final = "Type a coverage.json path…"
NO_COVERAGE_RECORDS: Final = (
    "No finished Coverage Record in this worktree. Run a coverage Simulation on a Target "
    "first (booley flow sim --coverage --target <Target>), or type a coverage.json path."
)


def focuses(category: str) -> tuple[str, ...]:
    """The review focuses of *category*, in the order the wizard lists them."""
    # Lazy: the Reviewer module is heavy and the wizard only needs these two constants.
    from booley.specialists.reviewer import RTL_FOCUSES, TB_FOCUS_CATEGORIES

    if category == "rtl":
        return RTL_FOCUSES
    return tuple(sorted(TB_FOCUS_CATEGORIES)) if category == "tb" else ()


@dataclass(frozen=True)
class ReviewerForm:
    """The reviewer's form: its rows follow the values, and the values become the command's words.

    ``note`` says which model and Agent CLI the run uses (or why that cannot be read).
    """

    note: str = ""

    def initial(self) -> dict[str, str]:
        """The values a fresh form starts with."""
        return {"category": "rtl", "spec": ""}

    def fields(self, values: Mapping[str, str]) -> tuple[Field, ...]:
        """The rows to show for *values*: the spec path only for the spec focus, more only when open."""
        focus = Field(
            "focus",
            "list",
            "Focus",
            "--focus",
            "What to review for; the choices follow the category.",
            ("--focus",),
            choices=focuses(values["category"]),
            required=True,
        )
        rows = [_CATEGORY, focus]
        if values.get("focus") == _SPEC_FOCUS:
            rows.append(_SPEC)
        rows += [_SCOPE, _INSTRUCTION, _MORE]
        if values.get("more"):
            rows += [_MAX_TURNS, _TIMEOUT]
        return tuple(rows)

    def changed(self, values: Mapping[str, str]) -> dict[str, str]:
        """*values* after a change: a focus the category lacks is dropped, a lone focus is chosen."""
        options = focuses(values["category"])
        focus = values.get("focus", "")
        if focus not in options:
            focus = options[0] if len(options) == 1 else ""
        return {**values, "focus": focus}

    def refusal(self, values: Mapping[str, str]) -> str:
        """Why the command cannot be checked yet; empty when it can."""
        focus = values.get("focus", "")
        if not focus:
            return "Choose a focus"
        if not values.get("scope", "").strip():
            return "Scope is empty: name the files to review"
        if focus == _SPEC_FOCUS and not values.get("spec", "").strip():
            return "The spec focus needs a spec path"
        return ""

    def argv(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """The argument words: the spec only for the spec focus, so a stale prefill never leaks."""
        words = [_CATEGORY.flag, values["category"], "--focus", values["focus"]]
        if values["focus"] == _SPEC_FOCUS:
            words += text_words(_SPEC.flag, values.get("spec", ""))
        for field in (_SCOPE, _INSTRUCTION, _MAX_TURNS, _TIMEOUT):
            words += text_words(field.flag, values.get(field.dest, ""))
        return tuple(words)


@dataclass(frozen=True)
class SimRun:
    """One finished Simulation Job: the invocation directory its report sits in and when it ended."""

    invocation: Path
    ended_at: str


@dataclass(frozen=True)
class CoverageRecordChoice:
    """One finished Coverage Record the picker offers: the ``--record`` path and its row text."""

    path: str
    label: str


def sim_runs(jobs: Iterable[JobView]) -> tuple[SimRun, ...]:
    """The finished Simulation Jobs among *jobs* that have a report: their invocation directories."""
    return tuple(
        SimRun(job.report_path.parent, job.record.ended_at or "")
        for job in jobs
        if job.record.endpoint == "sim"
        and job.state in FINISHED_JOB_STATES
        and job.report_path is not None
    )


def _complete_target(directory: Path) -> str | None:
    """The Target name of the finished Coverage Record in *directory*, ``None`` when it is not one.

    ``coverage_analyst`` accepts only a Coverage Record whose ``simulation.json`` says ``complete``; an
    unreadable or malformed file is a Coverage Record that is not listed, never an error.
    """
    try:
        projection = as_dict(
            json.loads((directory / "simulation.json").read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None
    if projection is None or projection.get("complete") is not True:
        return None
    target = projection.get("target")
    return target if isinstance(target, str) and target else None


def _shown_path(worktree: Path, path: Path) -> str:
    """*path* relative to *worktree* when it is inside it, else absolute (a session's runtime root)."""
    try:
        return path.relative_to(worktree).as_posix()
    except ValueError:
        return path.as_posix()


def _sort_key(ended_at: str, target: str) -> tuple[bool, float, str]:
    """Newest first; an end time that does not parse sorts last."""
    try:
        instant: datetime | None = parse_timestamp(ended_at)
    except ValueError:
        instant = None
    return (instant is None, -instant.timestamp() if instant else 0.0, target)


def finished_coverage_records(
    worktree: Path, runs: Iterable[SimRun]
) -> tuple[CoverageRecordChoice, ...]:
    """The finished Coverage Records of *runs*, newest first.

    Reads one ``targets/`` directory per run and no deeper; whatever cannot be read is skipped.
    Runs in a worker thread.
    """
    found: list[tuple[tuple[bool, float, str], CoverageRecordChoice]] = []
    for run in runs:
        try:
            directories = sorted((run.invocation / "targets").iterdir())
        except OSError:
            continue
        for directory in directories:
            target = _complete_target(directory)
            coverage = directory / "coverage.json"
            try:
                listed = target is not None and coverage.is_file()
            except OSError:  # e.g. PermissionError on a link into an unreadable folder
                listed = False
            if target is None or not listed:
                continue
            label = f"{target}  {format_clock(run.ended_at)}  sim #{run.invocation.name}"
            choice = CoverageRecordChoice(_shown_path(worktree, coverage), label)
            found.append((_sort_key(run.ended_at, target), choice))
    return tuple(choice for _, choice in sorted(found, key=lambda entry: entry[0]))


@dataclass(frozen=True)
class CoverageAnalystForm:
    """The coverage analyst's form: a Coverage Record to analyze, an instruction, and the model's limits.

    ``note`` says which model and Agent CLI the run uses (or why that cannot be read). No Coverage Record
    is preselected: the picker opens on the newest one.
    """

    note: str = ""

    def initial(self) -> dict[str, str]:
        """The values a fresh form starts with: none."""
        return {}

    def fields(self, values: Mapping[str, str]) -> tuple[Field, ...]:
        """The rows to show for *values*: max turns and timeout only when More is open."""
        rows = [_COVERAGE_RECORD, _INSTRUCTION, _MORE]
        if values.get("more"):
            rows += [_MAX_TURNS, _ANALYSIS_TIMEOUT]
        return tuple(rows)

    def changed(self, values: Mapping[str, str]) -> dict[str, str]:
        """*values* after a change: nothing depends on another value."""
        return dict(values)

    def refusal(self, values: Mapping[str, str]) -> str:
        """Why the command cannot be checked yet; empty when it can."""
        return "" if values.get("coverage_record", "").strip() else "Choose a Coverage Record"

    def argv(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """The argument words: the Coverage Record, then the optional rows that hold a value."""
        words = text_words(_COVERAGE_RECORD.flag, values.get("coverage_record", ""))
        for field in (_INSTRUCTION, _MAX_TURNS, _ANALYSIS_TIMEOUT):
            words += text_words(field.flag, values.get(field.dest, ""))
        return tuple(words)


# A Specialist's form: the wizard treats everything that is not a Flow's generated form as one.
SpecialistForm = ReviewerForm | CoverageAnalystForm

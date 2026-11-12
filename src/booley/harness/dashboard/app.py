"""Textual navigation over detached Dashboard snapshots, and the Run wizard that starts Flows."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.widgets import DataTable, Static
from textual.worker import Worker, WorkerState

from booley.core.boundary import as_float
from booley.goals.model import OCCUPYING_STATES
from booley.goals.presentation import proposal_presentation
from booley.harness.dashboard import sessions
from booley.harness.dashboard.footer import BackTarget, footer_hints
from booley.harness.dashboard.jobs import TERMINAL_JOB_STATES, JobSnapshot, JobView
from booley.harness.dashboard.launch import Launcher
from booley.harness.dashboard.links import linked_text, open_in_editor
from booley.harness.dashboard.model import DashboardSnapshot, GoalDetail, GoalView
from booley.harness.dashboard.resources import Resources
from booley.harness.dashboard.review import Occupants
from booley.harness.dashboard.targets import (
    TargetDetail,
    TargetListing,
    TargetsReader,
    WorktreeChoice,
    WorktreeList,
    first_line,
    order_worktrees,
)
from booley.harness.dashboard.topbar import (
    FILTER,
    HELP_ITEM,
    OWNER,
    RUN_ITEM,
    WINDOW,
    WORKTREE,
    TopBar,
)
from booley.harness.dashboard.view_probe import ViewState
from booley.harness.dashboard.wizard import LOADING, RunWizard
from booley.mcp.session_registry import Attribution, SessionRow, shares_worktree
from booley.presentation.colors import PALETTE_DODGER_BLUE, PALETTE_MAUVE, PALETTE_ORANGE
from booley.presentation.report_results import (
    ReportPresentation,
    ResultLine,
    configuration,
    literal,
    present_job_report,
    project_facts,
    proposal_summary,
)
from booley.review.goal_package import observation_facts
from booley.runtime.handles import job_prefix
from booley.runtime.job_records import parse_stamp, target_arg
from booley.runtime.pid import ProcessState
from booley.runtime.session_activity import (
    UNKNOWN,
    WAITING_TOOL,
    WAITING_USER,
    WORKING,
    Activity,
)
from booley.runtime.timefmt import format_age, format_clock, format_duration, parse_timestamp

logger = logging.getLogger(__name__)
READ_BUDGET = 1.0
# Seconds between snapshot refreshes, which also carry the view heartbeat.
REFRESH_SECONDS = 2
# Terminal request for the cursor position; the reply shows an emulator renders the view.
CURSOR_POSITION_REQUEST = "\x1b[6n"
# "Finished" lists this many newest Jobs; "All" lists everything the snapshot retains.
FINISHED_JOBS = 50
# An orphaned process is known dead even though its Job record never finalized.
_REPORT_DETAIL_STATES = TERMINAL_JOB_STATES | {"orphaned"}
HELP = (
    "↑↓ move through the list · Enter opens the selected row · Esc goes back one level\n"
    "←→ switch between the Sessions, Jobs and Targets windows; each keeps its place\n"
    "↑ on the first row (an empty list, or the top of a detail view) reaches the top bar:"
    " ←→ choose, Enter apply, ↓ or Esc back\n"
    "The top bar holds the window tabs, Run and Help; in the Jobs window also its filters:\n"
    f"Running, Finished (newest {FINISHED_JOBS}) and All, and the owner"
    " (Enter cycles All sessions, each session, Standalone)\n"
    "The Jobs tab counts unfinished Jobs; the Jobs window always opens on All sessions\n"
    "and keeps the Running / Finished / All choice\n"
    "The Targets window lists the selected worktree's Targets with their core and Flows;\n"
    "Enter shows filesets, tests, Test Variants and constraints. Its top bar Worktree item\n"
    "picks the worktree: those with sessions first, the main checkout last\n"
    "Job details appear beside the list on wide terminals; Enter opens them on narrow terminals\n"
    "Booley call rows are informational; Enter does not open them\n"
    "The footer shows the keys that apply where the cursor is\n"
    "Click a report, artifact or evidence path to open it in the editor\n"
    "Activity is what the agent client last reported: working, waiting for tool call,\n"
    "or waiting for user input. Unknown means no fresh report, never a guess.\n"
    "Run in the top bar starts a built-in Booley Flow as a Standalone Job: pick the worktree,\n"
    "the Flow, then fill in its form; check the equivalent shell command and submit or copy it;\n"
    "the review also says if the Job runs now or queues (a full queue refuses Submit) and warns\n"
    "about sessions, running Jobs and uncommitted changes in the worktree\n"
    "After Run the Jobs window opens on All sessions with the new Job selected; a Finished\n"
    "filter switches to Running, which keeps the selected Job listed after it finishes\n"
    "Approvals remain in chat."
)

_ACCENT = f"color({PALETTE_DODGER_BLUE})"
_CHROME = f"color({PALETTE_MAUVE})"
SEMANTIC_STYLE = {
    "pass": "green",
    "met": "green",
    "passing": "green",
    "warn": "yellow",
    "fail": "bold red",
    "failing": "bold red",
    "running": _ACCENT,
    "checking": _ACCENT,
    "needs recheck": f"color({PALETTE_ORANGE})",
    "attention": f"color({PALETTE_ORANGE})",
    "unknown": "dim",
    "not run": "dim",
    "not yet run": "dim",
    "unavailable": "dim",
    "ok": "dim",
}


UNMOUNTED_LIST_WIDTH = 160
NO_SESSIONS = "No agent sessions connected. Sessions appear when an agent client calls Booley."
# Views that replace the list with one full-width detail.
DEDICATED_VIEWS = frozenset({"job", "goal", "help", "proposal", "target"})
# Windows left to right; the arrow keys step along them.
WINDOW_ORDER = ("sessions", "jobs", "targets")
# Terminal columns from which a detail (Jobs) or preview (Sessions) sits beside the list.
WIDE_COLUMNS = 120
# Jobs list columns, left to right. Crit repeats the detail's Criterion line as a mark.
JOB_COLUMNS = ("Job", "Target", "Session", "State", "Result", "Crit", "Elapsed")
_TARGET, _RESULT = JOB_COLUMNS.index("Target"), JOB_COLUMNS.index("Result")
# A Target is cut to this many columns before a Result word is: six name characters,
# the ellipsis and the gap. The Job detail beside the list shows both in full.
TARGET_MIN = 8
_ABSENT = "—"
_ELLIPSIS = "…"


_Read = TypeVar("_Read")
TARGETS_GROUP_PREFIX = "targets-"
NO_PROJECT_ROOT = "Targets unavailable: no Project root"


class Owner(Enum):
    """Owner filters of the Jobs window that are not a session key."""

    STANDALONE = "standalone"


#: Label of the Jobs window's owner filter that lists everything.
ALL_SESSIONS = "All sessions"
_HANDLE_NUMBER = re.compile(r"S(\d+)")


@dataclass(frozen=True)
class Navigation:
    """A reversible view selection; navigation never invokes execution machinery.

    In the Jobs window ``session`` is the owner filter: ``None`` lists every
    owner, a session key lists that session's Jobs and ``Owner.STANDALONE``
    lists Jobs that belong to no session.
    """

    view: str = "overview"
    session: str | Owner | None = None
    job_filter: str = "running"
    selected: str = ""


@dataclass(frozen=True)
class SessionOrientation:
    """The facts session detail and the overview preview both show about one session.

    Empty strings mean "absent"; each view decides how to word that.
    """

    key: str
    #: ``S3``, or ``S3 · uart-fix`` for a named session.
    title: str
    checkout: str
    branch: str
    mode: str
    connection: str
    started: str
    activity: str
    warning: str
    last_call: str
    last_age: str


def _goal_summary(goal: GoalView) -> str:
    """One line: Goals met of total and pending proposals; ``Goals —`` without a status."""
    status = goal.status
    if status is None:
        return f"Goals {_ABSENT}"
    line = f"Goals {status.met}/{len(status.goals)} met"
    if status.pending_proposals > 0:
        count = status.pending_proposals
        line += f" · {count} proposal{'s' if count != 1 else ''} pending"
    return line


def _finished(job: JobView) -> bool:
    """True for Jobs that ended or whose process is known dead; they list under Finished."""
    return job.state in _REPORT_DETAIL_STATES


def _sentence(text: str) -> str:
    """Capitalize only the first letter; tool names keep their case."""
    return text[:1].upper() + text[1:]


def activity_label(activity: Activity, now: float) -> str:
    """Reported session activity and how long it has lasted; unknown is said plainly."""
    if activity.state == UNKNOWN:
        return "activity unknown"
    tool = f" ({activity.tool})" if activity.state == WAITING_TOOL and activity.tool else ""
    try:
        elapsed = now - parse_timestamp(activity.since).timestamp()
    except ValueError:
        elapsed = None
    return f"{activity.state}{tool} · {format_duration(elapsed)}"


def _measure(value: int | float | None) -> str:
    return "—" if value is None else f"{value / 1024**3:.1f} GiB"


def _resource_parts(resources: Resources) -> tuple[tuple[str, str], ...]:
    cpu = "—" if resources.cpu_percent is None else f"{resources.cpu_percent:.1f}%"
    capacity = (
        "capacity unavailable"
        if resources.cpu_capacity is None
        else f"of {resources.cpu_capacity:g} CPUs"
    )
    return (
        ("Sandbox CPU", f"{cpu} {capacity}"),
        ("Memory", f"{_measure(resources.memory)} / {_measure(resources.memory_limit)}"),
        ("Free disk", _measure(resources.disk_free)),
    )


def resource_summary(resources: Resources) -> str:
    """Label Sandbox CPU by its measured capacity; Job CPU uses separate core units."""
    return " · ".join(f"{label} {value}" for label, value in _resource_parts(resources))


def _resource_text(resources: Resources) -> Text:
    text = Text()
    for label, value in _resource_parts(resources):
        if text:
            text.append(" · ")
        text.append(label, style=_CHROME)
        text.append(" " + value)
    return text


# Session activity states, styled so the one that needs the human stands out.
_ACTIVITY_STYLE = {
    WAITING_USER: "bold " + SEMANTIC_STYLE["attention"],
    WORKING: _ACCENT,
    WAITING_TOOL: _ACCENT,
    UNKNOWN: "dim",
}
# Header of the session list's second section: the Booley calls, Jobs and
# proposals of the session (not its hook-reported Session activity).
BOOLEY_ACTIVITY = "Booley activity"


def _back_target(view: str) -> BackTarget:
    """What Down returns to from the top bar: Help, a detail, or the list."""
    if view == "help":
        return "Help"
    return "detail" if view in DEDICATED_VIEWS else "list"


def _orientation_text(orientation: str | Text) -> Text:
    """Bold the title line of a plain orientation; a styled one is shown as built."""
    if isinstance(orientation, Text):
        return orientation
    title, separator, details = orientation.partition("\n")
    text = Text(title, style="bold " + _ACCENT)
    # Keep detail lines outside the title's base style.
    return Text.assemble(text, separator + details)


def _join(*parts: str | tuple[str, str], style: str = "") -> Text:
    """Parts joined by dimmed `` · `` separators; a part may be ``(text, style)`` over ``style``."""
    # Each part carries the style itself: Textual ignores a Text's base style.
    text = Text()
    for index, part in enumerate(parts):
        if index:
            text.append(" · ", style="dim")
        content, own = (part, "") if isinstance(part, str) else part
        text.append(content, style=f"{style} {own}".strip())
    return text


def _markdown_lines(markdown: str) -> Text:
    """Agent-written Markdown, kept verbatim except headings, which become bold labels."""
    text = Text()
    for index, line in enumerate(markdown.split("\n")):
        if index:
            text.append("\n")
        heading = re.fullmatch(r"#{1,6} (.+)", line)
        text.append(*((heading[1], "bold " + _CHROME) if heading else (line,)))
    return text


def _health_text(snapshot: DashboardSnapshot) -> Text:
    text = Text()
    for line in snapshot.health:
        if text:
            text.append("\n")
        text.append(
            line.text.removesuffix(line.due_suffix),
            style=SEMANTIC_STYLE[line.severity] if line.severity is not None else None,
        )
        if line.due_suffix:
            text.append(line.due_suffix, style=SEMANTIC_STYLE["attention"])
    for diagnostic in (*snapshot.diagnostics, *snapshot.jobs.diagnostics):
        if text:
            text.append("\n")
        text.append(diagnostic)
    return text


def _doctor_text(snapshot: DashboardSnapshot) -> Text:
    """Only the Doctor line; review lines and diagnostics go to the notices area."""
    return _health_text(
        replace(snapshot, health=snapshot.health[:1], diagnostics=(), jobs=JobSnapshot(()))
    )


def _notices_text(snapshot: DashboardSnapshot) -> Text:
    """Upgrade-review lines and read diagnostics, below the status line."""
    text = Text()
    for line in snapshot.health[1:]:
        if text:
            text.append("\n")
        text.append(
            line.text, style=SEMANTIC_STYLE[line.severity] if line.severity is not None else None
        )
    for diagnostic in (*snapshot.diagnostics, *snapshot.jobs.diagnostics):
        if text:
            text.append("\n")
        text.append(diagnostic)
    return text


def _activity_row(kind: str, line: str) -> Text:
    """One Booley activity row: its kind muted (a pending proposal flagged), the rest plain."""
    style = SEMANTIC_STYLE["attention"] if kind == "Pending proposal" else _CHROME
    return Text.assemble((kind + ": ", style), line)


def _goal_text(goal: GoalDetail) -> Text:
    text = Text()
    if goal.checking:
        text.append("checking · ", style=SEMANTIC_STYLE["checking"])
    text.append(goal.state + " · ", style=SEMANTIC_STYLE[goal.state])
    text.append(goal.presentation.label + " · " + goal.presentation.detail, style=_ACCENT)
    return text


def job_elapsed_seconds(job: JobView, now: float | None = None) -> float | None:
    """Extract an interval, freezing terminal Jobs even without an end stamp."""
    if job.state in TERMINAL_JOB_STATES and not job.record.ended_at:
        reported = as_float((job.report or {}).get("elapsed_s"))
        return reported if reported is not None and reported >= 0 else None
    try:
        start = parse_timestamp(job.record.run_started_at or job.record.started_at).timestamp()
        end = (
            parse_timestamp(job.record.ended_at).timestamp()
            if job.record.ended_at
            else (time.time() if now is None else now)
        )
        return max(0, end - start)
    except ValueError:
        return None


def _connection_label(row: SessionRow, now: float) -> str:
    if row.process_state == ProcessState.RUNNING:
        return "connected"
    return f"last seen {format_age(row.last_call_at, now=now)} · connection unknown"


def _owner_handle(job: JobView, handles: Mapping[str, str] | None) -> str | None:
    """The owning session's handle, else its readable key; None for a standalone Job."""
    key = job.record.session_key
    if key is None:
        return None
    return sessions.session_key_label(job.owner_handle or (handles or {}).get(key, "S?"))


def _owner_label(
    job: JobView,
    sessions: tuple[SessionRow, ...],
    now: float,
    handles: Mapping[str, str] | None = None,
) -> str:
    label = _owner_handle(job, handles)
    if label is None:
        return "standalone"
    handles = handles or {}
    row = next(
        (
            row
            for row in sessions
            if row.attribution.key == job.record.session_key
            or (label != "S?" and handles.get(row.attribution.key) == label)
        ),
        None,
    )
    state = _connection_label(row, now) if row else "disconnected"
    return f"{label} ({state})"


def _job_label(job: JobView) -> str:
    return job.handle or job_prefix(job.record.endpoint) + "?"


def _job_cells(job: JobView, now: float, handles: Mapping[str, str] | None) -> tuple[Text, ...]:
    """The untruncated cells of one Jobs list row, in `JOB_COLUMNS` order."""
    target = target_arg(job.record.argv)
    owner = _owner_handle(job, handles)
    elapsed = job_elapsed_seconds(job, now)
    if job.state in _REPORT_DETAIL_STATES:
        presentation = _job_presentation(job)
        result = Text(
            presentation.flow_word.value,
            style=SEMANTIC_STYLE.get(presentation.flow_word.semantic, ""),
        )
        criterion = presentation.criterion
        crit = (
            Text(
                "✓" if criterion.semantic == "met" else "✗",
                style=SEMANTIC_STYLE[criterion.semantic],
            )
            if criterion is not None
            else Text()
        )
    else:
        result, crit = Text(_ELLIPSIS, style="dim"), Text()
    return (
        Text(_job_label(job)),
        Text(target) if target else Text(_ABSENT, style="dim"),
        Text(owner) if owner else Text(_ABSENT, style="dim"),
        Text(job.state),
        result,
        crit,
        Text(format_duration(elapsed) if elapsed is not None else "unavailable"),
    )


def _fit_job_widths(available: int, rows: list[tuple[Text, ...]]) -> tuple[int, ...]:
    """Column widths (each but the last includes a one-column gap) for the Jobs list.

    Every column but Target and Result takes its natural width. Target then takes
    what Result leaves; once Target is down to `TARGET_MIN`, Result gives way.
    """
    natural = [
        max([cell_len(label), *(row[index].cell_len for row in rows)])
        for index, label in enumerate(JOB_COLUMNS)
    ]
    gaps = [1] * (len(JOB_COLUMNS) - 1) + [0]
    widths = [size + gap for size, gap in zip(natural, gaps, strict=True)]
    spare = available - sum(widths) + widths[_TARGET] + widths[_RESULT]
    target_max = widths[_TARGET]
    target_min = min(TARGET_MIN, target_max)
    result_need = widths[_RESULT]
    if spare >= result_need + target_min:
        widths[_TARGET] = min(spare - result_need, target_max)
    else:
        widths[_TARGET] = target_min
        widths[_RESULT] = max(cell_len(JOB_COLUMNS[_RESULT]) + 1, spare - target_min)
    return tuple(widths)


def _truncate_job_rows(
    rows: list[tuple[Text, ...]], widths: tuple[int, ...]
) -> list[tuple[Text, ...]]:
    """Cut Target and Result cells that exceed their column, with an ellipsis."""
    fitted = []
    for row in rows:
        cells = list(row)
        for index in (_TARGET, _RESULT):
            limit = max(1, widths[index] - 1)
            if cells[index].cell_len > limit:
                cells[index] = cells[index].copy()
                cells[index].truncate(limit, overflow="ellipsis")
        fitted.append(tuple(cells))
    return fitted


def _job_presentation(job: JobView) -> ReportPresentation:
    return present_job_report(
        job.report or {},
        endpoint=job.record.endpoint,
        exit_code=job.record.exit_code,
        argv=job.record.argv,
    )


def _line_text(rows: tuple[ResultLine, ...] | list[ResultLine]) -> str:
    return "\n".join(
        f"{row.label}: {row.value}".rstrip() if row.label else row.value for row in rows
    )


def _job_rows(job: JobView, *, now: float | None = None) -> list[ResultLine]:
    """Compact result, independent acceptance, execution and published artifacts."""
    result = _job_presentation(job)
    rows = [result.flow_result]
    if result.criterion is not None:
        rows.append(result.criterion)
    elapsed = job_elapsed_seconds(job, now)
    duration = format_duration(elapsed) if elapsed is not None else "unavailable"
    rows.append(
        ResultLine("Execution", f"{job.state} · {result.execution.value} · elapsed {duration}")
    )
    rows.extend(
        (
            ResultLine("Job", f"{_job_label(job)} · {job.record.endpoint}"),
            ResultLine("Run ID", job.record.run_id),
        )
    )
    rows.extend(configuration(job.record.argv))
    rows.extend(_job_metadata(job, now=now))
    rows.extend(result.result_rows)
    if job.report is None:
        rows = [
            replace(row, value="Summary unavailable · no report")
            if row.value == "Summary unavailable"
            else row
            for row in rows
        ]
    report = str(job.report_path) if job.report_path is not None else None
    artifacts = [path for path in dict.fromkeys(job.artifacts) if path != report]
    if artifacts:
        rows.append(ResultLine("Artifacts", ""))
        rows.extend(ResultLine("", path) for path in artifacts[:3])
        if len(artifacts) > 3:
            rows.append(ResultLine("", f"{len(artifacts) - 3} more artifacts in report"))
    if job.log_path:
        rows.extend((ResultLine("Log", ""), ResultLine("", job.log_path)))
    if report:
        rows.extend((ResultLine("Report", ""), ResultLine("", report)))
    return rows


def job_detail(job: JobView, *, now: float | None = None) -> str:
    """Render the shared Job rows as plain text."""
    return _line_text(_job_rows(job, now=now))


def _job_metadata(job: JobView, *, now: float | None = None) -> list[ResultLine]:
    report = job.report or {}
    rows = []
    for label, stamp in (
        ("Start", job.record.run_started_at or job.record.started_at),
        ("End", job.record.ended_at),
    ):
        if stamp:
            rows.append(ResultLine(label, format_clock(stamp, now=now)))
    rows.extend(_read_row(job, now=now))
    tool = job.recorded_tools
    if job.state not in _REPORT_DETAIL_STATES:
        tool = tool or job.eda_tool
    elif not tool and (
        report.get("flow")
        or (job.report is None and job.record.endpoint in {"sim", "lint", "synth", "fpga"})
    ):
        tool = "not recorded by this Flow"
    if tool:
        rows.append(ResultLine("EDA tool", tool))
    if job.stage:
        rows.append(ResultLine("Stage", literal(job.stage)))
    rows.extend(_job_resources(job))
    return rows


def _read_row(job: JobView, *, now: float | None = None) -> list[ResultLine]:
    """When the owner first read a finished Job's result, and how long it waited.

    A Job that has not been read shows "unread" only when it is finished and
    owned; a record without an owner (written before owners existed, or an inline
    endpoint that never tracks reads) and a Job still running show no row: the
    detail never invents a latency.
    """
    record = job.record
    if record.read_at:
        row = format_clock(record.read_at, now=now)
        waited = _seconds_between(record.ended_at, record.read_at)
        if waited is not None:
            row += f" · {format_duration(max(0.0, waited))} after end"
        return [ResultLine("Read", row)]
    if record.owner is not None and job.state in _REPORT_DETAIL_STATES:
        return [ResultLine("Read", "unread")]
    return []


def _seconds_between(start: str | None, end: str) -> float | None:
    """Seconds from stamp *start* to stamp *end*; None when either is missing or unparseable."""
    started, ended = parse_stamp(start), parse_stamp(end)
    return None if started is None or ended is None else ended - started


def _job_resources(job: JobView) -> list[ResultLine]:
    if job.state in _REPORT_DETAIL_STATES:
        if job.report is None:
            return [ResultLine("", "resources not recorded (no report)", "unavailable")]
        resources = job.recorded_resources
        if resources is None:
            return []
        cpu, peak = resources
        return [
            ResultLine("CPU time", f"{cpu:g}s"),
            ResultLine("Peak memory (largest process)", f"{peak:g} MiB"),
        ]
    parts = []
    if job.cpu_percent is not None:
        parts.append(f"CPU {job.cpu_percent:.1f}% of one core")
    for label, value in (("Memory", job.memory), ("Peak memory", job.peak_memory)):
        if value is not None:
            parts.append(f"{label}: {_measure(value)}")
    return [ResultLine("Resources", " · ".join(parts))] if parts else []


def job_paths(job: JobView) -> tuple[str, ...]:
    """Visible deduplicated side artifacts first, then ``job.log``, exact report last."""
    report = str(job.report_path) if job.report_path is not None else None
    artifacts = tuple(path for path in dict.fromkeys(job.artifacts) if path != report)[:3]
    return (*artifacts, *(path for path in (job.log_path, report) if path))


def _job_words(job: JobView, now: float) -> str:
    """Flow, Target and elapsed time of a Job, as the overview shows them."""
    elapsed = job_elapsed_seconds(job, now)
    return f"{job.record.endpoint} {DashboardApp._target(job)} {format_duration(elapsed) if elapsed is not None else 'unavailable'}"


def _job_summary(job: JobView, now: float) -> str:
    """Compact overview form: marker, Job label, Flow, Target and elapsed time."""
    return f"▶ {_job_label(job)} {_job_words(job, now)}"


def _evidence_detail(goal: GoalDetail, *, now: float | None = None) -> str:
    frozen = "".join(f"{line}\n" for line in goal.test_runs)
    if goal.evidence is None:
        return frozen + "Last check: unavailable · Evidence unavailable"
    facts = observation_facts(goal.evidence)
    recorded = goal.evidence.get("recorded_at")
    checked = format_clock(recorded, now=now) if recorded is not None else "unavailable"
    lines = [
        f"Last check: {checked}",
        "Observations:",
        _line_text(project_facts(facts)),
        "Immutable derivation provenance:",
    ]
    for row in goal.provenance[:5]:
        lines.append(_line_text(project_facts(observation_facts(row))))
    if len(goal.provenance) > 5:
        lines.append(f"{len(goal.provenance) - 5} more provenance records in evidence")
    if goal.artifacts:
        lines.extend(("Available evidence paths:", *goal.artifacts))
    return frozen + "\n".join(lines)


class SessionList(DataTable):
    """The list, whose group header rows can never hold the cursor.

    Textual clamps every cursor move (arrows, paging, Home/End, clicks and
    ``move_cursor``) through ``validate_cursor_coordinate``, so skipping the
    headers here also keeps Enter and a second click from selecting one.
    """

    header_rows: frozenset[int] = frozenset()

    def validate_cursor_coordinate(self, value: Coordinate) -> Coordinate:
        """Move on from a header row in the direction of travel, else the other way."""
        clamped = super().validate_cursor_coordinate(value)
        row = clamped.row
        if row not in self.header_rows:
            return clamped
        forward = row >= self.cursor_coordinate.row
        for step in (1, -1) if forward else (-1, 1):
            candidate = row
            while candidate in self.header_rows:
                candidate += step
            if 0 <= candidate < self.row_count:
                return Coordinate(candidate, clamped.column)
        return clamped

    def first_selectable_row(self) -> int:
        """Index of the first row that is not a group header."""
        return next((row for row in range(self.row_count) if row not in self.header_rows), 0)


class DashboardApp(App[None]):
    """One observational Sandbox view, responsive and keyboard accessible."""

    TITLE = "Booley Dashboard"
    BINDINGS: ClassVar = [
        # Hidden: the footer lists only arrows, Enter and Esc.
        Binding("q", "quit", show=False),
        Binding("escape", "back", show=False),
        # Priority: the focused DataTable binds left/right/up itself.
        Binding("left", "step_window(-1)", show=False, priority=True),
        Binding("right", "step_window(1)", show=False, priority=True),
        Binding("up", "focus_top_bar", show=False, priority=True),
    ]
    CSS = """
    #orientation { height: auto; max-height: 8; }
    #status { height: 1; }
    #resources { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #health { width: auto; height: 1; margin-left: 2; }
    #notices { height: auto; max-height: 6; display: none; }
    #body { height: 1fr; }
    #list { width: 1fr; }
    #aside { width: 1fr; display: none; padding: 0 1; }
    #empty { height: auto; }
    #footer { dock: bottom; height: 1; color: $text-muted; }
    """

    def __init__(
        self,
        reader: Callable[[], DashboardSnapshot],
        opener: Callable[[str], str] = open_in_editor,
        view_state: ViewState | None = None,
        targets: TargetsReader | None = None,
        launcher: Launcher | None = None,
    ) -> None:
        super().__init__()
        self.reader = reader
        self.opener = opener
        # Set by `booley dashboard`: the heartbeat that shows a terminal still renders this view.
        self.view_state = view_state
        # Reads for the Targets window; without one the window reads nothing.
        self.targets = targets
        # Starts Flows for the Run wizard; without one (no Project root) Run is unavailable.
        self.launcher = launcher
        # The Job the Run wizard just submitted, until its row appears in the Jobs list.
        self._pending_job = ""
        # The window and history Run was chosen from; Esc on the Jobs root returns there.
        self._run_origin: tuple[str, list[Navigation]] | None = None
        self.snapshot = DashboardSnapshot()
        # Each window keeps its own non-empty history; its first entry is the top level.
        self.windows: dict[str, list[Navigation]] = {
            "sessions": [Navigation()],
            "jobs": [Navigation("jobs", None, "running")],
            "targets": [Navigation("targets")],
        }
        self.window = "sessions"
        # The Targets window's worktree is shared by all its views; until the human
        # picks one in the worktree list, entering the window preselects it.
        self._worktree: Path | None = None
        self._worktree_chosen = False
        # Set by entering the window: the next worktree list that arrives may preselect.
        self._preselect_pending = False
        self._worktree_list: WorktreeList | None = None
        self._listing: TargetListing | None = None
        self._detail: TargetDetail | None = None
        self._known_sessions: dict[str, SessionRow] = {}
        self.row_keys: list[str] = []
        self._read_task: asyncio.Task[DashboardSnapshot] | None = None
        self._rows: list[tuple[object, ...]] = []
        self._list_columns_shown: tuple[tuple[str | Text, int | None], ...] = ()
        # The overview's aligned column header and the list width it was fitted to.
        self._overview_header = Text()
        self._overview_width = 0
        # List width the session view's section rule was drawn for.
        self._fitted_width = 0
        # Table width the Jobs columns were fitted to; layout changes it after the first fit.
        self._jobs_fit_width = -1
        # Scroll offsets by (window, view, session, filter, detail selection).
        self._scrolls: dict[tuple[str, str, str | Owner | None, str, str], float] = {}
        self._detail_shown: tuple[str, str, str | Owner | None, str, str] | None = None

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="status"):
            yield Static(id="resources", markup=False)
            yield Static(id="health", markup=False)
        yield Static(id="notices", markup=False)
        yield Static(id="orientation", markup=False)
        yield Static(id="empty", markup=False)
        with Horizontal(id="body"):
            yield SessionList(id="list", cursor_type="row")
            with VerticalScroll(id="aside"):
                yield Static(id="detail", markup=False)
        yield Static(id="footer", markup=False)

    async def on_mount(self) -> None:
        await self.refresh_snapshot()
        self.set_interval(REFRESH_SECONDS, self.refresh_snapshot)
        self.query_one(DataTable).focus()

    def _request_view_heartbeat(self) -> None:
        """Ask the terminal for its cursor position; only an attached emulator answers."""
        driver = self._driver
        if self.view_state is not None and driver is not None:
            driver.write(CURSOR_POSITION_REQUEST)
            driver.flush()

    def on_cursor_position(self, _event: events.CursorPosition) -> None:
        """The terminal answered the heartbeat: someone still sees this Dashboard."""
        if self.view_state is not None:
            self.view_state.answered()

    async def refresh_snapshot(self) -> None:
        """Retain slow reads until consumed; all failures leave a responsive stale view."""
        self._request_view_heartbeat()
        if self._read_task is None:
            self._read_task = asyncio.create_task(asyncio.to_thread(self.reader))
        try:
            self.snapshot = await asyncio.wait_for(asyncio.shield(self._read_task), READ_BUDGET)
            self._read_task = None
            self._remember_sessions()
            if self._notices() is not None:
                self.render_snapshot()
        except TimeoutError:
            self._show_stale("read still in progress")
        except Exception as exc:
            self._read_task = None
            logger.exception("Dashboard refresh failed")
            self._show_stale(str(exc) or type(exc).__name__)

    def _notices(self) -> Static | None:
        """Return the notices area, or None once the view is gone.

        The refresh interval can fire once more while the app shuts down and
        its widgets are already removed; that tick has nothing to update.
        """
        try:
            return self.query_one("#notices", Static)
        except NoMatches:
            return None

    def _show_stale(self, reason: str) -> None:
        notices = self._notices()
        if notices is not None:
            notices.update(
                Text(
                    f"Dashboard data unavailable/stale: {reason}",
                    style=SEMANTIC_STYLE["attention"],
                )
            )
            notices.display = True

    def _remember_sessions(self) -> None:
        """Cache connected sessions, so one that disconnects can still be described."""
        if len(self._known_sessions) > 4096:
            self._known_sessions.clear()
        self._known_sessions.update({row.attribution.key: row for row in self.snapshot.sessions})

    @property
    def navigation(self) -> Navigation:
        """The view on top of the shown window's history."""
        return self.windows[self.window][-1]

    @navigation.setter
    def navigation(self, value: Navigation) -> None:
        self.windows[self.window][-1] = value

    def _scroll_key(self, nav: Navigation) -> tuple[str, str, str | Owner | None, str, str]:
        """Scroll memory is per window; dedicated details are also told apart by subject."""
        subject = nav.selected if nav.view in DEDICATED_VIEWS else ""
        return (self.window, nav.view, nav.session, nav.job_filter, subject)

    def _remember(self) -> None:
        """Save the cursor and scroll offset of the entry about to be left.

        Callers run this before they change a history, so a freshly built
        entry is never overwritten with the old view's cursor or scroll.
        """
        nav = self.navigation
        scrolled = (
            self.query_one("#aside") if nav.view in DEDICATED_VIEWS else self.query_one(DataTable)
        )
        self._scrolls[self._scroll_key(nav)] = scrolled.scroll_y
        self.navigation = replace(nav, selected=self._selected_key())

    def _show(self) -> None:
        self.row_keys = []
        self._rows = []
        self.render_snapshot()
        if not isinstance(self.focused, TopBar):
            self._focus_content()

    def _push(self, navigation: Navigation) -> None:
        self._remember()
        self.windows[self.window].append(navigation)
        self._show()

    def _navigate(self, window: str, *entries: Navigation) -> None:
        """Show a window with a fresh history."""
        self._remember()
        self.windows[window] = [*entries]
        self.window = window
        self._show()

    def _show_window(self, window: str) -> None:
        """Switch windows; each keeps its own history, selection and scroll."""
        self._forget_run()
        self._remember()
        if window == "jobs" and self.window != "jobs":
            self._reset_jobs_owner()
        entering_targets = window == "targets" and self.window != "targets"
        self.window = window
        if entering_targets:
            self._enter_targets()
        self._show()

    def action_step_window(self, step: int) -> None:
        """Left and right step along the windows; toward an edge does nothing."""
        index = WINDOW_ORDER.index(self.window) + step
        if 0 <= index < len(WINDOW_ORDER):
            self._show_window(WINDOW_ORDER[index])

    def action_focus_top_bar(self) -> None:
        self.query_one(TopBar).focus()

    def _focus_content(self) -> None:
        """Give the keyboard to the list, or to the detail when the list is hidden.

        Not while a modal (the Run wizard) is open: it keeps the keyboard.
        """
        if len(self.screen_stack) > 1:
            return
        table = self.query_one(DataTable)
        (table if table.display else self.query_one("#aside")).focus()

    def _content_at_top(self) -> bool:
        """True when up has nowhere to go inside the focused content."""
        table = self.query_one(DataTable)
        if table.display:
            return self.focused is table and (
                table.row_count == 0 or table.cursor_row <= table.first_selectable_row()
            )
        aside = self.query_one("#aside")
        return self.focused is aside and aside.scroll_y == 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Window keys act only on the base screen and while the top bar is not focused."""
        if action not in {"step_window", "focus_top_bar"}:
            return True
        if len(self.screen_stack) > 1 or isinstance(self.focused, TopBar):
            return False
        return self._content_at_top() if action == "focus_top_bar" else True

    def on_top_bar_selected(self, message: TopBar.Selected) -> None:
        if message.kind == HELP_ITEM:
            self.action_help()
        elif message.kind == WINDOW and message.value is not None:
            self._show_window(message.value)
        elif message.kind == FILTER and message.value is not None:
            self._set_jobs_filter(job_filter=message.value)
        elif message.kind == OWNER:
            self._set_jobs_filter(session=self._next_owner())
        elif message.kind == WORKTREE:
            self._open_worktrees()
        elif message.kind == RUN_ITEM:
            self._open_run_wizard()
        # A scroll view has no list to hand the keyboard back to: it takes it (#2886).
        # The worktree list is for choosing from, so it takes the keyboard too.
        if self.navigation.view in DEDICATED_VIEWS or message.kind == WORKTREE:
            self._focus_content()

    def on_top_bar_left(self, _message: TopBar.Left) -> None:
        self._focus_content()

    def on_top_bar_moved(self, _message: TopBar.Moved) -> None:
        self._update_footer()

    def action_back(self) -> None:
        """Go back one level inside the shown window; its top level stays put."""
        stack = self.windows[self.window]
        if len(stack) > 1:
            stack.pop()
            self._show()
        elif self.window == "jobs" and self._run_origin is not None:
            # Esc after Run returns to where the wizard started, history and all.
            window, history = self._run_origin
            self._forget_run()
            self._navigate(window, *history)

    def action_help(self) -> None:
        """Display the Dashboard's keys and views; reached from the top bar's Help item."""
        if self.navigation.view != "help":
            self._push(Navigation("help", self.navigation.session, self.navigation.job_filter))

    def _jobs_root(self) -> Navigation:
        """The Jobs window's top level, which holds its owner and status filters."""
        return self.windows["jobs"][0]

    def _reset_jobs_owner(self) -> None:
        """Opening the Jobs window starts on All sessions; its status filter is kept."""
        root = self._jobs_root()
        if root.session is not None:
            self.windows["jobs"][0] = replace(root, session=None, selected="")

    def _set_jobs_filter(self, **changes: Any) -> None:
        """Show the Jobs list with a new status or owner filter and the other one kept.

        A filter chosen while a narrow Job detail is open returns to the list.
        """
        self._forget_run()
        self._navigate("jobs", replace(self._jobs_root(), selected="", **changes))

    def _forget_run(self) -> None:
        """A window switch or a filter choice ends what Run set up: the pending Job and the way back."""
        self._pending_job = ""
        self._run_origin = None

    def _open_run_wizard(self) -> None:
        """Open the Run wizard over the current window, remembering where it started."""
        if self.launcher is None or self.targets is None:
            self.notify("Run unavailable: no Project root", severity="warning")
            return
        self._remember()
        origin = (self.window, list(self.windows[self.window]))
        self._load_worktrees()
        choices, message = self._wizard_worktrees()
        self.push_screen(
            RunWizard(
                self.launcher,
                self.targets,
                choices,
                message,
                self._default_worktree,
                self._worktree_occupants,
            ),
            lambda key: self._after_run(origin, key),
        )

    def _wizard_worktrees(self) -> tuple[tuple[WorktreeChoice, ...], str]:
        """The wizard's worktree list and what to say while there is none."""
        listed = self._worktree_list
        return self._worktree_choices(), LOADING if listed is None else listed.problem

    def _after_run(self, origin: tuple[str, list[Navigation]], key: str | None) -> None:
        """Show the submitted Job selected in the Jobs window, which opens on All sessions.

        A Finished filter would hide a Job that is still running, so it becomes Running;
        Running keeps the selected Job listed after it finishes. The Job's row may not be
        in the snapshot yet: the selection waits for it (``_pending_job``).
        """
        if key is None:
            return
        job_filter = self._jobs_root().job_filter
        self._navigate(
            "jobs",
            Navigation("jobs", None, "running" if job_filter == "finished" else job_filter, key),
        )
        self._pending_job = key
        self._run_origin = origin
        self._focus_content()  # the top bar had the keyboard when Run was chosen
        self._update_footer()  # Esc now goes back to where Run started
        self.call_later(self.refresh_snapshot)

    def action_open_path(self, path: str) -> None:
        """Open a published path in the editor; a failure is reported, never raised."""
        if hint := self.opener(path):
            self.notify(hint, severity="warning")

    def on_resize(self, _event: events.Resize) -> None:
        if self.is_mounted and self.query("#list"):
            self.call_after_refresh(self.render_snapshot)

    def _session(self) -> tuple[str, GoalView | None]:
        """The session's orientation header as plain text, and its Goal."""
        header, goal = self._session_header()
        return header.plain, goal

    def _session_row(self) -> tuple[SessionRow | None, bool]:
        """The open detail's session row and whether it is connected.

        The live row wins; a session that dropped out of the snapshot falls
        back to the row remembered when it was last seen. The header and the
        Booley activity list both read this one row.
        """
        live = next(
            (
                row
                for row in self.snapshot.sessions
                if row.attribution.key == self.navigation.session
            ),
            None,
        )
        if live is not None:
            return live, True
        return self._known_sessions.get(self.navigation.session or ""), False

    def _session_header(self) -> tuple[Text, GoalView | None]:
        """The session's orientation header, styled fact by fact, and its Goal."""
        row, connected = self._session_row()
        attention = SEMANTIC_STYLE["attention"]
        if row is None:
            title = self._session_title(self.navigation.session or "")
            return _join(
                (f"{title} ", "bold " + _ACCENT),
                ("disconnected", attention),
                "retained Jobs remain available",
            ), None
        goal = self._goal_for_session(row.attribution)
        info = self._orientation(row, goal, connected=connected)
        state = self.snapshot.activity.get(info.key, Activity()).state
        connection_style = "green" if info.connection == "connected" else attention
        header = _join(
            info.title,
            info.checkout,
            info.branch or "branch unavailable",
            info.mode,
            (info.connection, connection_style),
            f"Started {info.started}",
            style="bold " + _ACCENT,
        )
        header.append("\n")
        activity = (info.activity, _ACTIVITY_STYLE.get(state, ""))
        header.append_text(_join(activity, *([(info.warning, "yellow")] if info.warning else [])))
        header.append_text(Text.assemble("\n", ("Last Booley call: ", _CHROME)))
        header.append_text(_join((info.last_call or "—", _ACCENT), info.last_age))
        header.append_text(
            Text.assemble(
                "\n", ("Session key: ", _CHROME), (sessions.session_key_label(info.key), "dim")
            )
        )
        return header, goal

    def _orientation(
        self, row: SessionRow, goal: GoalView | None, *, connected: bool
    ) -> SessionOrientation:
        """Derive the shared orientation facts once for session detail and the preview."""
        facts = row.attribution
        shared = any(shares_worktree(facts, other.attribution) for other in self.snapshot.sessions)
        warning = (
            "Shared-worktree attribution unavailable"
            if facts.kind == "worktree"
            else ("WARNING: shared worktree" if shared else "")
        )
        last = row.calls[-1] if row.calls else None
        checkout, branch = self._checkout_label(facts, goal)
        now = self.snapshot.observed_at
        return SessionOrientation(
            key=facts.key,
            title=self._session_title(facts.key),
            checkout=checkout,
            branch=branch,
            mode="Goal Mode" if goal else "Interactive",
            connection=_connection_label(row, now) if connected else "disconnected",
            started=format_age(row.started_at, now=now),
            activity=_sentence(self._activity_label(facts.key)),
            warning=warning,
            last_call=last.tool if last else "",
            last_age=format_age(row.last_call_at, now=now),
        )

    @staticmethod
    def _checkout_label(facts: Attribution, goal: GoalView | None) -> tuple[str, str]:
        """Checkout and branch a session's Booley calls target, for display.

        A session that never passed ``work_dir`` runs every call in its MCP
        server's cwd, so it shows that checkout and branch, marked as the default,
        unless its process runs in a routable checkout.
        """
        if not facts.work_dir and facts.default_work_dir:
            return f"{facts.default_work_dir} (default)", facts.branch
        return facts.work_dir, facts.branch or (goal.record.branch if goal and goal.record else "")

    def _activity_label(self, key: str) -> str:
        return activity_label(
            self.snapshot.activity.get(key, Activity()), self.snapshot.observed_at
        )

    def _goal_for_session(self, facts: Attribution) -> GoalView | None:
        candidates = [
            goal
            for goal in self.snapshot.goals
            if goal.record is not None and goal.record.worktree.key == facts.worktree_key
        ]
        current = next(
            (
                goal
                for goal in candidates
                if goal.record is not None and goal.record.state.value in OCCUPYING_STATES
            ),
            None,
        )
        return current or next(
            (
                goal
                for goal in reversed(candidates)
                if goal.record is not None and goal.record.session_key == facts.key
            ),
            None,
        )

    def _jobs(self) -> list[JobView]:
        rows = self._owned_jobs(self.navigation.session)
        if self.navigation.job_filter != "all":
            finished = self.navigation.job_filter == "finished"
            rows = [
                job
                for job in rows
                if _finished(job) == finished
                or (not finished and json.dumps(job.key) == self.navigation.selected)
            ]
        rows = sorted(
            rows, key=lambda job: job.record.ended_at or job.record.started_at, reverse=True
        )
        if self.navigation.job_filter != "finished":
            return rows
        # A newer Job must not push the selected one out from under the cursor.
        return [
            job
            for index, job in enumerate(rows)
            if index < FINISHED_JOBS or json.dumps(job.key) == self.navigation.selected
        ]

    def _owned_jobs(self, owner: str | Owner | None) -> list[JobView]:
        """Every retained Job the owner filter admits, before any status filter."""
        if owner is None:
            return list(self.snapshot.jobs.jobs)
        key = None if owner is Owner.STANDALONE else owner
        return [job for job in self.snapshot.jobs.jobs if self._owned_by(job, key)]

    def _owner_handle(self, key: str) -> str:
        """Handle of a session key, also when only a retained Job still knows it."""
        return self.snapshot.session_handles.get(key) or next(
            (
                job.owner_handle
                for job in self.snapshot.jobs.jobs
                if job.record.session_key == key and job.owner_handle
            ),
            "",
        )

    def _owner_filter_label(self, owner: str | Owner | None) -> str:
        """Name an owner filter for the chip, the orientation and empty-state text."""
        return sessions.session_key_label(self._owner_filter_id(owner))

    def _owner_filter_id(self, owner: str | Owner | None) -> str:
        """Identify an owner filter: a handle, else its durable key (never the readable form)."""
        if owner is None:
            return ALL_SESSIONS
        if owner is Owner.STANDALONE:
            return "Standalone"
        return self._owner_handle(owner) or owner

    def _owner_choices(self) -> list[tuple[str | Owner | None, str]]:
        """Owner filters in cycling order: all sessions, each handle, standalone.

        Connected sessions and the owners of retained Jobs both count, so a
        disconnected session is still reachable. One entry per handle, since
        aliased keys share it; a connected key represents it when there is
        one. A key without a handle is its own entry, after the handles.
        """
        keys = [row.attribution.key for row in self.snapshot.sessions]
        keys += [
            job.record.session_key for job in self.snapshot.jobs.jobs if job.record.session_key
        ]
        by_label: dict[str, str] = {}
        for key in keys:
            by_label.setdefault(self._owner_handle(key) or key, key)

        def order(label: str) -> tuple[int, int, str]:
            number = _HANDLE_NUMBER.fullmatch(label)
            return (0, int(number[1]), label) if number else (1, 0, label)

        choices: list[tuple[str | Owner | None, str]] = [(None, ALL_SESSIONS)]
        choices += [(by_label[label], label) for label in sorted(by_label, key=order)]
        choices.append((Owner.STANDALONE, "Standalone"))
        return choices

    def _next_owner(self) -> str | Owner | None:
        """The owner filter after the current one; an unknown current one wraps to all sessions.

        The current entry is found by its id (a handle or the stored key, see
        ``_owner_filter_id``): the key that represents a handle changes when an
        aliased session connects.
        """
        choices = self._owner_choices()
        labels = [label for _, label in choices]
        current = self._owner_filter_id(self._jobs_root().session)
        if current not in labels:
            return None
        return choices[(labels.index(current) + 1) % len(choices)][0]

    def _jobs_orientation(self) -> str:
        """First line of the Jobs window: status filter and owner filter."""
        job_filter = self.navigation.job_filter
        status = {
            "running": "Running",
            "finished": f"Finished (newest {FINISHED_JOBS})",
            "all": "All",
        }.get(job_filter, job_filter)
        return f"Jobs · {status} · {self._owner_filter_label(self.navigation.session)}"

    def _jobs_empty_text(self) -> str:
        """Say why the Jobs list is empty and which filter shows something."""
        owner = self.navigation.session
        owned = self._owned_jobs(owner)
        who = self._owner_filter_label(owner)
        if not owned:
            if not self.snapshot.jobs.jobs:
                return "No Jobs yet."
            return f"No Jobs for {who}. Choose {ALL_SESSIONS} above."
        finished = sum(_finished(job) for job in owned)
        running = len(owned) - finished
        scope = "" if owner is None else f" for {who}"
        if self.navigation.job_filter == "running":
            subject = "" if owner is None else f"{who} has "
            return f"No Jobs running{scope}. {subject}{finished} finished — choose Finished above."
        # Only Running and Finished can be empty while the owner has Jobs: All lists them.
        hint = f" {running} running — choose Running above." if running else ""
        return f"No finished Jobs{scope} yet.{hint}"

    def _content(self) -> tuple[str | Text, list[str], list[tuple[object, ...]]]:
        nav = self.navigation
        if nav.view == "overview":
            return self._overview()
        if nav.view == "jobs":
            jobs = self._jobs()
            return (
                self._jobs_orientation(),
                [json.dumps(job.key) for job in jobs],
                [
                    _job_cells(job, self.snapshot.observed_at, self.snapshot.session_handles)
                    for job in jobs
                ],
            )
        if nav.view == "session":
            return self._session_content()
        if nav.view in {"targets", "worktrees", "target"}:
            return self._targets_window_content()
        return nav.view.title(), [], []

    def _session_content(self) -> tuple[Text, list[str], list[tuple[object, ...]]]:
        orientation, goal = self._session_header()
        self._fitted_width = 0  # Set again only when a section rule is drawn.
        keys, cells = [], []
        if goal is not None:
            ordered = sorted(
                goal.goals,
                key=lambda item: {
                    "failing": 0,
                    "needs recheck": 1,
                    "not yet run": 2,
                    "checking": 3,
                    "passing": 4,
                }["checking" if item.checking else item.state],
            )
            keys.extend(item.key for item in ordered)
            cells.extend(_goal_text(item) for item in ordered)
        if goal and goal.diagnostic:
            orientation.append("\n" + goal.diagnostic)
        if goal and goal.terminal_summary:
            orientation.append("\n")
            orientation.append_text(_markdown_lines(goal.terminal_summary))
        row, _ = self._session_row()
        activity = self._session_activity(goal, row)
        if goal is not None:
            # Goals head the list; Booley activity is its own section below a
            # spacer and a titled rule, both unselectable header rows.
            keys += [sessions.HEADER_PREFIX + "spacer", sessions.HEADER_PREFIX + "booley-activity"]
            cells += [Text(""), self._section_rule(BOOLEY_ACTIVITY)]
        if not activity:
            keys.append(sessions.HEADER_PREFIX + "no-booley-activity")
            cells.append(Text("No Booley activity yet", style="dim"))
        for key, content in activity:
            keys.append(key)
            cells.append(content)
        return orientation, keys, [(cell,) for cell in cells]

    def _section_rule(self, title: str) -> Text:
        """A titled rule as wide as the list, separating one list section from the next."""
        self._fitted_width = self._list_width()
        rule = f"── {title} "
        return Text(
            rule + "─" * max(0, self._fitted_width - cell_len(rule)), style="bold " + _CHROME
        )

    def _session_activity(
        self, goal: GoalView | None, session: SessionRow | None
    ) -> list[tuple[str, Text]]:
        """The session's pending proposals, unfinished Jobs and recent Booley calls.

        ``session`` is the resolved row (live or remembered) whose calls are listed.
        """
        activities = []
        if goal and goal.status:
            activities.extend(
                (
                    "proposal:" + view.proposal.id,
                    _activity_row(
                        "Pending proposal", proposal_summary(proposal_presentation(view))
                    ),
                )
                for view in goal.status.proposals
                if view.state == "pending" and not view.closed_by_abandonment
            )
        activities.extend(
            ("job:" + json.dumps(job.key), _activity_row("Running Job", self._job_line(job)))
            for job in self.snapshot.jobs.jobs
            if self._owned_by(job, self.navigation.session)
            and job.state not in TERMINAL_JOB_STATES
        )
        if session:
            now = self.snapshot.observed_at
            activities.extend(
                (
                    "call:" + str(index),
                    _activity_row(
                        "Booley call",
                        f"{call.tool} · {call.outcome} · {format_clock(call.ended_at or call.at, now=now)}",
                    ),
                )
                for index, call in enumerate(session.calls[-10:])
            )
        return activities

    def _job_line(self, job: JobView) -> str:
        """One Job as handle, endpoint, Target, state and elapsed time."""
        elapsed = format_duration(job_elapsed_seconds(job, self.snapshot.observed_at))
        return (
            f"{_job_label(job)} · {job.record.endpoint} · {self._target(job)} · {job.state}"
            f" · {elapsed}"
        )

    def _selected_key(self) -> str:
        if self._pending_job and self.navigation.view == "jobs":
            return self._pending_job  # not listed yet, so no cursor row can stand for it
        table = self.query_one(DataTable)
        index = table.cursor_row
        return self.row_keys[index] if index < len(self.row_keys) else self.navigation.selected

    @staticmethod
    def _target(job: JobView) -> str:
        return target_arg(job.record.argv) or "Target —"

    def _session_handle(self, key: str) -> str:
        return self.snapshot.session_handles.get(key, "S?")

    def _session_title(self, key: str) -> str:
        """The handle with its ``--name`` label when the session has one."""
        return self.snapshot.session_titles.get(key) or self._session_handle(key)

    def _owned_by(self, job: JobView, key: str | None) -> bool:
        """Aliases affect presentation membership only; routing keeps durable keys."""
        if key is None:
            return job.record.session_key is None
        owner = job.owner_handle or self.snapshot.session_handles.get(job.record.session_key or "")
        label = self.snapshot.session_handles.get(key)
        return owner == label if owner and label else job.record.session_key == key

    def _occupying_goal(self, facts: Attribution) -> GoalView | None:
        """The Goal occupying a session's worktree; overview rows never describe a finished one."""
        return next(
            (
                goal
                for goal in self.snapshot.goals
                if goal.record
                and goal.record.worktree.key == facts.worktree_key
                and goal.record.state.value in OCCUPYING_STATES
            ),
            None,
        )

    def _session_facts(self, row: SessionRow) -> sessions.SessionFacts:
        """Everything the overview shows about one session."""
        facts = row.attribution
        goal = self._occupying_goal(facts)
        mode = "Interactive"
        if goal:
            mode = f"Goal {goal.status.met}/{len(goal.status.goals)}" if goal.status else "Goal"
        notices = []
        if goal and goal.diagnostic:
            # A diagnostic can span lines; a row must stay one line.
            notices.append(" · ".join(part.strip() for part in goal.diagnostic.splitlines()))
        if facts.kind == "worktree":
            notices.append("shared attribution unavailable")
        elif any(shares_worktree(facts, other.attribution) for other in self.snapshot.sessions):
            notices.append("WARNING shared worktree")
        checkout, branch = self._checkout_label(facts, goal)
        return sessions.SessionFacts(
            key=facts.key,
            handle=self._session_handle(facts.key),
            title=self._session_title(facts.key),
            mode=mode,
            branch=branch,
            work_dir=checkout,
            started=format_age(row.started_at, now=self.snapshot.observed_at),
            activity=self.snapshot.activity.get(facts.key, Activity()),
            proposals=goal.status.pending_proposals if goal and goal.status else 0,
            jobs=tuple(
                _job_summary(job, self.snapshot.observed_at)
                for job in self.snapshot.jobs.jobs
                if self._owned_by(job, facts.key) and job.state == "running"
            ),
            notices=tuple(notices),
        )

    def _needs_you(self) -> int:
        """Sessions that wait for the human or hold a pending Goal-change proposal."""
        groups = (sessions.group_of(self._session_facts(row)) for row in self.snapshot.sessions)
        return sum(group == sessions.NEEDS_YOU for group in groups)

    def _list_width(self) -> int:
        """Cells available to a row: the list less cell padding and its scrollbar."""
        if not self.is_running:  # Not on screen yet (headless callers): fit a roomy terminal.
            return UNMOUNTED_LIST_WIDTH
        table = self.query_one(DataTable)
        return max(20, (table.size.width or self.size.width) - 2 - table.scrollbar_size_vertical)

    def _overview(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        now = self.snapshot.observed_at
        standalone = [
            sessions.StandaloneJob(
                "job:" + json.dumps(job.key), _job_label(job), _job_words(job, now)
            )
            for job in self.snapshot.jobs.jobs
            if job.record.session_key is None and job.state == "running"
        ]
        self._overview_width = self._list_width()
        built = sessions.build(
            [self._session_facts(row) for row in self.snapshot.sessions],
            standalone,
            now,
            self._overview_width,
        )
        self._overview_header = built.header
        return "Sessions", built.keys, [(line,) for line in built.rows]

    def render_snapshot(self) -> None:
        """Refresh content while retaining row identity and scroll position."""
        orientation, keys, rows = self._content()
        self.query_one("#orientation", Static).update(_orientation_text(orientation))
        empty = self.query_one("#empty", Static)
        empty_text = self._empty_text(rows)
        empty.update(empty_text)
        empty.display = bool(empty_text)  # no text, no row (#2886)
        self.query_one("#resources", Static).update(_resource_text(self.snapshot.resources))
        self.query_one("#health", Static).update(_doctor_text(self.snapshot))
        notices = self.query_one("#notices", Static)
        notices_text = _notices_text(self.snapshot)
        notices.update(notices_text)
        notices.display = bool(notices_text)
        self._update_top_bar()
        table = self.query_one(SessionList)
        selected = self._selected_key()
        scroll = (
            table.scroll_y
            if self._rows
            else self._scrolls.get(self._scroll_key(self.navigation), 0)
        )
        rows, job_widths = self._apply_list_columns(table, rows)
        if rows != self._rows:
            table.header_rows = frozenset()
            table.clear()
            for row in rows:
                table.add_row(
                    *(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row)
                )
            self._place_cursor(table, keys, selected, scroll)
            self._rows = rows
        self.row_keys = keys
        if self._pending_job in keys:
            self._pending_job = ""  # the cursor now stands on it
        self._render_detail()
        self._update_footer()
        # The list's width settles only after layout (a window switch, a scrollbar).
        self.call_after_refresh(self._refit_overview)
        if job_widths:
            # Showing or hiding the detail pane resizes the list after this pass.
            self.call_after_refresh(self._refit_job_columns)
        # Refreshes keep the keyboard where the user put it; only a hidden or
        # missing focus owner (a view change hid the list) moves it.
        if self.focused is None or not self.focused.display:
            self._focus_content()

    def _update_top_bar(self) -> None:
        """Tell the top bar the active window, filters, counts and worktree."""
        bar = self.query_one(TopBar)
        bar.active = self.window
        bar.needs_you = self._needs_you()
        jobs_root = self._jobs_root()
        bar.job_filter = jobs_root.job_filter
        bar.owner = self._owner_filter_label(jobs_root.session)
        bar.unfinished = sum(not _finished(job) for job in self.snapshot.jobs.jobs)
        bar.worktree = self._worktree.name if self._worktree else ""

    @staticmethod
    def _place_cursor(table: SessionList, keys: list[str], selected: str, scroll: float) -> None:
        """Mark the group headers, then keep the selected session in view where it moved."""
        table.header_rows = frozenset(
            index for index, key in enumerate(keys) if key.startswith(sessions.HEADER_PREFIX)
        )
        table.scroll_to(y=scroll, animate=False)  # First, so the cursor can scroll back in.
        table.move_cursor(
            row=keys.index(selected) if selected in keys else table.first_selectable_row(),
            animate=False,
        )
        # Textual scrolls the cursor into view only after its own refresh, and the
        # restored offset may win that race; check once the list has settled.
        table.call_after_refresh(DashboardApp._keep_cursor_visible, table)

    @staticmethod
    def _keep_cursor_visible(table: SessionList) -> None:
        """Scroll the minimum that puts the cursor row (and its group header) on screen."""
        visible = table.scrollable_content_region.height - table.header_height
        if visible <= 0:  # A hidden list has no viewport to place the cursor in.
            return
        row = table.cursor_row
        top = int(table.scroll_y)
        if row < top or row >= top + visible:
            first = row - 1 if row - 1 in table.header_rows else row
            table.scroll_to(y=first if row < top else row - visible + 1, animate=False)

    def _refit_overview(self) -> None:
        """Fit the overview, or a session's section rule, again when layout changed the width."""
        if not self.is_running:
            return
        view = self.navigation.view
        if (view == "overview" and self._overview_width != self._list_width()) or (
            view == "session" and self._fitted_width and self._fitted_width != self._list_width()
        ):
            self.render_snapshot()

    def _apply_list_columns(
        self, table: DataTable, rows: list[tuple[object, ...]]
    ) -> tuple[list[tuple[object, ...]], tuple[int, ...] | None]:
        """Fit the Jobs columns to the list, rebuild the table columns, and return fitted rows."""
        job_widths: tuple[int, ...] | None = None
        if self.navigation.view == "jobs":
            self._jobs_fit_width = table.size.width
            job_widths = _fit_job_widths(
                self._jobs_fit_width - table.styles.scrollbar_size_vertical, rows
            )
            rows = _truncate_job_rows(rows, job_widths)
        columns = self._list_columns(job_widths)
        if columns != self._list_columns_shown:
            table.clear(columns=True)
            table.cell_padding = 0 if job_widths else 1
            for label, width in columns:
                table.add_column(label, width=width)
            self._list_columns_shown = columns
            self._rows = []
        return rows, job_widths

    def _list_columns(
        self, job_widths: tuple[int, ...] | None
    ) -> tuple[tuple[str | Text, int | None], ...]:
        """Header and width of each list column; dedicated details hide the list and keep them."""
        if job_widths:
            return tuple(zip(JOB_COLUMNS, job_widths, strict=True))
        return {
            "overview": ((self._overview_header, None),),
            "session": (("Goals" if self._session()[1] else BOOLEY_ACTIVITY, None),),
            "targets": (("Target", None), ("Core", None), ("Flows", None)),
            "worktrees": (("Worktree", None),),
        }.get(self.navigation.view, self._list_columns_shown)

    def _refit_job_columns(self) -> None:
        """Fit the Jobs columns again once layout has settled on a different list width."""
        if (
            self.navigation.view == "jobs"
            and self.query_one(DataTable).size.width != self._jobs_fit_width
        ):
            self.render_snapshot()

    def _selected_goal(self) -> GoalDetail | None:
        _, goal = self._session()
        if goal is None:
            return None
        return next((item for item in goal.goals if item.key == self.navigation.selected), None)

    def _selected_job(self) -> JobView | None:
        """The Job named by a dedicated detail view, or under the Jobs list cursor."""
        table = self.query_one(DataTable)
        if self.navigation.view == "job":
            key = self.navigation.selected
        elif self.row_keys and table.cursor_row < len(self.row_keys):
            key = self.row_keys[table.cursor_row]
        else:
            key = ""
        return next((job for job in self.snapshot.jobs.jobs if json.dumps(job.key) == key), None)

    def _selected_proposal_detail(self) -> str:
        _, goal = self._session()
        view = (
            next(
                (
                    view
                    for view in goal.status.proposals
                    if view.proposal.id == self.navigation.selected
                ),
                None,
            )
            if goal and goal.status
            else None
        )
        if view is None:
            return "Proposal unavailable"
        return (
            _line_text(project_facts(proposal_presentation(view))) + "\nApproval remains in chat."
        )

    def _detail_content(self) -> tuple[str | Text, tuple[str, ...]]:
        """Detail text for the current view and the published paths it may link."""
        view = self.navigation.view
        if view in {"jobs", "job"}:
            job = self._selected_job()
            return (self._job_detail(job), job_paths(job)) if job else ("Job data unavailable", ())
        if view == "goal":
            goal = self._selected_goal()
            if goal is None:
                return "Goal data unavailable", ()
            return (
                f"{goal.presentation.label}\n{goal.state}\nRequirement: {goal.presentation.requirement}\n{goal.presentation.detail}\n"
                + _evidence_detail(goal, now=self.snapshot.observed_at)
            ), goal.artifacts
        if view == "proposal":
            return self._selected_proposal_detail(), ()
        if view in {"help", "target"}:
            return (HELP if view == "help" else self._target_detail_text()), ()
        return self._preview_content() if view == "overview" else ("", ())

    def _preview_content(self) -> tuple[Text, tuple[str, ...]]:
        """Preview of the overview row under the cursor and the paths it may link."""
        if not self.row_keys:
            return Text(), ()
        # The cursor, not `navigation.selected`: the latter lags until RowHighlighted is handled.
        key = self._selected_key()
        if key.startswith("job:"):
            job = next(
                (job for job in self.snapshot.jobs.jobs if json.dumps(job.key) == key[4:]), None
            )
            return (self._job_detail(job), job_paths(job)) if job else (Text(), ())
        row = next((row for row in self.snapshot.sessions if row.attribution.key == key), None)
        return (self._session_preview(row), ()) if row else (Text(), ())

    def _session_preview(self, row: SessionRow) -> Text:
        """Labelled orientation lines, the Goal summary and the unfinished Jobs of one session."""
        facts = row.attribution
        goal = self._occupying_goal(facts)
        info = self._orientation(row, goal, connected=True)
        lines: list[tuple[str, str]] = [
            ("Activity", info.activity),
            ("Branch", info.branch or _ABSENT),
            ("Work dir", info.checkout or _ABSENT),
            ("Started", info.started),
            ("Connection", info.connection),
            (
                "Last Booley call",
                f"{info.last_call} · {info.last_age}" if info.last_call else _ABSENT,
            ),
        ]
        text = Text(f"{info.title} · {info.mode}", style="bold " + _ACCENT)
        for label, value in lines:
            text.append("\n")
            text.append(label + ":", style=_CHROME)
            text.append(" ")
            text.append(value, style="dim" if value == _ABSENT else "")
        if info.warning:
            text.append("\n" + info.warning)
        if goal:
            text.append("\n" + _goal_summary(goal))
        jobs = [
            job
            for job in self.snapshot.jobs.jobs
            if self._owned_by(job, facts.key) and not _finished(job)
        ]
        text.append("\nUnfinished Jobs:", style=_CHROME)
        if not jobs:
            text.append(" ")
            text.append(_ABSENT, style="dim")
        for job in jobs:
            text.append("\n" + self._job_line(job))
        return text

    def _render_detail(self) -> None:
        nav = self.navigation
        aside = self.query_one("#aside", VerticalScroll)
        detail, paths = self._detail_content()
        dedicated = nav.view in DEDICATED_VIEWS
        previewing = nav.view == "jobs" or (nav.view == "overview" and bool(self.row_keys))
        aside.display = dedicated or (previewing and self.size.width >= WIDE_COLUMNS)
        self.query_one(DataTable).display = not dedicated
        aside.styles.width = "100%" if dedicated else "50%"
        self.query_one("#detail", Static).update(linked_text(detail, paths))
        # Entering a view (or returning to it from the other window) restores its offset;
        # later refreshes of the same view leave the user's scrolling alone.
        shown = self._scroll_key(nav)
        if nav.view == "overview":
            # A new previewed row opens at the top; a refresh of the same row keeps the scroll.
            shown = (*shown[:-1], self._selected_key())
        if shown != self._detail_shown:
            self._detail_shown = shown
            offset = self._scrolls.get(shown, 0) if dedicated else 0
            self.call_after_refresh(aside.scroll_to, y=offset, animate=False)

    def _job_detail(self, job: JobView) -> Text:
        rows = _job_rows(job, now=self.snapshot.observed_at)
        owner = [
            ResultLine(
                "Owner",
                _owner_label(
                    job,
                    self.snapshot.sessions,
                    self.snapshot.observed_at,
                    self.snapshot.session_handles,
                ),
            )
        ]
        if job.record.session_key:
            owner.append(
                ResultLine("Session key", sessions.session_key_label(job.record.session_key))
            )
        offset = next(index for index, row in enumerate(rows) if row.label == "Job")
        rows[offset:offset] = owner
        detail = Text()
        for index, row in enumerate(rows):
            if index:
                detail.append("\n")
            if row.label:
                detail.append(row.label + ":", style=_CHROME)
                if row.value:
                    detail.append(" ")
            detail.append(row.value, style=SEMANTIC_STYLE.get(row.semantic, ""))
        return detail

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        # A highlight queued before shutdown can arrive after the table is gone.
        if not self.query(DataTable):
            return
        self.navigation = replace(self.navigation, selected=self._selected_key())
        self._render_detail()
        self._update_footer()

    def on_descendant_focus(self, _event: events.DescendantFocus) -> None:
        """The footer differs while the top bar holds the keyboard."""
        self._update_footer()

    def _update_footer(self) -> None:
        """Show only the keys that apply where the cursor is."""
        # query, not query_one: focus events can arrive before the footer is mounted.
        for footer in self.query("#footer"):
            assert isinstance(footer, Static)
            footer.update(
                footer_hints(
                    top_bar=isinstance(self.focused, TopBar),
                    enter_opens=self._enter_target() is not None,
                    jobs_list=self.navigation.view == "jobs",
                    targets_list=self.navigation.view == "targets",
                    nested=len(self.windows[self.window]) > 1
                    or (self.window == "jobs" and self._run_origin is not None),
                    scrolling=self.focused is self.query_one("#aside"),
                    back_to=_back_target(self.navigation.view),
                    on_run=self.query_one(TopBar).current_kind() == RUN_ITEM,
                )
            )

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        if not self.query(DataTable):  # Same shutdown race as the highlight handler.
            return
        self._open_selected()

    def _enter_target(self) -> Navigation | None:
        """The view Enter opens on the selected row, or None when Enter does nothing.

        The footer's open hint and Enter itself both read this. It checks the
        row list rather than ``_selected_key()``, which falls back to the stale
        saved selection when the list is empty or the cursor is past its end.
        """
        if self.query_one(DataTable).cursor_row >= len(self.row_keys):
            return None
        if self._pending_job and self.navigation.view == "jobs":
            return None  # a Job that is not listed yet has nothing to open
        key = self._selected_key()
        if not key or key.startswith(sessions.HEADER_PREFIX):
            return None  # A header row, such as an empty-state line, opens nothing.
        nav = self.navigation
        target = None
        if nav.view == "overview":
            target = (
                Navigation("job", None, nav.job_filter, key.removeprefix("job:"))
                if key.startswith("job:")
                else Navigation("session", key)
            )
        elif nav.view == "session":
            target = self._session_row_target(nav, key)
        elif nav.view == "jobs" and self.size.width < WIDE_COLUMNS:
            target = Navigation("job", nav.session, nav.job_filter, key)
        elif nav.view == "targets":
            target = Navigation("target", selected=key)
        elif nav.view == "worktrees":
            target = Navigation("targets")  # Choosing a worktree opens its Targets list.
        return target

    @staticmethod
    def _session_row_target(nav: Navigation, key: str) -> Navigation | None:
        """What Enter opens on a row of a session detail; Booley call rows open nothing."""
        if key.startswith("job:"):
            return Navigation("job", nav.session, nav.job_filter, key.removeprefix("job:"))
        if key.startswith("proposal:"):
            return Navigation("proposal", nav.session, selected=key.removeprefix("proposal:"))
        if key.startswith("call:"):
            return None
        return Navigation("goal", nav.session, selected=key)

    def _open_selected(self) -> None:
        target = self._enter_target()
        if target is None:
            return
        if self.navigation.view == "worktrees":
            self._choose_worktree(Path(self._selected_key()))
            return
        if target.view == "target":
            self._detail = None
            self._load_detail(target.selected)
        self._push(target)

    # --- Targets window (#2301): reads run on worker threads, results apply here ---

    def _empty_text(self, rows: list[tuple[object, ...]]) -> str:
        """Say why the shown list is empty (or the overview has no sessions)."""
        view = self.navigation.view
        if view == "overview":
            return NO_SESSIONS if not self.snapshot.sessions else ""
        if rows:
            return ""
        if view == "jobs":
            return self._jobs_empty_text()
        if view == "targets":
            return self._targets_empty_text()
        if view == "worktrees":
            return self._worktrees_empty_text()
        return ""

    def _targets_empty_text(self) -> str:
        if self.targets is None:
            return NO_PROJECT_ROOT
        worktree, listing = self._worktree, self._listing
        if worktree is None or listing is None:
            return f"Loading Targets in {worktree.name}…" if worktree else "Loading Targets…"
        if listing.problem:
            return f"Targets unavailable in {worktree.name}: {listing.problem}"
        return f"No Targets in {worktree.name} ({worktree})."

    def _worktrees_empty_text(self) -> str:
        if self.targets is None:
            return NO_PROJECT_ROOT
        if self._worktree_list is None:
            return "Loading…"
        return self._worktree_list.problem or "No worktrees."

    def _targets_window_content(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        """Orientation, row keys and cells of the Targets window's views."""
        nav = self.navigation
        if nav.view == "worktrees":
            choices = self._worktree_choices()
            return (
                "Worktrees",
                [str(item.path) for item in choices],
                [(item.label,) for item in choices],
            )
        if nav.view == "target":
            return f"Target · {nav.selected}", [], []
        rows = self._listing.rows if self._listing else ()
        orientation = f"Targets · {self._worktree.name}" if self._worktree else "Targets"
        return (
            orientation,
            [row.selector for row in rows],
            [(row.selector, row.core, ", ".join(row.flows) or _ABSENT) for row in rows],
        )

    def _target_detail_text(self) -> Text:
        """The open Target's declarations; each part that could not be read says so."""
        detail = self._detail
        if detail is None or detail.selector != self.navigation.selected:
            return Text("Loading…")
        text = Text()

        def add(label: str, value: str, style: str = "") -> None:
            text.append(("\n" if text else "") + label + ":", style=_CHROME)
            text.append(" " + value if value else "", style=style)

        def part(lines: tuple[str, ...] | None) -> tuple[str, str]:
            return ("unavailable", "dim") if lines is None else (", ".join(lines) or "none", "")

        if detail.row is not None:
            add("Core", f"{detail.row.core} ({detail.core_file})")
            add("Flows", ", ".join(detail.row.flows) or "none")
            add("Filesets", ", ".join(detail.filesets) or "none")
            add("Constraints", *part(detail.constraints))
            if detail.tests:  # The lines follow below.
                add("Tests", "", "")
            else:
                add("Tests", *part(detail.tests))
            for line in detail.tests or ():
                text.append("\n  " + line)
        for problem in detail.problems:
            text.append(("\n" if text else "") + problem, style=SEMANTIC_STYLE["attention"])
        return text

    def _session_checkouts(self) -> list[tuple[Path, str]]:
        """Each connected session's checkout and ``S3 · uart-fix Interactive`` label, by handle number.

        The label starts with the session title (the bare handle when unnamed); the order
        follows the handle number, never the title.
        """

        def number(row: SessionRow) -> int:
            match = _HANDLE_NUMBER.fullmatch(self._session_handle(row.attribution.key))
            return int(match[1]) if match else 1 << 30

        pairs: list[tuple[Path, str]] = []
        for row in sorted(self.snapshot.sessions, key=number):
            facts = row.attribution
            checkout = facts.work_dir or facts.default_work_dir
            if checkout:
                label = f"{self._session_title(facts.key)} {self._session_facts(row).mode}"
                pairs.append((Path(checkout), label))
        return pairs

    def _worktree_occupants(self, worktree: Path) -> Occupants:
        """The connected sessions and unfinished Jobs in *worktree*, as the last read showed them."""
        if self.snapshot.observed_at == 0:
            return Occupants(problem="the Dashboard has not read them yet")
        here = worktree.resolve()
        sessions = tuple(
            label for path, label in self._session_checkouts() if path.resolve() == here
        )
        running = tuple(
            job.handle or job.record.run_id
            for job in self.snapshot.jobs.jobs
            if not _finished(job)
            and job.record.work_dir
            and Path(job.record.work_dir).resolve() == here
        )
        return Occupants(sessions, running)

    def _worktree_choices(self) -> tuple[WorktreeChoice, ...]:
        """The worktrees to pick from; sessions' first, the main checkout last."""
        entries = self._worktree_list.entries if self._worktree_list else ()
        return order_worktrees(entries, self._session_checkouts())

    def _highlighted_checkout(self) -> Path | None:
        """The checkout of the session the Sessions window has open or under its cursor."""
        nav = self.windows["sessions"][-1]
        key = nav.session if isinstance(nav.session, str) else nav.selected
        row = self._known_sessions.get(key)
        checkout = row.attribution.work_dir or row.attribution.default_work_dir if row else ""
        return Path(checkout) if checkout else None

    def _default_worktree(self, choices: tuple[WorktreeChoice, ...]) -> Path | None:
        """The highlighted session's worktree, else the main checkout."""
        checkout = self._highlighted_checkout()
        return next(
            (
                item.path
                for item in choices
                if checkout and item.path.resolve() == checkout.resolve()
            ),
            None,
        ) or next((item.path for item in choices if item.main), None)

    def _enter_targets(self) -> None:
        """Entering the window refreshes its reads and, until a choice, preselects the worktree."""
        self._preselect_pending = not self._worktree_chosen
        self._load_worktrees()
        if not self._apply_preselection():
            self._load_listing()

    def _apply_preselection(self) -> bool:
        """Preselect the highlighted session's worktree, else the main checkout.

        Returns True when that changed the worktree (and so reset the window).
        """
        if self._worktree_chosen or self.targets is None or self._worktree_list is None:
            return False
        picked = self._default_worktree(self._worktree_choices()) or self.targets.root
        if picked == self._worktree:
            return False
        self._set_worktree(picked)
        return True

    def _set_worktree(self, worktree: Path) -> None:
        """List another worktree's Targets; no detail of the old one stays open."""
        self._worktree = worktree
        self._listing = None
        self._detail = None
        self.windows["targets"] = [Navigation("targets")]
        self._load_listing()

    def _choose_worktree(self, worktree: Path) -> None:
        """The human picked a worktree: keep it from now on and show its Targets list."""
        self._worktree_chosen = True
        self._set_worktree(worktree)
        self._show()

    def _open_worktrees(self) -> None:
        if self.navigation.view != "worktrees":
            self._load_worktrees()
            self._push(Navigation("worktrees", selected=str(self._worktree or "")))

    def _load_worktrees(self) -> None:
        reader = self.targets
        if reader is not None:
            self._run_targets_read(
                "worktrees", reader.worktrees, lambda why: WorktreeList(problem=why)
            )

    def _load_listing(self) -> None:
        reader, worktree = self.targets, self._worktree
        if reader is not None and worktree is not None:
            self._run_targets_read(
                "listing",
                lambda: reader.listing(worktree),
                lambda why: TargetListing(worktree, (), why),
            )

    def _load_detail(self, selector: str) -> None:
        reader, worktree = self.targets, self._worktree
        if reader is not None and worktree is not None:
            self._run_targets_read(
                "detail",
                lambda: reader.detail(worktree, selector),
                lambda why: TargetDetail(worktree, selector, problems=(why,)),
            )

    def _run_targets_read(
        self, name: str, read: Callable[[], _Read], failed: Callable[[str], _Read]
    ) -> None:
        """Run one read on a worker thread; a newer read of the same kind replaces it.

        Whatever else the read raises is logged and shown as that read's problem.
        """

        def guarded() -> _Read:
            try:
                return read()
            except Exception as exc:
                logger.exception("Dashboard Targets %s read failed", name)
                return failed(first_line(exc))

        self.run_worker(
            guarded,
            name=name,
            group=TARGETS_GROUP_PREFIX + name,
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply a finished Targets read, unless it answers a worktree or Target not shown now."""
        if event.state is not WorkerState.SUCCESS or not event.worker.group.startswith(
            TARGETS_GROUP_PREFIX
        ):
            return
        result = event.worker.result
        reset = False
        if isinstance(result, WorktreeList):
            self._worktree_list = result
            if isinstance(self.screen, RunWizard):
                self.screen.set_worktrees(*self._wizard_worktrees())
            # Only the read started by entering the window preselects: a list opened later
            # by the human must not swap the worktree under them.
            reset = (
                self.window == "targets"
                and self._preselect_pending
                and self.navigation.view != "worktrees"
                and self._apply_preselection()
            )
            self._preselect_pending = False
        elif isinstance(result, TargetListing) and result.worktree == self._worktree:
            self._listing = result
        elif isinstance(result, TargetDetail) and result.worktree == self._worktree:
            self._detail = result
        if self.is_running and self.window == "targets":
            self._show() if reset else self.render_snapshot()

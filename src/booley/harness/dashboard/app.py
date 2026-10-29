"""Read-only Textual navigation over detached Dashboard snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Static

from booley.core.boundary import as_float
from booley.goals.model import OCCUPYING_STATES
from booley.goals.presentation import proposal_presentation
from booley.harness.colors import PALETTE_DODGER_BLUE, PALETTE_MAUVE, PALETTE_ORANGE
from booley.harness.dashboard.jobs import TERMINAL_JOB_STATES, JobSnapshot, JobView, target_arg
from booley.harness.dashboard.links import linked_text, open_in_editor
from booley.harness.dashboard.model import DashboardSnapshot, GoalDetail, GoalView
from booley.harness.dashboard.resources import Resources
from booley.harness.dashboard.results import (
    ReportPresentation,
    ResultLine,
    configuration,
    literal,
    present_report,
    project_facts,
    proposal_summary,
)
from booley.harness.dashboard.topbar import FILTER, HELP_ITEM, OWNER, WINDOW, TopBar
from booley.mcp.session_registry import Attribution, SessionRow, shares_worktree
from booley.review.goal_package import observation_facts
from booley.runtime.handles import job_prefix
from booley.runtime.pid import ProcessState
from booley.runtime.session_activity import UNKNOWN, WAITING_TOOL, Activity
from booley.runtime.timefmt import format_age, format_clock, format_duration, parse_timestamp

logger = logging.getLogger(__name__)
READ_BUDGET = 1.0
# "Finished" lists this many newest Jobs; "All" lists everything the snapshot retains.
FINISHED_JOBS = 50
# An orphaned process is known dead even though its Job record never finalized.
_REPORT_DETAIL_STATES = TERMINAL_JOB_STATES | {"orphaned"}
HELP = (
    "1-9 stable session shortcuts · arrows select rows\n"
    "Enter inspects sessions, Goals, Jobs and proposals; Booley call rows are informational\n"
    "Job details appear beside the list on wide terminals; Enter opens them on narrow terminals\n"
    "←→ Sessions / Jobs · ↑ on the first row: top bar (←→ choose, Enter apply, ↓ or Esc back)\n"
    "Jobs window top bar: Running / Finished / All and the owner (Enter cycles all sessions,\n"
    "each session, standalone); the Jobs tab counts unfinished Jobs\n"
    "Esc back · j Running Jobs · r Finished Jobs · a All Jobs\n"
    "s Sessions · u Jobs from all sessions · ? help · q quit\n"
    "The Jobs window always opens on all sessions; the Running / Finished / All choice is kept\n"
    "Click a report, artifact or evidence path to open it in the editor\n"
    "Activity is what the agent client last reported: working, waiting for tool call,\n"
    "or waiting for user input. Unknown means no fresh report, never a guess.\n"
    "Navigation is read-only; approvals remain in chat."
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


# Views that replace the list with one full-width detail.
DEDICATED_VIEWS = frozenset({"job", "goal", "help", "proposal"})
OVERVIEW_LABEL = "Sessions · arrows / Enter · stable 1-9"
# Jobs list columns, left to right. Crit repeats the detail's Criterion line as a mark.
JOB_COLUMNS = ("Job", "Target", "Session", "State", "Result", "Crit", "Elapsed")
_TARGET, _RESULT = JOB_COLUMNS.index("Target"), JOB_COLUMNS.index("Result")
# A Target is cut to this many columns before a Result word is: six name characters,
# the ellipsis and the gap. The Job detail beside the list shows both in full.
TARGET_MIN = 8
_ABSENT = "—"
_ELLIPSIS = "…"


class Owner(Enum):
    """Owner filters of the Jobs window that are not a session key."""

    STANDALONE = "standalone"


#: Label of the Jobs window's owner filter that lists everything.
ALL_SESSIONS = "All sessions"
_HANDLE_NUMBER = re.compile(r"S(\d+)")


@dataclass(frozen=True)
class Navigation:
    """A reversible view selection; shortcuts never invoke execution machinery.

    In the Jobs window ``session`` is the owner filter: ``None`` lists every
    owner, a session key lists that session's Jobs and ``Owner.STANDALONE``
    lists Jobs that belong to no session.
    """

    view: str = "overview"
    session: str | Owner | None = None
    job_filter: str = "running"
    selected: str = ""


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


def _orientation_text(orientation: str) -> Text:
    title, separator, details = orientation.partition("\n")
    text = Text(title, style="bold " + _ACCENT)
    # Keep detail lines outside the title's base style.
    return Text.assemble(text, separator + details)


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
    """The owning session's handle; None for a standalone Job."""
    key = job.record.session_key
    if key is None:
        return None
    return job.owner_handle or (handles or {}).get(key, "S?")


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
    report = dict(job.report or {})
    report["exit_code"] = job.record.exit_code
    report["argv"] = job.record.argv
    endpoint = job.record.endpoint
    report["flow" if endpoint in {"sim", "lint", "synth", "fpga"} else "mcp_tool"] = endpoint
    return present_report(report)


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
    """Visible deduplicated side artifacts first, exact report last."""
    report = str(job.report_path) if job.report_path is not None else None
    artifacts = tuple(path for path in dict.fromkeys(job.artifacts) if path != report)[:3]
    return (*artifacts, report) if report else artifacts


def _job_summary(job: JobView, now: float) -> str:
    """Compact overview form: Flow, Target and elapsed time."""
    elapsed = job_elapsed_seconds(job, now)
    return f"{_job_label(job)} · {job.record.endpoint} {DashboardApp._target(job)} {format_duration(elapsed) if elapsed is not None else 'unavailable'}"


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


class DashboardApp(App[None]):
    """One observational Sandbox view, responsive and keyboard accessible."""

    TITLE = "Booley Dashboard"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
        ("?", "help", "Help"),
        ("j", "jobs('running')", "Running Jobs"),
        ("r", "jobs('finished')", "Finished Jobs"),
        ("a", "all_jobs", "All Jobs"),
        ("s", "all_sessions", "Sessions"),
        ("u", "all_job_owners", "Jobs: all sessions"),
        # Priority: the focused DataTable binds left/right/up itself.
        Binding("left", "switch_window('sessions')", show=False, priority=True),
        Binding("right", "switch_window('jobs')", show=False, priority=True),
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
    #aside { width: 1fr; display: none; padding: 1; }
    #empty { height: auto; }
    """

    def __init__(
        self,
        reader: Callable[[], DashboardSnapshot],
        opener: Callable[[str], str] = open_in_editor,
    ) -> None:
        super().__init__()
        self.reader = reader
        self.opener = opener
        self.snapshot = DashboardSnapshot()
        # Each window keeps its own non-empty history; its first entry is the top level.
        self.windows: dict[str, list[Navigation]] = {
            "sessions": [Navigation()],
            "jobs": [Navigation("jobs", None, "running")],
        }
        self.window = "sessions"
        self.shortcuts: dict[str, int] = {}
        self._known_sessions: dict[str, SessionRow] = {}
        self.row_keys: list[str] = []
        self._read_task: asyncio.Task[DashboardSnapshot] | None = None
        self._rows: list[tuple[object, ...]] = []
        self._list_columns_shown: tuple[tuple[str, int | None], ...] = ()
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
            yield DataTable(id="list", cursor_type="row")
            with VerticalScroll(id="aside"):
                yield Static(id="detail", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_snapshot()
        self.set_interval(2, self.refresh_snapshot)
        self.query_one(DataTable).focus()

    async def refresh_snapshot(self) -> None:
        """Retain slow reads until consumed; all failures leave a responsive stale view."""
        if self._read_task is None:
            self._read_task = asyncio.create_task(asyncio.to_thread(self.reader))
        try:
            self.snapshot = await asyncio.wait_for(asyncio.shield(self._read_task), READ_BUDGET)
            self._read_task = None
            self._assign_shortcuts()
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

    def _assign_shortcuts(self) -> None:
        connected = {row.attribution.key for row in self.snapshot.sessions}
        if len(self._known_sessions) > 4096:
            self._known_sessions.clear()
        self._known_sessions.update({row.attribution.key: row for row in self.snapshot.sessions})
        self.shortcuts = {
            key: number for key, number in self.shortcuts.items() if key in connected
        }
        for key in sorted(connected):
            if key not in self.shortcuts:
                free = next(
                    (number for number in range(1, 10) if number not in self.shortcuts.values()),
                    None,
                )
                if free is not None:
                    self.shortcuts[key] = free

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
        """Show a window with a fresh history; Sessions keeps its overview as the root."""
        self._remember()
        root = self.windows[window][:1] if window == "sessions" else []
        self.windows[window] = [*root, *entries]
        self.window = window
        self._show()

    def _show_window(self, window: str) -> None:
        """Switch windows; each keeps its own history, selection and scroll."""
        self._remember()
        if window == "jobs" and self.window != "jobs":
            self._reset_jobs_owner()
        self.window = window
        self._show()

    def action_switch_window(self, window: str) -> None:
        """Left shows Sessions, right shows Jobs; toward the shown window does nothing."""
        if window != self.window:
            self._show_window(window)

    def action_focus_top_bar(self) -> None:
        self.query_one(TopBar).focus()

    def _focus_content(self) -> None:
        """Give the keyboard to the list, or to the detail when the list is hidden."""
        table = self.query_one(DataTable)
        (table if table.display else self.query_one("#aside")).focus()

    def _content_at_top(self) -> bool:
        """True when up has nowhere to go inside the focused content."""
        table = self.query_one(DataTable)
        if table.display:
            return self.focused is table and (table.row_count == 0 or table.cursor_row == 0)
        aside = self.query_one("#aside")
        return self.focused is aside and aside.scroll_y == 0

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Window keys act only on the base screen and while the top bar is not focused."""
        if action not in {"switch_window", "focus_top_bar"}:
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

    def on_top_bar_left(self, _message: TopBar.Left) -> None:
        self._focus_content()

    def action_back(self) -> None:
        """Go back one level inside the shown window; its top level stays put."""
        stack = self.windows[self.window]
        if len(stack) > 1:
            stack.pop()
            self._show()

    def action_help(self) -> None:
        """Display the available read-only shortcuts."""
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
        Reaching the Jobs window from another one starts on All sessions.
        """
        if self.window != "jobs":
            self._reset_jobs_owner()
        self._navigate("jobs", replace(self._jobs_root(), selected="", **changes))

    def action_jobs(self, job_filter: str) -> None:
        """Show Jobs with this status filter; from another window the owner is All sessions."""
        self._set_jobs_filter(job_filter=job_filter)

    def action_all_jobs(self) -> None:
        """Show retained unfinished and finished Jobs in one list."""
        self._set_jobs_filter(job_filter="all")

    def action_all_sessions(self) -> None:
        """Return to the session overview from any view."""
        self._navigate("sessions")

    def action_all_job_owners(self) -> None:
        """Remove the owner filter in the Jobs window; its status filter stays."""
        self._set_jobs_filter(session=None)

    def action_open_path(self, path: str) -> None:
        """Open a published path in the editor; a failure is reported, never raised."""
        if hint := self.opener(path):
            self.notify(hint, severity="warning")

    def on_key(self, event: events.Key) -> None:
        if event.key in "123456789" and len(event.key) == 1:
            key = next(
                (key for key, number in self.shortcuts.items() if number == int(event.key)), None
            )
            if key is not None:
                self._navigate("sessions", Navigation("session", key))
                event.stop()

    def on_resize(self, _event: events.Resize) -> None:
        if self.is_mounted and self.query("#list"):
            self.call_after_refresh(self.render_snapshot)

    def _session(self) -> tuple[str, GoalView | None]:
        row = next(
            (
                row
                for row in self.snapshot.sessions
                if row.attribution.key == self.navigation.session
            ),
            None,
        )
        connected = row is not None
        row = row or self._known_sessions.get(self.navigation.session or "")
        if row is None:
            return (
                f"{self._session_handle(self.navigation.session or '')} disconnected"
                " · retained Jobs remain available"
            ), None
        facts = row.attribution
        goal = self._goal_for_session(facts)
        shared = any(shares_worktree(facts, other.attribution) for other in self.snapshot.sessions)
        warning = (
            "Shared-worktree attribution unavailable"
            if facts.kind == "worktree"
            else ("WARNING: shared worktree" if shared else "")
        )
        last = row.calls[-1] if row.calls else None
        checkout, branch = self._checkout_label(facts, goal)
        connection = (
            _connection_label(row, self.snapshot.observed_at) if connected else "disconnected"
        )
        return (
            f"{self._session_handle(facts.key)} · {checkout} · {branch or 'branch unavailable'} · "
            f"{'Goal Mode' if goal else 'Interactive'} · {connection} · Started {format_age(row.started_at, now=self.snapshot.observed_at)}\n"
            f"{_sentence(self._activity_label(facts.key))} · {warning}\nLast Booley call: {last.tool if last else '—'} "
            f"· {format_age(row.last_call_at, now=self.snapshot.observed_at)}\nSession key: {facts.key}"
        ), goal

    @staticmethod
    def _checkout_label(facts: Attribution, goal: GoalView | None) -> tuple[str, str]:
        """Checkout and branch a session's Booley calls target, for display.

        A session that never passed ``work_dir`` runs every call in its MCP
        server's cwd, so it shows that checkout and branch, marked as the default.
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

        The current entry is found by label: the key that represents a handle
        changes when an aliased session connects.
        """
        choices = self._owner_choices()
        labels = [label for _, label in choices]
        current = self._owner_filter_label(self._jobs_root().session)
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

    def _content(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
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
        return nav.view.title(), [], []

    def _session_content(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        orientation, goal = self._session()
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
            orientation += "\n" + goal.diagnostic
        if goal and goal.terminal_summary:
            orientation += "\n" + goal.terminal_summary
        for key, content in self._session_activity(goal):
            keys.append(key)
            cells.append(content)
        return orientation, keys, [(cell,) for cell in cells]

    def _session_activity(self, goal: GoalView | None) -> list[tuple[str, str]]:
        activities = []
        if goal and goal.status:
            activities.extend(
                (
                    "proposal:" + view.proposal.id,
                    "Pending proposal: " + proposal_summary(proposal_presentation(view)),
                )
                for view in goal.status.proposals
                if view.state == "pending" and not view.closed_by_abandonment
            )
        activities.extend(
            (
                "job:" + json.dumps(job.key),
                f"Running Job: {_job_label(job)} · {job.record.endpoint} · {self._target(job)} · {job.state}"
                f" · {format_duration(job_elapsed_seconds(job, self.snapshot.observed_at))}",
            )
            for job in self.snapshot.jobs.jobs
            if self._owned_by(job, self.navigation.session)
            and job.state not in TERMINAL_JOB_STATES
        )
        session = next(
            (
                row
                for row in self.snapshot.sessions
                if row.attribution.key == self.navigation.session
            ),
            None,
        )
        if session:
            activities.extend(
                (
                    "call:" + str(index),
                    f"Booley call: {call.tool} · {call.outcome} · {format_clock(call.at, now=self.snapshot.observed_at)}",
                )
                for index, call in enumerate(session.calls[-10:])
            )
        return activities

    def _selected_key(self) -> str:
        table = self.query_one(DataTable)
        index = table.cursor_row
        return self.row_keys[index] if index < len(self.row_keys) else self.navigation.selected

    @staticmethod
    def _target(job: JobView) -> str:
        return target_arg(job.record.argv) or "Target —"

    def _session_handle(self, key: str) -> str:
        return self.snapshot.session_handles.get(key, "S?")

    def _owned_by(self, job: JobView, key: str | None) -> bool:
        """Aliases affect presentation membership only; routing keeps durable keys."""
        if key is None:
            return job.record.session_key is None
        owner = job.owner_handle or self.snapshot.session_handles.get(job.record.session_key or "")
        label = self.snapshot.session_handles.get(key)
        return owner == label if owner and label else job.record.session_key == key

    def _overview_row(self, row: Any) -> str:
        facts = row.attribution
        goal = next(
            (
                goal
                for goal in self.snapshot.goals
                if goal.record
                and goal.record.worktree.key == facts.worktree_key
                and goal.record.state.value in OCCUPYING_STATES
            ),
            None,
        )
        summary = ""
        if goal and goal.status:
            summary = f" · Goals {goal.status.met}/{len(goal.status.goals)} · proposals {goal.status.pending_proposals}"
        if goal and goal.diagnostic:
            summary += " · " + goal.diagnostic
        shared = any(shares_worktree(facts, other.attribution) for other in self.snapshot.sessions)
        warning = (
            " · shared attribution unavailable"
            if facts.kind == "worktree"
            else (" · WARNING shared worktree" if shared else "")
        )
        jobs = [
            job
            for job in self.snapshot.jobs.jobs
            if self._owned_by(job, facts.key) and job.state == "running"
        ]
        running = "; ".join(_job_summary(job, self.snapshot.observed_at) for job in jobs)
        checkout, branch = self._checkout_label(facts, goal)
        return (
            f"{self.shortcuts.get(facts.key, '·')} · {self._session_handle(facts.key)} · {checkout} · {branch or 'branch —'} · "
            f"{'Goal Mode' if goal else 'Interactive'} · Started {format_age(row.started_at, now=self.snapshot.observed_at)} · {self._activity_label(facts.key)}"
            + summary
            + warning
            + (" · " + running if running else "")
        )

    def _overview(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        keys = [row.attribution.key for row in self.snapshot.sessions]
        rows = [(self._overview_row(row),) for row in self.snapshot.sessions]
        standalone = [
            job
            for job in self.snapshot.jobs.jobs
            if job.record.session_key is None and job.state == "running"
        ]
        orientation = "Sessions" + (
            "\nStandalone running Jobs: "
            + "; ".join(_job_summary(job, self.snapshot.observed_at) for job in standalone)
            if standalone
            else ""
        )
        return orientation, keys, rows

    def render_snapshot(self) -> None:
        """Refresh content while retaining row identity and scroll position."""
        orientation, keys, rows = self._content()
        self.query_one("#orientation", Static).update(_orientation_text(orientation))
        self.query_one("#empty", Static).update(
            "No connected sessions"
            if not rows and self.navigation.view == "overview"
            else (self._jobs_empty_text() if not rows and self.navigation.view == "jobs" else "")
        )
        self.query_one("#resources", Static).update(_resource_text(self.snapshot.resources))
        self.query_one("#health", Static).update(_doctor_text(self.snapshot))
        notices = self.query_one("#notices", Static)
        notices_text = _notices_text(self.snapshot)
        notices.update(notices_text)
        notices.display = bool(notices_text)
        bar = self.query_one(TopBar)
        bar.active = self.window
        jobs_root = self._jobs_root()
        bar.job_filter = jobs_root.job_filter
        bar.owner = self._owner_filter_label(jobs_root.session)
        bar.unfinished = sum(not _finished(job) for job in self.snapshot.jobs.jobs)
        table = self.query_one(DataTable)
        selected = self._selected_key()
        scroll = (
            table.scroll_y
            if self._rows
            else self._scrolls.get(self._scroll_key(self.navigation), 0)
        )
        rows, job_widths = self._apply_list_columns(table, rows)
        if rows != self._rows:
            table.clear()
            for row in rows:
                table.add_row(
                    *(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row)
                )
            if selected in keys:
                index = keys.index(selected)
                table.move_cursor(row=index, animate=False)
            table.scroll_to(y=scroll, animate=False)
            self._rows = rows
        self.row_keys = keys
        self._render_detail()
        if job_widths:
            # Showing or hiding the detail pane resizes the list after this pass.
            self.call_after_refresh(self._refit_job_columns)
        # Refreshes keep the keyboard where the user put it; only a hidden or
        # missing focus owner (a view change hid the list) moves it.
        if self.focused is None or not self.focused.display:
            self._focus_content()

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
    ) -> tuple[tuple[str, int | None], ...]:
        """Header and width of each list column; dedicated details hide the list and keep them."""
        if job_widths:
            return tuple(zip(JOB_COLUMNS, job_widths, strict=True))
        return {
            "overview": ((OVERVIEW_LABEL, None),),
            "session": (("Goals and activity", None),),
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
        if view == "help":
            return HELP, ()
        if view == "proposal":
            return self._selected_proposal_detail(), ()
        return "", ()

    def _render_detail(self) -> None:
        nav = self.navigation
        aside = self.query_one("#aside", VerticalScroll)
        detail, paths = self._detail_content()
        dedicated = nav.view in DEDICATED_VIEWS
        aside.display = dedicated or (nav.view == "jobs" and self.size.width >= 120)
        self.query_one(DataTable).display = not dedicated
        aside.styles.width = "100%" if dedicated else "50%"
        self.query_one("#detail", Static).update(linked_text(detail, paths))
        # Entering a view (or returning to it from the other window) restores its offset;
        # later refreshes of the same view leave the user's scrolling alone.
        shown = self._scroll_key(nav)
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
            owner.append(ResultLine("Session key", job.record.session_key))
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
        self.navigation = replace(self.navigation, selected=self._selected_key())
        self._render_detail()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self._open_selected()

    def _open_selected(self) -> None:
        key = self._selected_key()
        if not key:
            return
        nav = self.navigation
        if nav.view == "overview":
            self._push(Navigation("session", key))
        elif nav.view == "session":
            if key.startswith("job:"):
                self._push(
                    Navigation("job", nav.session, nav.job_filter, key.removeprefix("job:"))
                )
            elif key.startswith("proposal:"):
                self._push(
                    Navigation("proposal", nav.session, selected=key.removeprefix("proposal:"))
                )
            elif not key.startswith("call:"):
                self._push(Navigation("goal", nav.session, selected=key))
        elif nav.view == "jobs" and self.size.width < 120:
            self._push(Navigation("job", nav.session, nav.job_filter, key))

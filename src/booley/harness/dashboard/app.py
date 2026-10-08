"""Read-only Textual navigation over detached Dashboard snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from booley.core.boundary import as_float
from booley.goals.model import OCCUPYING_STATES
from booley.harness.dashboard.model import DashboardSnapshot, GoalDetail, GoalView
from booley.harness.dashboard.resources import Resources
from booley.mcp.session_registry import Attribution, SessionRow, shares_worktree
from booley.runtime.job_snapshot import TERMINAL_JOB_STATES, JobView, target_arg
from booley.runtime.pid import ProcessState
from booley.runtime.timefmt import parse_timestamp

logger = logging.getLogger(__name__)
READ_BUDGET = 1.0

_STATUS_STYLE = {
    "passing": "green",
    "failing": "red",
    "needs recheck": "dark_orange",
    "not yet run": "dim",
    "checking": "cyan",
}


@dataclass(frozen=True)
class Navigation:
    """A reversible view selection; shortcuts never invoke execution machinery."""

    view: str = "overview"
    session: str | None = None
    job_filter: str = "running"
    selected: str = ""


def _age(stamp: str, now: float) -> str:
    try:
        seconds = max(0, int(now - parse_timestamp(stamp).timestamp()))
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    except ValueError:
        return "—"


def _measure(value: int | float | None) -> str:
    return "—" if value is None else f"{value / 1024**3:.1f} GiB"


def resource_summary(resources: Resources) -> str:
    """Label Sandbox CPU by its measured capacity; Job CPU uses separate core units."""
    cpu = "—" if resources.cpu_percent is None else f"{resources.cpu_percent:.1f}%"
    capacity = (
        "capacity unavailable"
        if resources.cpu_capacity is None
        else f"of {resources.cpu_capacity:g} CPUs"
    )
    return (
        f"Sandbox CPU {cpu} {capacity} · Memory {_measure(resources.memory)} / "
        f"{_measure(resources.memory_limit)} · Free disk {_measure(resources.disk_free)}"
    )


def _goal_text(goal: GoalDetail) -> Text:
    text = Text()
    if goal.checking:
        text.append("checking · ", style=_STATUS_STYLE["checking"])
    text.append(goal.state + " · ", style=_STATUS_STYLE[goal.state])
    text.append(goal.presentation.label + " · " + goal.presentation.detail, style="cyan")
    return text


def _elapsed(job: JobView, now: float | None = None) -> str:
    if job.state in TERMINAL_JOB_STATES and not job.record.ended_at:
        reported = as_float((job.report or {}).get("elapsed_s"))
        return f"{reported:g}s" if reported is not None and reported >= 0 else "unavailable"
    try:
        start = parse_timestamp(job.record.run_started_at or job.record.started_at).timestamp()
        end = (
            parse_timestamp(job.record.ended_at).timestamp()
            if job.record.ended_at
            else (time.time() if now is None else now)
        )
        return f"{max(0, end - start):.0f}s"
    except ValueError:
        return "unavailable"


def _connection_label(row: SessionRow, now: float) -> str:
    if row.process_state == ProcessState.RUNNING:
        return "connected"
    return f"last seen {_age(row.last_call_at, now)} ago · connection unknown"


def _owner_label(job: JobView, sessions: tuple[SessionRow, ...], now: float) -> str:
    key = job.record.session_key
    if key is None:
        return "standalone"
    row = next((row for row in sessions if row.attribution.key == key), None)
    state = _connection_label(row, now) if row else "disconnected"
    return f"{key} ({state})"


def _job_row(job: JobView, now: float, owner: str) -> str:
    if job.state == "running":
        cpu = "—" if job.cpu_percent is None else f"{job.cpu_percent:.1f}% of one core"
        outcome = f"CPU {cpu} / memory {_measure(job.memory)}"
    elif job.state in TERMINAL_JOB_STATES:
        outcome = f"exit {job.record.exit_code} · design verdict {(job.report or {}).get('passed', 'unavailable')}"
    else:
        outcome = "resources unavailable"
    return (
        f"{job.record.endpoint} · {DashboardApp._target(job)} · {owner} · "
        f"{job.state} · {_elapsed(job, now)} · {job.stage or 'stage —'} · {outcome}"
    )


def job_detail(job: JobView, *, now: float | None = None) -> str:
    """Execution outcome and design verdict remain distinct; no inline logs."""
    rec, report = job.record, job.report or {}
    paths = ([str(job.report_path)] if job.report_path is not None else []) + list(job.artifacts)
    return "\n".join(
        [
            f"{rec.endpoint} · {rec.run_id}",
            f"Execution: {job.state} · exit {rec.exit_code}",
            f"Design verdict: {report.get('passed', 'unavailable')}",
            f"Flow configuration: {' '.join(rec.argv) or '—'}",
            f"EDA tool: {report.get('eda_tool') or report.get('tool') or job.eda_tool or '—'}",
            f"Start: {rec.run_started_at or rec.started_at} · End: {rec.ended_at or '—'}",
            f"Elapsed: {_elapsed(job, now)}",
            f"Stage: {job.stage or '—'}",
            f"CPU: {'—' if job.cpu_percent is None else str(round(job.cpu_percent, 1)) + '% of one core'} · Memory: {_measure(job.memory)} · Peak memory: {_measure(job.peak_memory)}",
            f"Result: {report.get('summary', report.get('detail', 'unavailable'))}",
            "Metrics / failure details: " + _reported_metrics(report),
            "Artifacts: " + ("\n".join(paths) or "—"),
        ]
    )


def _reported_metrics(report: dict[str, Any]) -> str:
    structured = {"metrics", "counts", "failure_reason", "issues", "conditions"}
    metadata = {"run_id", "invocation_id", "exit_code", "elapsed_s", "schema", "schema_version"}
    fields = {
        key: value
        for key, value in report.items()
        if key in structured
        or (key not in metadata and type(value) in {int, float} and as_float(value) is not None)
    }
    return json.dumps(fields, indent=2)[:8000]


def _evidence_detail(goal: GoalDetail) -> str:
    if goal.evidence is None:
        return "Last check: unavailable · Evidence unavailable"
    facts = goal.evidence
    return (
        f"Last check: {facts.get('recorded_at', 'unavailable')}\n"
        + "Observations: "
        + json.dumps(facts.get("detail", {}), indent=2)[:8000]
        + "\nImmutable derivation provenance: "
        + json.dumps(goal.provenance, indent=2)[:8000]
        + "\nAvailable evidence paths: "
        + ("\n".join(goal.artifacts) or "—")
    )


class DashboardApp(App[None]):
    """One observational Sandbox view, responsive and keyboard accessible."""

    TITLE = "Booley Dashboard"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("escape", "back", "Back"),
        ("?", "help", "Help"),
        ("j", "jobs('running')", "Running Jobs"),
        ("r", "jobs('recent')", "Recent results"),
        ("a", "all_jobs", "All Jobs"),
        ("s", "all_sessions", "All sessions"),
    ]
    CSS = """
    #orientation { height: auto; max-height: 8; }
    #resources { height: 1; color: $text-muted; }
    #health { height: auto; max-height: 6; }
    #body { height: 1fr; }
    #list { width: 1fr; }
    #aside { width: 1fr; display: none; padding: 1; }
    #empty { height: auto; }
    """

    def __init__(self, reader: Callable[[], DashboardSnapshot]) -> None:
        super().__init__()
        self.reader = reader
        self.snapshot = DashboardSnapshot()
        self.navigation = Navigation()
        self.history: list[Navigation] = []
        self.shortcuts: dict[str, int] = {}
        self._known_sessions: dict[str, SessionRow] = {}
        self.row_keys: list[str] = []
        self.goal_columns = 1
        self._read_task: asyncio.Task[DashboardSnapshot] | None = None
        self._rows: list[tuple[object, ...]] = []
        self._table_columns = 1
        self._scrolls: dict[tuple[str, str | None, str], float] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="resources", markup=False)
        yield Static(id="health", markup=False)
        yield Static(id="orientation", markup=False)
        yield Static(id="empty", markup=False)
        with Horizontal(id="body"):
            yield DataTable(id="list", cursor_type="row")
            with VerticalScroll(id="aside"):
                yield Static(id="detail", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Sessions · arrows / Enter · stable 1-9")
        await self.refresh_snapshot()
        self.set_interval(2, self.refresh_snapshot)
        table.focus()

    async def refresh_snapshot(self) -> None:
        """Retain slow reads until consumed; all failures leave a responsive stale view."""
        if self._read_task is None:
            self._read_task = asyncio.create_task(asyncio.to_thread(self.reader))
        try:
            self.snapshot = await asyncio.wait_for(asyncio.shield(self._read_task), READ_BUDGET)
            self._read_task = None
            self._assign_shortcuts()
            self.render_snapshot()
        except TimeoutError:
            self.query_one("#health", Static).update(
                "Dashboard data unavailable/stale: read still in progress"
            )
        except Exception as exc:
            self._read_task = None
            logger.exception("Dashboard refresh failed")
            self.query_one("#health", Static).update(
                f"Dashboard data unavailable/stale: {str(exc) or type(exc).__name__}"
            )

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

    def _push(self, navigation: Navigation) -> None:
        previous = replace(self.navigation, selected=self._selected_key())
        self._scrolls[(previous.view, previous.session, previous.job_filter)] = self.query_one(
            DataTable
        ).scroll_y
        self.history.append(previous)
        self.navigation = navigation
        self.row_keys = []
        self._rows = []
        self.render_snapshot()

    def action_back(self) -> None:
        """Return to the previous scope and selection."""
        self.navigation = self.history.pop() if self.history else Navigation()
        self.row_keys = []
        self._rows = []
        self.render_snapshot()

    def action_help(self) -> None:
        """Display the available read-only shortcuts."""
        self._push(Navigation("help", self.navigation.session))

    def action_jobs(self, job_filter: str) -> None:
        """Session detail shortcuts carry that session's immutable ownership key."""
        scope = self.navigation.session if self.navigation.view != "overview" else None
        self._push(Navigation("jobs", scope, job_filter))

    def action_all_jobs(self) -> None:
        """Show retained running Jobs and results in one list."""
        self._push(Navigation("jobs", self.navigation.session, "all"))

    def action_all_sessions(self) -> None:
        """Remove the session filter in the Jobs view."""
        self._push(Navigation("jobs", None, self.navigation.job_filter))

    def on_key(self, event: events.Key) -> None:
        if event.key in "123456789" and len(event.key) == 1:
            key = next(
                (key for key, number in self.shortcuts.items() if number == int(event.key)), None
            )
            if key is not None:
                self._push(Navigation("session", key))
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
            return "Session disconnected · retained Jobs remain available", None
        facts = row.attribution
        goal = self._goal_for_session(facts)
        shared = any(shares_worktree(facts, other.attribution) for other in self.snapshot.sessions)
        warning = (
            "Shared-worktree attribution unavailable"
            if facts.kind == "worktree"
            else ("WARNING: shared worktree" if shared else "")
        )
        last = row.calls[-1] if row.calls else None
        branch = facts.branch or (goal.record.branch if goal and goal.record else "")
        connection = (
            _connection_label(row, self.snapshot.observed_at) if connected else "disconnected"
        )
        return (
            f"{facts.work_dir} · {branch or 'branch unavailable'} · "
            f"{'Goal Mode' if goal else 'Interactive'} · {connection} · uptime {_age(row.started_at, self.snapshot.observed_at)}\n"
            f"Activity unknown · {warning}\nLast Booley call: {last.tool if last else '—'} "
            f"· {_age(row.last_call_at, self.snapshot.observed_at)} ago"
        ), goal

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
        rows = [
            job
            for job in self.snapshot.jobs.jobs
            if self.navigation.session is None or job.record.session_key == self.navigation.session
        ]
        if self.navigation.job_filter != "all":
            recent = self.navigation.job_filter == "recent"
            rows = [
                job
                for job in rows
                if (job.state in TERMINAL_JOB_STATES) == recent
                or (not recent and json.dumps(job.key) == self.navigation.selected)
            ]
        return sorted(
            rows, key=lambda job: job.record.ended_at or job.record.started_at, reverse=True
        )

    def _content(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        nav = self.navigation
        if nav.view == "overview":
            return self._overview()
        if nav.view == "jobs":
            jobs = self._jobs()
            return (
                f"Jobs · {nav.job_filter} · {nav.session or 'all sessions and standalone'}",
                [json.dumps(job.key) for job in jobs],
                [
                    (
                        _job_row(
                            job,
                            self.snapshot.observed_at,
                            _owner_label(job, self.snapshot.sessions, self.snapshot.observed_at),
                        ),
                    )
                    for job in jobs
                ],
            )
        if nav.view == "session":
            return self._session_content()
        return nav.view.title(), [], []

    def _session_content(self) -> tuple[str, list[str], list[tuple[object, ...]]]:
        orientation, goal = self._session()
        columns = max(1, min(3, self.size.width // 60))
        self.goal_columns = columns
        keys, cells = [], []
        if goal is not None and not goal.diagnostic:
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
        while len(cells) % columns:
            cells.append("")
            keys.append("")
        activities = self._session_activity(goal)
        for key, content in activities:
            keys.extend((key, *("" for _ in range(columns - 1))))
            cells.extend((content, *("" for _ in range(columns - 1))))
        return (
            orientation,
            keys,
            [tuple(cells[index : index + columns]) for index in range(0, len(cells), columns)],
        )

    def _session_activity(self, goal: GoalView | None) -> list[tuple[str, str]]:
        activities = []
        if goal and goal.status:
            activities.extend(
                (
                    "proposal:" + view.proposal.id,
                    "Pending proposal: " + json.dumps(view.proposal.to_json()),
                )
                for view in goal.status.proposals
                if view.state == "pending" and not view.closed_by_abandonment
            )
        activities.extend(
            (
                "job:" + json.dumps(job.key),
                f"Running Job: {job.record.endpoint} · {self._target(job)} · {job.state}",
            )
            for job in self.snapshot.jobs.jobs
            if job.record.session_key == self.navigation.session
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
                ("call:" + str(index), f"Booley call: {call.tool} · {call.outcome} · {call.at}")
                for index, call in enumerate(session.calls[-10:])
            )
        return activities

    def _selected_key(self) -> str:
        table = self.query_one(DataTable)
        index = (
            table.cursor_row * self._table_columns + table.cursor_column
            if self.navigation.view == "session"
            else table.cursor_row
        )
        return self.row_keys[index] if index < len(self.row_keys) else self.navigation.selected

    @staticmethod
    def _target(job: JobView) -> str:
        return target_arg(job.record.argv) or "Target —"

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
            if job.record.session_key == facts.key and job.state == "running"
        ]
        running = "; ".join(
            f"{job.record.endpoint} {self._target(job)} {_age(job.record.started_at, self.snapshot.observed_at)}"
            for job in jobs
        )
        branch = facts.branch or (goal.record.branch if goal and goal.record else "")
        return (
            f"{self.shortcuts.get(facts.key, '·')} · {facts.work_dir} · {branch or 'branch —'} · "
            f"{'Goal Mode' if goal else 'Interactive'} · {_age(row.started_at, self.snapshot.observed_at)} · activity unknown"
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
            "\nStandalone running Jobs: " + "; ".join(job.record.endpoint for job in standalone)
            if standalone
            else ""
        )
        return orientation, keys, rows

    def render_snapshot(self) -> None:
        """Refresh content while retaining row identity and scroll position."""
        orientation, keys, rows = self._content()
        self.query_one("#orientation", Static).update(orientation)
        self.query_one("#empty", Static).update(
            "No connected sessions"
            if not rows and self.navigation.view == "overview"
            else (
                "No Jobs match this filter" if not rows and self.navigation.view == "jobs" else ""
            )
        )
        self.query_one("#resources", Static).update(resource_summary(self.snapshot.resources))
        self.query_one("#health", Static).update(
            "\n".join(
                (
                    *self.snapshot.health,
                    *self.snapshot.diagnostics,
                    *self.snapshot.jobs.diagnostics,
                )
            )
        )
        table = self.query_one(DataTable)
        selected = self._selected_key()
        scope = (self.navigation.view, self.navigation.session, self.navigation.job_filter)
        scroll = table.scroll_y if self._rows else self._scrolls.get(scope, 0)
        columns = self.goal_columns if self.navigation.view == "session" else 1
        if columns != self._table_columns:
            table.clear(columns=True)
            table.add_columns(*(f"Goals and activity · {number + 1}" for number in range(columns)))
            self._table_columns = columns
            self._rows = []
        table.cursor_type = "cell" if self.navigation.view == "session" else "row"
        if rows != self._rows:
            table.clear()
            for row in rows:
                table.add_row(
                    *(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row)
                )
            if selected in keys:
                index = keys.index(selected)
                table.move_cursor(row=index // columns, column=index % columns, animate=False)
            table.scroll_to(y=scroll, animate=False)
            self._rows = rows
        self.row_keys = keys
        self._render_detail()

    def _selected_goal_detail(self) -> str:
        nav = self.navigation
        _, goal = self._session()
        selected = (
            next((item for item in goal.goals if item.key == nav.selected), None) if goal else None
        )
        if selected is None:
            return "Goal data unavailable"
        return (
            f"{selected.presentation.label}\n{selected.state}\nRequirement: {selected.presentation.requirement}\n{selected.presentation.detail}\n"
            + _evidence_detail(selected)
        )

    def _render_detail(self) -> None:
        nav = self.navigation
        aside = self.query_one("#aside", VerticalScroll)
        table = self.query_one(DataTable)
        detail = ""
        if nav.view in {"jobs", "job"}:
            key = (
                nav.selected
                if nav.view == "job"
                else (
                    self.row_keys[table.cursor_row]
                    if self.row_keys and table.cursor_row < len(self.row_keys)
                    else ""
                )
            )
            job = next(
                (job for job in self.snapshot.jobs.jobs if json.dumps(job.key) == key), None
            )
            detail = self._job_detail(job) if job else "Job data unavailable"
        elif nav.view == "goal":
            detail = self._selected_goal_detail()
        elif nav.view == "help":
            detail = "1-9 stable session shortcuts · arrows / Enter select any row\nEsc back · j Running · r Recent · a All\ns all sessions / standalone · ? help · q quit\nActivity unknown: no fresh client lifecycle signals\nNavigation is read-only; approvals remain in chat."
        elif nav.view == "proposal":
            _, goal = self._session()
            view = (
                next(
                    (view for view in goal.status.proposals if view.proposal.id == nav.selected),
                    None,
                )
                if goal and goal.status
                else None
            )
            detail = (
                json.dumps(view.proposal.to_json(), indent=2) + "\nApproval remains in chat."
                if view
                else "Proposal unavailable"
            )
        dedicated = nav.view in {"job", "goal", "help", "proposal"}
        aside.display = dedicated or (nav.view == "jobs" and self.size.width >= 120)
        table.display = not dedicated
        aside.styles.width = "100%" if dedicated else "50%"
        self.query_one("#detail", Static).update(detail)

    def _job_detail(self, job: JobView) -> str:
        return (
            "Owner: "
            + _owner_label(job, self.snapshot.sessions, self.snapshot.observed_at)
            + "\n"
            + job_detail(job, now=self.snapshot.observed_at)
        )

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.navigation = replace(self.navigation, selected=self._selected_key())
        self._render_detail()

    def on_data_table_cell_highlighted(self, _event: DataTable.CellHighlighted) -> None:
        self.navigation = replace(self.navigation, selected=self._selected_key())
        self._render_detail()

    def on_data_table_cell_selected(self, _event: DataTable.CellSelected) -> None:
        self._open_selected()

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

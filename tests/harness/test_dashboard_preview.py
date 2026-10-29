"""Sessions window preview pane on wide terminals (#2246).

At 120 columns or wider the overview shows a preview of the highlighted row beside
the list: a session's orientation facts, or a standalone Job's detail. Every test
reads observable state only (widget display, widths, rendered text), so each one
fails in its body on a Dashboard without the preview.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from textual.widgets import DataTable, Static

from booley.criteria.presentation import CriterionPresentation
from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.mcp.session_registry import Attribution, RecentCall, SessionRow
from booley.runtime.job_records import JobRecord
from booley.runtime.pid import ProcessState
from booley.runtime.session_activity import WAITING_USER, WORKING, Activity
from tests.harness.test_dashboard_sessions import NOW, STARTED, stamp, standalone_job, world


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """A slow worker must finish the first read before any key press."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)


def detail_text(app) -> str:
    return str(app.query_one("#detail", Static).render())


def move_to(app, key: str) -> None:
    app.query_one(DataTable).move_cursor(row=app.row_keys.index(key), animate=False)


def job_view(
    run_id: str, handle: str, state: str, endpoint: str = "sim", session_key: str = "codex:00"
) -> JobView:
    """One Job of a session, with its Dashboard handle and a Target."""
    record = JobRecord(
        run_id, endpoint, STARTED, 60, argv=["--target", "fifo"], session_key=session_key
    )
    return JobView(Path("/jobs"), record, state, handle=handle)


def session_row(
    number: int, worktree: str, *, work_dir: str = "", calls=(), **attribution
) -> SessionRow:
    """A connected session whose process is running."""
    return SessionRow(
        Attribution(f"codex:{number:02}", "thread", work_dir, worktree, **attribution),
        STARTED,
        STARTED,
        calls,
        process_state=ProcessState.RUNNING,
    )


ONE_CALL = (RecentCall(STARTED, "sim", "ok"),)
GOAL_NAMES = ("Lint top clean", "Sim fifo passes")


def criterion(name: str) -> CriterionPresentation:
    return CriterionPresentation(name, "Target top", "must pass", "", "Target top")


def goal_view(worktree: str, state: str, *, session_key: str | None = None, **status):
    """A Goal on ``worktree``; ``status`` keywords fill its status, none leaves it unavailable."""
    return model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key=worktree),
            state=SimpleNamespace(value=state),
            branch="goal/fixture",
            session_key=session_key,
        ),
        status=SimpleNamespace(proposals=(), **status) if status else None,
        goals=tuple(model.GoalDetail(name, "passing", criterion(name), {}) for name in GOAL_NAMES),
    )


def snapshot_of(rows, *, jobs=(), goals=(), activity=None) -> model.DashboardSnapshot:
    """A snapshot of ``rows`` named S1.. in order, observed at NOW."""
    return model.DashboardSnapshot(
        tuple(rows),
        jobs=JobSnapshot(tuple(jobs)),
        goals=tuple(goals),
        observed_at=NOW,
        activity=activity or {},
        session_handles={row.attribution.key: f"S{index + 1}" for index, row in enumerate(rows)},
    )


@pytest.mark.asyncio
async def test_preview_shows_at_120_columns_and_hides_below_across_resize():
    activity = {f"codex:{n:02}": Activity(WORKING, stamp(100)) for n in range(3)}
    current = {"snapshot": world(activity, count=3)}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        aside, table = app.query_one("#aside"), app.query_one("#list")
        assert aside.display
        assert detail_text(app).startswith(app._session_handle(app._selected_key()) + " · ")
        assert table.size.width < 120

        # A resize re-renders after a refresh, and the list width settles one pass later.
        await pilot.resize_terminal(119, 30)
        await pilot.pause()
        await pilot.pause()
        assert not aside.display
        assert table.size.width >= 117

        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        await pilot.pause()
        assert aside.display

        # An overview without rows shows its empty state, not an empty preview.
        current["snapshot"] = world({}, count=0)
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys == []
        assert not aside.display


@pytest.mark.asyncio
async def test_interactive_session_preview_lists_fields_and_only_unfinished_jobs():
    rows = [
        # S1 shares its worktree with S2 and has no branch, work dir or Booley call.
        session_row(0, "wt0"),
        session_row(1, "wt0", work_dir="/work/b", branch="feature/b", calls=ONE_CALL),
        # S3's Goal ended: its row beside the preview is Interactive, so the preview is too.
        session_row(2, "wt2", work_dir="/work/c", branch="main"),
    ]
    jobs = [
        job_view("one", "J-SIM1", "running"),
        job_view("two", "J-LINT2", "queued", endpoint="lint"),
        job_view("three", "J-SIM3", "completed"),
        job_view("four", "J-SIM4", "orphaned"),
    ]
    ended = goal_view(
        "wt2", "finished", session_key="codex:02", met=1, goals=(0,) * 3, pending_proposals=0
    )
    activity = {"codex:00": Activity(WAITING_USER, stamp(100))}
    app = DashboardApp(lambda: snapshot_of(rows, jobs=jobs, goals=[ended], activity=activity))
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        move_to(app, "codex:00")
        await pilot.pause()
        first = detail_text(app).splitlines()
        assert first[:8] == [
            "S1 · Interactive",
            "Activity: Waiting for user input · 1m 40s",
            "Branch: —",
            "Work dir: —",
            "Started: 5m ago",
            "Connection: connected",
            "Last Booley call: —",
            "WARNING: shared worktree",
        ]
        assert first[8] == "Unfinished Jobs:"
        assert [line.split(" · ")[0] for line in first[9:]] == ["J-SIM1", "J-LINT2"]
        assert "Goals" not in "\n".join(first)

        move_to(app, "codex:01")
        await pilot.pause()
        second = detail_text(app)
        assert "Branch: feature/b" in second
        assert "Work dir: /work/b" in second
        assert "Last Booley call: sim · 5m ago" in second
        assert "Unfinished Jobs: —" in second

        move_to(app, "codex:02")
        await pilot.pause()
        third = detail_text(app)
        assert third.startswith("S3 · Interactive")
        assert "Goals" not in third


@pytest.mark.asyncio
async def test_goal_mode_session_preview_has_one_line_goal_summary():
    rows = [session_row(0, "wt0", work_dir="/work/a", branch="goal/fixture")]
    goal = goal_view("wt0", "active", met=2, goals=(0,) * 5, pending_proposals=1)
    app = DashboardApp(lambda: snapshot_of(rows, goals=[goal]))
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        text = detail_text(app)
        assert text.startswith("S1 · Goal Mode")
        assert text.splitlines().count("Goals 2/5 met · 1 proposal pending") == 1
        # The summary is one line: the Goal list belongs to session detail.
        assert not any(name in text for name in GOAL_NAMES)


@pytest.mark.asyncio
async def test_standalone_job_row_previews_its_job_detail():
    current = replace(world({}, count=1), jobs=JobSnapshot((standalone_job(),)))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        move_to(app, next(key for key in app.row_keys if key.startswith("job:")))
        await pilot.pause()
        preview = detail_text(app)
        assert "Run ID: three" in preview
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "job"
        assert detail_text(app) == preview


@pytest.mark.asyncio
async def test_preview_follows_cursor_and_refresh_keeps_cursor_and_scroll():
    working = {f"codex:{n:02}": Activity(WORKING, stamp(1000 - n)) for n in range(25)}
    current = {"snapshot": world(working, count=25)}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(130, 14)) as pilot:
        table = app.query_one(DataTable)
        await pilot.pause()
        move_to(app, "codex:24")
        await pilot.pause()
        assert table.scroll_y > 0
        assert detail_text(app).startswith("S25 · ")
        await pilot.press("up")
        await pilot.pause()
        assert detail_text(app).startswith("S24 · ")

        key, row, scroll = app.row_keys[table.cursor_row], table.cursor_row, table.scroll_y
        assert "Activity: Working · 16m 17s" in detail_text(app)
        current["snapshot"] = replace(current["snapshot"], observed_at=NOW + 60)
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys[table.cursor_row] == key
        assert (table.cursor_row, table.scroll_y) == (row, scroll)
        assert "Activity: Working · 17m 17s" in detail_text(app)


@pytest.mark.asyncio
async def test_overview_columns_fit_the_list_beside_the_preview():
    rows = [
        session_row(
            number, f"wt{number}", work_dir="/work/projects/fifo", branch="feature/long-branch"
        )
        for number in range(3)
    ]
    activity = {row.attribution.key: Activity(WORKING, stamp(190)) for row in rows}
    app = DashboardApp(lambda: snapshot_of(rows, activity=activity))
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "Started" not in app._overview_header.plain
        assert app._overview_width == app._list_width() < 100
        assert all(cell_len(row[0].plain) <= app._overview_width for row in app._rows)

        # Without the preview the list is full width again and keeps every column.
        await pilot.resize_terminal(119, 30)
        await pilot.pause()
        await pilot.pause()
        assert "Started" in app._overview_header.plain


@pytest.mark.asyncio
async def test_enter_from_preview_opens_unchanged_session_detail():
    rows = [session_row(0, "wt0", work_dir="/work/a", branch="goal/fixture", calls=ONE_CALL)]
    goal = goal_view("wt0", "active", met=1, goals=(0,) * 2, pending_proposals=0)
    job = job_view("one", "J-SIM1", "running")
    activity = {"codex:00": Activity(WORKING, stamp(100))}
    app = DashboardApp(lambda: snapshot_of(rows, jobs=[job], goals=[goal], activity=activity))
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert app.query_one("#aside").display
        assert detail_text(app).startswith("S1 · Goal Mode")
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "session"
        assert not app.query_one("#aside").display
        assert str(app.query_one("#orientation", Static).render()) == ORIENTATION
        assert app.row_keys == ROW_KEYS


# Captured from the unmodified session detail of the fixture above.
ORIENTATION = (
    "S1 · /work/a · goal/fixture · Goal Mode · connected · Started 5m ago\n"
    "Working · 1m 40s · \n"
    "Last Booley call: sim · 5m ago\n"
    "Session key: codex:00"
)
ROW_KEYS = ["Lint top clean", "Sim fifo passes", 'job:["/jobs", "one"]', "call:0"]

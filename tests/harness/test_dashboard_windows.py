"""Dashboard windows (Sessions, Jobs), per-window history and the top bar (#2242).

Assertions read observable state only (current view, focused widget, widget
regions and labels) so each one fails in the test body on a Dashboard without
windows.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import DataTable, Header

from booley.harness.dashboard import sessions
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.mcp.session_registry import RecentCall
from booley.runtime.job_records import JobRecord
from tests.harness.test_dashboard import (
    STAMP,
    _header_health,
    choose_in_bar,
    open_session,
    snapshot,
)

# The session list starts with the running Jobs owned by the session.
JOB_ROW = 0


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)


def world(*, jobs=45):
    """Two sessions; S1 also owns `jobs` extra running Jobs."""
    current = snapshot(2)
    extra = tuple(
        JobView(
            Path("/jobs"),
            JobRecord(f"extra-{index}", "lint", STAMP, 60, session_key="codex:00"),
            "running",
        )
        for index in range(jobs)
    )
    return replace(
        current,
        jobs=JobSnapshot((*current.jobs.jobs, *extra)),
        sessions=(
            replace(current.sessions[0], calls=(RecentCall(STAMP, "sim", "completed"),)),
            *current.sessions[1:],
        ),
    )


def table_of(app):
    return app.query_one(DataTable)


def job_ids(app):
    return [json.loads(key)[1] for key in app.row_keys]


def column_label(app):
    return str(next(iter(table_of(app).columns.values())).label)


async def open_row(app, pilot, row):
    """Put the list cursor on a row and open it."""
    table_of(app).move_cursor(row=row, animate=False)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# --- AC1 ---------------------------------------------------------------------

# name -> (starts in the Jobs window, how to reach the depth, view expected there)
DEPTHS = {
    "sessions-overview": (False, (), "overview"),
    "session": (False, (("session", "codex:00"),), "session"),
    "job-from-session": (False, (("session", "codex:00"), ("open", JOB_ROW)), "job"),
    "sessions-help": (False, (("help",),), "help"),
    "jobs-list": (True, (), "jobs"),
    "jobs-job": (True, ("enter",), "job"),
    "jobs-help": (True, (("help",),), "help"),
}


async def _switch_from_depth(depth):
    """Reach one depth in a fresh Dashboard, switch windows and come back."""
    in_jobs, steps, view = DEPTHS[depth]
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        if in_jobs:
            await pilot.press("right")
            assert app.navigation.view == "jobs", depth
        for step in steps:
            if isinstance(step, tuple) and step[0] == "session":
                await open_session(app, pilot, step[1])
            elif isinstance(step, tuple) and step[0] == "help":
                await choose_in_bar(app, pilot, "Help")
            elif isinstance(step, tuple):
                await open_row(app, pilot, step[1])
            else:
                await pilot.press(step)
        await pilot.pause()
        assert app.navigation.view == view, depth  # the depth was really reached
        before = app.navigation

        await pilot.press("left" if in_jobs else "right")
        await pilot.pause()
        assert app.navigation.view == ("overview" if in_jobs else "jobs"), depth

        await pilot.press("right" if in_jobs else "left")
        await pilot.pause()
        assert app.navigation == before, depth


@pytest.mark.asyncio
async def test_arrows_switch_windows_from_every_depth():
    for depth in DEPTHS:
        await _switch_from_depth(depth)


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switching_back_restores_detail_selection_and_scroll():
    current = world()
    app = DashboardApp(lambda: current)
    # Short enough that the Job detail still overflows its panel (no padding rows, #2886).
    async with app.run_test(size=(90, 8)) as pilot:
        await pilot.press("right")
        assert app.navigation.view == "jobs"
        table = table_of(app)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        selected, scroll = app.row_keys[table.cursor_row], table.scroll_y
        assert scroll > 0

        await pilot.press("left")
        await open_session(app, pilot, "codex:00")
        session = app.navigation
        assert session.view == "session"

        await pilot.press("right")
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert app.row_keys[table.cursor_row] == selected
        assert table.scroll_y == scroll

        # A scrolled narrow Job detail keeps its offset per window as well.
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "job"
        aside = app.query_one("#aside")
        aside.scroll_to(y=3, animate=False)
        await pilot.pause()
        assert aside.scroll_y == 3
        await pilot.press("left")
        await pilot.pause()
        assert app.navigation == session
        await pilot.press("right")
        await pilot.pause()
        assert app.navigation.view == "job"
        assert aside.scroll_y == 3


# --- AC3 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_goes_back_within_window_and_stops_at_top():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        await open_session(app, pilot, "codex:00")
        assert app.navigation.view == "session"
        await pilot.press("escape")
        assert app.navigation.view == "overview"
        await pilot.press("escape")
        assert app.navigation.view == "overview"

        await pilot.press("right")
        assert app.navigation.view == "jobs"
        await pilot.press("enter")
        assert app.navigation.view == "job"
        await pilot.press("escape")
        assert app.navigation.view == "jobs"
        await pilot.press("escape")
        assert app.navigation.view == "jobs"
        await pilot.press("left")
        assert app.navigation.view == "overview"


# --- AC4 ---------------------------------------------------------------------


def _reverse_painted(bar):
    return any("reverse" in str(span.style) for span in bar.render().spans)


@pytest.mark.asyncio
async def test_up_on_first_row_focuses_top_bar_and_bar_keys():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.press("right")
        assert app.navigation.view == "jobs"
        table = table_of(app)
        await pilot.press("down")
        assert table.cursor_row == 1
        await pilot.press("up")
        assert table.cursor_row == 0
        assert app.focused is table

        await pilot.press("up")
        bar = app.query_one("#topbar")
        assert app.focused is bar
        assert bar.cursor == 1  # starts on the active window's tab
        assert _reverse_painted(bar)

        await pilot.press("left")
        assert bar.cursor == 0
        assert app.navigation.view == "jobs"  # choosing needs Enter
        # Jobs window bar: Sessions, Jobs, Targets, Running, Finished, All, owner, Run, Help.
        await pilot.press(*["right"] * 9)
        assert bar.cursor == 8  # clamped at Help, no wrap

        await pilot.press(*["left"] * 8, "enter")
        await pilot.pause()
        assert app.navigation.view == "overview"
        assert app.focused is bar

        await pilot.press("right", "enter")
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert app.focused is bar

        await pilot.press(*["right"] * 7, "enter")
        await pilot.pause()
        assert app.navigation.view == "help"
        # Help has no list: the detail takes the keyboard at once (#2886).
        assert app.focused is app.query_one("#aside")
        assert not _reverse_painted(bar)

        depth = len(app.windows["jobs"])
        await pilot.press("up")  # Help is at scroll 0, so up reaches the top bar
        assert app.focused is bar
        await pilot.press(*["right"] * 6, "enter")
        await pilot.pause()
        assert len(app.windows["jobs"]) == depth  # Help is not stacked twice


@pytest.mark.asyncio
async def test_up_in_empty_list_focuses_top_bar_and_escape_returns():
    current = replace(snapshot(2), jobs=JobSnapshot(()))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.press("right")
        assert app.navigation.view == "jobs"
        assert table_of(app).row_count == 0
        await pilot.press("up")
        bar = app.query_one("#topbar")
        assert app.focused is bar
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is table_of(app)
        assert app.navigation.view == "jobs"


@pytest.mark.asyncio
async def test_up_in_detail_scrolls_then_focuses_top_bar():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 14)) as pilot:
        await choose_in_bar(app, pilot, "Help")
        assert app.navigation.view == "help"
        aside = app.query_one("#aside")
        assert app.focused is aside
        assert aside.max_scroll_y > 2

        aside.scroll_to(y=2, animate=False)
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is aside  # scrolled, not yet at the top

        aside.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.press("up")
        bar = app.query_one("#topbar")
        assert app.focused is bar
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is aside


# --- AC5 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_bar_replaces_header_and_status_shares_one_line(monkeypatch, tmp_path):
    current = replace(
        world(),
        health=_header_health(monkeypatch, tmp_path, upgrade=True),
        diagnostics=("diagnostic [literal/path]",),
    )
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        assert not app.query(Header)
        bar = app.query_one("#topbar")
        assert bar.region.y == 0
        text = bar.render()
        for label in ("Sessions", "Jobs", "Help"):
            assert label in text.plain
        start = text.plain.index("Sessions")
        assert any(
            span.start <= start < span.end and "bold" in str(span.style) for span in text.spans
        )

        resources, doctor = app.query_one("#resources"), app.query_one("#health")
        assert resources.region.y == doctor.region.y == 1
        assert str(doctor.content).startswith("Doctor:")
        assert "Upgrade review" not in str(doctor.content)

        notices = app.query_one("#notices")
        assert notices.display
        assert "Upgrade review" in str(notices.content)
        assert "diagnostic [literal/path]" in str(notices.content)

        app._show_stale("read failed")
        assert "unavailable/stale: read failed" in str(notices.content)
        assert str(doctor.content).startswith("Doctor:")


@pytest.mark.asyncio
async def test_notices_hidden_when_there_is_nothing_to_say():
    app = DashboardApp(world)
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        assert not app.query_one("#notices").display


# --- AC6 ---------------------------------------------------------------------


# --- AC7 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_header_is_set_per_view():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        overview = column_label(app)
        # The overview header is the aligned column line.
        assert overview.split()[:2] == ["Session", "Mode"]
        assert overview.split()[-1] == "Activity"
        await open_session(app, pilot, "codex:00")
        # A session's list is its Booley activity alone: no section rule between lists.
        assert column_label(app) == "Booley activity"
        assert not any(key.startswith(sessions.HEADER_PREFIX) for key in app.row_keys)
        await pilot.press("escape")
        await pilot.pause()
        assert column_label(app) == overview
        await pilot.press("right")
        await pilot.pause()
        assert [str(c.label) for c in table_of(app).ordered_columns] == [
            "Job",
            "Target",
            "Session",
            "State",
            "Result",
            "Crit",
            "Elapsed",
        ]


# --- AC8 ---------------------------------------------------------------------


async def _choose_filters_and_cycle_owner(app, pilot):
    await choose_in_bar(app, pilot, "Finished")
    assert (app.navigation.session, app.navigation.job_filter) == (None, "finished")
    await choose_in_bar(app, pilot, "All")
    assert (app.navigation.session, app.navigation.job_filter) == (None, "all")
    # The owner chip cycles All sessions, S1, S2, Standalone and back to All sessions.
    for _ in range(4):
        await choose_in_bar(app, pilot, "Owner")
    assert (app.navigation.session, app.navigation.job_filter) == (None, "all")


@pytest.mark.asyncio
async def test_arrows_and_top_bar_reach_same_content_in_matching_window():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        # Opening a session moves the overview cursor onto it; the overview remembers it.
        await open_session(app, pilot, "codex:01")
        await pilot.press("right")
        nav = app.navigation
        # The Jobs window opens on all sessions, not on the session it came from.
        assert (nav.view, nav.session, nav.job_filter) == ("jobs", None, "running")
        assert "two" not in job_ids(app)
        await pilot.press("left")
        assert (app.navigation.view, app.navigation.session) == ("session", "codex:01")
        await pilot.press("right")
        assert app.navigation.view == "jobs"

        await _choose_filters_and_cycle_owner(app, pilot)
        await choose_in_bar(app, pilot, "Finished")
        assert job_ids(app) == ["two"]
        await choose_in_bar(app, pilot, "Running")
        assert app.navigation.job_filter == "running"
        assert "two" not in job_ids(app)  # the finished Job does not stay pinned

        await pilot.press("down", "left", "escape")
        await pilot.pause()
        assert app.navigation.view == "overview"
        assert app._selected_key() == "codex:01"
        await pilot.press("right")
        assert app.navigation.view == "jobs"

        await choose_in_bar(app, pilot, "Help")
        assert app.navigation.view == "help"
        await pilot.press("left", "right")
        assert app.navigation.view == "help"
        await pilot.press("escape")
        assert app.navigation.view == "jobs"

        await pilot.press("left")
        await open_session(app, pilot, "codex:00")
        assert (app.navigation.view, app.navigation.session) == ("session", "codex:00")
        await pilot.press("right")
        assert app.navigation.view == "jobs"


# --- review fixes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_tick_keeps_focus_where_the_user_put_it():
    """A refresh must not pull the keyboard back from the detail pane (wide Jobs view)."""
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.press("right")
        aside = app.query_one("#aside")
        aside.focus()
        await pilot.pause()
        assert app.focused is aside

        await app.refresh_snapshot()
        app.render_snapshot()
        await pilot.pause()
        assert app.focused is aside


@pytest.mark.asyncio
async def test_filter_choice_after_scrolling_resets_cursor_into_view():
    """Choosing another filter after scrolling the Jobs list starts at the top."""
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 12)) as pilot:
        await pilot.press("right")
        table = table_of(app)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        assert table.scroll_y > 0
        # Cursor to the first row without scrolling, so `up` reaches the top bar.
        table.move_cursor(row=0, scroll=False)
        await pilot.pause()
        # Re-choosing Running restores its remembered offset; the reset must undo that.
        await choose_in_bar(app, pilot, "Running")
        await pilot.press("down")
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert app.navigation.job_filter == "running"
        assert table.cursor_row == 0
        assert table.scroll_y == 0

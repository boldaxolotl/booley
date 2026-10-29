"""Dashboard windows (Sessions, Jobs), per-window history and the top bar (#2242).

Assertions read observable state only (current view, focused widget, widget
regions and labels) so each one fails in the test body on a Dashboard without
windows.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Header, Static

from booley.criteria.presentation import CriterionPresentation
from booley.goals.model import GoalFamily, GoalSpec
from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.mcp.session_registry import RecentCall
from booley.runtime.job_records import JobRecord
from tests.harness.test_dashboard import STAMP, _header_health, snapshot

# Goal keys in the order the session list must show them (failing first).
GOAL_ORDER = ["g-failing", "g-recheck", "g-notrun", "g-checking", "g-passing"]
# The session list starts with its five Goals, then the pending proposal row,
# then the running Jobs owned by the session.
PROPOSAL_ROW = len(GOAL_ORDER)
JOB_ROW = PROPOSAL_ROW + 1


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)


def _goal_detail(key, state, *, checking=False, artifacts=()):
    return model.GoalDetail(
        key,
        state,
        CriterionPresentation("Lint top", "Target top", "must pass", "", "Target top · must pass"),
        {"recorded_at": STAMP, "detail": {"findings": 1}},
        checking=checking,
        artifacts=artifacts,
    )


def world(*, jobs=45, artifacts=0):
    """Two sessions; S1 has a Goal of five states, a proposal and `jobs` extra running Jobs."""
    current = snapshot(2)
    proposal = SimpleNamespace(
        proposal=SimpleNamespace(
            id="change",
            before=None,
            after=GoalSpec("lint_clean_top", GoalFamily.LINT, "top", {}, ("ad-hoc",)),
            to_json=lambda: {
                "id": "change",
                "kind": "relax",
                "before": None,
                "after": {},
                "rationale": "r",
            },
        ),
        state="pending",
        decision=None,
        closed_by_abandonment=False,
    )
    evidence_paths = tuple(f"/evidence/path-{index}.log" for index in range(artifacts))
    # Deliberately unordered: the list must sort by status.
    details = (
        _goal_detail("g-passing", "passing"),
        _goal_detail("g-checking", "passing", checking=True),
        _goal_detail("g-failing", "failing", artifacts=evidence_paths),
        _goal_detail("g-notrun", "not yet run"),
        _goal_detail("g-recheck", "needs recheck"),
    )
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key="wt0"),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
        status=SimpleNamespace(met=1, goals=details, pending_proposals=1, proposals=(proposal,)),
        goals=details,
    )
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
        goals=(goal,),
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
    "session": (False, ("1",), "session"),
    "goal": (False, ("1", "enter"), "goal"),
    "proposal": (False, ("1", ("open", PROPOSAL_ROW)), "proposal"),
    "job-from-session": (False, ("1", ("open", JOB_ROW)), "job"),
    "sessions-help": (False, ("?",), "help"),
    "jobs-list": (True, (), "jobs"),
    "jobs-job": (True, ("enter",), "job"),
    "jobs-help": (True, ("?",), "help"),
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
            if isinstance(step, tuple):
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
    async with app.run_test(size=(90, 12)) as pilot:
        await pilot.press("right")
        assert app.navigation.view == "jobs"
        table = table_of(app)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        selected, scroll = app.row_keys[table.cursor_row], table.scroll_y
        assert scroll > 0

        await pilot.press("left")
        await pilot.press("1", "enter")
        await pilot.pause()
        assert app.navigation.view == "goal"
        goal = app.navigation
        assert goal.selected == "g-failing"

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
        assert app.navigation == goal
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
        await pilot.press("1", "enter")
        assert app.navigation.view == "goal"
        await pilot.press("escape")
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
        # Jobs window bar: Sessions, Jobs, Running, Finished, All, owner, Help.
        await pilot.press(*["right"] * 7)
        assert bar.cursor == 6  # clamped at Help, no wrap

        await pilot.press(*["left"] * 6, "enter")
        await pilot.pause()
        assert app.navigation.view == "overview"
        assert app.focused is bar

        await pilot.press("right", "enter")
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert app.focused is bar

        await pilot.press(*["right"] * 5, "enter")
        await pilot.pause()
        assert app.navigation.view == "help"
        depth = len(app.windows["jobs"])
        await pilot.press("enter")
        assert len(app.windows["jobs"]) == depth  # Help is not stacked twice

        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one("#aside")  # Help has no list: the detail owns the keys
        assert not _reverse_painted(bar)


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
    current = world(artifacts=40)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 14)) as pilot:
        await pilot.press("1", "enter")
        await pilot.pause()
        assert app.navigation.view == "goal"
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


@pytest.mark.asyncio
async def test_session_goals_single_column_status_order_and_enter_opens_evidence():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(180, 30)) as pilot:
        await pilot.press("1")
        await pilot.pause()
        assert len(table_of(app).columns) == 1
        assert app.row_keys[: len(GOAL_ORDER)] == GOAL_ORDER
        table_of(app).move_cursor(row=0, animate=False)
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "goal"
        assert app.navigation.selected == GOAL_ORDER[0]
        assert "Last check:" in str(app.query_one("#detail", Static).render())


# --- AC7 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_header_is_set_per_view():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        overview = column_label(app)
        assert overview.startswith("Sessions")
        await pilot.press("1")
        await pilot.pause()
        assert column_label(app) == "Goals and activity"
        await pilot.press("escape")
        await pilot.pause()
        assert column_label(app) == overview
        await pilot.press("j")
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


@pytest.mark.asyncio
async def test_shortcuts_reach_same_content_in_matching_window():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.press("down")  # remember the second overview row
        await pilot.press("1", "j")
        nav = app.navigation
        # The Jobs window opens on all sessions, not on the session it came from.
        assert (nav.view, nav.session, nav.job_filter) == ("jobs", None, "running")
        assert "two" not in job_ids(app)
        await pilot.press("left")
        assert (app.navigation.view, app.navigation.session) == ("session", "codex:00")
        await pilot.press("right")
        assert app.navigation.view == "jobs"

        await pilot.press("r")
        assert (app.navigation.session, app.navigation.job_filter) == (None, "finished")
        await pilot.press("a")
        assert (app.navigation.session, app.navigation.job_filter) == (None, "all")
        await pilot.press("u")
        assert (app.navigation.session, app.navigation.job_filter) == (None, "all")
        await pilot.press("r")
        assert job_ids(app) == ["two"]
        await pilot.press("j")
        assert app.navigation.job_filter == "running"
        assert "two" not in job_ids(app)  # the finished Job does not stay pinned

        await pilot.press("s")
        await pilot.pause()
        assert app.navigation.view == "overview"
        assert app._selected_key() == "codex:01"
        await pilot.press("right")
        assert app.navigation.view == "jobs"

        await pilot.press("?")
        assert app.navigation.view == "help"
        await pilot.press("left", "right")
        assert app.navigation.view == "help"
        await pilot.press("escape")
        assert app.navigation.view == "jobs"

        await pilot.press("1")
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
async def test_job_shortcut_after_scrolling_resets_cursor_into_view():
    """j after scrolling the Jobs list starts at the top, not at the old offset."""
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 12)) as pilot:
        await pilot.press("right")
        table = table_of(app)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        assert table.scroll_y > 0
        await pilot.press("left")
        await pilot.press("j")
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert table.cursor_row == 0
        assert table.scroll_y == 0

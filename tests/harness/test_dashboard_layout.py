"""Dashboard panels start right below the orientation line (#2886).

An empty status line and the side panel's vertical padding used to take blank
rows in every list and detail view. Assertions read widget regions only, so
each one fails in the test body on a Dashboard that still reserves them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from textual.widgets import DataTable, Static

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.app import NO_SESSIONS, DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot
from tests.harness.test_dashboard import choose_in_bar, open_session, snapshot
from tests.harness.test_dashboard_sessions import standalone_job
from tests.harness.test_dashboard_windows import PROPOSAL_ROW, open_row, world

NARROW = (90, 24)
WIDE = (160, 30)


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)


async def _open_help(app, pilot):
    await choose_in_bar(app, pilot, "Help")
    assert app.navigation.view == "help"


async def _open_goal(app, pilot):
    await open_session(app, pilot, "codex:00")
    await pilot.press("enter")
    assert app.navigation.view == "goal"


async def _open_proposal(app, pilot):
    await open_session(app, pilot, "codex:00")
    await open_row(app, pilot, PROPOSAL_ROW)
    assert app.navigation.view == "proposal"


async def _open_jobs_job(app, pilot):
    await pilot.press("right", "enter")
    await pilot.pause()
    assert app.navigation.view == "job"


async def _open_jobs(app, pilot):
    await pilot.press("right")
    await pilot.pause()
    assert app.navigation.view == "jobs"


async def _nothing(_app, _pilot):
    """The view the Dashboard opens on."""


async def _open_session(app, pilot):
    await open_session(app, pilot, "codex:00")
    assert app.navigation.view == "session"


# case id -> (terminal size, snapshot, how to reach the view, view expected there)
CASES = {
    "overview-narrow": (NARROW, world(), _nothing, "overview"),
    "overview-wide-preview": (WIDE, world(), _nothing, "overview"),
    "session": (NARROW, world(), _open_session, "session"),
    "jobs-narrow": (NARROW, world(), _open_jobs, "jobs"),
    "jobs-wide-detail": (WIDE, world(), _open_jobs, "jobs"),
    "help": (NARROW, world(), _open_help, "help"),
    "goal": (NARROW, world(), _open_goal, "goal"),
    "job-narrow": (NARROW, world(), _open_jobs_job, "job"),
    "proposal": (NARROW, world(), _open_proposal, "proposal"),
    # Here `#empty` correctly shows text, so only the panel itself is checked.
    "jobs-empty-wide": (
        WIDE,
        replace(snapshot(2), jobs=JobSnapshot(())),
        _open_jobs,
        "jobs",
    ),
    "overview-standalone-only-wide": (
        WIDE,
        replace(snapshot(0), jobs=JobSnapshot((standalone_job(),))),
        _nothing,
        "overview",
    ),
}
# Views whose `#empty` line legitimately shows text.
EMPTY_TEXT_CASES = {"jobs-empty-wide", "overview-standalone-only-wide"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(CASES))
async def test_panel_starts_below_orientation(case):
    size, current, reach, view = CASES[case]
    app = DashboardApp(lambda: current)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await reach(app, pilot)
        await pilot.pause()
        assert app.navigation.view == view

        orientation = app.query_one("#orientation")
        empty = app.query_one("#empty")
        body = app.query_one("#body")
        if case in EMPTY_TEXT_CASES:
            assert empty.display
            assert body.region.y == empty.region.bottom
        else:
            assert not empty.display
            assert body.region.y == orientation.region.bottom

        aside = app.query_one("#aside")
        if aside.display:
            # `region` includes padding; the content region excludes it.
            assert aside.scrollable_content_region.y == aside.region.y
            assert aside.scrollable_content_region.bottom == aside.region.bottom
        else:
            assert case in {"session", "jobs-narrow", "overview-narrow"}


@pytest.mark.asyncio
async def test_help_fills_default_panel_height():
    current = world()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 13)) as pilot:
        await pilot.pause()
        await _open_help(app, pilot)
        await pilot.pause()
        assert not app.query_one("#notices").display
        aside = app.query_one("#aside")
        orientation = app.query_one("#orientation")
        footer = app.query_one("#footer")
        rows = footer.region.y - orientation.region.bottom
        assert aside.scrollable_content_region.height == rows


@pytest.mark.asyncio
async def test_empty_state_line_shows_only_with_text():
    # 90 columns: NO_SESSIONS is 80 characters long and must not wrap.
    box = [replace(snapshot(0), jobs=JobSnapshot(()))]
    app = DashboardApp(lambda: box[0])
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        empty = app.query_one("#empty", Static)
        assert empty.display
        assert empty.region.height == 1
        assert str(empty.render()) == NO_SESSIONS

        box[0] = snapshot(3)
        await app.refresh_snapshot()
        await pilot.pause()
        assert not empty.display
        assert app.query_one(DataTable).display

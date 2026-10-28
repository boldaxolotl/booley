"""Jobs filters live in the Jobs window's top bar, with clear empty states (#2243).

Assertions read observable state only (bar text and spans, the navigation, the
listed rows, the orientation and empty-state text), so each one fails in the
test body on a Dashboard whose Jobs window has no filters in its bar.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.widgets import Static

from booley.harness.dashboard.app import HELP, DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.harness.dashboard.topbar import TopBar
from booley.mcp.session_registry import Attribution, RecentCall, SessionRow
from booley.runtime.job_records import JobRecord
from tests.harness.test_dashboard import STAMP, snapshot

# Positions in the Jobs window's bar: Sessions, Jobs, Running, Finished, All, owner, Help.
RUNNING, FINISHED, ALL, OWNER = 2, 3, 4, 5
ALIAS_KEY = "worktree:path:/fixture/0"
GOAL_MODE_DOC = Path(__file__).parents[2] / "docs" / "user" / "GOAL-MODE.md"


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)


def job(run_id, state, session_key=None, *, owner_handle=""):
    """A retained Job; finished states carry the record fields a finished Job has."""
    finished = state in {"completed", "failed", "cancelled"}
    record = JobRecord(
        run_id,
        "sim",
        STAMP,
        60,
        session_key=session_key,
        status="done" if finished else "",
        ended_at=STAMP if finished else None,
    )
    return JobView(Path("/jobs"), record, state, owner_handle=owner_handle)


def world(jobs, *, sessions=2, handles=None):
    """Sessions S1 and S2 (codex:00, codex:01) holding the given Jobs."""
    current = snapshot(sessions)
    if handles:
        current = replace(current, session_handles={**current.session_handles, **handles})
    return replace(current, jobs=JobSnapshot(tuple(jobs)))


def bar_text(app):
    return app.query_one(TopBar).render().plain


def owner_chip(app):
    """The owner filter's label as the bar shows it."""
    plain = bar_text(app)
    assert "Owner: " in plain, f"no owner chip in the bar: {plain!r}"
    return plain.split("Owner: ")[1].split(" │")[0].strip()


def empty_text(app):
    return str(app.query_one("#empty", Static).render())


def orientation(app):
    return str(app.query_one("#orientation", Static).render())


def listed(app):
    return [json.loads(key)[1] for key in app.row_keys]


async def focus_bar(app, pilot):
    await pilot.press("up")
    assert isinstance(app.focused, TopBar)


async def choose(app, pilot, index):
    """Move the focused bar's cursor to an item with the arrows and press Enter."""
    bar = app.query_one(TopBar)
    step = index - bar.cursor
    await pilot.press(*(["right" if step > 0 else "left"] * abs(step)), "enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_top_bar_status_filters_reachable_with_arrows_and_applied_with_enter():
    current = world([job("one", "running", "codex:00"), job("two", "completed")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        assert "Finished" not in bar_text(app)  # the Sessions window has no Jobs filters
        await pilot.press("right")
        assert app.navigation.job_filter == "running"
        assert listed(app) == ["one"]
        for label in ("Running", "Finished", "All"):
            assert label in bar_text(app)

        await focus_bar(app, pilot)
        bar = app.query_one(TopBar)
        await pilot.press("right", "right")
        assert bar.cursor == FINISHED  # arrows reach the filters; Enter applies
        assert app.navigation.job_filter == "running"
        await pilot.press("enter")
        assert app.navigation.job_filter == "finished"
        assert listed(app) == ["two"]
        assert app.focused is bar

        # The active filter, and only it, is highlighted.
        plain, spans = bar.render().plain, bar.render().spans

        def bold(label):
            start = plain.index(label)
            return any(
                span.start <= start < span.end and "bold" in str(span.style) for span in spans
            )

        assert bold("Finished") and not bold("Running") and not bold("All")

        await choose(app, pilot, ALL)
        assert app.navigation.job_filter == "all"
        assert sorted(listed(app)) == ["one", "two"]
        await choose(app, pilot, RUNNING)
        assert app.navigation.job_filter == "running"
        assert listed(app) == ["one"]


@pytest.mark.asyncio
async def test_owner_chip_reachable_with_arrows_and_applied_with_enter():
    current = world([job("one", "running", "codex:00"), job("two", "running")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right")
        assert owner_chip(app) == "All sessions"
        assert orientation(app).endswith("All sessions")
        assert sorted(listed(app)) == ["one", "two"]

        await focus_bar(app, pilot)
        bar = app.query_one(TopBar)
        await pilot.press("right", "right", "right", "right")
        assert bar.cursor == OWNER
        assert app.navigation.session is None  # arrows alone change nothing
        await pilot.press("enter")
        assert app.navigation.session == "codex:00"
        assert owner_chip(app) == "S1"
        assert orientation(app).endswith("S1")
        assert listed(app) == ["one"]
        assert app.focused is bar


@pytest.mark.asyncio
async def test_owner_chip_cycles_handles_in_order_then_standalone():
    jobs = [
        job("one", "running", "codex:00"),
        job("alias", "completed", ALIAS_KEY, owner_handle="S1"),
        job("gone", "completed", "codex:gone", owner_handle="S17"),
        job("alone", "completed"),
    ]
    box = [world(jobs, handles={ALIAS_KEY: "S1"})]
    app = DashboardApp(lambda: box[0])
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right", "a")
        assert owner_chip(app) == "All sessions"
        assert sorted(listed(app)) == ["alias", "alone", "gone", "one"]

        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "S1"
        assert sorted(listed(app)) == ["alias", "one"]  # the alias-owned Job belongs to S1
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "S2"  # connected, no Jobs, still listed
        assert listed(app) == []
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "S17"  # known only through a retained Job
        assert listed(app) == ["gone"]

        # An aliased session connecting under that handle must not break the cycle.
        newcomer = SessionRow(
            Attribution("codex:new", "thread", "/fixture/new", "wtnew"),
            STAMP,
            STAMP,
            (RecentCall(STAMP, "sim", "completed"),),
        )
        current = box[0]
        box[0] = replace(
            current,
            sessions=(*current.sessions, newcomer),
            session_handles={**current.session_handles, "codex:new": "S17"},
        )
        await app.refresh_snapshot()
        await pilot.pause()
        assert owner_chip(app) == "S17"
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "Standalone"
        assert listed(app) == ["alone"]
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "All sessions"
        assert app.navigation.session is None


@pytest.mark.asyncio
async def test_jobs_window_opens_on_all_sessions_from_any_window_or_detail():
    current = world([job("one", "running", "codex:00"), job("two", "running")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        for key in ("j", "r", "a"):
            await pilot.press("1", key)
            assert app.navigation.view == "jobs"
            assert app.navigation.session is None, f"{key} from a session detail"
            assert orientation(app).endswith("All sessions")
        await pilot.press("1", "right")
        assert (app.navigation.view, app.navigation.session) == ("jobs", None)
        await pilot.press("s")
        assert app.window == "sessions"
        await pilot.press("right")
        assert app.navigation.session is None
        # Through the Jobs tab of the top bar, from a session detail.
        await pilot.press("1")
        await focus_bar(app, pilot)
        await choose(app, pilot, 1)
        assert (app.window, app.navigation.session) == ("jobs", None)

        # An owner chosen inside the Jobs window is dropped when the window is
        # opened again from anywhere; the status filter is kept.
        await choose(app, pilot, OWNER)
        assert app.navigation.session == "codex:00"
        await choose(app, pilot, RUNNING)
        assert listed(app) == ["one"]
        await pilot.press("down", "s")  # back to the list, then to Sessions
        await pilot.press("right")
        assert (app.navigation.session, app.navigation.job_filter) == (None, "running")
        assert sorted(listed(app)) == ["one", "two"]

        # The same through j from a session detail.
        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)
        assert app.navigation.session == "codex:00"
        await pilot.press("down", "s", "1", "j")
        assert app.navigation.session is None
        assert sorted(listed(app)) == ["one", "two"]

        # Inside the Jobs window r keeps the owner and u clears it.
        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)
        await pilot.press("down", "r")
        assert app.navigation.session == "codex:00"
        await pilot.press("u")
        assert app.navigation.session is None


@pytest.mark.asyncio
async def test_running_empty_names_finished_count():
    current = world([job("a", "completed", "codex:00"), job("b", "failed"), job("c", "completed")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right")
        assert listed(app) == []
        assert empty_text(app) == "No Jobs running. 3 finished — choose Finished above."
        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "S1"
        assert (
            empty_text(app) == "No Jobs running for S1. S1 has 1 finished — choose Finished above."
        )
        await choose(app, pilot, FINISHED)
        assert listed(app) == ["a"]
        assert empty_text(app) == ""


@pytest.mark.asyncio
async def test_finished_empty_says_none_yet():
    current = world([job("a", "running", "codex:00"), job("b", "queued")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right", "r")
        assert app.navigation.job_filter == "finished"
        assert listed(app) == []
        assert empty_text(app) == "No finished Jobs yet. 2 running — choose Running above."
        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "S1"
        assert empty_text(app) == "No finished Jobs for S1 yet. 1 running — choose Running above."


@pytest.mark.asyncio
async def test_owner_without_jobs_names_owner():
    current = world([job("a", "running", "codex:00")])
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right")
        await focus_bar(app, pilot)
        await choose(app, pilot, OWNER)  # S1
        await choose(app, pilot, OWNER)  # S2
        assert owner_chip(app) == "S2"
        assert empty_text(app) == "No Jobs for S2. Choose All sessions above."
        await choose(app, pilot, OWNER)
        assert owner_chip(app) == "Standalone"
        assert empty_text(app) == "No Jobs for Standalone. Choose All sessions above."
    nothing = world([])
    app = DashboardApp(lambda: nothing)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.press("right")
        assert empty_text(app) == "No Jobs yet."


@pytest.mark.asyncio
async def test_jobs_tab_counts_unfinished_jobs():
    box = [
        world(
            [
                job("a", "running", "codex:00"),
                job("b", "queued"),
                job("c", "completed"),
                job("d", "orphaned"),
            ]
        )
    ]
    app = DashboardApp(lambda: box[0])
    async with app.run_test(size=(100, 24)) as pilot:
        # An orphaned Job's process is known dead: it is not counted.
        assert "Jobs 2▶" in bar_text(app)
        await pilot.press("right")
        assert "Jobs 2▶" in bar_text(app)
        box[0] = world([job("c", "completed"), job("d", "orphaned")])
        await app.refresh_snapshot()
        await pilot.pause()
        assert "▶" not in bar_text(app)
        assert "Jobs" in bar_text(app)
        await pilot.press("left")
        assert "▶" not in bar_text(app)


@pytest.mark.asyncio
async def test_no_jobs_filter_text_says_recent_or_results():
    word = re.compile(r"\b(recent|results)\b", re.IGNORECASE)
    texts = [HELP]
    for binding in DashboardApp.BINDINGS:
        texts.append(binding.description if isinstance(binding, Binding) else binding[2])
    current = world([job("a", "completed", "codex:00"), job("b", "running")])
    empty = world([])
    for snap in (current, empty):
        app = DashboardApp(lambda snap=snap: snap)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("right")
            for key in ("j", "r", "a"):
                await pilot.press(key)
                texts += [bar_text(app), orientation(app), empty_text(app)]
    excerpt = GOAL_MODE_DOC.read_text(encoding="utf-8")
    excerpt = excerpt.split("The Dashboard has two windows")[1].split("Sessions have short")[0]
    texts.append(excerpt)
    assert [text for text in texts if word.search(text)] == []

"""The Jobs list is aligned columns that fit beside the Job detail (#1516)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import DataTable, Static

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.app import SEMANTIC_STYLE, DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.runtime.job_records import JobRecord
from booley.runtime.pid import ProcessState
from tests.harness.test_dashboard import STAMP, snapshot

HEADERS = ["Job", "Target", "Session", "State", "Result", "Crit", "Elapsed"]
LONG_TARGET = "asic_synthesize_" + "x" * 64
PASSED_WITH_WARNINGS = "PASSED with warnings"
UNMET_CRITERION = {"flow": "sim", "criterion_key": "sim_pass_top", "criterion_met": False}
LINT_WARNINGS = {"flow": "lint", "detail": {"total_warnings": 3}}


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023)."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)


def make_job(
    run_id,
    endpoint="sim",
    *,
    handle,
    state="running",
    code=None,
    report=None,
    target=None,
    stage="",
    session_key="codex:00",
):
    """One Job; terminal states carry an exit code and end time."""
    terminal = state in {"completed", "failed"}
    return JobView(
        Path("/jobs"),
        JobRecord(
            run_id,
            endpoint,
            STAMP,
            60,
            argv=["--target", target] if target else [],
            status="done" if terminal else "running",
            exit_code=code,
            ended_at=STAMP if terminal else None,
            session_key=session_key,
        ),
        state,
        report=report,
        report_path=Path("/reports/one.json") if report else None,
        stage=stage,
        handle=handle,
        owner_handle="S1" if session_key else "",
    )


def running_filler(count=25):
    """Running rows that force the vertical scrollbar."""
    return [make_job(f"fill{n}", "lint", handle=f"J-LINT{n + 10}") for n in range(count)]


def world(*jobs, process_state=None):
    current = snapshot(1)
    sessions = tuple(replace(row, process_state=process_state) for row in current.sessions)
    return replace(current, sessions=sessions, jobs=JobSnapshot(tuple(jobs)))


def headers(app):
    return [str(column.label) for column in app.query_one(DataTable).ordered_columns]


async def open_all_jobs(app, pilot):
    """Show the Jobs window's All list, then pause twice so the refit pass has run.

    Bar items from the Jobs tab: Running, Finished, All are one, two and three steps right.
    """
    await pilot.press("right", "up", "right", "right", "right", "enter", "down")
    await pilot.pause()
    await pilot.pause()


def row_for(app, job):
    """The cells of a Job's row, header assertion first so the base fails in the body."""
    assert headers(app) == HEADERS
    table = app.query_one(DataTable)
    return table.get_row_at(app.row_keys.index(json.dumps(job.key)))


def column_widths(app):
    table = app.query_one(DataTable)
    return {str(column.label): column.get_render_width(table) for column in table.ordered_columns}


@pytest.mark.asyncio
async def test_jobs_columns_fit_beside_detail_at_120x30():
    unmet = make_job("unmet", handle="J-SIM1", state="completed", code=0, report=UNMET_CRITERION)
    lint = make_job(
        "lint", "lint", handle="J-LINT1", state="completed", code=0, report=LINT_WARNINGS
    )
    running = make_job("run", handle="J-SIM2", target="asic_sim_top")
    current = world(unmet, lint, running, *running_filler())
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await open_all_jobs(app, pilot)
        assert app.query_one("#aside").display
        assert headers(app) == HEADERS
        table = app.query_one(DataTable)
        assert table.max_scroll_x == 0
        assert table.virtual_size.width <= table.scrollable_content_region.width
        assert sum(column_widths(app).values()) <= table.scrollable_content_region.width


@pytest.mark.asyncio
async def test_long_result_word_truncates_after_target_minimum():
    lint = make_job(
        "lint",
        "lint",
        handle="J-LINT1",
        state="completed",
        code=0,
        report=LINT_WARNINGS,
        target=LONG_TARGET,
    )
    current = world(lint, *running_filler())
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await open_all_jobs(app, pilot)
        assert app.query_one("#aside").display
        cells = row_for(app, lint)
        target, result = cells[1].plain, cells[4].plain
        widths = column_widths(app)
        # Target is cut to its minimum first, then the Result word gives way.
        assert target == LONG_TARGET[:6] + "…"
        assert result.endswith("…")
        assert PASSED_WITH_WARNINGS.startswith(result[:-1])
        assert result != PASSED_WITH_WARNINGS
        assert cell_len(result) <= widths["Result"]
        assert app.query_one(DataTable).max_scroll_x == 0
        # The selected Job's detail keeps the full word.
        app.query_one(DataTable).move_cursor(row=app.row_keys.index(json.dumps(lint.key)))
        await pilot.pause()
        assert f"Flow result: {PASSED_WITH_WARNINGS}" in str(
            app.query_one("#detail", Static).render()
        )


@pytest.mark.asyncio
async def test_headers_follow_overview_jobs_overview():
    current = world(make_job("run", handle="J-SIM1"))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        overview = [str(c.label) for c in app.query_one(DataTable).ordered_columns]
        assert len(overview) == 1 and overview[0].split()[:3] == ["Session", "Mode", "Branch"]
        await open_all_jobs(app, pilot)
        assert headers(app) == HEADERS
        assert not any("Sessions" in label for label in headers(app))
        await pilot.press("left")  # the overview is the Sessions window's top level here
        await pilot.pause()
        assert [str(c.label) for c in app.query_one(DataTable).ordered_columns] == overview
        await pilot.press("right")
        await pilot.pause()
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert headers(app) == HEADERS


@pytest.mark.asyncio
async def test_long_target_truncates_and_result_stays_visible():
    passed = make_job(
        "passed",
        handle="J-SIM1",
        state="completed",
        code=0,
        report={"flow": "sim"},
        target=LONG_TARGET,
    )
    current = world(passed)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await open_all_jobs(app, pilot)
        cells = row_for(app, passed)
        target = cells[1].plain
        assert target.endswith("…")
        assert LONG_TARGET.startswith(target[:-1])
        assert len(target) - 1 >= 6
        assert cell_len(target) <= column_widths(app)["Target"]
        assert cells[4].plain == "PASSED"
        assert app.query_one(DataTable).max_scroll_x == 0


@pytest.mark.asyncio
async def test_exit_zero_with_unmet_criterion_shows_passed_and_cross():
    unmet = make_job("unmet", handle="J-SIM1", state="completed", code=0, report=UNMET_CRITERION)
    current = world(unmet)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await open_all_jobs(app, pilot)
        cells = row_for(app, unmet)
        assert cells[4].plain == "PASSED"
        assert str(cells[4].style) == SEMANTIC_STYLE["pass"]
        assert cells[5].plain == "✗"
        assert str(cells[5].style) == SEMANTIC_STYLE["fail"]


@pytest.mark.asyncio
async def test_rows_hide_exit_code_stage_and_connection():
    done = make_job(
        "done",
        handle="J-SIM1",
        state="completed",
        code=0,
        report={"flow": "sim", "passed": True},
        stage="elaborate",
    )
    current = world(done, process_state=ProcessState.RUNNING)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await open_all_jobs(app, pilot)
        text = " ".join(cell.plain for cell in row_for(app, done))
        for hidden in ("exit", "elaborate", "connect", "last seen", "design verdict"):
            assert hidden not in text
        detail = str(app.query_one("#detail", Static).render())
        assert "exit 0" in detail
        assert "Stage: elaborate" in detail
        assert "Owner: S1 (connected)" in detail


@pytest.mark.asyncio
async def test_jobs_selection_and_scroll_survive_wide_window_round_trip():
    """Guard (green on the base too): the refit pass keeps the cursor and scroll."""
    current = world(*running_filler(45))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await open_all_jobs(app, pilot)
        table = app.query_one(DataTable)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        selected, scroll = app.row_keys[table.cursor_row], table.scroll_y
        assert scroll > 0
        await pilot.press("left")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.pause()
        assert app.navigation.view == "jobs"
        assert app.row_keys[table.cursor_row] == selected
        assert table.scroll_y == scroll

"""Sessions overview: groups, order, aligned columns, standalone Jobs, tab count (#2245).

The overview groups sessions by what needs the human, orders each group by how
long a session has been in its state, and lays every row out in columns that
narrow gracefully. Pure layout is checked through the ``sessions`` module; the
cursor, Enter, the tab count and the empty state through a Dashboard pilot.

Each test imports the new module in its own body so the file collects (and the
tests fail, rather than error) on a tree that predates it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from textual.widgets import DataTable, Static

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.harness.dashboard.topbar import TopBar
from booley.mcp.session_registry import Attribution, SessionRow
from booley.runtime.job_records import JobRecord
from booley.runtime.session_activity import UNKNOWN, WAITING_USER, WORKING, Activity
from booley.runtime.timefmt import rfc3339_from_epoch

NOW = 1791454200  # 2026-10-08T10:10:00Z
STARTED = rfc3339_from_epoch(NOW - 300)
NO_SESSIONS = "No agent sessions connected. Sessions appear when an agent client calls Booley."


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """A slow worker must finish the first read before any key press."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)


def stamp(seconds_ago: float) -> str:
    """The activity timestamp of something that began ``seconds_ago`` before NOW."""
    return rfc3339_from_epoch(NOW - seconds_ago)


def facts(number: int, state: str = WORKING, ago: float | None = 60, **overrides):
    """One session's overview facts; ``ago=None`` leaves the activity time empty."""
    from booley.harness.dashboard import sessions

    values = {
        "key": f"codex:{number:02}",
        "handle": f"S{number}",
        "mode": "Interactive",
        "branch": "main",
        "work_dir": f"/work/s{number}",
        "started": "5m ago",
        "activity": Activity(state, "" if ago is None else stamp(ago)),
        "proposals": 0,
    }
    return sessions.SessionFacts(**{**values, **overrides})


def world(activity: dict[str, Activity], count: int = 5, jobs=()):
    """A snapshot of ``count`` sessions ``codex:00``... with the given activity."""
    rows = tuple(
        SessionRow(
            Attribution(f"codex:{number:02}", "thread", f"/fixture/{number}", f"wt{number}"),
            STARTED,
            STARTED,
        )
        for number in range(count)
    )
    return model.DashboardSnapshot(
        rows,
        jobs=JobSnapshot(tuple(jobs)),
        observed_at=NOW,
        activity=activity,
        session_handles={row.attribution.key: f"S{index + 1}" for index, row in enumerate(rows)},
    )


def standalone_job(status: str = "", state: str = "running") -> JobView:
    """A running (or, by ``status``, finished) Job that belongs to no session."""
    record = JobRecord("three", "lint", STARTED, 60, argv=["--target", "lint_top"], status=status)
    return JobView(Path("/jobs"), record, state)


def plain_lines(overview):
    """Keys with the plain text of each rendered row."""
    return dict(zip(overview.keys, (row.plain for row in overview.rows), strict=True))


# --- AC1 ---------------------------------------------------------------------


def test_sessions_are_grouped_by_what_needs_you_with_counts():
    from booley.harness.dashboard import sessions

    overview = sessions.build(
        [
            facts(1, WAITING_USER, ago=100),
            facts(2, WORKING, ago=50, proposals=1),  # a proposal needs you whatever it does
            facts(3, WORKING),
            facts(4, "waiting for tool call"),
            facts(5, UNKNOWN, ago=None),
        ],
        [],
        NOW,
        120,
    )
    assert overview.keys == [
        "group:NEEDS YOU",
        "codex:01",
        "codex:02",
        "group:WORKING",
        "codex:03",
        "codex:04",
        "group:UNKNOWN",
        "codex:05",
    ]
    headers = [
        line.plain
        for key, line in zip(overview.keys, overview.rows, strict=True)
        if "group:" in key
    ]
    assert headers == ["NEEDS YOU 2", "WORKING 2", "UNKNOWN 1"]
    assert "dim" in str(overview.rows[overview.keys.index("group:UNKNOWN")].style)
    assert any("dim" in str(span.style) for span in overview.rows[-1].spans)


def test_empty_groups_are_hidden():
    from booley.harness.dashboard import sessions

    only_working = sessions.build([facts(3)], [], NOW, 120)
    assert only_working.keys == ["group:WORKING", "codex:03"]
    text = " ".join(row.plain for row in only_working.rows)
    assert "NEEDS YOU" not in text and "UNKNOWN" not in text and "STANDALONE" not in text
    assert sessions.build([], [], NOW, 120).keys == []


# --- AC2 ---------------------------------------------------------------------


def test_longest_in_state_first_ties_and_unparseable_by_handle_number():
    from booley.harness.dashboard import sessions

    overview = sessions.build(
        [
            facts(11, ago=None),  # unparseable time sorts last, by handle number
            facts(10, ago=100),  # ties with S2 on the time: S2 (2 < 10) comes first
            facts(2, ago=100),
            facts(3, ago=500),  # in state longest, so first
            facts(4, ago=None),
        ],
        [],
        NOW,
        120,
    )
    assert overview.keys[1:] == ["codex:03", "codex:02", "codex:10", "codex:04", "codex:11"]


# --- AC3 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_follows_selected_session_into_another_group():
    working = {f"codex:{n:02}": Activity(WORKING, stamp(1000 - n)) for n in range(25)}
    current = {"snapshot": world(working, count=25)}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(100, 14)) as pilot:
        table = app.query_one(DataTable)
        await pilot.pause()
        last = app.row_keys.index("codex:24")
        table.move_cursor(row=last, animate=False)
        await pilot.pause()
        assert app.row_keys[table.cursor_row] == "codex:24"

        # The scrolled-away selection starts waiting for the user: it moves to the top group.
        current["snapshot"] = world({**working, "codex:24": Activity(WAITING_USER, stamp(10))}, 25)
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys[:2] == ["group:NEEDS YOU", "codex:24"]
        assert app.row_keys[table.cursor_row] == "codex:24"
        assert table.cursor_row == 1
        visible = table.scrollable_content_region.height - table.header_height
        assert table.scroll_y <= table.cursor_row < table.scroll_y + visible


# --- AC4 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arrows_and_enter_skip_group_headers():
    activity = {
        "codex:00": Activity(WAITING_USER, stamp(300)),
        "codex:01": Activity(WAITING_USER, stamp(200)),
        "codex:02": Activity(WORKING, stamp(300)),
        "codex:03": Activity(WORKING, stamp(200)),
    }
    app = DashboardApp(lambda: world(activity))
    async with app.run_test(size=(100, 30)) as pilot:
        table = app.query_one(DataTable)
        await pilot.pause()
        # NEEDS YOU 0 / c0 1 / c1 2 / WORKING 3 / c2 4 / c3 5 / UNKNOWN 6 / c4 7
        assert app.row_keys[0] == "group:NEEDS YOU"
        assert table.cursor_row == 1  # the first render starts on a session, not a header
        visited = []
        for _ in range(5):
            await pilot.press("down")
            visited.append(table.cursor_row)
        assert visited == [2, 4, 5, 7, 7]
        visited = []
        for _ in range(4):
            await pilot.press("up")
            visited.append(table.cursor_row)
        assert visited == [5, 4, 2, 1]

        await pilot.press("up")  # nothing above the first session but the top bar
        assert isinstance(app.focused, TopBar)
        await pilot.press("down")
        assert app.focused is table and table.cursor_row == 1

        table.move_cursor(row=0, animate=False)  # programmatic moves cannot land on a header
        assert table.cursor_row == 1
        table.action_scroll_bottom()
        assert table.cursor_row == 7
        table.action_scroll_top()
        assert table.cursor_row == 1

        await pilot.click("#list", offset=(5, 4))  # the WORKING header, below the column line
        assert table.cursor_row == 4
        assert app.navigation.view == "overview"
        await pilot.press("enter")
        assert (app.navigation.view, app.navigation.session) == ("session", "codex:02")


# --- AC5 ---------------------------------------------------------------------


def test_columns_align_and_absent_values_are_dim_dashes():
    from booley.harness.dashboard import sessions

    bare = facts(45, branch="", work_dir="")
    full = facts(2, branch="feature/x", work_dir="/work/fifo", mode="Goal 1/5")
    overview = sessions.build([bare, full], [], NOW, 120)
    lines = plain_lines(overview)
    assert "·  ·" not in "".join(lines.values())

    header = overview.header.plain
    offsets = {label: header.index(label) for label in sessions.LABELS if label}
    expected = {
        "codex:45": {"Session": "S45", "Mode": "Interactive", "Branch": "—", "Work dir": "—"},
        "codex:02": {
            "Session": "S2",
            "Mode": "Goal 1/5",
            "Branch": "feature/x",
            "Work dir": "/work/fifo",
        },
    }
    for key, cells in expected.items():
        for label, text in cells.items():
            assert lines[key][offsets[label] :].startswith(text), (key, label)
        assert lines[key][offsets["Started"] :].startswith("5m ago")
        assert lines[key][offsets["Activity"] :].startswith("working")

    console = Console()
    row = dict(zip(overview.keys, overview.rows, strict=True))
    for label in ("Branch", "Work dir"):
        assert row["codex:45"].get_style_at_offset(console, offsets[label]).dim is True
        assert not row["codex:02"].get_style_at_offset(console, offsets[label]).dim


# --- AC6 ---------------------------------------------------------------------


def test_narrowing_drops_start_age_then_work_dir_then_branch():
    from booley.harness.dashboard import sessions

    wide = facts(
        1,
        branch="feature/long-branch",
        work_dir="/work/projects/fifo",
        mode="Goal 1/5",
        activity=Activity(WORKING, stamp(190)),
    )

    def labels(width: int) -> list[str]:
        header = sessions.build([wide], [], NOW, width).header.plain
        return [label for label in sessions.LABELS if label and label in header]

    # Natural widths 7+8+19+19+7+14 plus five gaps of two make 84 cells. Squeezing
    # shrinks Branch and Work dir to 8 and 10 first; below 64 the start age goes,
    # below 55 the work dir, below 43 the branch.
    everything = ["Session", "Mode", "Branch", "Work dir", "Started", "Activity"]
    assert labels(84) == everything
    assert labels(63) == [name for name in everything if name != "Started"]
    assert labels(54) == ["Session", "Mode", "Branch", "Activity"]
    assert labels(42) == ["Session", "Mode", "Activity"]
    for width in (84, 63, 54, 42):
        for line in sessions.build([wide], [], NOW, width).rows:
            assert line.cell_len <= width


def test_long_values_end_in_ellipsis_and_paths_keep_their_tail():
    from booley.harness.dashboard import sessions

    assert sessions.cut("/home/user/work/fifo", 11, keep_tail=True) == "…/work/fifo"
    assert sessions.cut("feature/very-long-branch", 12) == "feature/ver…"
    assert sessions.cut("short", 12) == "short"

    long_row = facts(
        1,
        branch="feature/very-long-branch-name-here",
        work_dir="/home/user/projects/work/fifo",
    )
    # 72 cells leave 5 spare cells after the minimum widths: Branch gets 13, Work dir stays 10.
    line = sessions.build([long_row], [], NOW, 72).rows[1].plain
    assert "feature/very…" in line
    assert "…work/fifo" in line and "projects" not in line

    # At the narrowest width the Activity itself is cut, still ending in an ellipsis.
    busy = facts(
        1,
        jobs=(
            "▶ J-SIM3 sim fifo_top 1m 12s",
            "▶ J-SIM4 sim fifo_top 1m 12s",
            "▶ J-LINT1 lint fifo_top 1m",
        ),
    )
    narrow = sessions.build([busy], [], NOW, 30)
    assert narrow.rows[1].cell_len <= 30
    assert narrow.rows[1].plain.endswith("…")


# --- AC7 ---------------------------------------------------------------------


def test_session_row_keeps_jobs_goal_summary_proposals_and_warnings():
    from booley.harness.dashboard import sessions

    row = SessionRow(Attribution("codex:00", "thread", "/work/a", "wt"), STARTED, STARTED)
    twin = SessionRow(Attribution("codex:01", "thread", "/work/b", "wt"), STARTED, STARTED)
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key="wt"),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
        status=SimpleNamespace(met=1, goals=(0,) * 5, pending_proposals=1),
        diagnostic="WARNING: first problem\nWARNING: second problem",
    )
    job = JobView(
        Path("/jobs"),
        JobRecord("one", "sim", STARTED, 60, argv=["--target", "fifo"], session_key="codex:00"),
        "running",
        handle="J-SIM1",
    )
    app = DashboardApp(lambda: None)
    app.snapshot = replace(
        world({"codex:00": Activity(WAITING_USER, stamp(100))}, jobs=(job,)),
        sessions=(row, twin),
        goals=(goal,),
        session_handles={"codex:00": "S1", "codex:01": "S2"},
    )
    overview = sessions.build([app._session_facts(row)], [], NOW, 400)
    line = plain_lines(overview)["codex:00"]
    assert "\n" not in line
    for expected in (
        "Goal 1/5",
        "waiting 1m 40s",
        "1 proposal",
        "▶ J-SIM1 sim fifo",
        "WARNING shared worktree",
        "WARNING: first problem · WARNING: second problem",
    ):
        assert expected in line, expected


# --- AC8 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standalone_running_jobs_group_and_enter_opens_detail():
    current = {"snapshot": replace(world({}, count=1), jobs=JobSnapshot((standalone_job(),)))}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(120, 30)) as pilot:
        table = app.query_one(DataTable)
        await pilot.pause()
        assert app.row_keys[:3] == ["group:UNKNOWN", "codex:00", "group:STANDALONE JOBS"]
        job_key = app.row_keys[3]
        cells = dict(zip(app.row_keys, (row[0].plain for row in app._overview()[2]), strict=True))
        assert cells["group:STANDALONE JOBS"] == "STANDALONE JOBS 1"
        assert cells[job_key].split() == ["J-LINT?", "standalone", "lint", "lint_top", "5m", "00s"]

        table.move_cursor(row=3, animate=False)
        await pilot.press("enter")
        assert app.window == "sessions"
        assert app.navigation.view == "job"
        assert "Run ID: three" in str(app.query_one("#detail", Static).render())
        await pilot.press("escape")
        assert app.navigation.view == "overview"

        # The group exists only while the Job runs.
        done = standalone_job(status="done", state="completed")
        current["snapshot"] = replace(world({}, count=1), jobs=JobSnapshot((done,)))
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys == ["group:UNKNOWN", "codex:00"]


# --- AC9 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_tab_shows_needs_you_count():
    needing = {
        "codex:00": Activity(WAITING_USER, stamp(30)),
        "codex:01": Activity(WORKING, stamp(30)),
    }
    current = {"snapshot": world(needing, count=3)}
    # Session 2 has a pending proposal, which needs the human as much as waiting does.
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key="wt1"),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
        status=SimpleNamespace(met=0, goals=(0,), pending_proposals=1),
    )
    current["snapshot"] = replace(current["snapshot"], goals=(goal,))
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(TopBar)
        assert bar.render().plain.split("│")[0].strip() == "Sessions 2◆  Jobs"
        await pilot.press("right")  # the count follows the user into the Jobs window
        assert app.window == "jobs"
        assert "Sessions 2◆" in bar.render().plain

        current["snapshot"] = world({}, count=3)
        await app.refresh_snapshot()
        await pilot.pause()
        assert bar.render().plain.split("│")[0].strip() == "Sessions  Jobs"


# --- AC10 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_state_text_and_standalone_group_still_shows():
    current = {"snapshot": replace(world({}, count=0), jobs=JobSnapshot((standalone_job(),)))}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert str(app.query_one("#empty", Static).render()) == NO_SESSIONS
        assert app.row_keys[0] == "group:STANDALONE JOBS"

        current["snapshot"] = world({}, count=1)
        await app.refresh_snapshot()
        await pilot.pause()
        assert str(app.query_one("#empty", Static).render()) == ""


# --- Fit follows the list's laid-out width ------------------------------------


@pytest.mark.asyncio
async def test_returning_from_the_jobs_window_refits_the_overview_to_the_full_width():
    activity = {f"codex:{n:02}": Activity(WORKING, stamp(100 + n)) for n in range(5)}
    app = DashboardApp(lambda: world(activity))
    async with app.run_test(size=(130, 20)) as pilot:
        await pilot.pause()
        assert "Started" in app._overview_header.plain
        app._show_window("jobs")
        await pilot.pause()
        app._show_window("sessions")
        await pilot.pause()
        await pilot.pause()
        assert "Started" in app._overview_header.plain


@pytest.mark.asyncio
async def test_moved_selection_stays_in_view_when_textual_does_not_scroll_it(monkeypatch):
    # Windows runs lost the race between Textual's deferred scroll-into-view and the
    # restored offset; take Textual's scroll away so the order cannot matter.
    working = {f"codex:{n:02}": Activity(WORKING, stamp(1000 - n)) for n in range(25)}
    current = {"snapshot": world(working, count=25)}
    app = DashboardApp(lambda: current["snapshot"])
    async with app.run_test(size=(100, 14)) as pilot:
        table = app.query_one(DataTable)
        await pilot.pause()
        table.move_cursor(row=app.row_keys.index("codex:24"), animate=False)
        await pilot.pause()
        monkeypatch.setattr(type(table), "_scroll_cursor_into_view", lambda *a, **k: None)

        current["snapshot"] = world({**working, "codex:24": Activity(WAITING_USER, stamp(10))}, 25)
        await app.refresh_snapshot()
        await pilot.pause()
        await pilot.pause()

        assert app.row_keys[table.cursor_row] == "codex:24"
        visible = table.scrollable_content_region.height - table.header_height
        assert table.scroll_y <= table.cursor_row < table.scroll_y + visible

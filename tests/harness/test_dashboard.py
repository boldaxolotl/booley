"""A few small Textual pilots and pure navigation/health model checks."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static

from booley.criteria.presentation import CriterionPresentation
from booley.harness import upgrade_review
from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp, Navigation
from booley.mcp.session_registry import Attribution, RecentCall, SessionRow
from booley.runtime.job_records import JobRecord
from booley.runtime.job_snapshot import JobSnapshot, JobView

STAMP = "2026-10-08T10:00:00Z"


def snapshot(count=12):
    rows = tuple(
        SessionRow(
            Attribution(f"codex:{number:02}", "thread", f"/fixture/{number}", f"wt{number}"),
            STAMP,
            STAMP,
            (RecentCall(STAMP, "sim", "completed"),),
        )
        for number in range(count)
    )
    jobs = (
        JobView(
            Path("/jobs"), JobRecord("one", "sim", STAMP, 60, session_key="codex:00"), "running"
        ),
        JobView(Path("/jobs"), JobRecord("two", "lint", STAMP, 60, status="done"), "completed"),
    )
    return model.DashboardSnapshot(rows, jobs=JobSnapshot(jobs), observed_at=1791454200)


@pytest.mark.asyncio
async def test_pilot_sessions_beyond_nine_scope_back_help_and_disconnect(monkeypatch):
    current = snapshot()
    proposal = SimpleNamespace(
        proposal=SimpleNamespace(
            id="change",
            to_json=lambda: {"kind": "relax", "threshold": 10, "reason": "[/untrusted]"},
        ),
        state="pending",
        closed_by_abandonment=False,
    )
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key="wt0"),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
        status=SimpleNamespace(
            met=1, goals=(1, 2, 3, 4), pending_proposals=1, proposals=(proposal,)
        ),
        goals=tuple(
            model.GoalDetail(
                str(index),
                state,
                CriterionPresentation(
                    "Lint top", "Target top", "must pass", "", "Target top · must pass"
                ),
                {"recorded_at": STAMP, "detail": {"findings": 1}},
            )
            for index, state in enumerate(("failing", "needs recheck", "not yet run", "passing"))
        ),
    )
    current = replace(
        current,
        goals=(goal,),
        sessions=(
            replace(current.sessions[0], calls=(RecentCall(STAMP, "[/untrusted]", "completed"),)),
            *current.sessions[1:],
        ),
    )
    app = DashboardApp(lambda: current)
    original_interval = app.set_interval

    def paused_interval(*args, **kwargs):
        timer = original_interval(*args, **kwargs)
        timer.pause()
        return timer

    # Periodic refresh can keep the pilot's idle wait busy under CI load.
    # This navigation scenario drives snapshot refresh explicitly below.
    monkeypatch.setattr(app, "set_interval", paused_interval)
    async with app.run_test(size=(90, 30)) as pilot:
        from rich.text import Text
        from textual.coordinate import Coordinate

        assert isinstance(app.query_one(DataTable).get_cell_at(Coordinate(0, 0)), Text)
        await pilot.press("1")
        assert (app.navigation.view, app.navigation.session) == ("session", "codex:00")
        await _inspect_goals_and_proposal(app, pilot)
        await pilot.press("j")
        assert app.navigation.session == "codex:00"
        assert len(app.row_keys) == 1
        await pilot.press("enter")
        assert app.navigation.view == "job"
        await pilot.press("escape", "escape", "r")
        assert app.navigation.job_filter == "recent"
        assert app.row_keys == []
        await pilot.press("s")
        assert app.navigation.session is None
        assert len(app.row_keys) == 1
        await pilot.press("?", "escape")
        assert app.navigation.view == "jobs"
        app.navigation = Navigation()
        app.render_snapshot()
        app.query_one(DataTable).move_cursor(row=11)
        await pilot.press("enter")
        assert app.navigation.session == "codex:11"
        current = replace(current, sessions=())
        await app.refresh_snapshot()
        assert "disconnected" in str(app.query_one("#orientation", Static).render())


async def _inspect_goals_and_proposal(app, pilot):
    assert app.row_keys[:4] == ["0", "1", "2", "3"]
    await pilot.press("enter")
    assert app.navigation.view == "goal"
    assert "must pass" in str(app.query_one("#detail", Static).render())
    await pilot.press("escape")
    app.query_one(DataTable).move_cursor(row=4, animate=False)
    await pilot.press("enter")
    assert app.navigation.view == "proposal"
    assert "Approval remains in chat" in str(app.query_one("#detail", Static).render())
    await pilot.press("escape")
    await pilot.resize_terminal(180, 30)
    await pilot.pause()
    assert app.goal_columns == 3
    assert app._selected_key() == "proposal:change"
    await pilot.resize_terminal(90, 30)
    await pilot.pause()
    assert app.goal_columns == 1


@pytest.mark.asyncio
async def test_pilot_wide_aside_narrow_detail_and_terminal_selection():
    current = snapshot(1)
    extra = tuple(
        JobView(
            Path("/jobs"),
            JobRecord(str(index), "lint", STAMP, 60, session_key="codex:00"),
            "running",
        )
        for index in range(45)
    )
    current = replace(current, jobs=JobSnapshot((*current.jobs.jobs, *extra)))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.press("j")
        assert app.query_one("#aside").display
        table = app.query_one(DataTable)
        table.move_cursor(row=35, animate=False)
        await pilot.pause()
        selected, scroll = app.row_keys[table.cursor_row], table.scroll_y
        assert scroll > 0
        current = replace(
            current,
            jobs=JobSnapshot(
                tuple(
                    replace(job, state="completed") if json.dumps(job.key) == selected else job
                    for job in current.jobs.jobs
                )
            ),
        )
        await app.refresh_snapshot()
        await pilot.pause()
        # A selected completion remains pinned in Running, including its scroll.
        assert app.row_keys[table.cursor_row] == selected
        assert table.scroll_y == scroll
        await pilot.resize_terminal(80, 25)
        await pilot.pause()
        assert not app.query_one("#aside").display
        await pilot.press("enter")
        assert app.navigation.view == "job"
        assert "completed" in str(app.query_one("#detail", Static).render())
        current = replace(current, sessions=())
        await app.refresh_snapshot()
        assert "disconnected" in str(app.query_one("#detail", Static).render())


def test_reported_activity_is_shown_per_session_and_unknown_is_never_guessed():
    from booley.harness.dashboard.app import activity_label
    from booley.runtime.session_activity import (
        WAITING_TOOL,
        WAITING_USER,
        WORKING,
        Activity,
    )

    now = 1791454200  # 10:10:00Z, ten minutes after STAMP.
    assert activity_label(Activity(), now) == "activity unknown"
    assert activity_label(Activity(WORKING, "2026-10-08T10:09:15Z"), now) == "working · 45s"
    assert (
        activity_label(Activity(WAITING_TOOL, STAMP, "mcp__booley__Sim"), now)
        == "waiting for tool call (mcp__booley__Sim) · 10m"
    )
    assert activity_label(Activity(WAITING_TOOL, STAMP), now) == "waiting for tool call · 10m"
    assert (
        activity_label(Activity(WAITING_USER, "2026-10-08T07:05:00Z"), now)
        == "waiting for user input · 3h 5m"
    )
    assert activity_label(Activity(WORKING, "not a time"), now) == "working · —"

    app = DashboardApp(snapshot)
    app.snapshot = replace(
        snapshot(3),
        activity={
            "codex:00": Activity(WAITING_TOOL, STAMP, "mcp__booley__Sim"),
            "codex:01": Activity(WAITING_USER, STAMP),
        },
    )
    app._assign_shortcuts()
    rows = [str(row) for row in app._overview()[2]]
    assert "waiting for tool call (mcp__booley__Sim) · 10m" in rows[0]
    assert "waiting for user input · 10m" in rows[1]
    assert "activity unknown" in rows[2]
    app.navigation = Navigation("session", "codex:00")
    assert "\nWaiting for tool call (mcp__booley__Sim) · 10m" in app._session()[0]
    app.navigation = Navigation("session", "codex:02")
    assert "\nActivity unknown" in app._session()[0]


def test_stable_shortcuts_and_unknown_activity_without_client_signals():
    app = DashboardApp(snapshot)
    app.snapshot = snapshot()
    app._assign_shortcuts()
    numbers = dict(app.shortcuts)
    app.snapshot = replace(app.snapshot, sessions=app.snapshot.sessions[1:])
    app._assign_shortcuts()
    assert app.shortcuts["codex:01"] == numbers["codex:01"]
    assert all("activity unknown" in str(row) for row in app._overview()[2])
    assert all("Last" not in str(row) for row in app._overview()[2])


def test_responsive_goal_selection_uses_the_displayed_column_count(monkeypatch):
    app = DashboardApp(snapshot)
    app.navigation = Navigation("session", "codex:00")
    app.row_keys = [str(number) for number in range(6)]
    app._table_columns = 2
    app.goal_columns = 3  # Incoming layout; the current cursor still belongs to two columns.
    monkeypatch.setattr(app, "query_one", lambda _: SimpleNamespace(cursor_row=1, cursor_column=1))
    assert app._selected_key() == "3"


@pytest.mark.parametrize(
    "report,due,expected",
    [
        ({"counts": {"fail": 0, "warn": 0}}, None, ()),
        ({"counts": {"fail": 0, "warn": 0}}, "expired", ("Doctor health stale: expired",)),
        (
            {"counts": {"fail": 1, "warn": 0}},
            "inputs changed",
            ("failure · stale: inputs changed",),
        ),
        (None, None, ("Doctor health unavailable: no readable automatic result",)),
    ],
)
def test_health_nonconsuming_clean_hidden_and_freshness(
    tmp_path, monkeypatch, report, due, expected
):
    monkeypatch.setattr(model.auto_doctor, "load_report", lambda _: report)
    monkeypatch.setattr(model.auto_doctor, "due_reason", lambda _: due)
    monkeypatch.setattr(model.auto_doctor, "current_summary", lambda _: "failure")
    monkeypatch.setattr(
        model.upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )
    monkeypatch.setattr(
        model.auto_doctor, "consume_changed_summary", lambda _: pytest.fail("health consumed")
    )
    monkeypatch.setattr(
        model.upgrade_review, "observe", lambda *_a, **_k: pytest.fail("upgrade state changed")
    )
    assert model.health_snapshot(tmp_path, tmp_path) == expected
    assert list(tmp_path.iterdir()) == []


def test_readonly_upgrade_pending_and_corrupt_states(tmp_path):
    path = upgrade_review.state_path(tmp_path)
    path.parent.mkdir()
    path.write_text(
        '{"schema":1,"reviewed_through":"1.0.0","pending_target":"1.1.0","first_seen_at":"2026-10-08T10:00:00Z"}'
    )
    before = path.read_bytes()
    assert (
        upgrade_review.read_status(tmp_path, running_version="1.1.0").condition
        == upgrade_review.ReviewCondition.PENDING
    )
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    path.write_bytes(b"broken")
    assert upgrade_review.read_status(tmp_path).condition == upgrade_review.ReviewCondition.CORRUPT


@pytest.mark.parametrize(
    "running,condition,target",
    [
        ("1.2.0", "pending", "1.2.0"),
        ("0.9.0", "stale-runtime", None),
        ("development", "unsupported-version", None),
    ],
)
def test_read_status_projects_unobserved_running_version_without_writing(
    tmp_path, running, condition, target
):
    import json

    from booley.harness import upgrade_review

    path = upgrade_review.state_path(tmp_path)
    path.parent.mkdir()
    original = b'{"schema":1,"reviewed_through":"1.0.0"}\n'
    path.write_bytes(original)
    status = upgrade_review.read_status(tmp_path, running_version=running)
    assert status.condition.value == condition
    assert status.pending_target == target
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == ["upgrade_review.json"]
    assert json.loads(original)["reviewed_through"] == "1.0.0"


def test_thread_without_process_proof_is_last_seen_and_own_fallback_is_not_other():
    row = SessionRow(Attribution("codex:one", "thread", "/work", "wt"), STAMP, STAMP)
    fallback = SessionRow(Attribution("worktree:wt", "worktree", "/work", "wt"), STAMP, STAMP)
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key="wt"),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
    )
    app = DashboardApp(lambda: None)
    app.snapshot = model.DashboardSnapshot(
        sessions=(row, fallback), goals=(goal,), observed_at=1791453900
    )
    app.navigation = Navigation("session", "codex:one")
    orientation, _ = app._session()
    assert "connected" not in orientation
    assert "last seen" in orientation
    assert "WARNING" not in orientation
    overview = app._overview_row(row)
    assert "goal/fixture" in overview
    assert "WARNING" not in overview


@pytest.mark.asyncio
async def test_slow_refresh_eventually_renders_completed_read_without_restarting(monkeypatch):
    import asyncio
    import threading

    from booley.harness.dashboard import app as app_module

    release = threading.Event()
    calls, messages, rendered = [], [], []
    expected = snapshot(1)

    def reader():
        calls.append(1)
        assert release.wait(2)
        return expected

    monkeypatch.setattr(app_module, "READ_BUDGET", 0.001, raising=False)
    app = DashboardApp(reader)
    monkeypatch.setattr(app, "query_one", lambda *_: SimpleNamespace(update=messages.append))
    monkeypatch.setattr(app, "render_snapshot", lambda: rendered.append(app.snapshot))
    try:
        await app.refresh_snapshot()
        assert "read still in progress" in str(messages[-1])
        release.set()
        await asyncio.wait_for(app._read_task, 2)
        await app.refresh_snapshot()
    finally:
        release.set()
    assert calls == [1]
    assert rendered == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [KeyError("bad row"), TypeError("bad row"), AttributeError("bad row")]
)
async def test_refresh_failure_is_stale_and_next_read_recovers(monkeypatch, failure):
    messages, rendered = [], []

    def broken():
        raise failure

    app = DashboardApp(broken)
    monkeypatch.setattr(app, "query_one", lambda *_: SimpleNamespace(update=messages.append))
    monkeypatch.setattr(app, "render_snapshot", lambda: rendered.append(app.snapshot))
    await app.refresh_snapshot()
    assert "unavailable/stale" in str(messages[-1]) and "bad row" in str(messages[-1])
    expected = snapshot(1)
    app.reader = lambda: expected
    await app.refresh_snapshot()
    assert rendered == [expected]


@pytest.mark.asyncio
async def test_published_paths_open_in_editor_on_click_and_report_failures(tmp_path):
    report = str(tmp_path / "report.json")
    log = str(tmp_path / "sim.log")
    job = JobView(
        Path("/jobs"),
        JobRecord("one", "sim", STAMP, 60, status="done"),
        "completed",
        report_path=Path(report),
        artifacts=(log,),
    )
    current = replace(snapshot(1), jobs=JobSnapshot((job,)))
    opened, hint = [], [""]

    def opener(path):
        opened.append(path)
        return hint[0]

    app = DashboardApp(lambda: current, opener)
    async with app.run_test(size=(max(90, len(report) + 10), 40)) as pilot:
        await pilot.press("r", "enter")
        assert app.navigation.view == "job"
        detail = app.query_one("#detail", Static)
        lines = str(detail.render()).split("\n")
        await pilot.click("#detail", offset=(3, lines.index(report)))
        await pilot.click("#detail", offset=(3, lines.index(log)))
        # Text that is not a published path stays inert.
        await pilot.click("#detail", offset=(3, 0))
        assert opened == [report, log]
        hint[0] = "No supported editor command on PATH; copy the path instead"
        await pilot.click("#detail", offset=(3, lines.index(report)))
        await pilot.pause()
        assert [note.message for note in app._notifications] == [hint[0]]


def test_overview_summarizes_session_and_standalone_running_jobs():
    current = snapshot(1)
    standalone = JobView(
        Path("/jobs"),
        JobRecord("three", "lint", STAMP, 60, argv=["--target", "lint_top"]),
        "running",
    )
    app = DashboardApp(lambda: current)
    app.snapshot = replace(current, jobs=JobSnapshot((*current.jobs.jobs, standalone)))
    elapsed = f"{1791454200 - 1791453600}s"
    orientation, _, rows = app._overview()
    assert f"Standalone running Jobs: lint lint_top {elapsed}" in orientation
    assert f"sim Target — {elapsed}" in str(rows[0])
    app.snapshot = current
    assert "Standalone" not in app._overview()[0]


def test_recent_results_are_newest_first_and_bounded(monkeypatch):
    from booley.harness.dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "RECENT_JOBS", 2)
    jobs = tuple(
        JobView(
            Path("/jobs"),
            JobRecord(
                str(hour), "sim", STAMP, 60, status="done", ended_at=f"2026-10-08T1{hour}:00:00Z"
            ),
            "completed",
        )
        for hour in (1, 3, 2)
    )
    app = DashboardApp(snapshot)
    app.snapshot = replace(snapshot(1), jobs=JobSnapshot(jobs))
    app.navigation = Navigation("jobs", None, "recent")
    assert [job.record.run_id for job in app._jobs()] == ["3", "2"]
    assert "recent (newest 2)" in app._content()[0]
    # The selected result stays listed when newer results push it past the bound.
    app.navigation = Navigation("jobs", None, "recent", json.dumps(jobs[0].key))
    assert [job.record.run_id for job in app._jobs()] == ["3", "2", "1"]
    app.navigation = Navigation("jobs", None, "all")
    assert [job.record.run_id for job in app._jobs()] == ["3", "2", "1"]

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
async def test_pilot_sessions_beyond_nine_scope_back_help_and_disconnect():
    current = snapshot()
    proposal = SimpleNamespace(
        proposal=SimpleNamespace(id="change", to_json=lambda: {"kind": "relax", "threshold": 10}),
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
    current = replace(current, goals=(goal,))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 30)) as pilot:
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

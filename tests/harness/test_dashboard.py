"""A few small Textual pilots and pure navigation/health model checks."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Static

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.criteria.presentation import CriterionPresentation
from booley.goals.model import GoalFamily, GoalSpec
from booley.harness import upgrade_review
from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import model
from booley.harness.dashboard.app import DashboardApp, Navigation
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.mcp.session_registry import Attribution, RecentCall, SessionRow
from booley.runtime import health
from booley.runtime.job_records import JobRecord

STAMP = "2026-10-08T10:00:00Z"


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch):
    """Let a slow CI worker finish the first read before any key press (#2023).

    Under load the 1 s budget can lapse, so the app starts empty and stale
    and keyboard tests act on rows that are not there yet. Tests of the
    budget itself patch it again.
    """
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)


@pytest.mark.asyncio
async def test_terminal_naming_failure_keeps_selectable_evidence_and_artifacts(tmp_path):
    artifact = str(tmp_path / "evidence.txt")
    diagnostic = "Goal unavailable (unsupported record)"
    detail = model.GoalDetail(
        "opaque_key_sentinel",
        "passing",
        CriterionPresentation(diagnostic, "", "", "", ""),
        {"recorded_at": STAMP, "detail": {"warnings": 0}},
        artifacts=(artifact,),
    )
    record = SimpleNamespace(
        worktree=SimpleNamespace(key="wt0"),
        state=SimpleNamespace(value="finished"),
        session_key="codex:00",
        branch="goal/fixture",
    )
    goal = model.GoalView(
        "fixture",
        record=record,
        goals=(detail,),
        diagnostic=diagnostic,
        terminal_summary="Completed work remains visible.",
    )
    current = replace(snapshot(1), goals=(goal,))
    opened = []
    app = DashboardApp(lambda: current, lambda path: opened.append(path) or "")
    app.snapshot = current
    app.navigation = Navigation("session", "codex:00")
    async with app.run_test(size=(max(90, len(artifact) + 10), 40)) as pilot:
        app.render_snapshot()
        assert "opaque_key_sentinel" in app.row_keys
        assert diagnostic in str(app.query_one("#orientation", Static).render())
        table = app.query_one(DataTable)
        table.move_cursor(row=0, column=0, animate=False)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        assert app.navigation.view == "goal"
        text = str(app.query_one("#detail", Static).render())
        assert diagnostic in text and "warnings" in text
        assert "opaque_key_sentinel" not in text
        await pilot.click("#detail", offset=(3, text.split("\n").index(artifact)))
        assert opened == [artifact]


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
    return model.DashboardSnapshot(
        rows,
        jobs=JobSnapshot(jobs),
        observed_at=1791454200,
        session_handles={row.attribution.key: f"S{index + 1}" for index, row in enumerate(rows)},
    )


@pytest.mark.asyncio
async def test_handles_name_every_dashboard_surface_and_full_keys_only_in_details():
    current = snapshot(12)
    jobs = tuple(
        replace(
            job,
            handle=f"J-{'SIM' if index == 0 else 'LINT'}{index + 1}",
            owner_handle="S1" if index == 0 else "",
        )
        for index, job in enumerate(current.jobs.jobs)
    )
    current = replace(current, jobs=JobSnapshot(jobs))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 35)) as pilot:
        assert "S12" in str(app._content()[2][-1])
        assert "J-SIM1" in str(app._content()[2][0])
        assert "codex:00" not in str(app._content()[2])
        await pilot.press("1")
        orientation, _, rows = app._content()
        assert orientation.startswith("S1 ·")
        assert "Session key: codex:00" in orientation
        assert "J-SIM1" in str(rows)
        await pilot.press("j")
        orientation, _, rows = app._content()
        # The Jobs window starts on all sessions, whichever session it was reached from.
        assert orientation.endswith(" · All sessions")
        assert "J-SIM1" in str(rows) and "S1" in str(rows)
        assert "codex:00" not in orientation + str(rows)
        await pilot.press("enter")
        detail = str(app.query_one("#detail", Static).render())
        assert "Owner: S1" in detail and "J-SIM1 · sim" in detail
        assert "Session key: codex:00" in detail and "Run ID: one" in detail
        await pilot.press("escape", "escape")
        assert app.navigation.view == "jobs"
        assert app.navigation.session is None


def test_alias_ownership_keeps_historical_jobs_in_session_scope_and_summaries():
    current = snapshot(1)
    old = "worktree:path:/fixture/0"
    job = replace(
        current.jobs.jobs[0],
        record=replace(current.jobs.jobs[0].record, session_key=old),
        handle="J-SIM1",
        owner_handle="S1",
    )
    current = replace(
        current, jobs=JobSnapshot((job,)), session_handles={"codex:00": "S1", old: "S1"}
    )
    app = DashboardApp(lambda: current)
    app.snapshot = current
    app.window = "jobs"
    app.navigation = Navigation("jobs", "codex:00")
    assert app._jobs() == [job]
    assert "J-SIM1" in app._overview_row(current.sessions[0])
    assert "S1" in str(app._content())
    assert old not in str(app._content())


def test_disconnected_session_without_cached_row_still_has_its_handle():
    current = replace(snapshot(0), session_handles={"codex:gone": "S17"})
    app = DashboardApp(lambda: current)
    app.snapshot = current
    app.navigation = Navigation("session", "codex:gone")
    orientation, goal = app._session()
    assert "S17" in orientation and "disconnected" in orientation
    assert goal is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shortcut", "job_filter", "expected"),
    [
        ("r", "finished", ["two"]),
        ("j", "running", ["one"]),
        ("a", "all", ["one", "two"]),
    ],
)
@pytest.mark.parametrize("scoped", [False, True])
async def test_jobs_shortcut_opens_jobs_window_and_s_returns_to_sessions(
    shortcut, job_filter, expected, scoped
):
    current = snapshot(2)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 30)) as pilot:
        if scoped:
            await pilot.press("1")
        await pilot.press(shortcut)
        assert app.navigation.view == "jobs"
        assert app.navigation.job_filter == job_filter
        assert app.navigation.session is None
        assert [json.loads(key)[1] for key in app.row_keys] == expected
        previous = app.navigation
        previous_rows = app.row_keys.copy()

        await pilot.press("s")
        assert app.navigation.view == "overview"
        assert app.navigation.session is None
        assert app.row_keys == [row.attribution.key for row in current.sessions]

        await pilot.press("enter")
        assert app.navigation.view == "session"
        assert app.navigation.session == current.sessions[0].attribution.key
        # Esc stays inside Sessions; the arrow returns to the Jobs window as it was.
        await pilot.press("escape", "escape")
        assert app.navigation.view == "overview"
        await pilot.press("right")
        assert app.navigation == previous
        assert app.row_keys == previous_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("shortcut", ["j", "r", "a"])
@pytest.mark.parametrize("via_help", [False, True])
async def test_all_job_owners_shortcut_preserves_filter(shortcut, via_help):
    current = snapshot(2)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.press("1", shortcut)
        job_filter = app.navigation.job_filter
        if via_help:
            await pilot.press("?")
        await pilot.press("u")
        assert app.navigation.view == "jobs"
        assert app.navigation.session is None
        assert app.navigation.job_filter == job_filter
        expected = {"j": ["one"], "r": ["two"], "a": ["one", "two"]}
        assert [json.loads(key)[1] for key in app.row_keys] == expected[shortcut]


@pytest.mark.asyncio
@pytest.mark.parametrize("shortcut", ["j", "r", "a"])
async def test_all_job_owners_shortcut_from_narrow_job_detail(shortcut):
    current = snapshot(2)
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.press(shortcut, "enter")
        assert app.navigation.view == "job"
        job_filter = app.navigation.job_filter
        await pilot.press("u")
        assert app.navigation.view == "jobs"
        assert app.navigation.session is None
        assert app.navigation.job_filter == job_filter
        expected = {"j": ["one"], "r": ["two"], "a": ["one", "two"]}
        assert [json.loads(key)[1] for key in app.row_keys] == expected[shortcut]


@pytest.mark.asyncio
async def test_session_arrows_help_back_and_quit_shortcuts():
    current = snapshot()
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(90, 30)) as pilot:
        for number in range(1, 10):
            await pilot.press(str(number))
            assert app.navigation.view == "session"
            assert app.navigation.session == f"codex:{number - 1:02}"
        await pilot.press("s", "down", "enter")
        assert app.navigation.session == "codex:01"
        await pilot.press("escape", "up", "enter")
        assert app.navigation.session == "codex:00"
        await pilot.press("j")
        previous = app.navigation
        previous_rows = app.row_keys.copy()
        await pilot.press("?")
        assert app.navigation.view == "help"
        assert "Navigation is read-only" in str(app.query_one("#detail", Static).render())
        await pilot.press("escape")
        assert app.navigation == previous
        assert app.row_keys == previous_rows
        await pilot.press("q")
        assert not app.is_running


@pytest.mark.asyncio
async def test_pilot_sessions_beyond_nine_scope_back_help_and_disconnect(monkeypatch):
    current = snapshot()
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
                "rationale": "[/untrusted]",
            },
        ),
        state="pending",
        decision=None,
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
        assert app.navigation.session is None
        assert len(app.row_keys) == 1
        await pilot.press("enter")
        assert app.navigation.view == "job"
        await pilot.press("escape", "escape", "r")
        assert app.navigation.job_filter == "finished"
        assert [json.loads(key)[1] for key in app.row_keys] == ["two"]
        await pilot.press("u")
        assert app.navigation.view == "jobs"
        assert app.navigation.session is None
        assert len(app.row_keys) == 1
        await pilot.press("?", "escape")
        assert app.navigation.view == "jobs"
        await pilot.press("s")
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
    assert app._selected_key() == "proposal:change"
    await pilot.resize_terminal(90, 30)
    await pilot.pause()
    assert app._selected_key() == "proposal:change"


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_fails", [False, True])
async def test_refresh_tick_after_shutdown_is_a_no_op(reader_fails):
    current = snapshot(1)
    failing = False

    def reader():
        if failing:
            raise OSError("registry unreadable")
        return current

    app = DashboardApp(reader)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()

    # The interval may fire once more after the widgets are gone.
    failing = reader_fails
    await app.refresh_snapshot()


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
        previous = app.navigation
        await pilot.press("enter")
        assert app.navigation == previous
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
        == "waiting for tool call (mcp__booley__Sim) · 10m 00s"
    )
    assert activity_label(Activity(WAITING_TOOL, STAMP), now) == "waiting for tool call · 10m 00s"
    assert (
        activity_label(Activity(WAITING_USER, "2026-10-08T07:05:00Z"), now)
        == "waiting for user input · 3h 05m"
    )
    assert activity_label(Activity(WORKING, "not a time"), now) == "working · unknown"

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
    assert "waiting for tool call (mcp__booley__Sim) · 10m 00s" in rows[0]
    assert "waiting for user input · 10m 00s" in rows[1]
    assert "activity unknown" in rows[2]
    app.navigation = Navigation("session", "codex:00")
    assert "\nWaiting for tool call (mcp__booley__Sim) · 10m 00s" in app._session()[0]
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


# (automatic Doctor report, due reason, expected Dashboard lines)
_HEALTH_ROWS = [
    ({"counts": {"fail": 0, "warn": 0}}, None, ("Doctor: OK",)),
    ({"counts": {"fail": 0, "warn": 0}}, "expired", ("Doctor: OK · recheck due",)),
    (
        {"counts": {"fail": 0, "warn": 1}},
        "inputs changed",
        ("Doctor: 1 WARN · recheck due",),
    ),
    (
        {"counts": {"fail": 1, "warn": 0}},
        "inputs changed",
        ("Doctor: 1 FAIL · recheck due",),
    ),
    (None, None, ("Doctor: unavailable: no readable automatic result",)),
]


def test_health_nonconsuming_compact_and_freshness(tmp_path, monkeypatch):
    for index, row in enumerate(_HEALTH_ROWS):
        root = tmp_path / f"row{index}"
        root.mkdir()
        # Rows share one test (the evidence helper needs function-level node IDs),
        # so name the failing row instead of leaving an anonymous loop assertion.
        try:
            _check_health_row(root, monkeypatch, *row)
        except AssertionError as error:
            error.add_note(f"failing row {index}: {row!r}")
            raise


def _check_health_row(root, monkeypatch, report, due, expected):
    findings = ()
    if report is not None and report["counts"]["fail"]:
        findings += (DiagnosticFinding(Severity.FAIL, "failure"),)
    if report is not None and report["counts"]["warn"]:
        findings += (DiagnosticFinding(Severity.WARN, "warning", check_id="test.warning"),)
    diagnostics = DiagnosticReport(findings)
    snapshot = (
        health.HealthSnapshot(diagnostics, "fixture-hash", root / "last.log", due)
        if report is not None
        else None
    )
    reads = []

    def read(_):
        reads.append(True)
        return snapshot

    monkeypatch.setattr(model.health, "read_health", read)
    monkeypatch.setattr(
        model.upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(upgrade_review.ReviewCondition.CURRENT, "1", "path"),
    )
    monkeypatch.setattr(
        model.health, "consume_changed_snapshot", lambda _: pytest.fail("health consumed")
    )
    monkeypatch.setattr(
        model.upgrade_review, "observe", lambda *_a, **_k: pytest.fail("upgrade state changed")
    )
    lines = model.health_lines(root, root)
    assert tuple(line.text for line in lines) == expected
    # The due marker travels as data so the renderer never sniffs the text.
    assert lines[0].due_suffix == (" · recheck due" if due else "")
    assert reads == [True]
    assert list(root.iterdir()) == []


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


def test_session_without_work_dir_shows_its_default_checkout_and_branch():
    """A session that never passed work_dir runs in its server cwd; show it, not a blank."""
    bare = SessionRow(
        Attribution("pid:one", "process", "", "", branch="booley", default_work_dir="/work"),
        STAMP,
        STAMP,
    )
    pinned = SessionRow(
        Attribution(
            "pid:two", "process", "/work/wt", "wt", branch="experiment", default_work_dir="/work"
        ),
        STAMP,
        STAMP,
    )
    unknown = SessionRow(Attribution("pid:three", "process", "", ""), STAMP, STAMP)
    # A Goal's branch belongs to the Goal worktree, never to the default checkout.
    goal = model.GoalView(
        "fixture",
        record=SimpleNamespace(
            worktree=SimpleNamespace(key=""),
            state=SimpleNamespace(value="active"),
            branch="goal/fixture",
        ),
    )
    app = DashboardApp(lambda: None)
    app.snapshot = model.DashboardSnapshot(
        sessions=(bare, pinned, unknown), goals=(goal,), observed_at=1791453900
    )
    overview = app._overview_row(bare)
    assert " · /work (default) · booley · " in overview
    assert "goal/fixture" not in overview
    # An explicit work_dir association always wins over the default.
    pinned_row = app._overview_row(pinned)
    assert " · /work/wt · experiment · " in pinned_row and "(default)" not in pinned_row
    # Rows from servers that predate default_work_dir keep the explicit placeholder.
    assert "(default)" not in app._overview_row(unknown)
    app.navigation = Navigation("session", "pid:one")
    orientation, _ = app._session()
    assert " · /work (default) · booley · " in orientation

    # A detached default checkout has no branch to show.
    app.snapshot = replace(
        app.snapshot,
        sessions=(SessionRow(replace(bare.attribution, branch=""), STAMP, STAMP),),
        goals=(),
    )
    assert " · /work (default) · branch — · " in app._overview_row(app.snapshot.sessions[0])


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
    async with app.run_test(size=(max(90, 2 * max(len(report), len(log)) + 40), 40)) as pilot:
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        assert app.navigation.view == ("job" if app.size.width < 120 else "jobs")
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
    elapsed = "10m 00s"
    orientation, _, rows = app._overview()
    assert f"Standalone running Jobs: J-LINT? · lint lint_top {elapsed}" in orientation
    assert f"sim Target — {elapsed}" in str(rows[0])
    app.snapshot = current
    assert "Standalone" not in app._overview()[0]


def test_finished_jobs_are_newest_first_and_bounded(monkeypatch):
    monkeypatch.setattr(dashboard_app, "FINISHED_JOBS", 2)
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
    app.window = "jobs"
    app.navigation = Navigation("jobs", None, "finished")
    assert [job.record.run_id for job in app._jobs()] == ["3", "2"]
    assert "Finished (newest 2)" in app._content()[0]
    # The selected Job stays listed when newer Jobs push it past the bound.
    app.navigation = Navigation("jobs", None, "finished", json.dumps(jobs[0].key))
    assert [job.record.run_id for job in app._jobs()] == ["3", "2", "1"]
    app.navigation = Navigation("jobs", None, "all")
    assert [job.record.run_id for job in app._jobs()] == ["3", "2", "1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [90, 140])
@pytest.mark.parametrize("state", ["completed", "orphaned"])
async def test_missing_report_resource_line_is_dim(tmp_path, width, state):
    from rich.text import Text

    record = JobRecord("fast", "sim", STAMP, 60, status="done", exit_code=0)
    job = JobView(tmp_path, record, state)
    current = replace(snapshot(1), jobs=JobSnapshot((job,)))
    app = DashboardApp(lambda: current)
    app.snapshot = current
    app.window = "jobs"
    app.navigation = Navigation("job", selected=json.dumps(job.key))
    async with app.run_test(size=(width, 40)):
        app.render_snapshot()
        rendered = app.query_one("#detail", Static).content
        assert isinstance(rendered, Text)
        start = rendered.plain.index("resources not recorded (no report)")
        assert any(span.start == start and str(span.style) == "dim" for span in rendered.spans)
        # Report-controlled matching words must never acquire the generated style.
        old = replace(job, report={"report_text": "resources not recorded (no report)"})
        app.snapshot = replace(current, jobs=JobSnapshot((old,)))
        app._render_detail()
        rendered = app.query_one("#detail", Static).content
        assert "resources not recorded (no report)" in rendered.plain
        start = rendered.plain.index("resources not recorded (no report)")
        assert not any(
            span.start <= start < span.end and str(span.style) == "dim" for span in rendered.spans
        )


@pytest.mark.asyncio
async def test_time_display_pilot_snapshots(monkeypatch):
    from booley.harness.dashboard.app import job_detail
    from booley.runtime.session_activity import WORKING, Activity

    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
    current = snapshot(1)
    completed = replace(
        current.jobs.jobs[0],
        state="completed",
        record=replace(current.jobs.jobs[0].record, ended_at="2026-10-08T10:01:01Z"),
    )
    detail = job_detail(completed, now=current.observed_at)
    assert "Start: 14:00:00\nEnd: 14:01:01" in detail, "dashboard time migration missing"
    assert "elapsed 1m 01s" in detail
    _check_time_edge_fields(current, completed)
    current = replace(
        current, jobs=JobSnapshot((completed,)), activity={"codex:00": Activity(WORKING, STAMP)}
    )
    app = DashboardApp(lambda: current)
    app.snapshot = current
    app.navigation = Navigation("session", "codex:00")
    app._assign_shortcuts()
    orientation, _ = app._session()
    assert "Started 10m ago" in orientation
    assert "· 10m ago" in orientation
    assert "working · 10m 00s" in orientation.lower()
    assert "14:00:00" in str(app._session_activity(None))
    for size in ((140, 40), (80, 25)):
        await _capture_time_pilot(monkeypatch, current, completed, orientation, size)


def _check_time_edge_fields(current, completed):
    from booley.harness.dashboard.app import _evidence_detail, job_detail

    invalid = replace(
        completed, record=replace(completed.record, started_at="bad", ended_at="bad")
    )
    assert "Start: unknown\nEnd: unknown" in job_detail(invalid, now=current.observed_at)
    future = replace(current.sessions[0], last_call_at="2026-10-09T10:00:00Z")
    future_app = DashboardApp(lambda: current)
    future_app.snapshot = replace(current, sessions=(future,))
    future_app.navigation = Navigation("session", "codex:00")
    assert "· just now" in future_app._session()[0]
    evidence = model.GoalDetail(
        "clock",
        "passing",
        CriterionPresentation("Lint top", "", "", "", ""),
        {"recorded_at": STAMP},
    )
    assert "Last check: 14:00:00" in _evidence_detail(evidence, now=current.observed_at)


async def _capture_time_pilot(monkeypatch, current, completed, orientation, size):
    import os

    app = DashboardApp(lambda: current)
    original_interval = app.set_interval

    def paused_interval(*args, **kwargs):
        timer = original_interval(*args, **kwargs)
        timer.pause()
        return timer

    monkeypatch.setattr(app, "set_interval", paused_interval)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.navigation = Navigation("session", "codex:00")
        app.render_snapshot()
        await pilot.pause()
        output = os.environ.get("BOOLEY_TIMEFMT_PILOT_DIR")
        if output:
            root = Path(output)
            root.mkdir(parents=True, exist_ok=True)
            (root / f"session-{size[0]}x{size[1]}.svg").write_text(app.export_screenshot())
        app.navigation = Navigation("job", "codex:00", selected=json.dumps(completed.key))
        app.render_snapshot()
        await pilot.pause()
        shown = str(app.query_one("#detail", Static).render())
        assert "Start: 14:00:00\nEnd: 14:01:01" in shown
        if output:
            (root / f"dashboard-{size[0]}x{size[1]}.svg").write_text(app.export_screenshot())
            (root / f"dashboard-{size[0]}x{size[1]}.txt").write_text(orientation + "\n" + shown)


def test_absent_time_fields_keep_unavailable_markers():
    from booley.harness.dashboard.app import _evidence_detail, _job_cells, _job_summary, job_detail

    current = snapshot(1)
    missing = replace(current.jobs.jobs[0], state="completed", report=None)
    assert _job_cells(missing, current.observed_at, current.session_handles)[6].plain == (
        "unavailable"
    ), "absent time marker changed"
    assert "unavailable" in _job_summary(missing, current.observed_at), (
        "absent time marker changed"
    )
    assert "elapsed unavailable" in job_detail(missing, now=current.observed_at)
    evidence = model.GoalDetail(
        "clock", "passing", CriterionPresentation("Lint top", "", "", "", ""), {}
    )
    assert "Last check: unavailable" in _evidence_detail(evidence, now=current.observed_at), (
        "absent time marker changed"
    )
    invalid = replace(evidence, evidence={"recorded_at": "bad"})
    assert "Last check: unknown" in _evidence_detail(invalid, now=current.observed_at)


def _header_health(monkeypatch, tmp_path, fail=2, warn=1, due=None, missing=False, upgrade=False):
    findings = tuple(
        DiagnosticFinding(Severity.FAIL, "sentinel finding [path] " * 20) for _ in range(fail)
    )
    findings += tuple(
        DiagnosticFinding(Severity.WARN, "sentinel warning", check_id="test.warning")
        for _ in range(warn)
    )
    findings += (
        DiagnosticFinding(Severity.PASS, "passed"),
        DiagnosticFinding(Severity.WAIVED, "waived", check_id="test.waived"),
    )
    observed = (
        None
        if missing
        else health.HealthSnapshot(
            DiagnosticReport(findings), "hash", tmp_path / "sentinel[transcript].log", due
        )
    )
    monkeypatch.setattr(health, "read_health", lambda _: observed)
    condition = (
        upgrade_review.ReviewCondition.PENDING
        if upgrade
        else upgrade_review.ReviewCondition.CURRENT
    )
    monkeypatch.setattr(
        upgrade_review,
        "read_status",
        lambda _: upgrade_review.ReviewStatus(condition, "1", "path", pending_target="2"),
    )
    return model.health_lines(tmp_path, tmp_path)


def _rich_payload(widget):
    from rich.text import Text

    assert isinstance(widget.content, Text), "header must mount styled Rich Text"
    return widget.content


def _style_at(text, offset=0):
    from rich.console import Console

    return text.get_style_at_offset(Console(), offset)


@pytest.mark.asyncio
async def test_pilot_header_semantic_styles(monkeypatch, tmp_path):
    for due in (None, "expired reason"):
        try:
            await _check_header_semantic_styles(monkeypatch, tmp_path, due)
        except AssertionError as error:
            error.add_note(f"failing row: due={due!r}")
            raise


async def _check_header_semantic_styles(monkeypatch, tmp_path, due):
    from rich.style import Style

    from booley.harness import colors
    from booley.harness.dashboard.resources import Resources

    current = model.DashboardSnapshot(
        health=_header_health(monkeypatch, tmp_path, due=due, upgrade=True),
        diagnostics=("diagnostic [literal/path]",),
        resources=Resources(12.5, 1024**3, 2 * 1024**3, 3 * 1024**3, 4),
    )
    app = DashboardApp(lambda: current)
    monkeypatch.setattr(app, "_content", lambda: ("Scope [literal]\ndetail [path]", [], []))
    async with app.run_test(size=(160, 30)) as pilot:
        await pilot.pause()
        orientation = _rich_payload(app.query_one("#orientation", Static))
        assert orientation.plain == "Scope [literal]\ndetail [path]"
        assert _style_at(orientation) == Style.parse(f"bold color({colors.PALETTE_DODGER_BLUE})")
        assert _style_at(orientation, orientation.plain.index("detail")) == Style()
        doctor = _rich_payload(app.query_one("#health", Static))
        assert doctor.plain.splitlines()[0] == "Doctor: 2 FAIL · 1 WARN" + (
            " · recheck due" if due else ""
        )
        assert _style_at(doctor) == Style.parse("bold red")
        if due:
            # #2241: the due suffix keeps the attention color through the wrapper.
            suffix = doctor.plain.index(" · recheck due")
            assert _style_at(doctor, suffix) == Style.parse(f"color({colors.PALETTE_ORANGE})")
        notices = _rich_payload(app.query_one("#notices", Static))
        assert "Upgrade review" in notices.plain
        assert _style_at(notices, notices.plain.index("Upgrade")) == Style()
        assert _style_at(notices, notices.plain.index("diagnostic")) == Style()
        widget = app.query_one("#resources", Static)
        resources = _rich_payload(widget)
        assert (
            resources.plain
            == "Sandbox CPU 12.5% of 4 CPUs · Memory 1.0 GiB / 2.0 GiB · Free disk 3.0 GiB"
        )
        chrome = Style.parse(f"color({colors.PALETTE_MAUVE})")
        for label in ("Sandbox CPU", "Memory", "Free disk"):
            assert _style_at(resources, resources.plain.index(label)) == chrome
        segments = list(widget.render_line(0))
        painted = [(char, segment.style) for segment in segments for char in segment.text]
        assert painted[0][1].color.get_truecolor() == chrome.color.get_truecolor()
        assert (
            painted[resources.plain.index("12.5")][1].color.get_truecolor()
            == app.screen.rich_style.color.get_truecolor()
        )
        print(
            "Header painted resource label/value colors:",
            painted[0][1],
            painted[resources.plain.index("12.5")][1],
        )


@pytest.mark.asyncio
async def test_pilot_stale_notice_uses_attention_style(monkeypatch, tmp_path):
    from rich.style import Style

    from booley.harness import colors

    current = model.DashboardSnapshot(health=_header_health(monkeypatch, tmp_path))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)):
        app._show_stale("read failed [path]")
        content = _rich_payload(app.query_one("#notices", Static))
        assert content.plain == "Dashboard data unavailable/stale: read failed [path]"
        assert _style_at(content) == Style.parse(f"color({colors.PALETTE_ORANGE})")
        # The Doctor line stays visible beside a stale notice.
        doctor = _rich_payload(app.query_one("#health", Static))
        assert doctor.plain == "Doctor: 2 FAIL · 1 WARN"
        assert _style_at(doctor) == Style.parse("bold red")
        app.render_snapshot()
        assert not app.query_one("#notices", Static).display


# (fail, warn, due reason, missing snapshot, expected text, count style)
_DOCTOR_LINE_ROWS = [
    (2, 1, None, False, "Doctor: 2 FAIL · 1 WARN", "bold red"),
    (2, 0, None, False, "Doctor: 2 FAIL", "bold red"),
    (0, 1, None, False, "Doctor: 1 WARN", "yellow"),
    (0, 0, None, False, "Doctor: OK", "dim"),
    (2, 1, "expired reason", False, "Doctor: 2 FAIL · 1 WARN · recheck due", "bold red"),
    (0, 0, None, True, "Doctor: unavailable: no readable automatic result", "dim"),
    (0, 0, "expired reason", False, "Doctor: OK · recheck due", "dim"),
    (0, 1, "expired reason", False, "Doctor: 1 WARN · recheck due", "yellow"),
]


@pytest.mark.asyncio
async def test_pilot_compact_doctor_line(monkeypatch, tmp_path):
    for row in _DOCTOR_LINE_ROWS:
        try:
            await _check_pilot_doctor_line(monkeypatch, tmp_path, *row)
        except AssertionError as error:
            error.add_note(f"failing row: {row!r}")
            raise


async def _check_pilot_doctor_line(
    monkeypatch, tmp_path, fail, warn, due, missing, expected, style
):
    from rich.style import Style

    from booley.harness import colors

    current = model.DashboardSnapshot(
        health=_header_health(monkeypatch, tmp_path, fail, warn, due, missing)
    )
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(160, 30)):
        widget = app.query_one("#health", Static)
        assert str(widget.content) == expected, "Doctor header must contain compact counts only"
        content = _rich_payload(widget)
        assert len(content.plain.splitlines()) == 1
        assert "sentinel" not in content.plain
        assert "expired reason" not in content.plain
        assert _style_at(content) == Style.parse(style)
        last = len(content.plain) - 1
        if not due or missing:
            # Not due or unavailable: one style end to end, no attention span.
            assert _style_at(content, last) == Style.parse(style)
            return
        suffix = " · recheck due"
        attention = Style.parse(f"color({colors.PALETTE_ORANGE})")
        start = content.plain.index(suffix)
        assert content.plain.endswith(suffix)
        # Counts keep their severity style; only the suffix takes the attention color.
        assert _style_at(content, start - 1) == Style.parse(style)
        assert _style_at(content, start) == attention
        assert _style_at(content, last) == attention
        # The widget actually paints the suffix in orange, not just the Text object.
        painted = [
            (char, segment.style) for segment in widget.render_line(0) for char in segment.text
        ]
        assert painted[start][1].color.get_truecolor() == attention.color.get_truecolor()
        assert painted[start - 1][1].color.get_truecolor() != attention.color.get_truecolor()


def test_goal_statuses_use_shared_semantic_styles():
    from rich.style import Style

    from booley.harness import colors
    from booley.harness.dashboard import app as app_module

    styles = getattr(app_module, "SEMANTIC_STYLE", {})
    assert styles, "Dashboard needs a shared semantic style map"
    expected = {
        "passing": "green",
        "failing": "bold red",
        "needs recheck": f"color({colors.PALETTE_ORANGE})",
        "not yet run": "dim",
        "checking": f"color({colors.PALETTE_DODGER_BLUE})",
    }
    for state, style in expected.items():
        if state == "checking":
            continue
        goal = model.GoalDetail(
            "fixture",
            state,
            CriterionPresentation("Label [path]", "", "", "", "detail"),
            checking=True,
        )
        text = app_module._goal_text(goal)
        assert text.plain == f"checking · {state} · Label [path] · detail"
        assert goal.state == state
        assert _style_at(text) == Style.parse(expected["checking"])
        assert _style_at(text, len("checking · ")) == Style.parse(style)
        assert _style_at(text, text.plain.index("Label")) == Style.parse(expected["checking"])
    for alias in ("pass", "met", "passing"):
        assert styles[alias] == "green"
    assert styles["ok"] == "dim"
    for aliases in (
        ("fail", "failing"),
        ("running", "checking"),
        ("attention", "needs recheck"),
        ("unknown", "not run", "not yet run", "unavailable", "ok"),
    ):
        assert len({styles[alias] for alias in aliases}) == 1
    assert styles["warn"] == "yellow"


def test_dashboard_palette_imports_have_no_copied_codes():
    import ast

    from booley.harness import colors
    from booley.harness.dashboard import app as app_module

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "booley.harness.colors"
        for alias in node.names
    }
    assert {"PALETTE_MAUVE", "PALETTE_ORANGE", "PALETTE_DODGER_BLUE"} <= imported, (
        "Dashboard must import the palette owner constants"
    )
    codes = {
        colors.PALETTE_MAUVE,
        colors.PALETTE_ORANGE,
        colors.PALETTE_DODGER_BLUE,
        colors.PALETTE_MINT,
        colors.PALETTE_LAVENDER,
    }
    for path in Path(app_module.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant):
                assert node.value not in codes if isinstance(node.value, int) else True
                if isinstance(node.value, str):
                    assert not any(f"color({code})" in node.value for code in codes)


def _compact_result_job(report, code=0):
    return JobView(
        Path("/jobs"),
        JobRecord("compact", "sim", STAMP, 60, status="done", exit_code=code, ended_at=STAMP),
        "completed",
        report=report,
        report_path=Path("/reports/one.json") if report else None,
        artifacts=("/reports/sim.log",) if report else (),
    )


async def _result_pilot(report, expected, code=0, compact=False):
    from textual.containers import VerticalScroll

    from tests.harness.test_dashboard_results import campaign

    if report == "passing":
        report = {"flow": "sim", "detail": {"campaigns": {"top": campaign()}}}
    item = _compact_result_job(report, code)
    current = replace(snapshot(1), jobs=JobSnapshot((item,)))
    app = DashboardApp(lambda: current)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("r")
        await pilot.pause()
        widget = app.query_one("#detail", Static)
        text = str(widget.content)
        assert "Flow result:" in text, "Pilot must show the new Flow result in the selected pane"
        assert expected in text
        if compact:
            aside = app.query_one("#aside", VerticalScroll)
            assert aside.virtual_size.height <= aside.size.height
            assert text.splitlines()[0] == "Flow result: PASSED · 2/2 tests passed · grade pass"
            assert len(text.splitlines()[0]) <= widget.size.width
            content = widget.content
            links = [
                span for span in content.spans if getattr(span.style, "meta", {}).get("@click")
            ]
            assert content.plain[links[-1].start : links[-1].end] == str(Path("/reports/one.json"))
            assert (
                widget.region.y + text.splitlines().index(str(Path("/reports/one.json")))
                < aside.region.bottom
            )
        if report is None:
            assert text.count("Summary unavailable") == 1
            assert "Execution:" in text


@pytest.mark.asyncio
async def test_pilot_compact_passing_result():
    await _result_pilot("passing", "Flow result: PASSED", compact=True)


@pytest.mark.asyncio
async def test_pilot_failing_result_names():
    from tests.harness.test_dashboard_results import campaign

    await _result_pilot(
        {"flow": "sim", "detail": {"campaigns": {"top": campaign(1, 1)}}}, "test_bad", 1
    )


@pytest.mark.asyncio
async def test_pilot_no_report_result():
    await _result_pilot(None, "Summary unavailable")

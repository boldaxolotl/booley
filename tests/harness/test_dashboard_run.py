"""The Dashboard's Run wizard (#3437): worktree, Flow, arguments, review, submit.

Pilots drive the real wizard over a real Git repository and the real argument
check (a child process running the shell's own parse); only the detached worker
spawn and, for selection tests, the Job reads are replaced.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.widgets import DataTable, Input, OptionList, Static

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.app import DashboardApp, Navigation
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.harness.dashboard.targets import TargetsReader
from booley.harness.dashboard.topbar import TopBar
from booley.harness.standalone_job import standalone_jobs_dir, standalone_request_path
from booley.mcp.registry import discover_mcp_tools
from booley.runtime import detached_job, job_records
from tests.harness.test_dashboard import STAMP, choose_in_bar, snapshot
from tests.harness.test_dashboard_targets import git, init_repository, session, settle, world

if TYPE_CHECKING:
    from booley.harness.dashboard.wizard import RunWizard

# Read from discovery, not from the Run code, which a Dashboard without Run lacks: its import
# belongs in each test body, so a missing module fails there and not at collection.
FLOW_NAMES = sorted(info.name for info in discover_mcp_tools() if info.kind == "flow")
ARGUMENTS = "--target t --report-dir 'out dir'"


@pytest.fixture(autouse=True)
def _quiet_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generous first read and no timer reads: the tests order every read themselves."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)
    monkeypatch.setattr(dashboard_app, "REFRESH_SECONDS", 3600)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git repository with one commit: the main checkout the wizard preselects."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    main = tmp_path / "main"
    init_repository(main)
    return main


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the detached worker commands instead of starting workers."""
    commands: list[list[str]] = []
    monkeypatch.setattr(
        detached_job, "spawn", lambda command, *_args, **_kw: commands.append(command)
    )
    return commands


def real_launcher(booley_dir: Path):
    from booley.harness.dashboard.launch import Launcher

    return Launcher(booley_dir)


class FakeLauncher:
    """A launcher whose check always passes and whose submit answers a fixed Job key."""

    def __init__(self, key: str = "", error: OSError | None = None) -> None:
        self.key, self.error = key, error
        self.submitted: list[tuple[Path, str, tuple[str, ...]]] = []

    def flows(self):
        from booley.harness.dashboard.launch import builtin_flows

        return builtin_flows()

    def check(self, worktree, flow, argv):
        return ""

    def submit(self, worktree, flow, argv):
        if self.error is not None:
            raise self.error
        self.submitted.append((worktree, flow, argv))
        return self.key


def make_app(checkout: Path, launcher, current=None, **kwargs) -> DashboardApp:
    current = current or world()
    return DashboardApp(
        lambda: current, targets=TargetsReader(checkout), launcher=launcher, **kwargs
    )


async def choose_run(app: DashboardApp, pilot) -> None:
    """Arrow to the top bar's Run item (not the Jobs window's "Running" filter) and apply it."""
    if not isinstance(app.focused, TopBar):
        await pilot.press("up")
    bar = app.query_one(TopBar)
    labels = [label for label, _, _ in bar._items()]
    step = labels.index("Run") - bar.cursor
    await pilot.press(*(["right" if step > 0 else "left"] * abs(step)), "enter")
    await pilot.pause()


async def start_run(app: DashboardApp, pilot) -> RunWizard:
    """Choose Run in the top bar and let the worktree read finish behind the wizard."""
    from booley.harness.dashboard.wizard import RunWizard

    await choose_run(app, pilot)
    await settle(app, pilot)
    assert isinstance(app.screen, RunWizard)
    return app.screen


def shown(wizard: RunWizard, widget_id: str) -> str:
    return str(wizard.query_one(widget_id, Static).render())


async def settle_check(app: DashboardApp, pilot) -> None:
    """Wait for the argument check's child process and for the wizard to apply its result."""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def to_form(app: DashboardApp, pilot, flow: str) -> RunWizard:
    """Run, accept the preselected worktree, pick *flow*."""
    wizard = await start_run(app, pilot)
    await pilot.press("enter")
    await pilot.press(*["down"] * FLOW_NAMES.index(flow), "enter")
    assert wizard.query_one("#run-arguments", Input).display
    return wizard


async def check_arguments(app: DashboardApp, pilot, wizard: RunWizard, text: str) -> None:
    wizard.query_one("#run-arguments", Input).value = text
    await pilot.press("enter")
    await settle_check(app, pilot)


def row_key(path: Path, run_id: str) -> str:
    return json.dumps([str(path), run_id])


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", FLOW_NAMES)
async def test_every_builtin_flow_launches_through_the_wizard(checkout, spawned, tmp_path, flow):
    booley_dir = tmp_path / "booley"
    app = make_app(checkout, real_launcher(booley_dir))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, flow)
        await check_arguments(app, pilot, wizard, ARGUMENTS)
        command = shown(wizard, "#run-command")
        assert command == f"cd {checkout} && booley flow {flow} --target t --report-dir 'out dir'"
        await pilot.press("enter")  # Submit is highlighted
        await pilot.pause()
        assert app.window == "jobs"
        jobs_dir, run_id = json.loads(app.navigation.selected)
        assert Path(jobs_dir) == standalone_jobs_dir(booley_dir)
    record = job_records.read_record(run_id, standalone_jobs_dir(booley_dir))
    assert record is not None
    assert record.argv == ["booley", "flow", flow, "--target", "t", "--report-dir", "out dir"]
    # The shown command and the submitted Job are the same command.
    assert shlex.split(command) == ["cd", record.work_dir, "&&", *record.argv]
    assert record.work_dir == str(checkout)
    request = json.loads(standalone_request_path(booley_dir, run_id).read_text(encoding="utf-8"))
    assert request == {
        "worktree": str(checkout),
        "kind": "flow",
        "name": flow,
        "argv": ["--target", "t", "--report-dir", "out dir"],
    }
    assert spawned == [
        [
            sys.executable,
            "-m",
            "booley.harness.standalone_worker",
            "standalone",
            str(booley_dir),
            run_id,
        ]
    ]


@pytest.mark.asyncio
async def test_flow_step_lists_every_builtin_flow_with_its_blurb(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await start_run(app, pilot)
        await pilot.press("enter")
        options = wizard.query_one("#run-flows", OptionList)
        lines = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        assert [line.split(" — ")[0] for line in lines] == ["fpga", "lint", "sim", "synth"]
        assert all(len(line.split(" — ", 1)[1]) > 10 for line in lines)
        assert lines[2].startswith("sim — Run RTL simulation")


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "text", "fragment"),
    [
        ("lint", "--bogus", "unrecognized arguments: --bogus"),
        ("lint", "--target 't", "unbalanced quotes"),
        ("lint", "--project /tmp", "the worktree chooses the Project"),
        ("lint", "--work-dir x", "the worktree chooses the Project"),
        ("lint", "--help", "not a run"),
        ("sim", "--tests-file nope.txt", "nope.txt"),
    ],
    ids=[
        "unknown-option",
        "unbalanced-quote",
        "project-selector",
        "work-dir-selector",
        "help",
        "missing-tests-file",
    ],
)
async def test_invalid_arguments_fail_in_the_form(
    checkout, spawned, tmp_path, flow, text, fragment
):
    booley_dir = tmp_path / "booley"
    app = make_app(checkout, real_launcher(booley_dir))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, flow)
        await check_arguments(app, pilot, wizard, text)
        assert fragment in shown(wizard, "#run-error")
        assert "usage:" not in shown(wizard, "#run-error")
        assert wizard.query_one("#run-arguments", Input).display
        assert not wizard.query_one("#run-command", Static).display
    assert spawned == []
    assert not booley_dir.exists()


@pytest.mark.asyncio
async def test_valid_arguments_after_a_dash_pass_like_the_shell(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "-- --target t")
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint -- --target t"


def test_check_resolves_relative_paths_in_the_worktree(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "tests.txt").write_bytes(b"reset\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert launch.check(worktree, "sim", ("--tests-file", "tests.txt")) == ""
    assert "tests.txt" in launch.check(elsewhere, "sim", ("--tests-file", "tests.txt"))


def test_check_changes_no_environment_and_writes_nothing(tmp_path, capfd):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def listing() -> list[str]:
        # coverage's subprocess patch may drop .coverage* files in the child's cwd.
        return sorted(p.name for p in worktree.rglob("*") if not p.name.startswith(".coverage"))

    environment = dict(os.environ)
    # --timeout-ms is a deprecated alias: an in-process parse would warn here and set the environment.
    assert launch.check(worktree, "lint", ("--timeout-ms", "1000")) == ""
    assert dict(os.environ) == environment
    assert listing() == []
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""


def test_check_reports_a_timeout_and_a_worktree_that_cannot_start_it(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    monkeypatch.setattr(launch, "CHECK_TIMEOUT_S", 0.001)
    assert launch.check(tmp_path, "lint", ()) == "argument check timed out after 0.001s"
    message = launch.check(tmp_path / "missing", "lint", ())
    assert "No such file or directory" in message or "cannot find" in message.lower()
    assert "\n" not in message


def test_child_main_rejects_selector_and_help(tmp_path, monkeypatch, capsys):
    from booley.harness.dashboard import launch

    monkeypatch.chdir(tmp_path)
    assert launch.main(["lint", "--", "--target", "t"]) == 0
    assert launch.main(["lint", "--", "--", "--target", "t"]) == 0
    for typed, message in (
        (["-C", "x"], "the worktree chooses the Project"),
        (["--project", "x"], "the worktree chooses the Project"),
        (["-h"], "--help shows help; it is not a run"),
        (["--bogus"], "unrecognized arguments: --bogus"),
    ):
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            launch.main(["lint", "--", *typed])
        assert exit_info.value.code == 2
        assert message in capsys.readouterr().err


@pytest.mark.asyncio
async def test_wizard_shows_why_worktrees_are_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    not_a_repository = tmp_path / "plain"
    not_a_repository.mkdir()
    app = make_app(not_a_repository, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await start_run(app, pilot)
        options = wizard.query_one("#run-worktrees", OptionList)
        assert "Worktrees unavailable" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")  # nothing to choose
        assert options.display


# --- AC3 ---------------------------------------------------------------------


def job_view(run_id: str, started: str, state: str = "running") -> JobView:
    status = "running" if state == "running" else "done"
    record = job_records.JobRecord(run_id, "sim", started, 60, status=status)
    return JobView(Path("/jobs"), record, state)


class Jobs:
    """Reads that show an existing Job always and the new one from its second read after submit."""

    def __init__(self) -> None:
        self.submitted = False
        self.reads_since_submit = 0
        self.new_state = "running"

    def read(self):
        jobs = [job_view("old", STAMP)]
        if self.submitted:
            self.reads_since_submit += 1
            if self.reads_since_submit >= 2:
                # Older than "old", so its row is not the first one.
                jobs.append(job_view("new", "2026-10-08T09:00:00Z", self.new_state))
        return replace(snapshot(0), sessions=(), session_handles={}, jobs=JobSnapshot(tuple(jobs)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_filter", "expected_filter"),
    [("running", "running"), ("finished", "running"), ("all", "all")],
)
async def test_submit_selects_the_new_job_once_its_record_appears(
    checkout, start_filter, expected_filter
):
    jobs = Jobs()
    key = row_key(Path("/jobs"), "new")
    launcher = FakeLauncher(key)
    original_submit = launcher.submit

    def submit(*args):
        jobs.submitted = True
        return original_submit(*args)

    launcher.submit = submit
    app = DashboardApp(jobs.read, targets=TargetsReader(checkout), launcher=launcher)
    app.windows["jobs"] = [Navigation("jobs", None, start_filter)]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("enter")  # Submit
        while jobs.reads_since_submit < 1:  # the refresh Run asks for
            await pilot.pause()
        await pilot.pause()
        assert app.window == "jobs"
        assert app.navigation.session is None
        assert app.navigation.job_filter == expected_filter
        table = app.query_one(DataTable)
        # Not listed yet: the cursor shows another row, the selection still names the new Job.
        assert key not in app.row_keys
        assert app._selected_key() == key
        assert app.focused is table
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys[table.cursor_row] == key
        assert table.cursor_row == 1
        # Finishing under Running (or All) keeps the Job listed and selected.
        jobs.new_state = "completed"
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.row_keys[table.cursor_row] == key


@pytest.mark.asyncio
async def test_enter_opens_nothing_while_the_new_job_is_not_listed(checkout):
    jobs = Jobs()
    key = row_key(Path("/jobs"), "new")
    app = DashboardApp(jobs.read, targets=TargetsReader(checkout), launcher=FakeLauncher(key))
    async with app.run_test(size=(90, 30)) as pilot:  # narrow: Enter opens a Job detail
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("enter")
        await pilot.pause()
        assert app._enter_target() is None
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "jobs"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["session-detail", "jobs-finished"])
async def test_esc_after_run_returns_to_where_the_wizard_started(checkout, origin):
    jobs = Jobs()
    key = row_key(Path("/jobs"), "new")
    current = replace(
        jobs.read(), sessions=(session("S1", checkout),), session_handles={"codex:01": "S1"}
    )
    app = DashboardApp(
        lambda: current, targets=TargetsReader(checkout), launcher=FakeLauncher(key)
    )
    if origin == "session-detail":
        app.windows["sessions"] = [Navigation(), Navigation("session", "codex:01")]
    else:
        app.window = "jobs"
        app.windows["jobs"] = [Navigation("jobs", None, "finished")]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("enter")
        await pilot.pause()
        assert (app.window, app.navigation.view) == ("jobs", "jobs")
        assert app.navigation.job_filter == "running"
        assert "esc back" in str(app.query_one("#footer", Static).render())  # Esc has a way back
        await pilot.press("escape")
        await pilot.pause()
        if origin == "session-detail":
            assert (app.window, app.navigation.view, app.navigation.session) == (
                "sessions",
                "session",
                "codex:01",
            )
        else:
            assert (app.window, app.navigation.job_filter) == ("jobs", "finished")
        assert isinstance(app.focused, DataTable)
        # The way back was used up: Esc at a top level does nothing more.
        await pilot.press("escape")
        await pilot.pause()
        assert app.window == ("sessions" if origin == "session-detail" else "jobs")


@pytest.mark.asyncio
async def test_a_filter_choice_after_run_forgets_the_way_back(checkout):
    key = row_key(Path("/jobs"), "new")
    app = make_app(checkout, FakeLauncher(key))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("enter")
        await pilot.pause()
        await choose_in_bar(app, pilot, "All")
        await pilot.press("down", "escape")
        await pilot.pause()
        assert (app.window, app.navigation.job_filter) == ("jobs", "all")


@pytest.mark.asyncio
async def test_esc_steps_back_and_closes_the_wizard(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "--target t")
        assert wizard.query_one("#run-command", Static).display
        before = {name: list(history) for name, history in app.windows.items()}
        stages = []
        for _ in range(3):
            await pilot.press("escape")
            stages.append(
                [
                    i
                    for i in ("run-actions", "run-arguments", "run-flows", "run-worktrees")
                    if wizard.query_one(f"#{i}").display
                ]
            )
        assert stages == [["run-arguments"], ["run-flows"], ["run-worktrees"]]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.windows == before
        assert isinstance(app.focused, TopBar)


@pytest.mark.asyncio
async def test_arguments_are_kept_for_the_same_flow_and_cleared_for_another(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        arguments = wizard.query_one("#run-arguments", Input)
        arguments.value = "--target t"
        await pilot.press("escape", "enter")  # back to the Flow list, lint again
        assert arguments.value == "--target t"
        await pilot.press("escape", "down", "enter")  # another Flow
        assert arguments.value == ""


@pytest.mark.asyncio
async def test_typed_arguments_reach_the_check_and_q_is_text(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await pilot.press("minus", "q", "enter")  # -q is a Flow option; the app's q quits
        await settle_check(app, pilot)
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint -q"


@pytest.mark.asyncio
async def test_review_copies_the_command_and_reports_a_submit_failure(checkout, monkeypatch):
    app = make_app(checkout, FakeLauncher(error=OSError("disk full")))
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "--target t")
        await pilot.press("down", "enter")  # Copy command
        assert copied == [f"cd {checkout} && booley flow lint --target t"]
        assert any("terminal to copy" in note.message for note in app._notifications)
        await pilot.press("up", "enter")  # Submit
        assert "Could not start the Job: disk full" in shown(wizard, "#run-error")
        assert type(app.screen).__name__ == "RunWizard"


class GatedLauncher(FakeLauncher):
    """A launcher whose check waits until the test lets it answer."""

    def __init__(self) -> None:
        super().__init__()
        self.started, self.release = threading.Event(), threading.Event()

    def check(self, worktree, flow, argv):
        self.started.set()
        assert self.release.wait(timeout=60)
        return ""


@pytest.mark.asyncio
async def test_stale_check_results_are_dropped(checkout):
    launcher = GatedLauncher()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        arguments = wizard.query_one("#run-arguments", Input)
        arguments.value = "--target t"
        await pilot.press("enter")
        assert launcher.started.wait(timeout=60)
        arguments.value = "--target other"  # edited while the check runs
        await pilot.pause()
        launcher.release.set()
        await settle_check(app, pilot)
        assert arguments.display
        assert not wizard.query_one("#run-command", Static).display


# --- the top bar and its footer -----------------------------------------------


@pytest.mark.asyncio
async def test_top_bar_footer_describes_run(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("up")
        footer = app.query_one("#footer", Static)
        assert "run a Flow" not in str(footer.render())
        labels = [label for label, _, _ in app.query_one(TopBar)._items()]
        await pilot.press(*["right"] * (labels.index("Run")))
        await pilot.pause()
        assert str(footer.render()) == "←→ choose · ⏎ run a Flow · ↓ back to list"
        await pilot.press("right")
        await pilot.pause()
        assert str(footer.render()) == "←→ choose · ⏎ apply · ↓ back to list"


@pytest.mark.asyncio
async def test_run_without_a_launcher_says_so(checkout):
    app = DashboardApp(world)
    async with app.run_test(size=(120, 40)) as pilot:
        await choose_run(app, pilot)
        assert len(app.screen_stack) == 1
        assert any(
            "Run unavailable: no Project root" in note.message for note in app._notifications
        )


class GatedWorktrees(TargetsReader):
    """A reader whose worktree read waits until the test lets it answer."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.release = threading.Event()

    def worktrees(self):
        assert self.release.wait(timeout=60)
        return super().worktrees()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["help", "targets"])
async def test_run_opened_from_help_and_targets_keeps_the_wizard_focused(checkout, origin):
    reader = GatedWorktrees(checkout)
    app = DashboardApp(world, targets=reader, launcher=FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        if origin == "help":
            reader.release.set()
            await choose_in_bar(app, pilot, "Help")
            await pilot.press("up")
        else:
            await pilot.press("right", "right")  # entering Targets starts a worktree read
        await choose_run(app, pilot)  # the wizard's own read replaces it
        wizard = app.screen
        assert type(wizard).__name__ == "RunWizard"
        reader.release.set()  # the reads land behind the wizard
        await settle(app, pilot)
        assert app.focused is wizard.query_one("#run-worktrees", OptionList)
        await pilot.press("enter")
        assert app.focused is wizard.query_one("#run-flows", OptionList)
        await pilot.press("escape", "escape")  # back to the worktrees, then close
        await pilot.pause()
        # Closing returns the keyboard to the top bar Run was chosen in, not to the list below.
        assert isinstance(app.focused, TopBar)


def test_error_text_falls_back_to_the_exit_code():
    from booley.harness.dashboard import launch

    assert launch._error_text("", 3) == "argument check failed (exit 3)"


# --- review fixes: preselection, Submit highlight, a crashed check ---------------


@pytest.mark.asyncio
async def test_main_checkout_is_preselected_when_linked_worktrees_exist(checkout):
    """The list is read after the wizard opens, and the main checkout sorts last in it."""
    git(checkout, "worktree", "add", "-q", "-b", "wt-a", str(checkout.parent / "wt-a"))
    launcher = FakeLauncher()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await start_run(app, pilot)
        worktrees = wizard.query_one("#run-worktrees", OptionList)
        assert worktrees.highlighted == worktrees.option_count - 1
        await pilot.press("enter", "enter")  # that worktree, then the first Flow
        await check_arguments(app, pilot, wizard, "")
        assert shown(wizard, "#run-command").startswith(f"cd {checkout} &&")


@pytest.mark.asyncio
async def test_stepping_back_to_the_form_and_on_again_highlights_submit(checkout):
    launcher = FakeLauncher()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("down", "escape")  # Copy highlighted, back to the form
        await check_arguments(app, pilot, wizard, "")
        assert wizard.query_one("#run-actions", OptionList).highlighted == 0


@pytest.mark.asyncio
async def test_a_crashed_check_is_reported_in_the_form(checkout):
    class Crashing(FakeLauncher):
        def check(self, worktree, flow, argv):
            raise ValueError("embedded null byte")

    app = make_app(checkout, Crashing())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "--target t")
        assert "Argument check failed: embedded null byte" in shown(wizard, "#run-error")
        assert wizard.query_one("#run-arguments", Input).display

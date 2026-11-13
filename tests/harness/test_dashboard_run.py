"""The Dashboard's Run wizard (#3437, #3580): worktree, Flow, form, review, submit.

Pilots drive the real wizard over a real Git repository, the form generated from
the real parsers and the real argument check (a child process running the
shell's own parse); only the detached worker spawn and, for selection tests, the
Job reads are replaced.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.widgets import DataTable, OptionList, Static
from textual.worker import WorkerFailed

from booley.core.booley_layout import BooleyLayoutError
from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.app import DashboardApp, Navigation
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.harness.dashboard.targets import TargetsReader
from booley.harness.dashboard.topbar import TopBar
from booley.harness.standalone_job import standalone_jobs_dir, standalone_request_path
from booley.harness.worktree_list import WorktreeListError
from booley.mcp.registry import discover_mcp_tools
from booley.runtime import detached_job, job_records
from booley.runtime.job_slots import SlotStore, slots_dir
from tests.harness.test_dashboard import STAMP, choose_in_bar, snapshot
from tests.harness.test_dashboard_targets import (
    FIFO_CORE,
    TESTS_TOML,
    git,
    init_repository,
    session,
    settle,
    world,
    write_files,
)

if TYPE_CHECKING:
    from booley.harness.dashboard.wizard import RunWizard

# Read from discovery, not from the Run code, which a Dashboard without Run lacks: its import
# belongs in each test body, so a missing module fails there and not at collection.
ARGUMENTS = "--target t --report-dir 'out dir'"  # typed into the Extra arguments row
FLOW_NAMES = sorted(info.name for info in discover_mcp_tools() if info.kind == "flow")


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


# What the Target and test pickers read: one Target per Flow, two sim Targets with their own Tests.
PICKER_CORE = (
    FIFO_CORE
    + """  fpga_fifo:
    default_tool: vivado
    filesets: [rtl, constraints]
    toplevel: fifo
  sim_wide:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb
"""
)
PICKER_TESTS = TESTS_TOML + '\n[sim_wide]\nselect = "+test={name}"\ntests = ["wide", "narrow"]\n'
PICKER_FILES = {
    "fifo.core": PICKER_CORE,
    "fifo.v": "module fifo; endmodule\n",
    "tb.sv": "module tb; endmodule\n",
    "fifo.sdc": "# constraints\n",
    ".booley/booley.toml": "[project]\n",
    ".booley/tests.toml": PICKER_TESTS,
}


def write_picker_project(root: Path) -> None:
    """Cores and a ``tests.toml`` in *root* for the Target and test pickers to read."""
    write_files(root, PICKER_FILES)


def real_launcher(booley_dir: Path):
    from booley.harness.dashboard.launch import Launcher

    return Launcher(booley_dir)


class FakeLauncher:
    """A launcher whose check always passes and whose submit answers a fixed Job key.

    Its form is the real one, built from the Flow's parser.

    ``facts`` (the arguments of ``RunFacts``) is what the check reports; ``outlook`` (``ahead``,
    ``cap``, ``queue_max``) and ``uncommitted`` are what the review reads, an exception instance
    being raised instead; ``outlook_reads`` counts the queue reads. Run-module types are built
    when used, so this file still imports without them.
    """

    def __init__(
        self,
        key: str = "",
        error: OSError | None = None,
        facts: tuple | None = None,
        outlook: tuple[int, int, int] | Exception | None = None,
        uncommitted: int | Exception = 0,
    ) -> None:
        self.key, self.error = key, error
        self.facts = facts or (None, False)
        self.outlook_result, self.uncommitted_result = outlook, uncommitted
        self.outlook_reads = 0
        self.submitted: list[tuple[Path, str, tuple[str, ...]]] = []

    def flows(self):
        from booley.harness.dashboard.launch import builtin_flows

        return builtin_flows()

    def form(self, flow):
        from booley.harness.dashboard import launch

        return launch.form(flow)

    def check(self, worktree, flow, argv):
        from booley.harness.dashboard.launch import CheckResult, RunFacts

        return CheckResult(facts=RunFacts(*self.facts))

    def outlook(self, job_class):
        from booley.runtime.job_slots import AdmissionOutlook

        self.outlook_reads += 1
        if isinstance(self.outlook_result, Exception):
            raise self.outlook_result
        assert self.outlook_result is not None
        return AdmissionOutlook(job_class, *self.outlook_result)

    def uncommitted(self, worktree):
        if isinstance(self.uncommitted_result, Exception):
            raise self.uncommitted_result
        return self.uncommitted_result

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


async def settle_form(app: DashboardApp, pilot) -> None:
    """Wait for the form's build worker and for the wizard to show its rows."""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def to_form(app: DashboardApp, pilot, flow: str) -> RunWizard:
    """Run, accept the preselected worktree, pick *flow* and wait for its form."""
    wizard = await start_run(app, pilot)
    await pilot.press("enter")
    await pilot.press(*["down"] * FLOW_NAMES.index(flow), "enter")
    await settle_form(app, pilot)
    assert wizard.query_one("#run-form", OptionList).display
    return wizard


def form_rows(wizard: RunWizard) -> list[str]:
    rows = wizard.query_one("#run-form", OptionList)
    return [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]


def row_value(wizard: RunWizard, label: str) -> str:
    """What the form row named *label* shows."""
    for prompt in form_rows(wizard):
        name, _, value = prompt.partition("  ")
        if name.strip() == label:
            return value.strip()
    raise AssertionError(f"no row {label!r} in {form_rows(wizard)}")


async def goto_row(pilot, wizard: RunWizard, label: str) -> None:
    """Arrow to the form row named *label*."""
    rows = wizard.query_one("#run-form", OptionList)
    names = [prompt.partition("  ")[0].strip() for prompt in form_rows(wizard)]
    assert rows.highlighted is not None
    step = names.index(label) - rows.highlighted
    await pilot.press(*(["down" if step > 0 else "up"] * abs(step)))


async def type_text(pilot, text: str) -> None:
    await pilot.press(*text)


async def set_toggle(pilot, wizard: RunWizard, label: str) -> None:
    await goto_row(pilot, wizard, label)
    await pilot.press("enter")


async def set_text(pilot, wizard: RunWizard, label: str, text: str) -> None:
    await goto_row(pilot, wizard, label)
    await pilot.press("enter")
    await type_text(pilot, text)
    await pilot.press("enter")


async def set_choice(pilot, wizard: RunWizard, label: str, choice: str) -> None:
    """Open the choice list of the row *label* and keep *choice*."""
    await goto_row(pilot, wizard, label)
    await pilot.press("enter")
    choices = wizard.query_one("#run-choices", OptionList)
    names = [str(choices.get_option_at_index(i).prompt) for i in range(choices.option_count)]
    assert choices.highlighted is not None
    step = names.index(choice) - choices.highlighted
    await pilot.press(*(["down" if step > 0 else "up"] * abs(step)), "enter")


def pick_rows(wizard: RunWizard) -> list[str]:
    """The rows of the open Target or test picker."""
    picks = wizard.query_one("#run-picks", OptionList)
    return [str(picks.get_option_at_index(i).prompt) for i in range(picks.option_count)]


async def press_pick(pilot, wizard: RunWizard, pick: str) -> None:
    """Arrow to the picker row for *pick* (a Target, or a Test Run behind its ``[x]`` box) and Enter."""
    names = [row.split()[-1] for row in pick_rows(wizard)]
    picks = wizard.query_one("#run-picks", OptionList)
    assert picks.highlighted is not None
    step = names.index(pick) - picks.highlighted
    await pilot.press(*(["down" if step > 0 else "up"] * abs(step)), "enter")


async def open_picker(app: DashboardApp, pilot, wizard: RunWizard, label: str) -> None:
    """Enter on the Target or test row *label* and let the read behind the picker land."""
    await goto_row(pilot, wizard, label)
    await pilot.press("enter")
    await settle(app, pilot)


async def set_target(app: DashboardApp, pilot, wizard: RunWizard, label: str, target: str) -> None:
    await open_picker(app, pilot, wizard, label)
    await press_pick(pilot, wizard, target)


async def set_tests(
    app: DashboardApp, pilot, wizard: RunWizard, label: str, runs: list[str]
) -> None:
    """Toggle each of *runs* in the test picker, then leave it with Esc."""
    await open_picker(app, pilot, wizard, label)
    for run in runs:
        await press_pick(pilot, wizard, run)
    await pilot.press("escape")


async def review_command(app: DashboardApp, pilot, wizard: RunWizard) -> None:
    """Enter on the Review row and wait for the argument check's answer."""
    await goto_row(pilot, wizard, "Review the command")
    await pilot.press("enter")
    await settle_check(app, pilot)


async def check_arguments(app: DashboardApp, pilot, wizard: RunWizard, text: str) -> None:
    """Type *text* into the extra arguments (when there is any), then review the command."""
    await settle_form(app, pilot)
    if text:
        await set_text(pilot, wizard, "Extra arguments", text)
    await review_command(app, pilot, wizard)


def row_key(path: Path, run_id: str) -> str:
    return json.dumps([str(path), run_id])


# --- AC1 ---------------------------------------------------------------------


@dataclass(frozen=True)
class FormScript:
    """What one Flow's AC1 pilot enters through the form, and the command it must produce.

    Every Flow has at least one field of each kind it offers; the Target picker is
    always set. ``steps`` are ``(kind, row label, value)``: kind ``toggle`` flips the row,
    ``text`` types the value, ``choice`` keeps that entry of the row's choice list,
    ``target`` chooses that Target, ``tests`` toggles the Test Runs named (comma list).
    """

    steps: tuple[tuple[str, str, str], ...]
    extra: str
    argv: tuple[str, ...]
    shown: str  # the argv as the shell would read it


FORM_SCRIPTS: dict[str, FormScript] = {
    "lint": FormScript(
        steps=(
            ("target", "--target", "lint_fifo"),
            ("toggle", "--dry-run", ""),
            ("text", "--scope", "a.sv"),
        ),
        extra="-q",
        argv=("--target", "lint_fifo", "--dry-run", "--scope", "a.sv", "-q"),
        shown="--target lint_fifo --dry-run --scope a.sv -q",
    ),
    "mutation": FormScript(
        steps=(
            ("target", "--target", "sim_fifo"),
            ("text", "--scope", "a.sv"),
            ("text", "--count", "3"),
        ),
        extra="--seed 7",
        argv=("--target", "sim_fifo", "--scope", "a.sv", "--count", "3", "--seed", "7"),
        shown="--target sim_fifo --scope a.sv --count 3 --seed 7",
    ),
    "sim": FormScript(
        steps=(
            ("target", "--target", "sim_fifo"),
            ("toggle", "--coverage", ""),
            ("choice", "--mode", "simulate"),
            ("tests", "--test", "reset,stress+soak"),
        ),
        extra="--result-verbosity full",
        argv=(
            "--target",
            "sim_fifo",
            "--coverage",
            "--mode",
            "simulate",
            "--test",
            "reset",
            "--test",
            "stress+soak",
            "--result-verbosity",
            "full",
        ),
        shown=(
            "--target sim_fifo --coverage --mode simulate --test reset --test stress+soak"
            " --result-verbosity full"
        ),
    ),
    "synth": FormScript(
        steps=(
            ("target", "--target", "synth_fifo"),
            ("toggle", "--no-cache", ""),
            ("choice", "--flatten/--no-flatten", "off"),
            ("choice", "--frontend", "slang"),
        ),
        extra="--repair-hold",
        argv=(
            "--target",
            "synth_fifo",
            "--no-flatten",
            "--frontend",
            "slang",
            "--no-cache",
            "--repair-hold",
        ),
        shown="--target synth_fifo --no-flatten --frontend slang --no-cache --repair-hold",
    ),
    "fpga": FormScript(
        steps=(
            ("target", "--target", "fpga_fifo"),
            ("toggle", "--no-cache", ""),
            ("choice", "--ppa-profile", "balanced"),
        ),
        extra="--report-dir 'out dir'",
        argv=(
            "--target",
            "fpga_fifo",
            "--no-cache",
            "--ppa-profile",
            "balanced",
            "--report-dir",
            "out dir",
        ),
        shown="--target fpga_fifo --no-cache --ppa-profile balanced --report-dir 'out dir'",
    ),
}


@pytest.mark.asyncio
async def test_every_builtin_flow_launches_through_the_form(checkout, spawned, tmp_path):
    for flow in FLOW_NAMES:
        spawned.clear()
        await launch_flow_through_the_form(checkout, spawned, tmp_path / flow, flow)


async def launch_flow_through_the_form(checkout, spawned, tmp_path, flow):
    """Fill the form for one Flow, submit it, and compare shown and submitted commands."""
    if flow not in FORM_SCRIPTS:
        pytest.fail(f"add a form script for the new built-in Flow {flow!r}")
    script = FORM_SCRIPTS[flow]
    write_picker_project(checkout)
    booley_dir = tmp_path / "booley"
    app = make_app(checkout, real_launcher(booley_dir))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, flow)
        for kind, label, value in script.steps:
            if kind == "toggle":
                await set_toggle(pilot, wizard, label)
            elif kind == "text":
                await set_text(pilot, wizard, label, value)
            elif kind == "target":
                await set_target(app, pilot, wizard, label, value)
            elif kind == "tests":
                await set_tests(app, pilot, wizard, label, value.split(","))
            else:
                await set_choice(pilot, wizard, label, value)
        await set_text(pilot, wizard, "Extra arguments", script.extra)
        await review_command(app, pilot, wizard)
        command = shown(wizard, "#run-command")
        assert command == f"cd {checkout} && booley flow {flow} {script.shown}"
        await pilot.press("enter")  # Submit is highlighted
        await pilot.pause()
        assert app.window == "jobs"
        jobs_dir, run_id = json.loads(app.navigation.selected)
        assert Path(jobs_dir) == standalone_jobs_dir(booley_dir)
    record = job_records.read_record(run_id, standalone_jobs_dir(booley_dir))
    assert record is not None
    assert record.argv == ["booley", "flow", flow, *script.argv]
    # The shown command and the submitted Job are the same command.
    assert shlex.split(command) == ["cd", record.work_dir, "&&", *record.argv]
    assert record.work_dir == str(checkout)
    request = json.loads(standalone_request_path(booley_dir, run_id).read_text(encoding="utf-8"))
    assert request == {
        "worktree": str(checkout),
        "kind": "flow",
        "name": flow,
        "argv": list(script.argv),
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
        assert [line.split(" — ")[0] for line in lines] == [
            "fpga",
            "lint",
            "mutation",
            "sim",
            "synth",
        ]
        assert all(len(line.split(" — ", 1)[1]) > 10 for line in lines)
        assert lines[3].startswith("sim — Run RTL simulation")


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
        assert wizard.query_one("#run-form", OptionList).display
        assert not wizard.query_one("#run-command", Static).display
    assert spawned == []
    assert not booley_dir.exists()


@pytest.mark.asyncio
async def test_a_leading_double_dash_in_extra_arguments_is_dropped_from_the_command(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "-- --target t")
        # Shown and submitted without it, as `booley flow` drops it.
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint --target t"


def test_check_resolves_relative_paths_in_the_worktree(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "tests.txt").write_bytes(b"reset\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert launch.check(worktree, "sim", ("--tests-file", "tests.txt")).error == ""
    assert "tests.txt" in launch.check(elsewhere, "sim", ("--tests-file", "tests.txt")).error


def test_check_changes_no_environment_and_writes_nothing(tmp_path, capfd):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def listing() -> list[str]:
        # coverage's subprocess patch may drop .coverage* files in the child's cwd.
        return sorted(p.name for p in worktree.rglob("*") if not p.name.startswith(".coverage"))

    environment = dict(os.environ)
    # --timeout-ms is a deprecated alias: an in-process parse would warn here and set the environment.
    assert launch.check(worktree, "lint", ("--timeout-ms", "1000")).error == ""
    assert dict(os.environ) == environment
    assert listing() == []
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""


def test_check_reports_a_timeout_and_a_worktree_that_cannot_start_it(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    monkeypatch.setattr(launch, "CHECK_TIMEOUT_S", 0.001)
    assert launch.check(tmp_path, "lint", ()).error == "argument check timed out after 0.001s"
    message = launch.check(tmp_path / "missing", "lint", ()).error
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
                    for i in ("run-actions", "run-form", "run-flows", "run-worktrees")
                    if wizard.query_one(f"#{i}").display
                ]
            )
        assert stages == [["run-form"], ["run-flows"], ["run-worktrees"]]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.windows == before
        assert isinstance(app.focused, TopBar)


@pytest.mark.asyncio
async def test_the_form_is_kept_for_the_same_pair_and_fresh_for_another_flow(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await set_text(pilot, wizard, "--scope", "a.sv")
        await set_toggle(pilot, wizard, "--dry-run")
        await set_text(pilot, wizard, "Extra arguments", "-q")
        await pilot.press("escape", "enter")  # back to the Flow list, lint again
        await settle_form(app, pilot)
        assert row_value(wizard, "--scope") == "a.sv"
        assert row_value(wizard, "--dry-run") == "on"
        assert row_value(wizard, "Extra arguments") == "-q"
        await pilot.press("escape", "down", "down", "enter")  # another Flow: sim, past mutation
        await settle_form(app, pilot)
        assert row_value(wizard, "--target") == "automatic"
        assert row_value(wizard, "Extra arguments") == ""
        assert "--coverage" in " ".join(form_rows(wizard))


@pytest.mark.asyncio
async def test_the_form_is_fresh_for_another_worktree(checkout):
    git(checkout, "worktree", "add", "-q", "-b", "wt-a", str(checkout.parent / "wt-a"))
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")  # the main checkout, listed last
        await set_text(pilot, wizard, "--scope", "a.sv")
        await pilot.press("escape", "escape", "up", "enter", "enter")  # the other worktree
        await settle_form(app, pilot)
        assert row_value(wizard, "--scope") == ""
        await pilot.press("escape", "escape", "down", "enter", "enter")  # and back: fresh again
        await settle_form(app, pilot)
        assert row_value(wizard, "--scope") == ""


class FormGate(FakeLauncher):
    """A launcher whose form waits until the test lets it be built."""

    def __init__(self, failures: tuple[Exception, ...] = ()) -> None:
        super().__init__()
        self.release = threading.Event()
        self.failures = list(failures)
        self.built: list[str] = []

    def form(self, flow):
        assert self.release.wait(timeout=60)
        self.built.append(flow)
        if self.failures:
            raise self.failures.pop(0)
        return super().form(flow)


@pytest.mark.asyncio
async def test_only_the_note_shows_while_the_form_builds(checkout):
    launcher = FormGate()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await start_run(app, pilot)
        await pilot.press("enter", "enter")  # the worktree, then the first Flow
        await pilot.pause()
        assert shown(wizard, "#run-note") == "Building the form…"
        assert not wizard.query_one("#run-form", OptionList).display
        launcher.release.set()
        await settle_form(app, pilot)
        assert not wizard.query_one("#run-note", Static).display
        assert wizard.query_one("#run-form", OptionList).display
        assert app.focused is wizard.query_one("#run-form", OptionList)
        assert wizard.query_one("#run-form", OptionList).highlighted == 0


@pytest.mark.asyncio
async def test_keys_do_nothing_while_the_next_form_builds_and_the_old_rows_are_gone(checkout):
    launcher = FormGate()
    launcher.release.set()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "synth")  # many rows: the old cursor can go far
        launcher.release.clear()
        await pilot.press("escape", "up", "enter")  # lint: building
        await pilot.pause()
        rows = wizard.query_one("#run-form", OptionList)
        assert rows.option_count == 0
        wizard._form_row_selected(9)  # a late Enter on a row of the old form
        await pilot.press(*["down"] * 9, "enter")
        assert wizard._stage == "form"
        assert shown(wizard, "#run-note") == "Building the form…"
        launcher.release.set()
        await settle_form(app, pilot)
        assert row_value(wizard, "--target") == "automatic"
        assert row_value(wizard, "Extra arguments") == ""
        assert "--flatten" not in " ".join(form_rows(wizard))


@pytest.mark.asyncio
async def test_a_result_of_a_replaced_build_is_dropped(checkout):
    from types import SimpleNamespace

    from textual.worker import WorkerState

    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        before = form_rows(wizard)
        other = SimpleNamespace(worker=object(), state=WorkerState.ERROR)
        wizard._form_built(other)  # an old build's message, still queued when the human moved on
        assert form_rows(wizard) == before
        assert not wizard.query_one("#run-note", Static).display


def build_failure(kind: str) -> Exception:
    from booley.harness.dashboard.run_form import FormError

    if kind == "form-error":
        return FormError("group 'Brand new' is not placed")
    return RuntimeError("boom")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "note"),
    [
        ("form-error", "The form cannot be built: group 'Brand new' is not placed"),
        ("crash", "The form cannot be built: RuntimeError: boom"),
    ],
)
async def test_a_form_that_cannot_be_built_says_why_and_still_takes_extra_arguments(
    checkout, kind, note
):
    launcher = FormGate((build_failure(kind),))
    launcher.release.set()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        assert shown(wizard, "#run-note") == note
        assert [row.split("  ")[0].strip() for row in form_rows(wizard)] == [
            "Extra arguments",
            "Review the command",
        ]
        await check_arguments(app, pilot, wizard, "--target t")
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint --target t"
        await pilot.press("enter")  # Submit
        await pilot.pause()
        assert launcher.submitted == [(checkout, "lint", ("--target", "t"))]


@pytest.mark.asyncio
async def test_a_form_that_failed_is_built_again_on_choosing_the_flow_again(checkout):
    launcher = FormGate((build_failure("crash"),))
    launcher.release.set()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        assert len(form_rows(wizard)) == 2
        await pilot.press("escape", "enter")
        await settle_form(app, pilot)
        assert launcher.built == ["lint", "lint"]
        assert len(form_rows(wizard)) > 2
        assert not wizard.query_one("#run-note", Static).display


@pytest.mark.asyncio
async def test_typed_arguments_reach_the_check_and_q_is_text(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "-q")  # -q is a Flow option; the app's q quits
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint -q"


@pytest.mark.asyncio
async def test_esc_discards_and_enter_keeps_a_choice_and_a_typed_value(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await goto_row(pilot, wizard, "--mode")
        await pilot.press("enter")
        choices = wizard.query_one("#run-choices", OptionList)
        assert [str(choices.get_option_at_index(i).prompt) for i in range(4)] == [
            "default",
            "simulate",
            "elab-only",
            "elab-only-standalone",
        ]
        assert choices.highlighted == 0
        assert "esc cancel" in shown(wizard, "#run-footer")
        assert "Execution mode" in shown(wizard, "#run-hint")
        await pilot.press("down", "down", "escape")  # discard
        assert wizard.query_one("#run-form", OptionList).display
        assert row_value(wizard, "--mode") == "default"
        await set_choice(pilot, wizard, "--mode", "elab-only")
        assert row_value(wizard, "--mode") == "elab-only"
        await pilot.press("enter")  # the list opens on the current value
        assert wizard.query_one("#run-choices", OptionList).highlighted == 2
        await pilot.press("escape")
        assert row_value(wizard, "--mode") == "elab-only"
        # The editor: typing, then Esc, keeps the old value; Enter keeps the new one.
        await goto_row(pilot, wizard, "--seed")
        await pilot.press("enter")
        assert "Seed for every selected Test Run" in shown(wizard, "#run-hint")
        assert "esc cancel" in shown(wizard, "#run-footer")
        await type_text(pilot, "7")
        await pilot.press("escape")
        assert row_value(wizard, "--seed") == ""
        await set_text(pilot, wizard, "--seed", "7")
        await pilot.press("enter")  # edit again: the value is there and typing appends
        await type_text(pilot, "2")
        await pilot.press("enter")
        assert row_value(wizard, "--seed") == "72"


@pytest.mark.asyncio
async def test_a_value_starting_with_a_dash_reaches_the_check_as_option_equals_value(checkout):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await set_text(pilot, wizard, "--scope", "-x.sv")
        await review_command(app, pilot, wizard)
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint --scope=-x.sv"


@pytest.mark.asyncio
async def test_ppa_profile_offers_the_underscore_spelling_its_parser_takes(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "fpga")
        await goto_row(pilot, wizard, "--ppa-profile")
        await pilot.press("enter")
        choices = wizard.query_one("#run-choices", OptionList)
        assert str(choices.get_option_at_index(3).prompt) == "max_frequency"


@pytest.mark.asyncio
async def test_a_toggle_and_a_three_way_field_cycle_their_values(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "synth")
        assert row_value(wizard, "--dry-run") == "off"
        await set_toggle(pilot, wizard, "--dry-run")
        assert row_value(wizard, "--dry-run") == "on"
        await set_toggle(pilot, wizard, "--dry-run")
        assert row_value(wizard, "--dry-run") == "off"
        assert row_value(wizard, "--flatten/--no-flatten") == "default"
        await set_choice(pilot, wizard, "--flatten/--no-flatten", "on")
        assert row_value(wizard, "--flatten/--no-flatten") == "on"
        await set_choice(pilot, wizard, "--flatten/--no-flatten", "default")
        await check_arguments(app, pilot, wizard, "")
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow synth"


@pytest.mark.asyncio
async def test_review_returns_to_the_form_with_every_value_kept(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await set_text(pilot, wizard, "--scope", "a.sv")
        await review_command(app, pilot, wizard)
        await pilot.press("escape")
        assert row_value(wizard, "--scope") == "a.sv"
        rows = wizard.query_one("#run-form", OptionList)
        assert rows.highlighted == rows.option_count - 1  # on Review, as left
        await review_command(app, pilot, wizard)
        assert shown(wizard, "#run-command") == f"cd {checkout} && booley flow lint --scope a.sv"


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
        return super().check(worktree, flow, argv)


@pytest.mark.asyncio
async def test_a_value_changed_while_a_check_runs_keeps_review_closed(checkout):
    launcher = GatedLauncher()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await set_text(pilot, wizard, "--scope", "a.sv")
        await goto_row(pilot, wizard, "Review the command")
        await pilot.press("enter")
        assert launcher.started.wait(timeout=60)
        await set_toggle(pilot, wizard, "--dry-run")  # flipped while the check runs
        launcher.release.set()
        await settle_check(app, pilot)
        assert wizard.query_one("#run-form", OptionList).display
        assert not wizard.query_one("#run-command", Static).display
        assert shown(wizard, "#run-error") == ""
        assert row_value(wizard, "--dry-run") == "on"


# --- the review screen: queue, timeout, warnings (#3440) --------------------------


class Slots:
    """Real slot entries under a Booley Directory, owned by live or dead processes."""

    def __init__(self, booley_dir: Path, *, max_light: int = 1, queue_max: int = 8) -> None:
        booley_dir.mkdir(parents=True, exist_ok=True)
        (booley_dir / "booley.toml").write_bytes(
            f"[jobs]\nmax_light = {max_light}\nqueue_max = {queue_max}\n".encode()
        )
        from booley.config.jobs import SlotCaps

        root = slots_dir(booley_dir)
        assert root is not None
        self._store = SlotStore(root, SlotCaps(max_light=max_light, queue_max=queue_max))
        self._tokens: list = []

    def occupy(self, count: int) -> None:
        """Add *count* claims by this live process: the first takes the slot, the rest queue."""
        for _ in range(count):
            token = self._store.submit("light", pid=os.getpid())
            self._store.refresh(token)
            self._tokens.append(token)

    def add_dead_owner(self) -> None:
        """Add a claim whose owner process has exited."""
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self._tokens.append(self._store.submit("light", pid=child.pid))

    def release(self) -> None:
        for token in self._tokens:
            self._store.release(token)


@pytest.fixture
def slots(tmp_path: Path):
    """Factory for :class:`Slots` over the test's Booley Directory; releases what it claimed."""
    made: list[Slots] = []

    def make(**kwargs) -> Slots:
        made.append(Slots(tmp_path / "booley", **kwargs))
        return made[-1]

    yield make
    for entry in made:
        entry.release()


LONG_TIMEOUT = "Timeout: 30m 00s per work unit"
ADMITTED_BY_MEMORY = "Admitted by memory: waits while its estimate does not fit the Memory Budget"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "live", "dead", "queue", "timeout"),
    [
        ("mutation", 0, 0, "Runs now", LONG_TIMEOUT),
        ("mutation", 1, 0, "Queues behind 1 Job (position 1) for a light slot", LONG_TIMEOUT),
        ("mutation", 2, 0, "Queues behind 2 Jobs (position 2) for a light slot", LONG_TIMEOUT),
        ("mutation", 0, 1, "Runs now", LONG_TIMEOUT),
        ("synth", 0, 0, ADMITTED_BY_MEMORY, LONG_TIMEOUT),
        ("lint", 1, 0, ADMITTED_BY_MEMORY, None),
    ],
    ids=[
        "empty",
        "one-holder",
        "holder-and-waiter",
        "dead-owner-only",
        "synth-memory-timeout",
        "lint-memory",
    ],
)
async def test_review_shows_whether_the_job_runs_now_or_queues(
    checkout, tmp_path, slots, flow, live, dead, queue, timeout
):
    entries = slots()
    entries.occupy(live)
    for _ in range(dead):
        entries.add_dead_owner()
    app = make_app(checkout, real_launcher(tmp_path / "booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, flow)
        await check_arguments(app, pilot, wizard, ARGUMENTS)
        assert shown(wizard, "#run-review").splitlines()[0] == queue
        assert ("Timeout: 30m 00s per work unit" in shown(wizard, "#run-review")) == (
            timeout is not None
        )
        assert shown(wizard, "#run-error") == ""


FULL = (9, 1, 8)  # ahead, cap, queue_max: 9 >= 1 + 8
ROOMY = (0, 1, 8)
COUNTED = ("light", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opened", "entered"), [(FULL, ROOMY), (ROOMY, FULL)], ids=["drains", "fills"]
)
async def test_full_queue_refuses_submit_and_rereads_on_enter(checkout, opened, entered):
    launcher = FakeLauncher("key", facts=COUNTED, outlook=opened)
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await check_arguments(app, pilot, wizard, "")
        assert launcher.outlook_reads == 1  # read when the review screen opens
        refused = "Refused: light queue is full (8 waiting). Retry when queued work drains"
        assert shown(wizard, "#run-error").startswith(refused) == (opened is FULL)
        launcher.outlook_result = entered
        await pilot.press("enter")  # Submit
        await pilot.pause()
        assert launcher.outlook_reads == 2  # and read again on Enter
        if entered is FULL:
            assert launcher.submitted == []
            assert shown(wizard, "#run-error").startswith(refused)
            assert "Queues behind 9 Jobs (position 9)" in shown(wizard, "#run-review")
            assert type(app.screen).__name__ == "RunWizard"
        else:
            assert len(launcher.submitted) == 1
            assert app.window == "jobs"
        if entered is FULL:  # Copy is never refused
            copied: list[str] = []
            app.copy_to_clipboard = copied.append
            await pilot.press("down", "enter")
            assert copied == [f"cd {checkout} && booley flow sim"]


@pytest.mark.asyncio
async def test_memory_admitted_review_reads_no_queue_and_never_refuses_submit(checkout):
    launcher = FakeLauncher("key", facts=("memory", False))
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await check_arguments(app, pilot, wizard, "")
        assert shown(wizard, "#run-review").splitlines()[0] == ADMITTED_BY_MEMORY
        assert shown(wizard, "#run-error") == ""
        await pilot.press("enter")  # Submit
        await pilot.pause()
        assert launcher.outlook_reads == 0
        assert len(launcher.submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("flow", "text"), [("sim", "--target t --dry-run")])
async def test_commands_without_a_slot_run_now_on_a_full_queue(
    checkout, tmp_path, slots, spawned, flow, text
):
    slots(queue_max=0).occupy(1)  # the light class is full: one holder, no room to wait
    app = make_app(checkout, real_launcher(tmp_path / "booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, flow)
        await check_arguments(app, pilot, wizard, text)
        assert shown(wizard, "#run-review").splitlines()[0] == "Runs now (takes no Job slot)"
        assert shown(wizard, "#run-error") == ""
        await pilot.press("enter")
        await pilot.pause()
        assert app.window == "jobs"
    assert len(spawned) == 1


def write_config(worktree: Path, text: str) -> None:
    (worktree / ".booley").mkdir(exist_ok=True)
    (worktree / ".booley" / "booley.toml").write_bytes(text.encode())


@pytest.mark.parametrize(
    ("flow", "argv", "config", "expected"),
    [
        ("sim", (), "", ("memory", False, None)),
        ("lint", (), "", ("memory", False, None)),
        ("sim", ("--dry-run",), "", ("memory", True, None)),
        ("synth", (), "", ("memory", False, 1_800_000)),
        ("fpga", (), "", ("memory", False, 7_200_000)),
        ("synth", ("--timeout-ms", "5000"), "", ("memory", False, 5000)),
        ("synth", (), "[flows.synth]\ntimeout_ms = 60000\n", ("memory", False, 60_000)),
        (
            "synth",
            ("--dry-run",),
            "[flows.synth]\ntimeout_ms = 60000\n",
            ("memory", True, 60_000),
        ),
        ("sim", (), "[flows.sim]\ntimeout_ms = 60000\n", ("memory", False, None)),
    ],
    ids=[
        "sim",
        "lint-memory",
        "sim-dry-run",
        "synth-default",
        "fpga-default",
        "synth-flag",
        "synth-config",
        "synth-dry-run-config",
        "sim-short-default-has-no-timeout",
    ],
)
def test_check_reports_run_facts(tmp_path, flow, argv, config, expected):
    from booley.harness.dashboard import launch

    write_config(tmp_path, config)
    result = launch.check(tmp_path, flow, ("--target", "t", *argv))
    facts = result.facts
    assert result.error == ""
    assert (facts.job_class, facts.dry_run, facts.timeout_ms) == expected


def test_check_reports_a_configured_timeout_the_flow_rejects(tmp_path):
    from booley.harness.dashboard import launch

    write_config(tmp_path, "[flows.synth]\ntimeout_ms = -5\n")
    facts = launch.check(tmp_path, "synth", ("--target", "t")).facts
    assert facts is not None
    assert facts.timeout_ms is None
    assert "must be a positive integer" in facts.timeout_problem


@pytest.mark.parametrize(
    "stdout",
    ["", "not json\n", "[1]\n", '{"job_class": 3, "dry_run": false}\n', '{"dry_run": false}\n'],
    ids=["silent", "text", "array", "bad-class", "missing-key"],
)
def test_check_refuses_a_child_that_gives_no_run_facts(stdout):
    from booley.harness.dashboard import launch

    assert launch._parse_facts(stdout) is None


@pytest.mark.asyncio
async def test_review_shows_a_timeout_problem_and_still_allows_submit(checkout):
    facts = ("light", False, None, "[flows.synth].timeout_ms must be positive")
    launcher = FakeLauncher("key", facts=facts, outlook=ROOMY)
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "synth")
        await check_arguments(app, pilot, wizard, "")
        assert (
            "Timeout problem: [flows.synth].timeout_ms must be positive; the Flow will reject"
            " this run"
        ) in shown(wizard, "#run-review")
        await pilot.press("enter")
        assert len(launcher.submitted) == 1


def running_job(handle: str, work_dir: Path, state: str = "running") -> JobView:
    status = "running" if state == "running" else "done"
    record = job_records.JobRecord(
        f"run-{handle}", "sim", STAMP, 60, status=status, work_dir=str(work_dir)
    )
    return JobView(Path("/jobs"), record, state, handle=handle)


@pytest.mark.asyncio
async def test_review_warns_about_sessions_running_jobs_and_changes(checkout, tmp_path, spawned):
    (checkout / "notes.txt").write_bytes(b"draft\n")  # one untracked file
    current = replace(
        world(session("S3", checkout)),
        jobs=JobSnapshot((running_job("J-SIM25", checkout), running_job("J-SYN34", checkout))),
    )
    app = make_app(checkout, real_launcher(tmp_path / "booley"), current)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        assert shown(wizard, "#run-review").splitlines() == [
            ADMITTED_BY_MEMORY,
            "Warning: session S3 Interactive is connected to this worktree",
            "Warning: 2 running Jobs in this worktree (J-SIM25, J-SYN34)",
            "Warning: 1 uncommitted change in this worktree",
        ]
        await pilot.press("enter")  # warnings do not block
        await pilot.pause()
        assert app.window == "jobs"
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_review_shows_no_warnings_for_a_quiet_worktree(checkout, tmp_path):
    other = tmp_path / "elsewhere"
    current = replace(
        world(session("S3", other)),
        jobs=JobSnapshot(
            (
                running_job("J-DONE", checkout, "completed"),
                running_job("J-OTHER", other),
                JobView(Path("/jobs"), job_records.JobRecord("bare", "sim", STAMP, 60), "running"),
            )
        ),
    )
    app = make_app(checkout, real_launcher(tmp_path / "booley"), current)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await check_arguments(app, pilot, wizard, "")
        assert shown(wizard, "#run-review") == ADMITTED_BY_MEMORY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("launcher_args", "snapshot_read", "expected"),
    [
        (
            {"uncommitted": WorktreeListError("git status failed")},
            True,
            "Cannot check uncommitted changes: git status failed",
        ),
        (
            {"uncommitted": BooleyLayoutError("[project].dir is retired")},
            True,
            "Cannot check uncommitted changes: [project].dir is retired",
        ),
        ({}, False, "Cannot check sessions and running Jobs: the Dashboard has not read them yet"),
        (
            {"facts": COUNTED, "outlook": OSError("permission denied")},
            True,
            "Cannot check the queue: permission denied",
        ),
    ],
    ids=["git-fails", "retired-layout", "snapshot-not-read", "queue-unreadable"],
)
async def test_review_says_which_checks_cannot_be_made(
    checkout, launcher_args, snapshot_read, expected
):
    launcher = FakeLauncher("key", **launcher_args)
    current = world() if snapshot_read else replace(world(), observed_at=0)
    app = make_app(checkout, launcher, current)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await check_arguments(app, pilot, wizard, "")
        assert expected in shown(wizard, "#run-review").splitlines()
        await pilot.press("enter")  # a check that cannot be made never blocks Submit
        await pilot.pause()
        assert len(launcher.submitted) == 1


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
        assert wizard.query_one("#run-form", OptionList).display


@pytest.mark.asyncio
async def test_a_check_that_crashes_after_the_human_moved_on_is_not_shown(checkout):
    gate = threading.Event()

    class SlowCrash(FakeLauncher):
        def check(self, worktree, flow, argv):
            assert gate.wait(timeout=60)
            raise ValueError("embedded null byte")

    app = make_app(checkout, SlowCrash())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await goto_row(pilot, wizard, "Review the command")
        await pilot.press("enter")  # the check starts and waits
        await pilot.press("escape")  # the human steps back to the Flow list
        gate.set()
        with contextlib.suppress(WorkerFailed):  # the crash itself is what the wizard must ignore
            await app.workers.wait_for_complete()
        await pilot.pause()
        assert shown(wizard, "#run-error") == ""

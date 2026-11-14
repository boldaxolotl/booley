"""The Run wizard starts the ``reviewer`` Specialist (#3816).

Pilots drive the real wizard over a real Git repository and the real launcher and
argument check; the fake launcher below only records what the wizard asks of it.
"""

from __future__ import annotations

import json
import shlex
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import OptionList

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import launch
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.harness.dashboard.specialist_form import ReviewerForm
from booley.harness.standalone_job import standalone_jobs_dir, standalone_request_path
from booley.runtime import auth_token, job_records
from tests.goals import conftest as goals_conftest
from tests.harness import test_dashboard as dashboard_tests
from tests.harness import test_dashboard_run as run_tests
from tests.harness.test_dashboard_run import (
    FLOW_NAMES,
    FakeLauncher,
    form_rows,
    goto_row,
    make_app,
    pick_rows,
    real_launcher,
    review_command,
    row_value,
    set_choice,
    set_text,
    settle_form,
    shown,
    start_run,
    type_text,
    write_config,
)
from tests.harness.test_dashboard_targets import settle

# Fixtures of the Run and Goal tests, shared by assignment so a test finds them by name.
checkout = run_tests.checkout
spawned = run_tests.spawned
layout = goals_conftest.layout
enter_goals = goals_conftest.enter_goals


@pytest.fixture(autouse=True)
def _quiet_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generous first read and no timer reads: the tests order every read themselves."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)
    monkeypatch.setattr(dashboard_app, "REFRESH_SECONDS", 3600)


@pytest.fixture
def empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No credential in the environment or on disk; the Claude Agent CLI is the default.

    The check child inherits this environment. ``BOOLEY_PRIMARY_PROVIDER`` keeps the
    settings loader from refusing an unresolved provider when the suite runs in a Sandbox.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "CODEX_HOME",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_AGENT_APP",
        "BOOLEY_AGENT_CLIS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
    return home


class ReviewerLauncher(FakeLauncher):
    """A launcher with a reviewer row and form that records the commands it is asked to check."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.checks: list[tuple[str, str, tuple[str, ...]]] = []

    def specialists(self, worktree):
        from booley.harness.dashboard.launch import SpecialistChoice, SpecialistListing

        return SpecialistListing((SpecialistChoice("reviewer", "Review RTL or testbench."),))

    def form(self, kind, name, worktree):
        from booley.harness.dashboard.specialist_form import ReviewerForm

        return ReviewerForm("") if kind == "specialist" else super().form(kind, name, worktree)

    def check(self, worktree, kind, name, argv):
        self.checks.append((kind, name, argv))
        return super().check(worktree, kind, name, argv)


def step_rows(wizard) -> list[tuple[str, bool]]:
    """The Flow or Specialist step's rows as (text, disabled)."""
    options = wizard.query_one("#run-flows", OptionList)
    rows = [options.get_option_at_index(i) for i in range(options.option_count)]
    return [(str(row.prompt), row.disabled) for row in rows]


def reviewer_row(wizard) -> int:
    """The index of the reviewer's row in the step."""
    rows = [text for text, _ in step_rows(wizard)]
    found = [i for i, text in enumerate(rows) if text.startswith("reviewer")]
    assert found, f"no reviewer row in the step: {rows}"
    return found[0]


async def to_step(app, pilot):
    """Run, accept the preselected worktree and let the Specialists read land."""
    wizard = await start_run(app, pilot)
    await pilot.press("enter")
    await settle(app, pilot)
    return wizard


async def to_reviewer(app, pilot, name="reviewer"):
    """Open the form of Specialist *name* from the step."""
    wizard = await to_step(app, pilot)
    options = wizard.query_one("#run-flows", OptionList)
    rows = [text for text, _ in step_rows(wizard)]
    target = next(i for i, text in enumerate(rows) if text.startswith(name))
    for _ in range(len(step_rows(wizard))):
        if options.highlighted == target:
            break
        await pilot.press("down")
    assert options.highlighted == target
    await pilot.press("enter")
    await settle_form(app, pilot)
    return wizard


def goal_worktree_app(layout):
    """An app over the Goal Mode layout; the launcher reads its Goal store."""
    return make_app(layout.main, real_launcher(layout.control))


async def to_goal_reviewer(app, pilot, layout):
    """The reviewer's form for the layout's linked worktree."""
    wizard = await start_run(app, pilot)
    index = next(i for i, choice in enumerate(wizard._choices) if choice.path == layout.worktree)
    wizard.query_one("#run-worktrees", OptionList).highlighted = index
    await pilot.press("enter")
    await settle(app, pilot)
    options = wizard.query_one("#run-flows", OptionList)
    options.highlighted = reviewer_row(wizard)
    await pilot.press("enter")
    await settle_form(app, pilot)
    return wizard


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_launches_with_the_shown_command_submitted(
    checkout, spawned, tmp_path, empty_home
):
    write_config(checkout, "")  # the check reads the worktree's settings, as the worker does
    booley_dir = tmp_path / "booley"
    app = make_app(checkout, real_launcher(booley_dir))
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot)
        assert "Extra arguments" not in " ".join(form_rows(wizard))
        await set_choice(pilot, wizard, "Focus", "bugs")
        await set_text(pilot, wizard, "Scope", "fifo.v")
        await review_command(app, pilot, wizard)
        command = shown(wizard, "#run-command")
        expected = ["--category", "rtl", "--focus", "bugs", "--scope", "fifo.v"]
        assert (
            command
            == f"cd {shlex.quote(str(checkout))} && booley specialist reviewer {' '.join(expected)}"
        )
        assert shown(wizard, "#run-review").splitlines()[0].startswith("Runs now")
        await pilot.press("down", "enter")  # Copy command
        assert copied == [command]
        await pilot.press("up", "enter")  # Submit
        await pilot.pause()
        assert app.window == "jobs"
        jobs_dir, run_id = json.loads(app.navigation.selected)
        assert Path(jobs_dir) == standalone_jobs_dir(booley_dir)
    record = job_records.read_record(run_id, standalone_jobs_dir(booley_dir))
    assert record is not None
    assert record.argv == ["booley", "specialist", "reviewer", *expected]
    assert shlex.split(command) == ["cd", record.work_dir, "&&", *record.argv]
    request = json.loads(standalone_request_path(booley_dir, run_id).read_text(encoding="utf-8"))
    assert request == {
        "worktree": str(checkout),
        "kind": "specialist",
        "name": "reviewer",
        "argv": expected,
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


def test_specialist_check_parses_like_the_shell(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    arguments = ["--category", "rtl", "--focus", "bugs", "--scope", "a.v", "--timeout", "5m"]
    result = launch.check(tmp_path, "specialist", "reviewer", tuple(arguments))
    assert result.error == ""
    assert result.facts is not None
    assert (result.facts.job_class, result.facts.dry_run) == ("light", False)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        launch.main(["specialist", "reviewer", "--", "--category", "x"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_choice_step_lists_enabled_builtin_specialists_and_each_is_selectable(
    checkout,
):
    (checkout / ".booley" / "mcp_tools").mkdir(parents=True)
    (checkout / ".booley" / "mcp_tools" / "custom.py").write_text(
        "from booley.specialists.specialist import Specialist\n\n\n"
        "class Custom(Specialist):\n"
        '    name = "custom_check"\n'
        '    description = "A Project Specialist."\n',
        encoding="utf-8",
    )
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_step(app, pilot)
        rows = step_rows(wizard)
        assert [text.split(" — ")[0] for text, _ in rows[len(FLOW_NAMES) :]] == [
            "coverage_analyst",
            "reviewer",
        ]
        assert [disabled for _, disabled in rows] == [False] * (len(FLOW_NAMES) + 2)
        assert all("form not built yet" not in text for text, _ in rows)
        assert len(rows[-1][0].split(" — ", 1)[1]) > 10
    write_config(checkout, "[specialists.coverage_analyst]\nenabled = false\n")
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_step(app, pilot)
        names = [text.split(" — ")[0] for text, _ in step_rows(wizard)[len(FLOW_NAMES) :]]
        assert names == ["reviewer"]  # custom Project Specialists never appear


class CrashingListing(FakeLauncher):
    def specialists(self, worktree):
        raise ValueError("boom")


@pytest.mark.asyncio
async def test_choice_step_says_why_specialists_cannot_be_read(checkout, monkeypatch):
    # An unreadable booley.toml, a layout error and a crashed read each become one reason row.
    problems: list[str] = []
    write_config(checkout, "not = [valid")
    for launcher in (real_launcher(checkout / ".booley"), CrashingListing()):
        app = make_app(checkout, launcher)
        async with app.run_test(size=(120, 40)) as pilot:
            wizard = await to_step(app, pilot)
            text, disabled = step_rows(wizard)[-1]
            assert text.startswith("Specialists unavailable"), text
            assert disabled
            problems.append(text)
            await pilot.press("enter")  # a Flow still opens its form
            await settle_form(app, pilot)
            assert wizard.query_one("#run-form", OptionList).display
    assert problems[0].startswith("Specialists unavailable: EndpointConfigError: Cannot read")
    assert problems[1] == "Specialists unavailable: ValueError: boom"

    def retired_layout(*_args):
        raise RuntimeError("retired layout")

    monkeypatch.setattr("booley.mcp.endpoint_config.get_endpoint_config", retired_layout)
    listing = launch.builtin_specialists(checkout)
    assert (listing.choices, listing.problem) == ((), "RuntimeError: retired layout")


class SpecialistGate(ReviewerLauncher):
    """A launcher whose Specialists read waits until the test lets it answer."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def specialists(self, worktree):
        assert self.release.wait(timeout=60)
        return super().specialists(worktree)


@pytest.mark.asyncio
async def test_flows_are_choosable_while_the_specialists_load(checkout):
    launcher = SpecialistGate()
    app = make_app(checkout, launcher)
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            wizard = await start_run(app, pilot)
            await pilot.press("enter")
            rows = step_rows(wizard)
            assert rows[-1] == ("Loading Specialists…", True)
            assert not any(disabled for _, disabled in rows[:-1])
            launcher.release.set()
            await settle(app, pilot)
            assert [text.split(" — ")[0] for text, _ in step_rows(wizard)][-1] == "reviewer"
    finally:
        launcher.release.set()


class BrokenReviewerForm(ReviewerLauncher):
    def form(self, kind, name, worktree):
        if kind == "specialist":
            raise ValueError("odd goal data")
        return super().form(kind, name, worktree)


@pytest.mark.asyncio
async def test_a_reviewer_form_that_cannot_be_built_says_why_instead_of_crashing(checkout):
    # The reviewer form has no Extra arguments row: only Review is left, and it still shows.
    app = make_app(checkout, BrokenReviewerForm())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot)
        assert shown(wizard, "#run-note") == "The form cannot be built: ValueError: odd goal data"
        assert form_rows(wizard) == ["Review the command"]


class KeyedListing(ReviewerLauncher):
    """A launcher whose Specialists read answers per worktree, or fails while ``broken``."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = True
        self.reads: list[Path] = []
        self.gates: dict[Path, threading.Event] = {}

    def specialists(self, worktree):
        from booley.harness.dashboard.launch import SpecialistChoice, SpecialistListing

        self.reads.append(worktree)
        assert self.gates.setdefault(worktree, threading.Event()).wait(timeout=60)
        if self.broken:
            return SpecialistListing((), "EndpointConfigError: bad toml")
        return SpecialistListing((SpecialistChoice("reviewer", str(worktree)),))


@pytest.mark.asyncio
async def test_a_failed_specialists_read_is_tried_again_on_choosing_the_worktree_again(checkout):
    launcher = KeyedListing()
    launcher.gates[checkout] = threading.Event()
    launcher.gates[checkout].set()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_step(app, pilot)
        assert step_rows(wizard)[-1] == (
            "Specialists unavailable: EndpointConfigError: bad toml",
            True,
        )
        launcher.broken = False  # the human fixed booley.toml
        await pilot.press("escape", "enter")
        await settle(app, pilot)
        assert launcher.reads == [checkout, checkout]
        assert step_rows(wizard)[-1][0].startswith("reviewer")
        await pilot.press("escape", "enter")  # a good listing is kept, not read again
        await settle(app, pilot)
        assert launcher.reads == [checkout, checkout]


@pytest.mark.asyncio
async def test_a_specialists_read_of_a_replaced_worktree_is_dropped(checkout, tmp_path):
    launcher = KeyedListing()
    launcher.broken = False
    first, second = checkout, tmp_path / "other"
    launcher.gates = {first: threading.Event(), second: threading.Event()}
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await start_run(app, pilot)
        wizard._choose_worktree(first)
        await pilot.pause()
        wizard.action_back()
        wizard._choose_worktree(second)
        launcher.gates[second].set()  # the second read lands first
        await pilot.pause(0.3)
        launcher.gates[first].set()  # then the replaced one answers late
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert step_rows(wizard)[-1][0] == f"reviewer — {second}"


# --- AC3 and AC4 -------------------------------------------------------------

SPEC_GOAL = {
    "family": "REVIEW",
    "review": "rtl_spec",
    "verdict": "clean",
    "paths": ["rtl.v"],
    "spec": "docs/spec.md",
}


@pytest.mark.asyncio
async def test_wizard_follows_the_category_and_prefills_the_spec_path(layout, spawned):
    enter_goals(layout, [SPEC_GOAL])
    app = goal_worktree_app(layout)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_goal_reviewer(app, pilot, layout)
        assert row_value(wizard, "Category") == "rtl"
        assert row_value(wizard, "Focus") == ""
        assert "Spec path" not in " ".join(form_rows(wizard))
        await set_choice(pilot, wizard, "Focus", "spec")
        assert row_value(wizard, "Spec path") == "docs/spec.md"
        await set_choice(pilot, wizard, "Category", "tb")
        assert row_value(wizard, "Focus") == "quality"
        assert "Spec path" not in " ".join(form_rows(wizard))
        await set_choice(pilot, wizard, "Category", "rtl")
        assert row_value(wizard, "Focus") == ""
        await set_choice(pilot, wizard, "Focus", "spec")
        assert row_value(wizard, "Spec path") == "docs/spec.md"
        await goto_row(pilot, wizard, "Spec path")
        await pilot.press(
            "enter", *["backspace"] * len("docs/spec.md")
        )  # the editor keeps the prefill
        await pilot.press(*"docs/other.md", "enter")
        assert row_value(wizard, "Spec path") == "docs/other.md"
        await goto_row(pilot, wizard, "More")
        await pilot.press("enter")
        assert row_value(wizard, "Max turns") == ""
        await pilot.press("enter")
        assert "Max turns" not in " ".join(form_rows(wizard))


@pytest.mark.asyncio
async def test_spec_path_is_prefilled_from_the_active_spec_review_goal(layout):
    launcher = real_launcher(layout.control)

    def prefill(worktree: Path = layout.worktree) -> str:
        form = launcher.form("specialist", "reviewer", worktree)
        assert isinstance(form, ReviewerForm)
        return form.initial()["spec"]

    assert prefill() == ""  # no Goal Mode
    other = {"family": "REVIEW", "review": "rtl_bugs", "verdict": "done", "paths": ["rtl.v"]}
    enter_goals(layout, [other, {**SPEC_GOAL, "verdict": "done"}])
    assert prefill() == "docs/spec.md"
    assert prefill(layout.main) == ""
    (record_file,) = layout.control.rglob("record.json")
    record_file.write_bytes(b"{ not json")
    assert prefill() == ""  # a corrupt Goal record only loses the prefill


# --- AC6 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_refuses_an_empty_focus_scope_or_spec_path(checkout):
    launcher = ReviewerLauncher()
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot)

        async def refused(message: str) -> None:
            await review_command(app, pilot, wizard)
            assert shown(wizard, "#run-error") == message
            assert wizard.query_one("#run-form", OptionList).display
            assert launcher.checks == []

        await refused("Choose a focus")
        await set_choice(pilot, wizard, "Focus", "bugs")
        await refused("Scope is empty: name the files to review")
        await set_text(pilot, wizard, "Scope", "   ")
        await refused("Scope is empty: name the files to review")
        await set_choice(pilot, wizard, "Focus", "spec")
        await set_text(pilot, wizard, "Scope", "a.v")
        await refused("The spec focus needs a spec path")
        await set_text(pilot, wizard, "Spec path", "docs/spec.md")
        await review_command(app, pilot, wizard)
        assert launcher.checks == [
            (
                "specialist",
                "reviewer",
                (
                    "--category",
                    "rtl",
                    "--focus",
                    "spec",
                    "--spec",
                    "docs/spec.md",
                    "--scope",
                    "a.v",
                ),
            )
        ]


# --- coverage_analyst (#2305) ------------------------------------------------

CAMPAIGN_ENDED = "2024-03-01T10:00:00Z"


def write_campaign(root: Path, number: int, target: str = "sim_fifo") -> Path:
    """A finished coverage campaign of Simulation run *number* under *root*'s Booley Directory."""
    directory = root / ".booley" / "flow-reports" / "sim" / str(number) / "targets" / target
    directory.mkdir(parents=True)
    (directory / "coverage.json").write_text("{}\n", encoding="utf-8")
    (directory / "simulation.json").write_text(
        json.dumps({"complete": True, "target": target}), encoding="utf-8"
    )
    return directory.parent.parent


def sim_job(run_id: str, work_dir: Path, *, endpoint="sim", state="completed", report=True):
    """A Job view of *endpoint* in *work_dir*; its report sits in sim run ``<run_id>``."""
    record = job_records.JobRecord(
        run_id,
        endpoint,
        CAMPAIGN_ENDED,
        60,
        status="running" if state == "running" else "done",
        work_dir=str(work_dir),
        ended_at=CAMPAIGN_ENDED if state != "running" else None,
    )
    report_path = work_dir / ".booley" / "flow-reports" / "sim" / run_id / "report.json"
    return JobView(Path("/jobs"), record, state, report_path=report_path if report else None)


@pytest.mark.asyncio
async def test_sim_runs_are_the_finished_simulation_jobs_of_the_worktree(checkout, tmp_path):
    from booley.harness.dashboard.specialist_form import SimRun

    other = tmp_path / "other"
    jobs = (
        sim_job("4", checkout),
        sim_job("5", checkout, state="running"),
        sim_job("6", other),
        sim_job("7", checkout, endpoint="lint"),
        sim_job("8", checkout, report=False),
    )
    current = replace(
        dashboard_tests.snapshot(0), sessions=(), session_handles={}, jobs=JobSnapshot(jobs)
    )
    app = make_app(checkout, FakeLauncher(), current)
    assert app._finished_sim_runs(checkout) is None  # the Jobs have not been read yet
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        runs = app._finished_sim_runs(checkout)
    assert runs == (SimRun(checkout / ".booley" / "flow-reports" / "sim" / "4", CAMPAIGN_ENDED),)


async def type_campaign(app, pilot, wizard, old: str, new: str) -> None:
    """Reopen the Coverage Record picker, take its manual row and replace *old* with *new*."""
    await goto_row(pilot, wizard, "Coverage Record")
    await pilot.press("enter")
    await settle(app, pilot)
    await pilot.press("down", "enter")  # the manual row opens the editor
    assert wizard.query_one("#run-edit").display
    await pilot.press(*["backspace"] * len(old))
    await type_text(pilot, new)
    await pilot.press("enter")
    assert row_value(wizard, "Coverage Record") == new


@pytest.mark.asyncio
async def test_coverage_analyst_launches_a_picked_or_typed_campaign(
    checkout, spawned, tmp_path, empty_home
):
    from booley.harness.dashboard.specialist_form import SimRun

    write_config(checkout, "")
    booley_dir = tmp_path / "booley"
    invocation = write_campaign(checkout, 1)
    app = make_app(checkout, real_launcher(booley_dir))
    app._finished_sim_runs = lambda _worktree: (SimRun(invocation, CAMPAIGN_ENDED),)  # type: ignore[method-assign]
    picked = ".booley/flow-reports/sim/1/targets/sim_fifo/coverage.json"
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot, "coverage_analyst")
        assert "Extra arguments" not in " ".join(form_rows(wizard))
        assert row_value(wizard, "Coverage Record") == ""
        await goto_row(pilot, wizard, "Coverage Record")
        await pilot.press("enter")
        await settle(app, pilot)
        rows = pick_rows(wizard)
        assert len(rows) == 2
        assert rows[0].startswith("sim_fifo  ") and rows[0].endswith("  sim #1")
        assert rows[1] == "Type a coverage.json path…"
        assert wizard.query_one("#run-picks", OptionList).highlighted == 0  # the newest
        await pilot.press("enter")  # one Enter picks the newest
        assert row_value(wizard, "Coverage Record") == picked
        await review_command(app, pilot, wizard)
        command = shown(wizard, "#run-command")
        assert command == (
            f"cd {shlex.quote(str(checkout))} && booley specialist coverage_analyst --record {picked}"
        )
        await pilot.press("escape")
        await type_campaign(app, pilot, wizard, picked, "other/coverage.json")
        await review_command(app, pilot, wizard)
        assert shown(wizard, "#run-command").endswith("--record other/coverage.json")
        await pilot.press("enter")  # Submit
        await pilot.pause()
        _jobs_dir, run_id = json.loads(app.navigation.selected)
    record = job_records.read_record(run_id, standalone_jobs_dir(booley_dir))
    assert record is not None
    assert record.argv == [
        "booley",
        "specialist",
        "coverage_analyst",
        "--record",
        "other/coverage.json",
    ]
    request = json.loads(standalone_request_path(booley_dir, run_id).read_text(encoding="utf-8"))
    assert request["argv"] == ["--record", "other/coverage.json"]
    assert request["name"] == "coverage_analyst"
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_coverage_analyst_without_campaigns_explains_how_to_make_one(checkout, empty_home):
    from booley.harness.dashboard.specialist_form import NO_CAMPAIGNS

    write_config(checkout, "")
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    app._finished_sim_runs = lambda _worktree: ()  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot, "coverage_analyst")
        await review_command(app, pilot, wizard)  # before choosing anything
        assert shown(wizard, "#run-error") == "Choose a Coverage Record"
        await goto_row(pilot, wizard, "Coverage Record")
        await pilot.press("enter")
        await settle(app, pilot)
        assert shown(wizard, "#run-hint") == NO_CAMPAIGNS
        assert pick_rows(wizard) == ["Type a coverage.json path…"]


@pytest.mark.asyncio
async def test_coverage_picker_says_when_the_jobs_are_unread_or_the_read_crashes(
    checkout, empty_home, monkeypatch
):
    from booley.harness.dashboard import wizard as wizard_module
    from booley.harness.dashboard.specialist_form import SimRun

    write_config(checkout, "")
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    app._finished_sim_runs = lambda _worktree: None  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot, "coverage_analyst")
        await goto_row(pilot, wizard, "Coverage Record")
        await pilot.press("enter")
        await settle(app, pilot)
        assert shown(wizard, "#run-hint") == wizard_module.JOBS_NOT_READ
        assert pick_rows(wizard) == ["Type a coverage.json path…"]
        await pilot.press("escape")
        # A read that crashes lists no campaign instead of waiting forever.
        wizard._sim_runs = lambda _worktree: (SimRun(checkout, CAMPAIGN_ENDED),)
        monkeypatch.setattr(wizard_module, "finished_campaigns", lambda *_args: 1 / 0)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "No finished Coverage Record" in shown(wizard, "#run-hint")

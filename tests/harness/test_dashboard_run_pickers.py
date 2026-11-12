"""The Run wizard's Target and test pickers (#3581).

Pilots drive the real wizard over real Git repositories carrying real ``.core``
files and a ``tests.toml``, and the real ``TargetsReader``: the pickers offer what
the Targets window reads, nothing is written for them, and a read that fails or
goes stale says so instead of leaving the field blank.
"""

from __future__ import annotations

import shlex
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from textual.widgets import OptionList

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.targets import TargetsReader
from booley.targets.domain import CoreCollisionError
from tests.harness.test_dashboard_run import (
    FLOW_NAMES,
    FakeLauncher,
    check_arguments,
    goto_row,
    make_app,
    open_picker,
    pick_rows,
    press_pick,
    review_command,
    row_value,
    set_target,
    set_tests,
    settle,
    shown,
    start_run,
    to_form,
    write_picker_project,
)
from tests.harness.test_dashboard_run_form import parser_for
from tests.harness.test_dashboard_targets import (
    SIM_CORE,
    UNRESOLVABLE_CASES,
    core_text,
    git,
    init_repository,
    world,
    write_files,
)

if TYPE_CHECKING:
    from booley.harness.dashboard.targets import RunChoices, TargetListing

# The Targets each Flow can drive in the fixture project.
TARGETS_BY_FLOW = {
    "fpga": ["fpga_fifo"],
    "lint": ["lint_fifo"],
    "mutation": ["sim_fifo", "sim_wide"],
    "sim": ["sim_fifo", "sim_wide"],
    "synth": ["synth_fifo"],
}
# Flows whose --target is required: their picker has no automatic row.
REQUIRED_TARGET_FLOWS = {"mutation"}
SIM_FIFO_RUNS = ["reset", "stress", "stress+short", "stress+soak", "slow"]


@pytest.fixture(autouse=True)
def _quiet_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generous first read and no timer reads: the tests order every read themselves."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)
    monkeypatch.setattr(dashboard_app, "REFRESH_SECONDS", 3600)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git repository with the picker fixture project in it: the main checkout."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    main = tmp_path / "main"
    init_repository(main)
    write_picker_project(main)
    return main


def stage_hint(wizard) -> str:
    return shown(wizard, "#run-hint")


async def until(pilot, condition, what: str) -> None:
    """Let the app run until *condition* holds, for reads a test is holding back."""
    for _ in range(600):
        if condition():
            return
        await pilot.pause(0.05)
    pytest.fail(f"never happened: {what}")


# --- the Target field ------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_field_offers_only_the_flows_compatible_targets(project):
    assert sorted(TARGETS_BY_FLOW) == FLOW_NAMES, "add the new Flow's Targets to the fixture"
    for flow, targets in TARGETS_BY_FLOW.items():
        app = make_app(project, FakeLauncher())
        async with app.run_test(size=(120, 40)) as pilot:
            wizard = await to_form(app, pilot, flow)
            unset = "" if flow in REQUIRED_TARGET_FLOWS else "automatic"
            assert row_value(wizard, "--target") == unset
            await open_picker(app, pilot, wizard, "--target")
            assert wizard._stage == "target"
            assert pick_rows(wizard) == ([] if unset == "" else ["automatic"]) + targets
            assert "One Target" in stage_hint(wizard)
            assert "esc back" in shown(wizard, "#run-footer")
            assert shown(wizard, "#run-title") == f"booley flow {flow}: --target"
            await pilot.press("escape")  # leaving changes nothing
            assert row_value(wizard, "--target") == unset
            await set_target(app, pilot, wizard, "--target", targets[-1])
            assert wizard._stage == "form"
            assert row_value(wizard, "--target") == targets[-1]
            await open_picker(app, pilot, wizard, "--target")  # opens on the chosen Target
            picks = wizard.query_one("#run-picks", OptionList)
            assert pick_rows(wizard)[picks.highlighted or 0] == targets[-1]
            if unset:
                await press_pick(pilot, wizard, "automatic")  # and automatic goes back
                assert row_value(wizard, "--target") == "automatic"


class RequiredTargetLauncher(FakeLauncher):
    """A launcher whose Flow insists on a Target: the form comes from such a parser."""

    def form(self, flow):
        from booley.harness.dashboard import run_form

        parser = parser_for("sim")
        next(action for action in parser._actions if action.dest == "target").required = True
        return run_form.describe(parser)


@pytest.mark.asyncio
async def test_target_field_of_a_flow_that_requires_a_target_offers_no_automatic(project):
    app = make_app(project, RequiredTargetLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        assert row_value(wizard, "--target") == ""
        await open_picker(app, pilot, wizard, "--target")
        assert pick_rows(wizard) == ["sim_fifo", "sim_wide"]
        await press_pick(pilot, wizard, "sim_wide")
        assert row_value(wizard, "--target") == "sim_wide"


# --- the test field --------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_field_offers_the_targets_test_runs_and_toggles_several(project):
    launcher = FakeLauncher()
    app = make_app(project, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        assert row_value(wizard, "--test") == "default suite"
        await set_target(app, pilot, wizard, "--target", "sim_fifo")
        await open_picker(app, pilot, wizard, "--test")
        assert wizard._stage == "tests"
        assert pick_rows(wizard) == [f"[ ] {run}" for run in SIM_FIFO_RUNS]
        assert "Enter toggles" in stage_hint(wizard)
        assert "⏎ toggle" in shown(wizard, "#run-footer")
        await press_pick(pilot, wizard, "reset")
        await press_pick(pilot, wizard, "stress+soak")
        await press_pick(pilot, wizard, "reset")  # off again
        await press_pick(pilot, wizard, "reset")  # and on: the order is the pick order
        assert pick_rows(wizard)[0] == "[x] reset"
        assert pick_rows(wizard)[3] == "[x] stress+soak"
        picks = wizard.query_one("#run-picks", OptionList)
        assert picks.highlighted == 0  # the highlight stays where the human toggled
        await pilot.press("space")  # no other key does anything
        assert pick_rows(wizard) == [
            "[x] reset",
            "[ ] stress",
            "[ ] stress+short",
            "[x] stress+soak",
            "[ ] slow",
        ]
        await pilot.press("escape")
        assert row_value(wizard, "--test") == "stress+soak, reset"
        await open_picker(app, pilot, wizard, "--test")  # reopened: nothing is read twice
        assert pick_rows(wizard)[0] == "[x] reset"
        await pilot.press("escape")
        await review_command(app, pilot, wizard)
        expected = ("--target", "sim_fifo", "--test", "stress+soak", "--test", "reset")
        assert (
            shown(wizard, "#run-command")
            == f"cd {shlex.quote(str(project))} && booley flow sim {' '.join(expected)}"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert launcher.submitted == [(project, "sim", expected)]


@pytest.mark.asyncio
async def test_choosing_another_target_clears_the_chosen_tests(project):
    app = make_app(project, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await set_target(app, pilot, wizard, "--target", "sim_fifo")
        await set_tests(app, pilot, wizard, "--test", ["reset"])
        assert row_value(wizard, "--test") == "reset"
        await set_target(app, pilot, wizard, "--target", "sim_fifo")  # the same Target again
        assert row_value(wizard, "--test") == "reset"
        await set_target(app, pilot, wizard, "--target", "sim_wide")
        assert row_value(wizard, "--test") == "default suite"
        await open_picker(app, pilot, wizard, "--test")
        assert pick_rows(wizard) == ["[ ] wide", "[ ] narrow"]


@pytest.mark.asyncio
async def test_unreadable_tests_say_why_write_nothing_and_extra_arguments_still_take_them(
    project, tmp_path
):
    # No Target chosen: nothing to list.
    app = make_app(project, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await open_picker(app, pilot, wizard, "--test")
        assert "Choose a Target first" in stage_hint(wizard)
        assert pick_rows(wizard) == []
        await pilot.press("enter", "escape")  # Enter on no row does nothing
        await check_arguments(app, pilot, wizard, "--test x")
        assert (
            shown(wizard, "#run-command")
            == f"cd {shlex.quote(str(project))} && booley flow sim --test x"
        )
    cases = {
        "a tests.toml the Target cannot apply": (
            {
                "sim.core": SIM_CORE,
                ".booley/tests.toml": UNRESOLVABLE_CASES["bad-tests-toml"][0][
                    ".booley/tests.toml"
                ],
            },
            "Tests unavailable: ",
        ),
        "no tests registered": (
            {"sim.core": SIM_CORE, ".booley/booley.toml": "[project]\n"},
            "This Target registers no tests",
        ),
    }
    for name, (files, expected) in cases.items():
        root = tmp_path / name.replace(" ", "-") / "project"
        root.parent.mkdir()
        init_repository(root)
        write_files(root, {"top.v": "module top; endmodule\n", **files})
        await check_unreadable(root, expected)
    await check_unreadable_stealth(tmp_path / "stealth")


@pytest.mark.asyncio
async def test_a_failed_test_read_is_tried_again_when_the_picker_reopens(project):
    tests_toml = project / ".booley" / "tests.toml"
    good = tests_toml.read_bytes()
    tests_toml.write_bytes(b"[tests\n")  # not TOML: the read fails
    app = make_app(project, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await set_target(app, pilot, wizard, "--target", "sim_fifo")
        await open_picker(app, pilot, wizard, "--test")
        assert "Tests unavailable" in stage_hint(wizard)
        assert pick_rows(wizard) == []
        await pilot.press("escape")
        tests_toml.write_bytes(good)  # fixed meanwhile
        await open_picker(app, pilot, wizard, "--test")
        assert pick_rows(wizard) == [f"[ ] {run}" for run in SIM_FIFO_RUNS]


async def check_unreadable(root: Path, expected: str) -> None:
    app = make_app(root, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await set_target(app, pilot, wizard, "--target", "sim_top")
        await open_picker(app, pilot, wizard, "--test")
        assert expected in stage_hint(wizard)
        assert "Type Test Runs in the extra arguments" in stage_hint(wizard)
        assert pick_rows(wizard) == []
        await pilot.press("escape")
        await check_arguments(app, pilot, wizard, "--test x")
        assert (
            shown(wizard, "#run-command")
            == f"cd {shlex.quote(str(root))} && booley flow sim --target sim_top --test x"
        )


def tree_state(root: Path) -> list[tuple[str, int]]:
    """Every path under *root* (but Git's own) with its modification time."""
    return sorted(
        (str(path), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if ".git" not in path.parts
    )


async def check_unreadable_stealth(base: Path) -> None:
    """Stealth projections that need a refresh: the picker says so and writes nothing."""
    root = base / "project"
    base.mkdir()
    init_repository(root)
    write_files(
        root,
        {
            ".booley/booley.toml": "[project]\n\n[stealth]\nenabled = true\n",
            ".booley/cores/stealth.core": SIM_CORE,
            "top.v": "module top; endmodule\n",
        },
    )
    before = tree_state(root)
    app = make_app(root, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await set_target(app, pilot, wizard, "--target", "sim_top")
        await open_picker(app, pilot, wizard, "--test")
        assert "core projections need a refresh" in stage_hint(wizard)
        await pilot.press("escape")
        await check_arguments(app, pilot, wizard, "--test x")
        assert "--test x" in shown(wizard, "#run-command")
    assert tree_state(root) == before


class CrashingReader(TargetsReader):
    """A reader whose listing, or whose Test Run read, raises."""

    def __init__(self, root: Path, *, listing_crashes: bool) -> None:
        super().__init__(root)
        self.listing_crashes = listing_crashes

    def listing(self, worktree: Path) -> TargetListing:
        if self.listing_crashes:
            raise RuntimeError("listing boom")
        return super().listing(worktree)

    def test_runs(self, worktree: Path, selector: str) -> RunChoices:
        raise RuntimeError("tests boom")


@pytest.mark.asyncio
async def test_unlistable_or_crashed_targets_say_why_and_extra_arguments_still_take_them(
    project, monkeypatch
):
    from booley.harness.dashboard import targets

    real = targets.collect_surface

    def collide(_root):
        raise CoreCollisionError("two cores declare acme:ip:dup:1.0")

    monkeypatch.setattr(targets, "collect_surface", collide)
    app = make_app(project, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "lint")
        await open_picker(app, pilot, wizard, "--target")
        assert "Targets unavailable: two cores declare acme:ip:dup:1.0" in stage_hint(wizard)
        assert "Type Targets in the extra arguments" in stage_hint(wizard)
        assert pick_rows(wizard) == ["automatic"]
        await pilot.press("escape")
        await check_arguments(app, pilot, wizard, "--target t")
        assert (
            shown(wizard, "#run-command")
            == f"cd {shlex.quote(str(project))} && booley flow lint --target t"
        )
        monkeypatch.setattr(targets, "collect_surface", real)  # fixed meanwhile
        await pilot.press("escape", "escape", "enter")  # back to the Flow list, lint again
        await settle(app, pilot)
        assert row_value(wizard, "Extra arguments") == "--target t"  # what was typed stays
        await open_picker(app, pilot, wizard, "--target")
        assert pick_rows(wizard) == ["automatic", "lint_fifo"]
    crashed = DashboardApp(
        world, targets=CrashingReader(project, listing_crashes=True), launcher=FakeLauncher()
    )
    async with crashed.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(crashed, pilot, "lint")
        await open_picker(crashed, pilot, wizard, "--target")
        assert "Targets unavailable: RuntimeError: listing boom" in stage_hint(wizard)
        assert "Loading" not in stage_hint(wizard)
    testless = DashboardApp(
        world, targets=CrashingReader(project, listing_crashes=False), launcher=FakeLauncher()
    )
    async with testless.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(testless, pilot, "sim")
        await set_target(testless, pilot, wizard, "--target", "sim_fifo")
        await open_picker(testless, pilot, wizard, "--test")
        assert "Tests unavailable: RuntimeError: tests boom" in stage_hint(wizard)
        assert "Loading" not in stage_hint(wizard)


# --- reads that land late, and worktrees --------------------------------------------


class GatedReader(TargetsReader):
    """A reader that holds back the listing of a worktree or the Test Runs of a Target."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gates: dict[str, threading.Event] = {}  # worktree name or selector -> release

    def _wait(self, key: str) -> None:
        gate = self.gates.get(key)
        if gate is not None:
            assert gate.wait(timeout=60)

    def listing(self, worktree: Path) -> TargetListing:
        self._wait(worktree.name)
        return super().listing(worktree)

    def test_runs(self, worktree: Path, selector: str) -> RunChoices:
        self._wait(selector)
        return super().test_runs(worktree, selector)

    def release_all(self) -> None:
        for gate in self.gates.values():
            gate.set()


@pytest.mark.asyncio
async def test_a_read_that_lands_after_the_request_changed_is_dropped(project):
    git(project, "worktree", "add", "-q", "-b", "wt-a", str(project.parent / "wt-a"))
    write_files(project.parent / "wt-a", {"alpha.core": core_text("alpha", "alpha_lint")})
    reader = GatedReader(project)
    try:
        await check_listing_of_another_worktree(project, reader)
        reader.gates.clear()
        await check_runs_of_another_target(project, reader)
    finally:
        reader.release_all()


async def check_listing_of_another_worktree(project: Path, reader: GatedReader) -> None:
    """The main checkout's Targets are held; the human moves on to the other worktree."""
    app = DashboardApp(world, targets=reader, launcher=FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        reader.gates["main"] = threading.Event()
        wizard = await start_run(app, pilot)
        await pilot.press("enter", *["down"] * FLOW_NAMES.index("lint"), "enter")
        await until(pilot, lambda: not wizard._building, "the form is built")
        await goto_row(pilot, wizard, "--target")
        await pilot.press("enter")
        assert stage_hint(wizard) == "Loading Targets…"
        assert pick_rows(wizard) == ["automatic"]
        await pilot.press("escape", "escape", "escape", "up", "enter", "enter")
        await until(pilot, lambda: not wizard._building, "the other form is built")
        reader.release_all()  # the main checkout's read finishes now, too late
        await settle(app, pilot)
        await open_picker(app, pilot, wizard, "--target")
        assert pick_rows(wizard) == ["automatic", "alpha_lint"]


async def check_runs_of_another_target(project: Path, reader: GatedReader) -> None:
    """The Test Runs of a Target the human has left are never shown for the new one."""
    # Imported here so a Dashboard without the Test Run read fails the test body, not collection.
    from booley.harness.dashboard.targets import RunChoices, TargetListing, TargetListRow

    app = DashboardApp(world, targets=reader, launcher=FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")
        await set_target(app, pilot, wizard, "--target", "sim_fifo")
        reader.gates["sim_fifo"] = threading.Event()
        await goto_row(pilot, wizard, "--test")
        await pilot.press("enter")
        assert stage_hint(wizard) == "Loading Test Runs…"
        await pilot.press("escape")
        await goto_row(pilot, wizard, "--target")
        await pilot.press("enter")
        await press_pick(pilot, wizard, "sim_wide")
        await open_picker(app, pilot, wizard, "--test")
        reader.release_all()
        await settle(app, pilot)
        assert pick_rows(wizard) == ["[ ] wide", "[ ] narrow"]
        # A finished read of a request that is no longer wanted changes nothing, even when
        # its result reaches the wizard (the worker had finished before it was replaced).
        stale_runs = RunChoices(project, "sim_fifo", ("stale",))
        other = TargetListing(project.parent / "wt-a", (TargetListRow("x", "x", "c", ()),))
        for stale in (stale_runs, other):
            event = SimpleNamespace(
                state=_success(), worker=SimpleNamespace(result=stale), stop=lambda: None
            )
            wizard._read_finished(event)
        assert pick_rows(wizard) == ["[ ] wide", "[ ] narrow"]
        assert wizard._listing is not None and wizard._listing.worktree == project


def _success():
    from textual.worker import WorkerState

    return WorkerState.SUCCESS


@pytest.mark.asyncio
async def test_picks_do_not_carry_over_to_another_worktree(project):
    wt_a, wt_b = project.parent / "wt-a", project.parent / "wt-b"
    git(project, "worktree", "add", "-q", "-b", "wt-a", str(wt_a))
    git(project, "worktree", "add", "-q", "-b", "wt-b", str(wt_b))
    write_files(wt_a, {"alpha.core": core_text("alpha", "alpha_lint").replace("lint", "sim")})
    app = make_app(project, FakeLauncher())
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_form(app, pilot, "sim")  # the main checkout, listed last
        await set_target(app, pilot, wizard, "--target", "sim_fifo")
        await set_tests(app, pilot, wizard, "--test", ["reset"])
        assert row_value(wizard, "--test") == "reset"
        await pilot.press("escape", "escape", "up", "up", "enter", "enter")  # wt-a, listed first
        await settle(app, pilot)
        assert row_value(wizard, "--target") == "automatic"
        assert row_value(wizard, "--test") == "default suite"
        await open_picker(app, pilot, wizard, "--target")
        assert pick_rows(wizard) == ["automatic", "alpha_sim"]
        await pilot.press("escape", "escape", "escape", "down", "enter", "enter")  # wt-b: no cores
        await settle(app, pilot)
        await open_picker(app, pilot, wizard, "--target")
        assert pick_rows(wizard) == ["automatic"]
        assert "No Target here can be driven by this Flow" in stage_hint(wizard)

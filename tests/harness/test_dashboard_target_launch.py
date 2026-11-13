"""Launching a Flow from a Target's detail (#2303): the Flows list, the preset form, Esc back.

Pilots drive the real Dashboard over a real Git repository: the main checkout holds no
cores, the linked worktree ``wt-a`` holds the Targets, so a wizard that used its own
preselected worktree would find none. Only the launcher is faked (it records submits).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.widgets import DataTable, OptionList

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard.run_form import NO_FORM, FormError
from booley.harness.dashboard.topbar import TopBar
from booley.harness.dashboard.wizard import RunWizard
from tests.harness.test_dashboard_run import (
    PICKER_FILES,
    FakeLauncher,
    check_arguments,
    goto_row,
    make_app,
    open_picker,
    pick_rows,
    review_command,
    row_key,
    row_value,
    settle_form,
    shown,
)
from tests.harness.test_dashboard_targets import (
    detail_text,
    footer,
    git,
    init_repository,
    open_targets,
    pick_worktree,
    settle,
    world,
    write_files,
)

if TYPE_CHECKING:
    from booley.harness.dashboard.app import DashboardApp

# ``odd_target`` has a default tool no Flow knows: it lists but nothing can drive it.
# ``dual_target`` has Verilator and no Flow of its own: the Lint and Simulation Flows both drive it.
UNDRIVEN_CORE = """CAPI=2:
name: acme:ip:odd:1.0
filesets:
  rtl: {files: [fifo.v], file_type: verilogSource}
targets:
  odd_target:
    default_tool: nosuchtool
    filesets: [rtl]
    toplevel: fifo
  dual_target:
    default_tool: verilator
    filesets: [rtl]
    toplevel: fifo
"""
FLOWS_LABEL = "Run a Booley Flow:"
PRESET_TARGET = "The Target comes from the Target detail; Esc goes back to it."
SCROLL_FOOTER = "↑↓ PgUp/PgDn scroll · ↓ Flows to run · ←→ Sessions / Jobs / Targets · esc back"
LIST_FOOTER = "↑↓ move · ⏎ run · ←→ Sessions / Jobs / Targets · esc back"


@pytest.fixture(autouse=True)
def _quiet_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generous first read and no timer reads: the tests order every read themselves."""
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)
    monkeypatch.setattr(dashboard_app, "REFRESH_SECONDS", 3600)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The main checkout (no cores) with a linked worktree ``wt-a`` holding every Target."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    main = tmp_path / "main"
    init_repository(main)
    git(main, "worktree", "add", "-q", "-b", "wt-a", str(tmp_path / "wt-a"))
    write_files(tmp_path / "wt-a", {**PICKER_FILES, "odd.core": UNDRIVEN_CORE})
    return main


def worktree_a(checkout: Path) -> Path:
    return (checkout.parent / "wt-a").resolve()


def flow_ids(app: DashboardApp) -> list[str | None]:
    """The Flows the open Target's list offers, by option id."""
    options = app.query_one("#target-flows", OptionList)
    return [options.get_option_at_index(i).id for i in range(options.option_count)]


async def open_detail(app: DashboardApp, pilot, selector: str) -> None:
    """From the Targets list, open *selector*'s detail and let its read land."""
    table = app.query_one(DataTable)
    table.move_cursor(row=app.row_keys.index(selector), animate=False)
    await pilot.pause()
    await pilot.press("enter")
    await settle(app, pilot)
    assert app.navigation.view == "target"
    assert app.navigation.selected == selector


async def targets_of_wt_a(app: DashboardApp, pilot) -> None:
    """Open the Targets window on worktree ``wt-a``."""
    await open_targets(app, pilot)
    await pick_worktree(app, pilot, "wt-a")


async def launch_from_detail(app: DashboardApp, pilot, selector: str) -> RunWizard:
    """Open *selector*'s detail, step into its Flow list and choose the first Flow."""
    await open_detail(app, pilot, selector)
    await pilot.press("down", "enter")
    await settle_form(app, pilot)
    assert isinstance(app.screen, RunWizard), "the Flow list did not open the Run wizard"
    return app.screen


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_detail_lists_exactly_the_flows_that_drive_it(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        for selector, flows in [
            ("sim_fifo", ["sim"]),
            ("synth_fifo", ["synth"]),
            ("lint_fifo", ["lint"]),
            ("fpga_fifo", ["fpga"]),
            ("dual_target", ["lint", "sim"]),
        ]:
            await open_detail(app, pilot, selector)
            assert flow_ids(app) == flows
            assert app.query_one("#target-flows").display
            assert detail_text(app).splitlines()[-1] == FLOWS_LABEL
            await pilot.press("escape")
        await open_detail(app, pilot, "odd_target")
        assert flow_ids(app) == []
        assert not app.query_one("#target-flows").display
        assert FLOWS_LABEL not in detail_text(app)


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_choosing_a_flow_opens_the_form_with_worktree_and_target_filled_in(checkout):
    wt_a = worktree_a(checkout)
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        for selector, flow in [("sim_wide", "sim"), ("synth_fifo", "synth")]:
            wizard = await launch_from_detail(app, pilot, selector)
            assert wizard.query_one("#run-form", OptionList).display
            assert shown(wizard, "#run-title") == f"booley flow {flow} in wt-a"
            assert row_value(wizard, "--target") == selector
            # Enter on the Target row does not open the picker: the Target comes from the detail.
            await goto_row(pilot, wizard, "--target")
            await pilot.press("enter")
            await settle(app, pilot)
            assert wizard.query_one("#run-form", OptionList).display
            assert not wizard.query_one("#run-picks", OptionList).display
            assert shown(wizard, "#run-error") == PRESET_TARGET
            assert row_value(wizard, "--target") == selector
            if flow == "sim":  # the Test Runs picker reads the preset Target's runs in wt-a
                await open_picker(app, pilot, wizard, "--test")
                assert [row.split()[-1] for row in pick_rows(wizard)] == ["wide", "narrow"]
                await pilot.press("escape")
            await review_command(app, pilot, wizard)
            command = f"cd {wt_a} && booley flow {flow} --target {selector}"
            assert shown(wizard, "#run-command") == command
            # Review, the form, then the detail, then the Targets list for the next Target.
            await pilot.press("escape", "escape", "escape")
            await pilot.pause()
            assert app.navigation.view == "targets"


class NoTargetField(FakeLauncher):
    """A launcher whose Flow form has no Target field to hold the preset Target."""

    def form(self, flow):
        return NO_FORM


class BrokenForm(FakeLauncher):
    """A launcher whose form cannot be built."""

    def form(self, flow):
        raise FormError("an option has no row")


@pytest.mark.asyncio
async def test_a_target_launched_form_that_cannot_be_built_keeps_the_target(checkout):
    app = make_app(checkout, BrokenForm())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        wizard = await launch_from_detail(app, pilot, "sim_wide")
        assert "The form cannot be built" in shown(wizard, "#run-note")
        assert row_value(wizard, "Extra arguments") == "--target sim_wide"


@pytest.mark.asyncio
async def test_target_detail_scrolls_by_arrows_and_reaches_the_flows_at_its_end(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(100, 10)) as pilot:
        await targets_of_wt_a(app, pilot)
        await open_detail(app, pilot, "sim_fifo")
        aside = app.query_one("#aside")
        flows = app.query_one("#target-flows", OptionList)
        assert app.focused is aside
        assert aside.scroll_y == 0
        assert aside.max_scroll_y > 0
        assert footer(app) == SCROLL_FOOTER
        await pilot.press("down")
        await pilot.pause()
        assert aside.scroll_y > 0
        assert app.focused is aside
        while aside.scroll_y < aside.max_scroll_y:
            await pilot.press("down")
            await pilot.pause()
            assert app.focused is aside
        offset = aside.scroll_y
        await pilot.press("down")  # past the end: the Flow list takes the keyboard
        await pilot.pause()
        assert app.focused is flows
        assert flows.highlighted == 0
        assert footer(app) == LIST_FOOTER
        await pilot.press("up")  # first Flow: back to the detail, same offset
        await pilot.pause()
        assert app.focused is aside
        assert aside.scroll_y == offset
        while aside.scroll_y > 0:
            assert app.focused is aside
            await pilot.press("up")
            await pilot.pause()
        await pilot.press("up")  # at the top: the top bar
        assert isinstance(app.focused, TopBar)


@pytest.mark.asyncio
async def test_a_second_flow_is_chosen_with_the_arrows_and_up_walks_back_to_the_first(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        await open_detail(app, pilot, "dual_target")
        flows = app.query_one("#target-flows", OptionList)
        await pilot.press("down", "down")
        assert app.focused is flows
        assert flows.highlighted == 1
        await pilot.press("up")
        assert app.focused is flows
        assert flows.highlighted == 0
        await pilot.press("down")
        # A refresh of the same Target keeps the keyboard and the highlight on the second Flow.
        await app.refresh_snapshot()
        await pilot.pause()
        assert app.focused is flows
        assert flows.highlighted == 1
        await pilot.press("enter")
        await settle_form(app, pilot)
        assert isinstance(app.screen, RunWizard)
        assert shown(app.screen, "#run-title") == "booley flow sim in wt-a"
        assert row_value(app.screen, "--target") == "dual_target"


@pytest.mark.asyncio
async def test_a_form_without_a_target_field_gets_the_target_in_extra_arguments(checkout):
    app = make_app(checkout, NoTargetField())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        wizard = await launch_from_detail(app, pilot, "sim_wide")
        assert row_value(wizard, "Extra arguments") == "--target sim_wide"


# --- AC3 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esc_from_a_target_launched_form_returns_to_the_target_detail(checkout):
    app = make_app(checkout, FakeLauncher())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        await open_detail(app, pilot, "sim_wide")
        before = {name: list(history) for name, history in app.windows.items()}
        await pilot.press("down", "enter")
        await settle_form(app, pilot)
        wizard = app.screen
        assert isinstance(wizard, RunWizard)
        await goto_row(pilot, wizard, "Extra arguments")
        await pilot.press("enter")  # the editor
        assert wizard.query_one("#run-edit").display
        await pilot.press("escape")
        assert wizard.query_one("#run-form", OptionList).display
        await pilot.press("escape")  # the form: no worktree or Flow step behind it
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.navigation.view == "target"
        assert app.navigation.selected == "sim_wide"
        assert app.windows == before
        assert app.focused is app.query_one("#target-flows")
        assert app.query_one("#target-flows", OptionList).highlighted == 0


@pytest.mark.asyncio
async def test_submit_from_a_target_selects_the_job_and_esc_returns_to_the_detail(checkout):
    wt_a = worktree_a(checkout)
    key = row_key(wt_a, "run-1")
    launcher = FakeLauncher(key)
    app = make_app(checkout, launcher, world())
    async with app.run_test(size=(130, 40)) as pilot:
        await targets_of_wt_a(app, pilot)
        wizard = await launch_from_detail(app, pilot, "sim_wide")
        await check_arguments(app, pilot, wizard, "")
        await pilot.press("enter")  # Submit is highlighted
        await settle(app, pilot)
        assert launcher.submitted == [(wt_a, "sim", ("--target", "sim_wide"))]
        assert app.window == "jobs"
        assert app.navigation.selected == key
        await pilot.press("escape")
        await pilot.pause()
        assert app.window == "targets"
        assert app.navigation.view == "target"
        assert app.navigation.selected == "sim_wide"
        assert app.focused is app.query_one("#aside")
        assert app.query_one("#target-flows").display

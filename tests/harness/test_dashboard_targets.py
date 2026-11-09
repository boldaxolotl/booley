"""The Dashboard's Targets window: Targets of a chosen worktree, their detail, navigation (#2301).

Each test drives the window through a pilot over real Git repositories and
real ``.core`` files, so it fails in its body on a Dashboard without the window.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import DataTable, Static

from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot
from booley.harness.dashboard.topbar import TopBar
from booley.mcp.session_registry import Attribution, RecentCall, SessionRow
from tests.harness.test_dashboard import STAMP, open_session, snapshot
from tests.harness.test_dashboard_keys import assert_retired_keys_change_nothing

FIFO_CORE = """CAPI=2:
name: acme:ip:fifo:1.0
filesets:
  rtl: {files: [fifo.v], file_type: verilogSource}
  tb: {files: [tb.sv], file_type: systemVerilogSource}
  constraints: {files: [fifo.sdc], file_type: SDC}
targets:
  sim_fifo:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb
  synth_fifo:
    default_tool: yosys
    filesets: [rtl, constraints]
    toplevel: fifo
  lint_fifo:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: fifo
"""
# One Test with two Test Variants, one Test the default suite skips.
TESTS_TOML = """[sim_fifo]
select = "+test={name}"
tests = ["reset", "stress", "slow"]
skip = ["slow"]

[sim_fifo.variants.stress]
default = "short"
short = ["+iters=10"]
soak = ["+iters=1000"]
"""
FIFO_ROWS = [
    ["lint_fifo", "acme:ip:fifo:1.0", "lint"],
    ["sim_fifo", "acme:ip:fifo:1.0", "sim"],
    ["synth_fifo", "acme:ip:fifo:1.0", "synth"],
]


def core_text(name: str, target: str) -> str:
    """A small lint core ``acme:ip:<name>`` with one Target."""
    return (
        f"CAPI=2:\nname: acme:ip:{name}:1.0\n"
        "filesets:\n  rtl: {files: [top.v], file_type: verilogSource}\n"
        f"targets:\n  {target}:\n    flow: lint\n    flow_options: {{tool: verilator}}\n"
        "    filesets: [rtl]\n    toplevel: top\n"
    )


def git(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test"]
    subprocess.run(
        ["git", *identity, "-C", str(root), *args], check=True, capture_output=True, timeout=60
    )


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())


def init_repository(root: Path) -> None:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "initial")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main checkout ``main`` plus linked worktrees ``wt-a`` (alpha), ``wt-b`` (beta),
    ``wt-c`` (no core) and a prunable ``gone``."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)
    main = tmp_path / "main"
    init_repository(main)
    for name in ("wt-a", "wt-b", "wt-c", "gone"):
        git(main, "worktree", "add", "-q", "-b", name, str(tmp_path / name))
    shutil.rmtree(tmp_path / "gone")
    write_files(
        main,
        {
            "fifo.core": FIFO_CORE,
            "fifo.v": "module fifo; endmodule\n",
            "tb.sv": "module tb; endmodule\n",
            "fifo.sdc": "# constraints\n",
            ".booley/booley.toml": "[project]\n",
            ".booley/tests.toml": TESTS_TOML,
        },
    )
    write_files(tmp_path / "wt-a", {"alpha.core": core_text("alpha", "alpha_lint")})
    write_files(tmp_path / "wt-b", {"beta.core": core_text("beta", "beta_lint")})
    return main


def session(handle: str, work_dir: Path | str, default: Path | str = "") -> SessionRow:
    """A connected session whose handle is *handle* (``S3``); the key is ``codex:03``."""
    number = int(handle[1:])
    attribution = Attribution(
        f"codex:{number:02}",
        "thread",
        str(work_dir),
        f"wk{number}",
        default_work_dir=str(default),
    )
    return SessionRow(attribution, STAMP, STAMP, (RecentCall(STAMP, "sim", "completed"),))


def world(*rows: SessionRow):
    """A snapshot with exactly these sessions and no Jobs."""
    handles = {row.attribution.key: f"S{int(row.attribution.key[-2:])}" for row in rows}
    return replace(snapshot(0), sessions=rows, session_handles=handles, jobs=JobSnapshot(()))


def make_app(root: Path, current) -> DashboardApp:
    from booley.harness.dashboard.targets import TargetsReader

    return DashboardApp(lambda: current, targets=TargetsReader(root))


async def settle(app: DashboardApp, pilot) -> None:
    """Wait for the Targets reads and for the results to be applied (a read can start another)."""
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def open_targets(app: DashboardApp, pilot) -> None:
    await pilot.press("right", "right")
    await settle(app, pilot)
    assert app.window == "targets"


def rows_of(app: DashboardApp) -> list[list[str]]:
    table = app.query_one(DataTable)
    return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


def selectors(app: DashboardApp) -> list[str]:
    return list(app.row_keys)


def bar_labels(app: DashboardApp) -> list[str]:
    return [label for label, _, _ in app.query_one(TopBar)._items()]


def detail_text(app: DashboardApp) -> str:
    return str(app.query_one("#detail", Static).render())


def empty_text(app: DashboardApp) -> str:
    return str(app.query_one("#empty", Static).render())


def footer(app: DashboardApp) -> str:
    return str(app.query_one("#footer", Static).render())


async def pick_worktree(app: DashboardApp, pilot, name: str) -> None:
    """Open the worktree list from the top bar and choose the worktree called *name*."""
    await pilot.press("up", "right")
    await pilot.press("enter")
    await pilot.pause()
    assert app.navigation.view == "worktrees"
    table = app.query_one(DataTable)
    row = next(index for index, key in enumerate(app.row_keys) if Path(key).name == name)
    table.move_cursor(row=row, animate=False)
    await pilot.pause()
    await pilot.press("enter")
    await settle(app, pilot)


# --- AC1 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targets_window_lists_targets_with_core_and_drivable_flows(repo):
    app = make_app(repo, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        assert app.navigation.view == "targets"
        assert rows_of(app) == FIFO_ROWS
        assert "Worktree: main" in bar_labels(app)


# --- AC2 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_detail_shows_filesets_tests_variants_and_constraints(repo):
    before = sorted(path for path in repo.rglob("*") if ".git" not in path.parts)
    app = make_app(repo, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        for name, expected in (
            (
                "sim_fifo",
                [
                    "Core: acme:ip:fifo:1.0 (fifo.core)",
                    "Flows: sim",
                    "Filesets: rtl, tb",
                    "Constraints: none",
                    "Tests:",
                    "  reset",
                    "  stress: short (default), soak",
                    "  slow (skipped by default)",
                ],
            ),
            (
                "synth_fifo",
                [
                    "Core: acme:ip:fifo:1.0 (fifo.core)",
                    "Flows: synth",
                    "Filesets: rtl, constraints",
                    "Constraints: fifo.sdc",
                    "Tests:",
                    "  only Simulation Targets have tests",
                ],
            ),
        ):
            app.query_one(DataTable).move_cursor(row=selectors(app).index(name), animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await settle(app, pilot)
            assert app.navigation.view == "target"
            assert detail_text(app).splitlines() == expected
            await pilot.press("escape")
            await pilot.pause()
    # The window only reads: it created no projection, registry or build directory.
    assert sorted(path for path in repo.rglob("*") if ".git" not in path.parts) == before


# --- AC3 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_list_orders_sessions_first_and_main_last(repo):
    tmp = repo.parent
    current = world(session("S1", repo), session("S10", tmp / "wt-a"), session("S3", tmp / "wt-b"))
    app = make_app(repo, current)
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        await pilot.press("up", "right", "enter")
        await pilot.pause()
        assert app.navigation.view == "worktrees"
        assert rows_of(app) == [
            ["wt-b · S3 Interactive"],
            ["wt-a · S10 Interactive"],
            ["wt-c"],
            ["main · main checkout · S1 Interactive"],
        ]


@pytest.mark.asyncio
async def test_choosing_a_worktree_reloads_the_list(repo):
    app = make_app(repo, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        assert selectors(app) == ["lint_fifo", "sim_fifo", "synth_fifo"]
        await pick_worktree(app, pilot, "wt-a")
        assert app.windows["targets"] == [app.navigation]
        assert app.navigation.view == "targets"
        assert selectors(app) == ["alpha_lint"]
        assert "Worktree: wt-a" in bar_labels(app)
        # The choice is kept when the human leaves the window and comes back.
        await pilot.press("left", "left", "right", "right")
        await settle(app, pilot)
        assert selectors(app) == ["alpha_lint"]


# --- AC4 ---------------------------------------------------------------------


def three_sessions(tmp: Path, main: Path, **overrides: SessionRow):
    rows = {
        "S1": session("S1", main),
        "S2": session("S2", tmp / "wt-a"),
        "S3": session("S3", tmp / "wt-b"),
    }
    return world(*{**rows, **overrides}.values())


PRESELECTION_CASES = {
    # (how the Sessions window is left, worktree whose Targets are listed)
    "cursor-on-session": ("cursor:S3", "beta_lint"),
    "open-session-detail": ("open:S2", "alpha_lint"),
    "default-checkout-only": ("cursor:S2", "alpha_lint"),
    "unregistered-checkout": ("cursor:S2", "sim_fifo"),
    "no-sessions": ("", "sim_fifo"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", PRESELECTION_CASES)
async def test_preselects_highlighted_session_worktree_else_main(repo, case):
    tmp = repo.parent
    action, expected_target = PRESELECTION_CASES[case]
    current = three_sessions(tmp, repo)
    if case == "default-checkout-only":
        current = three_sessions(tmp, repo, S2=session("S2", "", default=tmp / "wt-a"))
    elif case == "unregistered-checkout":
        current = three_sessions(tmp, repo, S2=session("S2", tmp / "elsewhere"))
    elif case == "no-sessions":
        current = world()
    app = make_app(repo, current)
    async with app.run_test(size=(130, 30)) as pilot:
        kind, _, handle = action.partition(":")
        key = f"codex:{int(handle[1:]):02}" if handle else ""
        if kind == "open":
            await open_session(app, pilot, key)
            assert app.navigation.view == "session"
        elif kind == "cursor":
            app.query_one(DataTable).move_cursor(row=app.row_keys.index(key), animate=False)
            await pilot.pause()
        await pilot.press("right", "right")
        await settle(app, pilot)
        assert app.window == "targets"
        assert expected_target in selectors(app)


@pytest.mark.asyncio
async def test_preselection_change_resets_open_detail(repo):
    tmp = repo.parent
    app = make_app(repo, three_sessions(tmp, repo))
    async with app.run_test(size=(130, 30)) as pilot:
        app.query_one(DataTable).move_cursor(row=app.row_keys.index("codex:03"), animate=False)
        await pilot.pause()
        await open_targets(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.navigation.view == "target"
        assert app.navigation.selected == "beta_lint"
        await pilot.press("left", "left")
        app.query_one(DataTable).move_cursor(row=app.row_keys.index("codex:02"), animate=False)
        await pilot.pause()
        await open_targets(app, pilot)
        assert app.navigation.view == "targets"
        assert selectors(app) == ["alpha_lint"]
        assert len(app.windows["targets"]) == 1


# --- AC5 ---------------------------------------------------------------------


def stealth_project(root: Path) -> None:
    write_files(
        root,
        {
            ".booley/booley.toml": "[project]\n\n[stealth]\nenabled = true\n",
            ".booley/cores/stealth.core": core_text("stealth", "stealth_lint"),
            "top.v": "module top; endmodule\n",
        },
    )


SIM_CORE = """CAPI=2:
name: acme:ip:sim:1.0
filesets:
  rtl: {files: [top.v], file_type: verilogSource}
targets:
  sim_top:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: top
"""

UNRESOLVABLE_CASES = {
    "missing-dependency": (
        {"sim.core": SIM_CORE.replace("filesets:", "depend: [acme:ip:missing]\nfilesets:", 1)},
        ["Filesets: rtl", "Inputs unavailable: ", "Tests unavailable: "],
    ),
    "bad-tests-toml": (
        {"sim.core": SIM_CORE, ".booley/tests.toml": '[sim_top]\ntests = ["two words"]\n'},
        ["Filesets: rtl", "Constraints: none", "Tests: unavailable", "Tests unavailable: "],
    ),
    "projection-missing": (
        None,
        ["Constraints: unavailable", "Inputs unavailable: core projections need a refresh"],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", UNRESOLVABLE_CASES)
async def test_unresolvable_target_stays_listed_and_detail_explains_why(
    tmp_path, monkeypatch, case
):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)
    root = tmp_path / "project"
    init_repository(root)
    files, expected = UNRESOLVABLE_CASES[case]
    if files is None:
        stealth_project(root)
    else:
        write_files(root, {"top.v": "module top; endmodule\n", **files})
    before = sorted(path for path in root.rglob("*") if ".git" not in path.parts)
    app = make_app(root, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        assert len(selectors(app)) == 1
        await pilot.press("enter")
        await settle(app, pilot)
        text = detail_text(app)
        for fragment in expected:
            assert fragment in text, text
    assert sorted(path for path in root.rglob("*") if ".git" not in path.parts) == before


@pytest.mark.asyncio
async def test_empty_worktree_names_it(repo):
    app = make_app(repo, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        await pick_worktree(app, pilot, "wt-c")
        assert selectors(app) == []
        assert empty_text(app) == f"No Targets in wt-c ({repo.parent / 'wt-c'})."


@pytest.mark.asyncio
async def test_failed_listing_and_failed_git_are_explained(tmp_path, monkeypatch):
    monkeypatch.setattr("booley.harness.dashboard.app.READ_BUDGET", 30.0)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "plain"  # Not a Git repository: the worktree list fails.
    write_files(root, {"top.v": "module top; endmodule\n"})
    (root / "bad.core").write_bytes(b"CAPI=2:\nname: [\n")
    app = make_app(root, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        # Without worktrees the window falls back to the Project root.
        assert "Worktree: plain" in bar_labels(app)
        assert empty_text(app) == f"No Targets in plain ({root})."
        await pilot.press("up", "right", "enter")
        await pilot.pause()
        assert empty_text(app).startswith("Worktrees unavailable: ")


@pytest.mark.asyncio
async def test_reader_failures_and_missing_reader_are_explained(repo):
    from booley.harness.dashboard.targets import TargetsReader

    class Exploding(TargetsReader):
        def listing(self, worktree):
            raise RuntimeError("boom")

    app = DashboardApp(world, targets=Exploding(repo))
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        assert empty_text(app) == "Targets unavailable in main: boom"
    bare = DashboardApp(world)
    async with bare.run_test(size=(130, 30)) as pilot:
        await open_targets(bare, pilot)
        assert empty_text(bare) == "Targets unavailable: no Project root"


# --- AC6 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_targets_navigation_uses_arrows_enter_and_escape_only(repo):
    app = make_app(repo, world())
    async with app.run_test(size=(130, 30)) as pilot:
        await open_targets(app, pilot)
        await pilot.press("right")
        assert app.window == "targets"
        assert footer(app) == "↑↓ move · ⏎ open · ←→ Sessions / Jobs / Targets · ↑ worktree"
        await assert_retired_keys_change_nothing(app, pilot, "Targets list")
        # Up reaches the top bar, right moves to the Worktree item, Enter opens the list.
        await pilot.press("up", "right")
        bar = app.query_one(TopBar)
        assert bar._items()[bar.cursor][1] == "worktree"
        await pilot.press("enter")
        await pilot.pause()
        assert app.navigation.view == "worktrees"
        assert footer(app) == "↑↓ move · ⏎ open · ←→ Sessions / Jobs / Targets · esc back"
        await pilot.press("escape")
        assert app.navigation.view == "targets"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.navigation.view == "target"
        assert footer(app) == "↑↓ PgUp/PgDn scroll · ←→ Sessions / Jobs / Targets · esc back"
        await assert_retired_keys_change_nothing(app, pilot, "Target detail")
        await pilot.press("escape")
        assert app.navigation.view == "targets"
        await pilot.press("left", "left")
        assert app.window == "sessions"

"""The Run form's argument check (a child process running the shell's own parse) and shared fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from booley.mcp.registry import discover_mcp_tools
from booley.runtime import detached_job
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import scaled

FLOW_NAMES = sorted(info.name for info in discover_mcp_tools() if info.kind == "flow")
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


def git(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.test"]
    run_bounded_child(["git", *identity, "-C", str(root), *args], check=True)


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
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git repository with one commit: the main checkout of the Project."""
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


# What the Run form reads: one Target per Flow, two sim Targets with their own Tests.
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


def write_config(worktree: Path, text: str) -> None:
    (worktree / ".booley").mkdir(exist_ok=True)
    (worktree / ".booley" / "booley.toml").write_bytes(text.encode())


WORK_UNIT_TIMEOUT = "Timeout: 30m 00s per work unit"
ADMITTED_BY_MEMORY = "Admitted by memory: waits while its estimate does not fit the Memory Budget"


def test_check_resolves_relative_paths_in_the_worktree(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "tests.txt").write_bytes(b"reset\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert launch.check(worktree, "flow", "sim", ("--tests-file", "tests.txt")).error == ""
    assert (
        "tests.txt" in launch.check(elsewhere, "flow", "sim", ("--tests-file", "tests.txt")).error
    )


def test_check_changes_no_environment_and_writes_nothing(tmp_path, capfd):
    from booley.harness.dashboard import launch

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def listing() -> list[str]:
        # coverage's subprocess patch may drop .coverage* files in the child's cwd.
        return sorted(p.name for p in worktree.rglob("*") if not p.name.startswith(".coverage"))

    environment = dict(os.environ)
    # --timeout-ms is a deprecated alias: an in-process parse would warn here and set the environment.
    assert launch.check(worktree, "flow", "lint", ("--timeout-ms", "1000")).error == ""
    assert dict(os.environ) == environment
    assert listing() == []
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""


def test_check_reports_a_timeout_and_a_worktree_that_cannot_start_it(tmp_path, monkeypatch):
    from booley.harness.dashboard import launch

    timeout_s = scaled(0.001)
    monkeypatch.setattr(launch, "CHECK_TIMEOUT_S", timeout_s)
    assert (
        launch.check(tmp_path, "flow", "lint", ()).error
        == f"argument check timed out after {timeout_s}s"
    )
    message = launch.check(tmp_path / "missing", "flow", "lint", ()).error
    assert "No such file or directory" in message or "cannot find" in message.lower()
    assert "\n" not in message


def test_child_main_rejects_selector_and_help(tmp_path, monkeypatch, capsys):
    from booley.harness.dashboard import launch

    monkeypatch.chdir(tmp_path)
    assert launch.main(["flow", "lint", "--", "--target", "t"]) == 0
    assert launch.main(["flow", "lint", "--", "--", "--target", "t"]) == 0
    for typed, message in (
        (["-C", "x"], "the worktree chooses the Project"),
        (["--project", "x"], "the worktree chooses the Project"),
        (["-h"], "--help shows help; it is not a run"),
        (["--bogus"], "unrecognized arguments: --bogus"),
    ):
        capsys.readouterr()
        with pytest.raises(SystemExit) as exit_info:
            launch.main(["flow", "lint", "--", *typed])
        assert exit_info.value.code == 2
        assert message in capsys.readouterr().err


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
    result = launch.check(tmp_path, "flow", flow, ("--target", "t", *argv))
    facts = result.facts
    assert result.error == ""
    assert (facts.job_class, facts.dry_run, facts.timeout_ms) == expected


def test_check_reports_a_configured_timeout_the_flow_rejects(tmp_path):
    from booley.harness.dashboard import launch

    write_config(tmp_path, "[flows.synth]\ntimeout_ms = -5\n")
    facts = launch.check(tmp_path, "flow", "synth", ("--target", "t")).facts
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


def test_error_text_falls_back_to_the_exit_code():
    from booley.harness.dashboard import launch

    assert launch._error_text("", 3) == "argument check failed (exit 3)"

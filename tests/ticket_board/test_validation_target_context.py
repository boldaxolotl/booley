"""Regressions for Target context shared by Ticket validation callers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.harness._ticket_ops import DirectTicketOps
from booley.harness.setup.intake import _resolve_and_validate
from booley.runtime.project_dir import reset_cache
from booley.ticket_board.cli import main
from booley.ticket_board.io import TicketIO


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=30
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> tuple[Path, TicketIO]:
    root = tmp_path / "project"
    fixture = Path(__file__).parents[1] / "fixtures" / "ticket_mode_smoke"
    shutil.copytree(fixture, root)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    reset_cache()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", ".")
    _git(root, "add", "-f", ".booley_project/booley.toml", ".booley_project/.gitignore")
    _git(root, "commit", "-qm", "baseline")
    board = TicketIO(root / ".booley_project" / "tickets", project_root=root)
    monkeypatch.setenv("TICKETS_DIR", str(board.tickets_dir))
    yield root, board
    reset_cache()


def _ticket(target: str, *, dependency: str = "") -> str:
    depends = f"dependencies: [{dependency}]\n" if dependency else ""
    return (
        "---\nsummary: Check Target context\ntype: feature\nbranch: main\n"
        f"scope: [README.md]\n{depends}on_success: [merge]\n"
        f"CRITERIA_MANDATORY:\n  LINT: {{{target}: clean}}\n"
        "---\n\n## Description\n\nCheck the Target.\n"
    )


def _provider(root: Path, board: TicketIO) -> Path:
    path = board.create_ticket_document("provider", _ticket("lint_future (new)"))
    assert path is not None
    core = next((root / ".booley_project/worktrees/provider").glob("*.core"))
    with core.open("a", encoding="utf-8") as stream:
        stream.write(
            "  lint_future:\n    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n    toplevel: dut\n"
        )
    assert board.enqueue_ticket("provider")
    return board.tickets_dir / "board/queue/provider.md"


def _assert_both_valid(root: Path, path: Path, capsys) -> None:
    capsys.readouterr()
    assert main(["validate-ticket", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert DirectTicketOps().validate_ticket(root, str(path)) == {
        "errors": [],
        "valid": True,
    }


def _assert_both_reject(root: Path, path: Path, capsys, expected: str) -> None:
    capsys.readouterr()
    assert main(["validate-ticket", str(path)]) == 1
    assert expected in " ".join(json.loads(capsys.readouterr().out)["errors"])
    assert expected in " ".join(DirectTicketOps().validate_ticket(root, str(path))["errors"])


def test_published_new_target_uses_pinned_authoring_checkout(project, capsys) -> None:
    root, board = project
    path = _provider(root, board)
    shutil.rmtree(root / ".booley_project/worktrees/provider")

    _assert_both_valid(root, path, capsys)


def test_published_simulation_target_binds_all_required_criteria(project, capsys) -> None:
    root, board = project
    ticket = (
        "---\nsummary: Check simulation Target\ntype: feature\nbranch: main\n"
        "scope: [README.md]\non_success: [merge]\nCRITERIA_MANDATORY:\n"
        "  ELAB: {sim_future (new): pass}\n"
        "  SIM: {sim_future (new): {smoke: pass}}\n"
        "  CYCLE_COUNT: {sim_future (new): {smoke: {cycle_count_max: 100}}}\n"
        "---\n\n## Description\n\nCheck the simulation Target.\n"
    )
    path = board.create_ticket_document(
        "simulation", ticket.replace("sim_future", "sim_smoke").replace(" (new)", "")
    )
    assert path is not None
    path.write_text(ticket, encoding="utf-8")
    workspace = root / ".booley_project/worktrees/simulation"
    core = next(workspace.glob("*.core"))
    with core.open("a", encoding="utf-8") as stream:
        stream.write(
            "  sim_future:\n    flow: sim\n"
            "    flow_options: {tool: icarus, iverilog_options: [-g2012]}\n"
            "    filesets: [rtl, tb_pass]\n    toplevel: tb_dut\n"
        )
    tests = workspace / ".booley_project/tests.toml"
    with tests.open("a", encoding="utf-8") as stream:
        stream.write("\n[sim_future]\ntests = ['smoke']\n")
    assert board.enqueue_ticket("simulation")
    queued = board.tickets_dir / "board/queue/simulation.md"
    shutil.rmtree(workspace)

    _assert_both_valid(root, queued, capsys)
    assert _resolve_and_validate(root, str(queued)) == (queued, "simulation")


def test_draft_provider_target_is_not_consumer_authored(project, capsys) -> None:
    root, board = project
    _provider(root, board)
    draft = _ticket("lint_future", dependency="provider")
    path = board.create_ticket_document(
        "consumer",
        draft.replace("LINT: {lint_future: clean}", "REVIEW: {rtl: {bugs: clean}}"),
    )
    assert path is not None
    path.write_text(draft, encoding="utf-8")
    before = (root / ".booley_project/.runtime/acceptance/drafts/consumer.json").read_bytes()
    marker = root / ".booley_project/.runtime/acceptance/providers/consumer"
    marker_before = {entry.name: entry.read_bytes() for entry in marker.iterdir()}

    _assert_both_valid(root, path, capsys)
    assert (root / ".booley_project/.runtime/acceptance/drafts/consumer.json").read_bytes() == before
    assert {entry.name: entry.read_bytes() for entry in marker.iterdir()} == marker_before
    assert board.enqueue_ticket("consumer")
    assert (board.tickets_dir / "board/waiting/consumer.md").exists()


def _deferred_provider(root: Path, board: TicketIO) -> None:
    baseline_core = root / "ticket_mode_smoke.core"
    baseline_core.write_text(
        baseline_core.read_text(encoding="utf-8").replace("\ntargets:\n", "targets:\n", 1),
        encoding="utf-8",
    )
    _git(root, "add", "ticket_mode_smoke.core")
    _git(root, "commit", "-qm", "use compact core sections")
    provider = (
        "---\nsummary: Add deferred simulation Target\ntype: feature\nbranch: main\n"
        "scope: ['rtl/future.sv [new]']\non_success: [merge]\n"
        "CRITERIA_MANDATORY:\n  SIM: {sim_future (new): {smoke: pass}}\n"
        "---\n\n## Description\n\nAdd the simulation Target.\n"
    )
    path = board.create_ticket_document(
        "provider", provider.replace("sim_future (new)", "sim_smoke")
    )
    assert path is not None
    path.write_text(provider, encoding="utf-8")
    workspace = root / ".booley_project/worktrees/provider"
    core = next(workspace.glob("*.core"))
    content = core.read_text(encoding="utf-8")
    content = content.replace(
        "  constraints:\n",
        "  future:\n    files:\n"
        "      - rtl/future.sv: {file_type: systemVerilogSource}\n"
        "  constraints:\n",
        1,
    )
    core.write_text(
        content
        + "  sim_future:\n    flow: sim\n"
        "    flow_options: {tool: icarus, iverilog_options: [-g2012]}\n"
        "    filesets: [future, tb_pass]\n    toplevel: tb_dut\n",
        encoding="utf-8",
    )
    (workspace / "rtl/future.sv").touch()
    with (workspace / ".booley_project/tests.toml").open("a", encoding="utf-8") as stream:
        stream.write("\n[sim_future]\ntests = ['smoke']\n")
    assert board.enqueue_ticket("provider")


def test_draft_provider_test_table_and_missing_rtl_placeholder(project, capsys) -> None:
    root, board = project
    _deferred_provider(root, board)
    consumer = (
        "---\nsummary: Use exported simulation Target\ntype: feature\nbranch: main\n"
        "scope: [README.md]\ndependencies: [provider]\non_success: [merge]\n"
        "CRITERIA_MANDATORY:\n  SIM: {sim_future: {smoke: pass}}\n"
        "---\n\n## Description\n\nUse the exported Target.\n"
    )
    path = board.create_ticket_document(
        "consumer", consumer.replace("SIM: {sim_future: {smoke: pass}}", "REVIEW: {rtl: {bugs: clean}}")
    )
    assert path is not None
    path.write_text(consumer, encoding="utf-8")

    _assert_both_valid(root, path, capsys)
    assert board.enqueue_ticket("consumer")
    waiting = board.tickets_dir / "board/waiting/consumer.md"
    assert waiting.exists()
    _assert_both_valid(root, waiting, capsys)


def test_draft_validation_rejects_modified_provider_surface(project, capsys) -> None:
    root, board = project
    _provider(root, board)
    draft = _ticket("lint_future", dependency="provider")
    path = board.create_ticket_document(
        "consumer",
        draft.replace("LINT: {lint_future: clean}", "REVIEW: {rtl: {bugs: clean}}"),
    )
    assert path is not None
    path.write_text(draft, encoding="utf-8")
    core = next((root / ".booley_project/worktrees/consumer").glob("*.core"))
    content = core.read_text(encoding="utf-8")
    core.write_text(content.replace("toplevel: dut", "toplevel: changed"), encoding="utf-8")

    _assert_both_reject(root, path, capsys, "materialized provider Target")
    assert core.read_text(encoding="utf-8").endswith("toplevel: changed\n")


def test_published_validation_rejects_authored_document_drift(project, capsys) -> None:
    root, board = project
    path = _provider(root, board)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Check Target context", "Changed Target context"
        ),
        encoding="utf-8",
    )

    _assert_both_reject(root, path, capsys, "authored Ticket changed")


def test_published_validation_rejects_missing_pinned_commit(project, capsys) -> None:
    root, board = project
    path = _provider(root, board)
    content = path.read_text(encoding="utf-8")
    damaged = re.sub(r"(commit: )[0-9a-f]{40}", r"\g<1>" + "f" * 40, content, count=1)
    assert damaged != content
    path.write_text(damaged, encoding="utf-8")

    _assert_both_reject(root, path, capsys, "commit")


def test_published_git_check_uses_product_checkout(project, capsys) -> None:
    root, board = project
    path = _provider(root, board)
    (root / "README.md").write_text("uncommitted product edit\n", encoding="utf-8")

    _assert_both_valid(root, path, capsys)
    capsys.readouterr()
    assert main(["validate-ticket", str(path), "--check-git"]) == 1
    assert "Dirty working tree" in " ".join(json.loads(capsys.readouterr().out)["errors"])
    assert "Dirty working tree" in " ".join(
        DirectTicketOps().validate_ticket(root, str(path), check_git=True)["errors"]
    )

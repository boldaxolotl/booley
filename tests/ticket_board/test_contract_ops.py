"""Contract authoring worktree, sealing, and enqueue integration."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import contract_ops
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.operations import op_reset


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_core(root: Path, version: str = "1.0") -> None:
    (root / "toy.core").write_text(
        "\n".join(
            [
                "CAPI=2:",
                f"name: acme:lib:toy:{version}",
                "filesets:",
                "  rtl:",
                "    files: [rtl/toy.sv]",
                "    file_type: systemVerilogSource",
                "targets:",
                "  lint_toy:",
                "    flow: lint",
                "    flow_options: {tool: verilator}",
                "    filesets: [rtl]",
                "    toplevel: toy",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _create_ticket(tio: TicketIO, slug: str = "change-target") -> Path:
    ticket = tio.create_ticket_file(
        slug,
        TicketFileSpec(
            summary="Change the Target contract",
            ticket_type="refactor",
            branch="main",
            scope=["toy.core"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nSeal the execution contract before implementation.\n",
        ),
    )
    assert ticket is not None
    return ticket


def _native_project(tmp_path: Path) -> tuple[Path, TicketIO, Path]:
    root = tmp_path / "rtl"
    _init_repo(root)
    (root / "rtl").mkdir()
    (root / "rtl" / "toy.sv").write_text("module toy; endmodule\n")
    project = root / ".booley_project"
    (project / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project / ".gitignore").write_text("/worktrees/\n")
    (project / "booley.toml").write_text("[flows.lint]\ndefault_target = 'lint_toy'\n")
    _write_core(root)
    _commit_all(root, "initial project")
    tio = TicketIO(project / "tickets", project_root=root)
    return root, tio, _create_ticket(tio)


def test_enqueue_refuses_unsealed_ticket_in_git_project(tmp_path: Path) -> None:
    _root, tio, _ticket = _native_project(tmp_path)

    assert tio.enqueue_ticket("change-target") is False


def test_existing_legacy_queue_ticket_cannot_start_fresh(tmp_path: Path) -> None:
    _root, tio, ticket = _native_project(tmp_path)
    queued = ticket.parent.parent / "queue" / ticket.name
    queued.parent.mkdir(parents=True)
    ticket.rename(queued)

    assert tio.init_ticket(queued) is None
    assert queued.is_file()


def test_native_contract_open_seal_and_enqueue(tmp_path: Path) -> None:
    root, tio, ticket = _native_project(tmp_path)

    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    _write_core(outer, version="2.0")
    sealed = tio.contract_seal("change-target")

    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert fields["base_sha"] == sealed["outer_sha"]
    assert fields["target_contract"] == sealed
    assert _git(root, "rev-parse", "change-target") == sealed["outer_sha"]
    assert tio.enqueue_ticket("change-target") is True


def test_seal_rejects_non_control_implementation_changes(tmp_path: Path) -> None:
    _root, tio, _ticket = _native_project(tmp_path)
    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    (outer / "rtl" / "toy.sv").write_text("module toy; wire implementation; endmodule\n")

    with pytest.raises(RuntimeError, match="non-control changes"):
        tio.contract_seal("change-target")


def test_revise_archives_identity_resets_evidence_and_reopens(tmp_path: Path) -> None:
    root, tio, ticket = _native_project(tmp_path)
    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    _write_core(outer, version="2.0")
    sealed = tio.contract_seal("change-target")
    (outer / "rtl" / "toy.sv").write_text("module toy; wire implementation; endmodule\n")
    implementation_sha = _commit_all(outer, "implementation in progress")
    assert implementation_sha != sealed["outer_sha"]
    evidence = tio.logs_dir / "change-target" / ".runtime" / "booley_state.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}\n")

    reopened = tio.contract_revise("change-target")

    archive = f"booley-contract-archive/change-target/{sealed['surface_digest'][:12]}"
    assert _git(root, "rev-parse", archive) == sealed["outer_sha"]
    assert Path(reopened["outer_worktree"]).is_dir()
    assert not evidence.exists()
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert "target_contract" not in fields
    assert "base_sha" not in fields
    assert sealed["surface_digest"] in fields["target_contract_history"][0]


def test_blocked_contract_revision_returns_ticket_to_draft(tmp_path: Path) -> None:
    _root, tio, ticket = _native_project(tmp_path)
    tio.contract_open("change-target")
    tio.contract_seal("change-target")
    blocked = ticket.parent.parent / "blocked" / ticket.name
    blocked.parent.mkdir(parents=True)
    ticket.rename(blocked)

    reopened = tio.contract_revise("change-target")

    draft = blocked.parent.parent / "drafts" / blocked.name
    assert draft.is_file()
    assert not blocked.exists()
    assert Path(reopened["outer_worktree"]).is_dir()


def test_standalone_project_repository_gets_paired_contract_commit(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _init_repo(root)
    (root / "rtl").mkdir()
    (root / "rtl" / "toy.sv").write_text("module toy; endmodule\n")
    _write_core(root)
    (root / ".gitignore").write_text("/.booley_project\n")
    _commit_all(root, "initial RTL")

    project = root / ".booley_project"
    _init_repo(project)
    (project / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project / "cores").mkdir()
    (project / ".gitignore").write_text("/worktrees/\n")
    (project / "booley.toml").write_text("[flows.lint]\ndefault_target = 'lint_toy'\n")
    _commit_all(project, "initial project data")
    tio = TicketIO(project / "tickets", project_root=root)
    ticket = _create_ticket(tio)

    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    paired = Path(opened["project_worktree"])
    _write_core(outer, version="2.0")
    (paired / "booley.toml").write_text("[flows.lint]\ndefault_target = 'lint_toy'\nstrict = true\n")
    sealed = tio.contract_seal("change-target")

    assert sealed["project_sha"]
    assert _git(paired, "rev-parse", "HEAD") == sealed["project_sha"]
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert fields["target_contract"]["project_sha"] == sealed["project_sha"]
    assert tio.enqueue_ticket("change-target") is True


def test_failed_cross_repository_seal_restores_unpublished_branch_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rtl"
    _init_repo(root)
    (root / "rtl").mkdir()
    (root / "rtl" / "toy.sv").write_text("module toy; endmodule\n")
    _write_core(root)
    (root / ".gitignore").write_text("/.booley_project\n")
    _commit_all(root, "initial RTL")
    project = root / ".booley_project"
    _init_repo(project)
    (project / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project / ".gitignore").write_text("/worktrees/\n")
    (project / "booley.toml").write_text("[flows.lint]\ndefault_target = 'lint_toy'\n")
    project_base = _commit_all(project, "initial project data")
    tio = TicketIO(project / "tickets", project_root=root)
    ticket = _create_ticket(tio)
    opened = tio.contract_open("change-target")
    paired = Path(opened["project_worktree"])
    (paired / "booley.toml").write_text(
        "[flows.lint]\ndefault_target = 'lint_toy'\nstrict = true\n"
    )
    original_commit = contract_ops._commit_changes
    calls = 0

    def fail_outer(repository: Path, paths: list[str], message: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise contract_ops.ContractOperationError("outer commit failed")
        return original_commit(repository, paths, message)

    monkeypatch.setattr(contract_ops, "_commit_changes", fail_outer)

    with pytest.raises(RuntimeError, match="outer commit failed"):
        tio.contract_seal("change-target")

    assert _git(project, "rev-parse", "booley-ticket/change-target") == project_base
    assert _git(paired, "diff", "--cached", "--name-only") == "booley.toml"
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert "target_contract" not in fields


def test_reset_refuses_unsealed_legacy_blocked_ticket(tmp_path: Path) -> None:
    root, tio, ticket = _native_project(tmp_path)
    blocked = ticket.parent.parent / "blocked" / ticket.name
    blocked.parent.mkdir(parents=True)
    ticket.rename(blocked)

    assert op_reset(tio, "change-target") is False
    assert blocked.is_file()
    assert (root / ".git").exists()

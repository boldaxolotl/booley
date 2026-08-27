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
from booley.ticket_board.target_contract import ContractParticipant, TargetContract


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
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
    assert sealed["schema"] == 3
    assert sealed["participants"] == [
        {
            "role": "outer",
            "sealed_sha": sealed["outer_sha"],
            "ticket_ref": "refs/heads/change-target",
            "destination_ref": "refs/heads/main",
            "destination_sha": opened["outer_base_sha"],
        }
    ]
    assert _git(root, "rev-parse", "change-target") == sealed["outer_sha"]
    assert tio.enqueue_ticket("change-target") is True


def test_sealed_refs_remain_valid_after_authoring_worktree_is_discarded(tmp_path: Path) -> None:
    root, tio, _ticket = _native_project(tmp_path)
    opened = tio.contract_open("change-target")
    sealed = tio.contract_seal("change-target")
    outer = Path(opened["outer_worktree"])
    _git(root, "worktree", "remove", "--force", str(outer))

    contract = contract_ops.TargetContract.from_mapping(sealed)

    assert contract_ops.validate_sealed_refs(root, contract) == []
    assert tio.enqueue_ticket("change-target") is True


def test_legacy_sealed_ref_validation_requires_ticket_slug(tmp_path: Path) -> None:
    root, _tio, _ticket = _native_project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    contract = TargetContract(base, "", "b" * 64, (), schema=2)

    assert contract_ops.validate_sealed_refs(root, contract) == [
        "legacy Target contract validation requires the Ticket slug"
    ]


def test_legacy_sealed_ref_validation_reports_missing_refs_and_repository(tmp_path: Path) -> None:
    root, _tio, _ticket = _native_project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    contract = TargetContract(base, base, "b" * 64, (), schema=2)

    assert contract_ops.validate_sealed_refs(root, contract, slug="change-target") == [
        "sealed ticket ref 'refs/heads/change-target' is unavailable",
        "sealed project repository is unavailable",
    ]


def test_sealed_ref_validation_reports_unknown_commit(tmp_path: Path) -> None:
    root, _tio, _ticket = _native_project(tmp_path)
    participant = ContractParticipant(
        "outer",
        "d" * 40,
        "refs/heads/main",
        "refs/heads/main",
        _git(root, "rev-parse", "HEAD"),
    )
    contract = TargetContract("d" * 40, "", "b" * 64, (), participants=(participant,))

    errors = contract_ops.validate_sealed_refs(root, contract)

    assert len(errors) == 1
    assert "does not resolve exactly" in errors[0]


def test_sealed_ref_validation_rejects_ref_that_does_not_descend_from_seal(
    tmp_path: Path,
) -> None:
    root, _tio, _ticket = _native_project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "branch", "old-ticket", base)
    (root / "main-only.txt").write_text("advance destination\n", encoding="utf-8")
    sealed = _commit_all(root, "advance main")
    participant = ContractParticipant(
        "outer",
        sealed,
        "refs/heads/old-ticket",
        "refs/heads/main",
        base,
    )
    contract = TargetContract(sealed, "", "b" * 64, (), participants=(participant,))

    assert contract_ops.validate_sealed_refs(root, contract) == [
        f"ticket ref 'refs/heads/old-ticket' does not descend from sealed outer commit {sealed}"
    ]


def test_seal_rejects_non_control_implementation_changes(tmp_path: Path) -> None:
    _root, tio, _ticket = _native_project(tmp_path)
    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    (outer / "rtl" / "toy.sv").write_text("module toy; wire implementation; endmodule\n")

    with pytest.raises(RuntimeError, match="non-control changes"):
        tio.contract_seal("change-target")


def test_seal_rejects_unreferenced_program_changes(tmp_path: Path) -> None:
    _root, tio, _ticket = _native_project(tmp_path)
    opened = tio.contract_open("change-target")
    outer = Path(opened["outer_worktree"])
    (outer / "unrelated.py").write_text("print('implementation')\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-control changes"):
        tio.contract_seal("change-target")


def test_blocked_legacy_contract_open_archives_work_and_can_reset(tmp_path: Path) -> None:
    root, tio, ticket = _native_project(tmp_path)
    slug = "change-target"
    legacy_worktree = root / ".booley_project" / "worktrees" / slug
    _git(root, "worktree", "add", "-b", slug, str(legacy_worktree), "main")
    (legacy_worktree / "rtl" / "toy.sv").write_text(
        "module toy; wire legacy_work; endmodule\n",
        encoding="utf-8",
    )
    legacy_sha = _commit_all(legacy_worktree, "legacy implementation")
    blocked = ticket.parent.parent / "blocked" / ticket.name
    blocked.parent.mkdir(parents=True)
    ticket.rename(blocked)

    opened = tio.contract_open(slug)

    outer = Path(opened["outer_worktree"])
    archive = f"booley-legacy-archive/{slug}/{legacy_sha[:12]}"
    assert _git(root, "rev-parse", archive) == legacy_sha
    assert _git(outer, "rev-parse", "HEAD") == _git(root, "rev-parse", "main")
    _write_core(outer, version="2.0")
    sealed = tio.contract_seal(slug)

    assert op_reset(tio, slug) is True
    queued = blocked.parent.parent / "queue" / blocked.name
    assert queued.is_file()
    assert _git(root, "rev-parse", slug) == sealed["outer_sha"]
    assert _git(outer, "rev-parse", "HEAD") == sealed["outer_sha"]
    assert tio.init_ticket(queued) is not None


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
    (paired / "booley.toml").write_text(
        "[flows.lint]\ndefault_target = 'lint_toy'\nstrict = true\n"
    )
    sealed = tio.contract_seal("change-target")

    assert sealed["project_sha"]
    assert [participant["role"] for participant in sealed["participants"]] == [
        "outer",
        "project",
    ]
    assert sealed["participants"][1] == {
        "role": "project",
        "sealed_sha": sealed["project_sha"],
        "ticket_ref": "refs/heads/booley-ticket/change-target",
        "destination_ref": "refs/heads/main",
        "destination_sha": opened["project_base_sha"],
    }
    assert _git(paired, "rev-parse", "HEAD") == sealed["project_sha"]
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    assert fields["target_contract"]["project_sha"] == sealed["project_sha"]
    assert tio.enqueue_ticket("change-target") is True

    queued = ticket.parent.parent / "queue" / ticket.name
    blocked = ticket.parent.parent / "blocked" / ticket.name
    blocked.parent.mkdir(parents=True)
    queued.rename(blocked)
    (outer / "rtl" / "toy.sv").write_text("module toy; wire retry; endmodule\n")
    _commit_all(outer, "outer implementation")
    (paired / "booley.toml").write_text("[flows.lint]\ndefault_target = 'other'\n")
    _commit_all(paired, "project implementation")

    assert op_reset(tio, "change-target") is True
    assert _git(outer, "rev-parse", "HEAD") == sealed["outer_sha"]
    assert _git(paired, "rev-parse", "HEAD") == sealed["project_sha"]


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

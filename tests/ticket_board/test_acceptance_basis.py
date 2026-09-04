"""Acceptance Basis schema and hard-cutoff behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import contract_ops, enqueue_publication
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    AcceptanceBasisError,
    BasisParticipant,
    authored_ticket_record,
    load_acceptance_basis,
    load_basis_receipt,
    load_basis_record,
)
from booley.ticket_board.acceptance_journal import JournalState
from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter
from booley.ticket_board.io import TicketFileSpec, TicketIO


@pytest.fixture(autouse=True)
def _clear_project_dir_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _participant(role: str = "outer") -> BasisParticipant:
    return BasisParticipant(
        role=role,
        authoring_sha="a" * 40,
        ticket_ref=f"refs/heads/booley-generation/0123456789abcdef/{role}",
        destination_ref="refs/heads/main",
        destination_sha="b" * 40,
    )


def test_minimal_basis_round_trips_through_ticket_frontmatter() -> None:
    basis = AcceptanceBasis((_participant(),))
    text = format_frontmatter(
        {"summary": "demo", "acceptance_basis": basis.as_dict()},
        "## Description\n\nDemo.\n",
    )

    fields, _body = parse_frontmatter(text)

    assert AcceptanceBasis.from_mapping(fields["acceptance_basis"]) == basis
    assert set(fields["acceptance_basis"]) == {"schema", "participants"}


@pytest.mark.parametrize(
    "mapping, message",
    [
        ({"schema": 2, "participants": []}, "schema must be 1"),
        ({"schema": 1, "participants": [], "digest": "x"}, "exactly"),
        (
            {
                "schema": 1,
                "participants": [
                    {
                        **_participant().as_dict(),
                        "ticket_ref": "refs/heads/legacy-ticket",
                    }
                ],
            },
            "generation-qualified",
        ),
        (
            {
                "schema": 1,
                "participants": [
                    _participant("project").as_dict(),
                    _participant("outer").as_dict(),
                ],
            },
            "sorted by unique role",
        ),
    ],
)
def test_basis_rejects_noncanonical_frontmatter(mapping: object, message: str) -> None:
    with pytest.raises(AcceptanceBasisError, match=message):
        AcceptanceBasis.from_mapping(mapping)


def test_authored_record_rejects_legacy_target_contract() -> None:
    with pytest.raises(AcceptanceBasisError, match="hard cutoff"):
        authored_ticket_record(
            {"summary": "old", "target_contract": {"schema": 4}},
            "body",
            (),
        )


def test_authored_record_pins_complete_ticket_projection() -> None:
    fields = {
        "summary": "demo",
        "type": "feature",
        "branch": "main",
        "scope": ["rtl/demo.sv"],
        "criteria": {"mandatory": {"review_rtl_bugs": True}},
        "on_success": {"destination": "review", "remove_targets": []},
        "priority": "medium",
    }

    record = authored_ticket_record(fields, "exact body", ())

    assert record["schema"] == 1
    assert record["ticket"] == {"frontmatter": fields, "body": "exact body"}


def test_no_manual_contract_commands_remain() -> None:
    from booley.ticket_board.cli import build_parser

    help_text = build_parser().format_help()
    assert "contract-open" not in help_text
    assert "contract-seal" not in help_text
    assert "revise-contract" not in help_text
    assert "return-to-draft" in help_text


def test_packaged_ticket_template_has_no_generated_basis_fields() -> None:
    template = (
        Path(__file__).parents[2]
        / "src/booley/data/skills/booley-ticket-create/TICKET_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    assert "target_contract:" not in template
    assert "base_sha:" not in template


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_enqueue_automatically_publishes_basis_record_and_receipt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    tio = TicketIO(project_dir / "tickets", project_root=root)

    ticket = tio.create_ticket_file(
        "automatic-basis",
        TicketFileSpec(
            summary="Publish automatically",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nExercise automatic basis publication.\n",
        ),
    )
    assert ticket is not None
    assert (project_dir / "worktrees" / "automatic-basis").is_dir()

    assert tio.enqueue_ticket("automatic-basis") is True

    queued = project_dir / "tickets" / "board" / "queue" / "automatic-basis.md"
    fields, body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    assert "target_contract" not in fields
    assert "base_sha" not in fields
    assert basis.participant("outer").ticket_ref.startswith("refs/heads/booley-generation/")
    record = project_dir / "worktrees" / "automatic-basis" / ".booley_project"
    record = record / "acceptance" / "bases" / "automatic-basis.json"
    assert record.is_file()
    receipt = project_dir / ".runtime" / "acceptance" / "bases" / "automatic-basis"
    assert (receipt / f"{basis.basis_id}.json").is_file()
    keepalive = f"refs/booley/bases/{basis.basis_id}/outer"
    assert _git(root, "rev-parse", keepalive) == basis.outer_sha
    loaded = load_acceptance_basis(root, "automatic-basis", fields, body)
    assert loaded.basis_id == basis.basis_id

    evidence = load_basis_receipt(root, "automatic-basis", basis.as_dict())
    assert evidence["basis_id"] == basis.basis_id
    assert evidence["record"]["sha256"]


def test_return_to_draft_preserves_old_ref_and_allocates_new_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    tio = TicketIO(project_dir / "tickets", project_root=root)
    ticket = tio.create_ticket_file(
        "new-generation",
        TicketFileSpec(
            summary="Start again",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nStart again.\n",
        ),
    )
    assert ticket is not None
    assert tio.enqueue_ticket("new-generation")
    queued = project_dir / "tickets" / "board" / "queue" / "new-generation.md"
    fields, _body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    old_basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    blocked = queued.parent.parent / "blocked" / queued.name
    blocked.parent.mkdir(parents=True)
    queued.rename(blocked)

    reopened = tio.return_to_draft("new-generation")

    draft = project_dir / "tickets" / "board" / "drafts" / "new-generation.md"
    draft_fields, _body = parse_frontmatter(draft.read_text(encoding="utf-8"))
    assert "acceptance_basis" not in draft_fields
    assert "created" not in draft_fields
    assert reopened["generation"] not in old_basis.participant("outer").ticket_ref
    assert _git(root, "rev-parse", old_basis.participant("outer").ticket_ref)
    keepalive = f"refs/booley/bases/{old_basis.basis_id}/outer"
    assert _git(root, "rev-parse", keepalive) == old_basis.outer_sha
    archived_worktree = _git(root, "worktree", "list", "--porcelain")
    assert old_basis.participant("outer").ticket_ref in archived_worktree


def _prepared_ticket(tmp_path: Path, slug: str = "transaction") -> tuple[Path, Path, TicketIO]:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    project_dir = root / ".booley_project"
    (project_dir / "tickets" / "board" / "drafts").mkdir(parents=True)
    (project_dir / ".gitignore").write_text("/worktrees/\n/.runtime/\n", encoding="utf-8")
    (project_dir / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "initial")
    tio = TicketIO(project_dir / "tickets", project_root=root)
    created = tio.create_ticket_file(
        slug,
        TicketFileSpec(
            summary="Recover publication",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nRecover publication.\n",
        ),
    )
    assert created is not None
    return root, project_dir, tio


def _blocked_ticket(tmp_path: Path, slug: str = "blocked-again") -> tuple[Path, Path, TicketIO]:
    root, project_dir, tio = _prepared_ticket(tmp_path, slug)
    assert tio.enqueue_ticket(slug)
    queued = project_dir / "tickets" / "board" / "queue" / f"{slug}.md"
    blocked = queued.parent.parent / "blocked" / queued.name
    blocked.parent.mkdir(parents=True)
    queued.replace(blocked)
    return root, blocked, tio


def test_invalid_enqueue_does_not_publish_basis_artifacts(tmp_path: Path) -> None:
    root, project_dir, tio = _prepared_ticket(tmp_path)

    assert tio.enqueue_ticket("transaction", on_success={"destination": "invalid"}) is False

    record = project_dir / "worktrees/transaction/.booley_project/acceptance/bases"
    assert not (record / "transaction.json").exists()
    assert not (project_dir / ".runtime/acceptance/bases/transaction").exists()
    assert "refs/booley/bases/" not in _git(root, "for-each-ref", "--format=%(refname)")
    assert (project_dir / "tickets/board/drafts/transaction.md").exists()


def test_enqueue_retry_finishes_interrupted_board_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project_dir, tio = _prepared_ticket(tmp_path)
    publish = enqueue_publication.publish_enqueue
    interrupted = False

    def interrupt(*args, **kwargs):
        nonlocal interrupted
        result = publish(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("after Board cutover")
        return result

    monkeypatch.setattr(enqueue_publication, "publish_enqueue", interrupt)
    with pytest.raises(OSError, match="Board cutover"):
        tio.enqueue_ticket("transaction")
    monkeypatch.setattr(enqueue_publication, "publish_enqueue", publish)

    assert tio.enqueue_ticket("transaction") is True
    queued = project_dir / "tickets/board/queue/transaction.md"
    assert queued.exists()
    transitions = project_dir / "tickets/logs/transaction/human-logs/transitions.log"
    assert transitions.read_text(encoding="utf-8").count("enqueue operation") == 1


def test_partial_keepalive_creation_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    basis = AcceptanceBasis((_participant("outer"), _participant("project")))
    outer = tmp_path / "outer"
    project = tmp_path / "project"
    deleted: list[tuple[Path, str]] = []

    def fake_git(repository: Path, *args: str):
        if args[:2] == ("update-ref", "-d"):
            deleted.append((repository, args[2]))
        return subprocess.CompletedProcess(args, 1, "", "")

    calls = 0

    def fake_require(repository: Path, *args: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise contract_ops.ContractOperationError("project ref failed")
        return ""

    monkeypatch.setattr(contract_ops, "_git", fake_git)
    monkeypatch.setattr(contract_ops, "_require_git", fake_require)

    with pytest.raises(contract_ops.ContractOperationError, match="project ref failed"):
        contract_ops._publish_basis_keepalives(basis, outer, project)
    assert deleted == [(outer, f"refs/booley/bases/{basis.basis_id}/outer")]


def test_malformed_committed_record_fails_with_basis_error(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    record = root / ".booley_project/acceptance/bases/malformed.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"bindings":[],"schema":1,"ticket":{}}\n', encoding="utf-8")
    _git(root, "add", "-f", ".booley_project")
    _git(root, "commit", "-m", "malformed record")
    sha = _git(root, "rev-parse", "HEAD")
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                sha,
                "refs/heads/booley-generation/0123456789abcdef/malformed",
                "refs/heads/main",
                sha,
            ),
        )
    )

    with pytest.raises(AcceptanceBasisError, match=r"record\.ticket"):
        load_basis_record(root, "malformed", basis)


def test_return_to_draft_rejects_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    lock = tio.logs_dir / "blocked-again/.runtime/ticket.lock"
    lock.write_text("424242", encoding="utf-8")
    monkeypatch.setattr("booley.runtime.pid.is_pid_alive", lambda _pid: True)

    with pytest.raises(RuntimeError, match="owned by live process"):
        tio.return_to_draft("blocked-again")


def test_return_to_draft_rejects_jobs_and_acceptance_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    monkeypatch.setattr(
        "booley.harness.job_fence.active_ticket_jobs",
        lambda _path: [SimpleNamespace(endpoint="sim", run_id="job-1")],
    )
    with pytest.raises(RuntimeError, match="active endpoint Jobs"):
        tio.return_to_draft("blocked-again")

    monkeypatch.setattr("booley.harness.job_fence.active_ticket_jobs", lambda _path: [])
    monkeypatch.setattr(
        "booley.ticket_board.io.acceptance_state",
        lambda _tickets, _slug: JournalState.PREPARED,
    )
    with pytest.raises(RuntimeError, match="publication in progress"):
        tio.return_to_draft("blocked-again")


def test_return_to_draft_recovers_after_board_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board import draft_transition

    root, blocked, tio = _blocked_ticket(tmp_path)
    old_worktree = root / ".booley_project/worktrees/blocked-again"
    (old_worktree / "unfinished.txt").write_text("preserve me\n", encoding="utf-8")
    old_log = tio.logs_dir / "blocked-again/human-logs/run.log"
    old_log.parent.mkdir(parents=True, exist_ok=True)
    old_log.write_text("old evidence\n", encoding="utf-8")
    publish_generation = draft_transition._publish_generation
    interrupted = False

    def interrupt(*args, **kwargs):
        nonlocal interrupted
        publish_generation(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("after descriptor cutover")

    monkeypatch.setattr(draft_transition, "_publish_generation", interrupt)
    with pytest.raises(OSError, match="descriptor cutover"):
        tio.return_to_draft("blocked-again")
    assert not blocked.exists()
    monkeypatch.setattr(draft_transition, "_publish_generation", publish_generation)

    reopened = tio.return_to_draft("blocked-again")

    assert Path(reopened["outer_worktree"]).is_dir()
    old_paths = _git(root, "worktree", "list", "--porcelain")
    archived_outer = next(
        Path(line.removeprefix("worktree "))
        for line in old_paths.splitlines()
        if line.startswith("worktree ") and "old-outer" in line
    )
    assert (archived_outer / "unfinished.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert (tio.logs_dir / "blocked-again/runs/001/human-logs/run.log").exists()

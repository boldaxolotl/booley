"""Recoverable publication of sealed Ticket repository participants."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from booley.runtime.project_dir import reset_cache
from booley.ticket_board import completion
from booley.ticket_board.completion import complete_review_ticket
from booley.ticket_board.target_contract import (
    SCHEMA_VERSION,
    ContractParticipant,
    ContractTargetBinding,
    TargetContract,
    surface_digest,
    surface_entries,
)


@pytest.fixture(autouse=True)
def _reset_project_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repository(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / ".git" / "info" / "exclude").write_text(
        ".booley_project/\n.runtime/\n", encoding="utf-8"
    )
    (path / "design.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "design.txt")
    _git(path, "commit", "-m", "base")
    return _git(path, "rev-parse", "HEAD")


def _ticket_commit(repo: Path, branch: str, content: str) -> str:
    _git(repo, "switch", "-c", branch)
    (repo / "design.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "design.txt")
    _git(repo, "commit", "-m", f"implement {branch}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "main")
    _git(repo, "branch", "--set-upstream-to=main", branch)
    return sha


@dataclass(frozen=True)
class _Policy:
    merge: bool = True
    cleanup: bool = False
    remove_targets: tuple[str, ...] = ()


class _TicketIO:
    def __init__(self, root: Path, contract: TargetContract) -> None:
        self._project_root = root
        self.tickets_dir = root / ".booley_project" / "tickets"
        self.logs_dir = self.tickets_dir / "logs"
        self.entry: dict[str, Any] = {
            "file": "board/review/change-target.md",
            "status": "review",
            "branch": "main",
            "target_contract": contract.as_dict(),
        }
        self.transitions: list[tuple[str, str, str, str]] = []

    def find_ticket(self, _slug: str) -> dict[str, Any]:
        return self.entry

    def move_and_update(self, _slug: str, to_dir: str, _updates: dict[str, Any], **kwargs) -> bool:
        self.entry["status"] = "done"
        self.transitions.append(kwargs["transition"])
        assert to_dir == "done"
        assert kwargs["expected_status"] == "review"
        return True


class _BoundaryTicketIO:
    def __init__(self, entry: dict[str, Any] | None) -> None:
        self.entry = entry

    def find_ticket(self, _slug: str) -> dict[str, Any] | None:
        return self.entry


def _contract(
    root: Path,
    participants: tuple[ContractParticipant, ...],
    *,
    schema: int = SCHEMA_VERSION,
) -> TargetContract:
    outer = next(item for item in participants if item.role == "outer")
    project = next((item for item in participants if item.role == "project"), None)
    return TargetContract(
        outer_sha=outer.sealed_sha,
        project_sha=project.sealed_sha if project else "",
        surface_digest=surface_digest(root, schema=schema),
        targets=(),
        participants=participants,
        surface_entries=surface_entries(root),
        schema=schema,
    )


def _boundary_contract() -> TargetContract:
    participant = ContractParticipant(
        role="outer",
        sealed_sha="a" * 40,
        ticket_ref="refs/heads/ticket",
        destination_ref="refs/heads/main",
        destination_sha="b" * 40,
    )
    return TargetContract(
        outer_sha=participant.sealed_sha,
        project_sha="",
        surface_digest="c" * 64,
        targets=(),
        participants=(participant,),
    )


def _paired_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, _TicketIO, tuple[ContractParticipant, ...]]:
    root = tmp_path / "rtl"
    outer_base = _repository(root)
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    project = root / ".booley_project"
    project_base = _repository(project)
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    participants = (
        ContractParticipant(
            "outer",
            outer_ticket,
            "refs/heads/change-target",
            "refs/heads/main",
            outer_base,
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    return root, project, _TicketIO(root, _contract(root, participants)), participants


def test_git_failures_report_repository_and_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(completion.CompletionError, match="git rev-parse HEAD failed"):
        completion._require_git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(completion.CompletionError, match="could not compare Git history"):
        completion._is_ancestor(tmp_path, "a" * 40, "b" * 40)

    def unavailable_git(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable_git)
    with pytest.raises(completion.CompletionError, match=r"git status failed.*git unavailable"):
        completion._git(tmp_path, "status")


def test_project_participant_requires_project_repository(tmp_path: Path) -> None:
    participant = ContractParticipant(
        "project",
        "a" * 40,
        "refs/heads/ticket",
        "refs/heads/main",
        "b" * 40,
    )

    with pytest.raises(completion.CompletionError, match="project repository is unavailable"):
        completion._repository_for(tmp_path, None, participant)


def test_candidate_clone_copies_repository_commit_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit = _repository(repository)
    clone = tmp_path / "clone"

    completion._clone_checkout(repository, clone, commit)

    assert _git(clone, "config", "--local", "--get", "user.name") == "Test"
    assert _git(clone, "config", "--local", "--get", "user.email") == "test@example.invalid"


def test_acceptance_ref_cas_rejects_symbolic_ref_without_moving_referent(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    base = _repository(repository)
    desired = _ticket_commit(repository, "change-target", "implemented\n")
    staging_ref = "refs/booley/acceptance/transaction/outer"
    _git(repository, "symbolic-ref", staging_ref, "refs/heads/main")

    with pytest.raises(completion.CompletionError, match="is symbolic"):
        completion._cas_ref(repository, staging_ref, desired, base)

    assert completion._ref_commit(repository, "refs/heads/main") == base
    assert _git(repository, "symbolic-ref", staging_ref) == "refs/heads/main"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "acceptance journal is unreadable"),
        (json.dumps({"ticket": "another", "participants": []}), "does not belong"),
        (
            json.dumps({"ticket": "change-target", "participants": []}),
            "sealed repository participants changed",
        ),
    ],
)
def test_acceptance_journal_rejects_corrupt_or_mismatched_state(
    tmp_path: Path, content: str, message: str
) -> None:
    journal = tmp_path / "acceptance.json"
    journal.write_text(content, encoding="utf-8")

    with pytest.raises(completion.CompletionError, match=message):
        completion._load_journal(journal, "change-target", _boundary_contract())


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"transaction": "short"}, "transaction is invalid"),
        ({"state": "invented"}, "state 'invented' is invalid"),
        ({"sources": []}, "sources must be a mapping"),
        ({"sources": {"outer": "short"}}, "full Git commit SHA"),
        (
            {"policy": {"merge": True, "cleanup": True}},
            "cleanup policy changed",
        ),
        (
            {
                "candidates": {
                    "outer": {
                        "prepared_sha": "d" * 40,
                        "finalized_sha": "d" * 40,
                        "staging_ref": "refs/booley/acceptance/{transaction}/outer",
                        "expected_destination_sha": "e" * 40,
                    }
                }
            },
            "candidates require pinned sources",
        ),
        ({"published": ["outer", "outer"]}, "published roles are out of order"),
    ],
)
def test_acceptance_journal_validates_every_recovery_field(
    tmp_path: Path, update: dict[str, Any], message: str
) -> None:
    contract = _boundary_contract()
    data = completion._initial_journal("change-target", contract)
    candidates = update.get("candidates")
    if isinstance(candidates, dict) and "outer" in candidates:
        candidates["outer"]["staging_ref"] = candidates["outer"]["staging_ref"].format(
            transaction=data["transaction"]
        )
    data.update(update)
    journal = tmp_path / "acceptance.json"
    journal.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(completion.CompletionError, match=message):
        completion._load_journal(journal, "change-target", contract)


def test_schema_one_done_journal_resumes_cleanup_as_accepted(tmp_path: Path) -> None:
    contract = _boundary_contract()
    data = completion._initial_journal("change-target", contract)
    transaction = data["transaction"]
    data.update(
        {
            "schema": 1,
            "state": "done",
            "sources": {"outer": "a" * 40},
            "candidates": {
                "outer": {
                    "sha": "d" * 40,
                    "staging_ref": f"refs/booley/acceptance/{transaction}/outer",
                    "expected_destination_sha": "e" * 40,
                }
            },
            "published": ["outer"],
        }
    )
    data.pop("policy")
    data.pop("cleaned")
    data.pop("removal_targets")
    data.pop("removal_digest")
    journal = tmp_path / "acceptance.json"
    journal.write_text(json.dumps(data), encoding="utf-8")

    loaded = completion._load_journal(journal, "change-target", contract, cleanup=True)

    assert loaded["schema"] == 4
    assert loaded["state"] == "accepted"
    assert loaded["policy"] == {"merge": True, "cleanup": True}
    assert loaded["cleaned"] == []


def test_finalization_schema_two_journal_upgrades_to_combined_schema(tmp_path: Path) -> None:
    contract = _boundary_contract()
    data = completion._initial_journal("change-target", contract)
    data["schema"] = 2
    data.pop("policy")
    data.pop("cleaned")
    data["finalized"] = True
    journal = tmp_path / "acceptance.json"
    journal.write_text(json.dumps(data), encoding="utf-8")

    loaded = completion._load_journal(journal, "change-target", contract)

    assert loaded["schema"] == 4
    assert loaded["policy"] == {"merge": True, "cleanup": False}
    assert loaded["cleaned"] == []


def test_schema_three_finalized_removal_does_not_invent_prepared_identity(
    tmp_path: Path,
) -> None:
    contract = _boundary_contract()
    removal = "acme:lib:toy:1.0#baseline"
    data = completion._initial_journal("change-target", contract, removal_targets=(removal,))
    transaction = data["transaction"]
    finalized = "d" * 40
    data.update(
        schema=3,
        state="prepared",
        sources={"outer": "a" * 40},
        candidates={
            "outer": {
                "sha": finalized,
                "staging_ref": f"refs/booley/acceptance/{transaction}/outer",
                "expected_destination_sha": "e" * 40,
            }
        },
        finalized=True,
    )
    journal = tmp_path / "acceptance.json"
    journal.write_text(json.dumps(data), encoding="utf-8")

    loaded = completion._load_journal(
        journal,
        "change-target",
        contract,
        removal_targets=(removal,),
    )

    assert loaded["schema"] == 4
    assert loaded["candidates"]["outer"] == {
        "prepared_sha": None,
        "finalized_sha": finalized,
        "staging_ref": f"refs/booley/acceptance/{transaction}/outer",
        "expected_destination_sha": "e" * 40,
    }


def test_complete_requires_merge_policy() -> None:
    with pytest.raises(completion.CompletionError, match="requires merge policy to be true"):
        complete_review_ticket(_BoundaryTicketIO(None), "missing", _Policy(merge=False))

    with pytest.raises(completion.CompletionError, match="cleanup policy to be boolean"):
        complete_review_ticket(
            _BoundaryTicketIO(None),
            "missing",
            _Policy(cleanup="yes"),  # type: ignore[arg-type]
        )


def test_complete_reports_missing_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    assert complete_review_ticket(_BoundaryTicketIO(None), "missing", _Policy()) is False
    assert "ticket 'missing' not found" in capsys.readouterr().err


def test_complete_reports_malformed_contract(capsys: pytest.CaptureFixture[str]) -> None:
    tio = _BoundaryTicketIO({"status": "review", "target_contract": {}})

    assert complete_review_ticket(tio, "bad-contract", _Policy()) is False
    assert "cannot complete 'bad-contract'" in capsys.readouterr().err


def test_complete_rejects_removal_policy_changed_after_sealing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = _boundary_contract()
    tio = _BoundaryTicketIO(
        {
            "file": "board/review/change-target.md",
            "status": "review",
            "branch": "main",
            "target_contract": contract.as_dict(),
        }
    )

    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=("baseline",)))
        is False
    )
    assert "changed after Target Contract sealing" in capsys.readouterr().err


def test_complete_rejects_legacy_contract_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tio = _BoundaryTicketIO(
        {
            "status": "review",
            "target_contract": {
                "schema": 2,
                "outer_sha": "a" * 40,
                "project_sha": "",
                "surface_digest": "b" * 64,
                "targets": [],
                "bindings": [],
            },
        }
    )

    assert complete_review_ticket(tio, "legacy", _Policy()) is False
    assert "target_contract.schema must be one of [3, 4]" in capsys.readouterr().err


def test_complete_rejects_retired_integration_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tio = _BoundaryTicketIO(
        {
            "file": "board/review/ambiguous.md",
            "status": "review",
            "branch": "main",
            "integration_base": "main~1",
            "target_contract": _boundary_contract().as_dict(),
        }
    )

    assert complete_review_ticket(tio, "ambiguous", _Policy()) is False
    error = capsys.readouterr().err
    assert "integration_base" in error
    assert "schema-3 Tickets" in error


def test_complete_rejects_destination_ref_as_cleanup_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract = _boundary_contract()
    participant = contract.participants[0]
    unsafe = TargetContract(
        outer_sha=contract.outer_sha,
        project_sha="",
        surface_digest=contract.surface_digest,
        targets=(),
        participants=(
            ContractParticipant(
                role="outer",
                sealed_sha=participant.sealed_sha,
                ticket_ref=participant.destination_ref,
                destination_ref=participant.destination_ref,
                destination_sha=participant.destination_sha,
            ),
        ),
    )
    tio = _BoundaryTicketIO(
        {
            "file": "board/review/unsafe.md",
            "status": "review",
            "branch": "main",
            "target_contract": unsafe.as_dict(),
        }
    )

    assert complete_review_ticket(tio, "unsafe", _Policy(cleanup=True)) is False
    assert "Ticket ref is also the destination ref" in capsys.readouterr().err


def test_complete_publishes_sealed_branch_before_approving(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        role="outer",
        sealed_sha=ticket_sha,
        ticket_ref="refs/heads/change-target",
        destination_ref="refs/heads/main",
        destination_sha=base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))

    assert complete_review_ticket(tio, "change-target", _Policy()) is True

    assert _git(root, "show", "main:design.txt") == "implemented"
    assert tio.entry["status"] == "done"
    assert tio.transitions == [
        ("review:summary", "done:complete", "op-complete", "terminal actions")
    ]
    journals = list((root / ".booley_project" / ".runtime" / "acceptance").glob("*.json"))
    assert len(journals) == 1


def test_complete_merges_ticket_onto_advanced_destination_without_rebasing(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        role="outer",
        sealed_sha=ticket_sha,
        ticket_ref="refs/heads/change-target",
        destination_ref="refs/heads/main",
        destination_sha=base,
    )
    contract = _contract(root, (participant,))
    (root / "baseline.txt").write_text("advanced baseline\n", encoding="utf-8")
    _git(root, "add", "baseline.txt")
    _git(root, "commit", "-m", "advance baseline")
    advanced = _git(root, "rev-parse", "HEAD")
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(tio, "change-target", _Policy()) is True

    assert _git(root, "show", "main:design.txt") == "implemented"
    assert _git(root, "show", "main:baseline.txt") == "advanced baseline"
    parents = _git(root, "show", "-s", "--format=%P", "main").split()
    assert parents == [advanced, ticket_sha]


def test_complete_cleans_recorded_ticket_ref_after_acceptance(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        role="outer",
        sealed_sha=ticket_sha,
        ticket_ref="refs/heads/change-target",
        destination_ref="refs/heads/main",
        destination_sha=base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))

    result = complete_review_ticket(tio, "change-target", _Policy(cleanup=True))

    assert result is True
    assert _git(root, "show", "main:design.txt") == "implemented"
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/change-target"],
            cwd=root,
            check=False,
        ).returncode
        == 1
    )
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "done"
    assert journal["cleaned"] == ["outer"]


def test_retry_cannot_change_frozen_cleanup_policy(tmp_path: Path, capsys) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))

    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=False)) is True
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is False
    assert "cleanup policy changed" in capsys.readouterr().err
    assert completion._ref_commit(root, participant.ticket_ref) == ticket_sha


def test_cleanup_refuses_moved_ticket_ref_and_retry_uses_recorded_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    _git(root, "switch", "-c", "late-source", ticket_sha)
    (root / "design.txt").write_text("late movement\n", encoding="utf-8")
    _git(root, "add", "design.txt")
    _git(root, "commit", "-m", "late source")
    late_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))
    validate_surface = completion._validate_source_surface

    def move_ref(*args: Any, **kwargs: Any) -> None:
        validate_surface(*args, **kwargs)
        _git(root, "branch", "-f", "change-target", late_sha)

    monkeypatch.setattr(completion, "_validate_source_surface", move_ref)

    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert _git(root, "rev-parse", "change-target") == late_sha
    assert "cleanup is pending" in capsys.readouterr().err

    monkeypatch.setattr(completion, "_validate_source_surface", validate_surface)
    _git(root, "branch", "-f", "change-target", ticket_sha)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/change-target"],
            cwd=root,
            check=False,
        ).returncode
        == 1
    )


def test_cleanup_preserves_dirty_ticket_worktree_until_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    ticket_worktree = tmp_path / "ticket-worktree"
    _git(root, "worktree", "add", str(ticket_worktree), "change-target")
    (ticket_worktree / "design.txt").write_text("dirty\n", encoding="utf-8")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))

    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert ticket_worktree.exists()
    assert _git(root, "rev-parse", "change-target") == ticket_sha
    assert "dirty Ticket worktree" in capsys.readouterr().err

    _git(ticket_worktree, "restore", "design.txt")
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert not ticket_worktree.exists()


def test_retry_resumes_after_project_cleanup_completed_before_journal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "rtl"
    outer_base = _repository(root)
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    project = root / ".booley_project"
    project_base = _repository(project)
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    participants = (
        ContractParticipant(
            "outer",
            outer_ticket,
            "refs/heads/change-target",
            "refs/heads/main",
            outer_base,
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    tio = _TicketIO(root, _contract(root, participants))
    write_journal = completion._write_journal
    failed = False

    def interrupt_project_checkpoint(path: Path, journal: dict[str, Any]) -> None:
        nonlocal failed
        if journal.get("state") == "cleanup-project" and not failed:
            failed = True
            raise OSError("simulated cleanup checkpoint interruption")
        write_journal(path, journal)

    monkeypatch.setattr(completion, "_write_journal", interrupt_project_checkpoint)

    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert "cleanup is pending" in capsys.readouterr().err
    assert completion._ref_commit(project, "refs/heads/booley-ticket/change-target") is None
    assert completion._ref_commit(root, "refs/heads/change-target") == outer_ticket

    monkeypatch.setattr(completion, "_write_journal", write_journal)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    assert completion._ref_commit(root, "refs/heads/change-target") is None
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "done"
    assert journal["cleaned"] == ["project", "outer"]


def test_unfinished_publication_blocks_another_ticket(tmp_path: Path, capsys) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    contract = _contract(root, (participant,))
    data = completion._initial_journal("earlier", contract)
    data["sources"] = {"outer": ticket_sha}
    data["candidates"] = {
        "outer": {
            "prepared_sha": ticket_sha,
            "finalized_sha": ticket_sha,
            "staging_ref": f"refs/booley/acceptance/{data['transaction']}/outer",
            "expected_destination_sha": base,
        }
    }
    data["state"] = "prepared"
    acceptance = root / ".booley_project" / ".runtime" / "acceptance"
    acceptance.mkdir(parents=True)
    (acceptance / "earlier.json").write_text(json.dumps(data), encoding="utf-8")
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(tio, "change-target", _Policy()) is False
    assert "resume it first" in capsys.readouterr().err
    assert _git(root, "show", "main:design.txt") == "base"


def test_malformed_finished_journal_blocks_another_ticket(tmp_path: Path, capsys) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    acceptance = root / ".booley_project" / ".runtime" / "acceptance"
    acceptance.mkdir(parents=True)
    (acceptance / "earlier.json").write_text(
        json.dumps({"ticket": "earlier", "state": "done"}), encoding="utf-8"
    )
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))

    assert complete_review_ticket(tio, "change-target", _Policy()) is False
    assert "earlier acceptance journal" in capsys.readouterr().err
    assert _git(root, "show", "main:design.txt") == "base"


def test_cleanup_status_is_false_without_acceptance_journal(tmp_path: Path) -> None:
    (tmp_path / ".booley_project").mkdir()

    assert completion.cleanup_finished(tmp_path, "change-target") is False


def test_cleanup_status_reader_rejects_corrupt_journal(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    (root / ".booley_project" / ".runtime" / "acceptance").mkdir(parents=True)
    journal = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal.write_text("{", encoding="utf-8")

    with pytest.raises(completion.CompletionError, match="acceptance journal is unreadable"):
        completion.cleanup_finished(root, "change-target")


def test_cross_repository_plan_conflict_leaves_repositories_unmodified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, _participants = _paired_completion(tmp_path, monkeypatch)
    (project / "design.txt").write_text("conflicting destination\n", encoding="utf-8")
    _git(project, "add", "design.txt")
    _git(project, "commit", "-m", "conflict with Ticket")
    outer_worktrees = _git(root, "worktree", "list", "--porcelain")
    project_worktrees = _git(project, "worktree", "list", "--porcelain")

    assert complete_review_ticket(tio, "change-target", _Policy()) is False

    assert _git(root, "for-each-ref", "--format=%(refname)", "refs/booley/acceptance") == ""
    assert _git(project, "for-each-ref", "--format=%(refname)", "refs/booley/acceptance") == ""
    assert _git(root, "worktree", "list", "--porcelain") == outer_worktrees
    assert _git(project, "worktree", "list", "--porcelain") == project_worktrees


def test_cleanup_validates_every_identity_before_removing_any_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, project, tio, participants = _paired_completion(tmp_path, monkeypatch)
    finish_approval = completion._finish_approval

    def move_outer_staging_ref(*args: Any, **kwargs: Any) -> None:
        finish_approval(*args, **kwargs)
        journal = args[3]
        staging_ref = journal["candidates"]["outer"]["staging_ref"]
        _git(root, "update-ref", staging_ref, participants[0].destination_sha)

    monkeypatch.setattr(completion, "_finish_approval", move_outer_staging_ref)

    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True

    assert "cleanup is pending" in capsys.readouterr().err
    assert completion._ref_commit(root, participants[0].ticket_ref) == participants[0].sealed_sha
    assert (
        completion._ref_commit(project, participants[1].ticket_ref) == participants[1].sealed_sha
    )


def test_complete_removes_target_only_from_final_merge_candidate(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _repository(root)
    (root / "toy.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "targets:\n"
        "  baseline: {flow: lint, toplevel: toy}\n"
        "  candidate: {flow: lint, toplevel: toy}\n",
        encoding="utf-8",
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target pair")
    base = _git(root, "rev-parse", "HEAD")
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer", ticket_sha, "refs/heads/change-target", "refs/heads/main", base
    )
    canonical = "acme:lib:toy:1.0#baseline"
    contract = TargetContract(
        outer_sha=ticket_sha,
        project_sha="",
        surface_digest=surface_digest(root),
        targets=("baseline", "candidate"),
        removal_targets=(canonical,),
        bindings=(
            ContractTargetBinding(
                "lint",
                "lint_clean",
                canonical,
                "acme:lib:toy:1.0#candidate",
                "baseline",
                "candidate",
            ),
        ),
        participants=(participant,),
        surface_entries=surface_entries(root),
    )
    tio = _TicketIO(root, contract)

    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )

    merged_core = _git(root, "show", "main:toy.core")
    assert "  baseline:" not in merged_core
    assert "  candidate:" in merged_core
    assert "  baseline:" in _git(root, "show", "change-target:toy.core")
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["removal_targets"] == [canonical]
    candidate = journal["candidates"]["outer"]
    assert candidate["finalized_sha"] != candidate["prepared_sha"]


def test_complete_finalizes_target_in_project_repository_before_outer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rtl"
    outer_base = _repository(root)
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    project = root / ".booley_project"
    _repository(project)
    (project / "cores").mkdir()
    (project / "cores" / "toy.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "targets:\n"
        "  baseline: {flow: lint, toplevel: toy}\n"
        "  candidate: {flow: lint, toplevel: toy}\n",
        encoding="utf-8",
    )
    (project / "tests.toml").write_text(
        '[baseline]\ntests = ["old"]\n\n[candidate]\ntests = ["new"]\n',
        encoding="utf-8",
    )
    _git(project, "add", "cores/toy.core", "tests.toml")
    _git(project, "commit", "-m", "add project target pair")
    project_base = _git(project, "rev-parse", "HEAD")
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    participants = (
        ContractParticipant(
            "outer", outer_ticket, "refs/heads/change-target", "refs/heads/main", outer_base
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    canonical = "acme:lib:toy:1.0#baseline"
    contract = TargetContract(
        outer_sha=outer_ticket,
        project_sha=project_ticket,
        surface_digest=surface_digest(root),
        targets=("baseline", "candidate"),
        removal_targets=(canonical,),
        bindings=(
            ContractTargetBinding(
                "lint",
                "lint_clean",
                canonical,
                "acme:lib:toy:1.0#candidate",
                "baseline",
                "candidate",
            ),
        ),
        participants=participants,
        surface_entries=surface_entries(root),
    )
    tio = _TicketIO(root, contract)

    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )

    assert "  baseline:" not in _git(project, "show", "main:cores/toy.core")
    assert "  candidate:" in _git(project, "show", "main:cores/toy.core")
    merged_tests = _git(project, "show", "main:tests.toml")
    assert "[baseline]" not in merged_tests
    assert "[candidate]" in merged_tests
    assert _git(root, "show", "main:design.txt") == "outer implementation"


def _outer_target_removal_repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "rtl"
    _repository(root)
    (root / "toy.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "targets:\n"
        "  baseline: {flow: lint, toplevel: toy}\n"
        "  candidate: {flow: lint, toplevel: toy}\n",
        encoding="utf-8",
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target pair")
    outer_base = _git(root, "rev-parse", "HEAD")
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    return root, outer_base, outer_ticket


def _project_target_removal_repository(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, str]:
    project = root / ".booley_project"
    _repository(project)
    (project / "tests.toml").write_text(
        '[baseline]\ntests = ["old"]\n\n[candidate]\ntests = ["new"]\n',
        encoding="utf-8",
    )
    _git(project, "add", "tests.toml")
    _git(project, "commit", "-m", "add target tests")
    project_base = _git(project, "rev-parse", "HEAD")
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    return project, project_base, project_ticket


def _target_removal_contract(
    root: Path,
    participants: tuple[ContractParticipant, ...],
    canonical: str,
) -> TargetContract:
    outer, project = participants
    return TargetContract(
        outer_sha=outer.sealed_sha,
        project_sha=project.sealed_sha,
        surface_digest=surface_digest(root),
        targets=("baseline", "candidate"),
        removal_targets=(canonical,),
        bindings=(
            ContractTargetBinding(
                "lint",
                "lint_clean",
                canonical,
                "acme:lib:toy:1.0#candidate",
                "baseline",
                "candidate",
            ),
        ),
        participants=participants,
        surface_entries=surface_entries(root),
    )


def _paired_target_removal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    Path,
    Path,
    _TicketIO,
    tuple[ContractParticipant, ...],
    str,
]:
    root, outer_base, outer_ticket = _outer_target_removal_repository(tmp_path)
    project, project_base, project_ticket = _project_target_removal_repository(root, monkeypatch)
    participants = (
        ContractParticipant(
            "outer", outer_ticket, "refs/heads/change-target", "refs/heads/main", outer_base
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    canonical = "acme:lib:toy:1.0#baseline"
    contract = _target_removal_contract(root, participants, canonical)
    return root, project, _TicketIO(root, contract), participants, canonical


def _acceptance_journal(root: Path) -> dict[str, Any]:
    path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _interrupt_finalized_ref_updates(monkeypatch: pytest.MonkeyPatch, message: str) -> Any:
    update_finalized_refs = completion._update_finalized_refs

    def interrupt(*_args: Any, **_kwargs: Any) -> None:
        raise completion.CompletionError(message)

    monkeypatch.setattr(completion, "_update_finalized_refs", interrupt)
    return update_finalized_refs


def _assert_destinations_unchanged(
    root: Path,
    project: Path,
    participants: tuple[ContractParticipant, ...],
) -> None:
    for participant, repository in zip(participants, (root, project), strict=True):
        assert (
            _git(repository, "rev-parse", participant.destination_ref)
            == participant.destination_sha
        )


def test_retry_rejects_unknown_finalization_identity_before_any_ref_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    update_finalized_refs = _interrupt_finalized_ref_updates(
        monkeypatch, "interrupted after finalized journal write"
    )
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )

    journal = _acceptance_journal(root)
    outer_staging = journal["candidates"]["outer"]["staging_ref"]
    project_staging = journal["candidates"]["project"]["staging_ref"]
    outer_prepared = completion._ref_commit(root, outer_staging)
    project_prepared = completion._ref_commit(project, project_staging)
    assert outer_prepared is not None
    assert project_prepared is not None
    for role, repository in (("outer", root), ("project", project)):
        candidate = journal["candidates"][role]
        assert candidate["prepared_sha"] != candidate["finalized_sha"]
        finalized_ref = f"refs/booley/acceptance/{journal['transaction']}/finalized-{role}"
        assert completion._ref_commit(repository, finalized_ref) == candidate["finalized_sha"]
    _git(
        project,
        "update-ref",
        project_staging,
        participants[1].destination_sha,
        project_prepared,
    )
    monkeypatch.setattr(completion, "_update_finalized_refs", update_finalized_refs)

    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert completion._ref_commit(root, outer_staging) == outer_prepared
    assert completion._ref_commit(project, project_staging) == participants[1].destination_sha
    _assert_destinations_unchanged(root, project, participants)
    assert tio.entry["status"] == "review"


def test_retry_rejects_tag_object_at_finalized_staging_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    update_finalized_refs = _interrupt_finalized_ref_updates(
        monkeypatch, "interrupted after finalized journal write"
    )
    policy = _Policy(remove_targets=(canonical,))
    assert complete_review_ticket(tio, "change-target", policy) is False

    journal = _acceptance_journal(root)
    candidate = journal["candidates"]["project"]
    _git(project, "tag", "-a", "finalized-object", candidate["finalized_sha"], "-m", "tag")
    tag_object = _git(project, "rev-parse", "refs/tags/finalized-object")
    _git(project, "update-ref", candidate["staging_ref"], tag_object, candidate["prepared_sha"])
    monkeypatch.setattr(completion, "_update_finalized_refs", update_finalized_refs)

    assert complete_review_ticket(tio, "change-target", policy) is False
    assert _git(project, "rev-parse", candidate["staging_ref"]) == tag_object
    _assert_destinations_unchanged(root, project, participants)
    assert tio.entry["status"] == "review"


def _interrupt_finalization_ref_update(
    monkeypatch: pytest.MonkeyPatch, role: str, timing: str
) -> tuple[Any, list[bool]]:
    cas_ref = completion._cas_ref
    interrupted: list[bool] = []

    def interrupt(
        repository: Path,
        ref: str,
        desired: str,
        expected: str | None,
    ) -> None:
        if not ref.endswith(f"/{role}") or interrupted:
            cas_ref(repository, ref, desired, expected)
            return
        interrupted.append(True)
        if timing == "before":
            raise completion.CompletionError(f"before {role} finalization ref update")
        cas_ref(repository, ref, desired, expected)
        raise completion.CompletionError(f"after {role} finalization ref update")

    monkeypatch.setattr(completion, "_cas_ref", interrupt)
    return cas_ref, interrupted


def _assert_retained_target_removal_completion(
    root: Path,
    project: Path,
    tio: _TicketIO,
    participants: tuple[ContractParticipant, ...],
) -> None:
    journal = _acceptance_journal(root)
    assert journal["state"] == "done"
    repositories = {"outer": root, "project": project}
    for participant in participants:
        repository = repositories[participant.role]
        candidate = journal["candidates"][participant.role]
        finalized = candidate["finalized_sha"]
        assert candidate["prepared_sha"] != finalized
        assert completion._ref_commit(repository, candidate["staging_ref"]) == finalized
        assert (
            completion._ref_commit(repository, participant.ticket_ref)
            == journal["sources"][participant.role]
        )
        assert completion._is_ancestor(
            repository,
            finalized,
            completion._ref_commit(repository, participant.destination_ref),
        )
        keepalive = f"refs/booley/acceptance/{journal['transaction']}/finalized-{participant.role}"
        assert completion._ref_commit(repository, keepalive) is None
    assert "  baseline:" not in _git(root, "show", "main:toy.core")
    assert "[baseline]" not in _git(project, "show", "main:tests.toml")
    assert tio.entry["status"] == "done"


@pytest.mark.parametrize("role", ["outer", "project"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_converges_each_finalization_ref_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    timing: str,
) -> None:
    root, project, tio, participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    cas_ref, interrupted = _interrupt_finalization_ref_update(monkeypatch, role, timing)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert interrupted == [True]

    monkeypatch.setattr(completion, "_cas_ref", cas_ref)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )
    _assert_retained_target_removal_completion(root, project, tio, participants)


def test_retry_recreates_absent_staging_ref_at_finalized_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, _participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    update_finalized_refs = _interrupt_finalized_ref_updates(
        monkeypatch, "before finalization ref updates"
    )
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    journal = _acceptance_journal(root)
    project_candidate = journal["candidates"]["project"]
    _git(
        project,
        "update-ref",
        "--no-deref",
        "-d",
        project_candidate["staging_ref"],
        project_candidate["prepared_sha"],
    )

    monkeypatch.setattr(completion, "_update_finalized_refs", update_finalized_refs)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )
    assert (
        completion._ref_commit(project, project_candidate["staging_ref"])
        == project_candidate["finalized_sha"]
    )


def test_target_finalization_cleanup_removes_all_journal_owned_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )

    assert (
        complete_review_ticket(
            tio,
            "change-target",
            _Policy(cleanup=True, remove_targets=(canonical,)),
        )
        is True
    )

    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    repositories = {"outer": root, "project": project}
    assert journal["state"] == "done"
    assert journal["cleaned"] == ["project", "outer"]
    for participant in participants:
        repository = repositories[participant.role]
        candidate = journal["candidates"][participant.role]
        keepalive = f"refs/booley/acceptance/{journal['transaction']}/finalized-{participant.role}"
        assert completion._ref_commit(repository, participant.ticket_ref) is None
        assert completion._ref_commit(repository, candidate["staging_ref"]) is None
        assert completion._ref_commit(repository, keepalive) is None


@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_converges_finalized_identity_journal_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
) -> None:
    root, project, tio, _participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    write_journal = completion._write_journal
    interrupted = False

    def interrupt_finalized_write(path: Path, journal: dict[str, Any]) -> None:
        nonlocal interrupted
        finalized = journal["candidates"] and all(
            candidate["finalized_sha"] is not None for candidate in journal["candidates"].values()
        )
        if finalized and not interrupted and timing == "before":
            interrupted = True
            raise OSError("before finalized identity journal write")
        write_journal(path, journal)
        if finalized and not interrupted and timing == "after":
            interrupted = True
            raise OSError("after finalized identity journal write")

    monkeypatch.setattr(completion, "_write_journal", interrupt_finalized_write)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert interrupted is True

    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if timing == "after":
        for role, repository in (("outer", root), ("project", project)):
            finalized = journal["candidates"][role]["finalized_sha"]
            unreachable = _git(repository, "fsck", "--unreachable", "--no-reflogs")
            assert f"unreachable commit {finalized}" not in unreachable

    monkeypatch.setattr(completion, "_write_journal", write_journal)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )


@pytest.mark.parametrize("role", ["outer", "project"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_converges_each_finalized_keepalive_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    timing: str,
) -> None:
    root, project, tio, _participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    cas_ref = completion._cas_ref
    interrupted = False

    def interrupt(
        repository: Path,
        ref: str,
        desired: str,
        expected: str | None,
    ) -> None:
        nonlocal interrupted
        if not ref.endswith(f"/finalized-{role}") or interrupted:
            cas_ref(repository, ref, desired, expected)
            return
        interrupted = True
        if timing == "before":
            raise completion.CompletionError(f"before {role} finalized keepalive")
        cas_ref(repository, ref, desired, expected)
        raise completion.CompletionError(f"after {role} finalized keepalive")

    monkeypatch.setattr(completion, "_cas_ref", interrupt)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert interrupted is True
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    for participant_role, repository in (("outer", root), ("project", project)):
        finalized = journal["candidates"][participant_role]["finalized_sha"]
        unreachable = _git(repository, "fsck", "--unreachable", "--no-reflogs")
        assert f"unreachable commit {finalized}" not in unreachable

    monkeypatch.setattr(completion, "_cas_ref", cas_ref)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )


@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_converges_finalization_worktree_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
) -> None:
    root, _project, tio, _participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    remove_worktrees = completion._remove_finalization_worktrees
    calls = 0

    def interrupt(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            remove_worktrees(*args, **kwargs)
            return
        if timing == "before":
            raise completion.CompletionError("before finalization worktree removal")
        remove_worktrees(*args, **kwargs)
        raise completion.CompletionError("after finalization worktree removal")

    monkeypatch.setattr(completion, "_remove_finalization_worktrees", interrupt)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert calls == 2

    monkeypatch.setattr(completion, "_remove_finalization_worktrees", remove_worktrees)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is True
    )
    acceptance_worktrees = root / ".booley_project" / ".runtime" / "acceptance-worktrees"
    assert not any(acceptance_worktrees.iterdir())


def _move_outer_staging_during_publication(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    publish_candidate = completion._publish_candidate
    moved: list[bool] = []

    def move_outer_staging_before_publish(
        repository: Path,
        participant: ContractParticipant,
        candidate: dict[str, Any],
        allowed_board_rename: tuple[Path, Path],
    ) -> None:
        if participant.role == "outer" and not moved:
            moved.append(True)
            _git(
                repository,
                "update-ref",
                candidate["staging_ref"],
                participant.destination_sha,
                candidate["finalized_sha"],
            )
        publish_candidate(repository, participant, candidate, allowed_board_rename)

    monkeypatch.setattr(completion, "_publish_candidate", move_outer_staging_before_publish)
    return moved


def test_staging_move_during_publication_leaves_its_destination_unmoved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, tio, participants, canonical = _paired_target_removal_completion(
        tmp_path, monkeypatch
    )
    moved = _move_outer_staging_during_publication(monkeypatch)
    assert (
        complete_review_ticket(tio, "change-target", _Policy(remove_targets=(canonical,))) is False
    )
    assert moved == [True]
    assert tio.entry["status"] == "review"

    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "published-project"
    assert journal["published"] == ["project"]
    outer_candidate = journal["candidates"]["outer"]
    assert (
        completion._ref_commit(root, participants[0].destination_ref)
        == participants[0].destination_sha
    )
    assert (
        completion._ref_commit(root, outer_candidate["staging_ref"])
        == participants[0].destination_sha
    )
    project_candidate = journal["candidates"]["project"]
    assert (
        completion._ref_commit(project, project_candidate["staging_ref"])
        == project_candidate["finalized_sha"]
    )
    assert completion._is_ancestor(
        project,
        project_candidate["finalized_sha"],
        completion._ref_commit(project, participants[1].destination_ref),
    )


def test_retry_rejects_changed_target_removal_policy(tmp_path: Path) -> None:
    contract = _boundary_contract()
    journal_path = tmp_path / "acceptance.json"
    first = completion._initial_journal(
        "change-target",
        contract,
        removal_targets=("acme:lib:toy:1.0#baseline",),
    )
    journal_path.write_text(json.dumps(first), encoding="utf-8")

    with pytest.raises(completion.CompletionError, match="removal policy changed"):
        completion._load_journal(
            journal_path,
            "change-target",
            contract,
            removal_targets=("acme:lib:toy:1.0#candidate",),
        )


def test_complete_publishes_project_repository_before_outer(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "rtl"
    outer_base = _repository(root)
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    project = root / ".booley_project"
    project_base = _repository(project)
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    participants = (
        ContractParticipant(
            "outer",
            outer_ticket,
            "refs/heads/change-target",
            "refs/heads/main",
            outer_base,
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    tio = _TicketIO(root, _contract(root, participants))

    assert complete_review_ticket(tio, "change-target", _Policy()) is True

    assert _git(project, "show", "main:design.txt") == "project implementation"
    assert _git(root, "show", "main:design.txt") == "outer implementation"


def test_retry_rolls_forward_after_only_project_was_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rtl"
    outer_base = _repository(root)
    outer_ticket = _ticket_commit(root, "change-target", "outer implementation\n")
    project = root / ".booley_project"
    project_base = _repository(project)
    project_ticket = _ticket_commit(
        project, "booley-ticket/change-target", "project implementation\n"
    )
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    participants = (
        ContractParticipant(
            "outer",
            outer_ticket,
            "refs/heads/change-target",
            "refs/heads/main",
            outer_base,
        ),
        ContractParticipant(
            "project",
            project_ticket,
            "refs/heads/booley-ticket/change-target",
            "refs/heads/main",
            project_base,
        ),
    )
    tio = _TicketIO(root, _contract(root, participants))
    publish = completion._publish_candidate
    failed_once = False

    def interrupt_outer(repository, participant, candidate, allowed_board_rename):
        nonlocal failed_once
        if participant.role == "outer" and not failed_once:
            failed_once = True
            raise completion.CompletionError("simulated interruption")
        publish(repository, participant, candidate, allowed_board_rename)

    monkeypatch.setattr(completion, "_publish_candidate", interrupt_outer)
    assert complete_review_ticket(tio, "change-target", _Policy()) is False
    assert _git(project, "show", "main:design.txt") == "project implementation"
    assert _git(root, "show", "main:design.txt") == "base"
    assert tio.entry["status"] == "review"

    monkeypatch.setattr(completion, "_publish_candidate", publish)
    assert complete_review_ticket(tio, "change-target", _Policy()) is True
    assert _git(root, "show", "main:design.txt") == "outer implementation"
    assert tio.entry["status"] == "done"


def test_retry_finishes_journal_after_board_approval_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))
    write_journal = completion._write_journal
    failed = False

    def fail_done_write(path: Path, journal: dict[str, Any]) -> None:
        nonlocal failed
        if journal["state"] == "done" and not failed:
            failed = True
            raise OSError("simulated journal write failure")
        write_journal(path, journal)

    monkeypatch.setattr(completion, "_write_journal", fail_done_write)
    assert complete_review_ticket(tio, "change-target", _Policy()) is False
    assert tio.entry["status"] == "done"
    assert _git(root, "show", "main:design.txt") == "implemented"

    monkeypatch.setattr(completion, "_write_journal", write_journal)
    assert complete_review_ticket(tio, "change-target", _Policy()) is True
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["state"] == "done"


def test_ticket_ref_move_after_pinning_prevents_retained_ref_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rtl"
    base = _repository(root)
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    _git(root, "switch", "-c", "late-source", ticket_sha)
    (root / "design.txt").write_text("late ref movement\n", encoding="utf-8")
    _git(root, "add", "design.txt")
    _git(root, "commit", "-m", "late source")
    late_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))
    validate_surface = completion._validate_source_surface

    def move_ref(*args: Any, **kwargs: Any) -> None:
        validate_surface(*args, **kwargs)
        _git(root, "branch", "-f", "change-target", late_sha)

    monkeypatch.setattr(completion, "_validate_source_surface", move_ref)

    assert complete_review_ticket(tio, "change-target", _Policy()) is False
    assert _git(root, "show", "main:design.txt") == "base"
    assert tio.entry["status"] == "review"


def test_schema_3_complete_rejects_target_control_drift_after_sealing(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _repository(root)
    (root / "toy.core").write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\ntargets: {}\n", encoding="utf-8"
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target")
    base = _git(root, "rev-parse", "HEAD")
    sealed = _ticket_commit(root, "change-target", "implemented\n")
    participant = ContractParticipant(
        "outer",
        sealed,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    contract = _contract(root, (participant,), schema=3)
    (root / ".booley_project").mkdir()
    _git(root, "switch", "change-target")
    (root / "toy.core").write_text(
        "CAPI=2:\nname: acme:lib:toy:2.0\ntargets: {}\n", encoding="utf-8"
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "mutate sealed target")
    _git(root, "switch", "main")
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(tio, "change-target", _Policy()) is False

    assert _git(root, "show", "main:design.txt") == "base"
    assert "toy:1.0" in _git(root, "show", "main:toy.core")
    assert tio.entry["status"] == "review"


def test_complete_rejects_concurrent_change_to_same_control_path(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _repository(root)
    core = root / "toy.core"
    core.write_text("CAPI=2:\nname: acme:lib:toy:1.0\ntargets: {}\n", encoding="utf-8")
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "change-target")
    core.write_text("CAPI=2:\nname: acme:lib:toy:2.0\ntargets: {}\n", encoding="utf-8")
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "ticket target change")
    sealed = _git(root, "rev-parse", "HEAD")
    participant = ContractParticipant(
        "outer",
        sealed,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    contract = _contract(root, (participant,))
    _git(root, "switch", "main")
    core.write_text("CAPI=2:\nname: acme:lib:toy:3.0\ntargets: {}\n", encoding="utf-8")
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "concurrent target change")
    (root / ".booley_project").mkdir()
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(tio, "change-target", _Policy()) is False

    assert "toy:3.0" in _git(root, "show", "main:toy.core")
    assert tio.entry["status"] == "review"


def test_complete_rejects_unrelated_dirty_product_edit(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _repository(root)
    unrelated = root / "unrelated.txt"
    unrelated.write_text("clean\n", encoding="utf-8")
    _git(root, "add", "unrelated.txt")
    _git(root, "commit", "-m", "add unrelated product file")
    base = _git(root, "rev-parse", "HEAD")
    ticket_sha = _ticket_commit(root, "change-target", "implemented\n")
    (root / ".booley_project").mkdir()
    participant = ContractParticipant(
        "outer",
        ticket_sha,
        "refs/heads/change-target",
        "refs/heads/main",
        base,
    )
    tio = _TicketIO(root, _contract(root, (participant,)))
    unrelated.write_text("dirty local edit\n", encoding="utf-8")

    assert complete_review_ticket(tio, "change-target", _Policy()) is False

    assert _git(root, "show", "main:design.txt") == "base"
    assert tio.entry["status"] == "review"


def _assert_paired_completion_finished(
    root: Path,
    project: Path,
    tio: _TicketIO,
    participants: tuple[ContractParticipant, ...],
    *,
    cleanup: bool,
) -> None:
    assert tio.entry["status"] == "done"
    assert _git(root, "show", "main:design.txt") == "outer implementation"
    assert _git(project, "show", "main:design.txt") == "project implementation"
    if cleanup:
        assert completion._ref_commit(root, participants[0].ticket_ref) is None
        assert completion._ref_commit(project, participants[1].ticket_ref) is None
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["state"] == "done"


@pytest.mark.parametrize("write_index", range(10))
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_survives_every_journal_write_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_index: int,
    timing: str,
) -> None:
    root, project, tio, participants = _paired_completion(tmp_path, monkeypatch)
    write_journal = completion._write_journal
    calls = 0

    def interrupt(path: Path, journal: dict[str, Any]) -> None:
        nonlocal calls
        current = calls
        calls += 1
        if current == write_index and timing == "before":
            raise OSError(f"before journal write {write_index}")
        write_journal(path, journal)
        if current == write_index and timing == "after":
            raise OSError(f"after journal write {write_index}")

    monkeypatch.setattr(completion, "_write_journal", interrupt)
    complete_review_ticket(tio, "change-target", _Policy(cleanup=True))
    assert calls > write_index

    monkeypatch.setattr(completion, "_write_journal", write_journal)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=True)) is True
    _assert_paired_completion_finished(root, project, tio, participants, cleanup=True)


@pytest.mark.parametrize(
    ("boundary", "function_name", "cleanup"),
    [
        ("candidate preparation", "_plan_candidate", False),
        ("publication", "_publish_candidate", False),
        ("retirement", "_cleanup_participant", True),
    ],
)
@pytest.mark.parametrize("role", ["project", "outer"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retry_survives_each_repository_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    function_name: str,
    cleanup: bool,
    role: str,
    timing: str,
) -> None:
    root, project, tio, participants = _paired_completion(tmp_path, monkeypatch)
    operation = getattr(completion, function_name)
    interrupted = False

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        participant = args[1]
        if participant.role != role or interrupted:
            return operation(*args, **kwargs)
        interrupted = True
        if timing == "before":
            raise completion.CompletionError(f"before {role} {boundary}")
        operation(*args, **kwargs)
        raise completion.CompletionError(f"after {role} {boundary}")

    monkeypatch.setattr(completion, function_name, interrupt)
    complete_review_ticket(tio, "change-target", _Policy(cleanup=cleanup))
    assert interrupted is True

    monkeypatch.setattr(completion, function_name, operation)
    assert complete_review_ticket(tio, "change-target", _Policy(cleanup=cleanup)) is True
    _assert_paired_completion_finished(root, project, tio, participants, cleanup=cleanup)

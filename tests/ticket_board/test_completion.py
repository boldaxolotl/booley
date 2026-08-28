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


def _contract(root: Path, participants: tuple[ContractParticipant, ...]) -> TargetContract:
    outer = next(item for item in participants if item.role == "outer")
    project = next((item for item in participants if item.role == "project"), None)
    return TargetContract(
        outer_sha=outer.sealed_sha,
        project_sha=project.sealed_sha if project else "",
        surface_digest=surface_digest(root),
        targets=(),
        participants=participants,
        surface_entries=surface_entries(root),
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
            {
                "candidates": {
                    "outer": {
                        "sha": "d" * 40,
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


def test_complete_requires_merge_policy() -> None:
    with pytest.raises(completion.CompletionError, match="requires merge policy"):
        complete_review_ticket(_BoundaryTicketIO(None), "missing", _Policy(merge=False))


def test_complete_reports_missing_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    assert complete_review_ticket(_BoundaryTicketIO(None), "missing", _Policy()) is False
    assert "ticket 'missing' not found" in capsys.readouterr().err


def test_complete_reports_malformed_contract(capsys: pytest.CaptureFixture[str]) -> None:
    tio = _BoundaryTicketIO({"status": "review", "target_contract": {}})

    assert complete_review_ticket(tio, "bad-contract", _Policy()) is False
    assert "cannot complete 'bad-contract'" in capsys.readouterr().err


def test_complete_rejects_noncanonical_or_unbound_target_removal(
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

    assert complete_review_ticket(
        tio, "change-target", _Policy(remove_targets=("baseline",))
    ) is False
    assert "sorted canonical Targets bound by the sealed contract" in capsys.readouterr().err


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
    assert "target_contract.schema must be 3" in capsys.readouterr().err


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


def test_complete_removes_target_only_from_final_merge_candidate(tmp_path: Path) -> None:
    root = tmp_path / "rtl"
    _repository(root)
    (root / "toy.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        "targets:\n"
        "  baseline: {flow: lint}\n"
        "  candidate: {flow: lint}\n",
        encoding="utf-8",
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target pair")
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
    canonical = "acme:lib:toy:1.0#baseline"
    contract = TargetContract(
        outer_sha=ticket_sha,
        project_sha="",
        surface_digest=surface_digest(root),
        targets=("baseline", "candidate"),
        bindings=(
            ContractTargetBinding(
                "lint",
                "lint_clean",
                canonical,
                "acme:lib:toy:1.0#candidate",
            ),
        ),
        participants=(participant,),
        surface_entries=surface_entries(root),
    )
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(
        tio, "change-target", _Policy(remove_targets=(canonical,))
    ) is True

    merged_core = _git(root, "show", "main:toy.core")
    assert "  baseline:" not in merged_core
    assert "  candidate:" in merged_core
    assert "  baseline:" in _git(root, "show", "change-target:toy.core")
    journal_path = root / ".booley_project" / ".runtime" / "acceptance" / "change-target.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["removal_targets"] == [canonical]
    assert journal["finalized"] is True


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
        "  baseline: {flow: lint}\n"
        "  candidate: {flow: lint}\n",
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
    canonical = "acme:lib:toy:1.0#baseline"
    contract = TargetContract(
        outer_sha=outer_ticket,
        project_sha=project_ticket,
        surface_digest=surface_digest(root),
        targets=("baseline", "candidate"),
        bindings=(
            ContractTargetBinding(
                "lint",
                "lint_clean",
                canonical,
                "acme:lib:toy:1.0#candidate",
            ),
        ),
        participants=participants,
        surface_entries=surface_entries(root),
    )
    tio = _TicketIO(root, contract)

    assert complete_review_ticket(
        tio, "change-target", _Policy(remove_targets=(canonical,))
    ) is True

    assert "  baseline:" not in _git(project, "show", "main:cores/toy.core")
    assert "  candidate:" in _git(project, "show", "main:cores/toy.core")
    merged_tests = _git(project, "show", "main:tests.toml")
    assert "[baseline]" not in merged_tests
    assert "[candidate]" in merged_tests
    assert _git(root, "show", "main:design.txt") == "outer implementation"


def test_retry_rejects_changed_target_removal_policy(tmp_path: Path) -> None:
    contract = _boundary_contract()
    journal_path = tmp_path / "acceptance.json"
    first = completion._initial_journal(
        "change-target", contract, ("acme:lib:toy:1.0#baseline",)
    )
    journal_path.write_text(json.dumps(first), encoding="utf-8")

    with pytest.raises(completion.CompletionError, match="removal policy changed"):
        completion._load_journal(
            journal_path,
            "change-target",
            contract,
            ("acme:lib:toy:1.0#candidate",),
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
        if journal.get("state") == "done" and not failed:
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


def test_ticket_ref_move_after_pinning_does_not_change_candidate(
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

    assert complete_review_ticket(tio, "change-target", _Policy()) is True
    assert _git(root, "show", "main:design.txt") == "implemented"


def test_complete_rejects_target_control_drift_after_sealing(tmp_path: Path) -> None:
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
    contract = _contract(root, (participant,))
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

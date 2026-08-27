"""Recoverable publication of sealed Ticket repository participants."""

from __future__ import annotations

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
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
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
    return sha


@dataclass(frozen=True)
class _Policy:
    merge: bool = True
    cleanup: bool = False


class _TicketIO:
    def __init__(self, root: Path, contract: TargetContract) -> None:
        self._project_root = root
        self.tickets_dir = root / ".booley_project" / "tickets"
        self.logs_dir = self.tickets_dir / "logs"
        self.entry: dict[str, Any] = {
            "file": "board/review/change-target.md",
            "status": "review",
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


def test_complete_publishes_project_repository_before_outer(
    tmp_path: Path, monkeypatch
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
    core.write_text(
        "CAPI=2:\nname: acme:lib:toy:1.0\ntargets: {}\n", encoding="utf-8"
    )
    _git(root, "add", "toy.core")
    _git(root, "commit", "-m", "add target")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "change-target")
    core.write_text(
        "CAPI=2:\nname: acme:lib:toy:2.0\ntargets: {}\n", encoding="utf-8"
    )
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
    core.write_text(
        "CAPI=2:\nname: acme:lib:toy:3.0\ntargets: {}\n", encoding="utf-8"
    )
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

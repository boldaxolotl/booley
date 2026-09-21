"""Public readiness checks at the executable-ticket boundary."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness.blocking import FatalError
from booley.harness.setup import intake
from booley.ticket_board import acceptance_validation, readiness, ticket_validation
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    TicketBaseline,
    TicketBaselineError,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_prepared_validator_materializes_submodule_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _git(dependency, "init", "-b", "main")
    _git(dependency, "config", "user.name", "Test")
    _git(dependency, "config", "user.email", "test@example.invalid")
    (dependency / "source.sv").write_text("module source; endmodule\n", encoding="utf-8")
    _git(dependency, "add", "source.sv")
    _git(dependency, "commit", "-m", "dependency")

    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "ip")
    _git(root, "commit", "-m", "project")
    sha = _git(root, "rev-parse", "HEAD")
    ticket_ref = "refs/heads/booley-generation/0123456789abcdef/outer"
    _git(root, "branch", ticket_ref.removeprefix("refs/heads/"), sha)
    ticket = root / ".booley_project/tickets/board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    basis = TicketBaseline((BasisParticipant("outer", sha, ticket_ref, "refs/heads/main", sha),))

    def prepare(_root: Path, checkout: Path, **_kwargs: object) -> SimpleNamespace:
        assert (checkout / "ip/source.sv").read_text(encoding="utf-8") == (
            "module source; endmodule\n"
        )
        return SimpleNamespace(ok=True, error="")

    monkeypatch.setattr(acceptance_validation, "prepare_project", prepare)
    monkeypatch.setattr(
        acceptance_validation, "resolve_checkout_project_dir", lambda checkout: checkout
    )
    monkeypatch.setattr(ticket_validation, "validate_ticket_spec", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(ticket_validation, "validate_ticket_view", lambda *_args: [])
    monkeypatch.setattr(ticket_validation, "assert_live_inputs_unchanged", lambda *_args: None)

    document = SimpleNamespace(spec=SimpleNamespace())
    assert (
        ticket_validation._validate_prepared_checkout(root, ticket, "ticket", document, basis)
        == []
    )


def test_executable_validator_reports_conversion_baseline_and_prepare_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    ticket = root / ".booley_project/tickets/board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(
        ticket_validation, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(
        ticket_validation, "find_ticket_file", lambda *_args, **_kwargs: (ticket, "queue")
    )

    def fail_conversion(*_args: object) -> None:
        raise TicketBaselineError("conversion failed")

    monkeypatch.setattr(
        ticket_validation,
        "_convert_executable_ticket",
        fail_conversion,
    )
    assert ticket_validation.validate_executable_ticket(root, "ticket") == ["conversion failed"]

    monkeypatch.setattr(ticket_validation, "_convert_executable_ticket", lambda *_args: object())

    def fail_baseline(*_args: object, **_kwargs: object) -> None:
        raise TicketBaselineError("baseline failed")

    monkeypatch.setattr(
        ticket_validation.TicketIO,
        "load_basis",
        fail_baseline,
    )
    assert ticket_validation.validate_executable_ticket(root, "ticket") == ["baseline failed"]

    monkeypatch.setattr(
        ticket_validation.TicketIO, "load_basis", lambda *_args, **_kwargs: object()
    )

    def fail_preparation(*_args: object) -> None:
        raise TicketBaselineError("prepare failed")

    monkeypatch.setattr(
        ticket_validation,
        "_validate_prepared_checkout",
        fail_preparation,
    )
    assert ticket_validation.validate_executable_ticket(root, "ticket") == ["prepare failed"]


def test_git_inspection_failure_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        intake.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], returncode=128, stdout="", stderr="fatal: broken repository"
        ),
    )

    with pytest.raises(FatalError, match="broken repository"):
        intake._is_git_backed(tmp_path)


def test_git_inspection_timeout_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        intake.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(["git"], 30)),
    )

    with pytest.raises(FatalError, match="Git repository inspection failed"):
        intake._is_git_backed(tmp_path)


def test_non_git_inspection_falls_back_to_draft_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        intake.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], returncode=128, stdout="", stderr="not a repository"
        ),
    )

    assert intake._is_git_backed(tmp_path) is False


def test_executable_validator_formats_conversion_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    ticket = root / ".booley_project/tickets/board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(
        ticket_validation, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(
        ticket_validation, "find_ticket_file", lambda *_args, **_kwargs: (ticket, "queue")
    )
    monkeypatch.setattr(
        ticket_validation,
        "ticket_conversion_context",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        ticket_validation,
        "convert_ticket_document",
        lambda *_args, **_kwargs: SimpleNamespace(
            document=None,
            diagnostics=(SimpleNamespace(line=3, column=4, message="bad field"),),
        ),
    )

    assert ticket_validation.validate_executable_ticket(root, "ticket") == ["3:4: bad field"]


def test_executable_validator_rejects_missing_and_nonoperational_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    ticket = root / ".booley_project/tickets/board/review/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(
        ticket_validation, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    found = iter(((None, None), (ticket, "review")))
    monkeypatch.setattr(
        ticket_validation, "find_ticket_file", lambda *_args, **_kwargs: next(found)
    )

    assert ticket_validation.validate_executable_ticket(root, "ticket") == [
        "executable Ticket Board entry 'ticket' is unavailable"
    ]
    assert ticket_validation.validate_executable_ticket(root, "ticket") == [
        "ticket 'ticket' is not operationally executable (status: review)"
    ]


def test_readiness_delegates_prepared_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    project_dir = root / ".booley_project"
    ticket = project_dir / "tickets/board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    (root / ".git").mkdir()
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda selected_root, slug: calls.append((selected_root, slug)) or [],
    )

    assert readiness.check_ticket_ready(root, "ticket").ready
    assert calls == [(root.resolve(), "ticket")]


def test_readiness_rejects_nonoperational_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    ticket = root / ".booley_project/tickets/board/review/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    (root / ".git").mkdir()
    monkeypatch.setattr(
        readiness, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "review"))

    result = readiness.check_ticket_ready(root, "ticket")

    assert result.errors == ("ticket 'ticket' is not executable (status: review)",)


def test_readiness_checkout_boundary_and_preparation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    tickets = root / ".booley_project/tickets"
    ticket = tickets / "board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        readiness, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    ticket.write_text("---\ntarget_contract: {}\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["missing required fields"],
    )
    assert "missing required fields" in readiness.check_ticket_ready(root, "ticket").errors[0]
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["prepare failed"],
    )
    assert "prepare failed" in readiness.check_ticket_ready(root, "ticket").errors[0]


def test_non_git_readiness_reports_preparation_failure_and_checkout_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("---\nbranch: main\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(readiness, "resolve_checkout_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    monkeypatch.setattr(readiness, "_checkout_statuses", lambda _root: ("clean",))
    monkeypatch.setattr("booley.flows.execution.flow_enabled", lambda *_args: False)
    monkeypatch.setattr(
        readiness,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, error="prepare failed"),
    )
    assert readiness.check_ticket_ready(tmp_path, "ticket").errors == ("prepare failed",)

    statuses = iter([("clean",), ("dirty",)])
    monkeypatch.setattr(readiness, "_checkout_statuses", lambda _root: next(statuses))
    monkeypatch.setattr(
        readiness,
        "prepare_project",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, error=""),
    )
    assert readiness.check_ticket_ready(tmp_path, "ticket").errors == (
        "project preparation changed Git-visible checkout state",
    )


def test_checkout_readiness_reports_missing_project_repository_and_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    tickets = root / ".booley_project/tickets"
    (root / ".git").mkdir(parents=True)
    ticket = tickets / "board/queue/ticket.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setattr(
        readiness, "resolve_checkout_project_dir", lambda _root: root / ".booley_project"
    )
    monkeypatch.setattr(readiness, "find_ticket_file", lambda *_args: (ticket, "queue"))
    monkeypatch.setattr(
        readiness,
        "validate_executable_ticket",
        lambda *_args: ["project participant repository is missing"],
    )
    assert (
        "project participant repository is missing"
        in readiness.check_ticket_ready(root, "ticket").errors[0]
    )

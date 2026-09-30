"""The Acceptance Journal promotes approved waivers inside its merge candidate (ADR 0066)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.coverage_waiver_application import WaiverPromotionPlan
from booley.ticket_board.acceptance_journal import (
    AcceptanceOperationError,
    AcceptanceOutcome,
    AcceptanceRequest,
    advance_acceptance,
)
from booley.ticket_board.acceptance_journal import _advance as acceptance_impl
from booley.ticket_board.acceptance_journal._repository import LocalAcceptanceRepositories
from booley.ticket_board.acceptance_journal._store import (
    AcceptanceCheckpoint,
    FaultingAcceptanceStore,
    FileAcceptanceStore,
)
from booley.ticket_board.ticket_baseline import BasisParticipant
from tests.flows.sim.test_coverage_waiver_promotion import _STAMPS, _candidate
from tests.ticket_board.test_completion import _contract, _git, _repository

_WAIVER_FILE = "coverage-waivers/rtl/counter.sv.toml"
_PROOF = "coverage-waivers/proofs/wc-0123456789ab.md"
_MESSAGE = "chore(change-target): approve coverage waivers"


def _ticket(
    tmp_path: Path, *, extra: dict[str, str] | None = None
) -> tuple[Path, AcceptanceRequest]:
    """A Ticket branch adding rtl/counter.sv (plus *extra* files) onto main."""
    root = tmp_path / "rtl"
    base = _repository(root)
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.eol", "lf")
    (root / ".booley_project").mkdir()
    _git(root, "switch", "-c", "change-target")
    files = {"rtl/counter.sv": "module counter; endmodule\n", **(extra or {})}
    for relative, content in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(content.encode("utf-8"))
    _git(root, "add", *files)
    _git(root, "commit", "-m", "implement change-target")
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    participant = BasisParticipant(
        "outer", head, "refs/heads/change-target", "refs/heads/main", base
    )
    request = AcceptanceRequest(
        root=root,
        slug="change-target",
        basis=_contract(root, (participant,)),
        cleanup=False,
        ticket_status="review",
        waiver_promotion=_plan(),
    )
    return root, request


def _plan(anchor: str = "rtl_repository") -> WaiverPromotionPlan:
    return WaiverPromotionPlan(anchor, "coverage-waivers", _STAMPS, (_candidate(),))


def test_promotion_reaches_the_destination_with_the_merge(tmp_path: Path) -> None:
    root, request = _ticket(tmp_path)

    progress = advance_acceptance(request)

    assert progress.outcome is AcceptanceOutcome.APPROVAL_REQUIRED
    assert "[[approval]]" in _git(root, "show", f"main:{_WAIVER_FILE}")
    assert "unverified" in _git(root, "show", f"main:{_PROOF}")
    assert _git(root, "log", "-1", "--format=%s", "main") == _MESSAGE
    assert _git(root, "log", "-1", "--format=%an <%ae>", "main") == _STAMPS.approved_by
    assert (
        _git(root, "log", "-1", "--format=%s", "main^")
        == "merge(change-target): recorded Ticket completed"
    )
    # The Ticket's own branch never changes.
    assert (
        _git(root, "rev-parse", "change-target")
        == request.basis.participant("outer").authoring_sha
    )


def test_without_a_plan_nothing_is_promoted(tmp_path: Path) -> None:
    root, request = _ticket(tmp_path)

    advance_acceptance(replace(request, waiver_promotion=None))

    assert _git(root, "log", "-1", "--format=%s", "main") != _MESSAGE
    assert "coverage-waivers" not in _git(root, "ls-tree", "-r", "--name-only", "main")


def test_interrupted_finalization_recomputes_the_same_promotion(tmp_path: Path) -> None:
    root, request = _ticket(tmp_path)
    store = FaultingAcceptanceStore(
        FileAcceptanceStore(), AcceptanceCheckpoint.CANDIDATES_FINALIZED, "before"
    )
    runner = acceptance_impl._AcceptanceRunner(store, LocalAcceptanceRepositories())
    with pytest.raises(OSError, match="before candidates-finalized checkpoint"):
        runner.advance(request)
    assert "coverage-waivers" not in _git(root, "ls-tree", "-r", "--name-only", "main")

    assert advance_acceptance(request).outcome is AcceptanceOutcome.APPROVAL_REQUIRED

    subjects = _git(root, "log", "--format=%s", "main").splitlines()
    assert subjects.count(_MESSAGE) == 1
    assert _git(root, "show", f"main:{_WAIVER_FILE}").count("[[approval]]") == 1


def test_colliding_destination_waivers_stop_publication(tmp_path: Path) -> None:
    """A merged directory that the real loader rejects never reaches the destination."""
    collision = (
        'schema = "booley.coverage-waivers/v1"\n'
        'source = "rtl/counter.sv"\n'
        'source_sha256 = "sha256:' + "0" * 64 + '"\n\n'
        "[[approval]]\n"
        'id = "other"\n'
    )
    root, request = _ticket(tmp_path, extra={_WAIVER_FILE: collision})
    main = _git(root, "rev-parse", "main")

    with pytest.raises(AcceptanceOperationError, match="waiver promotion failed"):
        advance_acceptance(request)

    assert _git(root, "rev-parse", "main") == main


def test_unversioned_approval_directory_is_refused_not_force_added(tmp_path: Path) -> None:
    """Single-repository project data under an ignored .booley_project stays out of RTL."""
    root, request = _ticket(tmp_path)
    main = _git(root, "rev-parse", "main")

    with pytest.raises(AcceptanceOperationError, match="not versioned"):
        advance_acceptance(replace(request, waiver_promotion=_plan("project_data_repository")))

    assert _git(root, "rev-parse", "main") == main


def _participants() -> list[dict[str, str]]:
    return [
        {
            "role": "outer",
            "authoring_sha": "a" * 40,
            "ticket_ref": "refs/heads/change-target",
            "destination_ref": "refs/heads/main",
            "destination_sha": "b" * 40,
        }
    ]


def test_journal_model_pins_the_promotion_digest() -> None:
    from booley.ticket_board.acceptance_journal._model import initial_journal, validate_journal

    digest = _plan().sha256()
    journal = initial_journal(
        "change-target", _participants(), cleanup=False, promotion_digest=digest
    )
    document = journal.as_dict()

    assert document["promotion_digest"] == digest
    assert journal.needs_finalization
    restored = validate_journal(
        document, "change-target", _participants(), cleanup=False, promotion_digest=digest
    )
    assert restored.promotion_digest == digest
    with pytest.raises(ValueError, match="waiver promotion changed"):
        validate_journal(
            document,
            "change-target",
            _participants(),
            cleanup=False,
            promotion_digest="sha256:" + "0" * 64,
        )


def test_journals_without_promotion_keep_their_shape() -> None:
    from booley.ticket_board.acceptance_journal._model import initial_journal, validate_journal

    journal = initial_journal("change-target", _participants(), cleanup=False)

    assert "promotion_digest" not in journal.as_dict()
    assert not journal.needs_finalization
    restored = validate_journal(journal.as_dict(), "change-target", _participants(), cleanup=False)
    assert restored.promotion_digest is None

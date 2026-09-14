"""Board amendment publication keeps a blocked Ticket's implementation lineage."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.ticket_board import amendment
from booley.ticket_board.acceptance_basis import (
    AcceptanceBasis,
    BasisParticipant,
    load_acceptance_basis,
    worktree_for_ref,
)
from booley.ticket_board.amendment import (
    AmendmentError,
    _reevaluate_changed_entry,
    apply_amendment,
    preview_amendment,
)
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.paths import runtime_file
from booley.ticket_board.validation import validate_ticket_fields

from .test_acceptance_basis import _blocked_ticket, _git, _paired_basis_project


def _optional_request() -> dict:
    return {
        "reason": "Human approved an optional review",
        "feedback": "Continue the existing implementation.",
        "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
    }


def test_optional_conversion_preserves_dirty_source_and_queues(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    old_fields, body = parse_frontmatter(blocked.read_text(encoding="utf-8"))
    old_basis = load_acceptance_basis(root, "blocked-again", old_fields, body)
    old_head = _git(root, "rev-parse", old_basis.participant("outer").ticket_ref)
    worktree = worktree_for_ref(root, old_basis.participant("outer").ticket_ref)
    assert worktree is not None
    (worktree / "README.md").write_text("unfinished implementation\n", encoding="utf-8")
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()

    preview = preview_amendment(tio, "blocked-again", request)
    assert preview["changes"][0]["before_mandatory"] is True
    assert preview["changes"][0]["after_mandatory"] is False
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])

    assert result["status"] == "queued"
    queued = blocked.parent.parent / "queue" / blocked.name
    fields, body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = load_acceptance_basis(root, "blocked-again", fields, body)
    assert tio.load_basis("blocked-again").basis_id == basis.basis_id
    new_head = _git(root, "rev-parse", basis.participant("outer").ticket_ref)
    assert _git(root, "merge-base", "--is-ancestor", old_head, new_head) == ""
    assert _git(root, "merge-base", "--is-ancestor", basis.outer_sha, new_head) == ""
    assert _git(worktree, "show", "HEAD:README.md") == "unfinished implementation"
    assert _git(root, "show", f"{basis.outer_sha}:README.md") == "demo"
    current = DevelopmentState.load(
        runtime_file(tio.logs_dir, "blocked-again", "booley_state.json")
    )
    assert current.criteria["review_rtl_bugs_clean"].mandatory is False
    assert current.authorized_zero_mandatory_basis_id == basis.basis_id
    assert validate_ticket_fields(fields, body, project_root=root) == []
    assert (tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.json").exists()
    assert (
        tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.prior-state.json"
    ).exists()


def test_stale_preview_rejected_without_publication(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    fields, _ = parse_frontmatter(blocked.read_text(encoding="utf-8"))
    basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    (checkout / "README.md").write_text("changed after preview\n")

    with pytest.raises(AmendmentError, match="stale"):
        apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert blocked.exists()
    assert not (tio.logs_dir / "blocked-again/amendments").exists()


def test_paired_publication_retains_both_implementation_participants(tmp_path: Path) -> None:
    from booley.ticket_board.io import TicketFileSpec

    root, project_dir, tio = _paired_basis_project(tmp_path)
    slug = "paired-amendment"
    created = tio.create_ticket_file(
        slug,
        TicketFileSpec(
            summary="Amend both participants",
            ticket_type="feature",
            branch="main",
            scope=["README.md"],
            criteria={"mandatory": {"review_rtl_bugs": True}},
            body="## Description\n\nAmend requirements.\n",
        ),
    )
    assert created is not None
    assert tio.enqueue_ticket(slug)
    (tio.logs_dir / slug / ".runtime/ticket.lock").unlink(missing_ok=True)
    queued = project_dir / "tickets/board/queue" / f"{slug}.md"
    blocked = project_dir / "tickets/board/blocked" / queued.name
    blocked.parent.mkdir(parents=True, exist_ok=True)
    queued.replace(blocked)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, slug, "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    old_fields, body = parse_frontmatter(blocked.read_text(encoding="utf-8"))
    old_basis = load_acceptance_basis(root, slug, old_fields, body)
    outer = worktree_for_ref(root, old_basis.participant("outer").ticket_ref)
    project = worktree_for_ref(project_dir, old_basis.participant("project").ticket_ref)
    assert outer is not None and project is not None
    (outer / "README.md").write_text("paired implementation\n")
    preview = preview_amendment(tio, slug, _optional_request())
    result = apply_amendment(tio, slug, _optional_request(), preview["digest"])
    assert result["status"] == "queued"
    current_fields, current_body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = load_acceptance_basis(root, slug, current_fields, current_body)
    assert tio.load_basis(slug).basis_id == basis.basis_id
    assert basis.outer_sha != old_basis.outer_sha
    assert basis.project_sha != old_basis.project_sha
    assert _git(outer, "show", "HEAD:README.md") == "paired implementation"
    assert _git(root, "show", f"{basis.outer_sha}:README.md") == "demo"
    assert validate_ticket_fields(current_fields, current_body, project_root=root) == []


def test_publication_interruption_rolls_forward_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    original = amendment._publish_board_and_state
    calls = 0

    def fail_once(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected post-ref interruption")
        original(*args)

    monkeypatch.setattr(amendment, "_publish_board_and_state", fail_once)
    with pytest.raises(OSError, match="injected"):
        apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert blocked.exists()
    with pytest.raises(Exception, match="amendment publication is pending"):
        tio._load_basis_unlocked("blocked-again")
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])
    queued = blocked.parent.parent / "queue" / blocked.name
    assert result["status"] == "queued" and queued.exists()
    history = tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.json"
    assert history.exists()
    assert amendment.pending_amendment(root, "blocked-again") is None
    assert len(list(history.parent.glob("*.json"))) == 2


def test_return_to_draft_clears_amendment_marker(tmp_path: Path) -> None:
    _root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    queued = blocked.parent.parent / "queue" / blocked.name
    queued.replace(blocked)
    tio.return_to_draft("blocked-again")
    draft = tio.tickets_dir / "board/drafts/blocked-again.md"
    fields, _body = parse_frontmatter(draft.read_text(encoding="utf-8"))
    assert "acceptance_basis" not in fields
    assert "acceptance_amendment" not in fields


def test_reset_uses_latest_requirements_and_original_authoring_source(tmp_path: Path) -> None:
    from booley.ticket_board.operations import op_reset

    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    old_fields, body = parse_frontmatter(blocked.read_text(encoding="utf-8"))
    old_basis = load_acceptance_basis(root, "blocked-again", old_fields, body)
    checkout = worktree_for_ref(root, old_basis.participant("outer").ticket_ref)
    assert checkout is not None
    (checkout / "README.md").write_text("discard on explicit reset\n")
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    queued = blocked.parent.parent / "queue" / blocked.name
    queued.replace(blocked)
    assert op_reset(tio, "blocked-again", reason="Start clean with revised criteria")
    fields, body = parse_frontmatter(queued.read_text(encoding="utf-8"))
    basis = load_acceptance_basis(root, "blocked-again", fields, body)
    restored = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert restored is not None
    assert _git(restored, "show", "HEAD:README.md") == "demo"
    assert fields["criteria"]["optional"]["review_rtl_bugs"] is True


def test_numeric_reuse_requires_original_producer_success(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/design.sv").write_text("module design; endmodule\n")
    stamp = {
        "categories": ["rtl"],
        "fingerprint": compute_source_fingerprint(tmp_path),
    }
    basis = AcceptanceBasis(
        (
            BasisParticipant(
                "outer",
                "a" * 40,
                "refs/heads/booley-generation/0123456789abcdef/demo",
                "refs/heads/main",
                "b" * 40,
            ),
        )
    )
    journal = {"basis": basis.as_dict()}
    for producer_passed, expected in ((True, True), (False, False)):
        entry = CriterionEntry(
            met=False,
            params={"fmax_mhz_min": 430},
            detail={
                "fmax_mhz": 438,
                "implementation": {
                    "status": {
                        "passed": producer_passed,
                        "tool_returncode": 0,
                        "timed_out": False,
                        "infra_error": None,
                        "failure_reasons": [],
                    }
                },
                SOURCE_FINGERPRINT_DETAIL_KEY: stamp,
            },
        )
        _reevaluate_changed_entry(entry, basis, journal, tmp_path)
        assert entry.met is expected

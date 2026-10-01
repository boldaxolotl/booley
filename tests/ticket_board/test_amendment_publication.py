"""Board amendment publication keeps a blocked Ticket's implementation lineage."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.ticket_board import amendment
from booley.ticket_board.amendment import (
    AmendmentError,
    _reevaluate_changed_entry,
    apply_amendment,
    preview_amendment,
)
from booley.ticket_board.amendment_proposal import AmendmentProposal, CriterionChange
from booley.ticket_board.board_layout import read_state_record, write_state_record
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState
from booley.ticket_board.paths import human_log_file, runtime_file
from booley.ticket_board.ticket_baseline import (
    BasisParticipant,
    TicketBaseline,
    TicketBaselineError,
    ticket_baseline_from_machine,
    worktree_for_ref,
)
from booley.ticket_board.ticket_document import TicketAuthoringView, TicketConversionContext
from booley.ticket_board.validation import validate_ticket_spec

from .test_ticket_baseline import _blocked_ticket, _create_v2_ticket, _git, _paired_basis_project


def _state(tio: TicketIO, slug: str) -> TicketState | None:
    """Return *slug*'s recorded lifecycle state, or None for a draft."""
    record = read_state_record(tio.tickets_dir, slug)
    return None if record is None else record.state


def _set_state(tio: TicketIO, slug: str, state: TicketState) -> None:
    """Move *slug* to *state* by rewriting its state record, as a test shortcut."""
    record = read_state_record(tio.tickets_dir, slug)
    assert record is not None
    write_state_record(tio.tickets_dir, slug, record.with_state(state))


def _v2_fields(source: str) -> tuple[dict, str]:
    frontmatter, body = source[4:].split("\n---\n", 1)
    return yaml.safe_load(frontmatter), body


def _optional_request() -> dict:
    return {
        "actor": "QA Human",
        "reason": "Human approved an optional review",
        "feedback": "Continue the existing implementation.",
        "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
    }


def test_amendment_serializer_error_is_domain_error(tmp_path: Path, monkeypatch) -> None:
    @contextmanager
    def conversion_context(*_args, **_kwargs):
        yield object()

    def fail_serialization(*_args, **_kwargs):
        raise ValueError("invalid output")

    monkeypatch.setattr(amendment, "ticket_conversion_context", conversion_context)
    monkeypatch.setattr(amendment, "serialize_ticket_document", fail_serialization)

    with pytest.raises(AmendmentError, match="invalid amended Ticket: invalid output"):
        amendment._serialize_ticket(tmp_path, "ticket", object())


def test_optional_conversion_preserves_dirty_source_and_queues(tmp_path: Path) -> None:
    authored_body = "\n## Description\n\nRecover publication.  \n\n"
    root, blocked, tio = _blocked_ticket(tmp_path, body=authored_body)
    _old_fields, old_body = _v2_fields(blocked.read_text(encoding="utf-8"))
    old_basis = tio.load_basis("blocked-again")
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
    assert _state(tio, "blocked-again") is TicketState.QUEUED
    _fields, body = _v2_fields(blocked.read_text(encoding="utf-8"))
    assert body == old_body
    assert body == authored_body
    basis = tio.load_basis("blocked-again")
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
    assert validate_ticket_spec(tio.load_document("blocked-again").spec, project_root=root) == []
    assert (tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.json").exists()
    assert (
        tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.prior-state.json"
    ).exists()
    history = json.loads(
        (tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.json").read_text()
    )
    assert history["actor"] == "QA Human"
    assert history["evidence_outcomes"]["review_rtl_bugs_clean"] == {
        "result": "unmet",
        "evidence": "unchanged",
    }
    assert "Resume Ticket Mode" in history["next_action"]
    assert (
        "## Amendment" in human_log_file(tio.logs_dir, "blocked-again", "blocked.md").read_text()
    )


def test_amendment_created_stamp_survives_execution_init(tmp_path: Path) -> None:
    _root, blocked, tio = _blocked_ticket(tmp_path)
    created = "2026-09-29T00:00:00Z"
    tio._write_spec_fields(blocked, {"created": created, "feature_branch": "blocked-again"})
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert _state(tio, "blocked-again") is TicketState.QUEUED

    assert tio.init_ticket(blocked) is not None
    assert _state(tio, "blocked-again") is TicketState.RUNNING
    active = tio.tickets_dir / "board/blocked-again.md"
    active_fields, _active_body = _v2_fields(active.read_text(encoding="utf-8"))
    assert active_fields["created"] == created
    assert active_fields["feature_branch"] == "blocked-again"


def _numeric_amendment_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, blocked, tio = _blocked_ticket(tmp_path)
    basis = tio.load_basis("blocked-again")
    fields, body = _v2_fields(blocked.read_text(encoding="utf-8"))
    fields["CRITERIA_MANDATORY"] = {
        "SYNTH": {"synth_core": {"area_um2_max": 100, "fmax_mhz_min": 500}}
    }
    blocked.write_bytes(amendment._render_ticket(fields, body).encode("utf-8"))

    @contextmanager
    def conversion_context(_root: Path, _slug: str, mode: str):
        view = TicketAuthoringView(
            resolve_target=lambda selector, _flow: selector,
            tests_for_target=lambda _target: (),
        )
        yield TicketConversionContext(mode, lambda _generated: view)

    monkeypatch.setattr(amendment, "ticket_conversion_context", conversion_context)
    monkeypatch.setattr(
        amendment,
        "load_ticket_baseline_from_document",
        lambda _root, _slug, document: (
            ticket_baseline_from_machine(document.generated["machine"])
            if "amendment" in document.generated.get("machine", {})
            else basis
        ),
    )
    monkeypatch.setattr(amendment, "validate_ticket_spec", lambda *_args, **_kwargs: [])
    document = amendment._convert_ticket(root, "blocked-again", blocked.read_text())
    fields["machine"]["authored_sha256"] = document.spec.semantic_digest()
    blocked.write_bytes(amendment._render_ticket(fields, body).encode("utf-8"))
    document = amendment._convert_ticket(root, "blocked-again", blocked.read_text())
    rows = {row.parameter: row for row in document.spec.criteria}
    fmax = rows["fmax_mhz_min"]
    area = rows["area_um2_max"]
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.slug = "blocked-again"
    state.init_criteria(
        {fmax.identity: True, area.identity: True},
        criterion_params={
            fmax.identity: {"target": "synth_core", "fmax_mhz_min": 500},
            area.identity: {"target": "synth_core", "area_um2_max": 100},
        },
    )
    state.save()
    return tio, fmax, area


def test_numeric_amendment_rebuilds_only_the_relaxed_atomic_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tio, fmax, area = _numeric_amendment_setup(tmp_path, monkeypatch)
    request = {
        "actor": "QA Human",
        "reason": "Accept the measured clock",
        "feedback": "Continue with the relaxed frequency threshold.",
        "criteria": [{"criterion": fmax.identity, "thresholds": {"fmax_mhz_min": 430}}],
    }

    preview = preview_amendment(tio, "blocked-again", request)
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])

    assert result["status"] == "queued"
    rebuilt = DevelopmentState.load(
        runtime_file(tio.logs_dir, "blocked-again", "booley_state.json")
    )
    assert rebuilt.criteria[fmax.identity].params["fmax_mhz_min"] == 430
    assert rebuilt.criteria[area.identity].params["area_um2_max"] == 100


def test_repeated_amendment_preserves_ticket_only_authority(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path, extra_file="EXTRA.md")
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    first_request = _optional_request()
    first_preview = preview_amendment(tio, "blocked-again", first_request)
    apply_amendment(tio, "blocked-again", first_request, first_preview["digest"])
    assert _state(tio, "blocked-again") is TicketState.QUEUED
    first_fields, _first_body = _v2_fields(blocked.read_text(encoding="utf-8"))
    first_basis = tio.load_basis("blocked-again")

    _set_state(tio, "blocked-again", TicketState.BLOCKED)
    second_request = {
        "actor": "QA Human",
        "reason": "Include the additional source",
        "scope_add": ["EXTRA.md"],
    }
    second_preview = preview_amendment(tio, "blocked-again", second_request)
    apply_amendment(tio, "blocked-again", second_request, second_preview["digest"])
    fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = tio.load_basis("blocked-again")

    assert basis.outer_sha != first_basis.outer_sha
    assert (
        fields["machine"]["amendment"]["previous_generation"]
        == first_fields["machine"]["generation"]
    )
    assert fields["machine"]["amendment"]["optional_conversions"] == ["review_rtl_bugs_clean"]
    assert fields["scope"] == ["README.md", "EXTRA.md"]
    assert validate_ticket_spec(tio.load_document("blocked-again").spec, project_root=root) == []
    assert not list(root.rglob("acceptance/bases/*.json"))


def test_stale_preview_rejected_without_publication(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    fields, _ = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = ticket_baseline_from_machine(fields["machine"])
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    (checkout / "README.md").write_text("changed after preview\n")

    with pytest.raises(AmendmentError, match="stale"):
        apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert blocked.exists()
    assert not (tio.logs_dir / "blocked-again/amendments").exists()


def test_apply_preserves_canonical_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)

    def fail_inspection(*_args: object, **_kwargs: object) -> None:
        raise AmendmentError("acceptance-input-change-required: post-setup hook timed out")

    monkeypatch.setattr(amendment, "_inspection", fail_inspection)

    with pytest.raises(AmendmentError, match=r"acceptance-input-change-required.*timed out"):
        apply_amendment(tio, "blocked-again", request, preview["digest"])


def test_reference_matched_marker_change_makes_apply_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    marker = checkout / "picosoc/FUSESOC_IGNORE"
    marker.parent.mkdir()
    marker.write_text("stable", encoding="utf-8")
    original = amendment.prepare_acceptance_checkout

    def prepare_reference(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        reference_marker = Path(args[1]) / "picosoc/FUSESOC_IGNORE"
        reference_marker.parent.mkdir()
        reference_marker.write_text("stable", encoding="utf-8")
        return result

    monkeypatch.setattr(amendment, "prepare_acceptance_checkout", prepare_reference)
    preview = preview_amendment(tio, "blocked-again", _optional_request())
    assert preview["source_state"]["outer"]["ignored_generated"] == {
        "picosoc/FUSESOC_IGNORE": {
            "kind": "reference",
            "sha256": hashlib.sha256(b"stable").hexdigest(),
            "executable": False,
        }
    }
    marker.write_text("changed", encoding="utf-8")

    with pytest.raises(AmendmentError, match=r"acceptance-input-change-required.*FUSESOC_IGNORE"):
        apply_amendment(tio, "blocked-again", _optional_request(), preview["digest"])


def test_new_protected_marker_after_preview_makes_apply_stale(tmp_path: Path) -> None:
    root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    preview = preview_amendment(tio, "blocked-again", _optional_request())
    marker = checkout / "new/FUSESOC_IGNORE"
    marker.parent.mkdir()
    marker.write_text("new", encoding="utf-8")

    with pytest.raises(AmendmentError, match=r"acceptance-input-change-required.*FUSESOC_IGNORE"):
        apply_amendment(tio, "blocked-again", _optional_request(), preview["digest"])


def test_recovery_rechecks_ignored_generated_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    marker = checkout / "picosoc/FUSESOC_IGNORE"
    marker.parent.mkdir()
    marker.write_text("stable", encoding="utf-8")
    original_prepare = amendment.prepare_acceptance_checkout

    def prepare_reference(*args: object, **kwargs: object) -> object:
        result = original_prepare(*args, **kwargs)
        reference_marker = Path(args[1]) / "picosoc/FUSESOC_IGNORE"
        reference_marker.parent.mkdir()
        reference_marker.write_text("stable", encoding="utf-8")
        return result

    monkeypatch.setattr(amendment, "prepare_acceptance_checkout", prepare_reference)
    preview = preview_amendment(tio, "blocked-again", _optional_request())
    prepare_participants = amendment._prepare_amendment_participants

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(
        amendment,
        "_prepare_amendment_participants",
        interrupt,
    )
    with pytest.raises(OSError, match="interrupted"):
        apply_amendment(tio, "blocked-again", _optional_request(), preview["digest"])
    monkeypatch.setattr(amendment, "_prepare_amendment_participants", prepare_participants)
    marker.write_text("changed", encoding="utf-8")

    with pytest.raises(AmendmentError, match="generated source"):
        apply_amendment(tio, "blocked-again", _optional_request(), preview["digest"])


def test_apply_requires_exact_current_preview_digest(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    with pytest.raises(AmendmentError, match="exact preview digest"):
        apply_amendment(tio, "blocked-again", request, "")
    with pytest.raises(AmendmentError, match="stale"):
        apply_amendment(tio, "blocked-again", request, "f" * 64)
    assert blocked.exists()
    assert amendment.pending_amendment(root, "blocked-again") is None
    assert preview["digest"] != "f" * 64


def test_preview_rejects_unblocked_ticket(tmp_path: Path) -> None:
    _, _blocked, tio = _blocked_ticket(tmp_path)
    _set_state(tio, "blocked-again", TicketState.QUEUED)
    with pytest.raises(AmendmentError, match="must be blocked"):
        preview_amendment(tio, "blocked-again", _optional_request())


def test_preview_directs_authored_drift_to_return_to_draft(tmp_path: Path) -> None:
    _root, blocked, tio = _blocked_ticket(tmp_path)
    blocked.write_bytes(
        blocked.read_text(encoding="utf-8")
        .replace("summary: Recover publication", "summary: Edited while blocked")
        .encode(),
    )

    with pytest.raises(
        AmendmentError,
        match="acceptance-input-change-required: authored Ticket changed; use return-to-draft",
    ):
        preview_amendment(tio, "blocked-again", _optional_request())


def test_preview_reports_preparation_failure_as_amendment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)

    def fail_preparation(*_args: object, **_kwargs: object) -> None:
        raise TicketBaselineError(
            "acceptance-input-change-required: post-setup hook timed out (900s)"
        )

    monkeypatch.setattr(amendment, "prepare_acceptance_checkout", fail_preparation)

    with pytest.raises(
        AmendmentError,
        match=r"acceptance-input-change-required: post-setup hook timed out \(900s\)",
    ):
        preview_amendment(tio, "blocked-again", _optional_request())


def test_preview_rejects_dirty_source_symlink(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    _fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    source = checkout / "README.md"
    source.unlink()
    source.symlink_to(tmp_path / "external-readme.md")
    with pytest.raises(AmendmentError, match="symlink"):
        preview_amendment(tio, "blocked-again", _optional_request())
    assert amendment.pending_amendment(root, "blocked-again") is None


@pytest.mark.parametrize("state_condition", ["missing", "unreadable", "criterion_absent"])
def test_missing_current_state_rejected_before_publication(
    tmp_path: Path, state_condition: str
) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    state_path = runtime_file(tio.logs_dir, "blocked-again", "booley_state.json")
    if state_condition == "missing":
        state_path.unlink(missing_ok=True)
    elif state_condition == "unreadable":
        state_path.write_text("not JSON")
    else:
        state = DevelopmentState.load(state_path)
        state.criteria.clear()
        state.save()
    with pytest.raises(AmendmentError, match=r"state|Criteria"):
        preview_amendment(tio, "blocked-again", _optional_request())
    assert blocked.exists()
    assert amendment.pending_amendment(root, "blocked-again") is None


def test_pending_amendment_rejects_corrupt_journal(tmp_path: Path) -> None:
    path = amendment._journal_path(tmp_path, "blocked-again")
    path.parent.mkdir(parents=True)
    path.write_text("not JSON")
    with pytest.raises(AmendmentError, match="unreadable"):
        amendment.pending_amendment(tmp_path, "blocked-again")
    path.write_text(json.dumps({"schema": 1, "slug": "blocked-again", "operation_id": "short"}))
    with pytest.raises(AmendmentError, match="invalid identity"):
        amendment.pending_amendment(tmp_path, "blocked-again")


def test_scope_expansion_does_not_checkpoint_prior_out_of_scope_dirty_file(
    tmp_path: Path,
) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path, extra_file="EXTRA.md")
    _fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert checkout is not None
    (checkout / "EXTRA.md").write_text("uncommitted implementation\n")
    request = {"actor": "QA Human", "reason": "Expand Scope", "scope_add": ["EXTRA.md"]}

    with pytest.raises(AmendmentError, match="outside prior Scope"):
        preview_amendment(tio, "blocked-again", request)
    assert (checkout / "EXTRA.md").read_text() == "uncommitted implementation\n"
    assert amendment.pending_amendment(root, "blocked-again") is None


def test_scope_expansion_rechecks_mandatory_sim_policy(tmp_path: Path) -> None:
    root, blocked, tio = _blocked_ticket(tmp_path)
    request = {
        "actor": "QA Human",
        "reason": "Add RTL Scope",
        "scope_add": ["rtl/new.sv [new]"],
    }
    with pytest.raises(AmendmentError, match="mandatory SIM"):
        preview_amendment(tio, "blocked-again", request)
    assert blocked.exists()
    assert amendment.pending_amendment(root, "blocked-again") is None


def test_paired_publication_retains_both_implementation_participants(tmp_path: Path) -> None:
    from booley.ticket_board.io import TicketFileSpec

    root, project_dir, tio = _paired_basis_project(tmp_path)
    slug = "paired-amendment"
    created = _create_v2_ticket(
        tio,
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
    blocked = project_dir / "tickets/board" / f"{slug}.md"
    _set_state(tio, slug, TicketState.BLOCKED)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, slug, "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    _old_fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    old_basis = tio.load_basis(slug)
    outer = worktree_for_ref(root, old_basis.participant("outer").ticket_ref)
    project = worktree_for_ref(project_dir, old_basis.participant("project").ticket_ref)
    assert outer is not None and project is not None
    (outer / "README.md").write_text("paired implementation\n")
    preview = preview_amendment(tio, slug, _optional_request())
    result = apply_amendment(tio, slug, _optional_request(), preview["digest"])
    assert result["status"] == "queued"
    assert _state(tio, slug) is TicketState.QUEUED
    _current_fields, _current_body = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = tio.load_basis(slug)
    assert tio.load_basis(slug).basis_id == basis.basis_id
    assert basis.outer_sha != old_basis.outer_sha
    assert basis.project_sha != old_basis.project_sha
    assert _git(outer, "show", "HEAD:README.md") == "paired implementation"
    assert _git(root, "show", f"{basis.outer_sha}:README.md") == "demo"
    assert validate_ticket_spec(tio.load_document(slug).spec, project_root=root) == []


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
    with pytest.raises(AmendmentError, match="publication is pending"):
        preview_amendment(tio, "blocked-again", request)
    with pytest.raises(AmendmentError, match="different preview digest"):
        apply_amendment(tio, "blocked-again", request, "f" * 64)
    with pytest.raises(Exception, match="amendment publication is pending"):
        tio._load_basis_unlocked("blocked-again")
    result = apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert _state(tio, "blocked-again") is TicketState.QUEUED
    assert result["status"] == "queued" and blocked.exists()
    history = tio.logs_dir / "blocked-again/amendments" / f"{result['operation_id']}.json"
    assert history.exists()
    assert amendment.pending_amendment(root, "blocked-again") is None
    assert len(list(history.parent.glob("*.json"))) == 3
    assert (history.parent / f"{result['operation_id']}.prior-evidence.json").exists()


def test_pre_generated_identity_journal_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    original = amendment._prepare_amendment_participants

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected pre-prepare interruption")

    monkeypatch.setattr(amendment, "_prepare_amendment_participants", interrupt)
    with pytest.raises(OSError, match="pre-prepare"):
        apply_amendment(tio, "blocked-again", request, preview["digest"])
    journal = amendment.pending_amendment(root, "blocked-again")
    assert journal is not None
    for snapshot in journal["source_state"].values():
        snapshot.pop("ignored_generated")
    amendment._write_journal(root, journal)
    monkeypatch.setattr(amendment, "_prepare_amendment_participants", original)

    result = apply_amendment(tio, "blocked-again", request, preview["digest"])

    assert result["status"] == "queued"


def test_return_to_draft_clears_amendment_marker(tmp_path: Path) -> None:
    _root, _blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert _state(tio, "blocked-again") is TicketState.QUEUED
    _set_state(tio, "blocked-again", TicketState.BLOCKED)
    tio.return_to_draft("blocked-again")
    assert _state(tio, "blocked-again") is None
    draft = tio.tickets_dir / "board/blocked-again.md"
    fields, _body = _v2_fields(draft.read_text(encoding="utf-8"))
    assert "machine" not in fields


def test_reset_uses_latest_requirements_and_original_authoring_source(tmp_path: Path) -> None:
    from booley.ticket_board.operations import op_reset

    root, blocked, tio = _blocked_ticket(tmp_path)
    state = DevelopmentState.load(runtime_file(tio.logs_dir, "blocked-again", "booley_state.json"))
    state.init_criteria({"review_rtl_bugs_clean": True})
    state.save()
    _old_fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    old_basis = tio.load_basis("blocked-again")
    checkout = worktree_for_ref(root, old_basis.participant("outer").ticket_ref)
    assert checkout is not None
    (checkout / "README.md").write_text("discard on explicit reset\n")
    request = _optional_request()
    preview = preview_amendment(tio, "blocked-again", request)
    apply_amendment(tio, "blocked-again", request, preview["digest"])
    assert _state(tio, "blocked-again") is TicketState.QUEUED
    _set_state(tio, "blocked-again", TicketState.BLOCKED)
    assert op_reset(tio, "blocked-again", reason="Start clean with revised criteria")
    fields, _body = _v2_fields(blocked.read_text(encoding="utf-8"))
    basis = tio.load_basis("blocked-again")
    restored = worktree_for_ref(root, basis.participant("outer").ticket_ref)
    assert restored is not None
    assert _git(restored, "show", "HEAD:README.md") == "demo"
    assert fields["CRITERIA_OPTIONAL"]["REVIEW"]["rtl"]["bugs"] == "clean"


def test_numeric_reuse_requires_original_producer_success(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/design.sv").write_text("module design; endmodule\n")
    stamp = {
        "categories": ["rtl"],
        "fingerprint": compute_source_fingerprint(tmp_path),
    }
    basis = TicketBaseline(
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


def test_numeric_preview_reports_reused_evidence_without_changing_state(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/design.sv").write_text("module design; endmodule\n")
    basis = TicketBaseline(
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
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.criteria["synthesis_ok_synth"] = CriterionEntry(
        met=False,
        params={"fmax_mhz_min": 500},
        detail={
            "fmax_mhz": 438,
            "implementation": {
                "status": {
                    "passed": True,
                    "tool_returncode": 0,
                    "timed_out": False,
                    "infra_error": None,
                    "failure_reasons": [],
                }
            },
            SOURCE_FINGERPRINT_DETAIL_KEY: {
                "categories": ["rtl"],
                "fingerprint": compute_source_fingerprint(tmp_path),
            },
        },
    )
    state.save()
    proposal = AmendmentProposal(
        fields={
            "criteria": {
                "mandatory": {
                    "synthesis_ok": {
                        "targets": ["synth"],
                        "fmax_mhz_min": 430,
                    }
                }
            }
        },
        actor="QA Human",
        reason="Accept measured clock",
        feedback="",
        changes=(
            CriterionChange(
                "synthesis_ok_synth",
                True,
                True,
                {"fmax_mhz_min": (500, 430)},
            ),
        ),
        scope_added=(),
    )
    result = amendment._preview_evidence(
        state_path,
        proposal,
        basis,
        tmp_path,
        {"synthesis_ok_synth": {"fmax_mhz_min": 430}},
    )
    assert result["synthesis_ok_synth"] == {
        "result": "unmet",
        "evidence": "reuse",
        "after_result": "met",
    }
    assert DevelopmentState.load(state_path).criteria["synthesis_ok_synth"].params == {
        "fmax_mhz_min": 500,
    }

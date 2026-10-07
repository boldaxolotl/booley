"""Persisted report authority survives interruptions without resurrecting positives."""

from __future__ import annotations

import hashlib

import pytest

from booley.ticket_board import report_submission as rs


def _detail(attempt):
    return {rs.ID_KEY: attempt.row["submission_id"], rs.DIGEST_KEY: attempt.row["report_sha256"]}


@pytest.mark.parametrize("checkpoint", ["pending", "report", "positive", "state", "publication"])
def test_restart_at_precommit_checkpoint_masks_positive(tmp_path, checkpoint):
    from booley.criteria.evidence_ledger import replay_projection
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import (
        TICKET_REPORT_PROJECTION,
        TICKET_SCOPE,
        record_changes,
    )

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.slug = "report"
    state.init_criteria({rs.KEY: True}, strict=True)
    state.save()
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    detail = {rs.ID_KEY: "a" * 32}
    if checkpoint != "pending":
        attempt.stage(b"candidate")
        detail = _detail(attempt)
    if checkpoint in {"positive", "state", "publication"}:
        changes = state.set_criterion(rs.KEY, True, detail=detail)
        record_changes(
            tmp_path,
            state,
            changes,
            invocation_id="report",
            producer="report",
            execution_id="execution",
            ticket_identity={},
        )
    if checkpoint in {"state", "publication"}:
        state.save()
    if checkpoint == "publication":
        (tmp_path / ".runtime/candidate-result.json").write_text('{"criterion_met": true}')
    attempt.close()
    reloaded = DevelopmentState.load(state._file_path)
    replay_projection(TICKET_SCOPE, TICKET_REPORT_PROJECTION, tmp_path, reloaded, {})
    assert not reloaded.is_met(rs.KEY)
    assert not rs.effective_met(tmp_path, True, detail, identity={})
    assert rs.read_receipt(tmp_path)["status"] == "pending"


def test_completed_receipt_only_validates_own_positive(tmp_path):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    detail = _detail(attempt)
    attempt.commit()
    attempt.close()
    assert rs.effective_met(tmp_path, True, detail, identity={})
    assert not rs.effective_met(tmp_path, False, detail, identity={})
    assert not rs.effective_met(tmp_path, True, {**detail, rs.ID_KEY: "b" * 32}, identity={})
    (tmp_path / "REPORT.md").write_bytes(b"changed")
    assert not rs.effective_met(tmp_path, True, detail, identity={})


def test_retry_pending_masks_legacy_and_completed_success(tmp_path):
    first = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    first.stage(b"report")
    detail = _detail(first)
    first.commit()
    first.close()
    second = rs.Submission(tmp_path, "b" * 32, {}, "execution")
    second.close()
    assert not rs.effective_met(tmp_path, True, {}, identity={})
    assert not rs.effective_met(tmp_path, True, detail, identity={})


def test_deleted_receipt_never_enables_tagged_positive(tmp_path):
    assert rs.effective_met(tmp_path, True, {})
    assert not rs.effective_met(tmp_path, True, {rs.ID_KEY: "a" * 32})


def test_malformed_receipt_fails_closed(tmp_path):
    path = rs.receipt_path(tmp_path)
    path.parent.mkdir()
    path.write_text('{"version":true}')
    with pytest.raises(rs.ReportSubmissionError, match="version"):
        rs.effective_met(tmp_path, True, {})


def test_final_rename_error_after_applied_is_committed(tmp_path, monkeypatch):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    real = rs.Path.replace

    def applied(source, destination):
        real(source, destination)
        raise OSError("applied but failed")

    monkeypatch.setattr(rs.Path, "replace", applied)
    attempt.commit()
    attempt.close()
    assert attempt.committed
    assert rs.effective_met(tmp_path, True, _detail(attempt))


def test_precommit_sync_error_preserves_pending(tmp_path, monkeypatch):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")

    def failed(_):
        raise OSError("sync unavailable")

    monkeypatch.setattr(rs, "synchronize", failed)
    with pytest.raises(OSError, match="unavailable"):
        attempt.commit()
    attempt.close()
    assert not attempt.committed
    assert rs.read_receipt(tmp_path)["status"] == "pending"


def test_receipt_digest_bound_to_report_bytes(tmp_path):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    assert attempt.stage(b"report") == hashlib.sha256(b"report").hexdigest()
    attempt.close()


@pytest.mark.parametrize("damage", ["pending", "report_bytes"])
def test_lost_commit_invalidates_frozen_acceptance_selection(tmp_path, damage):
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import (
        freeze_acceptance,
        read_acceptance,
        record_changes,
    )
    from booley.ticket_board.persistence import atomic_replace_bytes

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.slug = "report"
    state.init_criteria({rs.KEY: True}, strict=True)
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    pending = rs.receipt_path(tmp_path).read_bytes()
    changes = state.set_criterion(rs.KEY, True, detail=_detail(attempt))
    record_changes(
        tmp_path,
        state,
        changes,
        invocation_id="report",
        producer="report",
        execution_id="execution",
        ticket_identity={},
    )
    state.save()
    attempt.commit()
    attempt.close()
    frozen = freeze_acceptance(
        tmp_path,
        state,
        execution_id="execution",
        ticket_identity={},
        participant_heads={"outer": "c" * 40},
    )
    assert frozen.criteria[rs.KEY]["met"]
    assert read_acceptance(tmp_path).kind == "accepted"
    if damage == "pending":
        atomic_replace_bytes(rs.receipt_path(tmp_path), pending)
    else:
        (tmp_path / "REPORT.md").write_bytes(b"edited after freeze")
    assert read_acceptance(tmp_path).kind == "unavailable"
    assert (tmp_path / "acceptance/snapshots" / f"{frozen.digest}.json").exists()


def test_commit_applied_with_unreadable_storage_returns_unknown_then_recovers(
    tmp_path, monkeypatch
):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    real = rs.Path.replace
    read = rs.read_receipt

    def applied(source, destination):
        real(source, destination)
        monkeypatch.setattr(
            rs,
            "read_receipt",
            lambda _root: (_ for _ in ()).throw(rs.ReportSubmissionError("storage unavailable")),
        )
        raise OSError("storage unavailable")

    monkeypatch.setattr(rs.Path, "replace", applied)
    with pytest.raises(rs.UnknownReportCommitError, match="unknown"):
        attempt.commit()
    attempt.close()
    monkeypatch.setattr(rs, "read_receipt", read)
    assert rs.effective_met(tmp_path, True, _detail(attempt))


def test_stale_owner_rejected_before_pending_and_commit(tmp_path):
    validations = 0

    def validate():
        nonlocal validations
        validations += 1
        if validations > 1:
            raise rs.ReportSubmissionError("execution lease expired")

    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution", validate=validate)
    attempt.stage(b"report")
    with pytest.raises(rs.ReportSubmissionError, match="lease expired"):
        attempt.commit()
    attempt.close()
    assert rs.read_receipt(tmp_path)["status"] == "pending"


def test_foreign_completed_receipt_cannot_authorize_tagged_observation(tmp_path):
    from tests.ticket_board.test_ticket_baseline import _blocked_ticket

    _root, _ticket, tio = _blocked_ticket(tmp_path)
    identity = tio.load_basis("blocked-again").ticket_identity()
    log = tio.logs_dir / "blocked-again"
    attempt = rs.Submission(log, "a" * 32, identity, "execution")
    attempt.stage(b"report")
    attempt.commit()
    attempt.close()
    newer = {**identity, "generation": "d" * 32}
    assert not rs.effective_met(log, True, _detail(attempt), identity=newer)


def test_completed_authority_survives_unlock_and_diagnostic_failure(tmp_path, monkeypatch):
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    attempt.commit()

    def fail(*_args, **_kwargs):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(rs, "release_file_lock", fail)
    monkeypatch.setattr(rs.logger, "warning", fail)
    attempt.close()
    assert attempt.committed
    assert rs.effective_met(tmp_path, True, _detail(attempt))


def test_pending_write_after_replace_error_still_fences_old_success(tmp_path, monkeypatch):
    first = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    first.stage(b"report")
    first.commit()
    first.close()
    real = rs.atomic_replace_bytes

    def replaced(path, content, **kwargs):
        real(path, content, **kwargs)
        raise OSError("directory sync failed after replace")

    monkeypatch.setattr(rs, "atomic_replace_bytes", replaced)
    with pytest.raises(OSError, match="after replace"):
        rs.Submission(tmp_path, "b" * 32, {}, "execution")
    assert rs.read_receipt(tmp_path)["status"] == "pending"
    assert not rs.effective_met(tmp_path, True, _detail(first))


def test_latest_unmet_observation_masks_completed_mutable_positive(tmp_path):
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board import acceptance_ledger as ledger

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.init_criteria({rs.KEY: True}, strict=True)
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    detail = _detail(attempt)
    positive = state.set_criterion(rs.KEY, True, detail=detail)
    ledger.record_changes(
        tmp_path,
        state,
        positive,
        invocation_id="one",
        producer="report",
        execution_id="execution",
        ticket_identity={},
    )
    state.save()
    attempt.commit()
    attempt.close()
    ledger.freeze_acceptance(
        tmp_path,
        state,
        execution_id="execution",
        ticket_identity={},
        participant_heads={"outer": "c" * 40},
    )
    negative = state.set_criterion(rs.KEY, False, detail={"reason": "later failure"})
    ledger.record_changes(
        tmp_path,
        state,
        negative,
        invocation_id="two",
        producer="report",
        execution_id="execution",
        ticket_identity={},
    )
    # The mutable save failed: the immutable current observation still wins.
    reloaded = DevelopmentState.load(state._file_path)
    assert reloaded.is_met(rs.KEY)
    ledger.project_report_state(reloaded, tmp_path, identity={})
    assert not reloaded.is_met(rs.KEY)
    assert ledger.read_acceptance(tmp_path).kind == "unavailable"


def test_concurrent_attempt_times_out_without_replacing_fence(tmp_path):
    first = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    try:
        with pytest.raises(rs.ReportSubmissionError, match="another report submission"):
            rs.Submission(tmp_path, "b" * 32, {}, "execution")
        assert rs.read_receipt(tmp_path)["submission_id"] == "a" * 32
    finally:
        first.close()


def test_snapshot_refresh_retires_lost_report_commit_pointers(tmp_path, monkeypatch):
    import json

    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import freeze_acceptance, record_changes
    from booley.ticket_board.runtime_identity import refresh_snapshot
    from tests.ticket_board.test_ticket_baseline import _blocked_ticket

    root, ticket, tio = _blocked_ticket(tmp_path)
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    log = tio.logs_dir / "blocked-again"
    identity = tio.load_basis("blocked-again").ticket_identity()
    (log / "ticket.md").write_bytes(ticket.read_bytes())
    state = DevelopmentState.load(log / ".runtime/booley_state.json")
    state.init_criteria({rs.KEY: True}, strict=True)
    attempt = rs.Submission(log, "a" * 32, identity, "execution")
    attempt.stage(b"report")
    pending = rs.receipt_path(log).read_bytes()
    changes = state.set_criterion(rs.KEY, True, detail=_detail(attempt))
    record_changes(
        log,
        state,
        changes,
        invocation_id="report",
        producer="report",
        execution_id="execution",
        ticket_identity=identity,
    )
    state.save()
    attempt.commit()
    attempt.close()
    frozen = freeze_acceptance(
        log,
        state,
        execution_id="execution",
        ticket_identity=identity,
        participant_heads={"outer": "c" * 40},
    )
    pointers = [
        log / "acceptance/accepted.json",
        log / "review/entry.json",
        log / ".runtime/triage-prep/manifest.json",
    ]
    for path in pointers[1:]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ticket_identity": identity}))
    rs.receipt_path(log).write_bytes(pending)
    with tio._ticket_lock("blocked-again", review_operation=True, stamp_pid=False):
        refresh_snapshot(tio, "blocked-again")
    assert all(not path.exists() for path in pointers)
    assert len(list((log / ".runtime/report-recovery").iterdir())) == 3
    assert (log / "acceptance/snapshots" / f"{frozen.digest}.json").exists()


def test_downstream_freeze_sync_failure_publishes_no_selection(tmp_path, monkeypatch):
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import freeze_acceptance, record_changes

    state = DevelopmentState.load(tmp_path / ".runtime/booley_state.json")
    state.init_criteria({rs.KEY: True})
    attempt = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    attempt.stage(b"report")
    changes = state.set_criterion(rs.KEY, True, detail=_detail(attempt))
    record_changes(
        tmp_path,
        state,
        changes,
        invocation_id="report",
        producer="report",
        execution_id="execution",
        ticket_identity={},
    )
    attempt.commit()
    attempt.close()

    def fail(_log):
        raise rs.ReportSubmissionError("durability synchronization failed")

    monkeypatch.setattr(rs, "synchronize", fail)
    with pytest.raises(rs.ReportSubmissionError, match="durability synchronization"):
        freeze_acceptance(
            tmp_path,
            state,
            execution_id="execution",
            ticket_identity={},
            participant_heads={"outer": "c" * 40},
        )
    assert not (tmp_path / "acceptance/accepted.json").exists()


@pytest.mark.parametrize("attempt_id", ["invalid", "a" * 32])
def test_invalid_or_reused_attempt_preserves_active_receipt(tmp_path, attempt_id):
    original = rs.Submission(tmp_path, "a" * 32, {}, "execution")
    original.stage(b"report")
    original.commit()
    original.close()
    before = rs.receipt_path(tmp_path).read_bytes()
    with pytest.raises(rs.ReportSubmissionError):
        rs.Submission(tmp_path, attempt_id, {}, "execution")
    assert rs.receipt_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("pair", ["matching", "other-state", "missing-state", "missing-logs"])
def test_state_log_root_requires_exact_environment_pair(tmp_path, monkeypatch, pair):
    from booley.criteria.state import DevelopmentState

    state_path = tmp_path / "custom-state.json"
    state = DevelopmentState.load(state_path)
    logs = tmp_path / "independent-evidence"
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    if pair != "missing-state":
        monkeypatch.setenv(
            "BOOLEY_STATE_FILE",
            str(state_path if pair != "other-state" else tmp_path / "other.json"),
        )
    if pair != "missing-logs":
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    assert rs.state_log_dir(state) == (logs if pair == "matching" else tmp_path)

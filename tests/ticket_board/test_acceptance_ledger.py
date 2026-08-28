"""Durable Ticket acceptance evidence and snapshots."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.ticket_board.acceptance_ledger import (
    AcceptanceLedgerError,
    bind_review_package,
    freeze_acceptance,
    read_acceptance,
    record_changes,
    validate_review_package_binding,
)


def _accepted_state() -> DevelopmentState:
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True}, strict=True)
    state.set_criterion("sim_pass_uart", True)
    return state


def test_accepted_snapshot_survives_live_state_removal(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state_path = log_dir / ".runtime" / "booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True}, strict=True)
    state.set_criterion(
        "sim_pass_uart",
        True,
        detail={"target": "sim_uart", "passed_tests": ["test_tx"]},
    )
    state.save()

    frozen = freeze_acceptance(
        log_dir,
        state,
        execution_id="resume-generation",
        target_contract={"schema": 2, "surface_digest": "a" * 64},
        accepted_at="2026-08-28T12:00:00Z",
    )
    state_path.unlink()

    result = read_acceptance(log_dir)

    assert result.kind == "accepted"
    assert result.snapshot is not None
    assert result.snapshot.digest == frozen.digest
    assert result.snapshot.criteria["sim_pass_uart"]["met"] is True
    assert result.snapshot.execution_id == "resume-generation"


def test_normalized_observations_receive_deterministic_completion_sequences(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.init_criteria(
        {"sim_pass_uart": True},
        criterion_params={"sim_pass_uart": {"from_state": "fail", "target": "sim_uart"}},
    )
    red = state.set_criterion(
        "sim_pass_uart",
        False,
        detail={"test_selector": "test_tx", "failed_tests": ["test_tx"]},
    )
    green = state.set_criterion(
        "sim_pass_uart",
        True,
        detail={"test_selector": "test_tx", "passed_tests": ["test_tx"]},
    )

    red_refs = record_changes(
        log_dir,
        state,
        red,
        invocation_id="sim-red",
        producer="sim",
        execution_id="generation-1",
    )
    green_refs = record_changes(
        log_dir,
        state,
        green,
        invocation_id="sim-green",
        producer="sim",
        execution_id="generation-2",
    )

    assert [red_refs[0].sequence, green_refs[0].sequence] == [1, 2]
    assert red_refs[0].role == "baseline"
    assert green_refs[0].role == "candidate"

    frozen = freeze_acceptance(
        log_dir,
        state,
        execution_id="generation-2",
        target_contract=None,
    )

    assert [reference["sequence"] for reference in frozen.evidence] == [1, 2]
    assert [reference["role"] for reference in frozen.evidence] == [
        "baseline",
        "candidate",
    ]


def test_freeze_rejects_mutable_state_that_disagrees_with_latest_evidence(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.init_criteria({"sim_pass_uart": True})
    changes = state.set_criterion("sim_pass_uart", True)
    record_changes(
        log_dir,
        state,
        changes,
        invocation_id="sim-green",
        producer="sim",
        execution_id="generation-1",
    )
    state.criteria["sim_pass_uart"].met = False

    with pytest.raises(AcceptanceLedgerError, match="disagrees"):
        freeze_acceptance(
            log_dir,
            state,
            execution_id="generation-1",
            target_contract=None,
        )


def test_concurrent_observations_receive_unique_completion_sequences(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"

    def record(index: int) -> int:
        state = DevelopmentState()
        state.slug = "fix-uart"
        state.init_criteria({f"criterion_{index}": True})
        changes = state.set_criterion(f"criterion_{index}", True)
        return record_changes(
            log_dir,
            state,
            changes,
            invocation_id=f"run-{index}",
            producer="test",
            execution_id="generation-1",
        )[0].sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(record, range(24)))

    assert sorted(sequences) == list(range(1, 25))


def test_freeze_rejects_conflicting_content_at_an_existing_snapshot(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state = _accepted_state()
    frozen = freeze_acceptance(
        log_dir,
        state,
        execution_id="generation-1",
        target_contract=None,
        accepted_at="2026-08-28T12:00:00Z",
    )
    snapshot_path = log_dir / "acceptance" / "snapshots" / f"{frozen.digest}.json"
    snapshot_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(AcceptanceLedgerError, match="conflicting acceptance record"):
        freeze_acceptance(
            log_dir,
            state,
            execution_id="generation-1",
            target_contract=None,
            accepted_at="2026-08-28T12:00:00Z",
        )


def test_freeze_rejects_evidence_whose_sequence_disagrees_with_its_directory(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    evidence_dir = log_dir / "acceptance" / "evidence" / "000000001"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "record.json").write_text(
        json.dumps({"sequence": 2, "criterion": "sim_pass_uart", "role": "candidate"}),
        encoding="utf-8",
    )

    with pytest.raises(AcceptanceLedgerError, match="sequence does not match"):
        freeze_acceptance(
            log_dir,
            _accepted_state(),
            execution_id="generation-1",
            target_contract=None,
        )


def test_read_acceptance_reports_invalid_reference_and_snapshot_shapes(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    acceptance_dir = log_dir / "acceptance"
    acceptance_dir.mkdir(parents=True)
    (acceptance_dir / "accepted.json").write_text(
        '{"snapshot_digest":"short"}\n', encoding="utf-8"
    )

    invalid_reference = read_acceptance(log_dir)
    assert invalid_reference.kind == "corrupt"
    assert "invalid digest" in invalid_reference.reason

    payload = b"{}"
    digest = hashlib.sha256(payload).hexdigest()
    (acceptance_dir / "accepted.json").write_text(
        json.dumps({"snapshot_digest": digest}), encoding="utf-8"
    )
    snapshots = acceptance_dir / "snapshots"
    snapshots.mkdir()
    (snapshots / f"{digest}.json").write_bytes(payload)

    invalid_snapshot = read_acceptance(log_dir)
    assert invalid_snapshot.kind == "corrupt"
    assert "invalid acceptance snapshot" in invalid_snapshot.reason


def test_review_package_binding_handles_missing_and_unready_manifests(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    snapshot = freeze_acceptance(
        log_dir,
        _accepted_state(),
        execution_id="generation-1",
        target_contract=None,
    )

    assert bind_review_package(log_dir, snapshot) is False
    validate_review_package_binding(log_dir, snapshot)

    manifest = log_dir / ".runtime" / "triage-prep" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"status":"pending"}\n', encoding="utf-8")

    with pytest.raises(AcceptanceLedgerError, match="manifest is not ready"):
        bind_review_package(log_dir, snapshot)

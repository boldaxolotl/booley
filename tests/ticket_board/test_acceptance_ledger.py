"""Durable Ticket Criterion evidence and Criteria Satisfaction Records."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.ticket_board import acceptance_ledger
from booley.ticket_board.acceptance_ledger import (
    AcceptanceLedgerError,
    EvidenceRef,
    bind_review_package,
    freeze_acceptance,
    read_acceptance,
    record_changes,
    record_or_verify_transaction,
    validate_review_package_binding,
)


def _accepted_state() -> DevelopmentState:
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True}, strict=True)
    state.set_criterion("sim_pass_uart", True)
    return state


def _campaign_facts(*, finished_at: str = "2026-09-21T10:00:00Z") -> dict:
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": "12345678-1234-4234-9234-123456789abc",
        "manifest_sha256": f"sha256:{'a' * 64}",
        "origin": {"execution_id": "b" * 32, "invocation_id": 7},
        "target": {"identity": "acme:lib:uart:1#sim_uart", "selector": "sim_uart"},
        "required_suite": {
            "names": ["test_tx"],
            "default_invocation": True,
            "source_sha256": f"sha256:{'c' * 64}",
        },
        "prerequisites": [],
        "consumed_results": [{"finished_at": finished_at}],
        "observations": [],
        "coverage_reference": None,
    }


def _transaction_state(tmp_path: Path) -> tuple[Path, DevelopmentState, list]:
    log_dir = tmp_path / "logs" / "fix-uart"
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug = "fix-uart"
    state.init_criteria({"sim_pass_uart": True}, strict=True)
    changes = state.set_criterion(
        "sim_pass_uart", True, detail={"campaign_id": _campaign_facts()["campaign_id"]}
    )
    return log_dir, state, changes


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
        ticket_identity={"schema": 1, "participants": []},
        participant_heads={"outer": "a" * 40},
        accepted_at="2026-08-28T12:00:00Z",
    )
    state_path.unlink()

    result = read_acceptance(log_dir)

    assert result.kind == "accepted"
    assert result.snapshot is not None
    assert result.snapshot.digest == frozen.digest
    assert result.snapshot.criteria["sim_pass_uart"]["met"] is True
    assert result.snapshot.execution_id == "resume-generation"
    assert result.snapshot.participant_heads == {"outer": "a" * 40}


def _record_red_green_observations(
    log_dir: Path,
) -> tuple[DevelopmentState, tuple[EvidenceRef, ...], tuple[EvidenceRef, ...]]:
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
    return state, red_refs, green_refs


def test_normalized_observations_receive_deterministic_completion_sequences(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state, red_refs, green_refs = _record_red_green_observations(log_dir)

    assert [red_refs[0].sequence, green_refs[0].sequence] == [1, 2]
    assert red_refs[0].role == "baseline"
    assert green_refs[0].role == "candidate"

    frozen = freeze_acceptance(
        log_dir,
        state,
        execution_id="generation-2",
        ticket_identity=None,
        participant_heads={"outer": "a" * 40},
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
            ticket_identity=None,
            participant_heads={"outer": "a" * 40},
        )


@pytest.mark.parametrize("drift", ["matching", "missing", "generation", "authored", "commit"])
def test_freeze_requires_each_observation_to_match_current_ticket_identity(
    tmp_path: Path, drift: str
) -> None:
    log_dir = tmp_path / "logs" / "fix-uart"
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.init_criteria({"sim_pass_uart": True})
    changes = state.set_criterion("sim_pass_uart", True)
    identity = {
        "generation": "e" * 32,
        "authored_sha256": "a" * 64,
        "baseline": {"outer": {"commit": "b" * 40}},
    }
    recorded = deepcopy(identity)
    if drift == "generation":
        recorded["generation"] = "f" * 32
    elif drift == "authored":
        recorded["authored_sha256"] = "c" * 64
    elif drift == "commit":
        recorded["baseline"]["outer"]["commit"] = "d" * 40
    elif drift == "missing":
        recorded = None
    record_changes(
        log_dir,
        state,
        changes,
        invocation_id="sim-green",
        producer="sim",
        execution_id="run-1",
        ticket_identity=recorded,
    )

    if drift == "matching":
        snapshot = freeze_acceptance(
            log_dir,
            state,
            execution_id="run-1",
            ticket_identity=identity,
            participant_heads={"outer": "b" * 40},
        )
        assert len(snapshot.evidence) == 1
    else:
        with pytest.raises(AcceptanceLedgerError, match="another Ticket identity"):
            freeze_acceptance(
                log_dir,
                state,
                execution_id="run-1",
                ticket_identity=identity,
                participant_heads={"outer": "b" * 40},
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


def test_campaign_transaction_commits_before_selecting_mutable_state(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}

    transaction = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )

    assert state.acceptance_transactions == [transaction.transaction_id]
    assert [reference.sequence for reference in transaction.evidence] == [1]
    commit = json.loads(
        (
            log_dir / "acceptance" / "transactions" / f"{transaction.transaction_id}.json"
        ).read_text()
    )
    assert commit["$schema"] == "booley.acceptance-transaction/v1"
    assert commit["records"][0]["sequence"] == 1
    record_path = (
        log_dir
        / "acceptance"
        / "evidence"
        / f"000000001.tx.{transaction.transaction_id}"
        / "record.json"
    )
    assert json.loads(record_path.read_text())["$schema"] == "booley.acceptance-record/v2"
    persisted = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    assert persisted.acceptance_transactions == [transaction.transaction_id]
    assert persisted.criteria["sim_pass_uart"].met is True


def test_campaign_transaction_retry_uses_frozen_intent_after_mutable_drift(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    first = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )
    state.slug = ""
    drifted = [replace(changes[0], met=False, detail={"drifted": True})] * 4_001

    retried = record_or_verify_transaction(
        log_dir,
        state,
        drifted,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )

    assert retried == first
    assert state.criteria["sim_pass_uart"].met is True
    assert len(list((log_dir / "acceptance" / "transactions").glob("*.json"))) == 1


def test_campaign_transaction_recovers_uncommitted_canonical_prefix(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    second = state.set_criterion("sim_pass_uart", False, detail={"second": True})
    changes.extend(second)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    lookup, intent, transaction_id = acceptance_ledger._build_transaction_documents(
        state, changes, _campaign_facts(), identity
    )
    acceptance_ledger._load_or_publish_intent(log_dir, lookup, intent)
    evidence_root = log_dir / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True)
    acceptance_ledger._publish_v2_record(evidence_root, intent["envelope"], transaction_id, 0)

    recovered = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )

    assert recovered.transaction_id == transaction_id
    assert [reference.sequence for reference in recovered.evidence] == [1, 2]
    assert state.acceptance_transactions == [transaction_id]


def test_campaign_transaction_removes_truncated_current_temp_and_republishes(
    tmp_path: Path,
) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    _lookup, _intent, transaction_id = acceptance_ledger._build_transaction_documents(
        state, changes, _campaign_facts(), identity
    )
    temp = (
        log_dir
        / "acceptance"
        / "evidence"
        / (f".tmp.acceptance.tx-{transaction_id}.ord-00000000.seq-000000001.nonce-{'f' * 32}")
    )
    temp.mkdir(parents=True)
    (temp / "record.json").write_text('{"truncated":', encoding="utf-8")

    recovered = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )

    assert recovered.transaction_id == transaction_id
    assert not temp.exists()
    assert recovered.evidence[0].sequence == 1


def test_selecting_committed_transaction_replays_later_plain_evidence(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    lookup, intent, transaction_id = acceptance_ledger._build_transaction_documents(
        state, changes, _campaign_facts(), identity
    )
    acceptance_ledger._load_or_publish_intent(log_dir, lookup, intent)
    evidence_root = log_dir / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True)
    prefix = [
        acceptance_ledger._publish_v2_record(evidence_root, intent["envelope"], transaction_id, 0)
    ]
    commit = acceptance_ledger._commit_document(intent["envelope"], transaction_id, prefix)
    acceptance_ledger._write_once(
        log_dir / "acceptance" / "transactions" / f"{transaction_id}.json",
        acceptance_ledger._canonical(commit) + b"\n",
    )
    later = state.set_criterion("sim_pass_uart", False, detail={"later": True})
    record_changes(
        log_dir,
        state,
        later,
        invocation_id="later-sim",
        producer="sim",
        execution_id="e" * 32,
        ticket_identity=identity,
    )

    selected = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
    )

    assert selected.transaction_id == transaction_id
    assert state.acceptance_transactions == [transaction_id]
    assert state.criteria["sim_pass_uart"].met is False
    assert state.criteria["sim_pass_uart"].detail == {"later": True}


def test_allocate_sequence_reports_exhaustion(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "000000001").mkdir()
    monkeypatch.setattr(acceptance_ledger, "range", lambda *_args: (1,), raising=False)

    with pytest.raises(AcceptanceLedgerError, match="Criterion evidence sequence exhausted"):
        acceptance_ledger._allocate_sequence(root, "")


def test_freeze_rejects_conflicting_content_at_an_existing_snapshot(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    state = _accepted_state()
    frozen = freeze_acceptance(
        log_dir,
        state,
        execution_id="generation-1",
        ticket_identity=None,
        participant_heads={"outer": "a" * 40},
        accepted_at="2026-08-28T12:00:00Z",
    )
    snapshot_path = log_dir / "acceptance" / "snapshots" / f"{frozen.digest}.json"
    snapshot_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(AcceptanceLedgerError, match="conflicting acceptance record"):
        freeze_acceptance(
            log_dir,
            state,
            execution_id="generation-1",
            ticket_identity=None,
            participant_heads={"outer": "a" * 40},
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
            ticket_identity=None,
            participant_heads={"outer": "a" * 40},
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
    assert "invalid Criteria Satisfaction Record" in invalid_snapshot.reason


def test_review_package_binding_handles_missing_and_unready_manifests(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    snapshot = freeze_acceptance(
        log_dir,
        _accepted_state(),
        execution_id="generation-1",
        ticket_identity=None,
        participant_heads={"outer": "a" * 40},
    )

    assert bind_review_package(log_dir, snapshot) is False
    validate_review_package_binding(log_dir, snapshot)

    manifest = log_dir / ".runtime" / "triage-prep" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"status":"pending"}\n', encoding="utf-8")

    with pytest.raises(AcceptanceLedgerError, match="manifest is not ready"):
        bind_review_package(log_dir, snapshot)


def test_review_package_binding_requires_exact_basis_and_participant_heads(tmp_path):
    log_dir = tmp_path / "logs" / "fix-uart"
    generation = "f" * 32
    snapshot = freeze_acceptance(
        log_dir,
        _accepted_state(),
        execution_id="generation-1",
        ticket_identity={"generation": generation},
        participant_heads={"outer": "a" * 40},
    )
    prep_dir = log_dir / ".runtime" / "triage-prep"
    prep_dir.mkdir(parents=True)
    briefing = prep_dir / "briefing.json"
    briefing.write_text("{}\n", encoding="utf-8")
    manifest_path = prep_dir / "manifest.json"
    manifest = {
        "status": "ready",
        "briefing_path": str(briefing),
        "ticket_generation": generation,
        "head_sha": "b" * 40,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AcceptanceLedgerError, match="heads disagree"):
        bind_review_package(log_dir, snapshot)

    manifest["head_sha"] = "a" * 40
    manifest["ticket_generation"] = "e" * 32
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AcceptanceLedgerError, match="different Ticket generation"):
        bind_review_package(log_dir, snapshot)

    manifest["ticket_generation"] = generation
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert bind_review_package(log_dir, snapshot) is True
    validate_review_package_binding(log_dir, snapshot)

    different_snapshot = replace(snapshot, execution_id="generation-2")
    with pytest.raises(
        AcceptanceLedgerError, match="cannot rebind a different Criteria Satisfaction Record"
    ):
        bind_review_package(log_dir, different_snapshot, replace_existing=True)

"""Generic Criterion evidence recording and transaction recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from booley.criteria import evidence_ledger
from booley.criteria.state import DevelopmentState
from tests.criteria.ledger_support import (
    FIXTURE_SCOPE,
    AcceptanceLedgerError,
    EvidenceRef,
    record_changes,
    record_or_verify_transaction,
)


def _accepted_state() -> DevelopmentState:
    state = DevelopmentState()
    state.slug = "fix-uart"
    state.ticket_type = "bugfix"
    state.init_criteria({"sim_pass_uart": True}, strict=True)
    state.set_criterion("sim_pass_uart", True)
    return state


def _campaign_facts(
    *,
    finished_at: str = "2026-09-21T10:00:00Z",
    campaign_id: str = "12345678-1234-4234-9234-123456789abc",
) -> dict:
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": campaign_id,
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

    rows = evidence_ledger.read_evidence_records(FIXTURE_SCOPE, log_dir, state, {})
    assert [row["sequence"] for row in rows] == [1, 2]


def test_campaign_transaction_recovers_uncommitted_canonical_prefix(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    second = state.set_criterion("sim_pass_uart", False, detail={"second": True})
    changes.extend(second)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    lookup, intent, transaction_id = evidence_ledger._build_transaction_documents(
        FIXTURE_SCOPE, state, changes, _campaign_facts(), identity
    )
    evidence_ledger._load_or_publish_intent(FIXTURE_SCOPE, log_dir, lookup, intent)
    evidence_root = log_dir / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True)
    evidence_ledger._publish_v2_record(
        FIXTURE_SCOPE, evidence_root, intent["envelope"], transaction_id, 0
    )

    recovered = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        identity=identity,
    )

    assert recovered.transaction_id == transaction_id
    assert [reference.sequence for reference in recovered.evidence] == [1, 2]
    assert state.acceptance_transactions == [transaction_id]


def test_campaign_transaction_removes_truncated_current_temp_and_republishes(
    tmp_path: Path,
) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    _lookup, _intent, transaction_id = evidence_ledger._build_transaction_documents(
        FIXTURE_SCOPE, state, changes, _campaign_facts(), identity
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
        identity=identity,
    )

    assert recovered.transaction_id == transaction_id
    assert not temp.exists()
    assert recovered.evidence[0].sequence == 1


def test_selecting_committed_transaction_replays_later_plain_evidence(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    lookup, intent, transaction_id = evidence_ledger._build_transaction_documents(
        FIXTURE_SCOPE, state, changes, _campaign_facts(), identity
    )
    evidence_ledger._load_or_publish_intent(FIXTURE_SCOPE, log_dir, lookup, intent)
    evidence_root = log_dir / "acceptance" / "evidence"
    evidence_root.mkdir(parents=True)
    prefix = [
        evidence_ledger._publish_v2_record(
            FIXTURE_SCOPE, evidence_root, intent["envelope"], transaction_id, 0
        )
    ]
    commit = evidence_ledger._commit_document(intent["envelope"], transaction_id, prefix)
    evidence_ledger.write_once(
        log_dir / "acceptance" / "transactions" / f"{transaction_id}.json",
        evidence_ledger.canonical_json(commit) + b"\n",
    )
    later = state.set_criterion("sim_pass_uart", False, detail={"later": True})
    record_changes(
        log_dir,
        state,
        later,
        invocation_id="later-sim",
        producer="sim",
        execution_id="e" * 32,
        identity=identity,
    )

    selected = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        identity=identity,
    )

    assert selected.transaction_id == transaction_id
    assert state.acceptance_transactions == [transaction_id]
    assert state.criteria["sim_pass_uart"].met is False
    assert state.criteria["sim_pass_uart"].detail == {"later": True}


def test_allocate_sequence_reports_exhaustion(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    (root / "000000001").mkdir()
    monkeypatch.setattr(evidence_ledger, "range", lambda *_args: (1,), raising=False)

    with pytest.raises(AcceptanceLedgerError, match="Criterion evidence sequence exhausted"):
        evidence_ledger._allocate_sequence(root, "")


@pytest.mark.parametrize("encoding", ["plain", "v1", "v2"])
def test_current_generation_filters_valid_historical_records(
    tmp_path: Path, encoding: str
) -> None:
    old = {
        "generation": "a" * 32,
        "authored_sha256": "b" * 64,
        "baseline": {"outer": {"commit": "c" * 40}},
    }
    current = {**old, "generation": "d" * 32}
    log, state, _ = _transaction_state(tmp_path)
    first = state.set_criterion("sim_pass_uart", False)
    second = state.set_criterion("sim_pass_uart", True)
    if encoding == "v2":
        record_or_verify_transaction(
            log, state, first, acceptance_facts=_campaign_facts(), identity=old
        )
        record_or_verify_transaction(
            log, state, second, acceptance_facts=_campaign_facts(), identity=current
        )
    else:
        transaction = "e" * 64 if encoding == "v1" else ""
        for changes, identity in ((first, old), (second, current)):
            record_changes(
                log,
                state,
                changes,
                invocation_id="campaign",
                producer="sim",
                execution_id="execution",
                identity=identity,
                transaction_id=transaction,
            )
        if transaction:
            state.acceptance_transactions = [transaction]
    rows = evidence_ledger.read_evidence_records(FIXTURE_SCOPE, log, state, current)
    assert [row["sequence"] for row in rows] == [2]
    assert len(list((log / "acceptance/evidence").glob("*/record.json"))) == 2


def test_foreign_v2_manifest_corruption_is_still_fatal(tmp_path: Path) -> None:
    identity = {"generation": "a" * 32}
    log, state, changes = _transaction_state(tmp_path)
    record_or_verify_transaction(
        log, state, changes, acceptance_facts=_campaign_facts(), identity=identity
    )
    path = next((log / "acceptance/evidence").glob("*/record.json"))
    row = json.loads(path.read_text())
    row["met"] = False
    path.write_bytes(evidence_ledger.canonical_json(row) + b"\n")
    with pytest.raises(AcceptanceLedgerError, match="does not bind"):
        evidence_ledger.read_evidence_records(FIXTURE_SCOPE, log, state, {"generation": "b" * 32})


@pytest.fixture(autouse=True)
def isolated_owner(tmp_path, monkeypatch):
    import os

    from booley.runtime.project_dir import reset_cache

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
    reset_cache()


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
        identity=identity,
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


def test_campaign_replay_preserves_fail_to_pass_transition_once(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs" / "fix-uart"
    state = DevelopmentState.load(log_dir / ".runtime" / "booley_state.json")
    state.slug = "fix-uart"
    params = {"from_state": "fail", "target": "sim_uart"}
    state.init_criteria(
        {"sim_pass_uart": True}, criterion_params={"sim_pass_uart": params}, strict=True
    )
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}

    _record_campaign_transition(log_dir, state, identity, met=False)
    green_facts = _campaign_facts(
        finished_at="2026-09-21T10:01:00Z",
        campaign_id="22345678-1234-4234-9234-123456789abc",
    )
    green_shadow = deepcopy(state)
    green = green_shadow.set_criterion("sim_pass_uart", True, detail={"passed_tests": ["tx"]})
    transaction = record_or_verify_transaction(
        log_dir,
        state,
        green,
        acceptance_facts=green_facts,
        identity=identity,
    )
    state_bytes = state._file_path.read_bytes()  # type: ignore[union-attr]

    retried = record_or_verify_transaction(
        log_dir,
        state,
        green,
        acceptance_facts=green_facts,
        identity=identity,
    )

    assert retried == transaction
    assert state._file_path.read_bytes() == state_bytes  # type: ignore[union-attr]
    assert state.criteria["sim_pass_uart"].transition_evidence == _transition_evidence()


def _record_campaign_transition(log_dir, state, identity, *, met: bool) -> None:
    detail = {"passed_tests" if met else "failed_tests": ["tx"]}
    shadow = deepcopy(state)
    changes = shadow.set_criterion("sim_pass_uart", met, detail=detail)
    record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(finished_at="2026-09-21T10:00:00Z"),
        identity=identity,
    )


def _transition_evidence() -> list[dict[str, object]]:
    return [
        {
            "met": False,
            "recorded_at": "2026-09-21T10:00:00Z",
            "detail": {"failed_tests": ["tx"], "checks": []},
        },
        {
            "met": True,
            "recorded_at": "2026-09-21T10:01:00Z",
            "detail": {"passed_tests": ["tx"], "checks": []},
        },
    ]


def test_campaign_transaction_retry_uses_frozen_intent_after_mutable_drift(tmp_path: Path) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    first = record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        identity=identity,
    )
    state.slug = ""
    drifted = [replace(changes[0], met=False, detail={"drifted": True})] * 4_001

    retried = record_or_verify_transaction(
        log_dir,
        state,
        drifted,
        acceptance_facts=_campaign_facts(),
        identity=identity,
    )

    assert retried == first
    assert state.criteria["sim_pass_uart"].met is True
    assert len(list((log_dir / "acceptance" / "transactions").glob("*.json"))) == 1


from copy import deepcopy
from dataclasses import replace

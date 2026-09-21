"""Validate retained Simulation Campaign acceptance and corruption recovery evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _load(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, raw


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _transaction_id(transaction: dict[str, object]) -> str:
    envelope = transaction.get("envelope")
    _need(isinstance(envelope, dict), "acceptance transaction envelope is missing")
    raw = _canonical(envelope)
    transaction_id = hashlib.sha256(raw).hexdigest()
    _need(transaction.get("transaction_id") == transaction_id, "transaction ID differs")
    _need(transaction.get("envelope_sha256") == _digest(raw), "envelope digest differs")
    return transaction_id


def _acceptance_transactions(path: Path) -> list[object]:
    state, _ = _load(path)
    transactions = state.get("acceptance_transactions")
    _need(isinstance(transactions, list), f"{path}: acceptance transactions are missing")
    return transactions


def _validate_results(
    result_paths: list[Path], campaign_id: object, manifest_sha256: str
) -> None:
    _need(bool(result_paths), "terminal Simulation Results are required")
    for path in result_paths:
        result, _ = _load(path)
        _need(result.get("campaign_id") == campaign_id, "result campaign identity differs")
        _need(result.get("manifest_sha256") == manifest_sha256, "result manifest digest differs")
        _need(
            result.get("state") in {"completed", "timeout", "crash", "setup_error", "blocked_by_build"},
            "result is not terminal",
        )


def _validate_transaction(
    transaction_paths: list[Path], campaign_id: object, manifest_sha256: str
) -> str:
    _need(len(transaction_paths) == 1, "expected one matching acceptance transaction")
    transaction, _ = _load(transaction_paths[0])
    _need(
        transaction.get("$schema") == "booley.acceptance-transaction/v1",
        "acceptance transaction schema differs",
    )
    transaction_id = _transaction_id(transaction)
    envelope = transaction["envelope"]
    assert isinstance(envelope, dict)
    _need(
        envelope.get("$schema") == "booley.acceptance-transaction-envelope/v1",
        "acceptance transaction envelope schema differs",
    )
    _need(envelope.get("campaign_id") == campaign_id, "transaction campaign identity differs")
    _need(envelope.get("manifest_sha256") == manifest_sha256, "transaction manifest digest differs")
    records = transaction.get("records")
    _need(isinstance(records, list) and bool(records), "acceptance transaction records are missing")
    _need(transaction.get("record_count") == len(records), "acceptance record count differs")
    criteria = [item.get("criterion") for item in records if isinstance(item, dict)]
    _need(len(criteria) == len(records), "acceptance transaction record is invalid")
    _need(len(set(criteria)) == len(criteria), "acceptance transaction records conflict")
    return transaction_id


def validate_acceptance_recovery(
    manifest_path: Path,
    result_paths: list[Path],
    transaction_paths: list[Path],
    before_state_path: Path,
    failed_state_path: Path,
    recovered_state_path: Path,
    simulation_path: Path,
) -> None:
    """Require idempotent transaction selection after a state-save failure."""
    manifest, _ = _load(manifest_path)
    manifest_sha256 = _digest(_canonical(manifest))
    campaign_id = manifest.get("campaign_id")
    _need(isinstance(campaign_id, str) and bool(campaign_id), "campaign identity is missing")
    _validate_results(result_paths, campaign_id, manifest_sha256)
    transaction_id = _validate_transaction(transaction_paths, campaign_id, manifest_sha256)
    before = _acceptance_transactions(before_state_path)
    failed = _acceptance_transactions(failed_state_path)
    recovered = _acceptance_transactions(recovered_state_path)
    _need(failed == before, "failed state save changed the selected transactions")
    _need(recovered == [*before, transaction_id], "transaction was not selected exactly once")
    simulation, _ = _load(simulation_path)
    _need(simulation.get("complete") is True, "simulation projection is not complete")
    _need(simulation.get("campaign_manifest") == str(manifest_path), "projection manifest differs")


def validate_corrupt_terminal(
    archived_result_path: Path,
    restored_result_path: Path,
    rejection_path: Path,
) -> None:
    """Require fail-closed corrupt-result rejection and byte-exact restoration."""
    _archived_document, archived = _load(archived_result_path)
    _restored_document, restored = _load(restored_result_path)
    _need(archived == restored, "restored result bytes differ from archived baseline")
    rejection, _ = _load(rejection_path)
    _need(rejection.get("exit_code") == 2, "corrupt resume did not exit 2")
    diagnostic = rejection.get("diagnostic")
    _need(
        isinstance(diagnostic, str)
        and "integrity" in diagnostic.lower()
        and "result" in diagnostic.lower(),
        "corrupt resume lacks a precise integrity diagnostic",
    )
    _need(
        rejection.get("attempts_before") == rejection.get("attempts_after"),
        "corrupt resume created an attempt",
    )
    _need(rejection.get("control_exit_code") == 0, "restored control did not succeed")
    _need(rejection.get("control_complete") is True, "restored control is incomplete")
    _need(
        rejection.get("attempts_before") == rejection.get("attempts_after_restore"),
        "restored completed work item reran",
    )
    _need(rejection.get("eda_processes_started") == [], "corrupt resume launched EDA")
    valid_sha256 = _digest(archived)
    _need(rejection.get("valid_sha256") == valid_sha256, "archived result digest differs")
    _need(rejection.get("restored_sha256") == valid_sha256, "restored result digest differs")
    _need(rejection.get("corrupt_sha256") != valid_sha256, "corrupt result digest was unchanged")

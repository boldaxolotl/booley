"""Validate retained Simulation Campaign acceptance and corruption recovery evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load(path: Path) -> tuple[dict[str, object], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, raw


def _load_canonical(path: Path, label: str) -> dict[str, object]:
    value, raw = _load(path)
    _need(raw == _canonical(value) + b"\n", f"{label} is not canonical JSON")
    return value


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    _need(isinstance(value, dict), f"{label} is not an object")
    _need(set(value) == fields, f"{label} fields differ")
    return value


def _lookup_from_envelope(envelope: dict[str, object]) -> dict[str, object]:
    ticket = _exact(envelope["ticket"], {"slug", "identity", "generation"}, "ticket")
    return {
        "campaign_id": envelope["campaign_id"],
        "manifest_sha256": envelope["manifest_sha256"],
        "ticket_identity": ticket["identity"],
        "ticket_generation": ticket["generation"],
        "producer": envelope["producer"],
        "purpose": envelope["purpose"],
    }


def _acceptance_facts(
    value: object, campaign_id: object, manifest_sha256: str
) -> dict[str, object]:
    facts = _exact(
        value,
        {
            "$schema", "campaign_id", "manifest_sha256", "origin", "target",
            "required_suite", "prerequisites", "consumed_results", "observations",
            "coverage_reference",
        },
        "acceptance facts",
    )
    _need(facts["$schema"] == "booley.simulation-acceptance-facts/v1", "facts schema differs")
    _need(facts["campaign_id"] == campaign_id, "facts campaign identity differs")
    _need(facts["manifest_sha256"] == manifest_sha256, "facts manifest digest differs")
    return facts


def _envelope(
    value: object, campaign_id: object, manifest_sha256: str
) -> dict[str, object]:
    envelope = _exact(
        value,
        {
            "$schema", "campaign_id", "manifest_sha256", "origin", "producer", "purpose",
            "ticket", "recorded_at", "role_derivation", "changes",
        },
        "transaction envelope",
    )
    _need(envelope["$schema"] == "booley.acceptance-transaction-envelope/v1", "envelope schema differs")
    _need(envelope["campaign_id"] == campaign_id, "envelope campaign identity differs")
    _need(envelope["manifest_sha256"] == manifest_sha256, "envelope manifest digest differs")
    _need(envelope["producer"] == "simulation_campaign", "envelope producer differs")
    _need(envelope["purpose"] == "ticket_acceptance", "envelope purpose differs")
    _need(envelope["role_derivation"] == "booley.acceptance-role/v1", "role derivation differs")
    _need(isinstance(envelope["changes"], list) and bool(envelope["changes"]), "changes missing")
    return envelope


def _reject_matching_intents(path: Path, lookup: dict[str, object]) -> None:
    matches = 0
    for sibling in path.parent.glob("*.json"):
        candidate = _load_canonical(sibling, "acceptance intent")
        if candidate.get("lookup_key") == lookup:
            matches += 1
    _need(matches == 1, "multiple acceptance intents match the campaign")


def _validate_intent(
    path: Path, campaign_id: object, manifest_sha256: str
) -> tuple[dict[str, object], dict[str, object]]:
    intent = _load_canonical(path, "acceptance intent")
    _exact(
        intent,
        {"$schema", "lookup_key", "acceptance_facts", "acceptance_facts_sha256", "envelope"},
        "acceptance intent",
    )
    _need(intent["$schema"] == "booley.simulation-acceptance-intent/v1", "intent schema differs")
    facts = _acceptance_facts(intent["acceptance_facts"], campaign_id, manifest_sha256)
    _need(intent["acceptance_facts_sha256"] == _digest(_canonical(facts)), "facts digest differs")
    envelope = _envelope(intent["envelope"], campaign_id, manifest_sha256)
    lookup = _exact(
        intent["lookup_key"],
        {
            "campaign_id", "manifest_sha256", "ticket_identity", "ticket_generation",
            "producer", "purpose",
        },
        "intent lookup key",
    )
    _need(lookup == _lookup_from_envelope(envelope), "intent lookup key differs")
    _need(path.stem == hashlib.sha256(_canonical(lookup)).hexdigest(), "intent path differs")
    _reject_matching_intents(path, lookup)
    return facts, envelope


def _validate_consumed_results(facts: dict[str, object], result_paths: list[Path]) -> None:
    consumed = facts["consumed_results"]
    _need(isinstance(consumed, list), "consumed results are missing")
    _need(len(consumed) == len(result_paths) > 0, "consumed result count differs")
    expected: dict[object, tuple[dict[str, object], bytes]] = {}
    for path in result_paths:
        result, raw = _load(path)
        expected[result.get("work_item_id")] = (result, raw)
    _need(len(expected) == len(result_paths), "result work-item identities conflict")
    for item in consumed:
        entry = _exact(
            item,
            {"work_item_id", "role", "revision", "target", "attempt_id", "result", "finished_at"},
            "consumed result",
        )
        result, raw = expected.pop(entry["work_item_id"], ({}, b""))
        reference = _exact(
            entry["result"], {"path_base", "path", "bytes", "sha256", "kind", "owner"},
            "consumed result reference",
        )
        _need(result.get("attempt_id") == entry["attempt_id"] == reference["owner"], "attempt binding differs")
        _need(result.get("finished_at") == entry["finished_at"], "finished timestamp differs")
        _need(reference["kind"] == "simulation_result", "result reference kind differs")
        _need(reference["bytes"] == len(raw), "result byte count differs")
        _need(reference["sha256"] == _digest(raw), "result digest differs")
    _need(not expected, "acceptance facts omit a terminal result")


def _reject_matching_commits(path: Path, lookup: dict[str, object]) -> None:
    matches = 0
    for sibling in path.parent.glob("*.json"):
        candidate = _load_canonical(sibling, "acceptance commit")
        envelope = candidate.get("envelope")
        if isinstance(envelope, dict) and _lookup_from_envelope(envelope) == lookup:
            matches += 1
    _need(matches == 1, "multiple transactions match the acceptance intent")


def _expected_record(
    envelope: dict[str, object], transaction_id: str, ordinal: int, sequence: int
) -> dict[str, object]:
    changes = envelope["changes"]
    assert isinstance(changes, list)
    change = _exact(
        changes[ordinal], {"key", "met", "reason", "mandatory", "params", "detail", "role"},
        "transaction change",
    )
    origin = _exact(envelope["origin"], {"execution_id", "invocation_id"}, "origin")
    ticket = _exact(envelope["ticket"], {"slug", "identity", "generation"}, "ticket")
    return {
        "$schema": "booley.acceptance-record/v2", "sequence": sequence,
        "transaction_id": transaction_id, "transaction_ordinal": ordinal,
        "transaction_size": len(changes), "envelope_sha256": _digest(_canonical(envelope)),
        "ticket": ticket["slug"], "execution_id": origin["execution_id"],
        "purpose": envelope["purpose"], "producer": envelope["producer"],
        "invocation_id": origin["invocation_id"], "role": change["role"],
        "criterion": change["key"], "met": change["met"], "reason": change["reason"],
        "mandatory": change["mandatory"], "params": change["params"],
        "detail": change["detail"], "ticket_identity": ticket["identity"],
        "recorded_at": envelope["recorded_at"],
    }


def _validate_records(
    entries: list[object], envelope: dict[str, object], transaction_id: str, root: Path
) -> None:
    bound = set()
    for ordinal, value in enumerate(entries):
        entry = _exact(
            value, {"transaction_ordinal", "sequence", "sha256", "criterion", "role"},
            "record reference",
        )
        sequence = entry["sequence"]
        _need(type(sequence) is int and sequence > 0, "record sequence is invalid")
        _need(entry["transaction_ordinal"] == ordinal, "record ordinal differs")
        name = f"{sequence:09d}.tx.{transaction_id}"
        record = _load_canonical(root / name / "record.json", "V2 record")
        expected = _expected_record(envelope, transaction_id, ordinal, sequence)
        _need(record == expected, "V2 record contradicts its envelope")
        _need(entry["sha256"] == _digest(_canonical(record) + b"\n"), "V2 record digest differs")
        _need(entry["criterion"] == record["criterion"], "V2 record criterion differs")
        _need(entry["role"] == record["role"], "V2 record role differs")
        bound.add(name)
    actual = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and path.name.endswith(f".tx.{transaction_id}")
    }
    _need(actual == bound, "transaction has evidence outside its commit")


def _validate_commit(
    path: Path, envelope: dict[str, object], evidence_root: Path
) -> str:
    commit = _load_canonical(path, "acceptance commit")
    _exact(
        commit,
        {"$schema", "transaction_id", "envelope", "envelope_sha256", "record_count", "records"},
        "acceptance commit",
    )
    raw_envelope = _canonical(envelope)
    transaction_id = hashlib.sha256(raw_envelope).hexdigest()
    _need(commit["$schema"] == "booley.acceptance-transaction/v1", "commit schema differs")
    _need(path.stem == commit["transaction_id"] == transaction_id, "transaction ID differs")
    _need(commit["envelope"] == envelope, "commit envelope differs from intent")
    _need(commit["envelope_sha256"] == _digest(raw_envelope), "envelope digest differs")
    records = commit["records"]
    _need(isinstance(records, list), "commit records are missing")
    changes = envelope["changes"]
    assert isinstance(changes, list)
    _need(commit["record_count"] == len(records) == len(changes), "record count differs")
    _reject_matching_commits(path, _lookup_from_envelope(envelope))
    _validate_records(records, envelope, transaction_id, evidence_root)
    return transaction_id


def _validate_criteria_projection(
    archived: dict[str, object], recovered: dict[str, object], envelope: dict[str, object]
) -> None:
    before_raw = archived.get("criteria")
    _need(isinstance(before_raw, dict), "archived criteria are missing")
    expected = json.loads(json.dumps(before_raw))
    changes = envelope["changes"]
    assert isinstance(changes, list)
    for value in changes:
        change = _exact(
            value, {"key", "met", "reason", "mandatory", "params", "detail", "role"},
            "transaction change",
        )
        key = change["key"]
        _need(isinstance(key, str) and key in expected, "changed Criterion is undeclared")
        entry = expected[key]
        _need(isinstance(entry, dict), "archived Criterion is invalid")
        entry.update(
            met=change["met"], mandatory=change["mandatory"],
            params=change["params"], detail=change["detail"],
        )
        if change["met"] is True:
            entry["ever_met"] = True
        else:
            entry["ever_failed"] = True
    _need(recovered.get("criteria") == expected, "recovered Criteria mutation differs")


def _validate_state_transition(
    archived_path: Path, failed_path: Path, recovered_path: Path,
    transaction_id: str, envelope: dict[str, object]
) -> None:
    archived, archived_raw = _load(archived_path)
    failed, failed_raw = _load(failed_path)
    recovered, _ = _load(recovered_path)
    _need(failed_raw == archived_raw and failed == archived, "failed save changed archived state bytes")
    before = archived.get("acceptance_transactions")
    _need(isinstance(before, list), "archived acceptance transactions are missing")
    _need(recovered.get("acceptance_transactions") == [*before, transaction_id], "transaction was not selected exactly once")
    _need(set(recovered) == set(archived), "recovered state fields differ")
    stable = set(archived) - {"acceptance_transactions", "criteria", "all_mandatory_met", "last_updated"}
    _need(all(recovered.get(key) == archived[key] for key in stable), "unrelated state changed")
    _validate_criteria_projection(archived, recovered, envelope)


def validate_acceptance_recovery(
    manifest_path: Path, result_paths: list[Path], intent_path: Path,
    transaction_path: Path, evidence_root: Path, archived_state_path: Path,
    failed_state_path: Path, recovered_state_path: Path, simulation_path: Path,
) -> None:
    """Require byte-authenticated, idempotent acceptance recovery."""
    manifest, _ = _load(manifest_path)
    campaign_id = manifest.get("campaign_id")
    manifest_sha256 = _digest(_canonical(manifest))
    facts, envelope = _validate_intent(intent_path, campaign_id, manifest_sha256)
    _validate_consumed_results(facts, result_paths)
    transaction_id = _validate_commit(transaction_path, envelope, evidence_root)
    _validate_state_transition(
        archived_state_path, failed_state_path, recovered_state_path, transaction_id, envelope
    )
    simulation, _ = _load(simulation_path)
    _need(simulation.get("complete") is True, "simulation projection is not complete")
    _need(simulation.get("campaign_manifest") == str(manifest_path), "projection manifest differs")


def validate_corrupt_terminal(
    archived_result_path: Path, restored_result_path: Path, rejection_path: Path
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
    attempts = rejection.get("attempts_before")
    _need(attempts == rejection.get("attempts_after"), "corrupt resume created an attempt")
    _need(rejection.get("control_exit_code") == 0, "restored control did not succeed")
    _need(rejection.get("control_complete") is True, "restored control is incomplete")
    _need(attempts == rejection.get("attempts_after_restore"), "restored completed work item reran")
    _need(rejection.get("eda_processes_started") == [], "corrupt resume launched EDA")
    valid_sha256 = _digest(archived)
    _need(rejection.get("valid_sha256") == valid_sha256, "archived result digest differs")
    _need(rejection.get("restored_sha256") == valid_sha256, "restored result digest differs")
    _need(rejection.get("corrupt_sha256") != valid_sha256, "corrupt result digest was unchanged")

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from booley.ticket_board import acceptance_ledger as ledger


def _identity() -> dict[str, object]:
    return {"generation": "a" * 32, "slug": "ticket"}


def _envelope() -> dict[str, object]:
    return {
        "$schema": ledger._ENVELOPE_SCHEMA,
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "origin": {"execution_id": "e" * 32, "invocation_id": 1},
        "producer": "simulation_campaign",
        "purpose": "ticket_acceptance",
        "ticket": {"slug": "ticket", "identity": _identity(), "generation": "a" * 32},
        "recorded_at": "2026-09-21T10:00:00Z",
        "role_derivation": ledger._ROLE_DERIVATION,
        "changes": [
            {
                "key": "sim",
                "met": True,
                "reason": "passed",
                "mandatory": True,
                "params": {},
                "detail": {},
                "role": "candidate",
            }
        ],
    }


@pytest.mark.parametrize("value", [object(), float("nan")])
def test_canonical_rejects_non_json_values(value: object) -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="canonical JSON"):
        ledger._canonical({"value": value})


def test_exact_fields_rejects_unknown_fields() -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="unexpected fields"):
        ledger._exact_fields({"a": 1}, {"b"}, "value")


@pytest.mark.parametrize("value", [1, "2026-09-21T10:00:00.1Z", "not-a-dateZ", "2026-9-1T1:2:3Z"])
def test_timestamp_must_be_canonical(value: object) -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="canonical UTC"):
        ledger._canonical_timestamp(value, "timestamp")


@pytest.mark.parametrize("value", [1, "bad", "550e8400-e29b-11d4-a716-446655440000"])
def test_campaign_id_must_be_lowercase_uuid4(value: object) -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="UUIDv4"):
        ledger._campaign_uuid(value)


def test_scalar_identity_fields_are_authenticated() -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="SHA-256"):
        ledger._sha256_field("bad", "digest")
    with pytest.raises(ledger.AcceptanceLedgerError, match="identity"):
        ledger._identity([], "a" * 32)
    with pytest.raises(ledger.AcceptanceLedgerError, match="generation"):
        ledger._identity(_identity(), "b" * 32)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("$schema",), "unknown"),
        (("origin",), None),
        (("ticket",), None),
        (("origin", "execution_id"), "bad"),
        (("origin", "invocation_id"), False),
        (("ticket", "slug"), ""),
        (("changes",), "not-a-list"),
        (("changes", 0), "not-an-object"),
        (("changes", 0, "key"), ""),
        (("changes", 0, "met"), 1),
        (("changes", 0, "role"), "unknown"),
    ],
)
def test_envelope_rejects_invalid_values(path: tuple[str | int, ...], value: object) -> None:
    envelope = _envelope()
    target: object = envelope
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ledger.AcceptanceLedgerError):
        ledger._validate_envelope(envelope)


def test_json_document_requires_size_newline_and_canonical_bytes(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    with pytest.raises(ledger.AcceptanceLedgerError, match="corrupt"):
        ledger._read_json_document(path, 100, "record")

    path.write_bytes(b'{"b":1,"a":2}\n')
    with pytest.raises(ledger.AcceptanceLedgerError, match="noncanonical"):
        ledger._read_json_document(path, 100, "record")


def test_reserved_temporary_and_transaction_paths_are_strict(tmp_path: Path) -> None:
    (tmp_path / ".tmp.acceptance.bad").mkdir()
    with pytest.raises(ledger.AcceptanceLedgerError, match="temporary"):
        ledger._validate_reserved_temp_names(tmp_path)

    transaction_root = tmp_path / "acceptance" / "transactions"
    transaction_root.mkdir(parents=True)
    (transaction_root / "unexpected").write_text("x", encoding="utf-8")
    with pytest.raises(ledger.AcceptanceLedgerError, match="malformed"):
        ledger._matching_commits(tmp_path, tmp_path, {})


def test_existing_prefix_rejects_malformed_and_contradictory_evidence(tmp_path: Path) -> None:
    envelope = _envelope()
    transaction_id = "b" * 64
    malformed = tmp_path / f"bad.tx.{transaction_id}"
    malformed.mkdir()
    with pytest.raises(ledger.AcceptanceLedgerError, match="malformed"):
        ledger._existing_prefix(tmp_path, envelope, transaction_id)

    malformed.rmdir()
    record_dir = tmp_path / f"000000001.tx.{transaction_id}"
    record_dir.mkdir()
    record = ledger._v2_record(envelope, transaction_id, 0, 1)
    record["criterion"] = "different"
    (record_dir / "record.json").write_bytes(ledger._canonical(record) + b"\n")
    with pytest.raises(ledger.AcceptanceLedgerError, match="contradicts"):
        ledger._existing_prefix(tmp_path, envelope, transaction_id)


def test_verify_committed_records_rejects_shape_position_and_binding(tmp_path: Path) -> None:
    envelope = _envelope()
    transaction_id = "b" * 64
    with pytest.raises(ledger.AcceptanceLedgerError, match="must be an object"):
        ledger._verify_committed_records(tmp_path, envelope, transaction_id, [None])

    entry = {
        "transaction_ordinal": 1,
        "sequence": 1,
        "sha256": "sha256:" + "0" * 64,
        "criterion": "sim",
        "role": "candidate",
    }
    with pytest.raises(ledger.AcceptanceLedgerError, match="position"):
        ledger._verify_committed_records(tmp_path, envelope, transaction_id, [entry])

    entry["transaction_ordinal"] = 0
    record_dir = tmp_path / f"000000001.tx.{transaction_id}"
    record_dir.mkdir()
    record = ledger._v2_record(envelope, transaction_id, 0, 1)
    content = ledger._canonical(record) + b"\n"
    (record_dir / "record.json").write_bytes(content)
    with pytest.raises(ledger.AcceptanceLedgerError, match="does not bind"):
        ledger._verify_committed_records(tmp_path, envelope, transaction_id, [entry])


def test_transaction_record_set_must_match_commit(tmp_path: Path) -> None:
    (tmp_path / f"000000001.tx.{'b' * 64}").mkdir()
    with pytest.raises(ledger.AcceptanceLedgerError, match="outside"):
        ledger._verify_no_extra_transaction_records(tmp_path, "b" * 64, [])


def test_recorded_at_requires_bounded_terminal_result_objects() -> None:
    for consumed in (None, [], [None], [{}] * 1001):
        with pytest.raises(ledger.AcceptanceLedgerError):
            ledger._recorded_at({"consumed_results": consumed})


def test_change_payload_requires_a_criterion_key() -> None:
    change = ledger.CriterionChange("", True, "reason", True, {}, {})
    with pytest.raises(ledger.AcceptanceLedgerError, match="must not be empty"):
        ledger._change_payload(change)


@pytest.mark.parametrize("mutation", ["lookup", "facts", "envelope"])
def test_read_intent_authenticates_every_nested_document(tmp_path: Path, mutation: str) -> None:
    lookup = {"key": "value"}
    envelope = _envelope()
    acceptance_facts = {"facts": True}
    intent = {
        "$schema": ledger._INTENT_SCHEMA,
        "lookup_key": lookup,
        "acceptance_facts": acceptance_facts,
        "acceptance_facts_sha256": ledger._digest(ledger._canonical(acceptance_facts)),
        "envelope": envelope,
    }
    if mutation == "lookup":
        intent["lookup_key"] = {"wrong": True}
    elif mutation == "facts":
        intent["acceptance_facts_sha256"] = "sha256:" + "0" * 64
    else:
        intent["envelope"] = []
    path = tmp_path / "intent.json"
    path.write_bytes(ledger._canonical(intent) + b"\n")
    with pytest.raises(ledger.AcceptanceLedgerError):
        ledger._read_intent(path, lookup)


def test_existing_prefix_requires_contiguous_ordinals(tmp_path: Path) -> None:
    envelope = _envelope()
    envelope["changes"] = [deepcopy(envelope["changes"][0]), deepcopy(envelope["changes"][0])]
    transaction_id = "b" * 64
    record_dir = tmp_path / f"000000001.tx.{transaction_id}"
    record_dir.mkdir()
    record = ledger._v2_record(envelope, transaction_id, 1, 1)
    (record_dir / "record.json").write_bytes(ledger._canonical(record) + b"\n")
    with pytest.raises(ledger.AcceptanceLedgerError, match="canonical prefix"):
        ledger._existing_prefix(tmp_path, envelope, transaction_id)


def test_existing_prefix_rejects_duplicate_ordinals(tmp_path: Path) -> None:
    envelope = _envelope()
    transaction_id = "b" * 64
    for sequence in (1, 2):
        record_dir = tmp_path / f"{sequence:09d}.tx.{transaction_id}"
        record_dir.mkdir()
        record = ledger._v2_record(envelope, transaction_id, 0, sequence)
        (record_dir / "record.json").write_bytes(ledger._canonical(record) + b"\n")
    with pytest.raises(ledger.AcceptanceLedgerError, match="duplicate"):
        ledger._existing_prefix(tmp_path, envelope, transaction_id)


def test_legacy_records_require_json_object_schema_and_evidence(tmp_path: Path) -> None:
    transaction_id = "b" * 64
    with pytest.raises(ledger.AcceptanceLedgerError, match="no evidence"):
        ledger._legacy_transaction_records(tmp_path, transaction_id)

    directory = tmp_path / f"000000001.tx.{transaction_id}"
    directory.mkdir()
    (directory / "record.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ledger.AcceptanceLedgerError, match="not an object"):
        ledger._legacy_transaction_records(tmp_path, transaction_id)


def test_plain_legacy_records_reject_malformed_directory_and_schema(tmp_path: Path) -> None:
    malformed = tmp_path / "bad"
    malformed.mkdir()
    with pytest.raises(ledger.AcceptanceLedgerError, match="malformed"):
        ledger._plain_legacy_records(tmp_path)

    malformed.rmdir()
    directory = tmp_path / "000000001"
    directory.mkdir()
    (directory / "record.json").write_bytes(
        ledger._canonical({"sequence": 1, "$schema": "new", "schema": ledger.SCHEMA_VERSION})
        + b"\n"
    )
    with pytest.raises(ledger.AcceptanceLedgerError, match="not V1"):
        ledger._plain_legacy_records(tmp_path)


def _acceptance_facts() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "origin": {"execution_id": "e" * 32, "invocation_id": 1},
        "target": {},
        "required_suite": {},
        "prerequisites": [],
        "consumed_results": [{"finished_at": "2026-09-21T10:00:00Z"}],
        "observations": [],
        "coverage_reference": None,
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("$schema",), "unknown"),
        (("origin",), None),
        (("origin", "execution_id"), "bad"),
        (("origin", "invocation_id"), False),
    ],
)
def test_acceptance_fact_identity_rejects_invalid_origin(
    path: tuple[str | int, ...], value: object
) -> None:
    document = _acceptance_facts()
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ledger.AcceptanceLedgerError):
        ledger._acceptance_fact_identity(document)


def test_timestamp_rejects_parseable_but_noncanonical_separator() -> None:
    with pytest.raises(ledger.AcceptanceLedgerError, match="canonical UTC"):
        ledger._canonical_timestamp("2026-09-21t10:00:00Z", "timestamp")


def test_transaction_builder_enforces_change_slug_and_document_limits(monkeypatch) -> None:
    identity = {"generation": "a" * 32}
    state = type("State", (), {"slug": "ticket"})()
    monkeypatch.setattr(ledger, "_MAX_CHANGES", -1)
    with pytest.raises(ledger.AcceptanceLedgerError, match="too many"):
        ledger._build_transaction_documents(state, [], _acceptance_facts(), identity)

    monkeypatch.setattr(ledger, "_MAX_CHANGES", 4_000)
    state.slug = ""
    with pytest.raises(ledger.AcceptanceLedgerError, match="slug"):
        ledger._build_transaction_documents(state, [], _acceptance_facts(), identity)

    state.slug = "ticket"
    monkeypatch.setattr(ledger, "_MAX_DOCUMENT_BYTES", 0)
    with pytest.raises(ledger.AcceptanceLedgerError, match="envelope is too large"):
        ledger._build_transaction_documents(state, [], _acceptance_facts(), identity)


def test_intent_prefix_sequence_and_platform_guards(tmp_path: Path, monkeypatch) -> None:
    assert ledger._existing_prefix(tmp_path / "missing", _envelope(), "b" * 64) == []

    monkeypatch.setattr(ledger, "range", lambda *_args: (), raising=False)
    with pytest.raises(ledger.AcceptanceLedgerError, match="sequence exhausted"):
        ledger._next_sequence(tmp_path)

    monkeypatch.setattr(ledger, "os", type("OS", (), {"name": "nt"})())
    ledger._fsync_directory(tmp_path)


def test_intent_and_record_size_limits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ledger, "_MAX_INTENT_BYTES", 0)
    with pytest.raises(ledger.AcceptanceLedgerError, match="intent is too large"):
        ledger._load_or_publish_intent(tmp_path, {"key": "value"}, {"value": True})

    monkeypatch.setattr(ledger, "_MAX_RECORD_BYTES", 0)
    with pytest.raises(ledger.AcceptanceLedgerError, match="record is too large"):
        ledger._publish_v2_record(tmp_path, _envelope(), "b" * 64, 0)


def test_temp_recovery_rejects_reserved_malformed_paths(tmp_path: Path) -> None:
    (tmp_path / ".tmp.acceptance.bad").mkdir()
    with pytest.raises(ledger.AcceptanceLedgerError, match="malformed"):
        ledger._recover_current_temps(tmp_path, _envelope(), "b" * 64)


def test_commit_transaction_id_must_match_envelope(tmp_path: Path) -> None:
    envelope = _envelope()
    envelope["changes"] = []
    transaction_id = "b" * 64
    commit = {
        "$schema": ledger._COMMIT_SCHEMA,
        "transaction_id": transaction_id,
        "envelope": envelope,
        "envelope_sha256": ledger._digest(ledger._canonical(envelope)),
        "record_count": 0,
        "records": [],
    }
    path = tmp_path / "commit.json"
    path.write_bytes(ledger._canonical(commit) + b"\n")
    with pytest.raises(ledger.AcceptanceLedgerError, match="ID does not match"):
        ledger._verify_commit(tmp_path, path, transaction_id)

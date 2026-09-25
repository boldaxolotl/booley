from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import child_protocol
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError, canonical_json_bytes
from booley.runtime.execution_records import ExecutionId


def _entry(execution_id: str = "e" * 32) -> dict[str, object]:
    return {
        "$schema": child_protocol._ENTRY_SCHEMA,
        "child_execution_id": execution_id,
        "parent_execution_id": "a" * 32,
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_path": "/project/manifest.json",
        "manifest_sha256": "sha256:" + "1" * 64,
        "work_item_id": "item:0000:0123456789abcdef",
        "attempt_id": "550e8400-e29b-41d4-a716-446655440001",
        "attempt_ordinal": 1,
        "attempt_relative_path": "items/item/attempts/0001-attempt",
        "runtime_context_sha256": "sha256:" + "2" * 64,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("child_execution_id", "bad"),
        ("attempt_ordinal", False),
        ("attempt_ordinal", 0),
        ("manifest_sha256", "bad"),
        ("runtime_context_sha256", 1),
        ("parent_execution_id", ""),
        ("campaign_id", 1),
    ],
)
def test_child_entry_rejects_invalid_fields(tmp_path: Path, field: str, value: object) -> None:
    entry = _entry()
    entry[field] = value
    with pytest.raises(SimulationCampaignIntegrityError):
        child_protocol._validate_entry(tmp_path / f"{'e' * 32}.json", entry)


def test_child_entry_rejects_schema_and_filename_mismatches(tmp_path: Path) -> None:
    entry = _entry()
    entry["extra"] = True
    with pytest.raises(SimulationCampaignIntegrityError, match="schema"):
        child_protocol._validate_entry(tmp_path / f"{'e' * 32}.json", entry)

    with pytest.raises(SimulationCampaignIntegrityError, match="filename"):
        child_protocol._validate_entry(tmp_path / f"{'f' * 32}.json", _entry())


@pytest.mark.parametrize("value", [r"items\attempt", "/absolute", "../escape", "a/../b"])
def test_child_attempt_path_must_be_canonical_and_contained(value: str) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        child_protocol._safe_relative_path(value)


def test_child_retirement_rejects_invalid_json_and_identity() -> None:
    execution_id = ExecutionId("e" * 32)
    digest = "sha256:" + "1" * 64
    with pytest.raises(SimulationCampaignIntegrityError, match="invalid JSON"):
        child_protocol._validate_retirement(b"{", execution_id, digest, digest)

    retirement = {
        "$schema": child_protocol._RETIREMENT_SCHEMA,
        "child_execution_id": str(execution_id),
        "entry_sha256": digest,
        "execution_terminal_sha256": "sha256:" + "2" * 64,
        "lease_id": None,
        "token_absent": True,
        "terminal_cause": "completed",
    }
    with pytest.raises(SimulationCampaignIntegrityError, match="terminal digest"):
        child_protocol._validate_retirement(
            canonical_json_bytes(retirement), execution_id, digest, digest
        )

    retirement["execution_terminal_sha256"] = digest
    retirement["token_absent"] = False
    with pytest.raises(SimulationCampaignIntegrityError, match="identity"):
        child_protocol._validate_retirement(
            canonical_json_bytes(retirement), execution_id, digest, digest
        )


def test_protocol_record_rejects_invalid_and_noncanonical_json(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"{")
    with pytest.raises(SimulationCampaignIntegrityError, match="invalid JSON"):
        child_protocol._read_protocol_record(path, "record")

    path.write_text(json.dumps({"value": 1}), encoding="utf-8")
    with pytest.raises(SimulationCampaignIntegrityError, match="not canonical"):
        child_protocol._read_protocol_record(path, "record")


def test_publish_rejects_conflicting_existing_record(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"old")
    with pytest.raises(SimulationCampaignIntegrityError, match="disagrees"):
        child_protocol._publish_or_verify(path, b"new")


def test_protocol_record_size_limit_is_enforced(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"xx")
    monkeypatch.setattr(child_protocol, "_PROTOCOL_RECORD_LIMIT", 1)
    with pytest.raises(SimulationCampaignIntegrityError, match="size limit"):
        child_protocol._read_protocol_bytes(path, "record")


def _registry(tmp_path: Path) -> child_protocol.ChildExecutionRegistry:
    registry = object.__new__(child_protocol.ChildExecutionRegistry)
    registry._project_data_root = tmp_path
    return registry


def _prepared(tmp_path: Path) -> child_protocol.PreparedChild:
    return child_protocol.PreparedChild(
        ExecutionId("e" * 32),
        "sha256:" + "1" * 64,
        tmp_path / "project.json",
        tmp_path / "campaign.json",
        tmp_path / "context.json",
    )


@pytest.mark.parametrize("record", [None, {"state": "running", "tree_terminal": False}])
def test_mark_terminal_requires_a_recoverable_or_terminal_record(
    tmp_path: Path, monkeypatch, record
) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        child_protocol,
        "execution_paths",
        lambda *_args, **_kwargs: SimpleNamespace(record=tmp_path / "record"),
    )
    monkeypatch.setattr(child_protocol, "read_json", lambda _path: record)
    with pytest.raises(SimulationCampaignIntegrityError):
        registry.mark_terminal(_prepared(tmp_path), "failed")


def test_retire_requires_token_absence_and_terminal_proof(tmp_path: Path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        child_protocol,
        "execution_paths",
        lambda *_args, **_kwargs: SimpleNamespace(record=tmp_path / "record"),
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="token remains"):
        registry.retire(
            _prepared(tmp_path), lease_id=None, terminal_cause="failed", token_absent=False
        )
    monkeypatch.setattr(child_protocol, "read_json", lambda _path: None)
    with pytest.raises(SimulationCampaignIntegrityError, match="tree-terminal"):
        registry.retire(
            _prepared(tmp_path), lease_id=None, terminal_cause="failed", token_absent=True
        )


def test_cancel_reports_timeout(tmp_path: Path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        child_protocol,
        "execution_paths",
        lambda *_args, **_kwargs: SimpleNamespace(record=tmp_path / "record"),
    )
    monkeypatch.setattr(child_protocol, "request_cancellation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(child_protocol, "recover_execution", lambda *_args, **_kwargs: False)
    moments = iter([0.0, 6.0])
    monkeypatch.setattr(child_protocol.time, "monotonic", lambda: next(moments))
    assert registry.cancel(ExecutionId("e" * 32)) is False


def test_release_matching_token_rejects_duplicates_and_stale_files(tmp_path: Path) -> None:
    execution_id = ExecutionId("e" * 32)
    token = SimpleNamespace(execution_id=execution_id, lease_id="lease", path=tmp_path / "token")

    class Store:
        def __init__(self, values):
            self.values = values

        def snapshot(self, _kind):
            return self.values, ()

        def release(self, selected):
            assert selected is token

    with pytest.raises(SimulationCampaignIntegrityError, match="duplicate"):
        child_protocol.ChildExecutionRegistry._release_matching_token(
            Store((token, token)), execution_id
        )
    token.path.write_text("present", encoding="utf-8")
    with pytest.raises(SimulationCampaignIntegrityError, match="remains"):
        child_protocol.ChildExecutionRegistry._release_matching_token(
            Store((token,)), execution_id
        )


def test_token_absence_checks_managed_and_unmanaged_claims(tmp_path: Path, monkeypatch) -> None:
    execution_id = ExecutionId("e" * 32)
    token = SimpleNamespace(execution_id=execution_id)
    registry = _registry(tmp_path)
    store = SimpleNamespace(snapshot=lambda _kind: ((token,), ()))
    with pytest.raises(SimulationCampaignIntegrityError, match="slot claim remains"):
        registry._assert_token_absent(store, execution_id)

    token_path = tmp_path / "runtime" / "jobs" / "slots" / "heavy" / "token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(child_protocol, "read_json", lambda _path: {"execution_id": execution_id})
    with pytest.raises(SimulationCampaignIntegrityError, match="cannot be reconciled"):
        registry._assert_no_unmanaged_token(execution_id)

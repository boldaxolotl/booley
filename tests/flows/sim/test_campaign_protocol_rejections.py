from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import child_protocol
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError, canonical_json_bytes
from booley.flows.sim.campaign.store import CampaignStore
from booley.runtime.execution_records import ExecutionId, execution_paths
from tests.flows.sim.campaign_v030_support import publish_v030_child
from tests.flows.sim.test_campaign_phase3 import _two_item_manifest
from tests.fusesoc_test_support import symlinked_booley_dir


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


def test_child_recovery_rejects_display_form_parent_identity(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    registry = child_protocol.ChildExecutionRegistry(store, manifest, project)
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440001"
    ordinal, attempt = store.allocate_attempt_directory(str(item["work_item_id"]), attempt_id)
    prepared = publish_v030_child(
        registry,
        execution_id=ExecutionId("e" * 32),
        work_item_id=str(item["work_item_id"]),
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt,
        parent_execution_id=ExecutionId("a" * 32),
    )
    for path in (prepared.project_entry, prepared.campaign_entry):
        entry = json.loads(path.read_text())
        entry["parent_execution_id"] = "sim-20260925T153819Z-1"
        path.chmod(0o600)
        path.write_bytes(canonical_json_bytes(entry))

    with pytest.raises(
        SimulationCampaignIntegrityError,
        match="child entry parent execution identity is invalid",
    ):
        registry.recover_unretired(None)


@pytest.mark.parametrize("layout", ["entry", "below", "unvalidated"])
def test_child_records_publish_and_verify_through_the_booley_dir_entry(
    tmp_path: Path, layout: str
) -> None:
    """#2191: the symlinked ``.booley`` entry is followed; a link below it is refused.

    ``unvalidated`` is recovery handed a linked project-data path no caller proved to be the
    configured entry: it must stay refused.
    """
    manifest = _two_item_manifest()
    # The Campaign store is plain: only the Project-local child records cross the entry.
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    external = symlinked_booley_dir(project, tmp_path / "external-booley")
    if layout == "below":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (external / ".runtime").symlink_to(elsewhere, target_is_directory=True)
    if layout == "unvalidated":
        registry = child_protocol.ChildExecutionRegistry.from_project_data(
            store, manifest, project / ".booley"
        )
    else:
        registry = child_protocol.ChildExecutionRegistry(store, manifest, project)
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440001"
    ordinal, attempt = store.allocate_attempt_directory(str(item["work_item_id"]), attempt_id)
    fields = {
        "work_item_id": str(item["work_item_id"]),
        "attempt_id": attempt_id,
        "attempt_ordinal": ordinal,
        "attempt_directory": attempt,
        "parent_execution_id": ExecutionId("a" * 32),
    }

    # Unique: recovery signals every host process carrying this ID (xdist workers too).
    execution_id = ExecutionId(uuid.uuid4().hex)

    def cycle() -> None:
        # Publish, verify the identical records (second prepare), then recover a 0.3.0 entry.
        publish_v030_child(registry, execution_id=execution_id, **fields)
        registry.prepare(execution_id, **fields)
        registry.recover_unretired(None)

    if layout != "entry":
        with pytest.raises(SimulationCampaignIntegrityError, match="link"):
            cycle()
        return
    cycle()
    retired = f"{execution_id}.json"
    assert (external / ".runtime/campaign-child-executions/retired" / retired).is_file()
    assert (store.root / "child-executions/retired" / retired).is_file()


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
    registry._booley_dir = None
    registry._record_locks = {}
    registry._record_locks_gate = threading.Lock()
    return registry


def _prepared(tmp_path: Path) -> child_protocol.PreparedChild:
    return child_protocol.PreparedChild(ExecutionId("e" * 32), tmp_path / "context.json")


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


def test_v030_retirement_requires_terminal_proof(tmp_path: Path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(
        child_protocol,
        "execution_paths",
        lambda *_args, **_kwargs: SimpleNamespace(record=tmp_path / "record"),
    )
    monkeypatch.setattr(child_protocol, "read_json", lambda _path: None)
    child = child_protocol._V030Child(
        ExecutionId("e" * 32), "sha256:" + "1" * 64, tmp_path / "context.json"
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="tree-terminal"):
        registry._retire_v030(child, None, "failed")


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
    registry = _registry(tmp_path)
    store = SimpleNamespace(execution_token_absent=lambda _kind, _id: False)
    with pytest.raises(SimulationCampaignIntegrityError, match="slot claim remains"):
        registry._assert_token_absent(store, execution_id)

    token_path = tmp_path / "runtime" / "jobs" / "slots" / "heavy" / "token.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        child_protocol, "read_slot_entry", lambda _path: {"execution_id": execution_id}
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="cannot be reconciled"):
        registry._assert_no_unmanaged_token(execution_id)

    def unreadable(_path):
        raise PermissionError(13, "held by a scanner")

    monkeypatch.setattr(child_protocol, "read_slot_entry", unreadable)
    with pytest.raises(SimulationCampaignIntegrityError, match="entry unreadable"):
        registry._assert_no_unmanaged_token(execution_id)


def test_recovery_refuses_unreadable_child_token(tmp_path: Path) -> None:
    # An unreadable slot entry may be the child's live claim: never retire it as absent (#1905).
    execution_id = ExecutionId("e" * 32)
    store = SimpleNamespace(
        snapshot=lambda _kind: ((), ()),
        execution_token_absent=lambda _kind, _id: False,
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="cannot be proven"):
        _registry(tmp_path)._release_matching_token(store, execution_id)


@pytest.mark.parametrize("error", ["truncated", "schema", "missing"])
@pytest.mark.parametrize("kind", ["entries", "retired"])
def test_child_reader_rejects_corrupt_record(tmp_path, kind, error):
    from booley.flows.sim.campaign_retention import prune_invocation
    from booley.runtime.execution_records import _campaign_child_records
    from tests.flows.sim.test_campaign_phase6 import _publish_retired_child, _retained_invocation

    reports, project_data, store = _retained_invocation(tmp_path)
    execution_id = _publish_retired_child(project_data, store)
    assert f"{execution_id}.json" in _campaign_child_records(store.root / "child-executions")
    control = tmp_path / "control"
    control.mkdir()
    valid_reports, valid_project, valid_store = _retained_invocation(control)
    _publish_retired_child(valid_project, valid_store)
    prune_invocation(valid_reports, 1)
    path = store.root / "child-executions" / kind / f"{execution_id}.json"
    document = json.loads(path.read_bytes())
    if error == "schema":
        document["$schema"] = "unknown"
    elif error == "missing":
        del document["child_execution_id"]
    path.write_bytes(b"{" if error == "truncated" else canonical_json_bytes(document))
    with pytest.raises(ValueError):
        prune_invocation(reports, 1)


def _lane_registry(tmp_path: Path):
    """A real registry over a stored Campaign, plus one prepared (waiting) lane."""
    manifest = _two_item_manifest()
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    registry = child_protocol.ChildExecutionRegistry(store, manifest, project)
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440001"
    ordinal, attempt = store.allocate_attempt_directory(str(item["work_item_id"]), attempt_id)

    def prepare(execution_id: str) -> child_protocol.PreparedChild:
        return registry.prepare(
            ExecutionId(execution_id),
            work_item_id=str(item["work_item_id"]),
            attempt_id=attempt_id,
            attempt_ordinal=ordinal,
            attempt_directory=attempt,
            parent_execution_id=ExecutionId("a" * 32),
        )

    return registry, prepare


def test_recover_lane_finishes_a_waiting_lane(tmp_path: Path) -> None:
    registry, prepare = _lane_registry(tmp_path)
    # Unique: real recovery signals every host process carrying this ID (xdist workers too).
    lane = prepare(uuid.uuid4().hex)
    assert not registry.is_terminal(lane.execution_id)

    registry.recover_lane(lane.execution_id)

    assert registry.is_terminal(lane.execution_id)
    registry.recover_lane(lane.execution_id)  # already terminal: nothing to do


def test_recover_lane_fails_closed_when_cancel_fails(tmp_path: Path, monkeypatch) -> None:
    registry, prepare = _lane_registry(tmp_path)
    cancelled: list[ExecutionId] = []

    def failing_cancel(execution_id: ExecutionId) -> bool:
        cancelled.append(execution_id)
        return False

    monkeypatch.setattr(registry, "cancel", failing_cancel)
    registry.recover_lane(ExecutionId("d" * 32))  # no record: the lane never started
    assert cancelled == []

    lane = prepare("e" * 32)
    with pytest.raises(SimulationCampaignIntegrityError, match="recovery is incomplete"):
        registry.recover_lane(lane.execution_id)
    assert cancelled == [lane.execution_id]


def test_recover_lane_treats_an_unreadable_record_as_indeterminate(
    tmp_path: Path, monkeypatch
) -> None:
    registry, prepare = _lane_registry(tmp_path)
    lane = prepare("f" * 32)
    paths = execution_paths(lane.execution_id, booley_dir=tmp_path / "project" / ".booley")
    paths.record.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(registry, "cancel", lambda execution_id: False)

    with pytest.raises(SimulationCampaignIntegrityError, match="recovery is incomplete"):
        registry.recover_lane(lane.execution_id)

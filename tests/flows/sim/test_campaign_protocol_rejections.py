from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import child_protocol
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from booley.flows.sim.campaign.store import CampaignStore
from booley.runtime.execution_records import ExecutionId, execution_paths
from tests.flows.sim.test_campaign_phase3 import _two_item_manifest
from tests.fusesoc_test_support import symlinked_booley_dir


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
            manifest, project / ".booley"
        )
    else:
        registry = child_protocol.ChildExecutionRegistry(manifest, project)
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
        # Publish, then verify the identical records (second prepare).
        registry.prepare(execution_id, **fields)
        registry.prepare(execution_id, **fields)

    if layout != "entry":
        with pytest.raises(SimulationCampaignIntegrityError, match="link"):
            cycle()
        return
    cycle()
    assert execution_paths(execution_id, booley_dir=external).context.is_file()


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


def _lane_registry(tmp_path: Path):
    """A real registry over a stored Campaign, plus one prepared (waiting) lane."""
    manifest = _two_item_manifest()
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    registry = child_protocol.ChildExecutionRegistry(manifest, project)
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

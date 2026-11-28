from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.record import child_protocol
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.runtime.execution_records import (
    ExecutionId,
    atomic_write_json,
    execution_paths,
)
from tests.flows.sim.simulation_record_test_support import _two_item_manifest
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
    # The Simulation Record store is plain: only the Project-local child records cross the entry.
    store = SimulationRecordStore(tmp_path / "record")
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
        with pytest.raises(SimulationRecordIntegrityError, match="link"):
            cycle()
        return
    cycle()
    assert execution_paths(execution_id, booley_dir=external).context.is_file()


def _registry(tmp_path: Path) -> child_protocol.ChildExecutionRegistry:
    return child_protocol.ChildExecutionRegistry.from_project_data(_two_item_manifest(), tmp_path)


def _prepared(tmp_path: Path) -> child_protocol.PreparedChild:
    return child_protocol.PreparedChild(ExecutionId("e" * 32), tmp_path / "context.json")


@pytest.mark.parametrize("record", [None, {"state": "running", "tree_terminal": False}])
def test_mark_terminal_requires_a_recoverable_or_terminal_record(tmp_path: Path, record) -> None:
    registry = _registry(tmp_path)
    paths = execution_paths(ExecutionId("e" * 32), booley_dir=tmp_path)
    paths.root.mkdir(parents=True)
    if record is not None:
        atomic_write_json(paths.record, record)
    with pytest.raises(SimulationRecordIntegrityError):
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
    monkeypatch.setattr(child_protocol, "monotonic", lambda: next(moments))
    assert registry.cancel(ExecutionId("e" * 32)) is False


def _lane_registry(tmp_path: Path):
    """A real registry over a stored Simulation Record, plus one prepared (waiting) lane."""
    manifest = _two_item_manifest()
    store = SimulationRecordStore(tmp_path / "record")
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


@pytest.mark.parametrize("state", ["never_started", "waiting", "unreadable"])
def test_recover_lane_fails_closed_when_cancel_fails(
    tmp_path: Path, monkeypatch, state: str
) -> None:
    registry, prepare = _lane_registry(tmp_path)
    cancelled: list[ExecutionId] = []

    def failing_cancel(execution_id: ExecutionId) -> bool:
        cancelled.append(execution_id)
        return False

    monkeypatch.setattr(registry, "cancel", failing_cancel)
    execution_id = ExecutionId("e" * 32)
    if state == "never_started":
        registry.recover_lane(execution_id)  # no record: the lane never started
        assert cancelled == []
        return
    prepare(execution_id)
    if state == "unreadable":
        paths = execution_paths(execution_id, booley_dir=tmp_path / "project" / ".booley")
        paths.record.write_text("{not json", encoding="utf-8")

    with pytest.raises(SimulationRecordIntegrityError, match="recovery is incomplete"):
        registry.recover_lane(execution_id)
    assert cancelled == [execution_id]


@pytest.mark.parametrize("defect", ["conflict", "symlink"])
def test_prepare_rejects_a_conflicting_or_linked_context_record(
    tmp_path: Path, defect: str
) -> None:
    _registry, prepare = _lane_registry(tmp_path)
    execution_id = ExecutionId("c" * 32)
    context = execution_paths(execution_id, booley_dir=tmp_path / "project" / ".booley").context
    context.parent.mkdir(parents=True)
    source = tmp_path / "source.json"
    source.write_bytes(canonical_json_bytes({"value": "expected"}))
    if defect == "conflict":
        context.write_bytes(b"old")
    else:
        context.symlink_to(source)

    with pytest.raises(
        SimulationRecordIntegrityError, match="disagrees" if defect == "conflict" else "link"
    ):
        prepare(execution_id)

    assert source.read_bytes() == canonical_json_bytes({"value": "expected"})

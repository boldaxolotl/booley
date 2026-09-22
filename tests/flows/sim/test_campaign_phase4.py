"""Stable-seam verification for bounded Simulation Campaign scheduling."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.jobs import SlotCaps
from booley.flows.base import FlowMechanics
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign import child_protocol
from booley.flows.sim.campaign.capacity import HeavyCapacity, HeavyCapacityError
from booley.flows.sim.campaign.child_protocol import ChildExecutionRegistry
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
    SimulationCampaignCancellationError,
)
from booley.flows.sim.campaign.model import SimulationAttempt, create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
from booley.flows.sim.campaign.scheduler import (
    BoundedCampaignScheduler,
    CampaignSchedulingError,
    ScheduledAttempt,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from booley.runtime.execution_records import (
    ExecutionId,
    child_context_matches,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_HEAVY,
    HOLDING,
    QUEUED,
    ROLE_INTERACTIVE,
    ROLE_TICKET,
    SlotStore,
)
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    SupervisedProcessSet,
    supervised_execution_scope,
)
from tests.flows.sim.test_campaign_manifest_codec import _sha
from tests.flows.sim.test_campaign_phase3 import _build_execution, _two_item_manifest


def _unmanaged() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


def _managed(store: SlotStore, outer, *, max_heavy: int = 2) -> AdmissionContext:
    return AdmissionContext(
        "managed",
        store,
        outer,
        max_heavy,
        "interactive",
        "a" * 32,
        5.0,
        lambda: False,
    )


def test_cancelled_campaign_stops_before_publication() -> None:
    request = SimpleNamespace(admission=SimpleNamespace(cancellation=lambda: True))
    with pytest.raises(SimulationCampaignCancellationError, match="cancelled"):
        SimulationCampaign._raise_if_cancelled(request)  # type: ignore[arg-type]


def _three_item_manifest():
    document = json.loads(canonical_json_bytes(_two_item_manifest().document))
    document.pop("fingerprints")
    document["required_suite"]["names"] = ["alpha", "beta", "gamma"]
    identity = {
        key: value
        for key, value in document["work_items"][1].items()
        if key not in {"work_item_id", "fingerprint_sha256"}
    }
    identity["ordinal"] = 2
    identity["selection"] = {"kind": "named", "names": ["gamma"]}
    identity["arguments"] = ["gamma"]
    fingerprint = _sha(identity)
    document["work_items"].append(
        {
            "work_item_id": "item:0002:" + fingerprint.removeprefix("sha256:")[:16],
            **identity,
            "fingerprint_sha256": fingerprint,
        }
    )
    return finalize_manifest(document)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before the deadline")


def _prepared_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    project_data = project / ".booley_project"
    project_data.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_data))
    item = manifest.document["work_items"][0]
    attempt_id = "550e8400-e29b-41d4-a716-446655440010"
    ordinal, attempt = campaign_store.allocate_attempt_directory(
        str(item["work_item_id"]), attempt_id
    )
    child_id = ExecutionId("e" * 32)
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    prepared = registry.prepare(
        child_id,
        work_item_id=str(item["work_item_id"]),
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt,
        parent_execution_id="a" * 32,
    )
    flow = FlowMechanics()
    flow.args = SimpleNamespace(work_dir=project)
    return project_data, child_id, registry, prepared, flow


def test_child_scope_keeps_whole_work_item_nonterminal_between_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_data, child_id, registry, prepared, flow = _prepared_child(tmp_path, monkeypatch)
    scope = SupervisedExecutionScope(child_id, project_data, lambda: False)
    with supervised_execution_scope(scope):
        first = flow._execute_local([sys.executable, "-c", "print('first')"], timeout=5)
        between = read_json(execution_paths(child_id, project_dir=project_data).record)
        second = flow._execute_local([sys.executable, "-c", "print('second')"], timeout=5)
    record = read_json(execution_paths(child_id, project_dir=project_data).record)
    assert first.returncode == second.returncode == 0
    assert first.stdout.strip() == "first"
    assert second.stdout.strip() == "second"
    assert between is not None and between["state"] == "waiting"
    assert record is not None
    assert record["state"] == "waiting"
    assert record["tree_terminal"] is False
    registry.mark_terminal(prepared, "completed")
    terminal = read_json(execution_paths(child_id, project_dir=project_data).record)
    assert terminal is not None and terminal["tree_terminal"] is True


def test_cancelled_scope_rejects_a_later_command_between_processes(
    tmp_path: Path,
) -> None:
    cancelled = threading.Event()
    scope = SupervisedExecutionScope(None, tmp_path, cancelled.is_set)
    flow = FlowMechanics()
    flow.args = SimpleNamespace(work_dir=tmp_path)
    marker = tmp_path / "must-not-run"
    with supervised_execution_scope(scope):
        first = flow._execute_local([sys.executable, "-c", "print('first')"], timeout=5)
        cancelled.set()
        second = flow._execute_local(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            timeout=5,
        )
    assert first.returncode == 0
    assert second.returncode == -1
    assert not marker.exists()


def test_process_registration_closes_constructor_cancellation_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled = threading.Event()
    cancelled.set()  # cancellation raced after the caller's pre-spawn check
    killed: list[object] = []
    monkeypatch.setattr("booley.runtime.supervised_execution.kill_process_tree", killed.append)
    process = object()
    processes = SupervisedProcessSet(None, tmp_path, cancelled.is_set)
    processes.register(process)  # type: ignore[arg-type]
    assert killed == [process]


def test_real_child_process_is_terminated_immediately_on_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    project_data, child_id, registry, _prepared, flow = _prepared_child(tmp_path, monkeypatch)
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = HeavyCapacity(
        _managed(store, outer),
        terminal_proof=registry.is_terminal,
        recover_child=registry.cancel,
    )
    started = time.monotonic()
    try:
        with (
            pytest.raises(HeavyCapacityError, match="lease lost"),
            capacity.outer_permit(),
            capacity.child_permit("item", child_id) as permit,
        ):
            record_path = execution_paths(child_id, project_dir=project_data).record
            killer = threading.Thread(
                target=lambda: (
                    _wait_until(lambda: (read_json(record_path) or {}).get("state") == "running"),
                    permit.token.path.unlink(),
                )
            )
            killer.start()
            scope = SupervisedExecutionScope(
                child_id, project_data, permit.lease_health.lost.is_set
            )
            with supervised_execution_scope(scope):
                flow._execute_local(
                    [sys.executable, "-c", "import time; time.sleep(30)"], timeout=35
                )
            killer.join(timeout=2)
    finally:
        store.release(outer)
    assert time.monotonic() - started < 8
    assert registry.is_terminal(child_id)


def test_unmanaged_capacity_borrows_one_lane_and_never_claims_a_child() -> None:
    capacity = HeavyCapacity(
        _unmanaged(), terminal_proof=lambda _execution_id: True, recover_child=lambda _id: True
    )
    with capacity.outer_permit() as permit:
        assert permit.execution_id == ""
        assert capacity.max_lanes == 1
        assert capacity.managed is False
    with pytest.raises(HeavyCapacityError, match="exactly once"), capacity.outer_permit():
        pass
    with (
        pytest.raises(HeavyCapacityError, match="cannot acquire child"),
        capacity.child_permit("item", ExecutionId("b" * 32)),
    ):
        pass


def test_managed_capacity_releases_and_reacquires_exact_child_tokens(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    terminal: set[ExecutionId] = set()
    recovered: list[ExecutionId] = []

    def recover(execution_id: ExecutionId) -> bool:
        recovered.append(execution_id)
        terminal.add(execution_id)
        return True

    capacity = HeavyCapacity(
        _managed(store, outer),
        terminal_proof=terminal.__contains__,
        recover_child=recover,
    )
    child_ids = [ExecutionId("b" * 32), ExecutionId("c" * 32)]
    try:
        with capacity.outer_permit():
            for index, execution_id in enumerate(child_ids):
                with capacity.child_permit(f"item-{index}", execution_id) as permit:
                    holders, waiters = store.snapshot(CLASS_HEAVY)
                    assert waiters == []
                    assert {token.execution_id for token in holders} == {
                        ExecutionId("a" * 32),
                        execution_id,
                    }
                    assert permit.token.execution_id == execution_id
                holders, waiters = store.snapshot(CLASS_HEAVY)
                assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
                assert waiters == []
        assert recovered == child_ids
    finally:
        store.release(outer)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_real_slot_store_preserves_interactive_priority_and_peer_fifo(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    holder = store.acquire(CLASS_HEAVY, pid=os.getpid())
    first_ticket = store.submit(CLASS_HEAVY, pid=os.getpid(), role=ROLE_TICKET)
    second_ticket = store.submit(CLASS_HEAVY, pid=os.getpid(), role=ROLE_TICKET)
    interactive = store.submit(CLASS_HEAVY, pid=os.getpid(), role=ROLE_INTERACTIVE)
    try:
        holders, waiters = store.snapshot(CLASS_HEAVY)
        assert [token.lease_id for token in holders] == [holder.lease_id]
        assert [token.lease_id for token in waiters] == [
            interactive.lease_id,
            first_ticket.lease_id,
            second_ticket.lease_id,
        ]
        store.release(holder)
        assert store.refresh(interactive).state == HOLDING
        assert store.refresh(first_ticket).state == QUEUED
        store.release(interactive)
        assert store.refresh(first_ticket).state == HOLDING
        assert store.refresh(second_ticket).state == QUEUED
    finally:
        for token in (holder, interactive, first_ticket, second_ticket):
            store.release(token)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_capacity_cancellation_withdraws_a_real_queued_child(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    capacity = HeavyCapacity(
        _managed(store, outer, max_heavy=1),
        terminal_proof=lambda _execution_id: True,
        recover_child=lambda _execution_id: True,
    )
    errors: list[BaseException] = []

    def wait_for_child() -> None:
        try:
            with capacity.child_permit("queued", ExecutionId("b" * 32)):
                raise AssertionError("queued child unexpectedly acquired a slot")
        except BaseException as exc:  # noqa: BLE001 -- assertion inspects worker outcome
            errors.append(exc)

    worker = threading.Thread(target=wait_for_child)
    worker.start()
    try:
        _wait_until(lambda: len(store.snapshot(CLASS_HEAVY)[1]) == 1)
        capacity.cancel_waiters()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert store.snapshot(CLASS_HEAVY)[1] == []
    finally:
        store.release(outer)


def test_capacity_exception_recovers_then_releases_exact_child(tmp_path: Path) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    terminal: set[ExecutionId] = set()
    child_id = ExecutionId("e" * 32)

    def recover(execution_id: ExecutionId) -> bool:
        terminal.add(execution_id)
        return True

    capacity = HeavyCapacity(
        _managed(store, outer),
        terminal_proof=terminal.__contains__,
        recover_child=recover,
    )
    try:
        with (
            capacity.outer_permit(),
            pytest.raises(RuntimeError, match="injected worker failure"),
            capacity.child_permit("item", child_id),
        ):
            raise RuntimeError("injected worker failure")
        holders, waiters = store.snapshot(CLASS_HEAVY)
        assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
        assert waiters == []
        assert terminal == {child_id}
    finally:
        store.release(outer)


def test_child_reacquisition_rejoins_real_slot_fifo_behind_competitor(
    tmp_path: Path,
) -> None:
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    terminal: set[ExecutionId] = set()
    capacity = HeavyCapacity(
        _managed(store, outer),
        terminal_proof=terminal.__contains__,
        recover_child=lambda execution_id: terminal.add(execution_id) is None,
    )
    first = ExecutionId("b" * 32)
    competitor = None
    try:
        with capacity.outer_permit(), capacity.child_permit("first", first):
            competitor = store.submit(
                CLASS_HEAVY,
                pid=os.getpid(),
                role=ROLE_INTERACTIVE,
                execution_id=ExecutionId("c" * 32),
            )
            assert store.refresh(competitor).state == QUEUED
        assert store.refresh(competitor).state == HOLDING
    finally:
        if competitor is not None:
            store.release(competitor)
        store.release(outer)
    assert store.snapshot(CLASS_HEAVY) == ([], [])


def test_real_slot_store_reports_token_scoped_renewal_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    token = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("d" * 32))
    token.path.unlink()
    _wait_until(token.lease_health.lost.is_set)
    store.release(token)


def test_lease_loss_starts_exact_child_recovery_before_work_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    recovered: list[ExecutionId] = []
    child_id = ExecutionId("b" * 32)
    capacity = HeavyCapacity(
        _managed(store, outer),
        terminal_proof=lambda execution_id: execution_id in recovered,
        recover_child=lambda execution_id: recovered.append(execution_id) is None,
    )
    try:
        with (
            pytest.raises(HeavyCapacityError, match="lease lost"),
            capacity.outer_permit(),
            capacity.child_permit("item", child_id) as permit,
        ):
            permit.token.path.unlink()
            _wait_until(permit.lease_health.lost.is_set)
            _wait_until(lambda: recovered == [child_id])
    finally:
        store.release(outer)


def test_resume_rejects_retirement_with_wrong_terminal_digest(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    item = manifest.document["work_items"][0]
    work_item_id = str(item["work_item_id"])
    attempt_id = "f" * 32
    ordinal, attempt = campaign_store.allocate_attempt_directory(work_item_id, attempt_id)
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    execution_id = ExecutionId("e" * 32)
    registry.prepare(
        execution_id,
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt,
        parent_execution_id="a" * 32,
    )
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=execution_id)
    registry.recover_unretired(slot_store)
    project_retirement = (
        project
        / ".booley_project/.runtime/campaign-child-executions/retired"
        / f"{execution_id}.json"
    )
    campaign_retirement = campaign_store.root / "child-executions/retired" / f"{execution_id}.json"
    document = json.loads(project_retirement.read_text())
    document["execution_terminal_sha256"] = "sha256:" + "9" * 64
    project_retirement.chmod(0o600)
    project_retirement.write_bytes(canonical_json_bytes(document))
    campaign_retirement.unlink()
    with pytest.raises(SimulationCampaignIntegrityError, match="terminal digest"):
        registry.recover_unretired(slot_store)


@pytest.mark.parametrize("defect", ["symlink", "hardlink"])
def test_child_recovery_rejects_linked_entry_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    _project_data, child_id, registry, prepared, _flow = _prepared_child(tmp_path, monkeypatch)
    source = prepared.project_entry.with_name("source.json")
    prepared.project_entry.rename(source)
    if defect == "symlink":
        prepared.project_entry.symlink_to(source.name)
    else:
        prepared.project_entry.hardlink_to(source)

    with pytest.raises(SimulationCampaignIntegrityError, match="link"):
        registry.recover_unretired(None)

    assert not (registry._campaign_root / "retired" / f"{child_id}.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open file")
def test_child_protocol_parses_the_same_bytes_it_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry.json"
    original = canonical_json_bytes({"value": "original"})
    replacement = canonical_json_bytes({"value": "replacement"})
    path.write_bytes(original)
    real_open = child_protocol.open_regular_nofollow

    def open_then_replace(selected: Path) -> int:
        descriptor = real_open(selected)
        selected.rename(selected.with_name("opened-original.json"))
        selected.write_bytes(replacement)
        return descriptor

    monkeypatch.setattr(child_protocol, "open_regular_nofollow", open_then_replace)
    raw, document = child_protocol._read_protocol_record(path, "test entry")

    assert raw == original
    assert document == {"value": "original"}


@pytest.mark.parametrize("defect", ["symlink", "hardlink"])
def test_child_protocol_conflict_read_rejects_links(tmp_path: Path, defect: str) -> None:
    raw = canonical_json_bytes({"value": "expected"})
    source = tmp_path / "source.json"
    source.write_bytes(raw)
    record = tmp_path / "record.json"
    if defect == "symlink":
        record.symlink_to(source.name)
    else:
        record.hardlink_to(source)

    with pytest.raises(SimulationCampaignIntegrityError, match="link"):
        child_protocol._publish_or_verify(record, raw)

    assert source.read_bytes() == raw


@pytest.mark.parametrize(
    "boundary",
    ["campaign-entry", "context", "record", "waiter", "terminal", "campaign-retirement"],
)
def test_child_protocol_crash_prefixes_recover_exactly(tmp_path: Path, boundary: str) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    item = manifest.document["work_items"][0]
    work_item_id = str(item["work_item_id"])
    attempt_id = "f" * 32
    ordinal, attempt = campaign_store.allocate_attempt_directory(work_item_id, attempt_id)
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    execution_id = ExecutionId("e" * 32)
    prepared = registry.prepare(
        execution_id,
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt,
        parent_execution_id="a" * 32,
    )
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    paths = execution_paths(execution_id, project_dir=registry.project_data)
    if boundary == "campaign-entry":
        prepared.campaign_entry.unlink()
    elif boundary == "context":
        paths.context.unlink()
    elif boundary == "record":
        paths.record.unlink()
    elif boundary == "waiter":
        slot_store.submit(CLASS_HEAVY, pid=os.getpid(), execution_id=execution_id)
    elif boundary == "terminal":
        registry.mark_terminal(prepared, "completed")
        slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=execution_id)
    elif boundary == "campaign-retirement":
        registry.recover_unretired(slot_store)
        (campaign_store.root / "child-executions/retired" / f"{execution_id}.json").unlink()
    registry.recover_unretired(slot_store)
    assert registry.is_terminal(execution_id)
    assert prepared.campaign_entry.is_file()
    assert paths.context.is_file()
    assert slot_store.snapshot(CLASS_HEAVY) == ([], [])
    assert (campaign_store.root / "child-executions/retired" / f"{execution_id}.json").is_file()


def test_scheduler_excludes_equal_collision_keys_before_execution(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    active = 0
    maximum_active = 0
    gate = threading.Lock()

    def allocate(item) -> ScheduledAttempt:
        attempt_id = os.urandom(16).hex()
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        return ScheduledAttempt(item, attempt_id, ordinal, directory, "same-run-dir")

    def execute(_attempt, _child_id, _entry_digest) -> None:
        nonlocal active, maximum_active
        with gate:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with gate:
            active -= 1

    capacity = HeavyCapacity(
        _managed(slot_store, outer),
        terminal_proof=registry.is_terminal,
        recover_child=registry.cancel,
    )
    try:
        BoundedCampaignScheduler(capacity, registry, allocate=allocate, execute=execute).run(
            manifest.document["work_items"]
        )
    finally:
        slot_store.release(outer)

    assert maximum_active == 1
    assert slot_store.snapshot(CLASS_HEAVY) == ([], [])


def test_child_context_fails_closed_when_project_entry_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_data, child_id, _registry, _prepared, _flow = _prepared_child(tmp_path, monkeypatch)
    paths = execution_paths(child_id, project_dir=project_data)
    paths.context.unlink()
    assert child_context_matches(paths, child_id) is False


def test_normal_work_runtime_does_not_consume_shutdown_budget(tmp_path: Path) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    admission = AdmissionContext(
        "unmanaged", None, None, 1, "interactive", "a" * 32, 0.01, lambda: False
    )
    capacity = HeavyCapacity(
        admission, terminal_proof=registry.is_terminal, recover_child=registry.cancel
    )

    def allocate(item) -> ScheduledAttempt:
        attempt_id = os.urandom(16).hex()
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        return ScheduledAttempt(item, attempt_id, ordinal, directory, attempt_id)

    started = time.monotonic()
    BoundedCampaignScheduler(
        capacity,
        registry,
        allocate=allocate,
        execute=lambda *_args: time.sleep(0.04),
    ).run(manifest.document["work_items"])
    assert time.monotonic() - started >= 0.07


def test_shutdown_deadline_returns_bounded_but_retains_outer_until_worker_exits(
    tmp_path: Path,
) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    slots = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    outer = slots.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    cancelled = threading.Event()
    release_worker = threading.Event()
    entered = threading.Event()
    admission = AdmissionContext(
        "managed", slots, outer, 1, "interactive", "a" * 32, 0.05, cancelled.is_set
    )
    capacity = HeavyCapacity(
        admission, terminal_proof=registry.is_terminal, recover_child=registry.cancel
    )

    def execute(*_args) -> None:
        entered.set()
        release_worker.wait()

    scheduler = BoundedCampaignScheduler(
        capacity,
        registry,
        allocate=lambda item: _scheduled_attempt(campaign_store, item),
        execute=execute,
    )
    trigger = threading.Thread(target=lambda: (entered.wait(), cancelled.set()))
    trigger.start()
    started = time.monotonic()
    with pytest.raises(CampaignSchedulingError, match="bounded shutdown"):
        scheduler.run(manifest.document["work_items"][:1])
    assert time.monotonic() - started < 0.5
    slots.release(outer)  # endpoint AdmissionGate finally must be deferred
    assert [token.lease_id for token in slots.snapshot(CLASS_HEAVY)[0]] == [outer.lease_id]
    assert campaign_store.scan().interrupted == (
        str(manifest.document["work_items"][0]["work_item_id"]),
    )
    release_worker.set()
    _wait_until(lambda: slots.token_absent(outer))
    trigger.join(timeout=1)


def _scheduled_attempt(store: CampaignStore, item) -> ScheduledAttempt:
    attempt_id = os.urandom(16).hex()
    ordinal, directory = store.allocate_attempt_directory(str(item["work_item_id"]), attempt_id)
    return ScheduledAttempt(item, attempt_id, ordinal, directory, attempt_id)


def test_attempt_is_published_before_real_child_waiter_and_cancel_retires_it(
    tmp_path: Path,
) -> None:
    manifest = _two_item_manifest()
    campaign_store = CampaignStore(tmp_path / "campaign")
    campaign_store.publish_manifest(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    registry = ChildExecutionRegistry(campaign_store, manifest, project)
    slots = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=1))
    outer = slots.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    cancelled = threading.Event()
    admission = AdmissionContext(
        "managed", slots, outer, 2, "interactive", "a" * 32, 1.0, cancelled.is_set
    )
    capacity = HeavyCapacity(
        admission, terminal_proof=registry.is_terminal, recover_child=registry.cancel
    )
    attempts: list[Path] = []

    def allocate(item) -> ScheduledAttempt:
        attempt_id = str(uuid.uuid4())
        ordinal, directory = campaign_store.allocate_attempt_directory(
            str(item["work_item_id"]), attempt_id
        )
        return ScheduledAttempt(item, attempt_id, ordinal, directory, attempt_id)

    def prepare(attempt, child_id, digest) -> None:
        attempts.append(attempt.directory / "attempt.json")
        published = SimulationAttempt(
            {
                "$schema": "booley.simulation-attempt/v1",
                "campaign_id": manifest.document["campaign_id"],
                "manifest_sha256": manifest_digest(manifest),
                "workload_sha256": "sha256:" + "0" * 64,
                "work_item_id": attempt.item["work_item_id"],
                "attempt_id": attempt.attempt_id,
                "attempt_ordinal": attempt.ordinal,
                "producer_invocation_id": 1,
                "build_variant_id": "variant:" + "0" * 64,
                "run_directory": {
                    "kind": "literal",
                    "configured": "run",
                    "resolved": "run",
                    "collision_key": "run",
                    "owned": True,
                },
                "child_execution_id": child_id,
                "child_entry_sha256": digest,
                "pre_sim_build_access": "immutable",
                "policy": {"timeout_seconds": None, "no_kill": False, "diagnostic": False},
                "started_at": "2026-09-22T14:00:00Z",
            }
        )
        (attempt.directory / "attempt.json").write_bytes(published.canonical_bytes())

    errors: list[BaseException] = []
    scheduler = BoundedCampaignScheduler(
        capacity,
        registry,
        allocate=allocate,
        prepare_child=prepare,
        execute=lambda *_args: _wait_until(cancelled.is_set),
    )
    worker = threading.Thread(
        target=lambda: _capture_error(errors, scheduler.run, manifest.document["work_items"])
    )
    worker.start()
    try:
        _cancel_child_and_assert(slots, attempts, campaign_store, cancelled, worker)
    finally:
        slots.release(outer)


def _cancel_child_and_assert(slots, attempts, campaign_store, cancelled, worker) -> None:
    _wait_until(lambda: len(slots.snapshot(CLASS_HEAVY)[1]) == 1)
    assert attempts and attempts[0].is_file()
    assert tuple((campaign_store.root / "child-executions/entries").glob("*.json"))
    cancelled.set()
    worker.join(timeout=4)
    assert not worker.is_alive()
    entries = tuple((campaign_store.root / "child-executions/entries").glob("*.json"))
    retired = tuple((campaign_store.root / "child-executions/retired").glob("*.json"))
    assert len(entries) == len(retired) == 1
    assert slots.snapshot(CLASS_HEAVY)[1] == []


def _capture_error(errors: list[BaseException], call, *args) -> None:
    try:
        call(*args)
    except BaseException as exc:  # noqa: BLE001 -- thread ferry for assertion
        errors.append(exc)


class _ParallelState:
    def __init__(self, build_root, run_log, handle, first_finisher, schedule_seed):
        self.build_root = build_root
        self.run_log = run_log
        self.handle = handle
        self.first_finisher = first_finisher
        self.schedule_seed = schedule_seed
        self.barrier = threading.Barrier(2, timeout=2)
        self.first_finished = threading.Event()
        self.interval_gate = threading.Lock()
        self.intervals: list[tuple[str, float, float]] = []
        self.completion_order: list[str] = []
        self.compile_count = 0


class _ParallelGroup:
    def __init__(self, state: _ParallelState, names: tuple[str, ...]) -> None:
        self.state = state
        self.names = names
        self.build_root = state.build_root
        self.artifact_paths = (state.build_root / "simv",)

    def planning_disclosure(self):
        return {}

    def compile(self):
        self.state.compile_count += 1
        return SimpleNamespace(passed=True)

    def reuse_compilation_from(self, _source) -> None:
        return None

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
        del snapshot_root, run_cwd
        name = self.names[0]
        started = time.monotonic()
        self._coordinate(name)
        finished = time.monotonic()
        with self.state.interval_gate:
            self.state.intervals.append((name, started, finished))
            self.state.completion_order.append(name)
        passed = name != "beta"
        test = SimulationTestOutcome(
            name, "pass" if passed else "fail", passed, str(self.state.run_log)
        )
        return SimulationTargetOutcome(
            "sim",
            self.state.handle.identity,
            "tb",
            "icarus",
            passed,
            "pass" if passed else "fail",
            0.1,
            (test,),
        )

    def _coordinate(self, name: str) -> None:
        if name not in {"alpha", "beta"}:
            return
        self.state.barrier.wait()
        time.sleep(0.001 * self.state.schedule_seed)
        if name == self.state.first_finisher:
            self.state.first_finished.set()
        else:
            assert self.state.first_finished.wait(timeout=2)


class _ParallelExecution:
    def __init__(self, state: _ParallelState) -> None:
        self.state = state

    @contextmanager
    def ordinary_group(self, _handle, names):
        yield _ParallelGroup(self.state, names)


@pytest.mark.parametrize("first_finisher", ["alpha", "beta"])
@pytest.mark.parametrize("schedule_seed", range(5))
def test_managed_campaign_bounds_overlap_keeps_order_and_cleans_child_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_finisher: str,
    schedule_seed: int,
) -> None:
    manifest = _three_item_manifest()
    plan = create_simulation_campaign_plan(manifest)
    project, state = _parallel_environment(tmp_path, first_finisher, schedule_seed)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: state.handle),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _ParallelExecution(state),  # type: ignore[arg-type,return-value]
    )
    outcome, slot_store, invocation = _run_parallel_campaign(
        tmp_path, manifest, plan, project, executor
    )
    _assert_parallel_outcome(outcome, plan, state, first_finisher)
    _assert_child_claim_integrity(invocation, slot_store, plan)


def _parallel_environment(tmp_path, first_finisher, schedule_seed):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"runtime")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )
    return project, _ParallelState(build_root, run_log, handle, first_finisher, schedule_seed)


def _run_parallel_campaign(tmp_path, manifest, plan, project, executor):
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    slot_store = SlotStore(tmp_path / "slots", SlotCaps(max_heavy=2))
    outer = slot_store.acquire(CLASS_HEAVY, pid=os.getpid(), execution_id=ExecutionId("a" * 32))
    try:
        outcome = SimulationCampaign(executor).run(
            NewCampaignRunRequest(
                plan,
                project,
                invocation.parent,
                CampaignPolicy(),
                invocation,
                _managed(slot_store, outer),
            )
        )
        holders, waiters = slot_store.snapshot(CLASS_HEAVY)
        assert [token.execution_id for token in holders] == [ExecutionId("a" * 32)]
        assert waiters == []
    finally:
        slot_store.release(outer)
    return outcome, slot_store, invocation


def _assert_parallel_outcome(outcome, plan, state, first_finisher) -> None:
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert state.compile_count == 1
    by_name = {name: (start, end) for name, start, end in state.intervals}
    assert by_name["alpha"][0] <= by_name["beta"][1]
    assert by_name["beta"][0] <= by_name["alpha"][1]
    assert state.completion_order.index(first_finisher) < state.completion_order.index(
        "beta" if first_finisher == "alpha" else "alpha"
    )


def _assert_child_claim_integrity(invocation, slot_store, plan) -> None:
    store = CampaignStore(invocation / "targets/sim/campaign")
    summary = json.loads(store.summary_path.read_text())
    assert summary["completed"] == list(plan.work_item_ids)
    child_entries = tuple((store.root / "child-executions/entries").glob("*.json"))
    child_retirements = tuple((store.root / "child-executions/retired").glob("*.json"))
    assert child_entries
    assert len(child_retirements) == len(child_entries)
    assert slot_store.snapshot(CLASS_HEAVY) == ([], [])
    child_entry = child_entries[0]
    transplanted = json.loads(child_entry.read_text())
    transplanted["attempt_id"] = "0" * 32
    child_entry.chmod(0o600)
    child_entry.write_bytes(canonical_json_bytes(transplanted))
    with pytest.raises(SimulationCampaignIntegrityError, match="child entry digest disagrees"):
        store.scan()

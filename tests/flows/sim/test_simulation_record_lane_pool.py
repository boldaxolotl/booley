"""The parallel Simulation Campaign path is one bounded pool of lane threads."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.record.capacity import CampaignCapacity, CampaignCapacityError
from booley.flows.sim.record.child_protocol import ChildExecutionRegistry
from booley.flows.sim.record.scheduler import (
    BoundedCampaignScheduler,
    CampaignSchedulingError,
)
from booley.flows.sim.record.store import CampaignStore
from booley.runtime.execution_records import (
    ExecutionId,
    execution_paths,
    read_json,
)
from booley.runtime.job_slots import (
    CLASS_MEMORY,
    QUEUED,
    ROLE_INTERACTIVE,
    ClaimAbortedError,
    SlotRecovery,
    SlotStore,
)
from booley.runtime.supervised_execution import (
    current_supervised_execution,
)
from tests.flows.sim.simulation_record_test_support import (
    _PARALLEL_SYNC_TIMEOUT_S,
    _capacity,
    _holder_claim,
    _item_capacity,
    _lane_registry,
    _managed,
    _named_manifest,
    _outer_claim,
    _scheduled_attempt,
    _wait_until,
)

# Threads owned by other modules and unchanged by the pool: the slot store's lease
# renewal and the supervised scope's per-scope process killer.
_UNRELATED_THREAD_PREFIXES = ("booley-lease-", "booley-record-process-cancellation")


class _Campaign:
    """One real manifest, registry, slot store and held outer token."""

    def __init__(
        self,
        tmp_path: Path,
        names: tuple[str, ...],
        lanes: int,
        *,
        cancel_execution: Callable[[str], None] | None = None,
    ) -> None:
        self.manifest = _named_manifest(names)
        self.store = CampaignStore(tmp_path / "record")
        self.store.publish_manifest(self.manifest)
        project = tmp_path / "project"
        project.mkdir()
        (project / ".booley").mkdir()
        self.registry = ChildExecutionRegistry(self.manifest, project)

        # Like production, the store reads the child records the registry writes, so
        # any reap reclaims a child token once that child is terminal.
        def is_terminal(execution_id: str) -> bool:
            return self.registry.is_terminal(ExecutionId(execution_id))

        terminal = (
            SlotRecovery(execution_is_terminal=is_terminal)
            if cancel_execution is None
            else SlotRecovery(execution_is_terminal=is_terminal, cancel_execution=cancel_execution)
        )
        self.slots = SlotStore(tmp_path / "slots", recovery=terminal)
        self.outer = _outer_claim(self.slots)
        self.lanes = lanes

    def scheduler(
        self, execute, *, timeout_seconds: float, cancellation=lambda: False, prepare_child=None
    ):
        admission = replace(
            _managed(self.slots, self.outer, holders=self.lanes),
            timeout_seconds=timeout_seconds,
            cancellation=cancellation,
        )
        self.capacity = _item_capacity(
            admission, self.manifest, self.registry.project_data, lanes=self.lanes
        )
        return BoundedCampaignScheduler(
            self.capacity,
            allocate=lambda item: _scheduled_attempt(self.store, item),
            execute=execute,
            prepare_child=prepare_child,
        )

    @property
    def items(self):
        return self.manifest.document["work_items"]


def _test_name(attempt) -> str:
    return str(attempt.item["selection"]["names"][0])


def test_parallel_campaign_runs_only_bounded_lane_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _Campaign(tmp_path, ("alpha", "beta", "gamma", "delta"), lanes=3)
    all_lanes_running = threading.Barrier(3, timeout=_PARALLEL_SYNC_TIMEOUT_S)
    arrivals = iter(range(3))
    arrival_gate = threading.Lock()

    def execute(_attempt, _child_id) -> None:
        # The first three items overlap, so every lane is alive at the same time.
        with arrival_gate:
            first_wave = next(arrivals, None) is not None
        if first_wave:
            all_lanes_running.wait()

    started: list[str] = []
    original_start = threading.Thread.start

    def recording_start(self) -> None:
        started.append(self.name)
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", recording_start)
    try:
        campaign.scheduler(execute, timeout_seconds=30.0).run(campaign.items)
    finally:
        campaign.slots.release(campaign.outer)
    pool = sorted(name for name in started if not name.startswith(_UNRELATED_THREAD_PREFIXES))
    assert pool == ["booley-campaign-child-1", "booley-campaign-child-2", "booley-campaign-outer"]


def _delete_own_token(slots: SlotStore, child_id: str) -> None:
    """Make the lease of exactly this child disappear, like a reaper reclaiming it."""
    for token in slots.snapshot(CLASS_MEMORY)[0]:
        if str(token.execution_id) == child_id:
            with slots._lease_gate(token) as acquired:
                assert acquired
                token.path.unlink()


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        pytest.param("completed", "completed", id="completed"),
        # The lane held its slot, so its release-time tree recovery records the cause.
        pytest.param("work_failed", "orphan_recovered", id="work_failed"),
        # The lane never reached a slot: nothing was recovered, so the error is recorded.
        pytest.param("publish_failed", "record_error", id="publish_failed"),
    ],
)
def test_child_lane_terminal_cause(tmp_path: Path, failure: str, cause: str) -> None:
    """A child lane has its identity before its work starts and a terminal record after."""
    campaign = _Campaign(tmp_path, ("alpha",), lanes=1)
    lanes: list[ExecutionId] = []

    def lane_paths(child_id):
        return execution_paths(ExecutionId(child_id), booley_dir=campaign.registry.project_data)

    def publish(_attempt, child_id) -> None:
        lanes.append(child_id)
        assert lane_paths(child_id).context.is_file()
        if failure == "publish_failed":
            raise RuntimeError("publish boom")

    def execute(_attempt, child_id) -> None:
        assert read_json(lane_paths(child_id).record)["state"] == "waiting"
        if failure == "work_failed":
            raise RuntimeError("work boom")

    scheduler = campaign.scheduler(execute, timeout_seconds=30.0, prepare_child=publish)
    try:
        if failure == "completed":
            scheduler.run(campaign.items)
        else:
            with pytest.raises(RuntimeError, match="boom"):
                scheduler.run(campaign.items)
    finally:
        campaign.slots.release(campaign.outer)
    (lane,) = lanes
    record = read_json(lane_paths(lane).record)
    assert (record["state"], record["tree_terminal"]) == ("terminal", True)
    assert record["terminal_cause"] == cause


def test_child_token_left_after_terminal_proof_fails_the_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _Campaign(tmp_path, ("alpha", "beta"), lanes=2)
    child_done = threading.Event()
    # Report every token still present, as if the child's release never landed.
    monkeypatch.setattr(campaign.slots, "token_absent", lambda _token: False)

    def execute(_attempt, child_id) -> None:
        if child_id is None:
            assert child_done.wait(_PARALLEL_SYNC_TIMEOUT_S)
            return
        child_done.set()

    try:
        with pytest.raises(
            CampaignCapacityError, match="child token remained after terminal proof"
        ):
            campaign.scheduler(execute, timeout_seconds=30.0).run(campaign.items)
    finally:
        campaign.slots.release(campaign.outer)


def test_lost_child_lease_interrupts_only_that_lane_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.record.coordinator import raise_if_cancelled_before_publication

    monkeypatch.setattr("booley.runtime.job_slots.LEASE_RENEW_INTERVAL_SECONDS", 0.01)
    campaign = _Campaign(tmp_path, ("alpha", "beta", "gamma"), lanes=2)
    child_interrupted = threading.Event()
    completed: list[str] = []
    other_scope_cancelled: list[bool] = []

    def execute(attempt, child_id) -> None:
        scope = current_supervised_execution()
        assert scope is not None
        if _test_name(attempt) != "beta":
            # The other lane keeps working until beta's lane lost its lease.
            assert child_interrupted.wait(_PARALLEL_SYNC_TIMEOUT_S)
            other_scope_cancelled.append(scope.cancelled())
            completed.append(_test_name(attempt))
            return
        # The Flow's claim and both lanes' item claims.
        _wait_until(lambda: len(campaign.slots.snapshot(CLASS_MEMORY)[0]) == 3)
        _delete_own_token(campaign.slots, child_id)
        _wait_until(scope.cancelled, _PARALLEL_SYNC_TIMEOUT_S)
        # Exact recovery of this child starts while its work is still running.
        _wait_until(
            lambda: campaign.registry.is_terminal(ExecutionId(child_id)), _PARALLEL_SYNC_TIMEOUT_S
        )
        child_interrupted.set()
        raise_if_cancelled_before_publication()
        completed.append(_test_name(attempt))

    scheduler = campaign.scheduler(execute, timeout_seconds=30.0)
    try:
        with pytest.raises(CampaignCapacityError, match="lease lost"):
            scheduler.run(campaign.items)
    finally:
        campaign.slots.release(campaign.outer)
    assert sorted(completed) == ["alpha", "gamma"]
    assert campaign.capacity.cancellation_cause() is None
    assert other_scope_cancelled == [False, False]
    assert campaign.slots.snapshot(CLASS_MEMORY) == ([], [])


# Who reclaims a terminal child's token first: the lane's own hook, or another lane.
_REAP_ORDERINGS = ("hook_first", "other_lane_first")


def _reap_in_order(
    slots: SlotStore,
    unwrapped_reap: Callable[[str], list[str]],
    child_id: ExecutionId,
    ordering: str,
) -> None:
    """Reap the terminal child's token in the requested order and prove it is gone."""
    if ordering == "other_lane_first":
        # Another lane's reap, on its own thread outside this lane's scope.
        other_lane_reaped: list[str] = []
        other_lane = threading.Thread(
            target=lambda: other_lane_reaped.extend(unwrapped_reap(CLASS_MEMORY)),
            daemon=True,  # a hung reap must not keep the worker alive past the assert
        )
        other_lane.start()
        other_lane.join(_PARALLEL_SYNC_TIMEOUT_S)
        assert not other_lane.is_alive()
        assert len(other_lane_reaped) == 1  # exactly this child's token
        assert slots.reap(CLASS_MEMORY) == []  # nothing left for the hook
    else:
        assert len(slots.reap(CLASS_MEMORY)) == 1  # this child's token
    holders, waiters = slots.snapshot(CLASS_MEMORY)
    assert all(token.execution_id != child_id for token in (*holders, *waiters))


def _record_recovery_side_effects(
    slots: SlotStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[ExecutionId], list[str]]:
    """Record child cancels and token-recovery starts; both must stay empty."""
    cancelled: list[ExecutionId] = []
    registry_cancel = ChildExecutionRegistry.cancel

    def recording_cancel(registry, execution_id: ExecutionId) -> bool:
        cancelled.append(execution_id)
        return registry_cancel(registry, execution_id)

    monkeypatch.setattr(ChildExecutionRegistry, "cancel", recording_cancel)
    recoveries: list[str] = []
    begin_recovery = slots.begin_token_recovery

    def recording_begin_recovery(token) -> bool:
        recoveries.append(str(token.execution_id))
        return begin_recovery(token)

    monkeypatch.setattr(slots, "begin_token_recovery", recording_begin_recovery)
    return cancelled, recoveries


def _run_terminal_reap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordering: str) -> None:
    """Run a two-item Campaign whose terminal child tokens are reaped in a forced order.

    The competing reapers are the other lane's slot-store calls (``refresh``,
    ``snapshot`` and ``submit`` all reap the memory class). The ``reap_gate`` makes the
    race deterministic: the hook publishes the terminal record and reaps under the
    gate, so no other lane observes the terminal record before the ordered reap.
    """
    recovery_cancels: list[str] = []
    campaign = _Campaign(
        tmp_path, ("alpha", "beta"), lanes=2, cancel_execution=recovery_cancels.append
    )
    reap_gate = threading.RLock()
    unwrapped_reap = campaign.slots.reap

    def gated_reap(job_class: str) -> list[str]:
        with reap_gate:
            return unwrapped_reap(job_class)

    monkeypatch.setattr(campaign.slots, "reap", gated_reap)
    finished: list[ExecutionId] = []
    publish_terminal = ChildExecutionRegistry.mark_terminal

    def mark_terminal_then_reap(registry, prepared, terminal_cause: str) -> None:
        with reap_gate:
            publish_terminal(registry, prepared, terminal_cause)
            finished.append(prepared.execution_id)
            _reap_in_order(campaign.slots, unwrapped_reap, prepared.execution_id, ordering)

    monkeypatch.setattr(ChildExecutionRegistry, "mark_terminal", mark_terminal_then_reap)
    cancelled, recoveries = _record_recovery_side_effects(campaign.slots, monkeypatch)

    try:
        # A reaped terminal token handled as a lost lease raises LeaseLostError here.
        campaign.scheduler(lambda _attempt, _child_id: None, timeout_seconds=30.0).run(
            campaign.items
        )
    finally:
        campaign.slots.release(campaign.outer)
    assert len(finished) == 2  # each item's own child
    for child_id in finished:
        paths = execution_paths(child_id, booley_dir=campaign.registry.project_data)
        assert read_json(paths.record)["terminal_cause"] == "completed"
        assert not paths.cancel.exists()
    assert (cancelled, recoveries, recovery_cancels) == ([], [], [])
    assert campaign.capacity.cancellation_cause() is None
    assert campaign.slots.snapshot(CLASS_MEMORY) == ([], [])


def test_reaping_a_terminal_child_token_is_not_a_lost_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The terminal child's token may be reclaimed by the lane's own hook or by another
    # lane first; either way it is not a lost lease. Both orders are forced, not raced.
    for ordering in _REAP_ORDERINGS:
        with monkeypatch.context() as ordering_patch:
            try:
                _run_terminal_reap(tmp_path / ordering, ordering_patch, ordering)
            except AssertionError as error:
                error.add_note(f"failing ordering: {ordering}")
                raise


def test_lease_tick_after_terminal_reap_starts_no_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After the reap, a renewal tick marks the reaped lease lost. The await loop must
    # not then cancel the already-terminal child (no stray cancel file).

    campaign = _Campaign(tmp_path, ("alpha",), lanes=1)
    confirmed: list = []
    cancelled: list[ExecutionId] = []
    publish_terminal = ChildExecutionRegistry.mark_terminal

    def mark_terminal_then_reap_and_tick(registry, prepared, terminal_cause: str) -> None:
        publish_terminal(registry, prepared, terminal_cause)
        assert len(campaign.slots.reap(CLASS_MEMORY)) == 1
        (permit,) = confirmed
        assert not campaign.slots.renew(permit.token)  # the tick sees the lease gone
        campaign.capacity.recover_lost_children()

    monkeypatch.setattr(ChildExecutionRegistry, "mark_terminal", mark_terminal_then_reap_and_tick)
    monkeypatch.setattr(
        ChildExecutionRegistry,
        "cancel",
        lambda _registry, execution_id: cancelled.append(execution_id),
    )
    scheduler = campaign.scheduler(lambda _attempt, _child_id: None, timeout_seconds=30.0)
    confirm = CampaignCapacity._confirm_lease

    def recording_confirm(capacity, permit) -> bool:
        confirmed.append(permit)
        return confirm(capacity, permit)

    monkeypatch.setattr(CampaignCapacity, "_confirm_lease", recording_confirm)
    try:
        scheduler.run(campaign.items)
    finally:
        campaign.slots.release(campaign.outer)
    (permit,) = confirmed
    assert cancelled == []
    child_paths = execution_paths(
        permit.child_execution_id, booley_dir=campaign.registry.project_data
    )
    assert not child_paths.cancel.exists()
    assert campaign.slots.snapshot(CLASS_MEMORY) == ([], [])


def test_lane_past_cancel_deadline_holds_outer_slot_within_lanes(tmp_path: Path) -> None:
    campaign = _Campaign(tmp_path, ("alpha", "beta"), lanes=2)
    cancelled = threading.Event()
    child_running = threading.Event()
    release_child = threading.Event()

    holder_counts: list[int] = []

    def execute(attempt, _child_id) -> None:
        if _test_name(attempt) == "alpha":
            assert cancelled.wait(_PARALLEL_SYNC_TIMEOUT_S)
            # Sampled while the other lane is still running past the cancel.
            holder_counts.append(len(campaign.slots.snapshot(CLASS_MEMORY)[0]))
            return
        child_running.set()
        holder_counts.append(len(campaign.slots.snapshot(CLASS_MEMORY)[0]))
        # This lane ignores cancellation until the test releases it.
        assert release_child.wait(_PARALLEL_SYNC_TIMEOUT_S)

    scheduler = campaign.scheduler(execute, timeout_seconds=0.05, cancellation=cancelled.is_set)
    trigger = threading.Thread(
        target=lambda: (child_running.wait(_PARALLEL_SYNC_TIMEOUT_S), cancelled.set()),
        daemon=True,
    )
    trigger.start()
    competitors: list = []
    earlier_threads = set(threading.enumerate())
    try:
        with pytest.raises(CampaignSchedulingError, match="bounded shutdown"):
            scheduler.run(campaign.items)
        campaign.slots.release(campaign.outer)  # the endpoint's admission finally
        # Cancellation already recovered the stuck item's claim, so memory is
        # free for a competitor; the Flow's claim must still be held, which leaves
        # the second competitor queued and the holders within the Flow plus its lanes.
        for ordinal in range(2):
            competitors.append(
                campaign.slots.submit(
                    CLASS_MEMORY,
                    pid=os.getpid(),
                    role=ROLE_INTERACTIVE,
                    execution_id=ExecutionId(str(ordinal + 1) * 32),
                    memory=_holder_claim(1),
                )
            )
        holders, _waiters = campaign.slots.snapshot(CLASS_MEMORY)
        assert campaign.outer.lease_id in {token.lease_id for token in holders}
        holder_counts.append(len(holders))
        assert holder_counts and max(holder_counts) <= campaign.lanes + 1
        assert campaign.slots.refresh(competitors[1]).state == QUEUED
        # The lane that finished leaves; only the stuck lane (and its scope's
        # process killer) remain: retention lives inside the pool, not in a thread.
        _wait_until(lambda: len(_lane_thread_names(earlier_threads)) == 1)
        assert "booley-record-process-cancellation" in _campaign_thread_names(earlier_threads)
    finally:
        release_child.set()
        trigger.join(timeout=_PARALLEL_SYNC_TIMEOUT_S)
    _wait_until(lambda: campaign.slots.token_absent(campaign.outer), _PARALLEL_SYNC_TIMEOUT_S)
    campaign.slots.release(competitors[0])
    _wait_until(
        lambda: campaign.slots.refresh(competitors[1]).state != QUEUED, _PARALLEL_SYNC_TIMEOUT_S
    )
    for competitor in competitors:
        campaign.slots.release(competitor)
    assert campaign.slots.snapshot(CLASS_MEMORY) == ([], [])


def test_failed_lost_lease_recovery_still_holds_outer_slot_until_lanes_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _Campaign(tmp_path, ("alpha", "beta"), lanes=2)
    child_running = threading.Event()
    release_child = threading.Event()

    def execute(_attempt, _child_id) -> None:
        child_running.set()
        assert release_child.wait(_PARALLEL_SYNC_TIMEOUT_S)

    scheduler = campaign.scheduler(execute, timeout_seconds=0.05)

    def failing_recovery() -> None:
        # The owner loop must survive a recovery error (for example a file error).
        if child_running.is_set():
            raise OSError("recovery write failed")

    monkeypatch.setattr(campaign.capacity, "recover_lost_children", failing_recovery)
    try:
        with pytest.raises(CampaignSchedulingError, match="bounded shutdown"):
            scheduler.run(campaign.items)
        campaign.slots.release(campaign.outer)  # the endpoint's admission finally
        holders, _waiters = campaign.slots.snapshot(CLASS_MEMORY)
        assert campaign.outer.lease_id in {token.lease_id for token in holders}
    finally:
        release_child.set()
    _wait_until(lambda: campaign.slots.token_absent(campaign.outer), _PARALLEL_SYNC_TIMEOUT_S)
    assert campaign.slots.snapshot(CLASS_MEMORY) == ([], [])


def _lane_thread_names(earlier_threads: set[threading.Thread]) -> set[str]:
    """Names of lane threads (outer or child) started after ``earlier_threads`` was captured."""
    return {
        name
        for name in _campaign_thread_names(earlier_threads)
        if name.startswith(("booley-campaign-outer", "booley-campaign-child"))
    }


def _campaign_thread_names(earlier_threads: set[threading.Thread]) -> set[str]:
    """Names of campaign threads started after ``earlier_threads`` was captured."""
    return {
        thread.name
        for thread in threading.enumerate()
        if thread not in earlier_threads
        and thread.name.startswith(("booley-campaign-", "booley-record-process-cancellation"))
    }


@pytest.mark.parametrize(
    ("when", "error", "match", "cause"),
    [
        ("before_slot", ClaimAbortedError, "shutdown requested", "record_error"),
        ("in_work", CampaignCapacityError, "plain capacity failure", "orphan_recovered"),
        ("after_work", ClaimAbortedError, "cancellation requested", "completed"),
    ],
)
def test_child_permit_retires_the_lane_on_every_exit(
    tmp_path: Path, when: str, error: type[Exception], match: str, cause: str
) -> None:
    store = SlotStore(tmp_path / "slots")  # 2-holder budget
    outer = _outer_claim(store)
    cancelled = threading.Event()
    admission = replace(_managed(store, outer), cancellation=cancelled.is_set)
    capacity = _capacity(admission, tmp_path)
    lanes: list[ExecutionId] = []

    def publish(child_id: ExecutionId) -> None:
        lanes.append(child_id)
        if when == "before_slot":
            cancelled.set()

    try:
        with (
            capacity.outer_permit(),
            pytest.raises(error, match=match),
            capacity.child_permit(
                "item",
                attempt_id="attempt",
                attempt_ordinal=1,
                attempt_directory=Path("attempt"),
                publish=publish,
                memory=capacity.work_item_claim({}),
            ),
        ):
            if when == "in_work":
                raise CampaignCapacityError("plain capacity failure")
            cancelled.set()
    finally:
        store.release(outer)
    record = read_json(
        execution_paths(lanes[0], booley_dir=_lane_registry(tmp_path).project_data).record
    )
    assert (record["state"], record["terminal_cause"]) == ("terminal", cause)

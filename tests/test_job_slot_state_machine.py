"""Generated sequential histories for the shared Job-slot interface."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    multiple,
    rule,
    run_state_machine_as_test,
)

from booley.runtime import job_slots
from booley.runtime.job_slots import SlotCaps, SlotStore, SlotToken
from booley.runtime.pid import DEAD, REUSED, RUNNING, ProcessIdentity, ProcessObservation

PR_SETTINGS = settings(derandomize=True, max_examples=100, stateful_step_count=25)
EXPLORATORY_SETTINGS = settings(
    derandomize=False,
    max_examples=100,
    stateful_step_count=50,
)

_CLASSES = (job_slots.CLASS_HEAVY, job_slots.CLASS_LIGHT, job_slots.CLASS_TICKET)
_PIDS = (101, 102, 103, 104)
_PRIORITY = {job_slots.ROLE_INTERACTIVE: 0, job_slots.ROLE_TICKET: 1}
_CAPS = SlotCaps(max_heavy=1, max_light=2, max_tickets=2, queue_max=4)


class FakeProcessWorld:
    """Clock and durable identities for simulated process owners."""

    def __init__(self) -> None:
        self.clock = 1_000_000.0
        self.alive: set[int] = set()
        self.generations = dict.fromkeys(_PIDS, 0)

    def start(self, pid: int) -> None:
        if pid not in self.alive:
            self.generations[pid] += 1
            self.alive.add(pid)

    def stop(self, pid: int) -> None:
        self.alive.discard(pid)

    def reuse(self, pid: int) -> None:
        self.generations[pid] += 1
        self.alive.add(pid)

    def is_pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def read_cmdline(self, pid: int) -> list[str] | None:
        if pid not in self.alive:
            return None
        return ["fake-owner", str(pid), str(self.generations[pid])]

    def now(self) -> float:
        return self.clock

    def capture_identity(self, pid: int) -> ProcessIdentity | None:
        if pid not in self.alive:
            return None
        return ProcessIdentity(pid, "fake-pid-namespace", self.generations[pid])

    def observe_identity(self, identity: ProcessIdentity) -> ProcessObservation:
        if identity.pid not in self.alive:
            return ProcessObservation(DEAD)
        if self.generations[identity.pid] != identity.start_ticks:
            return ProcessObservation(REUSED)
        return ProcessObservation(RUNNING)


@dataclass
class ModelLease:
    lease_id: str
    job_class: str
    role: str
    priority: int
    seq: int
    pid: int
    owner_generation: int
    state: str = job_slots.QUEUED
    lease_generation: int = 0
    lease_expires_at: float | None = None


class JobSlotStateMachine(RuleBasedStateMachine):
    """Small ownership and ordering model over the public SlotStore seam."""

    claims = Bundle("claims")

    def __init__(self, parent: Path) -> None:
        super().__init__()
        parent.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self._temporary.name) / "slots"
        self.world = FakeProcessWorld()
        self.stores: dict[int, SlotStore] = {}
        self.leases: dict[str, ModelLease] = {}
        self.world.start(_PIDS[0])
        self.stores[_PIDS[0]] = self._new_store()

    def teardown(self) -> None:
        self._temporary.cleanup()

    def _new_store(self) -> SlotStore:
        return SlotStore(
            self.root,
            _CAPS,
            is_pid_alive=self.world.is_pid_alive,
            read_cmdline=self.world.read_cmdline,
            now=self.world.now,
            sleep=lambda _seconds: None,
            capture_identity=self.world.capture_identity,
            observe_identity=self.world.observe_identity,
            recovery=job_slots.SlotRecovery(recover_process_owner=lambda _identity: True),
        )

    def _reap_class(self, job_class: str) -> None:
        stale = [
            lease_id
            for lease_id, lease in self.leases.items()
            if lease.job_class == job_class and self._is_stale(lease)
        ]
        for lease_id in stale:
            del self.leases[lease_id]

    def _reap_all(self) -> None:
        for job_class in _CLASSES:
            self._reap_class(job_class)

    def _is_stale(self, lease: ModelLease) -> bool:
        owner_is_stale = (
            lease.pid not in self.world.alive
            or self.world.generations[lease.pid] != lease.owner_generation
        )
        lease_expired = (
            lease.state == job_slots.HOLDING
            and lease.lease_expires_at is not None
            and self.world.clock > lease.lease_expires_at
        )
        return owner_is_stale or lease_expired

    def _ordered(self, job_class: str) -> list[ModelLease]:
        entries = [lease for lease in self.leases.values() if lease.job_class == job_class]
        return sorted(
            entries,
            key=lambda lease: (
                0 if lease.state == job_slots.HOLDING else 1,
                lease.priority,
                lease.seq,
                lease.pid,
            ),
        )

    def _matching_lease(self, token: SlotToken) -> ModelLease | None:
        return self.leases.get(token.lease_id)

    @rule(pid=st.sampled_from(_PIDS))
    def make_owner_live(self, pid: int) -> None:
        if pid in self.world.alive:
            return
        self.world.start(pid)
        self.stores[pid] = self._new_store()

    @rule(
        target=claims,
        pid=st.sampled_from(_PIDS),
        job_class=st.sampled_from(_CLASSES),
        role=st.sampled_from((job_slots.ROLE_INTERACTIVE, job_slots.ROLE_TICKET)),
    )
    def submit_claim(self, pid: int, job_class: str, role: str) -> object:
        if pid not in self.world.alive:
            return multiple()
        self._reap_class(job_class)
        class_entries = self._ordered(job_class)
        if len(class_entries) >= _CAPS.cap_for(job_class) + _CAPS.queue_max:
            try:
                self.stores[pid].submit(job_class, pid=pid, role=role)
            except job_slots.QueueFullError:
                return multiple()
            raise AssertionError("SlotStore admitted a claim beyond its bounded queue")

        expected_seq = max((lease.seq for lease in class_entries), default=0) + 1
        token = self.stores[pid].submit(job_class, pid=pid, role=role)
        assert token.seq == expected_seq
        self.leases[token.lease_id] = ModelLease(
            lease_id=token.lease_id,
            job_class=job_class,
            role=role,
            priority=_PRIORITY[role],
            seq=expected_seq,
            pid=pid,
            owner_generation=self.world.generations[pid],
        )
        return token

    @rule(token=claims)
    def refresh_waiter(self, token: SlotToken) -> None:
        lease = self._matching_lease(token)
        if lease is None:
            return
        self._reap_class(lease.job_class)
        lease = self._matching_lease(token)
        actual = self.stores[token.pid].refresh(token)
        if lease is None:
            assert actual.state == job_slots.LOST
            return
        if lease.state == job_slots.HOLDING:
            assert actual.state == job_slots.HOLDING
            return

        ordered = self._ordered(lease.job_class)
        rank = ordered.index(lease)
        cap = _CAPS.cap_for(lease.job_class)
        if rank >= cap:
            assert (actual.state, actual.position) == (job_slots.QUEUED, rank - cap)
            return
        assert actual.state == job_slots.HOLDING
        lease.state = job_slots.HOLDING
        lease.lease_expires_at = self.world.clock + job_slots.LEASE_DURATION_SECONDS

    @rule(token=claims)
    def renew_holder(self, token: SlotToken) -> None:
        lease = self._matching_lease(token)
        expected = lease is not None and lease.state == job_slots.HOLDING
        assert self.stores[token.pid].renew(token) is expected
        if expected:
            lease.lease_generation += 1
            lease.lease_expires_at = self.world.clock + job_slots.LEASE_DURATION_SECONDS

    @rule(token=claims)
    def release_claim(self, token: SlotToken) -> None:
        self.stores[token.pid].release(token)
        lease = self._matching_lease(token)
        if lease is not None:
            del self.leases[lease.lease_id]

    @rule(pid=st.sampled_from(_PIDS))
    def cancel_waiter(self, pid: int) -> None:
        self._reap_all()
        candidate = next(
            (
                lease
                for job_class in _CLASSES
                for lease in self._ordered(job_class)
                if lease.state == job_slots.QUEUED and lease.pid == pid
            ),
            None,
        )
        reader = self._new_store()
        assert reader.cancel_waiter(pid) is (candidate is not None)
        if candidate is not None:
            del self.leases[candidate.lease_id]

    @rule(pid=st.sampled_from(_PIDS), reuse=st.booleans())
    def owner_disappears_or_reuses_pid(self, pid: int, reuse: bool) -> None:
        if pid not in self.world.alive:
            return
        if reuse:
            self.world.reuse(pid)
            self.stores[pid] = self._new_store()
        else:
            self.world.stop(pid)

    @rule(seconds=st.integers(min_value=1, max_value=45))
    def advance_clock(self, seconds: int) -> None:
        self.world.clock += seconds

    @rule(job_class=st.sampled_from(_CLASSES))
    def observe_class(self, job_class: str) -> None:
        self._assert_snapshot(job_class)

    @invariant()
    def snapshots_match_the_model(self) -> None:
        observed: set[str] = set()
        for job_class in _CLASSES:
            class_ids = self._assert_snapshot(job_class)
            assert observed.isdisjoint(class_ids)
            observed.update(class_ids)

    def _assert_snapshot(self, job_class: str) -> set[str]:
        self._reap_class(job_class)
        holders, waiters = self._new_store().snapshot(job_class)
        expected = self._ordered(job_class)
        expected_holders = [lease for lease in expected if lease.state == job_slots.HOLDING]
        expected_waiters = [lease for lease in expected if lease.state == job_slots.QUEUED]
        assert len(holders) <= _CAPS.cap_for(job_class)
        assert len({token.lease_id for token in [*holders, *waiters]}) == len(
            [*holders, *waiters]
        )
        assert self._projection(holders) == self._model_projection(expected_holders)
        assert self._projection(waiters) == self._model_projection(expected_waiters)
        return {token.lease_id for token in [*holders, *waiters]}

    @staticmethod
    def _projection(tokens: list[SlotToken]) -> list[tuple[object, ...]]:
        return [
            (
                token.lease_id,
                token.pid,
                token.role,
                token.priority,
                token.seq,
                token.lease_state,
                token.lease_generation,
            )
            for token in tokens
        ]

    @staticmethod
    def _model_projection(leases: list[ModelLease]) -> list[tuple[object, ...]]:
        return [
            (
                lease.lease_id,
                lease.pid,
                lease.role,
                lease.priority,
                lease.seq,
                job_slots.LEASE_ACTIVE,
                lease.lease_generation,
            )
            for lease in leases
        ]


def test_generated_job_slot_histories(tmp_path: Path) -> None:
    selected = (
        EXPLORATORY_SETTINGS
        if os.environ.get("BOOLEY_HYPOTHESIS_EXPLORATORY") == "1"
        else PR_SETTINGS
    )
    run_state_machine_as_test(lambda: JobSlotStateMachine(tmp_path), settings=selected)

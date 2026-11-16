"""Job slot stores interleaved at filesystem-step granularity, with injected sharing violations.

Three simulated processes share one slots directory. Each runs one store
operation at a time on its own thread, and every filesystem call the store makes
parks that thread until the machine grants the step (or fails it as a Windows
sharing violation, at most three times per thread), so another process can act
between any two steps. Invariants are checked after every step; the failing
schedule is printed step by step.

The sequential machine in ``test_job_slot_state_machine.py`` checks exact queue
order against a model. This one cannot predict an interleaved order and checks
safety instead:

- I1 at most ``_SLOTS`` holders.
- I2 an entry that was seen and then absent never reappears.
- I3 a promotion or lease gate is held by at most one live process.
- I4 every entry belongs to a live owner's token or is recoverable now.
- I5 a renewal that never took the lease gate does not mark an unexpired lease lost.
- S after the example drains, a gate left by a dead or recycled owner does not
  keep a live waiter out of a free slot.

``BOOLEY_HYPOTHESIS_EXPLORATORY=1`` selects the larger exploratory budget (about
seven minutes; pass ``--timeout=600`` to pytest, as the default per-test limit is shorter).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, note, settings, target
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from booley.core.sharing_retry import read_file_bytes
from booley.runtime import job_slots
from booley.runtime.job_slots import MemoryClaim, SlotToken
from booley.runtime.pid import ProcessIdentity
from tests.job_slot_steps import (
    FIRST_ACTOR_PID,
    READ_KINDS,
    Actor,
    ActorKilled,
    FakeProcessWorld,
    StepScheduler,
    Worker,
    stepped_root,
)

# The wall-clock health check and Hypothesis's per-example deadline would make the
# verdict depend on host load; every step here is a thread handoff.
_LOAD_INDEPENDENT = (HealthCheck.too_slow,)
# Hypothesis's targeted search (toward more conflicting steps) pays off only in the larger budget.
_UNTARGETED = (Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink)
PR_SETTINGS = settings(
    derandomize=True,
    phases=_UNTARGETED,
    max_examples=50,
    stateful_step_count=100,
    deadline=None,
    suppress_health_check=_LOAD_INDEPENDENT,
)
EXPLORATORY_SETTINGS = settings(
    derandomize=False,
    max_examples=800,
    stateful_step_count=200,
    deadline=None,
    suppress_health_check=_LOAD_INDEPENDENT,
)

# Equal reservations that fill the budget: exactly two claims hold at once.
_SLOTS = 2
_CLAIM = MemoryClaim(budget_mb=2, reservation_mb=1)
_ROLES = (job_slots.ROLE_INTERACTIVE, job_slots.ROLE_BACKGROUND)
_OPERATIONS = ("submit", "refresh", "refresh", "renew", "renew", "release", "cancel", "reap")
_MAX_TOKENS = 2
_ADVANCES = ("step", "fault", "fault", "run", "run", "finish")
_ACTORS = 3


def _no_sleep(_seconds: float) -> None:
    """A retry sleep that returns at once."""


def _fields(name: str) -> tuple[str, int, str] | None:
    """(state, owner PID, name without its state prefix) of an entry file name."""
    parts = name.removesuffix(".json").split("-")
    if len(parts) != 5 or parts[0] not in ("h", "w") or not parts[3].isdigit():
        return None
    return parts[0], int(parts[3]), "-".join(parts[1:])


def _pid(name: str) -> int:
    fields = _fields(name)
    assert fields is not None
    return fields[1]


def _trace(line: str) -> None:
    with contextlib.suppress(InvalidArgument):  # no Hypothesis test is running (a unit test)
        note(line)


class JobSlotStepMachine(RuleBasedStateMachine):
    """Three processes over one slots directory, interleaved one filesystem step at a time."""

    def __init__(self, parent: Path) -> None:
        super().__init__()
        parent.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(dir=parent)
        base = Path(self._temporary.name) / "slots"
        self.memory_dir = base / job_slots.CLASS_MEMORY
        self.world = FakeProcessWorld()
        self.scheduler = StepScheduler(self.world, trace=_trace)
        root = stepped_root(base, self.scheduler)
        self.actors = [
            Actor(i, self.world, self.scheduler, root, self._recover) for i in range(_ACTORS)
        ]
        self.renewals: list[tuple[Worker, SlotToken, Actor]] = []
        self._seen: set[str] = set()
        self._gone: set[str] = set()
        self.scheduler.checks = [
            self._check_one_holder_per_slot,
            self._check_no_entry_returns,
            self._check_gates,
            self._check_entry_owners,
            self._check_renewals,
        ]

    def teardown(self) -> None:
        with contextlib.suppress(InvalidArgument):  # no Hypothesis test is running (a unit test)
            target(float(self.scheduler.conflicts), label="conflicting steps")
            target(float(self.scheduler.most_judges), label="judges of one stale gate")
            target(float(self.scheduler.replaced_removals), label="removals of a replaced gate")
        try:
            if self.scheduler.violation is None:
                self.scheduler.drain()
                self._settle()
        finally:
            self.scheduler.abort()
            self._temporary.cleanup()

    def _recover(self, caller: Actor, identity: ProcessIdentity | None) -> bool:
        """Recovering an expired holder signals its owner: the owner process ends."""
        victim = next(
            (
                actor
                for actor in self.actors
                if identity is not None
                and not actor.killed
                and actor.pid == identity.pid
                and self.world.generations[actor.pid] == identity.start_ticks
            ),
            None,
        )
        if victim is not None:
            self.scheduler.kill(victim)
            if victim is caller:
                raise ActorKilled
        return True

    # ----------------------------------------------------------- operations

    def start_operation(self, actor: Actor, op: str, role: str, pick: int) -> Worker:
        """Start one store operation of *actor*; it parks at its first filesystem step."""
        holders = [token for token in actor.tokens if token.is_holder]
        if op == "renew" and not holders:
            op = "refresh"
        if op in ("refresh", "release") and not actor.tokens:
            op = "submit"
        if op == "submit" and len(actor.tokens) >= _MAX_TOKENS:
            op = "release"
        pool = holders if op == "renew" else actor.tokens
        token = pool[pick % len(pool)] if pool else None
        operation = self._operation(actor, op, role, pick, token)
        worker = self.scheduler.start(actor, op, operation)
        if token is not None and op == "release":
            actor.releasing[token.lease_id] = worker
        if token is not None and op == "renew":
            self.renewals.append((worker, token, actor))
        return worker

    def _operation(
        self, actor: Actor, op: str, role: str, pick: int, token: SlotToken | None
    ) -> Callable[[], Any]:
        store = actor.store

        def forget() -> None:
            actor.tokens = [held for held in actor.tokens if held is not token]

        def submit() -> None:
            actor.tokens.append(
                store.submit(job_slots.CLASS_MEMORY, pid=actor.pid, role=role, memory=_CLAIM)
            )

        def refresh() -> None:
            assert token is not None
            if store.refresh(token).state == job_slots.LOST:
                forget()

        def release() -> None:
            assert token is not None
            store.release(token)
            forget()

        operations: dict[str, Callable[[], Any]] = {
            "submit": submit,
            "refresh": refresh,
            "renew": lambda: store.renew(token) if token is not None else False,
            "release": release,
            "cancel": lambda: store.cancel_waiter(self._cancel_target(pick)),
            "reap": lambda: store.reap(job_slots.CLASS_MEMORY),
        }
        return operations[op]

    def _cancel_target(self, pick: int) -> int:
        """The PID whose waiter a cancellation aims at: preferably one that has a waiter."""
        queued = [a for a in self.actors if not a.killed and _has_waiter(a)]
        wanted = self.actors[pick % _ACTORS]
        pool = [wanted] if wanted in queued else queued or self.actors
        return pool[pick % len(pool)].pid

    # ---------------------------------------------------------------- rules

    def _idle(self) -> list[Actor]:
        return [actor for actor in self.actors if not actor.killed and not actor.busy]

    def _gate_holders(self) -> list[Actor]:
        held = {actor for gate in self.scheduler.holds.values() for actor in gate}
        return sorted((actor for actor in held if not actor.killed), key=lambda a: a.index)

    def _parked(self, which: int) -> Worker:
        parked = self.scheduler.parked()
        return parked[which % len(parked)]

    @precondition(lambda self: bool(self._idle()))
    @rule(
        who=st.integers(0, 99),
        op=st.sampled_from(_OPERATIONS),
        role=st.sampled_from(_ROLES),
        pick=st.integers(0, 99),
    )
    def begin(self, who: int, op: str, role: str, pick: int) -> None:
        idle = self._idle()
        self.start_operation(idle[who % len(idle)], op, role, pick)

    @precondition(lambda self: bool(self._idle()))
    @rule(
        picks=st.lists(
            st.tuples(st.sampled_from(_OPERATIONS), st.sampled_from(_ROLES), st.integers(0, 99)),
            min_size=_ACTORS,
            max_size=_ACTORS,
        )
    )
    def begin_together(self, picks: list[tuple[str, str, int]]) -> None:
        """Every idle process starts an operation at once, so they overlap from the first step."""
        for actor, (op, role, pick) in zip(self._idle(), picks, strict=False):
            self.start_operation(actor, op, role, pick)

    @precondition(lambda self: bool(self.scheduler.parked()))
    @rule(which=st.integers(0, 99), how=st.sampled_from(_ADVANCES))
    def advance(self, which: int, how: str) -> None:
        """Move one thread on: one step, one step that fails as a sharing violation, steps up
        to its next step that other processes can see, or to the end of its operation."""
        worker = self._parked(which)
        if how == "finish":
            self.scheduler.finish(worker)
            return
        if how == "run":
            self._run_to_visible(worker)
        else:
            self.scheduler.grant(worker, fail=how == "fault")

    def _run_to_visible(self, worker: Worker) -> None:
        self.scheduler.grant(worker)
        while worker.state == "parked" and not _visible(worker):
            self.scheduler.grant(worker)

    @precondition(lambda self: bool(self.scheduler.parked()))
    @rule(turns=st.lists(st.integers(0, 99), min_size=1, max_size=2 * _ACTORS))
    def take_turns(self, turns: list[int]) -> None:
        """Each chosen thread in turn runs to its next step that other processes can see."""
        for which in turns:
            if self.scheduler.parked():
                self._run_to_visible(self._parked(which))

    @precondition(lambda self: any(_has_waiter(a) for a in self._idle()))
    @rule(who=st.integers(0, 99), own=st.sampled_from(("release", "refresh")))
    def gang_up(self, who: int, own: str) -> None:
        """Every other idle process cancels one process's waiter while that process acts on it."""
        victims = [actor for actor in self._idle() if _has_waiter(actor)]
        victim = victims[who % len(victims)]
        for actor in self._idle():
            op = own if actor is victim else "cancel"
            self.start_operation(actor, op, job_slots.ROLE_INTERACTIVE, victim.index)

    @precondition(lambda self: any(not actor.killed for actor in self.actors))
    @rule(who=st.integers(0, 99), recycle=st.booleans())
    def crash(self, who: int, recycle: bool) -> None:
        """A process dies; its PID may start a new process at once (a recycled PID)."""
        live = [actor for actor in self.actors if not actor.killed]
        victim = live[who % len(live)]
        self.scheduler.kill_and_wait(victim)
        if recycle:
            victim.restart()

    @precondition(lambda self: bool(self._gate_holders()))
    @rule(who=st.integers(0, 99), recycle=st.booleans())
    def crash_holding_a_gate(self, who: int, recycle: bool) -> None:
        """A process dies while it holds a gate: the gate stays for others to break."""
        holders = self._gate_holders()
        victim = holders[who % len(holders)]
        self.scheduler.kill_and_wait(victim)
        if recycle:
            victim.restart()

    @precondition(lambda self: any(actor.killed for actor in self.actors))
    @rule(who=st.integers(0, 99))
    def restart(self, who: int) -> None:
        """A dead process's PID starts a new process: a recycled PID with a new identity."""
        dead = [actor for actor in self.actors if actor.killed]
        dead[who % len(dead)].restart()

    @rule(seconds=st.integers(1, 61))
    def advance_clock(self, seconds: int) -> None:
        self.world.clock += seconds

    @invariant()
    def invariants_hold_between_rules(self) -> None:
        self.scheduler.check()

    # ----------------------------------------------------------- invariants

    def _entries(self) -> dict[str, dict[str, Any] | None]:
        """Entry file name -> payload (None when unreadable), read around the scheduler."""
        entries: dict[str, dict[str, Any] | None] = {}
        listed = sorted(self.memory_dir.iterdir()) if self.memory_dir.is_dir() else []
        for path in listed:
            if _fields(path.name) is not None:
                entries[path.name] = self._payload(path)
        return entries

    @staticmethod
    def _payload(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(read_file_bytes(path, sleep=_no_sleep).decode("utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _check_one_holder_per_slot(self) -> None:
        holders = [name for name in self._entries() if name.startswith("h-")]
        assert len(holders) <= _SLOTS, f"I1: {len(holders)} holders for {_SLOTS} slots: {holders}"

    def _check_no_entry_returns(self) -> None:
        present = {fields[2] for name in self._entries() if (fields := _fields(name))}
        self._gone |= self._seen - present
        assert not self._gone & present, f"I2: entries came back: {sorted(self._gone & present)}"
        self._seen |= present

    def _check_gates(self) -> None:
        for gate, holders in self.scheduler.holds.items():
            live = sorted(actor.name for actor in holders if not actor.killed)
            assert len(live) <= 1, f"I3: gate {gate} is held by {live}"

    def _check_entry_owners(self) -> None:
        for name, payload in self._entries().items():
            fields = _fields(name)
            if payload is None or fields is None:
                continue
            owner = self._owner_of(fields[1])
            if self._recoverable(owner, fields[0], payload) or self._in_use(owner, payload):
                continue
            raise AssertionError(f"I4: {name} belongs to no live token of {owner.name}")

    def _owner_of(self, pid: int) -> Actor:
        return self.actors[pid - FIRST_ACTOR_PID]

    def _owner_gone(self, owner: Actor, payload: dict[str, Any]) -> bool:
        identity = ProcessIdentity.from_payload(payload.get("owner_identity"))
        return (
            owner.killed
            or not self.world.is_pid_alive(owner.pid)
            or (identity is not None and identity.start_ticks != self.world.generations[owner.pid])
        )

    def _recoverable(self, owner: Actor, state: str, payload: dict[str, Any]) -> bool:
        if self._owner_gone(owner, payload):
            return True
        expires = job_slots.parse_stamp(payload.get("lease_expires_at"))
        return state == "h" and expires is not None and self.world.clock > expires

    def _in_use(self, owner: Actor, payload: dict[str, Any]) -> bool:
        lease_id = payload.get("lease_id")
        if any(token.lease_id == lease_id for token in owner.tokens):
            return True
        releasing = owner.releasing.get(lease_id)
        if releasing is not None and any(
            w.root is releasing and w.state != "done" for w in self.scheduler.workers
        ):
            return True
        running = owner.operation
        return running is not None and running.state != "done" and running.label == "submit"

    def _check_renewals(self) -> None:
        pending: list[tuple[Worker, SlotToken, Actor]] = []
        for worker, token, actor in self.renewals:
            if worker.state != "done":
                pending.append((worker, token, actor))
                continue
            gate = f".{token.lease_id}.lease.lock"
            lost = token.lease_health.lost.is_set()
            assert not (
                lost and gate not in worker.acquired and not actor.store._lease_expired(token)
            ), (
                f"I5: {actor.name}'s renewal never took the lease gate and marked an "
                "unexpired lease lost"
            )
        self.renewals = pending

    # --------------------------------------------------------------- settle

    def _settle(self) -> None:
        """Two rounds of every live waiter refreshing in rank order, then S."""
        for _round in range(2):
            waiting = [
                (token.path.name, actor, token)
                for actor in self.actors
                if not actor.killed
                for token in actor.tokens
                if not token.is_holder
            ]
            for _name, actor, token in sorted(waiting, key=lambda item: item[0]):
                with contextlib.suppress(ActorKilled):  # recovering its own expired lease
                    actor.store.refresh(token)
        entries = self._entries()
        stuck = [
            name
            for name, payload in entries.items()
            if name.startswith("w-")
            and payload is not None
            and not self._owner_gone(self._owner_of(_pid(name)), payload)
        ]
        holders = [name for name in entries if name.startswith("h-")]
        assert not stuck or len(holders) >= _SLOTS, (
            f"S: live waiters {stuck} are kept out of a free slot ({len(holders)} of {_SLOTS} held)"
        )


def _has_waiter(actor: Actor) -> bool:
    return any(not token.is_holder for token in actor.tokens)


def _visible(worker: Worker) -> bool:
    """Whether the thread's next step changes what other processes can see."""
    assert worker.pending is not None
    kind, name = worker.pending
    return kind not in READ_KINDS and not name.endswith(".tmp")


@pytest.fixture(autouse=True)
def _no_inherited_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """``submit`` links no inherited execution, whatever environment the suite runs in."""
    monkeypatch.delenv(job_slots.RUNTIME_EXECUTION_ENV, raising=False)


@pytest.fixture
def build_machine(tmp_path: Path) -> Any:
    """Builds the machine inside the test body and tears it down, with its threads, afterwards."""
    built: list[JobSlotStepMachine] = []

    def build() -> JobSlotStepMachine:
        built.append(JobSlotStepMachine(tmp_path))
        return built[-1]

    yield build
    for machine in reversed(built):
        machine.teardown()


def _step_until(machine: JobSlotStepMachine, worker: Worker, kind: str, prefix: str) -> None:
    """Grant *worker* steps until the next one is *kind* on a file named *prefix*..."""
    for _ in range(500):
        if (
            worker.pending is not None
            and worker.pending[0] == kind
            and worker.pending[1].startswith(prefix)
        ):
            return
        machine.scheduler.grant(worker)
    raise AssertionError(f"{worker.label} never reached {kind} {prefix}")


def _entry_names(machine: JobSlotStepMachine) -> list[str]:
    return sorted(path.name for path in machine.memory_dir.glob("*.json"))


def test_actors_interleave_between_filesystem_steps(
    build_machine: Callable[[], JobSlotStepMachine],
) -> None:
    machine = build_machine()
    first, second = machine.actors[:2]
    slow = machine.start_operation(first, "submit", job_slots.ROLE_INTERACTIVE, 0)
    # The first submit has read the sequence but not yet published its entry.
    _step_until(machine, slow, "hardlink_to", "w-")
    machine.scheduler.finish(
        machine.start_operation(second, "submit", job_slots.ROLE_INTERACTIVE, 0)
    )
    machine.scheduler.finish(slow)
    names = _entry_names(machine)
    assert len(names) == 2
    assert all("-s0000000001-" in name for name in names)


def test_injected_sharing_violation_is_retried_like_windows(
    build_machine: Callable[[], JobSlotStepMachine],
) -> None:
    machine = build_machine()
    owner, canceller = machine.actors[:2]
    machine.scheduler.finish(
        machine.start_operation(owner, "submit", job_slots.ROLE_INTERACTIVE, 0)
    )
    cancel = machine.start_operation(canceller, "cancel", job_slots.ROLE_INTERACTIVE, owner.index)
    for expected in (1, 2, 3):
        _step_until(machine, cancel, "unlink", "w-")
        machine.scheduler.grant(cancel, fail=True)
        assert cancel.faults == expected
        assert len(_entry_names(machine)) == 1  # the unlink was not performed
    # A thread past its allowance performs the call instead of failing it.
    _step_until(machine, cancel, "unlink", "w-")
    machine.scheduler.grant(cancel, fail=True)
    assert cancel.faults == 3
    assert _entry_names(machine) == []
    machine.scheduler.finish(cancel)
    assert cancel.result is True


def _plant_third_holder(machine: JobSlotStepMachine) -> None:
    machine.memory_dir.mkdir(parents=True, exist_ok=True)
    for seq in range(1, 4):
        name = f"h-p0-s{seq:010d}-{machine.actors[0].pid:010d}-0001.json"
        (machine.memory_dir / name).write_bytes(b"{}")


def _plant_entry_without_token(machine: JobSlotStepMachine) -> None:
    machine.actors[2].store.submit(
        job_slots.CLASS_MEMORY, pid=machine.actors[2].pid, memory=_CLAIM
    )


def test_checks_run_after_every_step(build_machine: Callable[[], JobSlotStepMachine]) -> None:
    for invariant_name, plant in (
        ("I1", _plant_third_holder),
        ("I4", _plant_entry_without_token),
    ):
        machine = build_machine()
        reaper = machine.start_operation(machine.actors[1], "reap", job_slots.ROLE_INTERACTIVE, 0)
        plant(machine)
        with pytest.raises(AssertionError, match=invariant_name):
            machine.scheduler.grant(reaper)


def test_checks_notice_an_entry_that_comes_back(
    build_machine: Callable[[], JobSlotStepMachine],
) -> None:
    machine = build_machine()
    owner, other = machine.actors[:2]
    machine.scheduler.finish(
        machine.start_operation(owner, "submit", job_slots.ROLE_INTERACTIVE, 0)
    )
    reaper = machine.start_operation(other, "reap", job_slots.ROLE_INTERACTIVE, 0)
    (entry,) = machine.memory_dir.glob("*.json")
    content = entry.read_bytes()
    entry.unlink()
    machine.scheduler.grant(reaper)  # the check sees the entry absent
    entry.write_bytes(content)
    with pytest.raises(AssertionError, match="I2"):
        machine.scheduler.grant(reaper)


def test_interleaved_job_slot_schedules(tmp_path: Path) -> None:
    selected = (
        EXPLORATORY_SETTINGS
        if os.environ.get("BOOLEY_HYPOTHESIS_EXPLORATORY") == "1"
        else PR_SETTINGS
    )
    run_state_machine_as_test(lambda: JobSlotStepMachine(tmp_path), settings=selected)

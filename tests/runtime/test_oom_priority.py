"""Job processes are ranked for the OOM killer by admission order (#3760, ADR 0087)."""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
from pathlib import Path

import pytest
from tests.oom_priority_support import FakeProc, oom_priority
from tests.subprocess_budget import run_bounded_child
from tests.test_job_slots import FakeWorld, make_store

import booley
from booley.config.jobs import SlotCaps
from booley.runtime import job_slots

BUDGET_MB = 10_000.0
CLAIM = job_slots.MemoryClaim(budget_mb=BUDGET_MB, reservation_mb=100.0)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
def store(tmp_path: Path, world: FakeWorld) -> job_slots.SlotStore:
    return make_store(tmp_path / "slots", world, SlotCaps(max_light=3))


@pytest.fixture
def proc() -> FakeProc:
    fake = FakeProc()
    fake.add(1, value=0)  # the Developer Agent, at the container default
    return fake


def _admit(
    store: job_slots.SlotStore,
    world: FakeWorld,
    proc: FakeProc,
    pid: int,
    *,
    role: str = job_slots.ROLE_BACKGROUND,
    add_process: bool = True,
) -> tuple[job_slots.SlotToken, int]:
    """Admit a memory Job whose coordinator is *pid*; returns its claim and its one tool."""
    world.alive[pid] = ["coordinator"]
    token = store.acquire(job_slots.CLASS_MEMORY, pid=pid, role=role, memory=CLAIM)
    if not add_process:
        return token, -1
    proc.add(pid, parent=1, value=0)
    return token, proc.add(pid + 1000, parent=pid)


def _rank(store: job_slots.SlotStore, token: job_slots.SlotToken, proc: FakeProc) -> None:
    oom_priority.JobOomRanking(store, token, proc).apply()  # type: ignore[arg-type]


def test_later_admitted_job_ranks_above_the_earlier_and_both_above_the_agent(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    # A background claim is admitted first, then an interactive one: entry-name
    # order (priority first) disagrees with admission order.
    older, older_tool = _admit(store, world, proc, 101, role=job_slots.ROLE_BACKGROUND)
    newer, newer_tool = _admit(store, world, proc, 102, role=job_slots.ROLE_INTERACTIVE)

    _rank(store, older, proc)
    _rank(store, newer, proc)

    assert proc.values[newer_tool] == 1000
    assert proc.values[102] == 1000 - oom_priority.COORDINATOR_GAP
    assert proc.values[older_tool] == 500
    assert proc.values[101] == 500 - oom_priority.COORDINATOR_GAP
    assert proc.values[1] == 0
    assert (
        proc.values[newer_tool]
        > proc.values[102]
        > proc.values[older_tool]
        > proc.values[101]
        > proc.values[1]
    )


def test_ranking_holds_after_many_jobs_start_and_finish(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    live: list[tuple[job_slots.SlotToken, int]] = []
    for pid in range(101, 131):
        live.append(_admit(store, world, proc, pid))
        if len(live) > 3:  # the oldest finishes while the next two still run
            finished, _tool = live.pop(0)
            store.release(finished)
        for token, _tool in live:
            _rank(store, token, proc)
        levels = [proc.values[tool] for _token, tool in live]  # oldest first
        assert levels[-1] == 1000
        assert levels == sorted(levels)
        assert len(set(levels)) == len(levels)
        assert min(levels) > proc.values[1] == 0


class _ItemProcesses:
    """The registered command PIDs of one running work item."""

    def __init__(self, *pids: int) -> None:
        self._pids = pids

    def pids(self) -> tuple[int, ...]:
        return self._pids


def _item_claim(store: job_slots.SlotStore, flow_pid: int) -> job_slots.SlotToken:
    """A Campaign work item's claim: under the Flow's PID, after everything admitted so far."""
    return store.acquire(
        job_slots.CLASS_MEMORY, pid=flow_pid, role=job_slots.ROLE_BACKGROUND, memory=CLAIM
    )


def test_work_items_rank_by_their_own_admission_order(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    flow, flow_tool = _admit(store, world, proc, 101)
    older_item, newer_item = _item_claim(store, 101), _item_claim(store, 101)
    # Each item's adapter process has a simulator child; the Flow has a tool of its own.
    older_root = proc.add(201, parent=101)
    older_sim = proc.add(202, parent=older_root)
    newer_root = proc.add(301, parent=101)
    newer_sim = proc.add(302, parent=newer_root)

    with (
        oom_priority.work_item_processes(older_item.lease_id, _ItemProcesses(older_root)),
        oom_priority.work_item_processes(newer_item.lease_id, _ItemProcesses(newer_root)),
    ):
        _rank(store, flow, proc)

    assert (proc.values[newer_root], proc.values[newer_sim]) == (1000, 1000)
    assert (proc.values[older_root], proc.values[older_sim]) == (500, 500)
    assert proc.values[flow_tool] == 450  # both items are newer than the Flow
    assert proc.values[101] == 450 - oom_priority.COORDINATOR_GAP
    assert proc.values[1] == 0


def test_other_jobs_count_each_campaign_item_as_newer(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    flow, flow_tool = _admit(store, world, proc, 101)
    other, other_tool = _admit(store, world, proc, 102)
    _item_claim(store, 101)
    _item_claim(store, 101)

    _rank(store, flow, proc)
    _rank(store, other, proc)

    assert proc.values[other_tool] == 450  # two items were admitted after it
    assert proc.values[flow_tool] == 400  # the other Job and both items are newer


def test_an_unregistered_item_leaves_its_processes_at_the_flow_level(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    flow, flow_tool = _admit(store, world, proc, 101)
    item = _item_claim(store, 101)
    root = proc.add(201, parent=101)

    _rank(store, flow, proc)  # the item never registered its commands
    assert proc.values[root] == proc.values[flow_tool] == 500

    with oom_priority.work_item_processes(item.lease_id, _ItemProcesses(root)):
        _rank(store, flow, proc)
        assert proc.values[root] == 1000
    # Once it ends the registry forgets it: its processes fall back to the Flow's level.
    proc.values[root] = 0
    _rank(store, flow, proc)
    assert proc.values[root] == 500


def test_a_nested_job_ranks_its_own_tree_and_restores_what_it_inherited(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    outer, outer_tool = _admit(store, world, proc, 101)
    inner, _unused = _admit(store, world, proc, 102, add_process=False)
    _rank(store, outer, proc)
    # A Custom Flow command ran a ``booley`` endpoint: its coordinator forked from
    # the outer Job's tool and inherited that Job's level.
    proc.add(102, parent=outer_tool)
    nested_tool = proc.add(103, parent=102)
    assert proc.values[102] == proc.values[outer_tool] == 500

    with oom_priority.JobOomRanking(store, inner, proc):  # type: ignore[arg-type]
        assert proc.values[nested_tool] == 1000
        assert proc.values[102] == 1000 - oom_priority.COORDINATOR_GAP
        _rank(store, outer, proc)  # the outer Job leaves the nested subtree alone
        assert proc.values[outer_tool] == 500
        assert proc.values[nested_tool] == 1000

    assert proc.values[102] == 500  # back to what it inherited


def test_apply_without_the_claim_writes_nothing(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    token, _tool = _admit(store, world, proc, 101)
    store.release(token)

    _rank(store, token, proc)

    assert proc.writes == []


def test_admission_ordinals_follow_admission_and_are_stored(
    store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
) -> None:
    first, _ = _admit(store, world, proc, 101, role=job_slots.ROLE_BACKGROUND)
    second, _ = _admit(store, world, proc, 102, role=job_slots.ROLE_INTERACTIVE)

    holders, _waiters = store.observe(job_slots.CLASS_MEMORY)

    assert (first.admitted, second.admitted) == (1, 2)
    assert sorted(holder.admitted for holder in holders) == [1, 2]
    store.release(first)
    store.release(second)
    third, _ = _admit(store, world, proc, 103)
    assert third.admitted == 1  # ordinals only order Jobs that overlap


def test_a_slot_claim_has_no_admission_ordinal(
    store: job_slots.SlotStore, world: FakeWorld
) -> None:
    world.alive[101] = ["coordinator"]
    token = store.acquire(job_slots.CLASS_LIGHT, pid=101)
    (holder,), _waiters = store.observe(job_slots.CLASS_LIGHT)
    assert (token.admitted, holder.admitted) == (None, None)


class TestJobOomRankingLifecycle:
    def test_ranks_while_held_and_restores_the_coordinator_after(
        self,
        store: job_slots.SlotStore,
        world: FakeWorld,
        proc: FakeProc,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("booley.runtime.oom_priority.RANK_INTERVAL_SECONDS", 0.01)
        token, tool = _admit(store, world, proc, 101)
        late_tool = -1

        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            assert proc.values[101] == 1000 - oom_priority.COORDINATOR_GAP
            late_tool = proc.add(5000, parent=tool, value=3)
            deadline = time.monotonic() + 30
            while proc.values[late_tool] != 1000 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert proc.values[late_tool] == 1000

        assert proc.values[101] == 0

    def test_a_failed_pass_is_retried(
        self,
        store: job_slots.SlotStore,
        world: FakeWorld,
        proc: FakeProc,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("booley.runtime.oom_priority.RANK_INTERVAL_SECONDS", 0.01)
        token, tool = _admit(store, world, proc, 101)
        real_observe = store.observe
        failures = [OSError("unreadable slots directory")]

        def flaky(job_class: str):
            if failures:
                raise failures.pop()
            return real_observe(job_class)

        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            monkeypatch.setattr(store, "observe", flaky)
            deadline = time.monotonic() + 30
            while failures and time.monotonic() < deadline:
                time.sleep(0.01)  # a pass has hit the failure
            assert failures == []
            proc.values[tool] = 0
            while proc.values[tool] != 1000 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert proc.values[tool] == 1000

    def test_unreadable_slots_leave_the_job_unranked(
        self,
        store: job_slots.SlotStore,
        world: FakeWorld,
        proc: FakeProc,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token, tool = _admit(store, world, proc, 101)

        def unreadable(job_class: str):
            raise OSError("unreadable slots directory")

        monkeypatch.setattr(store, "observe", unreadable)
        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            pass

        assert proc.writes == []
        assert proc.values[tool] == 0

    def test_no_thread_to_be_had_restores_the_coordinator(
        self,
        store: job_slots.SlotStore,
        world: FakeWorld,
        proc: FakeProc,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token, _tool = _admit(store, world, proc, 101)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr("threading.Thread.start", refuse)
        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            assert proc.values[101] == 0

    def test_a_pass_after_the_exit_writes_nothing(
        self, store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
    ) -> None:
        token, tool = _admit(store, world, proc, 101)
        ranking = oom_priority.JobOomRanking(store, token, proc)  # type: ignore[arg-type]
        ranking._stop.set()

        ranking.apply()

        assert proc.writes == []
        assert proc.values[tool] == 0

    def test_a_process_without_proc_is_left_alone(
        self, store: job_slots.SlotStore, world: FakeWorld
    ) -> None:
        world.alive[101] = ["coordinator"]
        token = store.acquire(job_slots.CLASS_MEMORY, pid=101, memory=CLAIM)
        proc = FakeProc()  # no /proc: nothing can be read

        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            pass

        assert proc.writes == []

    def test_a_coordinator_already_at_the_lowest_level_is_left_alone(
        self, store: job_slots.SlotStore, world: FakeWorld, proc: FakeProc
    ) -> None:
        token, tool = _admit(store, world, proc, 101)
        proc.values[101] = oom_priority.LOWEST_COORDINATOR_ADJ

        with oom_priority.JobOomRanking(store, token, proc):  # type: ignore[arg-type]
            pass

        assert proc.writes == []
        assert proc.values[tool] == 0

    def test_only_a_memory_claim_is_ranked(
        self, store: job_slots.SlotStore, world: FakeWorld
    ) -> None:
        world.alive[101] = ["coordinator"]
        holder = store.acquire(job_slots.CLASS_LIGHT, pid=101)
        assert not isinstance(
            oom_priority.job_oom_priority(store, holder), oom_priority.JobOomRanking
        )
        assert not isinstance(
            oom_priority.job_oom_priority(store, None), oom_priority.JobOomRanking
        )
        assert not isinstance(
            oom_priority.job_oom_priority(None, holder), oom_priority.JobOomRanking
        )


class TestProcOom:
    @pytest.mark.skipif(sys.platform != "linux", reason="needs the Linux /proc tree")
    def test_a_process_is_nested_in_its_own_ancestors_only(self) -> None:
        accessor = oom_priority.ProcOom()
        assert accessor.nested_in(os.getpid(), {os.getppid()})
        assert not accessor.nested_in(os.getpid(), {os.getpid()})
        assert not accessor.nested_in(os.getpid(), {2**31 - 2})

    def test_an_absent_process_reads_none_and_refuses_writes(self) -> None:
        absent = 2**31 - 2
        assert oom_priority.ProcOom().read(absent) is None
        assert oom_priority.ProcOom().write(absent, 100) is False

    @pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is a Linux /proc file")
    def test_reads_and_rewrites_its_own_value(self) -> None:
        accessor = oom_priority.ProcOom()
        value = accessor.read(os.getpid())
        assert value is not None
        assert accessor.write(os.getpid(), value)  # writing the current value always works


def test_values_hold_newest_first_and_never_the_agent_at_adr_reference_sizes() -> None:
    """The kernel kills the highest ``1000 x share + oom_score_adj``.

    The shares are the reference sizes of ADR 0087 (Out-of-memory guard and
    requeue): change one here and in the ADR together. Shares are fractions of
    the memory limit (a 32 GiB container: 0.001 is 32 MiB).
    """

    def score(share: float, adj: int) -> float:
        return 1000 * share + adj

    levels = oom_priority.TOOL_ADJ
    assert len(set(levels)) == len(levels)
    assert [oom_priority.tool_adj(newer) for newer in range(len(levels) + 4)] == [
        *levels,
        *[levels[-1]] * 4,
    ]
    # Newest Job against the next older: 0.1 % against 45 %.
    assert score(0.001, levels[0]) > score(0.45, levels[1])
    # Newer against older among the older Jobs: 0.1 % against 4.5 %.
    for newer_level, older_level in itertools.pairwise(levels):
        assert score(0.001, newer_level) > score(0.045, older_level)
    # Never the agent: a 12 % agent against a Job process at 0 % on the lowest level.
    lowest_coordinator = oom_priority.LOWEST_COORDINATOR_ADJ
    assert score(0.0, levels[-1]) > score(0.12, 0)
    assert score(0.0, lowest_coordinator) > score(0.12, 0)
    # A coordinator 2 % above its tool still ranks below it, at every level.
    for level in levels:
        assert score(0.02, level - oom_priority.COORDINATOR_GAP) < score(0.0, level)
    # Every process of a later Job outranks every process of an earlier one at equal sizes.
    for newer_level, older_level in itertools.pairwise(levels):
        assert newer_level - oom_priority.COORDINATOR_GAP > older_level


@pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is a Linux /proc file")
def test_coordinator_ranks_below_the_tool_it_starts_on_linux(tmp_path: Path) -> None:
    """A real process takes a real memory claim and starts ``sleep``: no privilege needed."""
    script = """
import json, os, subprocess, sys, time
from pathlib import Path
from booley.runtime import job_slots, oom_priority

def adj(pid):
    return int(Path(f"/proc/{pid}/oom_score_adj").read_text())

# A sandboxed runner may start above the ranked values, where the ranking stays
# inert by design; lift that guard so the real kernel interplay is still checked.
oom_priority.LOWEST_COORDINATOR_ADJ = 1001
store = job_slots.SlotStore(Path(sys.argv[1]))
token = store.acquire(
    job_slots.CLASS_MEMORY,
    pid=os.getpid(),
    memory=job_slots.MemoryClaim(budget_mb=1000.0, reservation_mb=100.0),
)
start = adj(os.getpid())
with oom_priority.job_oom_priority(store, token):
    tool = subprocess.Popen(["sleep", "60"])
    deadline = time.monotonic() + 15
    while adj(tool.pid) <= adj(os.getpid()) and time.monotonic() < deadline:
        time.sleep(0.05)
    seen = {"coordinator": adj(os.getpid()), "tool": adj(tool.pid)}
    tool.kill()
    tool.wait()
store.release(token)
print(json.dumps({"start": start, "end": adj(os.getpid()), **seen}))
"""
    source_root = str(Path(booley.__file__).resolve().parents[1])
    environment = os.environ | {"PYTHONPATH": source_root}
    completed = run_bounded_child(
        [sys.executable, "-c", script, str(tmp_path / "slots")], env=environment
    )
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout.strip().splitlines()[-1])
    if seen["start"] > 1000 - oom_priority.COORDINATOR_GAP:
        pytest.skip("the runner starts processes above the coordinator level")
    assert seen["tool"] > seen["coordinator"] >= seen["start"]
    assert seen["end"] == seen["start"]

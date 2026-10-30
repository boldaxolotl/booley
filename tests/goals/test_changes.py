"""The append-only Change Log (D15): intent/applied pairs and torn last lines."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.goals.changes import (
    Approval,
    ChangeEntry,
    ChangeKind,
    ChangeLogCorruptError,
    ChangeLogOrderError,
    append_applied,
    append_intent,
    read_change_log,
)
from booley.goals.model import GoalFamily, GoalRecord, GoalSpec, GoalState, WorktreeIdentity
from booley.goals.store import GoalStore, GoalStoreError, RecordLock
from tests.goals.conftest import INPUT_PATHS

GOAL_ID = "fix-uart-20261006T101500Z"
REPOSITORY = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"


@pytest.fixture
def locked(tmp_path: Path) -> Iterator[tuple[RecordLock, Path]]:
    """A created Goal Record whose record lock is held; yields the lock and log path."""
    store = GoalStore(tmp_path)
    identity = WorktreeIdentity(REPOSITORY, "worktrees/uart")
    record = GoalRecord(
        id=GOAL_ID,
        state=GoalState.ENTERING,
        worktree=identity,
        worktree_path="/w",
        branch="goal/x",
        original_ref="refs/heads/main",
        base_sha="a" * 40,
        entered_at="2026-10-06T10:15:00Z",
        input_topology_digest="sha256:" + "e" * 64,
        input_paths=INPUT_PATHS,
    )
    with store.worktree_lock(identity) as wlock:
        store.create(wlock, record)
    with store.record_lock(GOAL_ID) as lock:
        yield lock, lock.record_dir / "changes.jsonl"


def _goal(threshold: int) -> GoalSpec:
    return GoalSpec(
        "synthesis_ok_s", GoalFamily.SYNTH, "s", {"area_um2_max": threshold}, ("feature",)
    )


def _entry(**changes: object) -> ChangeEntry:
    fields: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "at": "2026-10-06T10:20:00Z",
        "kind": ChangeKind.RELAX,
        "goal_key": "synthesis_ok_s",
        "before": _goal(100),
        "after": _goal(120),
        "reason": "area target moved with the new FIFO depth",
        "approval": Approval.ELICITED,
    }
    fields.update(changes)
    return ChangeEntry(**fields)  # type: ignore[arg-type]


def test_missing_log_is_empty(tmp_path: Path) -> None:
    log = read_change_log(tmp_path / "changes.jsonl")
    assert (log.applied, log.interrupted, log.torn_tail) == ((), (), False)


def test_intent_then_applied_is_one_applied_change(locked: tuple[RecordLock, Path]) -> None:
    lock, path = locked
    entry = _entry(session_key="pid:1:2", peer_process="pid:3:4")
    append_intent(lock, entry)
    append_applied(lock, entry.id)

    log = read_change_log(path)
    assert log.applied == (entry,)
    assert log.interrupted == ()
    assert [json.loads(line)["phase"] for line in path.read_text().splitlines()] == [
        "intent",
        "applied",
    ]


def test_intent_without_applied_is_reported_as_interrupted(
    locked: tuple[RecordLock, Path],
) -> None:
    lock, path = locked
    done, pending = _entry(), _entry(kind=ChangeKind.ADD, before=None)
    append_intent(lock, done)
    append_applied(lock, done.id)
    append_intent(lock, pending)

    log = read_change_log(path)
    assert log.applied == (done,)
    assert log.interrupted == (pending,)


@pytest.mark.parametrize("tail", [b'{"id": "x", "pha', b'{"id": "x"}', b"\x00\x00\x00\n"])
def test_torn_last_line_is_ignored_then_truncated_by_the_next_writer(
    locked: tuple[RecordLock, Path], tail: bytes
) -> None:
    lock, path = locked
    first = _entry()
    append_intent(lock, first)
    intact = path.read_bytes()
    path.write_bytes(intact + tail)

    log = read_change_log(path)
    assert log.torn_tail is True
    assert log.interrupted == (first,)

    append_applied(lock, first.id)
    lines = path.read_bytes().splitlines()
    assert path.read_bytes().startswith(intact)
    assert len(lines) == 2
    assert read_change_log(path).applied == (first,)
    assert read_change_log(path).torn_tail is False


def test_corruption_before_the_last_line_is_reported(locked: tuple[RecordLock, Path]) -> None:
    lock, path = locked
    append_intent(lock, _entry())
    path.write_bytes(b"{broken\n" + path.read_bytes())

    with pytest.raises(ChangeLogCorruptError) as caught:
        read_change_log(path)
    assert (caught.value.path, caught.value.line) == (path, 1)
    with pytest.raises(ChangeLogCorruptError):
        append_intent(lock, _entry())  # writers refuse too, never truncate real data


@pytest.mark.parametrize(
    "line",
    [
        {"id": str(uuid.uuid4()), "phase": "applied", "at": "2026-10-06T10:20:00Z"},
        {"id": str(uuid.uuid4()), "phase": "rollback"},
        {"id": "bad-applied-at", "phase": "applied", "at": "now"},
        {"id": "not-a-uuid", "phase": "intent", "entry": {}},
        ["not", "an", "object"],
    ],
)
def test_a_well_formed_line_of_the_wrong_shape_is_corrupt_even_last(
    tmp_path: Path, line: object
) -> None:
    path = tmp_path / "changes.jsonl"
    path.write_text(json.dumps(line) + "\n")
    with pytest.raises(ChangeLogCorruptError):
        read_change_log(path)


def test_writers_enforce_the_intent_then_applied_order(locked: tuple[RecordLock, Path]) -> None:
    lock, _ = locked
    entry = _entry()
    with pytest.raises(ChangeLogOrderError, match="no pending intent"):
        append_applied(lock, entry.id)
    append_intent(lock, entry)
    with pytest.raises(ChangeLogOrderError, match="already in"):
        append_intent(lock, entry)
    append_applied(lock, entry.id)
    with pytest.raises(ChangeLogOrderError, match="no pending intent"):
        append_applied(lock, entry.id)


def test_writers_require_a_held_record_lock(locked: tuple[RecordLock, Path]) -> None:
    lock, _ = locked
    stale = RecordLock(lock.booley_dir, lock.goal_id, lock.file_key, lock.owner)
    stale.held = False
    with pytest.raises(GoalStoreError, match="no longer held"):
        append_intent(stale, _entry())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": ChangeKind.ADD}, "'before' absent"),
        ({"before": None}, "'before' present"),
        ({"reason": "  "}, "reason"),
        ({"approval": Approval.AGENT_RECORDED}, "quoted words"),
        ({"id": "change-1"}, "UUIDv4"),
        ({"at": "2026-10-06 10:20"}, "RFC 3339"),
        ({"at": "2026-10-06T10:20:00+04:00"}, "RFC 3339"),
    ],
)
def test_change_entries_are_validated(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _entry(**changes)


def test_agent_recorded_entry_round_trips_with_its_quote() -> None:
    entry = _entry(approval=Approval.AGENT_RECORDED, quote="yes, relax it to 120")
    assert ChangeEntry.from_json(json.loads(json.dumps(entry.to_json()))) == entry


def test_an_applied_line_with_a_non_canonical_time_is_corrupt(
    locked: tuple[RecordLock, Path],
) -> None:
    lock, path = locked
    entry = _entry()
    append_intent(lock, entry)
    applied = {"id": entry.id, "phase": "applied", "at": "06 OCT 2026"}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(applied) + "\n")
    with pytest.raises(ChangeLogCorruptError, match="RFC 3339"):
        read_change_log(path)


def test_valid_retired_review_history_does_not_brick_current_projection(layout):
    from dataclasses import replace

    from booley.criteria.state import DevelopmentState
    from booley.goals.paths import record_paths
    from booley.goals.recorder import GoalEvidenceRecorder
    from booley.goals.state_store import load_goal_state
    from tests.goals.conftest import bind, enter_goals

    layout.record = enter_goals(
        layout,
        [{"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean", "paths": ["rtl.v"]}],
    )
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    key = layout.record.goals[0].spec.key
    changes = state.set_criterion(key, False, detail={"issues": 1})
    recorder.record_changes(state, changes, invocation_id="review", producer="reviewer")
    # An approved removal retires the key while preserving immutable history.
    current = replace(layout.record, goals=())
    projected = load_goal_state(GoalStore(layout.control), current)
    assert projected.slug == current.id


# Windows CI measured 30.4 s; preserve 3x/30s-rounded headroom (#1202).
@pytest.mark.timeout(120)
def test_source_invalidated_observation_cannot_be_resurrected_by_relaxation(layout):
    from booley.criteria.state import CriterionChange
    from booley.evidence.fields import SOURCE_INVALIDATED_REASON
    from booley.goals.state_store import load_goal_state
    from tests.goals.test_proposals import CYCLE_KEY, approve, proposal, publish, setup_cycle

    env = setup_cycle(layout)
    from booley.criteria.state import DevelopmentState
    from booley.goals.paths import record_paths
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import bind

    publish(layout)
    approve(layout, env, proposal(layout, env, maximum=200).proposal.id)
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    keys = state.reset_category("rtl")
    assert CYCLE_KEY in keys
    (layout.worktree / "rtl.v").write_text("module top; wire changed; endmodule\n")
    recorder.record_changes(
        state,
        [
            CriterionChange(
                key,
                False,
                SOURCE_INVALIDATED_REASON,
                dict(state.criteria[key].detail),
                True,
                dict(state.criteria[key].params),
            )
            for key in keys
        ],
        invocation_id="invalidated",
        producer="coder",
    )
    assert state.criteria[CYCLE_KEY].stale is True
    approve(layout, env, proposal(layout, env, maximum=300).proposal.id)
    current = load_goal_state(env.store, env.store.load(layout.record.id))
    assert current.criteria[CYCLE_KEY].met is False
    assert current.criteria[CYCLE_KEY].detail == {}

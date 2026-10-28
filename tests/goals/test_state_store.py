"""Goal state persistence: fail-closed loads and locked three-way-merge saves (B2, B10)."""

from __future__ import annotations

import json
import multiprocessing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.goals import state_store
from booley.goals.binding import GoalRunBinding, bind_run
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.state_store import (
    GoalStateError,
    GoalStatePersistence,
    load_goal_state,
    merge_states,
)
from booley.goals.store import GoalStore
from tests.goals.conftest import LINT_KEY, SIM_KEY, passing_sim
from tests.sharing_error_support import hold_opens

# Each spawned writer imports Booley afresh (~1-3 s); three steps stay far below this.
_EVENT_TIMEOUT_S = 30


def _bind(layout: SimpleNamespace) -> GoalRunBinding:
    return bind_run(GoalStore(layout.control), layout.worktree, "run-1")


def _state_file(layout: SimpleNamespace) -> Path:
    return record_paths(layout.control, layout.record.id).state_file


def _load(layout: SimpleNamespace, binding: GoalRunBinding | None = None) -> DevelopmentState:
    persistence = GoalStatePersistence(binding or _bind(layout))
    return DevelopmentState.load(_state_file(layout), persistence)


def _publish(layout, state, key, met, detail):
    if key == SIM_KEY:
        detail = {**passing_sim(), **detail}
    changes = state.set_criterion(key, met, detail=detail)
    GoalEvidenceRecorder(_bind(layout)).record_changes(
        state, changes, invocation_id="test", producer="test"
    )


def _disk(layout: SimpleNamespace) -> dict[str, Any]:
    return json.loads(_state_file(layout).read_text(encoding="utf-8"))


def _tools(document: dict[str, Any]) -> list[str]:
    return [entry["mcp_tool"] for entry in document["timeline"]]


# ---------------------------------------------------------------------------
# Merging saves
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_disjoint_keys_from_two_instances_both_land(goal_mode: SimpleNamespace) -> None:
    first, second = _load(goal_mode), _load(goal_mode)
    _publish(goal_mode, first, LINT_KEY, True, {"warnings": 0})
    _publish(goal_mode, second, SIM_KEY, True, {"passed_tests": ["smoke"]})

    first.save()
    second.save()

    criteria = _disk(goal_mode)["criteria"]
    assert criteria[LINT_KEY]["met"] is True
    assert criteria[SIM_KEY]["met"] is True
    assert second.criteria[LINT_KEY].met is True  # the saving instance takes the merge
    assert second.persistence_baseline == state_store.state_snapshot(second)


@pytest.mark.timeout(120)
def test_same_key_conflict_is_won_by_the_later_save(goal_mode: SimpleNamespace) -> None:
    first, second = _load(goal_mode), _load(goal_mode)
    _publish(goal_mode, first, LINT_KEY, True, {"warnings": 0})
    _publish(goal_mode, second, LINT_KEY, False, {"warnings": 4})

    first.save()
    second.save()

    assert _disk(goal_mode)["criteria"][LINT_KEY]["detail"]["warnings"] == 4


def test_stale_scalar_does_not_overwrite_a_newer_write(goal_mode: SimpleNamespace) -> None:
    stale, fresh = _load(goal_mode), _load(goal_mode)
    fresh.work_dir = "/elsewhere"
    fresh.save()

    stale.record_mcp_tool_run("lint", 0)
    stale.save()

    document = _disk(goal_mode)
    assert document["work_dir"] == "/elsewhere"
    assert _tools(document) == ["lint"]
    assert stale.work_dir == "/elsewhere"


def test_identical_timeline_entries_collapse(goal_mode: SimpleNamespace) -> None:
    first, second = _load(goal_mode), _load(goal_mode)
    entry = {"mcp_tool": "lint", "exit_code": 0, "timestamp": "2026-10-07T10:00:00Z"}
    first.timeline.append(dict(entry))
    second.timeline.append(dict(entry))

    first.save()
    second.save()

    assert _disk(goal_mode)["timeline"] == [entry]


@pytest.mark.timeout(120)
def test_shadow_adoption_keeps_a_competing_write(goal_mode: SimpleNamespace) -> None:
    live = _load(goal_mode)
    shadow = deepcopy(live)
    assert shadow.persistence_baseline == live.persistence_baseline
    _publish(goal_mode, shadow, LINT_KEY, True, {"warnings": 0})
    shadow.save()
    live.adopt(shadow)
    competitor = _load(goal_mode)
    _publish(goal_mode, competitor, SIM_KEY, True, {"passed_tests": ["smoke"]})
    competitor.save()

    live.record_mcp_tool_run("lint", 0)
    live.save()

    criteria = _disk(goal_mode)["criteria"]
    assert criteria[LINT_KEY]["met"] is True
    assert criteria[SIM_KEY]["met"] is True


# Windows CI measured 31 s (over half the default budget); keep 3x headroom (#1202).
@pytest.mark.timeout(120)
def test_partial_copy_back_without_adoption_still_merges_by_key(
    goal_mode: SimpleNamespace,
) -> None:
    """A writer that copies some fields back without the baseline loses nothing it did not touch."""
    live = _load(goal_mode)
    shadow = deepcopy(live)
    _publish(goal_mode, shadow, LINT_KEY, True, {"warnings": 0})
    shadow.save()
    live.criteria = shadow.criteria  # field copy, baseline left behind
    competitor = _load(goal_mode)
    _publish(goal_mode, competitor, SIM_KEY, True, {"passed_tests": ["smoke"]})
    competitor.save()

    live.save()

    criteria = _disk(goal_mode)["criteria"]
    assert criteria[LINT_KEY]["met"] is True
    assert criteria[SIM_KEY]["met"] is True


def test_removing_a_goal_is_refused(goal_mode: SimpleNamespace) -> None:
    state = _load(goal_mode)
    before = _state_file(goal_mode).read_bytes()
    del state.criteria[SIM_KEY]

    with pytest.raises(GoalStateError, match="cannot remove Goals"):
        state.save()

    assert _state_file(goal_mode).read_bytes() == before


def test_failed_save_leaves_instance_and_baseline_unchanged(
    goal_mode: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _load(goal_mode)
    baseline = deepcopy(state.persistence_baseline)
    before = _state_file(goal_mode).read_bytes()
    state.set_criterion(LINT_KEY, True, detail={"warnings": 0})

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state_store, "atomic_replace_bytes", fail)
    with pytest.raises(OSError, match="disk full"):
        state.save()

    assert state.persistence_baseline == baseline
    assert state.criteria[LINT_KEY].met is True
    assert _state_file(goal_mode).read_bytes() == before
    monkeypatch.undo()
    state.save()
    assert _disk(goal_mode)["criteria"][LINT_KEY]["met"] is False


def test_merge_keeps_disk_order_and_appends_local_entries() -> None:
    base = _snapshot(timeline=[{"n": 0}], transactions=["a" * 64])
    local = _snapshot(timeline=[{"n": 0}, {"n": 1}], transactions=["a" * 64, "c" * 64])
    disk = _snapshot(timeline=[{"n": 0}, {"n": 2}], transactions=["a" * 64, "b" * 64])

    merged = merge_states(base, local, disk)

    assert merged["timeline"] == [{"n": 0}, {"n": 2}, {"n": 1}]
    assert merged["acceptance_transactions"] == ["a" * 64, "b" * 64, "c" * 64]


def _snapshot(*, timeline: list[dict[str, int]], transactions: list[str]) -> dict[str, Any]:
    snapshot = state_store.state_snapshot(DevelopmentState())
    snapshot["timeline"] = timeline
    snapshot["acceptance_transactions"] = transactions
    return snapshot


# ---------------------------------------------------------------------------
# Two processes interleaving timeline appends (B2's A/B case)
# ---------------------------------------------------------------------------


def _timeline_writer(
    state_file: str, binding: dict[str, Any], tools: list[str], loaded: Any, steps: list[Any]
) -> None:
    """Load once, then append and save one tool run per step, each when released."""
    persistence = GoalStatePersistence(GoalRunBinding.from_json(binding))
    state = DevelopmentState.load(Path(state_file), persistence)
    loaded.set()
    for tool, (go, done) in zip(tools, steps, strict=True):
        if not go.wait(_EVENT_TIMEOUT_S):
            raise TimeoutError(f"{tool} was never released")
        state.record_mcp_tool_run(tool, 0)
        state.save()
        done.set()


@pytest.mark.timeout(90)
def test_two_process_timeline_interleaving_keeps_every_append(
    goal_mode: SimpleNamespace,
) -> None:
    context = multiprocessing.get_context("spawn")
    binding = _bind(goal_mode).to_json()
    state_file = str(_state_file(goal_mode))
    a_steps = [(context.Event(), context.Event()) for _ in range(2)]
    b_steps = [(context.Event(), context.Event())]
    a_loaded, b_loaded = context.Event(), context.Event()
    writers = [
        context.Process(
            target=_timeline_writer, args=(state_file, binding, ["a1", "a2"], a_loaded, a_steps)
        ),
        context.Process(
            target=_timeline_writer, args=(state_file, binding, ["b1"], b_loaded, b_steps)
        ),
    ]
    for writer in writers:
        writer.start()
    try:
        assert a_loaded.wait(_EVENT_TIMEOUT_S) and b_loaded.wait(_EVENT_TIMEOUT_S)
        for go, done in (a_steps[0], b_steps[0], a_steps[1]):
            go.set()
            assert done.wait(_EVENT_TIMEOUT_S)
    finally:
        for writer in writers:
            writer.join(_EVENT_TIMEOUT_S)
            if writer.is_alive():
                writer.kill()
    assert [writer.exitcode for writer in writers] == [0, 0]

    assert _tools(_disk(goal_mode)) == ["a1", "b1", "a2"]


# ---------------------------------------------------------------------------
# Fail-closed loading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"criteria": []}', b'{"criteria": {"lint_clean_top": 1}}'],
)
def test_corrupt_state_file_raises(goal_mode: SimpleNamespace, content: bytes) -> None:
    _state_file(goal_mode).write_bytes(content)

    with pytest.raises(GoalStateError, match="corrupt Goal state"):
        _load(goal_mode)
    with pytest.raises(GoalStateError, match="corrupt Goal state"):
        load_goal_state(GoalStore(goal_mode.control), goal_mode.record)


def test_missing_state_file_loads_the_declared_goals_unmet(goal_mode: SimpleNamespace) -> None:
    _state_file(goal_mode).unlink()

    for state in (
        _load(goal_mode),
        load_goal_state(GoalStore(goal_mode.control), goal_mode.record),
    ):
        assert state.slug == goal_mode.record.id
        assert state.strict_criteria is True
        assert set(state.criteria) == {LINT_KEY, SIM_KEY}
        assert all(not entry.met and not entry.detail for entry in state.criteria.values())
        assert state.criteria[LINT_KEY].params["target"] == "top"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("met", "false"),
        ("mandatory", 1),
        ("ever_met", None),
        ("stale", "no"),
        ("updated_at", 5),
        ("params", []),
        ("detail", "x"),
        ("transition_evidence", {}),
        ("transition_evidence", ["x"]),
    ],
)
def test_malformed_criterion_field_raises(
    goal_mode: SimpleNamespace, field: str, value: object
) -> None:
    document = _disk(goal_mode)
    document["criteria"][LINT_KEY][field] = value
    _state_file(goal_mode).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(GoalStateError, match=f"corrupt Goal state.*{field}"):
        _load(goal_mode)


def test_interrupted_append_recovers_observationally_without_state_write(goal_mode, monkeypatch):
    recorder = GoalEvidenceRecorder(_bind(goal_mode))
    state = _load(goal_mode)
    changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    before = _state_file(goal_mode).read_bytes()

    def interrupted(*args, **kwargs):
        raise OSError("publication interrupted after append")

    monkeypatch.setattr(state_store, "atomic_replace_bytes", interrupted)
    with pytest.raises(OSError, match="publication interrupted"):
        recorder.record_changes(state, changes, invocation_id="crash", producer="lint")
    recovered = load_goal_state(GoalStore(goal_mode.control), goal_mode.record)
    assert recovered.criteria[LINT_KEY].met is True
    assert recovered.criteria[LINT_KEY].detail["warnings"] == 0
    assert _state_file(goal_mode).read_bytes() == before


def test_ledger_corruption_never_falls_back_to_cached_success(goal_mode):
    from booley.criteria.evidence_ledger import AcceptanceLedgerError

    state = _load(goal_mode)
    _publish(goal_mode, state, LINT_KEY, True, {"warnings": 0})
    logs = record_paths(goal_mode.control, goal_mode.record.id).logs_dir
    (row,) = logs.rglob("record.json")
    row.write_bytes(b"{malformed")
    before = _state_file(goal_mode).read_bytes()
    with pytest.raises(AcceptanceLedgerError):
        _load(goal_mode)
    assert _state_file(goal_mode).read_bytes() == before


@pytest.mark.parametrize(
    "count",
    [
        pytest.param(100, id="100"),
        pytest.param(1000, marks=pytest.mark.timeout(120), id="1000"),
    ],
)
def test_projection_scans_validated_history_once_per_read_or_save(goal_mode, monkeypatch, count):
    from time import perf_counter

    from booley.criteria import evidence_ledger
    from booley.criteria.state import CriterionChange
    from booley.goals.recorder import GOAL_SCOPE, goal_identity

    state = _load(goal_mode)
    recorder = GoalEvidenceRecorder(_bind(goal_mode))
    evidence_ledger.record_changes(
        recorder.log_dir,
        state,
        [
            CriterionChange(
                LINT_KEY,
                True,
                "observed",
                {"warnings": 0, "index": index},
                True,
                dict(state.criteria[LINT_KEY].params),
            )
            for index in range(count)
        ],
        scope=GOAL_SCOPE,
        invocation_id="history",
        producer="lint",
        execution_id="",
        identity=goal_identity(goal_mode.record.id, {LINT_KEY: 1}),
    )
    original = evidence_ledger.validated_evidence_records
    calls = []

    def validated(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence_ledger, "validated_evidence_records", validated)
    started = perf_counter()
    state = _load(goal_mode)
    assert len(calls) == 1
    assert state.criteria[LINT_KEY].detail["index"] == count - 1
    state.save()
    assert len(calls) == 2
    print(f"{count} observations: read+save {perf_counter() - started:.4f}s")


def test_baseline_failure_keeps_frozen_pins_and_transition_history_without_success(goal_mode):
    from booley.criteria.evidence_ledger import record_changes
    from booley.criteria.state import CriterionChange
    from booley.goals.recorder import GOAL_SCOPE, goal_identity

    path = _state_file(goal_mode)
    raw = json.loads(path.read_bytes())
    pins = {"target": "top", "from_state": "fail", "_baseline_ref": "frozen-base"}
    raw["criteria"][LINT_KEY]["params"] = pins
    path.write_text(json.dumps(raw))
    state = _load(goal_mode)
    logs = record_paths(goal_mode.control, goal_mode.record.id).logs_dir
    record_changes(
        logs,
        state,
        [CriterionChange(LINT_KEY, False, "baseline failed", {"warnings": 3}, True, pins)],
        scope=GOAL_SCOPE,
        invocation_id="baseline",
        producer="lint",
        execution_id="",
        identity=goal_identity(goal_mode.record.id, {LINT_KEY: 1}),
    )
    recovered = _load(goal_mode)
    entry = recovered.criteria[LINT_KEY]
    assert (entry.met, entry.detail, entry.params) == (False, {}, pins)
    assert entry.ever_failed is True
    assert len(entry.transition_evidence) == 1
    recovered.save()
    assert _load(goal_mode).criteria[LINT_KEY].transition_evidence == entry.transition_evidence


# --- Windows sharing errors (#2187) -------------------------------------------------


def test_goal_state_read_survives_transient_sharing_error(goal_mode, monkeypatch) -> None:
    hold_opens(monkeypatch, {_state_file(goal_mode): 1})
    assert _load(goal_mode).criteria


def test_goal_state_read_retries_before_raising_lasting_sharing_error(
    goal_mode, monkeypatch
) -> None:
    state_file = _state_file(goal_mode)
    attempts = hold_opens(monkeypatch, {state_file: 5})
    with pytest.raises(GoalStateError, match="cannot read Goal state") as caught:
        _load(goal_mode)
    assert caught.value.__cause__.winerror == 32
    assert attempts[state_file] > 1

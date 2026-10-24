"""Goal Mode entry freezes each Goal's Test Runs into the Goal Record (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import fields, make_dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows import test_runs
from booley.goals.entry import GoalEntryError
from booley.goals.model import GoalRecord, GoalRecordFormatError
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore, InvalidRecordUpdateError
from booley.goals.test_run_resolution import SUITE_SLOT
from tests.goals.conftest import SIM_KEY, enter_goals, update_record

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

VARIANTS = (
    b'[top]\ntests = ["smoke", "stress"]\n'
    b'[top.variants.stress]\ndefault = "short"\nshort = ["+n=1"]\nsoak = ["+n=9"]\n'
)


def _names(record: GoalRecord, key: str) -> list[str]:
    resolution = record.test_run_resolution(key)
    assert resolution is not None
    return [run.name for run in resolution.test_runs]


def test_entry_freezes_target_and_test_goals(layout: SimpleNamespace, monkeypatch: Any) -> None:
    monkeypatch.setattr(test_runs.secrets, "SystemRandom", lambda: test_runs.random.Random(7))
    goals = [
        {"family": "LINT", "target": "top"},
        {"family": "SIM", "target": "top"},
        {"family": "SIM", "target": "top", "test": "stress+soak@x3"},
        {"family": "SIM", "target": "top", "test": "stress", "origin": "a"},
        {"family": "SIM", "target": "top", "test": "stress+short@1", "origin": "b"},
        {"family": "SIM", "target": "later"},
        {"family": "SIM", "target": "top", "test": "later"},
    ]
    record = enter_goals(layout, goals, tests_toml=VARIANTS)

    assert _names(record, SIM_KEY) == ["smoke@1", "stress+short@1"]
    seeds = _names(record, "sim_pass_top_run_7374726573732b736f616b_x")
    assert len(set(seeds)) == 3 and all(name.startswith("stress+soak@") for name in seeds)
    # ``stress`` and ``stress+short@1`` resolve to one Test Run: one Goal, both origins.
    merged = next(goal.spec for goal in record.goals if goal.spec.key.endswith("737472657373_d"))
    assert merged.origins == ("a", "b")
    assert _names(record, merged.key) == ["stress+short@1"]
    assert record.test_run_resolution("sim_pass_later").pending == (SUITE_SLOT,)
    assert record.test_run_resolution("sim_pass_top_run_6c61746572_d").pending == ("later",)
    assert "lint_clean_top" not in record.test_run_resolutions
    # The stored record carries the exact frozen runs and loads back to the same value.
    stored = GoalStore(layout.control).load(record.id)
    assert stored.test_run_resolutions == record.test_run_resolutions


@pytest.fixture
def unseeded(monkeypatch: Any) -> None:
    """Make every Target an Icarus one without a ``seed`` template: it applies no seed."""
    from booley.flows import target_test_plan_loading as loading

    def plan(work_dir: Any, target: str) -> Any:
        return loading.load_target_test_plan(work_dir, target, identity=target, adapter="icarus")

    monkeypatch.setattr(loading, "load_checkout_target_test_plan", plan)


@pytest.mark.parametrize("test", ["smoke@7", "smoke@x2"])
def test_entry_refuses_a_seeded_goal_on_a_target_that_cannot_apply_seeds(
    layout: SimpleNamespace, unseeded: None, test: str
) -> None:
    with pytest.raises(GoalEntryError, match="seed"):
        enter_goals(layout, [{"family": "SIM", "target": "top", "test": test}])
    assert GoalStore(layout.control).active_for_worktree(layout.worktree) is None


def test_entry_freezes_runs_without_seeds_on_an_unseeded_target(
    layout: SimpleNamespace, unseeded: None
) -> None:
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    assert _names(record, SIM_KEY) == ["smoke"]


def test_entry_refuses_a_tests_toml_the_target_cannot_apply(layout: SimpleNamespace) -> None:
    with pytest.raises(GoalEntryError, match="Test Runs of Target 'top' cannot be resolved"):
        enter_goals(layout, tests_toml=b'[top]\ntests = ["a+b"]\n')


def test_a_save_cannot_change_or_remove_frozen_test_runs(goal_mode: SimpleNamespace) -> None:
    record = goal_mode.record
    frozen = record.test_run_resolutions[SIM_KEY]
    other = replace(frozen.runs[0], run=test_runs.build_test_run("smoke", seed=5))
    with pytest.raises(InvalidRecordUpdateError, match="cannot change"):
        update_record(goal_mode, test_run_resolutions={SIM_KEY: replace(frozen, runs=(other,))})
    removed = r"the frozen Test Runs of Goal SIM:top \(all selected tests pass\) cannot be removed"
    with pytest.raises(InvalidRecordUpdateError, match=removed) as refused:
        update_record(goal_mode, test_run_resolutions={})
    assert SIM_KEY not in str(refused.value)  # the public Goal name, not its key
    with pytest.raises(InvalidRecordUpdateError, match="cannot change"):
        update_record(goal_mode, test_run_resolutions={SIM_KEY: replace(frozen, runs=())})
    update_record(goal_mode, test_run_resolutions={SIM_KEY: frozen})  # unchanged is fine


def test_record_of_this_version_is_refused_by_a_reader_without_the_field(
    goal_mode: SimpleNamespace,
) -> None:
    """An older Booley knows no ``test_run_resolutions`` and must not drop it on save."""
    raw = json.loads(record_paths(goal_mode.control, goal_mode.record.id).record_file.read_bytes())
    assert "test_run_resolutions" in raw
    older = make_dataclass(
        "OlderRecord",
        [(item.name, Any) for item in fields(GoalRecord) if item.name != "test_run_resolutions"],
    )
    with pytest.raises(GoalRecordFormatError, match="written by a newer Booley"):
        GoalRecord.from_json.__func__(older, raw)


def test_record_written_before_this_field_loads_with_no_resolutions(
    goal_mode: SimpleNamespace,
) -> None:
    raw = goal_mode.record.to_json()
    del raw["test_run_resolutions"]
    loaded = GoalRecord.from_json(raw)
    assert loaded.test_run_resolutions == {}
    assert loaded.test_run_resolution(SIM_KEY) is None


def test_resolution_of_an_unknown_goal_is_refused(goal_mode: SimpleNamespace) -> None:
    raw = goal_mode.record.to_json()
    raw["test_run_resolutions"]["sim_pass_other"] = raw["test_run_resolutions"][SIM_KEY]
    with pytest.raises(GoalRecordFormatError, match="name no Goal"):
        GoalRecord.from_json(raw)


def test_cycle_count_and_coverage_goals_name_test_runs_and_their_criteria_name_tests(
    layout: SimpleNamespace,
) -> None:
    """The Flow reads Tests from the Criterion params; the frozen runs decide what counts."""
    from booley.criteria.templates import cycle_count_criterion_key
    from booley.goals.state_store import load_goal_state
    from booley.goals.status import build_status
    from tests.goals.conftest import run_row
    from tests.goals.test_test_run_evidence import _record

    goals = [
        {
            "family": "CYCLE_COUNT",
            "target": "top",
            "test": "smoke@7",
            "thresholds": {"cycle_count_max": 900},
        },
        {
            "family": "COVERAGE",
            "target": "top",
            "tests": ["smoke@x2", "smoke"],
            "metrics": {"line": 80},
        },
    ]
    record = enter_goals(layout, goals)
    mode = SimpleNamespace(**vars(layout), record=record)
    cycle = cycle_count_criterion_key("top", "smoke@7")  # the key of the written selector
    by_key = {goal.spec.key: goal.spec for goal in record.goals}
    assert by_key[cycle].params["test"] == "smoke@7"
    assert by_key["coverage_top"].params["tests"] == ["smoke@x2", "smoke"]
    assert _names(record, cycle) == ["smoke@7"]
    assert len(_names(record, "coverage_top")) == 3  # two drawn seeds and the default one

    state = load_goal_state(GoalStore(layout.control), record)
    assert state.criteria[cycle].params["test"] == "smoke"
    assert state.criteria["coverage_top"].params["tests"] == ["smoke"]

    def status() -> str:
        view = build_status(GoalStore(layout.control), record, work_dir=layout.worktree)
        return next(goal.status for goal in view.goals if goal.key == cycle)

    # The Target's default seed is another Test Run of the same Test: not evidence.
    cycles = {"cycles": 100, "cycle_observation": "observed", "test": "smoke"}
    detail = _record(mode, "run-1", [run_row("smoke")], key=cycle, extra=cycles)
    assert detail["test_runs"] == [] and status() == "unmet"
    pinned = run_row(test_runs.build_test_run("smoke", seed=7))
    detail = _record(mode, "run-2", [pinned], key=cycle, extra=cycles)
    assert [row["name"] for row in detail["test_runs"]] == ["smoke@7"]
    assert status() == "met"


@pytest.mark.parametrize(
    "goal",
    [
        {"family": "SIM", "target": "top", "test": "stress+soak@x2"},
        {
            "family": "COVERAGE",
            "target": "top",
            "tests": ["smoke", "stress"],
            "metrics": {"line": 90},
        },
    ],
    ids=["sim", "coverage"],
)
def test_entry_refuses_a_goal_on_a_skipped_test(layout: SimpleNamespace, goal: dict) -> None:
    skipping = VARIANTS.replace(b"[top.variants", b'skip = ["stress"]\n[top.variants', 1)
    with pytest.raises(GoalEntryError, match="Test 'stress' of Target 'top' is skipped"):
        enter_goals(layout, [goal], tests_toml=skipping)
    assert GoalStore(layout.control).active_for_worktree(layout.worktree) is None

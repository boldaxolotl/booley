"""Goal evidence by frozen Test Runs: matching, the ledger, and its verification (ADR 0072)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.test_runs import TestRun, build_test_run
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.goals.status import GoalStatus, build_status
from booley.goals.store import GoalStore
from booley.goals.test_run_evidence import (
    LEDGER_DETAIL_KEY,
    build_ledger,
    flow_test_runs,
    fold_test_run_outcomes,
)
from tests.goals.conftest import (
    SIM_KEY,
    bind,
    bump_spec,
    campaign_facts,
    enter_goals,
    run_row,
    write_tests_toml,
)

TWO_TESTS = b'[top]\ntests = ["a", "b"]\n'
A, B = build_test_run("a", seed=1), build_test_run("b", seed=1)
SEEDS_KEY = "sim_pass_top_run_61_x"


class _InterruptedError(Exception):
    """Simulated process death at one publication checkpoint."""


def _interrupt(boundary: str) -> Callable[[str], None]:
    def checkpoint(seen: str) -> None:
        if seen == boundary:
            raise _InterruptedError(seen)

    return checkpoint


@pytest.fixture
def two(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode whose simulation Target Goal is frozen to ``a@1`` and ``b@1``."""
    record = enter_goals(layout, [{"family": "sim", "target": "top"}], tests_toml=TWO_TESTS)
    return SimpleNamespace(**vars(layout), record=record)


def _state_file(layout: SimpleNamespace) -> Any:
    return record_paths(layout.control, layout.record.id).state_file


def _record(
    layout: SimpleNamespace,
    run_id: str,
    rows: list[dict[str, Any]],
    *,
    key: str = SIM_KEY,
    met: bool = True,
) -> dict[str, Any]:
    """Record one V1 observation of *key* with Test Run *rows*; return its stored detail."""
    recorder = GoalEvidenceRecorder(bind(layout, run_id))
    state = DevelopmentState.load(_state_file(layout), recorder.state_persistence())
    changes = state.set_criterion(key, met, detail={"test_runs": rows})
    recorder.record_changes(state, changes, invocation_id="ignored", producer="sim")
    state.save()
    return state.criteria[key].detail


def _transaction(
    layout: SimpleNamespace,
    recorder: GoalEvidenceRecorder,
    campaign: str,
    rows: list[dict[str, Any]],
) -> None:
    """Record or recover one V2 campaign transaction of the simulation Goal."""
    state = DevelopmentState.load(_state_file(layout), recorder.state_persistence())
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(SIM_KEY, True, detail={"test_runs": rows})
    recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=campaign_facts(campaign), identity={}
    )


def _status(layout: SimpleNamespace, key: str = SIM_KEY) -> GoalStatus:
    view = build_status(GoalStore(layout.control), layout.record, work_dir=layout.worktree)
    return next(goal for goal in view.goals if goal.key == key)


def _outcomes(detail: dict[str, Any]) -> dict[str, str]:
    return {run["name"]: run["outcome"] for run in detail[LEDGER_DETAIL_KEY]["runs"]}


def _shown(status: GoalStatus) -> dict[str, str]:
    assert status.test_runs is not None
    return {run.name: run.outcome for run in status.test_runs.runs}


def _edit_rtl(layout: SimpleNamespace, text: str) -> None:
    """Change a dependent file of the Target (bytes: the digest is the same everywhere)."""
    (layout.worktree / "rtl.v").write_bytes(text.encode())


# --- The fold --------------------------------------------------------------------


def test_fold_failed_is_absorbing_and_errors_change_nothing() -> None:
    def fold(*grades: str) -> str:
        rows = [run_row(A, grade) for grade in grades]
        return fold_test_run_outcomes(rows, [A])[0]["outcome"]

    assert fold() == "missing"
    assert fold("pass") == "passed"
    assert fold("fail", "pass", "pass") == "failed"  # a later pass never cancels a failure
    assert fold("pass", "fail") == "failed"
    assert fold("error", "inconclusive") == "missing"
    assert fold("pass", "error") == "passed"
    other_inputs = build_test_run("a", seed=1, args=("+x=1",))
    assert fold_test_run_outcomes([run_row(other_inputs)], [A])[0]["outcome"] == "missing"
    assert fold_test_run_outcomes([run_row(B)], [A])[0]["outcome"] == "missing"
    assert build_ledger([], [run_row(A)], [A, B])["runs"] == [
        {"name": "a@1", "fingerprint": A.fingerprint, "outcome": "passed"},
        {"name": "b@1", "fingerprint": B.fingerprint, "outcome": "missing"},
    ]


# --- Accumulation ----------------------------------------------------------------


def test_partial_runs_accumulate_across_campaigns(two: SimpleNamespace) -> None:
    """A partial run extends the ledger; the other runs stay missing until they pass."""
    first = _record(two, "run-1", [run_row(A)])
    assert _outcomes(first) == {"a@1": "passed", "b@1": "missing"}
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-incomplete: ") and "b@1" in status.reason
    assert _shown(status) == {"a@1": "passed", "b@1": "missing"}

    second = _record(two, "run-2", [run_row(B)])
    assert _outcomes(second) == {"a@1": "passed", "b@1": "passed"}
    # The earlier campaign contributed and is linked by sequence and digest.
    (link,) = second[LEDGER_DETAIL_KEY]["source_evidence"]
    assert link["sequence"] == 1 and len(link["sha256"]) == 64
    assert (_status(two).status, _status(two).reason) == ("met", "")


def test_failure_is_sticky_at_the_same_contents_and_clears_when_they_change(
    two: SimpleNamespace,
) -> None:
    _record(two, "run-1", [run_row(A, "fail"), run_row(B)], met=False)
    after_pass = _record(two, "run-2", [run_row(A), run_row(B)])
    assert _outcomes(after_pass) == {"a@1": "failed", "b@1": "passed"}
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-failed: ") and "a@1" in status.reason
    (failed,) = status.test_runs.failed
    assert failed.rerun == "booley flow sim --target top --test=a@1"

    _edit_rtl(two, "module top; wire fixed; endmodule\n")
    fresh = _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(fresh) == {"a@1": "passed", "b@1": "passed"}
    assert fresh[LEDGER_DETAIL_KEY]["source_evidence"] == []  # other contents: nothing folds
    assert _status(two).status == "met"


def test_a_failing_run_no_goal_names_does_not_block(two: SimpleNamespace) -> None:
    other_seed = build_test_run("a", seed=77)
    detail = _record(two, "run-1", [run_row(A), run_row(B), run_row(other_seed, "fail")])
    assert [row["name"] for row in detail["test_runs"]] == ["a@1", "b@1"]
    assert _status(two).status == "met"


@pytest.mark.parametrize(
    "impostor",
    [
        build_test_run("a", seed=2),  # another seed
        build_test_run("a", seed=1, args=("+easy=1",)),  # other arguments, same name
        build_test_run("a", seed=1, env={"EASY": "1"}),  # another environment, same name
    ],
)
def test_a_pass_with_other_declared_inputs_is_not_evidence(
    two: SimpleNamespace, impostor: TestRun
) -> None:
    detail = _record(two, "run-1", [run_row(impostor), run_row(B)])
    assert _outcomes(detail) == {"a@1": "missing", "b@1": "passed"}
    assert _status(two).status == "unmet"


def test_rows_of_another_spec_revision_are_not_folded(two: SimpleNamespace) -> None:
    _record(two, "run-1", [run_row(A, "fail")], met=False)
    store = GoalStore(two.control)
    record = store.load(two.record.id)
    bump_spec(two, SIM_KEY)  # an approved change: accumulation restarts
    bumped = store.load(two.record.id)
    frozen = record.test_run_resolutions[SIM_KEY]
    with store.record_lock(bumped.id) as lock:
        store.save(
            lock,
            replace(
                bumped,
                test_run_resolutions={SIM_KEY: replace(frozen, spec_revision=bumped.revision)},
            ),
        )
    detail = _record(two, "run-2", [run_row(A), run_row(B)])
    assert _outcomes(detail) == {"a@1": "passed", "b@1": "passed"}
    assert detail[LEDGER_DETAIL_KEY]["source_evidence"] == []


# --- The stale-intent interleaving -----------------------------------------------


def test_recovered_stale_intent_cannot_erase_an_intervening_failure(
    two: SimpleNamespace,
) -> None:
    """Campaign A freezes a passing intent and stops; B records a failure; A is recovered."""
    passing = [run_row(A), run_row(B)]
    binding = bind(two, "run-a")
    stopped = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_record")
    )
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, "11111111-1111-4111-8111-111111111111", passing)

    _record(two, "run-b", [run_row(A, "fail"), run_row(B)], met=False)
    assert _status(two).reason.startswith("goal-test-run-failed")

    # A is recovered at a higher sequence with the ledger it froze before B's failure.
    _transaction(
        two, GoalEvidenceRecorder(binding), "11111111-1111-4111-8111-111111111111", passing
    )
    state = json.loads(_state_file(two).read_text(encoding="utf-8"))
    assert state["criteria"][SIM_KEY]["met"] is True  # the frozen intent says "met"
    assert _outcomes(state["criteria"][SIM_KEY]["detail"]) == {"a@1": "passed", "b@1": "passed"}
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-ledger-outdated: ")
    assert "rerun the Simulation Flow" in status.reason

    # The rerun folds every row, the intervening failure included: it stays sticky.
    rerun = _record(two, "run-c", passing)
    assert _outcomes(rerun) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")


# --- Tampering -------------------------------------------------------------------


def _evidence_file(layout: SimpleNamespace, sequence: int) -> Any:
    root = record_paths(layout.control, layout.record.id).logs_dir / "acceptance" / "evidence"
    (path,) = [item for item in root.iterdir() if item.name.startswith(f"{sequence:09d}")]
    return path / "record.json"


def test_tampered_state_ledger_is_refused(two: SimpleNamespace) -> None:
    _record(two, "run-1", [run_row(A, "fail"), run_row(B)], met=False)
    path = _state_file(two)
    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["criteria"][SIM_KEY]
    entry["met"] = True
    entry["detail"][LEDGER_DETAIL_KEY]["runs"][0]["outcome"] = "passed"
    path.write_text(json.dumps(state), encoding="utf-8")
    status = _status(two)
    assert status.status != "met"
    assert status.evidence_summary == "state conflicts with immutable evidence"


def test_substituted_contributor_is_refused(two: SimpleNamespace) -> None:
    """A contributing observation swapped for a passing one no longer has its linked digest."""
    _record(two, "run-1", [run_row(A)])
    _record(two, "run-2", [run_row(B)])
    assert _status(two).status == "met"
    path = _evidence_file(two, 1)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["detail"]["test_runs"][0]["grade"] = "fail"
    path.write_text(json.dumps(row), encoding="utf-8")
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-ledger-outdated")


# --- Multi-seed Goals ------------------------------------------------------------


@pytest.fixture
def seeds(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode with the multi-seed Goal ``a@x3`` and its three frozen seeds."""
    record = enter_goals(
        layout, [{"family": "sim", "target": "top", "test": "a@x3"}], tests_toml=TWO_TESTS
    )
    frozen = [run.seed for run in record.test_run_resolutions[SEEDS_KEY].test_runs]
    assert len(set(frozen)) == 3
    return SimpleNamespace(**vars(layout), record=record, seeds=frozen)


def _seed(seed: int, grade: str = "pass") -> dict[str, Any]:
    return run_row(build_test_run("a", seed=seed), grade)


def test_multi_seed_goal_accumulates_per_seed_and_lists_failing_seeds(
    seeds: SimpleNamespace,
) -> None:
    first, second, third = seeds.seeds
    frozen = flow_test_runs(seeds.record, {SEEDS_KEY: 1})
    assert frozen.seeds({"top"}, "a", None, 3) == (first, second, third)
    assert frozen.seeds({"top"}, "a", None, 2) is None  # another N is another Goal

    _record(seeds, "run-1", [_seed(first), _seed(second, "fail")], key=SEEDS_KEY, met=False)
    # A seed passes in a second campaign, after the first one ended early.
    detail = _record(seeds, "run-2", [_seed(third), _seed(second)], key=SEEDS_KEY)
    assert list(_outcomes(detail).values()) == ["passed", "failed", "passed"]
    status = _status(seeds, SEEDS_KEY)
    assert status.status == "unmet"
    assert [run.rerun for run in status.test_runs.failed] == [
        f"booley flow sim --target top --test=a@{second}"
    ]
    # A seed outside the frozen set is diagnostic, pass or fail.
    stranger = max(seeds.seeds) + 1
    assert _record(seeds, "run-3", [_seed(stranger, "fail")], key=SEEDS_KEY)["test_runs"] == []

    _edit_rtl(seeds, "module top; wire fixed; endmodule\n")
    _record(seeds, "run-4", [_seed(first), _seed(second)], key=SEEDS_KEY)
    assert _status(seeds, SEEDS_KEY).status == "unmet"  # one seed has not passed at these contents
    _record(seeds, "run-5", [_seed(third)], key=SEEDS_KEY)
    assert _status(seeds, SEEDS_KEY).status == "met"


# --- Pending, empty, missing and changed runs ------------------------------------


def test_pending_and_empty_goals_are_unmet_whatever_passes(layout: SimpleNamespace) -> None:
    goals = [
        {"family": "sim", "target": "top"},
        {"family": "sim", "target": "top", "test": "later"},
        {"family": "sim", "target": "soon"},
    ]
    record = enter_goals(layout, goals, tests_toml=b'[top]\ntests = ["a"]\nskip = ["a"]\n')
    mode = SimpleNamespace(**vars(layout), record=record)
    later = "sim_pass_top_run_6c61746572_d"

    assert _status(mode).reason.startswith("goal-simulation-suite-empty: ")
    assert _status(mode, later).reason.startswith("goal-test-run-pending: ")
    assert "later" in _status(mode, later).reason
    assert _status(mode, "sim_pass_soon").reason.startswith("goal-test-run-pending: ")
    # A pass that claims the Goals cannot meet them: nothing is required of it.
    for key in (SIM_KEY, later):
        detail = _record(mode, f"run-{key}", [run_row(A)], key=key)
        assert detail[LEDGER_DETAIL_KEY]["runs"] == []
        assert _status(mode, key).status == "unmet"


def test_removed_test_and_changed_variant_inputs_need_a_retarget(layout: SimpleNamespace) -> None:
    variants = (
        b'[top]\ntests = ["a", "b"]\n[top.variants.a]\ndefault = "v"\n'
        b'v = { args = ["+n=1"], env = { MODE = "1" } }\n'
    )
    record = enter_goals(layout, [{"family": "sim", "target": "top"}], tests_toml=variants)
    mode = SimpleNamespace(**vars(layout), record=record)
    frozen_a = record.test_run_resolutions[SIM_KEY].test_runs[0]
    assert frozen_a.name == "a+v@1"
    _record(mode, "run-1", [run_row(frozen_a), run_row(B)])
    assert _status(mode).status == "met"

    # The Variant's environment changes: same name, other declared inputs.
    write_tests_toml(mode, variants.replace(b'MODE = "1"', b'MODE = "2"'))
    status = _status(mode)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-changed: ") and "a+v@1" in status.reason

    write_tests_toml(mode, b'[top]\ntests = ["a"]\n')  # Test b and Variant v are gone
    status = _status(mode)
    assert status.reason.startswith("goal-test-run-missing: ")
    assert "a+v@1" in status.reason and "b@1" in status.reason
    assert "retarget" in status.reason


def test_evidence_recorded_before_this_change_does_not_count(goal_mode: SimpleNamespace) -> None:
    """An observation without a ledger (an older Booley wrote it) meets nothing."""
    path = _state_file(goal_mode)
    detail = _record(goal_mode, "run-1", [run_row()])
    assert _status(goal_mode).status == "met"
    # Rewrite the observation and its projection as the previous version stored them.
    old = {key: value for key, value in detail.items() if key != LEDGER_DETAIL_KEY}
    evidence = _evidence_file(goal_mode, 1)
    row = json.loads(evidence.read_text(encoding="utf-8"))
    row["detail"] = old
    evidence.write_text(json.dumps(row), encoding="utf-8")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["criteria"][SIM_KEY]["detail"] = old
    path.write_text(json.dumps(state), encoding="utf-8")
    status = _status(goal_mode)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-ledger-missing: ")


def test_two_publications_fold_one_after_the_other(two: SimpleNamespace) -> None:
    """The record lock orders publications: the second one sees the first one's row."""
    first = GoalEvidenceRecorder(bind(two, "run-1"))
    second = GoalEvidenceRecorder(bind(two, "run-2"))
    states = [
        DevelopmentState.load(_state_file(two), recorder.state_persistence())
        for recorder in (first, second)
    ]  # both loaded before either published
    for recorder, state, rows in (
        (first, states[0], [run_row(A, "fail")]),
        (second, states[1], [run_row(A), run_row(B)]),
    ):
        met = all(row["grade"] == "pass" for row in rows)
        changes = state.set_criterion(SIM_KEY, met, detail={"test_runs": rows})
        recorder.record_changes(state, changes, invocation_id="ignored", producer="sim")
        state.save()
    assert _outcomes(states[1].criteria[SIM_KEY].detail) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")


# --- The review package --------------------------------------------------------------


def _package_inputs(layout: SimpleNamespace) -> tuple[Any, Any, dict[int, dict[str, Any]]]:
    from booley.criteria.evidence_ledger import validated_evidence_records
    from booley.goals.recorder import GOAL_SCOPE
    from booley.goals.state_store import load_goal_state

    store = GoalStore(layout.control)
    record = store.load(layout.record.id)
    state = load_goal_state(store, record)
    logs = record_paths(layout.control, record.id).logs_dir
    rows = validated_evidence_records(GOAL_SCOPE, logs, state, {})
    return record, state, {row["sequence"]: row for row in rows}


def test_accumulated_goal_discloses_its_contributing_campaigns(two: SimpleNamespace) -> None:
    """The review package and generated-input authorization read these observations."""
    from booley.goals.derivation import selected_observations
    from booley.goals.review_package import original_observations, presentation_observations

    _record(two, "run-1", [run_row(A)])
    _record(two, "run-2", [run_row(B)])
    record, state, indexed = _package_inputs(two)
    selected = selected_observations(record, state, two.control)[SIM_KEY]
    assert selected["sequence"] == 2
    assert [row["sequence"] for row in original_observations(selected, indexed)] == [1]
    disclosed = presentation_observations(record, state, two.control)
    assert sorted(row["sequence"] for row in disclosed) == [1, 2]


def test_review_package_rejects_a_substituted_contributor(two: SimpleNamespace) -> None:
    from booley.goals.derivation import selected_observations
    from booley.goals.lifecycle import LifecycleError
    from booley.goals.review_package import original_observations

    _record(two, "run-1", [run_row(A)])
    _record(two, "run-2", [run_row(B)])
    record, state, indexed = _package_inputs(two)
    selected = selected_observations(record, state, two.control)[SIM_KEY]
    forged = {**indexed, 1: {**indexed[1], "invocation_id": "another-campaign"}}
    with pytest.raises(LifecycleError, match="missing or substituted original observation"):
        original_observations(selected, forged)
    with pytest.raises(LifecycleError, match="missing or substituted original observation"):
        original_observations(selected, {2: indexed[2]})


def test_briefing_names_test_runs_and_every_deferred_resolution(two: SimpleNamespace) -> None:
    from booley.goals.test_run_resolving import ensure_test_run_resolutions
    from booley.review.goal_package import (
        _deferred_resolution_lines,  # pyright: ignore[reportPrivateUsage]
        _test_run_lines,  # pyright: ignore[reportPrivateUsage]
    )

    detail = _record(two, "run-1", [run_row(A), run_row(B, "fail")], met=False)
    assert _test_run_lines(detail) == ["  Test Runs: a@1 passed, b@1 failed"]
    assert _test_run_lines({"warnings": 0}) == []
    assert _deferred_resolution_lines(two.record.to_json()) == []
    write_tests_toml(two, b'[top]\ntests = ["a", "b", "late"]\n')
    resolved = ensure_test_run_resolutions(GoalStore(two.control), two.record.id, two.worktree)
    lines = _deferred_resolution_lines(resolved.to_json())
    assert lines[:2] == ["", "Test Runs resolved after entry"]
    assert len(lines) == 3 and lines[2].startswith(f"- {SIM_KEY}: late@1 (slot *, resolved ")


# --- Rendering ---------------------------------------------------------------------


def test_status_shows_frozen_runs_outcomes_and_a_rerun_command_per_failing_seed(
    seeds: SimpleNamespace,
) -> None:
    from booley.goals.format import render_status, test_run_lines

    first, second, third = seeds.seeds
    _record(seeds, "run-1", [_seed(first), _seed(second, "fail")], key=SEEDS_KEY, met=False)
    view = build_status(GoalStore(seeds.control), seeds.record, work_dir=seeds.worktree)
    (goal,) = view.goals
    assert test_run_lines(goal) == [
        f"{SEEDS_KEY}: 3 Test Runs (1 passed, 1 failed, 1 missing): "
        f"a@{first} passed, a@{second} failed, a@{third} missing",
        f"  rerun a@{second}: booley flow sim --target top --test=a@{second}",
    ]
    text = render_status((view,))
    assert "Test Runs\n" in text
    assert f"rerun a@{second}: booley flow sim --target top --test=a@{second}" in text
    assert "goal-test-run-failed" in text


def test_status_lines_name_pending_slots_and_runs_resolved_after_entry(
    layout: SimpleNamespace,
) -> None:
    from booley.goals.format import test_run_lines
    from booley.goals.status import select_status

    goals = [{"family": "sim", "target": "top"}, {"family": "sim", "target": "top", "test": "x"}]
    record = enter_goals(layout, goals, tests_toml=TWO_TESTS)
    mode = SimpleNamespace(**vars(layout), record=record)
    write_tests_toml(mode, b'[top]\ntests = ["a", "b", "late"]\n')
    (view,) = select_status(GoalStore(mode.control), mode.worktree).views
    suite, pending = (test_run_lines(goal) for goal in view.goals)
    assert suite == [
        f"{SIM_KEY}: 3 Test Runs (3 missing): a@1 missing, b@1 missing, late@1 missing",
        "  resolved after entry: late@1",
    ]
    assert pending == [
        "sim_pass_top_run_78_d: 0 Test Runs (none): —",
        "  pending (does not exist yet): x",
    ]
    lint = build_status(GoalStore(mode.control), mode.record, work_dir=mode.worktree)
    assert all(goal.test_runs is not None for goal in lint.goals)


def test_dashboard_goal_detail_lists_the_frozen_test_runs(two: SimpleNamespace) -> None:
    from booley.harness.dashboard.app import (
        _evidence_detail,  # pyright: ignore[reportPrivateUsage]
    )
    from booley.harness.dashboard.model import project_goal

    _record(two, "run-1", [run_row(A), run_row(B, "fail")], met=False)
    view = project_goal(GoalStore(two.control), two.record)
    (detail,) = view.goals
    assert detail.test_runs == (
        f"{SIM_KEY}: 2 Test Runs (1 passed, 1 failed): a@1 passed, b@1 failed",
        "  rerun b@1: booley flow sim --target top --test=b@1",
    )
    assert _evidence_detail(detail).startswith(detail.test_runs[0] + "\n  rerun b@1: ")

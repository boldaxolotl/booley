"""Goal evidence by frozen Test Runs: matching and applicable history (ADR 0072)."""

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
from booley.goals.state_store import load_goal_state
from booley.goals.status import GoalStatus, build_status
from booley.goals.store import GoalStore
from booley.goals.test_run_evidence import (
    flow_test_runs,
    fold_test_run_outcomes,
)
from tests.goals.conftest import (
    SIM_KEY,
    SIM_NAME,
    bind,
    bump_spec,
    campaign_facts,
    enter_goals,
    run_row,
    write_tests_toml,
)

# Each test enters Goal Mode in a real Git layout; Windows needs the headroom (#1202).
pytestmark = pytest.mark.timeout(120)

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
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}], tests_toml=TWO_TESTS)
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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one V1 observation of *key* with Test Run *rows*; return its stored detail."""
    reader = getattr(layout, "campaign_reader", None)
    recorder = GoalEvidenceRecorder(bind(layout, run_id), campaign_reader=reader)
    state = DevelopmentState.load(_state_file(layout), recorder.state_persistence())
    changes = state.set_criterion(key, met, detail={**(extra or {}), "test_runs": rows})
    recorder.record_changes(state, changes, invocation_id="ignored", producer="sim")
    state.save()
    return state.criteria[key].detail


def _replayed(layout: SimpleNamespace, key: str) -> Any:
    """Criterion *key* with its latches rebuilt from the evidence ledger alone.

    The stored ``ever_failed`` flags are cleared first, so what the state reads back
    is what projecting the ledger's rows (every Goal state read) derives.
    """
    stored = DevelopmentState.load(_state_file(layout))
    for entry in stored.criteria.values():
        entry.ever_failed = False
    stored.save()
    return load_goal_state(GoalStore(layout.control), layout.record).criteria[key]


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
    reader = getattr(layout, "campaign_reader", None)
    store = GoalStore(layout.control)
    view = build_status(store, layout.record, work_dir=layout.worktree, campaign_reader=reader)
    return next(goal for goal in view.goals if goal.key == key)


def _outcomes(layout: SimpleNamespace, key: str = SIM_KEY) -> dict[str, str]:
    return _shown(_status(layout, key))


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
    assert fold_test_run_outcomes([run_row(A)], [A, B]) == [
        {"name": "a@1", "fingerprint": A.fingerprint, "outcome": "passed"},
        {"name": "b@1", "fingerprint": B.fingerprint, "outcome": "missing"},
    ]


# --- Accumulation ----------------------------------------------------------------


def test_partial_runs_accumulate_across_campaigns(two: SimpleNamespace) -> None:
    """A partial run extends the ledger; the other runs stay missing until they pass."""
    _record(two, "run-1", [run_row(A)])
    assert _outcomes(two) == {"a@1": "passed", "b@1": "missing"}
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-incomplete: ") and "b@1" in status.reason
    assert _shown(status) == {"a@1": "passed", "b@1": "missing"}

    _record(two, "run-2", [run_row(B)])
    assert _outcomes(two) == {"a@1": "passed", "b@1": "passed"}
    assert _contributors(two) == [1]
    assert (_status(two).status, _status(two).reason) == ("met", "")


def test_failure_is_sticky_at_the_same_contents_and_clears_when_they_change(
    two: SimpleNamespace,
) -> None:
    _record(two, "run-1", [run_row(A, "fail"), run_row(B)], met=False)
    _record(two, "run-2", [run_row(A), run_row(B)])
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-failed: ") and "a@1" in status.reason
    (failed,) = status.test_runs.failed
    assert failed.rerun == "booley flow sim --target top --test=a@1"

    _edit_rtl(two, "module top; wire fixed; endmodule\n")
    _record(two, "run-3", [run_row(A), run_row(B)])
    assert _outcomes(two) == {"a@1": "passed", "b@1": "passed"}
    assert _contributors(two) == []  # other contents: nothing folds
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
    _record(two, "run-1", [run_row(impostor), run_row(B)])
    assert _outcomes(two) == {"a@1": "missing", "b@1": "passed"}
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
    _record(two, "run-2", [run_row(A), run_row(B)])
    assert _outcomes(two) == {"a@1": "passed", "b@1": "passed"}
    assert _contributors(two) == []


# --- The stale-intent interleaving -----------------------------------------------


def test_stopped_transaction_is_completed_before_the_next_evidence_is_recorded(
    two: SimpleNamespace,
) -> None:
    """Campaign A freezes a passing intent and stops; B records a failure; A is recovered."""
    passing = [run_row(A), run_row(B)]
    campaign = "11111111-1111-4111-8111-111111111111"
    binding = bind(two, "run-a")
    stopped = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_record")
    )
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, campaign, passing)

    # B's recorder completes A's transaction first, so B's failure folds after it.
    _record(two, "run-b", [run_row(A, "fail"), run_row(B)], met=False)
    assert len(_contributors(two)) == 1
    assert _status(two).reason.startswith("goal-test-run-failed")

    # Recovering A now only verifies it: the failure stays the selected observation.
    _transaction(two, GoalEvidenceRecorder(binding), campaign, passing)
    assert _status(two).reason.startswith("goal-test-run-failed")
    _record(two, "run-c", passing)
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")


@pytest.mark.timeout(150)
def test_recovered_stale_intent_cannot_erase_an_intervening_failure(
    two: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-time check alone: a writer that did not complete A before recording B.

    Recorders account for earlier transactions before they record, so this
    interleaving needs that step switched off; status must still fold the intervening failure.
    """
    from booley.goals import campaign_accounting

    monkeypatch.setattr(campaign_accounting, "account_for_campaigns", lambda *_a, **_k: None)
    passing = [run_row(A), run_row(B)]
    binding = bind(two, "run-a")
    stopped = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_record")
    )
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, "11111111-1111-4111-8111-111111111111", passing)

    _record(two, "run-b", [run_row(A, "fail"), run_row(B)], met=False)
    assert _status(two).reason.startswith("goal-test-run-failed")

    # A is recovered at a higher sequence with its passing rows frozen before B's failure.
    _transaction(
        two, GoalEvidenceRecorder(binding), "11111111-1111-4111-8111-111111111111", passing
    )
    state = json.loads(_state_file(two).read_text(encoding="utf-8"))
    assert state["criteria"][SIM_KEY]["met"] is True  # the frozen intent says "met"
    status = _status(two)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-failed: ")
    assert "a@1" in status.reason

    # The rerun folds every row, the intervening failure included: it stays sticky.
    _record(two, "run-c", passing, extra={"test_run_outcomes_only": True})
    assert not DevelopmentState.load(_state_file(two)).criteria[SIM_KEY].met
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
    assert _status(two).reason.startswith("goal-test-run-failed")


# --- Tampering -------------------------------------------------------------------


def _evidence_file(layout: SimpleNamespace, sequence: int) -> Any:
    root = record_paths(layout.control, layout.record.id).logs_dir / "acceptance" / "evidence"
    (path,) = [item for item in root.iterdir() if item.name.startswith(f"{sequence:09d}")]
    return path / "record.json"


def _substitute_contributor(two: SimpleNamespace) -> None:
    from booley.criteria.evidence_ledger import canonical_json

    _transaction(
        two,
        GoalEvidenceRecorder(bind(two, "run-1")),
        "11111111-1111-4111-8111-111111111111",
        [run_row(A, "fail")],
    )
    _record(two, "run-2", [run_row(B)])
    assert _status(two).reason.startswith("goal-test-run-failed")
    path = _evidence_file(two, 1)
    row = json.loads(path.read_bytes())
    row["detail"]["test_runs"][0]["grade"] = "pass"
    path.write_bytes(canonical_json(row) + b"\n")


def test_substituted_contributor_is_refused(two: SimpleNamespace) -> None:
    from booley.criteria.evidence_ledger import AcceptanceLedgerError

    _substitute_contributor(two)
    with pytest.raises(
        AcceptanceLedgerError, match="transaction manifest does not bind its record"
    ):
        _status(two)


# --- Multi-seed Goals ------------------------------------------------------------


@pytest.fixture
def seeds(layout: SimpleNamespace) -> SimpleNamespace:
    """An active Goal Mode with the multi-seed Goal ``a@x3`` and its three frozen seeds."""
    record = enter_goals(
        layout, [{"family": "SIM", "target": "top", "test": "a@x3"}], tests_toml=TWO_TESTS
    )
    frozen = [run.seed for run in record.test_run_resolutions[SEEDS_KEY].test_runs]
    assert len(set(frozen)) == 3
    return SimpleNamespace(**vars(layout), record=record, seeds=frozen)


def _seed(seed: int, grade: str = "pass") -> dict[str, Any]:
    return run_row(build_test_run("a", seed=seed), grade)


@pytest.mark.timeout(180)
def test_multi_seed_goal_accumulates_per_seed_and_lists_failing_seeds(
    seeds: SimpleNamespace,
) -> None:
    first, second, third = seeds.seeds
    frozen = flow_test_runs(seeds.record, {SEEDS_KEY: 1})
    assert frozen.seeds({"top"}, "a", None, 3) == (first, second, third)
    assert frozen.seeds({"top"}, "a", None, 2) is None  # another N is another Goal

    _record(seeds, "run-1", [_seed(first), _seed(second, "fail")], key=SEEDS_KEY, met=False)
    # A seed passes in a second campaign, after the first one ended early.
    _record(seeds, "run-2", [_seed(third), _seed(second)], key=SEEDS_KEY)
    assert list(_outcomes(seeds, SEEDS_KEY).values()) == ["passed", "failed", "passed"]
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


@pytest.mark.timeout(150)
def test_pending_and_empty_goals_are_unmet_whatever_passes(layout: SimpleNamespace) -> None:
    goals = [
        {"family": "SIM", "target": "top"},
        {"family": "SIM", "target": "top", "test": "later"},
        {"family": "SIM", "target": "soon"},
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
        _record(mode, f"run-{key}", [run_row(A)], key=key)
        assert _outcomes(mode, key) == {}
        assert _status(mode, key).status == "unmet"


def test_removed_test_and_changed_variant_inputs_need_a_retarget(layout: SimpleNamespace) -> None:
    variants = (
        b'[top]\ntests = ["a", "b"]\n[top.variants.a]\ndefault = "v"\n'
        b'v = { args = ["+n=1"], env = { MODE = "1" } }\n'
    )
    record = enter_goals(layout, [{"family": "SIM", "target": "top"}], tests_toml=variants)
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


def test_a_test_skipped_after_entry_is_named_as_skipped(layout: SimpleNamespace) -> None:
    key = "sim_pass_top_run_61_d"
    record = enter_goals(
        layout, [{"family": "SIM", "target": "top", "test": "a"}], tests_toml=TWO_TESTS
    )
    mode = SimpleNamespace(**vars(layout), record=record)
    write_tests_toml(mode, b'[top]\ntests = ["a", "b"]\nskip = ["a"]\n')
    reason = _status(mode, key).reason
    assert reason.startswith("goal-test-run-missing: ")
    assert "a@1" in reason and "skipped in tests.toml" in reason
    assert "no longer exist" not in reason and "retarget" not in reason
    # Removed from the Target but still listed in skip: it is gone, not skipped.
    write_tests_toml(mode, b'[top]\ntests = ["b"]\nskip = ["a"]\n')
    assert "no longer exist" in _status(mode, key).reason


def test_evidence_recorded_before_this_change_does_not_count(goal_mode: SimpleNamespace) -> None:
    """An observation without Test Run rows meets nothing."""
    path = _state_file(goal_mode)
    detail = _record(goal_mode, "run-1", [run_row()])
    assert _status(goal_mode).status == "met"
    # Rewrite the observation and its projection as the previous version stored them.
    old = {
        key: value for key, value in detail.items() if key not in {"test_run_ledger", "test_runs"}
    }
    evidence = _evidence_file(goal_mode, 1)
    row = json.loads(evidence.read_text(encoding="utf-8"))
    row["detail"] = old
    evidence.write_text(json.dumps(row), encoding="utf-8")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["criteria"][SIM_KEY]["detail"] = old
    path.write_text(json.dumps(state), encoding="utf-8")
    status = _status(goal_mode)
    assert status.status == "unmet"
    assert status.reason.startswith("goal-test-run-incomplete: ")


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
    assert _outcomes(two) == {"a@1": "failed", "b@1": "passed"}
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


def _contributors(layout: SimpleNamespace, key: str = SIM_KEY) -> list[int]:
    from booley.goals.derivation import selected_observations
    from booley.goals.review_package import original_observations

    record, state, indexed = _package_inputs(layout)
    selected = selected_observations(record, state, layout.control)[key]
    return [row["sequence"] for row in original_observations(selected, indexed, record)]


def test_accumulated_goal_discloses_its_contributing_campaigns(two: SimpleNamespace) -> None:
    """The review package and generated-input authorization read these observations."""
    from booley.goals.derivation import selected_observations
    from booley.goals.review_package import original_observations, presentation_observations

    _record(two, "run-1", [run_row(A)])
    _record(two, "run-2", [run_row(B)])
    record, state, indexed = _package_inputs(two)
    selected = selected_observations(record, state, two.control)[SIM_KEY]
    assert selected["sequence"] == 2
    assert [row["sequence"] for row in original_observations(selected, indexed, record)] == [1]
    disclosed = presentation_observations(record, state, two.control)
    assert sorted(row["sequence"] for row in disclosed) == [1, 2]


def test_review_package_rejects_a_substituted_contributor(two: SimpleNamespace) -> None:
    from booley.criteria.evidence_ledger import AcceptanceLedgerError
    from booley.goals.review_package import presentation_observations

    _substitute_contributor(two)
    state = DevelopmentState.load(_state_file(two))
    with pytest.raises(
        AcceptanceLedgerError, match="transaction manifest does not bind its record"
    ):
        presentation_observations(two.record, state, two.control)


def test_briefing_names_test_runs_and_every_deferred_resolution(two: SimpleNamespace) -> None:
    from booley.goals.finish_presentation import (
        _deferred_resolution_lines,  # pyright: ignore[reportPrivateUsage]
        _test_run_lines,  # pyright: ignore[reportPrivateUsage]
    )
    from booley.goals.test_run_freezing import ensure_test_run_resolutions

    _record(two, "run-1", [run_row(A), run_row(B, "fail")], met=False)
    record, _state, indexed = _package_inputs(two)
    lines = _test_run_lines(
        record, {"key": SIM_KEY, "selected_observation": indexed[1], "original_observations": []}
    )
    assert lines[0] == "  Test Runs: a@1 passed, b@1 failed"
    assert "  rerun b@1: booley flow sim --target top --test=b@1" in lines
    names = {SIM_KEY: SIM_NAME}
    assert _deferred_resolution_lines(two.record.to_json(), names) == []
    write_tests_toml(two, b'[top]\ntests = ["a", "b", "late"]\n')
    resolved = ensure_test_run_resolutions(GoalStore(two.control), two.record.id, two.worktree)
    lines = _deferred_resolution_lines(resolved.to_json(), names)
    assert lines[:2] == ["", "Test Runs resolved after entry"]
    assert len(lines) == 3 and lines[2].startswith(f"- {SIM_NAME}: late@1 (slot *, resolved ")


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
        "SIM:top (all selected tests pass, test a@x3): 3 Test Runs (1 passed, 1 failed, 1 missing): "
        f"a@{first} passed, a@{second} failed, a@{third} missing",
        f"  rerun a@{second}: booley flow sim --target top --test=a@{second}",
        "SIM:top (all selected tests pass, test a@x3): baseline -> current: "
        f"1 pending -> pass (a@{first}: baseline has not been scheduled yet), "
        f"1 pending -> fail (a@{second}: baseline has not been scheduled yet), "
        f"1 pending -> current unavailable (a@{third}: baseline has not been scheduled yet)",
    ]
    text = render_status((view,))
    assert "Test Runs\n" in text
    # The heading and its indented lines, byte for byte.
    block = ["Test Runs", *(f"  {line}" for line in test_run_lines(goal))]
    lines = text.splitlines()
    start = lines.index("Test Runs")
    assert lines[start : start + len(block)] == block
    assert f"rerun a@{second}: booley flow sim --target top --test=a@{second}" in text
    assert "goal-test-run-failed" in text


def test_status_keeps_a_long_goal_name_and_its_status_on_one_line(two: SimpleNamespace) -> None:
    """A long unmet reason must not wrap the Goal name away from its status."""
    from dataclasses import replace

    from booley.goals.format import render_status
    from booley.goals.presentation import GoalPresentation

    view = build_status(GoalStore(two.control), two.record, work_dir=two.worktree)
    long_name = GoalPresentation(
        "SYNTH:synth_core", "cells increase ≤ 11% · critical path increase ≤ 3%"
    )
    (goal,) = view.goals
    assert len(goal.reason) > 30
    text = render_status((replace(view, goals=(replace(goal, presentation=long_name),)),))
    assert f"{long_name.text} unmet" in " ".join(text.split())
    assert any(long_name.text in line and "unmet" in line for line in text.splitlines())


def test_status_lines_name_pending_slots_and_runs_resolved_after_entry(
    layout: SimpleNamespace,
) -> None:
    from booley.goals.format import test_run_lines
    from booley.goals.status import select_status

    goals = [{"family": "SIM", "target": "top"}, {"family": "SIM", "target": "top", "test": "x"}]
    record = enter_goals(layout, goals, tests_toml=TWO_TESTS)
    mode = SimpleNamespace(**vars(layout), record=record)
    write_tests_toml(mode, b'[top]\ntests = ["a", "b", "late"]\n')
    (view,) = select_status(GoalStore(mode.control), mode.worktree).views
    suite, pending = (test_run_lines(goal) for goal in view.goals)
    assert suite == [
        f"{SIM_NAME}: 3 Test Runs (3 missing): a@1 missing, b@1 missing, late@1 missing",
        "  resolved after entry: late@1",
        f"{SIM_NAME}: baseline -> current: 3 pending -> current unavailable "
        "(a@1: no current Test Run evidence; b@1: no current Test Run evidence; "
        "late@1: no current Test Run evidence)",
    ]
    assert pending == [
        "SIM:top (all selected tests pass, test x): 0 Test Runs (none): —",
        "  pending (does not exist yet): x",
        "SIM:top (all selected tests pass, test x): baseline -> current: "
        "baseline pending (Test Runs unresolved)",
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
        f"{SIM_NAME}: 2 Test Runs (1 passed, 1 failed): a@1 passed, b@1 failed",
        "  rerun b@1: booley flow sim --target top --test=b@1",
        f"{SIM_NAME}: baseline -> current: 1 pending -> pass "
        "(a@1: baseline has not been scheduled yet), 1 pending -> fail "
        "(b@1: baseline has not been scheduled yet)",
    )
    assert _evidence_detail(detail).startswith(detail.test_runs[0] + "\n  rerun b@1: ")


def test_a_frozen_run_needs_a_real_timestamp() -> None:
    from booley.goals.test_run_resolution import (
        FrozenRun,
        ResolutionFormatError,
        TestRunResolution,
    )

    good = TestRunResolution(1, (FrozenRun(A, "a", "entry", "2026-10-13T10:00:00Z"),))
    assert TestRunResolution.from_json(good.to_json(), where="r") == good
    stored = good.to_json()
    stored["runs"][0]["resolved_at"] = "yesterday"
    with pytest.raises(ResolutionFormatError, match="resolved_at is not a timestamp"):
        TestRunResolution.from_json(stored, where="r")
    with pytest.raises(ResolutionFormatError, match=r"r\.spec_revision"):
        TestRunResolution.from_json({**good.to_json(), "spec_revision": 0}, where="r")


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "malformed",
        {
            "schema": "booley.goal-test-run-ledger/v1",
            "runs": [
                {"name": run.name, "fingerprint": run.fingerprint, "outcome": "failed"}
                for run in (A, B)
            ],
            "source_evidence": [],
        },
    ],
)
def test_outcomes_ignore_stored_ledger(two: SimpleNamespace, stored: Any) -> None:
    _record(two, "run-1", [run_row(A)])
    _record(two, "run-2", [run_row(B)])
    for sequence in (1, 2):
        path = _evidence_file(two, sequence)
        row = json.loads(path.read_bytes())
        row["detail"].pop("test_run_ledger", None)
        if stored is not None:
            row["detail"]["test_run_ledger"] = stored
        path.write_text(json.dumps(row), encoding="utf-8")
    assert _status(two).status == "met"
    assert _shown(_status(two)) == {"a@1": "passed", "b@1": "passed"}
    assert "test_run_ledger" not in _record(two, "run-3", [run_row(B)])


def test_recorded_and_restated_observations_store_only_own_runs(two: SimpleNamespace) -> None:
    first = _record(two, "run-1", [run_row(A)])
    second = _record(two, "run-2", [run_row(B)])
    assert "test_run_ledger" not in first and "test_run_ledger" not in second
    assert first["test_runs"] == [run_row(A)] and second["test_runs"] == [run_row(B)]
    path = _evidence_file(two, 2)
    row = json.loads(path.read_bytes())
    row["detail"]["test_run_ledger"] = "obsolete"
    path.write_text(json.dumps(row), encoding="utf-8")
    again = _record(two, "run-3", [run_row(B)], extra={"test_run_outcomes_only": True})
    assert "test_run_ledger" not in again and again["test_runs"] == [run_row(B)]
    assert _status(two).status == "met"


def test_record_time_and_status_agree_after_transaction_recovery(two: SimpleNamespace) -> None:
    from booley.criteria.evidence_ledger import validated_evidence_records
    from booley.goals.recorder import GOAL_SCOPE

    binding = bind(two, "transaction")
    stopped = GoalEvidenceRecorder(
        binding, publication_checkpoint=_interrupt("before:acceptance_state")
    )
    campaign = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(_InterruptedError):
        _transaction(two, stopped, campaign, [run_row(A, "fail")])
    state = DevelopmentState.load(_state_file(two))
    logs = record_paths(two.control, two.record.id).logs_dir
    assert validated_evidence_records(GOAL_SCOPE, logs, state, {}) == []
    _transaction(two, GoalEvidenceRecorder(binding), campaign, [run_row(A, "fail")])
    detail = _record(two, "later", [run_row(A), run_row(B)])
    state = DevelopmentState.load(_state_file(two))
    assert not state.criteria[SIM_KEY].met and _status(two).status == "unmet"
    assert _shown(_status(two)) == {"a@1": "failed", "b@1": "passed"}
    assert "test_run_ledger" not in detail


def test_a_pending_goal_never_latches_ever_failed(layout: SimpleNamespace) -> None:
    record = enter_goals(
        layout,
        [{"family": "SIM", "target": "top", "test": "later"}],
        tests_toml=b'[top]\ntests = ["a"]\nskip = ["a"]\n',
    )
    mode = SimpleNamespace(**vars(layout), record=record)
    later = "sim_pass_top_run_6c61746572_d"
    # What a producer claims while the Goal's Test Run does not exist yet.
    _record(mode, "run-1", [run_row(A)], key=later, met=False)
    assert _status(mode, later).reason.startswith("goal-test-run-pending: ")
    live = DevelopmentState.load(_state_file(mode)).criteria[later]
    assert live.met is False and live.ever_failed is False
    assert _replayed(mode, later).ever_failed is False

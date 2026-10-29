"""Per-Test-Run rows in ``simulation.json`` and in Criterion detail (ADR 0072)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import cycle_count_criterion_key
from booley.flows.frozen_test_runs import OUTCOMES_ONLY_DETAIL_KEY, FrozenTestRuns, GoalTestRuns
from booley.flows.sim.acceptance import (
    SimulationAcceptanceCoordinator,
    _campaign_projection,
)
from booley.flows.sim.campaign import AcceptanceFacts, decode_simulation_projection
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.projection import ProjectionTrust
from booley.flows.sim.campaign.test_run_report import evidence_test_run_rows
from booley.flows.test_runs import build_test_run, test_run_document
from tests.flows.sim.test_campaign_test_runs import _FAULT, _SOAK, _hdl_plan, _run_campaign

_RESET = build_test_run("reset", seed=1)
_COUNT = build_test_run("count", variant="soak", seed=42, args=("+iters=9",))


def _outcome(tmp_path: Path, *runs):
    return _run_campaign(tmp_path, _hdl_plan(tmp_path, tuple((run,) for run in runs)))


def _rows(*runs, classified=True) -> list[dict]:
    # The executor of these tests runs no simulator: every Test Run fails.
    return [
        {
            **test_run_document(run),
            "grade": "fail",
            **(
                {"functional_failure": False, "failure_execution": "blocked_by_build"}
                if classified
                else {}
            ),
        }
        for run in runs
    ]


def test_evidence_rows_are_the_recorded_test_runs_of_the_campaign(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _SOAK, _FAULT)
    assert evidence_test_run_rows(outcome) == _rows(_RESET, _SOAK, _FAULT)


def test_evidence_rows_refuse_a_manifest_the_facts_do_not_name(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET)
    (tmp_path / "other").mkdir()
    other = _hdl_plan(tmp_path / "other", ((_SOAK,),))
    outcome.manifest_path.write_bytes(encode_simulation_campaign_manifest(other.manifest))
    with pytest.raises(SimulationCampaignIntegrityError, match="not the one its acceptance"):
        evidence_test_run_rows(outcome)
    outcome.manifest_path.unlink()
    with pytest.raises(SimulationCampaignIntegrityError, match="cannot be read"):
        evidence_test_run_rows(outcome)


def test_projection_v3_carries_a_row_per_test_run_and_decodes(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    projection = _campaign_projection(outcome)

    assert projection["$schema"] == "booley.simulation-projection/v3"
    assert projection["test_runs"] == _rows(_RESET, _COUNT, classified=False)
    assert [
        (row["name"], row["test_run"], row["variant"], row["seed"], row["fingerprint"])
        for row in projection["tests"]
    ] == [
        ("reset", "reset@1", None, 1, _RESET.fingerprint),
        ("count", "count+soak@42", "soak", 42, _COUNT.fingerprint),
    ]
    document = json.loads(json.dumps({**projection, "complete": True}))
    decoded = decode_simulation_projection(document, coverage_expected=False)
    assert decoded.trust is ProjectionTrust.AUTHENTICATED


def _v3_document(tmp_path: Path) -> dict:
    projection = _campaign_projection(_outcome(tmp_path, _RESET))
    return json.loads(json.dumps({**projection, "complete": True}))


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        (lambda document: document.pop("test_runs"), "fields are not exact"),
        (lambda document: document.update(test_runs={}), "test_runs must be a list"),
        (lambda document: document["test_runs"][0].update(grade="maybe"), "no valid grade"),
        (lambda document: document["test_runs"][0].pop("grade"), "no valid grade"),
        (lambda document: document["test_runs"][0].update(seed=2), "disagrees with its own"),
        (lambda document: document["test_runs"][0].pop("args"), "must hold exactly"),
    ],
)
def test_projection_v3_refuses_an_inexact_test_run_row(tmp_path: Path, alter, message):
    document = _v3_document(tmp_path)
    alter(document)
    with pytest.raises(ValueError, match=message):
        decode_simulation_projection(document, coverage_expected=False)


def _state(tmp_path: Path, outcome, *, cycle_test: str | None = None) -> DevelopmentState:
    target = outcome.target
    criteria = {f"sim_pass_{target['name']}": True}
    params: dict[str, dict] = {}
    if cycle_test is not None:
        key = cycle_count_criterion_key(str(target["name"]), cycle_test)
        criteria[key] = True
        params[key] = {
            "target": f"{target['vlnv']}#{target['name']}",
            "_target_selector": target["selector"],
            "test": cycle_test,
            "cycle_count_max": 100,
        }
    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria(criteria, criterion_params=params, strict=True)
    return state


def test_simulation_and_cycle_criterion_detail_name_their_test_runs(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    state = _state(tmp_path, outcome, cycle_test="count")

    changes = SimulationAcceptanceCoordinator._derive_changes(outcome, state)

    detail = {change.key: change.detail for change in changes}
    simulation = detail[f"sim_pass_{outcome.target['name']}"]
    # Bare Test names stay for today's consumers; the rows are in the same order.
    assert simulation["selected_tests"] == ["count", "reset"]
    assert simulation["test_runs"] == _rows(_COUNT, _RESET)
    cycle = detail[cycle_count_criterion_key(str(outcome.target["name"]), "count")]
    assert cycle["test"] == "count"
    assert cycle["test_runs"] == _rows(_COUNT)


def test_criterion_detail_rows_come_from_the_authenticated_manifest(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    state = _state(tmp_path, outcome)
    outcome.manifest_path.write_bytes(b"{}\n")
    with pytest.raises(SimulationCampaignIntegrityError):
        SimulationAcceptanceCoordinator._derive_changes(outcome, state)


def test_outside_goal_mode_a_test_with_several_test_runs_judges_no_criterion(tmp_path: Path):
    """A Criterion names a Test; several Test Runs of it leave nothing single to judge."""
    outcome = _outcome(tmp_path, _RESET, _SOAK, _FAULT)
    state = _state(tmp_path, outcome)
    assert SimulationAcceptanceCoordinator._derive_changes(outcome, state) == []


# --- Inside Goal Mode: evidence matches frozen Test Runs (ADR 0072) ---------------------


def _goal(outcome, key: str, *runs, kind: str = "simulation", **extra) -> GoalTestRuns:
    return GoalTestRuns(key, kind, str(outcome.target["selector"]), tuple(runs), **extra)  # type: ignore[arg-type]


def _goal_changes(outcome, state, *goals: GoalTestRuns):
    frozen = FrozenTestRuns(tuple(goals))
    return SimulationAcceptanceCoordinator._derive_changes(outcome, state, frozen=frozen)


def _graded(monkeypatch, grades: dict[str, str]) -> None:
    """Grade the campaign's Test Run rows by name (the fake executor fails every run)."""
    from booley.flows.sim import acceptance

    def rows(outcome):
        return [
            {**row, "grade": grades.get(str(row["name"]), row["grade"])}
            for row in evidence_test_run_rows(outcome)
        ]

    monkeypatch.setattr(acceptance, "evidence_test_run_rows", rows)


def test_goal_evidence_is_the_runs_that_match_by_name_and_fingerprint(tmp_path, monkeypatch):
    outcome = _outcome(tmp_path, _RESET, _SOAK, _FAULT)
    key = f"sim_pass_{outcome.target['name']}"
    state = _state(tmp_path, outcome)
    _graded(monkeypatch, {_RESET.name: "pass", _SOAK.name: "pass"})
    # The Goal requires reset and soak; ``_FAULT`` failed but no Goal names it.
    (change,) = _goal_changes(outcome, state, _goal(outcome, key, _RESET, _SOAK))
    assert (change.key, change.met) == (key, True)
    assert [row["name"] for row in change.detail["test_runs"]] == [_RESET.name, _SOAK.name]
    assert change.detail["failed_tests"] == []


def test_same_name_with_other_declared_inputs_is_not_evidence(tmp_path, monkeypatch):
    outcome = _outcome(tmp_path, _RESET, _SOAK)
    key = f"sim_pass_{outcome.target['name']}"
    state = _state(tmp_path, outcome)
    _graded(monkeypatch, {_RESET.name: "pass", _SOAK.name: "pass"})
    other_args = build_test_run(
        _SOAK.test, variant=_SOAK.variant, seed=_SOAK.seed, args=("+other=1",)
    )
    assert other_args.name == _SOAK.name and other_args.fingerprint != _SOAK.fingerprint
    # Only a run of another seed and one with other arguments: the campaign is diagnostic.
    other_seed = build_test_run("reset", seed=99)
    assert _goal_changes(outcome, state, _goal(outcome, key, other_args, other_seed)) == []


def test_partial_run_is_evidence_but_does_not_claim_the_goal(tmp_path, monkeypatch):
    outcome = _outcome(tmp_path, _RESET)
    key = f"sim_pass_{outcome.target['name']}"
    state = _state(tmp_path, outcome)
    _graded(monkeypatch, {_RESET.name: "pass"})
    (change,) = _goal_changes(outcome, state, _goal(outcome, key, _RESET, _SOAK))
    # The Goal's recorder folds this run into the ledger; this campaign alone meets nothing.
    assert change.met is False
    assert [row["name"] for row in change.detail["test_runs"]] == [_RESET.name]


def test_goal_of_another_target_and_an_unobserved_run_get_no_change(tmp_path, monkeypatch):
    outcome = _outcome(tmp_path, _RESET)
    key = f"sim_pass_{outcome.target['name']}"
    state = _state(tmp_path, outcome)
    elsewhere = GoalTestRuns(key, "simulation", "another_target", (_RESET,))
    assert _goal_changes(outcome, state, elsewhere) == []
    _graded(monkeypatch, {_RESET.name: "error"})  # infrastructure: nothing was observed
    assert _goal_changes(outcome, state, _goal(outcome, key, _RESET)) == []


def test_cycle_count_goal_is_judged_from_its_frozen_test_run_only(tmp_path):
    outcome = _outcome(tmp_path, _RESET, _COUNT)
    state = _state(tmp_path, outcome, cycle_test="count")
    key = cycle_count_criterion_key(str(outcome.target["name"]), "count")
    (change,) = _goal_changes(outcome, state, _goal(outcome, key, _COUNT, kind="cycle_count"))
    assert change.key == key and change.detail["test_runs"] == _rows(_COUNT)
    # Another seed of the same Test is not the Goal's Test Run: no evidence.
    other = build_test_run("count", variant="soak", seed=7, args=("+iters=9",))
    assert _goal_changes(outcome, state, _goal(outcome, key, other, kind="cycle_count")) == []


def test_coverage_goal_takes_run_outcomes_but_no_metric_from_an_ordinary_campaign(tmp_path):
    outcome = _outcome(tmp_path, _RESET)
    state = _state(tmp_path, outcome)
    goal = _goal(outcome, f"sim_pass_{outcome.target['name']}", _RESET, kind="coverage")
    # The campaign collected no coverage, but it ran (and failed) the Goal's frozen run:
    # that outcome is evidence for the Goal, its coverage is not measured.
    (change,) = _goal_changes(outcome, state, goal)
    assert change.met is False
    assert change.detail["test_runs"] == _rows(_RESET)
    assert change.detail[OUTCOMES_ONLY_DETAIL_KEY] is True


def test_cycle_count_goal_keeps_a_failing_frozen_run_without_a_cycle_observation(
    tmp_path, monkeypatch
):
    from booley.flows.sim import acceptance

    outcome = _outcome(tmp_path, _COUNT)
    state = _state(tmp_path, outcome, cycle_test="count")
    key = cycle_count_criterion_key(str(outcome.target["name"]), "count")
    monkeypatch.setattr(acceptance, "_candidate_observations", lambda *_args: {})
    (change,) = _goal_changes(outcome, state, _goal(outcome, key, _COUNT, kind="cycle_count"))
    assert (change.key, change.met) == (key, False)
    assert change.detail["test_runs"] == _rows(_COUNT)
    assert change.detail[OUTCOMES_ONLY_DETAIL_KEY] is True


def test_an_incomplete_campaign_still_records_its_matching_failure(tmp_path):
    outcome = replace(_outcome(tmp_path, _RESET), complete=False)
    key = f"sim_pass_{outcome.target['name']}"
    (change,) = _goal_changes(outcome, _state(tmp_path, outcome), _goal(outcome, key, _RESET))
    assert change.met is False and change.detail["test_runs"] == _rows(_RESET)


def test_facts_that_name_no_test_runs_have_no_rows(tmp_path: Path):
    outcome = _outcome(tmp_path, _RESET)
    document = dict(outcome.acceptance_facts.document)
    document["$schema"] = "booley.simulation-acceptance-facts/v1"
    document["observations"] = [
        {
            key: value
            for key, value in observation.items()
            if key not in {"test_run", "test_run_fingerprint"}
        }
        for observation in document["observations"]
    ]
    older = replace(outcome, acceptance_facts=AcceptanceFacts(document))
    assert evidence_test_run_rows(older) == []

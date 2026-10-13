"""Public names and addresses are independent of persisted/evidence identity."""

import copy
import json
from pathlib import Path

import pytest

from booley.goals.model import (
    REVIEW_KINDS,
    GoalArgError,
    GoalFamily,
    GoalSpec,
    goal_arg_to_json,
    parse_goal_arg,
    parse_stored_goal_arg,
)
from booley.goals.presentation import UNAVAILABLE_GOAL, goal_presentation, present_spec
from booley.goals.translate import translate_goals


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        (GoalFamily.LINT, "LINT:core_with_underscores"),
        (GoalFamily.SIM, "SIM:core_with_underscores"),
        (GoalFamily.ELAB, "ELAB:core_with_underscores"),
        (GoalFamily.SYNTH, "SYNTH:core_with_underscores"),
        (GoalFamily.FPGA, "FPGA:core_with_underscores"),
        (GoalFamily.CYCLE_COUNT, "CYCLE_COUNT:core_with_underscores"),
        (GoalFamily.COVERAGE, "COVERAGE:core_with_underscores"),
        (GoalFamily.MUTATION, "MUTATION:core_with_underscores"),
    ],
)
def test_names_do_not_decode_keys_or_saved_presentation(family, expected):
    goal = GoalSpec("encoded_identity_sentinel", family, "core_with_underscores", {}, ("ad-hoc",))
    before = copy.deepcopy(goal.to_json())
    for facts in ({}, {"met": True}, {"stale": True, "presentation": {"label": "LEAK"}}):
        presentation = goal_presentation(goal, facts)
        assert presentation.name == expected
        assert "sentinel" not in presentation.text and "LEAK" not in presentation.text
    assert goal.to_json() == before


@pytest.mark.parametrize("kind", REVIEW_KINDS)
@pytest.mark.parametrize("verdict", ["clean", "done"])
def test_every_review_has_one_public_name_and_explicit_verdict(kind, verdict):
    goal = GoalSpec(f"review_{kind}_{verdict}", GoalFamily.REVIEW, None, {}, ("ad-hoc",))
    presentation = goal_presentation(goal)
    assert presentation.name == "REVIEW:" + kind
    assert presentation.details == verdict


@pytest.mark.parametrize("target", ["vendor:library:core:1#sim_core", "sim_removed"])
def test_authored_selector_survives_missing_catalog(target):
    goal = GoalSpec("sim_pass_internal_sentinel", GoalFamily.SIM, target, {}, ("ad-hoc",))
    assert goal_presentation(goal).name == "SIM:" + target
    assert present_spec(goal.to_json()) == goal_presentation(goal).text


def test_same_name_goals_show_test_baseline_and_clock_details():
    first = GoalSpec(
        "sentinel_a",
        GoalFamily.CYCLE_COUNT,
        "sim_core",
        {"test": "stress", "cycle_count_max": 10},
        ("ad-hoc",),
    )
    second = GoalSpec(
        "sentinel_b",
        GoalFamily.CYCLE_COUNT,
        "sim_core",
        {"test": "smoke", "cycle_count_max": 10},
        ("ad-hoc",),
    )
    assert goal_presentation(first).name == goal_presentation(second).name
    assert "test stress" in goal_presentation(first).text
    assert "test smoke" in goal_presentation(second).text
    synth = GoalSpec(
        "sentinel",
        GoalFamily.SYNTH,
        "synth_core",
        {"clk.fmax_mhz_min": 400, "baseline_target": "synth_base"},
        ("ad-hoc",),
    )
    assert "clk" in goal_presentation(synth).details
    assert "400 MHz" in goal_presentation(synth).details
    assert "baseline synth_base" in goal_presentation(synth).details


@pytest.mark.parametrize("raw", [{}, None, {"family": "unsupported", "key": "LEAK"}])
def test_malformed_storage_never_falls_back_to_raw_keys(raw):
    assert present_spec(raw) == UNAVAILABLE_GOAL
    unknown = GoalSpec("review_internal_sentinel", GoalFamily.REVIEW, None, {}, ("ad-hoc",))
    assert goal_presentation(unknown).text == UNAVAILABLE_GOAL


@pytest.mark.parametrize("family", GoalFamily)
def test_public_schema_spelling_preserves_legacy_translation(family):
    raw = {"family": family.name, "target": "core"}
    if family is GoalFamily.REVIEW:
        raw = {"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean"}
    elif family is GoalFamily.CYCLE_COUNT:
        raw.update(test="stress", thresholds={"cycle_count_max": 10})
    elif family is GoalFamily.COVERAGE:
        raw.update(metrics={"line": 90}, tests="all")
    arg = parse_goal_arg(raw)
    saved = {**raw, "family": family.value}
    assert goal_arg_to_json(arg)["family"] == family.name
    public = translate_goals((arg,)).goals[0]
    legacy = translate_goals((parse_stored_goal_arg(saved),)).goals[0]
    assert public.to_json() == legacy.to_json()
    assert public.to_json()["family"] == family.value


@pytest.mark.parametrize("token", ["sim", "synth", "synthesis_ok", "sim_pass", "LEAK_SENTINEL"])
def test_invalid_public_capability_is_not_echoed(token):
    with pytest.raises(GoalArgError) as caught:
        parse_goal_arg({"family": token, "target": "sim_core"})
    assert "SIM" in str(caught.value) and "SYNTH" in str(caught.value)
    assert token not in str(caught.value)


def test_legacy_non_waiver_proposal_payload_and_digest_round_trip():
    from booley.goals.proposals import Proposal, digest, encode

    path = Path(__file__).parent / "fixtures/non-waiver-proposal-v1/payload.json"
    content = path.read_bytes()
    raw = json.loads(content)
    proposal = Proposal.from_json(raw)
    assert proposal.after_arg["family"] == "lint"
    assert encode(proposal.to_json()) == content
    assert proposal.payload_digest == digest(raw)
    assert goal_presentation(proposal.after).name == "LINT:top"


def test_automatic_mutation_goals_keep_detection_floors_distinct():
    from dataclasses import replace

    goal = GoalSpec(
        "opaque_sentinel",
        GoalFamily.MUTATION,
        "top",
        {"auto": True, "min_detected": 1},
        ("ad-hoc",),
    )
    stricter = replace(goal, params={**goal.params, "min_detected": 2})
    first, second = goal_presentation(goal), goal_presentation(stricter)
    assert first.name == second.name == "MUTATION:top"
    assert "≥ 1 mutations detected" in first.details
    assert "≥ 2 mutations detected" in second.details
    assert first.text != second.text


@pytest.mark.parametrize(
    "raw, internal",
    [
        (
            {"family": "SYNTH", "target": "top", "thresholds": {"cell_count_max": -1}},
            "synthesis_ok",
        ),
        ({"family": "MUTATION", "target": "top", "min_detected": 3, "total": 2}, "mutation_score"),
    ],
)
def test_invalid_requirements_name_public_goal_without_internal_family(raw, internal):
    from booley.goals.translate import GoalTranslationError

    with pytest.raises(GoalTranslationError) as caught:
        translate_goals((parse_goal_arg(raw),))
    assert raw["family"] + ":top" in str(caught.value)
    assert internal not in str(caught.value)

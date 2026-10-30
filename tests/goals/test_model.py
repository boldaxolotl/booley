"""Goal arguments, Goal values, and the Goal Record format."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from booley.criteria import templates
from booley.goals.model import (
    ALLOWED_TRANSITIONS,
    COVERAGE_METRICS,
    MUTATION_PARAMS,
    OCCUPYING_STATES,
    REVIEW_KINDS,
    CoverageGoalArg,
    CycleCountGoalArg,
    GoalArgError,
    GoalFamily,
    GoalRecord,
    GoalRecordFormatError,
    GoalSpec,
    GoalState,
    ImplementationGoalArg,
    MutationGoalArg,
    RecordedGoal,
    ReviewGoalArg,
    TargetGoalArg,
    WorktreeIdentity,
    goal_arg_json_schema,
    goal_arg_to_json,
    parse_goal_arg,
    parse_goal_args,
)
from tests.goals.conftest import INPUT_PATHS

REPOSITORY = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
SHA = "a" * 40


def record(**changes: Any) -> GoalRecord:
    """A minimal valid Goal Record at revision 1."""
    base = GoalRecord(
        id="fix-uart-20261006T101500Z",
        state=GoalState.ACTIVE,
        worktree=WorktreeIdentity(REPOSITORY, "worktrees/uart"),
        worktree_path="/work/.booley/worktrees/uart",
        branch="goal/fix-uart-20261006",
        original_ref="refs/heads/main",
        base_sha=SHA,
        entered_at="2026-10-06T10:15:00Z",
        input_topology_digest="sha256:" + "e" * 64,
        input_paths=INPUT_PATHS,
        protected_digest=None,
        protected_head_digest=None,
        revision=1,
        goals=(
            RecordedGoal(
                GoalSpec("lint_clean_lint_uart", GoalFamily.LINT, "lint_uart", {}, ("bugfix",)),
                1,
            ),
        ),
    )
    return replace(base, **changes)


# --- GoalArg parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"family": "LINT", "target": "lint_uart"}, TargetGoalArg(GoalFamily.LINT, "lint_uart")),
        (
            {"family": "SIM", "target": "sim_uart", "origin": "bugfix"},
            TargetGoalArg(GoalFamily.SIM, "sim_uart", "bugfix"),
        ),
        ({"family": "ELAB", "target": "sim_uart"}, TargetGoalArg(GoalFamily.ELAB, "sim_uart")),
        (
            {"family": "SYNTH", "target": "synth_uart", "thresholds": {"area_um2_max": 10}},
            ImplementationGoalArg(GoalFamily.SYNTH, "synth_uart", {"area_um2_max": 10}),
        ),
        (
            {"family": "FPGA", "target": "fpga_new", "baseline": "fpga_old"},
            ImplementationGoalArg(GoalFamily.FPGA, "fpga_new", {}, "fpga_old"),
        ),
        (
            {"family": "SYNTH", "target": "synth_uart", "baseline": "synth_uart"},
            ImplementationGoalArg(GoalFamily.SYNTH, "synth_uart"),
        ),
        (
            {
                "family": "CYCLE_COUNT",
                "target": "sim_uart",
                "test": "smoke",
                "thresholds": {"cycle_count_max": 9},
            },
            CycleCountGoalArg("sim_uart", "smoke", {"cycle_count_max": 9}),
        ),
        (
            {"family": "COVERAGE", "target": "sim_uart", "metrics": {"line": 80}, "tests": "all"},
            CoverageGoalArg("sim_uart", {"line": 80}, "all"),
        ),
        (
            {"family": "COVERAGE", "target": "sim_uart", "metrics": {"line": 80}, "tests": ["a"]},
            CoverageGoalArg("sim_uart", {"line": 80}, ("a",)),
        ),
        (
            {"family": "MUTATION", "target": "sim_uart", "min_detected": 8, "total": 10},
            MutationGoalArg("sim_uart", {"min_detected": 8, "total": 10}),
        ),
        (
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_bugs",
                "verdict": "clean",
            },
            ReviewGoalArg("rtl_bugs", "clean", paths=("rtl/mod_a.sv",)),
        ),
        (
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_spec",
                "verdict": "done",
                "spec": "doc/uart.md",
            },
            ReviewGoalArg("rtl_spec", "done", "doc/uart.md", paths=("rtl/mod_a.sv",)),
        ),
    ],
)
def test_every_family_parses_to_its_shape(raw: dict[str, Any], expected: object) -> None:
    assert parse_goal_arg(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"family": "formal", "target": "x"}, "family must be one of"),
        ({"target": "x"}, "family is required"),
        ({"family": "LINT"}, "target is required"),
        ({"family": "SIM", "target": "  "}, "non-empty"),
        ({"family": "CYCLE_COUNT", "test": "t", "thresholds": {}}, "target is required"),
        ({"family": "COVERAGE", "metrics": {"line": 1}, "tests": "all"}, "target is required"),
        ({"family": "LINT", "target": "a b"}, "whitespace"),
        ({"family": "LINT", "target": "tb@lint"}, "'@'"),
        ({"family": "LINT", "target": "x->y"}, "'->'"),
        ({"family": "LINT", "target": "x", "thresholds": {}}, "unknown fields"),
        ({"family": "CYCLE_COUNT", "target": "x", "test": "t"}, "thresholds is required"),
        ({"family": "COVERAGE", "target": "x", "metrics": {"line": 1}}, "tests is required"),
        ({"family": "COVERAGE", "target": "x", "metrics": {}, "tests": []}, "tests must be"),
        (
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_magic",
                "verdict": "clean",
            },
            "review must be",
        ),
        (
            {"paths": ["rtl/mod_a.sv"], "family": "REVIEW", "review": "rtl_bugs", "verdict": "ok"},
            "verdict must be",
        ),
        (
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_spec",
                "verdict": "clean",
            },
            "spec is required",
        ),
        (
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_bugs",
                "verdict": "clean",
                "spec": "x.md",
            },
            "spec is required",
        ),
        ("lint", "must be a mapping"),
    ],
)
def test_malformed_goal_arguments_are_rejected(raw: object, message: str) -> None:
    with pytest.raises(GoalArgError, match=message):
        parse_goal_arg(raw)


def test_goal_list_errors_name_the_index() -> None:
    with pytest.raises(GoalArgError, match=r"goals\[1\]\.target is required"):
        parse_goal_args([{"family": "LINT", "target": "a"}, {"family": "SIM"}])
    with pytest.raises(GoalArgError, match="must be a list"):
        parse_goal_args({"family": "LINT"})


def test_review_kinds_match_the_base_review_criteria() -> None:
    base = {c.name for c in templates.load_base_criteria() if c.name.startswith("review_")}
    assert {f"review_{kind}" for kind in REVIEW_KINDS} == base


def test_vocabulary_matches_the_criteria_rules() -> None:
    assert frozenset(COVERAGE_METRICS) == templates._COVERAGE_METRICS
    assert templates._TARGET_CAMPAIGN_PARAM_REGISTRY["mutation_score"][0] == MUTATION_PARAMS


def test_schema_has_one_variant_per_family_matching_the_parser() -> None:
    variants = goal_arg_json_schema()["oneOf"]
    by_family = {v["properties"]["family"]["const"]: v for v in variants}
    assert set(by_family) == {family.name for family in GoalFamily}
    for family, variant in by_family.items():
        assert variant["additionalProperties"] is False
        raw = {name: _sample(name, family) for name in variant["required"]}
        raw["family"] = family
        if family == "REVIEW":
            raw.update(review="rtl_spec", spec="doc/spec.md", target="top")
        assert parse_goal_arg(raw).family.name == family
        for extra in set(variant["properties"]) - set(raw):
            if family == "REVIEW" and extra == "paths":
                parse_goal_arg(
                    {
                        **{key: value for key, value in raw.items() if key != "target"},
                        "paths": ["rtl/mod_a.sv"],
                    }
                )
                continue
            parse_goal_arg({**raw, extra: _sample(extra, family)})  # every schema field parses
    json.dumps(goal_arg_json_schema())  # serializable as an MCP input schema


def _sample(name: str, family: str) -> object:
    samples: dict[str, object] = {
        "metrics": {"line": 50},
        "tests": "all",
        "thresholds": {"cycle_count_max": 5},
        "verdict": "clean",
        "scope": ["rtl/a.sv"],
        "min_detected": 1,
        "total": 2,
        "auto": True,
    }
    if name == "thresholds" and family in ("SYNTH", "FPGA"):
        return {"critical_path_ps_max": 900}
    return samples.get(name, "x_name")


# --- Goal states ---------------------------------------------------------------


def test_only_entering_active_and_finishing_occupy_a_worktree() -> None:
    assert {GoalState.ENTERING, GoalState.ACTIVE, GoalState.FINISHING} == OCCUPYING_STATES


def test_terminal_states_have_no_exit() -> None:
    for state in set(GoalState) - OCCUPYING_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
    assert GoalState.ACTIVE in ALLOWED_TRANSITIONS[GoalState.FINISHING]


# --- Goal Record format -------------------------------------------------------


@pytest.mark.parametrize("family", list(GoalFamily))
@pytest.mark.parametrize("state", list(GoalState))
def test_record_round_trips_through_json(family: GoalFamily, state: GoalState) -> None:
    original = record(
        session_key="pid:12:34",
        goalsets_used=("bugfix",),
        protected_digest="sha256:" + "b" * 64,
        protected_head_digest="sha256:" + "f" * 64,
        protected_paths=("/work/booley.toml",),
        validated_head="c" * 64,
        package_digest="sha256:" + "d" * 64,
        ended_at="2026-10-06T11:00:00Z",
    )
    goal = original.goals[0]
    original = replace(
        original, state=state, goals=(replace(goal, spec=replace(goal.spec, family=family)),)
    )
    encoded = json.loads(json.dumps(original.to_json()))
    assert GoalRecord.from_json(encoded) == original


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "paused", "state 'paused' is unknown"),
        ("revision", 0, "positive integer"),
        ("revision", True, "must be an integer"),
        ("base_sha", "abc", "full commit id"),
        ("entered_at", "2026-10-06 10:15", "RFC 3339"),
        ("protected_digest", "md5:x", "sha256 digest"),
        ("schema", 2, "not supported"),
        ("goals", "lint", "must be a list"),
        ("branch_created", "yes", "boolean"),
        ("ended_at", "yesterday", "RFC 3339"),
    ],
)
def test_record_fields_are_validated(field: str, value: object, message: str) -> None:
    raw = record().to_json()
    raw[field] = value
    with pytest.raises(GoalRecordFormatError, match=message):
        GoalRecord.from_json(raw)


def test_record_rejects_a_goal_newer_than_the_record() -> None:
    raw = record().to_json()
    raw["goals"][0]["spec_revision"] = 2
    with pytest.raises(GoalRecordFormatError, match="newer than the record"):
        GoalRecord.from_json(raw)


@pytest.mark.parametrize(
    "worktree",
    [
        {"repository": "not-a-uuid", "checkout": "main"},
        {"repository": REPOSITORY, "checkout": "worktrees/a/b"},
        {"repository": REPOSITORY, "checkout": "mainline"},
    ],
)
def test_record_rejects_a_malformed_worktree_identity(worktree: dict[str, str]) -> None:
    raw = record().to_json()
    raw["worktree"] = worktree
    with pytest.raises(GoalRecordFormatError):
        GoalRecord.from_json(raw)


def test_primary_and_linked_checkout_named_main_never_collide() -> None:
    primary = WorktreeIdentity(REPOSITORY, "main")
    linked = WorktreeIdentity(REPOSITORY, "worktrees/main")
    assert primary != linked
    assert primary.key != linked.key


# --- Schema evolution within one version --------------------------------------


def test_an_older_record_without_optional_fields_loads_with_defaults() -> None:
    raw = record().to_json()
    for optional in ("failure", "package_digest", "validated_head", "goalsets_used", "goals"):
        del raw[optional]
    loaded = GoalRecord.from_json(raw)
    assert (loaded.failure, loaded.package_digest, loaded.goals) == (None, None, ())


def test_a_record_from_a_newer_booley_is_refused_with_that_reason() -> None:
    raw = record().to_json()
    raw["dashboard_color"] = "teal"
    with pytest.raises(GoalRecordFormatError, match="written by a newer Booley"):
        GoalRecord.from_json(raw)


@pytest.mark.parametrize(
    "required",
    [
        "id",
        "worktree",
        "base_sha",
        "entered_at",
        "schema",
        "branch",
        "input_paths",
        "input_topology_digest",
        "protected_head_digest",
    ],
)
def test_required_fields_stay_required(required: str) -> None:
    raw = record().to_json()
    del raw[required]
    with pytest.raises(GoalRecordFormatError, match="missing"):
        GoalRecord.from_json(raw)


@pytest.mark.parametrize("field", ["input_paths", "input_topology_digest"])
def test_record_input_fields_may_not_be_null(field: str) -> None:
    raw = record().to_json()
    raw[field] = None
    with pytest.raises(GoalRecordFormatError):
        GoalRecord.from_json(raw)


@pytest.mark.parametrize("state", ["entering", "failed"])
def test_record_without_a_protected_snapshot_round_trips(state: str) -> None:
    raw = record(state=GoalState(state)).to_json()
    assert raw["protected_digest"] is None and raw["protected_head_digest"] is None
    assert GoalRecord.from_json(raw).to_json() == raw


@pytest.mark.parametrize(
    "present",
    ["protected_digest", "protected_head_digest"],
    ids=["digest-without-head", "head-without-digest"],
)
def test_protected_digests_are_null_together(present: str) -> None:
    raw = record().to_json()
    raw[present] = "sha256:" + "b" * 64
    with pytest.raises(GoalRecordFormatError, match="null exactly when"):
        GoalRecord.from_json(raw)


@pytest.mark.parametrize(
    "spec", ["/etc/spec.md", "../outside.md", "doc/../../x.md", "C:\\spec.md", "a\\..\\..\\b"]
)
def test_spec_paths_may_not_escape_the_worktree(spec: str) -> None:
    with pytest.raises(GoalArgError, match="relative to the worktree"):
        parse_goal_arg(
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_spec",
                "verdict": "clean",
                "spec": spec,
            }
        )


_STRUCTURAL = [
    "targets",
    "target",
    "test",
    "baseline",
    "candidate",
    "metrics",
    "tests",
    "_baseline_target",
]


@pytest.mark.parametrize("family", ["SYNTH", "FPGA"])
@pytest.mark.parametrize(
    "name",
    [*_STRUCTURAL, "lut_or_area", "clk i.fmax_mhz_min", ".fmax_mhz_min", "a.b.fmax_mhz_min"],
)
def test_implementation_thresholds_accept_only_family_parameters(family: str, name: str) -> None:
    with pytest.raises(GoalArgError, match="unknown threshold"):
        parse_goal_arg({"family": family, "target": "t", "thresholds": {name: 1}})


@pytest.mark.parametrize("name", [*_STRUCTURAL, "clk.cycle_count_max", "area_um2_max"])
def test_cycle_count_thresholds_accept_only_cycle_count_parameters(name: str) -> None:
    with pytest.raises(GoalArgError, match="unknown threshold"):
        parse_goal_arg(
            {"family": "CYCLE_COUNT", "target": "t", "test": "x", "thresholds": {name: 1}}
        )


@pytest.mark.parametrize("clock", ["clk_i", "clk-out", "clk[0]", "u_pll/clk"])
def test_clock_scoped_thresholds_accept_every_clock_name_criteria_accept(clock: str) -> None:
    name = f"{clock}.fmax_mhz_min"
    arg = parse_goal_arg({"family": "SYNTH", "target": "t", "thresholds": {name: 1}})
    assert isinstance(arg, ImplementationGoalArg)
    assert dict(arg.thresholds) == {name: 1}
    # The same entry is valid Criteria authoring, bound to the same Target.
    authored = {"mandatory": {"synthesis_ok": {"targets": ["t"], name: 1}}}
    specs = templates.CriteriaTemplate.from_yaml(authored).specs
    assert [(spec.name, spec.targets, spec.params) for spec in specs] == [
        ("synthesis_ok", ["t"], {name: 1})
    ]


@pytest.mark.parametrize("name", ["a.b.fmax_mhz_min", ".fmax_mhz_min", "clk i.fmax_mhz_min"])
def test_malformed_clock_scopes_are_rejected(name: str) -> None:
    with pytest.raises(GoalArgError, match="unknown threshold"):
        parse_goal_arg({"family": "SYNTH", "target": "t", "thresholds": {name: 1}})


@pytest.mark.parametrize("name", ["targets", "tests", "statement"])
def test_coverage_metrics_accept_only_coverage_metrics(name: str) -> None:
    with pytest.raises(GoalArgError, match="unknown metrics"):
        parse_goal_arg(
            {"family": "COVERAGE", "target": "t", "metrics": {name: 50}, "tests": "all"}
        )


def test_schema_threshold_names_agree_with_the_parser() -> None:
    import re

    variants = {v["properties"]["family"]["const"]: v for v in goal_arg_json_schema()["oneOf"]}
    candidates = [
        *_STRUCTURAL,
        "area_um2_max",
        "clk_i.fmax_mhz_min",
        "cycle_count_max",
        "lut_count_max",
        "x.cycle_count_max",
    ]
    for family, extra in (("SYNTH", {}), ("FPGA", {}), ("CYCLE_COUNT", {"test": "x"})):
        pattern = variants[family]["properties"]["thresholds"]["propertyNames"]["pattern"]
        for name in candidates:
            raw = {"family": family, "target": "t", "thresholds": {name: 1}, **extra}
            try:
                parse_goal_arg(raw)
                parsed = True
            except GoalArgError:
                parsed = False
            assert bool(re.fullmatch(pattern, name)) == parsed, (family, name)


@pytest.mark.parametrize(
    "spec", ["\\outside\\spec.md", "/outside/spec.md", "D:spec.md", "\\\\server\\share\\s.md"]
)
def test_rooted_and_drive_relative_spec_paths_are_refused(spec: str) -> None:
    with pytest.raises(GoalArgError, match="relative to the worktree"):
        parse_goal_arg(
            {
                "paths": ["rtl/mod_a.sv"],
                "family": "REVIEW",
                "review": "rtl_spec",
                "verdict": "clean",
                "spec": spec,
            }
        )


# ---------------------------------------------------------------------------
# Goals that name Test Runs (ADR 0072)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test", ["smoke", "stress+soak", "stress@7", "stress+soak@7", "s@x3"])
def test_simulation_goal_accepts_one_test_run_selector(test: str) -> None:
    goal = parse_goal_arg({"family": "SIM", "target": "tb", "test": test})
    assert goal == TargetGoalArg(GoalFamily.SIM, "tb", test=test)
    assert goal_arg_to_json(goal) == {
        "family": "SIM",
        "target": "tb",
        "origin": "ad-hoc",
        "test": test,
    }


def test_simulation_goal_without_a_test_is_the_target_goal() -> None:
    goal = parse_goal_arg({"family": "SIM", "target": "tb"})
    assert isinstance(goal, TargetGoalArg) and goal.test is None
    assert "test" not in goal_arg_to_json(goal)


@pytest.mark.parametrize(
    ("test", "message"),
    [
        ("a+b+c", "more than one '\\+'"),
        ("a@1@2", "more than one '@'"),
        ("a@x0", "at least 1"),
        ("a@x1001", "exceeds the limit"),
        ("a@0", "out of range"),
        ("default", "default"),
        ("", "non-empty"),
    ],
)
def test_simulation_goal_refuses_a_malformed_selector(test: str, message: str) -> None:
    with pytest.raises(GoalArgError, match=message):
        parse_goal_arg({"family": "SIM", "target": "tb", "test": test})


@pytest.mark.parametrize("family", ["LINT", "ELAB"])
def test_only_a_simulation_goal_names_a_test(family: str) -> None:
    with pytest.raises(GoalArgError, match="unknown fields"):
        parse_goal_arg({"family": family, "target": "tb", "test": "smoke"})


def test_cycle_count_goal_accepts_a_pinned_run_and_refuses_a_seed_count() -> None:
    raw = {"family": "CYCLE_COUNT", "target": "tb", "thresholds": {"cycle_count_max": 9}}
    goal = parse_goal_arg({**raw, "test": "stress+soak@7"})
    assert isinstance(goal, CycleCountGoalArg) and goal.test == "stress+soak@7"
    with pytest.raises(GoalArgError, match="seed count"):
        parse_goal_arg({**raw, "test": "stress@x3"})
    with pytest.raises(GoalArgError, match="more than one"):
        parse_goal_arg({**raw, "test": "a+b+c"})


def test_coverage_goal_accepts_selectors_with_a_seed_count() -> None:
    raw = {"family": "COVERAGE", "target": "tb", "metrics": {"line": 80}}
    goal = parse_goal_arg({**raw, "tests": ["smoke", "stress+soak@x3", "stress@7"]})
    assert isinstance(goal, CoverageGoalArg)
    assert goal.tests == ("smoke", "stress+soak@x3", "stress@7")
    with pytest.raises(GoalArgError, match=r"tests\[1\]"):
        parse_goal_arg({**raw, "tests": ["smoke", "a@x0"]})


def test_schema_describes_the_test_run_selectors() -> None:
    variants = {v["properties"]["family"]["const"]: v for v in goal_arg_json_schema()["oneOf"]}
    assert "test" in variants["SIM"]["properties"]
    assert variants["SIM"]["required"] == ["family", "target"]
    assert "@xN" in variants["SIM"]["properties"]["test"]["description"]
    assert "test" not in variants["LINT"]["properties"]
    assert "test" not in variants["ELAB"]["properties"]
    assert "@xN" not in variants["CYCLE_COUNT"]["properties"]["test"]["description"]
    selectors = variants["COVERAGE"]["properties"]["tests"]["oneOf"][1]
    assert "@xN" in selectors["description"]

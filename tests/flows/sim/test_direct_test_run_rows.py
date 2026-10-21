"""The direct (non-campaign) Simulation path reports each Test Run (ADR 0072)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from booley.config.project_config import normalize_tests_toml
from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import SimulateFlow
from booley.flows.test_runs import build_test_run, test_run_document
from booley.runtime.endpoint_execution import EXIT_FAILURE, EXIT_SUCCESS
from tests.flows.sim.test_flow import (
    _FLOW_ENABLED,
    _make_flow,
    _use_legacy_build_transport_for_canned_flow_tests,  # noqa: F401 -- autouse fixture
)

_TESTS = {
    "lite": {
        "tests": ["smoke", "stress"],
        "select": "+test={name}",
        "variants": {
            "stress": {
                "default": "short",
                "short": ["+iters=10"],
                "soak": {"args": ["+iters=900"], "env": {"MODE": "soak"}},
            }
        },
    }
}

_SHORT = build_test_run("stress", variant="short", seed=1, args=("+iters=10",))
_SOAK_7 = build_test_run(
    "stress", variant="soak", seed=7, args=("+iters=900",), env={"MODE": "soak"}
)
_SMOKE = build_test_run("smoke", seed=1)


def _run(tmp_path: Path, *argv: str, failing: str = ""):
    """Run the direct path with a fake simulator that fails commands naming *failing*."""
    flow = _make_flow(tmp_path, config="lite", extra_args=list(argv))
    commands: list[str] = []

    def execute(command):
        commands.append(command[-1])
        failed = bool(failing) and failing in command[-1]
        verdict = "FAILED" if failed else "PASSED"
        return SubprocessResult(
            returncode=int(failed), stdout=f"[SIM_RESULT] {verdict}\n", duration_s=0.1
        )

    with (
        patch(
            "booley.flows.target_test_plan_loading.load_test_configuration",
            return_value=normalize_tests_toml(_TESTS),
        ),
        patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
        patch.object(flow, "_execute", side_effect=execute),
        flow.publication_resources,
    ):
        result = flow._run()
    report = json.loads((tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text())
    return flow, result, report, commands


def test_direct_report_names_the_test_run_of_every_result(tmp_path: Path):
    _flow, result, report, _commands = _run(tmp_path)

    assert result.exit_code == EXIT_SUCCESS
    assert report["test_runs"] == [
        {**test_run_document(_SMOKE), "grade": "pass"},
        {**test_run_document(_SHORT), "grade": "pass"},
    ]
    assert [
        (row["name"], row["test_run"], row["variant"], row["seed"], row["fingerprint"])
        for row in report["tests"]
    ] == [
        ("smoke", "smoke@1", None, 1, _SMOKE.fingerprint),
        ("stress", "stress+short@1", "short", 1, _SHORT.fingerprint),
    ]
    # A Test that ran under a Test Variant's name is listed; nothing failed,
    # so there is no rerun command.
    assert "    PASS         stress+short@1" in result.report_text.splitlines()
    assert "Rerun a failed Test Run" not in result.report_text


def test_direct_report_lists_rows_and_a_rerun_command_per_failed_run(tmp_path: Path):
    _flow, result, report, commands = _run(
        tmp_path, "--test", "stress", "--test", "stress+soak@7", failing="iters=900"
    )

    assert result.exit_code == EXIT_FAILURE
    assert len(commands) == 2
    assert report["test_runs"] == [
        {**test_run_document(_SHORT), "grade": "pass"},
        {**test_run_document(_SOAK_7), "grade": "fail"},
    ]
    lines = result.report_text.splitlines()
    start = lines.index("  Test Runs: 2 (1 passed, 1 failed)")
    assert lines[start + 1 : start + 5] == [
        "    PASS         stress+short@1",
        "    FAIL         stress+soak@7",
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target lite --test=stress+soak@7",
    ]


def test_direct_criterion_detail_carries_the_rows(tmp_path: Path):
    flow, _result, report, _commands = _run(tmp_path, failing="test=stress")

    detail = flow.state.criteria["sim_pass_lite"].detail
    assert detail["selected_tests"] == ["smoke", "stress"]
    assert detail["failed_tests"] == ["stress"]
    assert [
        {key: value for key, value in row.items() if key != "functional_failure"}
        for row in detail["test_runs"]
    ] == report["test_runs"]
    assert detail["test_runs"][-1]["functional_failure"] is True
    assert [row["grade"] for row in detail["test_runs"]] == ["pass", "fail"]


def _coverage_run(test_run, verdict: str, **attributes):
    from booley.flows.sim.coverage_campaign import CoverageRun

    return CoverageRun(
        f"001-{test_run.name}",
        test_run.test,
        verdict,  # type: ignore[arg-type]
        "collected",
        None,
        attributes,
        test_run=test_run.name,
        fingerprint=test_run.fingerprint,
        variant=test_run.variant,
        seed=test_run.seed,
    )


def test_directly_collected_coverage_reports_rows_and_rerun_commands():
    from booley.flows.sim.coverage_acceptance import coverage_test_run_rows
    from booley.flows.sim.flow import _coverage_test_run_lines

    runs = [
        _coverage_run(_SHORT, "pass"),
        _coverage_run(_SOAK_7, "fail"),
        _coverage_run(_SMOKE, "fail", failure_kind="infrastructure"),
    ]
    rows = coverage_test_run_rows((_SHORT, _SOAK_7, _SMOKE), runs)

    assert rows == [
        {**test_run_document(_SHORT), "grade": "pass"},
        {**test_run_document(_SOAK_7), "grade": "fail"},
        {**test_run_document(_SMOKE), "grade": "error"},
    ]
    assert _coverage_test_run_lines("lite", rows)[-2:] == [
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target lite --test=stress+soak@7",
    ]
    assert _coverage_test_run_lines("lite", None) == []


def test_coverage_rows_come_from_the_frozen_test_runs_only():
    from booley.flows.sim.coverage_acceptance import coverage_test_run_rows

    bare = build_test_run("smoke")
    # A run collected without frozen Test Runs is the bare Test.
    assert coverage_test_run_rows((), [_coverage_run(bare, "pass")]) == [
        {**test_run_document(bare), "grade": "pass"}
    ]
    # A run that names a Test Run nobody froze, or another fingerprint, has no row.
    assert coverage_test_run_rows((), [_coverage_run(_SOAK_7, "pass")]) == []
    changed = build_test_run("stress", variant="soak", seed=7, args=("+iters=1",))
    assert coverage_test_run_rows((changed,), [_coverage_run(_SOAK_7, "pass")]) == []


def test_rows_are_informative_relative_to_the_targets_own_default_seed():
    """A Target with ``default_seed = 7``: ``@7`` is its plain run, ``@1`` is not."""
    from booley.flows.sim.flow import (
        _campaign_test_run_lines,
        _coverage_test_run_lines,
        _effective_default_seed,
    )

    at_seven = [{**test_run_document(build_test_run("smoke", seed=7)), "grade": "pass"}]
    at_one = [{**test_run_document(_SMOKE), "grade": "pass"}]

    assert _coverage_test_run_lines("lite", at_seven, 7) == []
    assert _coverage_test_run_lines("lite", at_one, 7) == [
        "  Test Runs: 1 (1 passed)",
        "    PASS         smoke@1",
    ]
    # Without the Target's own value the Booley-wide Default Seed decides.
    assert _coverage_test_run_lines("lite", at_one) == []
    assert _coverage_test_run_lines("lite", at_seven) != []
    assert _effective_default_seed({"lite": 7}, "lite") == 7
    assert _effective_default_seed({"lite": None}, "lite") is None
    assert _effective_default_seed({}, "other") == 1
    assert _effective_default_seed(None, "other") == 1
    assert _campaign_test_run_lines("lite", object(), 7) == []  # type: ignore[arg-type]


def test_direct_run_at_a_target_default_seed_other_than_one_prints_no_rows(tmp_path: Path):
    """Regression: every plain run of such a Target printed its Test Run rows."""
    flow = _make_flow(tmp_path, config="lite", extra_args=["--test", "smoke"])
    tests = {"lite": {**_TESTS["lite"], "default_seed": 7}}
    with (
        patch(
            "booley.flows.target_test_plan_loading.load_test_configuration",
            return_value=normalize_tests_toml(tests),
        ),
        patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]}),
        patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
        patch.object(
            flow,
            "_execute",
            return_value=SubprocessResult(
                returncode=0, stdout="[SIM_RESULT] PASSED\n", duration_s=0.1
            ),
        ),
        flow.publication_resources,
    ):
        result = flow._run()

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert flow._test_runs.default_seeds == {"lite": 7}
    assert "Test Runs:" not in result.report_text
    assert "smoke@7" not in result.report_text

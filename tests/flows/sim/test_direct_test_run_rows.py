"""A Simulation Record reports each Test Run (ADR 0072)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.test_runs import build_test_run, test_run_document
from booley.runtime.endpoint_execution import EXIT_FAILURE, EXIT_SUCCESS
from tests.flows.sim.simulation_record_boundary import (
    Launch,
    SimulationRecordSimRun,
    run_simulation_record_sim,
)

_TESTS_HEAD = '[lite]\ntests = ["smoke", "stress"]\nselect = "+test={name}"\n'
_TESTS_VARIANTS = (
    '[lite.variants.stress]\ndefault = "short"\nshort = ["+iters=10"]\n'
    'soak = { args = ["+iters=900"], env = { MODE = "soak" } }\n'
)

_SHORT = build_test_run("stress", variant="short", seed=1, args=("+iters=10",))
_SOAK_7 = build_test_run(
    "stress", variant="soak", seed=7, args=("+iters=900",), env={"MODE": "soak"}
)
_SMOKE = build_test_run("smoke", seed=1)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *tests: str,
    failing: str = "",
    default_seed: int | None = None,
    **request: Any,
) -> tuple[SimulationRecordSimRun, dict[str, Any]]:
    """Run ``lite`` on the record path; a fake simulator fails launches whose document names *failing*."""

    def process(launch: Launch) -> SubprocessResult:
        failed = bool(failing) and any(failing in arg for arg in launch.work.plusargs)
        verdict = "FAILED" if failed else "PASSED"
        return SubprocessResult(
            returncode=int(failed), stdout=f"[SIM_RESULT] {verdict}\n", duration_s=0.1
        )

    seed = f"default_seed = {default_seed}\n" if default_seed is not None else ""
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        process,
        files={".booley/tests.toml": _TESTS_HEAD + seed + _TESTS_VARIANTS},
        target="lite",
        test=tests or None,
        **request,
    )
    report = json.loads(
        (tmp_path / "reports/sim/1/targets/lite/simulation.json").read_text(encoding="utf-8")
    )
    return run, report


def test_simulation_record_report_names_the_test_run_of_every_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, report = _run(tmp_path, monkeypatch)
    text = run.result.outcome.report_text

    assert run.result.exit_code == EXIT_SUCCESS
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
    assert "    PASS         stress+short@1" in text.splitlines()
    assert "Rerun a failed Test Run" not in text


def test_simulation_record_report_lists_rows_and_a_rerun_command_per_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, report = _run(tmp_path, monkeypatch, "stress", "stress+soak@7", failing="iters=900")

    assert run.result.exit_code == EXIT_FAILURE
    assert len(run.launches) == 2
    assert report["test_runs"] == [
        {**test_run_document(_SHORT), "grade": "pass"},
        {**test_run_document(_SOAK_7), "grade": "fail"},
    ]
    lines = run.result.outcome.report_text.splitlines()
    start = lines.index("  Test Runs: 2 (1 passed, 1 failed)")
    assert lines[start + 1 : start + 3] == [
        "    PASS         stress+short@1",
        "    FAIL         stress+soak@7",
    ]
    # The failed run names its log, the passing one prints no path.
    assert lines[start + 3].startswith("      log: ")
    assert lines[start + 4 : start + 6] == [
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target lite --test=stress+soak@7",
    ]


def _coverage_run(test_run, verdict: str, **attributes):
    from booley.flows.sim.coverage_record import CoverageRun

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


def test_coverage_rows_and_rerun_commands():
    from booley.flows.sim.coverage_acceptance import coverage_test_run_rows

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


def test_coverage_rows_come_from_the_planned_test_runs_only():
    from booley.flows.sim.coverage_acceptance import coverage_test_run_rows

    bare = build_test_run("smoke")
    # A run collected without planned Test Runs is the bare Test.
    assert coverage_test_run_rows((), [_coverage_run(bare, "pass")]) == [
        {**test_run_document(bare), "grade": "pass"}
    ]
    # A run that names a Test Run nobody planned, or another fingerprint, has no row.
    assert coverage_test_run_rows((), [_coverage_run(_SOAK_7, "pass")]) == []
    changed = build_test_run("stress", variant="soak", seed=7, args=("+iters=1",))
    assert coverage_test_run_rows((changed,), [_coverage_run(_SOAK_7, "pass")]) == []


def test_report_wording_falls_back_to_the_booley_wide_default_seed():
    # Private seam: only the unit reaches a Target whose Default Seed the report never
    # learned (None map, unknown selector) and an outcome without a manifest path.
    from booley.flows.sim.record_presentation import (
        _effective_default_seed,
        _simulation_record_test_run_lines,
    )

    assert _effective_default_seed({"lite": None}, "lite") is None
    assert _effective_default_seed({}, "other") == 1
    assert _effective_default_seed(None, "other") == 1
    assert _simulation_record_test_run_lines("lite", object(), 7) == []  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("test", "rows"),
    [("smoke", None), ("smoke@1", ["  Test Runs: 1 (1 passed)", "    PASS         smoke@1"])],
    ids=["own-default-seed", "other-seed"],
)
def test_rows_are_informative_relative_to_the_targets_own_default_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test: str, rows: list[str] | None
):
    """A Target with ``default_seed = 7``: ``@7`` is its plain run, ``@1`` is not.

    Regression: every plain run of such a Target printed its Test Run rows.
    """
    run, report = _run(tmp_path, monkeypatch, test, default_seed=7)
    text = run.result.outcome.report_text

    assert run.result.exit_code == EXIT_SUCCESS, text
    if rows is None:
        assert [r["name"] for r in report["test_runs"]] == ["smoke@7"]
        assert "Test Runs:" not in text
        assert "smoke@7" not in text
    else:
        assert [r["name"] for r in report["test_runs"]] == ["smoke@1"]
        assert all(line in text.splitlines() for line in rows)


def _card_path(text: str, label: str) -> Path:
    """The path of the one ``<label>:`` line of the card."""
    lines = [line for line in text.splitlines() if line.startswith(f"      {label}: ")]
    assert len(lines) == 1, f"expected one {label}: line on the card, found {lines}"
    return Path(lines[0].split(": ", 1)[1])


def test_simulation_record_report_and_rows_carry_each_test_runs_evidence_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.flows.sim.record.projection import decode_simulation_projection

    run, report = _run(tmp_path, monkeypatch, "stress", "stress+soak@7", failing="iters=900")
    target = tmp_path / "reports/sim/1/targets/lite"
    text = run.result.outcome.report_text
    mcp = json.loads((tmp_path / "reports/sim/1/report.json").read_text(encoding="utf-8"))
    observations = mcp["detail"]["records"]["lite"]["observations"]

    # Card: the FAIL row names its log; the PASS row prints nothing.
    log = _card_path(text, "log")
    assert "waveform:" not in text
    # simulation.json and the MCP rows carry both paths for every Test Run, passing included.
    for rows in (report["tests"], observations):
        assert [(row["trace"], (target / row["log"]).is_file()) for row in rows] == [
            (None, True),
            (None, True),
        ]
    assert (target / report["tests"][1]["log"]).resolve() == log.resolve()
    assert observations[1]["log"] == report["tests"][1]["log"]
    # The Test Run rows rerun matching reads are untouched, and the file still decodes.
    assert all({"log", "trace"}.isdisjoint(row) for row in report["test_runs"])
    decode_simulation_projection(report, coverage_expected=False)

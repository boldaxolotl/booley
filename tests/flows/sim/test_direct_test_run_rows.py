"""A Simulation Campaign reports each Test Run (ADR 0072)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.base import SubprocessResult
from booley.flows.test_runs import DEFAULT_SEED, build_test_run, test_run_document
from booley.runtime.endpoint_execution import EXIT_FAILURE, EXIT_SUCCESS
from tests.flows.sim.campaign_boundary import CampaignSimRun, Launch, run_campaign_sim
from tests.flows.sim.test_coverage_flow import _AcceptanceAdapter
from tests.flows.sim.test_flow import _ticket_state

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
) -> tuple[CampaignSimRun, dict[str, Any]]:
    """Run ``lite`` on the campaign path; a fake simulator fails launches whose document names *failing*."""

    def process(launch: Launch) -> SubprocessResult:
        failed = bool(failing) and any(failing in arg for arg in launch.work.plusargs)
        verdict = "FAILED" if failed else "PASSED"
        return SubprocessResult(
            returncode=int(failed), stdout=f"[SIM_RESULT] {verdict}\n", duration_s=0.1
        )

    seed = f"default_seed = {default_seed}\n" if default_seed is not None else ""
    run = run_campaign_sim(
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


def test_campaign_report_names_the_test_run_of_every_result(
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


def test_campaign_report_lists_rows_and_a_rerun_command_per_failed_run(
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


def test_campaign_criterion_detail_carries_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """In Ticket mode the Criterion's campaign detail carries the same rows."""
    state_path = _ticket_state(tmp_path, monkeypatch, ["sim_pass_lite"])
    recorder = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    _, report = _run(tmp_path, monkeypatch, failing="test=stress", adapter=recorder)
    detail = DevelopmentState.load(state_path).criteria["sim_pass_lite"].detail

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


def test_coverage_rows_and_rerun_commands():
    from booley.flows.sim.campaign.test_run_report import informative_test_run_lines
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
    assert informative_test_run_lines("lite", rows, default_seed=DEFAULT_SEED)[-2:] == [
        "  Rerun a failed Test Run with the same seed:",
        "    booley flow sim --target lite --test=stress+soak@7",
    ]


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
    from booley.flows.sim.campaign.test_run_report import informative_test_run_lines
    from booley.flows.sim.campaign_presentation import (
        _campaign_test_run_lines,
        _effective_default_seed,
    )

    at_seven = [{**test_run_document(build_test_run("smoke", seed=7)), "grade": "pass"}]
    at_one = [{**test_run_document(_SMOKE), "grade": "pass"}]

    assert informative_test_run_lines("lite", at_seven, default_seed=7) == []
    assert informative_test_run_lines("lite", at_one, default_seed=7) == [
        "  Test Runs: 1 (1 passed)",
        "    PASS         smoke@1",
    ]
    assert _effective_default_seed({"lite": 7}, "lite") == 7
    assert _effective_default_seed({"lite": None}, "lite") is None
    assert _effective_default_seed({}, "other") == 1
    assert _effective_default_seed(None, "other") == 1
    assert _campaign_test_run_lines("lite", object(), 7) == []  # type: ignore[arg-type]


def test_campaign_run_at_a_target_default_seed_other_than_one_prints_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression: every plain run of such a Target printed its Test Run rows."""
    run, report = _run(tmp_path, monkeypatch, "smoke", default_seed=7)
    text = run.result.outcome.report_text

    assert [r["name"] for r in report["test_runs"]] == ["smoke@7"]

    assert run.result.exit_code == EXIT_SUCCESS, text
    assert "Test Runs:" not in text
    assert "smoke@7" not in text


def _traced_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failing: bool
) -> tuple[CampaignSimRun, dict[str, Any]]:
    """Run Test ``smoke`` with ``--trace``; the fake simulator dumps a waveform and reports it."""

    def process(launch: Launch) -> SubprocessResult:
        dump = launch.cwd / "smoke.fst"
        dump.write_bytes(b"waveform")
        verdict = "FAILED" if failing else "PASSED"
        return SubprocessResult(
            returncode=int(failing),
            stdout=f"[SIM_RESULT] {verdict}\nTRACE_OK: {dump}\n",
            duration_s=0.1,
        )

    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke"]},
        process,
        files={".booley/tests.toml": '[lite]\ntests = ["smoke"]\nselect = "+test={name}"\n'},
        target="lite",
        trace=True,
    )
    return run, _simulation_json(tmp_path)


def _simulation_json(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "reports/sim/1/targets/lite/simulation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _card_path(text: str, label: str) -> Path:
    """The path of the one ``<label>:`` line of the card."""
    lines = [line for line in text.splitlines() if line.startswith(f"      {label}: ")]
    assert len(lines) == 1, f"expected one {label}: line on the card, found {lines}"
    return Path(lines[0].split(": ", 1)[1])


def test_campaign_report_and_rows_carry_each_test_runs_evidence_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.flows.sim.campaign.projection import decode_simulation_projection

    run, report = _run(tmp_path, monkeypatch, "stress", "stress+soak@7", failing="iters=900")
    target = tmp_path / "reports/sim/1/targets/lite"
    text = run.result.outcome.report_text
    mcp = json.loads((tmp_path / "reports/sim/1/report.json").read_text(encoding="utf-8"))
    observations = mcp["detail"]["campaigns"]["lite"]["observations"]

    # Card: the FAIL row names its log; the PASS row prints nothing.
    log = _card_path(text, "log")
    assert log.is_file()
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


def test_traced_failure_reports_its_captured_waveform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, report = _traced_run(tmp_path, monkeypatch, failing=True)
    target = tmp_path / "reports/sim/1/targets/lite"
    waveform = _card_path(run.result.outcome.report_text, "waveform")

    assert waveform.is_file()
    assert _card_path(run.result.outcome.report_text, "log").is_file()
    (row,) = report["tests"]
    assert (target / row["trace"]).resolve() == waveform.resolve()
    assert (target / row["log"]).is_file()


def test_traced_passing_run_shows_its_waveform_on_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, report = _traced_run(tmp_path, monkeypatch, failing=False)
    text = run.result.outcome.report_text
    target = tmp_path / "reports/sim/1/targets/lite"
    waveform = _card_path(text, "waveform")

    assert run.result.exit_code == EXIT_SUCCESS, text
    assert "  Test Runs: 1 (1 passed)" in text
    assert "      log: " not in text
    assert waveform.is_file()
    assert (target / report["tests"][0]["trace"]).resolve() == waveform.resolve()

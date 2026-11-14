"""A clean-checkout Flow run returns its typed values or why it has none (issue #3950).

Each test runs the real ``sim`` or ``lint`` Flow in-process with only the process edge faked,
so it writes the ``report.json`` the checkout runner reads. It then calls the runner's
``_result_from`` on that report directory, as the runner does after its child exits. The
child-process half is covered with a stub Flow in ``test_checkout_flow.py``. The runner
modules are imported inside the test bodies so a missing one fails the test, not collection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.lint.flow import LintFlow
from booley.flows.lint.request import LintRequest
from booley.flows.sim.backends.cocotb_results import find_import_failure
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.runtime import job_records
from tests.flows.conftest import _flow_project_root, _set_booley_dir, _set_flow_project_root
from tests.flows.lint.test_flow import _REAL_CATALOG_BUILD, _stub_resolved, _target_handle
from tests.flows.sim.conftest import (
    bare_identity_for_targets_without_a_core,
    declared_trace_files_convert_after_the_run,
    explicit_surface_for_lightweight_target_handles,
)
from tests.flows.sim.simulation_record_boundary import (
    FAIL_PROCESS,
    PASS_PROCESS,
    run_campaign_sim,
)
from tests.flows.sim.simulation_record_test_support import _observation, _simulation_outcome
from tests.flows.sim.test_cocotb import _cocotb_output, _run_cocotb
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_sim_flow_metrics import _MixedCoverageExecution
from tests.target_test_support import install_lenient_target_catalog

if TYPE_CHECKING:
    from booley.harness.checkout_flow import CheckoutFlowResult

# The conftest fixtures the Flow tests rely on are imported so they apply here too.
__all__ = [
    "_flow_project_root",
    "_set_booley_dir",
    "_set_flow_project_root",
    "bare_identity_for_targets_without_a_core",
    "declared_trace_files_convert_after_the_run",
    "explicit_surface_for_lightweight_target_handles",
]

RUN_ID = "run-metrics-1"
LINT_WARNINGS = (
    "%Warning-UNUSEDSIGNAL: rtl/mod_a.sv:42:5: Signal is not used: 'foo'\n"
    "%Warning-UNDRIVEN: rtl/mod_a.sv:100:3: Signal is not driven: 'bar'\n"
)
LINT_ERROR = "%Error: rtl/mod_a.sv:3:1: syntax error\n"


def checkout_result(outer: Path, flow: str, exit_code: int) -> CheckoutFlowResult:
    """The runner's view of a *flow* run whose reports are under ``outer/flow-reports``."""
    from booley.harness import checkout_flow

    final = job_records.JobRecord(RUN_ID, flow, "2026-11-13T00:00:00Z", 60)
    final.status = "done" if exit_code == 0 else "failed"
    final.exit_code = exit_code
    outer.mkdir(parents=True, exist_ok=True)
    log = outer / "job.log"
    log.write_bytes(b"")
    request = checkout_flow.CheckoutFlowRequest(outer, outer, outer, flow, "t")
    return checkout_flow._result_from(request, RUN_ID, final, log, outer, timed_out=False)


def sim_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process: SubprocessResult,
    *,
    tests: list[str] | None = None,
    build: SubprocessResult | None = None,
) -> CheckoutFlowResult:
    """Run the ``sim`` Flow for Target ``lite`` and return what the runner would."""
    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    outer = tmp_path / "out"
    work = tmp_path / "work"
    work.mkdir(parents=True)
    run = run_campaign_sim(
        work,
        monkeypatch,
        {"lite": tests or ["t1"]},
        process,
        build=build,
        report_dir=outer / "flow-reports",
        target="lite",
    )
    return checkout_result(outer, "sim", run.result.exit_code)


def lint_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: MagicMock | None = None,
    *,
    prepare_error: Exception | None = None,
) -> CheckoutFlowResult:
    """Run the ``lint`` Flow for Target ``lite`` with a faked linter and return the runner's view."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")  # a Flow runs only inside the Sandbox
    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    outer = tmp_path / "out"
    work = tmp_path / "work"
    work.mkdir(parents=True)
    prepared = (
        {"side_effect": prepare_error}
        if prepare_error is not None
        else {"return_value": (["verilator", "--lint-only"], _stub_resolved())}
    )
    with monkeypatch.context() as scoped:
        install_lenient_target_catalog(
            scoped,
            real_catalog_build=_REAL_CATALOG_BUILD,
            fallback_handle=lambda root, token: _target_handle(token, project_root=root),
        )
        with (
            patch.object(LintFlow, "_prepare_lint_command", **prepared),
            patch.object(LintFlow, "_execute", return_value=execution),
        ):
            outcome = LintFlow().execute(
                LintRequest(target="lite", work_dir=work, report_dir=outer / "flow-reports")
            )
    return checkout_result(outer, "lint", outcome.exit_code)


def linter_output(stdout: str, *, returncode: int = 0, timed_out: bool = False) -> MagicMock:
    """What the faked linter process returned."""
    return MagicMock(
        returncode=returncode, stdout=stdout, stderr="", timed_out=timed_out, duration_s=0.5
    )


def coverage_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution: Any
) -> CheckoutFlowResult:
    """Run ``sim --coverage`` on a two-test Target and return the runner's view."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    project(work)
    data = work / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    outer = tmp_path / "out"
    flow = SimulateFlow(coverage_execution=lambda *_args: execution)
    result = flow.execute(
        SimRequest(target="sim_0", work_dir=work, coverage=True, report_dir=outer / "flow-reports")
    )
    return checkout_result(outer, "sim", result.exit_code)


def judged(result: CheckoutFlowResult, rules_flow: str, key: str, metric: str) -> tuple[Any, int]:
    """Judge ``<metric>_max = 0`` against a Target's returned values (or its excerpt)."""
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.verdicts import (
        FlowFailed,
        FlowValues,
        evaluate_table,
        measure_exit_code,
    )

    rules = TableRules(
        "t", "vlnv:x#t", rules_flow, (Bound(key, metric, "max", 0, None, None, None),), ()
    )
    if result.metrics:
        (values,) = result.metrics.values()
        outcome: Any = FlowValues(values, values.clocks)
    else:
        assert result.excerpt is not None
        outcome = FlowFailed(result.excerpt)
    verdicts = evaluate_table(rules, outcome)
    return verdicts, measure_exit_code(v.verdict for v in verdicts)


def test_sim_and_lint_runs_return_their_typed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = sim_result(tmp_path / "sim", monkeypatch, PASS_PROCESS)
    lint = lint_result(tmp_path / "lint", monkeypatch, linter_output(LINT_WARNINGS))

    (sim_values,) = sim.metrics.values()
    assert sim_values.plain == {"tests_failed": 0, "tests_total": 1}
    assert sim_values.per_test == {"cycle_count": {"t1@1": 2561}}
    (lint_values,) = lint.metrics.values()
    assert lint_values.plain == {"warnings": 2, "errors": 0}
    assert (sim.unavailable, lint.unavailable) == ({}, {})


def test_coverage_run_returns_coverage_percents_with_test_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = coverage_result(tmp_path, monkeypatch, _MixedCoverageExecution())

    (values,) = result.metrics.values()
    assert values.plain == {
        "tests_failed": 0,
        "tests_total": 2,
        "line_coverage": 50.0,
        "branch_coverage": 75.0,
        "expression_coverage": 100.0,
        "toggle_coverage": 200 / 3,
        "cover_property_coverage": 20.0,
    }


def test_missing_tool_and_cocotb_import_failure_return_no_values_and_an_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.verdicts import FlowFailed, evaluate_table, measure_exit_code

    # What the sandbox prints with verilator off PATH, in a campaign of 12 Test Runs.
    build = SubprocessResult(
        returncode=127,
        stdout=(
            "/bin/sh: 1: verilator: not found\n"
            "make: *** [Makefile:9: Vtop] Error 127\n"
            "ERROR: Verilator elaboration failed (rc=127)\n"
        ),
    )
    missing = sim_result(
        tmp_path / "missing",
        monkeypatch,
        PASS_PROCESS,
        tests=[f"t{index}" for index in range(12)],
        build=build,
    )
    import_failure = tmp_path / "cocotb"
    import_failure.mkdir()
    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    noise = "Failed to import module tb_top: ModuleNotFoundError: No module named 'pyuvm'\n"
    # The run half swaps "results.xml not found" for the import failure it finds in the output.
    detail = find_import_failure(noise)
    stdout = noise + _cocotb_output([], state="missing", detail=detail, passed=False)
    _run_cocotb(
        import_failure,
        monkeypatch,
        stdout,
        report_dir=import_failure / "out" / "flow-reports",
    )
    cocotb = checkout_result(import_failure / "out", "sim", 1)

    rules = TableRules(
        "t",
        "vlnv:x#t",
        "sim",
        (Bound("tests_total_min", "tests_total", "min", 1, None, None, None),),
        (),
    )
    for result, fragments in (
        (missing, ("missing tool: verilator",)),
        (cocotb, ("tb_top", "pyuvm")),
    ):
        assert result.metrics == {}
        assert result.excerpt is not None
        assert all(fragment in result.excerpt for fragment in fragments), result.excerpt
        verdicts = evaluate_table(rules, FlowFailed(result.excerpt))
        assert [v.detail for v in verdicts] == [result.excerpt]
        assert measure_exit_code(v.verdict for v in verdicts) == 2
    (reason,) = cocotb.unavailable.values()
    assert "tb_top" in reason and "pyuvm" in reason


def unfinished_campaign_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observations: list[dict[str, Any]],
    *,
    complete: bool,
) -> CheckoutFlowResult:
    """The runner's view of a ``sim`` run that stopped, reported by the production failure path."""
    monkeypatch.setenv("BOOLEY_RUN_ID", RUN_ID)
    outcome = _simulation_outcome(tmp_path, observations, complete=complete)
    reported = SimulateFlow()._ordinary_campaign_failure(RuntimeError("stopped"), [outcome])
    report = tmp_path / "out" / "flow-reports" / "sim" / "1" / "report.json"
    report.parent.mkdir(parents=True)
    document = {"run_id": RUN_ID, "detail": reported.detail, "report_text": reported.report_text}
    report.write_text(json.dumps(document), encoding="utf-8")
    return checkout_result(tmp_path / "out", "sim", reported.exit_code)


def test_target_whose_values_cannot_be_produced_returns_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errored = _observation("t1") | {
        "execution": "setup_error",
        "failure_class": "infrastructure",
        "functional": "not_observed",
        "detail": {"reason": "simulator build crashed"},
    }
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cases = {
        "did not finish": unfinished_campaign_result(
            tmp_path / "a", monkeypatch, [_observation("t1")], complete=False
        ),
        "infrastructure error": unfinished_campaign_result(
            tmp_path / "b", monkeypatch, [errored], complete=True
        ),
        "Timed out": lint_result(
            tmp_path / "c", monkeypatch, linter_output("", returncode=-9, timed_out=True)
        ),
        "lint setup failed": lint_result(
            tmp_path / "d", monkeypatch, prepare_error=RuntimeError("broken setup")
        ),
    }

    for fragment, result in cases.items():
        assert result.unavailable, (fragment, result)
        (target,) = result.unavailable
        assert fragment in result.unavailable[target], fragment
        assert target not in result.metrics, fragment
        assert result.excerpt is not None and fragment in result.excerpt, fragment
    (reason,) = cases["infrastructure error"].unavailable.values()
    assert "simulator build crashed" in reason


def test_failing_test_and_lint_errors_return_values_judged_unmet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus
    from booley.measurement.run_document import Verdict

    cases: list[tuple[CheckoutFlowResult, str, str, str, int]] = [
        (
            sim_result(tmp_path / "sim", monkeypatch, FAIL_PROCESS),
            "sim",
            "tests_failed_max",
            "tests_failed",
            1,
        ),
        (
            lint_result(tmp_path / "lint", monkeypatch, linter_output(LINT_ERROR, returncode=1)),
            "lint",
            "errors_max",
            "errors",
            1,
        ),
    ]
    for result, flow, key, metric, count in cases:
        assert result.status is CheckoutFlowStatus.FAILED
        (values,) = result.metrics.values()
        assert values.plain[metric] == count
        verdicts, exit_code = judged(result, flow, key, metric)
        assert [v.verdict for v in verdicts] == [Verdict.UNMET]
        assert exit_code == 1

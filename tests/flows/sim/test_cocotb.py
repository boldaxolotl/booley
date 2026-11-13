"""SimulateFlow Cocotb Target tests (ADR 0034 — G3, G4, B2, B3, B4, G6).

Host-runnable: the Cocotb Target is simulated through the real campaign path
(``SimulateFlow.execute``) with only the process edge faked
(``campaign_boundary``). The fake process prints canned run-half output
([COCOTB_RESULTS] + [SIM_SUMMARY] lines built with the real formatters) and the
production Cocotb adapter's verdict logic turns it into the adapter result. The
real icarus/verilator cocotb runs are the Sandbox e2e's job (G8-G15).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.backends import cocotb_results as cr
from booley.flows.sim.execution import engine as execution_engine
from booley.flows.sim.result import format_summary
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from tests.flows.sim.campaign_boundary import CampaignSimRun, run_campaign_sim
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.test_flow import SimulateFlow

# A .core declaring a Cocotb Target (flow_options.cocotb_module — decision 2).
_COCOTB_CORE_TEXT = """\
CAPI=2:
name: ::ccfg:0
description: cocotb simulate fixture
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/test_counter.py: {file_type: user}
    tags: [tb]
targets:
  ccfg:
    filesets: [rtl, tb]
    toplevel: counter
    flow: sim
    flow_options:
      tool: icarus
      cocotb_module: test_counter
      iverilog_options: [-g2012]
"""

_TESTS = {"ccfg": ["test_reset", "test_count", "test_fail_assert"]}

_PASSING = [
    ("test_reset", "pass", ""),
    ("test_count", "pass", ""),
    ("test_fail_assert", "pass", ""),
]
# The sources the Cocotb core names; the faked build never reads them.
_CORE_FILES = {
    "ccfg.core": _COCOTB_CORE_TEXT,
    "rtl/counter.sv": "module counter; endmodule\n",
    "tb/test_counter.py": "",
}


def _cocotb_output(
    entries: list[tuple[str, str, str]],
    *,
    state: str = cr.STATE_OK,
    detail: str = "",
    sva_lines: str = "",
    sva_errors: int = 0,
    passed: bool | None = None,
) -> str:
    """Canned run-half stdout: sim noise + [COCOTB_RESULTS] + [SIM_SUMMARY].

    *sva_errors* rides the [SIM_SUMMARY] line; the campaign adapter ignores it
    and recounts SVA error lines over the run output itself.
    """
    results = cr.CocotbResults(
        state=state,
        detail=detail,
        tests=tuple(
            cr.CocotbTest(name=n, module="test_counter", status=s, failure_text=f)
            for n, s, f in entries
        ),
    )
    if passed is None:
        passed = bool(entries) and all(s == "pass" for _, s, _ in entries)
    return (
        f"sim noise\n{sva_lines}"
        f"{cr.format_results_line(cr.results_payload(results))}\n"
        f"{format_summary(passed, sva_errors)}\n"
    )


def _run_cocotb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str = "",
    *,
    returncode: int = 0,
    files: dict[str, str] | None = None,
    build: SubprocessResult | None = None,
    skip: dict[str, list[str]] | None = None,
    **request: Any,
) -> CampaignSimRun:
    """Run the Cocotb Target ``ccfg`` through the campaign with *stdout* as the run-half output."""
    process = SubprocessResult(returncode=returncode, stdout=stdout, duration_s=2.0)
    return run_campaign_sim(
        tmp_path,
        monkeypatch,
        _TESTS,
        process,
        files={**_CORE_FILES, **(files or {})},
        build=build,
        skip=skip,
        target="ccfg",
        **request,
    )


def _projection(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "reports/sim/1/targets/ccfg/simulation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _grades(tmp_path: Path) -> dict[str, str]:
    """Each Test's Test Run grade in the projection."""
    return {row["test"]: row["grade"] for row in _projection(tmp_path)["test_runs"]}


def _error_tails(tmp_path: Path) -> dict[str, str]:
    return {test["name"]: test["error_tail"] for test in _projection(tmp_path)["tests"]}


# ---------------------------------------------------------------------------
# G3 — verdict matrix (the two cocotb traps front and center)
# ---------------------------------------------------------------------------

_ALL_PASS = {name: "pass" for name, _status, _text in _PASSING}
_ALL_FAIL = dict.fromkeys(_ALL_PASS, "fail")
_ALL_INCONCLUSIVE = dict.fromkeys(_ALL_PASS, "inconclusive")
_SVA_LINE = "ERROR: rtl/counter.sv:18: counter reached error trap\n"
_ZERO_MATCH_IDS = ["test_reset/ifg=12", "test_reset/ifg=0"]
_ZERO_MATCH_RESULTS = cr.CocotbResults(
    state=cr.STATE_OK,
    tests=tuple(cr.CocotbTest(name, "test_counter", "skipped") for name in _ZERO_MATCH_IDS),
)


@pytest.mark.parametrize(
    ("stdout", "returncode", "request_args", "exit_code", "grades", "fragments"),
    [
        pytest.param(_cocotb_output(_PASSING), 0, {}, EXIT_SUCCESS, _ALL_PASS, (), id="pass"),
        pytest.param(
            # Regression (G3): exit code 0 with a failing test in the XML is FAIL;
            # the siblings stay pass (G9 shape) and the cocotb failure text is surfaced.
            _cocotb_output(
                [
                    *_PASSING[:2],
                    (
                        "test_fail_assert",
                        "fail",
                        "AssertionError: deliberate failure: count should be 0",
                    ),
                ],
                passed=False,
            ),
            0,
            {},
            EXIT_FAILURE,
            {**_ALL_PASS, "test_fail_assert": "fail"},
            ("deliberate failure",),
            id="fail",
        ),
        pytest.param(
            # Regression (G3): exit code 0 with no results.xml is INCONCLUSIVE.
            _cocotb_output(
                [],
                state=cr.STATE_MISSING,
                detail="results.xml not found at /b/results.xml",
                passed=False,
            ),
            0,
            {},
            EXIT_FAILURE,
            _ALL_INCONCLUSIVE,
            ("INCONCLUSIVE", "results.xml not found"),
            id="missing",
        ),
        pytest.param(
            # test_fail_assert is absent from the XML.
            _cocotb_output(_PASSING[:2], passed=False),
            0,
            {},
            EXIT_FAILURE,
            {**_ALL_PASS, "test_fail_assert": "inconclusive"},
            ("no matching test ID", '"test_reset", "test_count"'),
            id="absent",
        ),
        pytest.param(
            _cocotb_output(
                [],
                state=cr.STATE_UNPARSEABLE,
                detail="results.xml is truncated or malformed: no element found",
                passed=False,
            ),
            0,
            {},
            EXIT_FAILURE,
            _ALL_INCONCLUSIVE,
            ("INCONCLUSIVE", "truncated or malformed"),
            id="truncated",
        ),
        pytest.param(
            cr.format_results_line(
                cr.results_payload(cr.compact_results(_ZERO_MATCH_RESULTS, ["test_reset"]))
            )
            + "\n"
            + format_summary(False, 0, inconclusive=True)
            + "\n",
            1,
            {"test": ("test_reset",)},
            EXIT_FAILURE,
            {"test_reset": "inconclusive"},
            ("INCONCLUSIVE", *_ZERO_MATCH_IDS),
            id="zero-match",
        ),
        pytest.param(
            # An extra XML test is logged, never verdict-bearing: selected set only.
            _cocotb_output([*_PASSING, ("test_surprise", "pass", "")]),
            0,
            {},
            EXIT_SUCCESS,
            _ALL_PASS,
            (),
            id="extra",
        ),
        pytest.param(
            # C3: output scanning retained: RTL $fatal under a green XML fails the batch.
            _cocotb_output(_PASSING, sva_lines=_SVA_LINE, sva_errors=1, passed=False),
            0,
            {},
            EXIT_FAILURE,
            _ALL_FAIL,
            ("1 assertion/fatal error message",),
            id="sva",
        ),
        pytest.param(
            # G3 matrix: an all-pass XML after a failed-verdict run stays FAIL; the
            # production adapter turns every passing test into a failure.
            _cocotb_output(_PASSING, passed=False),
            1,
            {},
            EXIT_FAILURE,
            _ALL_FAIL,
            ("simulator process failed outside Cocotb results",),
            id="nonzero-rc",
        ),
        pytest.param(
            "sim noise\nERROR: cocotb simulation timed out (5s)\n"
            + _cocotb_output(
                [
                    ("test_reset", "pass", ""),
                    ("test_count", "fail", "SimFailure: Simulator shut down prematurely"),
                ],
                passed=False,
            ),
            1,
            {},
            EXIT_FAILURE,
            {**_ALL_PASS, "test_count": "fail", "test_fail_assert": "fail"},
            ("cocotb simulation timed out",),
            id="timeout",
        ),
        pytest.param(
            # The run died before the run-half's post-processing (e.g. an outer
            # kill): no [COCOTB_RESULTS], no [SIM_SUMMARY].
            "bare sim noise, rc 0\n",
            0,
            {},
            EXIT_FAILURE,
            _ALL_INCONCLUSIVE,
            ("INCONCLUSIVE",),
            id="no-results",
        ),
    ],
)
def test_cocotb_campaign_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    request_args: dict[str, Any],
    exit_code: int,
    grades: dict[str, str],
    fragments: tuple[str, ...],
) -> None:
    run = _run_cocotb(tmp_path, monkeypatch, stdout, returncode=returncode, **request_args)

    text = run.result.outcome.report_text
    assert run.result.exit_code == exit_code, text
    assert _grades(tmp_path) == grades
    for fragment in fragments:
        assert fragment in text, fragment
    assert "sva_errors=" not in text  # the raw summary key never leaks into the report
    assert [test["name"] for test in _projection(tmp_path)["tests"]] == list(grades)


def test_cocotb_timeout_marks_unfinished_tests_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = "sim noise\nERROR: cocotb simulation timed out (5s)\n" + _cocotb_output(
        [("test_reset", "pass", "")], passed=False
    )
    _run_cocotb(tmp_path, monkeypatch, stdout, returncode=1)

    # Finished-and-passed stays pass; the active and later selected tests read
    # timeout, and the timeout reason rides their error tail.
    executions = {test["name"]: test["execution"] for test in _projection(tmp_path)["tests"]}
    assert executions == {
        "test_reset": "completed",
        "test_count": "timeout",
        "test_fail_assert": "timeout",
    }
    assert "cocotb simulation timed out" in _error_tails(tmp_path)["test_fail_assert"]


def test_cocotb_named_cycles_within_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _cocotb_output(_PASSING) + "CYCLES test_count 17\nCYCLES test_reset 3\n"
    run = _run_cocotb(
        tmp_path,
        monkeypatch,
        stdout,
        files={".booley/booley.toml": '[flows.sim]\ncycle_sentinels = ["CYCLES"]\n'},
    )

    assert "test_count: cycles=17" in run.result.outcome.report_text
    cycles = {test["name"]: test["cycles"] for test in _projection(tmp_path)["tests"]}
    assert cycles == {"test_reset": 3, "test_count": 17, "test_fail_assert": None}


# ---------------------------------------------------------------------------
# B2 — batching: one prepared command, --test set filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_cocotb_batch_launch_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verbosity: str
) -> None:
    """One launch for the whole set, its document naming the adapter, module and grace."""
    request = {} if verbosity == "compact" else {"result_verbosity": "full"}
    # F-25: the frozen-clock watchdog knob crosses into the sandbox.
    with patch("booley.flows.sim.execution.engine.resolve_sim_time_grace_s", return_value=42.0):
        run = _run_cocotb(tmp_path, monkeypatch, _cocotb_output(_PASSING), **request)

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    (launch,) = run.launches  # one build + one sim process for 3 tests
    work = launch.work
    assert work.adapter == "cocotb"
    assert work.tests == tuple(_TESTS["ccfg"])
    assert work.cocotb_module == "test_counter"
    assert work.result_verbosity == verbosity
    assert work.adapter_result_path
    assert work.sim_time_grace_s == 42.0


def test_cocotb_trace_scope_reaches_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run_cocotb(tmp_path, monkeypatch, _cocotb_output(_PASSING), trace=True)

    # The fake process writes no waveform, so the grade is not asserted.
    (launch,) = run.launches
    assert (launch.work.trace, launch.work.trace_scope) == (True, "counter")


@pytest.mark.parametrize(
    ("request_args", "skip", "expected"),
    [
        ({"test": ("test_count",)}, None, ("test_count",)),
        # A configured skip prunes the set (it never empties it).
        ({}, {"ccfg": ["test_fail_assert"]}, ("test_reset", "test_count")),
    ],
    ids=["filter", "skip"],
)
def test_cocotb_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_args: dict[str, Any],
    skip: dict[str, list[str]] | None,
    expected: tuple[str, ...],
) -> None:
    stdout = _cocotb_output([(name, "pass", "") for name in expected])
    run = _run_cocotb(tmp_path, monkeypatch, stdout, skip=skip, **request_args)

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    assert run.launched == [("ccfg", expected)]


# ---------------------------------------------------------------------------
# G4 — tests.toml validation: `select` is a setup-time error on cocotb
# ---------------------------------------------------------------------------


def test_select_template_rejected_only_on_cocotb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selecting = '[{target}]\ntests = ["{test}"]\nselect = "+test_id={{index}}"\n'
    (tmp_path / "cocotb").mkdir()
    (tmp_path / "native").mkdir()
    cocotb = _run_cocotb(
        tmp_path / "cocotb",
        monkeypatch,
        files={".booley/tests.toml": selecting.format(target="ccfg", test="test_reset")},
    )

    assert cocotb.result.exit_code == EXIT_ERROR
    assert "COCOTB_TEST_FILTER" in cocotb.result.outcome.report_text
    assert "remove the `select` key" in cocotb.result.outcome.report_text
    assert cocotb.launched == []

    # The rejection keys off cocotb-ness, not the mere presence of select.
    native = run_campaign_sim(
        tmp_path / "native",
        monkeypatch,
        {"lite": ["t0"]},
        SubprocessResult(returncode=0, stdout="[SIM_RESULT] PASSED\n", duration_s=0.1),
        files={".booley/tests.toml": selecting.format(target="lite", test="t0")},
        target="lite",
    )
    assert native.result.exit_code == EXIT_SUCCESS, native.result.outcome.report_text


# ---------------------------------------------------------------------------
# B4 — build failure: no simulator ever starts
# ---------------------------------------------------------------------------


def test_cocotb_build_failure_blocks_every_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the BUILD half breaks, not one selected test runs.

    The campaign records every selected test as blocked by the build and rides
    the compile error on each (taxi: an icarus Target whose SV interface ports
    don't parse in iverilog gave 14 phantom FAILs). Nothing is launched, so the
    build failure can never read as a pass (ADR 0034 dec 6).
    """
    build_output = (
        "[cocotb simulation: test_counter on icarus]\n"
        "rtl/counter.sv:12: syntax error\n"
        "rtl/counter.sv:12: error: Invalid module instantiation\n"
        "ERROR: iverilog compilation failed\n"
    )
    run = _run_cocotb(
        tmp_path, monkeypatch, build=SubprocessResult(returncode=1, stdout=build_output)
    )

    assert run.result.exit_code == EXIT_FAILURE
    assert run.launched == []
    tests = _projection(tmp_path)["tests"]
    assert [test["name"] for test in tests] == _TESTS["ccfg"]
    assert {test["execution"] for test in tests} == {"blocked_by_build"}
    assert all(not test["passed"] for test in tests)
    for tail in _error_tails(tmp_path).values():
        assert "iverilog compilation failed" in tail
        assert "syntax error" in tail


# ---------------------------------------------------------------------------
# B4 — dry-run parity
# ---------------------------------------------------------------------------


class TestCocotbDryRun:
    def test_dry_run_emits_one_batched_command(self, tmp_path: Path) -> None:
        """The engine preview is one command whose document carries every Test."""
        from tests.flows.sim.test_execution_engine import _handle, _inspection

        engine = execution_engine
        build = patch.object(engine.TargetCatalog, "build", return_value=_inspection(cocotb=True))
        setup = patch.object(engine.fusesoc_registry, "setup_command_for_handle", return_value=[])
        execution = engine.SimulationExecution(
            invoke=MagicMock(), options=engine.SimulationOptions()
        )
        with build, setup:
            preview = execution.preview(_handle(tmp_path), engine.NamedTests(("a", "b", "c")))

        (command,) = preview.commands
        work = launched_work(command[2])
        assert (work.adapter, work.cocotb_module) == ("cocotb", "test_demo")
        assert work.tests == ("a", "b", "c")

    def test_dry_run_batches_the_selected_cocotb_tests_into_one_work_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.flows.sim.request import SimRequest
        from booley.runtime.booley_dir import reset_cache

        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        (tmp_path / "ccfg.core").write_text(_COCOTB_CORE_TEXT, encoding="utf-8")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n", encoding="utf-8")
        (tmp_path / "tb").mkdir()
        (tmp_path / "tb/test_counter.py").write_text("", encoding="utf-8")
        (tmp_path / ".booley").mkdir()
        (tmp_path / ".booley/tests.toml").write_text(
            f"[ccfg]\ntests = {json.dumps(_TESTS['ccfg'])}\n", encoding="utf-8"
        )
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
        reset_cache()

        result = SimulateFlow().execute(
            SimRequest(
                target="ccfg",
                test=("test_reset", "test_count"),
                dry_run=True,
                work_dir=tmp_path,
                report_dir=tmp_path / "reports",
            )
        )

        assert result.exit_code == EXIT_SUCCESS, result.outcome.report_text
        (preview,) = result.outcome.detail["record_preview"]
        # Naming tests needs the registry, so success shows it was read; two tests, one batch.
        assert preview["work_items"] == 1

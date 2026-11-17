"""Tests for SimulateFlow — multi-config sim, cycle parsing, dry-run, reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.eda_failures import find_missing_executable
from booley.flows.sim.build import SimulationBuildPreparationError
from booley.flows.sim.execution import SimulationExecution
from booley.flows.sim.execution.engine import TRACE_CLEANUP_MARGIN_S
from booley.flows.sim.execution.telemetry import (
    parse_build_seconds,
    parse_run_seconds,
    parse_sim_cpu_seconds,
)
from booley.flows.sim.flow import (
    SimulateFlow as ProductionSimulateFlow,
)
from booley.flows.sim.mode import SimulationMode
from booley.flows.sim.request import SimRequest
from booley.flows.sim.watchdog_sizing import resolve_simulation_record_work_units
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS
from booley.runtime import runtime_context
from booley.runtime.endpoint_execution import ExecutionResult
from tests.flows.sim.leased_session import canned_build_session
from tests.flows.sim.simulation_record_boundary import (
    FAIL_PROCESS,
    INCONCLUSIVE_PROCESS,
    PASS_PROCESS,
    Launch,
    SimulationRecordSimRun,
    run_simulation_record_sim,
)
from tests.target_test_support import write_sim_preview_project


class SimulateFlow(ProductionSimulateFlow):
    """Concrete Flow used by the record compatibility tests."""


@pytest.fixture(autouse=True)
def _canned_build_session():
    """Run canned Flow tests against a leased build session with stubbed inputs."""
    with canned_build_session():
        yield


def _start_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets, **kwargs) -> str | None:
    """The display label of the first ``endpoint_start`` event of a record-path run."""
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    run_simulation_record_sim(tmp_path, monkeypatch, targets, PASS_PROCESS, **kwargs)
    lines = (tmp_path / "runtime/display.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    return next(event["display_label"] for event in events if event["type"] == "endpoint_start")


@pytest.mark.parametrize(
    ("targets", "request_args", "expected"),
    [
        pytest.param(
            {"sim_mul": ["smoke", "edge"]},
            {"target": "sim_mul", "test": ("smoke",)},
            "target sim_mul · test smoke",
            id="one-requested-test",
        ),
        pytest.param(
            {"sim_mul": ["mul_base", "mul_edge"]},
            {"target": "sim_mul", "test": ("mul_base", "mul_edge")},
            "target sim_mul · 2 tests",
            id="distinct-exact-tests",
        ),
        pytest.param(
            {"sim_mul": ["smoke", "edge"]},
            {"target": "sim_mul", "skip": {"sim_mul": ["edge"]}},
            "target sim_mul · test smoke",
            id="configured-skip-applied",
        ),
        pytest.param(
            {"sim_a": ["smoke"], "sim_b": ["smoke"]},
            {"target": "sim_a,sim_b", "test": ("smoke",)},
            "2 targets · test smoke",
            id="same-test-across-targets",
        ),
        pytest.param(
            {"sim_a": ["smoke"], "sim_b": []},
            {"target": "sim_a,sim_b"},
            "2 targets · tests",
            id="undeclared-target",
        ),
        pytest.param(
            {"sim_core": []},
            {"target": "sim_core", "mode": "elab_only", "dry_run": True},
            "target sim_core · elaboration",
            id="elaboration-mode",
        ),
    ],
)
def test_display_label_names_the_requested_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets, request_args, expected: str
):
    assert _start_label(tmp_path, monkeypatch, targets, **request_args) == expected


def test_a_request_outside_the_sandbox_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    if runtime_context.inside_session_runtime():
        pytest.skip("the refusal needs a host process")

    result = SimulateFlow().execute(
        SimRequest(work_dir=tmp_path, report_dir=tmp_path / "reports", target="lite")
    )

    assert result.exit_code == EXIT_ERROR
    assert "runs inside the Booley Sandbox" in result.outcome.report_text


@pytest.mark.parametrize(
    ("target", "names", "skips", "expected"),
    [
        ("::demo:core:0#sim_mul", RuntimeError("boom"), {}, "target sim_mul · tests"),
        (
            "sim_mul",
            {"sim_mul": ["smoke", "edge"]},
            {"sim_mul": ["edge"]},
            "target sim_mul · test smoke",
        ),
        ("sim_a,sim_b", {"sim_a": ["smoke"]}, {}, "2 targets · tests"),
    ],
    ids=["discovery-failure", "skips-applied", "undeclared-target"],
)
def test_display_label_of_a_target_no_core_declares(
    tmp_path: Path, target: str, names: Any, skips: dict[str, list[str]], expected: str
):
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", target])

    # Private seam: the endpoint refuses a Target no core declares before it emits the label, so only
    # a stubbed discovery reaches the label's registry-only fallback.
    failure = {"side_effect": names} if isinstance(names, Exception) else {"return_value": names}
    with (
        patch("booley.flows.sim.flow._get_test_names", **failure),
        patch("booley.flows.sim.test_selection._get_test_skips", return_value=skips),
    ):
        assert flow._resolve_display_label() == expected


def test_simulation_record_work_units_count_native_tests_and_cocotb_batches(tmp_path: Path):
    # Private seam: the Target catalog and skip list need a real FuseSoC resolve; no public input
    # replaces them.
    with (
        patch(
            "booley.flows.sim.watchdog_sizing._target_is_cocotb",
            side_effect=lambda _root, target: target == "cocotb",
        ),
        patch(
            "booley.flows.sim.watchdog_sizing._get_test_names",
            return_value={
                "native": ["smoke", "stress", "known_hang"],
                "cocotb": ["one", "two", "three"],
            },
        ),
        patch(
            "booley.flows.sim.watchdog_sizing._get_test_skips",
            return_value={"native": ["known_hang"]},
        ),
    ):
        units = resolve_simulation_record_work_units(tmp_path, "native,cocotb")

    assert units == 3  # two native processes plus one cocotb batch, summed over both Targets


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SimulationMode.ELAB_ONLY, 2),
        (SimulationMode.ELAB_ONLY_STANDALONE, 3),
    ],
)
def test_simulation_record_work_units_count_elaboration_modes(
    tmp_path: Path,
    mode: SimulationMode,
    expected: int,
) -> None:
    assert resolve_simulation_record_work_units(tmp_path, "one,two", mode=mode) == expected


@pytest.mark.parametrize(
    ("parser", "output", "expected"),
    [
        (parse_build_seconds, "make\nBOOLEY_BUILD_MILLISECONDS: 5000\n[SIM_RESULT] PASSED", 5.0),
        (parse_run_seconds, "BOOLEY_RUN_STAGE token=abc123 rc=0 duration_ms=19", 0.019),
        (
            parse_sim_cpu_seconds,
            "BOOLEY_SIM_CPU_SECONDS: user=1.250000 system=0.125000",
            (1.25, 0.125),
        ),
        (parse_build_seconds, "no marker here", 0.0),
        (parse_build_seconds, "", 0.0),
        # Only a line-anchored marker counts: a log quoting the string is no measurement.
        (parse_build_seconds, "echo BOOLEY_BUILD_MILLISECONDS: 9000", 0.0),
    ],
)
def test_build_time_telemetry_parsers(parser, output: str, expected):
    assert parser(output) == expected


def test_job_class_is_memory():
    from booley.runtime import job_slots

    # Private seam: the job class is read only by the job-slot admission, which needs a live slot store.
    assert SimulateFlow()._resolve_job_class() == job_slots.CLASS_MEMORY


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


def _preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: dict[str, list[str]],
    **request: Any,
) -> ExecutionResult:
    """Dry-run ``SimulateFlow.execute`` over one ``.core`` per Target in *targets*.

    *targets* maps each selector to its configured Tests (empty: none configured).
    """
    from booley.flows.sim.request import SimRequest
    from booley.runtime.booley_dir import reset_cache

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    data = write_sim_preview_project(tmp_path, targets)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    reset_cache()
    return SimulateFlow().execute(
        SimRequest(dry_run=True, work_dir=tmp_path, report_dir=tmp_path / "reports", **request)
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(ValueError("preview failed"), "preview failed", id="value-error"),
        # A private mirror refusal (#2895) is a planning error, not an escaped exception.
        pytest.param(
            SimulationBuildPreparationError("mirror refused"),
            "mirror refused",
            id="mirror-refused",
        ),
    ],
)
def test_dry_run_reports_preview_errors_as_planning_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(SimulationExecution, "preview", refuse)
    result = _preview(tmp_path, monkeypatch, {"lite": ["smoke"]}, target="lite")

    assert result.exit_code == EXIT_ERROR
    assert f"lite: {message}" in result.outcome.report_text


class TestDryRun:
    @pytest.mark.parametrize(
        ("targets", "request_args", "expected"),
        [
            pytest.param({"lite": []}, {}, {"lite": 1}, id="default-selection"),
            pytest.param(
                {"lite": ["smoke", "stress", "boot"]}, {}, {"lite": 3}, id="one-item-per-test"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"]}, {"test": ("smoke",)}, {"lite": 1}, id="test-filter"
            ),
            pytest.param(
                {"lite": ["smoke", "stress"], "full": ["smoke"]},
                {"target": "lite,full", "test": ("smoke",)},
                {"lite": 1, "full": 1},
                id="multi-target",
            ),
        ],
    )
    def test_dry_run_previews_one_work_item_per_selected_test(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        targets: dict[str, list[str]],
        request_args: dict[str, Any],
        expected: dict[str, int],
    ) -> None:
        request_args = {"target": "lite", **request_args}
        result = _preview(tmp_path, monkeypatch, targets, **request_args)

        outcome = result.outcome
        assert result.exit_code == EXIT_SUCCESS, outcome.report_text
        assert {
            preview["target"]["selector"]: preview["work_items"]
            for preview in outcome.detail["record_preview"]
        } == expected
        assert outcome.detail["mutated"] is False
        # A dry run reserves no invocation directory and takes no lock.
        report_root = tmp_path / "reports/sim"
        assert not [p for p in report_root.glob("*") if p.name.isdigit() or p.suffix == ".lock"]


# ---------------------------------------------------------------------------
# Ordinary simulation through the record path (SimulateFlow.execute)
#
# These run the real endpoint, record planning and execution, and Criteria
# reconciliation; only the process edge is faked (``simulation_record_boundary``).
# ---------------------------------------------------------------------------


def _projection(tmp_path: Path, target: str = "lite") -> dict[str, Any]:
    """Return the ``simulation.json`` projection Target *target* published."""
    path = tmp_path / f"reports/sim/1/targets/{target}/simulation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _launched_tests(run: SimulationRecordSimRun) -> list[str]:
    return [test for _target, tests in run.launched for test in tests]


@pytest.mark.parametrize(
    ("targets", "selector", "message"),
    [
        (
            {"lite": ["smoke", "stress"]},
            ("typo",),
            "unknown exact test(s) for target 'lite': typo",
        ),
        (
            {"lite": ["lite_stress"]},
            ("stress",),
            "unknown exact test(s) for target 'lite': stress",
        ),
        ({"lite": []}, ("anything",), "requires target 'lite' to have a registered test catalog"),
        # Valid for one Target but absent from another: the other would run its default and PASS.
        ({"lite": ["lite_smoke"], "full": ["full_sign"]}, ("lite_smoke",), "for target 'full'"),
    ],
    ids=["typo", "partial-name", "no-catalog", "one-of-many-targets"],
)
def test_simulation_record_rejects_unknown_test_without_launching(
    tmp_path: Path, monkeypatch, targets, selector: tuple[str, ...], message: str
):
    # The reported bug: --test no_such_test used to run the TB's default test and
    # report PASS. The record must reject it before any simulator launches.
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, targets, PASS_PROCESS, target=",".join(targets), test=selector
    )
    assert run.result.exit_code == EXIT_ERROR
    assert message in run.result.outcome.report_text
    assert run.launched == []


@pytest.mark.parametrize(
    ("skip", "selector", "expected"),
    [
        (["stress"], None, ["smoke", "boot"]),
        (["stress", "boot"], None, ["smoke"]),
        (["stress"], ("stress",), ["stress"]),
        (["stress"], ("boot", "smoke"), ["boot", "smoke"]),
    ],
    ids=["one-skip", "two-skips", "explicit-test-overrides-skip", "input-order"],
)
def test_simulation_record_runs_tests_left_by_skip_list(
    tmp_path: Path,
    monkeypatch,
    skip: list[str],
    selector: tuple[str, ...] | None,
    expected: list[str],
):
    # Naming a skipped test by hand is a clear override of the skip list.
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress", "boot"]},
        PASS_PROCESS,
        skip={"lite": skip},
        target="lite",
        test=selector,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    assert _launched_tests(run) == expected


def test_simulation_record_rejects_target_whose_tests_are_all_skipped(tmp_path: Path, monkeypatch):
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke", "stress"]},
        PASS_PROCESS,
        skip={"lite": ["smoke", "stress"]},
        target="lite",
    )
    assert run.result.exit_code == EXIT_ERROR
    assert "no runnable tests" in run.result.outcome.report_text
    assert run.launched == []


def _lite_passes_full_fails(launch: Launch) -> SubprocessResult:
    return PASS_PROCESS if launch.target == "lite" else FAIL_PROCESS


@pytest.mark.parametrize(
    ("targets", "process", "target", "exit_code", "grades"),
    [
        ({"lite": ["smoke"]}, PASS_PROCESS, "lite", EXIT_SUCCESS, {"lite": "pass"}),
        ({"lite": ["smoke"]}, FAIL_PROCESS, "lite", EXIT_FAILURE, {"lite": "fail"}),
        (
            {"lite": ["smoke"]},
            INCONCLUSIVE_PROCESS,
            "lite",
            EXIT_FAILURE,
            {"lite": "inconclusive"},
        ),
        (
            {"lite": ["smoke", "stress"]},
            PASS_PROCESS,
            "lite",
            EXIT_SUCCESS,
            {"lite": "pass"},
        ),
        (
            {"lite": ["smoke"], "full": ["smoke"]},
            _lite_passes_full_fails,
            "lite,full",
            EXIT_FAILURE,
            {"lite": "pass", "full": "fail"},
        ),
    ],
    ids=["summary-pass", "summary-fail", "inconclusive", "two-tests-pass", "mixed-targets"],
)
def test_simulation_record_verdict_sets_exit_code(
    tmp_path: Path, monkeypatch, targets, process, target, exit_code, grades
):
    run = run_simulation_record_sim(tmp_path, monkeypatch, targets, process, target=target)
    outcome = run.result.outcome
    assert run.result.exit_code == exit_code, outcome.report_text
    assert {name: row["grade"] for name, row in outcome.detail["records"].items()} == grades
    report = (tmp_path / "reports/sim/1/report.json").resolve()
    assert f"report: {report}" in outcome.report_text
    assert report.is_file()
    if grades == {"lite": "inconclusive"}:
        assert "INCONCLUSIVE" in outcome.report_text


CRASH_PROCESS = SubprocessResult(returncode=-11, stdout="[SIM_CYCLES] 12\n", duration_s=1.0)


@pytest.mark.parametrize(
    ("process", "exit_code", "plain", "per_test", "verdicts"),
    [
        (PASS_PROCESS, EXIT_SUCCESS, {"tests_failed": 0, "tests_total": 1}, {"smoke@1": 2561}, 0),
        (FAIL_PROCESS, EXIT_FAILURE, {"tests_failed": 1, "tests_total": 1}, None, 1),
        (CRASH_PROCESS, EXIT_FAILURE, {"tests_failed": 1, "tests_total": 1}, None, 1),
    ],
    ids=["pass", "fail", "crash"],
)
def test_simulation_run_records_no_criterion(
    tmp_path: Path,
    monkeypatch,
    process: SubprocessResult,
    exit_code: int,
    plain: dict[str, int],
    per_test: dict[str, int] | None,
    verdicts: int,
):
    """A hand-run ``sim`` keeps its verdict, exit status and metric values, derives no
    Criterion and writes no development-state or evidence file."""
    from booley.measurement.flow_metrics import report_metric_values
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.verdicts import FlowValues, evaluate_table, measure_exit_code

    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite"
    )

    assert run.result.exit_code == exit_code
    assert list(tmp_path.rglob("state.json")) == []
    assert not (tmp_path / "logs").exists()
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text(encoding="utf-8"))
    values = report_metric_values("sim", report["detail"])["lite"]
    assert values.plain == plain
    assert values.per_test == ({"cycle_count": per_test} if per_test else {})
    rules = TableRules(
        "t",
        "vlnv:x#t",
        "sim",
        (Bound("tests_failed_max", "tests_failed", "max", 0, None, None, None),),
        (),
    )
    judged = evaluate_table(rules, FlowValues(values, values.clocks))
    assert measure_exit_code(result.verdict for result in judged) == verdicts


def test_simulation_run_leaves_declared_criteria_untouched(tmp_path: Path, monkeypatch):
    """Criteria declared in a state file are neither evaluated, baselined nor recorded."""
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b'{"opaque": true}\n')
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    before = state_path.read_bytes()
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite"
    )

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    assert run.launched == [("lite", ("smoke",))]
    assert state_path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX unreadable-directory regression")
@pytest.mark.parametrize("relative", ["unreadable", ".booley/unreadable"])
def test_unreadable_unrelated_directory_does_not_block_plain_simulation(
    tmp_path: Path, monkeypatch, relative: str
) -> None:
    unreadable = tmp_path / relative
    unreadable.mkdir(parents=True)
    unreadable.chmod(0)
    try:
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite"
        )
    finally:
        unreadable.chmod(0o700)

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text


def test_simulation_record_projection_reports_tests_and_label(tmp_path: Path, monkeypatch):
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, PASS_PROCESS, target="lite"
    )
    report = _projection(tmp_path)
    assert report["flow"] == "sim"
    assert report["target"] == "lite"
    assert report["passed"] is True
    assert [test["name"] for test in report["tests"]] == ["smoke", "stress"]
    assert report["tests"][0]["cycles"] == 2561
    assert run.result.outcome.display_label == "target lite · 2 tests"


def test_configured_cycle_sentinel_reaches_report_and_projection(tmp_path: Path, monkeypatch):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(
        '[flows.sim]\ncycle_sentinels = ["CoreMark completed in:"]\n', encoding="utf-8"
    )
    process = SubprocessResult(
        returncode=0,
        stdout=(
            "[SIM_RESULT] PASSED\n"
            "CoreMark completed in: coremark 31415\n"
            '[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
        ),
        duration_s=1.0,
    )
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["coremark"]}, process, target="lite"
    )

    assert "coremark: cycles=31415" in run.result.outcome.report_text
    assert _projection(tmp_path)["tests"][0]["cycles"] == 31415


def _run_logs(tmp_path: Path) -> list[Path]:
    """Every work item's evidence ``run.log``, in work-item order."""
    return sorted((tmp_path / "reports").rglob("execution-evidence/run.log"))


def test_simulation_record_keeps_each_test_log_in_its_own_work_item(tmp_path: Path, monkeypatch):
    """Each HDL launch gets durable evidence before the next one starts."""
    first_bytes: list[bytes] = []

    def process(launch: Launch) -> SubprocessResult:
        if launch.tests == ("stress",):
            first_bytes.append(_run_logs(tmp_path)[0].read_bytes())
            return SubprocessResult(
                returncode=1, stdout="STRESS_ONLY\n[SIM_RESULT] FAILED\n", duration_s=0.1
            )
        return SubprocessResult(
            returncode=0, stdout="SMOKE_ONLY\n[SIM_RESULT] PASSED\n", duration_s=0.1
        )

    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke", "stress"]}, process, target="lite"
    )

    assert run.result.exit_code == EXIT_FAILURE
    assert _launched_tests(run) == ["smoke", "stress"]
    smoke_log, stress_log = _run_logs(tmp_path)
    assert smoke_log.read_bytes() == first_bytes[0]
    assert "SMOKE_ONLY" in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" not in smoke_log.read_text(encoding="utf-8")
    assert "STRESS_ONLY" in stress_log.read_text(encoding="utf-8")
    assert "SMOKE_ONLY" not in stress_log.read_text(encoding="utf-8")


def test_consecutive_simulation_records_preserve_first_evidence(tmp_path: Path, monkeypatch):
    def run(output: str) -> tuple[Path, bytes]:
        run_simulation_record_sim(
            tmp_path,
            monkeypatch,
            {"lite": ["smoke"]},
            SubprocessResult(returncode=0, stdout=f"{output}\n[SIM_RESULT] PASSED\n"),
            target="lite",
            test=("smoke",),
        )
        path = _run_logs(tmp_path)[-1]
        return path, path.read_bytes()

    first_path, first_bytes = run("FIRST_ONLY")
    second_path, second_bytes = run("SECOND_ONLY")

    assert first_path != second_path
    assert first_path.read_bytes() == first_bytes
    assert b"FIRST_ONLY" in first_bytes
    assert b"SECOND_ONLY" in second_bytes


def test_simulation_record_crash_keeps_completed_target_in_progress(tmp_path: Path, monkeypatch):
    def process(launch: Launch) -> SubprocessResult:
        if launch.target == "full":
            raise RuntimeError("simulated outer interruption")
        return PASS_PROCESS

    monkeypatch.setenv("BOOLEY_RUN_ID", "sim-checkpoint-1")
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"], "full": ["smoke"]}, process, target="lite,full"
    )

    assert run.result.exit_code == EXIT_ERROR
    assert "simulated outer interruption" in run.result.outcome.report_text
    progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text(encoding="utf-8"))
    assert progress["run_id"] == "sim-checkpoint-1"
    assert progress["complete"] is True
    assert progress["phase"] == "aborted"
    assert progress["completed_targets"] == ["lite"]
    assert progress["pending_targets"] == ["full"]


def test_default_report_root_uses_project_data(tmp_path: Path, monkeypatch):
    rtl = tmp_path / "rtl-checkout"
    rtl.mkdir()
    project_data = tmp_path / "project-data"
    run = run_simulation_record_sim(
        rtl,
        monkeypatch,
        {"lite": []},
        PASS_PROCESS,
        target="lite",
        report_dir=None,
        project_data=project_data,
    )
    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    invocation = project_data / "flow-reports/sim/1"
    assert (invocation / "progress.json").is_file()
    assert (invocation / "targets/lite/simulation.json").is_file()
    assert not (rtl / "flow-reports").exists()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_mcp_default_timeout_allows_trace_retry(self, tmp_path: Path):
        flow = SimulateFlow()
        flow.parse_args(["--work-dir", str(tmp_path), "--target", "lite"])
        # Private seam: the wrapper timeout is read only by the MCP watchdog, not by a result.
        default_run_s = flow._get_timeout()  # pyright: ignore[reportPrivateUsage]
        retry_allowance = 2 * default_run_s + TRACE_CLEANUP_MARGIN_S

        assert SimulateFlow.default_timeout >= retry_allowance

    def test_custom_timeout(self, tmp_path: Path):
        flow = SimulateFlow()
        flow.parse_args(
            ["--work-dir", str(tmp_path), "--target", "lite", "--timeout-ms", "120000"]
        )
        assert flow.args.timeout_ms == 120000

    def test_timeout_results_in_fail(self, tmp_path: Path, monkeypatch):
        # The wrapper killed the launch: no adapter result, only the timed-out process.
        process = SubprocessResult(
            returncode=-1,
            stdout="partial output",
            timed_out=True,
            duration_s=600.0,
        )
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite"
        )

        assert run.result.exit_code == EXIT_FAILURE
        test = _projection(tmp_path)["tests"][0]
        assert test["timed_out"] is True
        assert test["execution"] == "timeout"
        hint = "raise --timeout-ms or [flows.sim].timeout_ms if the test legitimately needs longer"
        assert "TIMEOUT: simulation exceeded" in test["error_tail"]
        assert hint in test["error_tail"]
        assert hint in run.result.outcome.report_text


# ---------------------------------------------------------------------------
# Elaboration failure detection
# ---------------------------------------------------------------------------


class TestErrorTailSource:
    """The error excerpt must surface the DUT's own (stdout) failure signal, not
    the build's stderr lint-warning storm — except for a failed build, whose only
    diagnostic lives on stderr with no sim stdout to fall back to."""

    def test_sim_failure_tail_from_stdout_not_stderr_noise(self, tmp_path: Path, monkeypatch):
        """A real sim FAIL: the build's stderr warning storm must not bury the
        stdout mismatch line in the 50-line excerpt window."""
        noise = "\n".join(
            f"/work/rtl/foo.sv:{i}: warning: @* is sensitive to all 16 words" for i in range(60)
        )
        process = SubprocessResult(
            returncode=1,
            stdout="[SIM_RESULT] FAILED\nFAIL: coeff mismatch at index 7\n",
            stderr=noise,
            duration_s=5.0,
        )
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, process, target="lite"
        )

        assert run.result.exit_code == EXIT_FAILURE
        tail = _projection(tmp_path)["tests"][0]["error_tail"]
        assert "coeff mismatch at index 7" in tail
        assert "warning:" not in tail

    def test_elab_failure_tail_falls_back_to_stderr(self, tmp_path: Path, monkeypatch):
        """A failed build produces no sim stdout — the excerpt must carry the
        stderr diagnostic so it is not lost."""
        build = SubprocessResult(returncode=1, stderr="%Error: rtl/top.sv:17: syntax error\n")
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite", build=build
        )

        assert run.result.exit_code == EXIT_FAILURE
        assert run.launched == []
        assert "syntax error" in _projection(tmp_path)["tests"][0]["error_tail"]


# ---------------------------------------------------------------------------
# ravenoc F-32 — a missing EDA binary is a Flow error, never a test failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        ("/bin/sh: 1: verilator: not found", "verilator"),
        ("bash: fusesoc: command not found", "fusesoc"),
        ("make[1]: verilator: No such file or directory", "verilator"),
        # A plain setup failure keeps its own message and exit-1 grading.
        ("FileNotFoundError: [Errno 2] No such file or directory: '/work/foo.core'", None),
        ("%Error: tb.sv:12: Cannot find file containing module: 'dut'\nnot found\n", None),
    ],
    ids=["dash", "bash", "make", "missing-data-file", "compile-error"],
)
def test_missing_executable_detection(text: str, tool: str | None):
    assert find_missing_executable(text) == tool


class TestMissingExecutableIsEdaToolError:
    """F-32: the verdict channel must not say FAIL when nothing ever ran."""

    def test_missing_verilator_in_build_exits_2(self, tmp_path: Path, monkeypatch):
        # What the sandbox script actually prints with verilator off PATH.
        build = SubprocessResult(
            returncode=127,
            stdout=(
                "/bin/sh: 1: verilator: not found\n"
                "make: *** [Makefile:9: Vtop] Error 127\n"
                "ERROR: Verilator elaboration failed (rc=127)\n"
            ),
        )
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["t1"]}, PASS_PROCESS, target="lite", build=build
        )
        outcome = run.result.outcome
        assert run.result.exit_code == EXIT_ERROR
        assert outcome.detail["eda_tool_error"] == "build_infrastructure"
        assert "missing tool: verilator" in outcome.report_text
        assert run.launched == []

    def test_live_sim_echoing_command_not_found_is_not_hijacked(self, tmp_path: Path, monkeypatch):
        """A running TB's own $system noise must not become a Flow error."""
        process = SubprocessResult(
            returncode=1,
            stdout=(
                "sh: 1: helper_script: not found\n"
                '[SIM_RESULT] FAILED\n[SIM_SUMMARY] {"passed":false,"sva_errors":0}\n'
            ),
        )
        run = run_simulation_record_sim(
            tmp_path, monkeypatch, {"lite": ["t1"]}, process, target="lite"
        )
        assert run.result.exit_code == EXIT_FAILURE
        assert "eda_tool_error" not in run.result.outcome.detail

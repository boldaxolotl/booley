"""The implementation Flow base: per-Target publication shared by synth and FPGA."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.fpga.cli import FpgaArguments
from booley.flows.fpga.request import FpgaRequest
from booley.flows.implementation_report import (
    ImplementationContext,
    ImplementationReport,
    ImplementationRun,
    MetricPolicy,
    build_implementation_report,
)
from booley.runtime import job_slots
from tests.flows.test_implementation_report import _pair_plan


@dataclass
class _ProbeMetrics:
    infra_error: str = ""
    area: float = 1.0


def _run(metrics: _ProbeMetrics, *, passed: bool) -> ImplementationRun:
    return ImplementationRun(
        passed=passed,
        tool_returncode=0,
        metrics={"area": metrics.area},
        infra_error=metrics.infra_error or None,
    )


def _probe_class() -> Any:
    """A minimal subclass of the base, built inside the test that uses it."""
    from booley.flows.implementation_flow import ImplementationFlow

    class _Probe(ImplementationFlow[FpgaRequest, _ProbeMetrics]):
        request_type = FpgaRequest
        argument_adapter = FpgaArguments
        # Publication accepts only the two implementation Flow names.
        name = "fpga"

        def _build_implementation_report(
            self,
            context: ImplementationContext,
            current: _ProbeMetrics,
            baseline: _ProbeMetrics | None,
        ) -> ImplementationReport:
            return build_implementation_report(
                context,
                _run(current, passed=False),
                _run(baseline, passed=True) if baseline is not None else None,
                MetricPolicy(("area",)),
            )

        def _target_report_payload(
            self,
            target: str,
            current: _ProbeMetrics,
            baseline: _ProbeMetrics | None,
            baseline_ref: str | None,
            report_path: Path,
        ) -> dict[str, Any]:
            return {"passed": True, "artifacts": {}}

    return _Probe


def _probe(tmp_path: Path | None = None, *args: str) -> Any:
    flow = _probe_class()()
    if tmp_path is not None:
        report_dir = str(tmp_path / "reports")
        flow.parse_args(
            ["--target", "new", "--work-dir", str(tmp_path), "--report-dir", report_dir, *args]
        )
        flow._implementation_reports = {}
    flow._target_plan = lambda _target: _pair_plan("fpga")
    return flow


def test_finished_target_is_published_and_checkpointed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-3")
    flow = _probe(tmp_path)
    metrics = _ProbeMetrics()

    flow._persist_target_outcome("new", metrics, None, None)
    flow._write_progress_report(["new", "other"], {"new": metrics}, {}, phase="current")

    stable = json.loads((tmp_path / "reports" / "fpga_new.json").read_text(encoding="utf-8"))
    numbered = list((tmp_path / "reports" / "fpga").glob("*/targets/new.json"))
    assert len(numbered) == 1
    # The implementation verdict overrides the payload's own ``passed``.
    assert stable["passed"] is False
    assert json.loads(numbered[0].read_text(encoding="utf-8"))["passed"] is False
    assert flow._implementation_reports["new"].canonical["status"]["grade"] == "fail"
    progress = json.loads(
        next((tmp_path / "reports" / "fpga").glob("*/progress.json")).read_text(encoding="utf-8")
    )
    assert progress["completed_targets"] == ["new"]
    assert progress["pending_targets"] == ["other"]
    assert "new" in progress["implementation"]["results"]


def test_target_report_is_skipped_without_report_dir(tmp_path: Path) -> None:
    flow = _probe(tmp_path)
    flow.args.report_dir = None
    metrics = _ProbeMetrics()

    flow._persist_target_outcome("new", metrics, None, None)

    assert not (tmp_path / "reports").exists()
    assert "new" in flow._implementation_reports


def test_baseline_and_comparison_errors_become_notes_and_reasons() -> None:
    from booley.flows.implementation_flow import ImplementationFlow

    comparison_report = build_implementation_report(
        ImplementationContext(
            flow="fpga",
            target="b",
            eda_tool="vivado",
            invocation_run_id="run-1",
            baseline_target="b",
            baseline_target_identity="x#b",
            candidate_target_identity="x#b",
            requested_baseline_ref="main",
            resolved_baseline_ref="a" * 40,
        ),
        _run(_ProbeMetrics(), passed=True),
        _run(_ProbeMetrics(infra_error="baseline broke"), passed=True),
        MetricPolicy(("area",), ("lut_count",)),
    )
    errors = comparison_report.canonical["comparison"]["basis_errors"]
    assert errors[0] == "baseline infrastructure error: baseline broke"

    notes, failures = ImplementationFlow._baseline_error_notes(
        {"a": _ProbeMetrics(infra_error="baseline broke")}, {"b": comparison_report}
    )

    assert errors[1:] == [
        "current required metric 'lut_count' is unavailable",
        "baseline required metric 'lut_count' is unavailable",
    ]
    assert failures == [
        "baseline a: baseline broke",
        "comparison b: current required metric 'lut_count' is unavailable",
        "comparison b: baseline required metric 'lut_count' is unavailable",
    ]
    assert [line.split(" -- ")[0].strip() for line in notes] == [
        "baseline a: ERROR",
        "comparison b: ERROR",
        "comparison b: ERROR",
    ]
    assert notes[0].endswith("-- baseline broke")
    assert not any("baseline infrastructure error" in line for line in notes + failures)


def test_report_context_carries_refs_and_pair_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report identity: refs, Targets and eda tool."""
    monkeypatch.setenv("BOOLEY_RUN_ID", "run-7")
    flow = _probe()
    flow._args = SimpleNamespace(baseline="main~1")
    flow._eda_tool = "tool-x"
    current, baseline = _ProbeMetrics(), _ProbeMetrics()

    flow._baseline_full_sha = "a" * 40
    with_baseline = flow._implementation_report("new", current, baseline, "short").canonical
    flow._baseline_full_sha = None
    short_ref = flow._implementation_report("new", current, baseline, "short").canonical
    without_baseline = flow._implementation_report("new", current, None, None).canonical

    comparison = with_baseline["comparison"]
    assert comparison["requested_ref"] == "main~1"
    assert comparison["resolved_ref"] == "a" * 40
    assert comparison["baseline_target"] == "old"
    assert comparison["baseline_target_identity"] == "acme:lib:dut:1#old"
    assert comparison["candidate_target_identity"] == "acme:lib:dut:1#new"
    assert short_ref["comparison"]["resolved_ref"] == "short"
    assert without_baseline["comparison"] is None
    for canonical in (with_baseline, without_baseline):
        identity = canonical["identity"]
        assert identity["flow"] == "fpga"
        assert identity["target"] == "new"
        assert identity["target_identity"] == "acme:lib:dut:1#new"
        assert identity["eda_tool"] == "tool-x"
        assert identity["invocation_run_id"] == "run-7"


def test_implementation_flows_are_memory_jobs() -> None:
    assert _probe()._resolve_job_class() == job_slots.CLASS_MEMORY
    assert _probe()._build_command() == []
    assert _probe()._interpret_result(None).exit_code == 0


def test_flow_hooks_must_be_overridden() -> None:
    from booley.flows.implementation_flow import ImplementationFlow

    class _Bare(ImplementationFlow[FpgaRequest, _ProbeMetrics]):
        request_type = FpgaRequest
        argument_adapter = FpgaArguments
        name = "fpga"

    flow = _Bare()
    with pytest.raises(NotImplementedError):
        flow._build_implementation_report(None, None, None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        flow._target_report_payload("t", None, None, None, Path("x"))  # type: ignore[arg-type]

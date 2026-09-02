from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.criteria.templates import TargetPair
from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics
from booley.flows.fpga.implementation_report import build_fpga_implementation_report
from booley.flows.implementation_publication import (
    ImplementationProgress,
    ImplementationPublisher,
    target_report_path,
    target_report_slug,
)
from booley.flows.implementation_report import (
    ENVELOPE_KEY,
    ImplementationContext,
    ImplementationRun,
    MetricPolicy,
    build_implementation_aggregate,
    build_implementation_report,
)
from booley.flows.synth.flow import SynthMetrics
from booley.flows.synth.implementation_report import build_synth_implementation_report


def _context(**overrides) -> ImplementationContext:
    values = {
        "flow": "synth",
        "target": "asic",
        "eda_tool": "yosys",
        "invocation_run_id": "run-1",
    }
    values.update(overrides)
    return ImplementationContext(**values)


def _run(**overrides) -> ImplementationRun:
    values = {
        "passed": True,
        "tool_returncode": 0,
        "metrics": {"area": 10.0},
    }
    values.update(overrides)
    return ImplementationRun(**values)


def test_grade_is_resolved_once_from_policy_inputs() -> None:
    policy = MetricPolicy(("area",))
    warning = build_implementation_report(
        _context(), _run(warning_reasons=("negative slack",)), None, policy
    )
    failure = build_implementation_report(
        _context(), _run(failure_reasons=("fatal timing",)), None, policy
    )
    error = build_implementation_report(
        _context(), _run(infra_error="tool unavailable"), None, policy
    )

    assert warning.grade == "warn" and warning.passed
    assert failure.grade == "fail" and not failure.passed
    assert error.grade == "error" and not error.passed
    assert build_implementation_aggregate({"warn": warning}).exit_code == 0
    assert build_implementation_aggregate({"fail": failure}).exit_code == 1
    assert build_implementation_aggregate({"error": error}).exit_code == 2


def test_comparison_records_per_metric_unavailability() -> None:
    report = build_implementation_report(
        _context(
            baseline_target="old",
            requested_baseline_ref="main~1",
            resolved_baseline_ref="a" * 40,
        ),
        _run(metrics={"area": 12.0, "optional": None}),
        _run(metrics={"area": 0.0, "optional": 5.0}),
        MetricPolicy(("area", "optional"), required_comparison_metrics=("area",)),
    )

    comparison = report.canonical["comparison"]
    assert comparison["basis_valid"] is True
    assert comparison["resolved_ref"] == "a" * 40
    assert comparison["deltas"]["area"]["unavailable_reason"] == "baseline metric is zero"
    assert comparison["deltas"]["optional"]["unavailable_reason"] == (
        "current metric is unavailable"
    )


def test_adapter_inputs_are_defensively_copied() -> None:
    metrics = {"area": 10.0}
    recipe = {"mode": "physical"}
    report = build_implementation_report(
        _context(),
        _run(metrics=metrics, recipe_snapshot=recipe),
        None,
        MetricPolicy(("area",)),
    )
    metrics["area"] = 99.0
    recipe["mode"] = "logical"
    exposed = report.canonical
    exposed["metrics"]["area"] = -1

    assert report.canonical["metrics"]["area"] == 10.0
    assert report.canonical["recipe"]["snapshot"]["mode"] == "physical"


def test_context_rejects_missing_identity_and_diagnostics_are_bounded() -> None:
    with pytest.raises(ValueError, match="flow and target must be non-empty"):
        ImplementationContext(flow="", target="asic")

    report = build_implementation_report(
        _context(),
        _run(diagnostic_excerpt="x" * 5_000),
        None,
        MetricPolicy(("area",)),
    )

    diagnostic = report.canonical["status"]["diagnostic_excerpt"]
    assert diagnostic.startswith("[... 1000 character(s) omitted ...]")
    assert diagnostic.endswith("x" * 4_000)


def test_synth_and_fpga_adapters_share_structure_but_keep_unique_metrics() -> None:
    pair = TargetPair("old", "new")
    synth = build_synth_implementation_report(
        target="new",
        pair=pair,
        current=SynthMetrics(area_um2=100.0, area_kge=1.0, cells=10),
        baseline=None,
        baseline_ref=None,
        resolved_baseline_ref=None,
        eda_tool="yosys",
        fatal_timing=False,
    ).canonical
    fpga = build_fpga_implementation_report(
        target="new",
        pair=pair,
        current=FpgaMetrics(
            lut_count=10,
            ff_count=20,
            wns_ns=0.1,
            whs_ns=0.1,
        ),
        baseline=None,
        baseline_ref=None,
        resolved_baseline_ref=None,
        eda_tool="vivado",
    ).canonical

    assert set(synth) == set(fpga)
    assert "area_kge" in synth["metrics"] and "area_kge" not in fpga["metrics"]
    assert "lut_count" in fpga["metrics"] and "lut_count" not in synth["metrics"]


def test_fpga_cache_separates_producer_and_consumer_provenance() -> None:
    current = FpgaMetrics(
        lut_count=10,
        ff_count=20,
        wns_ns=0.1,
        whs_ns=0.1,
        cached=True,
        cache_fingerprint="cache-key",
        cache_consumer_run_id="consumer-run",
        run_evidence={"run_id": "producer-run", "source_revision": "abc"},
    )

    report = build_fpga_implementation_report(
        target="board",
        pair=TargetPair("board", "board"),
        current=current,
        baseline=None,
        baseline_ref=None,
        resolved_baseline_ref=None,
        eda_tool="vivado",
    ).canonical

    assert report["cache"] == {"cached": True, "fingerprint": "cache-key"}
    assert report["provenance"]["producer"]["run_id"] == "producer-run"
    assert report["provenance"]["consumer_run_id"] == "consumer-run"


def test_baseline_infrastructure_error_makes_comparison_an_error() -> None:
    report = build_implementation_report(
        _context(requested_baseline_ref="main", resolved_baseline_ref="a" * 40),
        _run(),
        _run(passed=False, tool_returncode=2, infra_error="baseline setup failed"),
        MetricPolicy(("area",), required_comparison_metrics=("area",)),
    )

    assert report.grade == "error"
    assert report.canonical["comparison"]["basis_valid"] is False
    assert "baseline infrastructure error" in report.canonical["comparison"]["basis_errors"][0]


def test_synthesis_fatal_timing_is_persisted_as_failure() -> None:
    report = build_synth_implementation_report(
        target="asic",
        pair=TargetPair("asic", "asic"),
        current=SynthMetrics(area_kge=1.0, cells=10, wns_ns=-0.2),
        baseline=None,
        baseline_ref=None,
        resolved_baseline_ref=None,
        eda_tool="yosys",
        fatal_timing=True,
    )

    assert report.grade == "fail"
    assert report.canonical["status"]["failure_reasons"] == ["setup slack -0.200 ns"]


def test_publication_writes_numbered_report_before_stable_alias(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    invocation = report_dir / "synth" / "1"
    invocation.mkdir(parents=True)
    log = tmp_path / "build" / "run.log"
    log.parent.mkdir()
    log.write_text("complete log\n", encoding="utf-8")
    baseline_log = tmp_path / "build" / "baseline.log"
    baseline_log.write_text("baseline log\n", encoding="utf-8")
    report = build_implementation_report(
        _context(
            target="vendor:core#asic",
            requested_baseline_ref="main~1",
            resolved_baseline_ref="a" * 40,
        ),
        _run(artifacts={"log": "build/run.log", "dirs": {"build": "build"}}),
        _run(artifacts={"log": "build/baseline.log"}),
        MetricPolicy(("area",)),
    )
    publisher = ImplementationPublisher(tmp_path, report_dir, invocation)

    published = publisher.publish_report(report, {"passed": True})

    stable = target_report_path("synth", "vendor:core#asic", report_dir)
    numbered = invocation / "targets" / f"{target_report_slug('vendor:core#asic')}.json"
    assert published.stable_path == stable
    assert published.invocation_path == numbered
    assert stable.read_bytes() == numbered.read_bytes()
    payload = json.loads(stable.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload[ENVELOPE_KEY]["artifacts"]["report"].endswith(
        numbered.relative_to(tmp_path).as_posix()
    )
    snapshot = tmp_path / payload[ENVELOPE_KEY]["artifacts"]["log"]
    assert snapshot.read_text(encoding="utf-8") == "complete log\n"
    assert payload[ENVELOPE_KEY]["artifacts"]["live_dirs"] == {"build": "build"}
    baseline_artifacts = payload[ENVELOPE_KEY]["comparison"]["baseline"]["artifacts"]
    baseline_snapshot = tmp_path / baseline_artifacts["log"]
    assert baseline_snapshot.read_text(encoding="utf-8") == "baseline log\n"


def test_publication_without_invocation_keeps_live_log_pointer(tmp_path: Path) -> None:
    report = build_implementation_report(
        _context(),
        _run(artifacts={"log": "build/missing.log"}),
        None,
        MetricPolicy(("area",)),
    )
    publisher = ImplementationPublisher(tmp_path, tmp_path / "reports", None)

    published = publisher.publish_report(report, {"passed": True})

    assert published.invocation_path is None
    assert published.stable_path == tmp_path / "reports" / "synth_asic.json"
    assert published.payload[ENVELOPE_KEY]["artifacts"] == {
        "report": "reports/synth_asic.json",
        "log": "build/missing.log",
    }


def test_publication_drops_missing_log_from_invocation_snapshot(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    invocation = report_dir / "synth" / "1"
    report = build_implementation_report(
        _context(),
        _run(artifacts={"log": "build/missing.log"}),
        None,
        MetricPolicy(("area",)),
    )

    published = ImplementationPublisher(tmp_path, report_dir, invocation).publish_report(
        report, {"passed": True}
    )

    assert published.payload[ENVELOPE_KEY]["artifacts"] == {
        "report": "reports/synth/1/targets/asic.json"
    }


def test_publication_can_be_disabled_without_losing_the_envelope(tmp_path: Path) -> None:
    report = build_implementation_report(
        _context(),
        _run(),
        None,
        MetricPolicy(("area",)),
    )

    published = ImplementationPublisher(tmp_path, None, None).publish_report(
        report, {"passed": True}
    )

    assert published.stable_path is None
    assert published.invocation_path is None
    assert published.payload[ENVELOPE_KEY]["identity"]["target"] == "asic"


def test_failed_stable_refresh_preserves_previous_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_dir = tmp_path / "reports"
    invocation = report_dir / "synth" / "2"
    invocation.mkdir(parents=True)
    stable = target_report_path("synth", "asic", report_dir)
    stable.parent.mkdir(parents=True, exist_ok=True)
    stable.write_text("previous stable report\n", encoding="utf-8")
    report = build_implementation_report(_context(), _run(), None, MetricPolicy(("area",)))
    original_replace = Path.replace

    def fail_stable_refresh(source: Path, destination: Path) -> Path:
        if destination == stable:
            raise OSError("simulated stable refresh failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_stable_refresh)

    with pytest.raises(OSError, match="stable refresh failure"):
        ImplementationPublisher(tmp_path, report_dir, invocation).publish_report(
            report, {"passed": True}
        )

    assert stable.read_text(encoding="utf-8") == "previous stable report\n"
    assert (invocation / "targets" / "asic.json").is_file()
    assert list(report_dir.glob(".*.tmp")) == []


def test_progress_uses_shared_shape(tmp_path: Path) -> None:
    invocation = tmp_path / "reports" / "fpga" / "1"
    invocation.mkdir(parents=True)
    report = build_implementation_report(
        _context(flow="fpga", target="board"),
        _run(metrics={"lut_count": 10}),
        None,
        MetricPolicy(("lut_count",)),
    )
    path = ImplementationPublisher(tmp_path, tmp_path / "reports", invocation).publish_progress(
        ImplementationProgress(
            flow="fpga",
            run_id="run-1",
            targets=("board", "board_2"),
            completed_targets=("board",),
            phase="current",
            reports={"board": report},
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["completed_targets"] == ["board"]
    assert payload["pending_targets"] == ["board_2"]
    assert payload[ENVELOPE_KEY]["results"]["board"]["metrics"]["lut_count"] == 10

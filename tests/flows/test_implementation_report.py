from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics
from booley.flows.fpga.implementation_report import build_fpga_implementation_report
from booley.flows.implementation_publication import (
    ImplementationProgress,
    ImplementationProgressRun,
    ImplementationPublisher,
    target_report_path,
    target_report_slug,
)
from booley.flows.implementation_report import (
    ENVELOPE_KEY,
    ImplementationContext,
    ImplementationRun,
    MetricPolicy,
    build_implementation_report,
)
from booley.flows.progress_lifecycle import ProgressPublicationError
from booley.flows.synth.flow import SynthMetrics
from booley.flows.synth.implementation_report import build_synth_implementation_report
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome


def _context(**overrides) -> ImplementationContext:
    values = {"flow": "synth", "target": "asic", "eda_tool": "yosys", "invocation_run_id": "run-1"}
    values.update(overrides)
    return ImplementationContext(**values)


def _run(**overrides) -> ImplementationRun:
    values = {"passed": True, "tool_returncode": 0, "metrics": {"area": 10.0}}
    values.update(overrides)
    return ImplementationRun(**values)


def test_comparison_records_per_metric_unavailability() -> None:
    report = build_implementation_report(
        _context(
            baseline_target="old", requested_baseline_ref="main~1", resolved_baseline_ref="a" * 40
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
        _context(), _run(metrics=metrics, recipe_snapshot=recipe), None, MetricPolicy(("area",))
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
        _context(), _run(diagnostic_excerpt="x" * 5_000), None, MetricPolicy(("area",))
    )

    diagnostic = report.canonical["status"]["diagnostic_excerpt"]
    assert diagnostic.startswith("[... 1000 character(s) omitted ...]")
    assert diagnostic.endswith("x" * 4_000)


def test_synth_and_fpga_adapters_share_structure_but_keep_unique_metrics() -> None:
    synth = build_synth_implementation_report(
        _context(flow="synth", target="new", eda_tool="yosys"),
        SynthMetrics(area_um2=100.0, area_kge=1.0, cells=10),
        None,
        fatal_timing=False,
    ).canonical
    fpga = build_fpga_implementation_report(
        _context(flow="fpga", target="new", eda_tool="vivado"),
        FpgaMetrics(lut_count=10, ff_count=20, wns_ns=0.1, whs_ns=0.1),
        None,
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
        _context(flow="fpga", target="board", eda_tool="vivado"), current, None
    ).canonical

    assert report["cache"] == {"cached": True, "fingerprint": "cache-key"}
    assert report["provenance"]["producer"]["run_id"] == "producer-run"
    assert report["provenance"]["consumer_run_id"] == "consumer-run"


@pytest.mark.parametrize(
    ("metrics", "reasons"),
    [
        (SynthMetrics(area_kge=1.0, cells=10, wns_ns=-0.2), ["setup slack -0.200 ns"]),
        (
            SynthMetrics(
                area_kge=1.0,
                cells=10,
                wns_ns=-0.2,
                returncode=1,
                termination="design_failure",
                failure_output=(
                    "unmapped generic cell: [ERROR ORD-2013] instance flop LEF master "
                    "$_DFF_P_ not found."
                ),
            ),
            ["unmapped generic cell", "setup slack -0.200 ns"],
        ),
    ],
)
def test_synthesis_fatal_timing_is_persisted_as_failure(
    metrics: SynthMetrics, reasons: list[str]
) -> None:
    report = build_synth_implementation_report(_context(), metrics, None, fatal_timing=True)

    assert report.grade == "fail"
    assert report.canonical["status"]["failure_reasons"] == reasons


def test_publication_writes_numbered_report_before_stable_alias(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    invocation = report_dir / "synth" / "1"
    invocation.mkdir(parents=True)
    log = invocation / "artifacts" / "asic" / "run.log"
    log.parent.mkdir(parents=True)
    log.write_text("complete log\n", encoding="utf-8")
    baseline_log = invocation / "artifacts" / "baseline" / "run.log"
    baseline_log.parent.mkdir(parents=True)
    baseline_log.write_text("baseline log\n", encoding="utf-8")
    report = build_implementation_report(
        _context(
            target="vendor:core#asic",
            requested_baseline_ref="main~1",
            resolved_baseline_ref="a" * 40,
        ),
        _run(artifacts={"log": log.relative_to(tmp_path).as_posix(), "dirs": {"build": "build"}}),
        _run(artifacts={"log": baseline_log.relative_to(tmp_path).as_posix()}),
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
    assert snapshot == log
    assert snapshot.read_text(encoding="utf-8") == "complete log\n"
    assert payload[ENVELOPE_KEY]["artifacts"]["live_dirs"] == {"build": "build"}
    baseline_artifacts = payload[ENVELOPE_KEY]["comparison"]["baseline"]["artifacts"]
    baseline_snapshot = tmp_path / baseline_artifacts["log"]
    assert baseline_snapshot == baseline_log
    assert baseline_snapshot.read_text(encoding="utf-8") == "baseline log\n"


@pytest.mark.parametrize(
    ("report_dir", "invocation", "stable", "numbered", "artifacts"),
    [
        # The live log pointer survives when no invocation folder exists.
        (
            "reports",
            None,
            "reports/synth_asic.json",
            None,
            {"report": "reports/synth_asic.json", "log": "build/missing.log"},
        ),
        # A log missing from the invocation snapshot is dropped.
        (
            "reports",
            "reports/synth/1",
            "reports/synth_asic.json",
            "reports/synth/1/targets/asic.json",
            {"report": "reports/synth/1/targets/asic.json"},
        ),
        # Publication can be disabled without losing the envelope.
        (None, None, None, None, None),
    ],
    ids=["live-log-pointer", "missing-log-dropped", "disabled"],
)
def test_publication_paths_and_artifacts_follow_the_folders_given(
    report_dir: str | None,
    invocation: str | None,
    stable: str | None,
    numbered: str | None,
    artifacts: dict[str, str] | None,
    tmp_path: Path,
) -> None:
    report = build_implementation_report(
        _context(),
        _run(artifacts={"log": "build/missing.log"} if report_dir else None),
        None,
        MetricPolicy(("area",)),
    )

    published = ImplementationPublisher(
        tmp_path,
        tmp_path / report_dir if report_dir else None,
        tmp_path / invocation if invocation else None,
    ).publish_report(report, {"passed": True})

    assert published.stable_path == (tmp_path / stable if stable else None)
    assert published.invocation_path == (tmp_path / numbered if numbered else None)
    assert published.payload[ENVELOPE_KEY]["identity"]["target"] == "asic"
    assert published.payload[ENVELOPE_KEY]["artifacts"] == artifacts


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


def test_progress_run_owns_start_and_complete_publication() -> None:
    phases: list[tuple[str, bool]] = []

    def publish(
        _current: dict[str, object],
        _baseline: dict[str, object],
        phase: str,
        _baseline_ref: str | None,
        complete: bool,
    ) -> None:
        phases.append((phase, complete))

    outcome = EndpointOutcome()
    with ImplementationProgressRun("synth", publish) as run:
        assert run.complete(outcome) is outcome

    assert phases == [("starting", False), ("complete", True)]


def test_progress_run_retains_outcome_when_terminal_publication_fails() -> None:
    def publish(
        _current: dict[str, object],
        _baseline: dict[str, object],
        phase: str,
        _baseline_ref: str | None,
        _complete: bool,
    ) -> None:
        if phase != "starting":
            raise OSError("disk unavailable")

    outcome = EndpointOutcome(report_text="existing report")
    run = ImplementationProgressRun("fpga", publish)
    run.__enter__()
    with pytest.raises(ProgressPublicationError) as raised:
        run.complete(outcome)

    failed = run.publication_failure(raised.value)
    assert failed is outcome
    assert failed.exit_code == EXIT_ERROR
    assert failed.detail["progress_error"] == "disk unavailable"
    assert failed.report_text == "existing report"


def test_progress_run_builds_report_for_publication_failure_without_outcome() -> None:
    run = ImplementationProgressRun("synth", lambda *_args: None)

    outcome = run.publication_failure(ProgressPublicationError("read-only filesystem"))

    assert outcome.exit_code == EXIT_ERROR
    assert outcome.report_text == "synth: progress publication failed: read-only filesystem"


def test_progress_without_invocation_directory_is_not_published(tmp_path: Path) -> None:
    progress = ImplementationProgress(flow="synth", run_id="run-1", targets=("asic",))

    assert ImplementationPublisher(tmp_path, tmp_path, None).publish_progress(progress) is None


def test_progress_rejects_inconsistent_terminal_shape(tmp_path: Path) -> None:
    progress = ImplementationProgress(
        flow="synth", run_id="run-1", targets=("asic",), phase="complete", complete=False
    )

    with pytest.raises(ValueError, match="complete and phase disagree"):
        ImplementationPublisher(tmp_path, tmp_path, tmp_path / "1").publish_progress(progress)


def _pair_plan(flow: str) -> SimpleNamespace:
    """Duck-typed pair plan with distinct baseline and candidate Targets."""
    return SimpleNamespace(
        flow=flow,
        baseline=SimpleNamespace(selector="old", identity="acme:lib:dut:1#old"),
        candidate=SimpleNamespace(selector="new", identity="acme:lib:dut:1#new"),
    )


def test_context_of_a_skipped_baseline_drops_its_refs_but_keeps_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.flows.implementation_report import implementation_context

    monkeypatch.setenv("BOOLEY_RUN_ID", "run-42")

    skipped = implementation_context(
        _pair_plan("fpga"),
        target="new",
        eda_tool="vivado",
        baseline_ran=False,
        requested_baseline_ref="main~1",
        resolved_baseline_ref="a" * 40,
    )

    assert (skipped.requested_baseline_ref, skipped.resolved_baseline_ref) == (None, None)
    assert skipped.baseline_target_identity == "acme:lib:dut:1#old"

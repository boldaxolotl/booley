"""Simulation Flow output fields and coverage_analyst inputs name the Record.

Part 6 of ADR 0090. The Flow's outcome and persisted ``report.json`` report
``detail.records`` (not ``campaigns``), ``detail.record_integrity_error`` and
``detail.record_preview``; a per-Target row names ``simulation_record_id`` and the
``local_record`` / ``external_origin_record`` dependency; a Target row names its
``coverage_record``; ``[flows.sim].run_cwd`` expands ``{record}``; ``coverage_analyst``
takes ``--record`` (MCP ``record``, ``BOOLEY_COVERAGE_RECORD``). No old spelling is an
alias. A report Booley 0.4.0 wrote is read from committed bytes under
``tests/flows/fixtures/booley_0_4_0`` and reported as an older-Booley record.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.config import literal_run_cwd_problem, parse_run_cwd_template
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record.run_directory import expand_run_directory
from booley.flows.sim.record_presentation import (
    _attach_failure_reference,
    _pre_sim_integrity_failure,
)
from booley.flows.sim.record_retention import _dependent_invocations, _report_references_origin
from booley.flows.sim.request import SimRequest
from booley.mcp.catalog import Exposure
from booley.mcp.flow_adapter import flow_schema
from booley.presentation.report_results import (
    present_job_report,
    present_report,
    project_facts,
)
from booley.runtime import owned_scratch
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.pid import capture_process_identity
from booley.runtime.supervised_execution import (
    SupervisedExecutionScope,
    supervised_execution_scope,
)
from booley.specialists import coverage_analyst as coverage_analyst_module
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from tests.flows.sim.simulation_record_boundary import run_campaign_sim
from tests.flows.sim.simulation_record_test_support import coverage_invocation
from tests.flows.sim.test_coverage_flow import NativeExecution, _interrupt_coverage_publication
from tests.flows.sim.test_flow import PASS_PROCESS, _preview
from tests.owned_scratch_support import exited_process_payload, write_marker

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "booley_0_4_0"
_OLDER = "written by an older Booley; no longer supported"
_OLD_REPORTS = (
    ("complete", "complete/sim/1/report.json"),
    ("coverage", "coverage/sim/1/report.json"),
    ("resumed", "resumed/sim/2/report.json"),
)
requires_procfs = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="owner liveness fixtures need procfs"
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _ordinary_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = run_campaign_sim(tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite")
    assert run.result.exit_code == 0, run.result.outcome.report_text
    return run


def test_a_run_reports_detail_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _ordinary_run(tmp_path, monkeypatch)
    persisted = _json(tmp_path / "reports/sim/1/report.json")

    for detail in (run.result.outcome.detail, persisted["detail"]):
        assert set(detail["records"]) == {"lite"}
        assert detail["records"]["lite"]["grade"] == "pass"
        assert "campaigns" not in detail


def test_record_detail_names_the_simulation_record_and_its_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    _ordinary_run(ordinary, monkeypatch)
    manifest = _json(ordinary / "reports/sim/1/targets/lite/record/manifest.json")
    record_id = manifest["simulation_record_id"]
    report_row = _json(ordinary / "reports/sim/1/report.json")["detail"]["records"]["lite"]
    progress_row = _json(ordinary / "reports/sim/1/progress.json")["detail"]["lite"]

    assert report_row["simulation_record_id"] == record_id
    assert report_row["dependency"] == "local_record"
    # Progress is always built without the invocation, so it names the external value.
    assert progress_row["simulation_record_id"] == record_id
    assert progress_row["dependency"] == "external_origin_record"

    resume = tmp_path / "resume"
    resume.mkdir()
    reports, _request, _runs = _interrupt_coverage_publication(resume, monkeypatch)
    origin = reports / "sim/1/targets/sim_0/record/manifest.json"
    resumed = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(resume_from=origin, work_dir=resume, report_dir=resume / "resumed")
    )
    assert resumed.exit_code == 0
    resumed_row = _json(resume / "resumed/sim/1/report.json")["detail"]["records"]["sim_0"]
    assert resumed_row["dependency"] == "external_origin_record"
    assert resumed_row["simulation_record_id"] == _json(origin)["simulation_record_id"]

    for row in (report_row, progress_row, resumed_row):
        assert "campaign_id" not in row
        assert row["dependency"] not in {"local_campaign", "external_origin_campaign"}


def test_retention_finds_a_dependent_report_through_simulation_record_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ordinary_run(tmp_path, monkeypatch)
    origin = tmp_path / "reports/sim/1"
    manifest = origin / "targets/lite/record/manifest.json"
    raw = manifest.read_bytes()
    record_id = json.loads(raw)["simulation_record_id"]
    reference = {
        "bytes": len(raw),
        "kind": "simulation_record_manifest",
        "owner": record_id,
        "path": "sim/1/targets/lite/record/manifest.json",
        "path_base": "reports_root",
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }
    sibling = tmp_path / "reports/sim/2/report.json"
    sibling.parent.mkdir()

    def write(id_key: str) -> None:
        row = {id_key: record_id, "artifacts": {"manifest": reference}}
        sibling.write_text(json.dumps({"detail": {"records": {"lite": row}}}), encoding="utf-8")

    write("simulation_record_id")
    assert _report_references_origin(sibling, (manifest,)), "no dependent via simulation_record_id"
    assert [item.path.name for item in _dependent_invocations(origin)] == ["2"]

    write("campaign_id")
    assert not _report_references_origin(sibling, (manifest,))


def test_coverage_reports_name_the_coverage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    persisted = _json(run.reports / "sim/1/report.json")
    row = persisted["detail"]["targets"]["sim_0"]

    assert row["coverage_record"]["kind"] == "coverage_record_reference"
    assert "coverage_campaign" not in row

    owner = _json(run.target / "record/manifest.json")["simulation_record_id"]
    failed: dict[str, object] = {}
    _attach_failure_reference(failed, run.public, run.reports / "sim/1", owner)
    assert failed["coverage_record"] == row["coverage_record"]
    assert "coverage_campaign" not in failed


def test_a_dry_run_reports_detail_record_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _preview(tmp_path, monkeypatch, {"lite": ["smoke"]}, target="lite").outcome.detail

    assert [entry["target"]["selector"] for entry in detail["record_preview"]] == ["lite"]
    assert "campaign_preview" not in detail


def test_an_integrity_failure_reports_detail_record_integrity_error() -> None:
    result = _pre_sim_integrity_failure(EndpointOutcome(), RuntimeError("tampered"))

    assert result.detail["record_integrity_error"] == "tampered"
    assert "campaign_integrity_error" not in result.detail


def test_the_coverage_record_path_label() -> None:
    rows = project_facts({"campaign_path": "reports/sim/1/coverage.json"})

    assert [row.label for row in rows] == ["Coverage Record path"], "label is not the record one"


def test_run_cwd_expands_the_record_placeholder(tmp_path: Path) -> None:
    assert parse_run_cwd_template("runs/{record}/{test}") == ("record", "test")

    run = expand_run_directory(
        "runs/{record}/{test}",
        project_root=tmp_path,
        simulation_record_id="rec-1",
        target_key="lite",
        work_item_key="smoke",
        attempt_key="a1",
    )
    assert run.path == tmp_path / "runs" / "rec-1" / "smoke"
    assert run.owned


def test_coverage_analyst_takes_the_record_argument_parameter_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.mcp import coverage_evidence

    record = tmp_path / "coverage.json"
    analyst = CoverageAnalystSpecialist()
    analyst.parse_args(["--work-dir", str(tmp_path), "--record", str(record)])
    assert analyst.args.record == record

    schema = flow_schema(CoverageAnalystSpecialist())
    assert "record" in schema["properties"]
    assert "record" in schema["required"]

    environment = analyst._evidence_environment(tmp_path, record, tmp_path / "audit.json")
    assert environment["BOOLEY_COVERAGE_RECORD"] == str(record)

    assert Exposure.from_mapping({"BOOLEY_COVERAGE_RECORD": "x"}).campaign is True

    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_RECORD", str(tmp_path / "missing.json"))
    with pytest.raises((OSError, ValueError)) as unreadable:
        coverage_evidence.query_active_coverage_evidence({})
    assert "No active Coverage Record" not in str(unreadable.value)


def test_coverage_analysis_scratch_and_cancellation_thread_use_record_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefixes: list[str] = []

    def stop(prefix: str):
        prefixes.append(prefix)
        raise RuntimeError("scratch opened")

    monkeypatch.setattr(coverage_analyst_module, "owned_tempdir", stop)
    campaign = types.SimpleNamespace(invocation={"id": "1"})
    with pytest.raises(RuntimeError, match="scratch opened"):
        coverage_analyst_module.analyze_coverage_campaign(campaign, None, "")  # type: ignore[arg-type]
    assert prefixes == ["booley-coverage-record-"], "scratch prefix is not the record one"

    scope = SupervisedExecutionScope(None, tmp_path / ".booley", lambda: False)
    with supervised_execution_scope(scope):
        names = {thread.name for thread in threading.enumerate()}
    assert "booley-record-process-cancellation" in names
    assert "booley-campaign-process-cancellation" not in names


def test_an_old_run_cwd_placeholder_fails_validation_naming_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError) as refused:
        parse_run_cwd_template("runs/{campaign}")
    assert "{record}" in str(refused.value)
    assert "{campaign}" in str(refused.value)

    run = run_campaign_sim(
        tmp_path,
        monkeypatch,
        {"lite": ["smoke"]},
        PASS_PROCESS,
        files={".booley/booley.toml": '[flows.sim]\nrun_cwd = "runs/{campaign}/{test}"\n'},
        target="lite",
    )
    assert literal_run_cwd_problem(tmp_path) == run_cwd_message(run.result.outcome.report_text)
    assert run.result.exit_code != 0
    assert "{record}" in run.result.outcome.report_text
    assert run.launches == []


def run_cwd_message(report_text: str) -> str:
    """The configuration problem line the run reports, without its report prefix."""
    marker = "invalid [flows.sim].run_cwd template"
    line = next(line for line in report_text.splitlines() if marker in line)
    return line[line.index(marker) :]


def test_coverage_analyst_refuses_the_old_campaign_input(tmp_path: Path) -> None:
    analyst = CoverageAnalystSpecialist()
    with pytest.raises(SystemExit):
        analyst.parse_args(["--campaign", str(tmp_path / "coverage.json")])

    schema = flow_schema(CoverageAnalystSpecialist())
    assert "campaign" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert Exposure.from_mapping({"BOOLEY_COVERAGE_CAMPAIGN": "x"}).campaign is False


def test_report_listing_reports_a_0_4_0_simulation_report_as_older() -> None:
    for name, relative in _OLD_REPORTS:
        report = _json(_FIXTURES / relative)
        word = {0: "PASSED", 2: "ERROR"}[report["exit_code"]]
        expected = f"{word} · {_OLDER}"

        assert present_report(report).flow_result.value == expected, name
        job = present_job_report(
            report, endpoint="sim", exit_code=report["exit_code"], argv=["sim"]
        )
        assert job.flow_result.value == expected, name
        for presentation in (present_report(report), job):
            values = " ".join(row.value for row in presentation.result_rows)
            for unavailable in (
                "Summary unavailable",
                "grade unavailable",
                "evaluation unavailable",
            ):
                assert unavailable not in values, name


@requires_procfs
def test_old_coverage_scratch_prefix_is_swept_never_created(tmp_path: Path) -> None:
    own = capture_process_identity(os.getpid())
    assert own is not None

    def entry(name: str, owner: dict[str, object]) -> Path:
        directory = tmp_path / name
        directory.mkdir()
        write_marker(directory, owner)
        return directory

    old = entry("booley-coverage-campaign-dead", exited_process_payload())
    new = entry("booley-coverage-record-dead", exited_process_payload())
    live = entry("booley-coverage-record-live", own.to_payload())

    removed = owned_scratch.sweep_stale_scratch(tmp_path)

    assert "booley-coverage-record-" in owned_scratch.OWNED_PREFIXES, "new prefix not swept"
    assert sorted(path.name for path in removed) == [old.name, new.name]
    assert live.is_dir()

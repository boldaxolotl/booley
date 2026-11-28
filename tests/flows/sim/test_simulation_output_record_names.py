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

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.config import literal_run_cwd_problem, parse_run_cwd_template

# Private seam: the failure detail is only built when projection publication fails mid-run,
# which a completed public run never does.
from booley.flows.sim.record_presentation import _attach_failure_reference
from booley.mcp.catalog import Exposure
from booley.mcp.flow_adapter import flow_schema
from booley.presentation.report_results import present_job_report, present_report, project_facts
from booley.runtime import owned_scratch
from booley.runtime.pid import capture_process_identity
from booley.specialists import coverage_analyst as coverage_analyst_module
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from tests.flows.sim.simulation_record_boundary import run_simulation_record_sim
from tests.flows.sim.simulation_record_test_support import coverage_invocation
from tests.flows.sim.test_flow import PASS_PROCESS
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
    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["smoke"]}, PASS_PROCESS, target="lite"
    )
    assert run.result.exit_code == 0, run.result.outcome.report_text
    return run


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

    for row in (report_row, progress_row):
        assert "campaign_id" not in row
        assert row["dependency"] not in {"local_campaign", "external_origin_campaign"}


def test_coverage_reports_name_the_coverage_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    persisted = _json(run.reports / "sim/1/report.json")
    row = persisted["detail"]["targets"]["sim_0"]

    owner = _json(run.target / "record/manifest.json")["simulation_record_id"]
    failed: dict[str, object] = {}
    _attach_failure_reference(failed, run.public, run.reports / "sim/1", owner)
    assert failed["coverage_record"] == row["coverage_record"]
    assert "coverage_campaign" not in failed


def test_the_coverage_record_path_label() -> None:
    rows = project_facts({"campaign_path": "reports/sim/1/coverage.json"})

    assert [row.label for row in rows] == ["Coverage Record path"], "label is not the record one"


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

    # Private seam: the environment is built only inside a live Coverage Analyst session.
    environment = analyst._evidence_environment(tmp_path, record, tmp_path / "audit.json")
    assert environment["BOOLEY_COVERAGE_RECORD"] == str(record)

    assert Exposure.from_mapping({"BOOLEY_COVERAGE_RECORD": "x"}).coverage_record is True

    # Private seam: ``_ACTIVE_SESSION`` and ``_TERMINAL_ERROR`` are the module's process-global
    # session state, with no public reset.
    monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
    monkeypatch.setattr(coverage_evidence, "_TERMINAL_ERROR", None)
    monkeypatch.setenv("BOOLEY_COVERAGE_PROJECT", str(tmp_path))
    monkeypatch.setenv("BOOLEY_COVERAGE_RECORD", str(tmp_path / "missing.json"))
    with pytest.raises((OSError, ValueError)) as unreadable:
        coverage_evidence.query_active_coverage_evidence({})
    assert "No active Coverage Record" not in str(unreadable.value)


def test_an_old_run_cwd_placeholder_fails_validation_naming_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert parse_run_cwd_template("runs/{record}/{test}") == ("record", "test")

    run = run_simulation_record_sim(
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


def test_coverage_analyst_refuses_the_old_coverage_record_input(tmp_path: Path) -> None:
    analyst = CoverageAnalystSpecialist()
    with pytest.raises(SystemExit):
        analyst.parse_args(["--campaign", str(tmp_path / "coverage.json")])

    schema = flow_schema(CoverageAnalystSpecialist())
    assert "campaign" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert Exposure.from_mapping({"BOOLEY_COVERAGE_CAMPAIGN": "x"}).coverage_record is False


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


@requires_procfs
def test_old_coverage_scratch_prefix_is_swept_never_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    assert sorted(path.name for path in removed) == [old.name, new.name]
    assert live.is_dir()

    # The prefix coverage analysis creates is the swept record one.
    prefixes: list[str] = []

    def stop(prefix: str):
        prefixes.append(prefix)
        raise RuntimeError("scratch opened")

    monkeypatch.setattr(coverage_analyst_module, "owned_tempdir", stop)
    coverage_record = types.SimpleNamespace(invocation={"id": "1"})
    with pytest.raises(RuntimeError, match="scratch opened"):
        coverage_analyst_module.analyze_coverage_record(coverage_record, None, "")  # type: ignore[arg-type]
    assert prefixes == ["booley-coverage-record-"], "scratch prefix is not the record one"
    assert prefixes[0] in owned_scratch.OWNED_PREFIXES, "new prefix not swept"

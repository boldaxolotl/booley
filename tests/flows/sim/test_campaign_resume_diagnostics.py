"""Public durable resume refusal diagnostics and immutable attempt evidence."""

from pathlib import Path

import pytest

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_coverage_invocation import project


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _interrupted_campaign(root, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: "a" * 40)
    project(root)
    core = root / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "targets:\n",
            "parameters:\n  WIDTH: {datatype: int, paramtype: vlogparam, default: 1}\ntargets:\n",
        )
        .replace("    filesets: [rtl]", "    parameters: [WIDTH]\n    filesets: [rtl]")
    )
    data = root / ".booley_project"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests=["reset", "wrap"]\n')
    reports = root / "reports"

    executions = []

    def interrupt_execution(_executor, _request):
        executions.append(_request.attempt_id)
        _executor.prepare_attempt(_request)
        raise KeyboardInterrupt("durable ordinary interruption")

    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor.execute", interrupt_execution
    )

    with pytest.raises(KeyboardInterrupt, match="durable ordinary interruption"):
        result = SimulateFlow().execute(
            SimRequest(target="sim_0", work_dir=root, report_dir=reports)
        )
        pytest.fail(str(result.outcome))
    manifest = reports / "sim/1/targets/sim_0/campaign/manifest.json"
    assert list(manifest.parent.rglob("attempt.json"))
    return reports, manifest, executions


def test_public_source_resume_names_cause_without_new_attempt(tmp_path, monkeypatch):
    reports, manifest, executions = _interrupted_campaign(tmp_path, monkeypatch)
    before = _snapshot(manifest.parent)
    (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n// change\n")
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )
    assert result.exit_code == 2
    assert len(executions) == 1
    assert "source changed: rtl/counter.sv" in result.outcome.report_text
    assert "8 derived fingerprints differ" in result.outcome.report_text
    assert result.outcome.detail["derived_fingerprint_count"] == 8
    assert "/workload/source_recipe/sources/" not in result.outcome.report_text
    assert {
        key: value
        for key, value in _snapshot(manifest.parent).items()
        if not key.startswith("dependency-receipts/")
    } == before


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("verbose", [False, True])
def test_public_resume_cli_summary_detail_and_dry_run_immutability(
    tmp_path, monkeypatch, dry_run, verbose
):
    reports, manifest, executions = _interrupted_campaign(tmp_path, monkeypatch)
    (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n// change\n")
    before = _snapshot(reports)
    argv = [
        "--resume-from",
        str(manifest),
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(reports),
    ]
    if dry_run:
        argv.append("--dry-run")
    if verbose:
        argv.append("--verbose")
    flow = SimulateFlow()
    result = flow.execute(parse_request(flow, argv))
    assert result.exit_code == 2
    assert len(executions) == 1
    assert "source changed: rtl/counter.sv" in result.outcome.detail["mismatch_summary"]
    assert any(
        "/planning_disclosures/" in line for line in result.outcome.detail["mismatch_summary"]
    )
    assert result.outcome.detail["derived_fingerprint_count"] == 8
    raw = result.outcome.detail["mismatches"]
    assert raw
    if verbose:
        assert all(line in result.outcome.report_text for line in raw)
    else:
        assert all(
            line not in result.outcome.report_text
            for line in raw
            if line.startswith("/build_variants/")
        )
    if dry_run:
        assert _snapshot(reports) == before


@pytest.mark.parametrize(
    ("changed", "line"),
    [
        ("parameter", "parameter WIDTH:"),
        ("suite", "suite changed: .booley_project/tests.toml"),
        ("option", "build command model changed"),
    ],
)
def test_public_resume_parameters_suite_content_and_opaque_options(
    tmp_path, monkeypatch, changed, line
):
    reports, manifest, executions = _interrupted_campaign(tmp_path, monkeypatch)
    if changed == "suite":
        path = tmp_path / ".booley_project/tests.toml"
        path.write_text(path.read_text() + "# catalog bytes changed, names remain frozen\n")
    else:
        path = tmp_path / "counter.core"
        old, new = (
            ("default: 1", "default: 2")
            if changed == "parameter"
            else ("tool: verilator}", "tool: verilator, verilator_options: [--Wall]}")
        )
        path.write_text(path.read_text().replace(old, new))
    before = _snapshot(manifest.parent)
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )
    assert result.exit_code == 2
    assert any(root.startswith(line) for root in result.outcome.detail["mismatch_summary"])
    assert len(executions) == 1
    assert {
        key: value
        for key, value in _snapshot(manifest.parent).items()
        if not key.startswith("dependency-receipts/")
    } == before


@pytest.mark.parametrize("removed", ["source", "suite"])
def test_missing_selected_input_preserves_precomparison_planning_error(
    tmp_path, monkeypatch, removed
):
    reports, manifest, executions = _interrupted_campaign(tmp_path, monkeypatch)
    if removed == "source":
        (tmp_path / "rtl/counter.sv").unlink()
    else:
        (tmp_path / ".booley_project/tests.toml").write_text('[sim_0]\ntests=["wrap"]\n')
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports, dry_run=True)
    )
    assert result.exit_code == 2
    if removed == "source":
        assert "mismatch_summary" not in result.outcome.detail
        assert "campaign workload mismatch" not in result.outcome.report_text
    else:
        assert result.outcome.detail["mismatch_summary"] == [
            "suite changed: .booley_project/tests.toml"
        ]
    assert len(executions) == 1

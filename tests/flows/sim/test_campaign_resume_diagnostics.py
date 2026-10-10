"""Public durable resume refusal diagnostics and immutable attempt evidence."""

import json
from pathlib import Path

import pytest

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.campaign.planning import compare_manifests
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_coverage_invocation import project


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _interrupted_campaign(root, monkeypatch, *, parameter_datatype="int", parameter_default="1"):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: "a" * 40)
    project(root)
    core = root / "counter.core"
    core.write_text(
        core.read_text()
        .replace(
            "targets:\n",
            "parameters:\n  WIDTH: {datatype: "
            + parameter_datatype
            + ", paramtype: vlogparam, default: "
            + parameter_default
            + "}\ntargets:\n",
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
    assert (
        json.loads(manifest.read_text())["planning_disclosures"][0]["generated_files"][0]["path"]
        == "src/acme_demo_counter_1/rtl/counter.sv"
    )
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
    assert result.outcome.detail["mismatch_summary"] == [
        "source changed: rtl/counter.sv",
    ]
    assert len(result.outcome.detail["mismatches"]) == 16
    assert "sha256:" not in result.outcome.report_text
    assert "8 derived fingerprints differ" in result.outcome.report_text
    assert result.outcome.detail["derived_fingerprint_count"] == 8
    assert "/workload/source_recipe/sources/" not in result.outcome.report_text
    assert {
        key: value
        for key, value in _snapshot(manifest.parent).items()
        if not key.startswith("dependency-receipts/")
    } == before


def _prime_prior_refusal(root, reports, manifest, executions):
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=root, report_dir=reports)
    )
    assert result.exit_code == 2
    assert len(executions) == 1
    assert len(list((manifest.parent / "dependency-receipts").glob("*.json"))) == 1


def _capture_resume_comparison(monkeypatch):
    pairs = []
    original = SimulateFlow._resume_campaign_plan

    def capture(flow, validated, *args, **kwargs):
        plan = original(flow, validated, *args, **kwargs)
        pairs.append((validated.manifest, plan.manifest))
        return plan

    monkeypatch.setattr(SimulateFlow, "_resume_campaign_plan", capture)
    return pairs


def _assert_durable_evidence_with_one_new_receipt(before, after):
    existing = {
        key: value for key, value in before.items() if key.startswith("dependency-receipts/")
    }
    observed = {
        key: value for key, value in after.items() if key.startswith("dependency-receipts/")
    }
    assert existing.items() <= observed.items()
    assert len(observed) == len(existing) + 1
    assert {key: value for key, value in before.items() if key not in existing} == {
        key: value for key, value in after.items() if key not in observed
    }
    added = observed.keys() - existing.keys()
    receipt = json.loads(observed[added.pop()])
    dependent = Path(receipt["dependent_invocation"])
    assert dependent.is_dir()
    assert int(dependent.name) == receipt["dependent_invocation_id"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("verbose", [False, True])
def test_public_resume_cli_summary_detail_and_dry_run_immutability(
    tmp_path, monkeypatch, dry_run, verbose
):
    reports, manifest, executions = _interrupted_campaign(tmp_path, monkeypatch)
    (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n// change\n")
    _prime_prior_refusal(tmp_path, reports, manifest, executions)
    before = _snapshot(reports)
    durable_before = _snapshot(manifest.parent)
    compared = _capture_resume_comparison(monkeypatch)
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
    assert result.outcome.detail["mismatch_summary"] == [
        "source changed: rtl/counter.sv",
    ]
    assert "/planning_disclosures/" not in result.outcome.report_text or verbose
    assert result.outcome.detail["derived_fingerprint_count"] == 8
    for line in result.outcome.detail["mismatch_summary"]:
        assert line in result.outcome.report_text
    assert "8 derived fingerprints differ" in result.outcome.report_text
    raw = result.outcome.detail["mismatches"]
    assert len(compared) == 1
    assert raw == [item.message for item in compare_manifests(*compared[0])]
    assert len(raw) == 16
    if verbose:
        assert all(line in result.outcome.report_text for line in raw)
    else:
        assert "sha256:" not in result.outcome.report_text
        assert all(line not in result.outcome.report_text for line in raw)
    if dry_run:
        assert _snapshot(reports) == before
    else:
        _assert_durable_evidence_with_one_new_receipt(durable_before, _snapshot(manifest.parent))


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
def test_missing_input_distinguishes_source_planning_error_from_suite_comparison(
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
        assert result.outcome.report_text == (
            "Simulation Campaign resume preview failed: Project compile input is not a file: "
            f"{tmp_path / 'rtl/counter.sv'}"
        )
        assert "mismatch_summary" not in result.outcome.detail
        assert "campaign workload mismatch" not in result.outcome.report_text
    else:
        assert result.outcome.detail["mismatch_summary"] == [
            "suite changed: .booley_project/tests.toml"
        ]
    assert len(executions) == 1


def test_public_resume_bool_to_int_names_parameter_and_explains_command_digest(
    tmp_path, monkeypatch
):
    reports, manifest, executions = _interrupted_campaign(
        tmp_path, monkeypatch, parameter_datatype="bool", parameter_default="false"
    )
    core = tmp_path / "counter.core"
    core.write_text(core.read_text().replace("default: false", "default: 0"))
    before = _snapshot(manifest.parent)
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )
    assert result.exit_code == 2
    assert result.outcome.detail["mismatch_summary"] == [
        'parameter WIDTH: {"datatype":"bool","default":false,"paramtype":"vlogparam"} → {"datatype":"bool","default":0,"paramtype":"vlogparam"}'
    ]
    assert "build command model changed" not in result.outcome.report_text
    raw = result.outcome.detail["mismatches"]
    assert any(line.startswith("/workload/build_recipe/command_model_sha256:") for line in raw)
    assert not any(line.startswith("/workload/source_recipe/parameters/") for line in raw)
    assert result.outcome.detail["derived_fingerprint_count"] == 9
    assert len(executions) == 1
    assert {
        key: value
        for key, value in _snapshot(manifest.parent).items()
        if not key.startswith("dependency-receipts/")
    } == before


def test_legacy_interrupted_campaign_new_source_mapping_refuses_before_attempt(
    tmp_path, monkeypatch
):
    from booley.flows.sim.execution.engine import _planning_disclosure

    def legacy_disclosure(entries):
        disclosure = _planning_disclosure(entries)
        disclosure["tool_provenance"]["contract_version"] = "1"
        return disclosure

    with monkeypatch.context() as legacy:
        legacy.setattr("booley.flows.sim.execution.engine._planning_disclosure", legacy_disclosure)
        legacy.setattr(
            "booley.flows.sim.execution.engine._source_declarations", lambda _handle: ()
        )
        reports, manifest, executions = _interrupted_campaign(tmp_path, legacy)
    document = json.loads(manifest.read_bytes())
    assert document["$schema"] == "booley.simulation-campaign-manifest/v1"
    assert "source_path" not in document["planning_disclosures"][0]["generated_files"][0]
    before = _snapshot(manifest.parent)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr("booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: "a" * 40)
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )
    assert result.exit_code == 2
    assert len(executions) == 1
    assert any("$schema" in line for line in result.outcome.detail["mismatch_summary"]), (
        result.outcome.detail
    )
    assert any("source_path" in line for line in result.outcome.detail["mismatch_summary"])
    assert {
        key: value
        for key, value in _snapshot(manifest.parent).items()
        if not key.startswith("dependency-receipts/")
    } == before

"""Public durable resume refusal diagnostics and immutable attempt evidence."""

import json
from pathlib import Path

import pytest

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record.codec import canonical_json_bytes
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_coverage_invocation import project


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _interrupted_simulation_record(
    root,
    monkeypatch,
    *,
    parameter_datatype="int",
    parameter_default="1",
    tests_toml='[sim_0]\ntests=["reset", "wrap"]\n',
):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr("booley.flows.sim.flow.git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr("booley.flows.sim.flow_resume.git_full_sha", lambda *_args: "a" * 40)
    monkeypatch.setattr("booley.flows.sim.record.resume.git_full_sha", lambda *_args: "a" * 40)
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
    data = root / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(tests_toml, newline="\n")
    reports = root / "reports"

    executions = []

    def interrupt_execution(_executor, _request):
        executions.append(_request.attempt_id)
        if _request.child_execution_id is None:  # a child lane's attempt is already published
            _executor.prepare_attempt(_request)
        raise KeyboardInterrupt("durable ordinary interruption")

    monkeypatch.setattr(
        "booley.flows.sim.flow_resume.OrdinaryHdlSerialExecutor.execute", interrupt_execution
    )

    with pytest.raises(KeyboardInterrupt, match="durable ordinary interruption"):
        result = SimulateFlow().execute(
            SimRequest(target="sim_0", work_dir=root, report_dir=reports)
        )
        pytest.fail(str(result.outcome))
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    assert list(manifest.parent.rglob("attempt.json"))
    assert (
        json.loads(manifest.read_text())["planning_disclosures"][0]["generated_files"][0]["path"]
        == "src/acme_demo_counter_1/rtl/counter.sv"
    )
    return reports, manifest, executions


def _resume_until_execution(monkeypatch):
    """Make a resume that reaches execution raise instead of running."""

    def resumed(executor, request):
        if request.child_execution_id is None:  # a child lane's attempt is already published
            executor.prepare_attempt(request)
        raise KeyboardInterrupt("resume reached execution")

    monkeypatch.setattr("booley.flows.sim.flow_resume.OrdinaryHdlSerialExecutor.execute", resumed)


def test_resume_accepts_attempts_recorded_with_the_diagnostic_policy(tmp_path, monkeypatch):
    reports, manifest, _executions = _interrupted_simulation_record(tmp_path, monkeypatch)
    old = sorted(manifest.parent.rglob("attempt.json"))
    for path in old:  # the policy shape every Booley before this change wrote
        document = json.loads(path.read_bytes())
        document["policy"]["diagnostic"] = True
        path.write_bytes(canonical_json_bytes(document))
    before = {path: path.read_bytes() for path in old}

    _resume_until_execution(monkeypatch)
    with pytest.raises(KeyboardInterrupt, match="resume reached execution"):
        SimulateFlow().execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
        )

    assert {path: path.read_bytes() for path in old} == before
    new = [path for path in manifest.parent.rglob("attempt.json") if path not in before]
    assert new
    assert all("diagnostic" not in json.loads(path.read_bytes())["policy"] for path in new)


_OWN_SECTION = '[sim_0]\ntests=["reset", "wrap"]\n'


@pytest.mark.parametrize(
    ("tests_toml", "summary"),
    [
        pytest.param(_OWN_SECTION + "# unrelated comment\n", None, id="unrelated-comment"),
        pytest.param('[sim_1]\ntests=["other"]\n' + _OWN_SECTION, None, id="other-target"),
        pytest.param(
            '[sim_0]\ntests=["reset", "wrap", "extra"]\n',
            ["suite changed: +extra"],
            id="test-added",
        ),
        pytest.param(
            _OWN_SECTION + '[sim_0.variants.wrap]\ndefault = "short"\nshort = ["+n=1"]\n',
            ["suite Test changed: wrap"],
            id="variant-added",
        ),
        pytest.param(
            '[sim_0]\ntests=["wrap", "reset"]\n', ["suite order changed"], id="order-changed"
        ),
        pytest.param(
            _OWN_SECTION + 'skip = ["wrap"]\n', ["suite Test changed: wrap"], id="skip-added"
        ),
        pytest.param(
            _OWN_SECTION + 'env = { FLAVOR = "b" }\n',
            ["suite setting changed: env"],
            id="env-changed",
        ),
    ],
)
def test_resume_compares_only_the_resumed_targets_tests(
    tmp_path, monkeypatch, tests_toml, summary
):
    reports, manifest, executions = _interrupted_simulation_record(tmp_path, monkeypatch)
    (tmp_path / ".booley/tests.toml").write_text(tests_toml, newline="\n")
    before = _snapshot(manifest.parent)
    _resume_until_execution(monkeypatch)
    request = SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    refused = None  # the resume reached execution
    try:
        result = SimulateFlow().execute(request)
    except KeyboardInterrupt:
        pass
    else:
        assert result.exit_code == 2
        refused = result.outcome.detail["mismatch_summary"]
    assert refused == summary
    if summary is not None:
        assert len(executions) == 1
        assert _snapshot(manifest.parent) == before


def test_resume_of_a_default_invocation_record_refuses_tests_added_to_its_target(
    tmp_path, monkeypatch
):
    reports, manifest, executions = _interrupted_simulation_record(
        tmp_path, monkeypatch, tests_toml="# no Tests for sim_0\n"
    )
    (tmp_path / ".booley/tests.toml").write_text('[sim_0]\ntests=["reset"]\n', newline="\n")
    before = _snapshot(manifest.parent)
    _resume_until_execution(monkeypatch)

    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert result.exit_code == 2
    assert result.outcome.detail["mismatch_summary"] == ["suite changed: +reset"]
    assert len(executions) == 1
    assert _snapshot(manifest.parent) == before


def _prime_prior_refusal(root, reports, manifest, executions):
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=root, report_dir=reports)
    )
    assert result.exit_code == 2
    assert len(executions) == 1


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("verbose", [False, True])
def test_public_resume_cli_summary_detail_and_dry_run_immutability(
    tmp_path, monkeypatch, dry_run, verbose
):
    reports, manifest, executions = _interrupted_simulation_record(tmp_path, monkeypatch)
    (tmp_path / "rtl/counter.sv").write_text("module counter; endmodule\n// change\n")
    durable_before = _snapshot(manifest.parent)
    _prime_prior_refusal(tmp_path, reports, manifest, executions)
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
    assert result.outcome.detail["mismatch_summary"] == [
        "source changed: rtl/counter.sv",
    ]
    assert "/planning_disclosures/" not in result.outcome.report_text or verbose
    assert result.outcome.detail["derived_fingerprint_count"] == 8
    for line in result.outcome.detail["mismatch_summary"]:
        assert line in result.outcome.report_text
    assert "8 derived fingerprints differ" in result.outcome.report_text
    raw = result.outcome.detail["mismatches"]
    assert len(raw) == 16
    assert any(line.startswith("/workload/source_recipe/sources/") for line in raw)
    if verbose:
        assert all(line in result.outcome.report_text for line in raw)
    else:
        assert "sha256:" not in result.outcome.report_text
        assert "/workload/source_recipe/sources/" not in result.outcome.report_text
        assert all(line not in result.outcome.report_text for line in raw)
    if dry_run:
        assert _snapshot(reports) == before
    else:
        assert _snapshot(manifest.parent) == durable_before


@pytest.mark.parametrize(
    ("changed", "line"),
    [
        ("parameter", "parameter WIDTH:"),
        ("option", "build command model changed"),
    ],
)
def test_public_resume_parameters_suite_content_and_opaque_options(
    tmp_path, monkeypatch, changed, line
):
    reports, manifest, executions = _interrupted_simulation_record(tmp_path, monkeypatch)
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
    assert _snapshot(manifest.parent) == before


@pytest.mark.parametrize("removed", ["source", "suite"])
def test_missing_input_distinguishes_source_planning_error_from_suite_comparison(
    tmp_path, monkeypatch, removed
):
    reports, manifest, executions = _interrupted_simulation_record(tmp_path, monkeypatch)
    if removed == "source":
        (tmp_path / "rtl/counter.sv").unlink()
    else:
        (tmp_path / ".booley/tests.toml").write_text('[sim_0]\ntests=["wrap"]\n')
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports, dry_run=True)
    )
    assert result.exit_code == 2
    if removed == "source":
        assert result.outcome.report_text == (
            "Simulation Record resume preview failed: Project compile input is not a file: "
            f"{tmp_path / 'rtl/counter.sv'}"
        )
        assert "mismatch_summary" not in result.outcome.detail
        assert "record workload mismatch" not in result.outcome.report_text
    else:
        assert result.outcome.detail["mismatch_summary"] == ["suite changed: -reset"]
    assert len(executions) == 1


def test_public_resume_bool_to_int_names_parameter_and_explains_command_digest(
    tmp_path, monkeypatch
):
    reports, manifest, executions = _interrupted_simulation_record(
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
    assert _snapshot(manifest.parent) == before


def _rewrite_manifest(manifest, edit):
    """Rewrite the planned manifest through *edit* and re-bind the interrupted attempts."""
    document = json.loads(manifest.read_bytes())
    document.pop("fingerprints")
    edit(document)
    rewritten = finalize_manifest(document)
    manifest.write_bytes(rewritten.canonical_bytes())
    # The interrupted attempt names the manifest it started under; re-bind it to the rewrite.
    for attempt in manifest.parent.rglob("attempt.json"):
        record = json.loads(attempt.read_bytes())
        record["manifest_sha256"] = manifest_digest(rewritten)
        record["workload_sha256"] = rewritten.document["fingerprints"]["workload_sha256"]
        attempt.write_bytes(canonical_json_bytes(record))


def test_legacy_interrupted_simulation_record_new_source_mapping_refuses_before_attempt(
    tmp_path, monkeypatch
):
    reports, manifest, executions = _interrupted_simulation_record(tmp_path, monkeypatch)

    # Rewrite the planned manifest as an older planner wrote it: contract version 1 and no
    # source mapping on the generated files. The attempt directory stays as it was.
    def as_older_planner(document):
        for disclosure in document["planning_disclosures"]:
            disclosure["tool_provenance"]["contract_version"] = "1"
            for entry in disclosure["generated_files"]:
                entry.pop("source_path", None)

    _rewrite_manifest(manifest, as_older_planner)
    document = json.loads(manifest.read_bytes())
    assert document["$schema"] == "booley.simulation-record-manifest/v1"
    assert "source_path" not in document["planning_disclosures"][0]["generated_files"][0]
    before = _snapshot(manifest.parent)
    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )
    assert result.exit_code == 2
    assert len(executions) == 1
    assert any("source_path" in line for line in result.outcome.detail["mismatch_summary"])
    assert _snapshot(manifest.parent) == before


_LEGACY_HINT = (
    "this Simulation Record predates per-Target Test fingerprints: "
    "restore tests.toml or start a new run"
)


@pytest.mark.parametrize(
    ("case", "summary"),
    [
        ("registered-unchanged", None),
        ("registered-comment", ["suite changed: .booley/tests.toml", _LEGACY_HINT]),
        ("unnamed-tests-added", ["suite changed: +reset", _LEGACY_HINT]),
    ],
)
def test_legacy_record_resumes_only_against_an_unchanged_suite(
    tmp_path, monkeypatch, case, summary
):
    unnamed = case == "unnamed-tests-added"
    reports, manifest, executions = _interrupted_simulation_record(
        tmp_path,
        monkeypatch,
        **({"tests_toml": "# no Tests for sim_0\n"} if unnamed else {}),
    )
    _rewrite_manifest(manifest, lambda document: document["required_suite"].pop("target_tests"))
    path = tmp_path / ".booley/tests.toml"
    if unnamed:
        path.write_text('[sim_0]\ntests=["reset"]\n', newline="\n")
    elif case == "registered-comment":
        path.write_text(path.read_text() + "# comment\n", newline="\n")
    before = _snapshot(manifest.parent)
    _resume_until_execution(monkeypatch)
    request = SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    if summary is None:
        with pytest.raises(KeyboardInterrupt, match="resume reached execution"):
            SimulateFlow().execute(request)
        return
    result = SimulateFlow().execute(request)
    assert result.exit_code == 2
    assert result.outcome.detail["mismatch_summary"] == summary
    assert len(executions) == 1
    assert _snapshot(manifest.parent) == before


_WORK_DIR_NOTICE = "booley: --work-dir is deprecated; use --project instead (removal after one compatibility release)\n"


@pytest.mark.parametrize("mode", ["success", "failure", "mismatch"])
def test_cli_resume_announces_validated_origin_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    from booley.flows.sim.flow import SimulateFlow
    from tests.flows.sim.test_coverage_flow import _interrupt_coverage_publication
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    reports, _request, _runs = _interrupt_coverage_publication(tmp_path, monkeypatch)
    manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    destination = tmp_path / "destination reports"
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    # Private seam: the interrupted record has its collection already recorded, so resume never
    # calls the injected coverage_execution factory; the executor builder is the first
    # observable point after the origin is announced.
    executor = flow._resume_simulation_record_executor
    witnessed: list[str] = []

    def before_executor(plan):
        witnessed.append(capsys.readouterr().err)
        assert witnessed == [
            _WORK_DIR_NOTICE + f"Simulation Record manifest: {manifest.resolve()}\n"
        ]
        assert manifest.is_file()
        if mode == "failure":
            raise RuntimeError("resume executor unavailable")
        return executor(plan)

    monkeypatch.setattr(flow, "_resume_simulation_record_executor", before_executor)
    if mode == "mismatch":
        (tmp_path / "rtl/counter.sv").write_text("module counter; wire changed; endmodule\n")
    capsys.readouterr()
    result = flow.execute_cli(
        [
            "--quiet",
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(destination),
            "--resume-from",
            str(manifest),
        ]
    )
    output = capsys.readouterr()
    assert result.exit_code == (0 if mode == "success" else 2)
    if mode == "mismatch":
        assert witnessed == []
        assert "Simulation Record manifest: " not in output.err
        assert "  manifest: " not in result.outcome.report_text
        assert "source changed: rtl/counter.sv" in result.outcome.report_text
        assert result.outcome.detail["mismatches"]
    else:
        assert len(witnessed) == 1
        assert f"  manifest: {manifest.resolve()}" in result.outcome.report_text
        assert (
            f"  report: {(destination / 'sim/1/report.json').resolve()}"
            in result.outcome.report_text
        )
        assert result.outcome.report_text.count("  manifest:") == 1

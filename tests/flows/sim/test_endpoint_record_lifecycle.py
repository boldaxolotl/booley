"""Public lifecycle contract for prepared Simulation Record execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_WORK_DIR_NOTICE = "booley: --work-dir is deprecated; use --project instead (removal after one compatibility release)\n"

from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import collect_pre_sim_firings
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.runtime_context import RuntimeLocation
from tests.flows.sim.fault_point_support import inject_faults
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.test_simulation_record_characterization import _AdapterAbortExecution, _plan
from tests.sandbox_venue_fake import place
from tests.time_budget import hang_guard_s


def _refresh_hook_workload_identity(document):
    from booley.flows.sim.record_files import document_digest

    workload = document["workload"]
    variant = document["build_variants"][0]
    digest = document_digest(
        {
            "kind": variant["kind"],
            "source_closure": variant["source_closure"],
            **{
                key: workload[key]
                for key in ("source_recipe", "build_recipe", "eda", "trace", "coverage")
            },
        }
    )
    variant["recipe_sha256"] = digest
    variant["build_variant_id"] = "variant:" + digest.removeprefix("sha256:")
    item = document["work_items"][0]
    item["target"] = document["target"]
    item["role"] = document["target"]["role"]
    item["revision"] = document["target"]["revision"]
    item["build_variant_id"] = variant["build_variant_id"]
    digest = document_digest(
        {
            key: value
            for key, value in item.items()
            if key not in {"work_item_id", "fingerprint_sha256"}
        }
    )
    item["fingerprint_sha256"] = digest
    item["work_item_id"] = "item:0000:" + digest.removeprefix("sha256:")[:16]


def _pre_sim_manifest(access, commands, transform):
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.planning import finalize_manifest
    from tests.flows.sim.simulation_record_test_support import _legacy_disclosures, _manifest_for

    document = json.loads(canonical_json_bytes(_manifest_for(("alpha",), access=access).document))
    document.pop("fingerprints")
    if transform is not None:
        transform(document)
    document["workload"]["source_recipe"]["pre_sim_commands"] = list(commands)
    _refresh_hook_workload_identity(document)
    disclosures = _legacy_disclosures() if access == "legacy-per-test" else {}
    if disclosures:
        document["planning_disclosures"] = [disclosures["alpha"]]
    return finalize_manifest(document), disclosures


def _pre_sim_plan(access, commands, transform):
    from booley.flows.sim.record.model import create_simulation_record_plan

    manifest, disclosures = _pre_sim_manifest(access, commands, transform)
    return create_simulation_record_plan(manifest), disclosures


def _hook_project(tmp_path, commands, live_commands):
    project = tmp_path / "project"
    project.mkdir()
    config = project / ".booley"
    config.mkdir()
    (config / "booley.toml").write_text(
        "[flows.sim]\npre_run_commands = "
        + json.dumps(commands if live_commands is None else live_commands)
        + "\n"
    )
    return project


def _hook_executor(build, disclosures, mutate_bundle):
    from tests.flows.sim.simulation_record_test_support import _executor

    return _executor(
        build,
        {"compile": 0, "durable_reuse": 0, "launch": 0},
        disclosures=disclosures,
        mutate_bundle=mutate_bundle,
    )


def _successful_pre_sim_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
    *,
    commands: tuple[str, ...] = ("echo hook-out", "echo hook-err >&2"),
    mutate_bundle: str = "",
    live_commands: tuple[str, ...] | None = None,
    transform=None,
    invocation_directory: Path | None = None,
    observer=None,
):
    from booley.flows.sim.record.coordinator import (
        SimulationRecord,
    )
    from tests.flows.sim.simulation_record_test_support import _handle

    project = _hook_project(tmp_path, commands, live_commands)
    plan, disclosures = _pre_sim_plan(access, commands, transform)
    build = tmp_path / "build"
    build.mkdir()
    (build / "simv").write_bytes(b"image")
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.simulation_target_environment",
        lambda _handle: {},
    )
    invocation = invocation_directory or tmp_path / "reports/1"
    invocation.mkdir(parents=True, exist_ok=True)
    simulation_record = SimulationRecord(_hook_executor(build, disclosures, mutate_bundle)).run(
        _observed_simulation_record_request(plan, project, invocation, observer)
    )
    return invocation, simulation_record


def _observed_simulation_record_request(plan, project, invocation, observer):
    from booley.flows.sim.record.coordinator import (
        NewSimulationRecordRunRequest,
        SimulationRecordPolicy,
    )
    from tests.flows.sim.simulation_record_test_support import _admission

    return NewSimulationRecordRunRequest(
        plan,
        project,
        invocation.parent,
        SimulationRecordPolicy(),
        invocation,
        _admission(),
        pre_sim_firing_published=observer,
    )


def _report_endpoint(flow, invocation, eda_tool):
    """Stand in for the endpoint ``write_report`` reads, around a flow that built the outcome."""
    import time

    return SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        refresh_report_detail=flow.refresh_report_detail,
        name="sim",
        endpoint_kind="flow",
        _reserved_invocation_dir=invocation,
        _start_time=time.monotonic(),
        _selected_target="sim",
        _eda_tool=eda_tool,
        _raw_argv=None,
        report_schema="booley.simulation-report/v3",
        _skip_report_detail_refresh=False,
        _eda_tools={},
        _pre_run_head=None,
        _automatic_target_notice="",
        _automatic_target_notice_published=False,
        _console_publication_requested=True,
        _stdout_witness=None,
    )


def test_downstream_integrity_failure_retains_authenticated_current_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.record import SimulationRecordIntegrityError

    flow = SimulateFlow()
    flow._current_published_pre_sim_keys = set()
    with pytest.raises(SimulationRecordIntegrityError):
        _successful_pre_sim_record(
            tmp_path,
            monkeypatch,
            "immutable",
            mutate_bundle="changed_artifact",
            observer=flow._record_pre_sim_firing,  # Private seam: the corruption lands before attach
        )
    flow._reserved_invocation_dir = tmp_path / "reports/1"
    result = flow._attach_published_pre_sim(  # Private seam: no public run stops between them
        EndpointOutcome(exit_code=2, report_text="integrity error")
    )
    assert len(result.detail["pre_sim_lines"]) == 1
    assert "rc=0" in result.detail["pre_sim_lines"][0]
    assert result.detail["pre_sim_runs"][0]["terminal"] is False


def test_endpoint_reauthentication_never_renders_corrupt_hook_as_authoritative(
    tmp_path, monkeypatch
):
    invocation, simulation_record = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"])
    # Private seams: the integrity failure and corruption must land between projection and attach,
    # which no public run exposes.
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    assert result.detail["pre_sim_current"] == 1
    firing = simulation_record.pre_sim_firings[0]
    firing.path.write_bytes(firing.path.read_bytes() + b" ")
    result = flow._attach_published_pre_sim(result)
    assert result.exit_code == 2
    assert "pre_run_commands" not in result.report_text
    assert "pre_sim_lines" not in result.detail
    assert "pre_sim_runs" not in result.detail
    assert result.detail["pre_sim_unauthenticated_references"]


@pytest.mark.parametrize("termination", ["disk_budget", "sim_time_stall"])
def test_infrastructure_simulation_record_reaches_persisted_endpoint_reports(
    tmp_path: Path, termination: str
) -> None:
    from booley.flows.endpoint_reporting import write_report
    from booley.flows.sim.record.coordinator import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
    from tests.flows.sim.test_simulation_record_characterization import (
        _AdapterAbortExecution,
        _admission,
        _plan,
    )

    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    execution = _AdapterAbortExecution(eda_tool="verilator", cocotb=True)
    execution.termination = termination
    execution.failure_kind = "infrastructure"
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,
        execution_factory=lambda _options: execution,
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    simulation_record = SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan,
            tmp_path,
            invocation.parent,
            SimulationRecordPolicy(),
            invocation,
            _admission(),
        )
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], report_dir=invocation.parent)
    # Private seam: projects a returned record; the fixture runs no flow around it.
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    endpoint = _report_endpoint(flow, invocation, "verilator")
    assert write_report(endpoint, result) == invocation / "report.json"
    assert result.exit_code == 2
    for path in (invocation / "report.json", invocation.parent / "sim.json"):
        report = json.loads(path.read_bytes())
        detail = report["detail"]["records"]["sim"]
        assert report["exit_code"] == 2
        assert detail["complete"] is True
        assert detail["grade"] == "error"
        assert detail["termination_counts"][termination] > 0
        assert detail["observations"][0]["failure_class"] == "infrastructure"
        assert detail["observations"][0]["detail"]["termination"] == termination


def _simulation_record_cli_args(
    *, work_dir, report_dir, target="", coverage=False, resume_from=None, test=()
):
    args = ["--work-dir", str(work_dir), "--report-dir", str(report_dir)]
    if target:
        args.extend(("--target", target))
    if coverage:
        args.append("--coverage")
    if resume_from is not None:
        args.extend(("--resume-from", str(resume_from)))
    for name in test:
        args.extend(("--test", name))
    return args


def _coverage_cli_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: int, reports: str = "reports"
) -> list[str]:
    """Create a Project with *targets* verilator Targets named ``sim_<n>``, each with one test.

    Returns the coverage CLI arguments that select every Target and write to *reports*.
    """
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",) * targets)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(
        "".join(f'[sim_{index}]\ntests = ["half"]\n' for index in range(targets))
    )
    return _simulation_record_cli_args(
        work_dir=tmp_path,
        report_dir=tmp_path / reports,
        target=",".join(f"sim_{index}" for index in range(targets)),
        coverage=True,
    )


@pytest.mark.parametrize("target_count", [1, 2])
def test_cli_discloses_committed_manifest_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_count: int,
) -> None:
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    argv = _coverage_cli_project(tmp_path, monkeypatch, target_count, "reports with spaces")
    observed: list[Path] = []

    def execution(*_args):
        captured = capsys.readouterr()
        lines = [
            line
            for line in captured.err.splitlines()
            if line.startswith("Simulation Record manifest: ")
        ]
        assert len(lines) == (target_count if not observed else 0)
        for line in lines:
            manifest = Path(line.removeprefix("Simulation Record manifest: "))
            assert manifest.is_absolute() and manifest.is_file()
            observed.append(manifest)
        return NativeExecution()

    flow = SimulateFlow(coverage_execution=execution)
    result = flow.execute_cli(argv)
    assert result.exit_code == 0
    output = capsys.readouterr()
    assert observed
    assert f"  manifest: {observed[0]}" in output.out
    assert (
        f"  report: {(tmp_path / 'reports with spaces/sim/1/report.json').resolve()}" in output.out
    )
    assert output.out.count("  manifest:") == target_count
    assert [path.parents[1].name for path in observed] == [
        f"sim_{index}" for index in range(target_count)
    ]
    # The typed request runs the same Targets and discloses the manifests on the card only.
    typed = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(
            work_dir=tmp_path,
            report_dir=tmp_path / "typed",
            target=argv[argv.index("--target") + 1],
            coverage=True,
        )
    )
    output = capsys.readouterr()
    assert typed.exit_code == 0
    assert output.out == output.err == ""
    assert typed.outcome.report_text.count("  manifest:") == target_count


@pytest.mark.parametrize(
    "failure",
    ["before:manifest_commit", "after:manifest_commit", "executor", "cancel", "interrupt"],
)
def test_cli_recovery_paths_survive_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    from booley.flows.sim.record.coordinator import SimulationRecordCancellationError

    argv = _coverage_cli_project(tmp_path, monkeypatch, 1)
    seen: list[str] = []

    def execution(*_args):
        seen.append(capsys.readouterr().err)
        assert "Simulation Record manifest: " in seen[0]
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "cancel":
            raise SimulationRecordCancellationError("cancelled")
        raise RuntimeError("executor failed")

    def checkpoint(boundary: str) -> None:
        if boundary == failure:
            raise KeyboardInterrupt()

    flow = SimulateFlow(coverage_execution=execution)
    with inject_faults(checkpoint):
        if failure in {"before:manifest_commit", "after:manifest_commit", "interrupt"}:
            with pytest.raises(KeyboardInterrupt):
                flow.execute_cli(argv)
        else:
            result = flow.execute_cli(argv)
            assert result.exit_code in {2, 130}
            assert "  manifest: " in result.outcome.report_text
            assert (
                f"  report: {(tmp_path / 'reports/sim/1/report.json').resolve()}"
                in result.outcome.report_text
            )
    _assert_recovery_announcement(capsys, seen, failure)


def _assert_recovery_announcement(capsys, seen, failure):
    output = capsys.readouterr()
    announcements = "".join(seen) + output.err
    assert announcements.count("Simulation Record manifest: ") == (
        0 if failure == "before:manifest_commit" else 1
    )
    if failure != "before:manifest_commit":
        path = Path(
            next(
                line.removeprefix("Simulation Record manifest: ")
                for line in announcements.splitlines()
                if line.startswith("Simulation Record manifest: ")
            )
        )
        assert path.is_file()


def test_same_flow_second_failure_does_not_reuse_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    argv = _coverage_cli_project(tmp_path, monkeypatch, 1)
    armed = False

    def fail(boundary: str) -> None:
        if armed and boundary == "before:manifest_commit":
            raise OSError("publication failed")

    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    with inject_faults(fail):
        first = flow.execute_cli(argv)
        manifest_line = next(
            line
            for line in first.outcome.report_text.splitlines()
            if line.startswith("  manifest:")
        )
        capsys.readouterr()

        armed = True
        second = flow.execute_cli(argv)
    assert second.exit_code == 2
    assert "manifest:" not in second.outcome.report_text
    assert manifest_line not in second.outcome.report_text
    assert "Simulation Record manifest: " not in capsys.readouterr().err


class _CardExecution(_AdapterAbortExecution):
    passing = False

    def _invoke_process(self, command, *, timeout):
        result = super()._invoke_process(command, timeout=timeout)
        if self.passing and "BOOLEY_BUILD_STAGE" not in command[-1]:
            identity = work_transport_identity(launched_work(command))
            assert identity is not None
            selected = identity.selected_tests
            write_adapter_result(
                identity,
                AdapterResult(
                    True,
                    False,
                    0,
                    selected,
                    test_results=tuple(AdapterTestResult(name, "pass") for name in selected),
                ),
            )
            assert self.cocotb_path is not None
            payload = json.loads(self.cocotb_path.read_text())
            for test in payload["tests"]:
                test.update(status="pass", failure="")
            self.cocotb_path.write_text(json.dumps(payload))
        return result


@pytest.mark.parametrize("grade", ["pass", "fail", "interrupt"])
def test_ordinary_cli_manifest_and_card_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], grade: str
) -> None:
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    flow = SimulateFlow()
    # Private seam: the cocotb card execution has no public Project fixture.
    monkeypatch.setattr(flow, "_ordinary_simulation_record_plan", lambda *_args, **_kwargs: plan)
    seen: list[Path] = []

    execution = _CardExecution(eda_tool="verilator", cocotb=True)
    execution.passing = grade == "pass"

    def factory(_options):
        output = capsys.readouterr()
        announcements = [
            line.removeprefix("Simulation Record manifest: ")
            for line in output.err.splitlines()
            if line.startswith("Simulation Record manifest: ")
        ]
        assert len(announcements) == 1
        path = Path(announcements[0])
        assert path.is_absolute() and path.is_file()
        seen.append(path)
        if grade == "interrupt":
            raise KeyboardInterrupt()
        return execution

    monkeypatch.setattr(
        "booley.flows.sim.flow.OrdinaryHdlSerialExecutor",
        lambda **kwargs: OrdinaryHdlSerialExecutor(**kwargs, execution_factory=factory),
    )
    argv = _simulation_record_cli_args(
        work_dir=str(tmp_path),
        report_dir=str(tmp_path / "reports"),
        target="sim",
        test=(
            "count",
            "reset",
        ),
    )
    if grade == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            flow.execute_cli(argv)
        assert len(seen) == 1 and seen[0].is_file()
        assert "Simulation Record manifest: " not in capsys.readouterr().err
        return
    result = flow.execute_cli(argv)
    assert seen
    assert result.exit_code == (0 if grade == "pass" else 1)
    assert f"  manifest: {seen[0]}" in result.outcome.report_text
    report = (tmp_path / "reports/sim/1/report.json").resolve()
    assert f"  report: {report}" in result.outcome.report_text
    assert report.is_file()
    assert result.outcome.report_text.count("  manifest:") == 1

    _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result)


def _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result):
    capsys.readouterr()
    destination = tmp_path / "resumed reports"
    monkeypatch.setattr("booley.flows.sim.record.resume.git_full_sha", lambda *_args: "abc123")
    resumed = flow.execute_cli(
        _simulation_record_cli_args(
            work_dir=str(tmp_path), report_dir=str(destination), resume_from=str(seen[0])
        )
    )
    assert resumed.exit_code == result.exit_code
    report = json.loads((destination / "sim.json").read_bytes())
    assert report.get("eda_tool") == plan.manifest.document["workload"]["eda"]["kind"]
    checkpoint = json.loads(next(destination.glob("sim/[0-9]*/progress.json")).read_text())
    assert checkpoint["complete"] is True
    assert checkpoint["completed_targets"] == ["sim"]
    assert "active" not in checkpoint
    assert f"  manifest: {seen[0]}" in resumed.outcome.report_text
    resumed_report = (destination / "sim/1/report.json").resolve()
    assert f"  report: {resumed_report}" in resumed.outcome.report_text
    assert resumed_report.is_file()


def test_cli_manifest_is_flushed_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from tests.flows.sim.test_coverage_transaction import NativeExecution

    class BufferedConsole(io.StringIO):
        def __init__(self):
            super().__init__()
            self.pending = ""

        def write(self, value):
            self.pending += value
            return len(value)

        def flush(self):
            super().write(self.pending)
            self.pending = ""

    argv = _coverage_cli_project(tmp_path, monkeypatch, 1)
    stream = BufferedConsole()
    monkeypatch.setattr("booley.flows.sim.flow.sys.stderr", stream)

    def execution(*_args):
        assert stream.pending == ""
        manifest = Path(
            stream.getvalue()
            .removeprefix(_WORK_DIR_NOTICE)
            .strip()
            .removeprefix("Simulation Record manifest: ")
        )
        assert manifest.is_absolute() and manifest.is_file()
        return NativeExecution()

    result = SimulateFlow(coverage_execution=execution).execute_cli(["--quiet", *argv])
    assert result.exit_code == 0


def test_partial_publication_discloses_only_committed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _coverage_cli_project(tmp_path, monkeypatch, 2)
    commits = 0

    def checkpoint(boundary):
        nonlocal commits
        if boundary == "before:manifest_commit":
            commits += 1
            if commits == 2:
                raise OSError("second publication failed")

    def executor(*_args):
        raise AssertionError("work must wait for every manifest")

    with inject_faults(checkpoint):
        result = SimulateFlow(coverage_execution=executor).execute_cli(argv)
    output = capsys.readouterr()
    assert result.exit_code == 2
    assert output.err.count("Simulation Record manifest: ") == 1
    assert result.outcome.report_text.count("  manifest: ") == 1
    manifest = Path(
        next(
            line.removeprefix("  manifest: ")
            for line in result.outcome.report_text.splitlines()
            if line.startswith("  manifest: ")
        )
    )
    assert manifest.parts[-4:] == ("targets", "sim_0", "record", "manifest.json")
    assert manifest.is_absolute() and manifest.is_file()


def test_failure_paths_deduplicate_and_start_without_blank_line(tmp_path: Path) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path / "reports")
    path = tmp_path / "manifest.json"
    with flow.publication_resources:
        # Private seam: no public run publishes one manifest twice.
        result = flow._with_simulation_record_paths(EndpointOutcome(), [path, path])
    report = (tmp_path / "reports/sim/1/report.json").resolve()
    assert result.report_text == f"  manifest: {path.resolve()}\n  report: {report}"


def _public_cycle_simulation_record(tmp_path, monkeypatch):
    from booley.flows.sim.record.coordinator import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.model import create_simulation_record_plan
    from tests.flows.sim.simulation_record_test_support import (
        _admission,
        _named_manifest,
        _shared_executor,
    )

    project = tmp_path / "project"
    project.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "simv").write_bytes(b"image")
    (build / "input.bin").write_bytes(b"pristine")
    log = build / "run.log"
    log.write_text("PASS")
    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim", project_root=project, selector="sim", eda_tool="icarus"
    )
    executor = _shared_executor(build, log, handle, [], [0], [])
    invocation = tmp_path / "reports/sim/1"
    invocation.mkdir(parents=True)
    plan = create_simulation_record_plan(_named_manifest(("smoke",)))
    with monkeypatch.context() as local:
        local.setattr(
            "booley.flows.sim.record.serial_execution.TargetCatalog.build",
            lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
        )
        return SimulationRecord(executor).run(
            NewSimulationRecordRunRequest(
                plan,
                project,
                invocation.parent,
                SimulationRecordPolicy(),
                invocation,
                _admission(),
            )
        )


def _public_cycle_flow(simulation_record, monkeypatch):
    from contextlib import nullcontext

    from booley.flows.endpoint_state import EndpointState

    flow = SimulateFlow()
    # Private seams kept: the run is stubbed at the prepared boundary, so the real
    # state gate has no Project to read.
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.flows.sim.record_invocation import SimulationRecordInvocation
    from tests.flows.invocation_doubles import prepare_as

    prepared = PreparedSimulationEndpoint((), None, (), {})
    prepare_as(monkeypatch, flow, SimulationRecordInvocation(flow, prepared))
    monkeypatch.setattr(EndpointState, "admission", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr(
        flow,
        "run_prepared_simulation",
        # Private seam: the stubbed run publishes the record built above as the real run does.
        lambda *_args: flow._with_projection_failures(
            flow._simulation_record_endpoint_outcome(
                [simulation_record] if flow._publish_projection(simulation_record) else []
            )
        ),
    )
    return flow


def _cycle_publication_fault(flow, monkeypatch, failure):
    from booley.flows import endpoint_reporting
    from booley.flows.sim import flow as module
    from booley.flows.sim import record_presentation

    original_dumps, original_write = json.dumps, endpoint_reporting.atomic_write_json
    writes = []

    def encode(value, *args, **kwargs):
        rows = value.get("cycle_counts", value) if isinstance(value, dict) else {}
        if failure == "metadata" and rows == {"sim": [{"test": "smoke", "cycle_count": 1234}]}:
            raise ValueError("cycle metadata cannot encode")
        return original_dumps(value, *args, **kwargs)

    def write(destination, document):
        writes.append(destination)
        if failure in {"atomic", "atomic_console"} and len(writes) == 1:
            raise OSError("atomic write failed")
        return original_write(destination, document)

    def fail(*args, **kwargs):
        raise OSError("injected " + str(failure))

    monkeypatch.setattr(json, "dumps", encode)
    monkeypatch.setattr(endpoint_reporting, "atomic_write_json", write)
    if failure in {"console", "atomic_console"}:
        # Private: the console writer has no public fault seam.
        monkeypatch.setattr(type(flow), "_publish_console_report", fail)
    if failure == "acceptance":
        monkeypatch.setattr(module, "publish_simulation_record_projections", fail)
    if failure in {"render", "structured"}:
        # Private: "render" is looked up by the presentation helpers, "structured" by the flow; no public fault seam.
        target, name = (
            (record_presentation, "_simulation_record_test_run_lines")
            if failure == "render"
            else (module, "_simulation_record_structured_details")
        )
        monkeypatch.setattr(target, name, fail)
    return writes


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "metadata",
        "console",
        "atomic",
        "atomic_console",
        "acceptance",
        "render",
        "structured",
    ],
)
def test_public_cycle_metadata_survives_publication_faults(tmp_path, monkeypatch, failure):
    simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    flow = _public_cycle_flow(simulation_record, monkeypatch)
    writes = _cycle_publication_fault(flow, monkeypatch, failure)
    reports = tmp_path / "reports"
    result = flow.execute_cli(
        ["--target", "sim", "--work-dir", str(tmp_path), "--report-dir", str(reports)]
    )
    report = json.loads((reports / "sim.json").read_text())
    assert result.exit_code == (0 if failure in {None, "metadata"} else 2)
    if failure == "metadata":
        assert report["passed"] is True and report["cycle_counts_error"] == "unavailable"
        assert "cycle_counts" not in report
    else:
        assert report["cycle_counts"] == {"sim": [{"test": "smoke", "cycle_count": 1234}]}
    numbered = sorted(reports.glob("sim/*/report.json"))
    assert len(numbered) == (2 if failure in {"console", "atomic_console"} else 1)
    assert len(writes) == {"atomic": 3, "console": 4, "atomic_console": 5}.get(failure, 2)
    assert json.loads(numbered[-1].read_text()) == report
    if failure in {None, "metadata"}:
        projection = json.loads(next(reports.glob("sim/*/targets/*/simulation.json")).read_text())
        assert projection["tests"][0]["cycles"] == 1234
    # An unpublished Target keeps its counts but has no card entry or record detail.
    if failure not in {"render", "structured", "acceptance"}:
        assert report["detail"]["records"]["sim"]["observations"][0]["cycle_count"] == 1234
        assert "smoke: cycles=1234" in result.outcome.report_text


@pytest.mark.parametrize("cancelled", [False, True])
def test_ordinary_partial_failure_keeps_report_counts_separate(tmp_path, monkeypatch, cancelled):
    """The first Target's counts reach the report; only it publishes."""
    from booley.flows.sim.record.coordinator import (
        SimulationRecord,
        SimulationRecordCancellationError,
    )
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    original_run = SimulationRecord.run
    returned = []

    def run(simulation_record, request):
        if returned:
            error = SimulationRecordCancellationError if cancelled else RuntimeError
            raise error("second target stopped")
        outcome = original_run(simulation_record, request)
        returned.append(outcome)
        return outcome

    monkeypatch.setattr(SimulationRecord, "run", run)
    result = flow.execute(
        SimRequest(target="sim_a,sim_b", work_dir=project, report_dir=tmp_path / "reports")
    )
    assert result.exit_code != 0
    if cancelled:
        assert "targets" not in result.outcome.detail and "records" not in result.outcome.detail
    report = json.loads((tmp_path / "reports/sim.json").read_text())
    assert list(report["cycle_counts"]) == ["sim_a"]
    published = (tmp_path / "reports").glob("sim/*/targets/*/simulation.json")
    assert [path.parent.name for path in published] == ["sim_a"]


@pytest.mark.parametrize("unnamed", [False, True])
def test_authenticated_nullable_counts_round_trip(tmp_path, monkeypatch, unnamed):
    from dataclasses import replace

    from booley.flows.sim.request import SimRequest
    from tests.flows.sim import simulation_record_test_support as fixtures

    if unnamed:
        simulation_record = _unnamed_cycle_simulation_record(tmp_path, monkeypatch)
    else:
        original = fixtures._shared_outcome

        def null_outcome(*args):
            outcome = original(*args)
            return replace(
                outcome, tests=tuple(replace(test, cycles=None) for test in outcome.tests)
            )

        monkeypatch.setattr(fixtures, "_shared_outcome", null_outcome)
        simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    flow = SimulateFlow()
    flow._args = SimRequest(target="sim", work_dir=tmp_path, report_dir=tmp_path / "reports")
    # Private seam: projects a returned record; the fixture runs no flow around it.
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    _assert_nullable_cycle_report(flow, result, tmp_path, unnamed)


def _unnamed_cycle_simulation_record(tmp_path, monkeypatch):
    from booley.flows.sim.record.coordinator import (
        NewSimulationRecordRunRequest,
        SimulationRecord,
        SimulationRecordPolicy,
    )
    from booley.flows.sim.record.model import create_simulation_record_plan
    from tests.flows.sim import simulation_record_test_support as fixtures
    from tests.flows.sim.simulation_record_test_support import _admission, _handle

    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    invocation = tmp_path / "reports/sim/1"
    invocation.mkdir(parents=True)
    plan = create_simulation_record_plan(fixtures._unfiltered_cocotb_manifest())
    executor = fixtures._failed_shared_executor(
        tmp_path / "engine-build", {"compile": 0, "launch": 0}
    )
    return SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan,
            project,
            invocation.parent,
            SimulationRecordPolicy(),
            invocation,
            _admission(),
        )
    )


def _assert_nullable_cycle_report(flow, result, tmp_path, unnamed):
    from booley.flows.endpoint_reporting import write_report

    with flow.publication_resources:
        path = write_report(flow, result)
    report = json.loads(path.read_text())
    expected = [{"test": None if unnamed else "smoke", "cycle_count": None}]
    assert report["cycle_counts"]["sim"] == expected
    assert report == json.loads((tmp_path / "reports/sim.json").read_text())
    preview = report["detail"]["records"]["sim"]["observations"]
    assert preview[0]["cycle_count"] is None
    assert preview[0]["test"] == expected[0]["test"]


def _three_target_coverage_origin(tmp_path, monkeypatch):
    from tests.flows.sim.test_coverage_flow import _prepare_coverage_origin

    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    core = tmp_path / "counter.core"
    target = core.read_text().split("targets:\n", 1)[1]
    core.write_text(
        core.read_text() + target.replace("sim_0:", "sim_1:") + target.replace("sim_0:", "sim_2:")
    )
    (tmp_path / ".booley/tests.toml").write_text(
        '[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n[sim_2]\ntests=["reset"]\n'
    )
    return reports


def _assert_coverage_fault(fault, result, reports, returned, terminal_phases, monkeypatch, capsys):
    detail = result.outcome.detail
    report = json.loads((reports / "sim/1/report.json").read_text())
    counted = {
        "progress": 1,
        "terminal_progress": 3,
        "retention": 2,
        "run": 1,
        "cancel": 1,
        "projection": 3,
    }[fault]
    assert list(report["cycle_counts"]) == [f"sim_{index}" for index in range(counted)]
    if fault == "terminal_progress":
        assert detail["completion_error"]["operation"] == "publish coverage progress"
        assert terminal_phases == ["complete", "aborted"]
        assert list(detail["targets"]) == ["sim_0", "sim_1", "sim_2"]
    elif fault == "progress":
        assert list(detail["targets"]) == ["sim_0"]
        assert detail["completion_error"]["path"] == str(reports / "sim/1/progress.json")
    elif fault == "cancel":
        assert "targets" not in detail and "records" not in detail
        assert all(row[0]["cycle_count"] is None for row in report["cycle_counts"].values())
        coverage_record = (returned[0].manifest_path.parents[1] / "coverage.json").resolve()
        assert f"  Coverage Record: {coverage_record}" in result.outcome.report_text.splitlines()
        assert _coverage_uncovered_exit(monkeypatch, capsys, coverage_record) == 0
    elif fault == "projection":
        error = detail["completion_error"]
        assert error["operation"] == "publish Simulation Record projections"
        assert Path(error["path"]).parts[-3:] == ("targets", "sim_1", "simulation.json")
        assert terminal_phases == ["aborted"]
        progress = json.loads((reports / "sim/1/progress.json").read_text())
        assert progress["phase"] == "aborted"
        assert progress["completed_targets"] == ["sim_0", "sim_2"]
        assert progress["pending_targets"] == ["sim_1"]
        lines = [
            line
            for line in result.outcome.report_text.splitlines()
            if line.startswith("  Coverage Record: ")
        ]
        assert [Path(line.split(": ", 1)[1]).parent.name for line in lines] == ["sim_0", "sim_2"]
    if fault in ("retention", "run"):
        # Retaining the failed Target would make its projection fail with a completion error.
        assert "completion_error" not in detail
    if fault not in ("cancel", "projection"):
        assert report["detail"]["pending_targets"] == (
            [] if fault == "terminal_progress" else ["sim_1", "sim_2"]
        )
    # Only the Targets the flow retains for publication get a Simulation projection.
    published = {path.parent.name for path in reports.glob("sim/1/targets/*/simulation.json")}
    retained = {
        "progress": {"sim_0"},
        "terminal_progress": {"sim_0", "sim_1", "sim_2"},
        "retention": {"sim_0"},
        "run": {"sim_0"},
        "cancel": {"sim_0"},
        "projection": {"sim_0", "sim_2"},
    }
    assert published == retained[fault]


def _coverage_uncovered_exit(monkeypatch, capsys, coverage_json: Path) -> int:
    """Exit code of ``booley coverage uncovered`` on a Coverage Record, run as a host terminal."""
    from tests.harness.test_coverage_cli import _booley

    place(monkeypatch, RuntimeLocation.HOST)
    return _booley(monkeypatch, capsys, "coverage", "uncovered", str(coverage_json))[0]


def _fail_projection_of(monkeypatch, target: str) -> None:
    """Make writing *target*'s ``simulation.json`` fail, as a full disk would.

    Patches Booley's own writer: no public entry point injects a disk-write fault.
    """
    from booley.flows.sim import acceptance

    original = acceptance.write_compatibility_projection

    def write(path, *args, **kwargs):
        if Path(path).parent.name == target:
            raise OSError(5, "projection unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(acceptance, "write_compatibility_projection", write)


@pytest.mark.parametrize(
    "fault", ["progress", "terminal_progress", "retention", "run", "cancel", "projection"]
)
def test_coverage_target_fault_keeps_completed_targets(tmp_path, monkeypatch, capsys, fault):
    """A fault after some Targets finished keeps their counts apart from what is published.

    A finished Target's Coverage Record is readable before the next Target starts, and a
    Target whose projection cannot be published stays pending.
    """
    from booley.flows.sim.coverage_progress import CoverageProgress
    from booley.flows.sim.record.coordinator import (
        SimulationRecord,
        SimulationRecordCancellationError,
    )
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution

    reports = _three_target_coverage_origin(tmp_path, monkeypatch)
    original_run = SimulationRecord.run
    original_checkpoint = CoverageProgress.checkpoint
    returned = []
    readable = []
    terminal_phases = []

    def run(simulation_record, request):
        if len(returned) == 1 and fault != "progress":
            # The second Target is starting: the first one is already readable.
            first = returned[0].manifest_path.parents[1] / "coverage.json"
            readable.append(_coverage_uncovered_exit(monkeypatch, capsys, first))
        if len(returned) == 1 and fault == "run":
            raise RuntimeError("second run failed before outcome")
        if len(returned) == 1 and fault == "cancel":
            raise SimulationRecordCancellationError("cancelled")
        outcome = original_run(simulation_record, request)
        returned.append(outcome)
        if fault == "retention" and len(returned) == 2:
            # The real Coverage Record authentication rejects an empty record.
            (outcome.manifest_path.parents[1] / "coverage.json").write_text("{}")
        return outcome

    def checkpoint(progress, *, complete=False, phase=None):
        if fault == "progress" and not complete and progress.outcomes:
            raise OSError("progress unavailable")
        if complete:
            terminal_phases.append(phase)
        if fault == "terminal_progress" and complete:
            raise OSError("terminal progress unavailable")
        return original_checkpoint(progress, complete=complete, phase=phase)

    monkeypatch.setattr(SimulationRecord, "run", run)
    monkeypatch.setattr(CoverageProgress, "checkpoint", checkpoint)
    if fault == "projection":
        _fail_projection_of(monkeypatch, "sim_1")
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(
            target="sim_0,sim_1,sim_2", work_dir=tmp_path, coverage=True, report_dir=reports
        )
    )
    # Observed while the second Target started: `booley coverage uncovered` took the first's record.
    assert readable == ([] if fault == "progress" else [0])
    assert result.exit_code == (130 if fault == "cancel" else 2)
    _assert_coverage_fault(fault, result, reports, returned, terminal_phases, monkeypatch, capsys)


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_simulation_record_executor_uses_frozen_hook_commands_despite_live_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    _invocation, outcome = _successful_pre_sim_record(
        tmp_path,
        monkeypatch,
        access,
        commands=("echo frozen-hook",),
        live_commands=("echo live-hook; exit 7",),
    )
    assert outcome.complete
    assert len(outcome.pre_sim_firings) == 1
    firing = outcome.pre_sim_firings[0].document
    assert firing["returncode"] == 0
    assert firing["stdout_tail"] == "frozen-hook\n"
    assert firing["command_count"] == 1


def _ordinary_public_hook_fixture(tmp_path, monkeypatch):
    import os

    from booley.flows.sim.execution import SimulationExecution, SimulationOptions
    from tests.flows.sim.test_execution_engine import (
        _subprocess_invoker,
        _write_stale_compiler_fixture,
    )

    project, _source = _write_stale_compiler_fixture(tmp_path)
    (project / ".booley/booley.toml").write_text(
        "[flows.sim]\npre_run_commands = " + json.dumps(["echo public-hook"]) + "\n"
    )
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a", "b"]\n')
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        # Private seams: the fake-EDA tool identity and execution have no public Project fixture.
        "booley.flows.sim.build_session._icarus_tool_identity",
        lambda: "test-tool-closure",
    )
    flow = SimulateFlow()
    # Publication tests must allow for Git for Windows shell startup under CI load.
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(timeout_ms=hang_guard_s() * 1000),
    )
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    return project, flow


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
@pytest.mark.parametrize("hook_exit", [0, 7])
def test_public_ordinary_prepared_endpoint_reports_each_shared_build_launch(
    tmp_path, monkeypatch, capsys, access, hook_exit
):
    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    command = "echo public-hook; echo public-err >&2" + ("; exit 7" if hook_exit else "")
    (project / ".booley/booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = {json.dumps([command])}\n"
        f'pre_sim_build_access = "{access}"\n'
    )
    result = flow.execute_cli(
        [
            "--target",
            "sim_a",
            "--work-dir",
            str(project),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    # A failing hook gives the run a nonzero exit; its adapter grade stays what the tests earned.
    assert result.exit_code == (1 if hook_exit else 0), result.outcome.report_text
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    lines = report["detail"]["pre_sim_lines"]
    assert len(lines) == report["detail"]["pre_sim_current"] == 2
    assert any(f"for sim_a/a: rc={hook_exit}" in line for line in lines)
    assert any(f"for sim_a/b: rc={hook_exit}" in line for line in lines)
    assert all(report["report_text"].count(line) == 1 for line in lines)
    console = capsys.readouterr().out
    if not hook_exit:  # a failing run prints its card on the CLI's error path instead
        assert all(console.count(line) == 1 for line in lines)
    assert (tmp_path / "reports/sim/1/report.json").read_bytes() == (
        tmp_path / "reports/sim.json"
    ).read_bytes()
    manifest = tmp_path / "reports/sim/1/targets/sim_a/record/manifest.json"

    firings = collect_pre_sim_firings(manifest)
    assert len(firings) == 2
    simulation_record_root = manifest.parent
    # One shared build for immutable access; legacy access builds privately per test.
    shared_builds = simulation_record_root.glob("build-variants/*/attempts/*/build-result.json")
    assert len(list(shared_builds)) == (1 if access == "immutable" else 0)
    assert all(firing.document["stdout_tail"] == "public-hook\n" for firing in firings)
    assert all(firing.document["stderr_tail"] == "public-err\n" for firing in firings)
    assert all(firing.document["returncode"] == hook_exit for firing in firings)


@pytest.mark.parametrize("mode", ["dry_run", "elab_only"])
def test_public_nonrunning_modes_create_no_hook_firings(tmp_path, monkeypatch, mode):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    options = {"dry_run": True} if mode == "dry_run" else {"mode": "elab_only"}
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports", **options)
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert not list((tmp_path / "reports").glob("**/pre-sim/*.json"))
    assert "pre_run_commands (" not in result.outcome.report_text


def _cocotb_hook_plan(document):
    import hashlib

    from tests.flows.sim.test_simulation_record_manifest_codec import _selection_test_runs

    names = ["alpha", "beta"]
    document["work_items"][0].update(
        kind="cocotb_batch",
        selection={"kind": "named", "names": names},
        arguments=names,
        test_runs=_selection_test_runs(names),
    )
    source = b"alpha\nbeta\n"
    document["required_suite"].update(
        names=names,
        source_bytes=len(source),
        source_sha256="sha256:" + hashlib.sha256(source).hexdigest(),
    )


def test_actual_hook_fires_once_for_two_test_cocotb_batch(tmp_path, monkeypatch):
    from dataclasses import replace

    from booley.flows.sim.execution.contract import (
        SimulationArtifactEvidence,
        SimulationTestOutcome,
    )
    from tests.flows.sim.simulation_record_test_support import _Group

    original = _Group.launch_bundle

    def two_results(group, bundle, evidence_root, run_cwd):
        outcome = original(group, bundle, evidence_root, run_cwd)

        evidence_root.mkdir(parents=True, exist_ok=True)
        transport = evidence_root / "cocotb-results.json"
        transport.write_text(
            json.dumps(
                {
                    "state": "ok",
                    "tests": [{"name": name, "status": "pass"} for name in group.names],
                }
            )
        )
        return replace(
            outcome,
            tests=tuple(
                SimulationTestOutcome(name=name, verdict="pass", passed=True)
                for name in group.names
            ),
            artifacts=(
                SimulationArtifactEvidence(
                    "cocotb_results_json", str(transport), transport.stat().st_size, group.names
                ),
            ),
        )

    monkeypatch.setattr(_Group, "launch_bundle", two_results)
    invocation, outcome = _successful_pre_sim_record(
        tmp_path, monkeypatch, "immutable", transform=_cocotb_hook_plan
    )
    assert outcome.aggregate_grade == "pass"
    firings = collect_pre_sim_firings(invocation / "targets/sim/record/manifest.json")
    assert len(firings) == 1
    assert firings[0].document["test_names"] == ("alpha", "beta")
    assert firings[0].document["stdout_tail"] == "hook-out\n"


@pytest.mark.parametrize("corrupt", [False, True])
def test_public_ticket_integrity_downgrade_never_publishes_true(tmp_path, monkeypatch, corrupt):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    project_outcome = flow._simulation_record_endpoint_outcome

    def corrupt_after_projection(outcomes):
        result = project_outcome(outcomes)
        if corrupt:
            path = outcomes[0].pre_sim_firings[0].path.parents[3] / "result.json"
            path.write_text("{}")
        return result

    # Private seam: the record must be corrupted between projection and attach.
    monkeypatch.setattr(flow, "_simulation_record_endpoint_outcome", corrupt_after_projection)
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports")
    )
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    assert result.exit_code == (2 if corrupt else 0), result.outcome.report_text
    assert "criterion_met" not in report and "criterion_key" not in report
    if corrupt:
        assert "Simulation Record integrity failure" in report["report_text"]
        assert report["detail"]["pre_sim_current"] == 2
        assert len(report["detail"]["pre_sim_lines"]) == 2


_UNADMITTED = {
    "ordinary": ("sim: Simulation Record has no borrowed admission context", {"mode": "simulate"}),
    "coverage": (
        "sim: coverage Simulation Record has no borrowed admission context",
        {"mode": "simulate"},
    ),
    "resume": (
        "sim: no admission context",
        {"manifest_sha256": "sha256:x", "completed": ["a"], "interrupted": [], "pending": ["b"]},
    ),
}


@pytest.mark.parametrize("kind", ["ordinary", "coverage", "resume"])
def test_simulation_record_kinds_refuse_without_admission(tmp_path: Path, kind: str) -> None:
    from booley.flows.sim.flow import (
        OrdinarySimulationRecordPlanning,
        PreparedSimulationEndpoint,
    )
    from booley.flows.sim.request import SimRequest

    flow = SimulateFlow()
    flow._args = SimRequest(
        target="sim", work_dir=tmp_path, report_dir=tmp_path, coverage=kind == "coverage"
    )
    flow._coverage_prepared = SimpleNamespace(targets=[])
    if kind == "resume":
        # Keeps the private call: a public resume needs a validated manifest on disk.
        recovery = SimpleNamespace(
            manifest_sha256="sha256:x", completed=("a",), interrupted=(), pending=("b",)
        )
        planning = OrdinarySimulationRecordPlanning((), {})
        validated = SimpleNamespace(path=tmp_path / "manifest.json")
        result = flow._execute_simulation_record(
            None, flow._resume_simulation_record_run(validated, recovery, planning)
        )
    else:
        with flow.publication_resources:
            result = flow.run_prepared_simulation(
                PreparedSimulationEndpoint((), None, (), {}), None
            )

    text, detail = _UNADMITTED[kind]
    assert (result.exit_code, result.report_text, result.detail) == (2, text, detail)


def test_coverage_failure_without_a_prepared_target_reports_the_unmatched_target(
    tmp_path: Path,
) -> None:
    def fail(_request: object) -> None:
        raise RuntimeError("boom")

    # Keeps the private call: a public run prepares every requested Target, so only a direct
    # call reaches a failed Target with no prepared Coverage plan.
    flow = SimulateFlow()
    flow._coverage_prepared = SimpleNamespace(targets=[])
    target = {
        "selector": "sim_0",
        "vlnv": "acme:demo:target:1.0",
        "name": "sim_0",
        "role": "candidate",
        "revision": "0" * 40,
    }
    document = {"target": target}
    request = SimpleNamespace(plan=SimpleNamespace(manifest=SimpleNamespace(document=document)))
    progress = SimpleNamespace(invocation_dir=tmp_path, checkpoint=lambda **_kwargs: None)

    result = flow._execute_coverage_simulation_record_requests(
        SimpleNamespace(run=fail), [request], progress
    )

    assert result.exit_code == 2
    assert "cannot match failed Target to the prepared Coverage plan (boom)" in result.report_text


@pytest.mark.parametrize("borrows", [True, False])
def test_invoke_endpoint_hands_the_admission_only_to_an_invocation_that_borrows_it(
    borrows: bool,
) -> None:
    from booley.flows.endpoint_session import invoke_endpoint

    class Invocation:
        borrows_admission = borrows
        received: object = "unset"

        def run(self, admission: object | None) -> EndpointOutcome:
            self.received = admission
            return EndpointOutcome(report_text="ok")

    admission, invocation = object(), Invocation()
    endpoint = SimpleNamespace(name="sim", _run_roots=None, _finalize_result=lambda _outcome: None)
    invoke_endpoint(
        endpoint, SimpleNamespace(invocation=invocation), admission=admission, started=0.0
    )  # type: ignore[arg-type]

    assert invocation.received is (admission if borrows else None)


def _kind_request(tmp_path, monkeypatch, kind):
    """Build the public flow and request for *kind*; ``resume`` first completes an ordinary run."""
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    if kind == "coverage":
        reports = _prepare_coverage_origin(tmp_path, monkeypatch)
        flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
        request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
        manifest = reports / "sim/1/targets/sim_0/record/manifest.json"
    else:
        project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
        reports = tmp_path / "reports"
        request = SimRequest(target="sim_a", work_dir=project, report_dir=reports)
        manifest = reports / "sim/1/targets/sim_a/record/manifest.json"
        if kind == "resume":
            for name in (
                "booley.flows.sim.flow.git_full_sha",
                "booley.flows.sim.record.resume.git_full_sha",
            ):
                monkeypatch.setattr(name, lambda *_args: "a" * 40)
            assert flow.execute(request).exit_code == 0
            request = SimRequest(
                resume_from=manifest, work_dir=project, report_dir=tmp_path / "resumed"
            )
            flow = SimulateFlow()
    return flow, request, manifest


def _faulty_kind_run(tmp_path, monkeypatch, kind, fault):
    """Run *kind* through the public flow with *fault* injected after its manifest is published.

    ``before_result``: the run stops with ``OSError`` once the manifest is committed.
    ``completion``: the work finishes and only the terminal progress write fails.
    Returns the result, the manifest ``--resume-from`` needs, and the terminal phases tried.
    """
    from booley.flows.sim import coverage_progress, live_progress
    from booley.flows.sim import flow as flow_module
    from booley.flows.sim.record.coordinator import SimulationRecord

    flow, request, manifest = _kind_request(tmp_path, monkeypatch, kind)
    modules = (coverage_progress if kind == "coverage" else flow_module,)
    original_checkpoint = live_progress.publish_checkpoint
    original_publish_new = SimulationRecord.publish_new
    terminal_phases = []

    def checkpoint(path, payload):
        if payload["complete"]:
            terminal_phases.append(payload["phase"])
            if fault == "completion":
                raise OSError("terminal progress unavailable")
        return original_checkpoint(path, payload)

    def fail_after_publication(record, run_request):
        result = original_publish_new(record, run_request)
        if fault == "before_result":
            raise OSError("disk gone")
        return result

    def fail_resumed_run(record, run_request):
        raise OSError("disk gone")

    for module in modules:
        monkeypatch.setattr(module, "publish_checkpoint", checkpoint)
    if fault == "projection":
        from booley.flows.sim import acceptance

        def unavailable(*_args, **_kwargs):
            raise OSError(5, "projection unavailable")

        monkeypatch.setattr(acceptance, "write_compatibility_projection", unavailable)
    if kind == "resume":
        monkeypatch.setattr(flow_module, "publish_checkpoint", checkpoint)
        if fault == "before_result":
            monkeypatch.setattr(SimulationRecord, "run", fail_resumed_run)
    else:
        monkeypatch.setattr(SimulationRecord, "publish_new", fail_after_publication)
    return flow.execute(request), manifest, terminal_phases


@pytest.mark.parametrize("kind", ["ordinary", "coverage", "resume"])
@pytest.mark.parametrize("fault", ["before_result", "completion", "projection"])
def test_simulation_record_kind_failure_cards_name_published_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, fault: str
) -> None:
    result, manifest, terminal_phases = _faulty_kind_run(tmp_path, monkeypatch, kind, fault)
    card = result.outcome.report_text

    assert result.exit_code == 2
    if fault == "completion":
        operation = {
            "ordinary": "publish simulation progress",
            "coverage": "publish coverage progress",
            "resume": "publish Simulation progress",
        }[kind]
        assert result.outcome.detail["completion_error"]["operation"] == operation
        assert terminal_phases == ["complete", "aborted"]
        # The finished work still reaches the card: its verdict is not lost with the write.
        assert " PASS" in card.splitlines()[0]
    elif fault == "projection":
        error = result.outcome.detail["completion_error"]
        assert error["operation"] == "publish Simulation Record projections"
        assert terminal_phases == ["aborted"]
        assert "  Coverage Record: " not in card
        assert result.outcome.detail["pending_targets"] == [
            "sim_0" if kind == "coverage" else "sim_a"
        ]
    else:
        failed = {
            "ordinary": "Simulation Record execution failed",
            "coverage": "Simulation Record coverage planning failed",
            "resume": "Simulation Record resume failed",
        }[kind]
        assert card.startswith(f"{failed}: disk gone")
    # Whatever stopped the run, the card names the manifest --resume-from needs.
    assert card.splitlines().count(f"  manifest: {manifest.resolve()}") == 1


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_prepared_ordinary_invoker_observes_owned_attempt_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
) -> None:
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    with (project / ".booley/booley.toml").open("a") as config:
        config.write(f'pre_sim_build_access = "{access}"\n')
    original = flow._execute_boundary
    observations = []
    reports = tmp_path / "reports"

    def invoke(command, *, timeout=None, cwd=None):
        invocation = flow._reserved_invocation_dir
        if invocation is not None and (invocation / "progress.json").exists():
            progress = json.loads((invocation / "progress.json").read_text())
            for active in progress.get("active", []):
                if active["stage"] == "executing":
                    assert progress["complete"] is False
                    assert progress["completed_targets"] == []
                    pointer = active["log"]
                    assert pointer["live"] is True and pointer["complete"] is False
                    log = project / pointer["path"]
                    assert log.is_file()
                    assert "flow=sim" in log.read_text().splitlines()[0]
                    assert str(log.parent) in launched_work(command).adapter_result_path
                observations.append(active["stage"])
        return original(command, timeout=timeout, cwd=cwd)

    # Keeps the private patch: the fixture's engine invoker runs below the
    # flow's process boundary, where progress.json has no active stage yet.
    monkeypatch.setattr(flow, "_execute_boundary", invoke)
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    assert "building" in observations
    assert observations.count("executing") == 2
    terminal = json.loads((reports / "sim/1/progress.json").read_text())
    assert terminal["completed_targets"] == ["sim_a"]
    assert "active" not in terminal


@pytest.mark.timeout(180)
def test_prepared_simulation_record_active_write_failure_preserves_durable_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.flows.sim import live_progress
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    control_reports = tmp_path / "control-reports"
    control = SimulateFlow().execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=control_reports)
    )
    assert control.exit_code == 0, control.outcome.report_text
    control_simulation_record = control_reports / "sim/1/targets/sim_a/record"
    control_records = sorted(
        (
            json.loads(path.read_text())
            for path in control_simulation_record.glob("work-items/*/result.json")
        ),
        key=lambda record: record["observations"][0]["test"],
    )
    publish = live_progress.write_progress_json
    failures = []

    def fail_active(path, payload, **kwargs):
        if "lock_timeout_s" in kwargs:
            failures.append(payload["phase"])
            raise OSError("advisory checkpoint unavailable")
        return publish(path, payload, **kwargs)

    monkeypatch.setattr(live_progress, "write_progress_json", fail_active)
    reports = tmp_path / "reports"
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    assert failures
    assert "observation_error" in result.outcome.detail
    assert "campaign_error" not in result.outcome.detail
    simulation_record = reports / "sim/1/targets/sim_a/record"
    results = [
        json.loads(path.read_text()) for path in simulation_record.glob("work-items/*/result.json")
    ]
    assert len(results) == 2
    assert all(record["grade"] == "pass" for record in results)
    results.sort(key=lambda record: record["observations"][0]["test"])
    for record, expected in zip(results, control_records, strict=True):
        assert {key: record[key] for key in ("grade", "state", "observations", "diagnostics")} == {
            key: expected[key] for key in ("grade", "state", "observations", "diagnostics")
        }
    builds = [
        json.loads(path.read_text())
        for path in simulation_record.glob("build-variants/*/attempts/*/build-result.json")
    ]
    assert len(builds) == 1
    assert builds[0]["state"] == "ready"
    final = json.loads((reports / "sim/1/progress.json").read_text())
    assert final["completed_targets"] == ["sim_a"]
    assert final["observation_health"]["error"] == "OSError: advisory checkpoint unavailable"


def test_hook_success_survives_build_failure(tmp_path, monkeypatch):
    from tests.flows.sim.simulation_record_test_support import _Group

    def fail_build(group):
        sidecars = list(
            tmp_path.glob("reports/1/targets/sim/record/work-items/*/attempts/*/pre-sim/*.json")
        )
        assert len(sidecars) == 1, "hook must be published before the build fails"
        return SimpleNamespace(passed=False)

    from dataclasses import replace

    from tests.flows.sim.simulation_record_test_support import _failed_shared_outcome

    failed = _failed_shared_outcome()
    failed = replace(failed, tests=(replace(failed.tests[0], name="alpha"),))
    monkeypatch.setattr(_Group, "finish_build_failure", lambda self: failed, raising=False)
    monkeypatch.setattr(_Group, "compile", fail_build)
    invocation, simulation_record = _successful_pre_sim_record(
        tmp_path, monkeypatch, "legacy-per-test"
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"])
    # Private seam: projects a returned record; the fixture runs no flow around it.
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    assert result.exit_code == 1
    assert len(simulation_record.pre_sim_firings) == 1
    firing = simulation_record.pre_sim_firings[0]
    assert firing.document["status"] == "passed" and firing.document["returncode"] == 0
    assert firing.document["stdout_tail"] == "hook-out\n"
    assert firing.document["stderr_tail"] == "hook-err\n"
    assert "rc=0" in result.detail["pre_sim_lines"][0]
    terminal = json.loads((firing.path.parents[3] / "result.json").read_text())
    assert terminal["state"] == "blocked_by_build"
    build = json.loads((firing.path.parent.parent / "private-build/build-result.json").read_text())
    assert build["state"] == "design_failure"
    assert build["observation"]["detail"]["text"] == "compiler tail"


def completed_simulation_record_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reports: Path | None = None
) -> dict[str, Any]:
    """Produce an actual endpoint report with a deterministic fake EDA executable."""
    from booley.flows.sim.request import SimRequest
    from tests.flows.test_endpoint_reporting import fake_accounting

    fake_accounting(monkeypatch)
    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    reports = reports or tmp_path / "reports"
    result = flow.execute(SimRequest(target="sim_a", work_dir=project, report_dir=reports))
    assert result.exit_code == 0, result.outcome.report_text
    return json.loads((reports / "sim.json").read_bytes())


def test_completed_simulation_record_report_records_tool_and_resources(tmp_path, monkeypatch):
    report = completed_simulation_record_report(tmp_path, monkeypatch)
    assert report.get("eda_tool") == "icarus"
    assert report.get("resources") == {"cpu_user_s": 3.75, "cpu_sys_s": 0.75, "peak_rss_mb": 256}


@pytest.mark.parametrize("mode", ["simulate", "elab_only", "coverage"])
def test_simulation_record_report_records_tools_by_target(tmp_path, monkeypatch, mode):
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    if mode == "coverage":
        reports = _prepare_coverage_origin(tmp_path, monkeypatch)
        flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
        request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    else:
        project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
        (project / ".booley/tests.toml").write_text(
            '[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n'
        )
        reports = tmp_path / "reports"
        request = SimRequest(target="sim_a,sim_b", mode=mode, work_dir=project, report_dir=reports)
    result = flow.execute(request)
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((reports / "sim.json").read_bytes())
    if mode == "coverage":
        assert report.get("eda_tool") == "verilator"
    else:
        assert report.get("eda_tools") == {"sim_a": "icarus", "sim_b": "icarus"}
        assert "eda_tool" not in report


def test_partial_simulation_record_planning_keeps_known_and_unknown_tools(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    original = flow._ordinary_simulation_record_plan

    def plan(planned, *args, **kwargs):
        if planned.selector == "sim_b":
            raise ValueError("second Target unavailable")
        return original(planned, *args, **kwargs)

    # Private seam: planning of one Target must fail while the other succeeds; no public input does.
    monkeypatch.setattr(flow, "_ordinary_simulation_record_plan", plan)
    result = flow.execute(
        SimRequest(target="sim_a,sim_b", work_dir=project, report_dir=tmp_path / "reports")
    )
    assert result.exit_code == 2
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    assert report.get("eda_tools") == {"sim_a": "icarus", "sim_b": None}


def test_pre_sim_read_rejects_result_reference_without_published_firing(tmp_path, monkeypatch):
    """The Pre-Sim read boundary binds a terminal result's firing references both ways."""
    from booley.flows.sim.record import SimulationRecordIntegrityError
    from booley.flows.sim.record.store import SimulationRecordStore

    invocation, outcome = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    outcome.pre_sim_firings[0].path.unlink()
    store = SimulationRecordStore(invocation / "targets/sim/record")

    with pytest.raises(SimulationRecordIntegrityError, match="result references disagree"):
        collect_pre_sim_firings(store.manifest_path)


def _spy_simulation_lifecycle(monkeypatch, events, terminal_phases, *, terminal_write_fails):
    """Append lifecycle step names to *events*, optionally failing the terminal progress write."""

    from booley.flows.sim import flow as flow_module

    publish_checkpoint = flow_module.publish_checkpoint
    projections = flow_module.publish_simulation_record_projections
    refresh = SimulateFlow.refresh_report_detail

    def progress(path, payload):
        if payload["completed_targets"] and not payload["complete"]:
            events.append("listed")
        if payload["complete"]:
            terminal_phases.append(payload["phase"])
            events.append("terminal")
            if terminal_write_fails:
                raise OSError("injected terminal progress failure")
        return publish_checkpoint(path, payload)

    def record(work_dir, outcomes):
        projections(work_dir, outcomes)
        events.append("publish")

    def refresh_detail(self, result):
        events.append("refresh")
        return refresh(self, result)

    monkeypatch.setattr(flow_module, "publish_checkpoint", progress)
    monkeypatch.setattr(flow_module, "publish_simulation_record_projections", record)
    monkeypatch.setattr(SimulateFlow, "refresh_report_detail", refresh_detail)


@pytest.mark.parametrize(
    "terminal_write_fails", [False, True], ids=["complete", "terminal_write_fails"]
)
def test_simulation_record_publication_lifecycle_order(
    tmp_path, monkeypatch, terminal_write_fails
):
    """Record publication precedes the progress that lists the Target completed."""
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    events: list[str] = []
    terminal_phases: list[str] = []
    _spy_simulation_lifecycle(
        monkeypatch, events, terminal_phases, terminal_write_fails=terminal_write_fails
    )

    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports")
    )

    first = {name: events.index(name) for name in dict.fromkeys(events)}
    assert first["publish"] < first["listed"] < first["terminal"] < first["refresh"]
    if terminal_write_fails:
        assert result.exit_code == 2
        operation = result.outcome.detail["completion_error"]["operation"]
        assert operation == "publish simulation progress"
        assert terminal_phases[:2] == ["complete", "aborted"]
        # The Pre-Sim firings collected before the failed write still reach the result.
        lines = result.outcome.detail["pre_sim_lines"]
        assert len(lines) == 2
        assert all(result.outcome.report_text.count(line) == 1 for line in lines)
    else:
        assert result.exit_code == 0, result.outcome.report_text

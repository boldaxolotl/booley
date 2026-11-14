"""Public lifecycle contract for prepared Simulation Campaign execution."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_WORK_DIR_NOTICE = "booley: --work-dir is deprecated; use --project instead (removal after one compatibility release)\n"

from booley.flows import endpoint_session
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.flow import OrdinarySimulationRecordPlanning, SimulateFlow
from booley.flows.sim.record import collect_pre_sim_firings
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult, execute_endpoint
from tests.flows.sim.launched_work import launched_work
from tests.flows.sim.test_simulation_record_characterization import _AdapterAbortExecution, _plan


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
    import json

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
    import json

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


def _hook_executor(build, disclosures, mutate_snapshot, checkpoint):
    from tests.flows.sim.simulation_record_test_support import _executor

    return _executor(
        build,
        {"compile": 0, "durable_reuse": 0, "launch": 0},
        disclosures=disclosures,
        mutate_snapshot=mutate_snapshot,
        checkpoint=checkpoint,
    )


def _successful_pre_simulation_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
    *,
    commands: tuple[str, ...] = ("echo hook-out", "echo hook-err >&2"),
    mutate_snapshot: bool = False,
    live_commands: tuple[str, ...] | None = None,
    transform=None,
    invocation_directory: Path | None = None,
    checkpoint=None,
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
    simulation_record = SimulationRecord(
        _hook_executor(build, disclosures, mutate_snapshot, checkpoint)
    ).run(_observed_simulation_record_request(plan, project, invocation, observer))
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


def _public_hook_endpoint(flow, invocation):
    import time

    from booley.flows.endpoint_report_criteria import ReportCriteria

    endpoint = SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        refresh_report_detail=flow.refresh_report_detail,
        name="sim",
        endpoint_kind="flow",
        _acceptance_recorder=flow._acceptance_recorder,
        _report_criteria=ReportCriteria(frozen=True),
        _reserved_invocation_dir=invocation,
        _start_time=time.monotonic(),
        _selected_target="sim",
        _eda_tool="icarus",
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
    return endpoint


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_successful_pre_sim_firing_reaches_public_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    access: str,
) -> None:
    import json
    import re

    from booley.flows.endpoint_reporting import _publish_console_report, write_report

    invocation, simulation_record = _successful_pre_simulation_record(
        tmp_path, monkeypatch, access
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    endpoint = _public_hook_endpoint(flow, invocation)
    write_report(endpoint, result)
    _publish_console_report(endpoint, result)
    lines = re.findall(
        r"pre_run_commands \(2 line\(s\)\) for sim/alpha: rc=0 in \d+\.\ds",
        result.report_text,
    )
    assert len(lines) == 1
    assert capsys.readouterr().out.count(lines[0]) == 1
    report = json.loads((invocation.parent / "sim.json").read_bytes())
    assert report["detail"]["pre_sim_lines"] == lines
    assert (invocation / "report.json").read_bytes() == (
        invocation.parent / "sim.json"
    ).read_bytes()
    sidecars = list(invocation.glob("targets/sim/record/work-items/*/attempts/*/pre-sim/*.json"))
    assert len(sidecars) == 1
    evidence = json.loads(sidecars[0].read_bytes())
    assert evidence["returncode"] == 0
    assert evidence["stdout_tail"] == "hook-out\n"
    assert evidence["stderr_tail"] == "hook-err\n"


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_failed_hook_reports_real_exit_code_without_changing_adapter_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    invocation, simulation_record = _successful_pre_simulation_record(
        tmp_path, monkeypatch, access, commands=("echo failed-out; echo failed-err >&2; exit 7",)
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    assert result.exit_code == 1
    assert len(result.detail["pre_sim_lines"]) == 1
    assert "rc=7" in result.detail["pre_sim_lines"][0]
    assert simulation_record.pre_sim_firings[0].document["stderr_tail"] == "failed-err\n"


def test_downstream_integrity_failure_retains_authenticated_current_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.sim.record import SimulationRecordIntegrityError

    flow = SimulateFlow()
    flow._current_published_pre_sim_keys = set()
    with pytest.raises(SimulationRecordIntegrityError):
        _successful_pre_simulation_record(
            tmp_path,
            monkeypatch,
            "immutable",
            mutate_snapshot=True,
            observer=flow._record_pre_sim_firing,
        )
    flow._reserved_invocation_dir = tmp_path / "reports/1"
    result = flow._attach_published_pre_sim(
        EndpointOutcome(exit_code=2, report_text="integrity error")
    )
    assert len(result.detail["pre_sim_lines"]) == 1
    assert "rc=0" in result.detail["pre_sim_lines"][0]
    assert result.detail["pre_sim_runs"][0]["terminal"] is False


def test_endpoint_reauthentication_never_renders_corrupt_hook_as_authoritative(
    tmp_path, monkeypatch
):
    invocation, simulation_record = _successful_pre_simulation_record(
        tmp_path, monkeypatch, "immutable"
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    assert result.detail["pre_sim_current"] == 1
    firing = simulation_record.pre_sim_firings[0]
    firing.path.write_bytes(firing.path.read_bytes() + b" ")
    result = flow._attach_published_pre_sim(result)
    assert result.exit_code == 2 and result.criterion_met is False
    assert "pre_run_commands" not in result.report_text
    assert "pre_sim_lines" not in result.detail
    assert "pre_sim_runs" not in result.detail
    assert result.detail["pre_sim_unauthenticated_references"]


def _recording_invocation(*, borrows: bool, events: list[str]):
    """Build an invocation recording the admission ``invoke_endpoint`` hands it.

    Its run also appends ``"invoke"`` to *events*, so the lifecycle order stays pinned.
    """
    from booley.flows.endpoint_report_criteria import ReportScope
    from booley.flows.prepared_invocation import PreparedInvocation

    class RecordingInvocation(PreparedInvocation):
        borrows_admission = borrows

        def __init__(self) -> None:
            self.received: list[object | None] = []

        def report_scope(self, endpoint: object) -> ReportScope:
            raise AssertionError("the execution sequence under test never plans a report scope")

        def run(self, admission: object | None) -> EndpointOutcome:
            events.append("invoke")
            self.received.append(admission)
            return EndpointOutcome(report_text="ok")

    return RecordingInvocation()


class _Endpoint:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.name = "sim"
        self.admission_object = object()

    @contextmanager
    def admission(
        self, prepared: PreparedExecution, *, requeued: bool = False
    ) -> Iterator[object]:
        self.events.append("admit")
        try:
            yield self.admission_object
        finally:
            self.events.append("release")

    def may_requeue_after_oom(self, prepared: PreparedExecution, admission: object | None) -> bool:
        return False

    def begin_requeued_attempt(self) -> None:
        pass

    def invoke_endpoint(
        self,
        prepared: PreparedExecution,
        *,
        admission: object | None,
        started: float,
    ) -> EndpointOutcome:
        return endpoint_session.invoke_endpoint(
            self,
            prepared,
            admission=admission,
            started=started,  # type: ignore[arg-type]
        )

    def _finalize_result(self, outcome: EndpointOutcome) -> None:
        self.events.append("finalize")

    def record_acceptance(self, prepared: PreparedExecution, outcome: EndpointOutcome) -> None:
        self.events.append("acceptance")

    def finish_execution(
        self,
        prepared: PreparedExecution,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        acceptance_recorded: bool,
    ) -> ExecutionResult:
        self.events.append("finish")
        return ExecutionResult(outcome.exit_code, outcome)


@pytest.mark.parametrize("borrows", [True, False])
def test_prepared_simulation_record_and_borrowed_admission_are_explicit_values(
    borrows: bool,
) -> None:
    endpoint = _Endpoint()
    invocation = _recording_invocation(borrows=borrows, events=endpoint.events)
    prepared = PreparedExecution(None, None, False, False, invocation)

    result = execute_endpoint(endpoint, prepared)

    assert result.outcome.report_text == "ok"
    # The borrower receives the admission object; any other invocation receives None.
    assert invocation.received == [endpoint.admission_object if borrows else None]
    assert endpoint.events == [
        "admit",
        "invoke",
        "finalize",
        "acceptance",
        "finish",
        "release",
    ]
    assert not hasattr(endpoint, "_simulation_prepared")
    assert not hasattr(endpoint, "_simulation_admission_context")


def _request(selector: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan=SimpleNamespace(
            manifest=SimpleNamespace(
                document={
                    "target": {
                        "selector": selector,
                        "vlnv": "acme:demo:target:1.0",
                        "name": selector,
                    }
                }
            )
        )
    )


def _retained_simulation_record_outcome(outcomes: list[object]) -> EndpointOutcome:
    targets = {f"target-{index}": {"simulation": "pass"} for index, _ in enumerate(outcomes)}
    return EndpointOutcome(detail={"targets": targets}, report_text="campaign verdict")


def test_completed_simulation_record_survives_per_target_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    outcomes = [object(), object()]
    simulation_record = SimpleNamespace(run=lambda _request: outcomes.pop(0))
    progress = SimpleNamespace(
        invocation_dir=tmp_path,
        checkpoint=lambda **_kwargs: (_ for _ in ()).throw(OSError("progress unavailable")),
    )
    monkeypatch.setattr(
        flow, "_simulation_record_endpoint_outcome", _retained_simulation_record_outcome
    )
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: None,
    )

    result = flow._execute_coverage_simulation_record_requests(
        simulation_record, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {"target-0": {"simulation": "pass"}}
    assert result.detail["pending_targets"] == ["second"]
    assert result.detail["completion_error"]["path"] == str(tmp_path / "progress.json")
    assert len(outcomes) == 1


def test_coverage_authentication_failure_returns_structured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._coverage_prepared = SimpleNamespace(targets=[])
    completed = [object(), object()]
    pending = list(completed)
    simulation_record = SimpleNamespace(run=lambda _request: pending.pop(0))
    progress = SimpleNamespace(invocation_dir=tmp_path, checkpoint=lambda **_kwargs: None)
    monkeypatch.setattr(
        flow, "_simulation_record_endpoint_outcome", _retained_simulation_record_outcome
    )
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda _progress, _outcome: (_ for _ in ()).throw(ValueError("untrusted campaign")),
    )

    result = flow._execute_coverage_simulation_record_requests(
        simulation_record, [_request("first"), _request("second")], progress
    )

    assert result.exit_code == 2
    assert result.detail["records"] == {}
    assert result.detail["targets"] == {}
    assert "untrusted campaign" in result.report_text
    assert len(pending) == 1


def test_all_simulation_records_survive_terminal_progress_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path, dry_run=False)
    flow._coverage_prepared = SimpleNamespace(
        targets=[
            SimpleNamespace(handle=SimpleNamespace(selector="first")),
            SimpleNamespace(handle=SimpleNamespace(selector="second")),
        ]
    )
    returned = [object(), object()]
    pending = list(returned)
    simulation_record = SimpleNamespace(run=lambda _request: pending.pop(0))
    requests = [_request("first"), _request("second")]

    progress = SimpleNamespace(invocation_dir=tmp_path, targets=("first", "second"), outcomes=[])
    terminal_phases: list[str] = []

    def checkpoint(*, complete: bool = False, phase: str | None = None) -> None:
        if complete:
            assert phase is not None
            terminal_phases.append(phase)
            raise OSError("terminal progress unavailable")

    progress.checkpoint = checkpoint
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(
        flow,
        "_coverage_simulation_record_session",
        lambda _admission, _invocation, _published: (simulation_record, requests),
    )
    monkeypatch.setattr("booley.flows.sim.flow.CoverageProgress", lambda *_args: progress)
    monkeypatch.setattr(
        flow, "_simulation_record_endpoint_outcome", _retained_simulation_record_outcome
    )
    monkeypatch.setattr(
        "booley.flows.sim.flow._retain_coverage_campaign",
        lambda retained, outcome: retained.outcomes.append(outcome),
    )

    result = flow._execute_simulation_record(
        AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False),
        flow._coverage_simulation_record_run(),
    )

    assert result.exit_code == 2
    assert result.detail["targets"] == {
        "target-0": {"simulation": "pass"},
        "target-1": {"simulation": "pass"},
    }
    assert result.detail["completion_error"]["operation"] == "publish coverage progress"
    assert terminal_phases == ["complete", "aborted"]
    assert pending == []


@pytest.mark.parametrize("termination", ["disk_budget", "sim_time_stall"])
def test_infrastructure_simulation_record_reaches_persisted_endpoint_reports(
    tmp_path: Path, termination: str
) -> None:
    import json
    import time

    from booley.flows.endpoint_report_criteria import ReportCriteria
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
    flow._args = SimpleNamespace(
        target=["sim"], result_verbosity="brief", report_dir=invocation.parent
    )
    result = flow._simulation_record_endpoint_outcome([simulation_record])
    endpoint = SimpleNamespace(
        args=SimpleNamespace(report_dir=invocation.parent, slug=""),
        refresh_report_detail=flow.refresh_report_detail,
        name="sim",
        endpoint_kind="flow",
        _acceptance_recorder=flow._acceptance_recorder,
        _report_criteria=ReportCriteria(frozen=True),
        _reserved_invocation_dir=invocation,
        _start_time=time.monotonic(),
        _selected_target="sim",
        _eda_tool="verilator",
        _raw_argv=None,
        report_schema="booley.simulation-report/v3",
        _skip_report_detail_refresh=False,
        _eda_tools={},
        _pre_run_head=None,
        _automatic_target_notice="",
        _automatic_target_notice_published=False,
    )
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


@pytest.mark.parametrize("target_count", [1, 2])
def test_cli_discloses_committed_manifest_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_count: int,
) -> None:
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",) * target_count)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text(
        "".join(f'[sim_{index}]\ntests = ["half"]\n' for index in range(target_count))
    )
    observed: list[Path] = []

    def execution(*_args):
        captured = capsys.readouterr()
        lines = [
            line for line in captured.err.splitlines() if line.startswith("campaign manifest: ")
        ]
        assert len(lines) == (target_count if not observed else 0)
        for line in lines:
            manifest = Path(line.removeprefix("campaign manifest: "))
            assert manifest.is_absolute() and manifest.is_file()
            observed.append(manifest)
        return NativeExecution()

    flow = SimulateFlow(coverage_execution=execution)
    result = flow.execute_cli(
        _simulation_record_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports with spaces"),
            target=",".join(f"sim_{index}" for index in range(target_count)),
            coverage=True,
        )
    )
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
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    seen: list[str] = []

    def execution(*_args):
        seen.append(capsys.readouterr().err)
        assert "campaign manifest: " in seen[0]
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "cancel":
            raise SimulationRecordCancellationError("cancelled")
        raise RuntimeError("executor failed")

    flow = SimulateFlow(coverage_execution=execution)

    def checkpoint(boundary: str) -> None:
        if boundary == failure:
            raise KeyboardInterrupt()

    monkeypatch.setattr(flow, "_simulation_record_publication_checkpoint", checkpoint)
    argv = _simulation_record_cli_args(
        work_dir=str(tmp_path), report_dir=str(tmp_path / "reports"), target="sim_0", coverage=True
    )
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
    assert announcements.count("campaign manifest: ") == (
        0 if failure == "before:manifest_commit" else 1
    )
    if failure != "before:manifest_commit":
        path = Path(
            next(
                line.removeprefix("campaign manifest: ")
                for line in announcements.splitlines()
                if line.startswith("campaign manifest: ")
            )
        )
        assert path.is_file()


def test_typed_simulation_record_execution_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(
            work_dir=tmp_path, report_dir=tmp_path / "reports", target="sim_0", coverage=True
        )
    )
    assert result.exit_code == 0
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert "  manifest: " in result.outcome.report_text


def test_same_flow_second_failure_does_not_reuse_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    argv = _simulation_record_cli_args(
        work_dir=str(tmp_path), report_dir=str(tmp_path / "reports"), target="sim_0", coverage=True
    )
    first = flow.execute_cli(argv)
    manifest_line = next(
        line for line in first.outcome.report_text.splitlines() if line.startswith("  manifest:")
    )
    capsys.readouterr()

    def fail(boundary: str) -> None:
        if boundary == "before:manifest_commit":
            raise OSError("publication failed")

    monkeypatch.setattr(flow, "_simulation_record_publication_checkpoint", fail)
    second = flow.execute_cli(argv)
    assert second.exit_code == 2
    assert "manifest:" not in second.outcome.report_text
    assert manifest_line not in second.outcome.report_text
    assert "campaign manifest: " not in capsys.readouterr().err


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
    monkeypatch.setattr(flow, "_ordinary_simulation_record_plan", lambda *_args, **_kwargs: plan)
    seen: list[Path] = []

    execution = _CardExecution(eda_tool="verilator", cocotb=True)
    execution.passing = grade == "pass"

    def factory(_options):
        output = capsys.readouterr()
        announcements = [
            line.removeprefix("campaign manifest: ")
            for line in output.err.splitlines()
            if line.startswith("campaign manifest: ")
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
        assert "campaign manifest: " not in capsys.readouterr().err
        return
    result = flow.execute_cli(argv)
    assert seen
    assert result.exit_code == (0 if grade == "pass" else 1)
    assert f"  manifest: {seen[0]}" in result.outcome.report_text
    report = (tmp_path / "reports/sim/1/report.json").resolve()
    assert f"  report: {report}" in result.outcome.report_text
    assert report.is_file()
    assert result.outcome.report_text.count("  manifest:") == 1

    _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result, factory)


def _assert_ordinary_cli_resume(flow, plan, seen, tmp_path, monkeypatch, capsys, result, factory):
    capsys.readouterr()
    destination = tmp_path / "resumed reports"
    monkeypatch.setattr("booley.flows.sim.record.resume.git_full_sha", lambda *_args: "abc123")
    monkeypatch.setattr(flow, "_resume_simulation_record_plan", lambda *_args, **_kwargs: plan)

    def resume_executor(_coverage):
        checkpoints = list(destination.glob("sim/[0-9]*/progress.json"))
        assert len(checkpoints) == 1
        preparing = json.loads(checkpoints[0].read_text())
        assert preparing["complete"] is False
        assert preparing["completed_targets"] == []
        assert preparing["active"][0]["stage"] == "preparing"
        assert preparing["active"][0]["operation"] == "publication"
        assert capsys.readouterr().err == _WORK_DIR_NOTICE + f"campaign manifest: {seen[0]}\n"
        return OrdinaryHdlSerialExecutor(
            invoke=lambda *_args, **_kwargs: None, execution_factory=factory
        )

    monkeypatch.setattr(flow, "_resume_simulation_record_executor", resume_executor)
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

    from tests.flows.sim.test_coverage_invocation import project
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

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator",))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n')
    stream = BufferedConsole()
    monkeypatch.setattr("booley.flows.sim.flow.sys.stderr", stream)

    def execution(*_args):
        assert stream.pending == ""
        manifest = Path(
            stream.getvalue()
            .removeprefix(_WORK_DIR_NOTICE)
            .strip()
            .removeprefix("campaign manifest: ")
        )
        assert manifest.is_absolute() and manifest.is_file()
        return NativeExecution()

    result = SimulateFlow(coverage_execution=execution).execute_cli(
        [
            "--quiet",
            *_simulation_record_cli_args(
                work_dir=str(tmp_path),
                report_dir=str(tmp_path / "reports"),
                target="sim_0",
                coverage=True,
            ),
        ]
    )
    assert result.exit_code == 0


def test_partial_publication_discloses_only_committed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.flows.sim.test_coverage_invocation import project

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path, ("verilator", "verilator"))
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["half"]\n[sim_1]\ntests = ["half"]\n')
    commits = 0

    def checkpoint(boundary):
        nonlocal commits
        if boundary == "before:manifest_commit":
            commits += 1
            if commits == 2:
                raise OSError("second publication failed")

    def executor(*_args):
        raise AssertionError("work must wait for every manifest")

    result = SimulateFlow(
        coverage_execution=executor, simulation_record_publication_checkpoint=checkpoint
    ).execute_cli(
        _simulation_record_cli_args(
            work_dir=str(tmp_path),
            report_dir=str(tmp_path / "reports"),
            target="sim_0,sim_1",
            coverage=True,
        )
    )
    output = capsys.readouterr()
    assert result.exit_code == 2
    assert output.err.count("campaign manifest: ") == 1
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
        result = flow._with_simulation_record_paths(EndpointOutcome(), [path, path])
    report = (tmp_path / "reports/sim/1/report.json").resolve()
    assert result.report_text == f"  manifest: {path.resolve()}\n  report: {report}"


def test_simulation_record_recording_failure_retains_evaluated_final_report(tmp_path):
    from booley.flows.sim.acceptance import (
        AcceptanceContext,
        SimulationAcceptanceCoordinator,
        _OutcomeEvidence,
    )
    from tests.flows.report_headline import final_report_omits_headline
    from tests.flows.sim.simulation_record_test_support import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import unbound_specialist

    tool = unbound_specialist(tmp_path, "--work-dir", str(tmp_path))
    tool.state._file_path = tmp_path / "state.json"
    tool.state.init_criteria({"sim_pass_sim": True}, strict=True)

    class FailedRecorder:
        def acceptance_identity(self):
            return {}

        def record_or_verify_transaction(self, *_args, **_kwargs):
            raise OSError("campaign ledger unavailable")

    outcome = _simulation_outcome(tmp_path, [_observation("smoke")], required=("smoke",))
    context = AcceptanceContext(
        "ticket",
        {},
        "generation",
        tool.state,
        FailedRecorder(),
        "invocation",
        observer=tool.record_report_criteria,
    )
    with pytest.raises(OSError):
        SimulationAcceptanceCoordinator().reconcile(_OutcomeEvidence(outcome), context)
    report = final_report_omits_headline(tool, EndpointOutcome(exit_code=2))
    assert report["passed"] is False
    assert tool.state.criteria["sim_pass_sim"].met is False


@pytest.mark.parametrize("conclusive", [True, False])
def test_standalone_simulation_record_has_only_current_conclusive_workload_verdict(
    tmp_path, conclusive
):
    from dataclasses import replace

    from booley.flows.sim.acceptance import _OutcomeEvidence, _record_standalone_simulation_record
    from tests.flows.report_headline import final_report_omits_headline
    from tests.flows.sim.simulation_record_test_support import _observation, _simulation_outcome
    from tests.mcp_tools.test_base import unbound_specialist

    tool = unbound_specialist(tmp_path, "--work-dir", str(tmp_path))
    outcome = _simulation_outcome(tmp_path, [_observation("smoke")], required=("smoke",))
    if not conclusive:
        outcome = replace(outcome, complete=False)
    _record_standalone_simulation_record(tool, _OutcomeEvidence(outcome))
    final_report_omits_headline(tool, EndpointOutcome())
    if conclusive:
        assert tool.state.criteria["sim_pass_sim"].met is True
        assert tool.state.criteria["sim_pass_sim"].detail["selected_tests"] == ["smoke"]
    else:
        assert "sim_pass_sim" not in tool.state.criteria
    assert tool.state._file_path is None
    assert not (tmp_path / "state.json").exists()


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


def _cycle_ticket(tmp_path):
    from booley.criteria.state import DevelopmentState
    from tests.criteria.ledger_support import FixtureRecorder

    path = tmp_path / "state.json"
    state = DevelopmentState.load(path)
    state.slug, state.ticket_type = "ticket", "implementation"
    key = "sim_pass_sim_smoke"
    params = {
        "target": "acme:lib:dut:1#sim",
        "_target_selector": "sim",
        "test_selector": "smoke",
        "required_tests": ["smoke"],
        "minimum_total": 1,
    }
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)
    state.save()
    recorder = FixtureRecorder(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    return path, state, key, recorder


def _public_cycle_flow(simulation_record, monkeypatch):
    from contextlib import nullcontext

    from booley.flows.endpoint_state import EndpointState

    flow = SimulateFlow()
    monkeypatch.setattr(flow, "_pre_state_gate", lambda: None)
    monkeypatch.setattr(EndpointState, "_criterion_binding_gate", lambda _self: None)
    from booley.flows.sim.flow import PreparedSimulationEndpoint
    from booley.flows.sim.record_invocation import SimulationRecordInvocation
    from tests.flows.invocation_doubles import prepare_as

    prepared = PreparedSimulationEndpoint((), None, (), {})
    prepare_as(monkeypatch, flow, SimulationRecordInvocation(flow, prepared))
    monkeypatch.setattr(flow, "_resolve_display_label", lambda: None)
    monkeypatch.setattr(EndpointState, "admission", lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr(
        flow,
        "run_prepared_simulation",
        lambda *_args: flow._simulation_record_endpoint_outcome([simulation_record]),
    )
    return flow


def _cycle_publication_fault(flow, monkeypatch, failure):
    import json

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
        monkeypatch.setattr(type(flow), "_publish_console_report", fail)
    if failure == "acceptance":
        monkeypatch.setattr(flow, "record_simulation_record_acceptance", fail)
    if failure in {"render", "structured"}:
        # "render" is looked up by the presentation helpers; "structured" by the flow.
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
def test_public_cycle_metadata_preserves_durable_accepted_criterion(
    tmp_path, monkeypatch, failure
):
    import json

    from booley.criteria.state import DevelopmentState

    simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    path, _state, key, recorder = _cycle_ticket(tmp_path)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(path))
    flow = _public_cycle_flow(simulation_record, monkeypatch)
    writes = _cycle_publication_fault(flow, monkeypatch, failure)
    reports = tmp_path / "reports"
    result = flow.execute_cli(
        ["--target", "sim", "--work-dir", str(tmp_path), "--report-dir", str(reports)],
        adapter=recorder,
    )
    report = json.loads((reports / "sim.json").read_text())
    assert result.exit_code == (0 if failure in {None, "metadata"} else 2)
    saved = DevelopmentState.load(path)
    assert (saved.criteria[key].met is True) == (failure != "acceptance")
    assert len(saved.acceptance_transactions) == int(failure != "acceptance")
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
    if failure not in {"render", "structured"}:
        assert report["detail"]["records"]["sim"]["observations"][0]["cycle_count"] == 1234
        assert "smoke: cycles=1234" in result.outcome.report_text


@pytest.mark.parametrize("cancelled", [False, True])
def test_ordinary_partial_failure_keeps_report_counts_separate(tmp_path, monkeypatch, cancelled):
    from booley.flows.sim.record.coordinator import SimulationRecordCancellationError

    flow = SimulateFlow()
    simulation_record = _public_cycle_simulation_record(tmp_path, monkeypatch)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(flow, "_write_simulation_record_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flow, "_plan_simulation_record_baselines", lambda *_args: ([], {}))
    monkeypatch.setattr(flow, "_candidate_simulation_record_requests", lambda *_args: [])

    def run(*_args, outcomes, **_kwargs):
        outcomes.append(simulation_record)
        error = SimulationRecordCancellationError if cancelled else RuntimeError
        raise error("second target stopped")

    monkeypatch.setattr(flow, "_publish_and_run_simulation_record_requests", run)
    result = flow._execute_simulation_record(
        AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False),
        flow._ordinary_simulation_record_run(OrdinarySimulationRecordPlanning((), None, {})),
    )
    assert result.exit_code != 0
    assert flow._simulation_record_outcomes == ()
    if cancelled:
        assert result.detail == {}
    _assert_partial_acceptance_policy(flow, result, tmp_path, simulation_record)


def test_coverage_cancellation_keeps_completed_report_only_counts(tmp_path, monkeypatch):
    from booley.flows.sim.record.coordinator import SimulationRecordCancellationError

    flow = SimulateFlow()
    outcome = _native_cycle_simulation_record(tmp_path, monkeypatch)
    responses = iter((outcome,))

    def run(_request):
        try:
            return next(responses)
        except StopIteration:
            raise SimulationRecordCancellationError("cancelled") from None

    monkeypatch.setattr("booley.flows.sim.flow._retain_coverage_campaign", lambda *_args: None)
    progress = SimpleNamespace(checkpoint=lambda: None)
    result = flow._execute_coverage_simulation_record_requests(
        SimpleNamespace(run=run), [_request("first"), _request("second")], progress
    )
    assert result.detail == {}
    # The finished first Target keeps its public Coverage Campaign on the stopped card.
    campaign_file = (outcome.manifest_path.parents[1] / "coverage.json").resolve()
    assert campaign_file.is_file()
    assert result.report_text.splitlines()[-1] == f"  coverage campaign: {campaign_file}"
    assert flow._simulation_record_outcomes == ()
    assert all(item["cycle_count"] is None for item in outcome.observations)
    _assert_partial_acceptance_policy(flow, result, tmp_path, outcome)


def _native_cycle_simulation_record(tmp_path, monkeypatch):
    from booley.flows.sim.record.coordinator import SimulationRecord
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    returned = []
    original = SimulationRecord.run

    def capture(self, request):
        outcome = original(self, request)
        returned.append(outcome)
        return outcome

    monkeypatch.setattr(SimulationRecord, "run", capture)
    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0
    return returned[0]


def _assert_partial_acceptance_policy(flow, result, tmp_path, simulation_record):
    import json

    from booley.flows.endpoint_acceptance import record_acceptance
    from booley.flows.endpoint_reporting import write_report
    from booley.flows.sim.request import SimRequest

    path, state, _key, recorder = _cycle_ticket(tmp_path)
    flow._args = SimRequest(target="sim", work_dir=tmp_path, report_dir=tmp_path / "reports")
    context = flow
    context._state = state
    context.configure_flow_execution(recorder)
    context._pending_criteria_set = ("preexisting",)
    before = path.read_bytes()
    projections = {p: p.read_bytes() for p in tmp_path.rglob("simulation.json")}
    from booley.flows.prepared_invocation import OrdinaryRunInvocation

    record_acceptance(
        context,
        PreparedExecution(None, None, False, False, OrdinaryRunInvocation(EndpointOutcome)),
        result,
    )
    assert path.read_bytes() == before
    assert context.state.criteria
    assert context._pending_criteria_set == ()
    assert not list((tmp_path / "logs").rglob("*.json"))
    assert {p: p.read_bytes() for p in tmp_path.rglob("simulation.json")} == projections
    with context.publication_resources:
        context._finalize_result(result)
        report_path = write_report(context, result)
    report = json.loads(report_path.read_text())
    assert report["cycle_counts"] == {
        str(simulation_record.target["selector"]): [
            {"test": row["test"], "cycle_count": row["cycle_count"]}
            for row in simulation_record.observations
        ]
    }


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

    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    handle = fixtures._handle(project)
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
            fixtures._admission(),
        )
    )


def _assert_nullable_cycle_report(flow, result, tmp_path, unnamed):
    import json

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


@pytest.mark.parametrize("failure", ["run", "retention"])
def test_coverage_failure_persists_only_returned_outcomes(tmp_path, monkeypatch, failure):
    from booley.flows.sim.record.coordinator import SimulationRecord
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    core = tmp_path / "counter.core"
    target = core.read_text().split("targets:\n", 1)[1]
    core.write_text(
        core.read_text() + target.replace("sim_0:", "sim_1:") + target.replace("sim_0:", "sim_2:")
    )
    (tmp_path / ".booley/tests.toml").write_text(
        '[sim_0]\ntests=["reset"]\n[sim_1]\ntests=["reset"]\n[sim_2]\ntests=["reset"]\n'
    )
    original_run = SimulationRecord.run
    original_retain = __import__("booley.flows.sim.flow", fromlist=["_retain_coverage_campaign"])
    retain = original_retain._retain_coverage_campaign
    returned = []

    def run(simulation_record, request):
        if failure == "run" and len(returned) == 1:
            raise RuntimeError("second run failed before outcome")
        outcome = original_run(simulation_record, request)
        returned.append(outcome)
        return outcome

    def retain_or_fail(progress, outcome):
        if failure == "retention" and len(returned) == 2:
            raise OSError("completed second retention failed")
        return retain(progress, outcome)

    monkeypatch.setattr(SimulationRecord, "run", run)
    monkeypatch.setattr(original_retain, "_retain_coverage_campaign", retain_or_fail)
    flow = SimulateFlow(coverage_execution=lambda *_args: NativeExecution())
    result = flow.execute(
        SimRequest(
            target="sim_0,sim_1,sim_2", work_dir=tmp_path, coverage=True, report_dir=reports
        )
    )
    assert result.exit_code == 2
    assert len(returned) == (1 if failure == "run" else 2)
    _assert_coverage_failure_counts(flow, reports, returned)


def _assert_coverage_failure_counts(flow, reports, returned):
    import json

    assert flow._simulation_record_outcomes == tuple(returned[:1])
    report = json.loads((reports / "sim/1/report.json").read_text())
    expected = {
        f"sim_{index}": [{"test": "reset", "cycle_count": None}] for index in range(len(returned))
    }
    assert report["cycle_counts"] == expected
    assert "sim_2" not in report["cycle_counts"]
    assert report["detail"]["pending_targets"] == ["sim_1", "sim_2"]


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_simulation_record_executor_uses_frozen_hook_commands_despite_live_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    _invocation, outcome = _successful_pre_simulation_record(
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
    import json
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
        "booley.flows.sim.build_session._icarus_tool_identity", lambda: "test-tool-closure"
    )
    flow = SimulateFlow()
    # Publication tests must allow for Git for Windows shell startup under CI load.
    execution = SimulationExecution(
        invoke=_subprocess_invoker(project),
        options=SimulationOptions(timeout_ms=60_000 if os.name == "nt" else 5000),
    )
    monkeypatch.setattr(flow, "_simulation_execution", lambda: execution)
    return project, flow


def test_public_ordinary_prepared_endpoint_reports_each_shared_build_launch(tmp_path, monkeypatch):
    import json

    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports")
    )
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    lines = report["detail"]["pre_sim_lines"]
    assert len(lines) == report["detail"]["pre_sim_current"] == 2
    assert any("for sim_a/a: rc=0" in line for line in lines)
    assert any("for sim_a/b: rc=0" in line for line in lines)
    assert all(report["report_text"].count(line) == 1 for line in lines)
    assert (tmp_path / "reports/sim/1/report.json").read_bytes() == (
        tmp_path / "reports/sim.json"
    ).read_bytes()
    manifest = tmp_path / "reports/sim/1/targets/sim_a/record/manifest.json"

    firings = collect_pre_sim_firings(manifest)
    assert len(firings) == 2
    simulation_record_root = manifest.parent
    assert (
        len(list(simulation_record_root.glob("build-variants/*/attempts/*/build-result.json")))
        == 1
    )
    assert all(firing.document["stdout_tail"] == "public-hook\n" for firing in firings)


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

    original = _Group.launch_snapshot

    def two_results(group, snapshot, run_cwd):
        outcome = original(group, snapshot, run_cwd)
        import json

        transport = snapshot / "cocotb-results.json"
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

    monkeypatch.setattr(_Group, "launch_snapshot", two_results)
    invocation, outcome = _successful_pre_simulation_record(
        tmp_path, monkeypatch, "immutable", transform=_cocotb_hook_plan
    )
    assert outcome.aggregate_grade == "pass"
    firings = collect_pre_sim_firings(invocation / "targets/sim/record/manifest.json")
    assert len(firings) == 1
    assert firings[0].document["test_names"] == ("alpha", "beta")
    assert firings[0].document["stdout_tail"] == "hook-out\n"


def _public_ticket_hook_adapter(tmp_path, project):
    from booley.criteria.state import DevelopmentState
    from booley.evidence.acceptance import ResolvedFlowAcceptance
    from tests.criteria.ledger_support import FixtureRecorder

    log_dir = tmp_path / "logs/ticket"
    state_path = log_dir / ".runtime/booley_state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "ticket"
    state.init_criteria({"sim_pass_sim_a": True}, strict=True)
    state.work_dir = str(project)
    state.save()

    class PreparedTicketRecorder(FixtureRecorder):
        def validate_and_resolve(self, request):
            # Ticket admission is a fixture; recording uses the actual ledger owner.
            request.state_file = state_path
            request.slug = "ticket"
            return ResolvedFlowAcceptance(ticket_backed=True)

    recorder = PreparedTicketRecorder(
        log_dir=log_dir,
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    return recorder, state_path, log_dir


@pytest.mark.parametrize("corrupt", [False, True])
def test_public_ticket_integrity_downgrade_never_commits_or_publishes_true(
    tmp_path, monkeypatch, corrupt
):
    import json

    from booley.criteria.state import DevelopmentState
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    adapter, state_path, log_dir = _public_ticket_hook_adapter(tmp_path, project)
    project_outcome = flow._simulation_record_endpoint_outcome

    def corrupt_after_projection(outcomes):
        result = project_outcome(outcomes)
        if corrupt:
            path = outcomes[0].pre_sim_firings[0].path.parents[3] / "result.json"
            path.write_text("{}")
        return result

    monkeypatch.setattr(flow, "_simulation_record_endpoint_outcome", corrupt_after_projection)
    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports"),
        adapter=adapter,
    )
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    state = DevelopmentState.load(state_path)
    assert result.exit_code == (2 if corrupt else 0), result.outcome.report_text
    assert report["criterion_met"] is (None if corrupt else True)
    assert state.criteria["sim_pass_sim_a"].met is (not corrupt)
    assert bool(state.acceptance_transactions) is (not corrupt)
    assert bool(list((log_dir / "acceptance/transactions").glob("*.json"))) is (not corrupt)
    if corrupt:
        assert "Simulation Campaign integrity failure" in report["report_text"]
        assert report["detail"]["pre_sim_current"] == 2
        assert len(report["detail"]["pre_sim_lines"]) == 2


def test_ordinary_resume_retains_returned_outcome_when_terminal_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path, dry_run=False)
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    validated = SimpleNamespace(manifest=plan.manifest, path=tmp_path / "origin" / "manifest.json")
    recovery = SimpleNamespace(
        manifest_sha256="sha256:x", completed=(), interrupted=(), pending=()
    )
    returned = SimpleNamespace(target=plan.manifest.document["target"], recovery=recovery)
    phases = []

    def run_resumed(*args):
        args[-1](validated.path)  # the origin manifest is published before the work runs
        return returned

    def endpoint(outcomes):
        assert outcomes == [returned]
        return EndpointOutcome(report_text="resumed result")

    def checkpoint(_targets, outcomes, *, phase, complete):
        phases.append(phase)
        if complete:
            assert outcomes == [returned]
            raise OSError("required terminal write unavailable")

    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    monkeypatch.setattr(flow, "_prepare_simulation_record_resume_dependency", lambda *_args: None)
    monkeypatch.setattr(flow, "_run_validated_resume_simulation_record", run_resumed)
    monkeypatch.setattr(flow, "_simulation_record_endpoint_outcome", endpoint)
    monkeypatch.setattr(flow, "_write_simulation_record_progress", checkpoint)

    result = flow._execute_simulation_record(
        AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False),
        flow._resume_simulation_record_run(validated, recovery),
    )

    assert phases == ["starting", "running", "complete", "aborted"]
    assert result.report_text.startswith("resumed result")
    assert result.detail["completion_error"]["operation"] == "publish Simulation progress"
    assert f"  manifest: {validated.path.resolve()}" in result.report_text.splitlines()


_UNADMITTED = {
    "ordinary": ("sim: Simulation Campaign has no borrowed admission context", {}),
    "coverage": ("sim: Coverage Campaign has no borrowed admission context", {}),
    "resume": (
        "sim: no admission context",
        {"manifest_sha256": "sha256:x", "completed": ["a"], "interrupted": [], "pending": ["b"]},
    ),
}


@pytest.mark.parametrize("kind", ["ordinary", "coverage", "resume"])
def test_simulation_record_kinds_refuse_without_admission_before_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path, dry_run=False)
    flow._coverage_prepared = SimpleNamespace(targets=[])
    recovery = SimpleNamespace(
        manifest_sha256="sha256:x", completed=("a",), interrupted=(), pending=("b",)
    )
    simulation_records = {
        "ordinary": lambda: flow._ordinary_simulation_record_run(
            OrdinarySimulationRecordPlanning((), None, {})
        ),
        "coverage": flow._coverage_simulation_record_run,
        "resume": lambda: flow._resume_simulation_record_run(SimpleNamespace(), recovery),
    }
    monkeypatch.setattr(
        flow, "reserve_invocation_dir", lambda: pytest.fail("reserved before the admission check")
    )

    result = flow._execute_simulation_record(None, simulation_records[kind]())

    text, detail = _UNADMITTED[kind]
    assert (result.exit_code, result.report_text, result.detail) == (2, text, detail)


def _faulty_simulation_record(flow, tmp_path, monkeypatch, kind, fault):
    """Build *kind*'s Campaign with its steps stubbed.

    The first step publishes one manifest, then raises ``OSError`` for
    ``before_result``; for ``completion`` the work returns a result and only the
    terminal progress write fails.
    """
    manifest = tmp_path / "manifest.json"
    done = EndpointOutcome(report_text="verdict")

    def work(publish):
        publish(manifest)
        if fault == "before_result":
            raise OSError("disk gone")

    def checkpoint(*_args, phase=None, complete=False):
        if complete and phase == "complete" and fault == "completion":
            raise OSError("terminal progress unavailable")

    monkeypatch.setattr(flow, "_simulation_record_endpoint_outcome", lambda _outcomes: done)
    if kind == "ordinary":
        publishers = []
        monkeypatch.setattr(
            "booley.flows.sim.flow.SimulationRecord",
            lambda *_args, manifest_published, **_kwargs: publishers.append(manifest_published),
        )

        def baselines(*_args):
            work(publishers[0])
            return [], {}

        monkeypatch.setattr(flow, "_write_simulation_record_progress", checkpoint)
        monkeypatch.setattr(flow, "_plan_simulation_record_baselines", baselines)
        monkeypatch.setattr(flow, "_candidate_simulation_record_requests", lambda *_args: [])
        monkeypatch.setattr(
            flow, "_publish_and_run_simulation_record_requests", lambda *_a, **_k: None
        )
        return flow._ordinary_simulation_record_run(
            OrdinarySimulationRecordPlanning((), None, {})
        ), manifest
    if kind == "coverage":
        progress = SimpleNamespace(targets=("a",), outcomes=[object()], checkpoint=checkpoint)

        def session(_admission, _invocation, publish):
            work(publish)
            return None, []

        flow._coverage_prepared = SimpleNamespace(
            targets=[SimpleNamespace(handle=SimpleNamespace(selector="a"))]
        )
        monkeypatch.setattr("booley.flows.sim.flow.CoverageProgress", lambda *_args: progress)
        monkeypatch.setattr(flow, "_coverage_simulation_record_session", session)
        monkeypatch.setattr(
            flow, "_execute_coverage_simulation_record_requests", lambda *_args: done
        )
        return flow._coverage_simulation_record_run(), manifest
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    validated = SimpleNamespace(manifest=plan.manifest, path=manifest)
    recovery = SimpleNamespace(
        manifest_sha256="sha256:x", completed=(), interrupted=(), pending=()
    )

    def resumed(*args):
        work(args[-1])
        return SimpleNamespace(recovery=recovery)

    monkeypatch.setattr(flow, "_write_simulation_record_progress", checkpoint)
    monkeypatch.setattr(flow, "_prepare_simulation_record_resume_dependency", lambda *_args: None)
    monkeypatch.setattr(flow, "_run_validated_resume_simulation_record", resumed)
    monkeypatch.setattr(
        "booley.flows.sim.flow_resume._fresh_simulation_record_recovery_detail",
        lambda *_args: {"pending": []},
    )
    return flow._resume_simulation_record_run(validated, recovery), manifest


@pytest.mark.parametrize("kind", ["ordinary", "coverage", "resume"])
@pytest.mark.parametrize("fault", ["before_result", "completion"])
def test_simulation_record_kind_failure_cards_name_published_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, fault: str
) -> None:
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=tmp_path, dry_run=False)
    monkeypatch.setattr(flow, "reserve_invocation_dir", lambda: tmp_path)
    simulation_record, manifest = _faulty_simulation_record(
        flow, tmp_path, monkeypatch, kind, fault
    )

    result = flow._execute_simulation_record(
        AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False),
        simulation_record,
    )

    assert result.exit_code == 2
    if fault == "completion":
        operation = {
            "ordinary": "publish simulation progress",
            "coverage": "publish coverage progress",
            "resume": "publish Simulation progress",
        }[kind]
        assert result.detail["completion_error"]["operation"] == operation
        assert result.report_text.startswith("verdict")
    else:
        failed = {
            "ordinary": "Simulation Campaign execution failed",
            "coverage": "Simulation Campaign coverage planning failed",
            "resume": "Simulation Campaign resume failed",
        }[kind]
        assert result.report_text.startswith(f"{failed}: disk gone")
    # Whatever stopped the run, the card names the manifest --resume-from needs.
    assert result.report_text.splitlines().count(f"  manifest: {manifest.resolve()}") == 1


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
    invocation, simulation_record = _successful_pre_simulation_record(
        tmp_path, monkeypatch, "legacy-per-test"
    )
    flow = SimulateFlow()
    flow._reserved_invocation_dir = invocation
    flow._args = SimpleNamespace(target=["sim"], result_verbosity="brief")
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


@pytest.mark.parametrize("mode", ["simulate", "elab_only"])
def test_simulation_record_report_records_tools_by_target(tmp_path, monkeypatch, mode):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    result = flow.execute(
        SimRequest(
            target="sim_a,sim_b", mode=mode, work_dir=project, report_dir=tmp_path / "reports"
        )
    )
    assert result.exit_code == 0, result.outcome.report_text
    report = json.loads((tmp_path / "reports/sim.json").read_bytes())
    assert report.get("eda_tools") == {"sim_a": "icarus", "sim_b": "icarus"}
    assert "eda_tool" not in report


def test_coverage_simulation_record_report_records_tool(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import NativeExecution, _prepare_coverage_origin

    reports = _prepare_coverage_origin(tmp_path, monkeypatch)
    result = SimulateFlow(coverage_execution=lambda *_args: NativeExecution()).execute(
        SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    )
    assert result.exit_code == 0
    assert json.loads((reports / "sim.json").read_bytes()).get("eda_tool") == "verilator"


def test_partial_simulation_record_planning_keeps_known_and_unknown_tools(tmp_path, monkeypatch):
    from booley.flows.sim.request import SimRequest

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    (project / ".booley/tests.toml").write_text('[sim_a]\ntests = ["a"]\n[sim_b]\ntests = ["a"]\n')
    original = flow._ordinary_simulation_record_plan

    def plan(planned, *args, **kwargs):
        if planned.selector == "sim_b":
            raise ValueError("second Target unavailable")
        return original(planned, *args, **kwargs)

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

    invocation, outcome = _successful_pre_simulation_record(tmp_path, monkeypatch, "immutable")
    outcome.pre_sim_firings[0].path.unlink()
    store = SimulationRecordStore(invocation / "targets/sim/record")

    with pytest.raises(SimulationRecordIntegrityError, match="result references disagree"):
        collect_pre_sim_firings(store.manifest_path)


def _strict_sim_a_ticket(tmp_path, monkeypatch):
    """A strict Ticket with one bound Simulation Criterion for the ``sim_a`` Target."""
    from booley.criteria.state import DevelopmentState

    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug, state.ticket_type = "ticket", "implementation"
    key = "sim_pass_sim_a"
    params = {
        "target": "acme:lib:multi:1#sim_a",
        "_target_selector": "sim_a",
        "test_selector": "a",
        "required_tests": ["a", "b"],
        "minimum_total": 2,
    }
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path


def _spy_simulation_lifecycle(monkeypatch, events, terminal_phases, *, terminal_write_fails):
    """Append lifecycle step names to *events*, optionally failing the terminal progress write."""
    from booley.flows.endpoint_state import EndpointState

    write_progress = SimulateFlow._write_simulation_record_progress
    hook = SimulateFlow.record_flow_owned_acceptance
    post_run = EndpointState._post_run
    refresh = SimulateFlow.refresh_report_detail

    def progress(self, targets, outcomes, *, phase, complete):
        if complete or phase == "aborted":
            terminal_phases.append(phase)
            events.append("terminal")
            if terminal_write_fails:
                raise OSError("injected terminal progress failure")
        return write_progress(self, targets, outcomes, phase=phase, complete=complete)

    def record(self, outcome):
        handled = hook(self, outcome)
        events.append(f"acceptance:{handled}")
        return handled

    def persist(self, *args):
        events.append("mutable")
        return post_run(self, *args)

    def refresh_detail(self, result):
        events.append("refresh")
        return refresh(self, result)

    monkeypatch.setattr(SimulateFlow, "_write_simulation_record_progress", progress)
    monkeypatch.setattr(SimulateFlow, "record_flow_owned_acceptance", record)
    monkeypatch.setattr(EndpointState, "_post_run", persist)
    monkeypatch.setattr(SimulateFlow, "refresh_report_detail", refresh_detail)


@pytest.mark.parametrize(
    "terminal_write_fails", [False, True], ids=["complete", "terminal_write_fails"]
)
def test_simulation_final_acceptance_lifecycle_order(tmp_path, monkeypatch, terminal_write_fails):
    """Acceptance follows the terminal result and precedes mutable persistence and refresh."""
    from booley.criteria.state import DevelopmentState
    from booley.flows.sim.request import SimRequest
    from tests.flows.sim.test_coverage_flow import _AcceptanceAdapter

    project, flow = _ordinary_public_hook_fixture(tmp_path, monkeypatch)
    state_path = _strict_sim_a_ticket(tmp_path, monkeypatch)
    events: list[str] = []
    terminal_phases: list[str] = []
    _spy_simulation_lifecycle(
        monkeypatch, events, terminal_phases, terminal_write_fails=terminal_write_fails
    )
    recorder = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )

    result = flow.execute(
        SimRequest(target="sim_a", work_dir=project, report_dir=tmp_path / "reports"),
        adapter=recorder,
    )

    first = {name: events.index(name) for name in dict.fromkeys(events)}
    assert first["terminal"] < first["acceptance:True"] < first["refresh"]
    assert len(DevelopmentState.load(state_path).acceptance_transactions) == 1
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
        assert first["acceptance:True"] < first["mutable"] < first["refresh"]

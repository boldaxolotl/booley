"""Hostile contracts for Cocotb-batch and coverage-aggregate campaign plans."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.coverage_execution import CoverageAggregateExecutor
from booley.flows.sim.campaign.flow_planning import plan_coarse_simulation_campaign
from booley.flows.sim.campaign.planning import manifest_digest
from booley.flows.sim.campaign.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    ValidatedTargetBinding,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.coverage_invocation import (
    CoverageInvocationRequest,
    prepare_coverage_invocation,
)
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationPreview,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.flow import _campaign_report_lines, _campaign_structured_details
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection
from tests.flows.sim.test_campaign_crash_matrix import (
    _build_execution,
    _CrashOnce,
    _InjectedProcessDeath,
)
from tests.flows.sim.test_coverage_invocation import project
from tests.flows.sim.test_coverage_transaction import NativeExecution


def _facts(
    root: Path, *, cocotb: bool, preview_names: tuple[str, ...] = ("count", "reset")
) -> tuple[TargetHandle, TargetInspection, SimulationPreview]:
    project = root / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text("[flows.sim]\nrun_cwd = '.'\n", encoding="utf-8")
    (project / "tests.toml").write_text('[sim]\ntests = ["reset", "count"]\n', encoding="utf-8")
    source = root / ("test_counter.py" if cocotb else "tb.sv")
    source.write_text("# cocotb\n" if cocotb else "module tb; endmodule\n", encoding="utf-8")
    if cocotb:
        _write_cocotb_core(root)
    handle = cast(
        TargetHandle,
        SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            selector="sim",
            name="sim",
            vlnv="acme:lib:dut:1",
            project_root=root.resolve(),
        ),
    )
    flow_options = {"cocotb_module": "test_counter"} if cocotb else {}
    inputs = (
        TargetInput(
            source.name,
            "acme:lib:dut:1",
            "user" if cocotb else "systemVerilogSource",
            ("tb",),
            False,
            {},
        ),
    )
    inspection = TargetInspection(handle, "tb", "sim", "verilator", flow_options, {}, inputs)
    preview = SimulationPreview(
        commands=(("run-batch", *preview_names),),
        groups=(preview_names,),
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="verilator",
        sources=(source.name,),
        constraints=(),
        parameters={},
        flow_options=flow_options,
    )
    return handle, inspection, preview


def test_coverage_plan_labels_its_build_variant_as_coverage(tmp_path: Path) -> None:
    handle, inspection, preview = _facts(tmp_path, cocotb=False)
    preview = replace(preview, groups=(("count", "reset"),))
    plan = plan_coarse_simulation_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=("count", "reset"),
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=False,
        kind="coverage_aggregate",
    )

    assert plan.manifest.document["build_variants"][0]["kind"] == "coverage"  # type: ignore[index]


def _write_cocotb_core(root: Path) -> None:
    (root / "dut.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:dut:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - test_counter.py: {file_type: user, copyto: test_counter.py}\n"
        "targets:\n"
        "  sim:\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n"
        "    flow: sim\n"
        "    flow_options:\n"
        "      tool: icarus\n"
        "      cocotb_module: test_counter\n",
        encoding="utf-8",
    )


def _plan(
    root: Path,
    *,
    kind: str,
    names: tuple[str, ...] = ("count", "reset"),
    cocotb: bool,
):
    handle, inspection, preview = _facts(root, cocotb=cocotb, preview_names=names)
    return plan_coarse_simulation_campaign(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=names,
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=7,
        execution_id="7" * 32,
        trace=False,
        kind=kind,
    )


def test_cocotb_named_selection_is_one_ordered_batch_work_item(tmp_path: Path) -> None:
    document = _plan(tmp_path, kind="cocotb_batch", cocotb=True).manifest.document

    assert document["workload"]["coverage"] is False  # type: ignore[index]
    assert len(document["work_items"]) == 1  # type: ignore[arg-type]
    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == "cocotb_batch"
    assert item["selection"] == {"kind": "named", "names": ("count", "reset")}
    assert item["arguments"] == ("count", "reset")


def test_cocotb_unfiltered_selection_is_one_disclosed_batch(tmp_path: Path) -> None:
    document = _plan(tmp_path, kind="cocotb_batch", names=(), cocotb=True).manifest.document

    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == "cocotb_batch"
    assert item["selection"] == {"kind": "unfiltered", "names": ()}
    assert item["arguments"] == ()


def test_coverage_selection_is_one_named_aggregate_with_coverage_variant(
    tmp_path: Path,
) -> None:
    document = _plan(tmp_path, kind="coverage_aggregate", cocotb=False).manifest.document

    assert document["workload"]["coverage"] is True  # type: ignore[index]
    assert len(document["work_items"]) == 1  # type: ignore[arg-type]
    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == "coverage_aggregate"
    assert item["selection"] == {"kind": "named", "names": ("count", "reset")}


def test_coverage_aggregate_accepts_a_cocotb_target(tmp_path: Path) -> None:
    document = _plan(tmp_path, kind="coverage_aggregate", cocotb=True).manifest.document

    assert document["work_items"][0]["kind"] == "coverage_aggregate"  # type: ignore[index]


@pytest.mark.parametrize(
    ("kind", "cocotb", "names"),
    [
        ("ordinary_hdl", False, ("reset",)),
        ("cocotb_batch", False, ("reset",)),
        ("coverage_aggregate", False, ()),
        ("coverage_aggregate", False, ("reset", "reset")),
    ],
)
def test_coarse_planner_rejects_kind_selection_and_target_contradictions(
    tmp_path: Path, kind: str, cocotb: bool, names: tuple[str, ...]
) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        _plan(tmp_path, kind=kind, names=names, cocotb=cocotb)


def test_coarse_planner_rejects_foreign_inspection(tmp_path: Path) -> None:
    handle, _inspection, preview = _facts(tmp_path, cocotb=True)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_handle = cast(
        TargetHandle,
        SimpleNamespace(
            identity="acme:lib:foreign:1#sim",
            selector="sim",
            name="sim",
            vlnv="acme:lib:foreign:1",
            project_root=foreign_root.resolve(),
        ),
    )
    inspection = TargetInspection(
        foreign_handle,
        "tb",
        "sim",
        "verilator",
        {"cocotb_module": "test_counter"},
        {},
        (),
    )

    with pytest.raises(SimulationCampaignIntegrityError, match="Target facts disagree"):
        plan_coarse_simulation_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=("reset", "count"),
            required_suite=("reset", "count"),
            revision="abc123",
            invocation_id=7,
            execution_id="7" * 32,
            trace=False,
            kind="cocotb_batch",
        )


def test_coarse_planner_rejects_selection_that_disagrees_with_preview(tmp_path: Path) -> None:
    handle, inspection, preview = _facts(tmp_path, cocotb=True)

    with pytest.raises(SimulationCampaignIntegrityError, match="preview"):
        plan_coarse_simulation_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=("reset",),
            required_suite=("reset", "count"),
            revision="abc123",
            invocation_id=7,
            execution_id="7" * 32,
            trace=False,
            kind="cocotb_batch",
        )


class _ImageCheckingExecution(NativeExecution):
    def __init__(self) -> None:
        super().__init__()
        self.original_executable = self.executable
        self.executed_original = False

    def bind_authenticated_attempt(self, snapshot_root: Path, run_cwd: Path) -> None:
        assert run_cwd.is_dir()
        self.executable = snapshot_root / self.executable.name
        self.original_executable.write_bytes(b"substituted original image")

    def run(self, request):
        self.executed_original |= self.executable == self.original_executable
        assert self.executable.read_bytes() == b"authenticated simulator image"
        return super().run(request)


class _FailedCoverageBuild(NativeExecution):
    def __init__(self, *, infrastructure: bool) -> None:
        super().__init__()
        self.infrastructure = infrastructure

    def build(self, request):
        del request
        return SimulationBuildResult(
            False, "compile failed", infrastructure_error=self.infrastructure
        )

    def run(self, request):
        raise AssertionError(f"failed build launched {request.run_id}")


class _RuntimeCheckingExecution(NativeExecution):
    def __init__(self) -> None:
        super().__init__()
        runtime = self.build_root / "data/stimulus.bin"
        runtime.parent.mkdir()
        runtime.write_bytes(b"authenticated runtime input")
        self.bound = False

    def bind_authenticated_attempt(self, snapshot_root: Path, run_cwd: Path) -> None:
        super().bind_authenticated_attempt(snapshot_root, run_cwd)
        assert (run_cwd / "data/stimulus.bin").read_bytes() == (b"authenticated runtime input")
        self.bound = True

    def run(self, request):
        assert self.bound
        return super().run(request)


def _coverage_campaign_plan(root: Path, *, runtime_input: bool = False):
    context = project(root)
    if runtime_input:
        (root / "stimulus.bin").write_bytes(b"source runtime input")
        core = root / "counter.core"
        core.write_text(
            core.read_text().replace(
                "files: [rtl/counter.sv]",
                "files: [rtl/counter.sv, {stimulus.bin: {file_type: user, "
                "copyto: data/stimulus.bin}}]",
            )
        )
    project_data = root / ".booley_project"
    project_data.mkdir()
    (project_data / "tests.toml").write_text(
        '[sim_0]\ntests = ["reset", "wrap"]\n', encoding="utf-8"
    )
    prepared = prepare_coverage_invocation(CoverageInvocationRequest(("sim_0",)), context)
    target = prepared.plan.targets[0]
    inspection = TargetCatalog.build(root).inspect(target.handle)
    names = target.selected_tests
    preview = SimulationPreview(
        commands=(("coverage", *names),),
        groups=(names,),
        target_identity=target.handle.identity,
        toplevel=inspection.toplevel,
        eda_tool=inspection.eda_tool,
        sources=tuple(item.path for item in inspection.inputs),
        constraints=(),
        parameters=inspection.parameters,
        flow_options=inspection.flow_options,
    )
    plan = plan_coarse_simulation_campaign(
        handle=target.handle,
        inspection=inspection,
        preview=preview,
        selected_tests=names,
        required_suite=target.declared_tests,
        revision="abc123",
        invocation_id=1,
        execution_id="8" * 32,
        trace=False,
        kind="coverage_aggregate",
    )
    return plan, target


def _run_coverage_campaign(root: Path, native: NativeExecution, *, runtime_input: bool = False):
    plan, target = _coverage_campaign_plan(root, runtime_input=runtime_input)
    executor = CoverageAggregateExecutor(
        plans={target.handle.identity: target},
        execution_factory=lambda _plan, _options: native,
    )
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
    )
    outcome = SimulationCampaign(executor).run(request)
    return outcome, CampaignStore(invocation / "targets/sim_0/campaign")


def test_coverage_aggregate_executes_authenticated_snapshot_not_original(
    tmp_path: Path,
) -> None:
    native = _ImageCheckingExecution()
    outcome, _store = _run_coverage_campaign(tmp_path, native)

    assert outcome.complete is True
    assert native.executed_original is False


def test_coverage_attempt_stages_runtime_before_binding_and_records_real_build_time(
    tmp_path: Path,
) -> None:
    native = _RuntimeCheckingExecution()
    outcome, store = _run_coverage_campaign(tmp_path, native, runtime_input=True)

    assert outcome.complete is True
    result = store.scan().items[0].result
    assert result is not None
    runtime = result.document["runtime_inputs"]
    assert len(runtime) == 1
    assert runtime[0]["destination"] == "data/stimulus.bin"
    attempt = next(
        store.work_item_directory(result.document["work_item_id"]).joinpath("attempts").iterdir()
    )
    build = json.loads((attempt / "private-build/build-result.json").read_bytes())
    assert build["elapsed_seconds"] > 0


def test_coverage_design_build_failure_has_exact_blocked_matrix(tmp_path: Path) -> None:
    outcome, store = _run_coverage_campaign(tmp_path, _FailedCoverageBuild(infrastructure=False))

    assert outcome.complete is True
    result = store.scan().items[0].result
    assert result is not None
    assert result.document["state"] == "blocked_by_build"
    assert result.document["bundle_id"] is None
    assert result.document["executable_snapshot"] is None
    assert result.document["runtime_inputs"] == ()
    for observation in result.document["observations"]:
        assert observation["execution"] == "blocked_by_build"
        assert observation["failure_class"] == "design"
        assert observation["functional"] == "not_observed"
        assert observation["assertions"] == "not_observed"


def test_coverage_infrastructure_build_failure_has_no_terminal_result(
    tmp_path: Path,
) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        _run_coverage_campaign(tmp_path, _FailedCoverageBuild(infrastructure=True))

    store = CampaignStore(tmp_path / "reports/1/targets/sim_0/campaign")
    recovery = store.scan()
    assert recovery.interrupted
    directory = store.work_item_directory(recovery.items[0].work_item_id)
    attempts = tuple((directory / "attempts").iterdir())
    assert len(attempts) == 1
    build = json.loads((attempts[0] / "private-build/build-result.json").read_bytes())
    assert build["state"] == "infrastructure_error"
    assert not (directory / "result.json").exists()


def _unmanaged() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


@dataclass
class _CocotbHarness:
    handle: TargetHandle
    build_root: Path
    launches: list[tuple[str, ...]]
    reported: tuple[SimulationTestOutcome, ...] | None = None
    transport_names: tuple[str, ...] | None = None
    transport_reported: tuple[SimulationTestOutcome, ...] | None = None

    @contextmanager
    def ordinary_group(self, _handle, names):
        yield _CocotbGroup(self, names)


class _CocotbGroup:
    def __init__(self, harness: _CocotbHarness, names: tuple[str, ...]) -> None:
        self._harness = harness
        self.names = names
        self.build_root = harness.build_root
        self.artifact_paths = (self.build_root / "simv",)
        self.compile_surface = SimpleNamespace(
            project_root=self.build_root.parent.resolve(), authored_paths=(), operational_paths=()
        )

    def planning_disclosure(self):
        return {}

    def compile(self):
        return SimpleNamespace(passed=True)

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()

    def reuse_compilation_from(self, source) -> None:
        assert source is not None

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
        del snapshot_root, run_cwd
        self._harness.launches.append(self.names)
        path = self.build_root / "cocotb-results.json"
        tests = self._harness.reported or tuple(
            SimulationTestOutcome(name=name, verdict="pass", passed=True) for name in self.names
        )
        transport_names = self._harness.transport_names or tuple(test.name for test in tests)
        transported = self._harness.transport_reported or tests
        path.write_text(json.dumps(_transport(transported, transport_names)), encoding="utf-8")
        return SimulationTargetOutcome(
            target="sim",
            target_identity=self._harness.handle.identity,
            toplevel="tb",
            eda_tool="icarus",
            passed=True,
            verdict="pass",
            elapsed_s=0.1,
            tests=tests,
            artifacts=(
                SimulationArtifactEvidence(
                    "cocotb_results_json", str(path), path.stat().st_size, transport_names
                ),
            ),
        )


def _transport(
    tests: tuple[SimulationTestOutcome, ...], names: tuple[str, ...]
) -> dict[str, object]:
    return {
        "state": "ok",
        "detail": "",
        "tests": [
            {
                "name": name,
                "module": "test_counter",
                "status": test.verdict,
                "failure": test.error_tail,
                "elapsed_s": 0.01,
            }
            for name, test in zip(names, tests, strict=True)
        ],
        "skipped_unselected": 0,
    }


def _assert_result_selection_is_authenticated(store: CampaignStore, work_item_id: str) -> None:
    result_path = store.work_item_directory(work_item_id) / "result.json"
    hostile = json.loads(result_path.read_bytes())
    hostile["observations"].reverse()
    result_path.chmod(0o600)
    result_path.write_bytes(canonical_json_bytes(hostile))
    with pytest.raises(SimulationCampaignIntegrityError, match="named work-item selection"):
        store.scan()


def _start_crashed_cocotb_campaign(
    plan, root: Path, executor: OrdinaryHdlSerialExecutor
) -> tuple[Path, CampaignStore]:
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
    )
    crash = _CrashOnce("before:simulation_result")
    with pytest.raises(_InjectedProcessDeath):
        SimulationCampaign(executor, publication_checkpoint=crash).run(request)
    return invocation, CampaignStore(invocation / "targets/sim/campaign")


def test_production_cocotb_batch_retries_whole_batch_after_crash(tmp_path: Path) -> None:
    """The production coordinator/executor never resumes inside a Cocotb batch."""
    plan = _plan(tmp_path, kind="cocotb_batch", cocotb=True)
    handle = TargetCatalog.build(tmp_path).select("sim", for_flow="sim")
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"simulator")
    launches: list[tuple[str, ...]] = []
    execution = _CocotbHarness(handle, build_root, launches)

    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: execution,  # type: ignore[arg-type,return-value]
    )
    invocation, store = _start_crashed_cocotb_campaign(plan, tmp_path, executor)
    node = ValidatedManifestNode(
        store.manifest_path, plan.manifest, manifest_digest(plan.manifest)
    )
    validated = ValidatedResumeManifest(
        node,
        (),
        (handle,),
        (ValidatedTargetBinding(node, tmp_path.resolve(), handle),),
    )
    outcome = SimulationCampaign(executor).run(
        ResumeCampaignRunRequest(
            validated,
            plan,
            tmp_path,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            _unmanaged(),
        )
    )

    assert outcome.complete is True
    assert launches == [("count", "reset"), ("count", "reset")]
    recovered = store.scan().items[0]
    assert recovered.attempt_count == 2
    result = recovered.result
    assert result is not None
    assert tuple(item["test"] for item in result.document["observations"]) == (
        "count",
        "reset",
    )
    assert [item["kind"] for item in result.document["evidence"]] == ["cocotb_results"]
    _assert_result_selection_is_authenticated(store, recovered.work_item_id)


def _run_cocotb(
    root: Path,
    names: tuple[str, ...],
    reported: tuple[SimulationTestOutcome, ...],
    *,
    transport_names: tuple[str, ...] | None = None,
    transport_reported: tuple[SimulationTestOutcome, ...] | None = None,
) -> CampaignStore:
    plan = _plan(root, kind="cocotb_batch", names=names, cocotb=True)
    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    build_root = root / "build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"simulator")
    execution = _CocotbHarness(
        handle, build_root, [], reported, transport_names, transport_reported
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: execution,  # type: ignore[arg-type,return-value]
    )
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _unmanaged()
    )
    SimulationCampaign(executor).run(request)
    return CampaignStore(invocation / "targets/sim/campaign")


def _result_document(store: CampaignStore) -> tuple[Path, dict[str, object]]:
    recovered = store.scan().items[0]
    path = store.work_item_directory(recovered.work_item_id) / "result.json"
    return path, json.loads(path.read_bytes())


def test_cocotb_result_preserves_exact_ordered_observations(tmp_path: Path) -> None:
    reported = (
        SimulationTestOutcome(
            name="count",
            verdict="fail",
            passed=False,
            sva_errors=2,
            error_tail="counter assertion failed",
        ),
        SimulationTestOutcome(name="reset", verdict="pass", passed=True),
    )
    store = _run_cocotb(tmp_path, ("count", "reset"), reported)

    result = store.scan().items[0].result
    assert result is not None
    observations = result.document["observations"]
    assert [(item["test"], item["functional"]) for item in observations] == [
        ("count", "fail"),
        ("reset", "pass"),
    ]
    assert observations[0]["assertions"] == "dirty"
    assert observations[0]["assertion_count"] == 2
    assert observations[0]["detail"] == {"reason": "counter assertion failed"}


def test_unfiltered_cocotb_binds_discovered_order_and_detail(tmp_path: Path) -> None:
    reported = (
        SimulationTestOutcome(name="discovered_b", verdict="pass", passed=True),
        SimulationTestOutcome(
            name="discovered_a", verdict="fail", passed=False, error_tail="bad value"
        ),
    )
    store = _run_cocotb(tmp_path, (), reported)

    result = store.scan().items[0].result
    assert result is not None
    observations = result.document["observations"]
    assert [(item["test"], item["functional"]) for item in observations] == [
        ("discovered_b", "pass"),
        ("discovered_a", "fail"),
    ]
    assert observations[1]["detail"] == {"reason": "bad value"}


@pytest.mark.parametrize("substitution", ["name", "verdict", "detail"])
def test_cocotb_rejects_authenticated_source_transport_substitution(
    tmp_path: Path, substitution: str
) -> None:
    reported = (
        SimulationTestOutcome(
            name="count", verdict="fail", passed=False, error_tail="reported failure"
        ),
        SimulationTestOutcome(name="reset", verdict="pass", passed=True),
    )
    transported = reported
    names = ("count", "reset")
    if substitution == "name":
        names = ("reset", "count")
    elif substitution == "verdict":
        transported = (
            SimulationTestOutcome(name="count", verdict="pass", passed=True),
            reported[1],
        )
    else:
        transported = (
            SimulationTestOutcome(
                name="count", verdict="fail", passed=False, error_tail="source failure"
            ),
            reported[1],
        )
    with pytest.raises(SimulationCampaignIntegrityError, match="transport"):
        _run_cocotb(
            tmp_path,
            ("count", "reset"),
            reported,
            transport_names=names,
            transport_reported=transported,
        )


def _substitute_observation(document: dict[str, object], field: str) -> None:
    observation = document["observations"][0]  # type: ignore[index]
    if field == "detail":
        observation["detail"] = {"reason": "forged detail"}
        return
    observation[field] = "fail" if field == "functional" else "dirty"
    if field == "assertions":
        observation["assertion_count"] = 1
    document["grade"] = "fail"


@pytest.mark.parametrize("field", ["functional", "assertions", "detail"])
def test_cocotb_rejects_result_substitution_against_authenticated_transport(
    tmp_path: Path, field: str
) -> None:
    reported = (
        SimulationTestOutcome(name="count", verdict="pass", passed=True),
        SimulationTestOutcome(name="reset", verdict="pass", passed=True),
    )
    store = _run_cocotb(tmp_path, ("count", "reset"), reported)
    path, document = _result_document(store)
    _substitute_observation(document, field)
    path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(SimulationCampaignIntegrityError, match="transport"):
        store.scan()


def test_mcp_campaign_details_bound_observations_without_collapsing_axes(
    tmp_path: Path,
) -> None:
    observations = tuple(
        {
            "test": f"test_{index:02d}",
            "execution": "timeout" if index == 39 else "completed",
            "functional": "fail" if index % 2 else "pass",
            "assertions": "dirty" if index % 3 == 0 else "clean",
            "assertion_count": index,
            "detail": {"reason": f"detail-{index}"},
        }
        for index in range(40)
    )
    outcome = SimpleNamespace(
        target={"selector": "sim"},
        manifest_path=tmp_path / "targets/sim/campaign/manifest.json",
        summary_path=tmp_path / "targets/sim/campaign/summary.json",
        coverage_reference=None,
        aggregate_grade="fail",
        complete=True,
        observations=observations,
    )

    details = _campaign_structured_details((outcome,))["sim"]

    assert details["observation_total"] == 40
    assert details["observations_truncated"] is True
    assert len(details["observations"]) == 32
    assert set(details["observations"][0]) == {
        "test",
        "execution",
        "functional",
        "assertions",
        "assertion_count",
        "detail",
    }
    assert details["observation_counts"]["execution"] == {"completed": 39, "timeout": 1}


def test_campaign_report_preserves_the_failed_simulator_reason(tmp_path: Path) -> None:
    outcome = SimpleNamespace(
        target={"selector": "sim_fail"},
        manifest_path=tmp_path / "campaign/manifest.json",
        aggregate_grade="fail",
        observations=({"detail": {"reason": "intentional simulator failure"}},),
    )

    assert "intentional simulator failure" in _campaign_report_lines((outcome,))[0]

"""Cocotb batch and coverage aggregate work items of Simulation Records."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.record import codec, collect_pre_sim_firings, serial_execution
from booley.flows.sim.record.codec import (
    MAX_COMMAND_BYTES,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    decode_simulation_result,
    encode_simulation_result,
)
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
    SimulationRecordPublicationError,
)
from booley.flows.sim.record.coverage_execution import (
    CoverageAggregateExecutor,
)
from booley.flows.sim.record.flow_planning import plan_coarse_simulation_record
from booley.flows.sim.record.model import SimulationResult, create_simulation_record_plan
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.verilator_coverage import SimulationRunResult
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInspection
from tests.flows.sim.simulation_record_test_support import (
    _admission,
    _build_execution,
    _completed_result_documents,
    _coverage_simulation_record_plan,
    _executor,
    _facts,
    _failed_shared_executor,
    _FailedCoverageBuild,
    _handle,
    _maximum_hook_selection_plan,
    _plan,
    _request,
    _run_coverage_simulation_record,
    _unfiltered_cocotb_manifest,
)
from tests.flows.sim.test_coverage_transaction import NativeExecution
from tests.flows.sim.test_simulation_record_crash_matrix import (
    _CrashOnce,
    _InjectedProcessDeath,
)


def test_no_waivers_is_written_only_when_requested(tmp_path: Path) -> None:
    (tmp_path / "default").mkdir()
    (tmp_path / "raw").mkdir()
    default = _plan(
        tmp_path / "default", kind="coverage_aggregate", cocotb=False
    ).manifest.document
    assert default["$schema"] == "booley.simulation-record-manifest/v1"
    assert "no_waivers" not in default["workload"]  # type: ignore[operator]
    raw = _plan(tmp_path / "raw", kind="coverage_aggregate", cocotb=False, no_waivers=True)
    document = raw.manifest.document
    assert document["$schema"] == "booley.simulation-record-manifest/v1"
    assert document["workload"]["no_waivers"] is True  # type: ignore[index]
    # Waivers never change the build, so the variant identity must not move.
    assert document["build_variants"] == default["build_variants"]


def test_no_waivers_plan_rejects_non_coverage_kind(tmp_path: Path) -> None:
    with pytest.raises(SimulationRecordIntegrityError, match="no_waivers"):
        _plan(tmp_path, kind="cocotb_batch", cocotb=True, no_waivers=True)


@pytest.mark.parametrize(
    ("kind", "names", "cocotb", "coverage", "selection", "arguments"),
    [
        pytest.param(
            "cocotb_batch",
            None,
            True,
            False,
            {"kind": "named", "names": ("count", "reset")},
            ("count", "reset"),
            id="cocotb-named-batch",
        ),
        pytest.param(
            "cocotb_batch",
            (),
            True,
            False,
            {"kind": "unfiltered", "names": ()},
            (),
            id="cocotb-unfiltered-batch",
        ),
        pytest.param(
            "coverage_aggregate",
            None,
            False,
            True,
            {"kind": "named", "names": ("count", "reset")},
            None,
            id="coverage-named-aggregate",
        ),
        pytest.param("coverage_aggregate", None, True, True, None, None, id="coverage-cocotb"),
    ],
)
def test_selection_is_one_work_item_of_its_kind(
    tmp_path: Path,
    kind: str,
    names: tuple[str, ...] | None,
    cocotb: bool,
    coverage: bool,
    selection: dict[str, object] | None,
    arguments: tuple[str, ...] | None,
) -> None:
    options = {} if names is None else {"names": names}
    document = _plan(tmp_path, kind=kind, cocotb=cocotb, **options).manifest.document

    assert document["workload"]["coverage"] is coverage  # type: ignore[index]
    assert (document["build_variants"][0]["kind"] == "coverage") is coverage  # type: ignore[index]
    assert len(document["work_items"]) == 1  # type: ignore[arg-type]
    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == kind
    if selection is not None:
        assert item["selection"] == selection
    if arguments is not None:
        assert item["arguments"] == arguments


@pytest.mark.parametrize(
    ("kind", "cocotb", "names"),
    [
        ("ordinary_hdl", False, ("reset",)),
        ("cocotb_batch", False, ("reset",)),
        ("coverage_aggregate", False, ()),
    ],
)
def test_coarse_planner_rejects_kind_selection_and_target_contradictions(
    tmp_path: Path, kind: str, cocotb: bool, names: tuple[str, ...]
) -> None:
    with pytest.raises(SimulationRecordIntegrityError):
        _plan(tmp_path, kind=kind, names=names, cocotb=cocotb)


def _plan_cocotb_batch(handle, inspection, preview, selected: tuple[str, ...]):
    return plan_coarse_simulation_record(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=selected,
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=7,
        execution_id="7" * 32,
        trace=False,
        kind="cocotb_batch",
    )


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

    with pytest.raises(SimulationRecordIntegrityError, match="Target facts disagree"):
        _plan_cocotb_batch(handle, inspection, preview, ("reset", "count"))


def test_coarse_planner_rejects_selection_that_disagrees_with_preview(tmp_path: Path) -> None:
    handle, inspection, preview = _facts(tmp_path, cocotb=True)

    with pytest.raises(SimulationRecordIntegrityError, match="preview"):
        _plan_cocotb_batch(handle, inspection, preview, ("reset",))


class _ImageCheckingExecution(NativeExecution):
    def __init__(self) -> None:
        super().__init__()
        self.original_executable = self.executable
        self.executed_original = False

    def bind_authenticated_attempt(
        self, bundle_root: Path, evidence_root: Path, run_cwd: Path, verify_bundle
    ) -> None:
        assert run_cwd.is_dir()
        self.executable = bundle_root / self.executable.name
        self.original_executable.write_bytes(b"substituted original image")

    def run(self, request):
        self.executed_original |= self.executable == self.original_executable
        assert self.executable.read_bytes() == b"authenticated simulator image"
        return super().run(request)


class _RuntimeCheckingExecution(NativeExecution):
    def __init__(self) -> None:
        super().__init__()
        runtime = self.build_root / "data/stimulus.bin"
        runtime.parent.mkdir()
        runtime.write_bytes(b"authenticated runtime input")
        self.bound = False

    def build(self, request):
        # The build, image copy and runtime staging all happen inside the retained lease.
        assert self.lease_open
        return super().build(request)

    def bind_authenticated_attempt(
        self, bundle_root: Path, evidence_root: Path, run_cwd: Path, verify_bundle
    ) -> None:
        super().bind_authenticated_attempt(bundle_root, evidence_root, run_cwd, verify_bundle)
        assert (run_cwd / "data/stimulus.bin").read_bytes() == (b"authenticated runtime input")
        assert self.lease_open
        self.bound = True

    def run(self, request):
        assert self.bound
        return super().run(request)


def test_coverage_aggregate_executes_authenticated_bundle_not_original(
    tmp_path: Path,
) -> None:
    native = _ImageCheckingExecution()
    outcome, store = _run_coverage_simulation_record(tmp_path, native)

    assert outcome.complete is True
    assert native.executed_original is False
    _assert_native_build_scope(store)


def test_coverage_attempt_launches_from_its_private_bundle_without_a_copy(tmp_path: Path) -> None:
    native = NativeExecution()
    outcome, store = _run_coverage_simulation_record(tmp_path, native)

    assert outcome.complete is True
    (attempt,) = store.root.glob("work-items/*/attempts/*")
    assert native.executable.parent == attempt / "private-build"
    assert native.evidence_root == attempt / "execution-evidence"
    assert not (attempt / "snapshot").exists()
    copies = [
        path
        for path in attempt.rglob("*")
        if path.is_file() and path.read_bytes() == b"authenticated simulator image"
    ]
    assert copies == [attempt / "private-build" / "simv"]
    result = store.scan().items[0].result
    assert result is not None
    assert result.document["bundle_verification"]["verified_after_exit"] is True


class _MutatingExecution(NativeExecution):
    """A native execution whose run changes the bundle before or after its launch."""

    def __init__(self, when: str) -> None:
        super().__init__()
        self.when = when

    def _stray(self) -> None:
        (self.executable.parent / "stray.so").write_bytes(b"stray")

    def run(self, request):
        if self.when == "before_launch":
            self._stray()
        result = super().run(request)
        if self.when == "during_run":
            self._stray()
        return result


@pytest.mark.parametrize("when", ["before_launch", "during_run"])
def test_coverage_bundle_mutation_fails_closed(tmp_path: Path, when: str) -> None:
    native = _MutatingExecution(when)

    with pytest.raises(SimulationRecordIntegrityError, match="undeclared bundle entry"):
        _run_coverage_simulation_record(tmp_path, native)

    # The integrity failure is the bundle's, not a coverage collection error, and no
    # Result is published for the attempt.
    assert (len(native.runs) == 0) is (when == "before_launch")
    assert not list(tmp_path.glob("reports/1/targets/*/record/work-items/*/result.json"))


def _assert_native_build_scope(store: SimulationRecordStore) -> None:
    """Native coverage builds stay private to their work item, with no compiler record."""
    (build_result,) = store.root.rglob("private-build/build-result.json")
    directory = build_result.parent
    attempt = json.loads((directory / "build-attempt.json").read_text())
    bundle = json.loads((directory / "evidence" / "bundle.json").read_text())
    result = json.loads(build_result.read_text())
    simulation_attempt_id = directory.parent.name.split("-", 1)[1]
    assert attempt["sharing"] == bundle["sharing"] == "private_work_item"
    assert result["bundle"]["sharing"] == "private_work_item"
    assert attempt["owner"] == bundle["owner"]
    assert attempt["owner"]["simulation_attempt_id"] == simulation_attempt_id
    assert attempt["owner"]["work_item_id"]
    assert attempt["build_attempt_id"] == simulation_attempt_id
    assert attempt["build_attempt_ordinal"] == 1
    assert result["build_attempt"]["build_attempt_id"] == simulation_attempt_id
    assert result["evidence"] == []


def _coverage_record_run(root: Path, native: NativeExecution):
    """Return the aggregate executor, its first-run request, store and planned Target."""
    plan, target = _coverage_simulation_record_plan(root)
    executor = CoverageAggregateExecutor(
        plans={target.handle.identity: target},
        execution_factory=lambda _plan, _options, _commands, _access: native,
    )
    invocation = root / "reports/1"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan, root, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )
    return executor, request, SimulationRecordStore(invocation / "targets/sim_0/record"), target


def test_coverage_resume_reuses_retained_nested_simulation_record(tmp_path: Path) -> None:
    native = NativeExecution()
    executor, request, store, _target = _coverage_record_run(tmp_path, native)
    simulation_record = SimulationRecord(executor)
    first = simulation_record.run(request)

    resumed = simulation_record.run(
        NewSimulationRecordRunRequest(
            request.plan,
            tmp_path,
            request.report_root,
            SimulationRecordPolicy(),
            record_directory=store.root,
            invocation_directory=request.invocation_directory,
            admission=_admission(),
        )
    )

    assert first.complete is True
    assert resumed.complete is True
    assert len(native.runs) == 2
    assert resumed.coverage_reference["coverage_record"]["schema"] == ("booley.coverage-record/v1")


@pytest.mark.parametrize(
    ("coverage", "scans"), [(True, 5), (False, 4)], ids=["coverage", "ordinary"]
)
def test_simulation_record_run_adds_no_recovery_scan_for_failure_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coverage: bool, scans: int
) -> None:
    """Failure-detail facts reuse the coordinator's scans.

    Run start, summary regeneration, final scan and the outcome collector scan
    the Simulation Record (4); a coverage commit adds one (5).
    """
    calls: list[None] = []
    original = SimulationRecordStore.scan

    def counting_scan(*args, **kwargs):
        calls.append(None)
        return original(*args, **kwargs)

    monkeypatch.setattr(SimulationRecordStore, "scan", counting_scan)
    if coverage:
        _run_coverage_simulation_record(tmp_path, NativeExecution())
    else:
        _run_cocotb(
            tmp_path,
            ("reset",),
            (SimulationTestOutcome(name="reset", verdict="pass", passed=True),),
        )

    assert len(calls) == scans


def test_coverage_simulation_record_refuses_an_oversized_result_at_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No coverage run produces a result above the real ceiling, so the limit is lowered in place.
    schema, fields, _limit, reader = codec._DOCUMENT_SPECS[SimulationResult]
    # No coverage run produces a result above the real ceiling, so the limit is lowered in place.
    monkeypatch.setitem(codec._DOCUMENT_SPECS, SimulationResult, (schema, fields, 64, reader))

    with pytest.raises(SimulationRecordPublicationError) as caught:
        _run_coverage_simulation_record(tmp_path, NativeExecution())

    assert isinstance(caught.value.__cause__, SimulationRecordIntegrityError)
    assert "size ceiling" in str(caught.value.__cause__)
    assert not list(tmp_path.rglob("work-items/*/result.json"))


class _TimeoutThenMissingInputExecution(NativeExecution):
    """The first Test times out; the second aborts on a missing input (a design fault)."""

    def run(self, request):
        super().run(request)
        if len(self.runs) == 1:
            return SimulationRunResult("timeout", "simulation timed out")
        return SimulationRunResult(
            "fail",
            "$readmemh: Cannot open memory.hex",
            termination="fatal_init",
            failure_kind="missing_input",
        )


def test_coverage_timeout_before_design_abort_publishes_aborted_fail(tmp_path: Path) -> None:
    """Abort dominates: the earlier timeout is recorded as an aborted design observation.

    Coverage runs one process per Test and continues after a timeout or a
    non-infrastructure abort, so both observations reach one result.
    """
    outcome, store = _run_coverage_simulation_record(tmp_path, _TimeoutThenMissingInputExecution())

    assert outcome.complete is True
    result = store.scan().items[0].result
    assert result is not None
    assert decode_simulation_result(encode_simulation_result(result)) == result
    assert result.document["state"] == "aborted"
    assert result.document["grade"] == "fail"
    for observation in result.document["observations"]:
        assert (
            observation["execution"],
            observation["failure_class"],
            observation["functional"],
            observation["assertions"],
            observation["cycle_count"],
        ) == ("aborted", "design", "fail", "not_observed", None)


def test_coverage_attempt_stages_runtime_before_binding_and_records_real_build_time(
    tmp_path: Path,
) -> None:
    native = _RuntimeCheckingExecution()
    outcome, store = _run_coverage_simulation_record(tmp_path, native, runtime_input=True)

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
    outcome, store = _run_coverage_simulation_record(
        tmp_path, _FailedCoverageBuild(infrastructure=False)
    )

    assert outcome.complete is True
    result = store.scan().items[0].result
    assert result is not None
    assert result.document["state"] == "blocked_by_build"
    assert result.document["bundle_id"] is None
    assert result.document["bundle_verification"] is None
    assert result.document["runtime_inputs"] == ()
    for observation in result.document["observations"]:
        assert observation["execution"] == "blocked_by_build"
        assert observation["failure_class"] == "design"
        assert observation["functional"] == "not_observed"
        assert observation["assertions"] == "not_observed"


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("compile failed", id="short-reason"),
        pytest.param("x" * (MAX_COMMAND_BYTES + 1), id="over-ceiling-reason"),
        # The odd-length ASCII prefix puts the byte cut inside a two-byte character.
        pytest.param("x" + "\u00e9" * MAX_COMMAND_BYTES, id="multibyte-straddles-cut"),
    ],
)
def test_coverage_infrastructure_build_failure_has_terminal_error_result(
    tmp_path: Path, reason: str
) -> None:
    executor, request, store, _target = _coverage_record_run(
        tmp_path, _FailedCoverageBuild(infrastructure=True, reason=reason)
    )
    outcome = SimulationRecord(executor).run(request)
    assert outcome.complete
    assert outcome.aggregate_grade == "error"

    recovery = store.scan()
    assert recovery.complete
    assert not recovery.interrupted
    directory = store.work_item_directory(recovery.items[0].work_item_id)
    attempts = tuple((directory / "attempts").iterdir())
    assert len(attempts) == 1
    build = json.loads((attempts[0] / "private-build/build-result.json").read_bytes())
    assert build["state"] == "infrastructure_error"
    message = build["observation"]["message"]
    assert len(message.encode()) <= MAX_COMMAND_BYTES
    assert message.startswith(reason[:64])
    assert (directory / "result.json").exists()
    result = recovery.items[0].result
    assert result is not None
    assert result.document["state"] == "aborted"
    assert result.document["grade"] == "error"
    assert result.document["bundle_verification"] is None
    observation = result.document["observations"][0]
    assert observation["execution"] == "aborted"
    assert observation["failure_class"] == "infrastructure"
    assert observation["functional"] == "not_observed"
    assert observation["detail"]["code"] == "build_transport"
    assert SimulationRecord(executor).run(request).complete
    assert store.scan().items[0].attempt_count == 1


@dataclass
class _CocotbHarness:
    handle: TargetHandle
    build_root: Path
    launches: list[tuple[str, ...]]
    reported: tuple[SimulationTestOutcome, ...] | None = None
    raw: bytes | None = None  # written verbatim instead of the generated transport

    @contextmanager
    def ordinary_group(self, _handle, names, _test_runs=None):
        yield _CocotbGroup(self, names)


class _CocotbGroup:
    def __init__(self, harness: _CocotbHarness, names: tuple[str, ...]) -> None:
        self._harness = harness
        self.names = names
        self.build_root = harness.build_root
        self.artifact_paths = (self.build_root / "simv",)
        self.compile_surface = SimpleNamespace(
            project_root=self.build_root.parent.resolve(),
            authored_paths=(),
            operational_paths=(),
            optional_paths=(),
        )

    def planning_disclosure(self):
        return {}

    def for_work_item(self, names, _test_runs):
        return _CocotbGroup(self._harness, names)

    def compile(self):
        return SimpleNamespace(passed=True)

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()

    def launch_bundle(self, bundle_root: Path, evidence_root: Path, run_cwd: Path):
        del bundle_root, evidence_root, run_cwd
        self._harness.launches.append(self.names)
        path = self.build_root / "cocotb-results.json"
        tests = self._harness.reported or tuple(
            SimulationTestOutcome(name=name, verdict="pass", passed=True) for name in self.names
        )
        transport_names = tuple(test.name for test in tests)
        if self._harness.raw is None:
            path.write_text(json.dumps(_transport(tests, transport_names)), encoding="utf-8")
        else:
            path.write_bytes(self._harness.raw)
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


def _start_crashed_cocotb_simulation_record(
    plan, root: Path, executor: OrdinaryHdlSerialExecutor
) -> tuple[Path, SimulationRecordStore]:
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan, root, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )
    crash = _CrashOnce("before:simulation_result")
    with pytest.raises(_InjectedProcessDeath):
        SimulationRecord(executor, publication_checkpoint=crash).run(request)
    return invocation, SimulationRecordStore(invocation / "targets/sim/record")


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
    invocation, store = _start_crashed_cocotb_simulation_record(plan, tmp_path, executor)
    outcome = SimulationRecord(executor).run(
        NewSimulationRecordRunRequest(
            plan,
            tmp_path,
            invocation.parent,
            SimulationRecordPolicy(),
            record_directory=store.root,
            invocation_directory=invocation,
            admission=_admission(),
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


def _run_cocotb(
    root: Path,
    names: tuple[str, ...],
    reported: tuple[SimulationTestOutcome, ...],
    raw: bytes | None = None,
) -> SimulationRecordStore:
    plan = _plan(root, kind="cocotb_batch", names=names, cocotb=True)
    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    build_root = root / "build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"simulator")
    execution = _CocotbHarness(handle, build_root, [], reported, raw)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: execution,  # type: ignore[arg-type,return-value]
    )
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewSimulationRecordRunRequest(
        plan, root, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
    )
    SimulationRecord(executor).run(request)
    return SimulationRecordStore(invocation / "targets/sim/record")


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


def test_cocotb_simulation_record_rejects_malformed_results_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A results file the decoder rejects is a record integrity error, not a verdict.

    The file is the payload itself: the record must decode it directly instead of
    wrapping it in a stdout line for the line parser.
    """
    reported = (SimulationTestOutcome(name="count", verdict="pass", passed=True),)

    def forbidden_parse(output: str) -> None:
        raise AssertionError("record decoded the file through the stdout-line parser")

    # Patch the defining module and the record's own namespace: either call style fails.
    monkeypatch.setattr(cocotb_results, "parse_results_line", forbidden_parse)
    monkeypatch.setattr(serial_execution, "parse_results_line", forbidden_parse, raising=False)
    for name, raw, message in (
        ("not-utf8", b"\xff", "Cocotb transport is not UTF-8"),
        ("not-json", b"{broken", "Cocotb transport is invalid"),
        ("not-a-results-object", b"[]", "Cocotb transport is invalid"),
    ):
        root = tmp_path / name
        root.mkdir()
        with pytest.raises(SimulationRecordIntegrityError, match=message):
            _run_cocotb(root, ("count",), reported, raw=raw)


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


def test_cocotb_scan_takes_the_published_result_without_rereading_its_transport(
    tmp_path: Path,
) -> None:
    reported = (
        SimulationTestOutcome(name="count", verdict="pass", passed=True),
        SimulationTestOutcome(name="reset", verdict="pass", passed=True),
    )
    store = _run_cocotb(tmp_path, ("count", "reset"), reported)
    published = store.scan().items[0].result
    assert published is not None
    (reference,) = published.document["evidence"]
    (attempt,) = (store.root / "work-items").glob("*/attempts/*")
    (attempt / reference["path"]).unlink()

    assert store.scan().items[0].result == published


def test_unfiltered_cocotb_shared_build_failure_has_one_unnamed_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _unfiltered_cocotb_manifest()
    plan = create_simulation_record_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()  # a literal run_cwd must exist before any build (#881)
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    outcome = SimulationRecord(_failed_shared_executor(tmp_path / "engine-build", counters)).run(
        NewSimulationRecordRunRequest(
            plan, project, invocation.parent, SimulationRecordPolicy(), invocation, _admission()
        )
    )

    store = SimulationRecordStore(invocation / "targets/sim/record")
    result = _completed_result_documents(store)[0]
    assert outcome.complete is True
    assert outcome.aggregate_grade == "fail"
    assert result["grade"] == "fail"
    assert result["observations"] == [
        {
            "test": None,
            "test_run": "<default>",
            "execution": "blocked_by_build",
            "failure_class": "design",
            "functional": "not_observed",
            "assertions": "not_observed",
            "assertion_count": 0,
            "detail": {"reason": "compiler reason"},
            "cycle_count": None,
        }
    ]


# Firings published for the overflow proof. Every firing adds one fixed-size
# reference to the result; with 4000 observations about 1460 firings reach
# RECORD_MAX_BYTES, so 2000 overflows it by about 10%. If format growth moves
# the threshold past this count, the size assertion fails instead of passing
# vacuously.
_AGGREGATE_OVERFLOW_FIRINGS = 2000


def _aggregate_result_bound_fixture(tmp_path, monkeypatch):
    # Durability is not under test here; disk syncs dominate under load.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.record.codec import RESULT_SCHEMA
    from booley.flows.sim.record.pre_sim_evidence import publish_pre_sim_firing
    from tests.flows.sim.test_simulation_record_codec_golden import _simulation_result

    names = tuple(f"test{index:04d}" for index in range(4000))
    plan, _ = _maximum_hook_selection_plan(names)
    store = SimulationRecordStore(tmp_path / "record")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    for ordinal, name in enumerate(names[:_AGGREGATE_OVERFLOW_FIRINGS], 1):
        publish_pre_sim_firing(
            request,
            PreSimEvidence(("true",), (name,), "passed", 0.0, returncode=0),
            ordinal=ordinal,
        )
    document = json.loads(_simulation_result("completed"))
    document["$schema"] = RESULT_SCHEMA
    # No public builder emits a result document above the ceiling, so it is built by hand and `_common` supplies its identity fields.
    document.update(serial_execution._common(request))
    document["attempt_id"] = request.attempt_id
    document["evidence"] = store.pre_sim_references(request.attempt_directory)
    document["observations"] = [
        {
            **serial_execution.observation_document(
                SimulationTestOutcome(name=name, verdict="pass", passed=True)
            ),
            "test_run": name,
        }
        for name in names
    ]
    return store, request, document


def test_maximum_aggregate_result_overflow_keeps_all_authenticated_sidecars(tmp_path, monkeypatch):
    from booley.flows.sim.record.codec import RECORD_MAX_BYTES, decode_simulation_result

    store, request, document = _aggregate_result_bound_fixture(tmp_path, monkeypatch)
    raw = canonical_json_bytes(document)
    assert len(document["evidence"]) == _AGGREGATE_OVERFLOW_FIRINGS
    assert len(document["observations"]) == 4000
    assert len(raw) > RECORD_MAX_BYTES
    with pytest.raises(SimulationRecordIntegrityError, match="byte size ceiling"):
        store.publish_result(request.work_item["work_item_id"], SimulationResult(document))
    assert not (request.attempt_directory.parent.parent / "result.json").exists()
    assert len(collect_pre_sim_firings(store.manifest_path)) == _AGGREGATE_OVERFLOW_FIRINGS
    without_hooks = {**document, "evidence": []}
    assert len(canonical_json_bytes(without_hooks)) < RECORD_MAX_BYTES
    assert (
        len(decode_simulation_result(canonical_json_bytes(without_hooks)).document["observations"])
        == 4000
    )

"""Cocotb batch and coverage aggregate work items of Simulation Campaigns."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.campaign import collect_pre_sim_firings, serial_execution
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_result,
    encode_simulation_result,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    CampaignPublicationError,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.coverage_execution import (
    CoverageAggregateExecutor,
    _CoverageAggregateError,
)
from booley.flows.sim.campaign.flow_planning import plan_coarse_simulation_campaign
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import manifest_digest
from booley.flows.sim.campaign.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    ValidatedTargetBinding,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import (
    SimulationArtifactEvidence,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.flows.sim.verilator_coverage import SimulationRunResult
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInspection
from tests.flows.sim.campaign_test_support import (
    _admission,
    _build_execution,
    _completed_result_documents,
    _coverage_campaign_plan,
    _executor,
    _facts,
    _failed_shared_executor,
    _FailedCoverageBuild,
    _handle,
    _maximum_hook_selection_plan,
    _plan,
    _request,
    _run_coverage_campaign,
    _unfiltered_cocotb_manifest,
    _validated_resume,
)
from tests.flows.sim.test_campaign_crash_matrix import (
    _CrashOnce,
    _InjectedProcessDeath,
)
from tests.flows.sim.test_coverage_transaction import NativeExecution


def test_coverage_plan_labels_its_build_variant_as_coverage(tmp_path: Path) -> None:
    handle, inspection, preview = _facts(tmp_path, cocotb=False)
    preview = replace(preview, groups=(("count", "reset"),), run_groups=())
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


def test_no_waivers_is_written_only_when_requested(tmp_path: Path) -> None:
    (tmp_path / "default").mkdir()
    (tmp_path / "raw").mkdir()
    default = _plan(
        tmp_path / "default", kind="coverage_aggregate", cocotb=False
    ).manifest.document
    assert default["$schema"] == "booley.simulation-campaign-manifest/v4"
    assert "no_waivers" not in default["workload"]  # type: ignore[operator]
    raw = _plan(tmp_path / "raw", kind="coverage_aggregate", cocotb=False, no_waivers=True)
    document = raw.manifest.document
    assert document["$schema"] == "booley.simulation-campaign-manifest/v4"
    assert document["workload"]["no_waivers"] is True  # type: ignore[index]
    # Waivers never change the build, so the variant identity must not move.
    assert document["build_variants"] == default["build_variants"]


def test_no_waivers_plan_rejects_non_coverage_kind(tmp_path: Path) -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="no_waivers"):
        _plan(tmp_path, kind="cocotb_batch", cocotb=True, no_waivers=True)


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


def _retain_score_bearing_invalid_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish a nested Campaign whose scoring is invalid but still carries rollups."""
    from booley.flows.sim import coverage_transaction

    original = coverage_transaction.publish_coverage_campaign

    def publish_invalid_campaign(target_dir, campaign):
        paths = original(target_dir, campaign)
        document = json.loads(paths.campaign.read_text())
        document["collection"]["status"] = "collector_error"
        document["normalization"]["status"] = "partial"
        document["scoring"] = {"status": "invalid", "reason": "collector_error"}
        paths.campaign.write_text(json.dumps(document))
        return paths

    monkeypatch.setattr(
        coverage_transaction, "publish_coverage_campaign", publish_invalid_campaign
    )


def test_coverage_aggregate_executes_authenticated_snapshot_not_original(
    tmp_path: Path,
) -> None:
    native = _ImageCheckingExecution()
    outcome, store = _run_coverage_campaign(tmp_path, native)

    assert outcome.complete is True
    assert native.executed_original is False
    _assert_native_build_scope(store)


def _assert_native_build_scope(store: CampaignStore) -> None:
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


def test_coverage_resume_reuses_retained_nested_campaign(tmp_path: Path) -> None:
    plan, target = _coverage_campaign_plan(tmp_path)
    native = NativeExecution()
    executor = CoverageAggregateExecutor(
        plans={target.handle.identity: target},
        execution_factory=lambda _plan, _options, _commands, _access: native,
    )
    invocation = tmp_path / "reports/1"
    invocation.mkdir(parents=True)
    campaign = SimulationCampaign(executor)
    first = campaign.run(
        NewCampaignRunRequest(
            plan,
            tmp_path,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            _admission(),
        )
    )
    store = CampaignStore(invocation / "targets/sim_0/campaign")
    validated = _validated_resume(store, plan, target, tmp_path)

    resumed = campaign.run(
        ResumeCampaignRunRequest(
            validated,
            plan,
            tmp_path,
            invocation.parent,
            CampaignPolicy(),
            invocation,
            _admission(),
        )
    )

    assert first.complete is True
    assert resumed.complete is True
    assert len(native.runs) == 2
    assert resumed.coverage_reference["coverage_campaign"]["schema"] == (
        "booley.coverage-campaign/v5"
    )


def test_coverage_campaign_rejects_score_bearing_invalid_nested_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _retain_score_bearing_invalid_campaign(monkeypatch)

    with pytest.raises(
        CampaignPublicationError, match="nested Coverage Campaign cannot be authenticated"
    ) as caught:
        _run_coverage_campaign(tmp_path, NativeExecution())

    assert isinstance(caught.value.__cause__, SimulationCampaignIntegrityError)


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
    outcome, store = _run_coverage_campaign(tmp_path, _TimeoutThenMissingInputExecution())

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


def test_post_commit_failure_reports_incomplete_collection(tmp_path: Path) -> None:
    """A committed `incomplete` collection survives a later publication failure."""

    class MissingInput(NativeExecution):
        def run(self, request):
            return SimulationRunResult(
                "fail",
                "$readmemh: Cannot open memory.hex",
                termination="fatal_init",
                failure_kind="missing_input",
            )

        def command(self, request):
            raise AssertionError("merge must not run after a design abort")

    def fail_after_commit(boundary: str) -> None:
        if boundary == "after:coverage_campaign":
            raise OSError("injected post-commit publication failure")

    with pytest.raises(_CoverageAggregateError) as raised:
        _run_coverage_campaign(tmp_path, MissingInput(), publication_checkpoint=fail_after_commit)

    detail = raised.value.detail
    assert detail["collection"] == "incomplete"
    assert detail["evaluation"] == "not_requested"
    assert detail["abort_remaining"] is True
    assert detail["passed"] is False
    assert detail["simulation"] == "aborted"
    assert "coverage_campaign" not in detail


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


def test_coverage_infrastructure_build_failure_has_terminal_error_result(
    tmp_path: Path,
) -> None:
    outcome, _store = _run_coverage_campaign(tmp_path, _FailedCoverageBuild(infrastructure=True))
    assert outcome.complete
    assert outcome.aggregate_grade == "error"

    store = CampaignStore(tmp_path / "reports/1/targets/sim_0/campaign")
    recovery = store.scan()
    assert not recovery.interrupted
    directory = store.work_item_directory(recovery.items[0].work_item_id)
    attempts = tuple((directory / "attempts").iterdir())
    assert len(attempts) == 1
    build = json.loads((attempts[0] / "private-build/build-result.json").read_bytes())
    assert build["state"] == "infrastructure_error"
    assert (directory / "result.json").exists()
    result = recovery.items[0].result
    assert result is not None
    assert result.document["state"] == "aborted"
    assert result.document["grade"] == "error"


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

    def compile(self):
        return SimpleNamespace(passed=True)

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()

    def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
        del snapshot_root, run_cwd
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


def _start_crashed_cocotb_campaign(
    plan, root: Path, executor: OrdinaryHdlSerialExecutor
) -> tuple[Path, CampaignStore]:
    invocation = root / "reports" / "1"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _admission()
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
            _admission(),
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
) -> CampaignStore:
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
    request = NewCampaignRunRequest(
        plan, root, invocation.parent, CampaignPolicy(), invocation, _admission()
    )
    SimulationCampaign(executor).run(request)
    return CampaignStore(invocation / "targets/sim/campaign")


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


def test_cocotb_campaign_rejects_malformed_results_file(tmp_path: Path, monkeypatch) -> None:
    """A results file the decoder rejects is a campaign integrity error, not a verdict.

    The file is the payload itself: the campaign must decode it directly instead of
    wrapping it in a stdout line for the line parser.
    """
    reported = (SimulationTestOutcome(name="count", verdict="pass", passed=True),)

    def forbidden_parse(output: str) -> None:
        raise AssertionError("campaign decoded the file through the stdout-line parser")

    # Patch the defining module and the campaign's own namespace: either call style fails.
    monkeypatch.setattr(cocotb_results, "parse_results_line", forbidden_parse)
    monkeypatch.setattr(serial_execution, "parse_results_line", forbidden_parse, raising=False)
    for name, raw, message in (
        ("not-utf8", b"\xff", "Cocotb transport is not UTF-8"),
        ("not-json", b"{broken", "Cocotb transport is invalid"),
        ("not-a-results-object", b"[]", "Cocotb transport is invalid"),
    ):
        root = tmp_path / name
        root.mkdir()
        with pytest.raises(SimulationCampaignIntegrityError, match=message):
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
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()  # a literal run_cwd must exist before any build (#881)
    handle = _handle(project)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )
    counters = {"compile": 0, "launch": 0}
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    outcome = SimulationCampaign(_failed_shared_executor(tmp_path / "engine-build", counters)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )

    store = CampaignStore(invocation / "targets/sim/campaign")
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


def _aggregate_result_bound_fixture(tmp_path):
    from booley.flows.sim.campaign.codec import RESULT_SCHEMA
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.contract import PreSimEvidence
    from tests.flows.sim.test_campaign_codec_golden import _simulation_result

    names = tuple(f"test{index:04d}" for index in range(4000))
    plan, _ = _maximum_hook_selection_plan(names)
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    for ordinal, name in enumerate(names, 1):
        publish_pre_sim_firing(
            request,
            PreSimEvidence(("true",), (name,), "passed", 0.0, returncode=0),
            ordinal=ordinal,
        )
    document = json.loads(_simulation_result("completed"))
    document["$schema"] = RESULT_SCHEMA
    document.update(serial_execution._common(request))
    document["attempt_id"] = request.attempt_id
    document["executable_snapshot"]["manifest"]["owner"] = request.attempt_id
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


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(90)
def test_maximum_aggregate_result_overflow_keeps_all_authenticated_sidecars(tmp_path):
    from booley.flows.sim.campaign.codec import RECORD_MAX_BYTES, decode_simulation_result

    store, request, document = _aggregate_result_bound_fixture(tmp_path)
    raw = canonical_json_bytes(document)
    assert len(document["evidence"]) == len(document["observations"]) == 4000
    assert len(raw) > RECORD_MAX_BYTES
    with pytest.raises(SimulationCampaignIntegrityError, match="byte size ceiling"):
        store.publish_result(request.work_item["work_item_id"], decode_simulation_result(raw))
    assert not (request.attempt_directory.parent.parent / "result.json").exists()
    assert len(collect_pre_sim_firings(store.manifest_path)) == 4000
    without_hooks = {**document, "evidence": []}
    assert len(canonical_json_bytes(without_hooks)) < RECORD_MAX_BYTES
    assert (
        len(decode_simulation_result(canonical_json_bytes(without_hooks)).document["observations"])
        == 4000
    )

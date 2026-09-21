"""Hostile contracts for Cocotb-batch and coverage-aggregate campaign plans."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
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
from booley.flows.sim.campaign.flow_planning import plan_coarse_simulation_campaign
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
    SimulationPreview,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection
from tests.flows.sim.test_campaign_crash_matrix import (
    _build_execution,
    _CrashOnce,
    _InjectedProcessDeath,
)


def _facts(
    root: Path, *, cocotb: bool, preview_names: tuple[str, ...] = ("count", "reset")
) -> tuple[TargetHandle, TargetInspection, SimulationPreview]:
    project = root / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text("[flows.sim]\nrun_cwd = '.'\n", encoding="utf-8")
    (project / "tests.toml").write_text(
        '[sim]\ntests = ["reset", "count"]\n', encoding="utf-8"
    )
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
    document = _plan(
        tmp_path, kind="cocotb_batch", names=(), cocotb=True
    ).manifest.document

    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == "cocotb_batch"
    assert item["selection"] == {"kind": "unfiltered", "names": ()}
    assert item["arguments"] == ()


def test_coverage_selection_is_one_named_aggregate_with_coverage_variant(
    tmp_path: Path,
) -> None:
    document = _plan(
        tmp_path, kind="coverage_aggregate", cocotb=False
    ).manifest.document

    assert document["workload"]["coverage"] is True  # type: ignore[index]
    assert len(document["work_items"]) == 1  # type: ignore[arg-type]
    item = document["work_items"][0]  # type: ignore[index]
    assert item["kind"] == "coverage_aggregate"
    assert item["selection"] == {"kind": "named", "names": ("count", "reset")}


@pytest.mark.parametrize(
    ("kind", "cocotb", "names"),
    [
        ("ordinary_hdl", False, ("reset",)),
        ("cocotb_batch", False, ("reset",)),
        ("coverage_aggregate", True, ("reset",)),
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


def _unmanaged() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


@dataclass
class _CocotbHarness:
    handle: TargetHandle
    build_root: Path
    launches: list[tuple[str, ...]]

    @contextmanager
    def ordinary_group(self, _handle, names):
        yield _CocotbGroup(self, names)


class _CocotbGroup:
    def __init__(self, harness: _CocotbHarness, names: tuple[str, ...]) -> None:
        self._harness = harness
        self.names = names
        self.build_root = harness.build_root
        self.artifact_paths = (self.build_root / "simv",)

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
        path.write_text(json.dumps({"tests": list(self.names)}), encoding="utf-8")
        tests = tuple(
            SimulationTestOutcome(name=name, verdict="pass", passed=True)
            for name in self.names
        )
        return SimulationTargetOutcome(
            target="sim", target_identity=self._harness.handle.identity,
            toplevel="tb", eda_tool="icarus", passed=True, verdict="pass",
            elapsed_s=0.1, tests=tests,
            artifacts=(SimulationArtifactEvidence(
                "cocotb_results_json", str(path), path.stat().st_size, self.names
            ),),
        )


def _assert_result_selection_is_authenticated(
    store: CampaignStore, work_item_id: str
) -> None:
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
    node = ValidatedManifestNode(store.manifest_path, plan.manifest, manifest_digest(plan.manifest))
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

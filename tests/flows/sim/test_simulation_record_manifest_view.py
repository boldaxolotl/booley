"""One typed, read-only view of a decoded Simulation Record manifest (#4207)."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

import booley.flows.sim as sim_package
from booley.flows.sim import record as sim_record
from booley.flows.sim.record import (
    SimulationRecordIntegrityError,
    SimulationRecordManifest,
    SimulationRecordRecoveryStatus,
    decode_simulation_record_manifest,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.codec import canonical_json_bytes
from booley.flows.sim.record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from booley.flows.test_runs import build_test_run
from tests.flows.sim.test_simulation_record_flow_planning import _planning_fixture
from tests.flows.sim.test_simulation_record_test_runs import _cocotb_plan, _hdl_plan

_RESET = build_test_run("reset", seed=1)
_SOAK = build_test_run("count", variant="soak", seed=42, args=("+iters=9",))


def _decoded(manifest: SimulationRecordManifest) -> SimulationRecordManifest:
    return decode_simulation_record_manifest(encode_simulation_record_manifest(manifest))


def _coverage_plan(tmp_path: Path, *, no_waivers: bool, trace: bool = False):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    runs = (_RESET, _SOAK)
    preview = replace(
        preview,
        groups=(("reset", "count"),),
        run_groups=(runs,),
        commands=(("run",),),
    )
    return plan_coarse_simulation_record(
        handle=handle,
        inspection=inspection,
        preview=preview,
        selected_tests=("reset", "count"),
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=trace,
        kind="coverage_aggregate",
        no_waivers=no_waivers,
    )


def _traced_hdl_plan(tmp_path: Path):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    preview = replace(
        preview,
        groups=(("reset",), ("count",)),
        run_groups=((_RESET,), (_SOAK,)),
    )
    return plan_ordinary_hdl_simulation_record(
        handle=handle,
        inspection=inspection,
        preview=preview,
        groups=preview.groups,
        required_suite=("reset", "count"),
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=True,
    )


def _status(
    *, pending: tuple[str, ...] = (), interrupted: tuple[str, ...] = ()
) -> SimulationRecordRecoveryStatus:
    return SimulationRecordRecoveryStatus(
        Path("manifest.json"), "0" * 64, (), interrupted, pending
    )


def test_view_reads_an_ordinary_hdl_plan_unchanged(tmp_path: Path) -> None:
    plan = _traced_hdl_plan(tmp_path)
    view = sim_record.SimulationRecordManifestView(_decoded(plan.manifest))

    assert view.work_item_kind == "ordinary_hdl"
    assert view.is_coverage is False
    assert view.test_run_groups == ((_RESET,), (_SOAK,))
    assert view.selected_name_groups == (("reset",), ("count",))
    assert view.work_item_ids == plan.work_item_ids
    assert view.trace is True
    assert view.no_waivers is False
    assert view.target == sim_record.ManifestTarget(
        "sim", "acme:lib:dut:1", "sim", "candidate", "abc123"
    )
    assert view.target.identity == "acme:lib:dut:1#sim"
    assert view.required_suite_names == ("reset", "count")
    assert view.eda_tool_kind == "icarus"
    assert view.origin_invocation_id == 1
    assert view.prerequisites == ()
    assert view.simulation_record_id == plan.manifest.document["simulation_record_id"]


def test_view_reads_a_cocotb_batch_plan_unchanged(tmp_path: Path) -> None:
    runs = ((_RESET,), (build_test_run("count", variant="fault", seed=7),))
    plan = _cocotb_plan(tmp_path, runs)
    view = sim_record.SimulationRecordManifestView(_decoded(plan.manifest))

    assert view.work_item_kind == "cocotb_batch"
    assert view.is_coverage is False
    assert view.test_run_groups == runs
    assert view.selected_name_groups == (("reset",), ("count",))
    assert view.work_item_ids == plan.work_item_ids
    assert view.trace is False
    assert view.no_waivers is False


def test_view_reads_a_coverage_aggregate_plan_unchanged(tmp_path: Path) -> None:
    plan = _coverage_plan(tmp_path, no_waivers=True, trace=True)
    view = sim_record.SimulationRecordManifestView(_decoded(plan.manifest))

    assert view.work_item_kind == "coverage_aggregate"
    assert view.is_coverage is True
    assert view.test_run_groups == ((_RESET, _SOAK),)
    assert view.selected_name_groups == (("reset", "count"),)
    assert view.work_item_ids == plan.work_item_ids
    assert view.trace is True
    assert view.no_waivers is True


def test_a_manifest_without_no_waivers_reads_as_waivers_apply(tmp_path: Path) -> None:
    manifest = _decoded(_coverage_plan(tmp_path, no_waivers=False).manifest)

    assert "no_waivers" not in manifest.document["workload"]  # type: ignore[operator]
    assert sim_record.SimulationRecordManifestView(manifest).no_waivers is False


def test_a_field_the_codec_accepts_malformed_is_an_integrity_error(tmp_path: Path) -> None:
    document = json.loads(_coverage_plan(tmp_path, no_waivers=True).manifest.canonical_bytes())
    document["workload"]["no_waivers"] = "yes"
    document["simulation_record_id"] = None
    view = sim_record.SimulationRecordManifestView(
        decode_simulation_record_manifest(canonical_json_bytes(document))
    )

    for field in ("no_waivers", "simulation_record_id"):
        with pytest.raises(SimulationRecordIntegrityError):
            getattr(view, field)


def test_unfinished_work_lists_pending_and_interrupted_coverage_items(tmp_path: Path) -> None:
    plan = _coverage_plan(tmp_path, no_waivers=False)
    view = sim_record.SimulationRecordManifestView(_decoded(plan.manifest))
    (item_id,) = plan.work_item_ids
    ids = (item_id, "not-in-the-manifest")

    assert view.unfinished_work_item_ids(_status(pending=ids)) == (item_id,)
    assert view.unfinished_work_item_ids(_status(interrupted=ids)) == (item_id,)
    assert view.unfinished_work_item_ids(_status()) == ()


def test_unfinished_work_is_empty_for_other_kinds(tmp_path: Path) -> None:
    (tmp_path / "hdl").mkdir()
    (tmp_path / "cocotb").mkdir()
    plans = (
        _hdl_plan(tmp_path / "hdl", ((_RESET,), (_SOAK,))),
        _cocotb_plan(tmp_path / "cocotb", ((_RESET,), (_SOAK,))),
    )
    for plan in plans:
        view = sim_record.SimulationRecordManifestView(_decoded(plan.manifest))
        observed = _status(pending=plan.work_item_ids, interrupted=plan.work_item_ids)
        assert view.unfinished_work_item_ids(observed) == ()


def _direct_manifest_reads(path: Path) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Attribute) and node.attr == "document":
            receiver = node.value
            name = receiver.id if isinstance(receiver, ast.Name) else None
            if isinstance(receiver, ast.Attribute):
                name = receiver.attr
            if name is not None and name.endswith("manifest"):
                hits.append(node.lineno)
        if isinstance(node, ast.Compare) and any(
            isinstance(part, ast.Constant) and part.value == "coverage_aggregate"
            for part in ast.walk(node)
        ):
            hits.append(node.lineno)
    return hits


def test_the_simulation_flow_reads_manifests_only_through_the_view() -> None:
    root = Path(next(iter(sim_package.__path__)))
    record = root / "record"
    found = [
        f"{path.relative_to(root).as_posix()}:{line}"
        for path in sorted(root.rglob("*.py"))
        if record not in path.parents
        for line in _direct_manifest_reads(path)
    ]

    assert found == []

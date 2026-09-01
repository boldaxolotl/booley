from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from booley.flows.base import SubprocessResult
from booley.flows.fpga.backends.vivado import cache as fpga_cache
from booley.flows.fpga.flow import FpgaImplFlow, _PreparedFpgaCommand
from booley.fusesoc.fusesoc_registry import ResolvedFile, ResolvedTarget


def _artifacts(root: Path, *, bitstream: bool = True) -> None:
    impl = root / "demo.runs" / "impl_1"
    impl.mkdir(parents=True)
    for name, text in (
        ("top_utilization_placed.rpt", "util"),
        ("top_timing_summary_routed.rpt", "timing"),
        ("top_drc_routed.rpt", "drc"),
        ("runme.log", "route_design completed successfully"),
    ):
        (impl / name).write_text(text, encoding="utf-8")
    if bitstream:
        (impl / "top.bit").write_bytes(b"bitstream")


def _parsed_pass() -> dict:
    return {
        "status": "pass",
        "lut_count": 10,
        "ff_count": 5,
        "wns_ns": 0.2,
        "whs_ns": 0.1,
    }


def _run_evidence(run_id: str = "producer-1") -> dict:
    return {
        "version": 1,
        "run_id": run_id,
        "source_revision": "a" * 40,
        "source_sha256": "b" * 64,
        "recipe_sha256": "c" * 64,
    }


def _flow(root: Path) -> FpgaImplFlow:
    flow = FpgaImplFlow()
    flow.parse_args(["--target", "fpga_demo", "--work-dir", str(root), "--timeout", "1000"])
    flow._project_root = root
    return flow


def _resolved(root: Path) -> ResolvedTarget:
    (root / "rtl.sv").write_text("module top; endmodule\n", encoding="utf-8")
    (root / "timing.xdc").write_text("create_clock -period 10 [get_ports clk]\n")
    return ResolvedTarget(
        name="fpga_demo",
        vlnv="::demo:0",
        toplevel="top",
        eda_tool="vivado",
        files=(
            ResolvedFile(name="rtl.sv", file_type="systemVerilogSource"),
            ResolvedFile(name="timing.xdc", file_type="xdc"),
        ),
        parameters={"WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 8}},
        build_root=root,
        edam_path=root / "demo.eda.yml",
        flow_options={"tool": "vivado", "part": "xc7a35tcpg236-1"},
    )


def test_input_fingerprint_changes_with_source_and_resolved_parameters(tmp_path: Path) -> None:
    resolved = _resolved(tmp_path)
    edam = {"name": "fpga_demo", "toplevel": "top", "tool_options": {"part": "p"}}
    first = fpga_cache.input_fingerprint(resolved, edam, out_of_context=False)

    (tmp_path / "rtl.sv").write_text("module top; wire changed; endmodule\n", encoding="utf-8")
    source_changed = fpga_cache.input_fingerprint(resolved, edam, out_of_context=False)
    assert source_changed != first

    parameters = {"WIDTH": {"datatype": "int", "paramtype": "vlogparam", "default": 16}}
    parameter_changed = fpga_cache.input_fingerprint(
        replace(resolved, parameters=parameters), edam, out_of_context=False
    )
    assert parameter_changed != source_changed


def test_cache_hit_requires_exact_artifact_bytes(tmp_path: Path) -> None:
    _artifacts(tmp_path)
    assert fpga_cache.store(
        tmp_path,
        "a" * 64,
        require_bitstream=True,
        producer_evidence=_run_evidence(),
    )
    assert fpga_cache.load(tmp_path, "a" * 64, require_bitstream=True) is not None
    assert fpga_cache.load(tmp_path, "b" * 64, require_bitstream=True) is None

    report = tmp_path / "demo.runs" / "impl_1" / "top_drc_routed.rpt"
    report.write_text("changed", encoding="utf-8")
    assert fpga_cache.load(tmp_path, "a" * 64, require_bitstream=True) is None


def test_cache_rejects_non_mapping_metadata_and_boolean_producer_version(tmp_path: Path) -> None:
    (tmp_path / fpga_cache.CACHE_FILE).write_text("[]", encoding="utf-8")
    assert fpga_cache._read_metadata(tmp_path) is None
    assert not fpga_cache._valid_producer_evidence({**_run_evidence(), "version": True})


def test_cache_store_requires_bitstream_when_target_is_not_ooc(tmp_path: Path) -> None:
    _artifacts(tmp_path, bitstream=False)
    assert not fpga_cache.store(
        tmp_path,
        "a" * 64,
        require_bitstream=True,
        producer_evidence=_run_evidence(),
    )
    assert fpga_cache.store(
        tmp_path,
        "a" * 64,
        require_bitstream=False,
        producer_evidence=_run_evidence(),
    )


def test_store_rejects_artifacts_predating_dispatch(tmp_path: Path) -> None:
    _artifacts(tmp_path)
    old = time.time() - 3600
    for path in tmp_path.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    assert not fpga_cache.store(
        tmp_path,
        "a" * 64,
        require_bitstream=True,
        producer_evidence=_run_evidence(),
        min_mtime=time.time(),
    )


def test_run_single_target_skips_executor_on_valid_cache_hit(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    prepared = _PreparedFpgaCommand(
        run_cmd=["make", "-C", "build"],
        work_root=tmp_path,
        fingerprint="a" * 64,
        require_bitstream=True,
        recipe_snapshot={"flow": "fpga", "target": "fpga_demo"},
        recipe_fingerprint="recipe-a",
        run_evidence=_run_evidence("consumer-2"),
    )
    with (
        patch.object(flow, "_prepare_fpga_command", return_value=prepared),
        patch.object(
            fpga_cache,
            "load",
            return_value=fpga_cache.CacheHit("a" * 64, "reports", _run_evidence()),
        ),
        patch("booley.flows.fpga.backends.vivado.edam.parse_fpga_reports", return_value=_parsed_pass()),
        patch.object(flow, "_execute_boundary") as execute,
    ):
        metrics = flow._run_single_target("fpga_demo")
    execute.assert_not_called()
    assert metrics.passed
    assert metrics.cached
    assert metrics.cache_fingerprint == "a" * 64
    assert metrics.recipe_fingerprint == "recipe-a"
    assert metrics.run_evidence["run_id"] == "producer-1"
    assert metrics.cache_consumer_run_id == "consumer-2"


def test_cache_miss_forces_make_recipe(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    prepared = _PreparedFpgaCommand(
        run_cmd=["make", "-C", "build"],
        work_root=tmp_path,
        fingerprint="a" * 64,
        require_bitstream=False,
    )
    executed: list[str] = []

    def execute(command, *, timeout):
        del timeout
        executed.extend(command)
        return SubprocessResult(returncode=0, stdout="routed", dispatched_unix=time.time())

    with (
        patch.object(flow, "_prepare_fpga_command", return_value=prepared),
        patch.object(fpga_cache, "load", return_value=None),
        patch.object(fpga_cache, "store", return_value=True),
        patch.object(flow, "_execute_boundary", side_effect=execute),
        patch.object(flow, "_collect_route_reports", return_value="reports"),
        patch("booley.flows.fpga.backends.vivado.edam.parse_fpga_reports", return_value=_parsed_pass()),
    ):
        metrics = flow._run_single_target("fpga_demo")
    assert executed[-1] == "-B"
    assert metrics.passed
    assert not metrics.cached


def test_no_cache_forces_executor_despite_valid_hit(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.args.no_cache = True
    prepared = _PreparedFpgaCommand(["make", "-C", "build"], tmp_path, "a" * 64, False)
    with (
        patch.object(flow, "_prepare_fpga_command", return_value=prepared),
        patch.object(fpga_cache, "load") as load,
        patch.object(fpga_cache, "store", return_value=True),
        patch.object(
            flow,
            "_execute_boundary",
            return_value=SubprocessResult(
                returncode=0, stdout="routed", dispatched_unix=time.time()
            ),
        ) as execute,
        patch.object(flow, "_collect_route_reports", return_value="reports"),
        patch("booley.flows.fpga.backends.vivado.edam.parse_fpga_reports", return_value=_parsed_pass()),
    ):
        metrics = flow._run_single_target("fpga_demo")
    load.assert_not_called()
    execute.assert_called_once()
    assert metrics.passed

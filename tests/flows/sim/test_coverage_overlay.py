"""Behavioral tests for Simulation-owned FuseSoC coverage overlays."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.coverage_overlay import coverage_overlay_vlnv, write_coverage_overlay
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.fusesoc.fusesoc_registry import FuseSocError, discover_cores, read_core
from booley.targets.catalog import TargetCatalog
from tests.fusesoc_test_support import CORE_TEXT as _CORE_TEXT
from tests.fusesoc_test_support import write_core as _write_core


class TestCoverageOverlay:
    _INSTRUMENTATION = (
        "--coverage-line",
        "--coverage-toggle",
        "--coverage-expr",
        "--coverage-user",
        "--coverage-per-instance",
    )

    def test_uses_distinct_vlnvs_for_coverage_build_variants(self):
        assert coverage_overlay_vlnv("::design:0", trace=False) == ("::design-booleycoverage:0")
        assert coverage_overlay_vlnv("::design:0", trace=True) == (
            "::design-booleytracecoverage:0"
        )

    @pytest.mark.parametrize("trace", [False, True])
    def test_writes_exact_instrumentation_without_touching_target(
        self, tmp_path: Path, trace: bool
    ):
        core = _CORE_TEXT.replace(
            "      tool: verilator\n",
            "      tool: verilator\n"
            "      verilator_options: [--timing, --coverage, --coverage-line]\n",
        )
        base = _write_core(tmp_path / "ip", core)
        overlay = write_coverage_overlay(
            TargetCatalog.build(tmp_path).select("sim"),
            instrumentation=self._INSTRUMENTATION,
            trace=trace,
        )
        try:
            options = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "verilator_options"
            ]
            assert options[-5:] == list(self._INSTRUMENTATION)
            assert options.count("--coverage-line") == 1
            assert "--coverage" not in options
            assert ("--trace" in options) is trace
            assert "--timing" in options
            assert TRACE_OVERLAY_MARKER in overlay.core_file.name
            assert overlay.core_file not in discover_cores(tmp_path)
            assert read_core(base)["targets"]["sim"]["flow_options"]["verilator_options"] == [
                "--timing",
                "--coverage",
                "--coverage-line",
            ]
        finally:
            overlay.cleanup()

    def test_rejects_non_verilator_sim_target(self, tmp_path: Path):
        core = _CORE_TEXT.replace("tool: verilator", "tool: icarus").replace(
            "default_tool: verilator", "default_tool: icarus"
        )
        _write_core(tmp_path / "ip", core)
        with pytest.raises(FuseSocError, match="only Verilator"):
            write_coverage_overlay(
                TargetCatalog.build(tmp_path).select("sim"),
                instrumentation=self._INSTRUMENTATION,
                trace=False,
            )

    def test_injects_packaged_bridge_for_declared_custom_main_hooks(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "      tool: verilator\n",
            "      tool: verilator\n"
            "      booley:\n"
            "        coverage:\n"
            "          reset_included: false\n"
            "          custom_main_hooks: [start_hook, write_hook]\n",
        )
        _write_core(tmp_path / "ip", core)
        overlay = write_coverage_overlay(
            TargetCatalog.build(tmp_path).select("sim"),
            instrumentation=self._INSTRUMENTATION,
            trace=False,
        )
        try:
            document = read_core(overlay.core_file)
            fileset = document["filesets"]["booley_verilator_coverage_bridge"]
            declared = [next(iter(item)) for item in fileset["files"]]
            assert [Path(path).name for path in declared] == [
                "booley_coverage.h",
                "booley_coverage.cpp",
            ]
            assert "booley_verilator_coverage_bridge" in document["targets"]["sim"]["filesets"]
        finally:
            overlay.cleanup()

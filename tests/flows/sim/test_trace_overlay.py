"""Behavioral tests for Simulation-owned FuseSoC trace overlays."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from booley.flows.sim.trace_overlay import (
    _target_includes_dump_module,
    trace_overlay_vlnv,
    write_trace_overlay,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.fusesoc.fusesoc_registry import (
    FuseSocError,
    _enumerate_all,
    _resolve_target,
    discover_cores,
    read_core,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import UnknownTargetError
from tests.fusesoc_test_support import CORE_TEXT as _CORE_TEXT
from tests.fusesoc_test_support import write_core as _write_core


def enumerate_targets(project_root: Path | str):
    """Return adapter-discovered Targets for overlay-discovery assertions."""
    return {name: refs[0] for name, refs in _enumerate_all(project_root).items()}


# ---------------------------------------------------------------------------
# write_trace_overlay / trace_overlay_vlnv  (the --trace overlay slice)
# ---------------------------------------------------------------------------


class TestTraceOverlayVlnv:
    @pytest.mark.parametrize(
        "base, expected",
        [
            ("::design:0", "::design-booleytrace:0"),
            ("vend:lib:core:1.2", "vend:lib:core-booleytrace:1.2"),
            ("solo", "solo-booleytrace"),
        ],
    )
    def test_suffixes_name_component(self, base: str, expected: str):
        assert trace_overlay_vlnv(base) == expected


class TestWriteTraceOverlay:
    def test_writes_colocated_overlay_with_trace_options(self, tmp_path: Path):
        base = _write_core(tmp_path / "ip")  # sim target: verilator, flow sim
        base_vlnv = read_core(base)["name"]
        expected_vlnv = trace_overlay_vlnv(base_vlnv)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            # Co-located with the base .core so relative fileset paths still resolve.
            assert overlay.core_file.parent == base.parent
            assert TRACE_OVERLAY_MARKER in overlay.core_file.name
            assert overlay.vlnv == expected_vlnv
            assert overlay.vlnv != base_vlnv  # distinct → its own build root

            doc = read_core(overlay.core_file)
            assert doc["name"] == expected_vlnv
            opts = doc["targets"]["sim"]["flow_options"]["verilator_options"]
            assert "--trace" in opts
            assert opts[opts.index("--trace-depth") + 1] == "99"
            # The base Target is untouched (agent-immutable).
            assert "--trace" not in read_core(base)["targets"]["sim"].get("flow_options", {}).get(
                "verilator_options", []
            )
        finally:
            overlay.cleanup()

    def test_overlay_is_skipped_by_discovery(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            # The overlay .core exists on disk beside the base...
            assert overlay.core_file.exists()
            # ...but Booley's enumeration ignores it, so it never pollutes the
            # selectable Target list (no `sim` collision, no overlay Target).
            found = discover_cores(tmp_path)
            assert overlay.core_file not in found
            assert set(enumerate_targets(tmp_path)) == {"sim"}
        finally:
            overlay.cleanup()

    def test_preserves_authored_vcd_recipe(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            "    flow_options:\n      tool: verilator\n"
            "      verilator_options: [--timing, --trace, --trace-depth, '5']\n",
        )
        assert "verilator_options" in core  # guard: the replace actually matched
        _write_core(tmp_path / "ip", core)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "verilator_options"
            ]
            # An authored recipe is one contract: Booley must not rewrite its
            # format or depth while leaving the project's harness untouched.
            assert opts.count("--trace") == 1
            assert opts.count("--trace-depth") == 1
            assert opts[opts.index("--trace-depth") + 1] == "5"
            assert "--timing" in opts  # non-trace options preserved
            assert overlay.mode is TraceMode.VCD_FIFO
        finally:
            overlay.cleanup()

    def test_preserves_authored_native_fst_recipe(self, tmp_path: Path):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            "    flow_options:\n      tool: verilator\n"
            "      verilator_options: [--timing, --trace, --trace-fst, "
            "--trace-depth, '7', -CFLAGS, -DVM_TRACE_FMT_FST]\n",
        )
        _write_core(tmp_path / "ip", core)

        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "verilator_options"
            ]
            assert opts == [
                "--timing",
                "--trace",
                "--trace-fst",
                "--trace-depth",
                "7",
                "-CFLAGS",
                "-DVM_TRACE_FMT_FST",
            ]
            assert overlay.mode is TraceMode.NATIVE_FST
        finally:
            overlay.cleanup()

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ("--trace-fst, --trace-vcd", "both native FST and VCD"),
            ("--trace-saif", "SAIF tracing is not supported"),
        ],
    )
    def test_rejects_unsupported_authored_trace_recipe(
        self,
        tmp_path: Path,
        options: str,
        message: str,
    ):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            f"    flow_options:\n      tool: verilator\n      verilator_options: [{options}]\n",
        )
        _write_core(tmp_path / "ip", core)

        with pytest.raises(FuseSocError, match=message):
            write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            (
                "-CFLAGS, -DVM_TRACE_FMT_FST",
                "VM_TRACE_FMT_FST.*requires --trace-fst",
            ),
            (
                "--trace-vcd, -CFLAGS, -DVM_TRACE_FMT_FST",
                "FST CFLAG.*VCD trace option",
            ),
            (
                "--trace-fst, -CFLAGS, -DVM_TRACE_FMT_VCD",
                "VCD CFLAG.*native FST trace option",
            ),
            (
                "-CFLAGS, '-DVM_TRACE_FMT_FST -DVM_TRACE_FMT_VCD'",
                "both FST and VCD CFLAGS",
            ),
        ],
    )
    def test_rejects_incoherent_trace_format_cflags(
        self,
        tmp_path: Path,
        options: str,
        message: str,
    ):
        core = _CORE_TEXT.replace(
            "    flow_options:\n      tool: verilator\n",
            f"    flow_options:\n      tool: verilator\n      verilator_options: [{options}]\n",
        )
        _write_core(tmp_path / "ip", core)

        with pytest.raises(FuseSocError, match=message):
            write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))

    def test_cleanup_is_idempotent(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        overlay.cleanup()
        assert not overlay.core_file.exists()
        overlay.cleanup()  # second call must not raise

    def test_rejects_unknown_target(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        with pytest.raises(UnknownTargetError):
            write_trace_overlay(TargetCatalog.build(tmp_path).select("nope"))

    def test_rejects_non_verilator_sim_target(self, tmp_path: Path):
        core = textwrap.dedent(
            """\
            CAPI=2:
            name: ::lint_demo:0
            filesets:
              rtl:
                files:
                  - rtl/dut.sv: {file_type: systemVerilogSource}
            targets:
              lint:
                flow: lint
                flow_options:
                  tool: verilator
                filesets: [rtl]
                toplevel: dut
            """
        )
        _write_core(tmp_path / "ip", core)
        # lint flow has no testbench — the trace overlay is sim-only.
        with pytest.raises(FuseSocError):
            write_trace_overlay(TargetCatalog.build(tmp_path).select("lint"))

    # --- Icarus sim trace overlay (roots the dump module, no verilator_options) -

    _ICARUS_CORE = textwrap.dedent(
        """\
        CAPI=2:
        name: ::icarus_demo:0
        filesets:
          rtl:
            files:
              - rtl/dut.sv: {file_type: systemVerilogSource}
          tb:
            files:
              - tb/tb_dut.sv: {file_type: systemVerilogSource}
              - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}
            tags: [tb]
        targets:
          default:
            filesets: [rtl]
          sim:
            default_tool: icarus
            flow: sim
            flow_options:
              tool: icarus
            filesets: [rtl, tb]
            toplevel: tb_dut
        """
    )

    def test_icarus_overlay_roots_dump_module(self, tmp_path: Path):
        _write_core(tmp_path / "ip", self._ICARUS_CORE)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            sim = read_core(overlay.core_file)["targets"]["sim"]["flow_options"]
            # Icarus gets an explicit dump-module root (edalize's -s <top> prunes
            # the uninstantiated booley_vcd_dump otherwise) and NO verilator_options.
            assert "-sbooley_vcd_dump" in sim["iverilog_options"]
            assert "verilator_options" not in sim
        finally:
            overlay.cleanup()

    def test_icarus_overlay_root_is_idempotent(self, tmp_path: Path):
        core = self._ICARUS_CORE.replace(
            "    flow_options:\n      tool: icarus\n",
            "    flow_options:\n      tool: icarus\n"
            "      iverilog_options: [-sbooley_vcd_dump, -g2012]\n",
        )
        assert "iverilog_options" in core  # guard: the replace matched
        _write_core(tmp_path / "ip", core)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            opts = read_core(overlay.core_file)["targets"]["sim"]["flow_options"][
                "iverilog_options"
            ]
            assert opts.count("-sbooley_vcd_dump") == 1  # no double-up
            assert "-g2012" in opts  # non-trace options preserved
        finally:
            overlay.cleanup()

    def test_icarus_overlay_injects_dump_module_when_absent(self, tmp_path: Path):
        # Stealth Mode: an Icarus sim Target whose fileset omits booley_vcd_dump
        # is NOT rejected — the overlay supplies the module from Booley's refs/
        # so the design repo needs no tracked trace source. It is still rooted.
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n",
            "",
        )
        assert "booley_vcd_dump" not in core  # guard: the replace matched
        _write_core(tmp_path / "ip", core, create_sources=False)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            doc = read_core(overlay.core_file)
            sim = doc["targets"]["sim"]
            # The supplied module rides in via an overlay-only fileset the target
            # pulls in, and is rooted just like an authored one.
            assert "booley_trace_dump" in sim["filesets"]
            assert "-sbooley_vcd_dump" in sim["flow_options"]["iverilog_options"]
            # It is physically supplied (ephemeral, marker-named) and tracked for
            # cleanup — nothing lands in the design's own tree by name.
            assert len(overlay.extra_files) == 1
            supplied = overlay.extra_files[0]
            assert supplied.is_file()
            assert TRACE_OVERLAY_MARKER in supplied.name
        finally:
            overlay.cleanup()
        assert not overlay.extra_files[0].exists()  # swept up with the .core

    def test_native_core_isolation_keeps_icarus_dump_source_after_cleanup(
        self,
        tmp_path: Path,
    ):
        pytest.importorskip("fusesoc")
        project = tmp_path / "project"
        cores = project / ".booley_project" / "cores"
        cores.mkdir(parents=True)
        (project / ".booley_project" / "booley.toml").write_text(
            "[stealth]\nenabled = true\nignore_native_cores = true\n",
            encoding="utf-8",
        )
        (project / ".booley_project" / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        (project / "rtl").mkdir()
        (project / "tb").mkdir()
        (project / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (project / "tb" / "tb_dut.sv").write_text(
            "module tb_dut; dut dut(); endmodule\n",
            encoding="utf-8",
        )
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n",
            "",
        )
        _write_core(cores, core, create_sources=False)
        overlay = write_trace_overlay(TargetCatalog.build(project).select("sim"))
        try:
            resolved = _resolve_target(
                "sim",
                project_root=project,
                build_root=project / "build",
                vlnv=overlay.vlnv,
            )
        finally:
            overlay.cleanup()

        dumps = [
            resolved.build_root / file.name
            for file in resolved.files
            if Path(file.name).name == "booley_vcd_dump.sv"
        ]
        assert len(dumps) == 1
        assert dumps[0].is_file()
        assert not (cores / f"booley_vcd_dump{TRACE_OVERLAY_MARKER}.sv").exists()

    def test_projected_icarus_overlay_rebases_injected_dump(self, tmp_path: Path):
        project_dir = tmp_path / ".booley_project"
        (project_dir / "cores").mkdir(parents=True)
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        (project_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        core = self._ICARUS_CORE.replace(
            "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n", ""
        )
        _write_core(project_dir / "cores", core, create_sources=False)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            from booley.fusesoc.fusesoc_registry import _setup_command

            _setup_command(
                "sim",
                project_root=tmp_path,
                build_root=tmp_path / "build",
                vlnv=overlay.vlnv,
            )
            projected = next(path for path in overlay.extra_files if path.name.endswith(".core"))
            doc = read_core(projected)
            entry = doc["filesets"]["booley_trace_dump"]["files"][0]
            assert next(iter(entry)).startswith(".booley_project/cores/")
        finally:
            overlay.cleanup()
        assert not projected.exists()


def test_dump_module_detection_walks_filesets_append(tmp_path: Path):
    """The trace-overlay readiness check shares the blind spot: a
    booley_vcd_dump.sv fileset added via append would false-warn 'no dump
    module' and provoke a duplicate overlay injection."""
    text = textwrap.dedent(
        """\
        CAPI=2:
        name: ::demo_core:0
        filesets:
          rtl:
            files:
              - rtl/counter.sv: {file_type: systemVerilogSource}
          trace:
            files:
              - dv/booley_vcd_dump.sv: {file_type: systemVerilogSource}
        targets:
          default: &default_target
            filesets: [rtl]
          sim:
            <<: *default_target
            filesets_append: [trace]
            toplevel: counter
        """
    )
    core = _write_core(tmp_path, text)
    assert _target_includes_dump_module(read_core(core), "sim") is True

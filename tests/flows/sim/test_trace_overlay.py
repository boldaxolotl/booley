"""Behavioral tests for Simulation-owned FuseSoC trace overlays."""

from __future__ import annotations

import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim import trace_overlay as trace_overlay_module
from booley.flows.sim.coverage_overlay import write_coverage_overlay
from booley.flows.sim.execution import SimulationExecution, SimulationOptions
from booley.flows.sim.execution.contract import NamedTests
from booley.flows.sim.execution.engine import PreparedOrdinaryGroup
from booley.flows.sim.record.coordinator import (
    NewSimulationRecordRunRequest,
    SimulationRecord,
    SimulationRecordPolicy,
)
from booley.flows.sim.record.flow_planning import (
    plan_coarse_simulation_record,
    plan_ordinary_hdl_simulation_record,
)
from booley.flows.sim.record.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.staged_input_ledger import StagedInputLedger, sweep_cancelled_run_inputs
from booley.flows.sim.trace_overlay import (
    packaged_vcd_dump_source,
    trace_overlay_vlnv,
    write_trace_overlay,
)
from booley.flows.sim.trace_recipe import TraceMode
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.fusesoc.fusesoc_registry import (
    FuseSocError,
    discover_cores,
    read_core,
    resolve_target_handle,
    setup_command_for_handle,
)
from booley.runtime.pid import capture_process_identity
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import UnknownTargetError
from tests.fusesoc_test_support import CORE_TEXT as _CORE_TEXT
from tests.fusesoc_test_support import INHERITED_CORE_TEXT, symlinked_booley_dir
from tests.fusesoc_test_support import write_core as _write_core


def _target_names(project_root: Path) -> set[str]:
    return {handle.name for handle in TargetCatalog.build(project_root).list()}


def _stealth_booley_dir(directory: Path, *, isolated: bool = False) -> Path:
    """Create a stealth Booley Directory and return its ``cores/`` directory."""
    cores = directory / "cores"
    cores.mkdir(parents=True, exist_ok=True)
    toggle = "ignore_native_cores = true\n" if isolated else ""
    (directory / "booley.toml").write_text(
        f"[stealth]\nenabled = true\n{toggle}", encoding="utf-8"
    )
    (directory / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    return cores


class _PlanningDisclosureAuthenticatedError(RuntimeError):
    pass


def _isolated_icarus_target(tmp_path: Path, core_text: str, *, cocotb: bool):
    project = tmp_path / "project"
    cores = _stealth_booley_dir(project / ".booley", isolated=True)
    (project / "rtl").mkdir()
    (project / "tb").mkdir()
    (project / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (project / "tb" / "tb_dut.sv").write_text(
        "module tb_dut; dut dut(); endmodule\n", encoding="utf-8"
    )
    core = core_text.replace(
        "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n", ""
    )
    if cocotb:
        core = core.replace(
            "      tool: icarus\n", "      tool: icarus\n      cocotb_module: test_demo\n"
        )
    _write_core(cores, core, create_sources=False)
    return TargetCatalog.build(project).select("sim")


def _run_locally(
    command: list[str], *, timeout: float | None = None, cwd: Path | None = None
) -> SubprocessResult:
    """Run planning's FuseSoC setup for real; nothing here builds or launches."""
    done = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return SubprocessResult(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)


def _trace_simulation_record_plan(handle, execution: SimulationExecution, *, cocotb: bool):
    names = ("smoke",)
    catalog = TargetCatalog.build(handle.project_root)
    inspection = catalog.inspect(handle)
    preview = execution.preview(handle, NamedTests(names))
    disclosure = execution.plan_ordinary_group(handle, names)
    common = {
        "handle": handle,
        "inspection": inspection,
        "preview": preview,
        "required_suite": (),
        "revision": "abc123",
        "invocation_id": 1,
        "execution_id": "",
        "trace": True,
        "planning_disclosures": (disclosure,),
        "required_suite_catalog_backed": False,
    }
    if cocotb:
        return plan_coarse_simulation_record(**common, selected_tests=names, kind="cocotb_batch")
    return plan_ordinary_hdl_simulation_record(**common, groups=(names,))


def _stop_after_planning_authentication(_group: PreparedOrdinaryGroup):
    raise _PlanningDisclosureAuthenticatedError


def _admission() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, "interactive", "", None, lambda: False)


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
    @pytest.mark.parametrize("resource_kind", ["missing", "directory"])
    def test_packaged_dump_source_must_resolve_to_regular_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_kind: str
    ) -> None:
        refs = tmp_path / "refs"
        refs.mkdir()
        if resource_kind == "directory":
            (refs / "booley_vcd_dump.sv").mkdir()
        monkeypatch.setattr("booley.runtime.paths.refs_dir", lambda: refs)

        with pytest.raises(FuseSocError, match="packaged trace dump module is not a regular file"):
            packaged_vcd_dump_source()

    def test_writes_colocated_overlay_with_trace_options(self, tmp_path: Path):
        base = _write_core(tmp_path / "ip")  # sim target: verilator, flow sim
        expected_vlnv = "::demo_core-booleytrace:0"
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            # Co-located with the base .core so relative fileset paths still resolve.
            assert overlay.core_file.parent == base.parent
            assert TRACE_OVERLAY_MARKER in overlay.core_file.name
            # Distinct from the base ``::demo_core:0`` → its own build root.
            assert overlay.vlnv == expected_vlnv

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

    def test_overlay_keeps_inherited_verilator_options(self, tmp_path: Path):
        _write_core(tmp_path / "ip", INHERITED_CORE_TEXT)  # #5077
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim_child"))
        try:
            opts = read_core(overlay.core_file)["targets"]["sim_child"]["flow_options"]
            assert opts["verilator_options"][0] == "--timing"
        finally:
            overlay.cleanup()

    def test_overlay_is_skipped_by_discovery(self, tmp_path: Path):
        _write_core(tmp_path / "ip")
        before = _target_names(tmp_path)
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            # The overlay .core exists on disk beside the base...
            assert overlay.core_file.exists()
            # ...but Booley's enumeration ignores it, so it never pollutes the
            # selectable Target list (no `sim` collision, no overlay Target).
            found = discover_cores(tmp_path)
            assert overlay.core_file not in found
            assert _target_names(tmp_path) == before == {"sim"}
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
            ("-CFLAGS, -DVM_TRACE_FMT_FST", "VM_TRACE_FMT_FST.*requires --trace-fst"),
            ("--trace-vcd, -CFLAGS, -DVM_TRACE_FMT_FST", "FST CFLAG.*VCD trace option"),
            ("--trace-fst, -CFLAGS, -DVM_TRACE_FMT_VCD", "VCD CFLAG.*native FST trace option"),
            ("-CFLAGS, '-DVM_TRACE_FMT_FST -DVM_TRACE_FMT_VCD'", "both FST and VCD CFLAGS"),
        ],
    )
    def test_rejects_unsupported_authored_trace_recipe(
        self, tmp_path: Path, options: str, message: str
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
        assert not list(tmp_path.rglob("staged-runtime-inputs/*.json"))
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
    # Same Target with no tracked dump module: the overlay must supply it.
    _ICARUS_CORE_NO_DUMP = _ICARUS_CORE.replace(
        "      - sim/booley_vcd_dump.sv: {file_type: systemVerilogSource}\n", ""
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
        core = self._ICARUS_CORE_NO_DUMP
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

    def test_native_core_isolation_keeps_icarus_dump_source_after_cleanup(self, tmp_path: Path):
        pytest.importorskip("fusesoc")
        handle = _isolated_icarus_target(tmp_path, self._ICARUS_CORE, cocotb=False)
        project = handle.project_root
        overlay = write_trace_overlay(handle)
        try:
            resolved = resolve_target_handle(
                handle, build_root=project / "build", resolution_vlnv=overlay.vlnv
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
        assert not (
            project / ".booley" / "cores" / f"booley_vcd_dump{TRACE_OVERLAY_MARKER}.sv"
        ).exists()

    @pytest.mark.parametrize("cocotb", [False, True], ids=["ordinary", "cocotb"])
    def test_native_core_isolation_discloses_packaged_dump_source_for_simulation_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cocotb: bool
    ) -> None:
        pytest.importorskip("fusesoc")
        handle = _isolated_icarus_target(tmp_path, self._ICARUS_CORE, cocotb=cocotb)
        execution = SimulationExecution(invoke=_run_locally, options=SimulationOptions(trace=True))
        plan = _trace_simulation_record_plan(handle, execution, cocotb=cocotb)
        disclosure = plan.manifest.document["planning_disclosures"][0]

        raw = packaged_vcd_dump_source().read_bytes()
        dump_entries = [
            item
            for item in disclosure["generated_files"]
            if item["path"] == "booley-package/refs/booley_vcd_dump.sv"
        ]
        assert dump_entries == [
            {
                "path": "booley-package/refs/booley_vcd_dump.sv",
                "bytes": len(raw),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "kind": "generated_input",
            }
        ]
        assert packaged_vcd_dump_source().is_file()
        monkeypatch.setattr(PreparedOrdinaryGroup, "compile", _stop_after_planning_authentication)
        executor = OrdinaryHdlSerialExecutor(
            invoke=lambda *_args, **_kwargs: None,
            execution_factory=lambda options: SimulationExecution(
                invoke=_run_locally, options=options
            ),
        )
        invocation = tmp_path / "reports" / "000001"
        invocation.mkdir(parents=True)

        with pytest.raises(_PlanningDisclosureAuthenticatedError):
            SimulationRecord(executor).run(
                NewSimulationRecordRunRequest(
                    plan,
                    handle.project_root,
                    invocation.parent,
                    SimulationRecordPolicy(),
                    invocation,
                    _admission(),
                )
            )

    def test_projected_icarus_overlay_rebases_injected_dump(self, tmp_path: Path):
        cores = _stealth_booley_dir(tmp_path / ".booley")
        _write_core(cores, self._ICARUS_CORE_NO_DUMP, create_sources=False)
        handle = TargetCatalog.build(tmp_path).select("sim")
        overlay = write_trace_overlay(handle)
        try:
            argv = setup_command_for_handle(
                handle, build_root=tmp_path / "build", resolution_vlnv=overlay.vlnv
            )
            assert overlay.vlnv in argv
            projected = overlay.projection
            assert projected is not None
            doc = read_core(projected)
            entry = doc["filesets"]["booley_trace_dump"]["files"][0]
            assert next(iter(entry)).startswith(".booley/cores/")
        finally:
            overlay.cleanup()
        assert not projected.exists()

    @pytest.mark.parametrize("tool", ["verilator", "icarus"])
    def test_stealth_overlay_of_native_core_is_not_projected(self, tmp_path: Path, tool: str):
        # Stealth Mode with a repository-native core (e.g. a scaffolded
        # `rbg.core` at the Project Root): only `.booley/cores/` cores are
        # projected, so the overlay must stay beside the native core instead of
        # failing with "core is outside the stealth core root".
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir(exist_ok=True)
        (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        if tool == "icarus":
            core_text = self._ICARUS_CORE_NO_DUMP
            base = _write_core(tmp_path / "ip", core_text, create_sources=False)
        else:
            base = _write_core(tmp_path / "ip")
        overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
        try:
            assert overlay.core_file.parent == base.parent
            assert overlay.projection is None
            if tool == "icarus":
                entry = read_core(overlay.core_file)["filesets"]["booley_trace_dump"]["files"][0]
                declared = next(iter(entry))
                # Declared relative to the overlay `.core`, which FuseSoC resolves against.
                assert (overlay.core_file.parent / declared).is_file()
        finally:
            overlay.cleanup()
        assert not overlay.core_file.exists()

    @pytest.mark.parametrize("fails_at", ["core_write", "projection"])
    def test_failed_overlay_write_leaves_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails_at: str
    ):
        # A failure after the injected dump module or the overlay `.core` is
        # written must remove them, or they linger untracked in the Project.
        cores = _stealth_booley_dir(tmp_path / ".booley")
        _write_core(cores, self._ICARUS_CORE_NO_DUMP, create_sources=False)
        handle = TargetCatalog.build(tmp_path).select("sim")
        runtime = tmp_path / ".booley" / ".runtime"

        def tree() -> list[Path]:
            return sorted(p for p in tmp_path.rglob("*") if not p.is_relative_to(runtime))

        before = tree()

        def fail(*_args: object) -> None:
            raise FuseSocError("injected failure")

        if fails_at == "core_write":
            real_created = StagedInputLedger.created
            calls: list[Path] = []

            def fail_second_confirmation(
                ledger: StagedInputLedger, relative: Path, *rest: object
            ) -> None:
                # No public entry point reaches a failure between create and confirm.
                calls.append(relative)
                if len(calls) == 2:
                    raise OSError("injected failure")
                real_created(ledger, relative, *rest)

            monkeypatch.setattr(StagedInputLedger, "created", fail_second_confirmation)
        else:
            monkeypatch.setattr(trace_overlay_module, "projected_core_path", fail)

        with pytest.raises(FuseSocError, match="injected failure"):
            write_trace_overlay(handle)
        assert tree() == before
        assert not list(runtime.rglob("*.json"))

    @pytest.mark.parametrize("isolated", [False, True], ids=["projected", "isolated"])
    def test_icarus_overlay_under_symlinked_booley_dir(self, tmp_path: Path, isolated: bool):
        project = tmp_path / "project"
        project.mkdir()
        external = symlinked_booley_dir(project, tmp_path / "external-booley")
        _stealth_booley_dir(external, isolated=isolated)
        _write_core(external / "cores", self._ICARUS_CORE_NO_DUMP, create_sources=False)
        handle = TargetCatalog.build(project).select("sim")
        overlay = write_trace_overlay(handle)
        try:
            projected = overlay.projection
            assert projected is not None
            argv = setup_command_for_handle(
                handle, build_root=project / "build", resolution_vlnv=overlay.vlnv
            )
            assert overlay.vlnv in argv
            entry = read_core(projected)["filesets"]["booley_trace_dump"]["files"][0]
            declared = next(iter(entry))
            # Projection roots the dump module under `.booley/cores/`; isolated
            # mode declares the packaged source instead.
            if isolated:
                assert Path(declared).is_absolute()
            else:
                assert declared.startswith(".booley/cores/")
            assert (project / declared).is_file()
        finally:
            overlay.cleanup()
        assert not projected.exists()


def test_dump_module_added_by_filesets_append_is_not_injected_again(tmp_path: Path):
    """A booley_vcd_dump.sv fileset added via ``filesets_append`` counts as present.

    Missing it would inject a duplicate dump module into the overlay.
    """
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
            default_tool: icarus
            flow: sim
            flow_options:
              tool: icarus
            filesets_append: [trace]
            toplevel: counter
        """
    )
    _write_core(tmp_path, text)

    overlay = write_trace_overlay(TargetCatalog.build(tmp_path).select("sim"))
    try:
        doc = read_core(overlay.core_file)
        assert overlay.extra_files == ()
        assert "booley_trace_dump" not in doc["filesets"]
        assert "-sbooley_vcd_dump" in doc["targets"]["sim"]["flow_options"]["iverilog_options"]
    finally:
        overlay.cleanup()


def _needs_kill(test):
    test = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")(test)
    return pytest.mark.skipif(
        capture_process_identity(os.getpid()) is None, reason="needs a process identity"
    )(test)


def _records(project: Path) -> list[Path]:
    return list((project / ".booley/.runtime/staged-runtime-inputs").glob("*.json"))


def _overlay_files(project: Path) -> list[Path]:
    return sorted(path for path in project.rglob("*") if ".booleytrace" in path.name)


def _writer(tmp_path: Path, kind: str):
    """Build a Project and return ``(core directory, write)`` for the writer *kind*."""
    (tmp_path / ".booley").mkdir(exist_ok=True)
    if kind == "trace":
        _write_core(
            tmp_path / "ip", TestWriteTraceOverlay._ICARUS_CORE_NO_DUMP, create_sources=False
        )
        handle = TargetCatalog.build(tmp_path).select("sim")
        return tmp_path / "ip", lambda: write_trace_overlay(handle)
    _write_core(tmp_path / "ip")
    handle = TargetCatalog.build(tmp_path).select("sim")
    return tmp_path / "ip", lambda: write_coverage_overlay(
        handle, instrumentation=("--coverage-line",), trace=False
    )


def _die_writing(write, trace: Path, *, before_content: bool = False) -> None:
    """Run *write* in a forked worker that dies, like a SIGKILL, before it can clean up."""
    from tests.flows.sim.test_simulation_record_crash_matrix import _KILLED, _run_killed

    def worker() -> None:
        if before_content:
            real = StagedInputLedger.created

            def dying(ledger: StagedInputLedger, relative: Path, *rest: object) -> None:
                real(ledger, relative, *rest)
                if relative.name.endswith(".core"):
                    # No public entry point stops between the confirmation and the content.
                    os._exit(_KILLED)

            StagedInputLedger.created = dying  # type: ignore[method-assign]
        write()
        os._exit(_KILLED)

    _run_killed(worker, trace)


@_needs_kill
@pytest.mark.parametrize(
    "case",
    [
        "trace-unchanged",
        "trace-before-content",
        "trace-modified",
        "coverage-unchanged",
        "coverage-modified",
    ],
)
def test_killed_overlay_write_is_swept_or_named(tmp_path: Path, case: str) -> None:
    """A killed run's overlay files go; one the user changed stays and is named."""
    kind, state = case.split("-", 1)
    _core_dir, write = _writer(tmp_path, kind)
    _die_writing(write, tmp_path / "trace.txt", before_content=state == "before-content")
    overlay = next(path for path in _overlay_files(tmp_path) if path.suffix == ".core")
    assert (len(_overlay_files(tmp_path)) == 2) is (kind == "trace")
    assert len(_records(tmp_path)) == 1
    if state == "before-content":
        assert overlay.stat().st_size == 0

    if state == "modified":
        with overlay.open("ab") as stream:
            stream.write(b"# mine\n")
        with pytest.raises(FuseSocError, match="cancelled Booley run left") as caught:
            write()
        assert overlay.name in str(caught.value)
        assert "safe to delete" in str(caught.value)
        assert overlay.read_bytes().endswith(b"# mine\n")
        assert len(_records(tmp_path)) == 1
        assert _overlay_files(tmp_path) == [overlay]
        return
    sweep_cancelled_run_inputs(tmp_path / ".booley")
    assert _overlay_files(tmp_path) == []
    assert _records(tmp_path) == []


@pytest.mark.parametrize("case", ["trace-core", "dump-module", "coverage-core"])
def test_occupied_overlay_path_is_kept_and_named(tmp_path: Path, case: str) -> None:
    """A file Booley did not write at an overlay path is never overwritten or deleted."""
    core_dir, write = _writer(tmp_path, "coverage" if case == "coverage-core" else "trace")
    stem = next(core_dir.glob("*.core")).stem
    occupied = (
        core_dir
        / {
            "trace-core": f"{stem}.booleytrace.core",
            "dump-module": "booley_vcd_dump.booleytrace.sv",
            "coverage-core": f"{stem}.booleytrace.coverage.core",
        }[case]
    )
    occupied.write_bytes(b"mine\n")

    with pytest.raises(FuseSocError, match="did not create") as caught:
        write()
    assert str(occupied) in str(caught.value)
    assert occupied.read_bytes() == b"mine\n"
    assert _overlay_files(tmp_path) == [occupied]
    assert _records(tmp_path) == []


@pytest.mark.parametrize("kind", ["trace", "coverage"])
def test_live_overlay_survives_a_sweep_and_a_second_writer(tmp_path: Path, kind: str) -> None:
    core_dir, write = _writer(tmp_path, kind)
    overlay = write()
    before = overlay.core_file.read_bytes()

    sweep_cancelled_run_inputs(tmp_path / ".booley")
    assert overlay.core_file.read_bytes() == before
    with pytest.raises(FuseSocError, match="did not create") as caught:
        write()
    assert str(core_dir) in str(caught.value)
    assert overlay.core_file.read_bytes() == before

    overlay.cleanup()
    assert _overlay_files(tmp_path) == []
    assert _records(tmp_path) == []

    overlay = write()
    replacement = core_dir / "user-file"
    replacement.write_bytes(b"mine\n")
    replacement.replace(overlay.core_file)  # a different inode at the overlay path
    overlay.cleanup()
    assert overlay.core_file.read_bytes() == b"mine\n"
    assert _records(tmp_path) == []


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX directory modes")
def test_cleanup_keeps_the_record_of_a_file_it_could_not_remove(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    core_dir, write = _writer(tmp_path, "coverage")
    overlay = write()
    core_dir.chmod(0o500)
    try:
        overlay.cleanup()
        assert overlay.core_file.exists()
        assert len(_records(tmp_path)) == 1
    finally:
        core_dir.chmod(0o700)
    overlay.cleanup()
    assert not overlay.core_file.exists()
    assert _records(tmp_path) == []


@_needs_kill
def test_swept_stealth_overlay_loses_its_projection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cores = _stealth_booley_dir(project / ".booley", isolated=False)
    _write_core(cores, TestWriteTraceOverlay._ICARUS_CORE_NO_DUMP, create_sources=False)
    handle = TargetCatalog.build(project).select("sim")
    from booley.flows.sim.trace_overlay import sweep_cancelled_run_leftovers
    from booley.fusesoc.fusesoc_registry import prepare_core_library_plan

    def worker() -> None:
        write_trace_overlay(handle)
        prepare_core_library_plan(project)

    _die_writing(worker, tmp_path / "trace.txt")
    projected = project / ".booley-projected-design.booleytrace.core"
    assert projected.exists()

    sweep_cancelled_run_leftovers(project, project / ".booley")
    assert not projected.exists()
    assert _overlay_files(project) == []
    assert (project / ".booley-projected-design.core").exists()

    (cores / "broken.core").write_text("not a core\n", encoding="utf-8")
    sweep_cancelled_run_leftovers(project, project / ".booley")  # reports nothing, raises nothing


@pytest.mark.parametrize("fail", [False, True])
def test_replacement_made_while_creating_is_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """A file swapped in at the path after the exclusive create is not Booley's to remove."""
    ledger = StagedInputLedger(tmp_path / ".booley", tmp_path)
    destination = tmp_path / "x.core"
    real_lstat = os.lstat
    swapped = False

    def lstat_after_swap(path: str | os.PathLike[str]) -> os.stat_result:
        # The swap comes just before the identity check, with no handle open (Windows
        # refuses to replace an open file).
        nonlocal swapped
        if not swapped and Path(path) == destination:
            swapped = True
            stranger = tmp_path / "stranger"
            stranger.write_bytes(b"mine\n")
            stranger.replace(destination)
        return real_lstat(path)

    def failing(self: StagedInputLedger, relative: Path, *rest: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("booley.flows.sim.staged_input_ledger.os.lstat", lstat_after_swap)
    if fail:
        monkeypatch.setattr(StagedInputLedger, "created", failing)
        with pytest.raises(OSError, match="boom"):
            ledger.create_file(Path("x.core"), b"content\n")
    else:
        ledger.create_file(Path("x.core"), b"content\n")
    ledger.remove_created()
    assert (tmp_path / "x.core").read_bytes() == b"mine\n"

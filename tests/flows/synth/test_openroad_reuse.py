"""OpenROAD reuse in physical synth (#2473), driven through the real configure half.

The boundary is faked: it emulates the Makefile's ``yosys`` and ``openroad``
stages and counts OpenROAD invocations, so OpenROAD never runs. Everything
else (Target resolution layout, configure, key, slot, interpretation,
reporting) is the production code.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

import booley
from booley.core.build_paths import work_root_for
from booley.flows.base import SubprocessResult
from booley.flows.builtin_cli import parse_request
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_SUCCESS
from booley.mcp.flow_adapter import flow_schema
from booley.mcp.server import _params_to_argv
from booley.targets.catalog import TargetCatalog
from tests.target_test_support import install_lenient_target_catalog, make_target_handle

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="OpenROAD reuse runs inside the Linux Sandbox; needs ELF/ldd"
)

TARGET = "core"
LOGICAL_TARGET = "core_logical"
SDC = (
    "# constraints for dut\n"
    "create_clock -name clk -period 4.0 [get_ports clk]\n"
    "set_input_delay 0.5 -clock clk [get_ports {d[*]}]\n"
    "set_max_fanout 50 [current_design]\n"
    "set_false_path -from [get_ports a] -to [get_ports b]\n"
    "set_load 0.1 {a b}\n"
)
NETLIST = "// netlist v0\nmodule dut(input clk); wire n1; endmodule\n"
_REAL_CATALOG_BUILD = TargetCatalog.build
# Per-run fields: when, how long, this run's id and numbered artifact paths.
_VOLATILE_REPORT_KEYS = {
    "timestamp",
    "elapsed_s",
    "peak_rss_mb",
    "openroad_reuse",
    "run_evidence",
    "artifacts",
    "provenance",
}


@dataclass
class Bench:
    """A scratch Project with a physical and a logical Target, and a fake boundary."""

    root: Path
    sdc: str = SDC
    netlist: str = NETLIST
    argv: list[str] = field(default_factory=list)
    openroad_calls: int = 0
    #: Read the SDC in place in the Project, as projection mode (ignore_native_cores) does.
    sdc_in_place: bool = False
    #: Runs in place of the OpenROAD stage's file writes when set; may return a result.
    on_openroad: Callable[[Path], SubprocessResult | None] | None = None

    @property
    def lib(self) -> Path:
        return self.root / "lib"

    @property
    def slot_root(self) -> Path:
        return work_root_for(self.root, "synth", TARGET, variant="openroad_reuse")

    def staged_sdc(self, target: str = TARGET) -> Path:
        return self._build_root(target) / "src" / "dut.sdc"

    def _build_root(self, target: str) -> Path:
        return work_root_for(self.root, "synth", target) / "dut_0" / "syn"

    def resolve(self, target: str = TARGET, **_kwargs) -> fusesoc_registry.ResolvedTarget:
        """Stage sources and SDC under the Target work root, as FuseSoC export does."""
        build_root = self._build_root(target)
        (build_root / "src").mkdir(parents=True, exist_ok=True)
        (build_root / "src" / "dut.sv").write_text("module dut(input clk); endmodule\n")
        self.staged_sdc(target).write_text(self.sdc)
        sdc = "src/dut.sdc"
        if self.sdc_in_place:
            sdc = str(self.root / "constraints" / "dut.sdc")
            Path(sdc).parent.mkdir(exist_ok=True)
            Path(sdc).write_text(self.sdc)
        mode = "logical" if target == LOGICAL_TARGET else "physical"
        return fusesoc_registry.ResolvedTarget(
            name=target,
            vlnv="::dut:0",
            toplevel="dut",
            eda_tool="yosys",
            flow_options={"tool": "yosys", "synth_mode": mode},
            files=(
                fusesoc_registry.ResolvedFile(name="src/dut.sv", file_type="systemVerilogSource"),
                fusesoc_registry.ResolvedFile(name=sdc, file_type="SDC"),
            ),
            parameters={},
            build_root=build_root,
            edam_path=build_root / "dut_0.eda.yml",
        )

    def boundary(self, cmd: list[str], **_kwargs) -> SubprocessResult:
        """Emulate ``make -C <dir> [yosys|openroad]``; a bare make runs both stages."""
        # Stamped before writing: the stale guard stays active while the coarse
        # file clock cannot make this call's own writes look stale.
        dispatched = time.time() - 1
        build = self.root / cmd[2]
        stage = cmd[3] if len(cmd) > 3 else "all"
        stdout = ""
        if stage in {"yosys", "all"}:
            for name, text in {
                "sv2v.log": "",
                "sv2v_converted.v": "module dut(input clk); endmodule\n",
                "yosys.log": "Chip area for top module '\\dut': 6400.0\nNumber of cells: 100\n",
                "stat_dut.txt": "Number of cells: 100\n",
                "check_dut.txt": "Found and reported 0 problems.\n",
                "synth_dut.v": self.netlist,
                "sta_dut.v": self.netlist,
            }.items():
                (build / name).write_text(text)
            stdout += "BOOLEY_STAGE: sv2v\nBOOLEY_STAGE: yosys\n"
        if stage in {"openroad", "all"}:
            self.openroad_calls += 1
            result = self.on_openroad(build) if self.on_openroad else self._openroad(build)
            if result is not None:
                result.dispatched_unix = dispatched
                return result
            stdout += "BOOLEY_STAGE: sta\n"
        return SubprocessResult(returncode=0, stdout=stdout, dispatched_unix=dispatched)

    def _openroad(self, build: Path) -> SubprocessResult | None:
        timing = build / "reports" / "timing"
        if not timing.is_dir():  # the real script writes into it without creating it
            return SubprocessResult(returncode=1, stdout="no reports/timing\n")
        # Slack differs per invocation, so a reused result is visibly the producer's.
        slack = 2.0 + self.openroad_calls / 1000
        (build / "openroad.log").write_text(
            f"STA_WORST_SLACK_NS: {slack:.6f}\n"
            f"STA_PERCLOCK: name=clk period_ns=4.000000 wns_ns={slack:.6f} whs_ns=0.1\n"
            "Design area 7000 u^2 50% utilization.\n"
        )
        (build / "openroad_dut.v").write_text(self.netlist)
        (timing / "overall.rpt").write_text(f"slack {slack}\n")
        (timing / "overall.csv.rpt").write_text(f"a,b,{slack}\n")
        return None

    def run(self, *argv: str, target: str = TARGET) -> tuple[object, dict]:
        """One fresh ``booley flow synth`` invocation; returns (outcome, Target report)."""
        flow = AsicSynthesizeFlow()
        flow.parse_args(
            [
                "--target",
                target,
                "--work-dir",
                str(self.root),
                "--report-dir",
                str(self.root / "reports"),
                *self.argv,
                *argv,
            ]
        )
        flow.read_state()
        with patch.object(flow, "_execute_boundary", side_effect=self.boundary):
            outcome = flow._run()
        report = json.loads((self.root / "reports" / f"synth_{target}.json").read_text())
        return outcome, report

    def run_fresh(self, *argv: str) -> tuple[object, dict]:
        """Run and assert OpenROAD ran and nothing was reused."""
        before = self.openroad_calls
        outcome, report = self.run(*argv)
        assert self.openroad_calls == before + 1
        assert "openroad_reuse" not in report
        return outcome, report

    def run_hit(self) -> tuple[object, dict]:
        """Run and assert OpenROAD was reused."""
        before = self.openroad_calls
        outcome, report = self.run()
        assert self.openroad_calls == before
        assert "openroad_reuse" in report
        return outcome, report


@pytest.fixture()
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    bench = Bench(root=tmp_path)
    for path, text in {
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib": "library (nangate) {}\n",
        "nangate45/Nangate45_tech.lef": "VERSION 5.8 ;\n",
        "nangate45/Nangate45_stdcell.lef": "MACRO INV_X1\n",
        "nangate45/Nangate45.rc": "# rc\nset_layer_rc -layer metal1 -resistance 5.432e-03\n",
    }.items():
        (bench.lib / path).parent.mkdir(parents=True, exist_ok=True)
        (bench.lib / path).write_text(text)
    # A real ELF stands in for openroad, so ldd and -version succeed; it never runs a stage.
    (tmp_path / "bin").mkdir()
    shutil.copyfile(
        Path(shutil.which("true") or "/bin/true").resolve(), tmp_path / "bin" / "openroad"
    )
    (tmp_path / "bin" / "openroad").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("PRJ_LIB_DIR", str(bench.lib))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_IMAGE_BUILT_AT", "2026-11-01T00:00:00Z")
    for name in ("BOOLEY_RUN_ID", "LD_PRELOAD", "LD_AUDIT", "RSZ_MT_SLEW_BIAS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(tmp_path / "state.json"))
    install_lenient_target_catalog(
        monkeypatch,
        real_catalog_build=_REAL_CATALOG_BUILD,
        fallback_handle=lambda root, selector: make_target_handle(
            root, selector, flow="generic", eda_tool="yosys", drivable_by=("synth",)
        ),
    )
    monkeypatch.setattr(fusesoc_registry, "_resolve_target", bench.resolve)
    return bench


def _without_volatile(value: object) -> object:
    """*value* with every per-run field removed, at any depth."""
    if not isinstance(value, dict):
        return value
    return {k: _without_volatile(v) for k, v in value.items() if k not in _VOLATILE_REPORT_KEYS}


def test_unchanged_rerun_reuses_openroad_with_producer_label(bench: Bench) -> None:
    _, first = bench.run_fresh()
    outcome, second = bench.run_hit()

    producer = first["run_evidence"]["run_id"]
    assert _without_volatile(second) == _without_volatile(first)
    assert second["openroad_reuse"]["producer_run_id"] == producer
    assert second["run_evidence"]["run_id"] != producer
    assert outcome.detail[TARGET]["openroad_reuse"]["producer_run_id"] == producer
    assert f"REUSED -- OpenROAD results reused from run {producer}" in outcome.report_text
    run_log = work_root_for(bench.root, "synth", TARGET) / "run.log"
    assert f"BOOLEY_OPENROAD_REUSED: run_id={producer}" in run_log.read_text()


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


_KEYED_CHANGES: dict[str, Callable[[Bench, pytest.MonkeyPatch], None]] = {
    "netlist": lambda b, _: setattr(b, "netlist", NETLIST.replace("v0", "v1")),
    "script": lambda b, _: b.argv.extend(["--utilization-pct", "33"]),
    "tech_lef": lambda b, _: _append(b.lib / "nangate45/Nangate45_tech.lef", "#"),
    "stdcell_lef": lambda b, _: _append(b.lib / "nangate45/Nangate45_stdcell.lef", "#"),
    "liberty": lambda b, _: _append(
        b.lib / "cell/lib/NangateOpenCellLibrary_typical_ccs.lib", " "
    ),
    "wire_rc": lambda b, _: _append(b.lib / "nangate45/Nangate45.rc", "# changed\n"),
    "sdc_content": lambda b, _: setattr(b, "sdc", SDC.replace("4.0", "5.0")),
    "openroad_binary": lambda b, _: _append_bytes(b.root / "bin" / "openroad", b"\0"),
    "image_identity": lambda _, mp: mp.setenv("BOOLEY_IMAGE_BUILT_AT", "2026-11-02T00:00:00Z"),
    "booley_version": lambda _, mp: mp.setattr(booley, "__version__", "0.0.0+changed"),
    "environment": lambda _, mp: mp.setenv("RSZ_MT_SLEW_BIAS", "1"),
}


def _append_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(path.read_bytes() + data)


@pytest.mark.parametrize("change", sorted(_KEYED_CHANGES))
def test_changing_one_keyed_input_misses(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    bench.run_fresh()
    bench.run_hit()

    _KEYED_CHANGES[change](bench, monkeypatch)

    bench.run_fresh()
    bench.run_hit()


_UNPROVABLE_SDC_LINES = {
    "source": "[ source other.sdc]",
    "unbraced_expr_x5b": "set_max_delay [expr \\x5bsource f\\x5d] -to a",
    "set_auto_path": "set auto_path [list /tmp]",
    "brace_close_in_comment": "set_load 1 {a}#}",
    "proc": "proc f {} {source other.sdc}",
    "env": "create_clock -period [array get env] clk",
    "rand": "create_clock -period [expr {rand()}] clk",
    "unbraced_expr": "create_clock -period [expr 1+2] clk",
    "semicolon": "create_clock -period 4 clk; source other.sdc",
    "nested_bracket": "set_false_path -from [get_ports [source other.sdc]]",
    "dollar_substitution": "create_clock -period $p clk",
    "non_getter_bracket": "set_load 1 [open other.sdc]",
    "unbraced_bus_index": "set_false_path -from [get_ports d[3]]",
    "backslash_continuation": "set_load 1 \\\n[source other.sdc]",
    "formfeed_comment": "# a\f[source other.sdc]",
    "bare_cr": "# a\r[source other.sdc]",
    "nbsp_comment": "\u00a0# [source other.sdc]",
}


_LIBERTY = "cell/lib/NangateOpenCellLibrary_typical_ccs.lib"
_UNPROVABLE_OTHER: dict[str, Callable[[Bench, pytest.MonkeyPatch], None]] = {
    "liberty_include_file": lambda b, _: _append(b.lib / _LIBERTY, "include_file(x);"),
    "liberty_gzip": lambda b, _: (b.lib / _LIBERTY).write_bytes(gzip.compress(b"library(x){}")),
    "relative_path_entry": lambda b, mp: mp.setenv("PATH", f".:{b.root / 'bin'}:/usr/bin:/bin"),
}


@pytest.mark.parametrize("construct", [*sorted(_UNPROVABLE_SDC_LINES), *sorted(_UNPROVABLE_OTHER)])
def test_unprovable_tcl_never_reuses(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, construct: str
) -> None:
    bench.run_fresh()
    bench.run_hit()  # the control SDC (comment, bus bits, brace-list, two getters) reuses

    if construct in _UNPROVABLE_OTHER:
        _UNPROVABLE_OTHER[construct](bench, monkeypatch)
    else:
        bench.sdc = SDC + _UNPROVABLE_SDC_LINES[construct] + "\n"

    bench.run_fresh()
    bench.run_fresh()
    assert not (bench.slot_root / "slot.json").exists()


def test_sdc_read_in_place_reuses(bench: Bench) -> None:
    bench.sdc_in_place = True
    bench.run_fresh()
    bench.run_hit()

    bench.sdc = SDC.replace("4.0", "5.0")

    bench.run_fresh()


def test_internal_name_only_netlist_change_misses(bench: Bench) -> None:
    bench.run_fresh()
    bench.run_hit()

    bench.netlist = NETLIST.replace("n1", "n9")

    bench.run_fresh()


def _fail(returncode: int = 1, *, timed_out: bool = False) -> Callable[[Path], SubprocessResult]:
    return lambda _build: SubprocessResult(returncode=returncode, timed_out=timed_out)


def _interrupt(_build: Path) -> SubprocessResult:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    "stage",
    [_fail(), _fail(-1, timed_out=True), _interrupt],
    ids=["failed", "timed_out", "interrupted"],
)
def test_unfinished_openroad_stage_leaves_no_hittable_slot(
    bench: Bench, stage: Callable[[Path], SubprocessResult]
) -> None:
    bench.run_fresh()
    bench.run_hit()

    bench.on_openroad = stage
    if stage is _interrupt:
        with pytest.raises(KeyboardInterrupt):
            bench.run("--fresh-openroad")
    else:
        bench.run("--fresh-openroad")
    bench.on_openroad = None

    bench.run_fresh()
    bench.run_hit()


def _damage_slot(bench: Bench, damage: str) -> None:
    if damage == "missing_slot":
        shutil.rmtree(bench.slot_root)
    elif damage == "corrupt_manifest":
        (bench.slot_root / "slot.json").write_text("{")
    else:
        next(bench.slot_root.glob("gen-*/reports/timing/overall.rpt")).unlink()


@pytest.mark.parametrize("damage", ["missing_slot", "corrupt_manifest", "missing_file"])
def test_unusable_slot_is_a_fresh_run(bench: Bench, damage: str) -> None:
    bench.run_fresh()
    _damage_slot(bench, damage)

    outcome, report = bench.run_fresh()

    assert outcome.exit_code == EXIT_SUCCESS
    assert report["infra_error"] == ""
    bench.run_hit()


def test_input_change_during_openroad_stores_nothing(bench: Bench) -> None:
    bench.run_fresh()
    bench.run_hit()

    def edit_sdc_mid_run(build: Path) -> None:
        # The SDC copy OpenROAD reads (configure stages it in the build dir).
        (build / "sdc" / "0_dut.sdc").write_text(SDC.replace("4.0", "6.0"))
        bench._openroad(build)

    bench.on_openroad = edit_sdc_mid_run
    bench.run("--fresh-openroad")
    bench.on_openroad = None

    assert not (bench.slot_root / "slot.json").exists()
    bench.run_fresh()
    bench.run_hit()


@pytest.mark.parametrize("case", ["fresh_run", "mismatched_slot"])
def test_stale_guard_still_rejects_old_openroad_outputs(bench: Bench, case: str) -> None:
    bench.run_fresh()
    bench.run_hit()
    # Keep the slot's outputs: the slot itself is invalidated before OpenROAD runs.
    old_outputs = bench.root / "old-outputs"
    shutil.copytree(next(bench.slot_root.glob("gen-*")), old_outputs)

    def leave_old_outputs(build: Path) -> SubprocessResult:
        for source in (p for p in old_outputs.rglob("*") if p.is_file()):
            destination = build / source.relative_to(old_outputs)
            shutil.copyfile(source, destination)
            os.utime(destination, (100, 100))  # older than this run's dispatch
        return SubprocessResult(returncode=0, stdout="BOOLEY_STAGE: sta\n")

    bench.on_openroad = leave_old_outputs
    if case == "fresh_run":
        _, report = bench.run("--fresh-openroad")
    else:
        bench.sdc = SDC.replace("4.0", "5.0")
        _, report = bench.run()

    assert "openroad_reuse" not in report
    assert report["timing_complete"] is False
    assert report["ppa_complete"] is False
    assert report["termination"] == "infrastructure_error"
    assert not (bench.slot_root / "slot.json").exists()


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_fresh_openroad_runs_and_refreshes_slot(bench: Bench, surface: str) -> None:
    _, first = bench.run_fresh()
    bench.run_hit()
    if surface == "cli":
        argv = ["--target", TARGET, "--fresh-openroad"]
        assert parse_request(AsicSynthesizeFlow(), argv).fresh_openroad is True
    else:
        assert (
            flow_schema(AsicSynthesizeFlow())["properties"]["fresh_openroad"]["type"] == "boolean"
        )
        argv = _params_to_argv({"target": TARGET, "fresh_openroad": True})
    assert "--fresh-openroad" in argv

    _, refreshed = bench.run_fresh("--fresh-openroad")
    _, reused = bench.run_hit()

    producer = refreshed["run_evidence"]["run_id"]
    assert producer != first["run_evidence"]["run_id"]
    assert reused["openroad_reuse"]["producer_run_id"] == producer


def test_logical_mode_creates_no_slot_and_never_reports_reuse(bench: Bench) -> None:
    bench.run_fresh()
    bench.run_hit()

    for _ in range(2):
        outcome, report = bench.run(target=LOGICAL_TARGET)
        assert "openroad_reuse" not in report
        assert "REUSED" not in outcome.report_text
    assert not work_root_for(
        bench.root, "synth", LOGICAL_TARGET, variant="openroad_reuse"
    ).exists()

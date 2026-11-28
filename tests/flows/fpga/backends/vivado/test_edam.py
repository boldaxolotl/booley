"""Unit tests for booley.flows.fpga.backends.vivado.edam — Edalize vivado-flow helpers (ADR 0019).

Covers the three Booley-side halves of the fpga_impl
conversion: the vivado EDAM build, the resolved run command, and the report
post-processor (validated against a captured Vivado impl log). The actual
live Vivado run is exercised separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.flows import edam as edam_layer
from booley.flows.edam import EdamFile
from booley.flows.fpga.backends.vivado import edam as fpga_edam
from booley.flows.fpga.profiles import VIVADO_PROFILES
from booley.flows.run_evidence import FlowSourceEvidence

if TYPE_CHECKING:
    from booley.flows.fpga.recipe import ResolvedFpgaRecipe

_FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "vivado_reports"


def _recipe(**overrides) -> ResolvedFpgaRecipe:
    """Build a ResolvedFpgaRecipe; only EDAM-relevant fields matter, rest are placeholders."""
    from booley.flows.fpga.recipe import ResolvedFpgaRecipe

    fields = {
        "target": "cfg",
        "resolved": None,
        "part": "xc7a35tcpg236-1",
        "top": "dut_top",
        "xdc_files": (),
        "sources": (),
        "include_dirs": (),
        "defines": (),
        "vlogparams": {},
        "out_of_context": False,
        "ppa_profile": VIVADO_PROFILES["balanced"],
        "recipe_snapshot": {},
        "recipe_fingerprint": "",
        "source_evidence": FlowSourceEvidence("rev", "sha"),
    }
    fields.update(overrides)
    return ResolvedFpgaRecipe(**fields)


def _metrics(**values) -> dict:
    """The post-processor's result for text that carries only *values*."""
    unset = dict.fromkeys(("lut_count", "ff_count", "bram_count", "dsp_count", "wns_ns", "whs_ns"))
    counts = {"latch_count": 0, "comb_loop_count": 0, "multi_driven_count": 0}
    return {**unset, "per_clock": {}, **counts, "status": None, **values}


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


_ROUTED = {"latch_count": 0, "comb_loop_count": 0, "multi_driven_count": 0, "status": "pass"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            _fixture("vivado_impl_pass.log"),
            _metrics(lut_count=2853, ff_count=1523, wns_ns=0.892, whs_ns=0.031, status="pass"),
            id="impl-log",
        ),
        # Real Vivado 2025.2 routed reports: report_timing_summary emits a worst-slack
        # *table*, not the "WNS(ns) : <v>" colon form, and "There are 0 combinational
        # loops in the design." must not count as a loop.
        pytest.param(
            _fixture("vivado_routed_real.rpt"),
            _metrics(lut_count=14284, ff_count=6475, bram_count=0, wns_ns=-10.431, whs_ns=0.048)
            | _ROUTED,
            id="routed-2025",
        ),
        # A from-scratch routed run with real BRAM and DSP rows and a signed worst slack.
        pytest.param(
            _fixture("vivado_routed_real_2025_2.rpt"),
            _metrics(lut_count=18, ff_count=36, bram_count=1, dsp_count=1, wns_ns=-8.949)
            | {"whs_ns": 0.195}
            | _ROUTED,
            id="routed-2025-2",
        ),
        # A standalone timing report: slack but no utilization and no success marker.
        pytest.param(
            _fixture("vivado_timing.rpt"), _metrics(wns_ns=0.892, whs_ns=0.031), id="timing-only"
        ),
        pytest.param("", _metrics(), id="empty"),
        pytest.param(
            "| Register as Latch | 3 |\n"
            "ERROR: [Route 35-7] LUTLP-1 combinational loop\n"
            "MDRV-1 multi-driven net foo\n",
            _metrics(latch_count=3, comb_loop_count=1, multi_driven_count=1),
            id="critical-conditions",
        ),
        # The `-?[\d.]+` capture can match a malformed token on Vivado log-format
        # drift; float() would raise, so the slack degrades to None instead.
        pytest.param("WNS(ns) : 1.2.3\nWHS(ns) : ..\n", _metrics(), id="malformed-slack"),
        pytest.param("Block RAM Tile | 0.5", _metrics(bram_count=0.5), id="fractional-bram"),
    ],
)
def test_parse_fpga_reports(text: str, expected: dict) -> None:
    assert fpga_edam.parse_fpga_reports(text) == expected


# A realistic two-clock report: the "Clock Summary" carries each clock's
# constrained Period(ns); the "Intra Clock Table" carries each clock's own worst
# setup/hold slack. The parser joins them by name and derives cp/fmax.
_CLK2_PERIOD = "clk2   {0.000 2.500} 5.000       200.000\n"
_CLK_SLACK = (
    "clk    0.250    0.000    0                      1234                 0.100    0.000\n"
)
_TWO_CLOCK_REPORT = f"""\
Clock Summary
Clock  Waveform(ns)  Period(ns)  Frequency(MHz)
-----  ------------  ----------  --------------
clk    {{0.000 5.000}} 10.000      100.000
{_CLK2_PERIOD}
Intra Clock Table
Clock  WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)
-----  -------  -------  ---------------------  -------------------  -------  -------
{_CLK_SLACK}clk2   -0.100   0.500    3                      200                  0.050    0.000
"""


def _clock(period, wns, whs, cp=None, fmax=None) -> dict:
    return {
        "period_ns": period,
        "wns_ns": wns,
        "whs_ns": whs,
        "critical_path_ps": cp,
        "fmax_mhz": fmax if fmax is None else pytest.approx(fmax, rel=1e-4),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # cp = (period - wns) * 1000; fmax = 1e6 / cp.
        pytest.param(
            _TWO_CLOCK_REPORT,
            {
                "clk": _clock(10.0, 0.25, 0.1, 9750.0, 102.5641),
                "clk2": _clock(5.0, -0.1, 0.05, 5100.0, 196.0784),
            },
            id="two-clocks",
        ),
        # A clock in only one table still gets a row; cp/fmax need both halves.
        pytest.param(
            _TWO_CLOCK_REPORT.replace(_CLK2_PERIOD, "").replace(_CLK_SLACK, ""),
            {"clk": _clock(10.0, None, None), "clk2": _clock(None, -0.1, 0.05)},
            id="one-table-each",
        ),
    ],
)
def test_parse_fpga_reports_joins_per_clock_timing(text: str, expected: dict) -> None:
    """Fmax and critical path are per clock: no flat scalar names them."""
    metrics = fpga_edam.parse_fpga_reports(text)
    assert metrics["per_clock"] == expected
    assert "critical_path_ps" not in metrics
    assert "fmax_mhz" not in metrics


def _build_edam(root: Path, **recipe_overrides) -> dict:
    recipe_fields = {
        "top": "dut_top",
        "part": "xcvu9p-flga2104-2-i",
        "sources": (
            EdamFile(root / "rtl" / "a.sv", "systemVerilogSource"),
            EdamFile(root / "rtl" / "b.sv", "systemVerilogSource"),
            # Declared SystemVerilog despite the .v name.
            EdamFile(root / "rtl" / "legacy.v", "systemVerilogSource"),
        ),
        "include_dirs": (root / "rtl" / "include",),
        "xdc_files": (root / "constr" / "pins.xdc",),
        "defines": ("SYNTH", "WIDTH=32"),
        "vlogparams": {"DEPTH": 16},
    }
    extra = recipe_overrides.pop("extra_eda_tool_options", None)
    return fpga_edam.build_fpga_edam(
        _recipe(**(recipe_fields | recipe_overrides)),
        name="fpga_cfgA",
        workspace_root=root,
        work_root=root / ".booley" / ".runtime" / "edalize" / "fpga" / "cfgA",
        extra_eda_tool_options=extra,
    )


def test_build_fpga_edam(tmp_path: Path) -> None:
    """Sources keep declared types and order, names stay relative, defines become params."""
    edam = _build_edam(tmp_path, extra_eda_tool_options={"jobs": "4"})

    up = "../../../../../"
    assert edam == {
        "name": "fpga_cfgA",
        "toplevel": "dut_top",
        "files": [
            {"name": f"{up}rtl/a.sv", "file_type": "systemVerilogSource"},
            {"name": f"{up}rtl/b.sv", "file_type": "systemVerilogSource"},
            {"name": f"{up}rtl/legacy.v", "file_type": "systemVerilogSource"},
            {"name": f"{up}constr/pins.xdc", "file_type": "xdc"},
            {
                "name": f"{up}rtl/include",
                "file_type": "verilogSource",
                "is_include_file": True,
                "include_path": f"{up}rtl/include",
            },
        ],
        "parameters": {
            "SYNTH": {"datatype": "bool", "paramtype": "vlogdefine", "default": True},
            "WIDTH": {"datatype": "int", "paramtype": "vlogdefine", "default": 32},
            "DEPTH": {"datatype": "int", "paramtype": "vlogparam", "default": 16},
        },
        "flow_options": {"tool": "vivado", "part": "xcvu9p-flga2104-2-i", "jobs": "4"},
    }


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"extra_eda_tool_options": {"source": "/etc/evil.tcl"}}, id="tool-option"),
        pytest.param(
            {"sources": (EdamFile(Path("/etc/passwd"), "verilogSource"),)}, id="outside-file"
        ),
    ],
)
def test_build_fpga_edam_rejects_unsafe_input(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(edam_layer.EdamSecurityError):
        _build_edam(tmp_path, **overrides)


def test_make_command_relative_to_work_dir(tmp_path: Path) -> None:
    work_root = tmp_path / ".booley" / ".runtime" / "edalize" / "fpga" / "cfgA"
    # Relative so it stays independent of the Runtime workspace location.
    assert fpga_edam.fpga_run_command(work_root, tmp_path)[:3] == [
        "make",
        "-C",
        ".booley/.runtime/edalize/fpga/cfgA",
    ]


def test_configure_writes_project_files(tmp_path: Path):
    """End-to-end file-gen — needs edalize, skipped where absent."""
    pytest.importorskip("edalize")
    root = tmp_path
    (root / "rtl").mkdir()
    (root / "rtl" / "a.sv").write_text("module dut_top; endmodule\n")
    (root / "constr").mkdir()
    (root / "constr" / "pins.xdc").write_text("# constraints\n")
    work_root = root / ".booley" / ".runtime" / "edalize" / "fpga" / "cfgA"
    edam = fpga_edam.build_fpga_edam(
        _recipe(
            top="dut_top",
            part="xcvu9p-flga2104-2-i",
            sources=(EdamFile(root / "rtl" / "a.sv", "systemVerilogSource"),),
            xdc_files=(root / "constr" / "pins.xdc",),
            defines=("SYNTH",),
            vlogparams={"WIDTH": 8, "FLAG": True},
        ),
        name="fpga_cfgA",
        workspace_root=root,
        work_root=work_root,
    )
    edam_layer.configure("vivado", edam, work_root)
    # configure() is pure file-gen: a Makefile + project tcl must appear.
    assert (work_root / "Makefile").is_file()
    assert any(work_root.glob("*.tcl"))
    project_tcl = work_root / "fpga_cfgA.tcl"
    assert "WIDTH=8" in project_tcl.read_text(encoding="utf-8")
    fpga_edam.validate_vivado_parameter_contract(
        work_root,
        "fpga_cfgA",
        {"WIDTH": 8, "FLAG": True},
    )

    original = project_tcl.read_bytes()
    fpga_edam.apply_ppa_profile(work_root, "fpga_cfgA", VIVADO_PROFILES["balanced"])
    assert project_tcl.read_bytes() == original

    fpga_edam.apply_ppa_profile(
        work_root,
        "fpga_cfgA",
        VIVADO_PROFILES["max_frequency"],
    )
    fpga_edam.enable_out_of_context(work_root, "fpga_cfgA")
    patched = project_tcl.read_text(encoding="utf-8")
    assert "STEPS.POST_ROUTE_PHYS_OPT_DESIGN.TCL.POST" in patched
    hook = work_root / "booley_post_route_complete.tcl"
    assert hook.read_text() == 'puts "BOOLEY_POST_ROUTE_PHYS_OPT_COMPLETE"\n'
    assert "Flow_PerfOptimized_high [get_runs synth_1]" in patched
    assert "Performance_ExplorePostRoutePhysOpt [get_runs impl_1]" in patched
    assert patched.index("Flow_PerfOptimized_high") < patched.index(
        "STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS"
    )


@pytest.mark.parametrize(
    ("tcl", "message"),
    [
        ("create_project x\n", r"dropped.*WIDTH"),
        ("set_property generic {WIDTH=99} [get_filesets sources_1]\n", r"WIDTH=8.*rendered 99"),
    ],
    ids=["dropped", "changed"],
)
def test_parameter_contract_rejects_a_lost_override(tmp_path: Path, tcl: str, message: str):
    (tmp_path / "fpga_cfgA.tcl").write_text(tcl, encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        fpga_edam.validate_vivado_parameter_contract(tmp_path, "fpga_cfgA", {"WIDTH": 8})

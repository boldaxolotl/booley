"""Golden snapshots for the Yosys/OpenROAD script generators.

What drift these protect against: the ``519842f`` class — a required line
silently missing from generated TCL (``make_tracks`` after
``initialize_floorplan`` broke every OpenROAD placement with PPL-0021, and a
padding tweak tripped GPL-0302), invisible to the substring asserts in
``tests/flows/synth/backends/openroad/test_timing.py``. Each test here snapshots the FULL
generated script so any line-level change must be acknowledged by
regenerating the golden (see ``tests/golden/conftest.py`` for the
``BOOLEY_REGEN_GOLDEN`` convention).

Inputs are canned: fixed fake PDK/liberty paths, and file-writing generators
run against ``tmp_path`` with the embedded work dir normalized to ``<WORK>``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.synth.backends.openroad.reporting import reg2reg_timing_tcl, write_sta_sdc
from booley.flows.synth.backends.openroad.timing import OpenRoadPdk, write_openroad_script
from booley.flows.synth.backends.yosys import core as syn_core
from booley.flows.synth.timing import StaTimingConfig
from tests.golden.conftest import assert_matches_golden, normalize_work_dir

# Canned, machine-independent input paths. These are embedded verbatim in the
# generated scripts (no existence checks in the generators), so fixed fake
# values keep the goldens deterministic.
_LIBERTY = Path("/opt/pdk/NangateOpenCellLibrary_typical.lib")
_PDK = OpenRoadPdk(
    tech_lef=Path("/opt/pdk/nangate45/Nangate45_tech.lef"),
    stdcell_lef=Path("/opt/pdk/nangate45/Nangate45_stdcell.lef"),
    layer_rc=Path("/opt/pdk/nangate45/Nangate45.rc"),
)


def _timing_config(
    *,
    utilization_pct: float = 40.0,
    repair_timing: bool = True,
) -> StaTimingConfig:
    """A fully pinned timing config (defaults spelled out for stability)."""
    return StaTimingConfig(
        mode="physical",
        clock="clk_i",
        period_ps=4000.0,
        input_delay_pct=30.0,
        output_delay_pct=70.0,
        sdc=(),
        utilization_pct=utilization_pct,
        repair_timing=repair_timing,
    )


# ---------------------------------------------------------------------------
# write_openroad_script — full TCL per config axis (the 519842f surface)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("golden_name", "utilization_pct", "repair_timing"),
    [
        # Baseline: default utilization, repair_timing on (the shipped default).
        ("openroad_repair_on_util40.tcl", 40.0, True),
        # repair_timing=False must drop exactly the repair_timing block
        # (BOOLEY_STAGE marker + repair_timing + re-place/re-estimate lines).
        ("openroad_repair_off_util40.tcl", 40.0, False),
        # Utilization sweep: density formula min(0.80, util/100 + 0.25) caps
        # at 0.80 here — the GPL-0302 headroom logic 519842f adjusted.
        ("openroad_repair_on_util70.tcl", 70.0, True),
    ],
)
def test_openroad_script_golden(
    tmp_path: Path,
    golden_name: str,
    utilization_pct: float,
    repair_timing: bool,
) -> None:
    """Snapshot the complete OpenROAD placement+repair+timing TCL."""
    config = _timing_config(
        utilization_pct=utilization_pct,
        repair_timing=repair_timing,
    )
    # The netlist/SDC/report paths live in the work dir in production; keeping
    # them under tmp_path here means one normalization pass covers them all.
    script_path = write_openroad_script(
        design_name="dut",
        liberty=_LIBERTY,
        sta_netlist=tmp_path / "sta_dut.v",
        sdc_path=tmp_path / "sta_constraints.sdc",
        pdk=_PDK,
        report_dir=tmp_path / "reports" / "timing",
        work_dir=tmp_path,
        config=config,
    )
    script = normalize_work_dir(script_path.read_text(encoding="utf-8"), tmp_path)
    assert_matches_golden(f"yosys/{golden_name}", script)


# ---------------------------------------------------------------------------
# reg2reg_timing_tcl — arg-free snippet embedded by OpenROAD
# ---------------------------------------------------------------------------


def test_reg2reg_timing_tcl_golden() -> None:
    """Snapshot the reg->reg slack reporting block embedded by OpenROAD."""
    assert_matches_golden("yosys/reg2reg_timing.tcl", reg2reg_timing_tcl())


# ---------------------------------------------------------------------------
# write_sta_sdc — generated SDC constraints
# ---------------------------------------------------------------------------


def test_sta_sdc_default_golden(tmp_path: Path) -> None:
    """Snapshot the auto-generated SDC (clock + I/O delays + drive/load)."""
    sdc_path = write_sta_sdc(_timing_config(), "clk_i", tmp_path)
    # No paths are embedded in the SDC — content is purely derived from the
    # config numbers, so no normalization is needed.
    assert_matches_golden("yosys/sta_sdc_default.sdc", sdc_path.read_text(encoding="utf-8"))


def test_sta_sdc_with_user_sdc_golden(tmp_path: Path) -> None:
    """A user SDC (extra constraints) is prepended verbatim to the defaults."""
    user_sdc = tmp_path / "user_constraints.sdc"
    user_sdc.write_text(
        "# project false paths\nset_false_path -from [get_ports {rst_ni}]\n",
        encoding="utf-8",
    )
    config = _timing_config()._replace(sdc=(user_sdc,))
    sdc_path = write_sta_sdc(config, "clk_i", tmp_path)
    assert_matches_golden("yosys/sta_sdc_with_user_sdc.sdc", sdc_path.read_text(encoding="utf-8"))


def test_sta_sdc_target_owns_clock_and_io_golden(tmp_path: Path) -> None:
    """A Target SDC that owns clock + I/O delays suppresses ALL generated
    defaults (ADR 0029 decision 5) — only the authored constraints remain."""
    user_sdc = tmp_path / "target_constraints.sdc"
    user_sdc.write_text(
        "create_clock -name clk_i -period 25.000000 [get_ports {clk_i}]\n"
        "set_input_delay -clock clk_i 0.0 [all_inputs]\n"
        "set_output_delay -clock clk_i 0.0 [all_outputs]\n"
        "set_false_path -from [get_ports {mode_i}]\n",
        encoding="utf-8",
    )
    config = _timing_config()._replace(sdc=(user_sdc,))
    sdc_path = write_sta_sdc(config, "clk_i", tmp_path)
    assert_matches_golden(
        "yosys/sta_sdc_target_owns_clock.sdc",
        sdc_path.read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# _build_yosys_script — the yosys -p command string
# ---------------------------------------------------------------------------

# _build_yosys_script does no filesystem I/O, so all inputs can be fully
# canned (stable /work-style paths — the sandbox worktree convention).
_SV2V_OUT = Path("/work/syn_result/dut/sv2v_converted.v")
_YS_WORK_DIR = Path("/work/syn_result/dut")


def test_yosys_script_bare_golden() -> None:
    """Snapshot the minimal script: `synth` unflattened, no params, default ABC."""
    script = syn_core._build_yosys_script(
        [_SV2V_OUT],
        "dut",
        _LIBERTY,
        _YS_WORK_DIR,
        flatten=False,
        params=None,
        abc_recipe=None,
    )
    assert_matches_golden("yosys/yosys_script_bare.txt", script)


def test_yosys_script_flatten_params_fast_golden() -> None:
    """Snapshot the fully-loaded script: `synth -flatten` + chparam + fast ABC."""
    script = syn_core._build_yosys_script(
        [_SV2V_OUT],
        "dut",
        _LIBERTY,
        _YS_WORK_DIR,
        flatten=True,
        params={"WIDTH": "8", "DEPTH": "16"},
        abc_recipe="fast",
    )
    assert_matches_golden("yosys/yosys_script_flatten_params_fast.txt", script)


# Raw SystemVerilog sources for the slang frontend (no sv2v transpile step).
_SLANG_SOURCES = [
    Path("/work/src/dut/pkg.sv"),
    Path("/work/src/dut/dut.sv"),
]
_SLANG_INC_DIRS = [Path("/work/src/dut/include")]


def test_yosys_script_slang_bare_golden() -> None:
    """Snapshot the slang-frontend script: read_slang, chformal, unflattened."""
    script = syn_core._build_yosys_script(
        _SLANG_SOURCES,
        "dut",
        _LIBERTY,
        _YS_WORK_DIR,
        flatten=False,
        params=None,
        abc_recipe=None,
        frontend="slang",
        inc_dirs=_SLANG_INC_DIRS,
        defines=["SYNTHESIS"],
    )
    assert_matches_golden("yosys/yosys_script_slang_bare.txt", script)


def test_yosys_script_slang_flatten_params_golden() -> None:
    """Snapshot the slang script with `synth -flatten` + -G parameter overrides."""
    script = syn_core._build_yosys_script(
        _SLANG_SOURCES,
        "dut",
        _LIBERTY,
        _YS_WORK_DIR,
        flatten=True,
        params={"WIDTH": "8", "DEPTH": "16"},
        abc_recipe="fast",
        frontend="slang",
        inc_dirs=_SLANG_INC_DIRS,
        defines=["SYNTHESIS", "SYNC_RESET"],
    )
    assert_matches_golden("yosys/yosys_script_slang_flatten_params.txt", script)


# ---------------------------------------------------------------------------
# _build_abc_command — one golden per recipe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("golden_name", "abc_recipe"),
    [
        # None falls through to the bare liberty mapping (the "default" recipe).
        ("abc_default.txt", None),
        ("abc_fast.txt", "fast"),
        ("abc_balanced.txt", "balanced"),
        # A literal "+..." recipe is forwarded verbatim to -script.
        ("abc_custom_plus.txt", "+strash;dch;map"),
    ],
)
def test_abc_command_golden(golden_name: str, abc_recipe: str | None) -> None:
    """Snapshot each ABC technology-mapping recipe string."""
    # Production always feeds ``_build_abc_command`` a POSIX string (the caller
    # ``_build_yosys_script`` runs both paths through ``_quote_yosys_path``,
    # which normalizes ``\`` → ``/``). Pass ``.as_posix()`` here so the direct
    # unit call matches that contract on a Windows host — ``str(Path("/opt/..."))``
    # would yield ``\opt\...`` and diverge from production.
    cmd = syn_core._build_abc_command(
        _LIBERTY.as_posix(),
        _YS_WORK_DIR.as_posix(),
        "dut",
        abc_recipe,
    )
    assert_matches_golden(f"yosys/{golden_name}", cmd)

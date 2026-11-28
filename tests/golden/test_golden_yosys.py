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

from booley.flows.synth.backends.openroad.reporting import reg2reg_timing_tcl
from booley.flows.synth.backends.openroad.timing import OpenRoadPdk, write_openroad_script
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
    *, utilization_pct: float = 40.0, repair_timing: bool = True
) -> StaTimingConfig:
    """A fully pinned timing config (defaults spelled out for stability)."""
    return StaTimingConfig(
        mode="physical", sdc=(), utilization_pct=utilization_pct, repair_timing=repair_timing
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
    tmp_path: Path, golden_name: str, utilization_pct: float, repair_timing: bool
) -> None:
    """Snapshot the complete OpenROAD placement+repair+timing TCL."""
    config = _timing_config(utilization_pct=utilization_pct, repair_timing=repair_timing)
    # The netlist/SDC/report paths live in the work dir in production; keeping
    # them under tmp_path here means one normalization pass covers them all.
    script_path = write_openroad_script(
        design_name="dut",
        liberty=_LIBERTY,
        sta_netlist=tmp_path / "sta_dut.v",
        sdc_paths=(tmp_path / "target.sdc",),
        pdk=_PDK,
        report_dir=tmp_path / "reports" / "timing",
        work_dir=tmp_path,
        config=config,
        target="synth_dut",
        source_sdc_paths=(tmp_path / "target.sdc",),
    )
    script = normalize_work_dir(script_path.read_text(encoding="utf-8"), tmp_path)
    assert_matches_golden(f"yosys/{golden_name}", script)


# ---------------------------------------------------------------------------
# reg2reg_timing_tcl — arg-free snippet embedded by OpenROAD
# ---------------------------------------------------------------------------


def test_reg2reg_timing_tcl_golden() -> None:
    """Snapshot the reg->reg slack reporting block embedded by OpenROAD."""
    assert_matches_golden("yosys/reg2reg_timing.tcl", reg2reg_timing_tcl())

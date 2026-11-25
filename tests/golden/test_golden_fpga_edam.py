"""Golden snapshots for the fpga_impl EDAM (edalize vivado flow input).

What drift these protect against: the same failure class as ``519842f``/
``ca5adaf`` — a silently missing or misshapen entry in generated EDA-tool input.
The EDAM is the single source the edalize vivado flow ``configure()`` renders
into the project TCL, so a dropped file entry, a lost ``include_path`` pin
(the incdir-dirname-strip bug documented in ``edam.build_edam``), or a
mistyped parameter would break Vivado runs while substring asserts stay
green.  The full dict is snapshotted as sorted-keys indented JSON (see
``tests/golden/conftest.py`` for the ``BOOLEY_REGEN_GOLDEN`` convention).

``build_fpga_edam`` does no filesystem I/O (path confinement is pure string
math), so every input is a canned fake path under a stable ``/ws`` workspace
root — nothing machine-specific can leak into the golden.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.flows.edam import EdamFile
from booley.flows.fpga.backends.vivado.edam import build_fpga_edam
from booley.flows.fpga.profiles import VIVADO_PROFILES
from booley.flows.run_evidence import FlowSourceEvidence
from tests.golden.conftest import assert_matches_golden

# Canned workspace layout. work_root mirrors work_root_for()'s real
# shape so the relative file names in the golden look like production ones.
_WS = Path("/ws")
_WORK_ROOT = _WS / ".booley" / ".runtime" / "edalize" / "fpga" / "lite"


def _build(extra_eda_tool_options: dict | None = None) -> str:
    """Build the canned EDAM and serialize it deterministically."""
    from booley.flows.fpga.recipe import ResolvedFpgaRecipe

    recipe = ResolvedFpgaRecipe(
        target="lite",
        resolved=None,
        part="xc7a100tcsg324-1",
        top="dut_top",
        xdc_files=(_WS / "constraints" / "board.xdc",),
        sources=(
            EdamFile(_WS / "rtl" / "pkg.sv", "systemVerilogSource"),
            EdamFile(_WS / "rtl" / "dut.sv", "systemVerilogSource"),
            EdamFile(_WS / "rtl" / "legacy_blk.v", "verilogSource"),
        ),
        include_dirs=(_WS / "rtl" / "include",),
        defines=("SYNTHESIS", "WIDTH=8"),
        vlogparams={"DEPTH": 16},
        out_of_context=False,
        ppa_profile=VIVADO_PROFILES["balanced"],
        recipe_snapshot={},
        recipe_fingerprint="",
        source_evidence=FlowSourceEvidence("rev", "sha"),
    )
    edam = build_fpga_edam(
        recipe,
        name="lite_fpga",
        workspace_root=_WS,
        work_root=_WORK_ROOT,
        extra_eda_tool_options=extra_eda_tool_options,
    )
    return json.dumps(edam, indent=2, sort_keys=True)


@pytest.mark.parametrize(
    ("golden_name", "extra_eda_tool_options"),
    [
        # Part-only EDA-tool options, typed files and parameters.
        ("edam_default.json", None),
        # Extra *whitelisted* vivado flow options merged in.
        ("edam_extra_eda_tool_options.json", {"jobs": 8, "synth": "Flow_PerfOptimized_high"}),
    ],
)
def test_fpga_edam_golden(golden_name: str, extra_eda_tool_options: dict | None) -> None:
    assert_matches_golden(f"fpga/{golden_name}", _build(extra_eda_tool_options))

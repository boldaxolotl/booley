#!/usr/bin/env python3
"""Check native fanout limits and bounded repair using production-generated Tcl."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from booley.flows.synth.backends.openroad.timing import OpenRoadPdk, write_openroad_script
from booley.flows.synth.timing import StaTimingConfig


def generate(
    path: Path, pdk: Path, count: int, setting: int | None, rule: str, library: bool
) -> Path:
    """Render a deterministic reset-load chain with the real physical generator."""
    path.mkdir(parents=True, exist_ok=True)
    netlist, sdc = path / "dut.v", path / "dut.sdc"
    lines = [
        "module dut(input clk, input reset_i, input data_i, output data_o);",
        f"wire reset_n; wire [{count}:0] q;",
        f"assign q[0] = data_i; assign data_o = q[{count}];",
        "INV_X1 driver (.A(reset_i), .ZN(reset_n));",
    ]
    lines.extend(
        f"DFFR_X1 load_{i} (.D(q[{i}]), .CK(clk), .RN(reset_n), .Q(q[{i + 1}]));"
        for i in range(count)
    )
    netlist.write_text("\n".join([*lines, "endmodule"]), encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 20 [get_ports clk]\n" + rule, encoding="utf-8")
    liberty = pdk / "cell/lib/NangateOpenCellLibrary_typical_ccs.lib"
    if library:
        text = liberty.read_text(encoding="utf-8").replace("{", "{\n default_max_fanout : 25;", 1)
        liberty = path / "fanout.lib"
        liberty.write_text(text, encoding="utf-8")
    reports = path / "reports/timing"
    reports.mkdir(parents=True)
    script = write_openroad_script(
        "dut",
        liberty,
        netlist,
        (sdc,),
        OpenRoadPdk(
            *(
                pdk / "nangate45" / name
                for name in ("Nangate45_tech.lef", "Nangate45_stdcell.lef", "Nangate45.rc")
            )
        ),
        reports,
        path,
        StaTimingConfig(mode="physical", repair_timing=False, max_fanout=setting),
        target="fanout_acceptance",
        source_sdc_paths=(sdc,),
    )
    text = script.read_text(encoding="utf-8").replace(
        "\nrepair_design\n",
        "\nset started [clock milliseconds]\nrepair_design\n"
        'puts "REPAIR_ELAPSED_MS: [expr {[clock milliseconds]-$started}]"\n',
    )
    script.write_text(text, encoding="utf-8")
    return script


def run_case(script: Path, executable: str, expected: float, buffering: bool) -> None:
    """Require measured repair, native limit, completed stages and structural buffers."""
    log = script.parent / "native.log"
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            [executable, "-exit", str(script)],
            cwd=script.parent,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    text = log.read_text(encoding="utf-8")
    if process.returncode or (not buffering and "RSZ-0035" in text):
        raise RuntimeError(f"native fanout arm failed: {log}")
    for stage in (
        "floorplan",
        "global_placement",
        "repair_design",
        "detailed_placement",
        "sta_report",
    ):
        if f"BOOLEY_STAGE: {stage}" not in text:
            raise RuntimeError(f"missing native physical stage {stage}: {log}")
    elapsed = re.findall(r"^REPAIR_ELAPSED_MS: (\d+)$", text, re.MULTILINE)
    if len(elapsed) != 1 or int(elapsed[0]) > 15000:
        raise RuntimeError(f"repair exceeded 15s or missing measurement: {log}")
    limits = re.findall(r"^OPENROAD_FANOUT_LIMIT: (\S+)$", text, re.MULTILINE)
    if len(limits) != 1 or abs(float(limits[0]) / expected - 1) > 1e-6:
        raise RuntimeError(f"native exact limit differs from {expected}: {log}")
    netlist = (script.parent / "openroad_dut.v").read_text(encoding="utf-8")
    if buffering and not re.search(r"\bBUF_X\d+\s", netlist):
        raise RuntimeError(f"constrained load fixture inserted no structural buffers: {log}")


def main() -> None:
    """Run required native arms; missing tools or PDK inputs fail explicitly."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--pdk-root", type=Path, default=Path("/opt/pdk"))
    args = parser.parse_args()
    executable = shutil.which("openroad")
    if executable is None:
        raise RuntimeError("required native OpenROAD executable unavailable")
    for name in (
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45.rc",
    ):
        if not (args.pdk_root / name).is_file():
            raise RuntimeError(f"required managed PDK input missing: {name}")
    for name, count, setting, rule, library, limit in (
        ("default", 10000, None, "", False, 1e20),
        ("setting50", 200, 50, "", False, 50),
        ("design100", 200, 50, "set_max_fanout 100 [current_design]\n", False, 100),
        (
            "scoped25",
            200,
            50,
            "set_max_fanout 25 [sta::Instance_cell [get_cells driver]]\n",
            False,
            25,
        ),
        (
            "scoped100",
            200,
            50,
            "set_max_fanout 100 [sta::Instance_cell [get_cells driver]]\n",
            False,
            50,
        ),
        ("library25", 200, 50, "", True, 25),
    ):
        script = generate(
            args.work_dir.resolve() / name, args.pdk_root.resolve(), count, setting, rule, library
        )
        run_case(script, executable, limit, name != "default")
    print("openroad.fanout: passed actual10k and setting/SDC/library native arms")


if __name__ == "__main__":
    main()

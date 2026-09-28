#!/usr/bin/env bash
set -euo pipefail

pdk_root="${1:-/opt/pdk}"
evidence_dir="${2:-}"
liberty="${pdk_root}/cell/lib/NangateOpenCellLibrary_typical_ccs.lib"
tech_lef="${pdk_root}/nangate45/Nangate45_tech.lef"
stdcell_lef="${pdk_root}/nangate45/Nangate45_stdcell.lef"
layer_rc="${pdk_root}/nangate45/Nangate45.rc"

for required in "$liberty" "$tech_lef" "$stdcell_lef" "$layer_rc"; do
  test -r "$required"
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/reports"

# Loading the Qt-backed entry point catches runtime-library omissions without
# needing a display server in CI.
QT_QPA_PLATFORM=offscreen openroad -gui -exit -no_init -no_splash /dev/null \
  2>&1 | tee "$work/openroad-gui.log"

cat > "$work/dut.v" <<'VERILOG'
module dut(input wire clk, input wire [3:0] data_i, output reg data_o);
  always @(posedge clk)
    data_o <= (data_i[0] & data_i[1]) | (data_i[2] ^ data_i[3]);
endmodule
VERILOG

cat > "$work/openroad_dut.v" <<'VERILOG'
module dut(input wire clk, input wire [7:0] data_i, output wire [7:0] data_o);
  wire buffered_data;
  wire [7:0] unused_qn;
  BUF_X1 u_probe_buffer (.A(data_i[0]), .Z(buffered_data));
  DFF_X1 u_probe_register_0 (.D(buffered_data), .CK(clk), .Q(data_o[0]), .QN(unused_qn[0]));
  DFF_X1 u_probe_register_1 (.D(data_i[1]), .CK(clk), .Q(data_o[1]), .QN(unused_qn[1]));
  DFF_X1 u_probe_register_2 (.D(data_i[2]), .CK(clk), .Q(data_o[2]), .QN(unused_qn[2]));
  DFF_X1 u_probe_register_3 (.D(data_i[3]), .CK(clk), .Q(data_o[3]), .QN(unused_qn[3]));
  DFF_X1 u_probe_register_4 (.D(data_i[4]), .CK(clk), .Q(data_o[4]), .QN(unused_qn[4]));
  DFF_X1 u_probe_register_5 (.D(data_i[5]), .CK(clk), .Q(data_o[5]), .QN(unused_qn[5]));
  DFF_X1 u_probe_register_6 (.D(data_i[6]), .CK(clk), .Q(data_o[6]), .QN(unused_qn[6]));
  DFF_X1 u_probe_register_7 (.D(data_i[7]), .CK(clk), .Q(data_o[7]), .QN(unused_qn[7]));
endmodule
VERILOG

cat > "$work/dut.sdc" <<'SDC'
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay 0.2 -clock clk [get_ports {data_i[*]}]
set_output_delay 0.2 -clock clk [get_ports {data_o[*]}]
SDC

PYTHONPATH=/booley-source/src python - "$work" "$liberty" "$tech_lef" "$stdcell_lef" "$layer_rc" <<'PY'
import sys
from pathlib import Path

from booley.flows.synth.backends.openroad.timing import OpenRoadPdk, write_openroad_script
from booley.flows.synth.backends.yosys.core import _build_yosys_script
from booley.flows.synth.timing import StaTimingConfig

work, liberty, tech_lef, stdcell_lef, layer_rc = map(Path, sys.argv[1:])


def write_yosys_script(path: Path, script: str) -> None:
    path.write_text("\n".join(script.split("; ")) + "\n", encoding="utf-8")


yosys_script = _build_yosys_script(
    [work / "dut.v"], "dut", liberty, work, False, None, "balanced"
)
write_yosys_script(work / "synth.ys", yosys_script)
undriven_script = yosys_script.replace(
    "read_liberty -lib -nooverwrite -setattr booley_check_library",
    "add -wire intentional_undriven 1; add -assert intentional_undriven; "
    "read_liberty -lib -nooverwrite -setattr booley_check_library",
)
write_yosys_script(work / "synth-undriven.ys", undriven_script)

abc_control_dir = work / "abc-control"
abc_control_dir.mkdir()
abc_control_script = _build_yosys_script(
    [work / "dut.v"], "dut", liberty, abc_control_dir, False, None, "balanced"
)
abc_control_script = abc_control_script.replace(
    " -dont_use FA_X1 -dont_use HA_X1", ""
)
write_yosys_script(abc_control_dir / "synth.ys", abc_control_script)

collision_liberty = work / "collision.lib"
collision_liberty.write_text(
    """library(test) {
  cell(BUF_X1) {
    area: 1;
    pin(A) { direction: input; }
    pin(Z) { direction: output; function: \"A\"; }
  }
}
""",
    encoding="utf-8",
)


def write_collision_case(label: str, design_name: str, rtl: str) -> None:
    run_dir = work / label
    run_dir.mkdir()
    source = run_dir / "dut.v"
    source.write_text(rtl, encoding="utf-8")
    script = _build_yosys_script(
        [source], design_name, collision_liberty, run_dir, False, None, None
    )
    write_yosys_script(run_dir / "synth.ys", script)


write_collision_case(
    "collision-preserve",
    "collision_preserve",
    "module BUF_X1(input A, output Z); assign Z = A; endmodule\n"
    "module collision_preserve(input A, output Z); "
    "BUF_X1 u_buf(.A(A), .Z(Z)); endmodule\n",
)
write_collision_case(
    "collision-attribute",
    "collision_attribute",
    "(* booley_check_library = 1 *) module marker(input A, output Z); "
    "assign Z = A; endmodule\n"
    "module collision_attribute(input A, output Z); "
    "marker u_marker(.A(A), .Z(Z)); endmodule\n",
)

pdk = OpenRoadPdk(tech_lef=tech_lef, stdcell_lef=stdcell_lef, layer_rc=layer_rc)
for label, repair_timing in (("repair-off", False), ("repair-on", True)):
    run_dir = work / label
    report_dir = run_dir / "reports"
    report_dir.mkdir(parents=True)
    write_openroad_script(
        "dut",
        liberty,
        work / "openroad_dut.v",
        (work / "dut.sdc",),
        pdk,
        report_dir,
        run_dir,
        StaTimingConfig(
            mode="physical",
            utilization_pct=20.0,
            repair_timing=repair_timing,
            placement_density=0.65,
        ),
        target="synth_dut",
        source_sdc_paths=(work / "dut.sdc",),
    )
    (work / f"run_openroad-{label}.tcl").write_text(
        (run_dir / "run_openroad.tcl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
PY

yosys -q -s "$work/synth.ys" > "$work/yosys.log" 2>&1
grep -Fq "Found and reported 0 problems." "$work/check_dut.txt"
if grep -F "used but has no driver" "$work/check_dut.txt"; then
  exit 1
fi
if grep -E '^(Warning:|ERROR:)' "$work/yosys.log"; then
  exit 1
fi
test -s "$work/log_abc_dut.txt"
if grep -E 'Warning:|ERROR:' "$work/log_abc_dut.txt"; then
  exit 1
fi
cp "$work/check_dut.txt" "$work/check_dut_driven.txt"
cp "$work/log_abc_dut.txt" "$work/log_abc_dut_driven.txt"

yosys -q -s "$work/abc-control/synth.ys" > "$work/abc-control/yosys.log" 2>&1
grep -Fq 'ABC: Warning: Detected 2 multi-output cells (for example, "FA_X1").' \
  "$work/abc-control/log_abc_dut.txt"

yosys -q -s "$work/synth-undriven.ys" > "$work/yosys-undriven.log" 2>&1
grep -Fq 'Warning: Wire dut.\intentional_undriven is used but has no driver.' \
  "$work/check_dut.txt"
cp "$work/check_dut.txt" "$work/check_dut_undriven.txt"
cp "$work/log_abc_dut.txt" "$work/log_abc_dut_undriven.txt"

yosys -q -s "$work/collision-preserve/synth.ys" \
  > "$work/collision-preserve/yosys.log" 2>&1
grep -Fq "Found and reported 0 problems." \
  "$work/collision-preserve/check_collision_preserve.txt"
grep -Fq "module BUF_X1" "$work/collision-preserve/synth_collision_preserve.v"
grep -Fq "assign Z = A;" "$work/collision-preserve/synth_collision_preserve.v"

if yosys -q -s "$work/collision-attribute/synth.ys" \
  > "$work/collision-attribute/yosys.log" 2>&1; then
  echo "reserved-attribute collision unexpectedly succeeded" >&2
  exit 1
fi
grep -Fq "Assertion failed" "$work/collision-attribute/yosys.log"
test ! -e "$work/collision-attribute/synth_collision_attribute.v"

run_openroad() {
  local label="$1"
  local run_dir="$work/$label"
  (
    cd "$run_dir"
    openroad -exit run_openroad.tcl 2>&1 | tee "$work/openroad-${label}.log"
  )
  grep -Fq "BOOLEY_STAGE: global_placement" "$work/openroad-${label}.log"
  grep -Fq "BOOLEY_STAGE: detailed_placement" "$work/openroad-${label}.log"
  grep -Eq 'Design area [0-9.]+ u?m\^2' "$work/openroad-${label}.log"
  grep -Eq '\[INFO RSZ-0026\] Removed [1-9][0-9]* buffers\.' \
    "$work/openroad-${label}.log"
  if grep -E '^\[WARNING ' "$work/openroad-${label}.log"; then
    exit 1
  fi
  test -s "$run_dir/openroad_dut.v"
  if grep -F "u_probe_buffer" "$run_dir/openroad_dut.v"; then
    exit 1
  fi
  cp "$run_dir/openroad_dut.v" "$work/placed-${label}.v"
}

run_openroad "repair-off"
run_openroad "repair-on"
grep -Fq "BOOLEY_STAGE: repair_timing" "$work/openroad-repair-on.log"

if test -n "$evidence_dir"; then
  mkdir -p "$evidence_dir"
  cp "$work"/check_dut_*.txt "$work"/yosys*.log \
    "$work"/log_abc_*.txt "$work"/openroad-*.log \
    "$work"/run_openroad-*.tcl "$work"/synth*.ys \
    "$work"/placed-*.v "$evidence_dir"/
  cp -R "$work/abc-control" "$work/collision-preserve" \
    "$work/collision-attribute" "$evidence_dir"/
fi

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
module dut(input wire clk, input wire rst_n, input wire [7:0] data_i,
           output reg [7:0] data_o);
  always @(posedge clk) begin
    if (!rst_n)
      data_o <= 8'h00;
    else
      data_o <= data_o + data_i + 8'h01;
  end
endmodule
VERILOG

yosys -q -p "read_verilog $work/dut.v; synth -top dut; dfflibmap -liberty $liberty; abc -liberty $liberty; clean; write_verilog -noattr $work/dut_mapped.v"
cat > "$work/dut.sdc" <<'SDC'
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay 0.2 -clock clk [get_ports {rst_n data_i[*]}]
set_output_delay 0.2 -clock clk [get_ports {data_o[*]}]
SDC

run_openroad() {
  local label="$1"
  local repair="$2"
  local repair_command=""
  if test "$repair" = 1; then
    repair_command=$'puts "BOOLEY_STAGE: repair_timing"\nrepair_timing -setup -skip_gate_cloning\ndetailed_placement\nestimate_parasitics -placement'
  fi
  cat > "$work/run-${label}.tcl" <<TCL
read_lef {$tech_lef}
read_lef {$stdcell_lef}
read_liberty {$liberty}
read_verilog {$work/dut_mapped.v}
link_design dut
read_sdc {$work/dut.sdc}
puts "BOOLEY_STAGE: floorplan"
initialize_floorplan -utilization 40.0 -aspect_ratio 1.0 -core_space 2.0 \\
  -site FreePDK45_38x28_10R_NP_162NW_34O
make_tracks
remove_buffers [get_cells *]
source {$layer_rc}
set_wire_rc -signal -layer metal3
set_wire_rc -clock -layer metal6
set_dont_use {CLKBUF_* AOI211_X1 OAI211_X1}
puts "BOOLEY_STAGE: global_placement"
global_placement -density 0.65 -pad_left 1 -pad_right 1 -skip_io
place_pins -hor_layers metal3 -ver_layers metal2
global_placement -density 0.65 -pad_left 1 -pad_right 1
estimate_parasitics -placement
puts "BOOLEY_STAGE: repair_design"
repair_design
repair_tie_fanout -separation 5 LOGIC0_X1/Z
repair_tie_fanout -separation 5 LOGIC1_X1/Z
set_placement_padding -global -left 1 -right 1
puts "BOOLEY_STAGE: detailed_placement"
detailed_placement
estimate_parasitics -placement
$repair_command
puts "BOOLEY_STAGE: report"
report_design_area
report_checks -path_delay max -sort_by_slack -group_count 1
write_verilog {$work/placed-${label}.v}
TCL
  openroad -exit "$work/run-${label}.tcl" 2>&1 | tee "$work/openroad-${label}.log"
  grep -Fq "BOOLEY_STAGE: global_placement" "$work/openroad-${label}.log"
  grep -Fq "BOOLEY_STAGE: detailed_placement" "$work/openroad-${label}.log"
  grep -Eq 'Design area [0-9.]+ u?m\^2' "$work/openroad-${label}.log"
  test -s "$work/placed-${label}.v"
}

run_openroad "repair-off" 0
run_openroad "repair-on" 1
grep -Fq "BOOLEY_STAGE: repair_timing" "$work/openroad-repair-on.log"

if test -n "$evidence_dir"; then
  mkdir -p "$evidence_dir"
  cp "$work"/openroad-*.log "$work"/placed-*.v "$evidence_dir"/
fi

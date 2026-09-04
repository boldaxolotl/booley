read_lef {/opt/pdk/nangate45/Nangate45_tech.lef}
read_lef {/opt/pdk/nangate45/Nangate45_stdcell.lef}
read_liberty {/opt/pdk/NangateOpenCellLibrary_typical.lib}
read_verilog {<WORK>/sta_dut.v}
link_design dut
read_sdc {<WORK>/sta_constraints.sdc}
catch { foreach_in_collection _clk [all_clocks] { puts [format "STA_CLOCK_PERIOD_NS: %.6f" [get_property $_clk period]] ; break } }
puts "BOOLEY_STAGE: floorplan"
initialize_floorplan -utilization 40.000 -aspect_ratio 1.0 -core_space 2.0 \
  -site FreePDK45_38x28_10R_NP_162NW_34O
make_tracks
remove_buffers [get_cells *]
source {/opt/pdk/nangate45/Nangate45.rc}
set_wire_rc -signal -layer metal3
set_wire_rc -clock -layer metal6
set_dont_use {CLKBUF_* AOI211_X1 OAI211_X1}
puts "BOOLEY_STAGE: global_placement"
global_placement -density 0.650 -pad_left 1 -pad_right 1 -skip_io
puts "BOOLEY_STAGE: place_pins"
place_pins -hor_layers metal3 -ver_layers metal2
puts "BOOLEY_STAGE: global_placement"
global_placement -density 0.650 -pad_left 1 -pad_right 1
estimate_parasitics -placement
puts "BOOLEY_STAGE: repair_design"
repair_design
repair_tie_fanout -separation 5 LOGIC0_X1/Z
repair_tie_fanout -separation 5 LOGIC1_X1/Z
set_placement_padding -global -left 1 -right 1
puts "BOOLEY_STAGE: detailed_placement"
detailed_placement
estimate_parasitics -placement
puts "BOOLEY_STAGE: sta_report"
report_design_area
write_verilog {openroad_dut.v}
report_checks -path_delay max -sort_by_slack -group_count 1 > {<WORK>/reports/timing/overall.rpt}
set paths [find_timing_paths -path_delay max -sort_by_slack -group_path_count 1]
set csv_out [open {<WORK>/reports/timing/overall.csv.rpt} w]
foreach path $paths {
  set startpoint_name [get_property [get_property $path startpoint] full_name]
  set endpoint_name [get_property [get_property $path endpoint] full_name]
  set slack [get_property $path slack]
  puts $csv_out [format "%s,%s,%.6f" $startpoint_name $endpoint_name $slack]
  puts [format "STA_WORST_SLACK_NS: %.6f" $slack]
  break
}
close $csv_out
if {[llength [info commands foreach_in_collection]] == 0} {
  proc foreach_in_collection {_var _coll _body} {
    upvar 1 $_var _v
    foreach _v $_coll { uplevel 1 $_body }
  }
}
if {![catch {set _clks [all_clocks]}]} {
  foreach_in_collection _clk $_clks {
    set _cn [get_property $_clk name]
    set _per [get_property $_clk period]
    set _wns "NA"
    if {![catch {set _sp [find_timing_paths -path_delay max -sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_sp] > 0} {
      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]
    }
    set _whs "NA"
    if {![catch {set _hp [find_timing_paths -path_delay min -sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_hp] > 0} {
      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]
    }
    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s whs_ns=%s" $_cn $_per $_wns $_whs]
  }
}
if {![catch {set _r2r [find_timing_paths -path_delay max -sort_by_slack -group_path_count 1 -from [all_registers] -to [all_registers]]}] && [llength $_r2r] > 0} {
  foreach _p $_r2r {
    puts [format "STA_REG2REG_SLACK_NS: %.6f" [get_property $_p slack]]
    break
  }
  catch {report_checks -path_delay max -sort_by_slack -group_count 1 -from [all_registers] -to [all_registers] -format full > {<WORK>/reports/timing/reg2reg.rpt}}
}

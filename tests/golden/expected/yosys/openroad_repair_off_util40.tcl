read_lef {/opt/pdk/nangate45/Nangate45_tech.lef}
read_lef {/opt/pdk/nangate45/Nangate45_stdcell.lef}
read_liberty {/opt/pdk/NangateOpenCellLibrary_typical.lib}
read_verilog {<WORK>/sta_dut.v}
link_design dut
read_sdc {<WORK>/target.sdc}
if {[llength [all_clocks]] == 0} { error "BOOLEY_INPUT_ERROR: synth Target 'synth_dut' SDC files \[<WORK>/target.sdc\] created no clocks; add create_clock to the Target-owned SDC" }
puts "BOOLEY_STAGE: floorplan"
initialize_floorplan -utilization 40.000 -aspect_ratio 1.0 \
  -core_space {2.8 2.8 2.09 2.09} \
  -site FreePDK45_38x28_10R_NP_162NW_34O
make_tracks
remove_buffers
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
puts [format "OPENROAD_INSTANCE_COUNT: %d" [llength [[ord::get_db_block] getInsts]]]
write_verilog {openroad_dut.v}
set paths [find_timing_paths -path_delay max -sort_by_slack -group_path_count 10]
set csv_out [open {<WORK>/reports/timing/overall.csv.rpt} w]
set slacks {}
foreach path [lrange $paths 0 9] {
  set startpoint_name [get_property [get_property $path startpoint] full_name]
  set endpoint_name [get_property [get_property $path endpoint] full_name]
  set slack [get_property $path slack]
  puts $csv_out [format "%s,%s,%.6f" $startpoint_name $endpoint_name $slack]
  lappend slacks $slack
}
close $csv_out
if {[llength $slacks] > 0} {
  puts [format "STA_WORST_SLACK_NS: %.6f" [lindex $slacks 0]]
catch {
set _same 0
set _cap "NA"
set _pc [lindex $paths 0]
set _sc [get_property $_pc startpoint_clock]
set _ec [get_property $_pc endpoint_clock]
if {$_ec ne "NULL"} {
  set _cap [format "%.6f" [get_property $_ec period]]
  if {$_sc ne "NULL" && $_sc eq $_ec} { set _same 1 }
}
puts "STA_WORST_CLOCKS: same_clock=$_same capture_period_ns=$_cap"
}
}
if {[llength $slacks] > 0} {
  report_checks -path_delay max -sort_by_slack -group_path_count 10 -slack_max [expr {[lindex $slacks end] + 1e-6}] > {<WORK>/reports/timing/overall.rpt}
} else {
  report_checks -path_delay max -sort_by_slack -group_path_count 10 > {<WORK>/reports/timing/overall.rpt}
}
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
    set _ssc 0
    if {![catch {set _sp [find_timing_paths -path_delay max -sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_sp] > 0} {
      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]
      catch {
set _same 0
set _cap "NA"
set _pc [lindex $_sp 0]
set _sc [get_property $_pc startpoint_clock]
set _ec [get_property $_pc endpoint_clock]
if {$_ec ne "NULL"} {
  set _cap [format "%.6f" [get_property $_ec period]]
  if {$_sc ne "NULL" && $_sc eq $_ec} { set _same 1 }
}
        set _ssc $_same
      }
    }
    set _whs "NA"
    if {![catch {set _hp [find_timing_paths -path_delay min -sort_by_slack -group_path_count 1 -to $_clk]}] && [llength $_hp] > 0} {
      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]
    }
    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s whs_ns=%s setup_same_clock=%s" $_cn $_per $_wns $_whs $_ssc]
  }
}
if {![catch {set _r2r [find_timing_paths -path_delay max -sort_by_slack -group_path_count 10 -from [all_registers] -to [all_registers]]}] && [llength $_r2r] > 0} {
  set _r2r_slacks {}
  foreach _p [lrange $_r2r 0 9] { lappend _r2r_slacks [get_property $_p slack] }
  puts [format "STA_REG2REG_SLACK_NS: %.6f" [lindex $_r2r_slacks 0]]
catch {
set _same 0
set _cap "NA"
set _pc [lindex $_r2r 0]
set _sc [get_property $_pc startpoint_clock]
set _ec [get_property $_pc endpoint_clock]
if {$_ec ne "NULL"} {
  set _cap [format "%.6f" [get_property $_ec period]]
  if {$_sc ne "NULL" && $_sc eq $_ec} { set _same 1 }
}
puts "STA_REG2REG_CLOCKS: same_clock=$_same capture_period_ns=$_cap"
}
  catch {report_checks -path_delay max -sort_by_slack -group_path_count 10 -from [all_registers] -to [all_registers] -format full -slack_max [expr {[lindex $_r2r_slacks end] + 1e-6}] > {<WORK>/reports/timing/reg2reg.rpt}}
}
if {![catch {with_output_to_variable _cc {check_setup -verbose -no_input_delay -no_output_delay -unconstrained_endpoints}}]} {
  set _ccf [open {<WORK>/reports/timing/unconstrained.rpt} w]
  puts -nonewline $_ccf $_cc
  close $_ccf
  puts "STA_CONSTRAINT_CHECK: begin"
  foreach _l [split $_cc "\n"] {
    if {[string trim $_l] ne ""} { puts "STA_CONSTRAINT_CHECK: $_l" }
  }
  puts "STA_CONSTRAINT_CHECK: end"
}

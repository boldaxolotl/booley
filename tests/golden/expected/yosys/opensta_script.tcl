read_liberty {/opt/pdk/NangateOpenCellLibrary_typical.lib}
read_verilog {<WORK>/sta_dut.v}
link_design dut
read_sdc {<WORK>/sta_constraints.sdc}
catch { foreach_in_collection _clk [all_clocks] { puts [format "STA_CLOCK_PERIOD_NS: %.6f" [get_property $_clk period]] ; break } }
report_checks -path_delay max -sort_by_slack -group_count 1 > {<WORK>/reports/timing/overall.rpt}
set paths [find_timing_paths -path_delay max -sort_by_slack -group_count 1]
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
    if {![catch {set _sp [find_timing_paths -path_delay max -sort_by_slack -group_count 1 -to $_clk]}] && [llength $_sp] > 0} {
      set _wns [format "%.6f" [get_property [lindex $_sp 0] slack]]
    }
    set _whs "NA"
    if {![catch {set _hp [find_timing_paths -path_delay min -sort_by_slack -group_count 1 -to $_clk]}] && [llength $_hp] > 0} {
      set _whs [format "%.6f" [get_property [lindex $_hp 0] slack]]
    }
    puts [format "STA_PERCLOCK: name=%s period_ns=%.6f wns_ns=%s whs_ns=%s" $_cn $_per $_wns $_whs]
  }
}
if {![catch {set _r2r [find_timing_paths -path_delay max -sort_by_slack -group_count 1 -from [all_registers] -to [all_registers]]}] && [llength $_r2r] > 0} {
  foreach _p $_r2r {
    puts [format "STA_REG2REG_SLACK_NS: %.6f" [get_property $_p slack]]
    break
  }
  catch {report_checks -path_delay max -sort_by_slack -group_count 1 -from [all_registers] -to [all_registers] -format full > {<WORK>/reports/timing/reg2reg.rpt}}
}
exit

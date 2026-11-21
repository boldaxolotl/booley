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
}

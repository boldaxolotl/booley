if {![catch {set _r2r [find_timing_paths -path_delay max -sort_by_slack -group_path_count 10 -from [all_registers] -to [all_registers]]}] && [llength $_r2r] > 0} {
  set _r2r_slacks {}
  foreach _p [lrange $_r2r 0 9] { lappend _r2r_slacks [get_property $_p slack] }
  puts [format "STA_REG2REG_SLACK_NS: %.6f" [lindex $_r2r_slacks 0]]
}

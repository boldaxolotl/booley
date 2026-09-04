if {![catch {set _r2r [find_timing_paths -path_delay max -sort_by_slack -group_path_count 1 -from [all_registers] -to [all_registers]]}] && [llength $_r2r] > 0} {
  foreach _p $_r2r {
    puts [format "STA_REG2REG_SLACK_NS: %.6f" [get_property $_p slack]]
    break
  }
}

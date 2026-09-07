if {$argc != 2} {
  error "usage: vivado -mode batch -source characterize.tcl -tclargs <profile> <output-root>"
}

set profile [lindex $argv 0]
set output_root [file normalize [lindex $argv 1]]
set fixture_root [file dirname [file normalize [info script]]]
if {[file exists $output_root]} {
  error "output root already exists: $output_root"
}
file mkdir $output_root

create_project -force "profile_$profile" $output_root -part xc7a35tcpg236-1
add_files -norecurse [file join $fixture_root top.sv]
add_files -fileset constrs_1 -norecurse [file join $fixture_root top.xdc]
set_property top top [get_filesets sources_1]

if {![regexp {SW Build ([0-9]+)} [version] _ vivado_build]} {
  error "could not parse Vivado build from: [version]"
}
puts "BOOLEY_VIVADO_VERSION=[version -short]"
puts "BOOLEY_VIVADO_BUILD=$vivado_build"
foreach strategy [lsort [list_property_value strategy [get_runs synth_1]]] {
  puts "BOOLEY_SYNTH_SUPPORTED=$strategy"
}
foreach strategy [lsort [list_property_value strategy [get_runs impl_1]]] {
  puts "BOOLEY_IMPL_SUPPORTED=$strategy"
}

# Strategy assignment resets some step properties. Keep it before Booley's
# out-of-context patch; the production adapter is required to use this order.
if {$profile eq "compact"} {
  set_property strategy Flow_AreaOptimized_high [get_runs synth_1]
  set_property strategy Area_Explore [get_runs impl_1]
} elseif {$profile eq "max_frequency"} {
  set_property strategy Flow_PerfOptimized_high [get_runs synth_1]
  set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]
} elseif {$profile ne "balanced"} {
  error "unknown profile $profile"
}
set_property -name {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} \
  -value {-mode out_of_context} -objects [get_runs synth_1]

puts "BOOLEY_PROFILE=$profile"
puts "BOOLEY_SYNTH_STRATEGY=[get_property strategy [get_runs synth_1]]"
puts "BOOLEY_IMPL_STRATEGY=[get_property strategy [get_runs impl_1]]"
puts "BOOLEY_SYNTH_MORE_OPTIONS=[get_property {STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS} [get_runs synth_1]]"
launch_runs synth_1 -jobs 4
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
  error "synthesis failed: [get_property STATUS [get_runs synth_1]]"
}

set final_step route_design
if {[get_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED [get_runs impl_1]]} {
  set final_step "phys_opt_design (Post-Route)"
}
launch_runs impl_1 -to_step $final_step -jobs 4
wait_on_run impl_1
set implementation_status [get_property STATUS [get_runs impl_1]]
if {![string match "*$final_step Complete*" $implementation_status]} {
  error "implementation failed: $implementation_status"
}

open_run impl_1
report_utilization -file [file join $output_root utilization.rpt]
report_timing_summary -file [file join $output_root timing.rpt]
set setup_paths [get_timing_paths -max_paths 1 -setup]
set hold_paths [get_timing_paths -max_paths 1 -hold]
puts "BOOLEY_WNS_NS=[get_property SLACK $setup_paths]"
puts "BOOLEY_WHS_NS=[get_property SLACK $hold_paths]"
puts "BOOLEY_STATUS=$implementation_status"
close_project
exit

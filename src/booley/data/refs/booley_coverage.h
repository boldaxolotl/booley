#pragma once

// Custom Verilator mains call these at the Coverage Window boundaries declared
// in flow_options.booley.coverage.custom_main_hooks.
extern "C" void booley_coverage_start();
extern "C" void booley_coverage_write();

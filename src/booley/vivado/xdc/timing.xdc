# Default timing constraints for Vivado FPGA implementation (non-project mode)
# Clock: 100 MHz (10 ns period) — override via --clk-period CLI flag
create_clock -name clk -period 10.000 [get_ports clk]
set_clock_uncertainty 0.100 [get_clocks clk]

# Reset is asynchronous — exclude from timing analysis
set_false_path -from [get_ports reset_n]

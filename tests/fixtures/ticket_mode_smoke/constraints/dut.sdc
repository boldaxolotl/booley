create_clock -name clk_i -period 20.0 [get_ports clk_i]
set_input_delay -clock clk_i 0.0 [get_ports {rst_ni enable_i}]
set_output_delay -clock clk_i 0.0 [get_ports count_o]

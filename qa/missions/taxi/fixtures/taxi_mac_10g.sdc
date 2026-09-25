create_clock -name rx_clk         -period 6.4 [get_ports rx_clk]
create_clock -name tx_clk         -period 6.4 [get_ports tx_clk]
create_clock -name stat_clk       -period 6.4 [get_ports stat_clk]
create_clock -name ptp_clk        -period 6.4 [get_ports ptp_clk]
create_clock -name ptp_sample_clk -period 8.0 [get_ports ptp_sample_clk]
# Fixture-only zero external delay, deliberately conservative across clocks.
# Exclude clock ports from external data-input delay application.
set qa_data_inputs [remove_from_collection [all_inputs] \
    [get_ports {rx_clk tx_clk stat_clk ptp_clk ptp_sample_clk}]]
foreach qa_clock_name {rx_clk tx_clk stat_clk ptp_clk ptp_sample_clk} {
    set_input_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 $qa_data_inputs
    set_output_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 [all_outputs]
}

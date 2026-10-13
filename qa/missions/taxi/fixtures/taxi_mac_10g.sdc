create_clock -name rx_clk         -period 6.4 [get_ports rx_clk]
create_clock -name tx_clk         -period 6.4 [get_ports tx_clk]
create_clock -name stat_clk       -period 6.4 [get_ports stat_clk]
create_clock -name ptp_clk        -period 6.4 [get_ports ptp_clk]
create_clock -name ptp_sample_clk -period 8.0 [get_ports ptp_sample_clk]
# Fixture-only zero external delay, deliberately conservative across clocks.
# Exclude clock ports from external data-input delay application.
# Plain Tcl list filtering: OpenSTA has no remove_from_collection.
set qa_clock_port_names {rx_clk tx_clk stat_clk ptp_clk ptp_sample_clk}
set qa_data_inputs {}
foreach qa_port [all_inputs] {
    if {[lsearch -exact $qa_clock_port_names [get_full_name $qa_port]] < 0} {
        lappend qa_data_inputs $qa_port
    }
}
foreach qa_clock_name $qa_clock_port_names {
    set_input_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 $qa_data_inputs
    set_output_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 [all_outputs]
}

// Interface only: no implementation or test logic.
module qa_uart (
    input logic clk_i, rst_ni,
    input logic req_valid_i,
    output logic req_ready_o,
    input logic req_write_i,
    input logic [31:0] req_addr_i, req_wdata_i,
    input logic [3:0] req_wstrb_i,
    output logic rsp_valid_o,
    input logic rsp_ready_i,
    output logic [31:0] rsp_rdata_o,
    output logic rsp_error_o,
    input logic rx_i,
    output logic tx_o,
    output logic [8:0] irq_o
);
endmodule

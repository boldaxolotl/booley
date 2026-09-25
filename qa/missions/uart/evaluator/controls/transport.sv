// Operator-owned transport/serial control. This is not a UART reference model.
module qa_uart (
    input wire clk_i, rst_ni, req_valid_i,
    output wire req_ready_o,
    input wire req_write_i,
    input wire [31:0] req_addr_i, req_wdata_i,
    input wire [3:0] req_wstrb_i,
    output reg rsp_valid_o,
    input wire rsp_ready_i,
    output reg [31:0] rsp_rdata_o,
    output reg rsp_error_o,
    input wire rx_i,
    output reg tx_o,
    output wire [8:0] irq_o
);
localparam CORRUPT_MMIO = 0;
localparam CORRUPT_SERIAL = 0;
reg [31:0] control;
reg [7:0] payload;
reg pending, active;
reg [9:0] frame;
integer remaining, bit_index, lane;
assign req_ready_o = !rsp_valid_o;
assign irq_o = 0;
always @(posedge clk_i) begin
    if (!rst_ni) begin
        control <= 0;
        payload <= 0;
        pending <= 0;
        active <= 0;
        frame <= 10'h3ff;
        remaining <= 0;
        bit_index <= 0;
        rsp_valid_o <= 0;
        rsp_rdata_o <= 0;
        rsp_error_o <= 0;
        tx_o <= 1;
    end else begin
        if (rsp_valid_o && rsp_ready_i) rsp_valid_o <= 0;
        if (req_valid_i && req_ready_o) begin
            rsp_valid_o <= 1;
            rsp_error_o <= 0;
            rsp_rdata_o <= 0;
            if (req_addr_i == 32'h10) begin
                if (req_write_i) begin
                    for (lane = 0; lane < 4; lane = lane + 1)
                        if (req_wstrb_i[lane]) control[lane*8 +: 8] <= req_wdata_i[lane*8 +: 8];
                end else rsp_rdata_o <= control ^ CORRUPT_MMIO;
            end else if (req_addr_i == 32'h1c && req_write_i && req_wstrb_i[0]) begin
                payload <= req_wdata_i[7:0];
                pending <= 1;
            end
        end
        if (!active && pending && control[0]) begin
            frame <= {1'b1, payload ^ CORRUPT_SERIAL[7:0], 1'b0};
            tx_o <= 0;
            remaining <= 64;
            bit_index <= 0;
            active <= 1;
            pending <= 0;
        end else if (active) begin
            if (remaining == 1) begin
                if (bit_index == 9) begin
                    tx_o <= 1;
                    active <= 0;
                end else begin
                    bit_index <= bit_index + 1;
                    tx_o <= frame[bit_index+1];
                    remaining <= 64;
                end
            end else remaining <= remaining - 1;
        end
    end
end
endmodule

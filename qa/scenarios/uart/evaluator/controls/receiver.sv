// Operator-owned observation control, independently written from the public
// interface. Supports exact-a RX, parity, RX FIFO, IRQ injection and VAL only.
// It is deliberately not a complete candidate UART implementation.
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
    output wire tx_o,
    output wire [8:0] irq_o
);
localparam CORRUPT_RX = 0;
localparam CORRUPT_DEPTH = 0;
localparam CORRUPT_WATERMARK = 0;
localparam CORRUPT_IRQ = 0;
localparam CORRUPT_HISTORY = 0;
reg [31:0] control;
reg [8:0] enabled, events, force_level;
reg [2:0] watermark;
reg [7:0] fifo [0:63];
reg [7:0] received;
reg [15:0] history;
reg [2:0] filter_samples;
reg filtered;
integer depth, head, tail, phase, state, countdown, bit_index, lane;
wire sample_rx = control[2] ? filtered : rx_i;
wire [6:0] threshold = watermark == 6 ? 62 : (7'd1 << watermark);
wire [8:0] levels = {7'b0, ((depth >= threshold) ^ CORRUPT_WATERMARK[0]), 1'b0};
assign irq_o = (events | levels | force_level) & enabled & (CORRUPT_IRQ ? 9'h000 : 9'h1ff);
assign tx_o = 1;
assign req_ready_o = !rsp_valid_o;
always @(posedge clk_i) begin
    if (!rst_ni) begin
        control <= 0; enabled <= 0; events <= 0; force_level <= 0;
        watermark <= 0; received <= 0; history <= 0;
        filter_samples <= 7; filtered <= 1;
        depth = 0; head = 0; tail = 0;
        phase <= 0; state <= 0; countdown <= 0; bit_index <= 0;
        rsp_valid_o <= 0; rsp_rdata_o <= 0; rsp_error_o <= 0;
    end else begin
        force_level <= 0;
        filter_samples <= {filter_samples[1:0], rx_i};
        if (&filter_samples) filtered <= 1;
        else if (~|filter_samples) filtered <= 0;
        // Independent divide-by-four sampler for the exact-a control contract.
        phase <= (phase + 1) % 4;
        if (phase == 3) history <= {history[14:0], rx_i};
        if (rsp_valid_o && rsp_ready_i) rsp_valid_o <= 0;
        if (req_valid_i && req_ready_o) begin
            rsp_valid_o <= 1; rsp_rdata_o <= 0; rsp_error_o <= 0;
            if (req_write_i) begin
                case (req_addr_i)
                    0: if (req_wstrb_i[0]) events <= events & ~req_wdata_i[8:0];
                    4: begin
                        if (req_wstrb_i[0]) enabled[7:0] <= req_wdata_i[7:0];
                        if (req_wstrb_i[1]) enabled[8] <= req_wdata_i[8];
                    end
                    8: if (req_wstrb_i[0]) begin
                        events <= events | (req_wdata_i[8:0] & 9'h0fc);
                        force_level <= req_wdata_i[8:0] & 9'h103;
                    end
                    32'h10: for (lane=0; lane<4; lane=lane+1)
                        if (req_wstrb_i[lane]) control[lane*8 +: 8] <= req_wdata_i[lane*8 +: 8];
                    32'h20: if (req_wstrb_i[0]) begin
                        watermark <= req_wdata_i[4:2];
                        if (req_wdata_i[0]) begin depth = 0; head = 0; tail = 0; end
                    end
                endcase
            end else begin
                case (req_addr_i)
                    0: rsp_rdata_o <= events | levels | force_level;
                    4: rsp_rdata_o <= enabled;
                    32'h10: rsp_rdata_o <= control;
                    32'h14: rsp_rdata_o <= 32'h14 | ((depth == 64) << 1) | ((depth == 0) << 5);
                    32'h18: if (depth > 0) begin
                        rsp_rdata_o <= fifo[head] ^ CORRUPT_RX;
                        head = (head + 1) % 64; depth = depth - 1;
                    end
                    32'h24: rsp_rdata_o <= (depth ^ CORRUPT_DEPTH) << 16;
                    32'h2c: rsp_rdata_o <= history ^ CORRUPT_HISTORY;
                endcase
            end
        end
        if (!control[1]) state <= 0;
        else if (state == 0) begin
            if (!sample_rx) begin state <= 1; countdown <= 31; end
        end else if (countdown != 0) countdown <= countdown - 1;
        else case (state)
            1: if (sample_rx) state <= 0;
               else begin state <= 2; countdown <= 63; bit_index <= 0; end
            2: begin
                received[bit_index] <= sample_rx; countdown <= 63;
                if (bit_index == 7) state <= control[6] ? 3 : 4;
                else bit_index <= bit_index + 1;
            end
            3: begin
                if (sample_rx != ((^received) ^ control[7])) events[7] <= 1;
                state <= 4; countdown <= 63;
            end
            4: begin
                if (!sample_rx) events[4] <= 1;
                else if (depth == 64) events[3] <= 1;
                else begin fifo[tail] <= received; tail = (tail+1)%64; depth = depth+1; end
                state <= 0;
            end
        endcase
    end
end
endmodule

// Operator-owned peripheral control, independently written from the public
// interface. It implements only the behavior needed to qualify evaluator
// observation paths and is not a candidate UART or reference implementation.
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
localparam CORRUPT_RATE = 0;
localparam CORRUPT_PARITY_ERROR = 0;
localparam CORRUPT_OVERFLOW = 0;
localparam CORRUPT_BREAK = 0;
localparam CORRUPT_WATERMARK = 0;
localparam CORRUPT_LEVEL_INJECTION = 0;
localparam CORRUPT_INVALID = 0;
localparam CORRUPT_LOOP = 0;
localparam CORRUPT_OVERRIDE = 0;
localparam CORRUPT_RESET = 0;

reg [31:0] control, timeout_control;
reg [7:0] fifo_control;
reg [1:0] override_control;
reg [8:0] enabled, events, force_level;
reg [7:0] rx_fifo [0:63];
reg [7:0] tx_fifo [0:31];
reg [7:0] received;
reg [15:0] history;
reg [2:0] filter_samples;
reg filtered, frame_bad, break_armed;
reg initialized = 0;
reg tx_active;
reg [9:0] tx_frame;
reg [16:0] rx_accumulator;
integer rx_depth, rx_head, rx_tail;
integer tx_depth, tx_head, tx_tail;
integer rx_state, rx_ticks, rx_bit;
integer tx_remaining, tx_bit;
integer low_clocks, high_clocks, lane;
reg tx_serial;

wire [15:0] selected_nco = control[31:16];
wire [15:0] receive_nco = CORRUPT_RATE ? selected_nco + 16'h1000 : selected_nco;
wire [16:0] rx_sum = rx_accumulator + receive_nco;
wire sample_rx = control[2] ? filtered : rx_i;
wire [6:0] rx_threshold = fifo_control[4:2] == 6 ? 62 : (7'd1 << fifo_control[4:2]);
wire [5:0] tx_threshold = 6'd1 << fifo_control[7:5];
wire [8:0] levels = {
    tx_depth == 0,
    6'b0,
    rx_depth >= rx_threshold,
    tx_depth < tx_threshold
};
wire [8:0] visible_levels = CORRUPT_WATERMARK ? levels ^ 9'h103 : levels;
wire loop_tx = CORRUPT_LOOP ? 1'b1 : rx_i;
wire override_tx = CORRUPT_OVERRIDE ? ~override_control[1] : override_control[1];
assign tx_o = override_control[0] ? override_tx : (control[5] ? loop_tx : tx_serial);
assign irq_o = (events | visible_levels | force_level) & enabled &
    (CORRUPT_LEVEL_INJECTION ? 9'h0fc : 9'h1ff);
assign req_ready_o = !rsp_valid_o;

function integer bit_period;
    input [15:0] nco;
    begin
        if (nco == 16'h2000) bit_period = 128;
        else if (nco == 16'h3000) bit_period = 86;
        else bit_period = 64;
    end
endfunction

function integer break_limit;
    input [1:0] encoding;
    input parity_enabled;
    integer characters;
    begin
        characters = 2 << encoding;
        break_limit = characters * (parity_enabled ? 11 : 10) * bit_period(selected_nco);
        if (CORRUPT_BREAK) break_limit = break_limit - 256;
    end
endfunction

task enqueue_rx;
    input [7:0] value;
    begin
        if (rx_depth == 64) begin
            if (!CORRUPT_OVERFLOW) events[3] <= 1;
        end else begin
            rx_fifo[rx_tail] = value;
            rx_tail = (rx_tail + 1) % 64;
            rx_depth = rx_depth + 1;
        end
    end
endtask

always @(posedge clk_i) begin
    if (!rst_ni) begin
        if (!initialized || !CORRUPT_RESET) begin
            initialized <= 1;
            control <= 0;
            timeout_control <= 0;
            fifo_control <= 0;
            override_control <= 0;
            enabled <= 0;
            events <= 0;
            rx_depth = 0;
            rx_head = 0;
            rx_tail = 0;
            tx_depth = 0;
            tx_head = 0;
            tx_tail = 0;
        end
        force_level <= 0;
        received <= 0;
        history <= 0;
        filter_samples <= 7;
        filtered <= 1;
        frame_bad <= 0;
        break_armed <= 1;
        tx_active <= 0;
        tx_frame <= 10'h3ff;
        tx_serial <= 1;
        rx_accumulator <= 0;
        rx_state = 0;
        rx_ticks = 0;
        rx_bit = 0;
        tx_remaining = 0;
        tx_bit = 0;
        low_clocks = 0;
        high_clocks = 0;
        rsp_valid_o <= 0;
        rsp_rdata_o <= 0;
        rsp_error_o <= 0;
    end else begin
        force_level <= 0;
        filter_samples <= {filter_samples[1:0], rx_i};
        if (&filter_samples) filtered <= 1;
        else if (~|filter_samples) filtered <= 0;
        history <= {history[14:0], rx_i};

        if (!rx_i && control[1]) begin
            low_clocks = low_clocks + 1;
            high_clocks = 0;
            if (break_armed && low_clocks > break_limit(control[9:8], control[6])) begin
                events[5] <= 1;
                break_armed <= 0;
                rx_state = 0;
                rx_ticks = 0;
            end
        end else begin
            low_clocks = 0;
            high_clocks = high_clocks + 1;
            if (high_clocks >= (bit_period(selected_nco) / 2)) break_armed <= 1;
        end

        if (rsp_valid_o && rsp_ready_i) rsp_valid_o <= 0;
        if (req_valid_i && req_ready_o) begin
            rsp_valid_o <= 1;
            rsp_rdata_o <= 0;
            rsp_error_o <= 0;
            case (req_addr_i)
                32'h00: if (req_write_i) begin
                    if (req_wstrb_i[0]) events <= events & ~req_wdata_i[8:0];
                end else rsp_rdata_o <= events | visible_levels | force_level;
                32'h04: if (req_write_i) begin
                    if (req_wstrb_i[0]) enabled[7:0] <= req_wdata_i[7:0];
                    if (req_wstrb_i[1]) enabled[8] <= req_wdata_i[8];
                end else rsp_rdata_o <= enabled;
                32'h08: if (req_write_i && req_wstrb_i[0]) begin
                    events <= events | (req_wdata_i[8:0] & 9'h0fc);
                    if (!CORRUPT_LEVEL_INJECTION) force_level <= req_wdata_i[8:0] & 9'h103;
                end
                32'h0c: rsp_rdata_o <= 0;
                32'h10: if (req_write_i) begin
                    for (lane = 0; lane < 4; lane = lane + 1)
                        if (req_wstrb_i[lane]) control[lane*8 +: 8] <= req_wdata_i[lane*8 +: 8];
                    rx_state = 0;
                    rx_ticks = 0;
                    rx_accumulator <= 0;
                end else rsp_rdata_o <= control;
                32'h14: rsp_rdata_o <=
                    (tx_depth == 32) | ((rx_depth == 64) << 1) |
                    ((tx_depth == 0) << 2) | ((!tx_active && tx_depth == 0) << 3) |
                    ((rx_state == 0) << 4) | ((rx_depth == 0) << 5);
                32'h18: if (!req_write_i && rx_depth > 0) begin
                    rsp_rdata_o <= rx_fifo[rx_head];
                    rx_head = (rx_head + 1) % 64;
                    rx_depth = rx_depth - 1;
                end
                32'h1c: if (req_write_i && req_wstrb_i[0] && tx_depth < 32) begin
                    if (control[4] && !CORRUPT_LOOP) enqueue_rx(req_wdata_i[7:0]);
                    else begin
                        tx_fifo[tx_tail] = req_wdata_i[7:0];
                        tx_tail = (tx_tail + 1) % 32;
                        tx_depth = tx_depth + 1;
                    end
                end
                32'h20: if (req_write_i && req_wstrb_i[0]) begin
                    fifo_control <= req_wdata_i[7:0] & 8'hfc;
                    if (req_wdata_i[0]) begin
                        rx_depth = 0; rx_head = 0; rx_tail = 0;
                    end
                    if (req_wdata_i[1]) begin
                        tx_depth = 0; tx_head = 0; tx_tail = 0;
                    end
                end else rsp_rdata_o <= fifo_control;
                32'h24: rsp_rdata_o <= (rx_depth << 16) | tx_depth;
                32'h28: if (req_write_i && req_wstrb_i[0])
                    override_control <= req_wdata_i[1:0];
                else rsp_rdata_o <= override_control;
                32'h2c: rsp_rdata_o <= history;
                32'h30: if (req_write_i) begin
                    for (lane = 0; lane < 4; lane = lane + 1)
                        if (req_wstrb_i[lane]) timeout_control[lane*8 +: 8] <= req_wdata_i[lane*8 +: 8];
                end else rsp_rdata_o <= timeout_control;
                default: begin
                    rsp_rdata_o <= 0;
                    rsp_error_o <= !CORRUPT_INVALID;
                end
            endcase
        end

        if (!break_armed) begin
            rx_state = 0;
            rx_ticks = 0;
            rx_accumulator <= 0;
        end else if (control[5]) begin
            rx_state = 0;
            rx_accumulator <= 0;
        end else if (!control[1] || selected_nco == 0) begin
            rx_state = 0;
            rx_accumulator <= 0;
        end else if (rx_sum >= 65536) begin
            rx_accumulator <= rx_sum - 65536;
            if (rx_state == 0) begin
                if (!sample_rx) begin rx_state = 1; rx_ticks = 0; end
            end else begin
                rx_ticks = rx_ticks + 1;
                if (rx_state == 1 && rx_ticks == 8) begin
                    if (sample_rx) rx_state = 0;
                    else begin rx_state = 2; rx_ticks = 0; rx_bit = 0; frame_bad <= 0; end
                end else if (rx_state == 2 && rx_ticks == 16) begin
                    received[rx_bit] <= sample_rx;
                    rx_ticks = 0;
                    if (rx_bit == 7) rx_state = control[6] ? 3 : 4;
                    else rx_bit = rx_bit + 1;
                end else if (rx_state == 3 && rx_ticks == 16) begin
                    if (sample_rx != ((^received) ^ control[7])) begin
                        if (!CORRUPT_PARITY_ERROR) events[7] <= 1;
                        frame_bad <= 1;
                    end
                    rx_state = 4;
                    rx_ticks = 0;
                end else if (rx_state == 4 && rx_ticks == 16) begin
                    if (!sample_rx) events[4] <= 1;
                    else if (!frame_bad) enqueue_rx(received);
                    rx_state = 0;
                    rx_ticks = 0;
                end
            end
        end else rx_accumulator <= rx_sum;

        if (control[5] || override_control[0] || control[4] || !control[0]) begin
            tx_active <= 0;
            tx_serial <= 1;
        end else if (!tx_active && tx_depth > 0 && selected_nco != 0) begin
            tx_frame <= {1'b1, tx_fifo[tx_head], 1'b0};
            tx_head = (tx_head + 1) % 32;
            tx_depth = tx_depth - 1;
            tx_serial <= 0;
            tx_remaining = bit_period(selected_nco);
            tx_bit = 0;
            tx_active <= 1;
        end else if (tx_active) begin
            if (tx_remaining == 1) begin
                if (tx_bit == 9) begin
                    if (tx_depth > 0) begin
                        tx_frame <= {1'b1, tx_fifo[tx_head], 1'b0};
                        tx_head = (tx_head + 1) % 32;
                        tx_depth = tx_depth - 1;
                        tx_serial <= 0;
                        tx_remaining = bit_period(selected_nco);
                        tx_bit = 0;
                    end else begin
                        tx_serial <= 1;
                        tx_active <= 0;
                        events[2] <= 1;
                    end
                end else begin
                    tx_bit = tx_bit + 1;
                    tx_serial <= tx_frame[tx_bit];
                    tx_remaining = bit_period(selected_nco);
                end
            end else tx_remaining = tx_remaining - 1;
        end
    end
end
endmodule

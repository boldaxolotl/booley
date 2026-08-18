`timescale 1ns/1ps

module fsm_traffic (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       walk_request,
    output reg  [1:0] state,
    output wire       green,
    output wire       yellow,
    output wire       red,
    output wire       walk_light
);

    // State encoding
    localparam GREEN  = 2'd0;
    localparam YELLOW = 2'd1;
    localparam RED    = 2'd2;
    localparam WALK   = 2'd3;

    // State durations (cycles)
    localparam GREEN_DUR  = 8;
    localparam YELLOW_DUR = 3;
    localparam RED_DUR    = 6;
    localparam WALK_DUR   = 4;

    reg [3:0] timer;
    reg       walk_pending;

    // Output decode
    assign green      = (state == GREEN);
    assign yellow     = (state == YELLOW);
    assign red        = (state == RED);
    assign walk_light = (state == WALK);

    // Latch walk_request until serviced
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            walk_pending <= 1'b0;
        else if (state == WALK)
            walk_pending <= 1'b0;
        else if (walk_request)
            walk_pending <= 1'b1;
    end

    // FSM + timer
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= GREEN;
            timer <= 4'd0;
        end else begin
            case (state)
                GREEN: begin
                    if (timer == GREEN_DUR - 1) begin
                        state <= YELLOW;
                        timer <= 4'd0;
                    end else
                        timer <= timer + 4'd1;
                end
                YELLOW: begin
                    if (timer == YELLOW_DUR - 1) begin
                        state <= RED;
                        timer <= 4'd0;
                    end else
                        timer <= timer + 4'd1;
                end
                RED: begin
                    if (timer == RED_DUR - 1) begin
                        state <= walk_pending ? WALK : GREEN;
                        timer <= 4'd0;
                    end else
                        timer <= timer + 4'd1;
                end
                WALK: begin
                    if (timer == WALK_DUR - 1) begin
                        state <= GREEN;
                        timer <= 4'd0;
                    end else
                        timer <= timer + 4'd1;
                end
                default: begin
                    state <= GREEN;
                    timer <= 4'd0;
                end
            endcase
        end
    end

endmodule

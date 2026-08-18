`timescale 1ns/1ps

module arbiter (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [1:0] req,
    output reg  [1:0] grant,
    output wire       busy,
    input  wire       ack
);

    localparam IDLE    = 2'd0;
    localparam GRANT0  = 2'd1;
    localparam GRANT1  = 2'd2;

    reg [1:0] state;

    assign busy = (state != IDLE);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            grant <= 2'b00;
        end else begin
            case (state)
                IDLE: begin
                    if (req[0]) begin
                        // req[0] has priority
                        state <= GRANT0;
                        grant <= 2'b01;
                    end else if (req[1]) begin
                        state <= GRANT1;
                        grant <= 2'b10;
                    end else begin
                        grant <= 2'b00;
                    end
                end
                GRANT0: begin
                    if (ack) begin
                        state <= IDLE;
                        grant <= 2'b00;
                    end
                end
                GRANT1: begin
                    if (ack) begin
                        state <= IDLE;
                        grant <= 2'b00;
                    end
                end
                default: begin
                    state <= IDLE;
                    grant <= 2'b00;
                end
            endcase
        end
    end

endmodule

`timescale 1ns/1ps

module counter_4bit (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       enable,
    output reg  [3:0] count,
    output wire       carry_out
);

    assign carry_out = (count == 4'hF) & enable;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 4'b0;
        else if (enable)
            count <= count + 4'b1;
    end

endmodule

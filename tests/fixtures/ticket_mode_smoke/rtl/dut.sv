`timescale 1ns/1ps

module dut (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       enable_i,
    output logic [3:0] count_o
);
    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            count_o <= '0;
        end else if (enable_i) begin
            count_o <= count_o + 1'b1;
        end
    end
endmodule

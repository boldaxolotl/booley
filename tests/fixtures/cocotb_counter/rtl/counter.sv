`timescale 1ns/1ps
// E2E fixture DUT (plan-cocotb-support Part G): 8-bit counter with an RTL
// $error trap at 8'hFD and a $fatal trap at 8'hFE so the crash-shape e2e
// (G11) can fire real RTL assertions under a cocotb testbench.
module counter #(
    parameter int WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             en,
    output logic [WIDTH-1:0] count
);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) count <= '0;
        else if (en) count <= count + 1'b1;
    end

    always_ff @(posedge clk) begin
        if (count == 8'hFD) $error("counter reached error trap value FD");
        if (count == 8'hFE) $fatal(1, "counter reached fatal trap value FE");
    end
endmodule

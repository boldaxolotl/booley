`timescale 1ns/1ps

module tb_dut;
    logic       clk_i = 1'b0;
    logic       rst_ni = 1'b0;
    logic       enable_i = 1'b0;
    logic [3:0] count_o;

    dut u_dut (.*);

    always #5 clk_i = ~clk_i;

    initial begin
        repeat (2) @(posedge clk_i);
        @(negedge clk_i);
        rst_ni = 1'b1;
        enable_i = 1'b1;
        repeat (4) @(posedge clk_i);
        @(negedge clk_i);
        if (count_o !== 4'd4) begin
            $display("[SIM_RESULT] FAILED: count_o=%0d", count_o);
            $finish;
        end
        $display("[SIM_RESULT] PASSED");
        $finish;
    end
endmodule

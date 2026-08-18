`timescale 1ns/1ps

module lfsr_8bit_tb;

    reg        clk;
    reg        rst_n;
    reg        enable;
    wire [7:0] lfsr_out;
    wire       bit_out;

    lfsr_8bit dut (
        .clk      (clk),
        .rst_n    (rst_n),
        .enable   (enable),
        .lfsr_out (lfsr_out),
        .bit_out  (bit_out)
    );

    // 10 ns clock period
    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        $dumpfile("trace.vcd");
        $dumpvars(0, lfsr_8bit_tb);

        // --- Reset phase: 3 cycles (seeds to 8'hA5) ---
        rst_n  = 0;
        enable = 0;
        @(posedge clk); // cycle 1
        @(posedge clk); // cycle 2
        @(posedge clk); // cycle 3
        rst_n = 1;

        // --- Enable LFSR for 50 cycles ---
        enable = 1;
        repeat (50) @(posedge clk); // cycles 4-53

        @(posedge clk);
        $finish;
    end

endmodule

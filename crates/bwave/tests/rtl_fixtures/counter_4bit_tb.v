`timescale 1ns/1ps

module counter_4bit_tb;

    reg        clk;
    reg        rst_n;
    reg        enable;
    wire [3:0] count;
    wire       carry_out;

    counter_4bit dut (
        .clk       (clk),
        .rst_n     (rst_n),
        .enable    (enable),
        .count     (count),
        .carry_out (carry_out)
    );

    // 10 ns clock period
    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        $dumpfile("trace.vcd");
        $dumpvars(0, counter_4bit_tb);

        // --- Reset phase: 3 cycles ---
        rst_n  = 0;
        enable = 0;
        @(posedge clk); // cycle 1
        @(posedge clk); // cycle 2
        @(posedge clk); // cycle 3
        rst_n = 1;

        // --- Enable counting: 12 cycles ---
        enable = 1;
        repeat (12) @(posedge clk); // cycles 4-15

        // --- Disable for 5 cycles ---
        enable = 0;
        repeat (5) @(posedge clk); // cycles 16-20

        // --- Re-enable for remaining 18 cycles ---
        enable = 1;
        repeat (18) @(posedge clk); // cycles 21-38

        @(posedge clk);
        $finish;
    end

endmodule

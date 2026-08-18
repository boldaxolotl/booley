`timescale 1ns/1ps

module arbiter_tb;

    reg        clk;
    reg        rst_n;
    reg  [1:0] req;
    wire [1:0] grant;
    wire       busy;
    reg        ack;

    arbiter dut (
        .clk   (clk),
        .rst_n (rst_n),
        .req   (req),
        .grant (grant),
        .busy  (busy),
        .ack   (ack)
    );

    // 10 ns clock period
    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        $dumpfile("trace.vcd");
        $dumpvars(0, arbiter_tb);

        // --- Init ---
        rst_n = 0;
        req   = 2'b00;
        ack   = 0;

        // --- Reset: 3 cycles ---
        @(posedge clk); // cycle 1
        @(posedge clk); // cycle 2
        @(posedge clk); // cycle 3
        rst_n = 1;

        // --- Test 1: single req[0] ---
        @(posedge clk); req = 2'b01;  // cycle 4: assert req[0]
        @(posedge clk); req = 2'b00;  // cycle 5: drop request (grant held)
        @(posedge clk);               // cycle 6: grant[0] still active
        @(posedge clk); ack = 1;      // cycle 7: acknowledge
        @(posedge clk); ack = 0;      // cycle 8: back to idle

        // --- Idle gap ---
        repeat (2) @(posedge clk);    // cycles 9-10

        // --- Test 2: single req[1] ---
        @(posedge clk); req = 2'b10;  // cycle 11: assert req[1]
        @(posedge clk); req = 2'b00;  // cycle 12: drop request
        @(posedge clk); ack = 1;      // cycle 13: acknowledge
        @(posedge clk); ack = 0;      // cycle 14: back to idle

        // --- Idle gap ---
        repeat (2) @(posedge clk);    // cycles 15-16

        // --- Test 3: simultaneous req[0] and req[1] -> req[0] wins ---
        @(posedge clk); req = 2'b11;  // cycle 17: both request
        @(posedge clk); req = 2'b10;  // cycle 18: drop req[0], keep req[1]
        @(posedge clk);               // cycle 19: grant[0] still active
        @(posedge clk); ack = 1;      // cycle 20: ack grant[0]
        @(posedge clk); ack = 0;      // cycle 21: idle, req[1] pending

        // --- req[1] should now get its turn ---
        @(posedge clk); req = 2'b10;  // cycle 22: re-assert req[1]
        @(posedge clk); req = 2'b00;  // cycle 23: drop request
        @(posedge clk); ack = 1;      // cycle 24: ack grant[1]
        @(posedge clk); ack = 0;      // cycle 25: back to idle

        // --- Idle tail ---
        repeat (5) @(posedge clk);    // cycles 26-30

        @(posedge clk);
        $finish;
    end

endmodule

`timescale 1ns/1ps

module sync_fifo_tb;

    reg        clk;
    reg        rst_n;
    reg        wr_en;
    reg        rd_en;
    reg  [7:0] wr_data;
    wire [7:0] rd_data;
    wire       full;
    wire       empty;
    wire [2:0] count;

    sync_fifo dut (
        .clk     (clk),
        .rst_n   (rst_n),
        .wr_en   (wr_en),
        .rd_en   (rd_en),
        .wr_data (wr_data),
        .rd_data (rd_data),
        .full    (full),
        .empty   (empty),
        .count   (count)
    );

    // 10 ns clock period
    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        $dumpfile("trace.vcd");
        $dumpvars(0, sync_fifo_tb);

        // --- Init ---
        rst_n   = 0;
        wr_en   = 0;
        rd_en   = 0;
        wr_data = 8'd0;

        // --- Reset: 3 cycles ---
        @(posedge clk); // cycle 1
        @(posedge clk); // cycle 2
        @(posedge clk); // cycle 3
        rst_n = 1;

        // --- Write 4 items (fills FIFO) ---
        @(posedge clk); wr_en = 1; wr_data = 8'hAA; // cycle 4: write 0xAA
        @(posedge clk); wr_data = 8'hBB;             // cycle 5: write 0xBB
        @(posedge clk); wr_data = 8'hCC;             // cycle 6: write 0xCC
        @(posedge clk); wr_data = 8'hDD;             // cycle 7: write 0xDD
        @(posedge clk); wr_en = 0;                   // cycle 8: stop writing

        // --- Read 2 items ---
        @(posedge clk); rd_en = 1;                   // cycle 9: read (0xAA)
        @(posedge clk);                              // cycle 10: read (0xBB)
        @(posedge clk); rd_en = 0;                   // cycle 11: stop reading

        // --- Write 2 more items ---
        @(posedge clk); wr_en = 1; wr_data = 8'hEE; // cycle 12: write 0xEE
        @(posedge clk); wr_data = 8'hFF;             // cycle 13: write 0xFF
        @(posedge clk); wr_en = 0;                   // cycle 14: stop writing

        // --- Read all remaining (4 items) ---
        @(posedge clk); rd_en = 1;                   // cycle 15: read (0xCC)
        @(posedge clk);                              // cycle 16: read (0xDD)
        @(posedge clk);                              // cycle 17: read (0xEE)
        @(posedge clk);                              // cycle 18: read (0xFF)
        @(posedge clk); rd_en = 0;                   // cycle 19: stop reading

        // --- Idle for 5 cycles ---
        repeat (5) @(posedge clk);                   // cycles 20-24

        @(posedge clk);
        $finish;
    end

endmodule

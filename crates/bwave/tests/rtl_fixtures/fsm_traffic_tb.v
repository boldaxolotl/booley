`timescale 1ns/1ps

module fsm_traffic_tb;

    reg        clk;
    reg        rst_n;
    reg        walk_request;
    wire [1:0] state;
    wire       green;
    wire       yellow;
    wire       red;
    wire       walk_light;

    fsm_traffic dut (
        .clk          (clk),
        .rst_n        (rst_n),
        .walk_request (walk_request),
        .state        (state),
        .green        (green),
        .yellow       (yellow),
        .red          (red),
        .walk_light   (walk_light)
    );

    // 10 ns clock period
    initial clk = 0;
    always #5 clk = ~clk;

    integer cycle;

    initial begin
        $dumpfile("trace.vcd");
        $dumpvars(0, fsm_traffic_tb);

        // --- Reset phase: 3 cycles ---
        rst_n        = 0;
        walk_request = 0;
        cycle        = 0;
        repeat (3) begin
            @(posedge clk);
            cycle = cycle + 1;
        end
        rst_n = 1;

        // --- Run until cycle 20, then assert walk_request ---
        while (cycle < 20) begin
            @(posedge clk);
            cycle = cycle + 1;
        end

        // Cycle 20: assert walk_request
        walk_request = 1;

        while (cycle < 25) begin
            @(posedge clk);
            cycle = cycle + 1;
        end

        // Cycle 25: deassert walk_request
        walk_request = 0;

        // Run remaining cycles to 63
        while (cycle < 63) begin
            @(posedge clk);
            cycle = cycle + 1;
        end

        @(posedge clk);
        $finish;
    end

endmodule

// Classic testbench for the mutation QA fixture. `+test=<name>` picks the test:
//   full   exhaustive sweeps checked against a reference written here
//   smoke  drives inputs and reads outputs, checks nothing but the handshake
//   fail   always fails (registered only by faults/tests-baseline-fail.toml)
// Every test first waits for `ack`. The clock runs freely and there is no
// watchdog, so a design that never answers hangs until Booley's time limit.
module mutation_tb;
  logic clk = 0;
  logic rst = 1;
  logic [3:0] a = 0, b = 0, sh_d = 0, v = 0;
  logic [1:0] op = 0, sh_n = 0;
  logic en = 0, req = 0;
  logic [7:0] wide = 0;
  wire [3:0] y, sh_y, q, low;
  wire lt, eq, busy, hi, r, ack;
  string test_name;

  mutation_top dut (
      .clk(clk), .rst(rst), .a(a), .b(b), .op(op), .y(y), .lt(lt), .eq(eq),
      .sh_d(sh_d), .sh_n(sh_n), .sh_y(sh_y), .en(en), .q(q), .v(v), .busy(busy),
      .wide(wide), .hi(hi), .low(low), .r(r), .req(req), .ack(ack)
  );

  always #1 clk = ~clk;

  // Report one mismatch and stop with a non-zero exit code.
  task automatic check(input bit ok, input string what);
    if (!ok) begin
      $display("[SIM_RESULT] FAILED");
      $fatal(1, "mismatch: %s", what);
    end
  endtask

  initial begin
    if (!$value$plusargs("test=%s", test_name)) test_name = "full";
    // Handshake first: waits forever when `ack` never rises.
    req = 1;
    while (ack !== 1'b1) @(posedge clk);
    req = 0;
    while (ack !== 1'b0) @(posedge clk);

    if (test_name == "full") begin
      for (int i = 0; i < 1024; i++) begin
        {op, a, b} = 10'(i);
        #1;
        check(y === (op[0] ? (a & b) : 4'(a + b)), "alu y");
        check(lt === (a < b), "alu lt");
        check(eq === (a == b), "alu eq");
      end
      for (int i = 0; i < 64; i++) begin
        {sh_n, sh_d} = 6'(i);
        #1;
        check(sh_y === 4'(sh_d << sh_n), "alu_shift y");
      end
      // Counter: reset value, hold while disabled, count through the wrap.
      repeat (2) @(negedge clk);
      rst = 0;
      check(q === 4'd2, "counter reset value");
      repeat (3) @(negedge clk);
      check(q === 4'd2, "counter holds while disabled");
      en = 1;
      for (int i = 1; i <= 18; i++) begin
        @(negedge clk);
        check(q === 4'(2 + i), "counter counts");
      end
      en = 0;
      repeat (2) @(negedge clk);
      check(q === 4'd4, "counter holds after counting");
      for (int i = 0; i < 256; i++) begin
        wide = 8'(i);
        @(negedge clk);
        check(hi === wide[7], "decoys hi");
        check(low === wide[3:0], "decoys low");
        check(r === wide[7], "decoys r");
      end
    end else begin
      // smoke and fail: drive a few inputs and read `busy`, which nothing checks.
      {op, a, b} = 10'h2a5;
      v = 4'd9;
      repeat (4) @(negedge clk);
      rst = 0;
      en = 1;
      repeat (4) @(negedge clk);
      if (busy === 1'bx) $display("busy unknown");
    end
    if (test_name == "fail") begin
      $display("[SIM_RESULT] FAILED");
      $fatal(1, "the fail test always fails");
    end
    $display("[SIM_RESULT] PASSED");
    $finish;
  end
endmodule

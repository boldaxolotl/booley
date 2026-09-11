module coverage_tb;
  logic clk = 0, reset = 1;
  logic [1:0] choice = 0;
  wire [3:0] count;
  wire [2:0] decoded;
  string test_name;
  coverage_dut dut(clk, reset, choice, count, decoded);
  always #1 clk = ~clk;
  initial begin
    if (!$value$plusargs("test=%s", test_name)) test_name = "gap";
    repeat (2) @(negedge clk);
    reset = 0;
    for (int i = 0; i < 18; i++) begin
      choice = 2'(i % 2); // Ticket adds choice 2 while retaining all assertions.
      if (test_name == "full") choice = 2'(i % 3);
      @(negedge clk);
      if (decoded !== (3'b001 << choice)) $fatal(1, "decoder mismatch");
    end
    if (count !== 4'd2) $fatal(1, "counter wrap mismatch");
    if (test_name == "fail") $display("[SIM_RESULT] FAILED");
    else $display("[SIM_RESULT] PASSED");
    $finish;
  end
endmodule

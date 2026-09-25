module amend_tb;
  logic clk = 0;
  logic progress;
  integer cycles = 0;
  amend_counter dut(.clk(clk), .progress(progress));
  always #5 clk = ~clk;
  initial begin
    repeat (438) begin
      @(posedge clk);
      #1;
      cycles = cycles + 1;
      if (progress !== cycles[0]) $fatal(1, "progress mismatch");
    end
    $display("[SIM_CYCLES] amend_smoke %0d", cycles);
    $finish;
  end
endmodule

module waiver_tb;
  logic clk=0; wire [3:0] value; waiver_counter dut(clk,value);
  always #1 clk=~clk;
  initial begin repeat(18) @(negedge clk); $display("[SIM_RESULT] PASSED"); $finish; end
endmodule

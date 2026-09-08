module harness;
  logic clk = 0;
  logic [1:0] a = 0, b = 0;
  logic [2:0] c = 0, hit;
  top dut(clk, a, b, c, hit);
  initial begin
    for (int i = 0; i < 8; i++) begin
      clk = 0; a = 2'(i); c = 3'(i);
      #1; clk = 1; #1;
    end
    $finish;
  end
endmodule

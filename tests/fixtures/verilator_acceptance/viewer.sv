module viewer;
  logic clk = 0;
  logic [3:0] counter = 0;
  always #1 clk = ~clk;
  always @(posedge clk) counter <= counter + 1'b1;
  initial begin
    $dumpfile("viewer.fst");
    $dumpvars(0, viewer);
    #20;
    $finish;
  end
endmodule

module properties3(input logic clk, input logic [3:0] value);
  value0: cover property (@(posedge clk) value == 4'd0);
  value1: cover property (@(posedge clk) value == 4'd1);
  value2: cover property (@(posedge clk) value == 4'd2);
endmodule

module properties16(input logic clk, input logic [3:0] value);
  value0: cover property (@(posedge clk) value == 4'd0);
  value1: cover property (@(posedge clk) value == 4'd1);
  value2: cover property (@(posedge clk) value == 4'd2);
  value3: cover property (@(posedge clk) value == 4'd3);
  value4: cover property (@(posedge clk) value == 4'd4);
  value5: cover property (@(posedge clk) value == 4'd5);
  value6: cover property (@(posedge clk) value == 4'd6);
  value7: cover property (@(posedge clk) value == 4'd7);
  value8: cover property (@(posedge clk) value == 4'd8);
  value9: cover property (@(posedge clk) value == 4'd9);
  value10: cover property (@(posedge clk) value == 4'd10);
  value11: cover property (@(posedge clk) value == 4'd11);
  value12: cover property (@(posedge clk) value == 4'd12);
  value13: cover property (@(posedge clk) value == 4'd13);
  value14: cover property (@(posedge clk) value == 4'd14);
  value15: cover property (@(posedge clk) value == 4'd15);
endmodule

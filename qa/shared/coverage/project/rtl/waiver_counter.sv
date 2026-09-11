module waiver_counter(input logic clk, output logic [3:0] value = 0);
  always_ff @(posedge clk) value <= value + 4'd2;
endmodule

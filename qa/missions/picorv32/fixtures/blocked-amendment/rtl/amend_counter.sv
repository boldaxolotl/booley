module amend_counter(input logic clk, output logic progress);
  logic [8:0] count = '0;
  always_ff @(posedge clk) count <= count + 1'b1;
  assign progress = count[0];
endmodule

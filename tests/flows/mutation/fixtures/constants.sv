module consts #(parameter int P = 5) (input logic clk, rst_n, input logic [7:0] a, output logic [7:0] q, output logic [15:0] y, output logic [7:0] z);
  localparam logic [3:0] L = 4'd3;
  logic [7:0] acc;
  assign y = {2{a}};
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) q <= '0;
    else q <= q + 8'd1;
  end
  always_comb begin
    z = '1;
    acc = 4'sb1_001;
    acc = 'x;
    acc = 8'(a);
    acc = 4'hF ? 8'd2 : 8'd3;
    unique case (a)
      8'd1: z = 8'h0F;
      default: z = 8'd9;
    endcase
    for (int i = 0; i < 3; i++) z = z + 8'd1;
    do z = z + 8'd2; while (z != 8'd9);
    z += 8'd4;
  end
endmodule

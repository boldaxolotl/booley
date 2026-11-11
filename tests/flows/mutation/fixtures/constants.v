module consts(input clk, input rst_n, input [7:0] a, output reg [7:0] q, output [15:0] y, output reg [7:0] z);
  parameter P = 8'd5;
  localparam [3:0] L = 4'd3;
  reg [7:0] acc;
  assign y = {2{a}};
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) q <= 8'h00;
    else q <= q + 1;
  end
  always @* begin
    z = 8'hFF;
    acc = 7;
    acc = 'hA;
    acc = 4'b01_01;
    acc = 4'bx01z;
    if (1'b1) z = 8'd2;
    case (a)
      8'd1: z = 8'h0f;
      default: z = 8'd9;
    endcase
    for (acc = 0; acc < 3; acc = acc + 1) z = z + 8'd1;
    while (acc != 5) acc = acc + 1;
  end
endmodule

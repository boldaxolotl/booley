module del(input clk, input [3:0] a, b, output y, output reg [3:0] q, r, output reg s, output [3:0] w);
  wire [3:0] t = a & b;
  wire u = a[0], v = b[0];
  reg [3:0] init = 4'd1;
  assign y = a[0];
  wire m1, m2;
  assign m1 = a[1], m2 = a[2];
  assign #1 w = b;
  always @* begin
    q = a;
    if (a[0]) r = a; else r = b;
  end
  always @(posedge clk) s <= a[3];
  always @(posedge clk) begin
    case (a)
      4'd0: q <= b;
      4'd1: r <= b;
      default: q <= a;
    endcase
    case (b)
      4'd2: s <= 1'b1;
    endcase
    repeat (2) q <= a;
    assign q = b;
  end
  integer i;
  always @* for (i = 0; i < 4; i = i + 1) r[i] = a[i];
  always @* while (i != 0) i = i - 1;
  wire gx, gy, gz;
  wire [1:0] gw;
  genvar g;
  generate
    if (1) assign gx = a[0]; else assign gx = a[1];
    for (g = 0; g < 2; g = g + 1) assign gw[g] = a[g];
    case (1)
      0: assign gy = a[2];
      default: assign gy = a[3];
    endcase
    begin : blk
      assign gz = a[0];
    end
  endgenerate
endmodule

module sub(input [1:0] i, output o);
  assign o = i[0];
endmodule
module bsel(input [7:0] a, input [0:7] b, input [8:1] c, input [1:0] n, output ya, yb, yc, yd, ye, yf, yg, yh, yi, yj, yk, yl, ym, output [3:0] sa, sb, sd, output [7:0] sc, output reg [7:0] r);
  parameter W = 8;
  wire [W-1:0] p;
  reg [3:0] mem [0:3];
  assign ya = a[3];
  assign yb = a[0];
  assign yc = a[7];
  assign yd = b[0];
  assign ye = b[7];
  assign yf = c[1];
  assign yg = c[8];
  assign yh = mem[0][1];
  assign yi = p[0];
  assign yj = a[W-1];
  assign yk = a[3 +: 2];
  assign yl = a[n];
  assign ym = a[c[1]];
  assign sa = a[3:0];
  assign sb = a[7:4];
  assign sc = a[7:0];
  assign sd = b[2:5];
  sub u(.i(a[1:0]), .o(yn));
  always @* begin
    r[0] = a[1];
    {r[2], r[3]} = a[5:4];
    r[7:4] = 4'd0;
  end
endmodule

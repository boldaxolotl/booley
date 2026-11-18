package pk;
  function automatic logic fp(input logic [7:0] x);
    return x[2];
  endfunction
endpackage
function automatic logic ff(input logic [3:0] x);
  return x[0];
endfunction
typedef logic [7:0] byte_t;
module bsel #(parameter int W = 8) (input logic [7:0] a, input logic [W-1:0] wa, wb, input logic [3:0] n, output logic ya, yb, yc, yd, ye, yf, yg, yh, output logic [3:0] sl, output logic [7:0] r);
  byte_t t;
  logic [3:0] u [4];
  logic [3:0][7:0] md;
  assign ya = a[3];
  assign yb = u[1][0];
  assign yc = md[1][7];
  assign yd = t[2];
  assign ye = wb[1];
  assign yf = ff(n) ^ pk::fp(a);
  assign yg = md[2][6:3];
  always_comb begin
    r = 8'd0;
    r[2] = a[4];
  end
  localparam logic [7:0] K = 8'hA5;
  int qd [$];
  assign yh = K[2] ^ qd[1];
endmodule

module arith(input logic [3:0] a, b, input logic p, q, output logic [3:0] s, d, m, n, o, x, y, z, output logic u, v, xn, nx, sl, sr);
  assign s = a + b;
  assign d = a - b;
  assign m = a * b;
  assign n = a & b;
  assign o = a | b;
  assign x = a ^ b;
  assign y = a << 1;
  assign z = a >> 1;
  assign u = p && q;
  assign v = p || q;
  assign xn = a ~^ b;
  assign nx = a ^~ b;
  assign sl = a <<< 1;
  assign sr = a >>> 1;
  assign d = a / b;
  assign m = -a;
endmodule

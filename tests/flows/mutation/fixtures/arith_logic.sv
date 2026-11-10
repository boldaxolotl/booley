module arith(input logic [3:0] a, b, input logic p, q, output logic [3:0] s, d, m, n, o, x, y, z, output logic u, v);
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
  assign d = a / b;
  assign m = -a;
endmodule

module rel(input [3:0] a, b, output lt, le, gt, ge, eq, ne);
  assign lt = a < b;
  assign le = a <= b;
  assign gt = a > b;
  assign ge = a >= b;
  assign eq = a == b;
  assign ne = a != b;
endmodule

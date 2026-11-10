module rel(input [3:0] a, b, output lt, le, gt, ge, eq, ne, ceq, cne);
  assign lt = a < b;
  assign le = a <= b;
  assign gt = a > b;
  assign ge = a >= b;
  assign eq = a == b;
  assign ne = a != b;
  assign ceq = a === b;
  assign cne = a !== b;
endmodule

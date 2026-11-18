// Combinational ALU. Only `op[0]` selects the result; `op[1]` is spare.
module alu (
    input  logic [3:0] a,
    input  logic [3:0] b,
    input  logic [1:0] op,
    output logic [3:0] y,
    output logic       lt,
    output logic       eq
);
  logic [3:0] sum;
  assign sum = a + b;
  assign lt = a < b;
  assign eq = a == b;
  assign y = op[0] ? (a & b) : sum;
endmodule

// Shifter in the same file, so a module's IDs can be checked against its neighbour.
module alu_shift (
    input  logic [3:0] d,
    input  logic [1:0] n,
    output logic [3:0] y
);
  assign y = d << n;
endmodule

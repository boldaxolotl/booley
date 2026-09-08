// Overflow cases from upstream #7955 / fix a6f4dd031f50.
// Variable two-bit amounts must be added at a wider precision by DFG.
module top(input logic [31:0] value, input logic [1:0] a, b,
           output logic [31:0] left_shift, right_shift);
  assign left_shift = (value << a) << b;
  assign right_shift = (value >> a) >> b;
endmodule

// Drop-in replacement for rtl/status.sv: the missing semicolon is a syntax error.
module status (
    input  logic [3:0] v,
    output logic       busy
);
  assign busy = v == 4'd9
endmodule

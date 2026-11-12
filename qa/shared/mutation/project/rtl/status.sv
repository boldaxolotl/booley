// Status flag. The `smoke` test reads it and checks nothing, and `full` does not
// observe it, so every Mutant here survives.
module status (
    input  logic [3:0] v,
    output logic       busy
);
  assign busy = v == 4'd9;
endmodule

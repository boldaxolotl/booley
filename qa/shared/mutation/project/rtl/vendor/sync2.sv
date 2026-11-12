// Vendored two-flop synchronizer (fileset tag `vendored`): default scope skips it.
module sync2 (
    input  logic clk,
    input  logic d,
    output logic q
);
  logic meta;
  always_ff @(posedge clk) begin
    meta <= d;
    q <= meta;
  end
endmodule

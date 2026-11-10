`define IDLE 2'd0
module excl #(parameter W = 4 + 1) (input logic clk, input logic [W-1:0] a, b, input logic [1:0] state, output logic y, z);
  localparam int L = 2 * W;
  logic [W-1:0] x;
  function automatic int f(int p);
    localparam int K = 1 + 2;
    return p;
  endfunction
  always_comb begin
    // a + b in a comment
    y = 1'b0;
    z = (a[0] == 1'b1);
    if (state == `IDLE) y = 1'b1;
    x = a[W-1:0];
  end
  assign z = a + (* keep = 1 < 2 *) b;
  always_ff @(posedge clk) begin
    z <= #(1 + 2) a;
    $display("a + b %d", a + b);
  end
  assign #(1 + 2) z = a;
  always @(a == b) z = a;
  always_comb assert (a == b);
  initial begin
    y = a + b;
  end
  for (genvar g = 0; g < 2; g++) begin : gen
  end
endmodule
class C;
  function void h();
    int q = 1 + 2;
  endfunction
endclass

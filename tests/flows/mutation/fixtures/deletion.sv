module del(input logic clk, input logic [3:0] a, b, output logic y, output logic [3:0] q, r, output logic s, output logic [3:0] w);
  wire logic [3:0] t = a & b;
  assign y = a[0];
  always_comb begin
    int tmp = 1;
    q = a;
    tmp++;
    r += 4'd1;
    lbl: s = a[1];
  end
  always_ff @(posedge clk) begin
    (* keep *) q <= a;
    unique case (b)
      4'd0: r <= a;
      4'd1: begin r <= b; s <= a[0]; end
      default: begin end
    endcase
    for (int i = 0; i < 4; i++) w[i] <= a[i];
    foreach (w[i]) w[i] <= b[i];
  end
  function automatic logic [3:0] f(input logic [3:0] x);
    logic [3:0] z;
    z = x;
    return z;
  endfunction
endmodule

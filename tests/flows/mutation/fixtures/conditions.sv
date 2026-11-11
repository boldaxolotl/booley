module cond(input logic clk, a, b, output logic y, z);
  always_comb begin
    if (a) y = 1'b1;
    else y = 1'b0;
  end
  assign z = b ? a : 1'b0;
  always_ff @(posedge clk) begin
    while (a) y <= b;
    do y <= a; while (b);
    for (int i = 0; i < 2; i++) y <= a;
    repeat (2) y <= a;
  end
  always_comb if (b matches 1) y = a;
  always_comb if (a &&& b) y = a;
endmodule

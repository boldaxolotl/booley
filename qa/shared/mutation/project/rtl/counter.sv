// Sequential counter: synchronous reset to 2, then counts up while `en` is high.
module counter (
    input  logic       clk,
    input  logic       rst,
    input  logic       en,
    output logic [3:0] q
);
  always_ff @(posedge clk) begin
    if (rst) q <= 4'd2;
    else begin
      q <= q;
      if (en) q <= q + 4'd1;
    end
  end
endmodule

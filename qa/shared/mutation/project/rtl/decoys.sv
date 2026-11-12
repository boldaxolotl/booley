// Decoys: text and constructs the Mutation Flow must leave alone. The comment
// `if (a < b) y = a + 4'd1;` and the string below hold operator text.
module decoys (
    input  logic       clk,
    input  logic [7:0] wide,
    output logic       hi,
    output logic [3:0] low,
    output logic       r
);
  localparam string NOTE = "wide[7] == 8'd1 ? a + b : a << 2";
  localparam logic [3:0] INIT = 4'd5;
  localparam int W = 4;
  (* keep = 1 *) logic [W-1:0] unused_w;
  assign hi = wide[7];
  assign low = wide[3:0];
  always_ff @(posedge clk) r <= hi;
`ifdef QA_ALT_DECODE
  assign low = wide[7:4] + 4'd1;
`endif
endmodule

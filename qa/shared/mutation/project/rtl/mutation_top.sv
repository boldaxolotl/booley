// Top level of the mutation QA fixture: instances only, so it has no mutation
// sites (port connections are not design code). It instantiates every module.
module mutation_top (
    input  logic       clk,
    input  logic       rst,
    input  logic [3:0] a,
    input  logic [3:0] b,
    input  logic [1:0] op,
    output logic [3:0] y,
    output logic       lt,
    output logic       eq,
    input  logic [3:0] sh_d,
    input  logic [1:0] sh_n,
    output logic [3:0] sh_y,
    input  logic       en,
    output logic [3:0] q,
    input  logic [3:0] v,
    output logic       busy,
    input  logic [7:0] wide,
    output logic       hi,
    output logic [3:0] low,
    output logic       r,
    input  logic       req,
    output logic       ack
);
  logic rst_sync;
  logic [3:0] version_major;

  alu u_alu (.a(a), .b(b), .op(op), .y(y), .lt(lt), .eq(eq));
  alu_shift u_alu_shift (.d(sh_d), .n(sh_n), .y(sh_y));
  counter u_counter (.clk(clk), .rst(rst), .en(en), .q(q));
  status u_status (.v(v), .busy(busy));
  decoys u_decoys (.clk(clk), .wide(wide), .hi(hi), .low(low), .r(r));
  handshake u_handshake (.req(req), .ack(ack));
  self_check u_self_check ();
  sync2 u_sync2 (.clk(clk), .d(rst), .q(rst_sync));
  version u_version (.major(version_major));
endmodule

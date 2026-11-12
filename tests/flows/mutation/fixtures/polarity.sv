package pk;
  typedef enum logic [1:0] {IDLE, RUN, DONE} st_t;
  logic x = 1'b1;
endpackage
checker chk(input logic c);
endchecker
interface ifc;
  logic [3:0] v = 4'd2;
endinterface
module sub(input logic en, input logic [3:0] x, output logic [3:0] y);
endmodule
module pols(input logic [3:0] a, b, input logic signed [7:0] sa, input logic clk, p, q, output logic [3:0] y1, output logic y2, y3, y4, y5, y6, y7);
  import pk::IDLE;
  typedef enum logic {OFF, ON} sw_t;
  typedef enum logic {ONLY} one_t;
  pk::st_t s;
  pk::st_t s0 = IDLE;
  sw_t w;
  one_t o;
  real r;
  logic signed [7:0] sv;
  logic [7:0] big, st8;
  logic [1:0] ap;
  localparam pk::st_t K = pk::RUN;
  enum logic [1:0] {A = 2'd0, B = 2'd1} inl;
  ifc i1();
  always_comb begin
    y2 = (s == IDLE);
    y3 = (s == pk::RUN);
    y4 = (w == ON);
    y5 = (o == ONLY);
    case (s)
      IDLE: y1 = 4'd1;
      default: y1 = 4'd2;
    endcase
    y6 = !r;
    y1 = signed'(a);
    y1 = unsigned'(b);
    big = sv >>> 1;
    big = a >>> 1;
    st8 = {<<{a, b}};
    st8 = {a, b};
    {y2, y3} = {q, p};
    ap = '{p, q};
  end
  always @(posedge clk) begin
    do y1 = ~a; while (y1 != 0);
    for (int i = 0; i < 2; i++) y1 = ~b;
    forever begin y1 = -a; @(posedge clk); end
  end
  sub u1(.en(s == IDLE), .x(a), .y(y1));
  sub u2(.en(ON), .x(a), .y());
  chk c1(p & q);
endmodule
bind pols chk c0(p & q);
module compound(input logic signed [7:0] a, output logic signed [7:0] y);
  always_comb begin y = a; y >>>= 1; end
endmodule

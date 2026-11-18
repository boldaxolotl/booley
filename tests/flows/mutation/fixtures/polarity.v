`define INV(x) ~x
module sub #(parameter P = 1) (input en, input [3:0] x, input [3:0] y, input [1:0] s, output [1:0] o);
endmodule
module pol(input [3:0] a, b, input [7:0] ua, input signed [7:0] sa, input p, q, rst, output [3:0] y1, y2, y3, output [7:0] w1, w2, w3, w4, w5, output o1, o2, o3, o4, o5, o6, o7, o8, output reg [3:0] r, output [3:0] t1, t2, t3, output [1:0] n1);
  wire [3:0] t4;
  wire \e ;
  reg [7:0] cnt = 8'd3;
  function [3:0] f(input [3:0] v); f = v; endfunction
  assign y1 = ~a;
  assign y2 = -b;
  assign y3 = a | !b;
  assign o1 = &a;
  assign o2 = |a;
  assign o3 = ^a;
  assign o4 = ~&a;
  assign o5 = ~|a;
  assign o6 = ~^a;
  assign o7 = p && q;
  assign o8 = p || q;
  assign t1 = a & b;
  assign t2 = a | b;
  assign t3 = f(a) & b;
  assign t4 = `INV(a);
  assign w1 = sa >>> 2;
  assign w2 = ua >>> 2;
  assign w3 = sa >> 2;
  assign w4 = $signed(a - b) >> 1;
  assign w5 = $signed(a & b);
  always @(posedge p) $display("%d", ~a);
  always @* if ((!rst)) r = a; else r = b;
  always @* case (a)
    ~b: r = 4'd1;
    default: r = 4'd2;
  endcase
  always @* while (r != 0) r = ~a;
  assign y1 = {a[3:2], b[1:0]};
  assign w1 = {a, a};
  assign w2 = {p, q, rst};
  assign w3 = {2{p, q}};
  assign {o1, o2} = 2'b10;
  assign y2 = ~\e ;
  assign y3 = \e & b;
  assign w4 = {\e , b};
  sub #(.P(3)) u1(.en(p & q), .x(a), .y(4'd3), .s(b[2:1]), .o({n1[0], n1[1]}));
  and g1 (o3, ~p, q);
  assign w5 = {`INV(a), a};
endmodule

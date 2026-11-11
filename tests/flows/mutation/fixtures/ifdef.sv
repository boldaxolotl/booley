`ifdef X
module m(input a, output y); assign y = a & a; endmodule
`else
module m(input a, output y); assign y = a | a; endmodule
`endif

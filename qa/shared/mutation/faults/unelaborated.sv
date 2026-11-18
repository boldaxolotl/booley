// Drop-in replacement for rtl/mutation_top.sv: instantiates a module that does not exist.
module mutation_top (
    input  logic clk
);
  no_such_module u_missing (.clk(clk));
endmodule

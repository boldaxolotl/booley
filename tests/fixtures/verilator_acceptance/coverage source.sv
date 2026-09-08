module child #(parameter int WIDTH=2)
  (input logic clk, input logic [WIDTH-1:0] value, output logic hit);
  assign hit = value[0] && value[1];
  always @(posedge clk) begin
    if (value == '1) $display("hit width=%0d", WIDTH);
    else if (value == 0) $display("zero width=%0d", WIDTH);
    else $display("partial width=%0d", WIDTH);
    if (value[0]) $display("odd");
    else $display("even");
  end
  cover property (@(posedge clk) value == '1);
endmodule

module top(input logic clk, input logic [1:0] a, b, input logic [2:0] c,
           output logic [2:0] hit);
  child same_a(clk, a, hit[0]);
  child same_b(clk, b, hit[1]);
  child #(.WIDTH(3)) different(clk, c, hit[2]);
endmodule

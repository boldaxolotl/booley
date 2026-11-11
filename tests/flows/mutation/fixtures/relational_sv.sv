module relsv(input logic [3:0] a, b, output logic we, wn);
  assign we = a ==? b;
  assign wn = a !=? b;
endmodule

module child(output logic [7:0] words [2]);
  initial begin words[0] = 8'h12; words[1] = 8'h34; end
endmodule
module top;
  wire [7:0] words [2];
  child dut(words);
  initial begin
    #1;
    force dut.words[0] = 8'hff;
    #1;
    if (words[0] !== 8'hff || words[1] !== 8'h34) $fatal(1, "force array");
    release dut.words[0];
    #1;
    force dut.words[1][3] = 1'b1;
    #1;
    if (words[1] !== 8'h3c) $fatal(1, "force bit select");
    release dut.words[1][3];
    $finish;
  end
endmodule

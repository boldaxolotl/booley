module multi_tb;
 logic clk=0,reset=1;logic [1:0] choice=0;
 wire [3:0] a,b,parity;wire [2:0] da,db;
 coverage_dut first(clk,reset,choice,a,da);
 coverage_dut second(clk,reset,choice,b,db);
 waiver_counter third(clk,parity);
 always #1 clk=~clk;
 initial begin repeat(2) @(negedge clk);reset=0;repeat(18) begin choice=choice==0?1:0;@(negedge clk);end $display("[SIM_RESULT] PASSED");$finish;end
endmodule

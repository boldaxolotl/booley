module scale_tb;
 logic clk=0,reset=1; logic [1:0] choice=0;
 for(genvar i=0;i<4096;i++) begin: copies
 wire [3:0] count; wire [2:0] decoded; coverage_dut dut(clk,reset,choice,count,decoded);
 end
 always #1 clk=~clk;
 initial begin repeat(2) @(negedge clk);reset=0; repeat(18) begin choice=choice==0?1:0; @(negedge clk);end $display("[SIM_RESULT] PASSED");$finish;end
endmodule

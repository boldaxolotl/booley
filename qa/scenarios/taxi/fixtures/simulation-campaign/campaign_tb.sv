module campaign_tb;
  string selected_test;

  initial begin
    if (!$value$plusargs("test=%s", selected_test)) begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end
    if (selected_test != "first" && selected_test != "second") begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end
    $display("QA_VERILATOR_CAMPAIGN_TEST=%s", selected_test);
    #1;
    $display("[SIM_RESULT] PASSED");
    $finish;
  end
endmodule

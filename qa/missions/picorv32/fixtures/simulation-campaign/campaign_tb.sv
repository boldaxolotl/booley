module campaign_tb;
  string selected_test;
  integer wait_status;

  initial begin
    if (!$value$plusargs("test=%s", selected_test)) begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end

    $display("QA_CAMPAIGN_TEST=%s", selected_test);
    if (selected_test == "slow") begin
      // A real bounded wall-clock interval for the external-interruption case.
      wait_status = $system("sleep 20");
      if (wait_status != 0) begin
        $display("[SIM_RESULT] FAILED");
        $finish;
      end
    end else begin
      #1000;
    end
    $display("[SIM_RESULT] PASSED");
    $finish;
  end
endmodule

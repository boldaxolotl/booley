module campaign_tb;
  string selected_test;
  integer vector_file;
  integer value;
  integer scanned;

  initial begin
    if (!$value$plusargs("test=%s", selected_test)) begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end
    if (selected_test == "alpha")
      vector_file = $fopen("inputs/alpha.hex", "r");
    else if (selected_test == "beta")
      vector_file = $fopen("inputs/beta.hex", "r");
    else
      vector_file = 0;
    if (vector_file == 0) begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end
    scanned = $fscanf(vector_file, "%h", value);
    $fclose(vector_file);
    if (scanned != 1) begin
      $display("[SIM_RESULT] FAILED");
      $finish;
    end
    $display("QA_UART_RUNTIME_INPUT=%s:%08x", selected_test, value);
    $display("[SIM_RESULT] PASSED");
    $finish;
  end
endmodule

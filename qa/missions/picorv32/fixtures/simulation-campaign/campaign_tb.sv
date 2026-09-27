module campaign_tb;
  string selected_test;
  // `slow` spins this many 1-time-unit steps so the external-interruption case
  // has a real wall-clock window (Icarus has no `$system`). Override with
  // +slow_steps=<n> if `slow` finishes in under ~15 s on your host.
  longint slow_steps = 64'd30_000_000;
  longint step;

  initial begin
    if (!$value$plusargs("test=%s", selected_test)) begin
      $display("[SIM_RESULT] FAILED");
      $display("ERROR! missing +test=<name>");
      $finish;
    end

    $display("QA_CAMPAIGN_TEST=%s", selected_test);
    if (selected_test == "slow") begin
      void'($value$plusargs("slow_steps=%d", slow_steps));
      for (step = 0; step < slow_steps; step = step + 1) begin
        #1;
        if (step % 64'd5_000_000 == 0)
          $display("QA_CAMPAIGN_SLOW_PROGRESS=%0d", step);
      end
    end else begin
      #1000;
    end
    // Print both the built-in marker and the PicoRV32 Project's configured
    // pass sentinel: configured sentinels replace the built-in ones.
    $display("[SIM_RESULT] PASSED");
    $display("ALL TESTS PASSED.");
    $finish;
  end
endmodule

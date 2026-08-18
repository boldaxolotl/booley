// Booley verdict-sentinel snippet. Booley decides pass/fail by scanning your
// testbench's stdout for a sentinel substring. Emit ONE of these at the end of
// your test so a plain `$finish` (exit 0) is never mistaken for a pass.
//
// Option 1 — use Booley's built-in markers (no config needed):
initial begin
	// ... run the test ...
	if (all_checks_passed)
		$display("[SIM_RESULT] PASSED");
	else
		$display("[SIM_RESULT] FAILED");
	$finish;
end

// Option 2 — keep your existing testbench wording and tell Booley about it in
// .booley_project/booley.toml instead of editing the testbench:
//
//   [flows.sim]
//   pass_sentinels = ["ALL TESTS PASSED."]
//   fail_sentinels = ["ERROR!", "TIMEOUT"]
//
// Booley then treats a line containing any pass sentinel as a pass and any fail
// sentinel as a fail (fail wins ties). No `[SIM_RESULT]` line required.

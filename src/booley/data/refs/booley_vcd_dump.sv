// Booley trace-convention module. Drop this file into your testbench fileset
// (the `tb` / `simulation`-tagged one) and Booley's `--trace` flag will produce
// a queryable waveform with no testbench edits.
//
// How it works: the module is *uninstantiated*. Booley's trace overlay roots it
// as a second elaboration top (`-s booley_vcd_dump` / `-top booley_vcd_dump`),
// and the runner passes `+trace` only when a trace is requested. The explicit
// `$dumpfile("dump.vcd")` is mandatory — some simulators (e.g. Xcelium's xmsim)
// write no VCD at all without it, so the trace overlay rejects a dump module
// that omits it. Guard is `ifndef VERILATOR` because Verilator traces via its
// C++ harness, not `$dumpvars`.
`ifndef VERILATOR
module booley_vcd_dump;
	initial if ($test$plusargs("trace")) begin
		$dumpfile("dump.vcd");
		$dumpvars(0);
	end
endmodule
`endif

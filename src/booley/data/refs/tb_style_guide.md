# Testbench Style Guide

Canonical style guide for SystemVerilog testbenches (`*_tb.sv`), including
unit testbenches and testbenches that compile across multiple hardware
configurations.

Project-specific overlays may be supplied by the caller or ticket context.

---

## 1. Module Layout

```systemverilog
// Copyright header
`include "<project_defines>.svh"  // project-specific header

module <unit>_tb();

  // Package imports
  // DPI-C imports (if using golden reference)
  // Localparams
  // Signal declarations
  // DUT instantiation - instance name MUST be `dut`
  // Test infrastructure (memory model, stall injection, etc.)
  // Test statistics counters
  // Clock generator
  // Error monitors
  // Main test block (initial)
  // Test tasks

endmodule
```

## 2. Clock, Reset, and Sampling

Clock initialized in `initial`, toggled in `always`. Delay before reset release
so parallel `initial` blocks can generate data.

```systemverilog
always begin
  #5 clk = ~clk;  // 10ns period, 100MHz
end

// In initial block:
clk = 0;
rstn = 0;
#100;                       // let parallel initial blocks complete
rstn = 1;
repeat (10) @(posedge clk); // settle after reset
```

### 2.1 Clocking Block (Required for Synchronous Stimulus & Sampling)

Drive and sample synchronous signals through a clocking block. Bare blocking
assigns or reads after `@(posedge clk)` race the RTL Active/NBA regions; Icarus
exposes this, trace dumpers can hide it.

```systemverilog
clocking cb @(posedge clk);
  default input #1step output #1ns;
  output address, write_data, read, write;
  input  read_data, hit, miss;
endclocking

// GOOD
@(cb); cb.address <= 5'h01; cb.read <= 1'b1;
@(cb); if (cb.read_data !== expected) ...

// BAD - race
@(posedge clk); address = 5'h01; read = 1'b1;
@(posedge clk); if (read_data !== expected) ...
```

**Exempt:** reset sequence, combinational DUTs, waiting-only constructs
(`repeat(N) @(posedge clk);`, `wait(done);`, timeout forks).

**Fallback** (async interfaces only, document why): `@(posedge clk); #1;`
before drive/sample.

### 2.2 DUT Output and Reset Checks

Never check registered DUT outputs in the same simulator region that updates
them. RTL flops normally use nonblocking assignments (`<=`), so values settle in
the NBA region after `@(posedge clk)` or `@(posedge rst)` TB code has run.
Same-edge checks can see stale values and falsely fail.

Use:

- Synchronous interfaces: clocking-block input skew from Section 2.1.
- Simple/async fallback: small settle delay (`#1` in this guide's timescale)
  before reading DUT outputs.
- Reset checks: after the settle delay, confirm reset is still asserted before
  reporting failures.

```systemverilog
// GOOD: async reset output check waits for DUT NBA reset assignments to settle.
always @(posedge rst) begin
  #1;
  if (rst) begin
    if (done_o !== 1'b0) report_error("done_o asserted during reset");
    if (data_o !== '0)   report_error("data_o nonzero during reset");
  end
end

// GOOD: synchronous sample through clocking block input skew.
@(cb);
if (cb.read_data !== expected) report_error("read_data mismatch");

// BAD: races RTL nonblocking reset/update assignments.
always @(posedge clk or posedge rst) begin
  if (rst && done_o !== 1'b0) report_error("done_o asserted during reset");
  else if (read_data !== expected) report_error("read_data mismatch");
end
```

## 3. Test Execution Task (`run_test`)

Use one task for the full stimulus-check cycle:

1. Print separator and `[TEST] Starting: <name>`
2. Increment `total_tests`, reset `error_cnt`
3. Load stimulus
4. Trigger DUT operation
5. Wait for completion with timeout (Section 4)
6. Compare DUT outputs vs expected; show first 5 errors only
7. Print `[PASS]`/`[FAIL]` with counts

Use open-array parameters where possible so one task handles multiple data
sizes.

## 4. Timeout Protection

```systemverilog
logic timed_out;
timed_out = 0;
fork
  wait(done_signal);
  begin
    repeat (TIMEOUT_CYCLES) @(posedge clk);
    timed_out = 1;
    disable fork;
  end
join_any
disable fork;
```

- Timeout = test failure; skip comparison.
- Scale timeout to operation complexity.

## 5. Error Monitor

Continuous monitors turn error signals into test failures. Prefer edge-triggered
monitors to avoid flooding on sustained assertions.

```systemverilog
always @(posedge state_err) begin
  $display("[ERROR] state_err asserted at time %0t", $time);
  total_failed++;
end
```

## 6. Test Statistics and Summary

```systemverilog
integer error_cnt;    // per-test, reset in run_test
integer total_tests;
integer total_passed;
integer total_failed;
```

End simulation with `$finish`, not `$stop`; `$stop` pauses Icarus and causes
sim timeouts.

Every TB must print a human summary, then a machine-readable verdict sentinel,
then `$finish`. Booley uses the sentinel instead of unreliable simulator return
codes. `[SIM_RESULT]` is the default shown below; an existing testbench may keep
different wording when `[flows.sim].pass_sentinels` / `fail_sentinels` declares
that project contract.

```systemverilog
print_summary();  // totals plus ALL/SOME TESTS PASSED/FAILED banner
if (total_failed == 0) begin
  $display("[SIM_RESULT] PASSED");
end else begin
  $display("[SIM_RESULT] FAILED");
end
$finish;
```

## 7. Test Banner

At start of `initial`, print module name, key parameters, and active
configuration/mode flags.

## 8. Test Vector Strategy

Every TB must include both:

1. Randomized vectors - broad input-space coverage for unexpected interactions.
2. Deterministic edge cases (Section 9) - boundary conditions, identities,
   near-overflow, and spec-defined corners.

Neither alone is sufficient: random-only misses critical boundaries;
edge-case-only misses interior interactions.

### Randomized Input Generation

- 32-bit values: rejection sample with `$urandom()` in a `do/while` loop.
- 64-bit values: rejection sample with `{$urandom(), $urandom()}`.
- Keep random values inside the valid input domain.

Never use `$urandom_range()` for large ranges (max > ~2^30). Vivado xsim can
generate values outside the specified range. `$urandom_range()` is acceptable
only for small ranges, e.g. `$urandom_range(255)`.

```systemverilog
// BAD - Vivado bug can generate values > max:
automatic int unsigned val = $urandom_range(LARGE_Q - 1);

// GOOD - rejection sampling:
automatic int unsigned val;
do val = $urandom(); while (val >= LARGE_Q);
```

## 9. Edge Case Testing

Cycle boundary patterns across all elements using `case (i % N)`.

| Category | Examples |
|----------|----------|
| Identity | `0 op 0` |
| Boundary | `max op 0`, `0 op max`, `max op max` |
| Near-overflow | `max op 1`, `1 op max` |
| Minimum non-zero | `1 op 1` |
| Mid-range | Bit-boundary values, e.g. `0xFFFF`, `0x10000` |
| Asymmetric | `small op large` |

Optionally test garbage in unused bit positions to verify DUT ignores padding.

## 10. Display Conventions

| Prefix | Usage |
|--------|-------|
| `[TB]` | Setup/init messages |
| `[TEST]` | Per-test progress |
| `[PASS]` | Test passed |
| `[FAIL]` | Test failed |
| `[ERROR]` | Mismatch or assertion failure |
| `[SIM_RESULT] PASSED` | Final verdict: all tests passed (mandatory, exactly once) |
| `[SIM_RESULT] FAILED` | Final verdict: test failures detected (mandatory, exactly once) |

- Hex addresses: `0x%04x`; simulation time: `%0t`.
- Error details: show inputs, expected, and actual values.
- Include per-element breakdown for wide mismatches.

## 11. DPI-C Golden Model (when applicable)

- Imports at module scope, before signal declarations.
- Use flat fixed-size arrays for DPI interface.
- Unpack from DUT's data layout before DPI call, repack after.
- Document which domain the DPI model operates in.

## 12. DUT Instance Naming

The DUT instance must be named `dut`; automated coverage derives hierarchy from
the instance name. Canonical naming gives `<tb_module>.dut.*`, predictable
without searching the trace.

```systemverilog
// GOOD
aes128_encrypt #(.WIDTH(128)) dut (
  .clk   (clk),
  .rst_n (rst_n),
  ...
);

// BAD - non-standard names break automated hierarchy discovery
aes128_encrypt uu_aes128_encrypt (...);  // prefixed
aes128_encrypt uut (...);                // abbreviation
aes128_encrypt u_dut (...);              // prefixed
```

## 13. Stall / Backpressure Testing

When the DUT has a ready/stall interface, run all tests twice:

1. Clean run (no stalls) - verify functional correctness
2. Random stall run (~67% ready) - verify handshake robustness

Use a `delay_phase` loop controlled by an `enable_*_delays` flag.

```systemverilog
`ifdef HAS_STALL_INTERFACE
  for (int delay_phase = 0; delay_phase < 2; delay_phase++) begin
`else
  for (int delay_phase = 0; delay_phase < 1; delay_phase++) begin
`endif
    enable_stalls = (delay_phase == 1);
    // ... run all tests ...
  end
```

## 14. Ifdef Nesting for Multi-Config

When a design has multiple orthogonal configuration axes (e.g., algorithm
selection, protection mode, bus interface), use a consistent nesting order:

1. Outermost: hardware variant (e.g., protection mode - affects widths/layouts)
2. Middle: algorithm/feature selection
3. Innermost: test code

- Set runtime selection signals before each algorithm's test block.
- Each configuration must compile and run independently.
- Stall/backpressure re-run phase guarded by bus interface ifdef, if applicable.

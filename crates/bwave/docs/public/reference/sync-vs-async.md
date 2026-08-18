# Sync vs async mode

B-Wave has two output modes. Picking the wrong one is the
most common source of "off by a clock period" confusion,
so this is the first thing to understand after a build.

## Sync mode (default)

Sync mode reports signal values **once per rising clock
edge**, indexed by **cycle number**. This is what
GTKWave, Vivado, Questa, and most other waveform viewers
default to. It's almost always what you want for
RTL-level debugging.

For each cycle:

1. The simulation tick of the rising edge is computed
   from the detected clock period.
2. Each watched signal's value is sampled **post-edge**:
   the value it holds *after* the edge has propagated
   through flip-flops.
3. That `(cycle, value)` pair is what appears in
   `signal` / `wave` / `value` / `find` / `sample`
   output.

Time arguments (`-t`, `--at`, `<T1> <T2>`, `--before`,
`--after`) are interpreted as cycle numbers.

### Post-edge sampling, concretely

Suppose you have:

```verilog
always @(posedge clk) state <= next_state;
```

If on cycle N the FSM decides `next_state = 3`, then in
B-Wave's sync output:

- Cycle N still shows the *old* `state` value (the
  flop hasn't captured yet at the start of the edge).
- Cycle N+1 shows `state = 3` (post-capture).

This matches what you see in any waveform viewer when you
park the cursor on the edge: the cursor sits on N, but
the displayed value reflects post-edge.

### Clock auto-detection

B-Wave picks the clock by scanning the signal tree for
the first 1-bit signal whose name matches `*clk*`,
breadth-first by scope depth (shallowest first). The
header prints which signal was selected:

```
# clock: tb.dut.clk  period=10ns
```

If no match is found, sync mode degrades:

- `value --at N` interprets `N` as a tick (raw timescale
  units), not a cycle, and emits `at_unit: "tick"` in
  JSON.
- `find` reports `unit: "tick"`.
- `stats` reports `total_cycles: null,
  clock_period_ns: null`.

Override with `--clock PATTERN` (any pattern syntax:
`--clock my_clock` matches the deepest `my_clock`,
`--clock "*tb.clk*"` matches the testbench clock when
the DUT also has one).

See `troubleshooting/clock-detection` for diagnostic
tips.

### Reset auto-skip

By default sync mode skips output until the reset
deasserts, so your first cycle is "real" simulation
rather than initialisation. See
`troubleshooting/reset-skipping` for the deassertion
rules and how to override with `--with-reset` or
`--reset PATTERN`.

## Async mode (`--async`)

Async mode reports **every VCD transition** with its
**raw timestamp** (in timescale units, typically ns or
ps depending on the simulator). No cycle numbering, no
clock-edge alignment, no post-edge sampling.

For each transition recorded in the VCD:

1. The exact tick is reported.
2. The value at that tick is reported.

Multiple transitions can happen at the same tick if the
simulator wrote them at the same VCD `#time` block.

### When async makes sense

- **Combo-logic glitches** between clock edges. Sync
  mode hides them by design.
- **Asynchronous reset behaviour** before the first
  clock edge.
- **Clock-domain crossings** where you want to see the
  raw setup/hold timing.
- **Designs without a clock** (test infrastructure,
  pre-synthesis combo blocks, X propagation tests).
  Sync mode falls back to tick-indexing automatically,
  but `--async` is more honest and clearer in the
  output.
- **VCD timing checks**: you need the exact ns at which
  something happened.

### Async cost

Async output is much busier than sync: a single FF
toggle becomes one row per VCD event, not one per
cycle. Always pair `--async` with `-t` to bound the
window or you'll hit `--limit` in milliseconds.

## Switching between modes

A single store supports both modes. There is no rebuild
required. `bwave signal sim.fst -s "*" -t 0:100` and
`bwave signal sim.fst -s "*" -t 0t:1000t --async`
inspect the same data.

The store holds the full asynchronous transition stream
— it is a plain, lossless FST file. Sync mode is a
sampling view computed over that stream at query time:
per-cycle post-edge values are derived from the same
transitions async mode reports. So the two modes always
agree, and switching between them never requires a
rebuild.

Time arguments switch meaning:

| Flag       | Sync     | Async                |
|------------|----------|----------------------|
| `-t 100:200` | cycles 100-200 | rejected — suffix required: `-t 100t:200t`, `-t 1us:2us` |
| `--at 100`   | cycle 100      | rejected — use `--at 100t` |
| `<T1> <T2>`  | cycles         | ticks, suffix required (`100t`, `100ns`) |
| `--before N` | cycle N        | tick N (bare bound)  |

Bare integers mean cycles in sync mode; async mode
rejects them outright and requires a unit suffix
(`100t` for ticks, `100ns` for physical time, `100c` to
convert cycles). See `reference/time-tokens` for the
full grammar.

## Quick decision guide

- Default to sync. It matches your mental model of "what
  did the FSM do on cycle 1234".
- Switch to async when sync hides something (combo
  glitch, async reset, no clock).
- If sync output looks 1 cycle off, recheck. You're
  probably staring at post-edge values from the cycle
  *after* the one you think you set up.
- If sync output is empty or wrong, your clock pattern
  is wrong. See `troubleshooting/clock-detection`.

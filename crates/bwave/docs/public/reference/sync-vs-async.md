# Sync vs async mode

B-Wave has two output modes. Picking the wrong one is the
most common source of "off by a clock period" confusion,
so this is the first thing to understand after a build.

## Sync mode (default)

Sync mode is a **uniform time grid anchored on the selected clock**.
The period is the gap between its first two rising edges. Cycle 1 is the
first grid point at or after reset deassertion; later cycles advance by that
period, including interior clock stops. Cycle distances remain proportional
to elapsed time. A stop during the first measured gap can make later edges
fail the check; choose another clock or use async in that case.

### Clock regularity

`build`, `overview` and `stats` summarize the selected clock and the verdict:

```text
# clock: tb.clk
# clock regularity: pass (stopped_stretches)
# clock stopped: 1 stretches, 3 cycles, longest 3:5
```

`pass` means all real rising edges fit the grid, with optional stopped
stretches. `warning` means an off-grid edge or X/Z after the first rising
edge makes sync sampling unreliable. `unavailable` means fewer than two
rising edges, including no clock candidate. Every sync query warns about
invalid clock evidence; JSON carries `clock_regularity` and `warnings`.
The verdict is advisory and never changes a query's mode.

Only **interior** missing grid points in the regular prefix are marked as
stopped. Gaps before the first or after the last rise are excluded. Stop
marking ends at the first off-grid edge or X/Z tick; subsequent queries
continue on the arithmetic grid with a warning. An integer-multiple slowdown
looks like repeated gating and remains on-grid. Several candidate periods
are reported as context in summaries, without invalidating the chosen clock.
B-Wave cannot infer ordinary signals' clock-domain ownership from a trace;
select `--clock` for the intended domain or inspect raw timestamps.

`clock_stopped` in JSON lists full inclusive `{from_cycle, to_cycle}` ranges
intersecting the query window. Stops are information, never warnings. Metadata
scales with the stored clock edges, not the elapsed cycle count, and is not
capped by `--limit`. Point queries only report their containing stretch.

### Sampling modes

`--sample-mode post-edge` is the default: each signal holds its settled value
at the grid tick, including same-tick updates recorded by the simulator.
`--sample-mode pre-edge` observes strictly before that tick (the last integer
tick before it), showing inputs that flops captured on the edge. The edge's
cycle label stays the same. For example, an input changes at tick 13 and its
flop output at the tick-15 edge: that row shows the new input and old output
in pre-edge mode; post-edge shows both new values.

`sample` applies the mode to both the trigger and captured values, including
Virtual Signals. `value` JSON retains the edge in `target_tick` and records
`sample_tick` separately. At tick zero there is no preceding observation:
pre-edge values are unknown and `sample_tick` is null. Cycle 0 observes before
the reset-dependent start tick; it is an initial snapshot, not a grid edge.
The first pre-edge observation may precede reset deassertion by one tick.
Explicit sampling modes are rejected with `--async`, and pre-edge needs a
usable clock. `list`, `overview`, and `stuck` reject sampling-mode options.

Clock selection scans one-bit names matching the `clk` heuristic, ordered
by scope depth and name, choosing the first usable period. `--clock PATTERN`
selects the first matching one-bit signal in that order. If no usable clock
exists, sync falls back to ticks (`value.at_unit` / `find.unit` are `tick`,
`stats.total_cycles` is null). Inspect `overview` before relying on cycles.

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
- If sync output is empty or wrong, inspect the regularity verdict and clock
  selection. See `troubleshooting/clock-detection`.

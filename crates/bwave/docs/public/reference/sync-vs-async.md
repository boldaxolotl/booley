# Sync vs async mode

B-Wave has two output modes. Picking the wrong one is the
most common source of "off by a clock period" confusion,
so this is the first thing to understand after a build.

## Sync mode (default)

Sync mode reads every signal on a **uniform grid of cycles** built from the
selected clock:

- The period is the gap between the clock's first two rising edges.
- Cycle 1 is the first grid point at or after reset deassertion.
- Each later cycle is one period further on, whether or not the clock rose
  there. Cycle distances stay proportional to elapsed time, even across a
  clock stop.

### Clock selection

B-Wave considers one-bit signals whose name contains `clk`, ordered by scope
depth and then name, and picks the first with a measurable period.
`--clock PATTERN` picks the first one-bit match in the same order.

Without a usable clock, sync falls back to ticks: `value.at_unit` and
`find.unit` are `tick`, and `stats.total_cycles` is null. See
`troubleshooting/clock-detection`.

### Clock regularity

The grid is only right while the clock keeps its period. `build`, `overview`,
and `stats` print the check:

```text
# clock: tb.clk
# clock regularity: pass (stopped_stretches)
# clock stopped: 1 stretches, 3 cycles, longest 3:5
```

| Status | Meaning |
|---|---|
| `pass` | Every rising edge lands on the grid. Clock stops are allowed. |
| `warning` | An edge is off the grid, or the clock goes X/Z after its first rise. Cycle numbers are unreliable. |
| `unavailable` | Fewer than two rising edges, or no clock candidate. |

The verdict is advisory and never changes a query's mode. Every sync query
warns on `warning` and `unavailable`; JSON carries the verdict in
`clock_regularity` and `warnings`. On a warning, use `--async` with tick or
time bounds.

### Clock stops

A clock stop is a run of grid points with no rising edge, between two real
edges. Stops are information, not warnings:

- A query whose window overlaps a stop lists it in JSON `clock_stopped` as
  an inclusive `{from_cycle, to_cycle}` range. The range is the whole stop,
  even when the window covers part of it.
- A point query reports only the stop that contains it.
- The list is not capped by `--limit`. Its size follows the stored clock
  edges, not the number of stopped cycles.

What the check cannot tell:

- Gaps before the first rise or after the last one are not stops.
- Stop marking ends at the first off-grid edge or X/Z tick. Later queries
  still use the grid, with a warning.
- A clock slowed to an integer multiple of its period looks like repeated
  gating, and passes.
- A stop inside the first gap stretches the measured period, so later edges
  fail the check. Pick another clock or use `--async`.
- Candidate clocks with different periods are reported as context. They do
  not invalidate the selected clock.
- A trace does not say which clock a signal belongs to. Select the intended
  domain with `--clock`, or inspect raw timestamps.

### Sampling modes

| `--sample-mode` | Reads | Shows |
|---|---|---|
| `post-edge` (default) | The settled value at the edge tick, including same-tick updates. | What flops hold after the edge. |
| `pre-edge` | The last tick before the edge. | What flops captured on the edge. |

Cycle numbers are the same in both modes. Example: an input changes at tick
13 and a flop captures it on the tick-15 edge. On that cycle, pre-edge shows
the changed input and the not-yet-updated output; post-edge shows both
updated.

Pre-edge details:

- `sample` applies the mode to the trigger and the captured values, Virtual
  Signals included.
- `value` JSON keeps the edge in `target_tick` and adds the tick actually
  read as `sample_tick`.
- An edge at tick 0 has nothing before it: values are unknown and
  `sample_tick` is null.
- Cycle 0 is a snapshot taken before the reset-dependent start tick, not a
  grid edge. The first pre-edge read can precede reset deassertion by one
  tick.
- Pre-edge needs a usable clock. `--sample-mode` is rejected with `--async`
  and by `list`, `overview`, and `stuck`.

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
- If sync output is empty or wrong, run `bwave overview` and check the clock
  and its verdict. See `troubleshooting/clock-detection`.

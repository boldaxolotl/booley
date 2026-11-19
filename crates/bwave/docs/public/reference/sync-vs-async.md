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
- Cycle 0 is not a grid point. It reads strictly before the reset
  deassertion tick and before cycle 1's read, in both sample modes. Cycle 0
  is omitted when no such tick exists: the start tick is tick 0 (no reset
  was found, `--with-reset` is set, or reset deasserts at tick 0), or a
  pre-edge read of cycle 1 already sits on tick 0. `wave` then
  starts at cycle 1, and `value --at 0` returns `x` with a null
  `sample_tick`. Cycle 0 is not an edge, so `rising`, `falling` and
  `change` skip it and `find` reports only level matches there.

### Clock selection

B-Wave picks the clock like this:

1. Candidates are one-bit signals whose hierarchical name, up to the first
   `[`, contains `clk` in any case.
2. They are ordered by scope depth, then name.
3. The first with two rising edges (0 to 1) wins.

`--clock PATTERN` picks the first one-bit match in the same order. No match
exits with code 2. A match with fewer than two rising edges gives an
`unavailable` verdict. `overview`, `list` and every query print
`# clock override: NAME (period=P ticks, first_rise=T)` on stderr, except in
JSON mode. List the candidates with `bwave list trace.fst -s "*clk*"`.

Without a usable clock, sync falls back to ticks: `value.at_unit` and
`find.unit` are `tick`, and `stats.total_cycles` is null. Every sync query
then prints one `# WARNING: no usable clock: ...` line (in JSON, the same
text without the prefix in `warnings`) that names `--clock PATTERN` or
`--async`.

### Clock regularity

The grid is only right while the clock keeps its period. `build`, `overview`,
and `stats` print the check:

```text
# clock: tb.clk (auto-detected; --clock PATTERN to override)
# clock regularity: pass (stopped_stretches)
# clock stopped: 1 stretches, 3 cycles, longest 3:5
```

The first line ends `(auto-detected; --clock PATTERN to override)`, or
`(--clock)` when `--clock` chose the clock, and is `# clock: <not detected>`
when there is no candidate.

| Status | Meaning |
|---|---|
| `pass` | Every rising edge lands on the grid. Clock stops are allowed. |
| `warning` | An edge is off the grid, or the clock goes X/Z after its first rise. Cycle numbers are unreliable. |
| `unavailable` | Fewer than two rising edges, or no clock candidate. |

| Reason | Meaning |
|---|---|
| `regular` | Every rising edge fits the grid. |
| `stopped_stretches` | Some interior grid points have no rising edge. |
| `off_grid_edge` | Frequency change, phase change, or jitter. |
| `non_integer_period` | The period is fractional and the simulator rounded it to ticks. |
| `unknown_clock` | The clock goes X/Z after its first rising edge. |
| `insufficient_edges` | Fewer than two rising edges, so no period. |
| `multiple_clock_periods` | Candidate clocks have different periods. |

`non_integer_period` is inferred, not proven, and is only considered when an
edge is off the grid. It needs at least seven rising edges, gaps of exactly
two adjacent lengths each seen at least twice, a total span that is not a
whole multiple of the gap count, and every rise within one tick of the
straight line from the first rise to the last. Tick rounding fits this;
some jitter patterns do too.

The verdict is advisory and never changes a query's mode. Which findings
warn in queries:

- `off_grid_edge`, `non_integer_period` and `unknown_clock` warn on every
  sync query, and on async queries that use cycle tokens.
- `insufficient_edges` gives the one no-clock warning on sync queries.
- `multiple_clock_periods` never warns in queries. It shows in the `build`,
  `overview` and `stats` header and in JSON `clock_regularity`.

JSON carries the verdict in `clock_regularity` and the warnings in
`warnings`. On `off_grid_edge`, `non_integer_period` or `unknown_clock`,
use `--async` with tick or time bounds. On `insufficient_edges`, select
another clock with `--clock` or use `--async`. On `multiple_clock_periods`,
select the intended domain with `--clock PATTERN`.

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
- Pre-edge needs a usable clock. `--sample-mode` is rejected with `--async`
  and by `list`, `overview`, and `stuck`.

### Reset auto-skip

By default sync mode skips output until the reset
deasserts, so your first cycle is "real" simulation
rather than initialisation.

- **Detection**: the shallowest 1-bit signal (then
  alphabetically first) whose last name part contains
  `rst` or `reset`, such as `tb.rst_n` or `tb.dut.reset`.
  `--reset PATTERN` picks a 1-bit signal by glob
  instead.
- **Polarity**: from the name. `rstn`, `rst_n`, `rst_ni`,
  `reset_b` and `rst_l` are active-low, also with a suffix such as
  `rst_n_sync`; `rst`, `reset`, `rst_gen`, `rst_in` and `reset_en`
  are active-high.
- **Deassertion**: the first transition to the inactive
  value. A reset that starts at X or Z stays asserted
  until then, and one that never deasserts skips
  nothing.
- **Header**: every sync query prints one line on stderr
  and `overview` prints it on stdout. Async output has
  none.

  ```
  # reset: tb.rst_n (active-low by name, auto) deasserts @ 25ns; 2 cycles skipped (--with-reset to include)
  ```

  Without a reset it reads
  `# reset: none detected; no cycles skipped`.
- **JSON**: `--format json` adds a `reset` object with the
  same facts.
- **`--with-reset`** keeps the reset phase; the header
  then says so and `skipped_cycles` is 0.
- **Bad `--reset`**: a glob that does not compile or
  matches no 1-bit signal exits 2 naming the pattern.

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

A single store supports both modes, with no rebuild. The store holds the
full asynchronous transition stream, and sync mode is a view computed from
it at query time, so the two modes always agree:
`bwave signal sim.fst -s "*" -t 0:100` and
`bwave signal sim.fst -s "*" -t 0t:1000t --async` inspect the same data.

Time arguments change meaning with the mode: bare integers are cycles in
sync mode, and async mode rejects them and requires a unit suffix (`100t`,
`100ns`, or `100c` to convert cycles). See `reference/time-tokens`.

## Quick decision guide

- Default to sync. It matches your mental model of "what
  did the FSM do on cycle 1234".
- Switch to async when sync hides something (combo
  glitch, async reset, no clock).
- If sync output looks 1 cycle off, recheck. You're
  probably staring at post-edge values from the cycle
  *after* the one you think you set up.
- If sync output is empty or wrong, run `bwave overview` and check the clock
  and its verdict (Clock regularity, above).

# bwave signal

## Synopsis

```bash
bwave signal <FST_FILE> [-s PATTERN[%RADIX] ...] [-t START:END]
            [--async] [--clock PAT] [--reset PAT] [--with-reset]
            [--format text|json] [--limit N]
```

## Semantics

Cycle-by-cycle (or transition-by-transition in async)
trace of selected signals. The default query view: one
line per cycle, columns are the signals, only cycles where
*something changed* are emitted (no blank-cycle padding).
Good for narrative debugging: "what happens around cycle
1000?".

In sync mode (default) values are sampled at the rising
clock edge and reflect post-edge state. In async mode each
VCD transition is its own row with the raw timestamp.

## Defaults and requiredness

- `<FST_FILE>` required.
- `-s PATTERN` optional, repeatable. Omitted means all
  signals (warning: that's typically too many; clap
  caps via `--limit`).
- `-t START:END` optional. Open-ended works:
  `-t 100:`, `-t :500`. A single `-t 100` is treated as
  `[100, 100]` (one cycle).
- Time units: bare integers are cycles in sync mode;
  async mode requires a unit suffix (`100t`, `100ns`).
  See `reference/time-tokens`.

## Output shape

Text mode prints a header showing the resolved clock and
reset, then change rows:

```
# clock: tb.dut.clk  period=10ns
# reset: tb.dut.rst_n  deasserts @ cycle 5
# signals: state, data_out
cycle     state  data_out
       6       1  'h00000000
      12       2  'h00000001
      18       3  'h0000DEAD
```

In async mode the first column is `time` (timescale units)
rather than `cycle`.

`signal` prints *transitions*. When nothing changes inside
the requested window — the common `-t N:N` "what is it
holding right now?" query — it falls back to the held
value at the window start and says so on stderr:

```
# no transitions in cycles 56:56 — showing held values at cycle 56
      56  expanded_key  'h62636363
```

`value --at 56` remains the direct way to ask that
question, and snapshots every matching signal.

JSON mode is **not yet implemented** for `signal`. Use
text mode for now, or `value`/`find`/`stats` if you need
structured output.

## Common errors

- **Output starts at cycle 6 instead of 0**: reset phase
  was auto-skipped. Pass `--with-reset` to include it. See
  `troubleshooting/reset-skipping`.
- **Off-by-one vs. waveform viewer**: sync mode samples
  *post-edge*. The value on cycle N is what flip-flops
  drive after capturing input. This matches GTKWave's
  default cursor behaviour. See `reference/sync-vs-async`.
- **Too many rows, output truncated**: `--limit` is
  2000 by default. Either narrow with `-s`, tighten `-t`,
  or raise `--limit` (max 10000 in the wrapper).
- **`-s "data%h"` shows binary anyway**: radix suffix
  must come AFTER the pattern, with no space. `data%h`
  not `data %h`. See `reference/verilog-literals` and
  `troubleshooting/radix-pitfalls`.

## Examples

Trace an FSM around its first transition:

```bash
bwave signal sim.fst -s "*fsm_state*" -t 0:200
```

Multi-signal trace with decimal radix on data:

```bash
bwave signal sim.fst -s "*state*" -s "*data*%d" -t 1000:1100
```

Async (raw VCD timestamps) view, useful for combo logic
glitches between clock edges:

```bash
bwave signal sim.fst -s "*valid*" -s "*ready*" -t 0t:1000t --async
```

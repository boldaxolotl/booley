# bwave signal

## Semantics

Trace of selected signals over time: one row per change, so cycles where
nothing changed are omitted. Use it for narrative debugging: "what happens
around cycle 1000?". Sync mode samples at clock edges; `--async` prints
every VCD transition with its raw tick (`reference/sync-vs-async`). Without
`-s`, every signal is traced, which is usually too many.

## Output shape

Text mode prints time/name/value change rows:

```
0 b 0
1000 b 1
5000 b 2
```

In async mode the first column is the raw tick rather
than the cycle.

`signal` prints *transitions*. When nothing changes inside
the requested window — the common `-t N:N` "what is it
holding right now?" query — it falls back to the held
value at the window start and says so on stderr:

```
# no transitions in range — showing held values at cycle 3
3 data_a[7:0] 39
```

`value --at 56` remains the direct way to ask that
question, and snapshots every matching signal.

`--format json` returns a structured envelope.

## Common errors

- **The reset phase is missing**: sync mode skips it, and output starts at
  cycle 0 (the tick before reset deasserts) or cycle 1. Pass `--with-reset` to include it;
  the `# reset:` header says what was skipped (`reference/sync-vs-async`).
- **Off by one cycle against a waveform viewer**: sync
  mode reads post-edge values by default. See the sampling
  modes in `reference/sync-vs-async`.

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

## Clock-grid behavior

- A clock stop prints a `# clk stopped A:B` line on **stdout**, in order with
  the value rows. It counts toward `--limit`.
- Changes inside the stop keep their grid-cycle numbers.
- All other diagnostics go to stderr.
- JSON `records` is an ordered array of `kind: value` and
  `kind: clock_stopped` entries. Values use the selected display radix.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

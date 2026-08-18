# Subcommand overview

Every B-Wave invocation starts with a subcommand. There is
no implicit fallback: `bwave foo.fst` is an error. Pick
one of the fourteen commands below.

## Build

| Command | One-line |
|---|---|
| `build` | Parse a VCD and write an FST waveform store (`.fst`). |

Run once per simulation. Every query subcommand consumes
the resulting `.fst` store, which is plain FST and also
opens directly in GTKWave and VaporView.

## Query (consumers of values)

These commands answer questions about signal values over
time. All of them accept `--virtual` and `--marker`.

| Command | One-line |
|---|---|
| `signal` | Cycle-by-cycle trace of N signals (default query view). |
| `wave` | Horizontal waveform table: rows are signals, columns are cycles. |
| `value` | Snapshot of N signals at one time point. |
| `find` | Cycles where PATTERN equals VALUE or an edge keyword. |
| `sample` | Snapshot of N signals each time TRIGGER fires. |
| `diff` | Compare signal values between two time points. |
| `distance` | Time between events: same-signal periods or two-event A to B latency. |

## Introspect (no values, summaries only)

These read the store but don't surface raw signal values.

| Command | One-line |
|---|---|
| `list` | Signal tree: names, widths, var types. |
| `stats` | Transition counts, toggle %, time-in-state per signal. |
| `stuck` | Signals that never changed (optionally pinned to a value). |

## Meta

| Command | One-line |
|---|---|
| `schema` | Print the JSON Schema for `--format json` envelopes to stdout. |
| `docs` | Browse this corpus. `topics`, `search`, `show`. |
| `skill` | Print the agent skill markdown. |

## Shared global options

Available on every query/introspect subcommand:

- `--async`: switch from sync (cycles) to async (raw
  ticks). See `reference/sync-vs-async`.
- `--clock PATTERN`: override the auto-detected clock.
- `--reset PATTERN`: override the auto-detected reset.
- `--with-reset`: include the reset phase in output
  (default skips it).
- `--format text|json`: output format. Default `text`.
- `--limit N`: max output lines. Default 2000.

## Consumer-only options

Available on `wave`, `find`, `sample`, `distance`, `value`:

- `--virtual "name = expr"`: define a boolean predicate
  over existing signals; usable like any other signal.
- `--marker NAME CYCLE`: pin a named cycle for display
  (especially in `wave`).

## File-input rule

Every query/introspect subcommand requires a built `.fst`
store as the first positional argument. Pass a `.vcd` and
you'll get an error with a `bwave build` hint.

(Query subcommands other than `wave`, `diff`, and
`distance` keep a hidden `--allow-vcd` flag for legacy
streaming use; do not rely on it.)

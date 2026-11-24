# Subcommand overview

Every B-Wave invocation starts with a subcommand. There is
no implicit fallback: `bwave foo.fst` is an error. Pick
one of the commands below.

## Build

| Command | One-line |
|---|---|
| `build` | Parse a VCD and write an FST waveform store (`.fst`). |

Run once per simulation. Every query subcommand consumes
the resulting `.fst` store.

## Query (consumers of values)

These commands answer questions about signal values over
time. Their command-specific options differ; consult each
command's live `--help`.

| Command | One-line |
|---|---|
| `signal` | Transition list of N signals: when did each one change. |
| `wave` | Horizontal waveform table: rows are signals, columns are cycles. The usual first query. |
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
| `overview` | Clock-grid verdict, stopped-stretch summary and trace metadata. |
| `stats` | Transition counts, toggle %, time-in-state per signal. |
| `stuck` | Signals that never changed (optionally pinned to a value). |

## Meta

| Command | One-line |
|---|---|
| `schema` | Print the JSON Schema for `--format json` envelopes to stdout. |
| `docs` | Browse this corpus. `topics`, `search`, `show`. |
| `skill` | Print the agent skill markdown. |

## Booley wrapper

The `bwave` installed with Booley wraps the native binary and adds:

| Command | One-line |
|---|---|
| `register` | Register a store or sim directory as a session. See `reference/sessions`. |
| `markers` | Name cycles on a session. See `reference/markers`. |
| `gui` | Check a waveform selection; no viewer for now. See `commands/gui`. |

## Shared global options

Available on every query/introspect subcommand:

- `--async`: switch from sync (cycles) to async (raw
  ticks). See `reference/sync-vs-async`.
- `--clock PATTERN`: override the auto-detected clock.
- `--reset PATTERN`: override the auto-detected reset.
- `--with-reset`: include the reset phase in output
  (default skips it).
- `-s PATTERN`: select signals. See `reference/patterns`.
- `--format text|json`: `bwave schema` lists the commands that emit JSON
  and the exact grammar.
- `--sample-mode post-edge|pre-edge`: read sync values after (default) or
  just before each clock edge. See `reference/sync-vs-async`.
- `--limit N`: max records (rows, signal blocks, or pairs);
  `wave` counts columns. Headers do not count. Default 2000; the Booley
  wrapper caps it at 10000.

## Query-specific options

- `--virtual "name = expr"`: define a Virtual Signal. See
  `reference/virtual-signals` for the commands that accept it.
- `--include-stopped` (`find`, `sample`): literal triggers also match cycles
  inside a clock stop. See `reference/sync-vs-async`.
- `--marker NAME TIME`: label a time in `wave` output. See
  `reference/markers`.

## File-input rule

Every query/introspect subcommand requires a built `.fst`
store as the first positional argument: `list`, `signal`,
`wave`, `value`, `find`, `sample`, `diff`, `distance`,
`stats`, `stuck`, and `overview`. Pass a `.vcd` and you'll get exit code 2
with a `bwave build` hint. Only `build` reads VCD input.

## The `overview` command

`bwave overview STORE_FILE` prints the selected clock, its regularity
verdict, the clock-stop summary, and the `# reset:` line without scanning the
data signals. Run it before trusting cycle numbers; `reference/sync-vs-async`
explains the verdict and the reset line.

- Accepts `--clock`, `--reset`, and `--with-reset`; rejects `--sample-mode`.
- `--format json` puts trace metadata in `data` and the verdict in
  `clock_regularity` and the reset facts in `reset`.

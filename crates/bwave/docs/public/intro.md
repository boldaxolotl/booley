# B-Wave

Signal-query CLI over FST waveform stores (`.fst`) built
from VCD waveforms. Read this when you need to understand a B-Wave
concept before running queries.

## What it is

B-Wave is a command-line EDA tool for inspecting RTL simulation
waveforms. You feed it a VCD file (any simulator: Verilator,
Icarus, VCS, Vivado xsim), it builds an `.fst` waveform
store, and then you query the store instead of re-parsing
the VCD on every question. The store is a plain FST file:
it also opens directly in GTKWave and VaporView.

`.fst` stores are compressed and seek-indexed. Queries
that would take seconds against the raw VCD return in
milliseconds against the store.

## Subcommand families

There are three families plus a meta family:

- **build**: produce an `.fst` store from a VCD.
- **query**: ask questions about signal values, edges,
  windows, and time distances. The heart of the EDA tool.
- **introspect**: list signals, dump stats, find stuck
  nets. Same engine as the query family, different reports.
- **meta**: `schema` prints the JSON schema for
  `--format json` envelopes.

Run `bwave docs show commands/overview` for the full
table, or `bwave docs show commands/<name>` for any single
subcommand.

## Sync vs async (one-liner)

Default sync mode samples each signal once per rising
clock edge and reports cycle numbers. Async mode reports
every transition with raw VCD timestamps. See
`bwave docs show reference/sync-vs-async` for the deep
dive: start there if anything in the output looks off by
a clock period.

## Mental model

- An `.fst` store is a snapshot of a finished simulation.
  You query it, you don't mutate it.
- Signal patterns suffix-match the full hierarchical name
  unless you wrap them in wildcards. See
  `bwave docs show reference/virtual-signals` for the full
  grammar including bit slicing and predicates.
- Values in `find`/`sample`/`distance` are Verilog
  literals (`'d255`, `'hFF`, `'b1010`). Bare hex is
  rejected. See `bwave docs show reference/verilog-literals`.
- Time arguments are typed tokens: bare integers mean
  cycles in sync mode, and async mode requires a unit
  suffix (`100t`, `100ns`). See
  `bwave docs show reference/time-tokens`.

## Output

Text mode (default) is human-readable and stable enough to
grep. JSON mode wraps every payload in a canonical envelope
(`$schema`, `command`, `data`, `warnings`). JSON is
currently implemented for `list`, `value`, `find`, and
`stats`. See `bwave docs show reference/json-envelope`.

## When something goes wrong

Start with the troubleshooting topics:

- `troubleshooting/empty-results`: query returned nothing.
- `troubleshooting/radix-pitfalls`: hex/decimal mismatch.
- `troubleshooting/reset-skipping`: output starts later
  than expected.
- `troubleshooting/clock-detection`: wrong clock picked.

## Where to go next

- `bwave docs show commands/overview`: full subcommand
  table.
- `bwave docs show commands/build`: how to produce a
  store.
- `bwave docs show commands/signal`: the default trace
  view, and a good starting query.
- `bwave docs show reference/sync-vs-async`: the most
  important concept after you've built a store.

---
name: bwave
description: Inspect RTL simulation traces. Works with .fst waveform stores and .vcd traces; registering a sim directory auto-builds an .fst store, while a .vcd passed directly must be converted with `bwave build` before querying.
---

# bwave skill

Inspect RTL simulation traces without ever reading raw VCD or .fst bytes.
This skill says how to *use* the commands. Each fact below links its full
topic: `bwave docs show <topic>`. `bwave <cmd> --help` is authoritative for
flags, and `bwave schema` for `--format json`.

**Never** `cat` / `Read` a `.fst` or `.vcd`: they can be hundreds of MB. Keep
globs narrow (`-s "tb.dut.*"`) and ranges bounded (`-t 0:100`). Prefer
`--format json` and parse it; text output is for humans, so do not
pattern-match it. Output stops at `--limit` (default 2000): check
`warnings` for truncation, since only some commands carry `truncated`.
Narrow the query rather than raising the limit.

## Mental model

- **Input**: queries need an `.fst` store; `bwave build trace.vcd -o
  trace.fst` converts a VCD (`commands/build`).
- **Sessions**: put the alias *first*, `bwave @dut find ...`; with no alias
  a query reads the default session, the last traced sim
  (`reference/sessions`).
- **Patterns**: a bare `-s` name matches a suffix at any depth; a glob is
  anchored at the `# scope:` line (`reference/patterns`).
- **Sync mode (default)**: rows are clock cycles, sampled after each edge,
  starting after reset deasserts; the clock and reset are auto-detected
  (`--clock`, `--reset`, `--with-reset` override). Run `bwave overview`
  before trusting cycle numbers; on a clock warning, switch to `--async`
  (`reference/sync-vs-async`).
- **Async mode (`--async`)**: raw timestamps, and time tokens need a unit
  suffix: `100t`, `5us` (`reference/time-tokens`).
- **Values**: Verilog literals, `'d255`, `'hFF`, `8'd255`; bare hex is
  rejected (`reference/verilog-literals`).
- **Virtual Signals**: `--virtual "hsk = *valid & *ready"` defines a 1-bit
  predicate (`reference/virtual-signals`).
- **Wide values**: `wave` elides values past 24 characters so one wide bus
  cannot set the column width of the whole table.

## Reach for `wave` first

`wave` puts signals on rows and cycles on columns, so a misalignment — between
two pipeline stages, or between a control signal and the data it gates — shows
up as a column offset. That is the question most debug sessions actually have.
`signal` prints a transition list, which answers "when did this change?" but
makes cross-signal comparison a manual join.

When you know a divergence exists but not where, don't scan by eye: `diff T1 T2`
compares two time points, `stuck` finds signals pinned at a constant, and
`distance` measures the gap between two events.

```bash
bwave build dump.vcd -o dump.fst          # build once, query the store many times
bwave @dut wave -s "tb.dut.fifo.*" -t 1180:1260
bwave @dut value --at 1000   -s "tb.dut.*"
bwave @dut value --at 5000ns -s "tb.dut.*" --async   # async needs a unit suffix
bwave @dut distance "tb.dut.req_fire" rising --to "tb.dut.resp_fire" rising --stats
bwave @dut find hsk 'h1 --virtual "hsk = *valid & *ready" --count
```

## Showing the human a waveform

Booley has no waveform viewer for now. When the human needs to see something,
report what the queries found as text: the signal names, the times and the
values.

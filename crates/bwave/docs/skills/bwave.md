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

`bwave gui` is for the HUMAN; you read values with the query commands. When
the human needs to see something, query first, then show a scoped view of
what you found, in named groups with a radix and color per selector. It
needs an attached VS Code window and hard-errors when none answers: pass the
setup hint on rather than assuming it worked. How to compose the view:
`commands/gui`.

```bash
bwave @dut find "tb.dut.fifo.overflow" rising --first
bwave gui @dut \
  --signals 'tb.clk%b@red' \
  --group 'FIFO handshake=tb.dut.fifo.*%h@green' \
  --group 'Control=tb.dut.ctrl.state%h@blue' \
  --time 1180c:1260c
```

## Common errors

- `ERROR: no signals match pattern(s) ...` (exit 2): every pattern matched
  nothing. Bare-name patterns use suffix-match — `dmem_addr` matches
  `tb.dut.dmem_addr` but NOT `tb.dut.dmem_addr_next`. A glob (`*`, `?`, `[`)
  matches the full path or the path relative to the store's common scope: the
  `# scope:` line an unfiltered `bwave list` prints, so with `# scope: TOP.tb`
  `dut.*` finds `TOP.tb.dut.*` at any depth. It is anchored at that scope: it
  never reaches `TOP.tb.other.dut.x` or anything outside it. (A narrowed
  `list -s` prints a deeper scope, which is not the anchor.) When a glob
  matches only below some other scope, the error carries
  `did you mean 'TOP.tb.dut.u_core.*'?` (the first such place in store
  order); paste that form. Use `*dmem_addr*`
  for substring, and `bwave list` to see what exists. JSON-mode `find`/`value`/
  `stats` still print an empty envelope on stdout alongside the error.
- `# WARNING: no signals match '<pat>' — filter dropped`: one `-s` of several
  matched nothing, so your result is missing that row (exit stays 0 because a
  sibling matched). **Do not read the partial table as the whole answer.**
  Before assuming a typo, check `bwave list`: the signal may never have been
  *dumped*. Unpacked arrays and memories (`reg [15:0] mem [0:15]`) are commonly
  absent from the trace entirely, and an iverilog log full of
  `VCD warning: ignoring signals in previously scanned scope` says the dumper,
  not your pattern, is at fault. No pattern will recover a signal that was
  never written.
- **A bare name matches at every scope depth.** `-s o_data` returns both
  `o_data` and `tb.dut.o_data` as separate identical rows, doubling the cost of
  the widest signals. Anchor the pattern when you know the scope.
- `ERROR: waveform store has no signals` (exit 2 on queries, exit 0 on `list`):
  the store is empty — a header-only trace.fst, the signature of a Verilator
  sim traced via the auto-generated `--main`. Re-run with a custom C++ `--exe`
  main; no pattern will help. (`bwave build` refuses to *create* such a store —
  `input VCD declares no signals`, exit 2 — so an empty store always came from
  an external producer writing FST directly.)
- **`register` refused a raw `.vcd`**: queries need a built store, so an alias
  bound to a VCD could never answer one. `bwave build <vcd> -o <out>.fst` first,
  or `register <vcd> --as ALIAS --build` to do it in one step.
- **`register <sim dir>` says `No trace file found`** while a `.vcd` sits in
  that directory: the testbench wrote its dump under its own name. Declare it in
  `[flows.sim].trace_files`, or point `register`/`build` straight at the
  file.
- **Async query returned only the t=0 row** (exit 0, no warning): the window
  held no transitions — usually because it is outside the trace. `list` reports
  `total_ticks` in *ticks*, not nanoseconds; converting by eye is how you land
  past the end. Check the timescale, or drop `--async` and use cycles.
- **`signal` printed nothing for `-t N:N`**: it prints *transitions*. It falls
  back to the held value and says so, but `value --at N` is the direct way to
  ask "what is it holding right now?".
- **`wave` cells show `AABB..CCDD`**: values wider than 24 chars are elided so
  one 512-bit bus can't set the column width for the whole table. Use
  `value --at CYCLE` or `signal` for full width.
- **Output began `... (truncated, showing last N bytes)`**: that is the MCP
  stdout cap, and it keeps the **tail** — the rows you asked for first are the
  ones that were dropped. Re-running unchanged returns the same truncation;
  narrow `-s` or `-t` instead.
- `unknown radix suffix '%u'` (exit 2): only `%h`, `%d`, `%b` exist.
- `--first and --last are mutually exclusive` (exit 2).
- `bad --virtual` syntax: parens required to mix operators (`(*a & *b) | *c`),
  signal-to-signal comparisons require equal widths, bare hex rejected (use
  `'h0C` not `0C`).

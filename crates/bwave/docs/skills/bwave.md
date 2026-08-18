---
name: bwave
description: Inspect RTL simulation traces. Works with .fst waveform stores and .vcd traces; registering a sim directory auto-builds an .fst store, while a .vcd passed directly must be converted with `bwave build` before querying.
---

# bwave skill

Inspect RTL simulation traces without ever reading raw VCD or .fst bytes. The
store it builds is a plain FST file, so it also opens in GTKWave and VaporView.
`bwave --help` lists the commands and flags; this skill is how to *use* them.

**Never** `cat` / `Read` a `.fst` or `.vcd` — they can be hundreds of MB. Keep
globs narrow (`-s "tb.dut.*"`) and ranges bounded (`-t 0:100`); prefer
`--format json` and parse it. Output caps at `--limit` lines (default 2000);
when an envelope says `truncated: true`, narrow the query rather than raising
the limit.

More depth, in order: `bwave <cmd> --help` (authoritative flags) →
`bwave docs topics` / `docs show <topic>` / `docs search <q>` (narrative
corpus) → `bwave schema` (JSON Schema for `--format json`).

## Mental model

- **Sync mode (default)**: every emitted row is indexed by *clock cycle*. Cycle
  1 is the first rising edge after reset deasserts. The clock is auto-detected
  (`*clk*` heuristic, shallowest scope wins) — override with `--clock PATTERN`.
  Reset is auto-skipped — override with `--with-reset` or `--reset PATTERN`.
- **Async mode (`--async`)**: every transition reported at its raw VCD
  timestamp. No cycle numbering, no reset skipping, and time tokens *require* a
  unit suffix.
- **Verilog literals on values**: `bwave find sig 'd255`, `'hFF`, `'b1010`,
  width-prefixed `8'd255`. Bare hex like `FF` is *rejected*.
- **Virtual signals**: `--virtual "name = expr"` (Verilog-subset boolean
  expressions), on `wave`, `find`, `sample`, `distance`, `value` only. See
  `bwave docs show reference/virtual-signals`.
- **JSON envelope**: `list`, `value`, `find` and `stats` wrap output in
  `{$schema, command, data, warnings}`; every other command emits text only.
  The envelope is the contract for programmatic callers (`bwave schema` has the
  full grammar). Text output is for humans — do not pattern-match it.

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
bwave value @dut --at 1000   -s "tb.dut.*"
bwave value @dut --at 5000ns -s "tb.dut.*" --async   # async needs a unit suffix
bwave distance @dut "tb.dut.req_fire" rising --to "tb.dut.resp_fire" rising --stats
bwave find @dut hsk 'h1 --virtual "hsk = *valid & *ready" --count
```

## Showing the human a waveform

`bwave gui` is for the HUMAN. It is not how *you* read values — `signal` /
`find` / `value` / `stats` answer those with no viewer running. When the human
does need to see something, query first, then show a scoped view of what you
found:

```bash
bwave find @dut "tb.dut.fifo.overflow" rising --first
bwave gui  @dut --signals 'tb.dut.fifo.*' --time 1180c:1260c
```

It requires VaporView's WCP control server in the user's VS Code window and
hard-errors when that is off — surface the setup hint rather than assuming it
worked. Flag semantics (clock row, markers, `--append`, `--max-signals`):
`bwave docs show commands/gui`.

## Common errors

- `ERROR: no signals match pattern(s) ...` (exit 2): every pattern matched
  nothing. Bare-name patterns use suffix-match — `dmem_addr` matches
  `tb.dut.dmem_addr` but NOT `tb.dut.dmem_addr_next`. Use `*dmem_addr*` for
  substring, and `bwave list` to see what exists. JSON-mode `find`/`value`/
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

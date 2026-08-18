# bwave build

## Synopsis

```bash
bwave build <VCD_FILE> -o <FST_FILE> [--scope SCOPE] [--input PATH]
```

## Semantics

Parse a VCD waveform and write an FST waveform store
(`.fst`). The store is a plain FST file: the full
transition stream, the original timescale, and the scope
tree, losslessly — it opens directly in GTKWave and
VaporView. Subsequent queries hit the store instead of
re-parsing the VCD.

`build` is the only B-Wave subcommand that does
significant work: every query after it is cheap.

## Defaults and requiredness

- Positional `<VCD_FILE>` is optional. If omitted and no
  `--input` is given, the VCD is read from stdin.
- `-o / --output <FST_FILE>` is **required**.
- `--scope SCOPE` is optional. When set, only signals
  inside that hierarchical scope (e.g. `tb.dut`) are
  recorded. Useful for trimming massive testbenches.
- `--input PATH` reads from PATH instead of the positional
  argument. The blocking open() makes it the right choice
  for named pipes / FIFOs where the writer side may not
  open until later. Cannot be combined with a positional
  VCD argument.

## Heartbeat sidecar

While building, B-Wave writes a `<output>.progress` file
periodically and removes it on success. External stall
monitors (the Booley FIFO watchdog) read this file to
distinguish "still parsing a huge VCD" from "stuck". The
file is best-effort; absence means the build finished or
the process died before any progress was made.

## Output shape

`build` does not write to stdout. The `.fst` file is the
sole artifact. Errors go to stderr. Exit code 0 on success.
On success it prints one line to stderr — `# wrote <path>` —
so stdout stays empty for piping.

There is no JSON mode for `build`: the result is a binary
file, not a payload.

## Common errors

- **`cannot open '<vcd>'`**: wrong path, or the file
  doesn't exist yet (race with the simulator). For FIFOs,
  use `--input <fifo>` so B-Wave blocks until the writer
  appears.
- **`-o / --output is required`**: clap rejects the
  invocation. The output path must be explicit; there is
  no implicit `<vcd>.fst` default.
- **`--input conflicts with VCD_FILE`**: pick one source.
  Combining them is ambiguous so clap rejects it.
- **`.fst` file truncated**: the FST is only flushed and
  finalized at end-of-VCD. If the simulator crashed
  mid-write, the resulting store is incomplete and queries
  will fail to load. Re-run the simulation, then rebuild.
- **`-o <name>.bwave` rejected (exit 2)**: the legacy
  `.bwave` format was retired; the store is plain FST now.
  The error reads `ERROR: the .bwave format was replaced
  by FST; rebuild with 'bwave build <vcd> -o trace.fst'`.
  Use a `.fst` output path.
- **`input VCD declares no signals` (exit 2)**: the VCD
  header carries zero `$var` declarations, so the store
  would be header-only and answer every query with
  silence — `build` refuses to write it. The classic
  producer is a Verilator sim traced via the
  auto-generated `--main`; trace via a custom C++
  `--exe` main instead.
- **`--scope '...' matches none of the N signal(s)`
  (exit 2)**: the scope filter would drop everything,
  which is the same unqueryable store by another road.
  Drop `--scope`, or build unscoped and `bwave list` to
  find the real hierarchy prefix.

## Examples

Standard build from a finished VCD:

```bash
bwave build sim.vcd -o sim.fst
```

Stream from stdin (useful when piping from a compressed
archive):

```bash
gunzip -c sim.vcd.gz | bwave build -o sim.fst
```

Limit the store to a single DUT scope to drop testbench
plumbing:

```bash
bwave build sim.vcd -o dut.fst --scope tb.dut
```

FIFO mode: start B-Wave before the simulator runs so the
FIFO open() blocks both sides correctly:

```bash
mkfifo /tmp/sim.vcd
bwave build --input /tmp/sim.vcd -o sim.fst &
my_simulator +vcd=/tmp/sim.vcd
wait
```

# bwave build

## Semantics

Parse a VCD and write a plain FST store (`.fst`): the full transition
stream, the original timescale, and the scope tree, losslessly. It opens
directly in GTKWave and Surfer. Every query reads the store instead of
re-parsing the VCD, so `build` is the only expensive step.

Use `--input PATH` rather than the positional path for a named pipe (FIFO):
its blocking open waits until the simulator opens the writer side.

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
On success it prints `# wrote <path>` to stderr, followed by
the clock verdict and clock-stop summary, so stdout stays
empty for piping. A `warning` verdict does not fail the
build: the store still serves `--async` queries. See
`reference/sync-vs-async`.

There is no JSON mode for `build`: the result is a binary
file, not a payload.

## Common errors

- **Store ends early**: a VCD cut short (the simulator
  crashed mid-write) still builds, exit 0, from the
  transitions before the cut. Check the trace end with
  `bwave stats`, then re-run the simulation and rebuild.

## Examples

```bash
bwave build sim.vcd -o sim.fst

# From a compressed archive, via stdin
gunzip -c sim.vcd.gz | bwave build -o sim.fst

# FIFO: start B-Wave first so both opens block correctly
mkfifo /tmp/sim.vcd
bwave build --input /tmp/sim.vcd -o sim.fst &
my_simulator +vcd=/tmp/sim.vcd
wait
```

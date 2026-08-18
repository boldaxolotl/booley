# Markers

Named cycle references. A marker is just `(NAME,
CYCLE)`. Once defined for a query, B-Wave shows the
name above the cycle column in `wave` output and (via
the Python wrapper integration) lets you refer to the
cycle by name in subsequent queries.

## CLI usage on consumer subcommands

```
--marker NAME CYCLE
```

Repeatable. Accepted on: `wave`, `find`, `sample`,
`distance`, `value`, `signal`.

The CYCLE is a literal integer. In sync mode it's a
cycle number; in async mode it's a tick. The typed time
tokens of `-t`/`--at` (see `reference/time-tokens`) do
not apply here: `--marker` takes bare integers only.

## Visual rendering in `wave`

`wave` is where markers shine. Each marker becomes a
label above the cycle header, anchored at its column:

```
              err_start              dma_done
              v                      v
              500         510         520         530
              |---------- |---------- |---------- |----------
clk           ^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_
state         'h2  'h2  'h2  'h5  'h5  'h5  'h0  'h0  'h0
```

B-Wave silently drops markers outside the `-t` window
(no warning).

## Other commands

In `find`, `sample`, `distance`, `value`, `signal` the
marker is accepted but not visually rendered: the
binary just remembers the names. The intended use is
that another component (the Python wrapper) sets markers and
then references them by name in subsequent invocations.

## Wrapper integration: `bwave markers`

The Booley `booley.bwave.cli` wrapper has a `markers`
subcommand that persists named markers per registered
trace alias. Workflow:

1. Register a trace: `bwave register @dut sim.fst`.
2. Set named markers: `bwave markers @dut set
   err_start 1234`.
3. Use the name anywhere a cycle is expected:
   `bwave @dut wave -t err_start:dma_done -s "*err*"`.

The wrapper resolves names to integers and passes
`--marker NAME CYCLE` (and the equivalent integer for
`-t`) into the binary. From the binary's perspective the
input is plain integers and `--marker` annotations; the
naming is wrapper-layer state.

This means: the binary does **not** persist markers.
Every invocation that wants named markers has to either
declare them with `--marker` or go through the wrapper.

## Common patterns

Set markers around an error window and inspect:

```bash
# via wrapper
bwave markers @dut set err_start 1234
bwave markers @dut set err_done 1450
bwave @dut wave -t err_start:err_done -s "*err*"
```

Annotate a wave plot without persistence (binary
directly):

```bash
bwave wave sim.fst -s "*err*" -t 1200:1500 \
    --marker err_start 1234 --marker err_done 1450
```

Multiple markers in one invocation:

```bash
bwave wave sim.fst -s "*fsm*" -t 0:500 \
    --marker reset_done 5 \
    --marker first_req 42 \
    --marker first_ack 46
```

## Constraints

- Marker names are arbitrary strings but should avoid
  whitespace and shell metacharacters (you'll fight
  quoting otherwise).
- Two markers with the same name in one invocation:
  last one wins (no error).
- Negative cycle markers are accepted (useful when
  `--with-reset` is set and the reset extended into
  negative cycle space relative to the deassertion
  origin).
- Markers do not affect query semantics. They are
  pure annotation.

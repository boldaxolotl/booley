# bwave value

## Synopsis

```bash
bwave value <FST_FILE> --at N [-s PATTERN[%RADIX] ...]
            [--async] [--clock PAT] [--reset PAT] [--with-reset]
            [--virtual "name = expr"]
            [--format text|json] [--limit N]
```

## Semantics

Snapshot of selected signals at one time point. Unlike
`signal` (a trace across cycles), `value` answers "what
were these signals on cycle N?". Cheap: a single store
lookup per matched signal.

In sync mode a bare `--at N` is a cycle number. In async
mode the timestamp needs a unit suffix (`12345t`,
`500ns`); see `reference/time-tokens`. If sync is
requested but no clock was found, `--at` is interpreted
as a tick and the output reports `at_unit: "tick"`.

## Defaults and requiredness

- `<FST_FILE>` required.
- `--at N` required. Integer, signed (negative cycles are
  valid before reset deasserts when `--with-reset`).
- `-s PATTERN` optionally selects stored and Virtual Signal
  rows. Without it, every stored and virtual row is selected.
- Standard global options apply.

## Output shape

Text mode prints a header line and then one signal per
row:

```
# Snapshot at cycle 1234 (tick 12345)
state          'h2
data_out       'h0000DEAD
valid          'h1
ready          'h0
```

JSON mode emits a `valueData` envelope:

```json
{
  "$schema": "...",
  "command": "value",
  "data": {
    "scope_prefix": "tb.dut",
    "mode": "sync",
    "at": 1234,
    "at_unit": "cycle",
    "target_tick": 12345,
    "time_label": "cycle 1234 (tick 12345)",
    "signals": [
      {"name": "state",    "value": "2"},
      {"name": "data_out", "value": "DEAD"},
      {"name": "valid",    "value": "1"},
      {"name": "ready",    "value": "0"}
    ]
  },
  "warnings": []
}
```

The `value` field is the **raw store value**: no
Verilog-literal prefix, no padding. Renderers add prefixes
in text mode based on the radix suffix. See
`reference/json-envelope`.

## Common errors

- **`unknown argument --at-cycle`**: that flag was
  removed in v0.2. Use `--at N` and rely on sync/async
  mode to disambiguate units.
- **`--at` out of range**: N exceeds the simulation
  length. The store reports its max cycle / tick; use
  `bwave stats` to find the bounds.
- **Value shows `'hX` or `'hZ`**: signal really is X/Z
  at that cycle (typically pre-reset or driven by
  uninitialised logic). Not a B-Wave bug.
- **`# Snapshot at tick N` instead of `cycle N`**: no
  clock was detected. Either pass `--clock PATTERN` or
  switch to `--async`. See
  `troubleshooting/clock-detection`.

## Examples

Snapshot the FSM state at the cycle of interest:

```bash
bwave value sim.fst --at 1234 -s "*state*" -s "*data*"
```

Decimal radix for counters:

```bash
bwave value sim.fst --at 500 -s "*counter*%d"
```

Async snapshot at a specific timestamp (sim has no
clock):

```bash
bwave value sim.fst --at 12345t --async -s "*"
```

Snapshot driven by a virtual predicate as well:

```bash
bwave value sim.fst --at 1000 \
    --virtual "hsk = *valid & *ready" \
    -s "hsk" -s "*state*"
```

Virtual definitions may be unselected helpers for later
definitions. They are evaluated but omitted from the snapshot:

```bash
bwave value sim.fst --at 1000 \
    --virtual "valid = *req & *ready" \
    --virtual "stalled = valid & *busy" \
    -s "stalled"
```

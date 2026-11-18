# bwave value

## Semantics

Snapshot of selected signals at one time point. Unlike
`signal` (a trace across cycles), `value` answers "what
were these signals on cycle N?". Cheap: a single store
lookup per matched signal. `--at` takes a time token
(`reference/time-tokens`); without a usable clock it is a
tick (`reference/sync-vs-async`).

## Output shape

Text mode prints a header line and then one signal per
row:

```
# Snapshot at 1000
b                                        = 1
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
Verilog-literal prefix, no padding. Text mode renders it in
the pattern's radix, also without a prefix. `bwave schema`
has the full grammar.

## Examples

```bash
bwave value sim.fst --at 1234 -s "*state*" -s "*data*"
bwave value sim.fst --at 12345t --async -s "*"
bwave value sim.fst --at 1000 \
    --virtual "hsk = *valid & *ready" -s "hsk" -s "*state*"
```

## Clock-grid behavior

- A sync point inside a clock stop reports its cycle and the stop's full
  range. JSON lists the stop in `clock_stopped`.
- `--sample-mode pre-edge` reads just before the same edge; the cycle number
  does not change. In JSON, `target_tick` is the edge and `sample_tick` is
  the tick actually read.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

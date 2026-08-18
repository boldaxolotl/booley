# bwave stats

## Synopsis

```bash
bwave stats <FST_FILE> [-s PATTERN[%RADIX] ...] [-t START:END]
            [--async] [--clock PAT] [--reset PAT] [--with-reset]
            [--format text|json] [--limit N]
```

## Semantics

Per-signal activity report. For each matching signal,
B-Wave counts transitions, computes toggle %, builds a
value histogram (how often each value held) and a
time-in-state map (how many ticks each value held).
Useful for "is this signal active?", "which state
dominates?", and as a sanity check before deeper
queries.

`stats` does **not** accept `--virtual` or `--marker`:
it's an introspection command, not a value consumer.

## Defaults and requiredness

- `<FST_FILE>` required.
- `-s PATTERN` optional. Without it, every signal is
  reported (often a wall of output; use `--limit` or
  narrow with `-s`).
- `-t START:END` optional. Bounds the stat window.
- Standard global options apply.

## Output shape

Text mode, one summary block per signal:

```
# simulation: 100000 ns (10000 cycles, 100ns period)
# clock: tb.dut.clk

tb.dut.state                  width=3 transitions=42  toggle=0.42%
   value_hist:    'h0:5000  'h1:3000  'h2:1500  'h3:500
   time_in_state: 'h0:50000ns  'h1:30000ns  'h2:15000ns  'h3:5000ns

tb.dut.data_out               width=32 transitions=187 toggle=1.87%
   value_hist:    'h00000000:8000  'h0000DEAD:1500  'h00C0FFEE:500
   time_in_state: 'h00000000:80000ns  'h0000DEAD:15000ns ...
```

JSON mode emits a `statsData` envelope. `value_hist`
and `time_in_state_ticks` keys are **Verilog literals**
(`'hFF`, `'d255`, `'b101`) matching the text-mode
display radix for that signal, so callers can grep
the same key in both modes. The duration map is named
`time_in_state_ticks` to make the unit unambiguous;
a parallel `time_in_state_ns` is emitted whenever the
trace has a timescale.

```json
{
  "$schema": "...",
  "command": "stats",
  "data": {
    "simulation_ns": 100000,
    "total_ticks": 100000,
    "total_cycles": 10000,
    "clock_period_ns": 10,
    "signals": [
      {
        "name": "tb.dut.state",
        "width": 3,
        "transitions": 42,
        "toggle_pct": 0.42,
        "value_pct": 0.50,
        "value_hist":          {"'h0": 5000, "'h1": 3000, "'h2": 1500, "'h3": 500},
        "time_in_state_ticks": {"'h0": 50000, "'h1": 30000, "'h2": 15000, "'h3": 5000},
        "time_in_state_ns":    {"'h0": 50000, "'h1": 30000, "'h2": 15000, "'h3": 5000}
      }
    ]
  },
  "warnings": []
}
```

(Note: v0.1 / early v0.2 emitted raw store values like
`"3"` as histogram keys and a single `time_in_state` map
in ticks. Both shapes were a regular source of confusion:
callers either assumed Verilog literals or assumed ns.
The current schema fixes both.)

When the simulation has no clock, `total_cycles` and
`clock_period_ns` are `null`.

See `reference/json-envelope` for the full schema.

## Common errors

- **`total_cycles: null`**: no clock detected. Pass
  `--clock PATTERN` or accept the tick-only summary.
- **`time_in_state` values don't sum to `total_ticks`**:
  the signal had X / Z values that don't appear in the
  histogram, or the time window (`-t`) was narrower than
  the full simulation.
- **Output truncated**: `--limit` hit. Narrow with
  `-s` or bump the limit.
- **`--virtual` rejected**: `stats` doesn't accept
  consumer options. If you need stats on a virtual
  signal, the workaround is to add the predicate as a
  rebuild step (not yet supported) or use `distance
  --stats` for time-between-events analysis.

## Examples

Activity summary for an FSM:

```bash
bwave stats sim.fst -s "*state*"
```

Toggle-rate audit of every clock signal:

```bash
bwave stats sim.fst -s "*clk*"
```

JSON for downstream analysis:

```bash
bwave stats sim.fst -s "*data_out*" --format json \
    | jq '.data.signals[] | {name, transitions, toggle_pct}'
```

Stats over a bounded window, e.g. a specific test phase:

```bash
bwave stats sim.fst -s "*" -t 1000:5000
```

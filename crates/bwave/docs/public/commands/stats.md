# bwave stats

## Semantics

Per-signal activity report. For each matching signal,
B-Wave counts transitions, computes toggle %, builds a
value histogram (how often each value held) and a
time-in-state map (how many ticks each value held).
Useful for "is this signal active?", "which state
dominates?", and as a sanity check before deeper
queries.

`stats` takes no Virtual Signals; for the time between
events on one, use `distance --stats`.

## Output shape

Text mode, one summary block per signal:

```
# Simulation: 5ns
# 1 signals analyzed

b  4-bit;  2 transitions;  2 unique values;

Toggle coverage: 25%
Value coverage: 12%

1: 80% (4ns)
0: 20% (1ns)

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

When the simulation has no clock, `total_cycles` and
`clock_period_ns` are `null`.

`bwave schema` has the full grammar.

## Examples

```bash
bwave stats sim.fst -s "*state*"
bwave stats sim.fst -s "*" -t 1000:5000
bwave stats sim.fst -s "*data_out*" --format json \
    | jq '.data.signals[] | {name, transitions, toggle_pct}'
```

## Clock-grid behavior

- The header summarizes the clock verdict and clock stops.
- Sync JSON adds per-value `time_in_state_clocked_cycles` and
  `time_in_state_stopped_cycles` maps. Both are null in async mode or without
  a clock. Text output shows the same clocked/stopped split.
- These maps, and the sync text percentages, count grid samples in the query
  window with both endpoints included. `total_cycles` counts whole elapsed
  periods, so it can differ from the sample total.
- Tick/ns occupancy stays a physical duration.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

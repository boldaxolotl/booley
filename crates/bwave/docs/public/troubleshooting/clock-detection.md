# Clock detection and regularity

Sync mode builds its cycle grid from one clock. Check which clock was picked
and whether it fits the grid:

```bash
bwave overview trace.fst
```

`--format json` puts the verdict in `clock_regularity`. `bwave stats` prints
the same header.

## Verdict reasons

| Reason | Meaning | Next step |
|---|---|---|
| `regular` | Every rising edge fits the grid. | None. |
| `stopped_stretches` | Some interior grid points have no rising edge. | Read the stop summary and query notes. |
| `off_grid_edge` | Frequency change, phase change, or jitter. The warning names the first off-grid tick. | Use `--async` with tick/time bounds. |
| `non_integer_period` | The period is fractional and the simulator rounded it to ticks. | Use `--async`. |
| `unknown_clock` | The clock goes X/Z after starting. | Inspect it with `--async`. |
| `insufficient_edges` | Fewer than two rising edges; no period to measure. | Select another clock or use `--async`. |
| `multiple_clock_periods` | Candidate clocks have different periods. | Select the intended domain with `--clock PATTERN`. |

`non_integer_period` is inferred, not proven. It needs:

- at least six gaps between rising edges;
- two adjacent gap lengths, each seen at least twice;
- under one tick of error against the straight line from the first rise to
  the last.

This recognizes tick rounding, but cannot separate it from every jitter
pattern.

## How the clock is picked

1. Candidates are one-bit signals whose hierarchical name, brackets
   stripped, contains `clk` (case-insensitive).
2. They are ordered by scope depth, then name.
3. The first one with two 0-to-1 transitions wins.

`--clock PATTERN` takes the first one-bit match in the same order. No match
exits with code 2. A match with fewer than two rising edges gives an
`unavailable` verdict.

List the candidates with `bwave list trace.fst -s "*clk*"`. Select a clock
with an unconventional name explicitly:

```bash
bwave overview trace.fst --clock phi1
bwave signal trace.fst -s state --clock core_clk
```

## When the verdict warns

The verdict never switches modes for you. Async queries with cycle tokens
(`-t 100c:200c`, `--at 10c`, markers) still use the same grid and keep the
warning. Use tick/time tokens for a view that does not depend on the clock:

```bash
bwave signal trace.fst -s state --async -t 0t:1000t
```

See `reference/sync-vs-async` for what the check cannot tell: first-gap
stops, late starts, integer-multiple slowdowns, and clock-domain ownership.

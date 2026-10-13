# Clock detection and regularity

Run `bwave overview trace.fst` or `bwave stats trace.fst -s clk` to inspect
the clock verdict; `--format json` puts it in `clock_regularity`.

| Reason | Meaning and next step |
|---|---|
| `regular` | Every real rising edge fits the grid. |
| `stopped_stretches` | Interior grid points lack rising edges; inspect the summary and query notes. |
| `off_grid_edge` | Frequency/phase change or jitter; warning names the first divergent tick. Use `--async` with tick/time bounds. |
| `non_integer_period` | Inferred quantized fractional cadence; use `--async`. |
| `unknown_clock` | Clock becomes X/Z after starting; use `--async` to inspect it. |
| `insufficient_edges` | Fewer than two rising edges, so no period can be measured. Select another clock or use async. |
| `multiple_clock_periods` | Candidate clocks have different periods. Select `--clock PATTERN` for the intended domain. |

The fractional inference requires at least six gaps, two adjacent gap lengths
with each appearing at least twice, and less than one tick of error to the
rational line through the first and last rise. This recognizes quantization,
but cannot prove simulator intent or distinguish every jitter pattern.

Auto-detection considers one-bit signals whose bracket-stripped hierarchical name contains `clk`
(case-insensitive), ordered by scope depth then name. The first candidate with two
strict 0-to-1 transitions wins. An explicit pattern uses the first matching
one-bit signal in the same order; a miss errors with exit 2, while an
insufficient-edge selection reports an unavailable verdict. Use `list -s
"*clk*"` to inspect candidates. A clock with an unconventional name can be
selected explicitly:

```bash
bwave overview trace.fst --clock phi1
bwave signal trace.fst -s state --clock core_clk
```

The grid uses the first gap even when that gap contains a clock stop. Later
edges can therefore fail the check. Late starts and final stops do not fail
regularity; only interior gaps are marked. Stops are reported only up to the
first off-grid or X/Z tick. Integer-multiple slowdowns fit the grid and look
like repeated gating. Per-signal domain ownership is not inferred.

The verdict never switches modes automatically. Async requests with cycle
tokens (`-t 100c:200c`, `--at 10c`, markers) still depend on that same grid and
retain invalid-clock warnings. Use tick/time tokens for an independent view:

```bash
bwave signal trace.fst -s state --async -t 0t:1000t
```

# bwave wave

## Semantics

Horizontal waveform table: rows are signals, columns are
cycles (or ticks in async mode). Reads like a printed
GTKWave window. Useful for spotting visual patterns:
periodic toggles, stuck signals, handshake gaps.

Without `-t`, or with an open-ended range, `--limit` bounds the column
count. Row selection with Virtual Signals: `reference/virtual-signals`.
`--marker` labels: `reference/markers`.

## Output shape

Text mode example:

```
time  0  1000  5000
   b  0     1     2
```

With markers:

```
         change
time  0    1000  5000
   b  0       1     2
```

`--format json` returns a structured envelope.

## Examples

```bash
bwave wave sim.fst -s "*state*" -s "*valid*" -s "*ready*" -t 1000:1100

bwave wave sim.fst -s "*err*" -t 1200:1500 \
    --marker err_start 1234c --marker err_done 1450c

bwave wave sim.fst --virtual "hsk = *valid & *ready" \
    -s "hsk" -s "*state%d" -t 0:200
```

## Clock-grid behavior

- A clock stop collapses into one `clk stopped A:B` column. It counts as one
  column toward `--limit` and survives `--rle`.
- Markers inside the stop appear in that column's label.
- Values inside the stop are hidden. Use `signal` or `--async` to see them.
- JSON has `columns`, edge `ticks`, `names`, and row `values`. JSON columns
  are never run-length compressed, even with `--rle`.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

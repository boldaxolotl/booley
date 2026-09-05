# bwave wave

## Synopsis

```bash
bwave wave <FST_FILE> -t START:END [-s PATTERN[%RADIX] ...]
           [--async] [--clock PAT] [--reset PAT] [--with-reset]
           [--virtual "name = expr"] [--marker NAME CYCLE]
           [--format text|json] [--limit N]
```

## Semantics

Horizontal waveform table: rows are signals, columns are
cycles (or ticks in async mode). Reads like a printed
GTKWave window. Useful for spotting visual patterns:
periodic toggles, stuck signals, handshake gaps.

Multi-bit signals render hex by default; single-bit
signals render as `_` (low) and `^` (high) over time so
the row reads like an ASCII waveform.

## Defaults and requiredness

- `<FST_FILE>` required. Like `diff` and `distance`,
  `wave` strictly requires a built `.fst` store; there is
  no raw-VCD fallback.
- `-t START:END` is effectively required: without a time
  window the output would be unbounded. Open-ended ranges
  work but you'll quickly hit `--limit`.
- `-s PATTERN` optionally selects stored and Virtual Signal
  rows. Without it, every stored and virtual row is selected.
  Wide tables truncate at terminal width.
- `--marker NAME CYCLE` is most useful here: markers
  appear as labels above the cycle header so you can
  annotate a region.

## Output shape

Text mode example:

```
# clock: tb.dut.clk  period=10ns
              100         110         120         130
              |---------- |---------- |---------- |----------
clk           ^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_^_
valid         ____________________^^^^^^^^^^^^^^^^____________
ready         ________________________^^^^^^^^^^^^^^^^^^^^^^^^
state         'h0  'h0  'h0  'h0  'h0  'h1  'h1  'h2  'h3  'h3
```

With markers:

```
              error_start            dma_done
              v                      v
              500         510         520         530
              |---------- |---------- |---------- |----------
...
```

JSON mode is **not yet implemented** for `wave`. Text mode
only.

## Common errors

- **Truncated columns**: terminal width caps how many
  cycles fit. Narrow the `-t` range or pipe through
  `less -S` and scroll horizontally.
- **No clock header, every cycle column shown**: clock
  auto-detection failed. Pass `--clock PATTERN` or check
  `troubleshooting/clock-detection`.
- **Rows missing**: pattern matched fewer signals than
  expected. The pattern is applied to both stored and Virtual
  Signal names; re-check stored names with `bwave list` and
  virtual names in the command's `--virtual` definitions.
- **Markers don't appear**: verify the cycle is inside
  the `-t` window. Markers outside the visible range are
  silently dropped.

## Examples

A 100-cycle window of FSM state plus its handshake:

```bash
bwave wave sim.fst -s "*state*" -s "*valid*" -s "*ready*" \
    -t 1000:1100
```

Annotate a window with markers:

```bash
bwave wave sim.fst -s "*err*" \
    --marker err_start 1234 --marker err_done 1450 \
    -t 1200:1500
```

Use a virtual handshake signal as a single-bit row:

```bash
bwave wave sim.fst \
    --virtual "hsk = *valid & *ready" \
    -s "hsk" -s "*state%d" \
    -t 0:200
```

Use an unselected helper to compose the displayed row:

```bash
bwave wave sim.fst \
    --virtual "valid = *req & *ready" \
    --virtual "stalled = valid & *busy" \
    -s "stalled" -t 0:200
```

Both definitions are evaluated, but only `stalled` is printed.

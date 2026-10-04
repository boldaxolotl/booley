# bwave wave

## Synopsis

```bash
bwave wave <FST_FILE> [-t START:END] [-s PATTERN[%RADIX] ...]
           [--async] [--clock PAT] [--reset PAT] [--with-reset]
           [--virtual "name = expr"] [--marker NAME TIME]
           [--format text] [--limit N]
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
- `-t START:END` is optional; `--limit` bounds the columns
  when the time window is omitted or open-ended.
- `-s PATTERN` optionally selects stored and Virtual Signal
  rows. Without it, every stored and virtual row is selected.
  Wide tables truncate at terminal width.
- `--marker NAME TIME` adds a named annotation: markers
  appear as labels above the cycle header so you can
  annotate a region. TIME accepts the same typed time
  tokens as `-t`; bare integers mean cycles in sync mode.

## Output shape

Text mode example:

```
time  0  1  5
   b  0  1  2
```

With markers:

```
         change
time  0       1  5
   b  0       1  2
```

`--format json` exits with code 2: `JSON output is not implemented for wave; use find/value/stats/list`.

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
    --marker err_start 1234c --marker err_done 1450c \
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

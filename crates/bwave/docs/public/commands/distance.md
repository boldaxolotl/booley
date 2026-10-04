# bwave distance

## Synopsis

```bash
bwave distance <FST_FILE> <PATTERN> <VALUE>
               [--to <PATTERN_B> <VALUE_B>] [--stats]
               [-t START:END]
               [--async] [--clock PAT] [--reset PAT] [--with-reset]
               [--virtual "name = expr"]
               [--format text] [--limit N]
```

## Semantics

Measure the distance (in cycles or ticks) between
matching events.

Two modes:

1. **Same-signal periods** (no `--to`): list distances
   between consecutive times `PATTERN == VALUE`. Useful
   for "how regular is this clock-divider output?" or
   "what's the gap between handshakes?".
2. **Two-event A to B latency** (`--to PAT_B VAL_B`):
   for each A event, find the next B event and report
   the latency. Useful for "request to response
   latency".

`--stats` collapses the per-pair list into summary
statistics (count, min, max, mean, median). Mean and median
use one decimal; an even median averages the two middle distances.

Both `VALUE` and `--to`'s value accept Verilog literals
and edge keywords.

`distance` strictly requires a built `.fst` store.

## Defaults and requiredness

- `<FST_FILE>`, `<PATTERN>`, `<VALUE>` required.
- `--to PAT_B VAL_B` optional; switches to two-event
  mode.
- `--stats` optional; replaces the raw pair list with
  summary stats.
- `-t START:END` time-bounds the search for events.
- `--virtual` applies.

## Output shape

Text mode, same-signal periods:

```
# same-signal: a 1
@ 1000 -> @ 4000  d=3000
@ 4000 -> @ 10000  d=6000
@ 10000 -> @ 25000  d=15000
```

Two-event mode (`--to`):

```
# A: a 1 -> B: b 2
@ 1000 -> @ 5000  d=4000
@ 4000 -> @ 5000  d=1000
```

With `--stats`:

```
# same-signal: a 1
count=3  min=3000  max=15000  mean=8000.0  median=6000.0
```

`--format json` exits with code 2: `JSON output is not implemented for distance; use find/value/stats/list`.

## Common errors

- **`requires a built waveform store`**: no VCD fallback.
  Build one first: `bwave build <vcd> -o trace.fst`.
- **B event has no matching A**: pairing is strictly
  A then the first B strictly later than A. Multiple A
  events can reuse the same B (overlapping is allowed).
  Verify expectations with `find`.
- **Distances look 10x too big**: you're in async mode
  and reading the result as cycles. Check
  the stderr pair footer for the unit or switch to sync.
- **`# no pairs found`**: no qualifying A-to-B pairs.
  Same-signal mode reports `# no pairs found (need at least
  2 events, found N)`. Run `find` for each side separately.

## Examples

How regular is the valid pulse?

```bash
bwave distance sim.fst "*valid" 'h1 --stats
```

Request-to-acknowledge latency, summarised:

```bash
bwave distance sim.fst "*req" 'h1 --to "*ack" 'h1 --stats
```

Latency between rising edges of two control signals:

```bash
bwave distance sim.fst "*enable" rising \
    --to "*done" rising
```

Latency using virtual signals on both sides:

```bash
bwave distance sim.fst \
    --virtual "issue = *req & *gnt" \
    --virtual "retire = *complete & *ack" \
    issue 'h1 --to retire 'h1 --stats
```

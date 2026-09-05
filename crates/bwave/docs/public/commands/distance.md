# bwave distance

## Synopsis

```bash
bwave distance <FST_FILE> <PATTERN> <VALUE>
               [--to <PATTERN_B> <VALUE_B>] [--stats]
               [-s PATTERN[%RADIX] ...] [-t START:END]
               [--async] [--clock PAT] [--reset PAT] [--with-reset]
               [--virtual "name = expr"]
               [--format text|json] [--limit N]
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
statistics (count, min, max, mean, median).

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
# distance: tb.dut.valid == 'h1 (consecutive matches)
A_cycle  B_cycle  delta
     42       50      8
     50       67     17
     67       89     22
# 3 intervals
```

Two-event mode (`--to`):

```
# distance: tb.dut.req == 'h1  ->  tb.dut.ack == 'h1
A_cycle  B_cycle  latency
     42       46      4
     50       55      5
     67       70      3
# 3 pairs
```

With `--stats`:

```
# distance: tb.dut.req == 'h1  ->  tb.dut.ack == 'h1
count    3
min      3
max      5
mean   4.00
median   4
```

JSON mode is **not yet implemented** for `distance`.
Text mode only.

## Common errors

- **`requires a built waveform store`**: no VCD fallback.
  Build one first: `bwave build <vcd> -o trace.fst`.
- **B event has no matching A**: pairing is strictly
  A then next-B. If A fires twice before B, the second
  A pairs with the same B (overlapping is allowed), but
  if B fires twice before any A, only the first B is
  consumed. Verify expectations with `find`.
- **Distances look 10x too big**: you're in async mode
  and reading the result as cycles. Check
  `mode`/`unit` in the header or switch to sync.
- **`--stats` says count=0**: neither A nor B matched.
  Run `find` for each side separately.

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

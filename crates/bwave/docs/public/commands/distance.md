# bwave distance

## Semantics

Measure the distance (in cycles or ticks) between
matching events.

Two modes:

1. **Same-signal periods** (no `--to`): list distances
   between consecutive times `PATTERN == VALUE`. Useful
   for "how regular is this clock-divider output?" or
   "what's the gap between handshakes?".
2. **Two-event A to B latency** (`--to PAT_B VAL_B`):
   for each A event, find the first B event strictly
   later and report the latency. Several A events can
   pair with the same B. Useful for "request to response
   latency".

`--stats` collapses the per-pair list into summary
statistics (count, min, max, mean, median). Mean and median
use one decimal; an even median averages the two middle distances.

Event values on both sides: `reference/verilog-literals`.

In both modes, synchronous events are matches on clock-edge sampled values,
labelled with their sampling cycle. Literals count every equal sample, even
when the level holds across edges; edge keywords compare consecutive samples,
with the first eligible sample serving as baseline. Glitches between samples
are invisible. `--async` observes raw transitions instead.

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

`--format json` returns a structured envelope.

## Examples

```bash
bwave distance sim.fst "*valid" 'h1 --stats
bwave distance sim.fst "*req" 'h1 --to "*ack" 'h1 --stats
bwave distance sim.fst \
    --virtual "issue = *req & *gnt" \
    --virtual "retire = *complete & *ack" \
    issue 'h1 --to retire 'h1 --stats
```

## Clock-grid behavior

Distance counts every grid cycle, including cycles inside a clock stop:

- Each pair reports the stopped cycles in `(A,B]`, e.g.
  `d=1003 cycles (1000 stopped)`.
- `--stats` adds `stopped_cycles`, summed per pair, so overlapping pairs
  count a shared stop more than once.
- Unlike `find`, literal events on stopped cycles still match.
- JSON pairs carry `from`, `to`, `elapsed`, and `stopped_cycles`; `unit` is
  `cycles` or `ticks`.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

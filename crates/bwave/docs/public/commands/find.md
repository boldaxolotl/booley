# bwave find

## Semantics

Search the store for cycles (or ticks in async mode)
where signal `PATTERN` holds `VALUE`. The result is a
list of matches with their times. `find` is the workhorse
for questions like "when did `error` first assert?" or
"every cycle the bus was stalled". `PATTERN` may match
several signals (e.g. `*err*`); each gets its own rows.
`VALUE` forms: `reference/verilog-literals`.

Synchronous queries sample values at clock edges and label matches with the
sampling cycle. Literals match every equal sample; `rising`, `falling`, and
`change` compare consecutive sampled values, with the first eligible sample
serving as baseline. Glitches between samples are invisible. `--async`
queries observe raw transitions instead.

`--first`, `--last`, `--before`, and `--after` are mutually exclusive, and
`--before`/`--after` cannot be combined with `-t`. `--count` combines with
any of them.

## Output shape

Text mode, one row per match:

```
1000 b 1
```

With `--count`:

```
1
```

With no match, stderr says `# No matches found` (with the reset cycles
skipped, if any), and at most one note follows: the value was never held in
the window, the value was held but no sampled cycle matched (`--async` sees
raw transitions), or `rising`/`falling` met multi-bit signals only. JSON
carries the note in `warnings[]`.

JSON mode emits a `findData` envelope:

```json
{
  "$schema": "...",
  "command": "find",
  "data": {
    "scope_prefix": "tb.dut",
    "pattern": "error",
    "value": "1",
    "mode": "sync",
    "unit": "cycle",
    "count": 3,
    "matches": [
      {"time": 42,  "name": "error", "value": "1"},
      {"time": 187, "name": "error", "value": "1"},
      {"time": 293, "name": "error", "value": "1"}
    ],
    "truncated": false,
    "first_only": false,
    "last_only": false,
    "count_only": false
  },
  "warnings": []
}
```

Note `value` in `matches[]` is the **raw store value**
(`"1"`, `"DEAD"`), not the Verilog literal the user typed.
The top-level `data.value` is normalized hex or an edge
keyword.

## Examples

```bash
bwave find sim.fst "*error*" 'h1 --first
bwave find sim.fst "*valid" rising --after 1000
bwave find sim.fst "*we" 'h1 --before 5000
bwave find sim.fst --virtual "hsk = *valid & *ready" hsk 'h1 --first
```

## Clock-grid behavior

- Literal level triggers (e.g. `'h1`) skip cycles inside a clock stop.
  `--include-stopped` matches them too.
- Edge and change triggers are unaffected.
- `--count`, `--first`, `--last`, and Virtual Signals follow the same rule.
- A stop overlapping the query window is reported as information, not a
  warning. JSON lists it in `clock_stopped`.
- `--sample-mode pre-edge` reads values just before each edge. Cycle numbers
  do not change.

See `reference/sync-vs-async` for the clock verdict and sampling modes.

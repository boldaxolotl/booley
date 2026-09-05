# bwave find

## Synopsis

```bash
bwave find <FST_FILE> <PATTERN> <VALUE>
           [--first | --last | --before N | --after N] [--count]
           [-t START:END]
           [--async] [--clock PAT] [--reset PAT] [--with-reset]
           [--virtual "name = expr"]
           [--format text|json] [--limit N]
```

## Semantics

Search the store for cycles (or ticks in async mode)
where signal `PATTERN` holds `VALUE`. The result is a
list of matches with their times. `find` is the workhorse
for questions like "when did `error` first assert?" or
"every cycle the bus was stalled".

`PATTERN` may match multiple signals (e.g. `*err*`).
Each gets its own row in the output.

`VALUE` is a Verilog literal (`'d255`, `'hFF`, `'b1010`,
or width-prefixed `8'd255`) **or** an edge keyword:
`rising`, `falling`, `change`. See
`reference/verilog-literals`.

## Defaults and requiredness

- `<FST_FILE>`, `<PATTERN>`, `<VALUE>` all required.
- Mode modifiers are optional, mutually exclusive:
  - `--first`: stop at first match.
  - `--last`: only the last match.
  - `--before N`: implies `--last`, equivalent to
    `-t :N --last`.
  - `--after N`: implies `--first`, equivalent to
    `-t N: --first`.
  - `--count`: print only the match count.
- `-t START:END` time-bounds the search. Cannot combine
  with `--before` / `--after` (they already imply a
  range).

## Output shape

Text mode, one row per match:

```
# find: tb.dut.error == 'h1
   42  tb.dut.error  'h1
  187  tb.dut.error  'h1
  293  tb.dut.error  'h1
# 3 matches
```

With `--count`:

```
3
```

JSON mode emits a `findData` envelope:

```json
{
  "$schema": "...",
  "command": "find",
  "data": {
    "scope_prefix": "tb.dut",
    "pattern": "error",
    "value": "'h1",
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
The literal-as-typed is preserved in the top-level
`data.value` field.

## Common errors

- **`--first and --last are mutually exclusive`**: pick
  one. Clap rejects the combo with exit 2.
- **`bare hex like 'FF' is rejected`**: values must be
  Verilog literals. Use `'hFF` or `8'hFF`. See
  `troubleshooting/radix-pitfalls`.
- **Zero matches when you expected some**: likely the
  reset phase ate them. Try `--with-reset`. Or your
  pattern matched no signals; verify with `bwave list`.
- **`'d255` produces no matches but the waveform clearly
  shows `0xFF`**: radix mismatch. Both should match,
  but only if the store value width permits. Compare to
  `8'd255` for a width-locked literal. See
  `troubleshooting/radix-pitfalls`.

## Examples

First cycle the error signal asserts:

```bash
bwave find sim.fst "*error*" 'h1 --first
```

All rising edges of `valid` after cycle 1000:

```bash
bwave find sim.fst "*valid" rising --after 1000
```

Just the count, no rows:

```bash
bwave find sim.fst "*state*" 'd3 --count
```

The last write before a checkpoint:

```bash
bwave find sim.fst "*we" 'h1 --before 5000
```

Find with a virtual predicate (handshake firing):

```bash
bwave find sim.fst \
    --virtual "hsk = *valid & *ready" \
    hsk 'h1 --first
```

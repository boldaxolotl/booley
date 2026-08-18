# Radix pitfalls

Two radix systems coexist in B-Wave: **display radix**
(how values render on screen) and **comparison
literal** (how you specify a value to search for).
Mixing them up produces queries that fail silently or
return surprising results.

## The two systems

### Display radix: `%RADIX` suffix on `-s`

Attached to signal patterns in `-s`:

```bash
bwave signal sim.fst -s "data_out%d"   # render decimal
bwave signal sim.fst -s "flags%b"      # render binary
bwave signal sim.fst -s "addr%h"       # render hex (default)
```

Affects text-mode output only. JSON output ignores the
suffix (raw store values, no prefix).

The suffix must be **immediately after** the pattern,
no space: `data%h` not `data %h`. The `%` is part of
the pattern argument as far as the shell is concerned;
quote the whole thing to avoid the shell interpreting
`%`: `"data%h"`.

Only `%h`, `%d` and `%b` exist. Anything else — `%u`,
`%x`, `%o` — is a hard error (exit 2). `%` cannot appear
in a Verilog identifier, so a mistyped suffix leaves a
pattern that matches nothing and used to be dropped from
a multi-`-s` query in silence: a nine-signal `wave` once
rendered two rows for exactly that reason.

### Comparison literal: value in `find` / `sample` / etc

The value positional argument:

```bash
bwave find sim.fst "*data" 'd255      # match decimal 255
bwave find sim.fst "*data" 'hFF       # match hex FF (same value!)
bwave find sim.fst "*data" 8'd255     # match 8-bit decimal 255
```

`'d255`, `'hFF`, `'b11111111`, and `8'd255` all match
the same store value (assuming the stored signal is wide
enough). The base prefix only tells the parser how to
interpret the digits. It does not constrain what value
ends up compared.

## Common pitfalls

### Pitfall 1: bare hex rejected

```bash
bwave find sim.fst "*error*" FF
# ERROR: cannot parse value 'FF' — use Verilog literal
```

`FF` is not a Verilog literal (no base prefix). Fix:

```bash
bwave find sim.fst "*error*" 'hFF
```

See `reference/verilog-literals` for the full grammar.

### Pitfall 2: display radix doesn't affect search

You see `data_out` displayed in decimal (`-s
"data_out%d"`) and assume you can search with bare
decimal:

```bash
bwave signal sim.fst -s "data_out%d" -t 0:100
# ... shows data_out values 0, 1, 2, ..., 255 ...
bwave find sim.fst data_out 255
# ... uh, did this match?
```

`255` (bare decimal) is accepted and matches. But this
worked despite the display-radix flag, not because of
it. The display radix is purely cosmetic.

### Pitfall 3: width-prefixed literal mismatch

```bash
bwave find sim.fst "*one_bit_sig" 8'd1
```

If `*one_bit_sig` is a 1-bit signal, the 8-bit literal
prefix may produce a width-mismatch warning or no
match, depending on the stored width. Use a width that
matches the signal, or drop the width prefix:

```bash
bwave find sim.fst "*one_bit_sig" 'h1
bwave find sim.fst "*one_bit_sig" 1'b1
```

### Pitfall 4: searching for a "decimal" display value

You see `state%d` showing `5` and search:

```bash
bwave find sim.fst "*state" 5         # ok, bare decimal
bwave find sim.fst "*state" 'd5       # ok, explicit decimal
bwave find sim.fst "*state" '5'       # broken — shell quoting
```

The third form: `'5'` is shell-quoted, becomes `5`,
behaves like the first. Confusing because the
Verilog-literal prefix is also `'`. Be explicit:
`'d5`.

### Pitfall 5: JSON envelope value is raw, no prefix

JSON output value strings are raw store values, not
Verilog literals:

```json
{"name": "state", "value": "5"}        // NOT "'d5"
{"name": "data", "value": "DEADBEEF"}  // NOT "'hDEADBEEF"
```

If your JSON post-processor expects Verilog literals,
adapt to the v0.2 contract: prefix-free strings, the
caller decides the radix. See
`reference/json-envelope`.

### Pitfall 6: stats histogram keys (v0.2.1 change)

In v0.1 / early v0.2 the JSON `stats` output had raw
`value_hist` keys like `"5"` / `"FF"`. As of v0.2.1
these are Verilog literals matching the display radix:
`"'d5"` / `"'hFF"`. If you have older tooling that
reads the histogram, update the key parsing. See
`reference/json-envelope`.

The Python wrapper help text in older Booley packages
still describes the v0.1 behaviour. Disregard it.

## Quick rules

- **Display radix** lives on `-s` patterns: `-s
  "sig%d"`. Cosmetic only.
- **Comparison literals** in `find` / `sample` /
  `distance` / `stuck` / virtual expressions use
  Verilog syntax with a mandatory base prefix.
- Bare integers (`5`, `255`) are decimal.
- Bare hex (`FF`, `DEAD`) is rejected.
- JSON output uses raw store values, no Verilog
  prefix (`stats` histogram keys are the exception).
- The two systems are independent: setting display
  radix doesn't change what `find` matches.

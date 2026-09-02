# JSON envelope

When `--format json` is passed to a subcommand that
supports it, B-Wave wraps the payload in a canonical
envelope. The envelope shape is the same across all
commands; only the `data` field varies.

## Envelope

```json
{
  "$schema": "https://raw.githubusercontent.com/boldaxolotl/booley/v0.2.11/crates/bwave/schema/bwave.json",
  "command": "<subcommand>",
  "data":    { ... },
  "warnings": ["..."]
}
```

| Field | Type | Notes |
|---|---|---|
| `$schema` | string | URL of the JSON Schema this payload conforms to. Pinned to the release tag (currently `v0.2.11`). |
| `command` | string | Subcommand name. Currently one of `list`, `value`, `find`, `stats`. |
| `data` | object | Per-command payload. See per-command docs for the shape. |
| `warnings` | array of string | Diagnostics that text mode would emit to stderr as `# WARNING: ...` lines. Empty array on clean runs. |

## Subcommands that emit JSON

As of v0.2, only four commands respect `--format
json`:

- `list`: see `commands/list`
- `value`: see `commands/value`
- `find`: see `commands/find`
- `stats`: see `commands/stats`

The other subcommands accept `--format json` on the CLI
(it's in the global options) but **still emit text**.
This is a known gap; track Phase 2+ work for expansion.

Commands that **do not** currently emit JSON:

- `signal`, `wave`, `sample`, `diff`, `distance`,
  `stuck`: text mode only, even with `--format json`.
- `build`: produces a binary file, no envelope.
- `schema`: produces the JSON Schema itself (no
  envelope around it; it *is* the schema).

## Per-command `data` shapes

Full grammar lives in the schema. Get it via:

```bash
bwave schema | jq .
```

Quick summary:

### `list` -> `listData`

```json
{
  "scope_prefix": "tb.dut",
  "signals": [
    {"name": "clk", "width": 1, "var_type": "wire"}
  ]
}
```

### `value` -> `valueData`

```json
{
  "scope_prefix": "tb.dut",
  "mode": "sync",
  "at": 1234,
  "at_unit": "cycle",
  "target_tick": 12345,
  "time_label": "cycle 1234 (tick 12345)",
  "signals": [
    {"name": "state", "value": "2"}
  ]
}
```

### `find` -> `findData`

```json
{
  "scope_prefix": "tb.dut",
  "pattern": "error",
  "value": "'h1",
  "mode": "sync",
  "unit": "cycle",
  "count": 3,
  "matches": [
    {"time": 42, "name": "error", "value": "1"}
  ],
  "truncated": false,
  "first_only": false,
  "last_only": false,
  "count_only": false
}
```

### `stats` -> `statsData`

```json
{
  "simulation_ns": 100000,
  "total_ticks": 100000,
  "total_cycles": 10000,
  "clock_period_ns": 10,
  "signals": [
    {
      "name": "state",
      "width": 3,
      "transitions": 42,
      "toggle_pct": 0.42,
      "value_pct": 0.50,
      "value_hist":          {"'h0": 5000, "'h1": 3000, "'h2": 1500, "'h3": 500},
      "time_in_state_ticks": {"'h0": 50000, "'h1": 30000, "'h2": 15000, "'h3": 5000},
      "time_in_state_ns":    {"'h0": 50000, "'h1": 30000, "'h2": 15000, "'h3": 5000}
    }
  ]
}
```

## Value encoding across JSON shapes

The `value` field on `signalValue` and `findMatch` is the
**raw store value** (no Verilog prefix, no padding, no
radix formatting). Examples:

- A 3-bit FSM state of 5 appears as `"5"`, not `"'d5"`
  or `"'h5"` or `"3'b101"`.
- A 32-bit `0xDEADBEEF` appears as `"DEADBEEF"`, not
  `"'hDEADBEEF"`.
- X / Z values appear as `"X"` / `"Z"` literally.

`stats` is the exception: as of v0.2.1, `value_hist` and
`time_in_state_ticks` map keys are **Verilog literals**
that match the text-mode rendering for that signal's
radix (`'hFF`, `'d255`, `'b101`). This makes it possible
to grep the same key in both modes. The prior raw form
was a regular source of "wait, which radix is this?"
confusion in scripts.

(Note: older releases described `value_hist` keys as raw
store values, and emitted a single `time_in_state` map
in ticks with no unit in the field name. Both have been
fixed; downstream consumers should migrate to
`time_in_state_ticks` / `time_in_state_ns`.)

## Warnings

Anything text mode would print to stderr as `# WARNING:
...` shows up in the `warnings` array. Common warnings:

- Clock auto-detection found no match (sync mode
  degraded to ticks).
- `--limit` capped the output (`truncated: true` in
  `findData`, plus a warning in `warnings`).
- A virtual signal failed to evaluate (binary still
  exits non-zero).

A clean run has `"warnings": []`.

## Schema versioning

The `$schema` URL is pinned to the release tag. v0.2
emits `.../v0.2.11/crates/bwave/schema/bwave.json`. Future releases
will bump the URL alongside any schema changes; older
URLs will continue to resolve to their historical
schema documents.

For local validation:

```bash
bwave schema > /tmp/bwave.json
bwave list sim.fst --format json \
    | jsonschema -i /dev/stdin /tmp/bwave.json
```

(Python `jsonschema` package required.)

## Getting the full schema

```bash
bwave schema
```

Prints the embedded JSON Schema to stdout. The schema
is baked into the binary at build time, so the output
always matches the installed version.

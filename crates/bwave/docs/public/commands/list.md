# bwave list

## Synopsis

```bash
bwave list <FST_FILE> [-s PATTERN ...] [--tree] [--format text|json]
```

## Semantics

List signals (or scopes) in an `.fst` store. No values,
just names, widths, and VCD var types. Use this first
when you don't yet know what's in the store or you're
hunting for the right pattern to feed to other queries.

Output is sorted hierarchically and indented; a common
scope prefix (the deepest scope shared by all matches) is
stripped from each line and shown once as `# scope:` on
stderr.

## Defaults and requiredness

- `<FST_FILE>` is required.
- `-s PATTERN` is optional and repeatable. Omitted means
  `*` (everything).
- `--tree` lists scopes only (modules), no leaf signals.
  Useful for a high-level structural view.
- Standard global options apply but `--time`, `--clock`,
  `--reset`, `--with-reset` are no-ops here (there are no
  values to time-bound).
- `--limit N` bounds the printed leaf-signal count (the
  Booley wrapper defaults it to 400 so a listing fits the
  MCP output window). Truncation is announced on stderr;
  `--tree` is never truncated.

## Output shape

Text mode: indented tree, one signal per line, columns are
`NAME WIDTH VAR_TYPE`. A footer on stderr reports the
match count and a hint.

```
# scope: tb.dut
clk            1 wire
rst_n          1 wire
state          3 reg
data_out      32 wire
# 4 signals — narrow with -s PATTERN or use --tree
```

A store that contains no signals at all (header-only
trace, e.g. from a Verilator sim traced via the auto
`--main`) replaces the footer with `ERROR: waveform
store has no signals ...` — still exit 0, so `list`
remains usable to diagnose the store, unlike the query
subcommands which exit 2 on it. A `-s` pattern that
merely matches none of the store's N signals keeps the
plain `# 0 signals` footer.

JSON mode produces a `listData` envelope:

```json
{
  "$schema": "...",
  "command": "list",
  "data": {
    "scope_prefix": "tb.dut",
    "signals": [
      {"name": "clk",      "width": 1,  "var_type": "wire"},
      {"name": "state",    "width": 3,  "var_type": "reg"},
      {"name": "data_out", "width": 32, "var_type": "wire"}
    ]
  },
  "warnings": []
}
```

See `bwave docs show reference/json-envelope` for the full
schema.

## Common errors

- **Empty output, no error**: pattern matched nothing.
  Bare names *suffix-match*: `data` matches `*.data` but
  not `data_out`. Use `*data*` for substring. See
  `troubleshooting/empty-results`.
- **`requires a built waveform store`**: pass an `.fst`
  store, not a `.vcd`. Build one first:
  `bwave build <vcd> -o trace.fst`.
- **Garbage signal names**: wrong `--scope` at build
  time. Rebuild with the right scope (or with no scope to
  capture everything).

## Examples

List every signal in the store:

```bash
bwave list sim.fst
```

Look for AXI-related signals:

```bash
bwave list sim.fst -s "axi*valid" -s "axi*ready"
```

Module-tree only, no leaves, quick structural view:

```bash
bwave list sim.fst --tree
```

Pipe to JSON for downstream tooling:

```bash
bwave list sim.fst -s "*fsm*" --format json | jq '.data.signals[].name'
```

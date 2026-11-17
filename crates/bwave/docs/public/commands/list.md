# bwave list

## Semantics

List signals (or, with `--tree`, scopes) in a store: names, widths, and
VCD var types, no values. Run it first, to learn what the store holds and
which pattern to pass to other queries (`reference/patterns`).

Output is an indented hierarchical tree. The deepest scope shared by all
matches is stripped from each line and printed once as `# scope:` on
stderr.

`--clock` changes only the reported clock metadata; `--async` and
`--with-reset` have no effect, and `--reset` is only checked: a bad pattern
exits 2. `--limit` never truncates `--tree`. The
Booley wrapper defaults `--limit` to 400.

## Output shape

Text mode: indented tree on stdout. A scope row is
`NAME (N signals)`; a signal row is `NAME VAR_TYPE WIDTH`.
The scope prefix and a footer with the match count and a
hint go to stderr.

```
clk                            wire     1-bit
data[7:0]                      wire     8-bit
rstn                           wire     1-bit
# scope: tb
# 3 signals — narrow with -s PATTERN or use --tree
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

`bwave schema` has the full grammar.

## Examples

```bash
bwave list sim.fst
bwave list sim.fst -s "axi*valid" -s "axi*ready"
bwave list sim.fst -s "*fsm*" --format json | jq '.data.signals[].name'
```
